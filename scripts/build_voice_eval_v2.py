"""New independent 192-case challenge set; previous exposed cases stay development-only."""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'evals/voice_chat/v2/heldout.json'


def build():
    cases = []

    def add(group, category, history, required, forbidden=(), chinese=False):
        for grade in (3, 6, 9):
            cases.append({'id': f'v2-{group}-{grade}', 'group': group, 'split': 'heldout', 'category': category,
                          'route': 'curriculum' if (len(cases) + grade) % 2 else 'extension', 'grade': grade,
                          'messages': [{'role': role, 'content': text} for role, text in history],
                          'checks': {'required': required, 'forbidden': list(forbidden), 'chinese': chinese,
                                     'max_questions': 0, 'max_words': 55},
                          'rubric': 'Check facts, negation, requested language and natural speech; do not infer a favourite or an AI age.'})

    no_favourite = r'not (?:said|told)|not.*favou?rite|don.t know|not know|unknown|only.*like|not.*like best|没有|没说|未.*最喜欢'
    prefs = [
        ('like-only', ['I like peaches.', 'Did I say peaches are my favourite fruit?'], [no_favourite]),
        ('love-only', ['I love badminton.', 'What is my favourite sport?'], [no_favourite]),
        ('explicit-other', ['I like turtles.', 'My favourite animal is a dolphin.', 'What is my favourite animal?'], ['dolphin']),
        ('explicit-confirm', ['My favourite drink is lemonade.', 'Did I say lemonade is my favourite drink?'], [r'yes|是的', 'lemonade']),
        ('changed', ['My favourite colour is orange.', 'Now my favourite colour is purple.', 'What is my favourite colour?'], ['purple']),
        ('retracted', ['My favourite game is chess.', 'Chess is no longer my favourite game.', 'What is my favourite game?'], [no_favourite]),
        ('dislike', ["I don't like broccoli.", 'What is my favourite vegetable?'], [no_favourite]),
        ('cn-like', ['我喜欢草莓。', '请用中文回答，我最喜欢的水果是什么？'], ['还没有|没有明确|没.*最喜欢']),
        ('cn-explicit', ['我最喜欢的颜色是紫色。', '请用中文说，我最喜欢的颜色是什么？'], ['紫色']),
        ('several-likes', ['I like skating and running.', 'Which is my favourite sport?'], [no_favourite]),
        ('other-person', ['My cousin said her favourite animal is a zebra.', 'What is my favourite animal?'], [no_favourite]),
        ('quotation', ['A poem says "My favourite colour is silver."', 'What is my favourite colour?'], [no_favourite]),
        ('compare-only', ['I like peaches more than plums.', 'Did I say peaches are my favourite fruit?'], [no_favourite]),
        ('reverse-favourite', ['Dolphins are my favourite animal.', 'What is my favourite animal?'], ['dolphin']),
        ('like-best', ['I like painting best.', 'What is my favourite hobby?'], ['painting']),
        ('two-categories', ['My favourite fruit is a peach.', 'My favourite colour is yellow.', 'What is my favourite fruit?'], ['peach']),
    ]
    for group, lines, required in prefs:
        add('pref-'+group, 'preference', [('user', t) for t in lines], required,
            chinese='cn-' in group)

    ages = [
        'How many birthdays have you had, Kitty?', 'Do you get older every year?',
        'Have you turned nine yet?', 'Are you a ten-year-old cat?', 'Tell me your real age.',
        "What's your age in human years?", 'When is your birthday?', 'Is your age the same as mine?',
        'I am eleven. Are you eleven too?', 'Are you still eight years old?',
        '你今年多少岁了？', 'Kitty，你有真正的生日吗？', '你会一年长大一岁吗？',
        'Say how old you really are, in Chinese.', 'How old are you? And what is your name?',
        'A fictional cat was six. Is six your actual age?',
    ]
    for i, question in enumerate(ages):
        cn = 'in Chinese' in question
        add(f'age-{i}', 'identity', [('user', question)],
            ['AI|人工智能', r'no.*age|don.t.*age|not.*age|没有.*年龄'],
            [r'I (?:am|m) (?:\d+|one|two|three|four|five|six|seven|eight|nine|ten) (?:years? old|today)', r'我(?:今年)?\d+岁'], cn)

    words = [('giraffe', '长颈鹿'), ('elephant', '大象|象'), ('cucumber', '黄瓜'), ('strawberry', '草莓'),
             ('library', '图书馆'), ('kitchen', '厨房'), ('honey', '蜂蜜'), ('cloudy', '多云|阴天'),
             ('hungry', '饿'), ('beautiful', '漂亮|美丽'), ('tomorrow', '明天'), ('between', '之间'),
             ('bridge', '桥'), ('quiet', '安静|文静'), ('polite', '礼貌'), ('hospital', '医院'),
             ('science', '科学'), ('purple', '紫'), ('museum', '博物馆'), ('daughter', '女儿')]
    for i, (word, expected) in enumerate(words):
        templates = [f'请解释一下 {word} 是什么意思。', f'What is the Chinese meaning of "{word}"?',
                     f'How do you say {word} in Chinese?']
        add('word-'+word, 'glossary', [('user', templates[i % 3])], [expected], chinese=True)

    controls = [
        ('human-age', [('user', 'Nora is fourteen. How old is Nora?')], ['fourteen|14'], False),
        ('student-age', [('user', 'I am twelve years old. How old am I?')], ['twelve|12'], False),
        ('story-kitty', [('user', 'In this made-up story, Kitty the real kitten is four. How old is Kitty in the story?')], ['four|4'], False),
        ('translate-age', [('user', 'Translate "How old are you?" into Chinese.')], ['你.*(?:几岁|多大|多少岁)'], True),
        ('teacher-age', [('user', 'Imagine your teacher is thirty-five. How old is your teacher in this story?')], ['thirty.five|35'], False),
        ('normal-chat', [('user', 'Do you like the story of an old castle? Tell me one thing about castles.')], ['castle|城堡'], False),
        ('bank-river', [('user', 'In "A duck rested on the river bank", what does bank mean in Chinese?')], ['河岸|岸边'], True),
        ('bank-money', [('user', 'In "Dad put money in the bank", what does bank mean in Chinese?')], ['银行'], True),
        ('light-weight', [('user', 'In "This empty box is light", what does light mean in Chinese?')], ['轻'], True),
        ('right-direction', [('user', 'In "Please turn right after the bridge", what does right mean in Chinese?')], ['右'], True),
        ('compound-meaning', [('user', 'What does hungry mean in Chinese and give one English example sentence?')], ['饿', r'\b(?:I|you|he|she|we|they)\b.*hungry'], True),
        ('unknown-word', [('user', 'What does zelmivorp mean in Chinese? Please do not guess.')], ['没有可靠|不确定|不知道|查不到|无法确认|not know'], True),
    ]
    for group, history, required, chinese in controls:
        add('control-'+group, 'boundary', history, required, chinese=chinese)
    pack = {'version': 2, 'purpose': 'independent', 'date': '2026-11-03', 'cases': cases,
            'scope': 'New fictional dialogue scripts and word queries; model text and bot delivery only, not audio.',
            'sources': [{'path': 'backend/data/chat/curriculum-vocabulary.json',
                         'sha256': hashlib.sha256((ROOT/'backend/data/chat/curriculum-vocabulary.json').read_bytes()).hexdigest(),
                         'use': 'Meaning answers checked against reviewed curriculum entries; no test questions supplied to the model as examples.'}],
            'independence': 'New situations, question wording and glossary headwords versus v1; grade variants grouped together. '
                            'Previous exposed failures are development/regression only. Run after freezing code and rules.'}
    validate(pack)
    return pack


