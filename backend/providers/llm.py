"""llm provider boundary."""

import asyncio
import json
import os
import queue
import time
from typing import AsyncIterator, Iterable, Optional
import aiohttp
from ..observability import log_event
from ..llm_runtime import llm_client, timing
from .common import OPENING_MESSAGE, PICK_UP_SIGNAL


class DemoAgent:
    """Development-only text bot with deterministic session opening behavior."""

    def __init__(self) -> None:
        self.delay_ms = max(0, int(os.getenv("DEMO_LLM_CHUNK_DELAY_MS", "0")))
        self.fail = os.getenv("DEMO_LLM_ERROR", "0") == "1"

    async def stream_reply(self, messages: Iterable[dict], signal: Optional[str] = None) -> AsyncIterator[str]:
        if self.fail:
            raise RuntimeError("Stub LLM error requested by DEMO_LLM_ERROR")
        messages = list(messages)
        if signal == PICK_UP_SIGNAL:
            for chunk in ("您好，我是猫猫，", "您有什么需要帮助的吗？"):
                await asyncio.sleep(self.delay_ms / 1000)
                yield chunk
            return
        for chunk in ("我已经收到你的", "消息。"):
            await asyncio.sleep(self.delay_ms / 1000)
            yield chunk


DEFAULT_SYSTEM_PROMPT = (
    "你是猫猫，一位亲切友好的语音助手，正在和用户进行实时语音对话。"
    "始终用和用户相同的语言回复。"
    "每次回复只输出1到3个简短的口语化句子，适合直接朗读。"
    "只输出纯文本：不要markdown、列表、标题、加粗、emoji或表情符号。"
    "不要复述用户的话，不要一次问多个问题。"
)


class OllamaAgent:
    """OpenAI-compatible streaming chat adapter (Ollama, DeepSeek, or similar).

    Defaults to the local Ollama profile; set OLLAMA_BASE_URL and LLM_API_KEY
    (or DEEPSEEK_API_KEY) to target a hosted OpenAI-compatible endpoint.
    """

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 api_key: Optional[str] = None, system_prompt: Optional[str] = None,
                 max_tokens: Optional[int] = None, temperature: Optional[float] = None,
                 thinking: Optional[bool] = None) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        # Keep the local real-time profile within the memory budget of this project.
        self.model = model or os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
        self.system_prompt = system_prompt if system_prompt is not None else os.getenv("LLM_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.thinking = thinking

    async def stream_reply(self, messages: Iterable[dict], signal: Optional[str] = None) -> AsyncIterator[str]:
        messages = list(messages)
        # Opening is a text-bot policy signal, not an LLM prompt. Keeping it
        # deterministic avoids cold-model latency and prevents variations in
        # the first thing a user hears after pick-up.
        if signal == PICK_UP_SIGNAL:
            yield OPENING_MESSAGE
            return
        normalized = []
        if self.system_prompt:
            normalized.append({"role": "system", "content": self.system_prompt})
        for item in messages:
            role = item.get("role", "user")
            normalized.append({
                "role": "assistant" if role in {"machine", "robot", "assistant"} else "user",
                "content": item.get("content", item.get("message", "")),
            })
        payload = {"model": self.model, "messages": normalized, "stream": True}
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # Provider-specific knobs stay inside this adapter, never in TextBot
        # or duplex. Don't send DeepSeek-only fields to other compatible APIs.
        from urllib.parse import urlparse
        if self.thinking is not None and urlparse(self.base_url).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        probe = timing()
        if probe:
            probe.mark("providerRequestMs")
            probe.metrics["promptChars"] = sum(len(item["content"]) for item in normalized)
            probe.metrics["systemPromptChars"] = len(self.system_prompt or "")
            probe.metrics["historyChars"] = sum(len(item["content"]) for item in normalized if item["role"] != "system")
            probe.metrics["historyMessages"] = sum(item["role"] != "system" for item in normalized)
            probe.metrics["connectionReused"] = False
        try:
            async with llm_client() as client:
                endpoint = self.base_url + ("/chat/completions" if self.base_url.endswith("/v1") else "/v1/chat/completions")
                async with client.post(endpoint, json=payload,
                                       headers=headers, trace_request_ctx=probe) as response:
                    if probe:
                        probe.mark("providerHeadersMs")
                    response.raise_for_status()
                    finish_reason = None
                    async for raw_line in response.content:
                        line = raw_line.decode("utf-8").strip()
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            if finish_reason in {"length", "content_filter"}:
                                raise RuntimeError("LLM reply did not finish normally")
                            return
                        event = json.loads(data)
                        choices = event.get("choices") or [{}]
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = (choice.get("delta") or {}).get("content", "")
                        if delta:
                            if probe:
                                probe.mark("providerFirstTokenMs")
                            yield delta
                    if finish_reason != "stop":
                        raise RuntimeError("LLM stream ended before completion")
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"LLM endpoint is unavailable at {self.base_url}; "
                "start Ollama or check LLM_API_KEY / network access"
            ) from exc


