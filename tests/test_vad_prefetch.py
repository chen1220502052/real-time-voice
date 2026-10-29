import asyncio
from dataclasses import replace

import pytest

from backend.bot_contract import ConversationRequest
from backend.bot_service import BotService
from backend.text_bot import BotEvent
from backend.vad_prefetch import VadPrefetch
from backend.llm_runtime import Timing, timing, use_timing


class ReadOnlyBot:
    protocol_version = 1
    side_effect_free = True

    def __init__(self, delay=.08):
        self.delay = delay
        self.calls = []

    async def stream(self, request):
        self.calls.append(request)
        await asyncio.sleep(self.delay)
        yield BotEvent(request.request_id, "text_delta", text="A complete answer. ")
        yield BotEvent(request.request_id, "done")


def query(text="Tell me about cats.", request_id="final", **kwargs):
    return ConversationRequest(request_id, "session", "turn", text=text, **kwargs)


async def candidate_started(prefetch, inner):
    async with asyncio.timeout(1):
        while not inner.calls:
            await asyncio.sleep(.001)


def test_exact_takeover_is_read_only_until_formal_accept_and_replays_idempotently():
    async def run():
        release = asyncio.Event()

        class InFlightBot(ReadOnlyBot):
            async def stream(self, request):
                self.calls.append(request)
                await release.wait()
                yield BotEvent(request.request_id, "text_delta", text="A complete answer. ")
                yield BotEvent(request.request_id, "done")

        service, inner = BotService(), InFlightBot()
        prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=0)
        try:
            prefetch.note_audio(b"\0" * 3200)
            assert prefetch.offer(query(request_id="draft"))
            await candidate_started(prefetch, inner)
            assert await service.store.snapshot("session") == ([], [])
            await asyncio.sleep(.04)
            prefetch.seal("turn")
            async def consume():
                return [event async for event in prefetch.stream(query(generation_epoch=1))]
            consumer = asyncio.create_task(consume())
            async with asyncio.timeout(1):
                while prefetch.last_outcome is None:
                    await asyncio.sleep(0)
            # Explicitly hold the first token beyond adoption. A fixed 80ms
            # model sleep could finish before seal on a busy developer laptop.
            await asyncio.sleep(.02)
            release.set()
            events = await consumer
            assert len(inner.calls) == 1
            assert [event.type for event in events] == ["text_delta", "done"]
            assert all(event.version == 2 and event.request_id == "final" for event in events)
            detail = prefetch.last_cache_detail
            assert prefetch.last_outcome == "hit" and detail["exactMatch"]
            assert detail["effectiveLeadMs"] >= 30
            assert detail["remainingWaitMs"] >= 10  # In-flight hits also save time.
            assert detail["adoptedReady"] is False
            replay = [event async for event in prefetch.stream(query(generation_epoch=1))]
            assert replay == events and len(inner.calls) == 1
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


@pytest.mark.parametrize("final", [
    "Tell me about cats, please.", "Tell me about dogs.", "Do not tell me about cats.",
    "Tell me about 45 cats.", "tell me about cats.", "Tell me about cats?",
])
def test_any_content_or_punctuation_change_misses_without_semantic_reuse(final):
    async def run():
        service, inner = BotService(), ReadOnlyBot(.01)
        prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=0)
        try:
            prefetch.note_audio(b"\0" * 3200)
            prefetch.offer(query(request_id="draft"))
            await candidate_started(prefetch, inner)
            prefetch.seal("turn")
            _ = [event async for event in prefetch.stream(query(text=final))]
            assert prefetch.last_outcome == "miss"
            assert len(inner.calls) == 2 and inner.calls[-1].messages[-1].content == final
            assert prefetch.last_cache_detail["effectiveLeadMs"] == 0
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


