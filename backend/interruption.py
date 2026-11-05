"""Per-session interruption gate and cancellation executor.

Owns acoustic gate state, not Bot content/history projection. Reply identity and
epoch stay on the dialogue, sharing one authority with media fencing.
"""

import asyncio
import os
from typing import TYPE_CHECKING, Any, Dict, Optional

from .duplex_events import DuplexEvent
from .history import HistoryItem
from .observability import log_event
from .local_turn_taking import LocalTurnTaking

if TYPE_CHECKING:
    from .duplex import DuplexDialogue


class InterruptionController:
    def __init__(self, dialogue: "DuplexDialogue") -> None:
        self.dialogue = dialogue
        self._barge_in_confirm_ms = int(os.getenv("BARGE_IN_CONFIRM_MS", "500"))
        self._barge_in_text_grace_ms = int(os.getenv("BARGE_IN_TEXT_GRACE_MS", "500"))
        self._barge_in_min_reply_age_ms = int(os.getenv("BARGE_IN_MIN_REPLY_AGE_MS", "400"))
        self._barge_in_cooldown_ms = int(os.getenv("BARGE_IN_COOLDOWN_MS", "1500"))
        self._pending_barge_in: Optional[Dict[str, Any]] = None
        self._last_barge_in_time: Optional[int] = None
        self._asr_text_capable = False
        self.local = LocalTurnTaking(dialogue) if getattr(dialogue.asr, 'supports_barge_in_partials', False) else None

    async def maybe_barge_in(self, result) -> None:
        """Gate robot interruption on sustained user speech, not one VAD frame.

        A noise spike (cough, keyboard, echo) produces a short status==1 burst
        that dies before the confirmation window elapses; only genuine
        interruption intent keeps talking long enough to confirm.
        """
        dialogue = self.dialogue
        if self.local is not None:
            return await self.local.update(result)
        if getattr(dialogue.asr, "barge_in_requires_final", False):
            # Local batch ASR keeps status 2 during the silence needed to
            # finalize an utterance. Counting that as sustained speech cancels
            # replies even for a 200 ms noise burst. Wait for recognized words;
            # empty/noise finals must leave the current reply running.
            if (result.status == 3 and result.text.strip()
                    and dialogue.session.robot.status in (1, 2)
                    and dialogue._active_reply_id is not None):
                dialogue._emit_timeline(
                    "barge_in_confirmed", "strategy",
                    turn_id=dialogue._active_user_turn_id,
                    reply_id=dialogue._active_reply_id,
                    attributes={"evidence": "asr_final", "sawText": True},
                )
                await dialogue._interrupt()
            return
        if self._barge_in_confirm_ms <= 0:
            if result.status == 1 and dialogue.session.robot.status in (1, 2):
                await dialogue._interrupt()
            return
        if result.status == 1:
            if (dialogue.session.robot.status in (1, 2) and dialogue._active_reply_id is not None
                    and self._pending_barge_in is None):
                self._pending_barge_in = {
                    "pending_at": result.speech_time,
                    "reply_id": dialogue._active_reply_id,
                    "saw_text": False,
                }
                dialogue._emit_timeline("barge_in_pending", "strategy",
                                    turn_id=dialogue._active_user_turn_id,
                                    reply_id=dialogue._active_reply_id,
                                    attributes={"confirmMs": self._barge_in_confirm_ms})
        elif result.status == 2:
            if result.text:
                # Cloud partial text for this utterance: real words, not noise.
                self._asr_text_capable = True
                if self._pending_barge_in is not None:
                    self._pending_barge_in["saw_text"] = True
            await self.confirm(result)
        else:
            # status 0 (back to silence) or 3 (utterance ended): the burst was
            # too short to count as an interruption intent.
            await self.cancel_pending(
                "too_short" if result.status == 3 else "silence")

    async def confirm(self, result) -> None:
        dialogue = self.dialogue
        pending = self._pending_barge_in
        if pending is None:
            return
        if (dialogue._active_reply_id is None or dialogue.session.robot.status not in (1, 2)
                or pending["reply_id"] != dialogue._active_reply_id):
            await self.cancel_pending("reply_changed")
            return
        sustained_ms = result.speech_time - pending["pending_at"]
        if sustained_ms < self._barge_in_confirm_ms:
            return
        if (not pending["saw_text"] and self._asr_text_capable):
            # Speech is sustained long enough but the recognizer produced no
            # partial text — likely non-speech noise. Give the cloud one grace
            # window to catch up; if words still never arrive, reject the burst.
            if sustained_ms < self._barge_in_confirm_ms + self._barge_in_text_grace_ms:
                return
            await self.cancel_pending("no_text")
            return
        if (dialogue.session.robot.start_time is not None
                and result.speech_time - dialogue.session.robot.start_time
                < self._barge_in_min_reply_age_ms):
            await self.cancel_pending("reply_too_young")
            return
        if (self._last_barge_in_time is not None
                and result.speech_time - self._last_barge_in_time
                < self._barge_in_cooldown_ms):
            await self.cancel_pending("cooldown")
            return
        self._pending_barge_in = None
        self._last_barge_in_time = result.speech_time
        dialogue._emit_timeline("barge_in_confirmed", "strategy",
                            turn_id=dialogue._active_user_turn_id,
                            reply_id=dialogue._active_reply_id,
                            attributes={"heldMs": sustained_ms, "sawText": pending["saw_text"]})
        await dialogue._interrupt()

    async def cancel_pending(self, reason: str) -> None:
        dialogue = self.dialogue
        if self._pending_barge_in is None:
            return
        self._pending_barge_in = None
        dialogue._emit_timeline("barge_in_cancelled", "strategy", level="info",
                            turn_id=dialogue._active_user_turn_id,
                            attributes={"reason": reason})

    async def interrupt(self, emit: bool = True) -> None:
        dialogue = self.dialogue
        if self.local is not None:
            self.local.release()
        dialogue._playback_deadline.cancel()
        dialogue._pending_action = None
        self._pending_barge_in = None
        if dialogue._active_reply_id is None:
            return
        old_reply_id = dialogue._active_reply_id
        old_turn_id = dialogue._active_turn_id
        old_text = dialogue.session.robot.text
        old_epoch = dialogue._epoch
        dialogue.record_interaction("generation_cancelled", old_reply_id)
        dialogue._epoch += 1
        if dialogue._reply_task and not dialogue._reply_task.done():
            dialogue._reply_task.cancel()
        self.drop_queued_media(old_reply_id)
        dialogue._active_reply_id = None
        dialogue._active_turn_id = None
        dialogue.session.robot.status = 0
        dialogue.session.robot.end_time = dialogue.session.current_time
        dialogue.history.append(HistoryItem("barge_in", "signal", dialogue.session.current_time, dialogue.session.current_time, "signal"))
        if emit:
            dialogue.prefetch.cancel_pending()
            cancel_pending = getattr(dialogue.text_bot, "cancel_pending", None)
            if cancel_pending:
                await cancel_pending()
            dialogue._reply_audio.pop(old_reply_id, None)
            dialogue._emit_timeline("barge_in_detected", "strategy", turn_id=old_turn_id,
                                reply_id=old_reply_id, generation_epoch=old_epoch)
            dialogue._emit_timeline("reply_invalidated", "duplex", turn_id=old_turn_id,
                                reply_id=old_reply_id, generation_epoch=old_epoch)
            if not dialogue._llm_finished:
                dialogue._emit_timeline("llm_cancelled", "llm", turn_id=old_turn_id,
                                    reply_id=old_reply_id, generation_epoch=old_epoch)
            if not dialogue._tts_finished:
                dialogue._emit_timeline("tts_cancelled", "tts", turn_id=old_turn_id,
                                    reply_id=old_reply_id, generation_epoch=old_epoch)
            dialogue._emit_timeline("reply_cancelled", "duplex", turn_id=old_turn_id,
                                reply_id=old_reply_id, generation_epoch=old_epoch,
                                attributes={"partialText": old_text})
            log_event("reply.interrupted", sessionId=dialogue.session.session_id, replyId=old_reply_id)
            await dialogue._control_outputs.put(DuplexEvent("playback_interrupt", {
                "turnId": old_turn_id, "replyId": old_reply_id,
                "generationEpoch": old_epoch, "reason": "barge_in", "partialText": old_text,
            }))
        else:
            dialogue._reply_audio.pop(old_reply_id, None)
            await dialogue._control_outputs.put(DuplexEvent("playback_interrupt", {
                "turnId": old_turn_id, "replyId": old_reply_id,
                "generationEpoch": old_epoch, "reason": "superseded", "partialText": old_text,
            }))

    def drop_queued_media(self, reply_id: str) -> None:
        dialogue = self.dialogue
        retained = []
        while True:
            try:
                event = dialogue._media_outputs.get_nowait()
            except asyncio.QueueEmpty:
                break
            if event is None or event.payload.get("replyId") != reply_id:
                retained.append(event)
        for event in retained:
            dialogue._media_outputs.put_nowait(event)
