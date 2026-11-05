"""Scoped acceptance checks for the separately installed grade-three resources."""
import hashlib
import json
from pathlib import Path

import pytest

from backend.chapter_practice import ChapterLibrary
from backend.writing_vocabulary import VocabularyScope

ROOT = Path(__file__).resolve().parents[1]
RESOURCES = ROOT / '英语'


@pytest.fixture(scope='module')
def installed():
    if not (RESOURCES/'三年级资料清单.json').is_file():
        pytest.skip('Run python -m scripts.import_grade3_resources to install grade-three media')
    return ChapterLibrary(RESOURCES)


def test_audited_grade_three_snapshot_is_complete():
    pack = json.loads((ROOT/'backend/data/chat/curriculum-vocabulary.json').read_text())
    rows = [c for c in pack['chapters'] if c['grade']=='3年级']
    assert len(rows)==24 and len({c['id'] for c in rows})==24
    assert sum(len(c['vocabulary']) for c in rows)==287
    for term in ['上册','下册']:
        assert {c['curriculum']['unitNumber'] for c in rows if c['curriculum']['term']==term}==set(range(1,13))


def test_installed_units_keep_all_15_audited_original_images(installed):
    pages = json.loads((ROOT/'backend/data/curriculum/grade3-words.json').read_text())['pages']
    assert len(pages)==15
    for page in pages:
        path = RESOURCES/'科普版'/page['image']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==page['sha256']
    rows = [c for c in installed.chapters.values() if c['grade']=='3年级']
    assert len(rows)==24
    assert all(c['curriculum']['verified'] and c['curriculum']['sourcePages'] for c in rows)
    assert len(installed.vocabulary_pages)>=15


def test_every_installed_unit_has_real_audio_and_textbook_pages(installed):
    import soundfile
    rows = [c for c in installed.chapters.values() if c['grade']=='3年级']
    recordings = {r['id'] for c in rows for r in c['recordings']}
    assert len(recordings)==207
    assert all(c.get('textbookPages') for c in rows)
    for audio_id in recordings:
        assert soundfile.info(installed.audio[audio_id]).duration>0
    for chapter in rows:
        for page in chapter['textbookPages']:
            assert page['recordingId'] in recordings
            assert installed.textbook_pages[page['id']].is_file()


def test_cumulative_scopes_do_not_admit_future_vocabulary(installed):
    scopes = [VocabularyScope(installed,c['id']) for c in installed.chapters.values() if c['grade']=='3年级']
    assert all(s.summary()['verified'] and s.textbook_ready for s in scopes)
    unit3 = next(s for s in scopes if s.chapter['curriculum']['term']=='上册' and s.chapter['curriculum']['unitNumber']==3)
    assert unit3.knows('yes') and not unit3.knows('mouth')
    revision = next(s for s in scopes if s.chapter['curriculum']['term']=='下册' and s.chapter['curriculum']['unitNumber']==12)
    assert revision.current_words==[] and len(revision.sources)==24
    assert revision.knows('silk') and not revision.knows('healthy')


def test_generated_word_audio_covers_grade_three_when_installed(installed):
    if not (RESOURCES/'本地示范音/kokoro-af-heart/generation.json').exists():
        pytest.skip('Generate the optional local Kokoro word pack first')
    import soundfile
    words=[w for c in installed.chapters.values() if c['grade']=='3年级' for w in c['vocabulary']]
    assert len(words)==287
    for word in words:
        sample=word['sampleAudio']
        audio_id=sample['url'].rsplit('/',1)[-1]
        info=soundfile.info(installed.audio[audio_id])
        assert sample['kind']=='synthetic' and info.samplerate==24000 and .2<info.duration<15
