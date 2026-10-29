"""Build unit-isolated vocabulary packs from the audited page transcriptions.

No vocabulary is inferred from a chapter theme or from an adjacent unit. The two
explicit cross-page fixes below are recorded with their original image evidence.
"""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.chapter_practice import _id

DATA = ROOT / 'backend/data/curriculum'
RESOURCE = ROOT / '英语/科普版'


def number(value):
    match = re.match(r'(?:Unit|Lesson)\s*(\d+)\b', value, re.I)
    return int(match[1]) if match else None


def build():
    manifest = json.loads((DATA / 'image-manifest.json').read_text())
    pages = defaultdict(list)
    for row in manifest:
        result = json.loads((DATA / 'pages' / (row['sha256'][:24] + '.json')).read_text())
        assert result['checked'] and hashlib.sha256((ROOT / row['image']).read_bytes()).hexdigest() == row['sha256']
        pages[row['grade'], row['term'], row['book']].append(result)
    lesson_evidence = defaultdict(list)
    lesson_manifest = json.loads((DATA / 'lesson-evidence-manifest.json').read_text())
    for row in lesson_manifest:
        path = DATA / 'pages' / (row['sha256'][:24]+'.json')
        if not path.is_file():
            raise ValueError('Lesson evidence extraction not finished')
        result = json.loads(path.read_text())
        assert result['checked'] and hashlib.sha256((ROOT / row['image']).read_bytes()).hexdigest() == row['sha256']
        lesson_evidence[row['unit']].append(result)
    media = json.loads((RESOURCE / '本地课文音频总索引.json').read_text())
    exercises = json.loads((RESOURCE / '单元原创补充训练.json').read_text())
    primary = {(int(re.search(r'\d+', r['grade'])[0]), r['edition'][:2], number(r['unit'])): r
               for r in [*media, *exercises] if number(r['unit'])}
    junior = json.loads((RESOURCE / '初中完整教材原页索引.json').read_text())
    sync = json.loads((RESOURCE / '初中本地课文词表索引.json').read_text())
    names = {}
    for book in sync:
        g = int(re.search(r'\d+', book['grade'])[0])
        for row in book['chapters']:
            names[g, book['term'], number(row['unit'])] = row['unit']
    new8 = ['My Dream Job', 'The World of Art', 'Sound Body, Sound Mind',
            'Changing and Growing', 'Amazing Places', 'Earth and Beyond']
    chapters, books, fixes = [], [], []
    visual = json.loads((DATA / 'visual-corrections.json').read_text())
    overrides = {(fix['image'], word): fix['toUnit'] for fix in visual['moves'] for word in fix['words']}
    for (grade, term, book_name), book_pages in sorted(pages.items()):
        if grade <= 6:
            edition = term + '_官方当前目录_版次待与学校核对'
            series, first, last = '科普小学现有教材', 1, (12 if grade == 4 or term == '下册' and grade == 5 else 10)
        else:
            meta = next(b for b in junior if b['grade'] == f'{grade}年级' and b['term'] == term)
            edition = book_name
            series = '仁爱科普2024新课标' if grade == 7 or grade == 8 and term == '上册' else '仁爱科普旧版'
            first, last = {(7,'上册'):(1,6),(7,'下册'):(7,12),(8,'上册'):(1,6),
                           (8,'下册'):(5,8),(9,'上册'):(1,4),(9,'下册'):(5,6)}[grade,term]
        book_id = _id('科普版', grade, term, edition)
        by_unit, unit_pages = defaultdict(list), defaultdict(list)
        current = None
        for page in book_pages:
            full_entries = {e['word'].strip().lstrip('*').strip(): e for b in page['passes'][-1]['blocks'] for e in b['entries']}
            blocks = ([block for col in page['columnPasses'] for block in col['passes'][-1]['blocks']]
                      if page.get('columnChecked') else page['passes'][-1]['blocks'])
            for block in blocks:
                if block['unit'] is not None and first <= block['unit'] <= last:
                    current = block['unit']
                elif current is None and len(page['units']) == 1:
                    current = page['units'][0]
                if current is None or not first <= current <= last:
                    raise ValueError(f'Unresolved unit on {page["image"]}')
                for item in block['entries']:
                    raw = item['word'].strip().lstrip('*').strip()
                    if raw.lower() in {'n.','v.','adj.','adv.','prep.','conj.','pron.','det.'}:
                        fixes.append({'image':page['image'],'kind':'continued_part_of_speech','raw':raw})
                        continue
                    # These are Chinese meanings continued from the preceding image,
                    # not new headwords. Their text remains in raw page transcriptions.
                    if re.search(r'[\u4e00-\u9fff]', raw):
                        fixes.append({'image': page['image'], 'kind': 'cross_page_meaning', 'raw': raw})
                        continue
                    if not re.search(r'[A-Za-z]', raw):
                        raise ValueError(f'Invalid headword on {page["image"]}: {raw}')
                    entry = {**item, 'word': raw, 'sourceImage': page['image'], 'pageSha256': page['sha256']}
                    if raw == 'everyone' and grade == 7 and term == '上册':
                        entry.update(meaning='pron.每人', uncertain=False,
                                     verificationNote='释义续于PDF页137，已对照136–137页原图。')
                    if raw == 'mum' and grade == 7 and term == '下册':
                        entry.update(meaning='n.妈妈', uncertain=False,
                                     verificationNote='释义续于PDF页142，已对照141–142页原图。')
                    if entry['uncertain'] and raw in full_entries and not full_entries[raw]['uncertain']:
                        entry.update(meaning=full_entries[raw]['meaning'],uncertain=False,
                                     verificationNote='跨栏释义使用同一完整原页的复核记录；词与单元仍按分栏结果。')
                    if entry['uncertain']:
                        raise ValueError(f'Unresolved headword: {entry}')
                    assigned = overrides.get((page['image'], raw), current)
                    if assigned != current:
                        entry['verificationNote'] = f'右栏续行归属已按原图纠正：Lesson {current} → Lesson {assigned}'
                    by_unit[assigned].append(entry)
                    image = page['image'].removeprefix('英语/科普版/')
                    if image not in [p['image'] for p in unit_pages[assigned]]:
                        unit_pages[assigned].append({'image':image,'sha256':page['sha256'],'url':page['source']})
                image = page['image'].removeprefix('英语/科普版/')
                if image not in [p['image'] for p in unit_pages[current]]:
                    unit_pages[current].append({'image': image, 'sha256': page['sha256'], 'url': page['source']})
        ids = []
        for unit in range(first, last + 1):
            if not by_unit[unit] and not (grade == 5 and term == '上册' and unit == 10):
                raise ValueError(f'Missing source vocabulary: {grade} {term} {unit}')
            row = primary.get((grade, term, unit)) if grade <= 6 else None
            title = row['unit'] if row else (f'Unit {unit} ' + new8[unit-1] if grade == 8 and term == '上册' else names.get((grade,term,unit), f'Unit {unit}'))
            chapter_id = _id('科普版', f'{grade}年级', edition, title)
            entries = list({e['word']: e for e in by_unit[unit]}.values())
            revision = grade == 5 and term == '上册' and unit == 10
            source_pages = unit_pages[unit]
            observed_words, evidence_records = set(), []
            if grade == 4 and term == '上册':
                for evidence in lesson_evidence[unit]:
                    extracted = evidence['passes'][-1]
                    sentences = extracted.get('evidenceSentences',[])
                    visible = set(re.findall(r"[a-z]+(?:['’][a-z]+)?", ' '.join(sentences).lower()))
                    for block in extracted['blocks']:
                        if block['unit'] != unit:
                            raise ValueError('Lesson evidence unit mismatch')
                        for entry in block['entries']:
                            word = entry['word'].strip()
                            if not entry['uncertain'] and word.lower() in visible:
                                observed_words.add(word)
                    image = evidence['image'].removeprefix('英语/科普版/')
                    source_pages.append({'image':image,'sha256':evidence['sha256'],'url':evidence['source']})
                    evidence_records.append({'image':image,'sentences':sentences,'sha256':evidence['sha256']})
            if revision:
                source_pages = [{'image': p['image'].removeprefix('英语/科普版/'), 'sha256': p['sha256'], 'url': p['source']} for p in book_pages]
            source_url = row.get('source_page') if row else meta['source']
            chapters.append({'id':chapter_id,'publisher':'科普版','grade':f'{grade}年级','edition':edition,
                'unit':title,'sourcePage':source_url,'question':f'Write about {title}.','example':'',
                'tasks':[],'textKind':'教材词表原图识别并复核','recordings':[],
                'vocabulary':[{'word':e['word'],'meaning':e['meaning']} for e in entries],
                'curriculum':{'bookId':book_id,'series':series,'grade':grade,'term':term,'unitNumber':unit,
                    'firstUnit':first,'lastUnit':last,'words':[e['word'] for e in entries], 'entries':entries,
                    'observedWords':sorted(observed_words),'lessonEvidence':evidence_records,
                    'sourcePages':source_pages,'verified':True,'revision':revision,
                    'source':source_url,'method':'原图双轮识别、跨页核对；非学校人工审定'}})
            ids.append(chapter_id)
        books.append({'id':book_id,'grade':grade,'term':term,'edition':edition,'series':series,'chapterIds':ids})
    from scripts.build_grade3_curriculum import build as build_grade3
    grade3_books, grade3_chapters = build_grade3()
    books = grade3_books + books
    chapters = grade3_chapters + chapters
    pack = {'version':2, 'foundation':[], 'books':books, 'chapters':chapters,
            'audit':{'pages':len(manifest),'method':'DeepSeek vision transcription and image verification',
                     'crossPageNotes':fixes,'visualCorrections':visual['moves'],
                     'columnCheckedPages':sum(p.get('columnChecked',False) for group in pages.values() for p in group),
                     'lessonEvidencePages':len(lesson_manifest),
                     'grade3SourcePages':15,'grade3Method':'原图逐项识读与单元边界核对',
                     'schoolEditionConfirmed':False}}
    serialized = json.dumps(pack,ensure_ascii=False,indent=2)+'\n'
    (RESOURCE / '结构化教材词表.json').write_text(serialized)
    (ROOT / 'backend/data/chat/curriculum-vocabulary.json').write_text(serialized)
    (DATA / 'coverage.json').write_text(json.dumps({'books':books,'audit':pack['audit'],
        'units':len(chapters),'entries':sum(len(c['vocabulary']) for c in chapters),
        'unavailablePublishers':['人教版：本地无完整词表原图，不能将原创补充词冒充教材词表']},ensure_ascii=False,indent=2)+'\n')
    print('Built',len(books),'books',len(chapters),'units',sum(len(c['vocabulary']) for c in chapters),'entries')


if __name__ == '__main__':
    build()
