import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from backend.atomic import OllamaAgent


@pytest.mark.parametrize('ending,valid', [('stop', True), ('done', True), ('length', False), ('eof', False)])
def test_provider_must_finish_normally_before_stream_can_succeed(ending, valid):
    async def run():
        async def handle(request):
            payload = await request.json()
            assert payload['stream'] is True
            frames = [{'choices':[{'delta':{'content':'Hello there. '}}]}]
            if ending in {'stop','length'}:
                frames += [{'choices':[{'delta':{},'finish_reason':ending}]}]
            # Usage-only frames must not crash delta parsing.
            frames += [{'choices':[], 'usage':{'completion_tokens':3}}]
            body = ''.join('data: '+json.dumps(frame)+'\n\n' for frame in frames)
            if ending in {'done','length'}:
                body += 'data: [DONE]\n\n'
            return web.Response(text=body,content_type='text/event-stream')
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handle)
        async with TestServer(app) as server:
            bot = OllamaAgent(base_url=str(server.make_url('/')).rstrip('/'), model='test-model',api_key='test-placeholder')
            if valid:
                assert ''.join([chunk async for chunk in bot.stream_reply([{'role':'user','content':'hello'}])]) == 'Hello there. '
            else:
                with pytest.raises(RuntimeError):
                    _ = [chunk async for chunk in bot.stream_reply([{'role':'user','content':'hello'}])]
    asyncio.run(run())
