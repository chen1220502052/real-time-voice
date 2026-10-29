"""Real WAV/ASR/Bot/TTS barge-in check; client playback receipts are fixtures."""

import argparse
import asyncio
import json
from pathlib import Path
import ssl
import time
import wave

import aiohttp
import certifi

from backend.protocol import encode_audio_frame
from scripts.browser_login import authenticated_ws


async def check(url, auth_file=None, fixture_dir=None):
    auth = None
    if auth_file:
        values = dict(line.split(":", 1) for line in auth_file.read_text().splitlines() if ":" in line)
        auth = aiohttp.BasicAuth(values["Username"].strip(), values["Password"].strip())
    report = {"mode": "real_voice_barge_in", "playbackReceipts": "simulated_0ms_then_550ms",
              "interruptions": [], "asrFinals": [], "errors": [], "lateOldAudio": 0}
    async with aiohttp.ClientSession(auth=auth, connector=aiohttp.TCPConnector(
            ssl=ssl.create_default_context(cafile=certifi.where()))) as client:
        async with authenticated_ws(client, url) as ws:
            async def expect(kind):
                async with asyncio.timeout(30):
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            value = json.loads(message.data)
                            if value.get("type") == kind:
                                return value
            await ws.send_json({"type": "prepare"})
            assert (await expect("prepare_result"))["ok"], "Existing trial was not interrupted"
            await ws.send_json({"type": "create_session", "grade": 7})
            created = await expect("create_session_result")
            assert created["textBotProtocolVersion"] == 2
            session = created["sessionId"]
            report["sessionId"] = session
            replies, completed, invalid = {}, set(), set()
            current_audio = None
            opening = None
            sequence = 0

            async def reader():
                nonlocal current_audio, opening
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        if current_audio in invalid:
                            report["lateOldAudio"] += 1
                        if current_audio in replies:
                            replies[current_audio]["audioFrames"] += 1
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    value = json.loads(message.data)
                    kind, reply = value.get("type"), value.get("replyId")
                    if kind == "assistant_delta":
                        if opening is None:
                            opening = reply
                        bucket = replies.setdefault(reply, {"text": "", "audioFrames": 0})
                        bucket["text"] += value["text"]
                    elif kind == "avatar_frame":
                        current_audio = reply
                    elif kind == "reply_complete":
                        completed.add(reply)
                    elif kind == "playback_interrupt":
                        invalid.add(reply)
                        played = 0 if reply == opening else 550
                        report["interruptions"].append({"replyId": reply, "declaredPlayedMs": played})
                        await ws.send_json({"type": "client_event", "event": {
                            "eventType": "playback_stopped", "replyId": reply,
                            "attributes": {"playedMs": played},
                        }})
                    elif kind == "asr_result" and value.get("status") == 3 and value.get("text"):
                        report["asrFinals"].append(value["text"])
                    elif kind in {"error", "reply_failed"}:
                        report["errors"].append(value.get("code"))

            async def packet(pcm):
                nonlocal sequence
                await ws.send_bytes(encode_audio_frame(0, session, sequence, int(time.time() * 1000), pcm))
                sequence += 1
                await asyncio.sleep(.1)

            async def until(predicate, timeout=35):
                async with asyncio.timeout(timeout):
                    while not predicate():
                        assert not report["errors"], report["errors"]
                        await packet(b"\0" * 3200)

            async def fixture(name):
                directory = Path(fixture_dir) if fixture_dir is not None else Path(__file__).resolve().parent.parent / "tests/fixtures/en"
                with wave.open(str(directory / name), "rb") as source:
                    assert (source.getframerate(), source.getsampwidth(), source.getnchannels()) == (16000, 2, 1)
                    pcm = source.readframes(source.getnframes())
                for offset in range(0, len(pcm), 3200):
                    await packet(pcm[offset:offset + 3200].ljust(3200, b"\0"))

            task = asyncio.create_task(reader())
            try:
                # Do not acknowledge opening playback: all packets may have
                # arrived but the user has heard none of this reply.
                await until(lambda: opening is not None and opening in completed)
                await fixture("en_hello_16k.wav")
                await until(lambda: len(replies) >= 2 and any(v["audioFrames"] for k, v in replies.items() if k != opening))
                first = next(k for k in replies if k != opening)
                await ws.send_json({"type": "client_event", "event": {
                    "eventType": "playback_progress", "replyId": first, "attributes": {"playedMs": 550},
                }})
                await fixture("en_help_16k.wav")
                await until(lambda: len(replies) >= 3 and any(k in completed for k in replies if k not in {opening, first}))
                final = next(k for k in replies if k not in {opening, first})
                await ws.send_json({"type": "client_event", "event": {"eventType": "playback_completed", "replyId": final}})
                assert {opening, first}.issubset(invalid), report
                assert not report["errors"] and not report["lateOldAudio"], report
                assert len(report["asrFinals"]) == 2, report
                report["replies"] = replies
                await ws.send_json({"type": "close_session"})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8875/ws")
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(check(args.url, args.auth_file))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
