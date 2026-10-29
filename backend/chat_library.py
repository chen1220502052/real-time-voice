"""Versioned SQLite curriculum snapshots and reviewed, exact-intent replies."""
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unicodedata

from .chapter_practice import ChapterLibrary
from .writing_vocabulary import VocabularyScope, IRREGULAR, tokens

DATA = Path(__file__).parent / 'data/chat'
SCHEMA_VERSION = 3
_BUILD_LOCK = threading.Lock()


def lexical_tokens(text):
    text = re.sub(r'\b(?:AmE|BrE|sb|sth)\b\.?', ' ', text, flags=re.I)
    return tokens(re.sub(r'\b(?:pl|sing)\.', ' ', text, flags=re.I))


def _scope(library, chapter_id, verified_path):
    return VocabularyScope(library, chapter_id, verified_path)


def _load_library(root):
    library=ChapterLibrary(root)
    source=Path(root)/'科普版/结构化教材词表.json'
    if source.is_file():
        return library  # ChapterLibrary already validated and indexed original pages.
    source=DATA/'curriculum-vocabulary.json'
    if source.is_file():
        pack=json.loads(source.read_text())
        if pack.get('version')!=2:
            raise ValueError('Unsupported curriculum vocabulary pack')
        for row in pack['chapters']:
            chapter=library.chapters.setdefault(row['id'],dict(row))
            chapter['curriculum']=row['curriculum']
            chapter['vocabulary']=row['vocabulary']
    return library


def question_key(text):
    text = unicodedata.normalize('NFKC', text).lower().replace('’', "'")
    for short, expanded in {"can't":"can not", "won't":"will not", "i'm":"i am",
                            "you're":"you are", "what's":"what is", "how's":"how is",
                            "it's":"it is", "that's":"that is", "let's":"let us"}.items():
        text = re.sub(r'\b' + re.escape(short) + r'\b', expanded, text)
    text = re.sub(r"n't\b", ' not', text)
    return ' '.join(re.sub(r"[^a-z0-9\u4e00-\u9fff']+", ' ', text).split())


@dataclass(frozen=True)
class ReplyMatch:
    text: str
    source: str
    answer_id: str
    chapter_id: str | None


class StoredScope(VocabularyScope):
    def __init__(self, payload):
        self._chapter = payload['chapter']
        self.words = frozenset(payload['words'])
        self.known_forms = frozenset(_known_forms(self.words))
        self._summary = payload['summary']
        self.textbook_ready = self._summary.get('textbookReady', False)

    @property
    def chapter(self):
        return deepcopy(self._chapter)

    @property
    def current_words(self):
        return deepcopy(self._summary['currentWords'])

    @property
    def sources(self):
        return deepcopy(self._summary['sources'])

    def knows(self, word):
        return word in self.known_forms

    def summary(self):
        # Cached snapshots stay isolated from response/model mutations.
        return deepcopy(self._summary)


