import asyncio
import os
import uuid
from collections import deque
from typing import Any, AsyncIterator, Dict, Optional

from .atomic import PICK_UP_SIGNAL, CatAnimationDriver, create_agent, create_asr_engine, create_tts
from .domain import Session, SessionState
from .events import make_session_event
from .history import ConversationHistory, HistoryItem
from .observability import log_event, span, trace_context
from .protocol import AudioFrame
from .bot_contract import ConversationRequest, interaction, identifier
from .bot_service import BotService
from .reply_lifecycle import PlaybackDeadline
from .duplex_events import DuplexEvent
from .reply_pipeline import ReplyPipeline
from .interruption import InterruptionController
from .latency_trace import ReplyTiming, milliseconds
from .vad_prefetch import VadPrefetch


class DuplexDialogue:
    """Single-session event loop with reply epoch fencing and media events."""

    def __init__(
        self,
        session: Session,
        asr=None,
        agent=None,
        tts=None,
        cat=None,
        trace_id: str = "-",
        input_queue_size: int = 10,
        control_queue_size: int = 32,
        media_queue_size: int = 40,
        timeline_queue_size: int = 256,
    ) -> None:
        self.session = session
        self.history = ConversationHistory()
        self.asr = asr if asr is not None else create_asr_engine()
        self.agent = agent if agent is not None else create_agent()
        self.text_bot = self.agent if getattr(self.agent, "protocol_version", None) == 2 else BotService().session(self.agent, owns_service=True)
        self.prefetch = VadPrefetch(self.text_bot)
        # Raw interaction facts only. Semantic history belongs to BotService.
        self._bot_facts = deque(maxlen=256)
        self._fact_sequence = 0
        self._fact_tasks = set()
        self._input_text = ""
        self._reply_pcm_bytes = 0
        self.tts = tts if tts is not None else create_tts()
        self.cat = cat if cat is not None else CatAnimationDriver()
        self._inputs: "asyncio.Queue" = asyncio.Queue(maxsize=input_queue_size)
        # Control and media must never share the same backpressure path. A
        # full speech queue must not delay barge-in or hang-up events.
        self._control_outputs: "asyncio.Queue" = asyncio.Queue(maxsize=control_queue_size)
        self._media_outputs: "asyncio.Queue" = asyncio.Queue(maxsize=media_queue_size)
        self._timeline_outputs: "asyncio.Queue" = asyncio.Queue(maxsize=timeline_queue_size)
        self._task: Optional[asyncio.Task] = None
        self._reply_task: Optional[asyncio.Task] = None
        self._epoch = 0
        self._active_reply_id: Optional[str] = None
        self._active_turn_id: Optional[str] = None
        self._active_user_turn_id: Optional[str] = None
        self._last_prepared_partial = None
        self._down_sequence = 0
        self._last_enqueued: Optional[int] = None
        self._last_sent: Optional[int] = None
        # Media accounting only; never evidence that the client played audio.
        self._reply_audio: Dict[str, Dict[str, int]] = {}
        self._reply_finished = False
        self._stream_complete_sent = False
        self._llm_finished = False
        self._tts_finished = False
        self.reply_pipeline = ReplyPipeline(self)
        self.interruption = InterruptionController(self)
        self.timing = ReplyTiming()
        self.trace_id = trace_id
        self._pending_action = None
        self._idle_task = None
        self._idle_seconds = max(0, float(os.getenv("BOT_IDLE_SECONDS", "30")))
        self._last_activity = 0
        self._command_ids = {}
        self._playback_deadline = PlaybackDeadline(max(.01, float(os.getenv("PLAYBACK_ACK_TIMEOUT_SECONDS", "15"))))

    async def start(self) -> None:
        if self._task is None:
            self.session.state = SessionState.ON_SERVICE
            self._task = asyncio.create_task(self._run())
            self._last_activity = asyncio.get_running_loop().time()
            if self._idle_seconds:
                self._idle_task = asyncio.create_task(self._watch_idle())
            self._emit_timeline("session_active", "session")

    async def push_audio(self, frame: AudioFrame) -> bool:
        try:
            self._inputs.put_nowait(("audio", frame))
            return True
        except asyncio.QueueFull:
            # Real-time priority: retain the newest microphone frame and drop
            # one stale frame so latency cannot grow without bound.
            try:
                self._inputs.get_nowait()
                self._inputs.put_nowait(("audio", frame))
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self._emit_timeline(
                "queue_overflow", "duplex", level="error", sequence=frame.sequence,
                attributes={"queue": "input", "capacity": self._inputs.maxsize,
                            "strategy": "drop_oldest"},
            )
            return False

    async def submit_text_for_development(self, text: str) -> None:
        """Temporary hook until a real Paraformer adapter supplies final text."""
        await self._inputs.put(("transcript", text))

    async def pick_up(self) -> None:
        """Start the deterministic opening reply after create_session succeeds."""
        await self._inputs.put((PICK_UP_SIGNAL, None))

    async def submit_command(self, event: str, request_id=None) -> None:
        if event not in {"connect", "time_out", "hang_up"}:
            raise ValueError("Unsupported bot command")
        if self.session.state != SessionState.ON_SERVICE:
            return
        if request_id is not None:
            identifier(request_id)
            if request_id in self._command_ids:
                if self._command_ids[request_id] != event:
                    raise ValueError("Command ID reused with different content")
                return
            if len(self._command_ids) >= 512:
                raise ValueError("Too many commands in one session")
        if event == "time_out" and (self._active_reply_id or self.session.user.status in {1, 2}):
            return
        self._last_activity = asyncio.get_running_loop().time()
        if request_id is not None:
            self._command_ids[request_id] = event
        await self._start_reply("turn_" + uuid.uuid4().hex[:12], signal=event)

    async def _watch_idle(self):
        try:
            while self.session.state == SessionState.ON_SERVICE:
                await asyncio.sleep(min(1, self._idle_seconds))
                if asyncio.get_running_loop().time() - self._last_activity >= self._idle_seconds:
                    await self.submit_command("time_out")
        except asyncio.CancelledError:
            pass

    async def mark_packet_sent(self, reply_id: str, sequence: int) -> None:
        if reply_id == self._active_reply_id:
            self._last_sent = sequence
            await self._maybe_complete(reply_id, sequence)

    async def mark_playback_completed(self, reply_id: str, *, event_id=None, source_sequence=None) -> None:
        self.record_interaction("playback_completed", reply_id, event_id=event_id, source_sequence=source_sequence)
        if reply_id == self._active_reply_id and self._stream_complete_sent:
            await self._finalize_reply(reply_id)

    def record_interaction(self, kind, reply_id, *, event_id=None, source_sequence=None, **values):
        """Forward facts without interpreting content or blocking barge-in."""
        self._fact_sequence += 1
        fact = dict(interaction({"eventId": event_id or "fact_" + uuid.uuid4().hex,
                                 "type": kind, "replyId": reply_id,
                                 "sequence": self._fact_sequence if source_sequence is None else source_sequence, **values}))
        self._bot_facts.append(fact)
        if kind == "playback_progress":
            self.reply_pipeline.note_playback(reply_id, fact["playedMs"])
        if kind == "playback_stopped" and self._pending_action and self._pending_action[0] == reply_id:
            self._pending_action = None
        task = asyncio.create_task(self.text_bot.observe(self.session.session_id, (fact,)))
        self._fact_tasks.add(task)
        def finished(done):
            self._fact_tasks.discard(done)
            if not done.cancelled() and done.exception():
                self._emit_timeline("bot_fact_save_failed", "text_bot", level="error")
        task.add_done_callback(finished)

    def bot_request(self, request_id, turn_id, text="", event="user_message", reply_id="", epoch=None):
        return ConversationRequest(
            request_id=request_id, session_id=self.session.session_id, turn_id=turn_id,
            text=text, event=event, channel="voice", options={"grade": self.session.grade,
                **({"chapterId": self.session.chapter_id} if self.session.chapter_id else {})},
            reply_id=reply_id, generation_epoch=self._epoch if epoch is None else epoch,
            interactions=tuple(self._bot_facts),
        )

    async def close(self, reason: str) -> None:
        # Closing is a control-plane operation: it must never wait behind a
        # backlog of 100 ms microphone frames in the dialogue queue.
        if self.session.state == SessionState.HANG_UP:
            return
        if self._task and self._task is not asyncio.current_task() and not self._task.done():
            self._task.cancel()
        await self._hang_up(reason)

    async def events(self) -> AsyncIterator[DuplexEvent]:
        """Yield high-priority control/state events only."""
        while True:
            event = await self._control_outputs.get()
            yield event
            if event.kind == "closed":
                return

    async def media_events(self) -> AsyncIterator[DuplexEvent]:
        """Yield bounded playback media independently from control events."""
        while True:
            event = await self._media_outputs.get()
            if event is None:
                return
            yield event

    async def timeline_events(self) -> AsyncIterator[dict]:
        while True:
            event = await self._timeline_outputs.get()
            if event is None:
                return
            yield event

    def runtime_stats(self) -> Dict[str, int]:
        return {
            "inputQueue": self._inputs.qsize(),
            "controlQueue": self._control_outputs.qsize(),
            "mediaOutputQueue": self._media_outputs.qsize(),
            "timelineQueue": self._timeline_outputs.qsize(),
        }

    async def _run(self) -> None:
        with trace_context(self.trace_id):
            while True:
                kind, payload = await self._inputs.get()
                if kind == "audio":
                    await self._decode_audio(payload)
                elif kind == "transcript":
                    await self._accept_final_text(payload)
                elif kind == PICK_UP_SIGNAL:
                    await self._accept_pick_up()
                elif kind == "close":
                    await self._hang_up(payload)
                    return

    async def _decode_audio(self, frame: AudioFrame) -> None:
        if self.session.state != SessionState.ON_SERVICE:
            return
        processing_started = milliseconds()
        try:
            with span("duplex.asr_vad", sessionId=self.session.session_id, sequence=frame.sequence):
                result = await self._feed_with_partials(frame)
        except Exception as exc:
            log_event("asr.failed", sessionId=self.session.session_id, error=str(exc))
            await self._control_outputs.put(DuplexEvent("error", {"code": "ASR_FAILED", "message": str(exc),
                "reasonCode": getattr(exc, "code", None), "component": "asr"}))
            if getattr(exc, "fatal", False):
                await self.close("asr_unrecoverable")
            return
        self.session.current_time = result.speech_time
        if result.status in {1, 2, 3}:
            self._last_activity = asyncio.get_running_loop().time()
        self.session.user.status = result.status
        self.session.user.speech_time = result.speech_time
        self.session.user.text = result.text
        self.session.user.start_time = result.start_time
        self.session.user.end_time = result.end_time
        inferred_turn_start = False
        if result.status in (1, 2, 3) and self._active_user_turn_id is None:
            self._active_user_turn_id = "turn_{}".format(uuid.uuid4().hex[:12])
            inferred_turn_start = result.status != 1
            if inferred_turn_start:
                self._emit_timeline(
                    "speech_started", "vad", turn_id=self._active_user_turn_id,
                    attributes={"startTime": result.start_time, "speechTime": result.speech_time,
                                "inferred": True},
                )
        if result.status == 1:
            self._emit_timeline(
                "speech_started", "vad", turn_id=self._active_user_turn_id,
                attributes={"startTime": result.start_time, "speechTime": result.speech_time},
            )
        elif result.status == 2 and result.text:
            self._emit_timeline(
                "asr_partial", "asr", turn_id=self._active_user_turn_id,
                attributes={"text": result.text, "speechTime": result.speech_time},
            )
            await self._prepare_partial(result.text)
        elif result.status == 3:
            self.timing.final(frame, result, self._active_user_turn_id, processing_started)
            self._emit_timeline(
                "speech_ended", "vad", turn_id=self._active_user_turn_id,
                attributes={"startTime": result.start_time, "endTime": result.end_time},
            )
            self._emit_timeline(
                "asr_final" if result.text else "asr_error", "asr",
                level="info" if result.text else "warning",
                turn_id=self._active_user_turn_id,
                attributes={"text": result.text, "endTime": result.end_time,
                            "message": "未识别到有效文本" if not result.text else ""},
            )
        await self._control_outputs.put(DuplexEvent("asr_result", {
            **result.__dict__, "turnId": self._active_user_turn_id,
        }))
        accept_final = await self._maybe_barge_in(result)
        if result.status == 3 and result.text and accept_final is not False:
            await self._accept_final_text(result.text, self._active_user_turn_id)
        if result.status == 3:
            self._active_user_turn_id = None

    async def _prepare_partial(self, text):
        if not text or not self._active_user_turn_id or self.session.state != SessionState.ON_SERVICE:
            return
        identity = (self._active_user_turn_id, self._epoch, self._fact_sequence, text)
        if self._last_prepared_partial == identity:
            return
        try:
            # Stable identity excludes the random draft request ID. The module
            # owns debounce/cancellation; ASR must never await a Bot snapshot.
            offered = self.prefetch.offer(self.bot_request('draft_' + self._active_user_turn_id,
                                                          self._active_user_turn_id, text))
            self._last_prepared_partial = identity
            if offered:
                self._emit_timeline('text_bot_prepare_requested', 'duplex', turn_id=identity[0])
        except Exception:
            self._emit_timeline('text_bot_prepare_failed', 'text_bot', level='warning', turn_id=identity[0])

    async def _feed_with_partials(self, frame):
        self.prefetch.note_audio(frame.payload)
        if self.interruption.local is not None:
            self.interruption.local.echo.input(frame.payload)
        set_robot_active = getattr(self.asr, 'set_robot_active', None)
        if set_robot_active:
            set_robot_active(self._active_reply_id is not None)
        peek = getattr(self.asr, 'peek_partial', None)
        if not peek or not self._active_user_turn_id:
            return await self.asr.feed(frame.payload, frame.speech_time)
        # SDK stop/flush may await late cloud text for hundreds of ms. Keep
        # preparing updated text during that wait; never publish it or alter
        # VAD/barge-in decisions. One task owns both paths, so no prepare can
        # race past the final request or a cancelled dialogue.
        feed = asyncio.create_task(self.asr.feed(frame.payload, frame.speech_time))
        try:
            while not feed.done():
                done, _ = await asyncio.wait({feed}, timeout=.05)
                if not done:
                    await self._prepare_partial(peek())
            return await feed
        finally:
            if not feed.done():
                feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)

    async def _accept_final_text(self, text: str, turn_id: Optional[str] = None) -> None:
        if self.session.state != SessionState.ON_SERVICE or not text.strip():
            return
        is_development_input = turn_id is None
        turn_id = turn_id or "turn_{}".format(uuid.uuid4().hex[:12])
        self.prefetch.seal(turn_id)
        if is_development_input:
            self._emit_timeline("asr_final", "asr", turn_id=turn_id, attributes={"text": text})
        # Zero is a valid timestamp for speech that starts in the first packet.
        # Do not use truthiness here: ``0 or current_time`` silently rewrites
        # the beginning of such a turn.
        start_time = (
            self.session.user.start_time
            if self.session.user.start_time is not None
            else self.session.current_time
        )
        end_time = (
            self.session.user.end_time
            if self.session.user.end_time is not None
            else self.session.current_time
        )
        self.history.append(HistoryItem(text, "text", start_time, end_time, "user", turn_id))
        self._input_text = text
        self._last_activity = asyncio.get_running_loop().time()
        await self._start_reply(turn_id)

    async def _accept_pick_up(self) -> None:
        if self.session.state != SessionState.ON_SERVICE:
            return
        turn_id = "turn_{}".format(uuid.uuid4().hex[:12])
        self.history.append(HistoryItem(PICK_UP_SIGNAL, "signal", self.session.current_time,
                                        self.session.current_time, "signal", turn_id))
        await self._start_reply(turn_id, signal=PICK_UP_SIGNAL)

    async def _start_reply(self, turn_id: str, signal: Optional[str] = None) -> None:
        return await self.reply_pipeline.start(turn_id, signal)

    async def _reply_worker(self, reply_id: str, epoch: int, signal: Optional[str] = None) -> None:
        return await self.reply_pipeline.run(reply_id, epoch, signal)

    async def _fail_reply(
        self, reply_id: str, epoch: int, component: str, code: str, message: str
    ) -> None:
        return await self.reply_pipeline.fail(reply_id, epoch, component, code, message)

    async def _flush_pcm_frames(self, reply_id: str, epoch: int, pcm_buffer: bytearray) -> None:
        return await self.reply_pipeline.flush_pcm_frames(reply_id, epoch, pcm_buffer)

    @staticmethod
    def _take_segments(buffer: str, *, first_chunk: bool = False):
        return ReplyPipeline.take_segments(buffer, first_chunk=first_chunk)

    async def _enqueue_playback(self, reply_id: str, epoch: int, pcm: bytes) -> None:
        return await self.reply_pipeline.enqueue_playback(reply_id, epoch, pcm)

    async def _maybe_complete(self, reply_id: str, sent_sequence: Optional[int]) -> None:
        return await self.reply_pipeline.maybe_complete(reply_id, sent_sequence)

    async def _finalize_reply(self, reply_id: str) -> None:
        return await self.reply_pipeline.finalize(reply_id)

    async def _maybe_barge_in(self, result) -> None:
        return await self.interruption.maybe_barge_in(result)

    async def _confirm_barge_in(self, result) -> None:
        return await self.interruption.confirm(result)

    async def _cancel_pending_barge_in(self, reason: str) -> None:
        return await self.interruption.cancel_pending(reason)

    async def _interrupt(self, emit: bool = True) -> None:
        return await self.interruption.interrupt(emit)

    async def _hang_up(self, reason: str) -> None:
        if self.session.state == SessionState.HANG_UP:
            return
        self.session.state = SessionState.PRE_HANG_UP
        if self._task and self._task is not asyncio.current_task() and not self._task.done():
            self._task.cancel()
        if self._idle_task and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        self._emit_timeline("session_closing", "session", attributes={"reason": reason})
        log_event("session.hanging_up", sessionId=self.session.session_id, reason=reason)
        await self._interrupt(emit=False)
        # Invalidate reply/epoch before awaiting speculative task cleanup.
        await self.prefetch.aclose()
        if self._fact_tasks:
            await asyncio.gather(*self._fact_tasks, return_exceptions=True)
        close_bot = getattr(self.text_bot, "aclose", None)
        if close_bot:
            await close_bot()
        try:
            await self.asr.reset()
        except Exception as exc:
            # Closing the control plane and releasing the reserved session are
            # more important than a cloud recognizer's late stop error. The
            # adapter has already discarded its local utterance state.
            log_event("asr.reset_failed", sessionId=self.session.session_id, error=str(exc))
            self._emit_timeline(
                "asr_reset_failed", "asr", level="warning",
                attributes={"reason": "session_close", "error": str(exc)},
            )
        await self._control_outputs.put(DuplexEvent("close_session", {"reason": reason}))
        self.session.state = SessionState.HANG_UP
        self._emit_timeline("session_closed", "session", attributes={"reason": reason})
        await self._control_outputs.put(DuplexEvent("closed", {"reason": reason}))
        # Closing must not wait for queued media to drain.
        while True:
            try:
                self._media_outputs.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._media_outputs.put_nowait(None)
        try:
            self._timeline_outputs.put_nowait(None)
        except asyncio.QueueFull:
            self._timeline_outputs.get_nowait()
            self._timeline_outputs.put_nowait(None)

    def _drop_queued_media(self, reply_id: str) -> None:
        return self.interruption.drop_queued_media(reply_id)

    def _is_current(self, reply_id: str, epoch: int) -> bool:
        return reply_id == self._active_reply_id and epoch == self._epoch

    def _emit_timeline(
        self,
        event_type: str,
        component: str,
        *,
        level: str = "info",
        turn_id: Optional[str] = None,
        reply_id: Optional[str] = None,
        generation_epoch: Optional[int] = None,
        sequence: Optional[int] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = make_session_event(
            event_type,
            component,
            level=level,
            session_id=self.session.session_id,
            turn_id=turn_id,
            reply_id=reply_id,
            generation_epoch=generation_epoch,
            sequence=sequence,
            attributes=attributes,
        )
        try:
            self._timeline_outputs.put_nowait(event)
        except asyncio.QueueFull:
            # Timeline data is observability, never control-plane backpressure.
            try:
                self._timeline_outputs.get_nowait()
                self._timeline_outputs.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
