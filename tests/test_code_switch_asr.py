"""Protocol and delivery regressions; real model accuracy is measured separately."""
import math
from types import SimpleNamespace

import pytest

from backend.text_bot import BotMessage
from backend.voice_grounding import resolve_voice_reply
from scripts.local_speech_worker import scored_qwen_transcription


class Tokenizer:
    def decode(self, tokens, **kwargs):
        return 'language Chinese<asr_text>请解释 milk。'

    def encode(self, text, **kwargs):
        return [0, 1, 2]  # The language header has three protocol tokens.


class Decoder:
    _tokenizer = Tokenizer()

    def stream_generate(self, audio, **kwargs):
        for token, probability in enumerate([.01, .01, .01, .81, .64]):
            yield token, {token: math.log(probability)}

    def generate(self, audio, **kwargs):
        assert kwargs['language'] is None  # Mandarin routing cannot force a Chinese-only decoder.
        list(self.stream_generate(audio, **kwargs))
        return SimpleNamespace(text='请解释 milk。', language=['Chinese'])


def test_transcription_confidence_excludes_the_language_header(monkeypatch):
    monkeypatch.delenv('LOCAL_ASR_MIN_LOGPROB', raising=False)
    decoder = Decoder()
    original = decoder.stream_generate
    result = scored_qwen_transcription(decoder, object())
    assert result['text'] == '请解释 milk。'
    assert result['confidence'] == pytest.approx(.72)
    assert result['language'] == 'zh' and result['engine'] == 'qwen3-asr'
    assert decoder.stream_generate == original


def test_uncertain_or_incomplete_asr_is_not_promoted_to_a_confident_reply(monkeypatch):
    monkeypatch.setenv('LOCAL_ASR_MIN_LOGPROB', '-.1')
    assert scored_qwen_transcription(Decoder(), object())['text'] == ''
    decoder = Decoder()
    original = decoder.stream_generate
    with pytest.raises(ValueError, match='token limit'):
        scored_qwen_transcription(decoder, object(), max_tokens=5)
    assert decoder.stream_generate == original


def test_decoder_failure_restores_the_score_hook():
    class Broken(Decoder):
        def generate(self, *args, **kwargs):
            list(self.stream_generate(object()))
            raise RuntimeError('decoder failed')
    decoder = Broken()
    original = decoder.stream_generate
    with pytest.raises(RuntimeError, match='decoder failed'):
        scored_qwen_transcription(decoder, object())
    assert decoder.stream_generate == original


@pytest.mark.parametrize('quote', ['"', '“', "'"])
def test_asr_quotes_do_not_turn_an_audible_target_word_into_a_missing_slot(quote):
    close = '”' if quote == '“' else quote
    word = quote + 'milk' + close
    direct = resolve_voice_reply((BotMessage('user', f'告诉我英语单词{word}的中文意思。'),))
    assert direct.source == 'grounded_glossary' and '牛奶' in direct.text
    compound = resolve_voice_reply((BotMessage('user', f'请解释{word}的中文意思，再给一个英语例句。'),))
    assert compound is None


def test_genuinely_empty_target_still_requests_clarification():
    result = resolve_voice_reply((BotMessage('user', '请解释的中文意思，再给一个英语例句。'),))
    assert result.source == 'grounded_missing_word'


def test_explicit_chinese_word_is_not_erased_as_a_missing_english_slot():
    assert resolve_voice_reply((BotMessage('user', '请用中文解释“早餐”的意思。'),)) is None
    result=resolve_voice_reply((BotMessage('user', '请告诉我的中文意思。'),))
    assert result.source=='grounded_missing_word'


def test_verified_meaning_is_kept_when_the_small_model_only_describes_the_colour(monkeypatch):
    import asyncio
    from backend import local_llm
    from backend.voice_grounding import ensure_chinese_reply
    async def forbidden(*a,**kw):raise AssertionError('The existing example is already complete')
    monkeypatch.setattr(local_llm,'complete',forbidden)
    async def source():yield 'red 是像国旗和花朵那样的颜色。The flag is red.'
    async def run():
        text=''.join([s async for s in ensure_chinese_reply(source(),(BotMessage('user','用中文解释 red，再给一个英语例句。'),))])
        assert '红色' in text and 'The flag is red.' in text and '花朵那样' not in text
    asyncio.run(run())


def test_known_meaning_survives_an_english_definition_in_a_bounded_repair(monkeypatch):
    import asyncio,json
    from backend import local_llm
    from backend.voice_grounding import ensure_chinese_reply
    calls=[]
    async def repair(*a,**kw):
        calls.append(kw)
        return json.dumps({'definition':'comfortable means relaxed.','examples':['This chair is comfortable.','My bed is comfortable.']})
    monkeypatch.setattr(local_llm,'complete',repair)
    async def source():yield 'comfortable 是一个形容词。'
    async def run():
        text=''.join([s async for s in ensure_chinese_reply(source(),(BotMessage('user','请用中文解释 comfortable，给两个英语例句。'),))])
        assert '舒服' in text and 'This chair is comfortable.' in text and 'My bed is comfortable.' in text
        assert len(calls)==1 and calls[0]['purpose']=='voice_chat'
    asyncio.run(run())
