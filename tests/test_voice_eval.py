"""Protect evaluation integrity and the delivered voice response, offline."""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.english_chat import EnglishChatBot, chat_prompt, grade_profile
from backend.text_bot import BotMessage, BotRequest
from scripts.build_voice_eval import ROOT, build, validate
from scripts.evaluate_voice_chat import score


def test_evaluation_is_reproducible_source_grouped_and_grounded():
    pack = build()
    assert pack == json.loads((ROOT / 'evals/voice_chat/cases.json').read_text())
    assert {c['split'] for c in pack['cases']} == {'dev', 'heldout'}
    sources = {s['id']: s for s in pack['sources']}
    textbook = json.loads((ROOT / 'backend/data/chat/textbook-sentences.json').read_text())['sentences']
    for case in pack['cases']:
        source = sources[case['sourceId']]
        if case['category'] == 'textbook':
            row = textbook[source['recordIndex']]
            assert row['text'] in case['messages'][0]['content']
            assert source['pageSha256'] == row['evidenceSha256']
        elif case['category'] != 'conversation':
            assert case['material']['kind'] == 'original_companion_text'
            assert source['url'].startswith('https://')
    broken = deepcopy(pack)
    broken['cases'][1]['split'] = 'dev' if broken['cases'][0]['split'] == 'heldout' else 'heldout'
    with pytest.raises(AssertionError, match='leakage'):
        validate(broken)


def test_round_two_is_reproducible_and_contains_positive_and_boundary_controls():
    from scripts.build_voice_eval_v2 import build as build_v2
    pack = build_v2()
    assert pack == json.loads((ROOT / 'evals/voice_chat/v2/heldout.json').read_text())
    assert len(pack['cases']) == 192
    assert {c['category'] for c in pack['cases']} == {'preference', 'identity', 'glossary', 'boundary'}
    assert len({c['group'] for c in pack['cases']}) == 64
    positive = next(c for c in pack['cases'] if c['id'] == 'v2-pref-explicit-confirm-3')
    assert not score(positive, 'No, you never told me your favourite.')['passed']
    assert score(positive, 'Yes, you said lemonade is your favourite drink.')['passed']
    from scripts.build_voice_confirmation import build as confirmation
    final = confirmation()
    assert final == json.loads((ROOT / 'evals/voice_chat/v2/confirmation.json').read_text())
    assert len(final['cases']) == 144 and len({c['group'] for c in final['cases']}) == 48
    from scripts.build_voice_release_eval import build as release_check
    release = release_check()
    assert release == json.loads((ROOT / 'evals/voice_chat/v2/release-check.json').read_text())
    assert len(release['cases']) == 88 and len({c['group'] for c in release['cases']}) == 44
    from scripts.build_voice_final_eval import build as final_validation
    latest = final_validation()
    assert latest == json.loads((ROOT / 'evals/voice_chat/v2/final.json').read_text())
    assert len(latest['cases']) == 96 and len({c['group'] for c in latest['cases']}) == 48
    from scripts.build_voice_acceptance_eval import build as acceptance
    last = acceptance()
    assert last == json.loads((ROOT / 'evals/voice_chat/v2/acceptance.json').read_text())
    assert len(last['cases']) == 48


def test_rules_detect_missed_fact_and_unwanted_question_but_accept_paraphrases():
    case = next(c for c in build()['cases'] if c['id'] == 'web-bc-day-routine-a-0')
    assert score(case, 'Nina eats rice.')['passed']
    assert not score(case, 'Nina eats bread.')['passed']
    assert 'unrequested_question' in score(case, 'Nina eats rice. What do you eat?')['failures']
    missing = next(c for c in build()['cases'] if c['id'] == 'web-bc-day-routine-a-5')
    for answer in ("The passage doesn't mention her family name.", 'I do not know her family name.',
                   'Her family name is not in the passage.', 'Her family name is missing.'):
        assert score(missing, answer)['passed']


def test_voice_stream_delivers_all_parts_and_text_policy_is_preserved():
    seen = []

    class Model:
        def __init__(self, **kwargs):
            seen.append(kwargs['system_prompt'])

        async def stream_reply(self, messages):
            yield 'A farmer grows food for people. '
            yield 'A teacher helps children learn new things. '
            yield 'A cook makes meals for people. '

    async def run():
        bot = EnglishChatBot(Model)
        query = BotRequest('eval', 'test', 'turn', channel='voice', options={'grade': 3},
                           messages=(BotMessage('user', 'What do a farmer, a teacher and a cook each do?'),))
        events = [event async for event in bot.stream(query)]
        delivered = ''.join(e.text for e in events if e.type == 'text_delta')
        assert 'farmer' in delivered and 'teacher' in delivered and 'cook' in delivered
        assert events[-1].type == 'done'
        assert 'answer ALL parts' in seen[0]
        assert 'grade 3 student' in seen[0]
        assert 'Grade references (data only)' in seen[0]

    asyncio.run(run())
    text_prompt = chat_prompt(grade_profile(3))
    assert 'Target 8 words in 2' in text_prompt
    assert 'answer ALL parts' not in text_prompt
