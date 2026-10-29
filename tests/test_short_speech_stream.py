"""Deterministic streaming/cancellation tests; no voice service or model loads."""

import asyncio
from dataclasses import replace

import pytest

from backend.bot_contract import ConversationRequest
from backend.bot_projection import project_request
from backend.bot_service import BotService
from backend.bot_store import BotStore
from backend.english_chat import EnglishChatBot
from backend.llm_runtime import Timing, use_timing
from backend.speech_buffer import SpeechBuffer, phrase_cut
from backend.text_bot import BotMessage, BotRequest, TextBotError, stream_bot
from backend.vad_prefetch import VadPrefetch
from tests.test_duplex import make_stub_duplex, collect_one_reply


def query(**kwargs):
    return BotRequest("r", "s", "t", messages=(BotMessage("user", "Tell me about cats"),),
                      channel="voice", **kwargs)


def test_existing_channel_projection_and_prefetch_identity_are_separate():
    v2 = ConversationRequest("r", "s", "t", text="Hello", channel="voice")
    voice = project_request(v2, [], [])
    text = project_request(replace(v2, channel="text"), [], [])
    assert voice.channel == "voice" and text.channel == "text"
    assert VadPrefetch.identity(voice) != VadPrefetch.identity(text)


def test_short_voice_text_precedes_complete_sentence_and_keeps_probe_meaning():
    async def run():
        release = asyncio.Event()
        class Model:
            def __init__(self, **kwargs):
                pass
            async def stream_reply(self, messages):
                yield "Cats enjoy chasing butterflies "
                await release.wait()
                yield "outside with friends."
        probe = Timing("r")
        with use_timing(probe):
            events = stream_bot(EnglishChatBot(Model), query())
            first = await asyncio.wait_for(anext(events), .5)
            assert first.text == "Cats enjoy chasing butterflies "
            assert "firstSpeechTextReadyMs" in probe.metrics
            assert "firstSentenceReadyMs" not in probe.metrics
            release.set()
            tail = [e async for e in events]
        assert first.text + "".join(e.text for e in tail) == "Cats enjoy chasing butterflies outside with friends. "
        assert tail[-1].type == "done" and "firstSentenceReadyMs" in probe.metrics
    asyncio.run(run())


@pytest.mark.parametrize("width", [1, 2, 5, 17, 200])
def test_word_boundaries_do_not_change_final_voice_answer(width):
    source = "I'm an AI cat, so I don't have a human age. I enjoy chatting about birthdays!"
    class Model:
        def __init__(self, **kwargs):
            pass
        async def stream_reply(self, messages):
            for i in range(0, len(source), width):
                yield source[i:i+width]
    async def run():
        events = [e async for e in stream_bot(EnglishChatBot(Model), query())]
        assert "".join(e.text for e in events).strip() == source
        assert events[-1].type == "done"
    asyncio.run(run())


def test_rejected_streamed_tail_fails_instead_of_faking_complete_or_fallback():
    class Model:
        def __init__(self, **kwargs):
            pass
        async def stream_reply(self, messages):
            yield "I'm an AI, so "
    async def run():
        seen = []
        with pytest.raises(TextBotError, match="finish consistently"):
            async for event in stream_bot(EnglishChatBot(Model), query()):
                seen.append(event)
        assert any(e.type == "text_delta" for e in seen)
        assert not any(e.type == "done" for e in seen)
        assert not any("lost that thought" in e.text for e in seen)
    asyncio.run(run())


def test_voice_short_start_can_be_disabled_without_changing_answer(monkeypatch):
    monkeypatch.setenv("VOICE_SHORT_START_ENABLED", "0")
    class Model:
        def __init__(self, **kwargs):
            pass
        async def stream_reply(self, messages):
            yield "Cats enjoy chasing butterflies "
            yield "outside with friends."
    async def run():
        events = [e async for e in stream_bot(EnglishChatBot(Model), query())]
        assert events[0].text == "Cats enjoy chasing butterflies outside with friends. "
    asyncio.run(run())


