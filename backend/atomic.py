import asyncio
import audioop
import functools
import json
import math
import os
import struct
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

import aiohttp


PICK_UP_SIGNAL = "pick_up"
OPENING_MESSAGE = "您好，我是猫猫，您有什需要帮助的吗"


def _is_pick_up(messages: Iterable[dict]) -> bool:
    return any(
        item.get("role") == "signal" and item.get("content", item.get("message")) == PICK_UP_SIGNAL
        for item in messages
    )

from .protocol import PCM_100MS_BYTES


@dataclass
class AsrVadResult:
    speech_time: int
    status: int
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    text: str = ""


class VadAsrStub:
    """VAD-only placeholder. Replace with Paraformer + fsmn-vad adapter."""

    def __init__(self, threshold: float = 0.015) -> None:
        self.threshold = threshold
        self.in_speech = False
        self.start_time: Optional[int] = None

    async def feed(self, pcm: bytes, speech_time: int) -> AsrVadResult:
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
            result = AsrVadResult(speech_time, 3, self.start_time, speech_time - 100, "")
            self.in_speech = False
            self.start_time = None
            return result
        return AsrVadResult(speech_time, 0)

    async def reset(self) -> None:
        self.in_speech = False
        self.start_time = None


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

    async def feed(self, pcm: bytes, speech_time: int) -> AsrVadResult:
        if len(pcm) != PCM_100MS_BYTES:
            raise ValueError("expected 100 ms PCM")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self._feed_sync, pcm, speech_time))

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
            if end is not None:
                self._drain_asr(is_final=True)
                text = "".join(self._text_parts).strip()
                result = AsrVadResult(speech_time, 3, self._start_time, end, text)
                self._finish_utterance()
                return result
            return AsrVadResult(speech_time, 2, self._start_time, speech_time, "".join(self._text_parts))

        return AsrVadResult(speech_time, 0)

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

    def _generate_vad(self, pcm: bytes, is_final: bool):
        samples = self._numpy.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        result = self._vad_model.generate(
            input=samples, cache=self._vad_cache, is_final=is_final, chunk_size=100,
        )
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

    async def reset(self) -> None:
        self._finish_utterance()
        self._vad_cache = {}


def create_asr_engine():
    """Select real ASR by default; use ASR_BACKEND=stub for protocol demos."""
    backend = os.getenv("ASR_BACKEND", "funasr").lower()
    if backend == "stub":
        return VadAsrStub()
    if backend != "funasr":
        raise ValueError("ASR_BACKEND must be 'funasr' or 'stub'")
    return FunAsrVadAdapter()


class DemoAgent:
    """Development-only text bot with deterministic session opening behavior."""

    async def stream_reply(self, messages: Iterable[dict]) -> AsyncIterator[str]:
        messages = list(messages)
        if _is_pick_up(messages):
            for chunk in ("您好，我是猫猫，", "您有什需要帮助的吗"):
                await asyncio.sleep(0)
                yield chunk
            return
        for chunk in ("我已经收到你的", "消息。"):
            await asyncio.sleep(0)
            yield chunk


