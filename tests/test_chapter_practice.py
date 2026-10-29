import asyncio
import hashlib
import json
from pathlib import Path

from aiohttp import ClientSession, web
import pytest

from backend.chapter_practice import ChapterLibrary, register_practice


ROOT = Path(__file__).resolve().parents[1] / "英语"


def test_real_chapters_keep_publisher_grade_and_edition_separate():
    lib = ChapterLibrary(ROOT)
    chapters = lib.catalog()["chapters"]
    assert len(chapters) == 202
    assert len({item["id"] for item in chapters}) == 202
    assert sum(c["publisher"] == "人教版" for c in chapters) == 84
    assert sum(c["publisher"] == "科普版" for c in chapters) == 118
    for chapter in lib.chapters.values():
        assert chapter["edition"] and chapter["unit"]
        assert chapter["textKind"].startswith("原创") or chapter.get('curriculum')
        if chapter["publisher"] == "科普版":
            assert chapter["recordings"] or chapter.get('curriculum')
            assert all(a["kind"] == "passage" and a["availability"] == "local" for a in chapter["recordings"])
            assert chapter["sourcePage"].startswith(("https://www.xdstudy.com/", "https://keben.app/"))
        else:
            assert any(a["kind"] == "words" for a in chapter["recordings"])
            assert any(a["kind"] == "passage" for a in chapter["recordings"])
            assert all(a["availability"] == "local" for a in chapter["recordings"])
    assert "local_audio" not in json.dumps(chapters)


def make_source(root, local_audio):
    publisher = root / "人教版"
    publisher.mkdir(exist_ok=True)
    row = {"grade": "7年级", "edition": "上册_新版", "unit": "Unit 1", "source_page": "https://www.pep.com.cn/test",
           "vocabulary": [], "question": "What?", "model": "Example", "tasks": [], "kind": "原创",
           "official_vocabulary_audio": [], "official_audio": [{"activity": "Read", "category": "课文与听力",
               "audio_url": "https://www.pep.com.cn/test.mp3", "local_audio": local_audio}]}
    (publisher / "新版单元听说训练.json").write_text(json.dumps([row]))


def test_missing_resources_and_external_paths_are_not_exposed(tmp_path):
    assert ChapterLibrary(tmp_path).catalog()["missingPublishers"] == ["人教版", "科普版"]
    secret = tmp_path / "outside.mp3"
    secret.write_bytes(b"not a teaching resource")
    make_source(tmp_path, "../outside.mp3")
    lib = ChapterLibrary(tmp_path)
    assert not lib.audio
    item = next(iter(lib.chapters.values()))["recordings"][0]
    assert item["availability"] == "official_online"
    assert item["url"] == "https://www.pep.com.cn/test.mp3"
    (tmp_path / "人教版" / "link.mp3").symlink_to(secret)
    make_source(tmp_path, "link.mp3")
    assert not ChapterLibrary(tmp_path).audio


def test_api_serves_only_indexed_audio_and_supports_segment_ranges(tmp_path, monkeypatch):
    make_source(tmp_path, "recording.mp3")
    payload = b"ID3" + bytes(range(100))
    (tmp_path / "人教版" / "recording.mp3").write_bytes(payload)
    monkeypatch.setenv("ENGLISH_RESOURCE_ROOT", str(tmp_path))

    async def run():
        app = web.Application()
        register_practice(app)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                async with client.get(url + "/api/practice/catalog") as response:
                    assert response.status == 200
                    chapter_id = (await response.json())["chapters"][0]["id"]
                async with client.get(url + "/api/practice/chapters/" + chapter_id) as response:
                    detail = await response.json()
                    audio_url = detail["recordings"][0]["url"]
                    assert str(tmp_path) not in json.dumps(detail)
                async with client.get(url + audio_url, headers={"Range": "bytes=3-12"}) as response:
                    assert response.status == 206
                    assert response.headers["Content-Type"] == "audio/mpeg"
                    assert await response.read() == payload[3:13]
                for path in ["/api/practice/audio/not-indexed", "/api/practice/chapters/unknown"]:
                    async with client.get(url + path) as response:
                        assert response.status == 404
                # A file replaced by an escaping symlink after catalog load is rejected too.
                outside = tmp_path.parent / (tmp_path.name + "-outside.mp3")
                outside.write_bytes(b"outside")
                try:
                    indexed = tmp_path / "人教版" / "recording.mp3"
                    indexed.unlink()
                    indexed.symlink_to(outside)
                    async with client.get(url + audio_url) as response:
                        assert response.status == 404
                finally:
                    outside.unlink()
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_collected_audio_uses_exact_course_and_rejects_escaping_paths(tmp_path):
    make_source(tmp_path, "missing.mp3")
    publisher = tmp_path / "人教版"
    (publisher / "collected.mp3").write_bytes(b"ID3-collected")
    (tmp_path / "outside.mp3").write_bytes(b"private")
    (publisher / "escape.mp3").symlink_to(tmp_path / "outside.mp3")
    assets = [{"kind":"分段音频", "order":1, "path":path}
              for path in ("collected.mp3", "collected.mp3", "../outside.mp3", "escape.mp3")]
    rows = [{"grade":"7年级", "edition":"different", "unit":"Unit 1", "assets":[
                {"kind":"整课音频", "path":"collected.mp3"}]},
            {"grade":"7年级", "edition":"上册_新版", "unit":"Unit 1", "assets":assets}]
    index = publisher / "本地课文音频总索引.json"
    index.write_text(json.dumps(rows))
    library = ChapterLibrary(tmp_path)
    assert list(library.audio.values()) == [publisher / "collected.mp3"]
    local = [r for r in next(iter(library.chapters.values()))["recordings"] if r["availability"] == "local"]
    assert len(local) == 1 and local[0]["url"].startswith("/api/practice/audio/")
    index.write_text(json.dumps(rows[:1]))
    assert not ChapterLibrary(tmp_path).audio


