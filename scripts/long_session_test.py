#!/usr/bin/env python3.11
"""Exercise one continuous WebSocket session for the configured duration."""

import argparse
import asyncio
import json
import math
import os
import resource
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
from aiohttp import web

from backend.app import create_app
from backend.atomic import DemoAgent, DemoTts, VadAsrStub
from backend.protocol import encode_audio_frame


async def receive_until(ws, wanted, latest_stats, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = await asyncio.wait_for(ws.receive(), deadline - time.monotonic())
        if message.type == aiohttp.WSMsgType.BINARY and wanted == "binary":
            return {"type": "binary", "bytes": len(message.data)}
        if message.type != aiohttp.WSMsgType.TEXT:
            continue
        payload = json.loads(message.data)
        if payload.get("type") == "runtime_stats":
            latest_stats.clear()
            latest_stats.update(payload)
        if payload.get("type") == "error":
            raise RuntimeError(f"server error: {payload.get('code')}")
        if payload.get("type") == wanted:
            return payload
    raise TimeoutError(wanted)


def current_rss_bytes():
    """Return current RSS, not the monotonic high-water value from ru_maxrss."""
    if sys.platform == "linux":
        with open("/proc/self/statm", encoding="ascii") as statm:
            resident_pages = int(statm.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(result.stdout.strip()) * 1024
    # Portable fallback is a peak rather than current RSS; keep it explicit in
    # the result so unsupported platforms do not silently claim leak coverage.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw * 1024


async def run(duration, turn_interval, sample_interval, max_rss_growth_mb):
    app = create_app(
        asr_factory=VadAsrStub,
        agent_factory=DemoAgent,
        tts_factory=DemoTts,
        warmup=False,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    latest_stats = {}
    rss_samples = []
    thread_samples = []
    task_samples = []
    turns = sequence = downlink_packets = 0
    errors = []
    started = time.monotonic()
    next_sample_at = started

    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{port}/ws") as ws:
                await ws.send_json({"type": "prepare", "version": 1, "audio": {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1, "frameDurationMs": 100}})
                await receive_until(ws, "prepare_result", latest_stats)
                await ws.send_json({"type": "create_session", "version": 1})
                created = await receive_until(ws, "create_session_result", latest_stats)

                opening = await receive_until(ws, "reply_complete", latest_stats)
                await ws.send_json({"type": "client_event", "version": 1, "event": {"eventType": "playback_completed", "replyId": opening["replyId"]}})

                samples = [int(9000 * math.sin(2 * math.pi * 440 * i / 16000)) for i in range(1600)]
                voiced = struct.pack("<1600h", *samples)
                silence = b"\x00" * 3200
                while time.monotonic() - started < duration:
                    for pcm in (voiced, voiced, silence):
                        await ws.send_bytes(encode_audio_frame(0, created["sessionId"], sequence, int(time.time() * 1000), pcm))
                        sequence += 1
                    reply = await receive_until(ws, "reply_complete", latest_stats)
                    await ws.send_json({"type": "client_event", "version": 1, "event": {"eventType": "playback_completed", "replyId": reply["replyId"]}})
                    turns += 1
                    downlink_packets = max(downlink_packets, int(latest_stats.get("audioSent", 0)))
                    now = time.monotonic()
                    if now >= next_sample_at:
                        rss_samples.append(current_rss_bytes())
                        thread_samples.append(threading.active_count())
                        task_samples.append(len([task for task in asyncio.all_tasks() if not task.done()]))
                        next_sample_at = now + sample_interval
                    await asyncio.sleep(turn_interval)

                await ws.send_json({"type": "close_session", "version": 1, "sessionId": created["sessionId"]})
                await receive_until(ws, "close_session", latest_stats)
    except Exception as exc:
        errors.append(str(exc))
    finally:
        await asyncio.sleep(0.1)
        pending_before_cleanup = len([task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()])
        await runner.cleanup()

    queue_peaks = {
        key: int(latest_stats.get(key, 0))
        for key in ("inputQueue", "controlQueue", "mediaOutputQueue", "mediaCache")
    }
    rss_growth = (rss_samples[-1] - rss_samples[0]) if len(rss_samples) > 1 else 0
    result = {
        "durationSeconds": round(time.monotonic() - started, 2),
        "singleSession": True,
        "turns": turns,
        "uplinkPackets": sequence,
        "downlinkPackets": downlink_packets,
        "errors": errors,
        "queueSnapshot": queue_peaks,
        "rssBytes": {"first": rss_samples[0] if rss_samples else 0, "max": max(rss_samples, default=0), "last": rss_samples[-1] if rss_samples else 0, "growth": rss_growth},
        "rssMetric": "current" if sys.platform in {"darwin", "linux"} else "peak_fallback",
        "threads": {"first": thread_samples[0] if thread_samples else 0, "max": max(thread_samples, default=0), "last": thread_samples[-1] if thread_samples else 0},
        "tasks": {"first": task_samples[0] if task_samples else 0, "max": max(task_samples, default=0), "last": task_samples[-1] if task_samples else 0},
        "pendingTasksBeforeCleanup": pending_before_cleanup,
        "pid": os.getpid(),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if (
        errors
        or any(value > 10 for value in queue_peaks.values())
        or pending_before_cleanup > 1
        or rss_growth > max_rss_growth_mb * 1024 * 1024
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=1800, help="single-session duration in seconds")
    parser.add_argument("--turn-interval", type=float, default=0.2, help="pause between completed turns")
    parser.add_argument("--sample-interval", type=float, default=10, help="resource sampling interval in seconds")
    parser.add_argument("--max-rss-growth-mb", type=float, default=64, help="fail if current RSS grows beyond this limit")
    args = parser.parse_args()
    asyncio.run(run(args.duration, args.turn_interval, args.sample_interval, args.max_rss_growth_mb))
