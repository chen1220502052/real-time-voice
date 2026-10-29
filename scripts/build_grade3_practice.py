"""Connect collected grade-three media and original speaking tasks to practice.

Does not transcribe wordlist images or claim that topic examples are textbook text.
"""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1] / '英语'


def task_fields(path):
    text = path.read_text()
    question = re.search(r'^A: (.+)$', text, re.M)[1]
    example = re.search(r'^B: (.+)$', text, re.M)[1]
    return {'grade': '3年级', 'kind': '原创口语补充_非教材原文或完整词表',
            'vocabulary': [], 'question': question, 'model': example,
            'tasks': [
                {'type': '听音跟读', 'instruction': '选一段教材对话，听一句、暂停、跟一句，共两遍。'},
                {'type': '角色扮演', 'instruction': '与同伴交换角色，再说一遍。'},
                {'type': '简短问答', 'instruction': question + ' 用一句简短英语回答。'}],
            'official_audio': [], 'official_vocabulary_audio': []}


def build():
    pep = ROOT / '人教版'
    audio = json.loads((pep / '资源总索引.json').read_text())
    rows = []
    for term in ('上册', '下册'):
        edition = term + '_新版'
        selected = [r for r in audio if r['grade'] == '3年级' and r['edition'] == edition]
        for unit in dict.fromkeys(r['unit'] for r in selected if re.match(r'Unit \d+\b', r['unit'])):
            row = task_fields(pep / '3年级' / edition / unit / '口语/口语任务与示范.txt')
            number = int(re.match(r'Unit (\d+)', unit)[1])
            row.update(edition=edition, unit=unit, source_page=selected[0]['source_page'])
            row['official_audio'] = [r for r in selected if r['unit'] == unit]
            row['official_vocabulary_audio'] = [r for r in selected if r['unit'] == 'Words in each unit'
                                              and re.search(r'Unit\s*' + str(number) + r'\b', r['activity'])]
            pages = json.loads((pep / '3年级' / edition / '教材原页索引.json').read_text())
            row['textbook_pages'] = [{'path': '3年级/' + page['path'],
                                     'label': '教材原页 · PDF第' + str(page['pdf_page']) + '页'}
                                    for page in pages[6 + (number - 1) * 12:6 + number * 12]]
            rows.append(row)
    (pep / '三年级单元训练.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    kep = ROOT / '科普版'
    rows = []
    for media in json.loads((kep / '本地课文音频总索引.json').read_text()):
        if media['grade'] != '3年级':
            continue
        row = task_fields(kep / media['local_directory'] / '口语/口语任务与示范.txt')
        row.update(edition=media['edition'], unit=media['unit'], source_page=media['source_page'])
        rows.append(row)
    (kep / '三年级单元训练.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    build()