def test_projection_changes_reject_same_text_candidate():
    async def run():
        service, inner = BotService(), ReadOnlyBot(.01)
        session = service.session(inner)
        prefetch = VadPrefetch(session, enabled=True, stable_ms=0)
        original = session.generation_snapshot
        async def different_snapshot(q):
            return replace(await original(q), idle_count=99)
        session.generation_snapshot = different_snapshot
        try:
            prefetch.note_audio(b"\0" * 3200)
            prefetch.offer(query(request_id="draft"))
            await candidate_started(prefetch, inner)
            prefetch.seal("turn")
            _ = [event async for event in prefetch.stream(query())]
            assert prefetch.last_outcome == "miss" and len(inner.calls) == 2
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


def test_switch_off_runs_one_formal_call_and_unknown_stateful_bot_is_not_speculated(monkeypatch):
    async def run():
        for safe, enabled in [(True, False), (False, True)]:
            inner, service = ReadOnlyBot(.01), BotService()
            inner.side_effect_free = safe
            prefetch = VadPrefetch(service.session(inner), enabled=enabled, stable_ms=0)
            try:
                prefetch.note_audio(b"\0" * 3200)
                assert not prefetch.offer(query(request_id="draft"))
                prefetch.seal("turn")
                _ = [event async for event in prefetch.stream(query())]
                assert len(inner.calls) == 1
                assert prefetch.last_outcome == ("disabled" if not enabled else "unsupported")
            finally:
                await prefetch.aclose()
                await service.close()
    asyncio.run(run())


def test_quiet_gate_and_stability_debounce_do_not_generate_during_speech():
    async def run():
        service, inner = BotService(), ReadOnlyBot(.01)
        prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=20)
        try:
            prefetch.note_audio(b"\xff\x3f" * 1600)
            prefetch.offer(query(request_id="draft"))
            await asyncio.sleep(.04)
            assert not inner.calls
            prefetch.note_audio(b"\0" * 3200)
            await asyncio.sleep(.005)
            prefetch.note_audio(b"\xff\x3f" * 1600)
            await asyncio.sleep(.03)
            assert not inner.calls
            prefetch.note_audio(b"\0" * 3200)
            await candidate_started(prefetch, inner)
            assert len(inner.calls) == 1
            assert await service.store.snapshot("session") == ([], [])
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


def test_barge_in_cancels_candidate_and_same_turn_can_prepare_again():
    async def run():
        service, inner = BotService(), ReadOnlyBot(.3)
        prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=0)
        try:
            prefetch.note_audio(b"\0" * 3200)
            prefetch.offer(query(request_id="draft"))
            await candidate_started(prefetch, inner)
            old = prefetch._candidate.task
            prefetch.cancel_pending()
            await asyncio.gather(old, return_exceptions=True)
            assert old.cancelled() and prefetch._candidate is None
            assert prefetch.offer(query(request_id="new_draft", generation_epoch=2))
            async with asyncio.timeout(1):
                while len(inner.calls) < 2:
                    await asyncio.sleep(.001)
        finally:
            await prefetch.aclose()
            await service.close()
        assert not prefetch._tasks
    asyncio.run(run())


def test_config_new_switch_overrides_legacy_switch(monkeypatch):
    monkeypatch.delenv("LLM_SPECULATIVE", raising=False)
    monkeypatch.delenv("DUPLEX_PREFETCH_ENABLED", raising=False)
    assert not VadPrefetch(object()).enabled
    monkeypatch.setenv("LLM_SPECULATIVE", "1")
    monkeypatch.setenv("DUPLEX_PREFETCH_ENABLED", "0")
    assert not VadPrefetch(object()).enabled
    monkeypatch.setenv("LLM_SPECULATIVE", "0")
    monkeypatch.setenv("DUPLEX_PREFETCH_ENABLED", "1")
    assert VadPrefetch(object()).enabled


