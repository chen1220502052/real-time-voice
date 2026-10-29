import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.writing_vocabulary import VocabularyScope
from backend.writing_workflows import find_model_sources, model_essay, public_url, review_essay


def scope():
    c = {'id':'test','publisher':'科普版','grade':'5年级','edition':'上册_新版',
         'unit':'Unit 1 School','vocabulary':[{'word':'school','meaning':'学校'}], 'textKind':'测试'}
    return VocabularyScope(SimpleNamespace(chapters={'test':c}),'test')


def feedback(corrections=None):
    return {'scores':{'content':25,'organization':16,'language':24},'overall':'表达清楚，可以补充一个细节。',
            'strengths':['主题明确。'],'suggestions':['用学过的词说明喜欢学校的原因。'],'corrections':corrections or []}


def response(value):
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value,ensure_ascii=False)}}]}


def test_review_keeps_student_text_and_anchors_each_correction():
    original = 'I is at school.'
    async def caller(path,payload,**kwargs):
        return response(feedback([{'original':'I is','suggestion':'I am','reason':'I 后面用 am。'}]))
    result = asyncio.run(review_essay(scope(),'My School',original,'写学校',80,caller=caller))
    assert result['originalEssay'] == original
    assert result['corrections'][0]['original'] in original
    assert result['totalScore'] == sum(v for v in result['scores'].values() if v is not None)
    assert result['maxScore'] == 80 and result['scores']['vocabulary'] is None
    assert '仅供' in result['notice']


def test_review_rejects_hallucinated_quotes_and_invalid_scores():
    calls = []
    async def caller(path,payload,**kwargs):
        calls.append(payload)
        return response(feedback([{'original':'I are reading','suggestion':'I am reading','reason':'改错'}]))
    with pytest.raises(ValueError):
        asyncio.run(review_essay(scope(),'','I am at school.','写学校',80,caller=caller))
    assert len(calls) == 2


def test_review_reports_outside_words_separately_from_grammar_errors():
    async def caller(*args,**kwargs):
        return response(feedback())
    result = asyncio.run(review_essay(scope(),'','I am an astronaut.','介绍自己',80,caller=caller))
    assert result['audit']['outsideWords'] == [{'word':'astronaut','count':1}]
    assert result['corrections'] == []


def test_search_sends_only_public_unit_topic_and_rejects_unsafe_result_urls():
    payloads = []
    async def caller(path,payload,**kwargs):
        payloads.append(payload)
        return {'results':[{'title':'bad','url':'javascript:alert(1)'}, {'title':'local','url':'https://127.0.0.1/'},
                           {'title':'School sample','url':'https://school.example.edu/essay','content':'School essay'}]}
    result = asyncio.run(find_model_sources(scope(),'私密学生姓名与家庭地址',caller=caller))
    assert '私密' not in json.dumps(payloads,ensure_ascii=False)
    assert result['status']=='found' and len(result['sources'])==1
    assert result['candidates'][0]['content']=='School essay'
    assert not public_url('https://person:secret@example.org/path')
    assert not public_url('https://test.local/path')


def test_suitable_web_paragraph_is_preferred_and_only_a_short_excerpt_is_returned():
    class Assistant:
        async def generate(self,*args):
            raise AssertionError('A suitable online sample must take priority over generation')
    paragraph = 'I am at school. ' * 8
    async def finder(*args):
        return {'status':'found','sources':[{'title':'School sample','url':'https://example.edu/essay','snippet':''}],
                'candidates':[{'sourceIndex':0,'content':paragraph}]}
    result = asyncio.run(model_essay(Assistant(),scope(),'写学校',40,True,finder=finder))
    assert result['origin']=='web_reference' and result['attempts']==0
    assert len(result['essay'].split())<=21 and result['essay']!=paragraph
    assert 'candidates' not in result['search']
    assert result['audit']['wordCount']==32


@pytest.mark.parametrize('status',['no_results','unavailable','found'])
def test_missing_unavailable_or_unsuitable_sources_fall_back_to_labelled_original_ai(status):
    class Assistant:
        async def generate(self,*args):
            return {'title':'School','essay':'original','glossary':{},'audit':{'passed':True}}
    async def finder(*args):
        return {'status':status,'sources':[], 'candidates':[{'sourceIndex':0,'content':'Quantum cryptography!'}]}
    result = asyncio.run(model_essay(Assistant(),scope(),'写学校',40,False,finder=finder))
    assert result['origin']=='ai_original' and result['essay']=='original'
    assert 'AI' in result['originLabel']


def test_search_failure_is_distinguished_from_no_results():
    async def caller(*args,**kwargs):
        raise RuntimeError('provider detail must not be exposed')
    result = asyncio.run(find_model_sources(scope(),'写学校',caller=caller))
    assert result['status']=='unavailable' and 'detail' not in json.dumps(result)