class SpeculativeAgent:
    """"Think while listening": pipeline speculative LLM replies for ASR partials.

    While the user is speaking, materially changed ASR partials fire
    background requests that treat the partial as the final user text.
    Deltas stream into a buffer queue immediately, and finished replies are
    kept in a small completed-cache.  Requests are PIPELINED, never
    restarted: a partial that arrives while requests are in flight only
    updates the "latest partial" bookmark, and up to ``MAX_CONCURRENT``
    requests may run side by side so a longer utterance always has a
    fresh speculative covering its tail — cancelling an in-flight request
    would throw away its time-to-first-byte head start.  When the utterance
    finalizes, ``stream_reply`` matches the final text (exact or prefix with
    coverage >= ``PREFIX_COVERAGE`` after normalization): a matching
    in-flight stream is adopted live (buffered deltas replay instantly, the
    rest follows in real time -> zero first-token latency), a matching
    completed reply replays, and otherwise a real request runs as before.
    """

    MAX_PREFETCHES_PER_TURN = 6
    MAX_CONCURRENT = 2
    PREFIX_COVERAGE = 0.6
    _REPLAY_CHUNK = 24

    def __init__(self, inner) -> None:
        self.inner = inner
        self._completed: dict = {}
        self._slots: list = []
        self._latest_key = ""
        self._latest_partial = ""
        self._base_messages: list = []
        self._turn_open = False
        self._fires_this_turn = 0
        self._last_fired_key = ""
        self._last_fired_at = 0.0
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _normalize(text: str) -> str:
        return "".join(ch for ch in str(text).lower() if ch.isalnum())

    @classmethod
    def _coverage(cls, partial_key: str, query_key: str) -> float:
        """Common prefix length over the longer key, 0 when diverged."""
        if not partial_key or not query_key:
            return 0.0
        common = 0
        for a, b in zip(partial_key, query_key):
            if a != b:
                break
            common += 1
        return common / max(len(partial_key), len(query_key))

    @classmethod
    def _match(cls, partial_key: str, query_key: str) -> bool:
        return cls._coverage(partial_key, query_key) >= cls.PREFIX_COVERAGE

    async def stream_reply(self, messages: Iterable[dict],
                           signal: Optional[str] = None) -> AsyncIterator[str]:
        if signal == PICK_UP_SIGNAL:
            async for delta in self.inner.stream_reply(messages, signal=signal):
                yield delta
            return
        self._turn_open = False
        query = str(messages[-1].get("content", "")) if messages else ""
        query_key = self._normalize(query)
        candidate = self._best_candidate(query_key)
        if self._base_messages != list(messages[:-1]):
            candidate = None
        if candidate is not None:
            kind, payload = candidate
            self.hits += 1
            if kind == "inflight":
                slot = payload
                self._slots.remove(slot)
                log_event("llm.cache_hit", query=query, live=not slot["task"].done(), source="inflight")
                async for delta in self._drain(slot["task"], slot["queue"]):
                    yield delta
            else:
                text = payload
                log_event("llm.cache_hit", query=query, live=False, source="completed")
                for offset in range(0, len(text), self._REPLAY_CHUNK):
                    yield text[offset:offset + self._REPLAY_CHUNK]
            await self._cancel_all_slots()  # stale speculatives from this turn
            self._reset_turn_state()
            return
        best = max(
            [self._coverage(slot["key"], query_key) for slot in self._slots]
            + [self._coverage(key, query_key) for key in self._completed],
            default=0.0,
        )
        self.misses += 1
        log_event("llm.cache_miss", query=query, bestCoverage=round(best, 3))
        await self._cancel_all_slots()
        self._reset_turn_state()
        async for delta in self.inner.stream_reply(messages, signal=signal):
            yield delta

    def _best_candidate(self, query_key: str) -> Optional[tuple]:
        best = None  # (coverage, preference, kind, payload)
        for slot in list(self._slots):
            if slot["error"] is not None:
                continue
            task = slot["task"]
            if task.done() and not task.cancelled() and task.exception() is not None:
                continue
            cov = self._coverage(slot["key"], query_key)
            if cov >= self.PREFIX_COVERAGE and (best is None or cov > best[0]):
                best = (cov, 1, "inflight", slot)
        for key, text in self._completed.items():
            cov = self._coverage(key, query_key)
            if cov >= self.PREFIX_COVERAGE and (best is None or cov > best[0]):
                best = (cov, 0, "completed", text)
        return (best[2], best[3]) if best else None

    def notify_partial(self, partial_text: str, base_messages: Iterable[dict]) -> None:
        """Bookmark the latest partial and fire (or pipeline) a speculative request."""
        key = self._normalize(partial_text)
        if not key:
            return
        self._latest_key = key
        self._latest_partial = partial_text
        self._base_messages = [dict(item) for item in base_messages]
        if not self._turn_open:
            self._turn_open = True
            self._fires_this_turn = 0
            self._completed.clear()
            self._last_fired_key = ""
        else:
            now = time.monotonic()
            grew = not self._last_fired_key or len(key) - len(self._last_fired_key) >= 2
            if not (grew or now - self._last_fired_at >= 1.2):
                return
        if any(slot["key"] == key for slot in self._slots):
            return
        if key in self._completed:
            return
        if len([s for s in self._slots if not s["task"].done()]) >= self.MAX_CONCURRENT:
            return
        self._fire(key, partial_text)

    def _fire(self, key: str, partial_text: str) -> None:
        if self._fires_this_turn >= self.MAX_PREFETCHES_PER_TURN:
            return
        self._last_fired_key = key
        self._last_fired_at = time.monotonic()
        self._fires_this_turn += 1
        log_event("llm.prefetch", query=str(partial_text)[:80])
        messages = list(self._base_messages) + [{"role": "user", "content": partial_text}]
        slot = {"key": key, "queue": asyncio.Queue(), "task": None, "error": None}
        slot["task"] = asyncio.create_task(self._prefetch(messages, slot))
        self._slots.append(slot)

    async def _prefetch(self, messages: list, slot: dict) -> None:
        try:
            parts = []
            async for delta in self.inner.stream_reply(messages):
                parts.append(delta)
                await slot["queue"].put(delta)
            self._completed[slot["key"]] = "".join(parts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Recorded, not raised: a stream that died mid-speculation must
            # not be adopted; the real request still runs on the miss path.
            slot["error"] = exc
        finally:
            await slot["queue"].put(None)
            self._pipeline_next()

    def _pipeline_next(self) -> None:
        """Keep the pipeline full: fire for the latest bookmarked partial."""
        try:
            if not (self._turn_open and self._latest_key):
                return
            active_keys = {s["key"] for s in self._slots if not s["task"].done()}
            if self._latest_key in active_keys or self._latest_key in self._completed:
                return
            if len(active_keys) >= self.MAX_CONCURRENT:
                return
            self._fire(self._latest_key, self._latest_partial)
        except RuntimeError:
            pass  # event loop closing during shutdown

    async def _drain(self, task: asyncio.Task, queue: asyncio.Queue) -> AsyncIterator[str]:
        while True:
            delta = await queue.get()
            if delta is None:
                break
            yield delta
        try:
            await task
        except BaseException:
            pass

    async def _cancel_all_slots(self) -> None:
        slots, self._slots = self._slots, []
        for slot in slots:
            task = slot["task"]
            if task is not None and not task.done():
                task.cancel()
        for slot in slots:
            try:
                await slot["task"]
            except BaseException:
                pass

    def _reset_turn_state(self) -> None:
        self._latest_key = ""
        self._latest_partial = ""
        self._base_messages = []
        self._completed.clear()

    async def cancel_pending(self) -> None:
        self._turn_open = False
        await self._cancel_all_slots()
        self._reset_turn_state()

