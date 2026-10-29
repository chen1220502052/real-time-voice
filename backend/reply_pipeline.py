"""Per-session reply pipeline; reply identity and epoch stay on the dialogue.

Text reception and sequential TTS overlap; media still has one epoch authority.
"""

import asyncio
import uuid
import time
from typing import TYPE_CHECKING, Optional

from .atomic import PICK_UP_SIGNAL
from .duplex_events import DuplexEvent
from .history import HistoryItem
from .observability import log_event, trace_context
from .speech_text import take_speech_segments
from .speech_buffer import SpeechBuffer
from .text_bot import stream_bot

if TYPE_CHECKING:
    from .duplex import DuplexDialogue


class _StageFailure(Exception):
    def __init__(self, component, message):
        self.component = component
        super().__init__(message)


class ReplyPipeline:
    def __init__(self, dialogue: "DuplexDialogue") -> None:
        self.dialogue = dialogue
        self._played_ms = 0
        self._played_at = None

    def note_playback(self, reply_id, played_ms):
        if reply_id == self.dialogue._active_reply_id:
            position = min(played_ms, self.dialogue._reply_pcm_bytes / 32)
            if position > self._played_ms:
                self._played_ms, self._played_at = position, time.monotonic()

    def remaining_playback_ms(self):
        """Scheduling estimate ONLY; never used to construct heard history."""
        dialogue = self.dialogue
        if self._played_at is not None:
            consumed = self._played_ms + (time.monotonic() - self._played_at) * 1000
        else:
            sent = dialogue.timing.replies.get(dialogue._active_reply_id, {}).get("firstFrameSendStartedMs")
            consumed = max(0, time.monotonic() * 1000 - sent) if sent is not None else 0
        return max(0, dialogue._reply_pcm_bytes / 32 - consumed)

    async def start(self, turn_id: str, signal: Optional[str] = None) -> None:
        dialogue = self.dialogue
        await dialogue._interrupt(emit=False)
        dialogue._epoch += 1
        epoch = dialogue._epoch
        reply_id = "reply_{}".format(uuid.uuid4().hex[:12])
        dialogue._active_reply_id = reply_id
        dialogue._active_turn_id = turn_id
        dialogue.timing.begin(reply_id, turn_id)
        dialogue._last_enqueued = None
        dialogue._last_sent = None
        dialogue._reply_finished = False
        dialogue._stream_complete_sent = False
        dialogue._llm_finished = False
        dialogue._tts_finished = False
        dialogue._reply_pcm_bytes = 0
        self._played_ms, self._played_at = 0, None
        dialogue._pending_action = None
        dialogue.session.robot.status = 1
        dialogue.session.robot.text = ""
        dialogue.session.robot.start_time = None
        dialogue.session.robot.end_time = None
        dialogue.session.robot.reply_id = reply_id
        dialogue.history.append(HistoryItem("reply_start", "signal", dialogue.session.current_time, dialogue.session.current_time,
                                        "signal", turn_id, reply_id))
        dialogue._reply_task = asyncio.create_task(self.run(reply_id, epoch, signal))
        dialogue._emit_timeline("reply_created", "strategy", turn_id=turn_id, reply_id=reply_id,
                            generation_epoch=epoch)
        dialogue._emit_timeline("llm_request_started", "llm", turn_id=turn_id, reply_id=reply_id,
                            generation_epoch=epoch, attributes={"signal": signal})
        dialogue._emit_timeline("text_bot_request_started", "text_bot", turn_id=turn_id, reply_id=reply_id,
                            generation_epoch=epoch, attributes={"event": "session_start" if signal == PICK_UP_SIGNAL else "user_message"})
        log_event("reply.started", sessionId=dialogue.session.session_id, replyId=reply_id, turnId=turn_id, epoch=epoch)

    async def run(self, reply_id: str, epoch: int, signal: Optional[str] = None) -> None:
        dialogue = self.dialogue
        text = SpeechBuffer()
        tasks = []
        try:
            request = dialogue.bot_request(
                reply_id, dialogue._active_turn_id, "" if signal else dialogue._input_text,
                "session_start" if signal == PICK_UP_SIGNAL else signal or "user_message", reply_id, epoch,
            )
            with trace_context(dialogue.trace_id):
                tasks = [
                    asyncio.create_task(self.read_text(reply_id, epoch, request, text)),
                    asyncio.create_task(self.synthesize(reply_id, epoch, text)),
                ]
                await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            if dialogue._is_current(reply_id, epoch):
                raise
        except Exception as exc:
            component = exc.component if isinstance(exc, _StageFailure) else "llm"
            code = {"llm": "LLM_FAILED", "tts": "TTS_FAILED", "playback": "PLAYBACK_FAILED"}[component]
            # Invalidate first, before awaiting cancellation-resistant providers.
            await self.fail(reply_id, epoch, component, code, str(exc))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def read_text(self, reply_id, epoch, request, text):
        dialogue = self.dialogue
        first_token = True
        events = stream_bot(dialogue.prefetch, request)
        try:
            async for event in events:
                if not dialogue._is_current(reply_id, epoch):
                    return
                if event.type == "action":
                    dialogue._pending_action = (reply_id, epoch, event.action)
                    continue
                if event.type not in {"text_delta", "progress"}:
                    continue
                delta = event.text if event.type == "text_delta" else event.text.rstrip() + " "
                dialogue.session.robot.text += delta
                await dialogue._control_outputs.put(DuplexEvent("assistant_delta", {
                    "turnId": dialogue._active_turn_id, "replyId": reply_id,
                    "generationEpoch": epoch, "text": delta,
                }))
                if not dialogue._is_current(reply_id, epoch):
                    return
                if first_token:
                    dialogue.timing.mark(reply_id, "botFirstTextMs")
                    first_token = False
                    for name, component in (("text_bot_first_text", "text_bot"), ("llm_first_token", "llm")):
                        dialogue._emit_timeline(name, component, turn_id=dialogue._active_turn_id,
                                                reply_id=reply_id, generation_epoch=epoch)
                    outcome = dialogue.prefetch.last_outcome
                    if outcome:
                        dialogue._emit_timeline("text_bot_cache_" + outcome, "text_bot",
                                                turn_id=dialogue._active_turn_id, reply_id=reply_id,
                                                generation_epoch=epoch,
                                                attributes=dialogue.prefetch.last_cache_detail)
                await text.append(delta)
            if dialogue._is_current(reply_id, epoch):
                dialogue._llm_finished = True
                for name, component in (("text_bot_completed", "text_bot"), ("llm_completed", "llm")):
                    dialogue._emit_timeline(name, component, turn_id=dialogue._active_turn_id,
                                            reply_id=reply_id, generation_epoch=epoch,
                                            attributes={"textLength": len(dialogue.session.robot.text)})
                await text.finish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _StageFailure("llm", str(exc)) from exc
        finally:
            await events.aclose()

    async def synthesize(self, reply_id, epoch, text):
        dialogue = self.dialogue
        pcm_buffer = bytearray()
        first_audio = True
        segment_index = 0
        phase = "tts"
        try:
            while dialogue._is_current(reply_id, epoch):
                segment = await text.next(self.remaining_playback_ms)
                if segment is None or not dialogue._is_current(reply_id, epoch):
                    break
                dialogue.timing.mark(reply_id, "firstSegmentReadyMs")
                dialogue.timing.mark(reply_id, "ttsStartedMs")
                dialogue._emit_timeline(
                    "tts_segment_started", "tts", turn_id=dialogue._active_turn_id,
                    reply_id=reply_id, generation_epoch=epoch,
                    attributes={"segmentIndex": segment_index, "textLength": len(segment),
                                "textWords": len(segment.split()),
                                "bufferEstimateMs": round(self.remaining_playback_ms(), 1)},
                )
                pcm_stream = dialogue.tts.stream_pcm(segment, speed=dialogue.session.tts_speed)
                before = dialogue._reply_pcm_bytes
                try:
                    async for pcm in pcm_stream:
                        if not dialogue._is_current(reply_id, epoch):
                            return
                        if first_audio:
                            dialogue.timing.mark(reply_id, "ttsFirstPcmMs")
                            first_audio = False
                            dialogue._emit_timeline("tts_first_audio", "tts", turn_id=dialogue._active_turn_id,
                                                    reply_id=reply_id, generation_epoch=epoch)
                        pcm_buffer.extend(pcm)
                        dialogue._reply_pcm_bytes += len(pcm)
                        phase = "playback"
                        await self.flush_pcm_frames(reply_id, epoch, pcm_buffer)
                        phase = "tts"
                finally:
                    await pcm_stream.aclose()
                if not dialogue._is_current(reply_id, epoch):
                    return
                if dialogue._reply_pcm_bytes == before:
                    raise RuntimeError("TTS returned no audio for a speech segment")
                dialogue.record_interaction("speech_segment", reply_id, text=segment,
                                            audioEndMs=dialogue._reply_pcm_bytes / 32)
                dialogue._emit_timeline("tts_segment_completed", "tts", turn_id=dialogue._active_turn_id,
                                        reply_id=reply_id, generation_epoch=epoch,
                                        attributes={"segmentIndex": segment_index})
                segment_index += 1
            phase = "playback"
            if not dialogue._is_current(reply_id, epoch):
                return
            if pcm_buffer:
                pcm_buffer.extend(b"\x00" * (3200 - len(pcm_buffer)))
                await self.enqueue_playback(reply_id, epoch, bytes(pcm_buffer))
            if dialogue._is_current(reply_id, epoch):
                dialogue._tts_finished = True
                dialogue._emit_timeline("tts_completed", "tts", turn_id=dialogue._active_turn_id,
                                        reply_id=reply_id, generation_epoch=epoch)
                dialogue._reply_finished = True
                await self.maybe_complete(reply_id, dialogue._last_sent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _StageFailure(phase, str(exc)) from exc

    async def fail(
        self, reply_id: str, epoch: int, component: str, code: str, message: str
    ) -> None:
        """Converge a failed LLM/TTS/playback reply without killing the call."""
        dialogue = self.dialogue
        if not dialogue._is_current(reply_id, epoch):
            return
        dialogue._playback_deadline.cancel()
        dialogue._pending_action = None
        turn_id = dialogue._active_turn_id
        partial_text = dialogue.session.robot.text
        dialogue.record_interaction("generation_cancelled", reply_id)
        dialogue._emit_timeline(
            "component_error", component, level="error", turn_id=turn_id,
            reply_id=reply_id, generation_epoch=epoch,
            attributes={"code": code, "message": message},
        )
        dialogue._epoch += 1
        dialogue._drop_queued_media(reply_id)
        dialogue._reply_audio.pop(reply_id, None)
        dialogue._active_reply_id = None
        dialogue._active_turn_id = None
        dialogue._last_enqueued = None
        dialogue._last_sent = None
        dialogue._reply_finished = False
        dialogue._stream_complete_sent = False
        dialogue.session.robot.status = 0
        dialogue.session.robot.end_time = dialogue.session.current_time
        dialogue._emit_timeline(
            "reply_failed", "duplex", level="error", turn_id=turn_id,
            reply_id=reply_id, generation_epoch=epoch,
            attributes={"code": code, "component": component},
        )
        log_event(
            "reply.failed", sessionId=dialogue.session.session_id, turnId=turn_id,
            replyId=reply_id, component=component, code=code,
        )
        await dialogue._control_outputs.put(DuplexEvent("reply_failed", {
            "turnId": turn_id, "replyId": reply_id, "generationEpoch": epoch,
            "code": code, "component": component, "partialText": partial_text,
        }))
        if code == "PLAYBACK_ACK_TIMEOUT":
            await dialogue._control_outputs.put(DuplexEvent("playback_interrupt", {
                "turnId": turn_id, "replyId": reply_id, "generationEpoch": epoch,
                "reason": "reply_failed", "partialText": partial_text,
            }))
        await dialogue._control_outputs.put(DuplexEvent("error", {
            "code": code, "message": message, "component": component,
            "turnId": turn_id, "replyId": reply_id, "generationEpoch": epoch,
        }))

    async def flush_pcm_frames(self, reply_id: str, epoch: int, pcm_buffer: bytearray) -> None:
        dialogue = self.dialogue
        while len(pcm_buffer) >= 3200:
            frame = bytes(pcm_buffer[:3200])
            del pcm_buffer[:3200]
            if dialogue._is_current(reply_id, epoch):
                await self.enqueue_playback(reply_id, epoch, frame)

    @staticmethod
    def take_segments(buffer: str, *, first_chunk: bool = False):
        return take_speech_segments(buffer, first_chunk=first_chunk)

    async def enqueue_playback(self, reply_id: str, epoch: int, pcm: bytes) -> None:
        dialogue = self.dialogue
        dialogue.timing.mark(reply_id, "firstPcmEnqueuedMs")
        sequence = dialogue._down_sequence
        dialogue._down_sequence += 1
        speech_time = (sequence + 1) * 100
        audio = dialogue._reply_audio.setdefault(reply_id, {"first_seq": sequence, "bytes": 0})
        audio["bytes"] += len(pcm)
        avatar = dialogue.cat.drive(pcm, sequence, speech_time)
        dialogue._last_enqueued = sequence
        if dialogue.session.robot.status == 1:
            dialogue.session.robot.status = 2
            dialogue.session.robot.start_time = dialogue.session.current_time
        dialogue.session.robot.speech_time = speech_time
        dialogue._emit_timeline(
            "playback_enqueued", "playback", turn_id=dialogue._active_turn_id,
            reply_id=reply_id, generation_epoch=epoch, sequence=sequence,
            attributes={"highFrequency": True, "queueDepth": dialogue._media_outputs.qsize() + 1},
        )
        await dialogue._media_outputs.put(DuplexEvent("playback_packet", {
            "replyId": reply_id,
            "turnId": dialogue._active_turn_id,
            "generationEpoch": epoch,
            "sequence": sequence,
            "speechTime": speech_time,
            "audio": pcm,
            "avatarState": avatar.avatar_state,
            "assetId": avatar.asset_id,
        }))

    async def maybe_complete(self, reply_id: str, sent_sequence: Optional[int]) -> None:
        dialogue = self.dialogue
        if reply_id != dialogue._active_reply_id or not dialogue._reply_finished or dialogue._stream_complete_sent:
            return
        if dialogue._last_enqueued is not None and sent_sequence != dialogue._last_enqueued:
            return
        dialogue._stream_complete_sent = True
        turn_id = dialogue._active_turn_id
        text = dialogue.session.robot.text
        epoch = dialogue._epoch
        dialogue._playback_deadline.arm(lambda: self.fail(
            reply_id, epoch, "playback", "PLAYBACK_ACK_TIMEOUT", "Playback confirmation timed out"))
        await dialogue._control_outputs.put(DuplexEvent("reply_complete", {
            "turnId": turn_id, "replyId": reply_id, "generationEpoch": epoch, "text": text,
        }))

    async def finalize(self, reply_id: str) -> None:
        dialogue = self.dialogue
        if reply_id != dialogue._active_reply_id:
            return
        dialogue._playback_deadline.cancel()
        turn_id = dialogue._active_turn_id
        text = dialogue.session.robot.text
        epoch = dialogue._epoch
        action = dialogue._pending_action
        dialogue._pending_action = None
        start_time = (
            dialogue.session.robot.start_time
            if dialogue.session.robot.start_time is not None
            else dialogue.session.current_time
        )
        dialogue.history.append(HistoryItem(text, "text", start_time, dialogue.session.current_time,
                                        "robot", turn_id=turn_id, reply_id=reply_id))
        dialogue._reply_audio.pop(reply_id, None)
        dialogue.session.robot.status = 0
        dialogue.session.robot.end_time = dialogue.session.current_time
        dialogue._active_reply_id = None
        dialogue._active_turn_id = None
        dialogue._last_activity = asyncio.get_running_loop().time()
        log_event("reply.completed", sessionId=dialogue.session.session_id, replyId=reply_id)
        dialogue._emit_timeline("reply_completed", "duplex", turn_id=turn_id, reply_id=reply_id,
                            generation_epoch=epoch)
        if action == (reply_id, epoch, "hang_up"):
            await dialogue._hang_up("bot_hang_up")
