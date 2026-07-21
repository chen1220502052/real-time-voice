import asyncio
import struct

import numpy as np

from backend.atomic import CatAnimationDriver, FunAsrVadAdapter, VadAsrStub


def test_vad_returns_a_result_for_every_frame():
    async def run():
        vad = VadAsrStub(threshold=0.001)
        silence = b"\x00" * 3200
        speech = struct.pack("<1600h", *([10000] * 1600))
        assert (await vad.feed(silence, 100)).status == 0
        assert (await vad.feed(speech, 200)).status == 1
        assert (await vad.feed(speech, 300)).status == 2
        assert (await vad.feed(silence, 400)).status == 3
    asyncio.run(run())


def test_cat_driver_uses_hysteresis():
    driver = CatAnimationDriver(open_threshold=0.01, close_threshold=0.005)
    voice = struct.pack("<1600h", *([10000] * 1600))
    silence = b"\x00" * 3200
    assert driver.drive(voice, 0, 100).avatar_state == "mouth_open"
    assert driver.drive(silence, 1, 200).avatar_state == "mouth_open"
    assert driver.drive(silence, 2, 300).avatar_state == "mouth_closed"


def test_funasr_adapter_returns_final_text_on_vad_end():
    class FakeVad:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            values = ([[0, -1]], [], [[-1, 250]])
            return [{"value": values[self.calls - 1]}]

    class FakeAsr:
        def generate(self, **kwargs):
            return [{"text": "你好"}] if kwargs["is_final"] else []

    async def run():
        adapter = FunAsrVadAdapter()
        adapter._numpy = np
        adapter._vad_model = FakeVad()
        adapter._asr_model = FakeAsr()
        pcm = b"\x01\x00" * 1600
        assert (await adapter.feed(pcm, 100)).status == 1
        assert (await adapter.feed(pcm, 200)).status == 2
        final = await adapter.feed(pcm, 300)
        assert final.status == 3
        assert final.start_time == 0
        assert final.end_time == 250
        assert final.text == "你好"

    asyncio.run(run())
