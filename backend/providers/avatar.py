"""avatar provider boundary."""

import math
import struct
from dataclasses import dataclass


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

