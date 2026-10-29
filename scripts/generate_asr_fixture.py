#!/usr/bin/env python3.11
"""Generate the reproducible 16 kHz Chinese ASR fixture with local Piper."""

import argparse
import asyncio
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.atomic import PiperTts
from backend.protocol import PCM_100MS_BYTES


async def generate(output: Path, text: str) -> None:
    tts = PiperTts()
    audio = b"".join([chunk async for chunk in tts.stream_pcm(text, volume=0.8)])
    if not audio:
        raise RuntimeError("Piper returned no audio")

    # Give streaming VAD deterministic utterance boundaries and align the
    # payload to the protocol's 100 ms / 3200-byte frames.
    leading_silence = b"\x00" * (PCM_100MS_BYTES * 5)
    trailing_silence = b"\x00" * (PCM_100MS_BYTES * 15)
    payload = leading_silence + audio + trailing_silence
    remainder = len(payload) % PCM_100MS_BYTES
    if remainder:
        payload += b"\x00" * (PCM_100MS_BYTES - remainder)

    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(payload)
    print(f"generated {output} ({len(payload) // PCM_100MS_BYTES} frames): {text}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/zh_help_16k.wav"),
    )
    parser.add_argument("--text", default="你好，请问有什么可以帮助我的？")
    args = parser.parse_args()
    asyncio.run(generate(args.output, args.text))


if __name__ == "__main__":
    main()
