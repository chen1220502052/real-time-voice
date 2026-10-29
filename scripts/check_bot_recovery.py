"""Real Bot API checks with explicit playback fixtures, not a device test."""

import argparse
import asyncio
import json
from pathlib import Path
import ssl
import uuid

import aiohttp
import certifi


async def check(url, auth_file=None):
    headers = {}
    if auth_file:
        labels = dict(line.split(":", 1) for line in auth_file.read_text().splitlines() if ":" in line)
        headers["Authorization"] = aiohttp.encode_basic_auth(labels["Username"].strip(), labels["Password"].strip())
    session = "recovery_" + uuid.uuid4().hex
    reports = []
    async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(
            ssl=ssl.create_default_context(cafile=certifi.where()))) as client:
        async def request(text, **extra):
            name = "req_" + uuid.uuid4().hex
            body = {"version": 2, "sessionId": session, "requestId": name, "turnId": name,
                    "replyId": name, "channel": "voice", "text": text,
                    "options": {"grade": 7}, **extra}
            events = await send(body)
            return body, events

        async def send(body, expected=200):
            async with client.post(url.rstrip("/") + "/api/text-bot/replies", json=body) as response:
                text = await response.text()
                assert response.status == expected, (response.status, text[:160])
                values = [json.loads(line) for line in text.splitlines()]
                if expected == 200:
                    assert values and values[-1]["type"] == "done", values
                    assert all(e["version"] == 2 for e in values)
                return values

        async def observe(reply, facts):
            events = [{"eventId": "fact_" + uuid.uuid4().hex, "replyId": reply,
                       "sequence": i + 1, **f} for i, f in enumerate(facts)]
            for _ in range(2):  # Exact event retries must be harmless.
                async with client.post(url.rstrip("/") + "/api/text-bot/interactions", json={
                    "sessionId": session, "interactions": events,
                }) as response:
                    assert response.status == 200, await response.text()
                    await response.read()

        def answer(events):
            return "".join(e.get("text", "") for e in events if e["type"] == "text_delta").strip()

        original, events = await request("I like basketball. My favorite player is Curry.")
        assert await send(original) == events
        assert await send(dict(original, afterEventSeq=1)) == events[1:]
        await send(dict(original, text="Changed content"), expected=409)
        await observe(original["replyId"], [{"type": "generation_cancelled"},
                                                {"type": "playback_stopped", "playedMs": 0}])
        recall, values = await request("Who is my favorite player?")
        assert "curry" in answer(values).lower(), answer(values)
        reports.append({"scenario": "unplayed_reply_does_not_rollback_user_fact", "answer": answer(values)})
        await observe(recall["replyId"], [{"type": "playback_completed"}])

        draft, values = await request("Say two very short sentences about cats. Ask a question last.")
        chunks = [e["text"] for e in values if e["type"] == "text_delta"]
        assert chunks and answer(values)
        await observe(draft["replyId"], [{"type": "generation_cancelled"},
                                             {"type": "playback_stopped", "playedMs": 0}])
        resumed, values = await request("Please continue what you were saying.")
        resumed_text = answer(values)
        assert any(word in resumed_text.lower() for word in ("cat", "pet", "kitten")), resumed_text
        reports.append({"scenario": "continue_unplayed_draft", "draft": "".join(chunks), "answer": resumed_text})
        await observe(resumed["replyId"], [{"type": "playback_completed"}])

        draft, values = await request("Say two very short sentences about dogs. Ask a question last.")
        chunks = [e["text"] for e in values if e["type"] == "text_delta"]
        assert len(chunks) >= 2, ("Expected two speech segments", values)
        await observe(draft["replyId"], [
            {"type": "speech_segment", "text": chunks[0], "audioEndMs": 500},
            {"type": "speech_segment", "text": "".join(chunks[1:]), "audioEndMs": 1000},
            {"type": "generation_cancelled"}, {"type": "playback_stopped", "playedMs": 550},
            {"type": "playback_completed"},  # Stale completion must not promote it.
        ])
        _, values = await request("Please finish what you were saying.")
        continuation = answer(values)
        assert not continuation.startswith(chunks[0].strip()), (chunks, continuation)
        reports.append({"scenario": "partial_delivery_then_continue", "deliveredPrefix": chunks[0],
                        "unheardDraft": "".join(chunks[1:]), "answer": continuation})
    return {"mode": "real_llm_structured_playback_fixture", "sessionId": session,
            "checks": ["exact_replay", "cursor_replay", "request_conflict", "duplicate_receipts"],
            "scenarios": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8874")
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(check(args.url, args.auth_file))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
