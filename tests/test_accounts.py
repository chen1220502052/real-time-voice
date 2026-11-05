import asyncio
from types import SimpleNamespace

import pytest

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from backend.app import create_app
from backend.accounts import COOKIE

# Ownership checks need a valid textbook selection now that default text chat
# requires a chapter. Keep malformed-payload checks separate below.
CHAPTER_OPTIONS = {'chapterId':'d2435143bdba0792312a2ecc','grade':5}


@pytest.fixture(autouse=True)
def textbook_scope(monkeypatch):
    # Account ownership is independent of the locally installed textbook pack.
    def scope(chapter_id):
        if chapter_id != CHAPTER_OPTIONS['chapterId']:
            raise ValueError('Unknown fixture chapter')
        return SimpleNamespace(chapter={'grade': '5年级'})
    monkeypatch.setattr('backend.curriculum_chat.chat_scope', scope)


def test_accounts_login_isolation_csrf_revocation_and_persistence(monkeypatch, tmp_path):
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '1')
    monkeypatch.setenv('BROWSER_AUTH_ENABLED', '0')
    async def run():
        app = create_app(warmup=False, bot_state_path=':memory:')
        async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as a:
            async with TestClient(a.server, cookie_jar=CookieJar(unsafe=True)) as b:
                assert (await a.get('/', allow_redirects=False)).status == 302
                redirect = await a.get('/practice.html?chapter=unit3&mode=shadow', allow_redirects=False)
                from urllib.parse import urlsplit, parse_qs
                assert parse_qs(urlsplit(redirect.headers['Location']).query)['next'] == ['/practice.html?chapter=unit3&mode=shadow']
                assert (await a.get('/login.html')).status == 200
                theme = await a.get('/theme.css?v=20261023-1')
                assert theme.status == 200 and theme.content_type == 'text/css'
                assert (await a.get('/account-context.js', allow_redirects=False)).status == 302
                for path in ['/api/text-bot/replies', '/api/browser-session', '/api/browser-disconnect']:
                    assert (await a.post(path, json={})).status == 401
                assert (await a.get('/api/practice/catalog')).status == 401
                assert (await a.get('/ws')).status == 401
                bad = await a.post('/api/auth/register', json={'username':'ab','password':'short'})
                assert bad.status == 400
                first = await a.post('/api/auth/register', json={'username':'Alice','password':'a-long-password'})
                assert first.status == 200
                uid = (await first.json())['user']['id']
                cookie = first.cookies[COOKIE]
                assert cookie['httponly'] and cookie['samesite'] == 'Strict'
                assert (await a.get('/')).status == 200
                context = await (await a.get('/account-context.js')).text()
                assert uid in context and 'a-long-password' not in context
                assert (await a.post('/api/browser-session')).status == 200
                assert (await a.post('/api/auth/register', json={'username':'alice','password':'a-long-password'})).status == 409
                assert (await b.post('/api/auth/login', json={'username':'alice','password':'wrong-password'})).status == 401
                second = await b.post('/api/auth/register', json={'username':'bob','password':'other-password'})
                assert second.status == 200 and (await second.json())['user']['id'] != uid
                # Exercise ownership without invoking an external LLM.
                request = {'user': {'id': uid}}
                class Request(dict):
                    pass
                ar = Request(request); ar.app = app
                app['accounts'].claim(ar, 'shared-session')
                payload = {'version':1,'sessionId':'shared-session','event':'session_start','messages':[],
                           'options':CHAPTER_OPTIONS}
                assert (await b.post('/api/text-bot/replies', json=payload)).status == 403
                payload = {'version':2,'sessionId':'shared-session','requestId':'r1','turnId':'t1',
                           'text':'Hello','channel':'text','options':CHAPTER_OPTIONS}
                assert (await b.post('/api/text-bot/replies', json=payload)).status == 403
                event = {'eventId':'e1','replyId':'r1','type':'text_displayed','sequence':1}
                assert (await b.post('/api/text-bot/interactions', json={
                    'sessionId':'shared-session','interactions':[event]})).status == 403
                assert (await b.post('/api/auth/logout', headers={'Origin':'https://evil.example'})).status == 403
                assert (await b.get('/ws', headers={'Origin':'https://evil.example'})).status == 403
                assert (await b.get('/api/auth/me')).status == 200
                token = cookie.value
                ws = await a.ws_connect('/ws')
                await ws.receive_json()
                assert (await a.post('/api/auth/logout')).status == 200
                await ws.send_json({'type':'prepare'})
                from aiohttp import WSMsgType
                assert (await ws.receive()).type == WSMsgType.CLOSE
                await ws.close()
                assert (await a.get('/api/auth/me')).status == 401
                assert (await b.get('/api/auth/me')).status == 200
                replay = await a.get('/api/auth/me', headers={'Cookie':f'{COOKIE}={token}'})
                assert replay.status == 401
                login = await a.post('/api/auth/login', json={'username':'ALICE','password':'a-long-password'})
                assert login.status == 200 and (await login.json())['user']['id'] == uid
                db = app['accounts'].db
                assert db.execute('SELECT password FROM users WHERE id=?',(uid,)).fetchone()[0] != 'a-long-password'
                db.execute('UPDATE sessions SET expires=0 WHERE user_id=?',(uid,)); db.commit()
                assert (await a.post('/api/browser-session')).status == 401
        # Persistent credentials survive a process restart.
        app = create_app(warmup=False, bot_state_path=':memory:')
        async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
            response = await client.post('/api/auth/login', json={'username':'alice','password':'a-long-password'})
            assert response.status == 200 and (await response.json())['user']['id'] == uid
    asyncio.run(run())