def test_short_start_still_hides_structured_output():
    class Model:
        def __init__(self, **kwargs):
            pass
        async def stream_reply(self, messages):
            for char in '{"answer": "private secret"}':
                yield char
    async def run():
        events = [e async for e in stream_bot(EnglishChatBot(Model), query())]
        assert "secret" not in "".join(e.text for e in events)
        assert events[-1].type == "done"
    asyncio.run(run())


def test_segment_target_grows_with_audio_reserve_and_shrinks_when_low():
    async def run():
        text = "Cats enjoy chasing butterflies outdoors during sunny mornings near quiet gardens behind wooden benches beside peaceful rivers daily. "
        for reserve, expected in [(1000, 10), (100, 6)]:
            buffer = SpeechBuffer(adaptive=True)
            await buffer.append(text)
            assert len((await buffer.next(lambda: 0)).split()) == 4
            assert len((await buffer.next(lambda: reserve)).split()) == expected
            await buffer.finish()
            while await buffer.next(lambda: reserve) is not None:
                pass
    asyncio.run(run())


def test_short_prefix_timeout_and_final_tail_are_not_lost():
    async def run():
        buffer = SpeechBuffer(adaptive=True, max_wait_ms=10)
        await buffer.append("Hello Kitty ")
        assert await asyncio.wait_for(buffer.next(lambda: 0), .3) == "Hello Kitty "
        await buffer.append("basketball")
        await buffer.finish()
        assert await buffer.next(lambda: 0) == "basketball"
        assert await buffer.next(lambda: 0) is None
    asyncio.run(run())


@pytest.mark.parametrize("width", [1, 3, 11, 200])
def test_incremental_segments_conserve_text_and_do_not_split_words(width):
    async def run():
        source = "Dr. Smith likes basketball, and paid 3.14 dollars today. 你好，今天天气真不错。"
        buffer = SpeechBuffer(adaptive=True, max_wait_ms=1)
        result = []
        async def read():
            for i in range(0, len(source), width):
                await buffer.append(source[i:i+width])
                await asyncio.sleep(0)
            await buffer.finish()
        producer = asyncio.create_task(read())
        while (segment := await buffer.next(lambda: 900)) is not None:
            result.append(segment)
        await producer
        assert "".join(result) == source
        for offset in [sum(map(len, result[:i])) for i in range(1, len(result))]:
            assert not (source[offset-1].isascii() and source[offset-1].isalnum() and source[offset].isascii() and source[offset].isalnum())
        assert not any(part.endswith("3.") or part.startswith("14") for part in result)
    asyncio.run(run())


def test_phrase_cut_keeps_number_expression_together():
    text = "The answer is forty five apples. "
    cut = phrase_cut(text, 4)
    assert cut is not None and not text[:cut].rstrip().endswith("forty")


def test_model_reading_continues_while_first_tts_is_blocked():
    async def run():
        entered, release, model_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        class Model:
            def __init__(self, **kwargs):
                pass
            async def stream_reply(self, messages):
                yield "Cats enjoy chasing butterflies "
                await entered.wait()
                yield "outdoors during sunny mornings near quiet gardens behind wooden benches beside peaceful rivers daily."
                model_finished.set()
        class Tts:
            async def stream_pcm(self, text, speed=1):
                calls.append(text)
                if len(calls) == 1:
                    entered.set()
                    await release.wait()
                yield b"\0\0" * (16000 if len(calls) == 1 else 1600)
        dialogue = make_stub_duplex(agent=EnglishChatBot(Model), tts=Tts())
        try:
            await dialogue.start()
            await dialogue.submit_text_for_development("Tell me about cats")
            consume = asyncio.create_task(collect_one_reply(dialogue))
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(model_finished.wait(), 1)
            assert len(calls) == 1 and not release.is_set()
            assert len(calls[0].split()) == 4
            release.set()
            await asyncio.wait_for(consume, 2)
            assert len(calls[1].split()) >= 6
            assert "".join(calls).strip() == "Cats enjoy chasing butterflies outdoors during sunny mornings near quiet gardens behind wooden benches beside peaceful rivers daily."
        finally:
            release.set()
            await dialogue.close("test_done")
    asyncio.run(run())


