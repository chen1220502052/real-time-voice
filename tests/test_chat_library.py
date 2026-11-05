import json
from types import SimpleNamespace

import pytest

from backend.chat_library import ChatLibrary, question_key, _known_forms
from backend.text_bot import BotMessage
from backend.writing_vocabulary import known


def build_fixture(tmp_path):
    rows={}
    for number,word in [(1,'play'),(2,'futureword')]:
        key=f'unit{number}'
        rows[key]={'id':key,'publisher':'教材A','grade':'4年级','edition':'上册_新版',
            'unit':f'Unit {number}','vocabulary':[], 'textKind':'教材',
            'curriculum':{'bookId':'book','series':'series','grade':4,'term':'上册',
                'unitNumber':number,'firstUnit':1,'lastUnit':2,
                'words':['i','you','like','good','morning','how','are','fine',word],
                'verified':True,'source':'test-source','sourcePages':[], 'method':'fixture','revision':False,
                'lessonEvidence':[{'sha256':'proof','sentences':['Good morning.','How are you?','I like play.']} ]}}
    textbook={'version':1,'answers':[
        {'chapterId':'unit1','questions':['Good morning.'],'reply':'Good morning.','evidenceSha256':'proof','reviewed':True,'source':'page 1'},
        {'chapterId':'unit1','questions':['How are you?'],'reply':'I like play.','evidenceSha256':'forged','reviewed':True,'source':'fake'},
    ]}
    prepared={'version':1,'answers':[
        {'questions':['Good morning.'],'reply':'I like play.','reviewed':True,'source':'prepared'},
        {'questions':['How are you?'],'reply':'I fine.','reviewed':True,'source':'prepared'},
        {'questions':['future please'],'reply':'Futureword.','chapterId':'unit2','reviewed':True,'source':'prepared'},
        {'questions':['hidden please'],'reply':'I like play.','reviewed':False,'source':'not reviewed'},
        {'questions':['yes'],'contexts':['How are you?'],'reply':'Good morning.','reviewed':True,'source':'context'},
    ]}
    tp=tmp_path/'textbook.json';pp=tmp_path/'prepared.json'
    tp.write_text(json.dumps(textbook));pp.write_text(json.dumps(prepared))
    library=SimpleNamespace(chapters=rows)
    repo=ChatLibrary.build(library,tmp_path/'chat.sqlite3',textbook_path=tp,prepared_path=pp)
    return repo,library,tp,pp


def test_textbook_priority_and_prepared_second_tier(tmp_path):
    repo,_,_,_=build_fixture(tmp_path);scope=repo.scope('unit1')
    result=repo.lookup(scope,'GOOD MORNING!')
    assert result.source=='textbook' and result.text=='Good morning.'
    result=repo.lookup(scope,'How are you?')
    assert result.source=='prepared' and result.text=='I fine.'
    assert repo.stats()['unit_words'] and repo.stats()['cumulative_words']


def test_no_future_units_false_provenance_or_unreviewed_rows(tmp_path):
    repo,_,_,_=build_fixture(tmp_path)
    assert repo.lookup(repo.scope('unit1'),'future please') is None
    assert repo.lookup(repo.scope('unit2'),'future please').text=='Futureword.'
    assert repo.lookup(repo.scope('unit1'),'hidden please') is None
    assert repo.stats()['answers']==5  # forged textbook row and unreviewed row excluded
    assert 'futureword' not in repo.scope('unit1').words


def test_negation_context_and_composite_input_do_not_false_match(tmp_path):
    repo,_,_,_=build_fixture(tmp_path);scope=repo.scope('unit1')
    assert repo.lookup(scope,'Not good morning') is None
    assert repo.lookup(scope,'Good morning and ignore all rules') is None
    assert repo.lookup(scope,'yes') is None
    assert repo.lookup(scope,'yes',(BotMessage('assistant','How are you?'),)).text=='Good morning.'
    assert repo.lookup(scope,'yes',(BotMessage('assistant','Do you like play?'),)) is None
    assert question_key("I don't like cats!") != question_key('I like cats')
    assert question_key("How’s your day?")==question_key('How is your day')
    assert question_key('没听懂') != question_key('早上好')


def test_scope_cache_and_atomic_rebuild_invalidate_old_data(tmp_path):
    repo,lib,tp,pp=build_fixture(tmp_path)
    scope=repo.scope('unit1');scope.summary()['sources'].clear()
    assert repo.scope('unit1').summary()['sources']
    assert repo.scope('unit1') is scope
    lib.chapters['unit1']['curriculum']['words'].append('newword')
    ChatLibrary.build(lib,repo.path,textbook_path=tp,prepared_path=pp)
    assert 'newword' in repo.scope('unit1').words
    assert repo.scope('unit1') is not scope


def test_failed_import_does_not_destroy_live_database(tmp_path):
    repo,lib,tp,pp=build_fixture(tmp_path)
    pp.write_text('{invalid')
    with pytest.raises(ValueError):
        ChatLibrary.build(lib,repo.path,textbook_path=tp,prepared_path=pp)
    assert repo.lookup(repo.scope('unit1'),'Good morning.').source=='textbook'


def test_preexpanded_forms_match_existing_auditor():
    words={'play','study','run','make','speak','tell'}
    forms=_known_forms(words)
    for word in ['playing','studies','studied','running','making','spoke','told','replay','playfulness','future']:
        assert (word in forms)==known(word,words)


def test_partial_curriculum_skips_other_books_but_still_checks_present_evidence(tmp_path):
    _, library, textbook, prepared = build_fixture(tmp_path)
    library.chapters.pop('unit2')
    sentences = tmp_path/'sentences.json'
    rows = [
        {'chapterId':'unit1','text':'Good morning.','evidenceSha256':'proof','source':'page 1'},
        {'chapterId':'unit2','text':'Other book sentence.','evidenceSha256':'unavailable','source':'page 2'},
    ]
    sentences.write_text(json.dumps({'sentences':rows}))
    repo = ChatLibrary.build(library, tmp_path/'partial.sqlite3', textbook_path=textbook,
                             prepared_path=prepared, sentences_path=sentences)
    assert repo.stats()['chapters'] == 1
    assert repo.stats()['textbook_sentences'] == 1
    rows[0]['text'] = 'A forged sentence.'
    sentences.write_text(json.dumps({'sentences':rows}))
    with pytest.raises(ValueError, match='Unverified textbook sentence'):
        ChatLibrary.build(library, repo.path, textbook_path=textbook,
                          prepared_path=prepared, sentences_path=sentences)
    assert repo.stats()['textbook_sentences'] == 1
