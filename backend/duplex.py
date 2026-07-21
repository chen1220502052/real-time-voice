import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional

from .atomic import PICK_UP_SIGNAL, CatAnimationDriver, create_agent, create_asr_engine, create_tts
from .domain import Session, SessionState
from .history import ConversationHistory, HistoryItem
from .observability import log_event, span, trace_context
from .protocol import AudioFrame


@dataclass
class DuplexEvent:
    kind: str
    payload: Dict[str, Any]


class DuplexDialogue:
    """Single-session event loop with reply epoch fencing and media events."""

    def __init__(self, session: Session, asr=None, agent=None, tts=None, cat=None, trace_id: str = "-") -> None:
        self.session = session
        self.history = ConversationHistory()
        self.asr = asr or create_asr_engine()
        self.agent = agent or create_agent()
        self.tts = tts or create_tts()
        self.cat = cat or CatAnimationDriver()
        self._inputs: "asyncio.Queue" = asyncio.Queue()
        self._outputs: "asyncio.Queue" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._reply_task: Optional[asyncio.Task] = None
        self._epoch = 0
        self._active_reply_id: Optional[str] = None
        self._down_sequence = 0
        self._last_enqueued: Optional[int] = None
        self._reply_finished = False
        self.trace_id = trace_id

    async def start(self) -> None:
        if self._task is None:
            self.session.state = SessionState.ON_SERVICE
            self._task = asyncio.create_task(self._run())

    async def push_audio(self, frame: AudioFrame) -> None:
        await self._inputs.put(("audio", frame))

    async def submit_text_for_development(self, text: str) -> None:
        """Temporary hook until a real Paraformer adapter supplies final text."""
        await self._inputs.put(("transcript", text))

    async def pick_up(self) -> None:
        """Start the deterministic opening reply after create_session succeeds."""
        await self._inputs.put((PICK_UP_SIGNAL, None))

    async def mark_packet_sent(self, reply_id: str, sequence: int) -> None:
        await self._inputs.put(("packet_sent", (reply_id, sequence)))

    async def close(self, reason: str) -> None:
        await self._inputs.put(("close", reason))

    async def events(self) -> AsyncIterator[DuplexEvent]:
        while True:
            event = await self._outputs.get()
            yield event
            if event.kind == "closed":
                return

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
                elif kind == "delta":
                    reply_id, epoch, text = payload
                    if self._is_current(reply_id, epoch):
                        self.session.robot.text += text
                elif kind == "pcm":
                    reply_id, epoch, pcm = payload
                    if self._is_current(reply_id, epoch):
                        await self._enqueue_playback(reply_id, pcm)
                elif kind == "reply_done":
                    reply_id, epoch = payload
                    if self._is_current(reply_id, epoch):
                        self._reply_finished = True
                        await self._maybe_complete(reply_id, self._last_enqueued)
                elif kind == "packet_sent":
                    reply_id, sequence = payload
                    await self._maybe_complete(reply_id, sequence)
                elif kind == "close":
                    await self._hang_up(payload)
                    return

    async def _decode_audio(self, frame: AudioFrame) -> None:
        try:
            with span("duplex.asr_vad", sessionId=self.session.session_id, sequence=frame.sequence):
                result = await self.asr.feed(frame.payload, frame.speech_time)
        except Exception as exc:
            log_event("asr.failed", sessionId=self.session.session_id, error=str(exc))
            await self._outputs.put(DuplexEvent("error", {"code": "ASR_FAILED", "message": str(exc)}))
            return
        self.session.current_time = result.speech_time
        self.session.user.status = result.status
        self.session.user.speech_time = result.speech_time
        self.session.user.text = result.text
        self.session.user.start_time = result.start_time
        self.session.user.end_time = result.end_time
        await self._outputs.put(DuplexEvent("asr_result", result.__dict__))
        if result.status == 1 and self.session.robot.status in (1, 2):
            await self._interrupt()
        if result.status == 3 and result.text:
            await self._accept_final_text(result.text)

    async def _accept_final_text(self, text: str) -> None:
        if self.session.state != SessionState.ON_SERVICE or not text.strip():
            return
        turn_id = "turn_{}".format(uuid.uuid4().hex[:12])
        self.history.append(HistoryItem(text, "text", self.session.user.start_time or self.session.current_time,
                                        self.session.user.end_time or self.session.current_time, "user", turn_id))
        if text.strip() in ("再见", "拜拜", "再见。", "拜拜。"):
            await self._hang_up("user_goodbye")
            return
        await self._start_reply(turn_id)

    async def _accept_pick_up(self) -> None:
        if self.session.state != SessionState.ON_SERVICE:
            return
        turn_id = "turn_{}".format(uuid.uuid4().hex[:12])
        self.history.append(HistoryItem(PICK_UP_SIGNAL, "signal", self.session.current_time,
                                        self.session.current_time, "signal", turn_id))
        await self._start_reply(turn_id)

    async def _start_reply(self, turn_id: str) -> None:
        await self._interrupt(emit=False)
        self._epoch += 1
        epoch = self._epoch
        reply_id = "reply_{}".format(uuid.uuid4().hex[:12])
        self._active_reply_id = reply_id
        self._last_enqueued = None
        self._reply_finished = False
        self.session.robot.status = 1
        self.session.robot.text = ""
        self.session.robot.start_time = None
        self.session.robot.end_time = None
        self.session.robot.reply_id = reply_id
        self.history.append(HistoryItem("reply_start", "signal", self.session.current_time, self.session.current_time,
                                        "signal", turn_id, reply_id))
        self._reply_task = asyncio.create_task(self._reply_worker(reply_id, epoch))
        log_event("reply.started", sessionId=self.session.session_id, replyId=reply_id, turnId=turn_id, epoch=epoch)

    async def _reply_worker(self, reply_id: str, epoch: int) -> None:
        buffer = ""
        pcm_buffer = bytearray()
        try:
            with trace_context(self.trace_id):
                async for delta in self.agent.stream_reply(self.history.to_agent_messages(include_signals=True)):
                    await self._inputs.put(("delta", (reply_id, epoch, delta)))
                    buffer += delta
                    segments, buffer = self._take_segments(buffer)
                    for segment in segments:
                        async for pcm in self.tts.stream_pcm(segment):
                            pcm_buffer.extend(pcm)
                            await self._flush_pcm_frames(reply_id, epoch, pcm_buffer)
                if buffer:
                    async for pcm in self.tts.stream_pcm(buffer):
                        pcm_buffer.extend(pcm)
                        await self._flush_pcm_frames(reply_id, epoch, pcm_buffer)
                if pcm_buffer:
                    pcm_buffer.extend(b"\x00" * (3200 - len(pcm_buffer)))
                    await self._inputs.put(("pcm", (reply_id, epoch, bytes(pcm_buffer))))
                await self._inputs.put(("reply_done", (reply_id, epoch)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._outputs.put(DuplexEvent("error", {"code": "REPLY_FAILED", "message": str(exc)}))

    async def _flush_pcm_frames(self, reply_id: str, epoch: int, pcm_buffer: bytearray) -> None:
        while len(pcm_buffer) >= 3200:
            frame = bytes(pcm_buffer[:3200])
            del pcm_buffer[:3200]
            await self._inputs.put(("pcm", (reply_id, epoch, frame)))

    @staticmethod
    def _take_segments(buffer: str):
        segments = []
        while len(buffer) >= 5:
            cut = next((index + 1 for index, char in enumerate(buffer[:12]) if char in "，。！？；"), None)
            if cut is None:
                cut = 5 if len(buffer) < 12 else 12
            segments.append(buffer[:cut])
            buffer = buffer[cut:]
        return segments, buffer

    async def _enqueue_playback(self, reply_id: str, pcm: bytes) -> None:
        sequence = self._down_sequence
        self._down_sequence += 1
        speech_time = (sequence + 1) * 100
        avatar = self.cat.drive(pcm, sequence, speech_time)
        self._last_enqueued = sequence
        if self.session.robot.status == 1:
            self.session.robot.status = 2
            self.session.robot.start_time = self.session.current_time
        self.session.robot.speech_time = speech_time
        await self._outputs.put(DuplexEvent("playback_packet", {
            "replyId": reply_id,
            "sequence": sequence,
            "speechTime": speech_time,
            "audio": pcm,
            "avatarState": avatar.avatar_state,
            "assetId": avatar.asset_id,
        }))

    async def _maybe_complete(self, reply_id: str, sent_sequence: Optional[int]) -> None:
        if reply_id != self._active_reply_id or not self._reply_finished:
            return
        if self._last_enqueued is not None and sent_sequence != self._last_enqueued:
            return
        self.history.append(HistoryItem(self.session.robot.text, "text", self.session.robot.start_time or self.session.current_time,
                                        self.session.current_time, "robot", reply_id=reply_id))
        self.session.robot.status = 0
        self.session.robot.end_time = self.session.current_time
        self._active_reply_id = None
        log_event("reply.completed", sessionId=self.session.session_id, replyId=reply_id)
        await self._outputs.put(DuplexEvent("reply_complete", {"replyId": reply_id}))

    async def _interrupt(self, emit: bool = True) -> None:
        if self._active_reply_id is None:
            return
        old_reply_id = self._active_reply_id
        self._epoch += 1
        if self._reply_task and not self._reply_task.done():
            self._reply_task.cancel()
        self._active_reply_id = None
        self.session.robot.status = 0
        self.session.robot.end_time = self.session.current_time
        self.history.append(HistoryItem("barge_in", "signal", self.session.current_time, self.session.current_time, "signal"))
        if emit:
            log_event("reply.interrupted", sessionId=self.session.session_id, replyId=old_reply_id)
            await self._outputs.put(DuplexEvent("playback_interrupt", {"replyId": old_reply_id, "reason": "barge_in"}))

    async def _hang_up(self, reason: str) -> None:
        if self.session.state == SessionState.HANG_UP:
            return
        self.session.state = SessionState.PRE_HANG_UP
        log_event("session.hanging_up", sessionId=self.session.session_id, reason=reason)
        await self._interrupt(emit=False)
        await self.asr.reset()
        await self._outputs.put(DuplexEvent("close_session", {"reason": reason}))
        self.session.state = SessionState.HANG_UP
        await self._outputs.put(DuplexEvent("closed", {"reason": reason}))

    def _is_current(self, reply_id: str, epoch: int) -> bool:
        return reply_id == self._active_reply_id and epoch == self._epoch
