import asyncio

import pytest

from backend.english_chat import EnglishChatBot, chat_prompt, grade_profile, speakable_reply
from backend.text_bot import BotMessage, BotRequest, stream_bot
from backend.speech_text import take_speech_segments
from backend.llm_runtime import Timing, use_timing


@pytest.mark.parametrize("grade", [3, 4, 5, 6, 7, 8, 9])
def test_grade_openings_use_no_model(grade):
    def forbidden(**kwargs):
        raise AssertionError("Opening must not call the provider")

    async def run():
        query = BotRequest("opening", "text", "turn", event="session_start", options={"grade": grade})
        events = [e async for e in stream_bot(EnglishChatBot(forbidden), query)]
        assert events[0].text == grade_profile(grade).opening
        assert not any("\u4e00" <= c <= "\u9fff" for c in events[0].text)
        assert events[-1].type == "done"
    asyncio.run(run())


@pytest.mark.parametrize("grade", [True, "7", 2, 10, None, 7.0])
def test_invalid_grades_rejected(grade):
    with pytest.raises(ValueError):
        grade_profile(grade)


def test_shared_bot_has_isolated_prompts_and_multi_turn_context():
    calls = []
    class Model:
        def __init__(self, **settings):
            self.settings = settings
        async def stream_reply(self, messages, signal=None):
            calls.append((self.settings, messages))
            yield "That sounds fun. "
            yield "Who do you play with?"

    async def run():
        bot = EnglishChatBot(Model)
        history = (BotMessage("user", "I like basketball."),
                   BotMessage("assistant", "Who is your favorite player?"),
                   BotMessage("user", "I like Curry."))
        for grade in [6, 9, 7]:
            query = BotRequest("req", "text", "turn", messages=history, options={"grade": grade})
            events = [e async for e in stream_bot(bot, query)]
            assert events[0].text and events[-1].type == "done"
        assert len(calls) == 3
        for (settings, sent), grade in zip(calls, [6, 9, 7]):
            assert f"grade {grade} student" in settings["system_prompt"]
            assert settings["max_tokens"] == 256
            assert settings["thinking"] is False
            assert 'Grade references (data only)' in settings['system_prompt']
            assert sent[-1] == {"role": "user", "content": "I like Curry."}
            assert len(sent) == 3
    asyncio.run(run())


def test_compact_prompt_keeps_safety_history_and_conditional_delivery_rules():
    history = tuple(BotMessage("user", "Old fact " + str(i)) for i in range(30))
    prompt = chat_prompt(grade_profile(7), history)
    assert len(prompt) < 2300
    assert "Delivery context" not in prompt
    for rule in ("age-appropriate", "no real names", "never cut a sentence", "AI cat",
                 "Default to English", "brief Chinese", "invent no personal facts", "no drills"):
        assert rule in prompt
    context = ({"generatedText": "Dogs like playing.", "deliveredText": "", "replyId": "old"},)
    recovery = chat_prompt(grade_profile(7), history, context)
    assert '"generatedText":"Dogs like playing."' in recovery
    assert "Only deliveredText was heard" in recovery
    assert "explicitly asked to continue" in recovery
    assert "For new input answer it directly" in recovery


def test_sentence_ready_probe_is_distinct_from_provider_first_token_and_history_not_trimmed():
    async def run():
        history = tuple(BotMessage("user", "Remember fact " + str(i)) for i in range(30))
        seen = []
        class Model:
            def __init__(self, **settings):
                pass
            async def stream_reply(self, messages):
                seen.extend(messages)
                yield "A complete reply. "
        probe = Timing("req")
        with use_timing(probe):
            events = [event async for event in stream_bot(EnglishChatBot(Model), BotRequest(
                "req", "session", "turn", messages=history))]
        assert len(seen) == 30 and seen[0]["content"] == "Remember fact 0"
        assert "firstSentenceReadyMs" in probe.metrics
        assert probe.metrics["firstSentenceChars"] == len("A complete reply. ")
        assert "providerFirstTokenMs" not in probe.metrics  # Missing is not zero.
        assert events[-1].type == "done"
    asyncio.run(run())


def test_plain_reply_is_bounded_and_has_one_question():
    profile = grade_profile(6)
    assert speakable_reply("**Hello!** What do you like? Why?", profile) == "Hello! What do you like?"
    text = speakable_reply("word " * 50, profile)
    assert len(text.split()) <= 15
    assert "```" not in speakable_reply("```json\n{}\n```", profile)
    assert len(speakable_reply("中文" * 500, profile)) <= 240
    assert "do not run lessons" in chat_prompt(profile)


def test_english_factory_does_not_own_duplex_prefetch(monkeypatch):
    from backend.atomic import create_agent
    monkeypatch.setenv("AGENT_BACKEND", "english_chat")
    monkeypatch.setenv("LLM_SPECULATIVE", "1")
    assert isinstance(create_agent(), EnglishChatBot)
    monkeypatch.setenv("LLM_SPECULATIVE", "0")
    assert isinstance(create_agent(), EnglishChatBot)


