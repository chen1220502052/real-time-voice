import asyncio
import json
from types import SimpleNamespace
from dataclasses import replace

from aiohttp import ClientSession, web
import pytest

from backend.app import create_app
from backend.curriculum_chat import CurriculumChatBot, audit_reply, chat_scope, validate_chat_options
from backend.text_bot import BotMessage, BotRequest, TextBotError, stream_bot
from backend.writing_vocabulary import VocabularyScope


def make_scope():
    chapters = {}
    for grade in (4, 5, 6):
        for term in ("上册", "下册"):
            for n in range(1, 13):
                key = f"{grade}-{term}-{n}"
                word = "previous" if grade == 4 else "current" if term == "上册" and n <= 10 else "future"
                chapters[key] = {"id": key, "publisher": "科普版", "grade": f"{grade}年级",
                    "edition": term + "_新版", "unit": f"Unit {n}", "textKind": "原创补充",
                    "vocabulary": [{"word": word}, {"word": "play"}, {"word": "like"}]}
    return VocabularyScope(SimpleNamespace(chapters=chapters), "5-上册-10")


class Agent:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []

    async def ainvoke(self, state, config):
        self.calls.append(state)
        return {"messages": [*state["messages"], SimpleNamespace(content=next(self.replies))]}


def query(**kwargs):
    return BotRequest("req", "session", "turn", options={"grade": 5, "chapterId": "5-上册-10"},
                      messages=(BotMessage("user", "Tell me about the future."),), **kwargs)


def test_default_text_transport_supports_extension_without_a_textbook():
    async def run():
        from backend.atomic import DemoTts, VadAsrStub
        app = create_app(asr_factory=VadAsrStub, tts_factory=DemoTts, warmup=False,
                         voice_enabled=False, bot_state_path=":memory:")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        opening = {"version":2, "requestId":"opening", "sessionId":"extension", "turnId":"t1",
                   "event":"session_start", "channel":"text", "options":{"grade":6}}
        try:
            async with ClientSession() as client:
                async with client.post(base + "/api/text-bot/replies", json=opening) as response:
                    assert response.status == 200
                    events = [json.loads(line) for line in (await response.text()).splitlines()]
                    assert events[-1]["type"] == "done"
                    assert "favorite animal" in events[0]["text"]
                goodbye = {**opening, "requestId":"bye", "turnId":"t2",
                           "event":"user_message", "text":"Goodbye"}
                async with client.post(base + "/api/text-bot/replies", json=goodbye) as response:
                    assert response.status == 200
                    events = [json.loads(line) for line in (await response.text()).splitlines()]
                    assert any(e["type"] == "action" and e["action"] == "hang_up" for e in events)
                    assert events[-1]["type"] == "done"
                for options in ({}, {"grade":True}, {"grade":6, "chapterId":""}, {"grade":6, "chapterId":"missing"}):
                    async with client.post(base + "/api/text-bot/replies", json={**opening, "options":options}) as response:
                        assert response.status == 400
                assert app["resource_manager"].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_grade5_unit10_boundary_and_short_reply_policy():
    scope = make_scope()
    assert len(scope.sources) == 34
    assert {"previous", "current"} <= scope.words
    assert "future" not in scope.words
    assert audit_reply(scope, "I like current.")["passed"]
    assert not audit_reply(scope, "I like future.")["passed"]
    assert audit_reply(scope, "I play. " * 17 + "Future.")["passed"]
    assert not audit_reply(scope, "I play. " * 17 + "Future future.")["passed"]
    assert not audit_reply(scope, "I play. " * 17 + "Future mystery.")["passed"]
    assert audit_reply(scope, "I played.")["passed"]
    assert not audit_reply(scope, "I like Tom.")["passed"]


def test_fallback_has_one_call_no_vocabulary_rewrite_and_keeps_history():
    async def run():
        agent = Agent("I like future.")
        scope = make_scope()
        bot = CurriculumChatBot(lambda s: agent, lambda _: scope)
        events = [e async for e in stream_bot(bot, query())]
        assert [e.type for e in events] == ["text_delta", "done"]
        assert events[0].text == "I like future."
        assert not audit_reply(scope, events[0].text)['passed']
        assert len(agent.calls) == 1
        assert agent.calls[0]["messages"] == [query().messages[0].to_dict()]
    asyncio.run(run())


@pytest.mark.parametrize("content", ['{"reply":"I play."}', "```I play.```", [{"type": "text", "text": "I play."}], "I was going to"])
def test_invalid_model_output_errors_without_repair(content):
    async def run():
        agent = Agent(content)
        bot = CurriculumChatBot(lambda s: agent, lambda _: make_scope())
        with pytest.raises(TextBotError) as error:
            _ = [e async for e in bot.stream(query())]
        assert error.value.code == 'CHAT_INVALID_REPLY'
        assert len(agent.calls) == 1
    asyncio.run(run())


