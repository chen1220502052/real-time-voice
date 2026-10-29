import asyncio
import json
import sqlite3
from types import SimpleNamespace
import pytest
from backend.dialogue_policy import project_state, sharing_kind, policy_reply
from backend.dialogue_store import DialogueStore
from backend.curriculum_chat import CurriculumChatBot
from backend.text_bot import BotMessage, BotRequest

WORDS = set('cats cat dogs dog or do you like apples apple food games game rain they sleep much stars shine because make light what eat rice bread yes no'.split())
def scope():
    return SimpleNamespace(chapter={'id':'unit','grade':'5年级','unit':'Unit 10'},words=WORDS)
def history(*lines):
    return tuple(BotMessage('user' if i%2==0 else 'assistant',t) for i,t in enumerate(lines))
def request(lines, rid='r', sid='s'):
    return BotRequest(rid,sid,rid,messages=lines,options={'grade':5,'chapterId':'unit'})
class Agent:
    def __init__(self, text): self.text,self.calls=text,[]
    async def ainvoke(self, state, config):
        self.calls.append(state)
        return {'messages':[{'content':self.text}]}


def test_repeats_change_action_and_never_repeat_recent_question():
    lines=history('I like cats.','Do you have a cat?','I like cats.')
    state=project_state(lines)
    assert state.repeat_count==2 and state.action=='simplify_question'
    assert policy_reply(state,scope())=='Cats or dogs?'
    lines+= (BotMessage('assistant','Cats or dogs?'),BotMessage('user','I like cats.'))
    state=project_state(lines)
    assert state.action=='offer_topics' and policy_reply(state,scope())=='Do you like apples?'
    lines+=(BotMessage('assistant','Do you like apples?'),BotMessage('user','I like cats.'))
    assert policy_reply(project_state(lines),scope())=='Do you like games?'


def test_followup_switch_and_practice():
    assert project_state(history('I like cats.','They sleep much.','Why?')).action=='follow_up'
    state=project_state(history('I like cats.','Do you like apples?','Yes.'))
    assert state.action=='follow_up' and state.topic=='food'
    state=project_state(history('I like cats.','Cats or dogs?','Let us talk about food.'))
    assert state.action=='switch_topic' and state.topic=='food' and state.previous_topic=='animals'
    state=project_state(history('Help me practice I like cats.','I like cats.','I like cats.','Good!','I like cats.'))
    assert state.action=='practice' and policy_reply(state,scope()) is None
    assert project_state(history('Help me practice I like cats.','I like cats.','What is rain?')).action=='switch_topic'
    assert project_state(history('Help me practice I like cats.','I like cats.','Stop practicing.')).action=='answer'
    assert project_state(history('I like cats.','Cats or dogs?','I like dogs.')).repeat_count==1

@pytest.mark.parametrize('q,a,kind',[
    ('Why do stars shine?','Stars make light.','general'),
    ('What is my pet named?','Your pet is named Luna.','personal'),
    ('What is rain?','Luna likes rain.','contextual'),
    ('What is rain?','Call 12345678.','contextual'),
    ('What is rain?','Rain is water. Tell me your school.','personal'),
    ('Why?','They make light.','contextual'),
    ('My phone is 12345678.','Thanks.','personal'),
])
def test_sharing_boundary(q,a,kind):
    assert sharing_kind(q,a,history(q))==kind


def test_public_question_after_personal_or_unknown_context_stays_private():
    assert sharing_kind('What is rain?','Rain is water.',history('My pet is named Luna.','Nice!','What is rain?'))=='personal'
    assert sharing_kind('What is rain?','Rain is water.',history('Bob likes cats.','Okay.','What is rain?'))=='contextual'


def test_store_survives_restart_is_idempotent_and_isolates_sessions_and_units(tmp_path):
    path=tmp_path/'dialogue.sqlite3';store=DialogueStore(path)
    q=request(history('Why do stars shine?'))
    state=project_state(q.messages)
    assert store.record(q,state,'Stars make light.','generated')=='general'
    store.record(q,state,'Stars shine.','generated')
    other=request(history('What is my pet named?'),'p','personal')
    store.record(other,project_state(other.messages),'Your pet is named Luna.','generated')
    store=DialogueStore(path)
    assert store.lookup(scope(),q.messages[-1].content,state,q.messages).text=='Stars make light.'
    s=scope();s.chapter=dict(s.chapter,id='future')
    assert store.lookup(s,q.messages[-1].content,state,q.messages) is None
    s=scope();s.words={'stars'}
    assert store.lookup(s,q.messages[-1].content,state,q.messages) is None
    assert store.lookup(scope(),'What is my pet named?',project_state(other.messages),other.messages) is None
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from dialogue_turns').fetchone()[0]==2
        assert db.execute('select count(*) from general_replies').fetchone()[0]==1
        assert json.loads(db.execute('select state from dialogue_turns where request_id="p"').fetchone()[0])['action']=='answer'
    assert path.stat().st_mode & 0o077 == 0


