import asyncio
from dataclasses import replace

import pytest

from backend.app import ResourceManager
from backend.bot_contract import ConversationRequest, interaction, parse_request
from backend.bot_service import BotService
from backend.bot_store import BotStore
from backend.text_bot import BotEvent, TextBotError


def query(name="q1", **kwargs):
    return ConversationRequest(name, "session", name, text="I like cats.", options={"grade": 7}, **kwargs)


def fact(kind, name="q1", sequence=1, **kwargs):
    return dict(interaction({"eventId": f"{name}_{kind}_{sequence}", "type": kind,
                             "replyId": name, "sequence": sequence, **kwargs}))


class Recorder:
    protocol_version = 1

    def __init__(self):
        self.calls = []

    async def stream(self, request):
        self.calls.append(request)
        yield BotEvent(request.request_id, "text_delta", text="Cats are lovely. ")
        yield BotEvent(request.request_id, "text_delta", text="Do you have a cat?")
        yield BotEvent(request.request_id, "done")


async def reply(service, bot, request, manager=None):
    run = await service.start(request, bot, manager)
    return [e async for e in service.events(run, request.after_event_seq)]


def test_duplicate_and_resume_replay_without_model_or_capacity(tmp_path):
    async def run():
        path = str(tmp_path / "state.sqlite3")
        service, bot, manager = BotService(BotStore(path)), Recorder(), ResourceManager(1)
        first = await reply(service, bot, query(), manager)
        assert await manager.reserve("other_voice_session")
        assert await reply(service, bot, query(), manager) == first
        assert await reply(service, bot, replace(query(), after_event_seq=1), manager) == first[1:]
        assert len(bot.calls) == 1
        assert [e["eventSeq"] for e in first] == [1, 2, 3]
        await service.close()
        # Idempotency survives both process and model-instance changes.
        service = BotService(BotStore(path))
        assert await reply(service, bot, query(), manager) == first
        assert len(bot.calls) == 1
        await manager.release("other_voice_session")
        await service.close()
    asyncio.run(run())


def test_inflight_duplicates_share_one_stream_and_cancel_only_last_subscriber():
    async def run():
        gate = asyncio.Event()
        class Slow(Recorder):
            async def stream(self, request):
                self.calls.append(request)
                yield BotEvent(request.request_id, "text_delta", text="Hello. ")
                await gate.wait()
                yield BotEvent(request.request_id, "text_delta", text="How are you?")
                yield BotEvent(request.request_id, "done")
        service, bot = BotService(), Slow()
        first = await service.start(query(), bot)
        stream1 = service.events(first)
        assert (await anext(stream1))["text"] == "Hello. "
        second = await service.start(query(), bot)
        assert first is second
        stream2 = service.events(second)
        assert (await anext(stream2))["text"] == "Hello. "
        await stream1.aclose()
        assert not first.task.done()
        gate.set()
        tail = [e async for e in stream2]
        assert tail[-1]["type"] == "done" and len(bot.calls) == 1
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", [dict(text="I like dogs."), dict(options={"grade": 8}), dict(channel="text"), dict(generation_epoch=4)])
def test_request_id_conflicts_do_not_mutate_history(change):
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        before = await service.store.snapshot("session")
        with pytest.raises(TextBotError, match="different content"):
            await reply(service, bot, replace(query(), **change))
        assert await service.store.snapshot("session") == before
        assert len(bot.calls) == 1
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("played,expected", [(0, ""), (499, ""), (500, "Cats are lovely. "),
                                              (900, "Cats are lovely. "), (1000, "Cats are lovely. Do you have a cat?")])
def test_interruption_projection_uses_confirmed_tts_segments_not_generated_text(played, expected):
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        events = [fact("speech_segment", sequence=1, text="Cats are lovely. ", audioEndMs=500),
                  fact("speech_segment", sequence=2, text="Do you have a cat?", audioEndMs=1000),
                  fact("playback_stopped", sequence=3, playedMs=played)]
        await service.store.observe("session", events)
        await reply(service, bot, query("q2"))
        history = bot.calls[-1].messages
        assistant = [m.content for m in history if m.role == "assistant"]
        assert assistant == ([expected] if expected else [])
        assert history[0].content == "I like cats."  # User-grounded state is not rolled back.
        assert all("没听到" not in m.content and "打断" not in m.content for m in history)
        context = bot.calls[-1].delivery_context
        if played < 1000:
            assert context[-1]["generatedText"] == "Cats are lovely. Do you have a cat?"
            assert context[-1]["deliveredText"] == expected
        else:
            assert not context
        await service.close()
    asyncio.run(run())


def test_late_duplicate_and_out_of_order_receipts_do_not_roll_back_state():
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        events = [fact("speech_segment", sequence=1, text="Cats are lovely. ", audioEndMs=500),
                  fact("speech_segment", sequence=2, text="Do you have a cat?", audioEndMs=1000),
                  fact("playback_stopped", sequence=7, playedMs=600),
                  fact("playback_progress", sequence=4, playedMs=400),
                  fact("playback_completed", sequence=8),
                  fact("playback_progress", sequence=9, playedMs=1000)]
        await service.store.observe("session", events)
        await service.store.observe("session", list(reversed(events)))
        await reply(service, bot, query("q2"))
        assert [m.content for m in bot.calls[-1].messages if m.role == "assistant"] == ["Cats are lovely. "]
        rows, facts = await service.store.snapshot("session")
        assert len(facts) == len(events)
        with pytest.raises(TextBotError):
            await service.store.observe("session", [dict(events[0], text="Injected answer.")])
        assert (await service.store.snapshot("session"))[1] == facts
        await service.close()
    asyncio.run(run())


