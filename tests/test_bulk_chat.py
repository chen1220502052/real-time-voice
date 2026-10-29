import asyncio
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest
from backend.chat_library import ChatLibrary,DATA,_load_library
from backend.curriculum_chat import CurriculumChatBot
from backend.text_bot import BotMessage,BotRequest
from backend.writing_vocabulary import known,tokens
from scripts.build_chat_reply_pack import make_pack,sentence_pack

@pytest.fixture(scope='module')
def repository(tmp_path_factory):
    root=Path(__file__).resolve().parents[1]
    return ChatLibrary.build(_load_library(os.getenv('ENGLISH_RESOURCE_ROOT',root/'英语')),tmp_path_factory.mktemp('bulk')/'chat.sqlite3',
        bulk_path=DATA/'bulk-replies.json',sentences_path=DATA/'textbook-sentences.json')


def test_generated_packs_are_reproducible_and_sources_verified():
    assert json.loads((DATA/'bulk-replies.json').read_text())==make_pack()
    assert json.loads((DATA/'textbook-sentences.json').read_text())==sentence_pack()
    assert len(sentence_pack()['sentences'])==90


def test_all_eligible_answers_and_sentences_stay_inside_cumulative_vocabulary(repository):
    db=sqlite3.connect(repository.path)
    for chapter,reply in db.execute('SELECT e.chapter_id,a.reply FROM eligible_answers e JOIN answers a ON a.id=e.answer_id'):
        scope=repository.scope(chapter)
        assert all(known(w,scope.words) for w in tokens(reply)),(chapter,reply)
    for chapter,text,anchor in db.execute('SELECT e.chapter_id,s.sentence,s.chapter_id FROM eligible_sentences e JOIN textbook_sentences s ON s.id=e.sentence_id'):
        scope=repository.scope(chapter)
        assert anchor in {s['chapterId'] for s in scope.sources}
        assert all(known(w,scope.words) for w in tokens(text))
    db.close()


def test_multi_turn_context_negation_and_alternatives(repository):
    scope=repository.scope('d2435143bdba0792312a2ecc')
    start=repository.lookup(scope,'I like apples.')
    assert start and start.text.startswith('Do you like ')
    assert repository.lookup_context(scope,'Yes.',(BotMessage('assistant',start.text),),()).text=='What do you like to eat?'
    assert repository.lookup_context(scope,'No.',(BotMessage('assistant',start.text),),()).text=='What do you like to eat?'
    assert repository.lookup_context(scope,'Yes.',(BotMessage('assistant','What is your question?'),),()) is None
    negative=repository.lookup(scope,"I don't like apples.")
    assert negative.text=='What do you like to eat?' and negative.answer_id!=start.answer_id
    next_reply=repository.lookup_next(scope,'I like apples.',(),(start.text,))
    assert next_reply and next_reply.text!=start.text
    assert repository.lookup(scope,"I don't like apples, but I love dogs.") is None
    assert repository.lookup(scope,'Bananas.') is None


def test_no_future_book_quotes_and_no_forged_evidence(repository,tmp_path):
    first=repository.scope('0500d50509c2f1f6668a11d6')
    quote=repository.lookup(first,'Read a sentence from this unit.')
    assert quote and quote.chapter_id==first.chapter['id'] and quote.source=='textbook'
    later=repository.scope('d2435143bdba0792312a2ecc')
    assert repository.lookup(later,'Read a sentence from this unit.') is None
    assert repository.lookup(first,'I like basketball.') is None
    pack=sentence_pack();pack['sentences'][0]['text']='A forged line.'
    file=tmp_path/'bad.json';file.write_text(json.dumps(pack))
    with pytest.raises(ValueError,match='Unverified'):
        ChatLibrary.build(_load_library(os.getenv('ENGLISH_RESOURCE_ROOT',Path(__file__).resolve().parents[1]/'英语')),tmp_path/'bad.sqlite3',sentences_path=file)


def test_context_hit_never_calls_model_and_missing_textbook_is_truthful(repository):
    async def run():
        def forbidden(_):raise AssertionError('No model needed')
        scope=repository.scope('d2435143bdba0792312a2ecc')
        bot=CurriculumChatBot(forbidden,lambda _:scope,repository)
        for rid,messages,expected in [
            ('context',(BotMessage('user','I like apples.'),BotMessage('assistant','Do you like bananas?'),BotMessage('user','Yes.')),'What do you like to eat?'),
            ('book',(BotMessage('user','Read a sentence from this unit.'),),'暂时没有其他可用的本单元课文原句')]:
            q=BotRequest(rid,'s',rid,options={'grade':5,'chapterId':scope.chapter['id']},messages=messages)
            out=[e async for e in bot.stream(q)]
            assert out[-1].type=='done' and expected in ''.join(e.text for e in out)
    asyncio.run(run())