class ChatLibrary:
    """Connections are short-lived; a rebuilt database cannot leave stale readers."""
    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def build(cls, library, path, *, prepared_path=DATA/'prepared-replies.json',
              textbook_path=DATA/'textbook-replies.json', verified_path=None, revision='manual',
              bulk_path=None, sentences_path=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
        os.close(handle)
        try:
            with sqlite3.connect(temporary) as db:
                db.executescript('''
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE chapters (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE unit_words (chapter_id TEXT, word TEXT, PRIMARY KEY(chapter_id, word));
                CREATE TABLE cumulative_words (chapter_id TEXT, word TEXT, PRIMARY KEY(chapter_id, word));
                CREATE TABLE answers (id TEXT PRIMARY KEY, origin TEXT NOT NULL, chapter_id TEXT,
                    reply TEXT NOT NULL, provenance TEXT NOT NULL);
                CREATE TABLE questions (answer_id TEXT, question_key TEXT, context_key TEXT NOT NULL,
                    PRIMARY KEY(answer_id, question_key, context_key));
                CREATE INDEX questions_lookup ON questions(question_key, context_key);
                CREATE TABLE eligible_answers (chapter_id TEXT, answer_id TEXT,
                    PRIMARY KEY(chapter_id, answer_id));
                ''')
                scopes = []
                for chapter in library.chapters.values():
                    try:
                        scope = _scope(library, chapter['id'], verified_path)
                    except ValueError:
                        continue  # e.g. an unsortable full-year resource remains unselectable.
                    payload = {'chapter':{k:chapter.get(k, '') for k in ('id','publisher','grade','edition','unit','question')},
                               'words':sorted(scope.words), 'summary':scope.summary()}
                    db.execute('INSERT INTO chapters VALUES (?,?)', (chapter['id'], json.dumps(payload, ensure_ascii=False)))
                    db.executemany('INSERT INTO unit_words VALUES (?,?)',
                        ((chapter['id'], w) for w in sorted(set(lexical_tokens(' '.join(scope.current_words))))))
                    db.executemany('INSERT INTO cumulative_words VALUES (?,?)',
                        ((chapter['id'], w) for w in sorted(scope.words)))
                    scopes.append(scope)
                db.executescript('''CREATE TABLE textbook_sentences (
                    id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL, sentence TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL, source TEXT NOT NULL);
                    CREATE TABLE eligible_sentences (chapter_id TEXT, sentence_id TEXT,
                    PRIMARY KEY(chapter_id,sentence_id));''')
                sentence_rows = []
                if sentences_path:
                    for row in json.loads(Path(sentences_path).read_text())['sentences']:
                        chapter = library.chapters.get(row['chapterId'])
                        if not chapter or not any(page['sha256']==row['evidenceSha256'] and row['text'] in page.get('sentences',[])
                            for page in chapter.get('curriculum',{}).get('lessonEvidence',[])):
                            raise ValueError('Unverified textbook sentence')
                        identifier = hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
                        db.execute('INSERT INTO textbook_sentences VALUES (?,?,?,?,?)',
                            (identifier,row['chapterId'],row['text'],row['evidenceSha256'],row['source']))
                        sentence_rows.append((identifier,row['chapterId'],row['text']))
                answers = []
                packs = [('textbook',textbook_path),('prepared',prepared_path)]
                if bulk_path:
                    packs.append(('prepared',bulk_path))
                for pack_origin, source in packs:

                    pack = json.loads(Path(source).read_text())
                    if pack.get('version') != 1:
                        raise ValueError('Unsupported reply pack')
                    for row in pack['answers']:
                        origin = row.get('origin',pack_origin)
                        if origin not in {'textbook','prepared'}:
                            raise ValueError('Invalid answer origin')
                        if not row.get('reviewed'):
                            continue
                        if (not isinstance(row.get('reply'), str) or not row['reply'].strip()
                                or len(row['reply']) > 600 or not row['reply'].endswith(tuple('.!?。！？'))
                                or not isinstance(row.get('questions'), list) or not row['questions']
                                or any(not isinstance(q,str) or not question_key(q) for q in row['questions'])
                                or not isinstance(row.get('source'), str) or not row['source']):
                            raise ValueError('Invalid reviewed answer')
                        anchor = row.get('chapterId')
                        if anchor and anchor not in library.chapters:
                            continue
                        if origin == 'textbook':
                            if not anchor or not _textbook_evidence(library.chapters[anchor], row):
                                continue  # A pack must never manufacture textbook provenance.
                        identifier = hashlib.sha256(json.dumps([origin, row], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
                        db.execute('INSERT INTO answers VALUES (?,?,?,?,?)',
                            (identifier, origin, anchor, row['reply'], row['source']))
                        contexts = row.get('contexts', [''])
                        # Short replies are unsafe without an exact previous-assistant context.
                        if any(question_key(q) in {'yes','no','why','是','不是','好','不'} for q in row['questions']) and not all(contexts):
                            raise ValueError('Context is required for a short conversational answer')
                        db.executemany('INSERT INTO questions VALUES (?,?,?)',
                            sorted({(identifier, question_key(q), question_key(context))
                             for q in row['questions'] for context in contexts}))
                        answers.append((identifier, anchor, row['reply'], row.get('requiredWords',[]),row.get('currentUnitOnly',False)))
                # Validate once while importing, never re-scan vocabulary on a hot hit.
                for scope in scopes:
                    included = {s['chapterId'] for s in scope.sources}
                    forms = _known_forms(scope.words)
                    for identifier, anchor, reply, required, current_only in answers:
                        if anchor and anchor not in included:
                            continue
                        if current_only and anchor != scope.chapter['id']:
                            continue
                        words = lexical_tokens(reply)
                        if words and set(words) <= forms and set(lexical_tokens(' '.join(required))) <= forms:
                            db.execute('INSERT INTO eligible_answers VALUES (?,?)', (scope.chapter['id'], identifier))
                    for identifier, anchor, sentence in sentence_rows:
                        if anchor in included and set(lexical_tokens(sentence)) <= forms:
                            db.execute('INSERT INTO eligible_sentences VALUES (?,?)',(scope.chapter['id'],identifier))
                db.executemany('INSERT INTO metadata VALUES (?,?)', [('version',str(SCHEMA_VERSION)),('revision',revision)])
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return cls(path)

    @property
    def generation(self):
        info = self.path.stat()
        return (str(self.path), info.st_mtime_ns, info.st_ctime_ns, info.st_ino, info.st_size)

    def scope(self, chapter_id):
        if not isinstance(chapter_id,str) or not 1 <= len(chapter_id) <= 64:
            raise ValueError('请选择有效教材单元')
        return _cached_scope(self.generation, chapter_id)

    def lookup(self, scope, text, messages=(), *, exclude=(), context_only=False):
        context = next((m.content for m in reversed(messages) if m.role == 'assistant'), '')
        blocked = {question_key(t) for t in exclude}
        db = sqlite3.connect(self.path)
        try:
            rows = db.execute('''SELECT a.reply,a.origin,a.id,a.chapter_id FROM questions q
                JOIN answers a ON a.id=q.answer_id
                JOIN eligible_answers e ON e.answer_id=a.id AND e.chapter_id=?
                WHERE q.question_key=? AND (q.context_key='' OR q.context_key=?)
                  AND (?=0 OR q.context_key<>'')
                ORDER BY CASE a.origin WHEN 'textbook' THEN 0 ELSE 1 END,
                    CASE WHEN a.chapter_id=? THEN 0 ELSE 1 END,
                    CASE WHEN q.context_key<>'' THEN 0 ELSE 1 END,
                    CASE WHEN q.context_key<>'' THEN length(a.reply) ELSE 0 END DESC, a.id''',
                (scope.chapter['id'],question_key(text),question_key(context),int(context_only),scope.chapter['id']))
            return next((ReplyMatch(*row) for row in rows if question_key(row[0]) not in blocked),None)
        finally:
            db.close()

    def lookup_next(self, scope, text, messages, exclude):
        return self.lookup(scope,text,messages,exclude=exclude)

    def lookup_context(self, scope, text, messages, exclude):
        return self.lookup(scope,text,messages,exclude=exclude,context_only=True)

    def stats(self):
        with sqlite3.connect(self.path) as db:
            return {name:db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                    for name in ('chapters','unit_words','cumulative_words','answers','eligible_answers','textbook_sentences','eligible_sentences')}


def _known_forms(vocabulary):
    """Expand exactly the forms accepted by writing_vocabulary.known, once per unit."""
    forms = set(vocabulary)
    forms.update(word for word, base in IRREGULAR.items() if base in vocabulary)
    for base in vocabulary:
        forms.update((base+'s',base+'es',base+'ed',base+'ing'))
        if base.endswith('e'):
            forms.update((base+'d',base[:-1]+'ing'))
        if len(base)>2 and base.endswith('y') and base[-2] not in 'aeiou':
            forms.update((base[:-1]+'ies',base[:-1]+'ied'))
        if len(base)>2 and base[-1] not in 'aeiouwxy' and base[-2] in 'aeiou' and base[-3] not in 'aeiou':
            forms.update((base+base[-1]+'ed',base+base[-1]+'ing'))
    return forms


def _textbook_evidence(chapter, row):
    for page in chapter.get('curriculum',{}).get('lessonEvidence',[]):
        if page.get('sha256') == row.get('evidenceSha256'):
            sentences = page.get('sentences',[])
            if row.get('quote'):
                return (row['quote'] in sentences and row['reply']=='课文原句：'+row['quote']
                    and (set(row['questions']) <= {'Read a sentence from this unit.','Give me a textbook sentence.','读一句本单元课文'}
                         or (row.get('bookQuestion') in sentences and set(row['questions']) <=
                             {'In the book, '+row['bookQuestion'],'课文里，'+row['bookQuestion']}))
                    and row.get('currentUnitOnly') is True)
            return row['reply'] in sentences and all(q in sentences for q in row['questions'])
    return False


@lru_cache(maxsize=64)
def _cached_scope(generation, chapter_id):
    with sqlite3.connect(generation[0]) as db:
        row = db.execute('SELECT payload FROM chapters WHERE id=?',(chapter_id,)).fetchone()
    if not row:
        raise ValueError('请选择有效教材单元')
    return StoredScope(json.loads(row[0]))


def _source_revision(root, verified_path):
    sources = [Path(root)/p for p in ['人教版/新版单元听说训练.json','科普版/单元原创补充训练.json','科普版/结构化教材词表.json','人教版/三年级单元训练.json','科普版/三年级单元训练.json']]
    sources += [DATA/'bulk-replies.json',DATA/'textbook-sentences.json',DATA/'prepared-replies.json',DATA/'textbook-replies.json',DATA/'curriculum-vocabulary.json',Path(__file__).parent/'data/writing/foundation.json']
    if verified_path:
        sources.append(Path(verified_path))
    # Content hash is stable across a release copy and detects same-size replacements.
    values = [(p.name, _file_digest(str(p), p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size)
               if p.is_file() else None) for p in sources]
    return hashlib.sha256(json.dumps([SCHEMA_VERSION,values],ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=64)
def _file_digest(path, mtime_ns, ctime_ns, size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ready_library(root, path, verified_path, revision):
    with _BUILD_LOCK:
        valid = False
        if Path(path).is_file():
            try:
                with sqlite3.connect(path) as db:
                    valid = dict(db.execute('SELECT key,value FROM metadata')) == {'version':str(SCHEMA_VERSION),'revision':revision}
            except sqlite3.DatabaseError:
                pass
        if not valid:
            ChatLibrary.build(_load_library(root),path,verified_path=verified_path,revision=revision,
                bulk_path=DATA/'bulk-replies.json',sentences_path=DATA/'textbook-sentences.json')
    return ChatLibrary(path)


def chat_library():
    root = os.getenv('ENGLISH_RESOURCE_ROOT') or str(Path(__file__).resolve().parents[1]/'英语')
    account_db = os.getenv('ACCOUNT_DB') or str(Path(__file__).resolve().parents[1]/'.local/accounts.sqlite3')
    path = os.getenv('CHAT_LIBRARY_DB') or str(Path(account_db).parent/'chat-library.sqlite3')
    verified = os.getenv('CHAT_VOCABULARY_FILE') or os.getenv('WRITING_VOCABULARY_FILE')
    return _ready_library(root,path,verified,_source_revision(root,verified))