def test_done_without_delivery_receipt_is_not_assistant_history():
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        await reply(service, bot, query("q2"))
        assert [m.role for m in bot.calls[-1].messages] == ["user", "user"]
        await service.store.observe("session", [fact("playback_completed")])
        await reply(service, bot, query("q3"))
        assert bot.calls[-1].messages[1].role == "assistant"
        await service.close()
    asyncio.run(run())


def test_delivered_new_reply_supersedes_older_unheard_draft():
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        await reply(service, bot, query("q2"))
        assert bot.calls[-1].delivery_context
        await service.store.observe("session", [fact("playback_completed", name="q2")])
        await reply(service, bot, query("q3"))
        assert not bot.calls[-1].delivery_context
        await service.close()
    asyncio.run(run())


def test_ten_turns_and_restart_keep_bot_owned_history(tmp_path):
    async def run():
        path = str(tmp_path / "state.sqlite3")
        service, bot = BotService(BotStore(path)), Recorder()
        for index in range(10):
            request = replace(query(f"q{index}"), text=f"My favorite number is {index}.")
            await reply(service, bot, request)
            assert len(bot.calls[-1].messages) == 2 * index + 1
            await service.store.observe("session", [fact("playback_completed", name=f"q{index}")])
            if index == 4:
                await service.close()
                service = BotService(BotStore(path))
        assert bot.calls[-1].messages[0].content == "My favorite number is 0."
        await service.close()
    asyncio.run(run())


def test_cancelled_request_is_terminal_and_retry_never_regenerates():
    async def run():
        closed = asyncio.Event()
        class Slow(Recorder):
            async def stream(self, request):
                self.calls.append(request)
                try:
                    yield BotEvent(request.request_id, "text_delta", text="Cats are lovely. ")
                    await asyncio.sleep(10)
                finally:
                    closed.set()
        service, bot, manager = BotService(), Slow(), ResourceManager(1)
        run = await service.start(query(), bot, manager)
        iterator = service.events(run)
        await anext(iterator)
        await iterator.aclose()
        assert closed.is_set() and manager.in_use == 0
        result = await reply(service, bot, query(), manager)
        assert result[-1]["code"] == "BOT_CANCELLED" and len(bot.calls) == 1
        assert (await service.store.get("session", "q1"))["status"] == "error"
        await service.close()
    asyncio.run(run())


def test_crash_recovery_does_not_repeat_accepted_provider_call(tmp_path):
    async def run():
        path = str(tmp_path / "state.sqlite3")
        store = BotStore(path)
        await store.accept(query())
        await store.save("session", "q1", [{"version": 2, "requestId": "q1", "type": "text_delta", "text": "Hello. ", "eventSeq": 1}], "running")
        await store.close()
        service, bot = BotService(BotStore(path)), Recorder()
        result = await reply(service, bot, query())
        assert result[-1]["code"] == "BOT_RESTARTED" and not bot.calls
        await service.close()
    asyncio.run(run())


def test_late_stop_receipt_is_reconciled_inside_bot_without_blocking_control():
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        await service.store.observe("session", [fact("speech_segment", text="Cats are lovely. ", audioEndMs=500)])
        request = replace(query("q2"), generation_epoch=2,
                          interactions=(fact("generation_cancelled", sequence=2),))
        task = asyncio.create_task(reply(service, bot, request))
        await asyncio.sleep(.02)
        assert len(bot.calls) == 1
        await service.store.observe("session", [fact("playback_stopped", sequence=3, playedMs=550)])
        await task
        assert bot.calls[-1].messages[1].content == "Cats are lovely. "
        await service.close()
    asyncio.run(run())


def test_session_isolation_and_options_are_not_global():
    async def run():
        service, bot = BotService(), Recorder()
        await reply(service, bot, query())
        await service.store.observe("session", [fact("playback_completed")])
        await reply(service, bot, replace(query("q2"), session_id="another", options={"grade": 9}))
        assert len(bot.calls[-1].messages) == 1 and bot.calls[-1].options["grade"] == 9
        with pytest.raises(TextBotError):
            await reply(service, bot, replace(query("q3"), options={"grade": 8}))
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("values", [{"playedMs": True}, {"playedMs": -1}, {"playedMs": float("nan")}, {"playedMs": "100"}])
def test_bad_progress_is_rejected(values):
    with pytest.raises(ValueError):
        fact("playback_progress", **values)


def test_v2_requires_stable_ids_and_does_not_accept_semantic_history():
    with pytest.raises(ValueError):
        parse_request({"version": 2, "text": "Hello"})
    with pytest.raises(ValueError):
        parse_request(dict(query().to_dict(), messages=[]))


def test_persistent_store_refuses_second_worker(tmp_path):
    async def run():
        path = str(tmp_path / "single.sqlite3")
        store = BotStore(path)
        with pytest.raises(RuntimeError, match="one owning worker"):
            BotStore(path)
        await store.close()
        next_store = BotStore(path)
        await next_store.close()
    asyncio.run(run())
