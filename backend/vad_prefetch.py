"""Duplex-owned, optional exact-input acceleration during VAD endpointing.

Only an explicitly side-effect-free generation adapter may be called early.
Candidates never enter the Bot ledger or media pipeline before final acceptance.
"""

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import time

from .llm_runtime import Timing, timing, use_timing
from .observability import log_event
from .text_bot import stream_bot
from .providers.common import normalized_pcm16_rms


def prefetch_enabled():
    configured = os.getenv("DUPLEX_PREFETCH_ENABLED", os.getenv("LLM_SPECULATIVE", "0"))
    return configured.lower() not in {"0", "false", "off"}


@dataclass
class Candidate:
    key: str
    started: float
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    first_text_at: float | None = None
    error: Exception | None = None
    metrics: dict = field(default_factory=dict)


class VadPrefetch:
    """One latest candidate per session; exact text AND projected context only."""

    def __init__(self, bot, *, enabled=None, stable_ms=None):
        self.enabled = prefetch_enabled() if enabled is None else enabled
        self.stable_s = max(0, float(os.getenv("DUPLEX_PREFETCH_STABLE_MS", "120"))
                            if stable_ms is None else stable_ms) / 1000
        self.max_per_turn = max(1, int(os.getenv("DUPLEX_PREFETCH_MAX_PER_TURN", "3")))
        self.bot = bot
        self.supported = getattr(bot, "supports_generation_snapshot", False) is True
        self._pending = None
        self._candidate = None
        self._tasks = set()
        self._offered = None
        self._turn = None
        self._fires = 0
        self._sealed = False
        self._final_at = None
        self._latest = None
        self._quiet = False
        self._quiet_since = None
        self.quiet_rms = max(0, float(os.getenv("DUPLEX_PREFETCH_QUIET_RMS", "0.008")))
        self.last_outcome = None
        self.last_cache_detail = {}

    @staticmethod
    def identity(request):
        # Deliberately preserve case, punctuation, numbers, and all content.
        value = request.to_dict()
        for name in ("requestId", "replyId", "generationEpoch"):
            value.pop(name, None)
        value["idleCount"] = request.idle_count
        value["channel"] = request.channel
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _track(self, task):
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def offer(self, query):
        """Nonblocking ASR hook; exact updates inside an established quiet gate."""
        if not self.enabled or not self.supported or query.event != "user_message":
            return False
        turn = (query.session_id, query.turn_id)
        if turn != self._turn:
            self.cancel_pending()
            self._turn, self._fires, self._sealed = turn, 0, False
        if self._sealed:
            return False
        fingerprint = query.fingerprint()
        if fingerprint == self._offered:
            return False
        self._offered = fingerprint
        self._latest = query
        if self._pending:
            self._pending.cancel()
        # Stop outdated generation immediately; never spend the stability
        # window generating an input we already know has changed.
        self._discard_candidate()
        if self._quiet:
            self._pending = self._track(asyncio.create_task(self._prepare(query)))
        return True

    def note_audio(self, pcm):
        """A cheap speculation-only quiet gate; never changes VAD/barge-in."""
        if not self.enabled or not self.supported:
            return
        quiet = normalized_pcm16_rms(pcm) < self.quiet_rms
        if quiet and not self._quiet:
            self._quiet_since = time.monotonic()
        elif not quiet:
            self._quiet_since = None
        if not quiet and self._pending:
            self._pending.cancel()
            self._pending = None
        elif quiet and not self._quiet and self._latest and not self._sealed:
            self._pending = self._track(asyncio.create_task(self._prepare(self._latest)))
        self._quiet = quiet

    async def _prepare(self, query):
        try:
            # Debounce the quiet window once, NOT every late cloud update.
            # Once quiet has lasted long enough, newer text can start at once.
            quiet_for = time.monotonic() - (self._quiet_since or time.monotonic())
            await asyncio.sleep(max(0, self.stable_s - quiet_for))
            if self._sealed or not self._quiet or self._fires >= self.max_per_turn:
                return
            model_query = await self.bot.generation_snapshot(query)
            if self._sealed or model_query is None:
                return
            key = self.identity(model_query)
            if self._candidate and self._candidate.key == key:
                return
            # Bound cancellation-resistant adapters as well as the live slot.
            if sum(not task.done() for task in self._tasks) > 2:
                return
            self._discard_candidate()
            slot = Candidate(key, time.monotonic(), asyncio.Queue(maxsize=4100))
            self._candidate = slot
            self._fires += 1
            slot.task = self._track(asyncio.create_task(self._produce(model_query, slot)))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Speculation failure must not fail ASR or the normal final request.
            log_event("duplex.prefetch_prepare_failed", turnId=query.turn_id)

    async def _produce(self, request, slot):
        # A speculative request has its own lifetime, not the previous reply's
        # provider timing context. Do not contaminate a formal latency probe.
        probe = Timing(request.request_id)
        slot.metrics = probe.metrics
        with use_timing(probe):
            slot.started = time.monotonic()
            events = stream_bot(self.bot.generation_engine, request)
            try:
                async for event in events:
                    if event.type == "text_delta" and slot.first_text_at is None:
                        slot.first_text_at = time.monotonic()
                    slot.queue.put_nowait(event)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                slot.error = error
            finally:
                await events.aclose()
                slot.queue.put_nowait(None)

    def seal(self, turn_id):
        """Capture the VAD-final boundary before storage/projection/TTS work."""
        self._final_at = time.monotonic()
        self._sealed = True
        if self._pending:
            self._pending.cancel()
            self._pending = None
        if not self._turn or self._turn[1] != turn_id:
            self._discard_candidate()

    def _discard_candidate(self):
        slot, self._candidate = self._candidate, None
        if slot and slot.task and not slot.task.done():
            slot.task.cancel()

    def cancel_pending(self):
        self._sealed = True
        self._turn = None
        self._offered = None
        self._latest = None
        if self._pending:
            self._pending.cancel()
            self._pending = None
        self._discard_candidate()

    async def generate(self, inner, request):
        """Called only after the Bot accepts the formal, idempotent request."""
        now = time.monotonic()
        final_at = self._final_at if request.event == "user_message" and self._final_at else now
        slot, self._candidate = self._candidate, None
        hit = bool(self.enabled and request.event == "user_message" and slot
                   and not slot.task.cancelled() and slot.error is None
                   and slot.key == self.identity(request))
        self.cancel_pending()
        if not hit and slot and not slot.task.done():
            slot.task.cancel()
        if request.event != "user_message":
            self.last_outcome = "opening" if request.event == "session_start" else "command"
        elif not self.enabled:
            self.last_outcome = "disabled"
        elif not self.supported:
            self.last_outcome = "unsupported"
        else:
            self.last_outcome = "hit" if hit else "miss"
        self.last_cache_detail = {
            "enabled": self.enabled, "supported": self.supported,
            "prefetches": self._fires, "exactMatch": hit,
            "adoptedReady": bool(hit and slot.first_text_at is not None),
            "effectiveLeadMs": round(max(0, final_at - slot.started) * 1000, 2) if hit else 0,
            "missReason": None if hit else "candidate_failed" if slot and slot.error else
                          "input_or_context_changed" if slot else "no_candidate",
        }
        events = None if hit else stream_bot(inner, request)
        first = True
        try:
            while True:
                if hit:
                    event = await slot.queue.get()
                    if event is None:
                        if slot.error:
                            raise slot.error
                        return
                    event = replace(event, request_id=request.request_id)
                else:
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        return
                if hit:
                    # A short prefix can arrive before the candidate completes
                    # its first sentence. Refresh later milestones as they appear.
                    for source, target in (
                        ("providerRequestMs", "candidateProviderRequestMs"),
                        ("providerHeadersMs", "candidateProviderHeadersMs"),
                        ("providerFirstTokenMs", "candidateProviderFirstTokenMs"),
                        ("firstSentenceReadyMs", "candidateFirstSentenceReadyMs"),
                        ("firstSpeechTextReadyMs", "candidateFirstSpeechTextReadyMs"),
                        ("promptChars", "candidatePromptChars"),
                        ("systemPromptChars", "candidateSystemPromptChars"),
                        ("historyChars", "candidateHistoryChars"),
                    ):
                        if source in slot.metrics:
                            self.last_cache_detail[target] = slot.metrics[source]
                            if timing():
                                timing().metrics[target] = slot.metrics[source]
                if first and event.type == "text_delta":
                    first = False
                    available_at = slot.first_text_at if hit else time.monotonic()
                    self.last_cache_detail.update(
                        remainingWaitMs=round(max(0, available_at - final_at) * 1000, 2),
                        adoptionWaitMs=round((time.monotonic() - now) * 1000, 2),
                    )
                    probe = timing()
                    if probe:
                        probe.metrics.update(self.last_cache_detail, cacheOutcome=self.last_outcome)
                    log_event("duplex.prefetch", requestId=request.request_id,
                              outcome=self.last_outcome, **self.last_cache_detail)
                yield event
        finally:
            if events:
                await events.aclose()
            if hit and not slot.task.done():
                slot.task.cancel()

    async def stream(self, query):
        inject = getattr(self.bot, "stream_with_generator", None)
        if inject is None:
            self.last_outcome = "unsupported" if self.enabled else "disabled"
            self.last_cache_detail = {"enabled": self.enabled, "supported": False,
                                      "effectiveLeadMs": 0, "exactMatch": False, "prefetches": 0}
            events = self.bot.stream(query)
        else:
            events = inject(query, self.generate)
        started, first = time.monotonic(), True
        try:
            async for event in events:
                if inject is None and first and event.type == "text_delta":
                    first = False
                    self.last_cache_detail.update(
                        remainingWaitMs=round((time.monotonic() - (self._final_at or started))*1000, 2),
                        adoptionWaitMs=round((time.monotonic() - started)*1000, 2),
                    )
                    log_event("duplex.prefetch", requestId=query.request_id,
                              outcome=self.last_outcome, **self.last_cache_detail)
                yield event
        finally:
            await events.aclose()
            # Replays do not invoke generate, but must still discard an unused
            # same-turn candidate. Never clear a newer turn's work here.
            if self._turn == (query.session_id, query.turn_id):
                self.cancel_pending()

    async def aclose(self):
        self.cancel_pending()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