class OllamaAgent:
    """Ollama OpenAI-compatible streaming chat adapter for the local model."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        # Keep the local real-time profile within the memory budget of this project.
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")

    async def stream_reply(self, messages: Iterable[dict]) -> AsyncIterator[str]:
        messages = list(messages)
        # Opening is a text-bot policy signal, not an LLM prompt. Keeping it
        # deterministic avoids cold-model latency and prevents variations in
        # the first thing a user hears after pick-up.
        if _is_pick_up(messages):
            yield OPENING_MESSAGE
            return
        normalized = []
        for item in messages:
            role = item.get("role", "user")
            normalized.append({
                "role": "assistant" if role in {"machine", "robot", "assistant"} else "user",
                "content": item.get("content", item.get("message", "")),
            })
        payload = {"model": self.model, "messages": normalized, "stream": True}
        timeout = aiohttp.ClientTimeout(total=None, connect=5, sock_read=120)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(f"{self.base_url}/v1/chat/completions", json=payload) as response:
                    response.raise_for_status()
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            return
                        event = json.loads(data)
                        delta = event.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"Ollama is unavailable at {self.base_url}; start it and run `ollama pull {self.model}`"
            ) from exc


def create_agent():
    """Select the lightweight local Ollama model only when explicitly enabled."""
    backend = os.getenv("AGENT_BACKEND", "demo").lower()
    if backend == "demo":
        return DemoAgent()
    if backend == "ollama":
        return OllamaAgent()
    raise ValueError("AGENT_BACKEND must be 'demo' or 'ollama'")


class DemoTts:
    """Development-only tone stream. It verifies the audio pipeline, not speech quality."""

    async def stream_pcm(self, text: str, speed: float = 1.0, volume: float = 0.2, voice: str = "default") -> AsyncIterator[bytes]:
        _ = (speed, voice)
        duration_frames = max(1, len(text))
        for frame_index in range(duration_frames):
            samples = []
            for index in range(1600):
                sample = int(32767 * volume * math.sin(2 * math.pi * 440 * (frame_index * 1600 + index) / 16000))
                samples.append(sample)
            await asyncio.sleep(0)
            yield struct.pack("<{}h".format(len(samples)), *samples)


class PiperTts:
    """Lightweight local Chinese TTS backed by Piper's ONNX voice model.

    The voice emits 22.05 kHz PCM.  The websocket protocol is fixed at 16 kHz,
    so conversion occurs here before ``DuplexDialogue`` frames it into 100 ms
    packets.
    """

    OUTPUT_SAMPLE_RATE = 16_000

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path or os.getenv(
            "PIPER_MODEL_PATH", "pretrained_models/piper/zh_CN-huayan-medium.onnx"
        )
        self._voice = None
        self._load_lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()

    async def stream_pcm(
        self, text: str, speed: float = 1.0, volume: float = 0.2, voice: str = "default"
    ) -> AsyncIterator[bytes]:
        _ = voice
        async with self._synthesis_lock:
            await self._ensure_voice()
            loop = asyncio.get_running_loop()
            pcm = await loop.run_in_executor(None, self._synthesize_sync, text, speed, volume)
        if pcm:
            yield pcm

    async def _ensure_voice(self) -> None:
        if self._voice is not None:
            return
        async with self._load_lock:
            if self._voice is not None:
                return
            if not Path(self.model_path).is_file():
                raise RuntimeError(
                    f"Piper voice not found: {self.model_path}. "
                    "Download zh_CN-huayan-medium before starting the service."
                )
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise RuntimeError("Piper is not installed. Install with `python3.11 -m pip install -e '.[tts]'`.") from exc
            loop = asyncio.get_running_loop()
            self._voice = await loop.run_in_executor(None, PiperVoice.load, self.model_path)

    def _synthesize_sync(self, text: str, speed: float, volume: float) -> bytes:
        from piper import SynthesisConfig

        # Piper uses length_scale: values above 1 slow speech down.
        config = SynthesisConfig(length_scale=1.0 / max(speed, 0.1), volume=max(volume, 0.0))
        output = bytearray()
        for chunk in self._voice.synthesize(text, config):
            if chunk.sample_channels != 1 or chunk.sample_width != 2:
                raise RuntimeError("Piper voice must produce mono 16-bit PCM")
            raw = chunk.audio_int16_bytes
            if chunk.sample_rate != self.OUTPUT_SAMPLE_RATE:
                raw, _ = audioop.ratecv(raw, 2, 1, chunk.sample_rate, self.OUTPUT_SAMPLE_RATE, None)
            output.extend(raw)
        return bytes(output)


def create_tts():
    """Use lightweight Piper by default; keep the tone generator for protocol-only demos."""
    backend = os.getenv("TTS_BACKEND", "piper").lower()
    if backend == "piper":
        return PiperTts()
    if backend == "demo":
        return DemoTts()
    raise ValueError("TTS_BACKEND must be 'piper' or 'demo'")


@dataclass
class CatAnimationFrame:
    sequence: int
    speech_time: int
    avatar_state: str
    asset_id: str


class CatAnimationDriver:
    def __init__(self, open_threshold: float = 0.020, close_threshold: float = 0.012) -> None:
        self.open_threshold = open_threshold
        self.close_threshold = close_threshold
        self.state = "mouth_closed"
        self.low_energy_frames = 0

    def drive(self, pcm: bytes, sequence: int, speech_time: int) -> CatAnimationFrame:
        samples = struct.unpack("<{}h".format(len(pcm) // 2), pcm)
        rms = math.sqrt(sum((sample / 32768.0) ** 2 for sample in samples) / len(samples))
        if rms >= self.open_threshold:
            self.state = "mouth_open"
            self.low_energy_frames = 0
        elif rms < self.close_threshold:
            self.low_energy_frames += 1
            if self.low_energy_frames >= 2:
                self.state = "mouth_closed"
        return CatAnimationFrame(sequence, speech_time, self.state, self.state)
