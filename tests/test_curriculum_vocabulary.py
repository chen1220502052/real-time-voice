import hashlib
import json
from pathlib import Path

from backend.chapter_practice import ChapterLibrary
from backend.writing_vocabulary import VocabularyScope, lexical_tokens, position

ROOT = Path(__file__).resolve().parents[1]


def library():
    return ChapterLibrary(ROOT / '英语')


def chapter(lib, grade, term, unit):
    return next(c for c in lib.chapters.values() if c.get('curriculum')
                and (c['curriculum']['grade'], c['curriculum']['term'], c['curriculum']['unitNumber']) == (grade,term,unit))


def test_all_112_source_images_have_matching_hashes_and_two_transcription_passes():
    rows = json.loads((ROOT / 'backend/data/curriculum/image-manifest.json').read_text())
    assert len(rows) == 112
    for row in rows:
        assert hashlib.sha256((ROOT / row['image']).read_bytes()).hexdigest() == row['sha256']
        record = json.loads((ROOT / 'backend/data/curriculum/pages' / (row['sha256'][:24] + '.json')).read_text())
        assert record['checked'] and len(record['passes']) == 2


def test_complete_source_catalog_spans_all_seven_grades_and_14_books():
    lib = library()
    rows = [c for c in lib.chapters.values() if c.get('curriculum')]
    assert len(rows) == 118 and len(lib.curriculum_books) == 14
    assert {c['curriculum']['grade'] for c in rows} == set(range(3,10))
    assert sum(len(c['vocabulary']) for c in rows) == 3844
    for c in rows:
        assert c['curriculum']['verified'] and c['curriculum']['sourcePages']
        assert not any(e['uncertain'] for e in c['curriculum']['entries'])
        assert not any(any('\u4e00' <= char <= '\u9fff' for char in e['word']) for e in c['vocabulary'])


def test_shared_page_is_split_by_lesson_instead_of_admitting_all_page_words():
    lib = library()
    one, two, three = (chapter(lib,5,'下册',n) for n in (1,2,3))
    assert 'speak' in one['curriculum']['words']
    assert 'must' in two['curriculum']['words']
    assert 'pupil' in three['curriculum']['words']
    assert 'pupil' not in one['curriculum']['words']
    assert 'pupil' not in two['curriculum']['words']
    assert one['curriculum']['sourcePages'][0]['sha256'] == three['curriculum']['sourcePages'][0]['sha256']
    one, two = (chapter(lib,6,'下册',n) for n in (1,2))
    assert 'angry' not in one['curriculum']['words'] and 'angry' in two['curriculum']['words']
    assert len(one['vocabulary']) == 11 and len(two['vocabulary']) == 10
    assert 'if' in chapter(lib,6,'下册',9)['curriculum']['words']
    assert 'if' not in chapter(lib,6,'下册',10)['curriculum']['words']


def test_vocabulary_is_exact_union_of_sources_and_contains_no_default_foundation():
    lib = library()
    c = chapter(lib,5,'上册',10)
    scope = VocabularyScope(lib,c['id'])
    expected = set()
    for source in scope.sources:
        row = lib.chapters[source['chapterId']]
        assert position(row) <= position(c)
        for word in [*row['curriculum']['words'],*row['curriculum'].get('observedWords',[])]:
            expected.update(lexical_tokens(word))
    assert scope.words == expected
    assert len(scope.sources) == 58 and scope.current_words == []
    assert scope.summary()['textbookReady'] and scope.summary()['verified']


def test_future_lesson_and_junior_editions_do_not_leak_into_scope():
    lib = library()
    c = chapter(lib,7,'下册',7)
    scope = VocabularyScope(lib,c['id'])
    assert not scope.missing_units
    assert all(position(lib.chapters[s['chapterId']]) <= position(c) for s in scope.sources)
    c = chapter(lib,9,'上册',1)
    scope = VocabularyScope(lib,c['id'])
    assert any('7年级' in term for term in scope.missing_terms)
    assert not any('2025年7月' in s['edition'] for s in scope.sources)  # new grade 8 upper is incompatible
    assert not scope.summary()['verified']  # missing preceding editions are visible


def test_dictionary_editorial_abbreviations_are_not_vocabulary_but_real_prefixes_survive():
    assert lexical_tokens('favourite (AmE favorite)') == ['favourite','favorite']
    assert lexical_tokens('child (pl. children)') == ['child','children']
    assert lexical_tokens('drink to sb./sth.') == ['drink','to']
    assert lexical_tokens('please play plant sing single') == ['please','play','plant','sing','single']


def test_missing_publisher_wordlists_are_not_falsely_verified():
    lib = library()
    c = next(c for c in lib.chapters.values() if c['publisher']=='人教版')
    s = VocabularyScope(lib,c['id'])
    assert not s.textbook_ready and not s.summary()['verified']


def test_grade_three_page_boundaries_revision_and_cumulative_scope():
    lib = library()
    three = chapter(lib, 3, '上册', 3)
    assert 'yes' in three['curriculum']['words']
    assert 'mouth' not in three['curriculum']['words']
    scope = VocabularyScope(lib, three['id'])
    assert scope.summary()['verified'] and len(scope.sources) == 3
    assert not scope.knows('mouth')
    assert chapter(lib, 3, '上册', 12)['curriculum']['words'] == ['gift']
    revision = chapter(lib, 3, '下册', 12)
    scope = VocabularyScope(lib, revision['id'])
    assert revision['curriculum']['revision'] and not scope.current_words
    assert scope.summary()['verified'] and len(scope.sources) == 24
    assert scope.knows('silk') and not scope.knows('healthy')
    first_four = VocabularyScope(lib, chapter(lib, 4, '上册', 1)['id'])
    assert first_four.knows('silk') and len(first_four.sources) == 25
    pack = json.loads((ROOT / 'backend/data/curriculum/grade3-words.json').read_text())
    assert len(pack['pages']) == 15
    for page in pack['pages']:
        assert page['checked']
        assert hashlib.sha256((ROOT / '英语/科普版' / page['image']).read_bytes()).hexdigest() == page['sha256']
