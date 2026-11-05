import asyncio
from types import SimpleNamespace

import pytest

from backend.curriculum_chat import CurriculumChatBot
from backend.english_chat import EnglishChatBot
from backend.text_bot import BotMessage, BotRequest
from backend.voice_grounding import resolve_text_reply, text_grounding_note


@pytest.mark.parametrize('curriculum', [False, True])
@pytest.mark.parametrize('messages, expected', [
    ([('user', 'I like oranges.'), ('assistant', 'Oranges are your favourite.'),
      ('user', 'What is my favourite fruit?')], 'have not said'),
    ([('user', 'My favourite fruit is now pears.'), ('user', 'What is my favourite fruit?')], 'pears'),
    ([('user', 'When were you born? Answer in Chinese.')], '没有人类的年龄'),
    ([('user', 'What does “cherry” mean in Chinese?')], '樱桃'),
])
def test_both_text_routes_ground_answers_without_model(curriculum, messages, expected):
    def unexpected(*args, **kwargs):
        pytest.fail('An evidenced direct answer should not call the model')
    scope = SimpleNamespace(chapter={'grade': '3年级'})
    bot = (CurriculumChatBot(unexpected, scope_factory=lambda _: scope) if curriculum
           else EnglishChatBot(model_factory=unexpected))
    request = BotRequest('request', 'session', 'turn', tuple(BotMessage(*m) for m in messages),
                         options={'chapterId': 'unit'} if curriculum else {}, channel='text')

    async def run():
        return [event async for event in bot.stream(request)]

    events = asyncio.run(run())
    assert [event.type for event in events] == ['text_delta', 'done']
    assert expected in events[0].text


@pytest.mark.parametrize('query', [
    'What does “peach” mean in Chinese? Give one English example too.',
    'What does “peach” mean in Chinese and use it in a sentence?',
    'peach 是什么意思，顺便造句。',
    'Pretend you are Mia. How old are you?',
    'How do you say “How old are you?” in Chinese?',
])
def test_text_grounding_leaves_compound_fiction_and_translation_to_model(query):
    assert resolve_text_reply((BotMessage('user', query),)) is None


def test_compound_word_request_supplies_definition_to_model():
    messages = (BotMessage('user', 'What does “peach” mean in Chinese? Give one English example too.'),)
    note = text_grounding_note(messages)
    assert '桃子' in note and 'English examples' in note
