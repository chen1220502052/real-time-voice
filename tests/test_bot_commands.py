import asyncio

import pytest

from backend.bot_contract import ConversationRequest
from backend.bot_service import BotService
from backend.bot_tasks import with_progress
from backend.english_chat import EnglishChatBot
from backend.text_bot import BotEvent, BotRequest, TextBotError, stream_bot
from backend.domain import SessionState
from tests.test_duplex import make_stub_duplex, collect_one_reply


def query(name, event='user_message', text='Hello', epoch=0):
    return ConversationRequest(name, 'commands', name, event=event, text=text,
                               channel='text', generation_epoch=epoch, options={'grade': 7})


def test_command_alias_idempotency_and_idle_policy():
    async def run():
        service = BotService()
        try:
            async def call(q):
                return [v async for v in service.events(await service.start(q, EnglishChatBot()))]
            opening = query('open', 'connect', '')
            events = await call(opening)
            assert events == await call(query('open', 'session_start', ''))
            assert 'Kitty' in events[0]['text']
            for i in range(1, 9):
                q = query('idle' + str(i), 'time_out', '')
                events = await call(q)
                assert [v['type'] for v in events] == ['text_delta', 'done']
                assert 'Are you' in events[0]['text']
                assert 'bye' not in events[0]['text'].lower()
                assert events == await call(q)
                await service.store.observe(q.session_id, ({'eventId': 'display_' + q.request_id,
                    'replyId': q.request_id, 'type': 'text_displayed', 'sequence': 1},))
            rows, _ = await service.store.snapshot('commands')
            assert all(not p['text'] for p, _, _ in rows)
        finally:
            await service.close()
    asyncio.run(run())


def test_worker_push_is_ordered_durable_idempotent_and_epoch_fenced():
    async def run():
        service = BotService()
        release = asyncio.Event()
        class Work:
            protocol_version = 1
            async def stream(self, request):
                await release.wait()
                yield BotEvent(request.request_id, 'text_delta', text='All done. ')
                yield BotEvent(request.request_id, 'done')
        try:
            q = query('task', epoch=4)
            run = await service.start(q, Work())
            push = await service.push_progress('commands', 'task', 4, 'step1', 'Still working. ')
            assert push == await service.push_progress('commands', 'task', 4, 'step1', 'Still working. ')
            with pytest.raises(TextBotError, match='epoch'):
                await service.push_progress('commands', 'task', 3, 'wrong', 'Still working. ')
            with pytest.raises(TextBotError):
                await service.push_progress('commands', 'task', 4, 'step1', 'Different text. ')
            release.set()
            events = [v async for v in service.events(run)]
            assert [v['eventSeq'] for v in events] == [1, 2, 3]
            assert events[0] == push and events[-1]['type'] == 'done'
            assert push == await service.push_progress('commands', 'task', 4, 'step1', 'Still working. ')
            with pytest.raises(TextBotError):
                await service.push_progress('commands', 'task', 4, 'late', 'Too late. ')
            replay = await service.start(q, Work())
            assert [v async for v in service.events(replay)] == events
        finally:
            await service.close()
    asyncio.run(run())


def test_progress_timer_keeps_single_work_and_cancels_it_on_interrupt():
    async def run():
        closed = asyncio.Event()
        calls = []
        async def work():
            calls.append(1)
            try:
                await asyncio.Event().wait()
                yield 'Done.'
            finally:
                closed.set()
        request = BotRequest('slow', 's', 't', event='session_start')
        iterator = with_progress(work(), request, delay=.01)
        assert (await asyncio.wait_for(anext(iterator), 1)).type == 'progress'
        assert len(calls) == 1
        await iterator.aclose()
        assert closed.is_set()
    asyncio.run(run())


@pytest.mark.parametrize('interrupt', [False, True])
def test_hangup_action_requires_current_playback_ack(interrupt):
    async def run():
        duplex = make_stub_duplex(agent=EnglishChatBot())
        await duplex.start()
        await duplex.submit_command('hang_up')
        async def media():
            async for event in duplex.media_events():
                await duplex.mark_packet_sent(event.payload['replyId'], event.payload['sequence'])
        task = asyncio.create_task(media())
        try:
            async def complete():
                async for event in duplex.events():
                    if event.kind == 'reply_complete':
                        return event.payload['replyId']
            reply = await asyncio.wait_for(complete(), 2)
            assert duplex.session.state == SessionState.ON_SERVICE
            if interrupt:
                await duplex._interrupt()
            await duplex.mark_playback_completed(reply)
            assert duplex.session.state == (SessionState.ON_SERVICE if interrupt else SessionState.HANG_UP)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await duplex.close('test_done')
    asyncio.run(run())


def test_idle_detector_does_not_speak_over_a_reply(monkeypatch):
    monkeypatch.setenv('BOT_IDLE_SECONDS', '.03')
    async def run():
        duplex = make_stub_duplex(agent=EnglishChatBot())
        await duplex.start()
        try:
            await asyncio.wait_for(collect_one_reply(duplex), 2)
            assert any('Are you still there?' in h.message for h in duplex.history.items)
        finally:
            await duplex.close('test_done')
    asyncio.run(run())


def test_repeated_idle_reminders_never_hang_up_after_playback(monkeypatch):
    monkeypatch.setenv('BOT_IDLE_SECONDS', '0')
    async def run():
        duplex = make_stub_duplex(agent=EnglishChatBot())
        await duplex.start()
        try:
            for _ in range(5):
                await duplex.submit_command('time_out')
                packets = await asyncio.wait_for(collect_one_reply(duplex), 3)
                assert packets
                assert duplex.session.state == SessionState.ON_SERVICE
                assert duplex._pending_action is None
            # Explicit end-of-call still retains its original structured action.
            await duplex.submit_command('hang_up')
            await asyncio.wait_for(collect_one_reply(duplex), 3)
            assert duplex.session.state == SessionState.HANG_UP
        finally:
            await duplex.close('test_done')
    asyncio.run(run())


def test_background_progress_reaches_tts_before_task_finishes_and_late_push_is_fenced():
    async def run():
        service = BotService()
        ready, release = asyncio.Event(), asyncio.Event()
        class TaskBot:
            protocol_version = 1
            async def stream(self, request):
                ready.set()
                await release.wait()
                yield BotEvent(request.request_id, 'text_delta', text='Here is the result. ')
                yield BotEvent(request.request_id, 'done')
        duplex = make_stub_duplex(agent=service.session(TaskBot()))
        await duplex.start()
        try:
            await duplex.submit_text_for_development('Please help with this task.')
            await asyncio.wait_for(ready.wait(), 1)
            reply, epoch = duplex._active_reply_id, duplex._epoch
            await service.push_progress(duplex.session.session_id, reply, epoch, 'working', 'Still working. ')
            packet = await asyncio.wait_for(duplex._media_outputs.get(), 1)
            assert packet.kind == 'playback_packet' and packet.payload['replyId'] == reply
            assert not release.is_set() and not duplex._reply_finished
            await duplex._interrupt()
            await asyncio.sleep(.02)
            with pytest.raises(TextBotError):
                await service.push_progress(duplex.session.session_id, reply, epoch, 'late', 'Too late. ')
            assert duplex._media_outputs.empty()
        finally:
            release.set()
            await duplex.close('test_done')
            await service.close()
    asyncio.run(run())