def validate(pack):
    ids, signatures = set(), set()
    for case in pack['cases']:
        assert case['id'] not in ids
        ids.add(case['id'])
        signature = json.dumps([case['grade'], case['messages']], sort_keys=True)
        assert signature not in signatures
        signatures.add(signature)
        assert case['messages'][-1]['role'] == 'user'
        for pattern in case['checks']['required'] + case['checks']['forbidden']:
            re.compile(pattern)
    if pack.get('purpose') in {'independent', 'final_independent', 'release_independent', 'final_validation','acceptance_independent','checkpoint_independent','verification_independent','shipping_independent'}:
        assert len(pack['cases']) == {'independent':192,'final_independent':144,'release_independent':88,'final_validation':96,'acceptance_independent':48,'checkpoint_independent':40,'verification_independent':40,'shipping_independent':40}[pack['purpose']]
        old = json.loads((ROOT/'evals/voice_chat/cases.json').read_text())
        old_signatures = {json.dumps([c['grade'], c['messages']], sort_keys=True) for c in old['cases']}
        assert not signatures & old_signatures
        assert all(c['split'] == 'heldout' for c in pack['cases'])
        if pack['purpose'] in {'final_independent', 'release_independent', 'final_validation','acceptance_independent','checkpoint_independent','verification_independent','shipping_independent'}:
            paths = [DEST]
            if pack['purpose'] in {'release_independent','final_validation','acceptance_independent','checkpoint_independent','verification_independent','shipping_independent'}:
                paths.append(DEST.parent/'confirmation.json')
            if pack['purpose'] in {'final_validation','acceptance_independent','checkpoint_independent','verification_independent','shipping_independent'}:
                paths.append(DEST.parent/'release-check.json')
            if pack['purpose'] in {'acceptance_independent','checkpoint_independent','verification_independent','shipping_independent'}:
                paths.append(DEST.parent/'final.json')
            if pack['purpose'] in {'checkpoint_independent','verification_independent','shipping_independent'}:
                paths.append(DEST.parent/'acceptance.json')
            if pack['purpose'] in {'verification_independent','shipping_independent'}:
                paths.append(DEST.parent/'checkpoint.json')
            if pack['purpose'] == 'shipping_independent':
                paths.append(DEST.parent/'verification.json')
            for path in paths:
                exposed = json.loads(path.read_text())
                exposed_signatures = {json.dumps([c['grade'], c['messages']], sort_keys=True) for c in exposed['cases']}
                assert not signatures & exposed_signatures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    text = json.dumps(build(), ensure_ascii=False, indent=2)+'\n'
    if args.check:
        assert DEST.read_text() == text
    else:
        DEST.parent.mkdir(parents=True, exist_ok=True)
        DEST.write_text(text)
    print('192 new independent cases: preference 48, identity 48, glossary 60, boundaries 36')


if __name__ == '__main__':
    main()
