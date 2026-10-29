import hashlib
import struct
from dataclasses import dataclass


MAGIC = 0x5652
VERSION = 1
HEADER_FORMAT = "!HBBQIQ"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
PCM_100MS_BYTES = 3200


class ProtocolError(ValueError):
    pass


class SequenceValidator:
    """Validate a monotonically contiguous packet stream.

    After a forward gap we resynchronize to the received packet so one loss
    does not make every later packet invalid. Duplicates remain rejected.
    """

    def __init__(self, expected: int = 0) -> None:
        self.expected = expected

    def validate(self, sequence: int) -> None:
        if sequence != self.expected:
            expected = self.expected
            if sequence > self.expected:
                self.expected = sequence + 1
            raise ProtocolError(f"audio sequence discontinuity: expected {expected}, got {sequence}")
        self.expected += 1


@dataclass
class AudioFrame:
    direction: int
    session_hash: int
    sequence: int
    timestamp_ms: int
    payload: bytes

    @property
    def speech_time(self) -> int:
        return (self.sequence + 1) * 100


def session_hash(session_id: str) -> int:
    return int.from_bytes(hashlib.sha256(session_id.encode("utf-8")).digest()[:8], "big")


def encode_audio_frame(direction: int, session_id: str, sequence: int, timestamp_ms: int, payload: bytes) -> bytes:
    if len(payload) != PCM_100MS_BYTES:
        raise ProtocolError("audio payload must be exactly 3200 bytes")
    header = struct.pack(HEADER_FORMAT, MAGIC, VERSION, direction, session_hash(session_id), sequence, timestamp_ms)
    return header + payload


def decode_audio_frame(data: bytes) -> AudioFrame:
    if len(data) != HEADER_SIZE + PCM_100MS_BYTES:
        raise ProtocolError("invalid audio frame length")
    magic, version, direction, hashed, sequence, timestamp_ms = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
    if magic != MAGIC or version != VERSION:
        raise ProtocolError("unsupported audio frame")
    if direction not in (0, 1):
        raise ProtocolError("invalid direction")
    return AudioFrame(direction, hashed, sequence, timestamp_ms, data[HEADER_SIZE:])
