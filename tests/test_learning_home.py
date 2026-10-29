"""Learning entry routing and bundled media must work with either service mode."""
import asyncio
from aiohttp import ClientSession, web
from backend.app import create_app


def test_learning_home_and_chat_routes_in_both_service_modes():
    async def run(voice_enabled):
        app = create_app(warmup=False, voice_enabled=voice_enabled, bot_state_path=':memory:')
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        try:
            async with ClientSession() as client:
                async with client.get(url + '/') as response:
                    assert response.status == 200
                    home = await response.text()
                    assert '/learning.js' in home
                    assert '/course-selection.js' in home
                    assert 'id="courseSwitch"' in home
                    assert 'id="courseDialog"' in home
                async with client.get(url + '/voice.html', allow_redirects=False) as response:
                    assert response.status == (200 if voice_enabled else 302)
                    if not voice_enabled:
                        assert response.headers['Location'] == '/text.html'
                for asset in ('learning-data.js', 'learning-store.js', 'course-selection.js', 'learning.css', 'theme.css', 'kitty.svg', 'text.html', 'practice.html'):
                    async with client.get(url + '/' + asset) as response:
                        assert response.status == 200
                async with client.get(url + '/audio/weekend/dialogue.m4a', headers={'Range':'bytes=0-100'}) as response:
                    assert response.status == 206
                    assert len(await response.read()) == 101
                assert app['resource_manager'].in_use == 0
        finally:
            await runner.cleanup()
    asyncio.run(run(False))
    asyncio.run(run(True))
