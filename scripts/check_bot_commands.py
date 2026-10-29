"""Real WebSocket/Piper command lifecycle; playback receipts are simulated."""
import argparse
import asyncio
import json
from pathlib import Path
import ssl
import uuid

import aiohttp
import certifi


async def check(url, auth_file=None):
    auth = None
    if auth_file:
        values = dict(line.split(':', 1) for line in auth_file.read_text().splitlines() if ':' in line)
        auth = aiohttp.BasicAuth(values['Username'].strip(), values['Password'].strip())
    report = {'mode': 'real_ws_piper_commands', 'playbackReceipts': 'simulated', 'errors': [], 'replies': []}
    async with aiohttp.ClientSession(auth=auth, connector=aiohttp.TCPConnector(
            ssl=ssl.create_default_context(cafile=certifi.where()))) as client:
        async with client.ws_connect(url) as ws:
            complete = asyncio.Queue()
            closed = asyncio.Event()
            replies = {}
            events = asyncio.Queue()
            async def reader():
                current = None
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        assert current is not None
                        replies[current]['audioFrames'] += 1
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        value = json.loads(message.data)
                        kind = value.get('type')
                        await events.put(value)
                        if kind == 'assistant_delta':
                            reply = value['replyId']
                            replies.setdefault(reply, {'replyId': reply, 'text': '', 'audioFrames': 0})['text'] += value['text']
                        elif kind == 'avatar_frame':
                            current = value['replyId']
                        elif kind == 'reply_complete':
                            await complete.put(value['replyId'])
                        elif kind == 'close_session':
                            closed.set()
                        elif kind in {'error', 'reply_failed'}:
                            report['errors'].append(value.get('code'))
            async def expect(kind):
                async with asyncio.timeout(30):
                    while True:
                        value = await events.get()
                        if value.get('type') == kind:
                            return value
            async def ack(reply):
                await ws.send_json({'type': 'client_event', 'event': {
                    'eventType': 'playback_completed', 'replyId': reply}})
            async def take():
                reply = await asyncio.wait_for(complete.get(), 30)
                assert replies[reply]['audioFrames'] > 0 and replies[reply]['text']
                report['replies'].append(replies[reply])
                return reply
            task = asyncio.create_task(reader())
            try:
                await ws.send_json({'type': 'prepare', 'version': 1})
                assert (await expect('prepare_result'))['ok'], 'Active trial was not interrupted'
                await ws.send_json({'type': 'create_session', 'version': 1, 'grade': 7})
                created = await expect('create_session_result')
                assert 'hang_up' in created['textBotCapabilities']['commands']
                opening = await take()
                await ack(opening)
                for _ in range(5):
                    command_id = 'idle_' + uuid.uuid4().hex
                    command = {'type': 'bot_command', 'requestId': command_id, 'command': 'time_out'}
                    await ws.send_json(command)
                    await ws.send_json(command)  # Transport retry is not a second prompt.
                    idle = await take()
                    assert 'there' in replies[idle]['text'].lower()
                    assert 'bye' not in replies[idle]['text'].lower()
                    await ack(idle)
                    assert not closed.is_set(), 'Idle must never hang up'
                await ws.send_json({'type': 'bot_command', 'command': 'hang_up', 'requestId': 'end_' + uuid.uuid4().hex})
                goodbye = await take()
                await ack(opening)  # A stale completion cannot execute a newer action.
                await asyncio.sleep(.3)
                assert not closed.is_set(), 'Hung up before current farewell was played'
                await ack(goodbye)
                await asyncio.wait_for(closed.wait(), 10)
                assert not report['errors'], report
                assert len(replies) == 7, replies
                report['checks'] = ['opening', 'time_out', 'command_retry', 'farewell_audio',
                                    'five_idle_reminders_no_hangup', 'no_hangup_before_ack',
                                    'stale_ack_ignored', 'hangup_after_ack']
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:8881/ws')
    parser.add_argument('--auth-file', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = asyncio.run(check(args.url, args.auth_file))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
