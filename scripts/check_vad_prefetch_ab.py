"""Paired prefetch on/off probe. No listening server is started.

Default: deterministic synthetic 1s Bot / 500ms VAD-window mechanism check.
--real-voice: actual configured ASR/VAD, LLM and TTS; run on the remote host,
never on the user's laptop. Playback acknowledgements are simulated.
"""

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
import uuid

from backend.bot_contract import ConversationRequest
from backend.bot_service import BotService
from backend.text_bot import BotEvent
from backend.vad_prefetch import VadPrefetch


async def synthetic(enabled, window=.5, delay=1.):
    class Model:
        protocol_version = 1
        side_effect_free = True
        calls = 0

        async def stream(self, request):
            self.calls += 1
            await asyncio.sleep(delay)
            yield BotEvent(request.request_id, "text_delta", text="A complete reply. ")
            yield BotEvent(request.request_id, "done")

    model, service = Model(), BotService()
    prefetch = VadPrefetch(service.session(model), enabled=enabled, stable_ms=0)
    query = ConversationRequest("final", "ab", "turn", text="Tell me about cats.")
    try:
        prefetch.note_audio(b"\0" * 3200)
        prefetch.offer(query)
        await asyncio.sleep(window)
        prefetch.seal("turn")
        final_at = time.monotonic()
        first = None
        text = ""
        async for event in prefetch.stream(query):
            if event.type == "text_delta":
                first = first or time.monotonic()
                text += event.text
        return {"enabled": enabled, "finalToFirstTextMs": round((first-final_at)*1000, 2),
                "text": text, "generationCalls": model.calls, **prefetch.last_cache_detail}
    finally:
        await prefetch.aclose()
        await service.close()


async def voice(enabled, fixture, tts):
    from backend.atomic import create_agent, create_asr_engine
    from backend.domain import Session, SessionState
    from backend.duplex import DuplexDialogue
    from backend.protocol import AudioFrame
    from scripts.real_e2e_client import load_packets

    asr = create_asr_engine()
    await asr.warmup()
    dialogue = DuplexDialogue(Session("ab_" + uuid.uuid4().hex, SessionState.PICK_UP),
                              asr=asr, agent=create_agent(), tts=tts)
    dialogue.prefetch.enabled = enabled
    dialogue._idle_seconds = 0
    packets, speech_end_ms = load_packets(fixture)
    result = {"enabled": enabled, "fixture": fixture.name, "errors": []}
    completed = asyncio.Event()
    first_text = first_pcm = final = None
    answer = ""

    async def controls():
        nonlocal first_text, final, answer
        async for event in dialogue.events():
            if event.kind == "asr_result" and event.payload.get("status") == 3:
                final = time.monotonic()
                result["asrFinal"] = event.payload.get("text", "")
            elif event.kind == "assistant_delta":
                first_text = first_text or time.monotonic()
                answer += event.payload["text"]
            elif event.kind == "reply_complete":
                await dialogue.mark_playback_completed(event.payload["replyId"])
                completed.set()
            elif event.kind in {"error", "reply_failed"}:
                result["errors"].append(event.payload.get("code"))
                completed.set()

    async def media():
        nonlocal first_pcm
        async for event in dialogue.media_events():
            first_pcm = first_pcm or time.monotonic()
            await dialogue.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])

    await dialogue.start()
    consumers = [asyncio.create_task(controls()), asyncio.create_task(media())]
    started = time.monotonic()
    try:
        sequence = 0
        async with asyncio.timeout(45):
            while not completed.is_set():
                pcm = packets[sequence] if sequence < len(packets) else b"\0" * 3200
                await dialogue.push_audio(AudioFrame(0, 0, sequence, (sequence+1)*100, pcm))
                sequence += 1
                await asyncio.sleep(max(0, started + sequence*.1 - time.monotonic()))
        assert not result["errors"] and first_pcm and first_text and final, result
        result.update(
            reply=answer,
            speechEndToFirstPcmMs=round((first_pcm-started)*1000-speech_end_ms, 2),
            finalToFirstPcmMs=round((first_pcm-final)*1000, 2),
            finalToFirstTextMs=round((first_text-final)*1000, 2),
            cacheOutcome=dialogue.prefetch.last_outcome,
            **dialogue.prefetch.last_cache_detail,
        )
        assert first_pcm >= final, "Audio was published before VAD final"
        if "twenty three plus forty five" in result["asrFinal"].lower():
            assert any(value in answer.lower() for value in ("68", "sixty eight", "sixty-eight")), result
        return result
    finally:
        await dialogue.close("ab_done")
        for task in consumers:
            task.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)


async def check(args):
    report = {"mode": "real_asr_llm_tts" if args.real_voice else "synthetic_mechanism",
              "measurement": "server_first_pcm_no_browser_network" if args.real_voice else "first_text",
              "playbackAcknowledgement": "simulated", "pairs": []}
    from backend.llm_runtime import LLMHttpPool, use_llm_pool
    pool = LLMHttpPool()
    try:
        with use_llm_pool(pool):
            if args.real_voice:
                from backend.atomic import create_tts
                tts = create_tts()
                await tts.warmup()
                fixtures = args.fixture or [Path("tests/fixtures/en") / name for name in
                                           ("en_hello_16k.wav", "en_help_16k.wav", "en_numbers_16k.wav")]
            else:
                fixtures, tts = [None], None
            for repeat in range(args.repeat):
                for index, fixture in enumerate(fixtures):
                    arms = {}
                    # Alternate order to reduce always-on-second warmup bias.
                    for enabled in ((False, True) if (repeat+index)%2 == 0 else (True, False)):
                        row = await voice(enabled, fixture, tts) if args.real_voice else await synthetic(enabled)
                        arms["on" if enabled else "off"] = row
                    metric = "speechEndToFirstPcmMs" if args.real_voice else "finalToFirstTextMs"
                    report["pairs"].append({**arms, "savedMs": round(arms["off"][metric]-arms["on"][metric], 2)})
            savings = [pair["savedMs"] for pair in report["pairs"]]
            report["medianPairedSavedMs"] = statistics.median(savings)
            report["note"] = "Small interleaved pilot, not a production SLA or statistically conclusive benchmark."
    finally:
        await pool.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-voice", action="store_true")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--fixture", type=Path, action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(check(args))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