def test_late_text_update_does_not_restart_an_established_quiet_window():
    async def run():
        service, inner = BotService(), ReadOnlyBot(.3)
        prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=80)
        try:
            prefetch.note_audio(b"\0" * 3200)
            prefetch.offer(query(text="Tell me about", request_id="draft"))
            await candidate_started(prefetch, inner)
            prefetch.offer(query(request_id="latest"))
            async with asyncio.timeout(.04):
                while len(inner.calls) < 2:
                    await asyncio.sleep(.001)
            assert inner.calls[-1].messages[-1].content == "Tell me about cats."
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


def test_ready_hit_has_zero_remaining_wait_and_failed_candidate_falls_back():
    async def run():
        for fail in (False, True):
            class Model(ReadOnlyBot):
                async def stream(self, request):
                    if fail and request.request_id == "draft":
                        self.calls.append(request)
                        raise RuntimeError("speculation failed")
                    async for event in super().stream(request):
                        yield event
            service, inner = BotService(), Model(.001)
            prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=0)
            try:
                prefetch.note_audio(b"\0" * 3200)
                prefetch.offer(query(request_id="draft"))
                await candidate_started(prefetch, inner)
                await prefetch._candidate.task
                prefetch.seal("turn")
                events = [event async for event in prefetch.stream(query())]
                assert events[-1].type == "done"
                assert prefetch.last_outcome == ("miss" if fail else "hit")
                assert len(inner.calls) == (2 if fail else 1)
                if not fail:
                    assert prefetch.last_cache_detail["remainingWaitMs"] == 0
                    assert prefetch.last_cache_detail["adoptedReady"]
            finally:
                await prefetch.aclose()
                await service.close()
    asyncio.run(run())


def test_candidate_probe_is_isolated_and_records_raw_token_separately_from_sentence():
    async def run():
        class ProbedBot(ReadOnlyBot):
            async def stream(self, request):
                self.calls.append(request)
                probe = timing()
                probe.metrics.update(providerFirstTokenMs=7, firstSentenceReadyMs=12, promptChars=123)
                yield BotEvent(request.request_id, "text_delta", text="A complete answer. ")
                yield BotEvent(request.request_id, "done")
        service, inner = BotService(), ProbedBot()
        prefetch = VadPrefetch(service.session(inner), enabled=True, stable_ms=0)
        parent = Timing("formal")
        parent.metrics["providerFirstTokenMs"] = 999
        try:
            with use_timing(parent):
                prefetch.note_audio(b"\0" * 3200)
                prefetch.offer(query(request_id="draft"))
                await candidate_started(prefetch, inner)
                await prefetch._candidate.task
                prefetch.seal("turn")
                _ = [event async for event in prefetch.stream(query())]
            assert parent.metrics["providerFirstTokenMs"] == 999
            assert prefetch.last_cache_detail["candidateProviderFirstTokenMs"] == 7
            assert prefetch.last_cache_detail["candidateFirstSentenceReadyMs"] == 12
            assert prefetch.last_cache_detail["candidatePromptChars"] == 123
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())


def test_new_playback_fact_changes_projection_and_rejects_previous_candidate():
    async def run():
        service, inner = BotService(), ReadOnlyBot(.001)
        session = service.session(inner)
        prefetch = VadPrefetch(session, enabled=True, stable_ms=0)
        try:
            seed = ConversationRequest("seed", "session", "previous_turn", text="Hello.")
            _ = [event async for event in session.stream(seed)]
            prefetch.note_audio(b"\0" * 3200)
            prefetch.offer(query(request_id="draft"))
            async with asyncio.timeout(1):
                while len(inner.calls) < 2:
                    await asyncio.sleep(.001)
            await prefetch._candidate.task
            await session.observe("session", ({"eventId": "ack", "type": "playback_completed",
                                               "replyId": "seed", "sequence": 1},))
            prefetch.seal("turn")
            _ = [event async for event in prefetch.stream(query())]
            assert prefetch.last_outcome == "miss" and len(inner.calls) == 3
            assert any(message.role == "assistant" for message in inner.calls[-1].messages)
        finally:
            await prefetch.aclose()
            await service.close()
    asyncio.run(run())
