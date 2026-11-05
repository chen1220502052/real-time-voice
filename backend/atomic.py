"""Compatibility exports and provider composition; runtime behavior lives in providers."""

import os
from .protocol import PCM_100MS_BYTES
from .providers.common import resample_pcm16_mono, normalized_pcm16_rms, AsrVadResult, AsrUnavailable, PICK_UP_SIGNAL, OPENING_MESSAGE
from .providers.asr_local import VadAsrStub, FunAsrVadAdapter
from .providers.asr_cloud import DashScopeAsrVadAdapter
from .providers.asr_nls import AliyunNlsAsrVadAdapter, _NlsStartMarker, _NlsFlushMarker, AliyunNlsTokenProvider
from .providers.llm import DemoAgent, DEFAULT_SYSTEM_PROMPT, OllamaAgent, SpeculativeAgent
from .providers.tts import DemoTts, PiperTts
from .providers.avatar import CatAnimationFrame, CatAnimationDriver


def create_asr_engine():
    """Select local, cloud-hybrid, or deterministic ASR."""
    backend = os.getenv("ASR_BACKEND", "funasr").lower()
    if backend == "stub":
        return VadAsrStub()
    if backend == "mlx":
        from .providers.speech_local import MlxWhisperAsr
        return MlxWhisperAsr()
    if backend == "parakeet":
        from .providers.speech_local import ParakeetAsr
        return ParakeetAsr()
    if backend == "funasr":
        return FunAsrVadAdapter()
    if backend == "dashscope":
        return DashScopeAsrVadAdapter()
    if backend == "nls":
        return AliyunNlsAsrVadAdapter()
    raise ValueError("ASR_BACKEND must be 'parakeet', 'mlx', 'nls', 'dashscope', 'funasr', or 'stub'")


def create_agent():
    """Select the chat backend: deterministic demo or OpenAI-compatible endpoint.

    The 'ollama' backend also serves hosted OpenAI-compatible APIs such as
    DeepSeek via OLLAMA_BASE_URL + LLM_API_KEY (or DEEPSEEK_API_KEY).
    """
    backend = os.getenv("AGENT_BACKEND", "demo").lower()
    if backend == "demo":
        return DemoAgent()
    if backend == "english_chat":
        from .english_chat import EnglishChatBot
        return EnglishChatBot()
    if backend == "ollama":
        return OllamaAgent()
    raise ValueError("AGENT_BACKEND must be 'demo', 'ollama', or 'english_chat'")


def create_tts():
    """Use lightweight Piper by default; keep the tone generator for protocol-only demos."""
    backend = os.getenv("TTS_BACKEND", "piper").lower()
    if backend == "piper":
        return PiperTts()
    if backend == "demo":
        return DemoTts()
    if backend == "kokoro":
        from .providers.speech_local import KokoroTts
        return KokoroTts()
    raise ValueError("TTS_BACKEND must be 'kokoro', 'piper' or 'demo'")
