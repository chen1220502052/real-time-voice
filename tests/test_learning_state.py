import asyncio

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from backend.app import create_app

COURSE = {'version': 1, 'grade': 3, 'track': 'textbook', 'publisher': 'test'}
PROGRESS = {'version': 1, 'completed': {'words': 1000}, 'drafts': {'speakingText': 'My weekend'},
            'attempts': {}, 'concepts': {}, 'lastModule': 'words', 'listening': {}, 'review': None}


def test_learning_state_sync_isolation_conflicts_reset_and_restart(monkeypatch):
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '1')
    async def run():
        app = create_app(warmup=False, voice_enabled=False, bot_state_path=':memory:')
        server = TestServer(app)
        a = TestClient(server, cookie_jar=CookieJar(unsafe=True))
        b = TestClient(server, cookie_jar=CookieJar(unsafe=True))
        device = TestClient(server, cookie_jar=CookieJar(unsafe=True))
        await a.start_server(); await b.start_server(); await device.start_server()
        try:
            assert (await a.get('/api/account/learning')).status == 401
            creds = {'username': 'learner', 'password': 'testing-password'}
            response = await a.post('/api/auth/register', json=creds)
            uid = (await response.json())['user']['id']
            await b.post('/api/auth/register', json={'username':'other','password':'testing-password'})
            await device.post('/api/auth/login', json=creds)
            headers = {'X-Account-ID': uid}
            async def write(client, kind, value, revision):
                return await client.put('/api/account/learning/' + kind, headers=headers,
                                        json={'value':value,'revision':revision})
            assert (await (await a.get('/api/account/learning')).json())['documents']['progress']['revision'] == 0
            # A browser tab cannot write to a newly switched account with its old identity.
            assert (await write(b, 'course', COURSE, 0)).status == 401
            assert (await a.put('/api/account/learning/course', json={'value':COURSE,'revision':0})).status == 401
            for kind, value in [('course', COURSE), ('progress', PROGRESS)]:
                response = await write(a, kind, value, 0)
                assert response.status == 200 and (await response.json())['revision'] == 1
                # Import or keepalive retry after a lost acknowledgement is idempotent.
                response = await write(device, kind, value, 0)
                assert response.status == 200 and (await response.json())['revision'] == 1
            snapshot = await (await device.get('/api/account/learning')).json()
            assert snapshot['documents']['progress']['value'] == PROGRESS
            assert snapshot['documents']['course']['value'] == COURSE
            other = await (await b.get('/api/account/learning')).json()
            assert all(doc['value'] is None for doc in other['documents'].values())
            newer = {**COURSE, 'grade':9}
            assert (await write(device, 'course', newer, 1)).status == 200
            conflict = await write(a, 'course', {**COURSE, 'grade':6}, 1)
            assert conflict.status == 409 and (await conflict.json())['current']['value'] == newer
            # A reset is a new durable version, not a deletion eligible for re-import.
            empty = {**PROGRESS, 'completed':{}, 'drafts':{}}
            assert (await write(a, 'progress', empty, 1)).status == 200
            assert (await write(device, 'progress', PROGRESS, 0)).status == 409
            assert (await a.put('/api/account/learning/course', headers={**headers, 'Origin':'https://evil.example'},
                                json={'value':COURSE,'revision':2})).status == 403
            assert (await (await a.get('/api/account/learning')).json())['documents']['progress']['value'] == empty
        finally:
            await device.close(); await b.close(); await a.close()
        restarted = create_app(warmup=False, voice_enabled=False, bot_state_path=':memory:')
        async with TestClient(TestServer(restarted), cookie_jar=CookieJar(unsafe=True)) as client:
            assert (await client.post('/api/auth/login', json=creds)).status == 200
            result = await (await client.get('/api/account/learning')).json()
            assert result['documents']['progress']['revision'] == 2
            assert result['documents']['progress']['value']['completed'] == {}
            assert result['documents']['course']['value']['grade'] == 9
    asyncio.run(run())


def test_invalid_learning_documents_do_not_overwrite_saved_state(monkeypatch):
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '1')
    async def run():
        async with TestClient(TestServer(create_app(warmup=False, voice_enabled=False, bot_state_path=':memory:')),
                              cookie_jar=CookieJar(unsafe=True)) as client:
            registered = await client.post('/api/auth/register', json={'username':'valid','password':'testing-password'})
            uid = (await registered.json())['user']['id']
            headers = {'X-Account-ID': uid}
            cases = [('course', {'value': {**COURSE, 'grade':True}, 'revision':0}),
                     ('course', {'value': {**COURSE, 'userId':'someone'}, 'revision':0}),
                     ('course', {'value': COURSE, 'revision':True}),
                     ('course', {'value': COURSE, 'revision':-1}),
                     ('progress', {'value': {'version':1}, 'revision':0}),
                     ('progress', {'value': {**PROGRESS, 'drafts':{'__proto__':{}}}, 'revision':0}),
                     ('progress', {'value': {**PROGRESS, 'drafts':{'text':'x'*50000}}, 'revision':0})]
            for kind, data in cases:
                response = await client.put('/api/account/learning/'+kind, headers=headers, json=data)
                assert response.status == 400
            assert (await client.put('/api/account/learning/nope', headers=headers, json={})).status == 404
            result = await (await client.get('/api/account/learning')).json()
            assert result['documents']['progress']['revision'] == 0
    asyncio.run(run())


def test_grade_three_course_round_trips_without_normalizing_to_sixth_grade():
    import json
    import pytest
    from backend.learning_state import validate
    course = {**COURSE, 'grade': 3}
    assert json.loads(validate('course', course)) == course
    for grade in (2, 10, True, '3'):
        with pytest.raises(ValueError):
            validate('course', {**course, 'grade': grade})
