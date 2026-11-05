"""Chapter practice over validated local audio; no runtime TTS or model call."""
import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def _audio_file(path):
    if not path or not path.is_file():return False
    with path.open('rb') as stream:
        header=stream.read(32)
    return not (header.startswith((b'\xff\xd8\xff',b'\x89PNG',b'GIF8',b'%PDF'))
                or header.lstrip().lower().startswith((b'<html',b'<!doctype')))


def _official_url(value):
    parsed = urlsplit(value or "")
    return value if parsed.scheme == "https" and parsed.hostname in {
        "www.pep.com.cn", "www.xdstudy.com",
    } else None


class ChapterLibrary:
    def __init__(self, root, *, scene_source=None):
        self.root = Path(root).resolve()
        self.chapters = {}
        self.audio = {}
        self.subtitles = {}
        self.textbook_pages = {}
        self.vocabulary_pages = {}
        self.curriculum_books = []
        self.missing = []
        title_source = Path(__file__).parent / 'data/practice/recording-titles.json'
        self.recording_titles = (json.loads(title_source.read_text(encoding='utf-8'))['titles']
                                 if title_source.is_file() else {})
        for publisher, filename in [("人教版", "新版单元听说训练.json"),
                                    ("科普版", "单元原创补充训练.json"),
                                    ("人教版", "三年级单元训练.json"),
                                    ("科普版", "三年级单元训练.json")]:
            source = self.root / publisher / filename
            if not source.is_file():
                if filename != "三年级单元训练.json":
                    self.missing.append(publisher)
                continue
            for row in json.loads(source.read_text(encoding="utf-8")):
                chapter_id = _id(publisher, row["grade"], row["edition"], row["unit"])
                chapter = {"id": chapter_id, "publisher": publisher, "grade": row["grade"],
                           "edition": row["edition"], "unit": row["unit"],
                           "sourcePage": _official_url(row["source_page"]),
                           "vocabulary": row["vocabulary"], "question": row["question"],
                           "example": row["model"], "tasks": row["tasks"],
                           "textKind": row["kind"], "recordings": []}
                for kind, key in [("words", "official_vocabulary_audio"),
                                  ("passage", "official_audio")]:
                    for item in row[key]:
                        audio_id = _id(chapter_id, kind, item["audio_url"])
                        path = (self.root / publisher / item["local_audio"]).resolve() if item.get("local_audio") else None
                        # Never expose arbitrary files, symlinks outside the library, or source paths.
                        local = bool(path and path.is_relative_to(self.root / publisher)
                                     and path.suffix.lower() == ".mp3" and _audio_file(path))
                        if local:
                            self.audio[audio_id] = path
                        url = _official_url(item.get("audio_url"))
                        chapter["recordings"].append({"id": audio_id, "kind": kind,
                            "title": item["activity"], "category": item["category"],
                            "sourcePage": _official_url(item.get("source_page")),
                            "checkedOn": item.get("checked_on"),
                            "url": f"/api/practice/audio/{audio_id}" if local else url,
                            "availability": "local" if local else "official_online" if url else "unavailable"})
                for page in row.get('textbook_pages', []):
                    path = (self.root / publisher / page['path']).resolve()
                    if (not path.is_relative_to(self.root / publisher) or not path.is_file()
                            or path.suffix.lower() not in {'.jpg', '.jpeg', '.png'}):
                        continue
                    page_id = _id(chapter_id, 'textbook-page', page['path'])
                    self.textbook_pages[page_id] = path
                    chapter.setdefault('textbookPages', []).append({
                        'id': page_id, 'url': '/api/practice/textbook-pages/' + page_id,
                        'label': page['label'], 'recordingId': None})
                self.chapters[chapter_id] = chapter

        self._add_curriculum()

        # The collected textbook media index is separate from original topic exercises.
        for publisher in ("人教版", "科普版"):
            self._add_local_recordings(publisher)
        from .lesson_audio import add_local_lessons
        add_local_lessons(self)
        self._add_sentence_scenes(Path(scene_source) if scene_source is not None else
                                 Path(__file__).parent / 'data/practice/dialogue-scenes.json')
        self._add_shadow_assets()

    def _add_shadow_assets(self):
        data = Path(__file__).parent / 'data/practice'
        word_pack = data / 'word-audio.json'
        excerpt_pack = data / 'audio-excerpts.json'
        words = json.loads(word_pack.read_text())['words'] if word_pack.is_file() else {}
        excerpts = json.loads(excerpt_pack.read_text())['recordings'] if excerpt_pack.is_file() else {}
        valid_words = {}
        for text, entry in words.items():
            path = (self.root / entry['path']).resolve()
            if (not path.is_relative_to(self.root) or path.suffix.lower() not in {'.mp3', '.wav'}
                    or not _audio_file(path) or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']):
                continue
            audio_id = _id('word-pronunciation', text, entry['sha256'])
            self.audio[audio_id] = path
            valid_words[text] = {'url': '/api/practice/audio/' + audio_id, 'kind': entry['kind'],
                                 'duration': entry['duration']}
        for chapter in self.chapters.values():
            for word in chapter['vocabulary']:
                sample = valid_words.get(' '.join(word['word'].casefold().split()))
                if sample:
                    word['sampleAudio'] = sample
            for recording in chapter['recordings']:
                path = self.audio.get(recording['id'])
                entry = excerpts.get(hashlib.sha256(path.read_bytes()).hexdigest()) if path else None
                if entry is None:
                    continue
                duration = entry['duration']
                clips = entry['clips']
                if (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0
                        or any(not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (c['start'], c['end']))
                               or c['start'] < 0 or c['end'] <= c['start'] or c['end'] > duration + .05
                               or c['end'] - c['start'] > 18.05 for c in clips)):
                    raise ValueError('Invalid short practice excerpts')
                recording['duration'] = duration
                recording['fullLesson'] = bool(entry['fullLesson'])
                recording['practiceClips'] = clips
                title = recording['title']
                recording['practiceTitle'] = title if not title.startswith('教材分段') else '看图听读 · 第' + title.removeprefix('教材分段').strip() + '部分'

    def _add_sentence_scenes(self, source):
        if not source.is_file():
            return
        pack = json.loads(source.read_text(encoding='utf-8'))
        if pack.get('version') != 1:
            raise ValueError('Unsupported dialogue scene pack')
        for entry in pack['pages']:
            chapter = self.chapters.get(entry['chapterId'])
            page = next((p for p in chapter.get('textbookPages', []) if p['id'] == entry['pageId']), None) if chapter else None
            if page is None or page.get('recordingId') not in self.audio:
                continue
            image = self.textbook_pages[page['id']]
            audio = self.audio[page['recordingId']]
            # Time ranges and scene crops apply only to these exact source assets.
            if (hashlib.sha256(image.read_bytes()).hexdigest() != entry['imageSha256']
                    or hashlib.sha256(audio.read_bytes()).hexdigest() != entry['audioSha256']):
                continue
            width, height = entry['imageSize']
            if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (width, height)):
                raise ValueError('Invalid scene image size')
            for scene in entry['scenes']:
                x, y, w, h = scene['crop']
                if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y, w, h))
                        or min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height):
                    raise ValueError('Invalid scene crop')
                for line in scene['lines']:
                    start, end = line['start'], line['end']
                    if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (start, end))
                            or start < 0 or end <= start or not isinstance(line['text'], str) or not line['text'].strip()):
                        raise ValueError('Invalid sentence time range')
                    left, top, size = line['bubble']
                    if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (left, top, size))
                            or min(left, top) < 0 or size <= 0 or left + size > 100 or top >= 100):
                        raise ValueError('Invalid sentence placement')
                    minimum_height = line.get('minHeight', 0)
                    if (not isinstance(minimum_height, (int, float)) or not math.isfinite(minimum_height)
                            or minimum_height < 0 or top + minimum_height > 100):
                        raise ValueError('Invalid sentence bubble height')
            page['imageSize'] = [width, height]
            page['scenes'] = entry['scenes']
            background = entry.get('backgroundUrl')
            if isinstance(background, str) and background.startswith('/illustrations/') and '..' not in background:
                page['backgroundUrl'] = background

    def _add_curriculum(self):
        source = self.root / '科普版/结构化教材词表.json'
        if not source.is_file():
            return
        pack = json.loads(source.read_text(encoding='utf-8'))
        if pack.get('version') != 2:
            raise ValueError('Unsupported curriculum pack')
        self.curriculum_books = pack['books']
        for row in pack['chapters']:
            chapter = self.chapters.setdefault(row['id'], {**row, 'recordings': []})
            chapter['vocabulary'] = row['vocabulary']
            chapter['curriculum'] = {**row['curriculum']}
            chapter['vocabularyKind'] = '教材原图识别词表'
            pages = []
            for page in row['curriculum']['sourcePages']:
                path = (self.root / '科普版' / page['image']).resolve()
                if not path.is_relative_to(self.root / '科普版') or not path.is_file() or path.suffix.lower() not in {'.jpg','.png','.jpeg'}:
                    raise ValueError('Missing indexed vocabulary image')
                page_id = page['sha256'][:24]
                self.vocabulary_pages[page_id] = path
                pages.append({'sha256': page['sha256'], 'url': '/api/practice/vocabulary-pages/' + page_id,
                              'source': page['url'], 'label': path.name})
            chapter['curriculum']['sourcePages'] = pages

    def _add_local_recordings(self, publisher):
        source = self.root / publisher / "本地课文音频总索引.json"
        if not source.is_file():
            return
        chapters = {(c["grade"], c["edition"], c["unit"]): c
                    for c in self.chapters.values() if c["publisher"] == publisher}
        for row in json.loads(source.read_text(encoding="utf-8")):
            chapter = chapters.get((row.get("grade"), row.get("edition"), row.get("unit")))
            if chapter is None:
                continue
            # Titles are checked against the paired textbook page, not inferred from segment numbers.
            named_audio = {}
            for asset in row.get('assets', []):
                title = self.recording_titles.get(asset.get('sha256'))
                if (asset.get('kind') != '教材原图' or not title
                        or not isinstance(asset.get('path'), str) or not isinstance(asset.get('paired_audio'), str)):
                    continue
                image = (self.root / publisher / asset['path']).resolve()
                if (image.is_relative_to(self.root / publisher) and image.is_file()
                        and hashlib.sha256(image.read_bytes()).hexdigest() == asset['sha256']):
                    named_audio[(self.root / publisher / asset['paired_audio']).resolve()] = title
            existing = {self.audio[item["id"]] for item in chapter["recordings"] if item["id"] in self.audio}
            for asset in row.get("assets", []):
                if asset.get("kind") not in {"整课音频", "分段音频"} or not isinstance(asset.get("path"), str):
                    continue
                path = (self.root / publisher / asset["path"]).resolve()
                if (not path.is_relative_to(self.root / publisher) or path.suffix.lower() != ".mp3"
                        or not _audio_file(path) or path in existing):
                    continue
                audio_id = _id(chapter["id"], "passage", "local-index", asset["path"])
                self.audio[audio_id] = path
                existing.add(path)
                title = "整课录音" if asset["kind"] == "整课音频" else named_audio.get(path, f"教材分段 {asset.get('order', '')}")
                chapter["recordings"].append({"id": audio_id, "kind": "passage", "title": title,
                    "category": "课文与听力", "sourcePage": _official_url(row.get("source_page")),
                    "checkedOn": row.get("checked_on"), "url": f"/api/practice/audio/{audio_id}",
                    "availability": "local"})
            audio_ids = {self.audio[item['id']]: item['id'] for item in chapter['recordings']
                         if item['kind'] == 'passage' and item['id'] in self.audio}
            pages = chapter.setdefault('textbookPages', [])
            for asset in row.get('assets', []):
                if asset.get('kind') != '教材原图' or not isinstance(asset.get('path'), str):
                    continue
                path = (self.root / publisher / asset['path']).resolve()
                if (not path.is_relative_to(self.root / publisher) or not path.is_file()
                        or path.suffix.lower() not in {'.jpg', '.jpeg', '.png'}):
                    continue
                page_id = _id(chapter['id'], 'textbook-page', asset['path'])
                if any(page['id'] == page_id for page in pages):
                    continue
                paired = asset.get('paired_audio')
                recording_id = audio_ids.get((self.root / publisher / paired).resolve()) if isinstance(paired, str) else None
                page_title = named_audio.get((self.root / publisher / paired).resolve()) if isinstance(paired, str) else None
                self.textbook_pages[page_id] = path
                pages.append({'id': page_id, 'url': '/api/practice/textbook-pages/' + page_id,
                              'label': page_title or f"教材原图 {asset.get('order', len(pages) + 1)}",
                              'recordingId': recording_id})

    def catalog(self):
        return {"version": 1, "missingPublishers": self.missing, "chapters": [
            {**{key: chapter[key] for key in ("id", "publisher", "grade", "edition", "unit")},
             "recordingCount": len(chapter["recordings"]),
             "playableRecordingCount": sum(bool(r.get('url')) for r in chapter['recordings']),
             "vocabularyCount": len(chapter['vocabulary'])} for chapter in self.chapters.values()]}


