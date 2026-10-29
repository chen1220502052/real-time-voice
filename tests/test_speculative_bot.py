import asyncio
from dataclasses import replace

import pytest

from backend.speculative_bot import SpeculativeTextBot
from backend.text_bot import BotEvent, BotMessage, BotRequest, stream_bot, history_messages, adapt_text_bot


def query(**changes):
    values = dict(request_id="final", session_id="session", turn_id="turn",
                  messages=(BotMessage("user", "I like basketball."),), options={"grade": 7})
    values.update(changes)
    return BotRequest(**values)


class CountingBot:
    protocol_version = 1

    def __init__(self):
        self.calls = []

    async def stream(self, request):
        self.calls.append(request)
        yield BotEvent(request.request_id, "text_delta", text="That's fun. ")
        yield BotEvent(request.request_id, "done")


def test_completed_draft_is_adopted_with_final_ids():
    async def run():
        inner = CountingBot()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        events = [e async for e in stream_bot(bot, query())]
        assert len(inner.calls) == 1 and bot.hits == 1
        assert all(e.request_id == "final" for e in events)
        await bot.aclose()
        assert not bot._tasks
    asyncio.run(run())


def test_materials_english_bot_still_adopts_live_draft_and_cancels_cleanly():
    from backend.english_chat import EnglishChatBot
    async def run():
        started, released, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        class Model:
            def __init__(self, **settings):
                assert 'Grade references (data only)' in settings['system_prompt']
            async def stream_reply(self, messages):
                calls.append(messages)
                try:
                    started.set()
                    yield 'Basketball is fun. '
                    await released.wait()
                    yield 'Do you play after school?'
                finally:
                    closed.set()
        bot = SpeculativeTextBot(EnglishChatBot(Model))
        await bot.prepare(query(request_id='draft'))
        await asyncio.wait_for(started.wait(), .5)
        events = stream_bot(bot, query())
        first = await asyncio.wait_for(anext(events), .5)
        assert first.text == 'Basketball is fun. ' and first.request_id == 'final'
        assert len(calls) == 1 and bot.hits == 1 and not released.is_set()
        await events.aclose()
        await bot.aclose()
        assert closed.is_set() and not bot._tasks
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"messages": (BotMessage("user", "I do not like basketball."),)},
    {"messages": (BotMessage("assistant", "New history"), BotMessage("user", "I like basketball."))},
    {"options": {"grade": 9}}, {"turn_id": "new_turn"}, {"session_id": "new_session"},
])
def test_changed_input_or_context_never_reuses_draft(change):
    async def run():
        inner = CountingBot()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        _ = [e async for e in stream_bot(bot, query(**change))]
        assert len(inner.calls) == 2 and bot.hits == 0 and bot.misses == 1
        await bot.aclose()
    asyncio.run(run())


