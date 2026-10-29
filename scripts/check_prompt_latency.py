"""Remote-only interleaved baseline/compact-prompt real LLM benchmark.

No voice server or model is started. Both arms share the same provider client,
messages and grading; only EnglishChatBot prompt/material construction differs.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time


def load_baseline(root):
    def load(name, file):
        spec = importlib.util.spec_from_file_location(name, root / "backend" / file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    materials = load("backend._baseline_materials", "chat_materials.py")
    english = load("backend._baseline_english", "english_chat.py")
    english.material_context = materials.material_context
    return english.EnglishChatBot


async def check(args, report):
    from backend.english_chat import EnglishChatBot
    from backend.llm_runtime import LLMHttpPool, Timing, use_llm_pool, use_timing
    from backend.providers.llm import OllamaAgent
    from backend.text_bot import BotMessage, BotRequest, stream_bot

    baseline_root = Path("/opt/real-time-voice")
    baseline = load_baseline(baseline_root)
    report["baselineHashes"] = {name: hashlib.sha256((baseline_root / "backend" / name).read_bytes()).hexdigest()
                                 for name in ("english_chat.py", "chat_materials.py")}
    cases = [
        ("greeting", (BotMessage("user", "Hello, how are you today?"),)),
        ("numbers", (BotMessage("user", "What is twenty three plus forty five?"),)),
        ("chinese_help", (BotMessage("user", "What does guitar mean? Please explain in Chinese."),)),
        ("memory", (BotMessage("user", "I like basketball. My favorite player is Curry."),
                    BotMessage("assistant", "Basketball is fun with friends."),
                    BotMessage("user", "Who is my favorite player?"))),
    ]
    pool = LLMHttpPool()
    try:
        with use_llm_pool(pool):
            # Warm TLS/provider path with an unscored request, not a scored arm.
            warm = BotRequest("warm", "warm", "warm", messages=cases[0][1], options={"grade": 7})
            _ = [e async for e in stream_bot(EnglishChatBot(OllamaAgent), warm)]
            for repeat in range(args.repeat):
                for index, (name, messages) in enumerate(cases):
                    pair = {"case": name, "repeat": repeat, "arms": {}}
                    report["pairs"].append(pair)
                    for arm in (("baseline", "compact") if (repeat + index) % 2 == 0 else ("compact", "baseline")):
                        bot = (baseline if arm == "baseline" else EnglishChatBot)(OllamaAgent)
                        query = BotRequest(f"p_{repeat}_{index}_{arm}", "bench", "turn",
                                           messages=messages, options={"grade": 7})
                        probe, started, first, text = Timing(query.request_id), time.monotonic(), None, ""
                        events = []
                        with use_timing(probe):
                            async for event in stream_bot(bot, query):
                                events.append(event)
                                if event.type == "text_delta":
                                    first = first or time.monotonic()
                                    text += event.text
                        probe.metrics["observedFirstSentenceMs"] = round((first - started)*1000, 2)
                        assert events[-1].type == "done" and text.strip()
                        if name == "numbers":
                            assert any(n in text.lower() for n in ("68", "sixty eight", "sixty-eight")), text
                        if name == "memory":
                            assert "curry" in text.lower(), text
                        if name == "chinese_help":
                            assert "吉他" in text, text
                        pair["arms"][arm] = {"text": text, "metrics": probe.metrics}
                        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                        print(json.dumps({"case": name, "repeat": repeat, "arm": arm,
                                          "promptChars": probe.metrics.get("promptChars"),
                                          "providerFirstTokenMs": probe.metrics.get("providerFirstTokenMs")}), flush=True)
            report["summary"] = {}
            for key in ("promptChars", "providerFirstTokenMs", "observedFirstSentenceMs"):
                values = {arm: [p["arms"][arm]["metrics"][key] for p in report["pairs"]]
                          for arm in ("baseline", "compact")}
                paired = [a-b for a,b in zip(values["baseline"], values["compact"])]
                report["summary"][key] = {"baselineMedian": statistics.median(values["baseline"]),
                                           "compactMedian": statistics.median(values["compact"]),
                                           "pairedSavedMedian": statistics.median(paired),
                                           "pairedSavedMean": statistics.mean(paired)}
            report.update(status="passed", finishedAt=datetime.now(timezone.utc).isoformat())
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    if sys.platform != "linux" or not Path("/etc/real-time-voice.env").is_file() or args.output.exists():
        parser.error("Use a fresh evidence file on the remote ECS only")
    report = {"status": "running", "mode": "real_llm_prompt_ab", "startedAt": datetime.now(timezone.utc).isoformat(),
              "pairs": [], "note": "Interleaved small pilot, not an SLA or conclusive causal estimate."}
    try:
        asyncio.run(check(args, report))
    except Exception as error:
        report.update(status="failed", failureType=type(error).__name__)
        raise SystemExit(1) from None
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
