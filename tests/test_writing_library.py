"""Writing must use the same revisioned SQLite snapshots as curriculum chat."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from backend.chat_library import ChatLibrary, chat_library
from backend.writing_assistant import WritingAssistant, register_writing
from backend.writing_workflows import student_vocabulary


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / 'english'
    path = root / '科普版/结构化教材词表.json'
    path.parent.mkdir(parents=True)
    chapters = []
    for number, word in [(1, 'play'), (2, 'futureword')]:
        chapters.append({'id': f'unit{number}', 'publisher': '科普版', 'grade': '4年级',
            'edition': '上册_新版', 'unit': f'Unit {number}', 'question': 'What do you like?',
            'textKind': '测试词表', 'vocabulary': [{'word': word, 'meaning': '测试释义'}],
            'curriculum': {'bookId': 'book', 'series': 'series', 'grade': 4, 'term': '上册',
                'unitNumber': number, 'firstUnit': 1, 'lastUnit': 2, 'words': ['i', word],
                'verified': True, 'source': 'test-source', 'sourcePages': [], 'method': 'fixture',
                'revision': False, 'lessonEvidence': []}})
    path.write_text(json.dumps({'version': 2, 'books': [], 'chapters': chapters}, ensure_ascii=False))
    monkeypatch.setenv('ENGLISH_RESOURCE_ROOT', str(root))
    monkeypatch.setenv('CHAT_LIBRARY_DB', str(tmp_path / 'shared-vocabulary.sqlite3'))
    monkeypatch.delenv('CHAT_VOCABULARY_FILE', raising=False)
    monkeypatch.delenv('WRITING_VOCABULARY_FILE', raising=False)
    return path


def replace_word(path, old='play', new='read'):
    before = path.stat()
    changed = path.read_text().replace('"' + old + '"', '"' + new + '"')
    temp = path.with_suffix('.tmp')
    temp.write_text(changed)
    # Same-size replacement with the original mtime must still invalidate caches.
    os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
    temp.replace(path)


def test_writing_and_chat_share_cached_scope_and_sqlite_rows(source, monkeypatch):
    service = WritingAssistant()
    scope = service.scope('unit1')
    repo = chat_library()
    assert scope is repo.scope('unit1') is service.scope('unit1')
    assert scope.chapter['question'] == 'What do you like?'
    assert scope.textbook_ready and 'futureword' not in scope.words
    with sqlite3.connect(repo.path) as db:
        assert db.execute('SELECT word FROM cumulative_words WHERE chapter_id=? ORDER BY word', ('unit1',)).fetchall() == [('i',), ('play',)]
        assert db.execute('SELECT word FROM unit_words WHERE chapter_id=? ORDER BY word', ('unit2',)).fetchall() == [('futureword',), ('i',)]
    def forbidden(*args):
        raise AssertionError('A hot request must not reparse sources or scan every known base')
    monkeypatch.setattr('backend.chat_library._load_library', forbidden)
    monkeypatch.setattr('backend.writing_vocabulary.known', forbidden)
    assert service.scope('unit1') is scope
    assert scope.audit('I play', ' '.join(['I played'] * 15), word_count=30)['passed']
    assert student_vocabulary(scope, '', 'I played. I am replaying.')['outsideWords'] == [
        {'word': 'am', 'count': 1}, {'word': 'replaying', 'count': 1}]


def test_source_updates_refresh_additions_removals_and_restored_revisions(source):
    service = WritingAssistant()
    first = service.scope('unit1')
    old_generation = chat_library().generation
    replace_word(source)
    updated = service.scope('unit1')
    assert updated is chat_library().scope('unit1') and updated is not first
    assert chat_library().generation != old_generation
    assert updated.knows('reading') and not updated.knows('played')
    assert 'read' in service.scope('unit2').words
    assert 'futureword' not in updated.words
    assert first.knows('played') and not first.knows('reading')  # in-flight snapshot is stable
    replace_word(source, 'read', 'play')
    restored = service.scope('unit1')
    assert restored.knows('played') and not restored.knows('reading')
    with sqlite3.connect(chat_library().path) as db:
        assert db.execute("SELECT count(*) FROM cumulative_words WHERE word='read'").fetchone()[0] == 0


def test_cached_snapshot_metadata_cannot_be_modified_by_callers(source):
    service = WritingAssistant()
    scope = service.scope('unit1')
    scope.chapter['question'] = 'Changed'
    scope.current_words.clear()
    scope.sources.clear()
    scope.summary()['sources'].clear()
    with pytest.raises(AttributeError):
        scope.words.add('futureword')
    assert service.scope('unit1').chapter['question'] == 'What do you like?'
    assert scope.current_words == ['i', 'play'] and scope.sources


def test_invalid_source_keeps_previous_database_and_recovers_after_repair(source):
    service = WritingAssistant()
    previous = service.scope('unit1')
    repo = chat_library()
    original = source.read_text()
    source.write_text('{broken')
    with pytest.raises(ValueError):
        service.scope('unit1')
    assert repo.scope('unit1') is previous
    source.write_text(original)
    assert service.scope('unit1').words == previous.words


def test_concurrent_first_requests_build_one_database(source, monkeypatch):
    original = ChatLibrary.build.__func__
    builds = []
    def build(cls, *args, **kwargs):
        builds.append(1)
        return original(cls, *args, **kwargs)
    monkeypatch.setattr(ChatLibrary, 'build', classmethod(build))
    service = WritingAssistant()
    with ThreadPoolExecutor(max_workers=6) as pool:
        scopes = list(pool.map(lambda _: service.scope('unit1'), range(12)))
    assert len(builds) == 1
    assert all(s.words == frozenset({'i', 'play'}) for s in scopes)


def test_all_writing_endpoints_use_updated_sqlite_snapshot(source, monkeypatch):
    observed = []
    async def compose(assistant, scope, *args, **kwargs):
        observed.append(('compose', scope))
        return {'words': sorted(scope.words)}
    async def review(scope, title, essay, *args):
        observed.append(('review', scope))
        return student_vocabulary(scope, title, essay)
    monkeypatch.setattr('backend.writing_workflows.model_essay', compose)
    monkeypatch.setattr('backend.writing_workflows.review_essay', review)
    class Manager:
        async def reserve(self, owner): return True
        async def release(self, owner): pass
    async def run():
        app = web.Application()
        app['resource_manager'] = Manager()
        register_writing(app)
        async with TestClient(TestServer(app)) as client:
            before = await (await client.get('/api/writing/scopes/unit1')).json()
            assert before['currentWords'] == ['i', 'play']
            replace_word(source)
            after = await (await client.get('/api/writing/scopes/unit1')).json()
            assert after['currentWords'] == ['i', 'read']
            response = await client.post('/api/writing/compositions', json={'chapterId': 'unit1'})
            assert response.status == 200 and (await response.json())['words'] == ['i', 'read']
            response = await client.post('/api/writing/reviews', json={'chapterId': 'unit1', 'essay': 'I read. I play.'})
            assert response.status == 200 and (await response.json())['outsideWords'] == [{'word': 'play', 'count': 1}]
            assert observed[0][1] is observed[1][1] is WritingAssistant().scope('unit1')
    asyncio.run(run())


def test_verified_override_updates_the_same_database(source, monkeypatch):
    override = source.parent / 'verified.json'
    override.write_text(json.dumps({'version': 1, 'foundation': ['i'], 'chapters': {
        'unit1': {'words': ['play'], 'source': 'verified-page'},
        'unit2': {'words': ['futureword'], 'source': 'verified-page'}}}))
    monkeypatch.setenv('WRITING_VOCABULARY_FILE', str(override))
    service = WritingAssistant()
    first = service.scope('unit1')
    assert first is chat_library().scope('unit1')
    assert first.words == frozenset({'i', 'play'})
    replace_word(override)
    assert service.scope('unit1').words == frozenset({'i', 'read'})
    assert service.scope('unit1') is chat_library().scope('unit1')


def test_real_curriculum_roundtrip_preserves_scopes_audits_and_indexed_sources(tmp_path):
    from backend.chapter_practice import ChapterLibrary
    from backend.writing_vocabulary import VocabularyScope
    library = ChapterLibrary(Path(__file__).resolve().parents[1] / '英语')
    repo = ChatLibrary.build(library, tmp_path / 'real.sqlite3')
    for row in library.chapters.values():
        if not row.get('curriculum'):
            continue
        expected = VocabularyScope(library, row['id'])
        stored = repo.scope(row['id'])
        assert stored.words == expected.words
        assert stored.summary() == expected.summary()
        assert stored.chapter['question'] == row['question']
        text = 'I read stories and play with friends. Quantum quantum.'
        assert stored.audit('My School', text) == expected.audit('My School', text)
        assert student_vocabulary(stored, '', text) == student_vocabulary(expected, '', text)
        assert all(page['url'].startswith('/api/practice/vocabulary-pages/')
                   for source in stored.sources for page in source.get('sourcePages', []))
    assert len(repo.scope('d2435143bdba0792312a2ecc').words) == 523
