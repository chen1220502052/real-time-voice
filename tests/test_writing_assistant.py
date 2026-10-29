import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, web
import pytest

from backend.chapter_practice import ChapterLibrary
from backend.writing_assistant import WritingAssistant, WritingError, register_writing
from backend.writing_vocabulary import VocabularyScope, known, tokens

ROOT = Path(__file__).resolve().parents[1]


def library():
    chapters = {}
    for publisher in ('科普版', '人教版'):
        for grade in (4, 5, 6):
            for term in ('上册', '下册'):
                for unit in range(1, 13):
                    for family in ('新版', '旧版'):
                        key = f'{publisher}-{grade}-{term}-{unit}-{family}'
                        chapters[key] = {'id': key, 'publisher': publisher, 'grade': f'{grade}年级',
                            'edition': f'{term}_{family}', 'unit': f'Unit {unit} Test',
                            'question': 'What do you like?', 'textKind': '原创补充',
                            'vocabulary': [{'word': f'word{grade}', 'meaning': ''}, {'word': 'play', 'meaning': '玩'}]}
    return SimpleNamespace(chapters=chapters)


def scope():
    return VocabularyScope(library(), '科普版-5-上册-10-新版')


def essay(title='I play', text=None, glossary=None):
    return {'title': title, 'essay': text or ' '.join(['I play'] * 15), 'glossary': glossary or {}}


class Agent:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, state, config):
        self.calls.append(state)
        content = next(self.responses)
        if isinstance(content, dict):
            content = json.dumps(content)
        return {'messages': [*state['messages'], SimpleNamespace(content=content)]}


def test_grade5_upper10_includes_prior_semesters_and_stops_at_unit10():
    s = scope()
    assert len(s.sources) == 34
    assert [(x['grade'], x['edition'], x['unit']) for x in s.sources][-1] == ('5年级', '上册_新版', 'Unit 10 Test')
    assert all(x['chapterId'].startswith('科普版-') and x['chapterId'].endswith('-新版') for x in s.sources)
    assert sum(x['grade'] == '4年级' for x in s.sources) == 24
    assert not any(x['grade'] == '6年级' or x['edition'] == '下册_新版' and x['grade'] == '5年级' for x in s.sources)
    assert s.summary()['verified'] is False


def test_lower_semester_includes_upper_and_only_current_lower_units():
    s = VocabularyScope(library(), '科普版-5-下册-2-新版')
    assert len(s.sources) == 38
    assert s.sources[-1]['unit'] == 'Unit 2 Test'


def test_reference_word_membership_does_not_leak_future_words():
    lib = library()
    lib.chapters['科普版-5-上册-10-新版']['vocabulary'] = [{'word': 'current', 'meaning': ''}]
    lib.chapters['科普版-5-上册-11-新版']['vocabulary'] = [{'word': 'futureword', 'meaning': ''}]
    lib.chapters['人教版-4-上册-1-新版']['vocabulary'] = [{'word': 'otherpublisher', 'meaning': ''}]
    lib.chapters['科普版-4-上册-1-旧版']['vocabulary'] = [{'word': 'oldedition', 'meaning': ''}]
    s = VocabularyScope(lib, '科普版-5-上册-10-新版')
    assert 'current' in s.words
    assert not s.words & {'futureword', 'otherpublisher', 'oldedition'}


def test_real_grade5_example_uses_58_units_in_numeric_order():
    lib = ChapterLibrary(ROOT / '英语')
    current = next(c for c in lib.chapters.values() if c['publisher'] == '科普版' and c['grade'] == '5年级'
                   and c['edition'].startswith('上册') and c['unit'].startswith('Unit 10 '))
    s = VocabularyScope(lib, current['id'])
    assert len(s.sources) == 58
    assert s.sources[-1]['chapterId'] == current['id']
    assert s.summary()['verified']
    assert s.summary()['textbookReady']
    assert s.current_words == []  # Unit 10 is revision, with no new-word page.


