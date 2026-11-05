import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from backend import local_llm
from backend.writing_workflows import model_essay, provider_json


def test_local_adapter_streams_without_secrets_and_rejects_truncation(monkeypatch):
    async def run():
        requests = []
        truncated = False

        async def chat(request):
            data = await request.json()
            requests.append(data)
            assert 'Authorization' not in request.headers
            response = web.StreamResponse(headers={'Content-Type': 'application/x-ndjson'})
            await response.prepare(request)
            for part in ('Hello', ', cat!'):
                await response.write((json.dumps({'message': {'content': part}, 'done': False}) + '\n').encode())
            await response.write((json.dumps({'done': True, 'done_reason': 'length' if truncated else 'stop'}) + '\n').encode())
            return response

        app = web.Application()
        app.router.add_post('/api/chat', chat)
        async with TestServer(app) as server:
            monkeypatch.setenv('OLLAMA_NATIVE_BASE_URL', str(server.make_url('')).rstrip('/'))
            monkeypatch.setenv('OLLAMA_MODEL', 'test-local')
            agent = local_llm.LocalAgent('Be kind.')
            state = {'messages': [{'role': 'user', 'content': 'Hi'}]}
            result = await agent.ainvoke(state)
            assert result['messages'][-1]['content'] == 'Hello, cat!'
            assert requests[0]['messages'][0] == {'role': 'system', 'content': 'Be kind.'}
            assert requests[0]['think'] is False
            assert requests[0]['model'] == 'test-local'
            monkeypatch.setenv('VOICE_CHAT_MODEL','test-fast-voice')
            monkeypatch.setenv('WRITING_MODEL','test-writing')
            await local_llm.complete(state['messages'],purpose='voice_chat')
            await local_llm.complete(state['messages'],purpose='writing')
            assert requests[-2]['model']=='test-fast-voice'
            assert requests[-1]['model']=='test-writing'
            assert requests[-2]['options']['num_ctx']==4096
            assert requests[-1]['options']['num_ctx']==8192
            monkeypatch.delenv('VOICE_CHAT_MODEL')
            monkeypatch.setenv('CHAT_MODEL','test-chat')
            await local_llm.complete(state['messages'],purpose='voice_chat')
            assert requests[-1]['model']=='qwen2.5:3b'
            await local_llm.complete(state['messages'],purpose='chat')
            assert requests[-1]['model']=='test-chat'
            truncated = True
            with pytest.raises(ValueError, match='truncated'):
                await agent.ainvoke(state)
    asyncio.run(run())


def test_local_mode_does_not_allow_cloud_endpoint(monkeypatch):
    for url in ('https://api.example.com', 'http://localhost@evil.test', 'http://127.0.0.1/v1'):
        monkeypatch.setenv('OLLAMA_NATIVE_BASE_URL', url)
        with pytest.raises(ValueError, match='loopback'):
            local_llm.endpoint()


def test_local_writing_skips_cloud_search_and_routes_reviews(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'ollama')
    calls = []

    async def generated(*args, **kwargs):
        return {'title': 'Cats', 'essay': 'I like cats.'}

    async def forbidden_search(*args, **kwargs):
        raise AssertionError('Local mode must not call cloud search')

    async def complete(messages, **kwargs):
        calls.append(kwargs)
        return '{"scores":{}}'

    monkeypatch.setattr(local_llm, 'complete', complete)
    from types import SimpleNamespace

    async def run():
        scope = SimpleNamespace(chapter={'unit': 'Animals'})
        result = await model_essay(SimpleNamespace(generate=generated), scope, '动物', 40, False, finder=forbidden_search)
        assert result['search']['status'] == 'skipped'
        reply = await provider_json('/chat/completions', {'messages': []})
        assert reply['choices'][0]['finish_reason'] == 'stop'
        assert calls[0]['purpose'] == 'writing' and calls[0]['json_mode']
    asyncio.run(run())
