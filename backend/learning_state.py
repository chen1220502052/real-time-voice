"""Account-owned learning documents with optimistic concurrency control."""
import json
import math
import sqlite3
import time

from aiohttp import web

KINDS = ('progress', 'course')
MAX_BYTES = 48 * 1024


def validate(kind, value):
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('Unsupported learning document')
    budget = 4000
    def walk(item, depth=0):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 12:
            raise ValueError('Learning document too complex')
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {'__proto__', 'constructor', 'prototype'} or len(key) > 200:
                    raise ValueError('Invalid field')
                walk(child, depth+1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth+1)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError('Invalid number')
    walk(value)
    if kind == 'course':
        if (set(value) - {'version', 'grade', 'track', 'unitId', 'publisher', 'edition', 'chapterId'}
                or type(value.get('grade')) is not int or value['grade'] not in range(3, 10)
                or value.get('track') not in ('extension', 'textbook')):
            raise ValueError('Invalid course selection')
        for field in ('unitId', 'publisher', 'edition', 'chapterId'):
            if field in value and (not isinstance(value[field], str) or len(value[field]) > 200):
                raise ValueError('Invalid course field')
    else:
        if set(value) - {'version', 'completed', 'drafts', 'attempts', 'concepts', 'lastModule', 'listening', 'review'}:
            raise ValueError('Invalid progress fields')
        for field in ('completed', 'drafts', 'attempts', 'concepts', 'listening'):
            if not isinstance(value.get(field), dict):
                raise ValueError('Invalid progress section')
        if (not isinstance(value.get('lastModule'), str) or len(value['lastModule']) > 64
                or value.get('review') is not None and not isinstance(value['review'], dict)):
            raise ValueError('Invalid progress')
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'), sort_keys=True)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError('Learning document too large')
    return encoded


def read(db, user_id, kind):
    row = db.execute('SELECT value,revision FROM learning_state WHERE user_id=? AND kind=?',
                     (user_id, kind)).fetchone()
    return {'value': json.loads(row[0]), 'revision': row[1]} if row else {'value': None, 'revision': 0}


async def learning_state(request):
    accounts = request.app['accounts']
    if not accounts.enabled:
        return web.json_response({'code': 'ACCOUNTS_DISABLED'}, status=404)
    uid = request['user']['id']
    expected = request.headers.get('X-Account-ID')
    if (request.method != 'GET' or expected) and expected != uid:
        return web.json_response({'code': 'ACCOUNT_CHANGED'}, status=401)
    db = accounts.db
    try:
        if request.method == 'GET':
            return web.json_response({'userId': uid, 'documents': {kind: read(db, uid, kind) for kind in KINDS}})
        kind = request.match_info['kind']
        if kind not in KINDS:
            return web.json_response({'code': 'UNKNOWN_LEARNING_DOCUMENT'}, status=404)
        try:
            data = await request.json()
            if not isinstance(data, dict) or set(data) != {'value', 'revision'}:
                raise ValueError()
            revision = data['revision']
            if type(revision) is not int or not 0 <= revision < 2**53:
                raise ValueError()
            encoded = validate(kind, data['value'])
        except (ValueError, TypeError, KeyError, RecursionError):
            return web.json_response({'code': 'INVALID_LEARNING_STATE'}, status=400)
        with db:
            db.execute('BEGIN IMMEDIATE')
            current = read(db, uid, kind)
            # A lost HTTP acknowledgement can safely be retried without another write.
            if current['value'] == data['value']:
                return web.json_response({'userId': uid, **current})
            if current['revision'] != revision:
                return web.json_response({'code': 'LEARNING_CONFLICT', 'current': current}, status=409)
            db.execute('''INSERT INTO learning_state(user_id,kind,value,revision,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id,kind) DO UPDATE SET
                value=excluded.value,revision=excluded.revision,updated_at=excluded.updated_at''',
                (uid, kind, encoded, revision+1, time.time()))
        return web.json_response({'userId': uid, 'value': data['value'], 'revision': revision+1})
    except sqlite3.Error:
        return web.json_response({'code': 'LEARNING_STORAGE_UNAVAILABLE'}, status=503)
