"""Remote-only, interleaved real WAV/NLS/LLM/Piper prefetch acceptance.

Runs the deployed app once on an ephemeral loopback port. Only this isolated
process changes its environment between sessions; production stays untouched.
No fake providers, no semantic reuse, no physical-speaker latency claim.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys


def summarize(pairs):
    """All completed paired user turns count, including misses and regressions."""
    if not pairs:
        return {"pairs": 0}
    result = {"pairs": len(pairs), "arms": {}}
    metrics = ("fixtureSignalEndToFirstPacketMs", "finalToFirstPacketMs", "botStartToFirstTextMs")
    for arm in ("off", "on"):
        rows = [pair[arm] for pair in pairs]
        hits = sum(row["cache"] == "hit" for row in rows)
        result["arms"][arm] = {
            "turns": len(rows), "hits": hits, "hitRate": hits / len(rows),
            "prefetchCalls": sum(row["cacheDetail"].get("prefetches", 0) for row in rows),
            "metrics": {metric: {"mean": statistics.mean(row[metric] for row in rows),
                                  "median": statistics.median(row[metric] for row in rows)}
                        for metric in metrics},
            "effectiveLeadMsMedianAllTurns": statistics.median(
                row["cacheDetail"].get("effectiveLeadMs", 0) for row in rows),
            "remainingWaitMsMedian": statistics.median(
                row["cacheDetail"]["remainingWaitMs"] for row in rows),
        }
    result["pairedSavingsMs"] = {}
    for metric in metrics:
        values = [pair["off"][metric] - pair["on"][metric] for pair in pairs]
        result["pairedSavingsMs"][metric] = {
            "mean": statistics.mean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values),
            "fasterPairs": sum(value > 0 for value in values),
        }
    result["sameTranscriptPairs"] = sum(pair["off"]["asrFinal"] == pair["on"]["asrFinal"] for pair in pairs)
    return result


def turn(report):
    assert report.get("status", "passed") == "passed" and not report["errors"], report.get("errors")
    assert len(report["asrFinals"]) == 1 and len(report["replies"]) == 2
    row = dict(report["replies"][1], asrFinal=report["asrFinals"][0])
    stages = row["serverStages"]
    row["botStartToFirstTextMs"] = stages["botStartToFirstTextMs"]
    timing = row["timing"]
    # Same server monotonic clock: do not subtract client and server timestamps.
    row["finalToFirstPacketMs"] = timing["firstFrameSendStartedMs"] - timing["asrFinalMs"]
    return row


async def check(args, report):
    # Imports that can load models are deliberately below the remote-only guard.
    import backend.app as app_module
    from aiohttp import web
    from scripts.check_english_voice import check as voice
    from scripts.check_voice_interrupt import check as interrupt
    from scripts.check_bot_commands import check as commands
    from scripts.check_bot_recovery import check as recovery

    root = Path(app_module.__file__).resolve().parent.parent
    assert root == Path("/opt/real-time-voice"), "Test the deployed code, not another checkout"
    report["sourceHashes"] = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for folder in ("backend", "web") for p in (root / folder).rglob("*")
                              if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("._")}
    report["startedAt"] = datetime.now(timezone.utc).isoformat()
    # Keep every provider, model, VAD, interruption, and prefetch tuning setting
    # from the production EnvironmentFile; only the Boolean varies per session.
    report["config"] = {k: os.getenv(k) for k in (
        "ASR_BACKEND", "AGENT_BACKEND", "TTS_BACKEND", "ALIYUN_NLS_AUTH_MODE",
        "VAD_END_SILENCE_MS", "NLS_MAX_SENTENCE_SILENCE_MS", "DUPLEX_PREFETCH_STABLE_MS",
        "DUPLEX_PREFETCH_MAX_PER_TURN", "DUPLEX_PREFETCH_QUIET_RMS")}
    runner = web.AppRunner(app_module.create_app(bot_state_path=str(args.output.with_suffix(".sqlite3"))),
                           handler_cancellation=True, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ws_url, http_url = f"ws://127.0.0.1:{port}/ws", f"http://127.0.0.1:{port}"

    async def released():
        async with asyncio.timeout(10):
            while runner.app["resource_manager"].in_use:
                await asyncio.sleep(.05)

    def save():
        report["summary"] = summarize(report["pairs"])
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    fixtures = [args.fixture_dir / name for name in args.fixtures]
    try:
        for repeat in range(args.repeat):
            for index, fixture in enumerate(fixtures):
                pair = {"repeat": repeat, "fixture": fixture.name, "order": []}
                report["pairs"].append(pair)
                for enabled in ((False, True) if (repeat + index) % 2 == 0 else (True, False)):
                    arm = "on" if enabled else "off"
                    os.environ["DUPLEX_PREFETCH_ENABLED"] = "1" if enabled else "0"
                    await released()
                    raw = {}
                    try:
                        result = await voice(ws_url, 7, [fixture], report_out=raw)
                        pair[arm] = turn(result)
                        pair[arm]["raw"] = result
                        assert pair[arm]["cache"] != "disabled" if enabled else pair[arm]["cache"] == "disabled"
                    except BaseException:
                        pair[arm] = {"failed": True, "raw": raw}
                        raise
                    pair["order"].append(arm)
                    # Persist partial raw evidence without dropping a failed arm.
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                    print(json.dumps({"pair": len(report["pairs"]), "arm": arm,
                                      "fixture": fixture.name, "cache": pair[arm]["cache"],
                                      "leadMs": pair[arm]["cacheDetail"].get("effectiveLeadMs"),
                                      "packetMs": pair[arm]["fixtureSignalEndToFirstPacketMs"]}), flush=True)
        save()
        for enabled in (False, True):
            arm = "on" if enabled else "off"
            os.environ["DUPLEX_PREFETCH_ENABLED"] = "1" if enabled else "0"
            await released()
            multi = await voice(ws_url, 7, [args.fixture_dir / name for name in (
                "en_hello_16k.wav", "en_help_16k.wav", "en_short_16k.wav", "en_numbers_16k.wav")])
            report["multiturn"][arm] = multi
            await released()
            report["interruptions"][arm] = await interrupt(ws_url)
            save()
            print(json.dumps({"guardrails": arm, "multiturn": "passed", "interruptions": "passed"}), flush=True)
        os.environ["DUPLEX_PREFETCH_ENABLED"] = "1"
        await released()
        report["commands"] = await commands(ws_url)
        await released()
        report["recovery"] = await recovery(http_url)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        report["status"] = "passed"
        save()
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-ecs", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--fixtures", nargs="+", default=["en_hello_16k.wav", "en_pause300_16k.wav",
                                                          "en_catfact_16k.wav", "en_numbers_16k.wav"])
    args = parser.parse_args()
    if sys.platform != "linux" or not Path("/etc/real-time-voice.env").is_file():
        parser.error("Remote ECS only; never starts models or a voice service on the laptop")
    if args.repeat < 2 or args.output.exists():
        parser.error("At least two interleaved repeats and a fresh evidence file are required")
    report = {"status": "running", "mode": "remote_loopback_real_wav_nls_llm_piper",
              "measurement": "source_signal_end_to_first_ws_packet_not_speaker",
              "playbackAcknowledgement": "simulated", "physicalOutput": None,
              "pairs": [], "multiturn": {}, "interruptions": {},
              "note": "Small interleaved pilot; not a statistically conclusive SLA estimate."}
    try:
        asyncio.run(check(args, report))
    except BaseException as error:
        report.update(status="failed", failureType=type(error).__name__)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        raise SystemExit(1) from None
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
