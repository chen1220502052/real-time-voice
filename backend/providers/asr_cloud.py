"""asr cloud provider boundary."""

import asyncio
import functools
import os
import threading
from collections import deque
from typing import Optional
from ..protocol import PCM_100MS_BYTES
from .common import AsrUnavailable, AsrVadResult
from .asr_local import FunAsrVadAdapter


class DashScopeAsrVadAdapter:
    """Local FSMN-VAD plus Alibaba Cloud Paraformer real-time ASR.

    FSMN-VAD still consumes every 100 ms packet and remains authoritative for
    the public ``status=0/1/2/3`` contract. Only an active utterance (plus a
    short pre-roll) is sent to DashScope, so listening silence is not uploaded
    or billed. A fresh cloud Recognition object is used for every utterance,
    as required by the DashScope SDK lifecycle.
    """

    PRE_ROLL_PACKETS = 5
    _vad_pool = {}
    _vad_pool_lock = threading.Lock()

    def __init__(
        self,
        device: Optional[str] = None,
        recognition_factory=None,
        callback_base=None,
    ) -> None:
        self.device = device or os.getenv("ASR_DEVICE", "cpu")
        self.vad_model_name = os.getenv("FUNASR_VAD_MODEL", "fsmn-vad")
        self.model = os.getenv("DASHSCOPE_ASR_MODEL", "paraformer-realtime-v2")
        self._numpy = None
        self._vad_model = None
        self._vad_cache = {}
        self._recognition_factory = recognition_factory
        self._callback_base = callback_base
        self._recognition = None
        self._pre_roll = deque(maxlen=self.PRE_ROLL_PACKETS)
        self._in_speech = False
        self._start_time: Optional[int] = None
        self._latest_text = ""
        self._final_text = ""
        self._cloud_error: Optional[str] = None
        self._result_lock = threading.Lock()
        self._fallback_silence_packets = 0
        self._fallback_end_silence_packets = max(
            1, int(os.getenv("VAD_FALLBACK_END_SILENCE_MS", "800")) // 100
        )
        self._fallback_rms_threshold = float(os.getenv("VAD_FALLBACK_RMS", "0.006"))

    async def feed(self, pcm: bytes, speech_time: int) -> AsrVadResult:
        if len(pcm) != PCM_100MS_BYTES:
            raise ValueError("expected 100 ms PCM")
        loop = asyncio.get_running_loop()
        if getattr(self, "_unusable", False):
            raise AsrUnavailable("ASR transport requires a new session")
        try:
            return await loop.run_in_executor(None, functools.partial(self._feed_sync, pcm, speech_time))
        except Exception as original:
            try:
                await asyncio.wait_for(self.reset(), 3)
            except asyncio.TimeoutError:
                # Cancelling the await cannot stop an SDK worker thread.
                # Never reuse this adapter while old callbacks may still run.
                self._unusable = True
                raise AsrUnavailable("ASR recovery timed out; a new session is required") from None
            except Exception:
                # reset may surface the previous cloud error AFTER safely
                # clearing the utterance. Only failed cleanup is fatal.
                thread = getattr(self, "_send_thread", None)
                if self._in_speech or self._recognition is not None or (thread and thread.is_alive()):
                    self._unusable = True
                    raise AsrUnavailable("ASR recovery could not close its transport") from None
            raise original

    async def warmup(self) -> None:
        # Validate configuration and warm the local VAD without opening a paid
        # cloud recognition stream.
        loop = asyncio.get_running_loop()
        def prime():
            self._ensure_dependencies()
            # Load feature extraction / inference kernels before 100ms live
            # packets arrive, not merely the checkpoint. Never start ASR.
            for _ in range(3):
                self._generate_vad(b"\x00" * PCM_100MS_BYTES)
            self._vad_cache = {}
        await loop.run_in_executor(None, prime)

    def _feed_sync(self, pcm: bytes, speech_time: int) -> AsrVadResult:
        self._ensure_dependencies()
        events = self._generate_vad(pcm)
        start, end = FunAsrVadAdapter._parse_vad_events(events)
        self._pre_roll.append(pcm)

        if start is not None and not self._in_speech:
            self._in_speech = True
            self._start_time = start
            self._start_recognition()
            for frame in self._pre_roll:
                self._send_cloud_audio(bytes(frame))
            if end is not None:
                text = self._stop_recognition()
                result = AsrVadResult(speech_time, 3, start, end, text)
                self._finish_utterance()
                return result
            return AsrVadResult(speech_time, 1, start, speech_time, self._get_latest_text())

        if self._in_speech:
            # The start packet was already sent as part of pre-roll.
            if start is None:
                self._send_cloud_audio(bytes(pcm))
            if end is None:
                end = FunAsrVadAdapter._fallback_end_time(self, pcm, speech_time)
            if end is not None:
                text = self._stop_recognition()
                result = AsrVadResult(speech_time, 3, self._start_time, end, text)
                self._finish_utterance()
                return result
            return AsrVadResult(speech_time, 2, self._start_time, speech_time, self._get_latest_text())

        return AsrVadResult(speech_time, 0)

    def _ensure_dependencies(self) -> None:
        self._ensure_vad()
        if self._recognition_factory is None:
            api_key = os.getenv("DASHSCOPE_API_KEY")
            if not api_key:
                raise RuntimeError("DASHSCOPE_API_KEY is required when ASR_BACKEND=dashscope")
            try:
                import dashscope
                from dashscope.audio.asr import Recognition, RecognitionCallback
            except ImportError as exc:
                raise RuntimeError(
                    "DashScope SDK is not installed. Install with "
                    "`python3.11 -m pip install -e '.[cloud-asr]'`."
                ) from exc
            dashscope.api_key = api_key
            websocket_url = os.getenv("DASHSCOPE_WEBSOCKET_URL")
            workspace_id = os.getenv("DASHSCOPE_WORKSPACE_ID")
            if not websocket_url and workspace_id:
                websocket_url = f"wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
            if websocket_url:
                dashscope.base_websocket_api_url = websocket_url
            self._recognition_factory = Recognition
            self._callback_base = RecognitionCallback

    def _ensure_vad(self) -> None:
        if self._vad_model is None:
            try:
                import numpy as np
                from funasr import AutoModel
            except ImportError as exc:
                raise RuntimeError(
                    "Local FSMN-VAD is required for DashScope ASR. Install with "
                    "`python3.11 -m pip install -e '.[asr,cloud-asr]'`."
                ) from exc
            vad_model_path = FunAsrVadAdapter._resolve_local_model(self.vad_model_name)
            key = (self.device, vad_model_path)
            with self._vad_pool_lock:
                vad = self._vad_pool.get(key)
                if vad is None:
                    # A tiny streaming VAD must not compete with ONNX TTS
                    # using a full-machine native thread pool every 100ms.
                    vad = AutoModel(model=vad_model_path, device=self.device, disable_update=True,
                                    disable_pbar=True,
                                    ncpu=max(1, min(4, int(os.getenv("VAD_CPU_THREADS", "1")))))
                    self._vad_pool[key] = vad
            self._numpy = np
            self._vad_model = vad

    def _generate_vad(self, pcm: bytes):
        samples = self._numpy.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        result = self._vad_model.generate(
            input=samples, cache=self._vad_cache,
            **FunAsrVadAdapter._vad_generate_kwargs(self, False),
        )
        return result[0].get("value", []) if result else []

    def _start_recognition(self) -> None:
        with self._result_lock:
            self._latest_text = ""
            self._final_text = ""
            self._cloud_error = None
        adapter = self
        callback_base = self._callback_base or object

        class Callback(callback_base):
            def on_event(self, result) -> None:
                adapter._on_cloud_event(result)

            def on_error(self, result) -> None:
                adapter._on_cloud_error(result)

            def on_complete(self) -> None:
                return None

            def on_open(self) -> None:
                return None

            def on_close(self) -> None:
                return None

        self._recognition = self._recognition_factory(
            model=self.model,
            format="pcm",
            sample_rate=16_000,
            language_hints=["zh"],
            semantic_punctuation_enabled=False,
            callback=Callback(),
        )
        self._recognition.start()

    def _send_cloud_audio(self, pcm: bytes) -> None:
        self._recognition.send_audio_frame(pcm)

    def _on_cloud_event(self, result) -> None:
        sentence = result.get_sentence() or {}
        text = str(sentence.get("text", "")).strip()
        if not text:
            return
        with self._result_lock:
            self._latest_text = text
            if sentence.get("sentence_end") or sentence.get("end_time") is not None:
                self._final_text = text

    def _on_cloud_error(self, result) -> None:
        code = getattr(result, "code", None) or "unknown_error"
        detail = getattr(result, "message", None) or "DashScope returned no error message"
        message = f"{code}: {detail}"
        with self._result_lock:
            self._cloud_error = message

    def _get_latest_text(self) -> str:
        with self._result_lock:
            return self._latest_text

    def peek_partial(self) -> str:
        """Nonblocking view of cloud updates arriving during SDK finalization."""
        with self._result_lock:
            return self._latest_text if self._in_speech and not self._cloud_error else ''

    def _stop_recognition(self) -> str:
        recognition = self._recognition
        if recognition is not None:
            recognition.stop()
        with self._result_lock:
            if self._cloud_error:
                raise RuntimeError(f"DashScope ASR failed: {self._cloud_error}")
            return (self._final_text or self._latest_text).strip()

    def _finish_utterance(self) -> None:
        self._recognition = None
        self._in_speech = False
        self._start_time = None
        self._pre_roll.clear()
        self._fallback_silence_packets = 0
        with self._result_lock:
            self._latest_text = ""
            self._final_text = ""
            self._cloud_error = None

    async def reset(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            if self._recognition is not None:
                await loop.run_in_executor(None, self._stop_recognition)
        finally:
            # Reset is also used during hang-up. Even if a cloud SDK reports a
            # late task/connection error while stopping, the adapter must not
            # retain a stale recognition object for the next session.
            self._finish_utterance()
            self._vad_cache = {}

