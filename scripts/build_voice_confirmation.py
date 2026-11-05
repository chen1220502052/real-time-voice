"""A fresh 144-case final set after the exploratory v2 failures became regressions."""
import argparse
import hashlib
import json
from pathlib import Path

from scripts.build_voice_eval_v2 import ROOT, validate

DEST = ROOT / 'evals/voice_chat/v2/confirmation.json'


def build():
    cases = []

    def add(group, category, texts, required, *, chinese=False, questions=0):
        for grade in (4, 7, 8):
            cases.append({'id': f'confirm-{group}-{grade}', 'group': group, 'split': 'heldout',
                          'category': category, 'grade': grade,
                          'route': 'extension' if len(cases) % 2 else 'curriculum',
                          'messages': [{'role': 'user', 'content': t} for t in texts],
                          'checks': {'required': required, 'forbidden': [], 'chinese': chinese,
                                     'max_questions': questions, 'max_words': 55},
                          'rubric': 'Evaluate actual requested facts and language; translated questions may contain question marks.'})

    unknown = r'not (?:said|told)|not.*favou?rite|not know|don.t know|unknown|not.*like best|没有|未.*最喜欢'
    prefs = [
        (['I like seahorses.', 'Have I said seahorses are my favourite animal?'], [unknown]),
        (['I love playing the violin.', 'What is my favourite hobby?'], [unknown]),
        (['My favourite food is soup.', 'I also like noodles.', 'What is my favourite food?'], ['soup']),
        (['I like cocoa best.', 'What is my favourite drink?'], ['cocoa']),
        (['My favourite instrument is the flute.', 'Did I say the flute is my favourite instrument?'], ['yes|是的', 'flute']),
        (['My favourite animal is a penguin.', 'Actually, my favourite animal is an otter.', 'What is my favourite animal?'], ['otter']),
        (["I don't have a favourite subject.", 'What is my favourite subject?'], [unknown]),
        (['Grey is my favourite colour.', 'What is my favourite colour?'], ['grey']),
        (['I like dumplings and rice.', 'What is my favourite food?'], [unknown]),
        (['我喜欢弹钢琴。', '请用中文回答，我最喜欢的爱好是什么？'], ['还没有|没有明确|没.*最喜欢']),
        (['我最喜欢的动物是海豚。', '请用中文说，我最喜欢的动物是什么？'], ['海豚']),
        (['My favourite colour is pink.', 'My favourite subject is maths.', 'What is my favourite colour?'], ['pink']),
    ]
    for i, (texts, required) in enumerate(prefs):
        add(f'pref-{i}', 'preference', texts, required, chinese=i in {9,10})

    ages = [
        'Could you tell me your actual age, please?', 'What is your birthday, Kitty?',
        'Are you really twelve years old?', 'How old were you last year?',
        'When were you born, little AI cat?', 'Have you turned seven already?',
        'How old you really are is what I want to know.', 'Does your age change each year?',
        '请用中文说，你的真实年龄是多少？', '你现在有几岁？',
        'Are you a four-year-old cat or a five-year-old cat?', 'How old are you in human years?'
    ]
    for i, text in enumerate(ages):
        add(f'age-{i}', 'identity', [text], ['AI|人工智能', r'no.*age|don.t.*age|not.*age|没有.*年龄'], chinese=i==8)

    words = [('umbrella', '伞'), ('rainy', '雨'), ('windy', '风'), ('snowy', '雪'),
             ('healthy', '健康'), ('sweater', '毛衣|线衣'), ('trousers', '裤'), ('sandwich', '三明治'),
             ('water', '水'), ('window', '窗'), ('garden', '花园'), ('vegetable', '蔬菜'),
             ('rabbit', '兔'), ('cinema', '电影院|影院'), ('weekend', '周末'), ('patient', '病人|耐心')]
    for i, (word, meaning) in enumerate(words):
        text = [f'What is the Chinese translation of "{word}"?', f'请告诉我 {word} 的中文意思。',
                f'Translate "{word}" into Chinese.'][i % 3]
        add('word-'+word, 'glossary', [text], [meaning], chinese=True)

    controls = [
        ('boy-age', ['The boy Max is fifteen. How old is Max?'], ['fifteen|15'], False, 0),
        ('my-age', ['I am thirteen. What is my age?'], ['thirteen|13'], False, 0),
        ('fiction-age', ['In my story an AI called Kitty was made eight years ago. How many years ago was it made in the story?'], ['eight|8'], False, 0),
        ('quoted-age', ['Translate "Are you seven years old?" into Chinese.'], ['你.*(?:七|7).*岁'], True, 1),
        ('quoted-name', ['Translate "What is your name?" into Chinese.'], ['你.*(?:叫|名字)'], True, 1),
        ('meaning-example', ['What does windy mean in Chinese and give one short English statement as an example?'], ['风', r'\b(?:it|today|the|we|I)\b.*windy'], True, 0),
        ('unknown', ['Please give the Chinese meaning of "vornelisk". If you cannot verify it, say so.'], ['没有可靠|不确定|不知道|无法确认|查不到|not know'], True, 0),
        ('right-answer', ['In "Your answer is right", what does right mean in Chinese?'], ['正确|对的'], True, 0),
    ]
    for group, texts, required, chinese, questions in controls:
        add('control-'+group, 'boundary', texts, required, chinese=chinese, questions=questions)
    pack = {'version': 2, 'purpose': 'final_independent', 'date': '2026-11-03', 'cases': cases,
            'scope': '144 fresh scenarios after exploratory 192-case results were inspected; text and Bot delivery, not audio.',
            'independence': 'New question/history signatures versus v1 and exploratory v2; new grades 4/7/8 and glossary requests. '
                            'No adaptation after this final set is executed.',
            'sources': [{'path': 'backend/data/chat/curriculum-vocabulary.json',
                         'sha256': hashlib.sha256((ROOT/'backend/data/chat/curriculum-vocabulary.json').read_bytes()).hexdigest()}]}
    validate(pack)
    return pack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    output = json.dumps(build(), ensure_ascii=False, indent=2)+'\n'
    if args.check:
        assert DEST.read_text() == output
    else:
        DEST.write_text(output)
    print('144 final independent cases: preference 36, identity 36, glossary 48, boundaries 24')


if __name__ == '__main__':
    main()
