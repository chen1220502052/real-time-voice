import os
import wave
from pathlib import Path

import pytest

from backend.atomic import AliyunNlsAsrVadAdapter, FunAsrVadAdapter, OllamaAgent, PiperTts
from backend.protocol import PCM_100MS_BYTES


pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(os.getenv("RUN_REAL_MODELS") != "1", reason="set RUN_REAL_MODELS=1"),
]


def test_real_asr_and_tts_models_load_and_run():
    async def run():
        asr = FunAsrVadAdapter(device="cpu")
        await asr.warmup()
        silent = await asr.feed(b"\x00" * 3200, 100)
        assert silent.status == 0

        fixture = Path(__file__).parent / "fixtures" / "zh_help_16k.wav"
        with wave.open(str(fixture), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
        finals = []
        for offset in range(0, len(pcm), PCM_100MS_BYTES):
            result = await asr.feed(
                pcm[offset:offset + PCM_100MS_BYTES],
                offset // PCM_100MS_BYTES * 100 + 200,
            )
            if result.status == 3:
                finals.append(result)
        assert any(item.text.strip() for item in finals)
        await asr.reset()

        tts = PiperTts()
        await tts.warmup()
        chunks = [chunk async for chunk in tts.stream_pcm("你好")]
        assert chunks and any(chunks[0])

    import asyncio
    asyncio.run(run())


def test_real_ollama_model_streams_text():
    async def run():
        agent = OllamaAgent(model="qwen2.5:0.5b")
        chunks = [chunk async for chunk in agent.stream_reply([
            {"role": "user", "content": "请只回复两个字：你好"},
        ])]
        assert "".join(chunks).strip()

    import asyncio
    asyncio.run(asyncio.wait_for(run(), timeout=90))


@pytest.mark.skipif(
    os.getenv("RUN_REAL_NLS_FIXTURE") != "1",
    reason="set RUN_REAL_MODELS=1 and RUN_REAL_NLS_FIXTURE=1",
)
@pytest.mark.real_cloud
def test_real_nls_returns_text_for_chinese_audio_fixture():
    fixture = Path(__file__).parent / "fixtures" / "zh_help_16k.wav"
    assert fixture.is_file(), "run python3.11 scripts/generate_asr_fixture.py"

    async def run():
        with wave.open(str(fixture), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16_000, 1, 2)
            pcm = wav.readframes(wav.getnframes())
        assert len(pcm) % PCM_100MS_BYTES == 0

        adapter = AliyunNlsAsrVadAdapter(device="cpu")
        await adapter.warmup()
        finals = []
        try:
            for offset in range(0, len(pcm), PCM_100MS_BYTES):
                speech_time = offset // PCM_100MS_BYTES * 100 + 100
                result = await adapter.feed(pcm[offset:offset + PCM_100MS_BYTES], speech_time)
                if result.status == 3:
                    finals.append(result)
                # NLS is a real-time streaming service. Feeding a multi-second
                # recording in a tight loop can close the stream before its
                # asynchronous result callbacks catch up.
                await asyncio.sleep(0.1)
        finally:
            await adapter.reset()

        assert finals, "FSMN-VAD did not produce an utterance end"
        assert any(result.text.strip() for result in finals), "NLS returned no final text"

    import asyncio
    asyncio.run(asyncio.wait_for(run(), timeout=120))
