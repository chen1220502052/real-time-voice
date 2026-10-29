import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from backend.atomic import OllamaAgent
from backend.llm_runtime import LLMHttpPool, Timing, timing, use_llm_pool, use_timing


def test_reuse_isolates_headers_timings_and_closes():
    async def run():
        seen = []

        async def handle(request):
            seen.append((request.transport, request.headers.get("Authorization"), request.headers.get("Cookie")))
            body = 'data: ' + json.dumps({'choices': [{'delta': {'content': 'Hello. '}, 'finish_reason': 'stop'}]})
            response = web.Response(text=body + '\n\ndata: [DONE]\n\n')
            response.set_cookie('unsafe_shared_state', 'must_not_return')
            return response

        app = web.Application()
        app.router.add_post('/v1/chat/completions', handle)
        pool = LLMHttpPool()
        probes = []
        async with TestServer(app) as server:
            try:
                with use_llm_pool(pool):
                    for key in ['first', 'second']:
                        probe = Timing(key)
                        probes.append(probe)
                        with use_timing(probe):
                            model = OllamaAgent(base_url=str(server.make_url('/')).rstrip('/'), api_key=key)
                            assert ''.join([s async for s in model.stream_reply([])]) == 'Hello. '
                        assert timing() is None
                assert seen[0][0] is seen[1][0]
                assert [s[1] for s in seen] == ['Bearer first', 'Bearer second']
                assert not any(s[2] for s in seen)
                assert probes[0].metrics['connectionReused'] is False
                assert probes[1].metrics['connectionReused'] is True
                assert all('providerFirstTokenMs' in p.metrics for p in probes)
                other = LLMHttpPool()
                assert other.get() is not pool.get()
                await other.close()
            finally:
                await pool.close()
            assert pool.client.closed
    asyncio.run(run())


def test_cancel_stream_does_not_close_pool_or_sibling():
    async def run():
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def handle(request):
            payload = await request.json()
            if payload['model'] == 'slow':
                waiting.set()
                await release.wait()
            return web.Response(text='data: {"choices":[{"delta":{"content":"OK. "},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

        app = web.Application()
        app.router.add_post('/v1/chat/completions', handle)
        pool = LLMHttpPool()
        async with TestServer(app) as server:
            try:
                async def consume(name):
                    model = OllamaAgent(base_url=str(server.make_url('/')).rstrip('/'), model=name)
                    return ''.join([s async for s in model.stream_reply([])])
                with use_llm_pool(pool):
                    slow = asyncio.create_task(consume('slow'))
                    await asyncio.wait_for(waiting.wait(), 2)
                    sibling = asyncio.create_task(consume('fast'))
                    slow.cancel()
                    await asyncio.gather(slow, return_exceptions=True)
                    assert await asyncio.wait_for(sibling, 2) == 'OK. '
                    assert not pool.client.closed
                    assert await consume('fast') == 'OK. '
            finally:
                release.set()
                await pool.close()
    asyncio.run(run())


def test_app_cleanup_closes_only_its_own_pool():
    from backend.app import create_app

    async def run():
        app = create_app(warmup=False, voice_enabled=False, bot_state_path=':memory:')
        other = LLMHttpPool()
        async with TestServer(app):
            owned = app['llm_http_pool'].get()
            independent = other.get()
            assert owned is not independent
        assert owned.closed
        assert not independent.closed
        await other.close()
    # Fresh application/session owners also work on a second event loop.
    asyncio.run(run())
    asyncio.run(run())
