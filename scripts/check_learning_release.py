#!/usr/bin/env python3
"""Exercise a deployed learning release with disposable accounts, without LLM calls.

Run on the server with --account-db to remove only the generated test users.
"""
import argparse
import asyncio
import json
import secrets
import sqlite3
from pathlib import Path

import aiohttp


async def check(base, account_db=None, catalog_baseline=None):
    users = []
    checks = 0
    async def request(client, method, path, expected=200, **kwargs):
        nonlocal checks
        async with client.request(method, base + path, **kwargs) as response:
            body = await response.read()
            assert response.status == expected, f'{method} {path}: {response.status}, expected {expected}'
            checks += 1
            if response.content_type == 'application/json':
                return json.loads(body)
            return body
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as a, \
            aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as b, \
            aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as device:
        try:
            health = await request(a, 'GET', '/health')
            assert health['ok'] and health['browserAuth'] == 'account'
            await request(a, 'GET', '/', 302, allow_redirects=False)
            await request(a, 'GET', '/login.html')
            await request(a, 'GET', '/theme.css')
            await request(a, 'GET', '/api/account/learning', 401)
            await request(a, 'GET', '/api/practice/catalog', 401)
            credentials = {'username': 'qa_' + secrets.token_hex(8), 'password': secrets.token_urlsafe(24)}
            for client, creds in [(a, credentials), (b, {**credentials, 'username': 'qa_' + secrets.token_hex(8)})]:
                result = await request(client, 'POST', '/api/auth/register', json=creds)
                users.append(result['user']['id'])
            uid = users[0]
            await request(device, 'POST', '/api/auth/login', json=credentials)
            for path in ['/', '/home.html', '/practice.html', '/text.html', '/account-context.js', '/learning.js']:
                await request(a, 'GET', path)
            catalog = await request(a, 'GET', '/api/practice/catalog')
            assert catalog['chapters'] and not catalog['missingPublishers']
            if catalog_baseline:
                assert catalog == json.loads(Path(catalog_baseline).read_text()), 'Textbook catalog changed from deployment baseline'
            grades = set()
            recordings = 0
            first_audio = None
            for chapter in catalog['chapters']:
                grades.add(int(str(chapter['grade']).replace('年级', '')))
                detail = await request(a, 'GET', '/api/practice/chapters/' + chapter['id'])
                assert all(detail[key] == chapter[key] for key in ('id', 'grade', 'publisher', 'edition', 'unit'))
                for item in detail['recordings']:
                    assert item['availability'] == 'local', 'Missing textbook audio'
                    first_audio = first_audio or item['url']
                    recordings += 1
            assert grades == set(range(3, 10))
            audio = await request(a, 'GET', first_audio, 206, headers={'Range': 'bytes=0-127'})
            assert len(audio) == 128
            sample = next(Path(__file__).resolve().parents[1].glob('web/audio/weekend/*.m4a'), None)
            if sample:
                await request(a, 'GET', '/audio/weekend/' + sample.name, 206, headers={'Range': 'bytes=0-127'})
            headers = {'X-Account-ID': uid}
            course = {'version':1, 'grade':3, 'track':'textbook', 'chapterId':next(c['id'] for c in catalog['chapters'] if c['grade']=='3年级')}
            progress = {'version':1, 'completed':{'words':1}, 'drafts':{'speakingText':'My weekend'},
                        'attempts':{}, 'concepts':{}, 'lastModule':'words', 'listening':{}, 'review':None}
            for kind, value in [('course', course), ('progress', progress)]:
                path = '/api/account/learning/' + kind
                result = await request(a, 'PUT', path, headers=headers, json={'value':value,'revision':0})
                assert result['revision'] == 1
                retry = await request(device, 'PUT', path, headers=headers, json={'value':value,'revision':0})
                assert retry['revision'] == 1
                await request(b, 'PUT', path, 401, headers=headers, json={'value':value,'revision':0})
            data = await request(device, 'GET', '/api/account/learning')
            assert data['documents']['progress']['value'] == progress
            assert data['documents']['course']['value'] == course
            other = await request(b, 'GET', '/api/account/learning')
            assert all(item['value'] is None for item in other['documents'].values())
            changed = {**course, 'grade':9}
            await request(device, 'PUT', '/api/account/learning/course', headers=headers, json={'value':changed,'revision':1})
            await request(a, 'PUT', '/api/account/learning/course', 409, headers=headers,
                          json={'value':{**course,'grade':6},'revision':1})
            await request(a, 'PUT', '/api/account/learning/course', 403,
                          headers={**headers,'Origin':'https://evil.example'}, json={'value':course,'revision':2})
            await request(a, 'PUT', '/api/account/learning/progress', headers=headers,
                          json={'value':{**progress,'completed':{},'drafts':{}},'revision':1})
            opening = {'version':2, 'sessionId':'qa_'+secrets.token_hex(10), 'requestId':'opening',
                       'turnId':'t1', 'event':'session_start', 'channel':'text',
                       'options':{'grade':int(catalog['chapters'][0]['grade'].replace('年级','')),
                                  'chapterId':catalog['chapters'][0]['id']}}
            stream = await request(a, 'POST', '/api/text-bot/replies', json=opening)
            events = [json.loads(line) for line in stream.splitlines()]
            assert events[-1]['type'] == 'done' and any(e['type']=='text_delta' for e in events)
            replay = await request(a, 'POST', '/api/text-bot/replies', json=opening)
            assert replay == stream
            await request(b, 'POST', '/api/text-bot/replies', 403, json=opening)
            await request(b, 'POST', '/api/text-bot/replies', 400,
                          json={k:v for k,v in opening.items() if k!='event'})
            receipt = {'sessionId':opening['sessionId'], 'interactions':[{
                'eventId':'shown', 'replyId':'opening', 'type':'text_displayed', 'sequence':1}]}
            await request(a, 'POST', '/api/text-bot/interactions', json=receipt)
            await request(b, 'POST', '/api/text-bot/interactions', 403, json=receipt)
            await request(a, 'POST', '/api/browser-session')
            if health['voiceEnabled']:
                async with a.ws_connect(base.replace('http', 'ws', 1) + '/ws', origin=base) as ws:
                    hello = await ws.receive_json(timeout=10)
                    assert hello['type'] == 'session_event' and hello['event']['eventType'] == 'connection_opened'
                    checks += 1
            await request(a, 'POST', '/api/auth/logout')
            await request(a, 'GET', '/api/auth/me', 401)
            await request(b, 'GET', '/api/auth/me')
            # Login afresh after logout: persisted state does not depend on a browser cache.
            await request(a, 'POST', '/api/auth/login', json=credentials)
            saved = await request(a, 'GET', '/api/account/learning')
            assert saved['documents']['course']['value']['grade'] == 9
            assert saved['documents']['progress']['value']['completed'] == {}
            print(json.dumps({'ok':True,'checks':checks,'chapters':len(catalog['chapters']),
                              'recordings':recordings,'release':health.get('gitCommit'),
                              'voiceWebSocket':bool(health['voiceEnabled'])}, ensure_ascii=False))
        finally:
            if account_db:
                with sqlite3.connect(account_db) as db:
                    for uid in users:
                        for table in ('sessions', 'learning_state', 'conversation_owners'):
                            db.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
                        db.execute('DELETE FROM users WHERE id=?', (uid,))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base')
    parser.add_argument('--account-db')
    parser.add_argument('--catalog-baseline', type=Path, help='Exact pre-deployment catalog snapshot for regression checks')
    args = parser.parse_args()
    asyncio.run(check(args.base.rstrip('/'), args.account_db, args.catalog_baseline))
