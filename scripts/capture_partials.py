#!/usr/bin/env python3
"""Capture every session_event from a live session to inspect ASR partials."""
import asyncio
import base64
import json
import sys
import wave
from pathlib import Path

import aiohttp

ROOT = Path(__file__).parent.parent


def packets(fixture):
    with wave.open(str(fixture), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())
    return [pcm[i:i + 3200] for i in range(0, len(pcm) - 3199, 3200)]


async def main(fixture_name: str):
    fixture = ROOT / "tests" / "fixtures" / "en" / fixture_name
    auth = "trial:wbYLZV-bi7XCrwyKAxheVJO3_xRAXEpy"
    headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode()}
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect("wss://aimaoren.com/ws", headers=headers, ssl=False)
        async def send(obj):
            await ws.send_str(json.dumps(obj))
        await send({"type": "prepare", "audio": {"codec": "pcm_s16le", "sampleRate": 16000,
                                                  "channels": 1, "frameDurationMs": 100}})
        while True:
            msg = await ws.receive()
            data = json.loads(msg.data) if isinstance(msg.data, str) else None
            if data and data.get("type") == "prepare_result":
                break
        await send({"type": "create_session", "grade": 7})
        while True:
            msg = await ws.receive()
            data = json.loads(msg.data) if isinstance(msg.data, str) else None
            if data and data.get("type") == "create_session_result":
                sid = data["sessionId"]
                break
        from backend.protocol import encode_audio_frame
        async def pump():
            seq = 0
            for pkt in packets(fixture):
                await ws.send_bytes(encode_audio_frame(0, sid, seq, seq * 100, pkt))
                seq += 1
                await asyncio.sleep(0.1)
        pumper = asyncio.create_task(pump())
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=5)
            except asyncio.TimeoutError:
                break
            if isinstance(msg.data, str):
                data = json.loads(msg.data)
                if data.get("type") == "session_event":
                    ev = data["event"]
                    if ev["eventType"] in ("asr_partial", "text_bot_cache_hit", "text_bot_cache_miss",
                                           "text_bot_prepare_failed", "asr_final", "speech_ended"):
                        print(f"[{ev['eventType']}] {json.dumps(ev.get('attributes', {}), ensure_ascii=False)[:260]}")
                elif data.get("type") in ("reply_complete", "close_session"):
                    print(f"[{data['type']}] {data.get('replyId', '')}")
                    if data["type"] == "close_session":
                        break
        pumper.cancel()
        await ws.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "en_pause300_16k.wav"))
