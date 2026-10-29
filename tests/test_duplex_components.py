"""Composition boundaries; no real models, microphone, or network services."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.atomic import AsrVadResult, PICK_UP_SIGNAL
from backend.duplex import DuplexEvent
from backend.duplex_events import DuplexEvent as SharedEvent
from tests.test_duplex import make_stub_duplex


def test_duplex_event_import_remains_compatible():
    assert DuplexEvent is SharedEvent


@pytest.mark.parametrize("hook,component,method,args", [
    ("_start_reply", "reply_pipeline", "start", ("turn", None)),
    ("_reply_worker", "reply_pipeline", "run", ("reply", 1, None)),
    ("_fail_reply", "reply_pipeline", "fail", ("reply", 1, "tts", "TTS_FAILED", "failed")),
    ("_flush_pcm_frames", "reply_pipeline", "flush_pcm_frames", ("reply", 1, bytearray())),
    ("_enqueue_playback", "reply_pipeline", "enqueue_playback", ("reply", 1, b"pcm")),
    ("_maybe_complete", "reply_pipeline", "maybe_complete", ("reply", 3)),
    ("_finalize_reply", "reply_pipeline", "finalize", ("reply",)),
    ("_maybe_barge_in", "interruption", "maybe_barge_in", (None,)),
    ("_confirm_barge_in", "interruption", "confirm", (None,)),
    ("_cancel_pending_barge_in", "interruption", "cancel_pending", ("silence",)),
    ("_interrupt", "interruption", "interrupt", (False,)),
])
def test_existing_hooks_delegate_to_explicit_components(monkeypatch, hook, component, method, args):
    async def run():
        dialogue = make_stub_duplex()
        try:
            with monkeypatch.context() as patch:
                target = AsyncMock()
                patch.setattr(getattr(dialogue, component), method, target)
                await getattr(dialogue, hook)(*args)
                target.assert_awaited_once_with(*args)
        finally:
            await dialogue.close("test_done")

    asyncio.run(run())


def test_interruption_gate_state_is_per_session_and_cleared_on_invalidation(monkeypatch):
    monkeypatch.setenv("BARGE_IN_CONFIRM_MS", "200")
    monkeypatch.setenv("BARGE_IN_MIN_REPLY_AGE_MS", "0")
    monkeypatch.setenv("BARGE_IN_COOLDOWN_MS", "0")

    async def run():
        first, second = make_stub_duplex("first"), make_stub_duplex("second")
        try:
            for dialogue in (first, second):
                dialogue._active_reply_id = "reply"
                dialogue._active_turn_id = "turn"
                dialogue.session.robot.status = 2
            await first.interruption.maybe_barge_in(AsrVadResult(100, 1, 0, 100, ""))
            await first.interruption.maybe_barge_in(AsrVadResult(200, 2, 0, 200, "hello"))
            assert first.interruption._pending_barge_in is not None
            assert first.interruption._asr_text_capable
            assert second.interruption._pending_barge_in is None
            assert not second.interruption._asr_text_capable
            await first.interruption.maybe_barge_in(AsrVadResult(300, 2, 0, 300, "hello"))
            assert first._active_reply_id is None
            assert first._epoch == 1
            assert first.interruption._pending_barge_in is None
            assert second._active_reply_id == "reply"
            assert second._epoch == 0
        finally:
            await first.close("test_done")
            await second.close("test_done")

    asyncio.run(run())


def test_cancelled_pipeline_cannot_publish_late_pcm_or_fail_new_reply():
    class LateTts:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stream_pcm(self, text, speed=1):
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Model/native work can finish after cancellation. Epoch fencing
                # must discard its result rather than trusting cancellation alone.
                yield b"\x01\x00" * 1600

    async def run():
        tts = LateTts()
        dialogue = make_stub_duplex(tts=tts)
        try:
            await dialogue.start()
            await dialogue.reply_pipeline.start("old_turn", PICK_UP_SIGNAL)
            await asyncio.wait_for(tts.entered.wait(), 1)
            old_reply, old_epoch, old_task = dialogue._active_reply_id, dialogue._epoch, dialogue._reply_task
            await dialogue.interruption.interrupt()
            await asyncio.wait_for(old_task, 1)
            assert dialogue._media_outputs.empty()
            assert dialogue._epoch == old_epoch + 1
            assert dialogue._active_reply_id is None

            tts.entered.clear()
            dialogue._input_text = "Tell me about cats."
            await dialogue.reply_pipeline.start("new_turn")
            await asyncio.wait_for(tts.entered.wait(), 1)
            new_reply, new_epoch = dialogue._active_reply_id, dialogue._epoch
            await dialogue.reply_pipeline.fail(old_reply, old_epoch, "tts", "TTS_FAILED", "late")
            await dialogue.mark_playback_completed(old_reply)
            assert dialogue._active_reply_id == new_reply
            assert dialogue._epoch == new_epoch
            assert dialogue._media_outputs.empty()
            assert not dialogue._reply_task.done()
        finally:
            await dialogue.close("test_done")
            await asyncio.gather(dialogue._reply_task, return_exceptions=True)

    asyncio.run(run())


def test_hangup_invalidates_reply_before_awaiting_prefetch_cleanup():
    async def run():
        dialogue = make_stub_duplex()
        dialogue._active_reply_id = "old_reply"
        dialogue._active_turn_id = "old_turn"
        dialogue._epoch = 7
        original = dialogue.prefetch.aclose
        checked = False

        async def close_prefetch():
            nonlocal checked
            assert dialogue._active_reply_id is None
            assert dialogue._epoch == 8
            checked = True
            await original()

        dialogue.prefetch.aclose = close_prefetch
        await dialogue.close("test_done")
        assert checked
    asyncio.run(run())