def register_practice(app):
    library = None
    revision = None

    def get_library():
        nonlocal library, revision
        root = Path(os.getenv("ENGLISH_RESOURCE_ROOT") or str(Path(__file__).resolve().parents[1] / "英语"))
        paths=[root/p for p in ['科普版/结构化教材词表.json','科普版/本地课文音频总索引.json',
            '科普版/单元原创补充训练.json','科普版/三年级单元训练.json',
            '人教版/新版单元听说训练.json','人教版/三年级单元训练.json','本地语音处理/lesson-audio.json']]
        paths += list((Path(__file__).parent/'data/practice').glob('*.json'))
        current=tuple((str(p),p.stat().st_mtime_ns,p.stat().st_ctime_ns,p.stat().st_size) for p in paths if p.exists())
        if library is None or current!=revision:
            library = ChapterLibrary(root)
            revision = current
        return library

    async def catalog(request):
        return web.json_response(get_library().catalog())

    async def chapter(request):
        item = get_library().chapters.get(request.match_info["chapter_id"])
        if item is None:
            raise web.HTTPNotFound()
        return web.json_response(item)

    async def audio(request):
        lib = get_library()
        path = lib.audio.get(request.match_info["audio_id"])
        if path is None or not path.is_file() or not path.resolve().is_relative_to(lib.root):
            raise web.HTTPNotFound()
        # FileResponse supplies Range support for repeatable audio segments.
        return web.FileResponse(path, headers={"Content-Type": "audio/wav" if path.suffix.lower() == ".wav" else "audio/mpeg"})

    async def vocabulary_page(request):
        lib = get_library()
        path = lib.vocabulary_pages.get(request.match_info['page_id'])
        if path is None or not path.is_file() or not path.resolve().is_relative_to(lib.root):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def subtitles(request):
        lib=get_library();path=lib.subtitles.get(request.match_info['audio_id'])
        if path is None or not path.is_file() or not path.resolve().is_relative_to(lib.root):
            raise web.HTTPNotFound()
        return web.FileResponse(path,headers={'Content-Type':'text/vtt; charset=utf-8','X-Content-Type-Options':'nosniff'})

    async def textbook_page(request):
        lib = get_library()
        path = lib.textbook_pages.get(request.match_info['page_id'])
        if path is None or not path.is_file() or path.resolve() != path or not path.is_relative_to(lib.root):
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    app.router.add_get("/api/practice/catalog", catalog)
    app.router.add_get("/api/practice/chapters/{chapter_id}", chapter)
    app.router.add_get("/api/practice/audio/{audio_id}", audio)
    app.router.add_get('/api/practice/subtitles/{audio_id}', subtitles)
    app.router.add_get('/api/practice/vocabulary-pages/{page_id}', vocabulary_page)
    app.router.add_get('/api/practice/textbook-pages/{page_id}', textbook_page)
