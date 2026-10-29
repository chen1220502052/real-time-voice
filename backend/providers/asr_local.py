"""asr local provider boundary."""

import asyncio
import functools
import math
import os
import struct
import threading
from collections import deque
from pathlib import Path
from typing import Optional
from ..protocol import PCM_100MS_BYTES
from .common import AsrVadResult, normalized_pcm16_rms


class VadAsrStub:
    """Deterministic VAD/ASR double for protocol demos and fault injection."""

    def __init__(self, threshold: Optional[float] = None) -> None:
        # The tone-based demo TTS can leak back into laptop microphones.  A
        # conservative default avoids self-trigger loops while explicit tests
        # and demos can still lower the threshold.
        self.threshold = threshold if threshold is not None else float(os.getenv("DEMO_VAD_THRESHOLD", "0.04"))
        self.in_speech = False
        self.start_time: Optional[int] = None
        self.final_text = os.getenv("DEMO_ASR_TEXT", "你好")
        self.delay_ms = max(0, int(os.getenv("DEMO_ASR_DELAY_MS", "0")))
        self.fail = os.getenv("DEMO_ASR_ERROR", "0") == "1"

    async def feed(self, pcm: bytes, speech_time: int) -> AsrVadResult:
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        if self.fail:
            raise RuntimeError("Stub ASR error requested by DEMO_ASR_ERROR")
        if len(pcm) != PCM_100MS_BYTES:
            raise ValueError("expected 100 ms PCM")
        samples = struct.unpack("<{}h".format(len(pcm) // 2), pcm)
        rms = math.sqrt(sum((sample / 32768.0) ** 2 for sample in samples) / len(samples))
        voiced = rms >= self.threshold
        if voiced and not self.in_speech:
            self.in_speech = True
            self.start_time = speech_time - 100
            return AsrVadResult(speech_time, 1, self.start_time, speech_time)
        if voiced:
            return AsrVadResult(speech_time, 2, self.start_time, speech_time)
        if self.in_speech:
            result = AsrVadResult(speech_time, 3, self.start_time, speech_time - 100, self.final_text)
            self.in_speech = False
            self.start_time = None
            return result
        return AsrVadResult(speech_time, 0)

    async def reset(self) -> None:
        self.in_speech = False
        self.start_time = None

    async def warmup(self) -> None:
        return None


class FunAsrVadAdapter:
    """Streaming ``fsmn-vad`` + ``paraformer-zh-streaming`` adapter.

    The websocket protocol remains at 100 ms.  FSMN VAD consumes every such
    packet, while Paraformer receives the model's recommended 600 ms chunks.
    A VAD end event flushes the remaining ASR cache, so only status=3 is used
    as the authoritative, complete utterance text.

    Models are imported and loaded lazily.  This keeps ``python -m
    backend.app`` usable for protocol work until the optional ASR extra has
    been installed, and prevents a multi-hundred-MB model load at import time.
    """

    ASR_CHUNK_SAMPLES = 9600  # FunASR chunk_size=[0, 10, 5] => 600 ms at 16 kHz
    PRE_ROLL_PACKETS = 5
    # Model weights are process-level resources. Each call/session still owns
    # separate streaming caches below, but must not reload the same 220M ASR
    # model on every dial-up.
    _model_pool = {}
    _model_pool_lock = threading.Lock()

    def __init__(self, device: Optional[str] = None) -> None:
        self.device = device or os.getenv("ASR_DEVICE", "cpu")
        self.vad_model_name = os.getenv("FUNASR_VAD_MODEL", "fsmn-vad")
        self.asr_model_name = os.getenv("FUNASR_ASR_MODEL", "paraformer-zh-streaming")
        self._vad_model = None
        self._asr_model = None
        self._numpy = None
        self._vad_cache = {}
        self._asr_cache = {}
        self._pre_roll = deque(maxlen=self.PRE_ROLL_PACKETS)
        self._asr_pcm = bytearray()
        self._in_speech = False
        self._start_time: Optional[int] = None
        self._text_parts = []
        self._fallback_silence_packets = 0
        self._fallback_end_silence_packets = max(
            1, int(os.getenv("VAD_FALLBACK_END_SILENCE_MS", "800")) // 100
        )
        self._fallback_rms_threshold = float(os.getenv("VAD_FALLBACK_RMS", "0.006"))

    async def feed(self, pcm: bytes, speech_time: int) -> AsrVadResult:
        if len(pcm) != PCM_100MS_BYTES:
            raise ValueError("expected 100 ms PCM")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self._feed_sync, pcm, speech_time))

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_models)

    def _feed_sync(self, pcm: bytes, speech_time: int) -> AsrVadResult:
        self._ensure_models()
        vad_events = self._generate_vad(pcm, is_final=False)
        start, end = self._parse_vad_events(vad_events)

        # Preserve a small amount of leading context because a streaming VAD
        # normally reports its start only after seeing the speech onset.
        self._pre_roll.append(pcm)
        if start is not None and not self._in_speech:
            self._in_speech = True
            self._start_time = start
            self._text_parts = []
            self._asr_pcm = bytearray(b"".join(self._pre_roll))
            self._drain_asr(is_final=False)
            if end is None:
                return AsrVadResult(speech_time, 1, start, speech_time, "")

        if self._in_speech:
            # The start packet was included in the pre-roll; subsequent
            # packets are appended one by one.
            if start is None:
                self._asr_pcm.extend(pcm)
            self._drain_asr(is_final=False)
            if end is None:
                end = self._fallback_end_time(pcm, speech_time)
            if end is not None:
                self._drain_asr(is_final=True)
                text = "".join(self._text_parts).strip()
                result = AsrVadResult(speech_time, 3, self._start_time, end, text)
                self._finish_utterance()
                return result
            return AsrVadResult(speech_time, 2, self._start_time, speech_time, "".join(self._text_parts))

        return AsrVadResult(speech_time, 0)

    def _fallback_end_time(self, pcm: bytes, speech_time: int) -> Optional[int]:
        """Bound an utterance when FSMN-VAD starts but never emits its end."""
        if normalized_pcm16_rms(pcm) <= self._fallback_rms_threshold:
            self._fallback_silence_packets += 1
        else:
            self._fallback_silence_packets = 0
        if self._fallback_silence_packets < self._fallback_end_silence_packets:
            return None
        return max(self._start_time or 0, speech_time - self._fallback_silence_packets * 100)

    def _ensure_models(self) -> None:
        if self._vad_model is not None:
            return
        try:
            import numpy as np
            from funasr import AutoModel
        except ImportError as exc:
            raise RuntimeError(
                "FunASR is not installed. Install with `python3 -m pip install -e '.[asr]'` "
                "and start with ASR_BACKEND=funasr."
            ) from exc
        vad_model_path = self._resolve_local_model(self.vad_model_name)
        asr_model_path = self._resolve_local_model(self.asr_model_name)
        key = (self.device, vad_model_path, asr_model_path)
        with self._model_pool_lock:
            pooled = self._model_pool.get(key)
            if pooled is None:
                pooled = (
                    AutoModel(model=vad_model_path, device=self.device, disable_update=True),
                    AutoModel(model=asr_model_path, device=self.device, disable_update=True),
                )
                self._model_pool[key] = pooled
        self._numpy = np
        self._vad_model, self._asr_model = pooled

    @staticmethod
    def _resolve_local_model(model_name: str) -> str:
        """Use downloaded ModelScope snapshots without an online cache check."""
        supplied = Path(model_name).expanduser()
        if supplied.is_dir():
            return str(supplied)
        snapshot_names = {
            "fsmn-vad": "iic--speech_fsmn_vad_zh-cn-16k-common-pytorch",
            "paraformer-zh-streaming": "iic--speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online",
        }
        cached_name = snapshot_names.get(model_name)
        if cached_name:
            snapshot = Path.home() / ".cache" / "modelscope" / "models" / cached_name / "snapshots" / "master"
            if snapshot.is_dir():
                return str(snapshot)
        return model_name

    def _vad_generate_kwargs(self, is_final: bool) -> dict:
        kwargs = {"is_final": is_final, "chunk_size": 100}
        # Explicit endpointing threshold: without it the model's dynamic
        # schedule keeps short utterances open for up to 2s of trailing
        # silence and the RMS fallback (800ms) becomes the effective endpoint.
        kwargs["max_end_silence_time"] = max(
            200, min(6000, int(os.getenv("VAD_MAX_END_SILENCE_MS", "600")))
        )
        noise_threshold = os.getenv("VAD_SPEECH_NOISE_THRESHOLD")
        if noise_threshold:
            kwargs["speech_noise_thres"] = float(noise_threshold)
        return kwargs

    def _generate_vad(self, pcm: bytes, is_final: bool):
        samples = self._numpy.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        result = self._vad_model.generate(input=samples, cache=self._vad_cache,
                                          **self._vad_generate_kwargs(is_final))
        return result[0].get("value", []) if result else []

    @staticmethod
    def _parse_vad_events(events):
        start = end = None
        for event in events:
            if not isinstance(event, (list, tuple)) or len(event) != 2:
                continue
            begin, finish = event
            if begin is not None and begin >= 0 and finish == -1:
                start = int(begin)
            elif finish is not None and finish >= 0:
                # FSMN-VAD emits [-1, end] for an end-only event and
                # [begin, end] when a short utterance is completed together.
                if begin is not None and begin >= 0:
                    start = int(begin)
                end = int(finish)
        return start, end

    def _drain_asr(self, is_final: bool) -> None:
        while len(self._asr_pcm) >= self.ASR_CHUNK_SAMPLES * 2:
            chunk = bytes(self._asr_pcm[: self.ASR_CHUNK_SAMPLES * 2])
            del self._asr_pcm[: self.ASR_CHUNK_SAMPLES * 2]
            self._append_asr_result(chunk, is_final=False)
        if is_final:
            # Paraformer needs one final invocation even when its remaining
            # audio is shorter than 600 ms to emit the tail of the sentence.
            tail = bytes(self._asr_pcm)
            self._asr_pcm.clear()
            self._append_asr_result(tail, is_final=True)

    def _append_asr_result(self, pcm: bytes, is_final: bool) -> None:
        if not pcm and not is_final:
            return
        samples = self._numpy.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        result = self._asr_model.generate(
            input=samples,
            cache=self._asr_cache,
            is_final=is_final,
            chunk_size=[0, 10, 5],
            encoder_chunk_look_back=4,
            decoder_chunk_look_back=1,
        )
        for item in result or []:
            text = str(item.get("text", "")).strip()
            if text:
                self._text_parts.append(text)

    def _finish_utterance(self) -> None:
        self._in_speech = False
        self._start_time = None
        self._asr_pcm.clear()
        self._pre_roll.clear()
        self._asr_cache = {}
        self._text_parts = []
        self._fallback_silence_packets = 0

    async def reset(self) -> None:
        self._finish_utterance()
        self._vad_cache = {}

