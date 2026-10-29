"""Generate the e2e test fixture WAVs (16 kHz mono 16-bit) under tests/fixtures.

Keeps generation reproducible; safe to re-run (overwrites). Speech clips are
synthesized with the macOS ``say`` command; noises and pauses are built in
Python.
"""

import struct
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).parent.parent / "tests" / "fixtures"
EN = ROOT / "en"
SILENCE_100MS = b"\x00\x00" * 1600


def say(text: str, voice: str = "Samantha") -> bytes:
    aiff = Path("/tmp/rtv_say.aiff")
    wav = Path("/tmp/rtv_say.wav")
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                    str(aiff), str(wav)], check=True)
    with wave.open(str(wav), "rb") as w:
        pcm = w.readframes(w.getnframes())
    wav.unlink()
    aiff.unlink()
    return pcm


def write_wav(name: str, pcm: bytes, subdir: str = "") -> None:
    path = ROOT / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    print("wrote", path.relative_to(ROOT.parent), f"({len(pcm) / 32000:.2f}s)")


def gap(seconds: float) -> bytes:
    return SILENCE_100MS * int(seconds * 10)


def noise_burst(duration_s: float, amplitude: float = 0.08, seed: int = 7) -> bytes:
    import random
    rng = random.Random(seed)
    n = int(16000 * duration_s)
    samples = []
    for i in range(n):
        env = min(1.0, i / 800, (n - i) / 800)
        samples.append(int(rng.uniform(-1, 1) * amplitude * 32767 * env))
    return struct.pack("<{}h".format(n), *samples)


def keyboard_clicks() -> bytes:
    """Six sharp click impulses spread over ~1.2s (keyboard/desk simulation)."""
    import random
    rng = random.Random(11)
    out = bytearray()
    for start_ms in (0, 180, 350, 700, 900, 1100):
        out.extend(b"\x00\x00" * int(start_ms * 16 - len(out) // 2))
        for i in range(120):  # 7.5ms click
            env = 1.0 - i / 120
            out.extend(struct.pack("<h", int(rng.uniform(-1, 1) * 0.35 * 32767 * env)))
    out.extend(b"\x00\x00" * 1600)
    return bytes(out)


def main() -> None:
    # --- dialogue turns ---
    write_wav("en_help_16k.wav", say("What can you help me with?"), "en")
    write_wav("en_catfact_16k.wav", say("Tell me a fun fact about cats."), "en")
    write_wav("en_hello_16k.wav", say("Hello! How are you today?"), "en")
    write_wav("en_short_16k.wav", say("What's the capital of France?"), "en")
    write_wav("en_long_16k.wav", say(
        "Can you help me plan a three day trip to Tokyo with a budget of about "
        "five hundred dollars per person?"), "en")
    write_wav("en_numbers_16k.wav", say("What is twenty three plus forty five?"), "en")
    write_wav("en_followup_16k.wav", say("Wait, actually, can you speak slower?"), "en")
    write_wav("zh_help_16k.wav", say("你好，请问有什么可以帮助我的？", voice="Tingting"))

    # --- mid-sentence pause canaries (must not split at 500ms endpointing) ---
    p1 = say("What can you help me with,")
    p2 = say("and can you tell me a joke?")
    write_wav("en_pause300_16k.wav", p1 + gap(0.3) + p2, "en")
    write_wav("en_pause400_16k.wav", p1 + gap(0.4) + p2, "en")
    write_wav("en_pause1500_16k.wav", p1 + gap(1.5) + p2, "en")

    # --- noise / false-interruption injections ---
    write_wav("noise_burst_800ms.wav", noise_burst(0.8))
    write_wav("noise_sustained_2s.wav", noise_burst(2.0, amplitude=0.06, seed=13))
    write_wav("noise_clicks_1_2s.wav", keyboard_clicks())


if __name__ == "__main__":
    main()
