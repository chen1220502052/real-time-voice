"""Local accounts and revocable, opaque browser sessions."""
import asyncio
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode

from aiohttp import web

COOKIE = 'kitty_account'
PUBLIC = {'/login.html', '/login.js', '/account.css', '/theme.css', '/favicon.svg', '/kitty.svg',
          '/api/auth/login', '/api/auth/register', '/api/auth/me', '/health'}


class Accounts:
    def __init__(self):
        self.enabled = os.getenv('ACCOUNT_AUTH_ENABLED', '1') != '0'
        self.db = None
        self.attempts = {}
        if not self.enabled:
            return
        path = Path(os.getenv('ACCOUNT_DB', '.local/accounts.sqlite3')).resolve()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                salt TEXT NOT NULL, password TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_state (
                user_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
                revision INTEGER NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, kind));
            CREATE TABLE IF NOT EXISTS conversation_owners (
                session TEXT PRIMARY KEY, user_id TEXT NOT NULL);
        ''')
        self.db.commit()
        # Unknown users still incur the same password derivation cost.
        self.dummy_salt = secrets.token_hex(16)

    @staticmethod
    def derive(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384,
                              r=8, p=1, dklen=32).hex()

    @staticmethod
    def token(request):
        return hashlib.sha256(request.cookies.get(COOKIE, '').encode()).hexdigest()

    def user(self, request):
        row = self.db.execute('''SELECT users.id, users.username FROM sessions
            JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?''',
            (self.token(request), time.time())).fetchone()
        return {'id': row[0], 'username': row[1]} if row else None

    def claim(self, request, session):
        if not self.enabled:
            return
        owner = self.db.execute('SELECT user_id FROM conversation_owners WHERE session=?', (session,)).fetchone()
        if owner is None:
            # Pre-account conversations have no provable owner; never expose them.
            old = request.app['bot_service'].store.db.execute(
                'SELECT 1 FROM requests WHERE session=? UNION SELECT 1 FROM interactions WHERE session=? LIMIT 1',
                (session, session)).fetchone()
            if old:
                raise web.HTTPForbidden(text='Conversation unavailable')
            self.db.execute('INSERT INTO conversation_owners VALUES (?,?)', (session, request['user']['id']))
            self.db.commit()
        elif owner[0] != request['user']['id']:
            raise web.HTTPForbidden(text='Conversation unavailable')

    def limited(self, request):
        now = time.monotonic()
        self.attempts = {k: v for k, v in self.attempts.items() if now-v[0] < 60}
        key = (request.headers.get('X-Real-IP', request.remote)
               if request.remote in {'127.0.0.1', '::1'} else request.remote)
        start, count = self.attempts.get(key, (now, 0))
        if count >= 20 or (key not in self.attempts and len(self.attempts) >= 4096):
            return True
        self.attempts[key] = (start, count+1)
        return False

    def close(self):
        if self.db:
            self.db.close()


@web.middleware
async def account_access(request, handler):
    accounts = request.app['accounts']
    if not accounts.enabled:
        return await handler(request)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') or request.path == '/ws':
        if not request.app['browser_access'].same_origin(request):
            return web.json_response({'code': 'ORIGIN_REJECTED'}, status=403)
    request['user'] = accounts.user(request)
    if request.path not in PUBLIC and not request['user']:
        if request.path.startswith('/api/') or request.path == '/ws':
            return web.json_response({'code': 'AUTH_REQUIRED', 'message': '请先登录'}, status=401)
        raise web.HTTPFound('/login.html?' + urlencode({'next': str(request.rel_url)}))
    response = await handler(request)
    if not response.prepared:
        response.headers['Cache-Control'] = 'no-store'
    return response


async def credentials(request):
    accounts = request.app['accounts']
    if not accounts.enabled:
        return web.json_response({'message': '账户登录未启用'}, status=404)
    if accounts.limited(request):
        return web.json_response({'message': '尝试过于频繁，请一分钟后再试'}, status=429)
    try:
        data = await request.json()
        username, password = data['username'], data['password']
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError()
        username = username.strip().lower()
        if not re.fullmatch(r'[a-z0-9_\-]{3,32}', username) or not 8 <= len(password) <= 128:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        return web.json_response({'message': '账户名需为 3–32 位字母、数字、下划线或短横线；密码需为 8–128 位'}, status=400)
    register = request.path.endswith('/register')
    row = accounts.db.execute('SELECT id,salt,password FROM users WHERE username=?', (username,)).fetchone()
    salt = secrets.token_hex(16) if register else (row[1] if row else accounts.dummy_salt)
    digest = await asyncio.to_thread(accounts.derive, password, salt)
    if register:
        uid = secrets.token_hex(16)
        try:
            accounts.db.execute('INSERT INTO users VALUES (?,?,?,?)', (uid, username, salt, digest))
            accounts.db.commit()
        except sqlite3.IntegrityError:
            accounts.db.rollback()
            return web.json_response({'message': '该账户名已被使用'}, status=409)
    else:
        if not row or not hmac.compare_digest(digest, row[2]):
            return web.json_response({'message': '账户或密码错误'}, status=401)
        uid = row[0]
    token = secrets.token_urlsafe(32)
    accounts.db.execute('DELETE FROM sessions WHERE expires<=? OR token=?', (time.time(), accounts.token(request)))
    accounts.db.execute('INSERT INTO sessions VALUES (?,?,?)',
                        (hashlib.sha256(token.encode()).hexdigest(), uid, time.time()+604800))
    accounts.db.commit()
    response = web.json_response({'user': {'id': uid, 'username': username}})
    # TLS terminates on the local reverse proxy in production.
    secure = request.secure or (request.remote in {'127.0.0.1', '::1'} and request.headers.get('X-Forwarded-Proto') == 'https')
    response.set_cookie(COOKIE, token, httponly=True, secure=secure, samesite='Strict', max_age=604800, path='/')
    return response


async def me(request):
    accounts = request.app['accounts']
    user = accounts.user(request) if accounts.enabled else None
    return web.json_response({'enabled': accounts.enabled, 'user': user},
                             status=401 if accounts.enabled and not user else 200,
                             headers={'Cache-Control': 'no-store'})


async def logout(request):
    accounts = request.app['accounts']
    if accounts.enabled:
        token = accounts.token(request)
        accounts.db.execute('DELETE FROM sessions WHERE token=?', (token,))
        accounts.db.commit()
        connections = list(request.app['resource_manager'].connections.values())
        await asyncio.gather(*(c.finish() for c in connections if getattr(c, 'account_token', None) == token))
    response = web.json_response({'ok': True})
    response.del_cookie(COOKIE, path='/')
    return response


async def context_script(request):
    import json
    user = request.get('user')
    value = json.dumps(user, ensure_ascii=True)
    return web.Response(text=f'window.KittyAccountUser = {value};', content_type='application/javascript',
                        headers={'Cache-Control': 'no-store'})


def register_accounts(app):
    from .learning_state import learning_state
    app['accounts'] = Accounts()
    app.router.add_get('/api/account/learning', learning_state)
    app.router.add_put('/api/account/learning/{kind}', learning_state)
    app.router.add_post('/api/auth/register', credentials)
    app.router.add_post('/api/auth/login', credentials)
    app.router.add_get('/api/auth/me', me)
    app.router.add_post('/api/auth/logout', logout)
    app.router.add_get('/account-context.js', context_script)
