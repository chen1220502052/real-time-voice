"""End-to-end client against a real running server (scripts/run_hybrid.sh).

Streams the real 16 kHz fixtures through the WebSocket session and prints a
metrics report covering the four monitored dialogue indicators:

1. Reply quality (language match, TTS readability, length band)
2. Reply latency (speech-end -> first audio, ASR-final -> first audio)
3. False interruption rate (noise bursts injected during playback must NOT
   interrupt the robot reply)
4. Cut-in rate (reply starting before the user turn's audio stream ends,
   i.e. the robot jumped in during a mid-sentence pause)

Not part of the default pytest suite.
"""

import argparse
import asyncio
import json
import statistics
import struct
import time
import wave
from pathlib import Path

import aiohttp

from backend.protocol import PCM_100MS_BYTES, decode_audio_frame, encode_audio_frame

FIXTURE = Path(__file__).parent.parent / "tests" / "fixtures" / "zh_help_16k.wav"
LATENCY_TARGET_MS = 800


def load_packets(fixture: Path) -> tuple[list[bytes], int]:
    """Return (100 ms packets, speech_end_ms): offset of the last non-silent
    sample, so latency/cut-in measure from when the user actually stopped
    speaking rather than the end of the WAV file."""
    with wave.open(str(fixture), "rb") as wav:
        pcm = wav.readframes(wav.getnframes())
    packets = [pcm[o:o + PCM_100MS_BYTES] for o in range(0, len(pcm), PCM_100MS_BYTES)]
    if packets and len(packets[-1]) < PCM_100MS_BYTES:
        packets[-1] = packets[-1].ljust(PCM_100MS_BYTES, b"\x00")
    samples = struct.unpack("<{}h".format(len(pcm) // 2), pcm[: len(pcm) // 2 * 2])
    speech_end_ms = 0
    for index in range(len(samples) - 1, -1, -1):
        if abs(samples[index]) >= 300:
            speech_end_ms = round(index / 16.0)
            break
    return packets, speech_end_ms


def _ssl_context():
    # Homebrew Python's default CA path misses common issuers on macOS.
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


def _is_cjk(text: str) -> bool:
    return any("\u2e80" <= ch <= "\u9fff" for ch in text)


def reply_quality(user_text: str, reply_text: str) -> dict:
    readable_chars = set("*_#`>|[]")
    has_markdown = any(ch in readable_chars for ch in reply_text)
    has_emoji = any(0x1F000 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF
                    for ch in reply_text)
    sentences = [s for s in reply_text.replace("!", ".").replace("?", ".").replace("！", "。").replace("？", "。").split(".") if s.strip()]
    sentences = [s for s in sentences if s.strip()]
    expected_cjk = _is_cjk(user_text)
    actual_cjk = _is_cjk(reply_text)
    return {
        "language_match": expected_cjk == actual_cjk,
        "readable": not has_markdown and not has_emoji,
        "sentences": len(sentences),
        "length_ok": 1 <= len(sentences) <= 4,
    }


def compute_metrics(summary: dict, turns_log: list[dict], noise_windows: list[dict]) -> dict:
    replies = summary["replies"]
    asr_finals = summary["asr_finals"]
    marks = summary["interruptions"]

    # Link each turn to the ASR final and reply that fall inside its window.
    turns = []
    for i, log in enumerate(turns_log):
        window_end = turns_log[i + 1]["stream_start_ms"] if i + 1 < len(turns_log) else None
        final = next((a for a in asr_finals
                      if a["at_ms"] >= log["stream_start_ms"]
                      and (window_end is None or a["at_ms"] < window_end)), None)
        reply = next((b for b in replies.values()
                      if b["first_delta_at_ms"] is not None
                      and b["first_delta_at_ms"] >= log["stream_start_ms"]
                      and (window_end is None or b["first_delta_at_ms"] < window_end)), None)
        entry = {"fixture": log["fixture"], "user_text": final["text"] if final else None}
        if reply:
            entry["reply_text"] = reply["text"]
            entry["quality"] = reply_quality(final["text"] if final else "", reply["text"])
            if reply["first_audio_at_ms"] is not None:
                entry["latency_speech_end_to_audio_ms"] = reply["first_audio_at_ms"] - log["user_speech_end_at_ms"]
                if final:
                    entry["latency_asr_final_to_audio_ms"] = reply["first_audio_at_ms"] - final["at_ms"]
            # Server-caliber latency, identical to the browser recording test's
            # "打点口径(服务端)": server receipt of the frame holding speech end
            # -> server finished sending the first audio frame. Pure server
            # monotonic clocks, so lean and browser runs are directly comparable.
            timing = reply.get("timing") or {}
            speech_end_received = timing.get("speechEndInputReceivedMs")
            send_completed = timing.get("firstFrameSendCompletedMs")
            if speech_end_received is not None and send_completed is not None:
                entry["latency_server_dotted_ms"] = round(send_completed - speech_end_received, 1)
            # Segment decomposition, same definitions as the browser recording
            # test's serverParts, for cross-tool segment-level comparison.
            asr_final = timing.get("asrFinalMs")
            bot_first = timing.get("botFirstTextMs")
            tts_first = timing.get("ttsFirstPcmMs")
            if speech_end_received is not None and asr_final is not None:
                entry["latency_endpoint_ms"] = round(asr_final - speech_end_received, 1)
            if asr_final is not None and bot_first is not None:
                entry["latency_bot_first_text_ms"] = round(bot_first - asr_final, 1)
            if bot_first is not None and tts_first is not None:
                entry["latency_tts_first_pcm_ms"] = round(tts_first - bot_first, 1)
        # Cut-in: the robot started speaking before the user actually
        # finished speaking (per the fixture's real speech end, not the WAV
        # file end — generated fixtures carry trailing silence).
        entry["cut_in"] = bool(
            reply and reply["first_audio_at_ms"] is not None
            and reply["first_audio_at_ms"] < log["user_speech_end_at_ms"] - 50)
        turns.append(entry)

    latencies = [t.get("latency_speech_end_to_audio_ms") for t in turns
                 if t.get("latency_speech_end_to_audio_ms") is not None]
    asr_latencies = [t.get("latency_asr_final_to_audio_ms") for t in turns
                     if t.get("latency_asr_final_to_audio_ms") is not None]

    # False interruption: playback_interrupt inside a noise-injection window.
    false_marks = []
    for window in noise_windows:
        false_marks.extend(m for m in marks
                           if window["start_ms"] - 300 <= m["at_ms"] <= window["end_ms"] + 3000)
    reply_count = len([b for b in replies.values() if b["first_delta_at_ms"] is not None])

    return {
        "latency_target_ms": LATENCY_TARGET_MS,
        "latency_speech_end_to_audio": {
            "per_turn_ms": latencies,
            "p50_ms": statistics.median(latencies) if latencies else None,
            "within_target": sum(1 for v in latencies if v <= LATENCY_TARGET_MS),
            "total": len(latencies),
        },
        "latency_asr_final_to_audio_p50_ms": statistics.median(asr_latencies) if asr_latencies else None,
        "latency_server_dotted_p50_ms": statistics.median(
            [t["latency_server_dotted_ms"] for t in turns if "latency_server_dotted_ms" in t]
        ) if any("latency_server_dotted_ms" in t for t in turns) else None,
        "false_interruptions": {
            "noise_injections": len(noise_windows),
            "false_interrupts": len(false_marks),
            "rate_per_injection": round(len(false_marks) / len(noise_windows), 3) if noise_windows else None,
            "rate_per_reply": round(len(false_marks) / reply_count, 3) if reply_count else None,
        },
        "cut_ins": {
            "turns": len(turns),
            "cut_in_turns": sum(1 for t in turns if t["cut_in"]),
            "rate": round(sum(1 for t in turns if t["cut_in"]) / len(turns), 3) if turns else None,
        },
        "quality": {
            "turns": len(turns),
            "language_match": sum(1 for t in turns if t.get("quality", {}).get("language_match")),
            "readable": sum(1 for t in turns if t.get("quality", {}).get("readable")),
            "length_ok": sum(1 for t in turns if t.get("quality", {}).get("length_ok")),
        },
        "turns": turns,
    }


async def run(url: str, fixtures: list[Path], noise_fixture: Path | None,
              noise_after_reply_ms: int, auth: str | None = None) -> dict:
    summary = {
        "sessionId": None,
        "replies": {},
        "asr_finals": [],
        "interruptions": [],
        "errors": [],
        "audio_frames_total": 0,
        "avatar_frames_total": 0,
    }
    turns_log: list[dict] = []
    noise_windows: list[dict] = []
    noise_packets = load_packets(noise_fixture)[0] if noise_fixture else []
    t0 = time.monotonic()

    def elapsed() -> int:
        return round((time.monotonic() - t0) * 1000)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_context())) as session:
        headers = None
        if auth:
            import base64
            headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode()}
        ws = await session.ws_connect(url, headers=headers)
        req_id = 0

        async def send_control(payload: dict) -> None:
            nonlocal req_id
            req_id += 1
            payload.setdefault("version", 1)
            payload.setdefault("requestId", f"req-{req_id}")
            await ws.send_json(payload)

        async def expect(message_type: str, timeout: float = 30.0) -> dict:
            while True:
                msg = await asyncio.wait_for(ws.receive(), timeout)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == message_type:
                        return data
                    if data.get("type") == "error":
                        raise RuntimeError(f"server error: {data}")

        # prepare -> create_session
        await send_control({"type": "prepare", "audio": {
            "codec": "pcm_s16le", "sampleRate": 16000, "channels": 1, "frameDurationMs": 100,
        }})
        prepared = await expect("prepare_result")
        assert prepared["ok"], prepared
        await send_control({"type": "create_session"})
        created = await expect("create_session_result")
        assert created["ok"], created
        session_id = created["sessionId"]
        summary["sessionId"] = session_id

        state = {
            "current_reply": None,
        }

        def reply_bucket(reply_id):
            return summary["replies"].setdefault(reply_id, {
                "first_delta_at_ms": None,
                "deltas": 0,
                "text": "",
                "first_audio_at_ms": None,
                "audio_frames": 0,
                "avatar_frames": 0,
                "complete_at_ms": None,
                "timing": None,
            })

        async def reader() -> None:
            while True:
                msg = await ws.receive()
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frame = decode_audio_frame(msg.data)
                    summary["audio_frames_total"] += 1
                    bucket = reply_bucket(state["current_reply"])
                    bucket["audio_frames"] += 1
                    if bucket["first_audio_at_ms"] is None:
                        bucket["first_audio_at_ms"] = elapsed()
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    mtype = data.get("type")
                    if mtype == "assistant_delta":
                        state["current_reply"] = data.get("replyId")
                        bucket = reply_bucket(data.get("replyId"))
                        bucket["deltas"] += 1
                        bucket["text"] += data.get("text") or ""
                        if bucket["first_delta_at_ms"] is None:
                            bucket["first_delta_at_ms"] = elapsed()
                    elif mtype == "avatar_frame":
                        summary["avatar_frames_total"] += 1
                        if data.get("replyId"):
                            state["current_reply"] = data.get("replyId")
                        bucket = reply_bucket(data.get("replyId"))
                        bucket["avatar_frames"] += 1
                        if data.get("timing"):
                            bucket["timing"] = data["timing"]
                    elif mtype == "asr_result":
                        if data.get("status") == 3:
                            summary["asr_finals"].append({
                                "at_ms": elapsed(), "text": data.get("text"),
                                "turnId": data.get("turnId"),
                            })
                    elif mtype == "playback_interrupt":
                        bucket = reply_bucket(data.get('replyId'))
                        bucket['interrupted'] = True
                        await send_control({'type': 'client_event', 'event': {
                            'eventType': 'playback_stopped', 'replyId': data.get('replyId'),
                            'sessionId': session_id, 'attributes': {'playedMs': 0},
                        }})  # Headless fixture: no physical playback is claimed.
                        summary["interruptions"].append({
                            "at_ms": elapsed(), "replyId": data.get("replyId"),
                        })
                    elif mtype == "session_event":
                        event = data.get("event") or {}
                        etype = str(event.get("eventType", ""))
                        if event.get("attributes", {}).get("timing") and event.get("replyId"):
                            bucket = reply_bucket(event.get("replyId"))
                            merged = dict(bucket.get("timing") or {})
                            merged.update(event["attributes"]["timing"])
                            bucket["timing"] = merged
                        if etype.startswith("text_bot_cache_") or etype in (
                                "text_bot_prepare_requested", "text_bot_prepare_failed"):
                            summary.setdefault("bot_cache_events", []).append({
                                "at_ms": elapsed(), "eventType": etype,
                                "attributes": event.get("attributes"),
                            })
                    elif mtype == "reply_complete":
                        bucket = reply_bucket(data.get("replyId"))
                        bucket["complete_at_ms"] = elapsed()
                    elif mtype == "reply_failed":
                        summary["errors"].append({"type": "reply_failed", "at_ms": elapsed(), "data": data})
                    elif mtype == "error":
                        summary["errors"].append({"type": "error", "at_ms": elapsed(), "data": data})
                    elif mtype in ("close_session",) :
                        return
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.ERROR):
                    return

        reader_task = asyncio.create_task(reader())

        async def stream_packets(packets: list[bytes], sequence: int) -> int:
            for payload in packets:
                await ws.send_bytes(encode_audio_frame(
                    0, session_id, sequence, int(time.time() * 1000), payload))
                sequence += 1
                await asyncio.sleep(0.1)
            return sequence

        # Give the opening reply a head start, then stream each fixture as its own turn.
        await asyncio.sleep(2.5)
        sequence = 0
        silence = b"\x00" * PCM_100MS_BYTES
        for turn_index, fixture in enumerate(fixtures):
            packets, speech_end_ms = load_packets(fixture)
            turn_start = elapsed()
            sequence = await stream_packets(packets, sequence)
            turns_log.append({
                "fixture": fixture.name,
                "stream_start_ms": turn_start,
                "stream_end_ms": elapsed(),
                "user_speech_end_at_ms": turn_start + speech_end_ms,
            })
            # Browsers keep streaming microphone silence between turns; mimic that
            # so the VAD sees the trailing silence and can close the utterance.
            noise_injected = False
            deadline = time.monotonic() + 45.0
            while time.monotonic() < deadline:
                if summary["errors"]:
                    break
                # Inject the noise burst once, while the reply to this turn is
                # already playing — the gate must reject it as a false barge-in.
                if (noise_packets and not noise_injected
                        and turn_index == 0
                        and summary["replies"]):
                    playing = [b for b in summary["replies"].values()
                               if b["first_audio_at_ms"] is not None
                               and b["first_audio_at_ms"] >= turn_start]
                    if playing and elapsed() >= max(b["first_audio_at_ms"] for b in playing) + noise_after_reply_ms:
                        noise_injected = True
                        window = {"start_ms": elapsed()}
                        sequence = await stream_packets(noise_packets, sequence)
                        window["end_ms"] = elapsed()
                        noise_windows.append(window)
                        continue
                await ws.send_bytes(encode_audio_frame(
                    0, session_id, sequence, int(time.time() * 1000), silence))
                sequence += 1
                done = [(rid, b) for rid, b in summary["replies"].items()
                               if not b.get('interrupted') and b["first_delta_at_ms"] is not None
                        and b["first_delta_at_ms"] >= turn_start
                        and b["complete_at_ms"] is not None]
                if done:
                    # A real browser acks playback as soon as the audio has
                    # finished playing; without this ack the server never
                    # appends the assistant turn to the conversation history
                    # and later LLM requests see only unanswered user turns.
                    for rid, _ in done:
                        await send_control({"type": "client_event", "event": {
                            "eventType": "playback_completed",
                            "replyId": rid, "sessionId": session_id,
                        }})
                    break
                await asyncio.sleep(0.1)

        # Finalize history so the session closes cleanly.
        for reply_id in list(summary["replies"]):
            if summary['replies'][reply_id].get('interrupted') or summary['replies'][reply_id]['complete_at_ms'] is None:
                continue
            await send_control({"type": "client_event", "event": {
                "eventType": "playback_completed", "replyId": reply_id,
                "sessionId": session_id,
            }})
        await send_control({"type": "close_session", "reason": "e2e_test_done"})
        try:
            await asyncio.wait_for(reader_task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            reader_task.cancel()

        summary["uplink_packets"] = sequence
        summary["noise_windows"] = noise_windows
        summary["metrics"] = compute_metrics(summary, turns_log, noise_windows)
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    parser.add_argument("--auth", default=None,
                        help="HTTP Basic auth as user:pass for deployments behind an authenticating proxy.")
    parser.add_argument("--fixture", action="append", type=Path, default=None,
                        help="WAV fixture per turn; repeatable. Defaults to the zh fixture.")
    parser.add_argument("--noise", type=Path, default=None,
                        help="WAV noise burst played back during the first reply to test false interruptions.")
    parser.add_argument("--noise-after-reply-ms", type=int, default=1200,
                        help="Delay between reply first audio and noise injection.")
    args = parser.parse_args()
    fixtures = args.fixture if args.fixture else [FIXTURE]
    summary = asyncio.run(run(args.url, fixtures, args.noise, args.noise_after_reply_ms, args.auth))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
