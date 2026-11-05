import asyncio
import json
from types import SimpleNamespace

import pytest

from backend import natural_writing
from backend.writing_assistant import WritingError


def test_natural_mode_labels_new_words_without_claiming_textbook_compliance(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'ollama')
    class Agent:
        def __init__(self, *args, **kwargs): pass
        async def ainvoke(self, state):
            return {'messages':[{'content':json.dumps({'title':'My Cat','essay':'My cat likes milk.'})}]}
    calls = []
    async def translate(messages, **kwargs):
        calls.append(kwargs['json_mode'])
        return '{"likes":"喜欢"}'
    monkeypatch.setattr(natural_writing,'LocalAgent',Agent)
    monkeypatch.setattr(natural_writing,'complete',translate)
    scope=SimpleNamespace(textbook_ready=True,words={'cat','milk'},chapter={'grade':'3年级','unit':'Animals'},summary=lambda:{},
        audit=lambda *a,**kw:{'passed':False,'englishOnly':True,'lengthOK':True,
            'outsideWords':[{'word':'likes','count':1}],'policy':{'maxUniqueOutside':4,'maxOutsideRatio':.05}})
    result=asyncio.run(natural_writing.generate(scope,'Animals',40))
    assert result['audit']['passed'] and result['audit']['textbookPolicyPassed'] is False
    assert result['audit']['policy']['maxOutsideRatio'] is None
    assert result['vocabularyMode']=='natural' and result['glossary']=={'likes':'喜欢'}
    assert calls[0]['required']==['likes']


def test_natural_mode_is_explicit_and_requires_verified_local_curriculum(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER','deepseek')
    with pytest.raises(WritingError,match='自然表达'):
        asyncio.run(natural_writing.generate(SimpleNamespace(textbook_ready=True),'hello',40))
    monkeypatch.setenv('LLM_PROVIDER','ollama')
    with pytest.raises(WritingError,match='自然表达'):
        asyncio.run(natural_writing.generate(SimpleNamespace(textbook_ready=False),'hello',40))


def test_glossary_translation_rejects_missing_words_and_non_chinese_meanings(monkeypatch):
    responses=iter(['{"outside":"outside"}','{"unrelated":"无关"}'])
    async def translate(*args,**kwargs):
        return next(responses)
    monkeypatch.setattr(natural_writing,'complete',translate)
    with pytest.raises(ValueError,match='释义'):
        asyncio.run(natural_writing.translate_glossary('School','We play outside.',['outside']))
