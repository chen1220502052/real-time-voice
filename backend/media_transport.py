"""Final write-boundary fencing for the unchanged WebSocket/PCM protocol."""
import json
from .latency_trace import milliseconds


class FencedMediaWriter:
    def __init__(self, ws, lock, is_open, is_current):
        self.ws, self.lock = ws, lock
        self.is_open, self.is_current = is_open, is_current

    async def send(self, packet, metadata, wire):
        # Hold the same lock as control writes. An invalidation may happen
        # while the avatar write is blocked; recheck before enqueueing PCM.
        # If invalidated, the caller must NOT consume the wire sequence.
        async with self.lock:
            if not self.is_open() or not self.is_current(packet):
                return False
            if "timing" in metadata:
                metadata["timing"]["firstFrameSendStartedMs"] = milliseconds()
            await self.ws.send_str(json.dumps(metadata, ensure_ascii=False))
            if not self.is_open() or not self.is_current(packet):
                return False
            await self.ws.send_bytes(wire)
            return True
