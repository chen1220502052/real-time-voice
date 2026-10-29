import asyncio
import json
from pathlib import Path

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
import pytest

from backend.app import create_app, ResourceManager, WebSocketConnection
from backend.browser_access import BrowserAccess, COOKIE
from tests.test_app_e2e import MemoryWebSocket


def configured(monkeypatch, tmp_path):
    key = tmp_path/'browser.key';key.write_bytes(b'a'*32)
    monkeypatch.setenv('BROWSER_AUTH_ENABLED','1')
    monkeypatch.setenv('BROWSER_AUTH_KEY_FILE',str(key))
    return BrowserAccess()


def test_signed_cookie_expiry_tampering_restart_and_no_secret_in_value(monkeypatch,tmp_path):
    access=configured(monkeypatch,tmp_path);token=access.issue(1000)
    assert access.valid(token,1001)
    assert BrowserAccess().valid(token,1001)
    assert not access.valid(token,8200)
    assert not access.valid(token+'x',1001)
    assert not access.valid('9.bad.bad',1001)
    assert not access.valid(token,0)
    assert 'a'*32 not in token


def test_missing_key_fails_closed(monkeypatch,tmp_path):
    monkeypatch.setenv('BROWSER_AUTH_ENABLED','1')
    monkeypatch.setenv('BROWSER_AUTH_KEY_FILE',str(tmp_path/'missing'))
    with pytest.raises(FileNotFoundError):BrowserAccess()


def test_http_bootstrap_cookie_websocket_csrf_and_safe_logs(monkeypatch,tmp_path,caplog):
    configured(monkeypatch,tmp_path)
    async def run():
        app=create_app(warmup=False,bot_state_path=':memory:')
        async with TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True)) as client:
            response=await client.post('/api/browser-session')
            assert response.status==401
            response=await client.get('/ws')
            assert response.status==401 and 'WWW-Authenticate' not in response.headers
            response=await client.post('/api/browser-session',headers={'X-Authenticated-User':'test'})
            assert response.status==200
            cookie=response.cookies[COOKIE]
            assert cookie['httponly'] and cookie['secure'] and cookie['samesite']=='Strict'
            token=cookie.value
            # Secure cookies are not sent on ordinary HTTP by all test clients.
            headers={'Cookie':f'{COOKIE}={token}'}
            async with client.ws_connect('/ws',headers=headers) as ws:
                assert (await ws.receive_json())['event']['eventType']=='connection_opened'
            response=await client.get('/ws',headers={**headers,'Origin':'https://evil.example'})
            assert response.status==401
            response=await client.post('/api/browser-session',headers={'X-Authenticated-User':'test','Origin':'https://evil.example'})
            assert response.status==403
            event={'attemptId':'test-1','code':'WEBSOCKET_TIMEOUT','elapsedMs':15001,
                   'secret':'never-log-this','url':'https://secret.invalid'}
            with caplog.at_level('INFO'):
                response=await client.post('/api/connection-events',headers=headers,json=event)
                assert response.status==200
            assert 'never-log-this' not in caplog.text and 'secret.invalid' not in caplog.text
            assert 'browser.connection_failure' in caplog.text
            response=await client.post('/api/connection-events',headers=headers,json={'attemptId':'bad','code':'ARBITRARY'})
            assert response.status==400
            for _ in range(20):
                response=await client.post('/api/connection-events',headers=headers,json=event)
            assert response.status==429
    asyncio.run(run())


def test_three_reservations_fourth_busy_release_and_retry(monkeypatch):
    monkeypatch.setenv('MAX_CONCURRENT_SESSIONS','3')
    async def run():
        manager=ResourceManager()
        sockets=[MemoryWebSocket() for _ in range(4)]
        connections=[WebSocketConnection(ws,resource_manager=manager) for ws in sockets]
        await asyncio.gather(*(c.handle_control({'type':'prepare'}) for c in connections))
        assert manager.in_use==3
        rejected=[i for i,ws in enumerate(sockets) if any(m.get('code')=='NO_RESOURCE' for m in ws.text)]
        assert len(rejected)==1
        busy=rejected[0]
        response=next(m for m in sockets[busy].text if m.get('code')=='NO_RESOURCE')
        assert response['resources']=={'capacity':3,'inUse':3}
        accepted=next(i for i in range(4) if i!=busy)
        await connections[accepted].finish()
        await connections[busy].handle_control({'type':'prepare'})
        assert connections[busy].prepared and manager.in_use==3
        await asyncio.gather(*(c.finish() for c in connections))
        assert manager.in_use==0
    asyncio.run(run())


def test_nginx_ws_still_requires_cookie_and_header_cannot_be_forged():
    source=Path('deploy/nginx-https.conf').read_text()
    assert 'location = /ws' in source and 'location = /api/browser-session' in source
    assert 'proxy_set_header X-Authenticated-User $remote_user;' in source
    assert 'proxy_set_header X-Authenticated-User "";' in source


def test_abandoned_prepare_lease_releases_capacity_without_waiting_for_user_hangup(monkeypatch):
    monkeypatch.setenv('PREPARE_LEASE_SECONDS','.1')
    async def run():
        manager=ResourceManager(1);ws=MemoryWebSocket()
        c=WebSocketConnection(ws,resource_manager=manager)
        await c.handle_control({'type':'prepare'})
        assert manager.in_use==1
        await asyncio.sleep(.15)
        assert manager.in_use==0 and ws.closed
    asyncio.run(run())


def test_page_leave_requires_session_key_and_cleanup_is_idempotent(monkeypatch,tmp_path):
    from backend.domain import Session,SessionState
    configured(monkeypatch,tmp_path)
    async def run():
        app=create_app(warmup=False,bot_state_path=':memory:')
        async with TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True)) as client:
            access=app['browser_access'];headers={'Cookie':f'{COOKIE}={access.issue()}'}
            c=WebSocketConnection(MemoryWebSocket(),resource_manager=app['resource_manager'])
            await c.handle_control({'type':'prepare'})
            c.session=Session('leaving',SessionState.ON_SERVICE)
            app['resource_manager'].connections['leaving']=c
            body={'sessionId':'leaving','disconnectKey':'wrong'}
            response=await client.post('/api/browser-disconnect',headers=headers,json=body)
            assert response.status==403 and app['resource_manager'].in_use==1
            body['disconnectKey']='无效密钥'
            response=await client.post('/api/browser-disconnect',headers=headers,json=body)
            assert response.status==403 and app['resource_manager'].in_use==1
            body['disconnectKey']=c.disconnect_key
            for _ in range(2):
                response=await client.post('/api/browser-disconnect',headers=headers,json=body)
                assert response.status in (200,202)
            await asyncio.sleep(.03)
            assert c.closed and app['resource_manager'].in_use==0
            assert not app['resource_manager'].connections
            await asyncio.gather(c.finish(),c.finish())
    asyncio.run(run())


def test_parallel_cleanup_runs_duplex_close_once_even_if_socket_already_closed():
    from backend.domain import Session,SessionState
    async def run():
        c=WebSocketConnection(MemoryWebSocket())
        await c.handle_control({'type':'prepare'})
        c.session=Session('closing',SessionState.ON_SERVICE)
        class Duplex:
            calls=0
            async def close(self,reason):
                self.calls+=1
                await asyncio.sleep(.01)
                raise RuntimeError('provider stop failed')
        c.duplex=Duplex()
        await c.shutdown()
        await asyncio.gather(c.finish(),c.finish())
        assert c.duplex.calls==1 and c.finish_done
        assert c.resource_manager.in_use==0
    asyncio.run(run())
