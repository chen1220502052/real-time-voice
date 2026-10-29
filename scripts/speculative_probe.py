"""Probe the SpeculativeAgent against the real DeepSeek endpoint.

Simulates one utterance: a partial fires the speculative prefetch while the
user is still speaking; the finalized text then arrives and is matched
against the in-flight speculative stream.  Reports whether the stream was
adopted (hit) and the first-delta latency compared with a fresh request.

Not part of the default pytest suite.  Loads DEEPSEEK_API_KEY from .env.local.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from backend.atomic import OllamaAgent, SpeculativeAgent

ENV_FILE = Path(__file__).parent.parent / ".env.local"


def load_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY")
    if key:
        return key
    for line in ENV_FILE.read_text().splitlines():
        name, _, value = line.partition("=")
        if name.strip() in ("DEEPSEEK_API_KEY", "LLM_API_KEY") and value.strip():
            return value.strip().strip('"').strip("'")
    raise RuntimeError("DEEPSEEK_API_KEY not found")


async def collect(agent, messages) -> tuple[float, str]:
    t0 = time.monotonic()
    first_at = None
    parts = []
    async for delta in agent.stream_reply(messages):
        if first_at is None:
            first_at = time.monotonic() - t0
        parts.append(delta)
    return (first_at if first_at is not None else time.monotonic() - t0), "".join(parts)


async def main() -> None:
    inner = OllamaAgent(base_url="https://api.deepseek.com",
                        model=os.getenv("OLLAMA_MODEL", "deepseek-flash"),
                        api_key=load_key())
    agent = SpeculativeAgent(inner)
    base = [{"role": "user", "content": "hi"}]
    partial = "Tell me a fun fact about cats"
    final = "Tell me a fun fact about cats."

    # Speak for 1.5s after the partial appears, then the utterance finalizes.
    agent.notify_partial(partial, base)
    await asyncio.sleep(1.5)
    hit_first, hit_text = await collect(agent, base + [{"role": "user", "content": final}])

    # Baseline: a fresh request with the same final query.
    real_first, real_text = await collect(agent, base + [{"role": "user", "content": final}])

    print(json.dumps({
        "hit": agent.hits == 1 and agent.misses == 1,
        "speculative_first_delta_s": round(hit_first, 3),
        "real_first_delta_s": round(real_first, 3),
        "saved_s": round(real_first - hit_first, 3),
        "speculative_reply": hit_text[:80],
        "real_reply": real_text[:80],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
