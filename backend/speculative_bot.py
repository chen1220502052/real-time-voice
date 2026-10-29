"""Legacy v1 compatibility cache; not assembled by the voice Agent factory.

Current duplex acceleration lives in vad_prefetch.py and uses exact input only.
"""

import asyncio
from dataclasses import replace
import hashlib
import json
import os
import time

from .text_bot import BotRequest, TextBotError, stream_bot


class SpeculativeTextBot:
    protocol_version = 1
    max_concurrent = 2
    max_per_turn = 6
    # Start the pipeline on the first few characters: an early draft warms
    # the provider time-to-first-byte and its completion re-fires for the
    # latest partial sooner. Adoption still requires prefix coverage, so a
    # draft fired on a word fragment can never be answered to a full question.
    min_chars = 4
    min_interval = .5
    # A finalized utterance normally trails the last draft by the endpoint
    # window (a few words), so adoption matches on prefix coverage instead of
    # exact identity; a late negation or changed word still misses.
    prefix_coverage = .6

    def __init__(self, inner):
        self.inner = inner
        # Grace window: a finalized utterance that just misses prefix_coverage
        # often has an in-flight draft trailing it by a few characters. Wait
        # briefly for that draft to land instead of paying full provider
        # latency; on timeout the miss proceeds as before (bounded cost).
        self.grace_coverage = float(os.getenv("SPEC_GRACE_COVERAGE", ".45"))
        self.grace_wait_s = float(os.getenv("SPEC_GRACE_WAIT_MS", "400")) / 1000.
        self._slots = {}
        self._tasks = set()
        self._latest = None
        self._scope = None
        self._active = False
        self._fires = 0
        self._last_fire = 0.
        self.hits = self.misses = self.prefetches = 0
        self.last_outcome = None
        self.last_cache_detail = {}
        self._timer = None
        self._match_detail = {}

    @staticmethod
    def _coverage(draft: str, query: str) -> float:
        common = 0
        for a, b in zip(draft, query):
            if a != b:
                break
            common += 1
        return common / max(len(draft), len(query)) if draft and query else 0.

    def _match_slot(self, key, threshold=None, live=True):
        """Best-coverage draft in the same context.

        Default bar is prefix_coverage and live (in-flight) drafts count;
        the grace window reuses this with a lower threshold and only finished
        drafts, so a slow provider falls back to a fresh request on timeout.
        """
        threshold = self.prefix_coverage if threshold is None else threshold
        scope, query = key
        best_key, best_slot, best_cov = None, None, 0.
        best_score, eligible_cov = (-1, False, 0.), 0.
        raw_cov, rejected, reasons = 0., 0, {}
        for candidate_key, slot in self._slots.items():
            if candidate_key[0] != scope or slot["error"] is not None:
                continue
            if slot['task'].cancelled():
                continue
            cov = self._coverage(candidate_key[1], query)
            raw_cov = max(raw_cov, cov)
            content_guard = getattr(self.inner, "can_adopt", None)
            if content_guard is not None and not content_guard(candidate_key[1], query):
                rejected += 1
                reason = getattr(self.inner, 'adoption_reason', lambda a, b: 'content_policy')(candidate_key[1], query)
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            # A slot cancelled mid-grace (e.g. a new turn arrived) holds a
            # truncated queue; never adopt it, even though the task is "done".
            if slot["task"].cancelled():
                continue
            if not live and not slot["task"].done():
                continue
            eligible_cov = max(eligible_cov, cov)
            # Prefer an already usable SAFE answer over a newly fired exact
            # draft. Below the threshold retain coverage ranking for grace.
            score = (cov >= threshold, bool(slot.get('ready')) if cov >= threshold else False, cov)
            if cov > 0 and score > best_score:
                best_key, best_slot, best_cov = candidate_key, slot, cov
                best_score = score
        self._match_detail = {'rawBestCoverage': round(raw_cov, 3),
                              'eligibleBestCoverage': round(eligible_cov, 3),
                              'rejectedByPolicy': rejected, 'policyReasons': reasons}
        if best_slot is None or best_cov < threshold:
            return None, None, best_cov
        return best_slot, best_key, best_cov

    async def _grace_wait(self, key):
        """Near-miss fallback: caller already saw best_cov in [grace_coverage,
        prefix_coverage). Give in-flight drafts a short chance to land and then
        adopt the best *finished* one at the lower bar; on timeout the miss
        proceeds to a fresh request (cost is capped at the wait).

        Returns (slot, matched_key, cov, waited_seconds).
        """
        scope, _ = key
        inflight = [slot["task"] for candidate_key, slot in self._slots.items()
                    if candidate_key[0] == scope and slot["error"] is None
                    and not slot["task"].done()]
        if not inflight:
            slot, matched_key, cov = self._match_slot(key, threshold=self.grace_coverage, live=False)
            return slot, matched_key, cov, 0.
        started = time.monotonic()
        deadline = started + self.grace_wait_s
        pending = set(inflight)
        # asyncio.wait leaves producers running on timeout; cancel_pending
        # owns teardown either way. Check after each landing so an early
        # finish adopts immediately instead of waiting out the full window.
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(pending, timeout=remaining,
                                               return_when=asyncio.FIRST_COMPLETED)
            slot, matched_key, cov = self._match_slot(key, threshold=self.grace_coverage, live=False)
            if matched_key is not None:
                return slot, matched_key, cov, time.monotonic() - started
            if not done:  # timed out
                break
        slot, matched_key, cov = self._match_slot(key, threshold=self.grace_coverage, live=False)
        return slot, matched_key, cov, time.monotonic() - started

    @staticmethod
    def _identity(request):
        # Case, punctuation and ASR whitespace are presentation differences;
        # changed/added words (including a late negation) require a new reply.
        user = " ".join("".join(c.lower() if c.isalnum() else " "
                               for c in request.messages[-1].content).split())
        context = {"version": request.version, "session": request.session_id,
                   "turn": request.turn_id, "options": dict(request.options),
                   "history": [m.to_dict() for m in request.messages[:-1]]}
        context["deliveryContext"] = [dict(item) for item in request.delivery_context]
        scope = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return scope, user

    async def prepare(self, request: BotRequest) -> None:
        if request.event != "user_message":
            return
        scope, user = self._identity(request)
        if self._scope != scope:
            await self.cancel_pending()
            self._scope = scope
            self._fires = 0
        self._active = True
        self._latest = request
        if len(user) >= self.min_chars:
            self._fire_latest()

    def _fire_latest(self):
        if not self._active or self._latest is None or self._fires >= self.max_per_turn:
            return
        key = self._identity(self._latest)
        if len(key[1]) < self.min_chars or key in self._slots:
            return
        can_prepare = getattr(self.inner, 'can_prepare', None)
        if can_prepare is not None and not can_prepare(key[1]):
            return
        delay = self.min_interval - (time.monotonic() - self._last_fire)
        if delay > 0:
            if self._timer is None:
                self._timer = asyncio.get_running_loop().call_later(delay, self._scheduled_fire)
            return
        if sum(not slot["task"].done() for slot in self._slots.values()) >= self.max_concurrent:
            return
        # The common runner caps text at 4096 characters. Nonempty deltas
        # therefore require at most 4096 entries + terminal + sentinel.
        slot = {"queue": asyncio.Queue(maxsize=4098), "error": None, "task": None, 'ready': False}
        self._slots[key] = slot
        self._fires += 1
        self.prefetches += 1
        self._last_fire = time.monotonic()
        task = asyncio.create_task(self._produce(self._latest, slot))
        slot["task"] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _scheduled_fire(self):
        self._timer = None
        self._fire_latest()

    async def _produce(self, request, slot):
        events = stream_bot(self.inner, request)
        try:
            async for event in events:
                if event.type == 'text_delta':
                    slot['ready'] = True
                slot["queue"].put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            slot["error"] = error
        finally:
            await events.aclose()
            slot["queue"].put_nowait(None)
            # Run after this task is marked done, so the active limit is
            # accurate. The latest partial is bookmarked even when busy.
            asyncio.get_running_loop().call_soon(self._fire_latest)

    async def stream(self, request: BotRequest):
        self._active = False
        self._latest = None
        key = self._identity(request) if request.event == "user_message" else None
        slot, matched_key, best_cov = self._match_slot(key) if key else (None, None, 0.)
        grace_waited = 0.
        grace_attempted = False
        if (matched_key is None and key is not None
                and best_cov >= self.grace_coverage
                and 0. < self.grace_wait_s
                and self.grace_coverage < self.prefix_coverage):
            grace_attempted = True
            slot, matched_key, best_cov, grace_waited = await self._grace_wait(key)
        if matched_key is not None:
            self._slots.pop(matched_key)
        self.last_cache_detail = {**(self._match_detail if key else {}), "prepared": len(self._slots) + (1 if matched_key else 0),
                                  'adoptedReady': bool(slot and slot.get('ready')),
                                  "prefetches": self._fires,
                                  "sameContext": sum(k[0] == key[0] for k in self._slots) if key else 0,
                                  "finalChars": len(key[1]) if key else 0,
                                  "draftChars": [len(k[1]) for k in self._slots],
                                  "matchedCoverage": round(best_cov, 3) if key else None,
                                  "graceAttempted": grace_attempted,
                                  "graceAdopted": grace_attempted and matched_key is not None,
                                  "graceWaitMs": round(grace_waited * 1000) if grace_waited else 0}
        await self.cancel_pending()
        if slot is not None and slot["error"] is None:
            self.hits += 1
            self.last_outcome = "hit"
            adoption_started, first_text = time.monotonic(), True
            try:
                while True:
                    event = await slot["queue"].get()
                    if event is None:
                        if slot["error"]:
                            raise TextBotError("BOT_UNAVAILABLE", "Prepared reply failed")
                        return
                    if first_text and event.type == 'text_delta':
                        self.last_cache_detail['adoptionWaitMs'] = round((time.monotonic()-adoption_started)*1000, 2)
                        first_text = False
                    # IDs belong to the confirmed turn, not the draft request.
                    yield replace(event, request_id=request.request_id)
            finally:
                if not slot["task"].done():
                    slot["task"].cancel()
                await asyncio.gather(slot["task"], return_exceptions=True)
        else:
            if request.event == "user_message":
                self.misses += 1
                self.last_outcome = "miss"
            events = stream_bot(self.inner, request)
            try:
                async for event in events:
                    yield event
            finally:
                await events.aclose()

    async def cancel_pending(self):
        self._active = False
        self._latest = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        slots, self._slots = self._slots, {}
        tasks = [slot["task"] for slot in slots.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self):
        await self.cancel_pending()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
