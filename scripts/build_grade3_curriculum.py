"""Build grade-three curriculum from inspected, hash-bound original wordlist pages."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from backend.chapter_practice import _id

ROOT = Path(__file__).resolve().parents[1]


def build():
    resource = ROOT / '英语/科普版'
    pack = json.loads((ROOT / 'backend/data/curriculum/grade3-words.json').read_text())
    training = json.loads((resource / '三年级单元训练.json').read_text())
    books, chapters = [], []
    for term in ('上册', '下册'):
        entries, sources = defaultdict(list), defaultdict(list)
        pages = [p for p in pack['pages'] if p['term'] == term]
        for page in pages:
            assert page['checked'] and hashlib.sha256((resource / page['image']).read_bytes()).hexdigest() == page['sha256']
            evidence = {'image':page['image'], 'sha256':page['sha256'], 'url':page['source']}
            for block in page['blocks']:
                entries[block['unit']].extend({**entry, 'sourceImage':'英语/科普版/' + page['image'],
                    'pageSha256':page['sha256']} for entry in block['entries'])
                sources[block['unit']].append(evidence)
        edition = term + '_官方当前目录_版次待与学校核对'
        book_id = _id('科普版', 3, term, edition)
        ids = []
        for unit in range(1, 13):
            revision = unit in pack['revisionUnits'].get(term, [])
            if not entries[unit] and not revision:
                raise ValueError(f'Missing grade-three vocabulary: {term} Unit {unit}')
            row = next(r for r in training if r['edition'] == edition and r['unit'].startswith(f'Unit {unit} '))
            chapter_id = _id('科普版', '3年级', edition, row['unit'])
            words = entries[unit]
            source_pages = sources[unit] if not revision else [
                {'image':p['image'],'sha256':p['sha256'],'url':p['source']} for p in pages]
            chapters.append({'id':chapter_id,'publisher':'科普版','grade':'3年级','edition':edition,
                'unit':row['unit'],'sourcePage':row['source_page'],'question':row['question'],
                'example':row['model'],'tasks':row['tasks'],'textKind':'教材词表原图识读并核对','recordings':[],
                'vocabulary':[{'word':e['word'],'meaning':e['meaning']} for e in words],
                'curriculum':{'bookId':book_id,'series':'科普小学现有教材','grade':3,'term':term,
                    'unitNumber':unit,'firstUnit':1,'lastUnit':12,'words':[e['word'] for e in words],
                    'entries':words,'observedWords':[],'lessonEvidence':[],'sourcePages':source_pages,
                    'verified':True,'revision':revision,'source':row['source_page'],
                    'method':'原图逐项识读与单元边界核对；非学校人工审定'}})
            ids.append(chapter_id)
        books.append({'id':book_id,'grade':3,'term':term,'edition':edition,
                      'series':'科普小学现有教材','chapterIds':ids})
    return books, chapters
