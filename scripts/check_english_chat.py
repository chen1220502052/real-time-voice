"""Real API multi-turn chat checks; not a substitute for manual student review."""
import argparse
import asyncio
import json
from pathlib import Path
import ssl
import time
import uuid

import aiohttp
import certifi

async def check(base_url, grades, auth_file=None, busy_timeout=0):
    headers = {}
    if auth_file:
        labels = dict(line.split(':', 1) for line in auth_file.read_text().splitlines() if ':' in line)
        headers['Authorization'] = aiohttp.encode_basic_auth(labels['Username'].strip(), labels['Password'].strip())
    reports = []
    connector = aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))
    async with aiohttp.ClientSession(connector=connector, headers=headers) as client:
        for grade in grades:
            turns = []
            session = 'check_' + uuid.uuid4().hex
            questions = ['hello', 'how old are you?',
                         'I like basketball. My favorite player is Curry.', 'Who is my favorite player?',
                         'What does guitar mean?', '没听懂，请用中文解释guitar。',
                         "Let's talk about rain and weather instead.", 'I like staying at home when it rains.',
                         'Usually I draw cats.', 'What do I usually do on rainy days?']
            for question in questions:
                request_id = 'req_' + uuid.uuid4().hex
                payload = {'version':2,'requestId':request_id,'sessionId':session,'turnId':request_id,
                           'text':question,'channel':'text','options':{'grade':grade}}
                started = time.monotonic()
                first = None
                answer = ''
                done = False
                deadline = started + busy_timeout
                resource_wait = 0
                while True:
                    response = await client.post(base_url.rstrip('/') + '/api/text-bot/replies', json=payload)
                    if response.status != 429 or time.monotonic() >= deadline:
                        break
                    await response.read()
                    response.release()
                    await asyncio.sleep(1)
                    resource_wait = round((time.monotonic() - started)*1000)
                async with response:
                    response.raise_for_status()
                    async for line in response.content:
                        event = json.loads(line)
                        assert event['requestId'] == request_id
                        if event['type'] == 'error':
                            raise AssertionError(event.get('code'))
                        if event['type'] == 'text_delta':
                            if first is None:
                                first = round((time.monotonic()-started)*1000)
                            answer += event['text']
                        if event['type'] == 'done':
                            done = True
                completion_ms = round((time.monotonic()-started)*1000)
                answer = answer.strip()
                assert done and answer and answer[-1] in '.!?。！？', ('Incomplete answer',grade,question,answer)
                assert not any(name in answer.lower() for name in ['li ming','henan']), ('Invented persona',answer)
                assert 'lost that thought' not in answer, ('Fallback instead of answer',grade,question)
                assert answer.count('?') + answer.count('？') <= 1
                if question == 'Who is my favorite player?':
                    assert 'curry' in answer.lower(), ('Memory failed',answer)
                if question.startswith('没听懂'):
                    assert any('\u4e00' <= c <= '\u9fff' for c in answer), ('Chinese help failed',answer)
                if question.startswith("Let's talk"):
                    assert any(word in answer.lower() for word in ['rain','weather','雨','天气']), ('Topic switch failed',answer)
                if question == 'What do I usually do on rainy days?':
                    assert 'draw' in answer.lower() and 'cat' in answer.lower(), ('Longer context memory failed',answer)
                async with client.post(base_url.rstrip('/') + '/api/text-bot/interactions', json={
                    'sessionId':session, 'interactions':[{'eventId':'display_'+request_id,
                        'replyId':request_id,'type':'text_displayed','sequence':1}]
                }) as receipt:
                    receipt.raise_for_status()
                    await receipt.read()
                turn = {'requestId':request_id,'user':question,'assistant':answer,'firstSentenceMs':first,
                        'completionMs':completion_ms, 'resourceWaitMs':resource_wait}
                turns.append(turn)
                print(json.dumps({'grade':grade,**turn}, ensure_ascii=False),flush=True)
            reports.append({'grade':grade,'turns':turns})
    return {'mode':'real_api_multi_turn','grades':reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8872')
    parser.add_argument('--grades',type=int,nargs='+',choices=[6,7,8,9],default=[7])
    parser.add_argument('--auth-file',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--wait-busy',type=int,default=0,help='Wait for existing trial sessions, without interrupting them')
    args = parser.parse_args()
    report = asyncio.run(check(args.url,args.grades,args.auth_file,args.wait_busy))
    if args.output:
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Multi-turn real API checks passed.')


if __name__ == '__main__':
    main()
