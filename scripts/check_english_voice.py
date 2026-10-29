#!/usr/bin/env python3.11
"""Real fixture -> WSS/WS -> ASR -> TextBot -> TTS check, not a mic/speaker test."""

import argparse
import asyncio
import json
import ssl
from pathlib import Path
import time
import wave
import struct

import aiohttp

from backend.protocol import decode_audio_frame, encode_audio_frame
from scripts.browser_login import authenticated_ws


async def check(url: str, grade: int, fixtures: list[Path], auth=None, busy_timeout=0, report_out=None, timing_probes=True, on_session_ready=None, content_checks=True, chapter_id=None) -> dict:
    report = report_out if report_out is not None else {}
    report.update({"grade": grade, "mode": "real_audio_fixture", "playbackAcknowledgement": "simulated_after_receiving_audio",
                   "asrFinals": [], "replies": [], "errors": [], "uplinkPackets": 0,
                   "legacyContentChecks": content_checks})
    if chapter_id:
        report['chapterId'] = chapter_id
    started = time.monotonic()
    completed = asyncio.Queue()
    replies = {}
    sequence = 0
    timeline = []
    captures = {}
    clock_pings, calibrations = {}, []
    import certifi
    async with aiohttp.ClientSession(auth=auth, connector=aiohttp.TCPConnector(
            ssl=ssl.create_default_context(cafile=certifi.where()))) as client:
        async with authenticated_ws(client, url) as ws:
            async def expect(kind):
                async with asyncio.timeout(20):
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") == kind:
                                return data
                    raise RuntimeError("Connection ended during setup")
            resource_deadline = time.monotonic() + busy_timeout
            while True:
                await ws.send_json({"type": "prepare", "version": 1})
                prepared = await expect("prepare_result")
                if prepared["ok"]:
                    break
                if prepared.get("code") != "NO_RESOURCE" or time.monotonic() >= resource_deadline:
                    raise RuntimeError("Trial resource unavailable; active calls were not interrupted")
                await asyncio.sleep(1)
            report["resourceWaitMs"] = round((time.monotonic() - started)*1000)
            await ws.send_json({"type": "create_session", "version": 1, "grade": grade,
                                **({'chapterId':chapter_id} if chapter_id else {})})
            created = await expect("create_session_result")
            assert created["ok"] and created["grade"] == grade
            assert created["textBotProtocolVersion"] == 2
            if chapter_id:
                assert created['chapterId'] == chapter_id
            session_id = created["sessionId"]
            report["sessionId"] = session_id
            if on_session_ready is not None:
                await on_session_ready()

            async def read():
                current = None
                media = {}
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            frame = decode_audio_frame(msg.data)
                            metadata = media.pop(frame.sequence, None)
                            audio_reply = metadata.get("replyId") if metadata else None
                            if audio_reply:
                                bucket = replies.setdefault(audio_reply, {"text": "", "audioFrames": 0})
                                bucket["audioFrames"] += 1
                                bucket.setdefault("firstPacketReceivedMs", time.monotonic()*1000)
                                bucket.setdefault("firstAudioMs", round((time.monotonic() - started) * 1000))
                                bucket.setdefault("timing", {}).update(metadata.get("timing", {}))
                                assert len(frame.payload) == 3200
                            else:
                                report["errors"].append({"code": "UNMATCHED_AUDIO_METADATA", "sequence": frame.sequence})
                            continue
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        kind = data.get("type")
                        if kind == "timing_pong":
                            received = time.monotonic()*1000
                            sent = clock_pings.pop(data["pingId"], None)
                            if sent is not None:
                                calibrations.append({"offsetLowMs": data["serverSentMs"]-received,
                                                     "offsetHighMs": data["serverReceivedMs"]-sent})
                        elif kind == "avatar_frame":
                            media[data["sequence"]] = data
                        elif kind == "assistant_delta":
                            current = data["replyId"]
                            bucket = replies.setdefault(current, {"text": "", "audioFrames": 0})
                            bucket.setdefault("firstTextMs", round((time.monotonic() - started) * 1000))
                            bucket["text"] += data["text"]
                        elif kind == "session_event":
                            event = data["event"]
                            if event.get("replyId") and event.get("attributes", {}).get("timing"):
                                replies.setdefault(event["replyId"], {"text": "", "audioFrames": 0}).setdefault("timing", {}).update(event["attributes"]["timing"])
                            if event["eventType"] in {"asr_final", "text_bot_request_started", "text_bot_first_text", "tts_segment_started", "tts_first_audio", "text_bot_cache_hit", "text_bot_cache_miss", "text_bot_cache_disabled", "text_bot_cache_unsupported", "text_bot_prepare_requested"}:
                                timeline.append(event)
                        elif kind == "asr_result" and data.get("status") == 3 and data.get("text"):
                            report["asrFinals"].append(data["text"])
                        elif kind == "asr_result" and data.get("status") == 3:
                            report.setdefault("emptyAsrFinals", []).append({"turnId": data.get("turnId"),
                                                                         "timing": data.get("timing")})
                            report["errors"].append({"code": "ASR_EMPTY_FINAL", "turnId": data.get("turnId")})
                            await completed.put(None)
                        elif kind == "reply_complete":
                            bucket = replies[data["replyId"]]
                            bucket["replyId"] = data["replyId"]
                            assert bucket["text"] and bucket["audioFrames"] > 0
                            report["replies"].append(bucket.copy())
                            await ws.send_json({"type": "client_event", "event": {
                                "eventType": "playback_completed", "replyId": data["replyId"], "sessionId": session_id,
                            }})
                            await completed.put(bucket.copy())
                        elif kind in ("error", "reply_failed"):
                            report["errors"].append({"type": kind, "code": data.get("code")})
                            await completed.put(None)
                except Exception as error:
                    report["errors"].append({"type": type(error).__name__})
                    await completed.put(None)

            reader = asyncio.create_task(read())
            try:
                for index in range(5 if timing_probes else 0):
                    ping_id = f"timing-{index}"
                    clock_pings[ping_id] = time.monotonic()*1000
                    await ws.send_json({"type": "timing_ping", "pingId": ping_id})
                    await asyncio.sleep(.15)
                assert await asyncio.wait_for(completed.get(), 30), "Opening failed"
                for fixture in fixtures:
                    with wave.open(str(fixture), "rb") as source:
                        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
                        pcm = source.readframes(source.getnframes())
                    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
                    last_signal = next((i for i in range(len(samples)-1, -1, -1) if abs(samples[i]) >= 300), None)
                    turn_start_sequence = sequence
                    before = len(report["asrFinals"])
                    for offset in range(0, len(pcm), 3200):
                        frame = pcm[offset:offset + 3200].ljust(3200, b"\0")
                        captures[sequence] = time.monotonic()*1000
                        await ws.send_bytes(encode_audio_frame(0, session_id, sequence, int(time.time()*1000), frame))
                        sequence += 1
                        await asyncio.sleep(.1)
                    wav_stream_end = round((time.monotonic() - started) * 1000)
                    # Packet timestamp is send time. Reconstruct the fixture's
                    # non-silent sample position relative to its containing
                    # frame, not the end of the WAV (which includes silence).
                    signal_end = (captures[turn_start_sequence + last_signal//1600]
                                  - 100 + (last_signal%1600)/16) if last_signal is not None else None
                    deadline = time.monotonic() + 30
                    while completed.empty() and time.monotonic() < deadline:
                        await ws.send_bytes(encode_audio_frame(0, session_id, sequence, int(time.time()*1000), b"\0" * 3200))
                        sequence += 1
                        await asyncio.sleep(.1)
                    answer = await asyncio.wait_for(completed.get(), 1)
                    assert answer, ("Reply failed", report["errors"])
                    answer["wavStreamEndToFirstReceivedAudioMs"] = answer["firstAudioMs"] - wav_stream_end
                    answer["fixtureSignalEndToFirstPacketMs"] = (answer["firstPacketReceivedMs"] - signal_end if signal_end is not None else None)
                    answer["sourceEndSequence"] = turn_start_sequence + last_signal//1600 if last_signal is not None else None
                    answer["sourceEndSentMs"] = captures.get(answer["sourceEndSequence"])
                    answer["sourceEndWithinPacketMs"] = last_signal%1600/16 if last_signal is not None else None
                    answer["measurement"] = "paced_fixture_packet_receipt_not_browser_or_physical_output"
                    report["replies"][-1].update(answer)
                    assert len(report["asrFinals"]) > before, "No real ASR final"
                await ws.send_json({"type": "close_session", "version": 1})
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                report["uplinkPackets"] = sequence
                report["partialReplies"] = replies
    assert not report["errors"], "Voice pipeline reported errors"
    assert len(report["asrFinals"]) == len(fixtures)
    assert len(report["replies"]) == len(fixtures) + 1
    report["clockCalibration"] = min(calibrations, key=lambda item:item["offsetHighMs"]-item["offsetLowMs"], default=None)
    report["physicalOutput"] = None
    for answer in report["replies"]:
        # Merge post-send timestamps, which may arrive after the first PCM.
        answer["timing"] = replies[answer["replyId"]].get("timing", {})
        answer["physicalOutput"] = None
    for answer in report["replies"]:
        events = [e for e in timeline if e.get("replyId") == answer["replyId"]]
        points = {}
        for event in events:
            points.setdefault(event["eventType"], event["monotonicMs"])
        turn = next((e.get("turnId") for e in events if e.get("turnId")), None)
        asr = next((e for e in timeline if e["eventType"] == "asr_final" and e.get("turnId") == turn), None)
        if asr:
            points["asr_final"] = asr["monotonicMs"]
        pairs = {
            "asrFinalToBotStartMs": ("asr_final", "text_bot_request_started"),
            "botStartToFirstTextMs": ("text_bot_request_started", "text_bot_first_text"),
            "firstTextToTtsStartMs": ("text_bot_first_text", "tts_segment_started"),
            "ttsStartToFirstPcmMs": ("tts_segment_started", "tts_first_audio"),
        }
        answer["serverStages"] = {name: round(points[b] - points[a], 1) for name, (a, b) in pairs.items() if a in points and b in points}
        answer["cache"] = next((name for name in ("hit", "miss", "disabled", "unsupported")
                                if "text_bot_cache_" + name in points), "opening")
        answer["cacheDetail"] = next((e["attributes"] for e in events
                                    if e["eventType"] in {"text_bot_cache_hit", "text_bot_cache_miss",
                                                          "text_bot_cache_disabled", "text_bot_cache_unsupported"}), {})
    for transcript, answer in zip(report["asrFinals"], report["replies"][1:]):
        lowered, reply = transcript.lower(), answer['text'].lower()
        if content_checks and 'fun fact about cats' in lowered:
            assert 'cat' in reply and 'about what' not in reply, ('Incomplete cached question adopted', answer)
        if content_checks and 'twenty three plus forty five' in lowered:
            assert '68' in reply or 'sixty-eight' in reply or 'sixty eight' in reply, ('Wrong numeric draft adopted', answer)
        if content_checks and 'speak slower' in lowered:
            assert 'only type' not in reply and "don't have a real voice" not in reply and 'typing words' not in reply, ('Invalid voice identity', answer)
    report["preparationRequests"] = sum(e["eventType"] == "text_bot_prepare_requested" for e in timeline)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8870/ws")
    parser.add_argument("--grade", type=int, choices=[3, 4, 5, 6, 7, 8, 9], default=7)
    parser.add_argument("--fixture", type=Path, action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--auth-file", type=Path, help="Root-private trial access file; credentials never printed")
    parser.add_argument("--wait-busy", type=int, default=0, help="Wait for existing trial sessions to finish")
    parser.add_argument("--no-timing-probes", action="store_true", help="Compatibility with releases before timing_ping")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    fixtures = args.fixture or [root / "tests/fixtures/en/en_hello_16k.wav", root / "tests/fixtures/en/en_help_16k.wav"]
    auth = None
    if args.auth_file:
        # Same trial file format used by the deployment workflow.
        import re
        values = dict(re.findall(r"^(?:export )?([A-Z_]+)=(.*)$", args.auth_file.read_text(), re.M))
        labels = dict(line.split(":", 1) for line in args.auth_file.read_text().splitlines() if ":" in line)
        values.setdefault("TRIAL_USER", labels.get("Username", "").strip())
        values.setdefault("TRIAL_PASSWORD", labels.get("Password", "").strip())
        user = values.get("TRIAL_USER") or values.get("BASIC_AUTH_USER")
        password = values.get("TRIAL_PASSWORD") or values.get("BASIC_AUTH_PASSWORD")
        if not user or not password:
            raise ValueError("Trial credential file must contain TRIAL_USER and TRIAL_PASSWORD")
        auth = aiohttp.BasicAuth(user.strip().strip('\"\''), password.strip().strip('\"\''))
    report = {}
    try:
        report = asyncio.run(check(args.url, args.grade, fixtures, auth, args.wait_busy, report, not args.no_timing_probes))
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["failureType"] = type(error).__name__
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