def test_opening_idle_and_goodbye_do_not_call_model_or_leak_outside_words():
    def forbidden(scope):
        raise AssertionError("Fixed events do not need a model")

    async def run():
        scope = make_scope()
        bot = CurriculumChatBot(forbidden, lambda _: scope)
        for event in ("session_start", "time_out", "hang_up"):
            request = BotRequest("req", "session", "turn", event=event,
                                 options={"chapterId": "5-上册-10"})
            events = [e async for e in bot.stream(request)]
            assert audit_reply(scope, events[0].text)["outsideCount"] == 0
            assert events[-1].type == "done"
            assert any(e.type == "action" for e in events) == (event == "hang_up")
    asyncio.run(run())


def test_cancellation_reaches_agent_before_any_draft_is_visible():
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()
        class Slow:
            async def ainvoke(self, *args, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()
        bot = CurriculumChatBot(lambda s: Slow(), lambda _: make_scope())
        iterator = stream_bot(bot, query())
        task = asyncio.create_task(anext(iterator))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    asyncio.run(run())


def test_voice_releases_stable_words_before_sentence_while_text_waits():
    async def run():
        release = asyncio.Event()
        class Streaming:
            async def astream(self, *args, **kwargs):
                yield SimpleNamespace(content='Cats enjoy chasing butterflies '), {'langgraph_node':'model'}
                await release.wait()
                yield SimpleNamespace(content='outside with friends.'), {'langgraph_node':'model'}
        bot = CurriculumChatBot(lambda _:Streaming(),lambda _:make_scope())
        stream = stream_bot(bot,replace(query(),channel='voice'))
        first = await asyncio.wait_for(anext(stream),.5)
        assert first.text=='Cats enjoy chasing butterflies '
        release.set();rest=[e async for e in stream]
        assert ''.join(e.text for e in [first,*rest]).strip()=='Cats enjoy chasing butterflies outside with friends.'
        assert rest[-1].type=='done'
    asyncio.run(run())


@pytest.mark.parametrize('width',[1,3,11,200])
def test_voice_phrase_chunks_preserve_exact_final_answer(width):
    text='My name is Kitty. I enjoy chatting with you!'
    class Streaming:
        async def astream(self,*args,**kwargs):
            for i in range(0,len(text),width):
                yield SimpleNamespace(content=text[i:i+width]),{'langgraph_node':'model'}
    async def run():
        bot=CurriculumChatBot(lambda _:Streaming(),lambda _:make_scope())
        events=[e async for e in stream_bot(bot,replace(query(),channel='voice'))]
        assert ''.join(e.text for e in events).strip()==text
        assert events[-1].type=='done'
    asyncio.run(run())


def test_voice_cannot_report_success_after_streaming_an_incomplete_tail():
    class Streaming:
        async def astream(self,*args,**kwargs):
            yield SimpleNamespace(content='I was going to '),{'langgraph_node':'model'}
    async def run():
        bot=CurriculumChatBot(lambda _:Streaming(),lambda _:make_scope())
        events=[]
        with pytest.raises(TextBotError,match='mid-sentence'):
            async for event in stream_bot(bot,replace(query(),channel='voice')):events.append(event)
        assert events and not any(e.type=='done' for e in events)
    asyncio.run(run())


def test_voice_context_keeps_delivered_history_and_latest_user_in_order():
    async def run():
        agent=Agent('Cats are fun.')
        history=(BotMessage('user','I like cats.'),BotMessage('assistant','Do you have a cat?'),
                 BotMessage('user','Why?'))
        delivery=({'replyId':'old','generatedText':'Do you have a cat? What is its name?',
                   'deliveredText':'Do you have a cat?','status':'interrupted'},)
        request=replace(query(),channel='voice',messages=history,delivery_context=delivery)
        bot=CurriculumChatBot(lambda _:agent,lambda _:make_scope())
        _=[e async for e in stream_bot(bot,request)]
        sent=agent.calls[0]['messages']
        assert sent[:2]==[m.to_dict() for m in history[:2]]
        assert sent[-1]==history[-1].to_dict()
        notes=' '.join(m['content'] for m in sent[2:-1])
        assert 'I like cats.' in notes and 'deliveredText' in notes
        assert 'What is its name?' in notes  # Unheard continuation remains available.
        assert 'recent_replies' not in notes
    asyncio.run(run())


@pytest.mark.parametrize('latest,keeps_draft',[('I want to be a teacher.',False),('Continue.',True),('接着说',True)])
def test_voice_new_input_does_not_receive_unheard_drafts_but_resume_does(latest,keeps_draft):
    async def run():
        agent=Agent('Teachers help kids learn.')
        heard='A teacher helps kids.';unheard='An astronaut travels in space.'
        history=(BotMessage('user','Tell me about jobs.'),BotMessage('assistant',heard),BotMessage('user',latest))
        request=replace(query(),channel='voice',messages=history,delivery_context=({'replyId':'old',
            'generatedText':heard+' '+unheard,'deliveredText':heard,'status':'interrupted'},))
        bot=CurriculumChatBot(lambda _:agent,lambda _:make_scope())
        _=[e async for e in stream_bot(bot,request)]
        sent=agent.calls[0]['messages'];notes=' '.join(m['content'] for m in sent if m['role']=='system')
        assert (unheard in notes)==keeps_draft
        assert sent[:2]==[m.to_dict() for m in history[:2]] and sent[-1]==history[-1].to_dict()
    asyncio.run(run())


def test_real_unit_selection_and_http_rejections_before_acceptance():
    async def run():
        app = create_app(warmup=False, voice_enabled=False, bot_state_path=":memory:")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                catalog = await (await client.get(url + "/api/practice/catalog")).json()
                chapter = next(c for c in catalog["chapters"] if c["grade"] == "5年级"
                    and c["publisher"] == "科普版" and c["edition"].startswith("上册")
                    and c["unit"].startswith("Unit 10 "))
                scope = chat_scope(chapter["id"])
                assert len(scope.sources) == 58
                assert validate_chat_options({"grade": 5, "chapterId": chapter["id"]}).chapter == scope.chapter
                response = await client.get(url + "/api/text-bot/scopes/" + chapter["id"])
                assert response.status == 200
                summary = await response.json()
                assert summary["vocabularySize"] == len(scope.words)
                assert summary["verified"]
                assert summary["chatPolicy"]["fallbackVocabularyLimit"] is False
                for options in ({}, {"grade": 2}, {"chapterId": "missing"},
                    {"grade": 6, "chapterId": chapter["id"]}, {"grade": True, "chapterId": chapter["id"]},
                    {"chapterId": chapter["id"], "allowedWords": ["future"]}):
                    for version in (1, 2):
                        body = {"version": version, "requestId": "bad", "sessionId": "bad", "turnId": "bad",
                                "event": "session_start", "options": options}
                        response = await client.post(url + "/api/text-bot/replies", json=body)
                        assert response.status == 400, options
                response = await client.post(url + "/api/text-bot/replies", json={
                    "version": 2, "requestId": "open", "sessionId": "selected", "turnId": "open",
                    "event": "session_start", "channel": "text", "options": {"grade": 5, "chapterId": chapter["id"]}})
                assert response.status == 200
                events = [json.loads(s) for s in (await response.text()).splitlines()]
                assert [e["type"] for e in events] == ["text_delta", "done"]
                assert audit_reply(scope, events[0]["text"])["outsideCount"] == 0
                assert app["resource_manager"].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_real_deepagents_graph_disables_tools_and_streams_unrestricted_fallback(monkeypatch):
    pytest.importorskip("deepagents")
    deepseek = pytest.importorskip("langchain_deepseek")
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from backend.curriculum_chat import build_chat_agent

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            assert not tools
            return self
    model = ToolModel(responses=[AIMessage(content="Space travel can be exciting.")])
    monkeypatch.setenv("LLM_API_KEY", "unit-test-placeholder")
    monkeypatch.setattr(deepseek, "ChatDeepSeek", lambda **kwargs: model)
    async def run():
        bot = CurriculumChatBot(build_chat_agent, lambda _: make_scope())
        events = [e async for e in bot.stream(query())]
        assert ''.join(e.text for e in events if e.type=='text_delta').strip()=='Space travel can be exciting.'
    asyncio.run(run())


def test_chinese_only_help_is_allowed_only_when_explicitly_requested():
    scope = make_scope()
    assert not audit_reply(scope, "我们可以用简单的话说。")['passed']
    assert audit_reply(scope, "我们可以用简单的话说。", allow_chinese_only=True)['passed']
    assert not audit_reply(scope, "future 是未来。", allow_chinese_only=True)['passed']
    async def run():
        agent = Agent("太空旅行就是乘坐飞船去太空。")
        bot = CurriculumChatBot(lambda s: agent, lambda _: scope)
        q = BotRequest('req', 'session', 'turn', options={'grade':5,'chapterId':'5-上册-10'},
                       messages=(BotMessage('user', '没听懂，请用中文解释。'),))
        events = [e async for e in bot.stream(q)]
        assert events[0].text == "太空旅行就是乘坐飞船去太空。"
    asyncio.run(run())


def test_common_irregular_past_forms_require_their_known_base():
    from backend.writing_vocabulary import known
    words = {'tell', 'speak', 'say', 'think', 'know', 'feel'}
    assert all(known(w, words) for w in ['told', 'spoke', 'spoken', 'said', 'thought', 'knew', 'known', 'felt'])
    assert not known('spoken', {'play'})


def test_retrieval_bypasses_model_and_records_route():
    from backend.chat_library import ReplyMatch
    from backend.llm_runtime import Timing, use_timing
    class Repository:
        def lookup(self, scope, text, messages):
            return ReplyMatch('I play.', 'textbook', 'answer1', scope.chapter['id'])
    def forbidden(scope):
        raise AssertionError('A cache hit must not construct a model')
    async def run():
        probe=Timing('req')
        with use_timing(probe):
            events=[e async for e in CurriculumChatBot(forbidden,lambda _:make_scope(),Repository()).stream(query())]
        assert events[0].text=='I play.' and events[-1].type=='done'
        assert probe.metrics['chatReplySource']=='textbook'
    asyncio.run(run())


def test_fallback_emits_first_sentence_before_model_finishes_and_closes_on_cancel():
    closed=asyncio.Event()
    class Streaming:
        async def astream(self,*args,**kwargs):
            try:
                yield SimpleNamespace(content='Space travel is exciting. ',tool_calls=[]),{'langgraph_node':'model'}
                await asyncio.Event().wait()
            finally:
                closed.set()
    async def run():
        bot=CurriculumChatBot(lambda _:Streaming(),lambda _:make_scope())
        iterator=stream_bot(bot,query())
        first=await asyncio.wait_for(anext(iterator),.5)
        assert first.text=='Space travel is exciting. '
        await iterator.aclose()
        assert closed.is_set()
    asyncio.run(run())


@pytest.mark.parametrize("grade,chapter_id", [(5,"d2435143bdba0792312a2ecc"),
    (3,"8fcc72e435dc65cc5e034915")])
def test_websocket_selected_unit_propagates_and_uses_curriculum_bot(grade, chapter_id):
    from backend.atomic import DemoTts, VadAsrStub
    async def run():
        seen=[]
        class UnitBot:
            protocol_version=1
            async def stream(self,request):
                seen.append(request)
                from backend.text_bot import BotEvent
                yield BotEvent(request.request_id,'text_delta',text='Hi!')
                yield BotEvent(request.request_id,'done')
        app=create_app(asr_factory=VadAsrStub,tts_factory=DemoTts,text_bot_factory=UnitBot,
                       voice_enabled=True,warmup=False,bot_state_path=':memory:')
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        try:
            async with ClientSession() as client:
                # The browser resolves the unit before requesting microphone admission.
                response=await client.get(url+'/api/text-bot/scopes/'+chapter_id)
                assert response.status==200
                await response.read()
                async with client.ws_connect(url+'/ws') as ws:
                    async def until(kind):
                        async with asyncio.timeout(5):
                            async for msg in ws:
                                if msg.type==1:
                                    data=json.loads(msg.data)
                                    if data.get('type')==kind:return data
                    await ws.send_json({'type':'prepare'})
                    assert (await until('prepare_result'))['ok']
                    await ws.send_json({'type':'create_session','grade':grade,'chapterId':chapter_id})
                    created=await until('create_session_result')
                    assert created['ok'] and created['grade']==grade
                    assert created['chapterId']==chapter_id
                    await until('reply_complete')
                    assert seen[0].options['chapterId']==created['chapterId']
                    assert seen[0].options['grade']==grade and seen[0].channel=='voice'
                    await ws.send_json({'type':'close_session'})
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_repeated_cached_followup_uses_contextual_generation():
    from backend.chat_library import ReplyMatch
    class Repository:
        def lookup(self,*args):
            return ReplyMatch('Do you like play?', 'prepared', 'q1', None)
    agent=Agent('I remember that.')
    async def run():
        q=BotRequest('req','session','turn',options={'grade':5,'chapterId':'5-上册-10'},
                     messages=(BotMessage('assistant','Do you like play?'),BotMessage('user','Yes.')))
        events=[e async for e in CurriculumChatBot(lambda _:agent,lambda _:make_scope(),Repository()).stream(q)]
        assert events[0].text=='I remember that.' and len(agent.calls)==1
    asyncio.run(run())
