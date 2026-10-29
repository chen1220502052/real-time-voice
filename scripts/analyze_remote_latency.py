"""Read-only journal projection and per-reply latency audit. No voice imports."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sys


def project(lines):
    rows, malformed, suppressed = [], 0, 0
    strings = {"event", "traceId", "sessionId", "turnId", "replyId", "requestId", "eventType", "span",
               "status", "cacheOutcome", "outcome"}
    numbers = {"status", "sequence", "speechTime", "speech_time", "start_time", "end_time", "count", "epoch",
               "audioReceived", "audioSent", "lagMs", "prefetches", "adoptedReady", "exactMatch",
               "enabled", "supported", "historyMessages"}
    for line in lines:
        try:
            raw = json.loads(line)
            message = raw.get("MESSAGE", "")
            if "Suppressed " in message:
                suppressed += 1
            value = json.loads(message)
            if not isinstance(value, dict) or "event" not in value:
                continue
            row = {"wallMs": int(raw["__REALTIME_TIMESTAMP"]) / 1000,
                   "monoMs": int(raw["__MONOTONIC_TIMESTAMP"]) / 1000, "pid": raw.get("_PID")}
            for k, v in value.items():
                if k in strings and (isinstance(v, str) or v is None):
                    row[k] = v
                elif (k in numbers or k.endswith("Ms") or k.endswith("Chars")) and (
                        isinstance(v, (int, float, bool)) or v is None):
                    row[k] = v
            rows.append(row)
        except (ValueError, TypeError, KeyError):
            malformed += 1
    return {"capturedAt": datetime.now(timezone.utc).isoformat(), "rows": rows,
            "nonStructuredLines": malformed, "suppressionNotices": suppressed,
            "privacy": "No message text, prompts, audio or credentials retained."}


def describe(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "median": None, "min": None, "max": None}
    return {"n": len(values), "mean": statistics.mean(values), "median": statistics.median(values),
            "min": min(values), "max": max(values)}


def analyze(source, tests):
    rows = source["rows"]
    starts = {r["replyId"]: r for r in rows if r["event"] == "reply.started"}
    finals = {(r["pid"], r.get("sessionId"), r.get("turnId")): r for r in rows
              if r["event"] == "asr.vad_status" and r.get("status") == 3}
    bot = {r["requestId"]: r for r in rows if r["event"] == "bot.latency"}
    caches = {r["requestId"]: r for r in rows if r["event"] == "duplex.prefetch"}
    sessions = {r.get("traceId"): r.get("sessionId") for r in rows if r["event"] == "session.created"}
    original_starts = set(starts)
    for reply in set(bot) | set(caches):
        if reply not in starts:
            row = bot.get(reply, caches.get(reply))
            starts[reply] = {**row, "sessionId": sessions.get(row.get("traceId")), "turnId": None}
    interrupted = {r["replyId"] for r in rows if r["event"] in {"reply.interrupted", "playback.interrupt"}}
    replies = []
    for reply, start in starts.items():
        final = finals.get((start["pid"], start.get("sessionId"), start.get("turnId")))
        b, c = bot.get(reply, {}), caches.get(reply, {})
        cache = c.get("outcome", b.get("cacheOutcome"))
        if final is None and cache not in {"hit", "miss", "disabled", "unsupported"}:
            continue  # Exclude commands/openings, but retain logged user turns when ASR logging was suppressed.
        metrics = {k: v for k, v in b.items() if k.endswith("Ms") or k.endswith("Chars")}
        # Candidate provider milestones are relative to the candidate, never
        # subtract them from formal request milestones. Compare durations only.
        provider = c if "candidateProviderFirstTokenMs" in c else b
        prefix = "candidateProvider" if provider is c else "provider"
        headers, token, request = (provider.get(prefix + k) for k in ("HeadersMs", "FirstTokenMs", "RequestMs"))
        metrics.update(providerRequestToHeadersMs=headers-request if headers is not None and request is not None else None,
                       providerHeadersToFirstCharMs=token-headers if token is not None and headers is not None else None)
        ready = c.get("candidateFirstSentenceReadyMs") if provider is c else b.get("firstSentenceReadyMs")
        metrics["providerFirstCharToSentenceMs"] = ready-token if ready is not None and token is not None else None
        metrics["requestToFirstSentenceMs"] = b.get("firstSentenceReadyMs")
        metrics["remainingWaitMs"] = c.get("remainingWaitMs", b.get("remainingWaitMs"))
        metrics["effectiveLeadMs"] = c.get("effectiveLeadMs", b.get("effectiveLeadMs"))
        metrics["promptChars"] = c.get("candidatePromptChars", b.get("promptChars"))
        replies.append({"replyId": reply, "sessionId": start["sessionId"], "pid": start["pid"],
                        "localTime": datetime.fromtimestamp(start["wallMs"]/1000, timezone.utc).astimezone(
                            timezone(timedelta(hours=8))).isoformat(),
                        "traffic": tests.get(start["sessionId"], "other_trial"),
                        "cache": cache, "hasBotLatency": bool(b), "hasAsrFinal": final is not None,
                        "hasReplyStarted": reply in original_starts,
                        "interrupted": reply in interrupted, "status": b.get("status"),
                        "adoptedReady": c.get("adoptedReady", b.get("adoptedReady")), "metrics": metrics})
    groups = {}
    for pid in sorted({r["pid"] for r in replies}):
        for traffic in ("all", "other_trial", "deployment_voice", "deployment_interrupt"):
            chosen = [r for r in replies if r["pid"] == pid and (traffic == "all" or r["traffic"] == traffic)]
            if not chosen:
                continue
            groups[pid+":"+traffic] = {"attempts": len(chosen), "missingBotLatency": sum(not r["hasBotLatency"] for r in chosen),
                "interrupted": sum(r["interrupted"] for r in chosen),
                "hit": sum(r["cache"] == "hit" for r in chosen), "ready": sum(r["adoptedReady"] is True for r in chosen),
                "metrics": {k: describe(r["metrics"].get(k) for r in chosen) for k in (
                    "requestToFirstSentenceMs", "remainingWaitMs", "effectiveLeadMs", "historyReadyMs",
                    "eventPersistenceMs", "promptChars", "providerRequestToHeadersMs",
                    "providerHeadersToFirstCharMs", "providerFirstCharToSentenceMs")}}
    return {"sourceCapturedAt": source["capturedAt"], "groups": groups, "replies": replies,
            "eventCounts": dict(Counter(r["event"] for r in rows)),
            "clientTypes": dict(Counter(r.get("eventType") for r in rows if r["event"] == "client.event")),
            "journalSuppressionNotices": source["suppressionNotices"],
            "physicalOutput": None, "note": "No persistent per-reply first-frame/client-render timestamp in journal; no physical E2E inference."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--test-report", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.extract:
        result = project(sys.stdin)
    else:
        tests = {}
        for file in args.test_report:
            r = json.loads(file.read_text())
            tests[r["sessionId"]] = "deployment_interrupt" if "interrupt" in file.name else "deployment_voice"
        result = analyze(json.loads(args.source.read_text()), tests)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"rows": len(result.get("rows", result.get("replies", []))), "output": str(args.output)}))


if __name__ == "__main__":
    main()
