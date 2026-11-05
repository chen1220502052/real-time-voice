"""Restore the audited grade-three pack and publisher audio/images for local use.

Downloads only public publisher pages and their explicitly linked CDN assets.
No model calls. Wordlist originals must match the existing audit's SHA-256 values.
The publisher exposes these public resources over HTTP; TLS checks are never disabled.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_HOSTS = {'www.xdstudy.com', 'qiniu.xdstudy.com'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


class LessonPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.assets = []
        self.audio = None
        self.title = ''
        self.in_title = False
        self.in_button = False
        self.image_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and (self.image_depth or 'lesson-img' in attrs.get('class', '').split()):
            self.image_depth += 1
        if tag == 'h3' and 'title' in attrs.get('class', '').split():
            self.in_title = True
        if self.in_title and tag == 'button':
            self.in_button = True
        url = attrs.get('src', '')
        if tag == 'a' and 'download' in attrs and attrs.get('href', '').endswith('.mp3'):
            self.assets.append({'kind': '整课音频', 'url': attrs['href']})
        if tag == 'audio' and url:
            self.audio = url
            self.assets.append({'kind': '分段音频', 'url': url})
        if tag == 'img' and self.image_depth and urlsplit(url).hostname == 'qiniu.xdstudy.com':
            self.assets.append({'kind': '教材原图', 'url': url, 'paired_url': self.audio})

    def handle_endtag(self, tag):
        if tag == 'div' and self.image_depth:
            self.image_depth -= 1
        if tag == 'h3':
            self.in_title = False
        if tag == 'button':
            self.in_button = False

    def handle_data(self, data):
        if self.in_title and not self.in_button:
            self.title += data


def retrieve(url, cache):
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError('Resource is not on the publisher allowlist')
    suffix = Path(parsed.path).suffix or '.html'
    target = cache / (digest(url.encode())[:24] + suffix)
    if target.is_file():
        return target
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as response:
                if urlsplit(response.url).hostname not in ALLOWED_HOSTS:
                    raise ValueError('Resource redirected outside publisher hosts')
                data = response.read()
            if not data:
                raise ValueError('Empty publisher resource')
            temporary = target.with_suffix(target.suffix + '.tmp')
            temporary.write_bytes(data)
            temporary.replace(target)
            return target
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def build(output, cache, workers=4):
    output, cache = Path(output).resolve(), Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    source = json.loads((ROOT / 'backend/data/chat/curriculum-vocabulary.json').read_text())
    rows = [deepcopy(row) for row in source['chapters']
            if row['publisher'] == '科普版' and row['grade'] == '3年级']
    if len(rows) != 24:
        raise ValueError('Expected exactly 24 audited grade-three units')
    pages = json.loads((ROOT / 'backend/data/curriculum/grade3-words.json').read_text())['pages']
    expected = {page['sha256']: page['image'] for page in pages}
    books = [deepcopy(book) for book in source['books'] if book['grade'] == 3]
    if len(books) != 2 or any(len(book['chapterIds']) != 12 for book in books):
        raise ValueError('Expected two complete grade-three books')
    checked = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()

    def read_lesson(row):
        parsed = urlsplit(row['sourcePage'])
        page_url = urlunsplit(('http', parsed.netloc, parsed.path, '', ''))
        parser = LessonPage()
        parser.feed(retrieve(page_url, cache).read_text(encoding='utf-8'))
        number = row['curriculum']['unitNumber']
        if not re.match(rf'Unit\s+{number}\b', parser.title.strip(), re.I):
            raise ValueError(f'Publisher unit changed: {row["unit"]}')
        if not parser.assets or not any(a['kind'] == '整课音频' for a in parser.assets):
            raise ValueError(f'Missing publisher audio: {row["unit"]}')
        return row, page_url, parser

    with ThreadPoolExecutor(max_workers=workers) as pool:
        lessons = list(pool.map(read_lesson, rows))
    urls = sorted({asset['url'] for _, _, page in lessons for asset in page.assets})
    print(f'Publisher pages: {len(lessons)}; downloading {len(urls)} linked assets', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths = dict(zip(urls, pool.map(lambda url: retrieve(url, cache), urls)))

    publisher = output / '科普版'
    media = publisher / '3年级/资源'
    media.mkdir(parents=True, exist_ok=True)
    files, matched = {}, set()
    for url, cached in paths.items():
        data = cached.read_bytes()
        suffix = cached.suffix.lower()
        if suffix in {'.jpg', '.jpeg'} and not data.startswith(b'\xff\xd8'):
            raise ValueError('Invalid JPEG resource')
        if suffix == '.png' and not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('Invalid PNG resource')
        if suffix == '.mp3' and not (data.startswith(b'ID3') or data[0] == 255 and data[1] & 224 == 224):
            raise ValueError('Invalid MP3 resource')
        sha = digest(data)
        if sha in expected:
            target = publisher / expected[sha]
            matched.add(sha)
        else:
            target = media / (sha[:24] + suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or digest(target.read_bytes()) != sha:
            shutil.copyfile(cached, target)
        files[url] = {'path': target.relative_to(publisher).as_posix(), 'sha256': sha,
                      'bytes': len(data)}
    if matched != set(expected):
        missing = [path for sha, path in expected.items() if sha not in matched]
        raise ValueError('Audited wordlist originals changed or missing: ' + ', '.join(missing))

    training, recordings, provenance = [], [], []
    for row, page_url, page in lessons:
        training.append({'grade': row['grade'], 'edition': row['edition'], 'unit': row['unit'],
            'source_page': row['sourcePage'], 'kind': row['textKind'],
            'vocabulary': row['vocabulary'], 'question': row['question'], 'model': row['example'],
            'tasks': row['tasks'], 'official_audio': [], 'official_vocabulary_audio': []})
        assets = []
        counters = Counter()
        for asset in page.assets:
            counters[asset['kind']] += 1
            record = {**files[asset['url']], 'kind': asset['kind'], 'order': counters[asset['kind']]}
            if asset.get('paired_url'):
                record['paired_audio'] = files[asset['paired_url']]['path']
            assets.append(record)
        recordings.append({'grade': row['grade'], 'edition': row['edition'], 'unit': row['unit'],
            'source_page': row['sourcePage'], 'checked_on': checked, 'assets': assets})
        provenance.append({'chapterId': row['id'], 'unit': row['unit'],
            'term': row['curriculum']['term'], 'source': row['sourcePage'],
            'retrievedFrom': page_url, 'publisherTitle': ' '.join(page.title.split()),
            'wordCount': len(row['vocabulary']), 'assets': [dict(asset, **files[asset['url']]) for asset in page.assets]})
    pack = {'version': 2, 'foundation': [], 'books': list(books), 'chapters': list(rows),
            'audit': {'grade3SourcePages': len(pages), 'method': '恢复仓库已核对词表，并以 SHA-256 校验原图',
                      'schoolEditionConfirmed': False}}
    existing_pack = publisher / '结构化教材词表.json'
    if existing_pack.is_file():
        previous = json.loads(existing_pack.read_text(encoding='utf-8'))
        if previous.get('version') != 2:
            raise ValueError('Unsupported existing curriculum pack')
        pack['books'] += [book for book in previous['books'] if book['grade'] != 3]
        pack['chapters'] += [chapter for chapter in previous['chapters'] if chapter['grade'] != '3年级']
        pack['audit'] = {**previous.get('audit', {}), **pack['audit']}
    existing_media = publisher / '本地课文音频总索引.json'
    if existing_media.is_file():
        recordings += [entry for entry in json.loads(existing_media.read_text(encoding='utf-8'))
                       if entry['grade'] != '3年级']
    manifest = {'version': 1, 'checkedOn': checked, 'publisher': '科普版', 'grade': 3,
        'books': len(books), 'units': len(rows), 'vocabularyEntries': sum(len(c['vocabulary']) for c in rows),
        'verifiedWordlistImages': len(matched), 'uniqueAssets': len(files), 'bytes': sum(f['bytes'] for f in files.values()),
        'notice': '教材原音与原图来自出版社公开资源；任务及示范沿用已标注原创的练习；学校版次尚需核对。',
        'chapters': provenance}
    for name, value in [('三年级单元训练.json', training), ('本地课文音频总索引.json', recordings),
                        ('结构化教材词表.json', pack)]:
        (publisher / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / '三年级资料清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '英语')
    parser.add_argument('--cache', type=Path, default=ROOT / '.local/grade3-import-cache')
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    result = build(args.output, args.cache, args.workers)
    print(json.dumps({key: value for key, value in result.items() if key != 'chapters'}, ensure_ascii=False, indent=2))
