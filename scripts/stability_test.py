#!/usr/bin/env python3.11
"""Repeated real-WebSocket sessions for leak and cleanup observation."""

import argparse
import asyncio
import json
import math
import resource
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from backend.app import create_app
from backend.atomic import DemoAgent, DemoTts, VadAsrStub
from backend.protocol import encode_audio_frame


async def wait_type(ws, wanted, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await asyncio.wait_for(ws.receive(), deadline - time.monotonic())
        if message.type == aiohttp.WSMsgType.BINARY and wanted == "binary":
            return message.data
        if message.type == aiohttp.WSMsgType.TEXT and json.loads(message.data).get("type") == wanted:
            return json.loads(message.data)
    raise TimeoutError(wanted)


async def one_session(client, base_url):
    async with client.ws_connect(base_url + "/ws") as ws:
        await ws.send_json({"type": "prepare", "version": 1, "requestId": "stability-prepare", "audio": {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1, "frameDurationMs": 100}})
        await wait_type(ws, "prepare_result")
        await ws.send_json({"type": "create_session", "version": 1, "requestId": "stability-create"})
        created = await wait_type(ws, "create_session_result")
        samples = [int(9000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(1600)]
        voiced = struct.pack("<1600h", *samples)
        for sequence, pcm in enumerate((voiced, voiced, b"\x00" * 3200)):
            await ws.send_bytes(encode_audio_frame(0, created["sessionId"], sequence, int(time.time() * 1000), pcm))
        await wait_type(ws, "binary")
        await ws.send_json({"type": "close_session", "version": 1, "sessionId": created["sessionId"]})
        await wait_type(ws, "close_session")


async def run(duration):
    app = create_app(asr_factory=VadAsrStub, agent_factory=DemoAgent, tts_factory=DemoTts, warmup=False)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    sessions = errors = 0
    async with aiohttp.ClientSession() as client:
        while time.monotonic() - started < duration:
            try:
                await one_session(client, base_url)
                sessions += 1
            except Exception as exc:
                errors += 1
                print(json.dumps({"event": "stability_error", "error": str(exc)}, ensure_ascii=False))
    await asyncio.sleep(0.1)
    pending_tasks = len([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()])
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    await runner.cleanup()
    result = {"durationSeconds": round(time.monotonic() - started, 2), "sessions": sessions, "errors": errors, "pendingTasksBeforeCleanup": pending_tasks, "maxRssRaw": rss}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if errors or pending_tasks > 1:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=1800, help="seconds; default is the PRD 30-minute gate")
    asyncio.run(run(parser.parse_args().duration))
