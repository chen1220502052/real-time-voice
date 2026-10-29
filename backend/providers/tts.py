"""tts provider boundary."""

import asyncio
import json
import math
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncIterator, Optional
from .common import resample_pcm16_mono
from ..pcm_stream import executor_chunks


class DemoTts:
    """Development-only tone stream. It verifies the audio pipeline, not speech quality."""

    def __init__(self) -> None:
        self.frame_delay_ms = max(0, int(os.getenv("DEMO_TTS_FRAME_DELAY_MS", "0")))
        self.fail = os.getenv("DEMO_TTS_ERROR", "0") == "1"

    async def stream_pcm(self, text: str, speed: float = 1.0, volume: float = 0.2, voice: str = "default") -> AsyncIterator[bytes]:
        if self.fail:
            raise RuntimeError("Stub TTS error requested by DEMO_TTS_ERROR")
        _ = (speed, voice)
        duration_frames = max(1, len(text))
        for frame_index in range(duration_frames):
            samples = []
            for index in range(1600):
                sample = int(32767 * volume * math.sin(2 * math.pi * 440 * (frame_index * 1600 + index) / 16000))
                samples.append(sample)
            await asyncio.sleep(self.frame_delay_ms / 1000)
            yield struct.pack("<{}h".format(len(samples)), *samples)

    async def warmup(self) -> None:
        return None


class PiperTts:
    """Lightweight local TTS backed by Piper's ONNX voice models.

    The primary voice defaults to the Chinese huayan model; an optional
    English voice (``PIPER_MODEL_PATH_EN``, or any ``en_*.onnx`` next to the
    primary model) is selected per segment for non-CJK text. The voice emits
    22.05 kHz PCM.  The websocket protocol is fixed at 16 kHz, so conversion
    occurs here before ``DuplexDialogue`` frames it into 100 ms packets.
    """

    OUTPUT_SAMPLE_RATE = 16_000
    _voice_pool = {}
    _executor_pool = {}
    _voice_pool_lock = threading.Lock()

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path or os.getenv(
            "PIPER_MODEL_PATH", "pretrained_models/piper/zh_CN-huayan-medium.onnx"
        )
        self.model_path_en = os.getenv("PIPER_MODEL_PATH_EN", "") or self._detect_english_voice()
        self._voices: dict[str, object] = {}
        self._load_lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()

    def _detect_english_voice(self) -> str:
        candidate = Path(self.model_path).parent
        for onnx in sorted(candidate.glob("en_*.onnx")):
            return str(onnx)
        return ""

    @staticmethod
    def _voice_lang(text: str) -> str:
        # Han ideographs and kana always go to the primary (Chinese) voice;
        # plain Latin text prefers the English voice when one is configured.
        if any("\u2e80" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" for ch in text):
            return "primary"
        return "en"

    async def stream_pcm(
        self, text: str, speed: float = 1.0, volume: float = 0.2, voice: str = "default"
    ) -> AsyncIterator[bytes]:
        _ = voice
        async with self._synthesis_lock:
            lang = self._voice_lang(text)
            if lang == "en" and not self.model_path_en:
                lang = "primary"
            await self._ensure_voice(lang)
            # Cancelling an asyncio Future cannot stop an already-running
            # native Piper call. A per-model single-thread executor fences all
            # callers (including other PiperTts instances sharing the voice),
            # so a replacement reply can queue but never synthesize alongside
            # the cancelled one.
            chunks = executor_chunks(self._executor_for_model(self._model_path_for(lang)),
                                     lambda: self._synthesize_chunks(text, speed, volume, lang))
            try:
                async for pcm in chunks:
                    yield pcm
            finally:
                await chunks.aclose()

    async def warmup(self) -> None:
        await self._ensure_voice("primary")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor_for_model(self._model_path_for("primary")),
            self._synthesize_sync, "你好", 1.0, 0.05, "primary",
        )
        if self.model_path_en:
            await self._ensure_voice("en")
            await loop.run_in_executor(
                self._executor_for_model(self._model_path_for("en")),
                self._synthesize_sync, "Hello", 1.0, 0.05, "en",
            )

    def _executor_for_model(self, model_path: str) -> ThreadPoolExecutor:
        model_path = str(Path(model_path).resolve())
        with self._voice_pool_lock:
            executor = self._executor_pool.get(model_path)
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="piper-tts")
                self._executor_pool[model_path] = executor
            return executor

    def _model_path_for(self, lang: str) -> str:
        return self.model_path_en if lang == "en" and self.model_path_en else self.model_path

    async def _ensure_voice(self, lang: str = "primary") -> None:
        if lang in self._voices:
            return
        async with self._load_lock:
            if lang in self._voices:
                return
            model_path = self._model_path_for(lang)
            if not Path(model_path).is_file():
                raise RuntimeError(
                    f"Piper voice not found: {model_path}. "
                    "Download the voice model before starting the service."
                )
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise RuntimeError("Piper is not installed. Install with `python3.11 -m pip install -e '.[tts]'`.") from exc
            loop = asyncio.get_running_loop()
            self._voices[lang] = await loop.run_in_executor(
                None, self._load_voice, PiperVoice, model_path
            )

    def _load_voice(self, piper_voice_class, model_path: str):
        model_path = str(Path(model_path).resolve())
        with self._voice_pool_lock:
            voice = self._voice_pool.get(model_path)
            if voice is None:
                import onnxruntime
                from piper.config import PiperConfig
                options = onnxruntime.SessionOptions()
                options.intra_op_num_threads = max(1, min(4, int(os.getenv("TTS_CPU_THREADS", "2"))))
                options.inter_op_num_threads = 1
                options.add_session_config_entry("session.intra_op.allow_spinning", "0")
                options.add_session_config_entry("session.inter_op.allow_spinning", "0")
                # Piper.load currently creates an unbounded default session;
                # keep the same CPU provider/config/voice but own its budget.
                voice = piper_voice_class(
                    config=PiperConfig.from_dict(json.loads(Path(model_path + ".json").read_text())),
                    session=onnxruntime.InferenceSession(model_path, sess_options=options,
                                                        providers=["CPUExecutionProvider"]),
                )
                self._voice_pool[model_path] = voice
            return voice

    def _synthesize_sync(self, text: str, speed: float, volume: float, lang: str = "primary") -> bytes:
        return b''.join(self._synthesize_chunks(text, speed, volume, lang))

    def _synthesize_chunks(self, text: str, speed: float, volume: float, lang: str = "primary"):
        from piper import SynthesisConfig

        # Piper uses length_scale: values above 1 slow speech down.
        config = SynthesisConfig(length_scale=1.0 / max(speed, 0.1), volume=max(volume, 0.0))
        for chunk in self._voices[lang].synthesize(text, config):
            if chunk.sample_channels != 1 or chunk.sample_width != 2:
                raise RuntimeError("Piper voice must produce mono 16-bit PCM")
            raw = chunk.audio_int16_bytes
            if chunk.sample_rate != self.OUTPUT_SAMPLE_RATE:
                raw = resample_pcm16_mono(raw, chunk.sample_rate, self.OUTPUT_SAMPLE_RATE)
            yield raw