def test_verified_pack_replaces_supplement_and_reports_missing_terms(tmp_path):
    lib = library()
    selected = '科普版-5-上册-10-新版'
    sources = VocabularyScope(lib, selected).sources
    pack = {'version': 1, 'foundation': ['i'], 'chapters': {
        row['chapterId']: {'words': ['verifiedword'], 'source': '学校核对词表'} for row in sources}}
    path = tmp_path / 'words.json'
    path.write_text(json.dumps(pack))
    s = VocabularyScope(lib, selected, path)
    assert s.summary()['verified']
    assert s.words == {'i', 'verifiedword'}
    del lib.chapters['科普版-4-下册-1-新版']
    for key in list(lib.chapters):
        if key.startswith('科普版-4-下册-'):
            del lib.chapters[key]
    s = VocabularyScope(lib, selected, path)
    assert not s.summary()['verified']
    assert s.missing_terms == ['4年级下册']


def test_contractions_and_inflections_are_not_derivations():
    assert tokens("I'm happy. I don't play. We're here. Tom’s book.") == ['i','am','happy','i','do','not','play','we','are','here','tom','book']
    words = {'play', 'child', 'study', 'run', 'write', 'go', 'make'}
    assert all(known(w, words) for w in ['playing','played','children','studies','studied','running','written','went','making'])
    assert not known('playfulness', words)
    assert not known('replay', words)


def test_audit_counts_title_names_and_repeated_outside_occurrences():
    s = scope()
    assert s.audit('I play', essay()['essay'], word_count=30)['passed']
    result = s.audit('Quantum', essay()['essay'], word_count=30)
    assert result['outsideWords'] == [{'word': 'quantum', 'count': 1}]
    assert result['passed']  # A necessary unfamiliar word is allowed within 5%.
    assert s.audit('Quantum', ' '.join(['I play'] * 20), word_count=40)['passed']
    assert not s.audit('Quantum', ' '.join(['I play'] * 20), strict=True, word_count=40)['passed']
    result = s.audit('Tom', ' '.join(['quantum'] * 5 + ['I play'] * 20), word_count=40)
    assert result['outsideCount'] == 6 and not result['passed']
    assert not s.audit('I', 'I play.', word_count=80)['passed']
    assert not s.audit('I', essay()['essay'] + ' 中文', word_count=30)['passed']


def test_server_repairs_even_if_agent_did_not_use_check_tool():
    agent = Agent([essay(title='Quantum'), essay()])
    service = WritingAssistant(agent_factory=lambda *args: agent)
    result = asyncio.run(service.generate(scope(), '写作文', 30, False))
    assert result['attempts'] == 2 and result['audit']['passed']
    assert len(agent.calls) == 2
    assert 'outsideWords' in agent.calls[1]['messages'][-1]['content']


def test_passing_first_draft_still_gets_editorial_revision():
    draft = essay()
    revised = essay(title='I play', text=' '.join(['I play'] * 16))
    agent = Agent([draft, revised])
    service = WritingAssistant(agent_factory=lambda *args: agent)
    result = asyncio.run(service.generate(scope(), '写作文', 30, False))
    assert result['essay'] == revised['essay'] and result['attempts'] == 2
    assert '搭配、指代、时态和表达是否自然' in agent.calls[1]['messages'][-1]['content']


def test_editorial_revision_is_checked_again_and_can_keep_necessary_new_words():
    revised = essay(title='School story', text=' '.join(['I play'] * 20), glossary={'story':'故事'})
    s = scope()
    s.words.add('school')
    agent = Agent([essay(), essay(title='Quantum', text=' '.join(['quantum'] * 40)), revised])
    result = asyncio.run(WritingAssistant(agent_factory=lambda *args: agent).generate(s, '写作文', 40, False))
    assert result['attempts'] == 3 and result['glossary'] == {'story':'故事'}
    assert result['audit']['passed']


