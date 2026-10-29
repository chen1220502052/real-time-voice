"""HTTP-authenticated bootstrap -> signed, HttpOnly browser session.

Nginx authenticates bootstrap HTTP requests and overwrites the trusted user
header. WebSocket upgrades use the cookie, not another Basic-auth challenge.
"""
import hashlib
import asyncio
import hmac
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web
from .observability import log_event

COOKIE = 'rtv_browser_session'
EVENT_CODES = {'AUTH_REQUIRED', 'AUTH_TIMEOUT', 'AUTH_UNAVAILABLE', 'WEBSOCKET_TIMEOUT',
               'CONNECTION_FAILED', 'PREPARE_TIMEOUT', 'CREATE_SESSION_TIMEOUT', 'NO_RESOURCE'}


class BrowserAccess:
    def __init__(self):
        self.enabled = (os.getenv('BROWSER_AUTH_ENABLED', '0') == '1'
                        and os.getenv('ACCOUNT_AUTH_ENABLED', '1') == '0')
        self.key = b''
        self.ttl = 7200
        self.secure = os.getenv('BROWSER_AUTH_COOKIE_SECURE', '1') != '0'
        if self.enabled:
            self.key = Path(os.environ['BROWSER_AUTH_KEY_FILE']).read_bytes()
            if len(self.key) < 32:
                raise ValueError('Browser authentication requires a persistent key of at least 32 bytes')

    def issue(self, now=None):
        expiry = int(time.time() if now is None else now) + self.ttl
        payload = f'{expiry}.{secrets.token_hex(16)}'
        signature = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return f'{payload}.{signature}'

    def valid(self, token, now=None):
        if not token or len(token) > 200:
            return False
        try:
            expiry, nonce, signature = token.split('.')
            if not re.fullmatch(r'[0-9a-f]{32}', nonce):
                return False
            current = time.time() if now is None else now
            if not current < int(expiry) <= current + self.ttl:
                return False
            expected = hmac.new(self.key, f'{expiry}.{nonce}'.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def same_origin(request):
        origin = request.headers.get('Origin')
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return False
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme) if request.remote in {'127.0.0.1', '::1'} else request.scheme
        return parsed.scheme == scheme and parsed.netloc == request.host

    def authorized(self, request):
        if request.app.get("accounts") and request.app["accounts"].enabled:
            return bool(request.get("user")) and self.same_origin(request)
        return not self.enabled or (self.same_origin(request) and self.valid(request.cookies.get(COOKIE)))


async def bootstrap(request):
    access = request.app['browser_access']
    if not access.same_origin(request):
        return web.json_response({'code': 'AUTH_REQUIRED'}, status=403)
    account_mode = request.app['accounts'].enabled
    if access.enabled and not account_mode and (request.remote not in {'127.0.0.1', '::1'} or not request.headers.get('X-Authenticated-User')):
        log_event('browser.auth_rejected', reason='missing_trusted_http_login')
        return web.json_response({'code': 'AUTH_REQUIRED'}, status=401)
    manager = request.app['resource_manager']
    response = web.json_response({'ok': True, 'authMode': 'account' if account_mode else ('cookie' if access.enabled else 'none'),
        'resources': {'capacity': manager.capacity, 'inUse': manager.in_use},
        'expiresInSeconds': access.ttl}, headers={'Cache-Control': 'no-store'})
    if access.enabled and not account_mode:
        response.set_cookie(COOKIE, access.issue(), max_age=access.ttl, httponly=True,
                            secure=access.secure, samesite='Strict', path='/')
    log_event('browser.auth_ready', mode='cookie' if access.enabled else 'none')
    return response


async def connection_event(request):
    """Cookie-authenticated, allowlisted diagnostics; no URLs, secrets or text."""
    if not request.app['browser_access'].authorized(request):
        return web.json_response({'code': 'AUTH_REQUIRED'}, status=401)
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return web.json_response({'code': 'INVALID_EVENT'}, status=400)
    if not isinstance(data, dict) or data.get('code') not in EVENT_CODES:
        return web.json_response({'code': 'INVALID_EVENT'}, status=400)
    attempt = data.get('attemptId', '')
    if not isinstance(attempt, str) or not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', attempt):
        return web.json_response({'code': 'INVALID_EVENT'}, status=400)
    # Bound log volume per cookie without retaining personal browser identifiers.
    now = time.monotonic()
    bucket = request.app['connection_event_limits']
    key = hashlib.sha256(request.cookies.get(COOKIE, request.remote or '').encode()).hexdigest()
    previous, count = bucket.get(key, (now, 0))
    count = count + 1 if now-previous < 60 else 1
    if now-previous >= 60:
        previous = now
    if len(bucket) >= 1024:
        bucket.clear()
    bucket[key] = (previous, count)
    if count > 20:
        return web.json_response({'code': 'RATE_LIMITED'}, status=429)
    elapsed = data.get('elapsedMs')
    elapsed = round(elapsed, 1) if type(elapsed) in (int, float) and 0 <= elapsed <= 300000 else None
    log_event('browser.connection_failure', attemptId=attempt, code=data['code'], elapsedMs=elapsed)
    return web.json_response({'ok': True}, headers={'Cache-Control': 'no-store'})


async def browser_disconnect(request):
    if not request.app['browser_access'].authorized(request):
        return web.json_response({'code':'AUTH_REQUIRED'},status=401)
    try:
        data=await request.json()
    except (ValueError,TypeError):
        return web.json_response({'code':'INVALID_EVENT'},status=400)
    if not isinstance(data,dict) or not isinstance(data.get('sessionId'),str) or not isinstance(data.get('disconnectKey'),str):
        return web.json_response({'code':'INVALID_EVENT'},status=400)
    connection=request.app['resource_manager'].connections.get(data['sessionId'])
    if connection is None:
        return web.json_response({'ok':True})  # Idempotent duplicate / already closed.
    try:
        matches=hmac.compare_digest(connection.disconnect_key,data['disconnectKey'])
    except TypeError:
        matches=False
    if request.app["accounts"].enabled:
        matches = matches and getattr(connection, "account_token", None) == request.app["accounts"].token(request)
    if not matches:
        return web.json_response({'code':'AUTH_REQUIRED'},status=403)
    log_event('browser.page_left',sessionId=data['sessionId'])
    # Closing a page also cancels its HTTP request; cleanup must outlive it.
    task=asyncio.create_task(connection.finish())
    tasks=request.app['disconnect_tasks'];tasks.add(task);task.add_done_callback(tasks.discard)
    return web.json_response({'ok':True},status=202)
