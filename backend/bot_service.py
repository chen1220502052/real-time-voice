"""Stateful Bot service. Playback semantics and request idempotency live here."""

import asyncio
import os
import time
from dataclasses import dataclass, field

from .bot_contract import ConversationRequest
from .bot_store import BotStore
from .bot_stream import RunSubscription
from .bot_projection import delivered_text, project_request
from .concurrency import KeyedLocks
from .llm_runtime import Timing, use_timing
from .bot_contract import identifier
from .text_bot import BotEvent, TextBotError, adapt_text_bot, stream_bot
from .reply_sentences import sentences




@dataclass
class BotRun:
    query: ConversationRequest
    events: list = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task | None = None
    terminal: bool = False
    subscribers: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    timing: Timing | None = None
    append_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    failure: TextBotError | None = None


class BotService:
    protocol_version = 2

    def __init__(self, store=None):
        self.store = store or BotStore()
        self.runs = {}
        self.active = {}
        self.session_locks = KeyedLocks()
        self.storage_failed = False

    def session(self, inner, *, owns_service=False):
        return BotSession(self, adapt_text_bot(inner), owns_service=owns_service)

    async def model_query(self, query, probe=None):
        started = time.perf_counter()
        rows, facts = await self.store.snapshot(query.session_id)
        fetched = time.perf_counter()
        result = project_request(query, rows, facts)
        if probe:
            probe.metrics.update(snapshotMs=round((fetched-started)*1000, 2),
                                 projectionMs=round((time.perf_counter()-fetched)*1000, 2))
        return result

    async def settle_delivery(self, query):
        """A bounded Bot-side grace for the browser's final stop receipt.

        Control/media cancellation is already immediate. Only semantic context
        construction waits; on timeout it uses confirmed complete segments.
        """
        replies = {e["replyId"] for e in query.interactions if e["type"] == "generation_cancelled"}
        if not replies:
            return
        deadline = asyncio.get_running_loop().time() + max(0, min(500, int(os.getenv("BOT_STOP_GRACE_MS", "150")))) / 1000
        while True:
            rows, facts = await self.store.snapshot(query.session_id)
            # Only the most recent reply can be the current interruption.
            replies.intersection_update(p["replyId"] for p, _, _ in rows[-1:])
            settled = {e["replyId"] for e in facts if e["type"] in {"playback_stopped", "playback_completed"}}
            if not replies - settled or asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(.01)

    async def start(self, query, inner, manager=None, generator=None):
        if self.storage_failed:
            raise TextBotError("BOT_STORAGE_FAILED", "Bot storage is unavailable; generation was not retried")
        probe = Timing(query.request_id)
        key = (query.session_id, query.request_id)
        async with self.session_locks.hold(query.session_id):
            if self.storage_failed:
                raise TextBotError("BOT_STORAGE_FAILED", "Bot storage is unavailable")
            probe.mark("lockAcquiredMs")
            old = await self.store.get(*key)
            probe.mark("idempotencyCheckedMs")
            if old:
                if old["fingerprint"] != query.fingerprint():
                    raise TextBotError("BOT_ID_CONFLICT", "Request ID reused with different content")
                if key in self.runs:
                    return self.runs[key]
                return BotRun(query, events=old["events"], terminal=True)
            await self.store.observe(query.session_id, query.interactions, ignore_unknown=True)
            wait_started = time.perf_counter()
            await self.settle_delivery(query)
            probe.metrics['deliveryWaitMs'] = round((time.perf_counter()-wait_started)*1000, 2)
            prior = self.active.get(query.session_id)
            if prior and not prior.terminal:
                cancelled = any(e["replyId"] == (prior.query.reply_id or prior.query.request_id)
                                and e["type"] == "generation_cancelled" for e in query.interactions)
                if not cancelled or query.generation_epoch <= prior.query.generation_epoch:
                    raise TextBotError("BOT_SESSION_BUSY", "Previous generation is still active")
                prior.task.cancel()
                await asyncio.gather(prior.task, return_exceptions=True)
            # Validate options and construct the bounded context in the Bot,
            # not in the media engine. Same projection is used for prepare.
            model_query = await self.model_query(query, probe)
            probe.mark("historyReadyMs")
            owner = "bot_" + query.session_id + "_" + query.request_id
            if manager is not None and not await manager.reserve(owner):
                raise TextBotError("NO_RESOURCE", "服务正在处理其他对话，请稍后重试")
            try:
                await self.store.accept(query)
                if self.storage_failed:
                    raise TextBotError("BOT_STORAGE_FAILED", "Bot storage is unavailable")
            except BaseException:
                if manager is not None:
                    await manager.release(owner)
                raise
            probe.mark("acceptedMs")
            run = BotRun(query, timing=probe)
            self.runs[key] = run
            self.active[query.session_id] = run
            run.task = asyncio.create_task(self._produce(run, inner, model_query, manager, owner, generator))
            try:
                await run.started.wait()
            except asyncio.CancelledError:
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
                raise
            return run

    async def _append(self, run, value):
        async with run.append_lock:
            return await self._append_locked(run, value)

    async def _append_locked(self, run, value):
        if run.terminal:
            raise TextBotError("BOT_STALE_PUSH", "Reply is already terminal")
        if value["type"] == "progress" and any(e["type"] == "action" for e in run.events):
            raise TextBotError("BOT_STALE_PUSH", "Reply action is already committed")
        if value["type"] == "progress" and sum(e["type"] == "progress" for e in run.events) >= 8:
            raise TextBotError("BOT_PUSH_LIMIT", "Too many progress notices")
        if value["type"] in {"progress", "text_delta"} and sum(len(e.get("text", "")) for e in run.events) + len(value.get("text", "")) > 4096:
            raise TextBotError("BOT_OUTPUT_LIMIT", "Reply output exceeds the limit")
        if run.timing and value["type"] == "text_delta":
            run.timing.mark("firstSpeechTextReadyMs")
            generated = "".join(e.get("text", "") for e in run.events if e["type"] == "text_delta") + value["text"]
            if sentences(generated, final=True)[0]:
                run.timing.mark("firstSentenceReadyMs")
        value.update(version=2, eventSeq=len(run.events) + 1)
        events = run.events + [value]
        terminal = value["type"] in {"done", "error"}
        write_started = time.perf_counter()
        try:
            await self.store.save(run.query.session_id, run.query.request_id, events,
                                  value["type"] if terminal else "running")
        except Exception:
            # Do not forge an uncommitted terminal event or retry paid work.
            # Wake all readers with an operational failure; fail closed until
            # restart/journal recovery rather than regenerating an accepted ID.
            self.storage_failed = True
            async with run.condition:
                run.failure = TextBotError("BOT_STORAGE_FAILED", "Bot event could not be persisted")
                run.terminal = True
                run.condition.notify_all()
            raise run.failure from None
        if run.timing:
            metrics = run.timing.metrics
            metrics["eventPersistenceMs"] = round(metrics.get("eventPersistenceMs", 0) +
                                                  (time.perf_counter() - write_started) * 1000, 2)
        if run.timing and value["type"] == "text_delta":
            run.timing.mark("firstSpeechTextPersistedMs")
            if "firstSentenceReadyMs" in run.timing.metrics:
                run.timing.mark("firstSentencePersistedMs")
        async with run.condition:
            run.events.append(value)
            run.terminal = terminal
            run.condition.notify_all()
        return dict(value)

    async def push_progress(self, session_id, request_id, generation_epoch, push_id, text):
        """Trusted Bot worker push into an ACTIVE request, never a media RPC.

        Same ID/content replays its durable event; stale epochs/late pushes
        cannot create new speech. No public student-callable injection route.
        """
        identifier(push_id)
        if type(generation_epoch) is not int or generation_epoch < 0:
            raise ValueError("Invalid generation epoch")
        if not isinstance(text, str) or not text.strip() or not 1 <= len(text) <= 240 or text.rstrip()[-1] not in ".!?。！？":
            raise ValueError("Progress must be a short complete utterance")
        run = self.runs.get((session_id, request_id))
        async def existing(events, epoch):
            if generation_epoch != epoch:
                raise TextBotError("BOT_STALE_PUSH", "Generation epoch changed")
            old = next((e for e in events if e.get("pushId") == push_id), None)
            if old and old.get("text") != text:
                raise TextBotError("BOT_ID_CONFLICT", "Push ID reused with different content")
            return old
        if run is None:
            saved = await self.store.get(session_id, request_id)
            old = await existing(saved["events"], saved["payload"]["generationEpoch"]) if saved else None
            if old:
                return dict(old)
            raise TextBotError("BOT_STALE_PUSH", "No active reply")
        async with run.append_lock:
            old = await existing(run.events, run.query.generation_epoch)
            if old:
                return dict(old)
            return await self._append_locked(run, {"requestId": request_id, "type": "progress",
                                                  "pushId": push_id, "text": text})

    async def _produce(self, run, inner, model_query, manager, owner, generator=None):
        with use_timing(run.timing):
            try:
                await self._produce_timed(run, inner, model_query, manager, owner, generator)
            finally:
                if run.timing:
                    run.timing.metrics["status"] = run.events[-1]["type"] if run.events else "empty"
                    run.timing.metrics.setdefault("cacheOutcome", getattr(inner, "last_outcome", None) or "fresh")
                    detail = getattr(inner, 'last_cache_detail', {})
                    for key in ('rawBestCoverage', 'eligibleBestCoverage', 'rejectedByPolicy', 'policyReasons', 'prefetches', 'graceWaitMs', 'adoptedReady', 'adoptionWaitMs'):
                        if key in detail:
                            run.timing.metrics[key] = detail[key]
                    run.timing.report()

    async def _produce_timed(self, run, inner, model_query, manager, owner, generator=None):
        run.started.set()
        iterator = generator(inner, model_query) if generator else stream_bot(inner, model_query)
        try:
            async for event in iterator:
                await self._append(run, event.to_dict())
        except asyncio.CancelledError:
            if not run.terminal and not run.failure:
                await self._append(run, {"requestId": run.query.request_id, "type": "error",
                                         "code": "BOT_CANCELLED", "message": "Reply generation was cancelled"})
        except Exception as error:
            if run.failure:
                return
            code = error.code if isinstance(error, TextBotError) else "BOT_UNAVAILABLE"
            await self._append(run, {"requestId": run.query.request_id, "type": "error",
                                     "code": code, "message": "Bot could not complete this reply"})
        finally:
            try:
                await iterator.aclose()
            finally:
                if manager is not None:
                    await manager.release(owner)
                # Replay comes from SQLite. Retain only live task buffers.
                self.runs.pop((run.query.session_id, run.query.request_id), None)
                if self.active.get(run.query.session_id) is run:
                    self.active.pop(run.query.session_id, None)

    def events(self, run, after=0):
        return RunSubscription(run, after)

    async def close(self):
        tasks = [r.task for r in self.runs.values() if r.task and not r.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()


class BotSession:
    """Per-connection adapter, sharing Bot-owned durable state service."""

    protocol_version = 2

    def __init__(self, service, inner, *, owns_service=False):
        self.service, self.inner = service, inner
        self.owns_service = owns_service

    @property
    def last_outcome(self):
        return getattr(self.inner, "last_outcome", None)

    @property
    def last_cache_detail(self):
        return getattr(self.inner, "last_cache_detail", {})

    async def stream(self, query):
        events = self.stream_with_generator(query)
        try:
            async for event in events:
                yield event
        finally:
            await events.aclose()

    async def stream_with_generator(self, query, generator=None):
        run = await self.service.start(query, self.inner, generator=generator)
        iterator = self.service.events(run, query.after_event_seq)
        try:
            async for value in iterator:
                yield BotEvent(query.request_id, value["type"], text=value.get("text", ""),
                               finish_reason=value.get("finishReason", "stop"),
                               code=value.get("code", ""), message=value.get("message", ""),
                               version=2, event_seq=value["eventSeq"], action=value.get("action", ""),
                               push_id=value.get("pushId", ""))
        finally:
            await iterator.aclose()

    @property
    def supports_generation_snapshot(self):
        return getattr(self.inner, "side_effect_free", False) is True

    @property
    def generation_engine(self):
        return self.inner

    async def generation_snapshot(self, query):
        """Optional read-only adapter; no speculative request/result persistence."""
        if not self.supports_generation_snapshot or self.service.storage_failed:
            return None
        await self.service.store.observe(query.session_id, query.interactions, ignore_unknown=True)
        return await self.service.model_query(query)

    async def observe(self, session_id, events):
        await self.service.store.observe(session_id, events, ignore_unknown=True)

    async def cancel_pending(self):
        cancel = getattr(self.inner, "cancel_pending", None)
        if cancel:
            await cancel()

    async def aclose(self):
        close = getattr(self.inner, "aclose", None)
        if close:
            await close()
        if self.owns_service:
            await self.service.close()
