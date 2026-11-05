"""common provider boundary."""

import math
import struct
from dataclasses import dataclass
from typing import Optional


def resample_pcm16_mono(raw: bytes, source_rate: int, target_rate: int) -> bytes:
    """Resample little-endian mono PCM16 with linear interpolation.

    This deliberately uses only supported Python APIs.  The former
    ``audioop.ratecv`` implementation prevents the project from moving to
    Python 3.13 because ``audioop`` was removed there.
    """
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    if len(raw) % 2:
        raise ValueError("PCM16 payload length must be even")
    if source_rate == target_rate or not raw:
        return raw

    sample_count = len(raw) // 2
    samples = struct.unpack(f"<{sample_count}h", raw)
    output_count = max(1, round(sample_count * target_rate / source_rate))
    rate_ratio = source_rate / target_rate
    output = []
    for output_index in range(output_count):
        position = output_index * rate_ratio
        left_index = min(int(position), sample_count - 1)
        right_index = min(left_index + 1, sample_count - 1)
        fraction = position - left_index
        value = round(samples[left_index] + (samples[right_index] - samples[left_index]) * fraction)
        output.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{output_count}h", *output)


def normalized_pcm16_rms(raw: bytes) -> float:
    if len(raw) % 2:
        raise ValueError("PCM16 payload length must be even")
    if not raw:
        return 0.0
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    return math.sqrt(sum((sample / 32768.0) ** 2 for sample in samples) / len(samples))


@dataclass
class AsrVadResult:
    speech_time: int
    status: int
    start_time: Optional[int] = None
    end_time: Optional[int] = None
    text: str = ""
    timing: Optional[dict] = None
    activity: Optional[dict] = None


class AsrUnavailable(RuntimeError):
    fatal = True


PICK_UP_SIGNAL = "pick_up"


OPENING_MESSAGE = "您好，我是猫猫，您有什么需要帮助的吗？"