def test_default_vocabulary_allowance_is_bounded_and_strict_mode_is_unchanged():
    s = scope()
    body = ' '.join(['I play'] * 38)  # 76 known words
    at_limit = s.audit('alpha beta gamma delta', body, word_count=80)
    assert at_limit['passed'] and at_limit['outsideRatio'] == .05
    assert not s.audit('alpha beta gamma delta epsilon', body, word_count=80)['passed']
    assert not s.audit('alpha', body + ' alpha alpha alpha alpha', word_count=80)['passed']
    assert not s.audit('alpha', body, strict=True, word_count=80)['passed']


def test_server_requires_glossary_and_bounds_failed_rewrites():
    agent = Agent([essay(title='Quantum', text=' '.join(['I play'] * 20))] * 3)
    service = WritingAssistant(agent_factory=lambda *args: agent)
    with pytest.raises(WritingError) as caught:
        asyncio.run(service.generate(scope(), '写作文', 40, False))
    assert caught.value.code == 'WRITING_CONSTRAINT_FAILED'
    assert len(agent.calls) == 3


def test_bad_json_repaired_and_glossary_is_chinese():
    agent = Agent(['not json', essay(title='Quantum', text=' '.join(['I play'] * 20), glossary={'quantum':'量子'})])
    service = WritingAssistant(agent_factory=lambda *args: agent)
    result = asyncio.run(service.generate(scope(), '写作文', 40, False))
    assert result['glossary'] == {'quantum': '量子'}


def test_writing_api_validation_capacity_and_provider_errors():
    class Manager:
        available = True
        reserved = set()

        async def reserve(self, owner):
            if not self.available:
                return False
            self.reserved.add(owner)
            return True

        async def release(self, owner):
            self.reserved.remove(owner)

    async def run():
        agent = Agent([essay(), essay()])
        assistant = WritingAssistant(agent_factory=lambda *args: agent,
                                     scope_factory=lambda key: VocabularyScope(library(), key))
        app = web.Application()
        manager = app['resource_manager'] = Manager()
        register_writing(app, assistant)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        payload = {'chapterId': '科普版-5-上册-10-新版', 'wordCount': 30}
        try:
            async with ClientSession() as client:
                async with client.get(url + '/api/writing/scopes/' + payload['chapterId']) as r:
                    assert r.status == 200 and len((await r.json())['sources']) == 34
                for invalid in [[], {**payload, 'wordCount': True}, {**payload, 'strict': 'false'},
                                {**payload, 'wordCount': 301}, {**payload, 'allowedWords': ['quantum']},
                                {**payload, 'chapterId': 'unknown'}]:
                    async with client.post(url + '/api/writing/compositions', json=invalid) as r:
                        assert r.status == 400
                manager.available = False
                async with client.post(url + '/api/writing/compositions', json=payload) as r:
                    assert r.status == 429
                manager.available = True
                async with client.post(url + '/api/writing/compositions', json=payload) as r:
                    assert r.status == 200 and (await r.json())['audit']['passed']
                    assert r.headers['Cache-Control'] == 'no-store'
                assert not manager.reserved
                # Exhausted fake provider raises; response cannot expose exception details.
                async with client.post(url + '/api/writing/compositions', json=payload) as r:
                    assert r.status == 502 and (await r.json())['code'] == 'WRITING_PROVIDER_FAILED'
                assert not manager.reserved
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_writing_endpoints_require_account_authentication(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from backend.app import create_app
    monkeypatch.setenv('ACCOUNT_AUTH_ENABLED', '1')

    async def run():
        app = create_app(warmup=False, voice_enabled=False, bot_state_path=':memory:')
        async with TestClient(TestServer(app)) as client:
            assert (await client.get('/writing.html', allow_redirects=False)).status == 302
            assert (await client.get('/api/writing/scopes/unknown')).status == 401
            assert (await client.post('/api/writing/compositions', json={})).status == 401
            assert (await client.post('/api/writing/reviews', json={})).status == 401
    asyncio.run(run())