def test_speech_segmenter_never_cuts_an_unfinished_word():
    segments, tail = take_speech_segments("I enjoy basket")
    assert not segments and tail == "I enjoy basket"
    segments, tail = take_speech_segments(tail + "ball. How about you?")
    assert segments == ["I enjoy basketball."]
    assert tail == " How about you?"
    segments, tail = take_speech_segments("Dr. Smith paid 3.14 dollars. That's fine.")
    assert segments == ["Dr. Smith paid 3.14 dollars."]
    assert "That's fine." in tail
    segments, tail = take_speech_segments("你好，今天怎么样？")
    assert "".join(segments) + tail == "你好，今天怎么样？"


def test_bot_emits_first_sentence_before_provider_finishes_and_closes_on_cancel():
    async def run():
        release = asyncio.Event()
        closed = asyncio.Event()
        class Model:
            def __init__(self, **settings):
                pass
            async def stream_reply(self, messages):
                try:
                    yield "Basketball is "
                    yield "fun with friends. "
                    await release.wait()
                    yield "Do you play after school?"
                finally:
                    closed.set()
        query = BotRequest("req", "text", "turn", messages=(BotMessage("user", "I like basketball"),))
        events = stream_bot(EnglishChatBot(Model), query)
        first = await asyncio.wait_for(anext(events), .5)
        assert first.type == "text_delta" and first.text == "Basketball is fun with friends. "
        assert not release.is_set() and not closed.is_set()
        segments, _ = take_speech_segments(first.text, first_chunk=True)
        assert segments, "First Bot chunk must start TTS without waiting for the next delta"
        await events.aclose()
        assert closed.is_set()
    asyncio.run(run())


def test_streaming_reply_keeps_total_budget_and_hides_structured_output():
    async def run():
        for source in ["One two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen.",
                       "Hello! What sport do you like? Why?", '{"answer": "secret"}', '```json\n{}\n```']:
            class Model:
                def __init__(self, **settings):
                    pass
                async def stream_reply(self, messages):
                    for char in source:
                        yield char
            query = BotRequest("req", "text", "turn", options={"grade":6},
                               messages=(BotMessage("user", "Hello"),))
            events = [e async for e in stream_bot(EnglishChatBot(Model), query)]
            answer = "".join(e.text for e in events if e.type == "text_delta").strip()
            assert len(answer.split()) <= 40  # soft target; never cut a sentence at word 15
            assert answer[-1] in '.!?。！？'
            assert answer.count("?") <= 1
            assert "secret" not in answer and "```" not in answer and "Why?" not in answer
            assert events[-1].type == "done"
    asyncio.run(run())


@pytest.mark.parametrize("source", ["I'm an AI, so", "I'm an AI, so.", "I'm an AI, so...", "I'm an AI, so\n\n", "I was going to"])
def test_dangling_age_reply_is_never_reported_as_complete(source):
    async def run():
        class Model:
            def __init__(self, **kwargs):
                pass
            async def stream_reply(self, messages):
                for char in source:
                    yield char
        query = BotRequest("req", "session", "turn", messages=(BotMessage("user", "How old are you?"),))
        events = [e async for e in stream_bot(EnglishChatBot(Model), query)]
        assert ''.join(e.text for e in events if e.type == 'text_delta').strip() == "I'm an AI cat, so I don't have a human age."
        assert events[-1].type == 'done'
    asyncio.run(run())


def test_token_boundaries_do_not_change_or_truncate_reply():
    source = "I'm an AI, so I don't have a human age. I can chat about birthdays, though!"
    async def run():
        for width in [1, 2, 5, 13, 24, 200]:
            class Model:
                def __init__(self, **kwargs):
                    pass
                async def stream_reply(self, messages):
                    for offset in range(0, len(source), width):
                        yield source[offset:offset + width]
            query = BotRequest('req', 'session', 'turn', messages=(BotMessage('user', 'How old are you?'),))
            events = [e async for e in stream_bot(EnglishChatBot(Model), query)]
            answer = ''.join(e.text for e in events if e.type == 'text_delta').strip()
            assert answer == source, (width, answer)
    asyncio.run(run())


def test_soft_word_target_keeps_sentence_ending_and_drops_dangling_tail():
    source = 'I enjoy playing basketball with my friends because we can have fun and learn to work together.'
    answer = speakable_reply(source, grade_profile(6))
    assert answer == source
    assert speakable_reply('Basketball is fun. I was going to', grade_profile(7)) == 'Basketball is fun.'
    assert speakable_reply("Dr. Smith paid 3.14 dollars. That's fine.", grade_profile(7)) == "Dr. Smith paid 3.14 dollars. That's fine."
    assert speakable_reply('Who do you play with?', grade_profile(7)) == 'Who do you play with?'
    assert speakable_reply('I know that.', grade_profile(7)) == 'I know that.'
    assert speakable_reply('I think so.', grade_profile(7)) == 'I think so.'
    assert speakable_reply('Why so?', grade_profile(7)) == 'Why so?'