def test_bot_repeats_use_policy_without_model_and_save_state(tmp_path):
    async def run():
        store=DialogueStore(tmp_path/'db');agent=Agent('Should not run.')
        bot=CurriculumChatBot(lambda _:agent,lambda _:scope(),store=store)
        q=request(history('I like cats.','Do you have a cat?','I like cats.'))
        events=[e async for e in bot.stream(q)]
        assert events[0].text=='Cats or dogs?' and events[-1].type=='done'
        assert not agent.calls
        with store.connect() as db:
            source,sharing,state=db.execute('select source,sharing,state from dialogue_turns').fetchone()
        assert source=='policy' and sharing=='personal' and json.loads(state)['repeat_count']==2
    asyncio.run(run())


def test_bot_followups_and_topic_switch_retain_history_and_instructions(tmp_path):
    async def run():
        store=DialogueStore(tmp_path/'db')
        for rid,lines,answer,action in [
            ('a',history('My pet is named Luna.','Luna is a nice name.','What is my pet named?'),'Your pet is named Luna.','answer'),
            ('b',history('Tell me about cats.','They sleep much.','Why?'),'They need rest.','follow_up'),
            ('c',history('I like cats.','Cats or dogs?','Let us talk about food.'),'What food do you like?','switch_topic')]:
            agent=Agent(answer);bot=CurriculumChatBot(lambda _:agent,lambda _:scope(),store=store)
            events=[e async for e in bot.stream(request(lines,rid))]
            assert answer in ''.join(e.text for e in events)
            sent=agent.calls[0]['messages']
            assert sent[-1]['content']==lines[-1].content
            assert any(m['content']==lines[0].content for m in sent)
            if action!='answer':assert sent[0]['role']=='system' and 'Dialogue policy' in sent[0]['content']
        with store.connect() as db:
            assert db.execute('select count(*) from dialogue_turns').fetchone()[0]==3
            assert db.execute('select count(*) from general_replies').fetchone()[0]==0
    asyncio.run(run())


def test_completed_generation_is_cached_but_cancelled_draft_is_not(tmp_path):
    async def run():
        store=DialogueStore(tmp_path/'db');agent=Agent('Stars make light.')
        bot=CurriculumChatBot(lambda _:agent,lambda _:scope(),store=store)
        events=[e async for e in bot.stream(request(history('Why do stars shine?')))]
        assert events[-1].type=='done' and len(agent.calls)==1
        bot=CurriculumChatBot(lambda _:(_ for _ in ()).throw(AssertionError('cached')),lambda _:scope(),store=store)
        events=[e async for e in bot.stream(request(history('Why do stars shine?'),'r2','s2'))]
        assert events[0].text=='Stars make light.'
        agent=Agent('Your pet is named Luna. What food do you like?')
        bot=CurriculumChatBot(lambda _:agent,lambda _:scope(),store=store)
        stream=bot.stream(request(history('My pet is named Luna.'),'cancel'))
        assert (await anext(stream)).type=='text_delta'
        await stream.aclose()
        with store.connect() as db:
            assert db.execute('select count(*) from dialogue_turns where request_id="cancel"').fetchone()[0]==0
    asyncio.run(run())


def test_session_facts_survive_process_and_history_window_without_cross_session_leak(tmp_path):
    async def run():
        path=tmp_path/'db';store=DialogueStore(path)
        q=request(history('My pet is named Luna.'))
        store.record(q,project_state(q.messages),'That is a nice name.','generated')
        assert DialogueStore(path).session_facts('s','unit')==('My pet is named Luna.',)
        assert DialogueStore(path).session_facts('other','unit')==()
        assert DialogueStore(path).session_facts('s','different')==()
        agent=Agent('Your pet is named Luna.')
        bot=CurriculumChatBot(lambda _:agent,lambda _:scope(),store=DialogueStore(path))
        _=[e async for e in bot.stream(request(history('What is my pet named?'),'recall'))]
        assert 'My pet is named Luna.' in agent.calls[0]['messages'][0]['content']
        # Generic-looking questions in a session with personal facts stay private.
        agent=Agent('Stars make light.')
        bot=CurriculumChatBot(lambda _:agent,lambda _:scope(),store=DialogueStore(path))
        _=[e async for e in bot.stream(request(history('Why do stars shine?'),'generic'))]
        with store.connect() as db:
            assert db.execute('select sharing from dialogue_turns where request_id="generic"').fetchone()[0]=='personal'
            assert db.execute('select count(*) from general_replies').fetchone()[0]==0
    asyncio.run(run())


def test_request_ids_are_scoped_to_session_and_old_schema_migrates(tmp_path):
    path=tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE dialogue_turns (request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, chapter_id TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, source TEXT NOT NULL, sharing TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO dialogue_turns(request_id,session_id,chapter_id,question,answer,source,sharing,state) VALUES ('r','old','unit','My pet is named Luna.','Luna.','generated','personal','{}')")
    store=DialogueStore(path)
    q=request(history('Why do stars shine?'),rid='r',sid='new')
    store.record(q,project_state(q.messages),'Stars make light.','generated')
    store.record(q,project_state(q.messages),'Stars shine.','generated')
    with store.connect() as db:
        rows=db.execute('SELECT session_id,answer FROM dialogue_turns ORDER BY session_id').fetchall()
    assert rows==[('new','Stars make light.'),('old','Luna.')]