def test_playback_estimate_is_not_history_and_ignores_other_reply():
    async def run():
        dialogue = make_stub_duplex()
        try:
            dialogue._active_reply_id = "r"
            dialogue._reply_pcm_bytes = 64000
            dialogue.reply_pipeline.note_playback("old", 1500)
            assert dialogue.reply_pipeline.remaining_playback_ms() == 2000
            dialogue.reply_pipeline.note_playback("r", 1500)
            assert 490 < dialogue.reply_pipeline.remaining_playback_ms() <= 500
            assert not dialogue._bot_facts
        finally:
            await dialogue.close("test_done")
    asyncio.run(run())


def test_incremental_voice_replay_is_durable_and_recovery_uses_only_heard_prefix(tmp_path):
    async def run():
        calls = []
        class Model:
            def __init__(self, **kwargs):
                pass
            async def stream_reply(self, messages):
                calls.append(messages)
                yield "Cats enjoy chasing butterflies "
                yield "outside with friends."
        path = str(tmp_path / "voice.sqlite3")
        service = BotService(BotStore(path))
        inner = EnglishChatBot(Model)
        request = ConversationRequest("r", "s", "t", text="Tell me about cats", channel="voice")
        async def collect():
            run = await service.start(request, inner)
            return [event async for event in service.events(run)]
        first = await collect()
        assert await collect() == first and len(calls) == 1
        assert first[0]["text"] == "Cats enjoy chasing butterflies "
        assert len([e for e in first if e["type"] == "text_delta"]) > 1
        await service.store.observe("s", [
            {"eventId": "segment", "type": "speech_segment", "replyId": "r", "sequence": 1,
             "text": first[0]["text"], "audioEndMs": 500},
            {"eventId": "stopped", "type": "playback_stopped", "replyId": "r", "sequence": 2, "playedMs": 500},
        ])
        projected = await service.model_query(ConversationRequest("next", "s", "next", text="Why?", channel="voice"))
        assert [m.content for m in projected.messages if m.role == "assistant"] == [first[0]["text"]]
        assert projected.delivery_context[-1]["deliveredText"] == first[0]["text"]
        assert "outside with friends" in projected.delivery_context[-1]["generatedText"]
        await service.close()
        service = BotService(BotStore(path))
        try:
            assert await collect() == first and len(calls) == 1
        finally:
            await service.close()
    asyncio.run(run())


def test_prefetch_adopts_short_prefix_then_refreshes_complete_sentence_metrics():
    async def run():
        release, started = asyncio.Event(), asyncio.Event()
        calls = []
        class Model:
            def __init__(self, **kwargs):
                pass
            async def stream_reply(self, messages):
                calls.append(messages)
                started.set()
                yield "Cats enjoy chasing butterflies "
                await release.wait()
                yield "outside with friends."
        service = BotService()
        prefetch = VadPrefetch(service.session(EnglishChatBot(Model)), enabled=True, stable_ms=0)
        request = ConversationRequest("r", "s", "t", text="Tell me about cats", channel="voice")
        try:
            prefetch.note_audio(b"\0" * 3200)
            prefetch.offer(request)
            await asyncio.wait_for(started.wait(), 1)
            async def ready():
                while prefetch._candidate.first_text_at is None:
                    await asyncio.sleep(0)
            await asyncio.wait_for(ready(), 1)
            prefetch.seal("t")
            stream = prefetch.stream(request)
            first = await asyncio.wait_for(anext(stream), 1)
            assert first.text == "Cats enjoy chasing butterflies "
            assert prefetch.last_outcome == "hit" and len(calls) == 1
            assert "candidateFirstSpeechTextReadyMs" in prefetch.last_cache_detail
            assert "candidateFirstSentenceReadyMs" not in prefetch.last_cache_detail
            release.set()
            tail = [e async for e in stream]
            assert tail[-1].type == "done"
            assert "candidateFirstSentenceReadyMs" in prefetch.last_cache_detail
        finally:
            release.set()
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