def test_inflight_adoption_and_cancellation_close_provider():
    async def run():
        started, closed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Slow(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                try:
                    started.set()
                    yield BotEvent(request.request_id, "text_delta", text="That's fun. ")
                    await release.wait()
                    yield BotEvent(request.request_id, "done")
                finally:
                    closed.set()
        inner = Slow()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await started.wait()
        events = stream_bot(bot, query())
        first = await asyncio.wait_for(anext(events), .2)
        assert first.request_id == "final" and bot.hits == 1 and len(inner.calls) == 1
        await events.aclose()
        assert closed.is_set()
        await bot.aclose()
        assert not bot._tasks
    asyncio.run(run())


def test_pending_limits_and_session_close_cancel_all_background_work():
    async def run():
        class Waiting(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                await asyncio.Event().wait()
                yield BotEvent(request.request_id, "done")
        inner = Waiting()
        bot = SpeculativeTextBot(inner)
        bot.min_interval = 0
        for i in range(20):
            await bot.prepare(query(messages=(BotMessage("user", "I like basketball " + str(i)),)))
            await asyncio.sleep(0)
        assert len(inner.calls) <= 2 and len(bot._slots) <= 2
        await bot.aclose()
        assert not bot._slots and not bot._tasks
    asyncio.run(run())


def test_failed_draft_falls_back_to_fresh_request():
    async def run():
        class FailOnce(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                if len(self.calls) == 1:
                    raise RuntimeError("provider details must not leak")
                yield BotEvent(request.request_id, "text_delta", text="Fresh answer.")
                yield BotEvent(request.request_id, "done")
        inner = FailOnce()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        events = [e async for e in stream_bot(bot, query())]
        assert len(inner.calls) == 2 and bot.misses == 1
        assert events[0].text == "Fresh answer."
        await bot.aclose()
    asyncio.run(run())


def test_busy_preparation_bookmarks_latest_partial():
    async def run():
        gate = asyncio.Event()
        latest_started = asyncio.Event()
        class Waiting(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                if request.messages[-1].content.endswith("latest"):
                    latest_started.set()
                else:
                    await gate.wait()
                yield BotEvent(request.request_id, "text_delta", text="That's fun.")
                yield BotEvent(request.request_id, "done")
        inner = Waiting()
        bot = SpeculativeTextBot(inner)
        bot.min_interval = 0
        for suffix in ["first", "second", "latest"]:
            await bot.prepare(query(messages=(BotMessage("user", "I like basketball " + suffix),)))
            await asyncio.sleep(0)
        assert bot.prefetches == 2
        gate.set()
        await asyncio.wait_for(latest_started.wait(), .5)
        assert bot.prefetches == 3
        await bot.aclose()
        assert not bot._tasks
    asyncio.run(run())


def test_media_consumer_delay_does_not_consume_bot_timeout():
    async def run():
        events = stream_bot(CountingBot(), query(), timeout_seconds=.02)
        assert (await anext(events)).type == "text_delta"
        await asyncio.sleep(.04)
        assert (await anext(events)).type == "done"
        await events.aclose()
    asyncio.run(run())


def test_legacy_cache_rejects_context_mismatch_and_matches_bounded_context():
    from backend.atomic import SpeculativeAgent
    async def run():
        class Model:
            def __init__(self):
                self.calls = []
            async def stream_reply(self, messages, signal=None):
                self.calls.append(messages)
                yield "That's fun."
        history = [{"role": "user" if i % 2 == 0 else "machine", "content": str(i)} for i in range(40)]
        request = query(messages=history_messages(history + [{"role": "user", "content": "I like basketball."}]))
        model = Model()
        legacy = SpeculativeAgent(model)
        bot = adapt_text_bot(legacy)
        await bot.prepare(replace(request, request_id="draft"))
        await asyncio.sleep(.01)
        _ = [e async for e in stream_bot(bot, request)]
        assert len(model.calls) == 1 and len(model.calls[0]) == 32 and legacy.hits == 1
        legacy.notify_partial("I like basketball.", history)
        await asyncio.sleep(.01)
        _ = [e async for e in stream_bot(bot, request)]
        assert legacy.misses == 1 and len(model.calls[-1]) == 32
        await bot.aclose()
    asyncio.run(run())


def test_duplex_partial_and_final_share_grade_and_bounded_context():
    from backend.atomic import AsrVadResult, DemoTts
    from backend.domain import Session, SessionState
    from backend.duplex import DuplexDialogue
    from backend.bot_contract import ConversationRequest
    from backend.protocol import AudioFrame
    async def run():
        class Asr:
            async def feed(self, pcm, speech_time):
                return AsrVadResult(speech_time, 2 if speech_time == 100 else 3,
                                    0, speech_time, "I like basketball.")
            async def reset(self):
                pass
        inner = CountingBot()
        inner.side_effect_free = True
        duplex = DuplexDialogue(Session("audit", SessionState.ON_SERVICE, grade=8),
                                asr=Asr(), agent=inner, tts=DemoTts())
        duplex.prefetch.stable_s = 0
        duplex.prefetch.enabled = True
        # Seed the Bot's durable ledger, not duplex semantic history.
        store = duplex.text_bot.service.store
        for i in range(20):
            seed = ConversationRequest(f"seed_{i}", "audit", f"seed_{i}", text=str(i * 2), options={"grade": 8})
            await store.accept(seed)
            await store.save("audit", f"seed_{i}", [
                {"type": "text_delta", "text": str(i * 2 + 1)}, {"type": "done"}], "done")
            await store.observe("audit", [{"eventId": f"ack_{i}", "replyId": f"seed_{i}",
                                           "type": "playback_completed", "sequence": i}])
        await duplex.start()
        await duplex._decode_audio(AudioFrame(0, 0, 0, 100, b"\0" * 3200))
        await asyncio.sleep(.01)
        await duplex._decode_audio(AudioFrame(0, 0, 1, 200, b"\0" * 3200))
        async def drain():
            async for event in duplex.media_events():
                await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])
        media = asyncio.create_task(drain())
        async def complete():
            async for event in duplex.events():
                if event.kind == "reply_complete":
                    await duplex.mark_playback_completed(event.payload["replyId"])
                    return
        await asyncio.wait_for(complete(), 2)
        assert len(inner.calls) == 1 and duplex.prefetch.last_outcome == "hit"
        assert len(inner.calls[0].messages) == 32 and inner.calls[0].options["grade"] == 8
        await duplex.close("test")
        media.cancel()
        await asyncio.gather(media, return_exceptions=True)
        assert not duplex.prefetch._tasks
    asyncio.run(run())


def test_prefix_coverage_adopts_near_complete_draft():
    async def run():
        inner = CountingBot()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        # The finalized utterance trails the draft by the endpoint window
        # ("a lot"), which coverage matching must treat as a hit.
        events = [e async for e in stream_bot(bot, query(
            messages=(BotMessage("user", "I like basketball a lot."),)))]
        assert len(inner.calls) == 1 and bot.hits == 1
        assert all(e.request_id == "final" for e in events)
        await bot.aclose()

    asyncio.run(run())


def test_low_coverage_draft_is_never_adopted():
    async def run():
        inner = CountingBot()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        _ = [e async for e in stream_bot(bot, query(
            messages=(BotMessage("user", "I actually hate basketball."),)))]
        assert len(inner.calls) == 2 and bot.misses == 1 and bot.hits == 0
        await bot.aclose()

    asyncio.run(run())


@pytest.mark.parametrize('draft,final,adopt', [
    ('Tell me a fun fact about', 'Tell me a fun fact about cats.', False),
    ('What is twenty three plus forty', 'What is twenty three plus forty five?', False),
    ('I like basketball', 'I like basketball but not football.', False),
    ('Hello how are you', 'Hello how are you today?', True),
    ('I like basketball', 'I like basketball a lot.', True),
])
def test_english_content_guard_does_not_adopt_changed_question(draft, final, adopt):
    from backend.english_chat import EnglishChatBot
    async def run():
        class ContentBot(CountingBot):
            can_adopt = staticmethod(EnglishChatBot.can_adopt)
        inner = ContentBot()
        bot = SpeculativeTextBot(inner)
        try:
            await bot.prepare(query(request_id='draft', messages=(BotMessage('user', draft),)))
            await asyncio.sleep(.01)
            _ = [e async for e in stream_bot(bot, query(messages=(BotMessage('user', final),)))]
            assert (bot.hits == 1) == adopt
            assert len(inner.calls) == (1 if adopt else 2)
        finally:
            await bot.aclose()
    asyncio.run(run())


FINAL_NEAR_MISS = "I like basketball and also tennis."  # draft trails by half


def test_grace_window_adopts_finished_near_miss():
    async def run():
        inner = CountingBot()
        bot = SpeculativeTextBot(inner)
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        events = [e async for e in stream_bot(bot, query(
            messages=(BotMessage("user", FINAL_NEAR_MISS),)))]
        assert len(inner.calls) == 1 and bot.hits == 1 and bot.misses == 0
        assert bot.last_cache_detail["matchedCoverage"] < bot.prefix_coverage
        assert all(e.request_id == "final" for e in events)
        await bot.aclose()

    asyncio.run(run())


def test_grace_window_waits_for_inflight_near_miss():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        class Slow(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                started.set()
                yield BotEvent(request.request_id, "text_delta", text="That's fun. ")
                await release.wait()
                yield BotEvent(request.request_id, "done")
        inner = Slow()
        bot = SpeculativeTextBot(inner)
        bot.grace_wait_s = .5
        await bot.prepare(query(request_id="draft"))
        await started.wait()
        stream = stream_bot(bot, query(messages=(BotMessage("user", FINAL_NEAR_MISS),)))

        async def release_soon():
            await asyncio.sleep(.1)
            release.set()
        asyncio.create_task(release_soon())
        events = [e async for e in stream]
        assert len(inner.calls) == 1 and bot.hits == 1
        # The release task is scheduled before the grace clock is sampled.
        assert bot.last_cache_detail["graceWaitMs"] >= 80
        assert all(e.request_id == "final" for e in events)
        await bot.aclose()

    asyncio.run(run())


def test_grace_window_timeout_falls_back_to_fresh_request():
    async def run():
        class Hanging(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                if len(self.calls) == 1:
                    await asyncio.Event().wait()
                    return
                yield BotEvent(request.request_id, "text_delta", text="Fresh answer.")
                yield BotEvent(request.request_id, "done")
        inner = Hanging()
        bot = SpeculativeTextBot(inner)
        bot.grace_wait_s = .4
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(0)  # let the draft start, still in flight
        assert not bot._slots or not all(s["task"].done() for s in bot._slots.values())
        events = [e async for e in stream_bot(bot, query(
            messages=(BotMessage("user", FINAL_NEAR_MISS),)))]
        assert len(inner.calls) == 2 and bot.hits == 0 and bot.misses == 1
        assert bot.last_cache_detail["graceWaitMs"] >= 400
        assert events[0].text == "Fresh answer."
        await bot.aclose()
        assert not bot._tasks

    asyncio.run(run())


def test_grace_window_disabled_when_threshold_at_prefix_coverage():
    async def run():
        inner = CountingBot()
        bot = SpeculativeTextBot(inner)
        bot.grace_coverage = bot.prefix_coverage
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(.01)
        _ = [e async for e in stream_bot(bot, query(
            messages=(BotMessage("user", FINAL_NEAR_MISS),)))]
        assert len(inner.calls) == 2 and bot.misses == 1 and bot.hits == 0
        await bot.aclose()

    asyncio.run(run())


def test_grace_window_adopts_on_first_landing_not_all_done():
    async def run():
        gates = [asyncio.Event(), asyncio.Event()]
        class TwoDraft(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                await gates[len(self.calls) - 1].wait()
                yield BotEvent(request.request_id, "text_delta", text="That's fun. ")
                yield BotEvent(request.request_id, "done")
        inner = TwoDraft()
        bot = SpeculativeTextBot(inner)
        bot.grace_wait_s = .5
        bot.min_interval = 0
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(0)
        await bot.prepare(query(request_id="draft2",
                                messages=(BotMessage("user", "I like basketball a"),)))
        await asyncio.sleep(.01)
        assert len(inner.calls) == 2

        async def release_first():
            await asyncio.sleep(.1)
            gates[0].set()
        asyncio.create_task(release_first())
        events = [e async for e in stream_bot(bot, query(
            messages=(BotMessage("user", FINAL_NEAR_MISS),)))]
        assert len(inner.calls) == 2 and bot.hits == 1
        # The first draft lands at ~100ms; adoption must not wait for the
        # second (still-blocked) draft or the full 500ms window.
        assert bot.last_cache_detail["graceWaitMs"] < 300
        assert all(e.request_id == "final" for e in events)
        await bot.aclose()

    asyncio.run(run())


def test_grace_wait_cancelled_slot_is_never_adopted():
    async def run():
        class HangThenFresh(CountingBot):
            async def stream(self, request):
                self.calls.append(request)
                if len(self.calls) == 1:
                    await asyncio.Event().wait()
                    return
                yield BotEvent(request.request_id, "text_delta", text="Fresh answer.")
                yield BotEvent(request.request_id, "done")
        inner = HangThenFresh()
        bot = SpeculativeTextBot(inner)
        bot.grace_wait_s = .5
        await bot.prepare(query(request_id="draft"))
        await asyncio.sleep(0)

        async def consume():
            return [e async for e in stream_bot(bot, query(
                messages=(BotMessage("user", FINAL_NEAR_MISS),)))]
        consumer = asyncio.create_task(consume())
        await asyncio.sleep(.05)
        # A new turn arrives mid-grace and cancels everything in flight.
        await bot.cancel_pending()
        events = await consumer
        assert bot.hits == 0 and bot.misses == 1
        assert len(inner.calls) == 2 and events[0].text == "Fresh answer."
        assert bot.last_cache_detail["graceAdopted"] is False
        await bot.aclose()
        assert not bot._tasks

    asyncio.run(run())
