import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from backend.english_chat import EnglishChatBot
from backend.pcm_stream import executor_chunks
from backend.speculative_bot import SpeculativeTextBot
from backend.text_bot import BotMessage, stream_bot
from tests.test_speculative_bot import CountingBot, query
from tests.test_bot_service import Recorder, query as service_query, fact, reply
from backend.bot_service import BotService
from backend.speech_text import take_speech_segments


def test_short_first_pcm_segment_preserves_complete_confirmed_text_and_numbers():
    text = 'I can help you practice English through friendly conversations. '
    chunks, tail = take_speech_segments(text, first_chunk=True)
    assert ''.join(chunks) + tail == text
    assert len(chunks[0]) <= 37
    numeric = 'Twenty three plus forty five is sixty eight. '
    chunks, tail = take_speech_segments(numeric, first_chunk=True)
    assert chunks == [numeric.strip()] and tail == ' '


@pytest.mark.parametrize('draft,final,adopt', [
    ('can you tell me a joke', 'can you tell me a joke please', True),
    ('can you tell me a joke', 'well can you tell me a joke', True),
    ('can you tell me a joke', 'can you tell me a joke if you can', True),
    ('tell me', 'tell me a joke', False),
    ('what can you help me', 'what can you help me with', True),
    ('can you help me', 'can you help me with cats', False),
    ('tell me a fact about', 'tell me a fact about cats', False),
    ('what is twenty three plus forty', 'what is twenty three plus forty five', False),
    ('i like cats', 'i like cats not dogs', False),
    ('i like cats', 'i like cats if they are quiet', False),
])
def test_policy_reuse_never_drops_objects_numbers_or_conditions(draft, final, adopt):
    assert EnglishChatBot.can_adopt(draft, final) == adopt


def test_rejected_candidate_retains_raw_coverage_without_grace_wait():
    async def run():
        class Bot(CountingBot):
            can_adopt = staticmethod(EnglishChatBot.can_adopt)
            adoption_reason = staticmethod(EnglishChatBot.adoption_reason)
        bot = SpeculativeTextBot(Bot())
        try:
            await bot.prepare(query(request_id='draft', messages=(BotMessage('user', 'What is twenty three plus forty'),)))
            await asyncio.sleep(.01)
            _ = [e async for e in stream_bot(bot, query(messages=(BotMessage('user', 'What is twenty three plus forty five?'),)))]
            assert bot.last_cache_detail['rawBestCoverage'] > .6
            assert bot.last_cache_detail['eligibleBestCoverage'] == 0
            assert bot.last_cache_detail['rejectedByPolicy'] == 1
            assert bot.last_cache_detail['policyReasons'] == {'number_changed': 1}
            assert not bot.last_cache_detail['graceAttempted']
        finally:
            await bot.aclose()
    asyncio.run(run())


def test_ready_safe_candidate_beats_new_pending_exact_candidate():
    from backend.text_bot import BotEvent
    async def run():
        release = asyncio.Event()
        class Bot(CountingBot):
            can_adopt = staticmethod(EnglishChatBot.can_adopt)
            async def stream(self, request):
                self.calls.append(request)
                if request.request_id == 'new':
                    await release.wait()
                yield BotEvent(request.request_id, 'text_delta', text='Hello there. ')
                yield BotEvent(request.request_id, 'done')
        bot = SpeculativeTextBot(Bot())
        bot.min_interval = 0
        try:
            await bot.prepare(query(request_id='old', messages=(BotMessage('user','hello how are you'),)))
            await asyncio.sleep(.01)
            await bot.prepare(query(request_id='new', messages=(BotMessage('user','hello how are you today'),)))
            await asyncio.sleep(0)
            stream = stream_bot(bot, query(messages=(BotMessage('user','hello how are you today'),)))
            first = await asyncio.wait_for(anext(stream), .1)
            assert first.text == 'Hello there. ' and not release.is_set()
            assert bot.last_cache_detail['adoptedReady']
            assert bot.last_cache_detail['matchedCoverage'] < 1
            assert bot.last_cache_detail['eligibleBestCoverage'] == 1
            await stream.aclose()
        finally:
            release.set()
            await bot.aclose()
    asyncio.run(run())


def test_fragmentary_partials_do_not_exhaust_the_prefetch_budget():
    async def run():
        class Bot(CountingBot):
            can_prepare = staticmethod(EnglishChatBot.can_prepare)
        bot = SpeculativeTextBot(Bot())
        try:
            for text in ['tell', 'tell me', 'tell me a fun', 'tell me a fun fact about']:
                await bot.prepare(query(messages=(BotMessage('user', text),)))
            assert bot.prefetches == 0
            await bot.prepare(query(messages=(BotMessage('user', 'tell me a fun fact about cats'),)))
            await asyncio.sleep(.01)
            _ = [e async for e in stream_bot(bot, query(messages=(BotMessage('user', 'tell me a fun fact about cats'),)))]
            assert bot.hits == 1 and bot.prefetches == 1
        finally:
            await bot.aclose()
    asyncio.run(run())


