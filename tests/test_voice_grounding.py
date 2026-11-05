import asyncio

import pytest

from backend.text_bot import BotMessage, BotRequest
from backend.voice_grounding import resolve_voice_reply


def conversation(*texts):
    return tuple(BotMessage('user', t) for t in texts)


@pytest.mark.parametrize('text', ['How old are you?', 'When were you born?', 'What is your real age?',
                                 'Are you ten years old?', 'Tell me how old you really are.',
                                 '你今年多少岁？', '你的生日是什么时候？'])
def test_self_age_has_no_generated_number(text):
    reply = resolve_voice_reply(conversation(text))
    assert reply.source == 'grounded_identity'
    assert 'AI' in reply.text and 'human age' in reply.text
    assert not any(c.isdigit() for c in reply.text)


@pytest.mark.parametrize('text', ['How old is your teacher?', 'Translate "How old are you?" into Chinese.',
                                 'In the story Kitty is five. How old is Kitty in the story?',
                                 'Your brother is old. What does he do?', 'I am nine. How old am I?',
                                 '你的老师几岁？', 'My favourite fruit is pears.',
                                 'Tell me why your favourite stories have old castles.'])
def test_nearby_questions_still_reach_model(text):
    assert resolve_voice_reply(conversation(text)) is None


def test_like_is_not_favourite_but_explicit_favourites_and_changes_are_preserved():
    no = resolve_voice_reply(conversation('I like pears.', 'Did I say pears are my favourite fruit?'))
    assert 'not said' in no.text and not no.text.startswith('Yes')
    yes = resolve_voice_reply(conversation('My favourite fruit is pears.', 'Did I say pears are my favourite fruit?'))
    assert yes.text.startswith('Yes')
    updated = resolve_voice_reply(conversation('My favourite fruit is pears.', 'My favourite fruit is grapes.',
                                               'What is my favourite fruit?'))
    assert 'grapes' in updated.text and 'pears' not in updated.text
    retracted = resolve_voice_reply(conversation('My favourite fruit is pears.', 'Pears are no longer my favourite fruit.',
                                                 'What is my favourite fruit?'))
    assert 'not told' in retracted.text


def test_quotes_other_people_and_questions_do_not_create_user_preferences():
    for statement in ['My sister likes peaches.', 'A story says "My favourite fruit is pears."',
                      'Did I say my favourite fruit is pears?', 'If my favourite fruit is pears, what happens?']:
        reply = resolve_voice_reply(conversation(statement, 'What is my favourite fruit?'))
        assert 'not told' in reply.text
    unknown = resolve_voice_reply(conversation('I like pears.', 'Is my favourite colour green?'))
    assert 'green' not in unknown.text


def test_explicit_categories_and_cross_language_facts():
    messages = conversation('My favourite fruit is pears.', 'My favourite color is blue.', 'What is my favourite colour?')
    assert 'blue' in resolve_voice_reply(messages).text
    assert '芒果' in resolve_voice_reply(conversation('我最喜欢的水果是芒果。', 'What is my favourite fruit?')).text
    cn = resolve_voice_reply(conversation('我喜欢梨。', '请用中文回答，我最喜欢的水果是什么？'))
    assert '还没有明确' in cn.text
    kept = resolve_voice_reply(conversation('My favourite fruit is plums.', 'My favourite sport is running.',
                                            "I don't have a favourite sport now.", 'What is my favourite fruit?'))
    assert 'plums' in kept.text


@pytest.mark.parametrize(('text', 'meaning'), [
    ('watermelon是什么意思？', '西瓜'), ('P.E.是什么意思？', '体育'),
    ('What is the Chinese meaning of "cloudy"?', '多云'),
    ('请说说 thirsty 的中文意思。', '渴'),
    ('Please tell me the Chinese meaning of "peach".', '桃'),
    ('What is the Chinese meaning of gloves?', '手套'),
    ('What is the Chinese meaning of cherries?', '樱桃'),
    ('In "Turn right at the shop", what does right mean in Chinese?', '右'),
    ('In "Your answer is right", what does right mean in Chinese?', '正确'),
    ('In "The bag is light", what does light mean in Chinese?', '轻'),
    ('In "We sat by the river bank", what does bank mean in Chinese?', '河岸'),
    ('In "Chinese is my school subject", what does Chinese mean in Chinese?', '语文'),
])
def test_verified_word_meanings_and_contextual_senses(text, meaning):
    reply = resolve_voice_reply(conversation(text))
    assert reply.source == 'grounded_glossary' and meaning in reply.text