def test_empty_tts_cannot_create_heard_segment_or_success():
    async def run():
        class EmptyTts:
            async def stream_pcm(self, text, speed=1):
                if False:
                    yield b""
        dialogue = make_stub_duplex(tts=EmptyTts())
        try:
            await dialogue.start()
            await dialogue.pick_up()
            async def failed():
                async for event in dialogue.events():
                    assert event.kind != "reply_complete"
                    if event.kind == "reply_failed":
                        return event
            event = await asyncio.wait_for(failed(), 1)
            assert event.payload["code"] == "TTS_FAILED"
            assert not any(e["type"] == "speech_segment" for e in dialogue._bot_facts)
        finally:
            await dialogue.close("test_done")
    asyncio.run(run())


def test_interrupt_closes_text_and_tts_workers_and_fences_late_short_audio():
    async def run():
        entered, model_closed, tts_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Model:
            def __init__(self, **kwargs):
                pass
            async def stream_reply(self, messages):
                try:
                    yield "Cats enjoy chasing butterflies "
                    await asyncio.Event().wait()
                finally:
                    model_closed.set()
        class Tts:
            async def stream_pcm(self, text, speed=1):
                try:
                    entered.set()
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    yield b"\1\0" * 1600
                finally:
                    tts_closed.set()
        dialogue = make_stub_duplex(agent=EnglishChatBot(Model), tts=Tts())
        try:
            await dialogue.start()
            await dialogue.submit_text_for_development("Tell me about cats")
            await asyncio.wait_for(entered.wait(), 1)
            old_task = dialogue._reply_task
            await dialogue._interrupt()
            await asyncio.wait_for(old_task, 1)
            assert model_closed.is_set() and tts_closed.is_set()
            assert dialogue._active_reply_id is None and dialogue._media_outputs.empty()
            assert not any(e["type"] == "speech_segment" for e in dialogue._bot_facts)
        finally:
            await dialogue.close("test_done")
    asyncio.run(run())


@pytest.mark.parametrize("width", [1, 2, 7, 200])
@pytest.mark.parametrize("channel", ["text", "voice"])
def test_explicit_resume_removes_only_exact_heard_prefix(width, channel):
    source = "Dogs like to run and play outside. They enjoy treats after a good walk."
    class Model:
        def __init__(self, **kwargs):
            pass
        async def stream_reply(self, messages):
            for i in range(0, len(source), width):
                yield source[i:i+width]
    async def run():
        request = BotRequest("r", "s", "t", channel=channel,
            messages=(BotMessage("assistant", "Dogs like "), BotMessage("user", "Please finish what you were saying.")),
            delivery_context=({"replyId": "old", "generatedText": source,
                               "deliveredText": "Dogs like ", "status": "not_fully_delivered"},))
        events = [e async for e in stream_bot(EnglishChatBot(Model), request)]
        assert "".join(e.text for e in events).strip() == source.removeprefix("Dogs like ")
        assert events[-1].type == "done"
    asyncio.run(run())


def test_resume_filter_keeps_new_topics_and_mismatching_words_unchanged():
    from backend.continuation import without_delivered_prefix, continuation_prefix
    async def source(text):
        for char in text:
            yield char
    async def run():
        for text in ("Dogs likeable? No, dogs are lovable.", "Actually, cats sleep a lot."):
            assert "".join([x async for x in without_delivered_prefix(source(text), "Dogs like")]) == text
        request = replace(query(), delivery_context=({"replyId": "old", "generatedText": "Dogs like playing.",
                          "deliveredText": "Dogs like ", "status": "not_fully_delivered"},))
        assert continuation_prefix(request) == ""  # New topic: never trim its opening.
    asyncio.run(run())


def test_cancel_while_disambiguating_heard_prefix_closes_provider():
    from backend.continuation import without_delivered_prefix
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def source():
            try:
                yield "Dogs li"
                entered.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        stream = without_delivered_prefix(source(), "Dogs like")
        task = asyncio.create_task(anext(stream))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert closed.is_set()
    asyncio.run(run())
