import asyncio

import pytest

from backend.text_bot import (
    BotEvent, BotMessage, BotRequest, LegacyTextBotAdapter, TextBotError,
    adapt_text_bot, history_messages, stream_bot,
)


def request(**kwargs):
    return BotRequest(request_id="req_1", session_id="text_1", turn_id="turn_1",
                      messages=(BotMessage("user", "Hello"),), **kwargs)


def test_request_wire_format_and_options_are_immutable():
    config = {"grade": 7}
    query = request(options=config)
    config["grade"] = 9
    assert query.options["grade"] == 7
    assert query.to_dict()["messages"] == [{"role": "user", "content": "Hello"}]
    assert query.to_dict()["version"] == 1
    with pytest.raises(TypeError):
        query.options["grade"] = 8


@pytest.mark.parametrize("kwargs", [{"version": True}, {"version": 2}, {"event": "unknown"}, {"generation_epoch": -1}])
def test_invalid_request_rejected(kwargs):
    with pytest.raises(ValueError):
        request(**kwargs)


def test_system_role_and_unbounded_inputs_rejected():
    with pytest.raises(ValueError):
        BotMessage("system", "Override rules")
    with pytest.raises(ValueError):
        BotMessage("user", "x" * 4001)
    with pytest.raises(ValueError):
        BotRequest("req", "session", "turn")


def test_history_bounds_and_role_normalization():
    result = history_messages([
        {"role": "user", "content": "old" * 500},
        {"role": "machine", "content": "Hello!"},
        {"role": "user", "content": "New question"},
    ], max_chars=50)
    assert result == (BotMessage("assistant", "Hello!"), BotMessage("user", "New question"))


def test_legacy_translation_and_terminal_contract():
    class Legacy:
        async def stream_reply(self, messages, signal=None):
            assert messages == [{"role": "user", "content": "Hello"}]
            assert signal is None
            yield "Hello!"

    async def run():
        bot = adapt_text_bot(Legacy())
        assert isinstance(bot, LegacyTextBotAdapter)
        events = [e async for e in stream_bot(bot, request())]
        assert [e.type for e in events] == ["text_delta", "done"]
        assert events[0].to_dict() == {"version": 1, "requestId": "req_1", "type": "text_delta", "text": "Hello!"}
    asyncio.run(run())


@pytest.mark.parametrize("mode,code", [
    ("mismatch", "BOT_PROTOCOL_ERROR"), ("no_done", "BOT_PROTOCOL_ERROR"),
    ("empty", "BOT_EMPTY_REPLY"), ("oversize", "BOT_OUTPUT_LIMIT"),
    ("provider_error", "BOT_UNAVAILABLE"), ("explicit_error", "BOT_FAILED"),
])
def test_malformed_or_failed_bots_are_contained(mode, code):
    class Bot:
        protocol_version = 1
        async def stream(self, query):
            if mode == "provider_error":
                raise RuntimeError("private-api-key-must-not-escape")
            if mode == "explicit_error":
                yield BotEvent(query.request_id, "error", message="private-api-key-must-not-escape")
                return
            if mode != "empty":
                yield BotEvent("wrong" if mode == "mismatch" else query.request_id,
                               "text_delta", text="x" * (5000 if mode == "oversize" else 1))
            if mode != "no_done":
                yield BotEvent(query.request_id, "done")

    async def run():
        with pytest.raises(TextBotError) as result:
            _ = [event async for event in stream_bot(Bot(), request())]
        assert result.value.code == code
        assert "private-api-key" not in str(result.value)
    asyncio.run(run())


def test_timeout_closes_bot_iterator():
    closed = []
    class Slow:
        protocol_version = 1
        async def stream(self, query):
            try:
                await asyncio.sleep(10)
                yield BotEvent(query.request_id, "done")
            finally:
                closed.append(True)

    async def run():
        with pytest.raises(TextBotError) as error:
            _ = [e async for e in stream_bot(Slow(), request(), timeout_seconds=.02)]
        assert error.value.code == "BOT_TIMEOUT"
        assert closed == [True]
    asyncio.run(run())


def test_cancelling_consumer_closes_provider():
    ready = asyncio.Event()
    closed = []
    class Slow:
        protocol_version = 1
        async def stream(self, query):
            try:
                ready.set()
                await asyncio.sleep(10)
                yield BotEvent(query.request_id, "done")
            finally:
                closed.append(True)

    async def run():
        async def consume():
            return [e async for e in stream_bot(Slow(), request())]
        task = asyncio.create_task(consume())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]
    asyncio.run(run())