def test_unknown_word_is_not_guessed_and_extra_tasks_are_not_discarded():
    unknown = resolve_voice_reply(conversation('What does flumbazor mean in Chinese?'))
    assert unknown.source == 'grounded_glossary_unknown' and '没有可靠' in unknown.text
    assert resolve_voice_reply(conversation('What does thirsty mean in Chinese and give an example sentence?')) is None
    from backend.english_chat import chat_prompt, grade_profile
    prompt = chat_prompt(grade_profile(3), conversation(
        'What does hungry mean in Chinese and give one English example sentence?'), voice=True)
    assert '饥饿' in prompt and 'finish all requested extra tasks' in prompt
    passage = chat_prompt(grade_profile(4), conversation(
        'Read "Have some watermelon." Which fruit is offered? 请用中文回答。'), voice=True)
    assert 'watermelon' in passage and '西瓜' in passage and 'use Chinese' in passage
    from backend.voice_prompt import localized_voice_prompt
    assert '本轮学生明确要求中文' in localized_voice_prompt(3, conversation('请用中文回答。'))


def test_both_voice_routes_use_evidence_before_any_model_or_cache(monkeypatch):
    from types import SimpleNamespace
    from backend.english_chat import EnglishChatBot
    from backend.curriculum_chat import CurriculumChatBot

    def forbidden(*args, **kwargs):
        raise AssertionError('Narrow grounded reply must precede generation/retrieval')

    class Repository:
        lookup = forbidden

    async def run():
        for route in ['extension', 'curriculum']:
            bot = (EnglishChatBot(forbidden) if route == 'extension' else
                   CurriculumChatBot(agent_factory=forbidden, scope_factory=lambda _: SimpleNamespace(
                       chapter={'grade': '3年级', 'unit': 'Test'}, words=frozenset()), repository=Repository()))
            options = {'grade': 3, **({'chapterId': 'test'} if route == 'curriculum' else {})}
            query = BotRequest('test', 'test', 'test', channel='voice', options=options,
                               messages=conversation('How old are you?'))
            events = [event async for event in bot.stream(query)]
            assert events[0].type == 'text_delta' and 'AI' in events[0].text
            assert events[-1].type == 'done'
    asyncio.run(run())


def test_chinese_language_repair_never_releases_the_wrong_language(monkeypatch):
    from backend import local_llm
    from backend.voice_grounding import ensure_chinese_reply
    calls=[]
    async def convert(messages, **kwargs):
        calls.append((messages,kwargs))
        return '花园里有一朵花。'
    monkeypatch.setattr(local_llm,'complete',convert)
    async def source(text):
        yield text
    async def run():
        result=''.join([x async for x in ensure_chinese_reply(source('A flower is in the garden.'),conversation('请用中文回答。'))])
        assert result=='花园里有一朵花。' and 'A flower' not in result
        assert calls[0][1]['purpose']=='voice_chat'
        calls.clear()
        result=''.join([x async for x in ensure_chinese_reply(source('已经是中文。'),conversation('请用中文回答。'))])
        assert result=='已经是中文。' and not calls
        result=''.join([x async for x in ensure_chinese_reply(source('English is fine.'),conversation('Tell me about flowers.'))])
        assert result=='English is fine.' and not calls
        result=''.join([x async for x in ensure_chinese_reply(source('书在桌子上。桌子在书上。'),
                                                            conversation('Where is the book? 请用中文回答。'))])
        assert result=='书在桌子上。'
        async def bad_conversion(*args, **kwargs):
            return 'Still English.'
        monkeypatch.setattr(local_llm,'complete',bad_conversion)
        from backend.text_bot import TextBotError
        emitted=[]
        with pytest.raises(TextBotError, match='中文'):
            async for item in ensure_chinese_reply(source('Wrong language.'),conversation('请用中文回答。')):
                emitted.append(item)
        assert not emitted
    asyncio.run(run())


def test_short_bilingual_answers_are_not_rejected_as_english_fragments():
    from backend.reply_sentences import complete_tail
    assert complete_tail('white 是白色') == 'white 是白色。'
    assert complete_tail('Sara 有十四支铅笔') == 'Sara 有十四支铅笔。'
    assert complete_tail('the dog') == ''


def test_explicit_passage_facts_override_world_assumptions_without_guessing():
    colour=resolve_voice_reply(conversation('Read "The number twelve is blue." What colour is the number twelve? 请用中文回答。'))
    assert '蓝色' in colour.text and colour.source=='grounded_passage'
    owner=resolve_voice_reply(conversation('Text: "The pencil belongs to Lily." Who owns the pencil? 请用中文回答。'))
    assert 'Lily' in owner.text
    for question in ['Read "The hat is not red." What colour is the hat? 请用中文回答。',
                     'Read "The hat is red." What colour is the bag? 请用中文回答。',
                     'Read "The red bag is on the hat." What colour is the hat? 请用中文回答。',
                     'Read "The hat is red and blue." What colour is the hat? 请用中文回答。']:
        assert resolve_voice_reply(conversation(question)) is None


@pytest.mark.parametrize('task', ['Give one English example.', 'Then make one English sentence.',
                                  'Give an example in English.', '再用这个词造句。'])