def test_final_stop_receipt_avoids_grace_without_losing_unheard_context(monkeypatch):
    monkeypatch.setenv('BOT_STOP_GRACE_MS', '500')
    async def run():
        service, bot = BotService(), Recorder()
        try:
            await reply(service, bot, service_query())
            receipts = (fact('generation_cancelled', sequence=3),
                        fact('playback_stopped', sequence=4, playedMs=0))
            q = replace(service_query('q2'), generation_epoch=1, interactions=receipts)
            run = await service.start(q, bot)
            _ = [e async for e in service.events(run)]
            assert run.timing.metrics['deliveryWaitMs'] < 100
            assert run.timing.metrics['snapshotMs'] < 100
            assert not [m for m in bot.calls[-1].messages if m.role == 'assistant']
            assert bot.calls[-1].delivery_context
        finally:
            await service.close()
    asyncio.run(run())


def test_native_pcm_first_chunk_is_delivered_before_full_synthesis():
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    def native():
        yield b'first'
        assert release.wait(2)
        yield b'last'
    async def run():
        stream = executor_chunks(executor, native)
        try:
            assert await asyncio.wait_for(anext(stream), .5) == b'first'
            assert not release.is_set()
            release.set()
            assert [p async for p in stream] == [b'last']
        finally:
            release.set()
            await stream.aclose()
    try:
        asyncio.run(run())
    finally:
        executor.shutdown(wait=True)


def test_pcm_consumer_disconnect_releases_bounded_worker_and_propagates_error():
    executor = ThreadPoolExecutor(max_workers=1)
    closed = threading.Event()
    def many():
        try:
            for _ in range(100):
                yield b'pcm'
        finally:
            closed.set()
    def failed():
        yield b'prefix'
        raise RuntimeError('synthesis failed')
    async def run():
        stream = executor_chunks(executor, many)
        assert await anext(stream) == b'pcm'
        await stream.aclose()
        assert await asyncio.to_thread(closed.wait, 1)
        stream = executor_chunks(executor, failed)
        with pytest.raises(RuntimeError, match='synthesis failed'):
            _ = [p async for p in stream]
    try:
        asyncio.run(run())
    finally:
        executor.shutdown(wait=True)


def test_late_cloud_partial_prepares_while_stop_waits_without_publishing(monkeypatch):
    from backend.atomic import AsrVadResult
    from backend.protocol import AudioFrame
    from tests.test_duplex import make_stub_duplex
    monkeypatch.setenv('BOT_IDLE_SECONDS', '0')
    async def run():
        release, prepared = asyncio.Event(), asyncio.Event()
        calls = []
        class Asr:
            async def feed(self, *args):
                await release.wait()
                return AsrVadResult(100, 3, 0, 100, 'Tell me about cats.')
            def peek_partial(self):
                return 'Tell me about cats.'
            async def reset(self):
                pass
        d = make_stub_duplex(asr=Asr())
        await d.start()
        d._active_user_turn_id = 'late_cloud_turn'
        d.prefetch.offer = lambda q: (calls.append(q), prepared.set(), True)[-1]
        task = asyncio.create_task(d._feed_with_partials(AudioFrame(0,0,0,0,b'\0'*3200)))
        try:
            await asyncio.wait_for(prepared.wait(), .3)
            await asyncio.sleep(.06)
            assert len(calls) == 1  # Unchanged text/facts do not repeat snapshots.
            assert d._active_reply_id is None and d._media_outputs.empty()
            release.set()
            assert (await task).status == 3
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await d.close('test_done')
    asyncio.run(run())


def test_cancelling_asr_wait_closes_partial_bridge_and_never_prepares_later(monkeypatch):
    from backend.protocol import AudioFrame
    from tests.test_duplex import make_stub_duplex
    monkeypatch.setenv('BOT_IDLE_SECONDS', '0')
    async def run():
        closed, prepared = asyncio.Event(), asyncio.Event()
        calls = []
        class Asr:
            async def feed(self, *args):
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
            def peek_partial(self):
                return 'Hello there.'
            async def reset(self):
                pass
        d = make_stub_duplex(asr=Asr())
        await d.start()
        d._active_user_turn_id = 'cancel_cloud_turn'
        d.prefetch.offer = lambda q: (calls.append(q), prepared.set(), True)[-1]
        task = asyncio.create_task(d._feed_with_partials(AudioFrame(0,0,0,0,b'\0'*3200)))
        try:
            await asyncio.wait_for(prepared.wait(), .3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert closed.is_set()
            await asyncio.sleep(.1)
            assert len(calls) == 1 and d._active_reply_id is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await d.close('test_done')
    asyncio.run(run())