def test_textbook_pages_pair_only_with_same_chapter_audio_and_serve_indexed_images(tmp_path, monkeypatch):
    make_source(tmp_path, 'recording.mp3')
    publisher = tmp_path / '人教版'
    (publisher / 'recording.mp3').write_bytes(b'ID3-audio')
    image_bytes = b'\xff\xd8\xff-textbook-page'
    (publisher / 'page.jpg').write_bytes(image_bytes)
    outside = tmp_path / 'outside.jpg'
    outside.write_bytes(b'private')
    (publisher / 'escape.jpg').symlink_to(outside)
    assets = [{'kind': '教材原图', 'order': i, 'path': path, 'paired_audio': 'recording.mp3'}
              for i, path in enumerate(['page.jpg', 'page.jpg', '../outside.jpg', 'escape.jpg', 'recording.mp3'], 1)]
    index = [{'grade': '7年级', 'edition': 'different', 'unit': 'Unit 1', 'assets': assets},
             {'grade': '7年级', 'edition': '上册_新版', 'unit': 'Unit 1', 'assets': assets}]
    (publisher / '本地课文音频总索引.json').write_text(json.dumps(index))
    lib = ChapterLibrary(tmp_path)
    chapter = next(iter(lib.chapters.values()))
    assert len(chapter['textbookPages']) == 1
    page = chapter['textbookPages'][0]
    assert page['recordingId'] == chapter['recordings'][0]['id']
    assert str(tmp_path) not in json.dumps(chapter)
    monkeypatch.setenv('ENGLISH_RESOURCE_ROOT', str(tmp_path))

    async def run():
        app = web.Application()
        register_practice(app)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        try:
            async with ClientSession() as client:
                async with client.get(url + page['url']) as response:
                    assert response.status == 200
                    assert response.headers['Content-Type'].startswith('image/jpeg')
                    assert await response.read() == image_bytes
                async with client.get(url + '/api/practice/textbook-pages/unknown') as response:
                    assert response.status == 404
                (publisher / 'page.jpg').unlink()
                (publisher / 'page.jpg').symlink_to(outside)
                async with client.get(url + page['url']) as response:
                    assert response.status == 404
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_real_unit_textbook_pages_keep_exact_audio_pairing():
    lib = ChapterLibrary(ROOT)
    chapter = next(c for c in lib.chapters.values() if c['unit'] == 'Unit 1 Healthy eating and drinking'
                   and c['grade'] == '4年级' and c['edition'].startswith('上册'))
    assert chapter['textbookPages']
    recordings = {r['id'] for r in chapter['recordings']}
    assert all(page['recordingId'] in recordings for page in chapter['textbookPages'])
    assert all(page['id'] in lib.textbook_pages for page in chapter['textbookPages'])


def test_recording_picker_names_match_verified_sections_and_paired_pages():
    lib = ChapterLibrary(ROOT)
    chapter = lib.chapters['0500d50509c2f1f6668a11d6']
    recordings = {r['id']: r for r in chapter['recordings']}
    titles = [r['title'] for r in chapter['recordings']]
    assert titles[0] == '整课录音'
    assert len(titles) == 11 and len(set(titles)) == 11
    assert all(not title.startswith('教材分段') for title in titles)
    assert 'Let’s Read · You Eat Like a Horse!' in titles
    assert 'Let’s Learn A · 1. Listen, look and say' in titles
    assert 'Let’s Learn B · 1. Listen, look and say' in titles
    for page in chapter['textbookPages']:
        assert page['label'] == recordings[page['recordingId']]['title']


def test_breakfast_scene_has_four_separate_bounded_original_audio_sentences():
    lib = ChapterLibrary(ROOT)
    chapter = lib.chapters['0500d50509c2f1f6668a11d6']
    page = next(p for p in chapter['textbookPages'] if p.get('scenes'))
    assert page['imageSize'] == [718, 672]
    lines = [line for scene in page['scenes'] for line in scene['lines']]
    assert [line['text'] for line in lines] == [
        'Good morning.', 'Good morning, Mum.', 'Have some bread and juice.', 'Thank you, Mum!']
    assert all(0 <= line['start'] < line['end'] < 18.103 for line in lines)
    assert all(a['end'] <= b['start'] for a, b in zip(lines, lines[1:]))
    assert page['recordingId'] in lib.audio