def test_compound_definition_never_short_circuits_the_example(task):
    assert resolve_voice_reply(conversation('What does thirsty mean in Chinese? '+task)) is None


def test_example_repair_preserves_count_and_does_not_translate_english(monkeypatch):
    from backend import local_llm
    from backend.voice_grounding import ensure_chinese_reply
    calls=[]
    async def repair(messages, **kwargs):
        calls.append(messages)
        return '{"definition":"hungry 的意思是饿的。","examples":["I am hungry.","My cat is hungry."]}'
    monkeypatch.setattr(local_llm,'complete',repair)
    async def source(text):yield text
    async def run():
        query=conversation('请用中文解释 hungry，并给两个英语例句。')
        text=''.join([s async for s in ensure_chinese_reply(source('hungry 的意思是饿的。'),query)])
        assert "I am hungry." in text and 'My cat is hungry.' in text
        assert len(calls)==1
        calls.clear()
        assert ''.join([s async for s in ensure_chinese_reply(source(text),query)])==text
        assert not calls
        query=conversation('请用中文解释 hungry，并给一个例句，例句用英语。')
        text="hungry 的意思是饿的。I am hungry."
        assert ''.join([s async for s in ensure_chinese_reply(source(text),query)])==text
        assert not calls
    asyncio.run(run())


def test_parenthetical_translations_cannot_displace_a_requested_second_example(monkeypatch):
    from backend import local_llm
    from backend.voice_grounding import ensure_chinese_reply
    async def forbidden(*args,**kwargs):raise AssertionError('Both examples are already complete')
    monkeypatch.setattr(local_llm,'complete',forbidden)
    async def source():yield 'hungry 的意思是饿的。I am hungry.（我饿了。）My cat is hungry.（猫饿了。）'
    async def run():
        reply=''.join([s async for s in ensure_chinese_reply(source(),conversation('请用中文解释 hungry，给两个英语例句。'))])
        from backend.reply_sentences import sentences
        complete,tail=sentences(reply,final=True)
        assert len(complete)==3 and not tail.strip() and complete[-1]=='My cat is hungry.'
    asyncio.run(run())


@pytest.mark.parametrize(('question','meaning'),[
 ('Please translate the word sweater into Chinese.','毛衣'),
 ('请用中文解释单词 strawberry 的意思。','草莓'),
 ('我想知道 scarf 这个单词的中文意思。','围巾'),
 ('What is the Chinese meaning of lemon in this question?','柠檬'),
])
def test_definition_wrappers_are_not_part_of_the_headword(question,meaning):
    assert meaning in resolve_voice_reply(conversation(question)).text


def test_requested_examples_keep_question_sentences_and_numerals():
    from backend.english_chat import _ReplyGuard,grade_profile
    guard=_ReplyGuard(grade_profile(3),voice=True,stop_at_question=False)
    assert guard.accept('cold 的意思是冷的。')
    assert guard.accept('Is it cold?') and not guard.finished
    assert guard.accept('I have 2 cold drinks.')


def test_unknown_word_cannot_be_given_invented_examples():
    reply=resolve_voice_reply(conversation('What does zorbif mean in Chinese? Give an example in English.'))
    assert reply.source=='grounded_glossary_unknown' and '暂时无法' in reply.text


@pytest.mark.parametrize('text',['请用中文解释，并给一个英语例句。',
 '请用中文解释的意思，再用英语造一个句子。','请解释的中文意思，并用它写一个英语句子。'])
def test_missing_asr_word_is_clarified_instead_of_invented(text):
    reply=resolve_voice_reply(conversation(text))
    assert reply.source=='grounded_missing_word' and '没有听清' in reply.text


def test_complete_word_request_is_not_mistaken_for_an_empty_slot():
    assert resolve_voice_reply(conversation('请用中文解释 happy，并给一个英语例句。')) is None


def test_chinese_which_question_without_asr_question_mark_cannot_promote_a_like():
    reply=resolve_voice_reply(conversation('我喜欢企鹅。','我最喜欢哪一种动物,请用中文告诉我。'))
    assert reply.source=='grounded_preference' and '还没有明确' in reply.text


def test_one_explicit_chinese_story_age_is_copied_without_an_extra_age():
    reply=resolve_voice_reply(conversation('故事里的汤姆十二岁,他多大,请用中文回答。'))
    assert reply.source=='grounded_story_age' and reply.text=='汤姆是十二岁。'
    for q in ['故事里的汤姆十二岁，他的爸爸多大？请用中文回答。',
              '故事里的汤姆不是十二岁，他多大？请用中文回答。',
              '故事里的汤姆十二岁，他三年后多大？请用中文回答。']:
        assert not (resolve_voice_reply(conversation(q)) or type('R',(),{'source':''})()).source=='grounded_story_age'
    assert resolve_voice_reply(conversation('英语单词的中文意思是什么？')).source=='grounded_missing_word'
