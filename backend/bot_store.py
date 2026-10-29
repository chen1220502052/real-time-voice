"""Bot-owned durable interaction ledger and idempotency journal (single worker).

SQLite work runs off the media event loop. No student audio is persisted.
"""

import asyncio
import fcntl
import json
from pathlib import Path
import os
import sqlite3
import time

from .text_bot import TextBotError


class BotStore:
    def __init__(self, path=":memory:"):
        self.lease = None
        if path != ":memory:":
            folder = Path(path).resolve().parent
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            self.lease = os.fdopen(descriptor, "w")
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lease.close()
                raise RuntimeError("Bot state database requires one owning worker") from None
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        if path != ":memory:":
            os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL, request TEXT NOT NULL, reply TEXT NOT NULL,
                turn TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, events TEXT NOT NULL DEFAULT '[]',
                created REAL NOT NULL, UNIQUE(session, request), UNIQUE(session, reply),
                UNIQUE(session, turn)
            );
            CREATE TABLE IF NOT EXISTS interactions (
                session TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(session, id)
            );
            CREATE INDEX IF NOT EXISTS interaction_session ON interactions(session);
        """)
        # Accepted work is never silently regenerated after a worker crash.
        for session, request, events in self.db.execute("SELECT session, request, events FROM requests WHERE status='running'").fetchall():
            values = json.loads(events)
            values.append({"version": 2, "requestId": request, "type": "error",
                           "eventSeq": len(values) + 1, "code": "BOT_RESTARTED",
                           "message": "Bot restarted before this reply completed"})
            self.db.execute("UPDATE requests SET status='error', events=? WHERE session=? AND request=?",
                            (json.dumps(values), session, request))
        self.db.commit()
        self.lock = asyncio.Lock()
        self.retention_days = max(1, min(30, int(os.getenv("BOT_STATE_RETENTION_DAYS", "7"))))
        self.last_purge = 0
        self._purge()
        self.db.commit()

    def _purge(self):
        now = time.time()
        if now - self.last_purge < 3600:
            return
        cutoff = now - self.retention_days * 86400
        expired = self.db.execute("""
            SELECT session FROM (
                SELECT session, created FROM requests UNION ALL
                SELECT session, created FROM interactions
            ) GROUP BY session HAVING MAX(created) < ?
        """, (cutoff,)).fetchall()
        for (session,) in expired:
            if not self.db.execute("SELECT 1 FROM requests WHERE session=? AND status='running'", (session,)).fetchone():
                self.db.execute("DELETE FROM requests WHERE session=?", (session,))
                self.db.execute("DELETE FROM interactions WHERE session=?", (session,))
        self.last_purge = now

    async def _call(self, operation):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not release the connection lock while its thread is running.
                await task
                raise

    async def get(self, session, request):
        def read():
            row = self.db.execute("SELECT fingerprint,status,events,payload FROM requests WHERE session=? AND request=?", (session, request)).fetchone()
            return None if row is None else dict(fingerprint=row[0], status=row[1], events=json.loads(row[2]), payload=json.loads(row[3]))
        return await self._call(read)

    async def accept(self, query):
        def write():
            try:
                self._purge()
                count = self.db.execute("SELECT COUNT(*) FROM requests WHERE session=?", (query.session_id,)).fetchone()[0]
                if count >= 1000:
                    raise TextBotError("BOT_SESSION_LIMIT", "Start a new conversation")
                self.db.execute("INSERT INTO requests(session,request,reply,turn,fingerprint,payload,status,created) VALUES(?,?,?,?,?,?,'running',?)",
                                (query.session_id, query.request_id, query.reply_id or query.request_id,
                                 query.turn_id, query.fingerprint(), json.dumps(query.to_dict()), time.time()))
                self.db.commit()
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise TextBotError("BOT_ID_CONFLICT", "Request, turn or reply ID already exists") from None
        await self._call(write)

    async def save(self, session, request, events, status):
        payload = json.dumps(events, ensure_ascii=False)
        def write():
            self.db.execute("UPDATE requests SET events=?,status=? WHERE session=? AND request=?", (payload, status, session, request))
            self.db.commit()
        await self._call(write)

    async def observe(self, session, events, *, ignore_unknown=False):
        def write():
            try:
                for event in events:
                    value = dict(event)
                    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
                    old = self.db.execute("SELECT payload FROM interactions WHERE session=? AND id=?", (session, value["eventId"])).fetchone()
                    if old and old[0] != payload:
                        raise TextBotError("BOT_EVENT_CONFLICT", "Interaction ID reused with different facts")
                    if not old and not self.db.execute("SELECT 1 FROM requests WHERE session=? AND reply=?", (session, value["replyId"])).fetchone():
                        if ignore_unknown:
                            continue
                        raise TextBotError("BOT_UNKNOWN_REPLY", "Interaction does not belong to this session")
                    self.db.execute("INSERT OR IGNORE INTO interactions VALUES(?,?,?,?)", (session, value["eventId"], payload, time.time()))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        if events:
            await self._call(write)

    async def snapshot(self, session):
        def read():
            rows = self.db.execute("SELECT payload,events,status FROM requests WHERE session=? ORDER BY position", (session,)).fetchall()
            facts = [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM interactions WHERE session=?", (session,))]
            return [(json.loads(p), json.loads(e), s) for p, e, s in rows], facts
        return await self._call(read)

    async def close(self):
        def close():
            self.db.close()
            if self.lease:
                self.lease.close()
        await self._call(close)
