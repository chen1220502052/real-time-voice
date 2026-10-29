"""Durable generated replies and session snapshots, separate from rebuilt教材 DB.

Only conservative general records are queryable across sessions. Personal and
contextual content has no shared retrieval path. No cancelled draft is promoted.
"""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from .chat_library import ReplyMatch, question_key
from .dialogue_policy import sharing_kind
from .writing_vocabulary import known, tokens

class DialogueStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize creation inside the database; don't reset existing sessions.
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS dialogue_turns (
              request_id TEXT NOT NULL, session_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
              question TEXT NOT NULL, answer TEXT NOT NULL, source TEXT NOT NULL,
              sharing TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(session_id,request_id));
            CREATE INDEX IF NOT EXISTS dialogue_session ON dialogue_turns(session_id,created_at);
            CREATE TABLE IF NOT EXISTS general_replies (
              id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, question_key TEXT NOT NULL,
              answer TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS general_lookup ON general_replies(chapter_id, question_key);
            ''')
            db.execute('BEGIN IMMEDIATE')
            keys = {row[1] for row in db.execute('PRAGMA table_info(dialogue_turns)') if row[5]}
            if keys == {'request_id'}:
                # Upgrade the first deployment without dropping any private rows.
                db.execute('ALTER TABLE dialogue_turns RENAME TO dialogue_turns_v1')
                db.execute('''CREATE TABLE dialogue_turns (
                    request_id TEXT NOT NULL, session_id TEXT NOT NULL, chapter_id TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL, source TEXT NOT NULL,
                    sharing TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(session_id,request_id))''')
                db.execute('INSERT INTO dialogue_turns SELECT * FROM dialogue_turns_v1')
                db.execute('DROP TABLE dialogue_turns_v1')
                db.execute('CREATE INDEX dialogue_session ON dialogue_turns(session_id,created_at)')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, request, state, answer, source):
        question = request.messages[-1].content
        sharing = 'personal' if state.facts else sharing_kind(question, answer, request.messages)
        with self.connect() as db:
            inserted = db.execute('INSERT OR IGNORE INTO dialogue_turns '
                '(request_id,session_id,chapter_id,question,answer,source,sharing,state) VALUES (?,?,?,?,?,?,?,?)',
                (request.request_id, request.session_id, request.options['chapterId'], question,
                 answer, source, sharing, json.dumps(state.to_dict(), ensure_ascii=False))).rowcount
            if inserted and source == 'generated' and sharing == 'general' and state.action == 'answer' and state.repeat_count == 1:
                key = question_key(question)
                digest = hashlib.sha256((request.options['chapterId'] + '\0' + key + '\0' + answer).encode()).hexdigest()[:24]
                db.execute('INSERT OR IGNORE INTO general_replies(id,chapter_id,question_key,answer) VALUES (?,?,?,?)',
                           (digest, request.options['chapterId'], key, answer))
        return sharing

    def session_facts(self, session_id, chapter_id):
        with self.connect() as db:
            row = db.execute('SELECT state FROM dialogue_turns WHERE session_id=? AND chapter_id=? '
                             'ORDER BY rowid DESC LIMIT 1', (session_id,chapter_id)).fetchone()
        return tuple(json.loads(row[0]).get('facts', ())) if row else ()

    def lookup(self, scope, question, state, messages):
        if state.facts or state.action != 'answer' or state.repeat_count != 1 or sharing_kind(question, '', messages) != 'general':
            return None
        with self.connect() as db:
            rows = db.execute('SELECT id,answer FROM general_replies WHERE chapter_id=? AND question_key=? '
                              'ORDER BY created_at DESC,id LIMIT 20', (scope.chapter['id'],question_key(question))).fetchall()
        recent = {question_key(t) for t in state.recent_replies}
        for identifier, answer in rows:
            if (question_key(answer) not in recent and sharing_kind(question,answer,messages)=='general'
                    and all(known(w, scope.words) for w in tokens(answer))):
                return ReplyMatch(answer, 'generated_cache', identifier, scope.chapter['id'])
        return None

@lru_cache(maxsize=8)
def _store(path):
    return DialogueStore(path)

def dialogue_store():
    path = os.getenv('CHAT_DIALOGUE_DB') or str(Path(os.getenv('ACCOUNT_DB','.local/accounts.sqlite3')).resolve().with_name('chat-dialogue.sqlite3'))
    return _store(str(Path(path).resolve()))
