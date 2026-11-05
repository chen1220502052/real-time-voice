"""Acoustic recovery safety: real models are exercised in separate frozen runs."""
from types import SimpleNamespace

import pytest

from backend.asr_target import target_slot, verify_target, needs_verification
from backend.text_bot import BotMessage
from backend.voice_grounding import resolve_voice_reply


def aligned(*chunks):
    return [dict(word=text, start=start, end=end) for text, start, end in chunks]


def token(text, start, end, confidence=.96):
    return SimpleNamespace(text=text, start=start, end=end, confidence=confidence)


def test_missing_word_recovery_requires_two_acoustic_hypotheses_and_keeps_the_request():
    text = '请解释的中文意思，并给一个英语例句。'
    words = aligned(('请解释', 0, 1.8), ('的中文意思，并给一个英语例句。', 1.8, 4))
    called = []
    def decode(start, end):
        called.append((start, end))
        return dict(text='Window.', confidence=.7)
    result = verify_target(dict(text=text, confidence=.95), words,
                           [token(' Window', 1.5, 1.9)], decode)
    assert result['text'] == '请解释window的中文意思，并给一个英语例句。'
    assert result['targetVerification']['status'] == 'recovered'
    assert called[0] == pytest.approx((1.42, 2.22))


def test_wrong_short_word_disagreement_requests_clarification_without_choosing_a_spelling():
    text = 'head的中文意思是什么？'
    words = aligned(('head', 0, .5), ('的中文意思是什么？', .5, 2))
    result = verify_target(dict(text=text, confidence=.9), words,
                           [token(' Had', .1, .4)],
                           lambda *a: dict(text='hat', confidence=.65))
    assert result['text'] == '[英语词未听清]的中文意思是什么？'
    assert result['targetVerification']['status'] == 'uncertain'
    reply = resolve_voice_reply((BotMessage('user', result['text']),))
    assert reply.source == 'grounded_missing_word'
    assert '再说一遍' in reply.text


def test_empty_slot_does_not_accept_a_different_crop_word_or_low_confidence_line():
    text = '英语单词中文怎么说？'
    words = aligned(('英语单词', 0, 1), ('中文怎么说？', 1, 2))
    result = verify_target(dict(text=text), words, [token(' bag', .8, 1.2)],
                           lambda *a: dict(text='bad', confidence=.9))
    assert '[英语词未听清]' in result['text']
    assert result['targetVerification']['status'] == 'unresolved'
    result = verify_target(dict(text=text), words, [token(' bag', .8, 1.2, .4)],
                           lambda *a: pytest.fail('Low-confidence phonetic guess cannot trigger insertion'))
    assert '[英语词未听清]' in result['text']


@pytest.mark.parametrize('text', ['请解释“早餐”的中文意思。', '森林是什么意思？',
                                 '请解释 black cat 的中文意思。', '请解释 hat 和 head 的区别。',
                                 '我喜欢绿色。', 'What does window mean?'])
def test_chinese_targets_multitarget_tasks_and_ordinary_chat_are_left_alone(text):
    words = aligned((text, 0, 3))
    assert target_slot(text, words) is None


def test_alignment_mismatch_is_never_used_to_guess_audio_coordinates():
    assert target_slot('请解释dog的意思。', aligned(('请解释的意思。', 0, 3))) is None


def test_known_word_spelling_is_preserved_when_its_audio_crop_agrees():
    text = '请解释“BED”的中文意思。'
    words = aligned(('请解释', 0, .8), ('BED', .8, 1.2), ('的中文意思。', 1.2, 2))
    result = verify_target(dict(text=text), words, [],
                           lambda *a: dict(text='bed', confidence=.7))
    assert result['text'] == '请解释“bed”的中文意思。'
    assert result['targetVerification']['status'] == 'verified'


def test_two_supported_different_words_are_not_collapsed_into_one_answer():
    text = '请解释的中文意思。'
    words = aligned(('请解释', 0, 1), ('的中文意思。', 1, 2))
    results = iter([dict(text='pen', confidence=.8), dict(text='pan', confidence=.8)])
    result = verify_target(dict(text=text), words,
                           [token(' pen', .6, .8), token(' pan', .9, 1.1)],
                           lambda *a: next(results))
    assert '[英语词未听清]' in result['text']
    assert result['targetVerification']['status'] == 'unresolved'


def test_already_agreed_short_words_do_not_pay_for_alignment_or_recropping():
    assert not needs_verification('请解释 tree 的中文意思。', [dict(text='tree')])
    assert needs_verification('请解释 head 的中文意思。', [dict(text='had')])
    assert needs_verification('请解释的中文意思。', [dict(text='window')])
    assert not needs_verification('请解释 kitchen 的意思。', [])
    assert not needs_verification('请解释早餐的意思。', [])


@pytest.mark.parametrize('text', [
    '我想知道的中文含义。',
    '我想知道这个英语单词的中文意思。',
    '请告诉我这个词的中文含义。',
    '请用中文解释英文单词的意思，并给两个英语例句。',
])
def test_empty_mandarin_carriers_never_invite_the_dialogue_model_to_invent_a_word(text):
    result = resolve_voice_reply((BotMessage('user', text),))
    assert result is not None and result.source == 'grounded_missing_word'
    assert '再说一遍' in result.text
