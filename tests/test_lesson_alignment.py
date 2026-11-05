import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.lesson_alignment import align_references, tokens
from backend.lesson_audio import add_local_lessons


def transcript(text, start=1):
    return {'segments':[{'words':[{'word':w,'start':start+i*.3,'end':start+i*.3+.25,'probability':.98}
                                  for i,w in enumerate(text.split())]}]}


def test_alignment_uses_recognized_positions_instead_of_unrelated_source_timecodes():
    reference={'text':'I am in Class 6, Grade 7.','audio_start':700000,'audio_end':800000}
    result=align_references([reference],transcript("I'm in Class Six Grade Seven",start=4),15)
    assert len(result)==1
    assert result[0]['start']==3.975 and result[0]['end']<7
    assert result[0]['text']==reference['text']
    assert tokens("I'm") == ['i','am']


def test_unspoken_text_low_confidence_and_single_word_noise_never_gain_timestamps():
    refs=[{'text':'This is a giraffe.'},{'text':'Hello'},{'text':'I am happy.'}]
    actual=transcript('I am happy')
    actual['segments'][0]['words'][0]['probability']=.01
    assert align_references(refs,actual,10)==[]


def test_nested_references_do_not_produce_overlapping_playback_ranges():
    refs=[{'text':'I like my cat.'},{'text':'my cat.'},{'text':'I like my cat.'}]
    result=align_references(refs,transcript('I like my cat'),5)
    assert len(result)==1 and result[0]['text']=='I like my cat.'


def test_activity_instructions_are_not_follow_reading_targets():
    assert align_references([{'text':'Listen and repeat.'}],transcript('Listen and repeat'),5)==[]


def test_stale_audio_or_wrong_editions_cannot_be_attached(tmp_path):
    import hashlib
    root=tmp_path/'resources';root.mkdir()
    audio=root/'sample.wav';audio.write_bytes(b'local fixture')
    sha=hashlib.sha256(audio.read_bytes()).hexdigest()
    chapter={'id':'unit','publisher':'科普版','grade':'8年级','edition':'新版','unit':'Unit 1','recordings':[]}
    library=SimpleNamespace(root=root,chapters={'unit':chapter},audio={},textbook_pages={})
    row={'chapterId':'unit','path':'sample.wav','sha256':'0'*64,'duration':2,
         'audioKind':'synthetic','sourceLabel':'本地 AI 合成示范音','title':'示范句',
         'sentences':[{'text':'My cat.','start':0,'end':1.8}]}
    target=root/'本地语音处理/lesson-audio.json';target.parent.mkdir()
    def pack():target.write_text(json.dumps({'version':1,'chapterAdditions':[],'recordings':[row]}))
    pack();add_local_lessons(library)
    assert chapter['recordings']==[]
    row.update(sha256=sha,chapterKey={'edition':'旧版'});pack()
    with pytest.raises(ValueError,match='edition'):add_local_lessons(library)
    row['chapterKey']={'edition':'新版'};pack();add_local_lessons(library)
    assert chapter['recordings'][0]['audioKind']=='synthetic'
    assert chapter['recordings'][0]['alignedSentences'][0]['text']=='My cat.'


def test_image_bytes_cannot_be_served_as_audio(tmp_path):
    from backend.chapter_practice import _audio_file
    path=tmp_path/'pretend.mp3';path.write_bytes(b'\xff\xd8\xff\xe1image')
    assert not _audio_file(path)


def test_resource_pack_refreshes_catalog_without_restarting_service(tmp_path,monkeypatch):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from backend import chapter_practice
    monkeypatch.setenv('ENGLISH_RESOURCE_ROOT',str(tmp_path))
    builds=[]
    class Library:
        def __init__(self,root):builds.append(root)
        def catalog(self):return {'build':len(builds)}
    monkeypatch.setattr(chapter_practice,'ChapterLibrary',Library)
    async def run():
        app=web.Application();chapter_practice.register_practice(app)
        async with TestClient(TestServer(app)) as client:
            assert (await (await client.get('/api/practice/catalog')).json())['build']==1
            assert (await (await client.get('/api/practice/catalog')).json())['build']==1
            target=tmp_path/'本地语音处理/lesson-audio.json';target.parent.mkdir();target.write_text('{}')
            assert (await (await client.get('/api/practice/catalog')).json())['build']==2
    asyncio.run(run())


def test_final_publish_refuses_an_incomplete_transcription_batch(monkeypatch):
    from scripts import prepare_local_lessons
    monkeypatch.setattr(prepare_local_lessons,'load',lambda name: {'pendingTranscripts':['unfinished']})
    with pytest.raises(ValueError,match='Finish all'):prepare_local_lessons.publish()


def test_supplemental_excerpts_are_bound_to_original_pages_when_installed():
    import hashlib
    root=Path(__file__).resolve().parents[1]
    pack=json.loads((root/'backend/data/practice/supplemental-sentences.json').read_text())
    assert len(pack['pages'])==12 and sum(len(p['sentences']) for p in pack['pages'])==62
    for page in pack['pages']:
        path=root/'英语/科普版'/page['source']
        if not path.exists():pytest.skip('Original textbook images are installed separately')
        assert hashlib.sha256(path.read_bytes()).hexdigest()==page['evidenceSha256']
        assert page['method']=='visually-transcribed-from-local-textbook-page'
