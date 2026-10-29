"""Deterministic failures at transport, provider and journal boundaries."""
import asyncio
import json

import pytest

from backend.app import WebSocketConnection
from backend.atomic import AliyunNlsAsrVadAdapter
from backend.bot_contract import ConversationRequest
from backend.bot_service import BotService
from backend.bot_store import BotStore
from backend.domain import Session, SessionState
from backend.duplex import DuplexEvent
from backend.english_chat import EnglishChatBot
from backend.text_bot import BotEvent, TextBotError
from tests.test_duplex import make_stub_duplex


class Reply:
    protocol_version = 1

    async def stream(self, request):
        yield BotEvent(request.request_id, 'text_delta', text='Hello. ')
        yield BotEvent(request.request_id, 'done')


def request(name, session='review', **kwargs):
    return ConversationRequest(name, session, name, text='Hello', **kwargs)


def test_send_backpressure_never_emits_invalidated_audio_or_consumes_wire_sequence():
    async def run():
        gate, ready = asyncio.Event(), asyncio.Event()
        order = []
        class WS:
            closed = False
            async def send_str(self, text):
                kind = json.loads(text)['type']
                order.append(kind)
                if kind == 'avatar_frame':
                    ready.set()
                    await gate.wait()
            async def send_bytes(self, data):
                order.append('old_audio')
        class Duplex:
            async def events(self):
                yield DuplexEvent('playback_interrupt', {'replyId':'old', 'turnId':'t', 'generationEpoch':1})
            async def mark_packet_sent(self, *args):
                pass
        c = WebSocketConnection(WS())
        c.session, c.duplex = Session('review', SessionState.ON_SERVICE), Duplex()
        await c.media_cache.put({'replyId':'old', 'turnId':'t', 'generationEpoch':1, 'sequence':0,
                                'avatarState':'speaking', 'speechTime':0, 'audio':b'\0'*3200})
        ticker = asyncio.create_task(c.playback_ticker())
        await asyncio.wait_for(ready.wait(), 1)
        control = asyncio.create_task(c.forward_control_events())
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(control, 1)
        await asyncio.sleep(.02)
        c.closed = True
        await asyncio.wait_for(ticker, 1)
        assert 'old_audio' not in order
        assert c.output_sequence == 0
    asyncio.run(run())


def test_asr_cleanup_timeout_is_fatal_even_if_fields_were_cleared():
    from backend.atomic import AsrUnavailable
    async def run():
        a = AliyunNlsAsrVadAdapter(transcriber_factory=lambda **kwargs: None)
        def fail(*args):
            raise RuntimeError('provider failed')
        a._feed_sync = fail
        async def timeout():
            a._finish_utterance()
            raise asyncio.TimeoutError()
        a.reset = timeout
        for _ in range(2):
            with pytest.raises(AsrUnavailable):
                await a.feed(b'\0'*3200, 100)
    asyncio.run(run())


def test_http_prepare_failure_releases_eager_subscription(monkeypatch):
    from backend import text_api
    from backend.accounts import Accounts
    from backend.app import ResourceManager
    async def run():
        s = BotService()
        manager = ResourceManager(1)
        class Request:
            app = {'bot_service': s, 'resource_manager': manager, 'text_bot_factory': Reply,
                   'accounts': Accounts()}
            async def json(self):
                return {'version':2, 'sessionId':'prepare', 'requestId':'r', 'turnId':'r',
                        'text':'Hello', 'channel':'text', 'options':{'grade':7}}
        class Response:
            def __init__(self, **kwargs):
                pass
            async def prepare(self, request):
                raise ConnectionResetError('disconnected before headers')
        monkeypatch.setattr(text_api.web, 'StreamResponse', Response)
        try:
            await text_api.text_bot_handler(Request())
            assert not s.runs and manager.in_use == 0
            assert (await s.store.get('prepare', 'r'))['status'] in {'done','error'}
        finally:
            await s.close()
    asyncio.run(run())


def test_keyed_lock_cancellation_releases_key_without_losing_mutual_exclusion():
    from backend.concurrency import KeyedLocks
    async def run():
        locks = KeyedLocks()
        async def waiter():
            async with locks.hold('same'):
                pass
        async with locks.hold('same'):
            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with locks.hold('different'):
                pass
        assert not locks.entries
    asyncio.run(run())


def test_replacing_or_cancelling_playback_deadline_cannot_fail_new_reply():
    from backend.reply_lifecycle import PlaybackDeadline
    async def run():
        clock = PlaybackDeadline(.02)
        calls = []
        async def expired(name):
            calls.append(name)
        clock.arm(lambda: expired('old'))
        await asyncio.sleep(0)
        clock.arm(lambda: expired('new'))
        await asyncio.sleep(.04)
        assert calls == ['new']
        clock.arm(lambda: expired('cancelled'))
        clock.cancel()
        await asyncio.sleep(.04)
        assert calls == ['new']
    asyncio.run(run())