def test_accounts_rate_limit_and_secure_cookie(monkeypatch):
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '1')
    async def run():
        async with TestClient(TestServer(create_app(warmup=False, bot_state_path=':memory:'))) as client:
            response = await client.post('/api/auth/register', headers={'X-Forwarded-Proto':'https'},
                                         json={'username':'test','password':'long-password'})
            assert response.cookies[COOKIE]['secure']
            for _ in range(19):
                await client.post('/api/auth/login', json={})
            assert (await client.post('/api/auth/login', json={})).status == 429
    asyncio.run(run())


def test_accounts_enabled_by_default_without_legacy_signing_key(monkeypatch):
    from backend.accounts import Accounts
    from backend.browser_access import BrowserAccess
    monkeypatch.delenv('ACCOUNT_AUTH_ENABLED')
    monkeypatch.setenv('BROWSER_AUTH_ENABLED', '1')
    monkeypatch.setenv('BROWSER_AUTH_KEY_FILE', '/missing-legacy-key')
    accounts = Accounts()
    try:
        assert accounts.enabled
        assert not BrowserAccess().enabled
    finally:
        accounts.close()


def test_invalid_bot_payloads_are_not_mistaken_for_ownership_checks(monkeypatch):
    """Validate the request before claiming a conversation; keep real denials 403."""
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '1')
    monkeypatch.setenv('BROWSER_AUTH_ENABLED', '0')

    async def run():
        app = create_app(warmup=False, voice_enabled=False, bot_state_path=':memory:')
        async with TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True)) as client:
            registered = await client.post('/api/auth/register', json={
                'username': 'ownership_test', 'password': 'fixture-password-2026',
            })
            assert registered.status == 200
            own_id = (await registered.json())['user']['id']
            app['accounts'].db.execute('INSERT INTO conversation_owners VALUES (?,?)',
                                       ('foreign-session', 'another-user'))
            app['accounts'].db.commit()
            for version in (1, 2):
                base = {'version': version, 'sessionId': 'foreign-session',
                        'requestId': f'check-v{version}', 'turnId': 'turn1', 'options':CHAPTER_OPTIONS}
                # Without an opening event, the empty default user_message is invalid.
                invalid = await client.post('/api/text-bot/replies', json=base)
                assert invalid.status == 400
                denial = await client.post('/api/text-bot/replies', json={
                    **base, 'event': 'session_start',
                })
                assert denial.status == 403
                message = {**base, 'text': 'Hello'} if version == 2 else {
                    **base, 'messages': [{'role': 'user', 'content': 'Hello'}],
                }
                assert (await client.post('/api/text-bot/replies', json=message)).status == 403
                assert app['resource_manager'].in_use == 0
            # Malformed requests must not claim a previously unused conversation either.
            assert (await client.post('/api/text-bot/replies', json={
                'version': 2, 'sessionId': 'unclaimed', 'requestId': 'bad', 'turnId': 't',
            })).status == 400
            assert app['accounts'].db.execute(
                'SELECT user_id FROM conversation_owners WHERE session=?', ('unclaimed',),
            ).fetchone() is None
            assert app['accounts'].db.execute(
                'SELECT user_id FROM conversation_owners WHERE session=?', ('foreign-session',),
            ).fetchone()[0] != own_id

    asyncio.run(run())