def test_scene_assets_must_match_and_invalid_sentence_ranges_are_rejected(tmp_path):
    make_source(tmp_path, 'recording.mp3')
    publisher = tmp_path / '人教版'
    (publisher / 'recording.mp3').write_bytes(b'ID3-original')
    (publisher / 'page.jpg').write_bytes(b'original-image')
    index = [{'grade': '7年级', 'edition': '上册_新版', 'unit': 'Unit 1', 'assets': [
        {'kind': '教材原图', 'order': 1, 'path': 'page.jpg', 'paired_audio': 'recording.mp3'}]}]
    (publisher / '本地课文音频总索引.json').write_text(json.dumps(index))
    scene_source = tmp_path / 'scenes.json'
    lib = ChapterLibrary(tmp_path, scene_source=scene_source)
    chapter = next(iter(lib.chapters.values()))
    page = chapter['textbookPages'][0]
    entry = {'chapterId': chapter['id'], 'pageId': page['id'],
             'imageSha256': hashlib.sha256(b'original-image').hexdigest(),
             'audioSha256': hashlib.sha256(b'ID3-original').hexdigest(), 'imageSize': [718, 672],
             'scenes': [{'title': 'Dialogue', 'crop': [68, 128, 585, 260], 'lines': [
                 {'text': 'Good morning.', 'start': 1, 'end': 2, 'bubble': [1, 3, 40]}]}]}
    def save():
        scene_source.write_text(json.dumps({'version': 1, 'pages': [entry]}))
    save()
    assert next(iter(ChapterLibrary(tmp_path, scene_source=scene_source).chapters.values()))['textbookPages'][0]['scenes']
    entry['scenes'][0]['lines'][0]['end'] = 0
    save()
    with pytest.raises(ValueError, match='sentence time range'):
        ChapterLibrary(tmp_path, scene_source=scene_source)
    # A changed recording must never inherit old timestamps.
    entry['scenes'][0]['lines'][0]['end'] = 2
    save()
    (publisher / 'recording.mp3').write_bytes(b'ID3-replacement')
    changed = next(iter(ChapterLibrary(tmp_path, scene_source=scene_source).chapters.values()))
    assert 'scenes' not in changed['textbookPages'][0]


def test_grade_three_has_both_terms_local_media_pages_and_honest_wordlist_status():
    from backend.writing_vocabulary import VocabularyScope
    lib = ChapterLibrary(ROOT)
    rows = [c for c in lib.chapters.values() if c['grade'] == '3年级']
    assert len(rows) == 36
    for publisher, count in [('人教版', 6), ('科普版', 12)]:
        for term in ('上册', '下册'):
            chapters = [c for c in rows if c['publisher'] == publisher and c['edition'].startswith(term)]
            assert len(chapters) == count
            for c in chapters:
                assert c['question'] and c['example'] and c['tasks']
                assert c['textbookPages']
                assert all(r['availability'] == 'local' for r in c['recordings'])
                assert any(r.get('practiceClips') for r in c['recordings'] if r['kind'] == 'passage')
                scope = VocabularyScope(lib, c['id'])
                assert scope.textbook_ready == (publisher == '科普版')
                assert scope.sources[-1]['chapterId'] == c['id']
                assert all(s['grade'] == '3年级' for s in scope.sources)


def test_word_samples_cover_every_visible_word_and_keep_synthetic_source_explicit():
    lib = ChapterLibrary(ROOT)
    chapter = lib.chapters['74e28cc36583e470ddbf9b48']
    assert [word['word'] for word in chapter['vocabulary']] == ['sing','swim','climb','jump','dance','draw','do','lawn','paw']
    for item in lib.chapters.values():
        for word in item['vocabulary']:
            sample = word['sampleAudio']
            assert sample['url'].removeprefix('/api/practice/audio/') in lib.audio
            assert sample['kind'] in {'recorded', 'synthetic'}
            assert 0 < sample['duration'] < 30
    assert chapter['vocabulary'][-1]['sampleAudio']['kind'] == 'synthetic'


def test_i_can_has_named_sections_and_bounded_excerpts_not_a_six_minute_follow_target():
    lib = ChapterLibrary(ROOT)
    chapter = lib.chapters['74e28cc36583e470ddbf9b48']
    full = chapter['recordings'][0]
    assert full['fullLesson'] and full['duration'] > 400
    assert not full['practiceClips']
    assert any('你会游泳吗' in recording['title'] for recording in chapter['recordings'])
    assert all(not recording['title'].startswith('教材分段') for recording in chapter['recordings'])
    for recording in chapter['recordings'][1:]:
        assert all(0 <= clip['start'] < clip['end'] <= recording['duration'] + .05
                   and clip['end'] - clip['start'] <= 18.05 for clip in recording['practiceClips'])