def test_storage_failure_marks_readiness_unhealthy_and_http_retry_unavailable():
    from aiohttp import ClientSession
    from tests.test_text_api import server
    async def run():
        app, runner, url = await server(Reply)
        async def fail(*args):
            raise OSError('fixture disk full')
        app['bot_service'].store.save = fail
        payload = {'version':2,'sessionId':'disk','requestId':'r','turnId':'r',
                   'channel':'text','options':{'grade':7},'text':'Hello'}
        try:
            async with ClientSession() as client:
                async with client.post(url+'/api/text-bot/replies',json=payload) as response:
                    assert response.status == 200
                    assert await response.text() == ''  # No forged durable event.
                async with client.get(url+'/health') as response:
                    assert response.status == 503 and not (await response.json())['ok']
                async with client.post(url+'/api/text-bot/replies',json=payload) as response:
                    assert response.status == 503
                    assert (await response.json())['code'] == 'BOT_STORAGE_FAILED'
            assert app['resource_manager'].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_nls_failed_utterance_is_discarded_and_next_one_starts_fresh():
    async def run():
        a = AliyunNlsAsrVadAdapter(transcriber_factory=lambda **kwargs: None)
        a._ensure_dependencies = lambda: None
        events = iter([[[0,-1]], [[-1,200]], [[300,-1]], [[-1,500]]])
        a._generate_vad = lambda pcm: next(events)
        a._send_cloud_audio = lambda pcm: None
        starts = []
        class Recognition:
            def stop(self):
                if len(starts) == 1:
                    a._on_nls_error({'header': {'status':40000000, 'message':'fixture transient failure'}})
                else:
                    a._latest_text = 'Recovered.'
        def start():
            starts.append(1)
            a._recognition = Recognition()
        a._start_recognition = start
        await a.feed(b'\x01\0'*1600, 100)
        with pytest.raises(RuntimeError):
            await a.feed(b'\x01\0'*1600, 200)
        assert not a._in_speech
        await a.feed(b'\x01\0'*1600, 300)
        result = await a.feed(b'\x01\0'*1600, 500)
        assert len(starts) == 2 and result.text == 'Recovered.'
        await a.reset()
    asyncio.run(run())


def test_journal_failure_closes_subscribers_and_blocks_regeneration():
    class BrokenStore(BotStore):
        async def save(self, *args):
            raise OSError('fixture full disk')
    async def run():
        service = BotService(BrokenStore())
        try:
            q = request('broken')
            run = await service.start(q, Reply())
            stream = service.events(run)
            with pytest.raises(TextBotError) as error:
                await asyncio.wait_for(anext(stream), .3)
            assert error.value.code == 'BOT_STORAGE_FAILED'
            await stream.aclose()
            assert run.terminal
            with pytest.raises(TextBotError) as retry:
                await service.start(q, Reply())
            assert retry.value.code == 'BOT_STORAGE_FAILED'
        finally:
            await service.close()
    asyncio.run(run())


def test_no_playback_ack_expires_without_marking_content_heard_or_executing_action(monkeypatch):
    monkeypatch.setenv('PLAYBACK_ACK_TIMEOUT_SECONDS', '.05')
    async def run():
        d = make_stub_duplex(agent=EnglishChatBot())
        await d.start()
        await d.submit_command('hang_up')
        async def drain():
            async for e in d.media_events():
                await d.mark_packet_sent(e.payload['replyId'], e.payload['sequence'])
        task = asyncio.create_task(drain())
        try:
            async def failed():
                async for e in d.events():
                    if e.kind == 'reply_failed':
                        return e
            event = await asyncio.wait_for(failed(), 3)
            assert event.payload['code'] == 'PLAYBACK_ACK_TIMEOUT'
            assert d.session.state == SessionState.ON_SERVICE
            assert d._active_reply_id is None and d._pending_action is None
            assert not any(f['type'] == 'playback_completed' for f in d._bot_facts)
            await d.mark_playback_completed(event.payload['replyId'])
            assert d.session.state == SessionState.ON_SERVICE  # Late farewell ACK cannot hang up.
            d._playback_deadline.seconds = 1
            await d.submit_command('time_out')
            async def completed():
                async for e in d.events():
                    if e.kind == 'reply_complete':
                        return e
            next_reply = await asyncio.wait_for(completed(), 3)
            assert next_reply.payload['replyId'] != event.payload['replyId']
            await d.mark_playback_completed(next_reply.payload['replyId'])
            assert d._active_reply_id is None and d.session.state == SessionState.ON_SERVICE
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await d.close('test_done')
    asyncio.run(run())


def test_waiting_for_one_session_receipts_does_not_block_other_session_replay():
    async def run():
        s = BotService()
        try:
            for q in [request('a0', 'a'), request('b0', 'b')]:
                _ = [v async for v in s.events(await s.start(q, Reply()))]
            q = request('a1', 'a', generation_epoch=1, interactions=(
                {'eventId':'cancel', 'replyId':'a0', 'type':'generation_cancelled', 'sequence':1},))
            task = asyncio.create_task(s.start(q, Reply()))
            await asyncio.sleep(.01)
            replay = await asyncio.wait_for(s.start(request('b0', 'b'), Reply()), .05)
            assert replay.terminal
            active = await task
            _ = [v async for v in s.events(active)]
        finally:
            await s.close()
    asyncio.run(run())


def test_closing_subscription_before_first_read_cancels_last_producer():
    class Hanging:
        protocol_version = 1
        async def stream(self, request):
            await asyncio.Event().wait()
            yield BotEvent(request.request_id, 'done')
    async def run():
        s = BotService()
        try:
            run = await s.start(request('orphan'), Hanging())
            stream = s.events(run)
            await stream.aclose()
            assert run.task.done() and run.subscribers == 0
            assert (await s.store.get('review', 'orphan'))['status'] == 'error'
        finally:
            await s.close()
    asyncio.run(run())
