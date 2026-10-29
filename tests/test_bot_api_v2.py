import asyncio
import json

from aiohttp import ClientSession

from tests.test_text_api import server
from aiohttp import web
from backend.app import create_app
from backend.atomic import DemoTts, VadAsrStub
from backend.english_chat import EnglishChatBot
from backend.text_bot import BotEvent


def body(name="r1", **kwargs):
    return {"version": 2, "sessionId": "api_session", "requestId": name,
            "turnId": name, "text": "I enjoy basketball.", "channel": "text",
            "options": {"grade": 7}, **kwargs}


async def read(client, url, payload):
    async with client.post(url + "/api/text-bot/replies", json=payload) as response:
        text = await response.text()
        return response.status, [json.loads(line) for line in text.splitlines()]


def test_http_v2_multiturn_idempotency_conflict_cursor_and_delivery():
    async def run():
        calls = []
        class Bot:
            protocol_version = 1
            async def stream(self, request):
                calls.append(request)
                yield BotEvent(request.request_id, "text_delta", text="Basketball sounds fun.")
                yield BotEvent(request.request_id, "done")
        app, runner, url = await server(Bot)
        try:
            async with ClientSession() as client:
                status, first = await read(client, url, body())
                assert status == 200 and first[-1]["type"] == "done"
                assert first[0]["version"] == 2
                await app["resource_manager"].reserve("voice")
                assert (await read(client, url, body())) == (status, first)
                assert (await read(client, url, body(afterEventSeq=1)))[1] == first[1:]
                assert len(calls) == 1
                assert (await read(client, url, body(text="Different request")))[0] == 409
                assert (await read(client, url, body("r2")))[0] == 429
                await app["resource_manager"].release("voice")
                event = {"eventId": "receipt", "replyId": "r1", "type": "text_displayed", "sequence": 1}
                for _ in range(2):
                    async with client.post(url + "/api/text-bot/interactions", json={"sessionId": "api_session", "interactions": [event]}) as response:
                        assert response.status == 200
                assert (await read(client, url, body("r2", text="What did I say I enjoy?")))[0] == 200
                assert [m.role for m in calls[-1].messages] == ["user", "assistant", "user"]
                assert calls[-1].messages[0].content == "I enjoy basketball."
                assert app["resource_manager"].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_http_v2_disconnect_is_terminal_without_repeating_provider_call():
    async def run():
        started, closed = asyncio.Event(), asyncio.Event()
        calls = []
        class Bot:
            protocol_version = 1
            async def stream(self, query):
                calls.append(query)
                started.set()
                try:
                    await asyncio.sleep(10)
                    yield BotEvent(query.request_id, "done")
                finally:
                    closed.set()
        app, runner, url = await server(Bot)
        try:
            async with ClientSession() as client:
                response = await client.post(url + "/api/text-bot/replies", json=body())
                await started.wait()
                response.close()
                await asyncio.wait_for(closed.wait(), 2)
                for _ in range(100):
                    if app["resource_manager"].in_use == 0:
                        break
                    await asyncio.sleep(.005)
                status, events = await read(client, url, body())
                assert status == 200 and events[-1]["code"] == "BOT_CANCELLED"
                assert len(calls) == 1 and app["resource_manager"].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_v2_validation_and_cross_session_receipts_are_rejected():
    async def run():
        app, runner, url = await server()
        try:
            async with ClientSession() as client:
                for payload in [body(messages=[]), body(requestId=None), body(text=""), body(options={"grade": True})]:
                    assert (await read(client, url, payload))[0] == 400
                await read(client, url, body(event="session_start", text=""))
                async with client.post(url + "/api/text-bot/interactions", json={
                    "sessionId": "another_session", "interactions": [{"eventId": "wrong", "replyId": "r1", "type": "text_displayed", "sequence": 1}]
                }) as response:
                    assert response.status == 409
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_voice_disconnect_during_async_cleanup_always_releases_capacity():
    async def run():
        reset_started = asyncio.Event()
        class SlowReset(VadAsrStub):
            async def reset(self):
                reset_started.set()
                await asyncio.sleep(.1)
        app = create_app(asr_factory=SlowReset, tts_factory=DemoTts,
                         agent_factory=EnglishChatBot, warmup=False)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        try:
            async with ClientSession() as client:
                ws = await client.ws_connect(url + "/ws")
                await ws.send_json({"type": "prepare"})
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "prepare_result":
                        assert message["ok"]
                        break
                await ws.send_json({"type": "create_session"})
                while True:
                    message = await ws.receive_json()
                    if message.get("type") == "create_session_result":
                        assert message["ok"]
                        break
                await ws.close()
                await asyncio.wait_for(reset_started.wait(), 2)
                for _ in range(200):
                    if app["resource_manager"].in_use == 0:
                        break
                    await asyncio.sleep(.01)
                assert app["resource_manager"].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run())
