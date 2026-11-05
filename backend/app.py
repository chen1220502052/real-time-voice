import asyncio
import json
import os
import re
import secrets
import subprocess
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from aiohttp import WSMsgType, web

from .domain import Session, SessionState
from .duplex import DuplexDialogue
from .events import make_session_event
from .atomic import create_agent, create_asr_engine, create_tts
from .protocol import ProtocolError, SequenceValidator, decode_audio_frame, encode_audio_frame, session_hash
from .observability import configure_observability, log_event, new_trace_id, span, trace_context
from .english_chat import EnglishChatBot, grade_profile
from .text_api import text_bot_handler, interactions_handler, chat_scope_handler
from .curriculum_chat import CurriculumChatBot, validate_chat_options
from .bot_service import BotService
from .bot_store import BotStore
from .media_transport import FencedMediaWriter
from .llm_runtime import LLMHttpPool, use_llm_pool
from .vad_prefetch import prefetch_enabled
from .latency_trace import milliseconds
from .browser_access import BrowserAccess, bootstrap, connection_event, browser_disconnect
from .chapter_practice import register_practice
from .writing_assistant import register_writing
from .accounts import account_access, register_accounts


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
PROJECT_ROOT = WEB_ROOT.parent
SERVICE_VERSION = "0.1.0"
BOT_CAPABILITIES = {"commands": ["connect", "time_out", "hang_up"],
                    "events": ["text_delta", "progress", "action", "done", "error"],
                    "actions": ["hang_up"], "actionWhen": "after_playback",
                    "pushScope": "active_request"}


def _git_commit() -> str:
    configured = os.getenv("GIT_COMMIT")
    if configured:
        return configured
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
            text=True, stderr=subprocess.DEVNULL, timeout=1,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class ResourceManager:
    """Process-local reservation gate for expensive model capacity."""

    def __init__(self, capacity: Optional[int] = None) -> None:
        self.capacity = max(1, capacity or int(os.getenv("MAX_CONCURRENT_SESSIONS", "1")))
        self._owners = set()
        self._lock = asyncio.Lock()
        self.connections = {}

    async def reserve(self, owner: str) -> bool:
        async with self._lock:
            if owner in self._owners:
                return True
            if len(self._owners) >= self.capacity:
                return False
            self._owners.add(owner)
            return True

    async def release(self, owner: str) -> None:
        async with self._lock:
            self._owners.discard(owner)

    @property
    def in_use(self) -> int:
        return len(self._owners)


class WebSocketConnection:
    def __init__(
        self,
        ws: web.WebSocketResponse,
        asr_factory: Callable = create_asr_engine,
        agent_factory: Callable = create_agent,
        tts_factory: Callable = create_tts,
        resource_manager: Optional[ResourceManager] = None,
        curriculum_agent_factory: Optional[Callable] = None,
        local_voice_runtime=None,
    ) -> None:
        self.ws = ws
        self.local_voice_runtime = local_voice_runtime
        self.prepared = False
        self.session: Optional[Session] = None
        self.duplex: Optional[DuplexDialogue] = None
        self.media_cache: "asyncio.Queue" = asyncio.Queue(maxsize=20)
        self.control_task: Optional[asyncio.Task] = None
        self.media_task: Optional[asyncio.Task] = None
        self.ticker_task: Optional[asyncio.Task] = None
        self.timeline_task: Optional[asyncio.Task] = None
        self.stats_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()
        self.media_writer = FencedMediaWriter(
            ws, self.send_lock, lambda: not self.closed,
            lambda packet: packet["replyId"] not in self.interrupted_reply_ids and
            (not hasattr(self.duplex, "_is_current") or
             self.duplex._is_current(packet["replyId"], packet.get("generationEpoch"))),
        )
        self.closed = False
        self.trace_id = new_trace_id()
        self.audio_received = 0
        self.audio_sent = 0
        self.output_sequence = 0
        self.last_vad_status = None
        self.audio_log_every = max(1, int(os.getenv("AUDIO_LOG_EVERY", "10")))
        self.playback_reply_id: Optional[str] = None
        self.playback_burst_sent = 0
        self.playback_burst_packets = 3
        self.input_sequences = SequenceValidator()
        self.interrupted_reply_ids = set()
        self.asr_factory = asr_factory
        self.agent_factory = agent_factory
        self.curriculum_agent_factory = curriculum_agent_factory or agent_factory
        self.tts_factory = tts_factory
        self.resource_manager = resource_manager or ResourceManager()
        self.resource_owner = self.trace_id
        self.resource_reserved = False
        self.event_journal = deque(maxlen=1000)
        self.error_count = 0
        self.connected_at = time.monotonic()
        self.attempt_id = None
        self.disconnect_key = secrets.token_urlsafe(32)
        self.finish_lock = asyncio.Lock()
        self.finish_done = False
        self.prepare_lease_task = None

    async def expire_preparation(self):
        await asyncio.sleep(max(.1, float(os.getenv('PREPARE_LEASE_SECONDS', '30'))))
        if not self.closed and self.session is None:
            log_event('connection.prepare_expired', attemptId=self.attempt_id)
            await self.shutdown()

    async def handle_control(self, message: dict) -> None:
        message_type = message.get("type")
        if message_type == "timing_ping":
            ping_id = message.get("pingId")
            if not isinstance(ping_id, str) or not 1 <= len(ping_id) <= 64:
                return
            received = milliseconds()
            async with self.send_lock:
                if not self.closed:
                    await self.ws.send_str(json.dumps({"type": "timing_pong", "version": 1,
                        "pingId": ping_id, "serverReceivedMs": received,
                        "serverSentMs": milliseconds(), "clockDomain": "server_monotonic"}))
            return
        with span("websocket.control", messageType=message_type):
            log_event("control.received", messageType=message_type)
            if message.get("version", 1) != 1:
                await self.send_error("PROTOCOL_VERSION_UNSUPPORTED", "仅支持协议版本 1，请刷新页面后重试")
                return
            if message_type == "bot_command":
                if not self.duplex or message.get("command") not in {"time_out", "hang_up"}:
                    await self.send_error("INVALID_BOT_COMMAND", "不支持的 Bot 命令")
                    return
                # Connect is delivered once by create_session/pick_up. User
                # transport hang-up remains immediate via close_session.
                try:
                    await self.duplex.submit_command(message["command"], message.get("requestId"))
                except ValueError:
                    await self.send_error("INVALID_BOT_COMMAND", "命令标识无效或重复内容冲突")
                return
            if message_type == "client_event":
                event = message.get("event")
                if isinstance(event, dict):
                    self.event_journal.append(event)
                    log_event("client.event", eventType=event.get("eventType"), sessionId=event.get("sessionId"))
                    if event.get("eventType") == "playback_completed" and self.duplex and event.get("replyId"):
                        try:
                            await self.duplex.mark_playback_completed(event["replyId"], event_id=event.get("eventId"),
                                                                     source_sequence=event.get("sequence"))
                        except (TypeError, ValueError):
                            await self.send_error("INVALID_PLAYBACK_EVENT", "播放回执格式错误")
                    elif event.get("eventType") in {"playback_progress", "playback_stopped"} and self.duplex:
                        try:
                            self.duplex.record_interaction(event["eventType"], event.get("replyId"),
                                                           event_id=event.get("eventId"), source_sequence=event.get("sequence"),
                                                           playedMs=event.get("attributes", {}).get("playedMs"))
                        except (TypeError, ValueError):
                            await self.send_error("INVALID_PLAYBACK_EVENT", "播放进度格式错误")
                return
            if message_type == "prepare":
                attempt = message.get('attemptId')
                if isinstance(attempt, str) and re.fullmatch(r'[a-zA-Z0-9-]{1,64}', attempt):
                    self.attempt_id = attempt
                log_event('connection.prepare', attemptId=self.attempt_id,
                          connectionAgeMs=round((time.monotonic()-self.connected_at)*1000, 1))
                audio = message.get("audio", {})
                if audio and (audio.get("codec") != "pcm_s16le" or audio.get("sampleRate") != 16000
                              or audio.get("channels") != 1 or audio.get("frameDurationMs") != 100):
                    await self.send_json({
                        "type": "prepare_result", "version": 1, "requestId": message.get("requestId"),
                        "ok": False, "code": "AUDIO_FORMAT_UNSUPPORTED",
                        "message": "需要 pcm_s16le / 16kHz / 单声道 / 100ms 音频",
                    })
                    await self.emit_event("protocol_error", "protocol", level="error",
                                          attributes={"code": "AUDIO_FORMAT_UNSUPPORTED"})
                    return
                self.resource_reserved = await self.resource_manager.reserve(self.resource_owner)
                if not self.resource_reserved:
                    log_event('session.capacity_rejected', capacity=self.resource_manager.capacity,
                              inUse=self.resource_manager.in_use)
                    await self.send_json({
                        "type": "prepare_result", "version": 1, "requestId": message.get("requestId"),
                        "ok": False, "code": "NO_RESOURCE", "message": "当前通话名额已满，请稍后重试。",
                        "resources": {"capacity": self.resource_manager.capacity, "inUse": self.resource_manager.in_use},
                    })
                    await self.emit_event("session_rejected", "session", level="warning",
                                          attributes={"code": "NO_RESOURCE"})
                    return
                if self.local_voice_runtime and not self.prepared:
                    try:
                        detail=await self.local_voice_runtime.prepare(self.resource_owner)
                        log_event('local_voice.prepared',**detail)
                    except Exception:
                        await self.resource_manager.release(self.resource_owner)
                        self.resource_reserved=False
                        await self.send_json({'type':'prepare_result','version':1,'ok':False,
                            'code':'LOCAL_VOICE_NOT_READY','message':'本地模型仍在忙碌或预热失败，请稍后重新拨通。'})
                        return
                self.prepared = True
                if self.prepare_lease_task is None:
                    self.prepare_lease_task = asyncio.create_task(self.expire_preparation())
                await self.send_json({
                    "type": "prepare_result", "version": 1, "requestId": message.get("requestId"),
                    "ok": True, "code": "OK", "message": "resource available",
                })
                await self.emit_event("prepare_accepted", "session")
                return
            if message_type == "create_session":
                if not self.prepared or self.session is not None:
                    await self.emit_event("session_rejected", "session", level="warning",
                                          attributes={"code": "INVALID_STATE"})
                    await self.send_json({
                        "type": "create_session_result", "version": 1, "requestId": message.get("requestId"),
                        "ok": False, "code": "INVALID_STATE", "message": "prepare is required",
                    })
                    return
                try:
                    chapter_id = message.get("chapterId", "")
                    grade = message.get("grade", 7)
                    if chapter_id or "chapterId" in message:
                        await asyncio.to_thread(validate_chat_options, {"grade":grade,"chapterId":chapter_id})
                        profile = grade_profile(grade)
                    else:
                        profile = grade_profile(grade)
                except ValueError:
                    await self.send_json({"type": "create_session_result", "version": 1,
                                          "requestId": message.get("requestId"), "ok": False,
                                          "code": "INVALID_GRADE", "message": "请检查年级与教材单元是否匹配"})
                    return
                session_id = "sess_{}".format(uuid.uuid4().hex)
                self.session = Session(session_id=session_id, state=SessionState.PICK_UP,
                                       grade=profile.grade, tts_speed=profile.speed, chapter_id=chapter_id)
                if self.prepare_lease_task:
                    self.prepare_lease_task.cancel()
                self.resource_manager.connections[session_id] = self
                self.duplex = DuplexDialogue(
                    self.session,
                    asr=self.asr_factory(),
                    agent=(self.curriculum_agent_factory() if chapter_id else self.agent_factory()),
                    tts=self.tts_factory(),
                    trace_id=self.trace_id,
                )
                await self.duplex.start()
                self.control_task = asyncio.create_task(self.forward_control_events())
                self.media_task = asyncio.create_task(self.forward_media_events())
                self.ticker_task = asyncio.create_task(self.playback_ticker())
                self.timeline_task = asyncio.create_task(self.forward_timeline_events())
                self.stats_task = asyncio.create_task(self.runtime_stats_ticker())
                log_event("session.created", sessionId=session_id)
                await self.send_json({
                    "type": "create_session_result", "version": 1, "requestId": message.get("requestId"),
                    "ok": True, "code": "OK", "sessionId": session_id,
                    "disconnectKey": self.disconnect_key,
                    "grade": profile.grade, "textBotProtocolVersion": 2,
                    **({"chapterId":chapter_id} if chapter_id else {}),
                    "textBotCapabilities": BOT_CAPABILITIES,
                    "audio": {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1, "frameDurationMs": 100},
                })
                await self.emit_event("session_created", "session", session_id=session_id)
                await self.emit_event("session_ready", "session", session_id=session_id)
                # Contract ordering: only after the client has received the
                # successful create result does the server signal the text bot
                # to generate its opening reply.
                await self.duplex.pick_up()
                return
            if message_type == "close_session":
                log_event("session.close_requested", sessionId=self.session.session_id if self.session else None)
                if self.duplex:
                    await self.duplex.close("user_hangup")
                else:
                    await self.shutdown()
                return
            await self.send_error("INVALID_MESSAGE", "未知控制消息，请刷新页面后重试")

    async def handle_audio(self, binary: bytes) -> None:
        if self.duplex is None or self.session is None:
            return
        self.audio_received += 1
        try:
            if self.audio_received % self.audio_log_every == 0:
                with span("websocket.audio.decode", sessionId=self.session.session_id, sequenceHint=self.audio_received - 1):
                    frame = decode_audio_frame(binary)
            else:
                frame = decode_audio_frame(binary)
        except ProtocolError as exc:
            log_event("audio.rejected", sessionId=self.session.session_id, error=str(exc))
            await self.send_error("INVALID_MESSAGE", str(exc))
            return
        if frame.direction != 0 or frame.session_hash != session_hash(self.session.session_id):
            log_event("audio.rejected", sessionId=self.session.session_id, reason="invalid_audio_session")
            await self.send_error("INVALID_MESSAGE", "音频包不属于当前会话")
            return
        try:
            self.input_sequences.validate(frame.sequence)
        except ProtocolError as exc:
            log_event(
                "audio.sequence_rejected",
                sessionId=self.session.session_id,
                sequence=frame.sequence,
                error=str(exc),
            )
            await self.send_error("AUDIO_SEQUENCE_ERROR", str(exc), component="uplink")
            return
        timing = getattr(self.duplex, "timing", None)
        if timing:
            timing.received(frame.sequence)
        # Raw cross-host wall-clock delta, NOT calibrated one-way latency.
        # Client timestamp is send time and excludes capture/packet assembly.
        if self.audio_received % self.audio_log_every == 0:
            log_event("audio.uplink_lag", sessionId=self.session.session_id,
                      sequence=frame.sequence, lagMs=int(time.time() * 1000) - frame.timestamp_ms)
        if self.audio_received % self.audio_log_every == 0:
            log_event("audio.received", sessionId=self.session.session_id, sequence=frame.sequence, speechTime=frame.speech_time,
                      count=self.audio_received)
        accepted = await self.duplex.push_audio(frame)
        if not accepted:
            await self.send_error("QUEUE_OVERFLOW", "上行音频积压，已丢弃最旧包；请检查模型处理速度", component="duplex")

    async def forward_control_events(self) -> None:
        assert self.duplex is not None
        async for event in self.duplex.events():
            if event.kind == "playback_interrupt":
                reply_id = event.payload["replyId"]
                self.interrupted_reply_ids.add(reply_id)
                retained = []
                while not self.media_cache.empty():
                    packet = self.media_cache.get_nowait()
                    if packet["replyId"] != reply_id:
                        retained.append(packet)
                for packet in retained:
                    self.media_cache.put_nowait(packet)
                await self.send_json({"type": "playback_interrupt", "version": 1, "sessionId": self.session.session_id, **event.payload})
                log_event("playback.interrupt", sessionId=self.session.session_id,
                          **{key: value for key, value in event.payload.items() if key != "partialText"})
            elif event.kind == "close_session":
                await self.send_json({
                    "type": "close_session", "version": 1, "sessionId": self.session.session_id,
                    "initiator": "server", **event.payload,
                })
                log_event("session.close_sent", sessionId=self.session.session_id, **event.payload)
            elif event.kind == "asr_result":
                status = event.payload["status"]
                if status != self.last_vad_status:
                    self.last_vad_status = status
                    log_event("asr.vad_status", sessionId=self.session.session_id,
                              **{key: value for key, value in event.payload.items() if key != "text"})
                await self.send_json({
                    "type": "asr_result", "version": 1, "sessionId": self.session.session_id,
                    **event.payload,
                    **({"timing": self.duplex.timing.turns.get(event.payload.get("turnId"), {})}
                       if status == 3 and hasattr(self.duplex, "timing") else {}),
                })
            elif event.kind == "assistant_delta":
                await self.send_json({
                    "type": "assistant_delta", "version": 1, "sessionId": self.session.session_id,
                    **event.payload,
                })
            elif event.kind == "error":
                log_event("duplex.error", sessionId=self.session.session_id, **event.payload)
                await self.send_error(event.payload.get("code", "INTERNAL_ERROR"),
                                      event.payload.get("message", "处理失败"),
                                      component=event.payload.get("component", "duplex"),
                                      turn_id=event.payload.get("turnId"),
                                      reply_id=event.payload.get("replyId"),
                                      generation_epoch=event.payload.get("generationEpoch"),
                                      reason_code=event.payload.get("reasonCode"))
            elif event.kind == "reply_complete":
                await self.send_json({
                    "type": "reply_complete",
                    "version": 1,
                    "sessionId": self.session.session_id,
                    **event.payload,
                })
            elif event.kind == "reply_failed":
                await self.send_json({
                    "type": "reply_failed",
                    "version": 1,
                    "sessionId": self.session.session_id,
                    **event.payload,
                })
            elif event.kind == "closed":
                await self.shutdown()
                return

    async def forward_timeline_events(self) -> None:
        assert self.duplex is not None
        async for event in self.duplex.timeline_events():
            self.event_journal.append(event)
            await self.send_json({"type": "session_event", "version": 1, "event": event})

    async def runtime_stats_ticker(self) -> None:
        while not self.closed and self.duplex is not None:
            stats = self.duplex.runtime_stats()
            stats["mediaCache"] = self.media_cache.qsize()
            stats["audioReceived"] = self.audio_received
            stats["audioSent"] = self.audio_sent
            stats["errorCount"] = self.error_count
            await self.send_json({
                "type": "runtime_stats", "version": 1,
                "sessionId": self.session.session_id if self.session else None,
                **stats,
            })
            await asyncio.sleep(0.5)

    async def forward_media_events(self) -> None:
        assert self.duplex is not None
        async for event in self.duplex.media_events():
            packet = event.payload
            if packet["replyId"] in self.interrupted_reply_ids:
                continue
            # Bounded backpressure applies only to media. Control events use a
            # separate task and remain immediately deliverable.
            await self.media_cache.put(packet)

    async def playback_ticker(self) -> None:
        while not self.closed:
            if self.media_cache.empty() or self.session is None or self.duplex is None:
                await asyncio.sleep(0.01)
                continue
            packet = self.media_cache.get_nowait()
            if packet["replyId"] in self.interrupted_reply_ids:
                continue
            # Prime the browser-side jitter buffer as soon as a reply begins.
            # The remaining packets retain the 100 ms real-time cadence.
            if packet["replyId"] != self.playback_reply_id:
                self.playback_reply_id = packet["replyId"]
                self.playback_burst_sent = 0
            wire_sequence = self.output_sequence
            metadata = {
                "type": "avatar_frame", "version": 1, "sessionId": self.session.session_id,
                "sequence": wire_sequence, "avatarState": packet["avatarState"],
                "turnId": packet.get("turnId"), "replyId": packet["replyId"],
                "generationEpoch": packet.get("generationEpoch"),
            }
            timing = getattr(self.duplex, "timing", None)
            if self.playback_burst_sent == 0 and timing:
                timing.mark(packet["replyId"], "firstFrameSendQueuedMs")
                metadata["timing"] = timing.snapshot(packet["replyId"])
            wire = encode_audio_frame(1, self.session.session_id, wire_sequence, int(time.time() * 1000), packet["audio"])
            if not await self.media_writer.send(packet, metadata, wire):
                continue
            if self.playback_burst_sent == 0 and timing:
                timing.mark(packet["replyId"], "firstFrameSendCompletedMs")
            self.output_sequence += 1
            await self.duplex.mark_packet_sent(packet["replyId"], packet["sequence"])
            self.audio_sent += 1
            if self.playback_burst_sent == 0:
                await self.emit_event(
                    "playback_stream_started", "playback", turn_id=packet.get("turnId"),
                    reply_id=packet["replyId"], generation_epoch=packet.get("generationEpoch"),
                    sequence=wire_sequence,
                    attributes={"timing": timing.snapshot(packet["replyId"])} if timing else {},
                )
            if self.audio_sent % self.audio_log_every == 0:
                log_event("audio.sent", sessionId=self.session.session_id, sequence=wire_sequence,
                          speechTime=packet["speechTime"], count=self.audio_sent)
            self.playback_burst_sent += 1
            if self.playback_burst_sent >= self.playback_burst_packets:
                await asyncio.sleep(0.1)

    async def send_json(self, value: dict) -> None:
        if not self.closed:
            async with self.send_lock:
                await self.ws.send_str(json.dumps(value, ensure_ascii=False))

    async def send_bytes(self, value: bytes) -> None:
        if not self.closed:
            async with self.send_lock:
                await self.ws.send_bytes(value)

    async def send_error(
        self, code: str, message: str, component: str = "protocol", *,
        turn_id: Optional[str] = None, reply_id: Optional[str] = None,
        generation_epoch: Optional[int] = None,
        reason_code: Optional[str] = None,
    ) -> None:
        self.error_count += 1
        public_messages = {
            "ASR_FAILED": "语音识别失败，请检查 ASR 配置",
            "LLM_FAILED": "文本模型请求失败，请检查 Ollama 服务",
            "TTS_FAILED": "语音合成失败，请检查 TTS 配置",
            "PLAYBACK_FAILED": "音频生成队列失败，请重新拨通",
            "REPLY_FAILED": "回复生成失败，请检查模型配置",
        }
        public_message = public_messages.get(code, message)
        reasons = {
            "NLS_AUTH_FAILED": "语音识别鉴权失败，请检查 NLS 角色权限和项目配置",
            "NLS_ROLE_CREDENTIALS_UNAVAILABLE": "无法获取 ECS RAM 角色凭证，请检查角色绑定和 IMDSv2",
            "NLS_TOKEN_REFRESH_FAILED": "获取语音识别 Token 失败，请检查角色权限和网络",
            "NLS_TOKEN_INVALID": "语音识别 Token 无效或已过期",
            "NLS_AUTH_CONFIG_INVALID": "语音识别认证配置无效",
            "NLS_NOT_READY": "语音识别任务未就绪，请重新连接",
            "NLS_CONNECTION_CLOSED": "语音识别连接提前关闭，请重新连接",
            "NLS_CONNECTION_FAILED": "语音识别连接失败，请检查网络和 TLS",
            "NLS_SERVICE_FAILED": "语音识别服务拒绝请求，请检查项目配置",
            "NLS_UPLOAD_FAILED": "语音识别上传失败，请重新连接",
            "NLS_UPLOAD_QUEUE_FULL": "语音识别上传队列已满，请重新连接",
        }
        reason = reason_code if code == "ASR_FAILED" and reason_code in reasons else None
        if reason:
            public_message = reasons[reason]
        await self.emit_event("component_error", component, level="error",
                              turn_id=turn_id, reply_id=reply_id,
                              generation_epoch=generation_epoch,
                              attributes={"code": code, "message": public_message,
                                          **({"reasonCode": reason} if reason else {})})
        await self.send_json({
            "type": "error", "version": 1, "code": code, "message": public_message,
            "turnId": turn_id, "replyId": reply_id,
            "generationEpoch": generation_epoch,
            **({"reasonCode": reason} if reason else {}),
        })

    async def emit_event(
        self, event_type: str, component: str, *, level: str = "info",
        session_id: Optional[str] = None, turn_id: Optional[str] = None,
        reply_id: Optional[str] = None, generation_epoch: Optional[int] = None,
        sequence: Optional[int] = None, attributes: Optional[dict] = None,
    ) -> dict:
        event = make_session_event(
            event_type, component, level=level,
            session_id=session_id or (self.session.session_id if self.session else None),
            turn_id=turn_id, reply_id=reply_id, generation_epoch=generation_epoch,
            sequence=sequence, attributes=attributes,
        )
        self.event_journal.append(event)
        if event_type in {'prepare_accepted', 'session_rejected', 'session_created', 'component_error'}:
            log_event('connection.stage', stage=event_type, sessionId=event.get('sessionId'),
                      code=(attributes or {}).get('code'))
        await self.send_json({"type": "session_event", "version": 1, "event": event})
        return event

    async def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.local_voice_runtime:
            await self.local_voice_runtime.release(self.resource_owner)
        if self.session:
            self.resource_manager.connections.pop(self.session.session_id, None)
        if self.resource_reserved:
            await self.resource_manager.release(self.resource_owner)
            self.resource_reserved = False
        log_event("websocket.closed", sessionId=self.session.session_id if self.session else None,
                  audioReceived=self.audio_received, audioSent=self.audio_sent, attemptId=self.attempt_id,
                  connectionAgeMs=round((time.monotonic()-self.connected_at)*1000, 1), prepared=self.prepared)
        current = asyncio.current_task()
        for task in (self.control_task, self.media_task, self.ticker_task, self.timeline_task, self.stats_task, self.prepare_lease_task):
            if task and task is not current:
                task.cancel()
        if not self.ws.closed:
            await self.ws.close()

    async def finish(self) -> None:
        """Release the reservation even when the browser cancels its handler."""
        async with self.finish_lock:
            if self.finish_done:
                return
            try:
                if self.duplex and self.session and self.session.state != SessionState.HANG_UP:
                    try:
                        await asyncio.wait_for(self.duplex.close("websocket_closed"), 5)
                    except Exception:
                        log_event("session.cleanup_incomplete", sessionId=self.session.session_id)
            finally:
                await self.shutdown()
                self.finish_done = True


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    if not request.app['browser_access'].authorized(request):
        log_event('websocket.auth_rejected', reason='missing_expired_or_cross_origin_cookie')
        return web.json_response({'code': 'AUTH_REQUIRED', 'message': '登录已失效，请刷新页面重新登录。'},
                                 status=401, headers={'Cache-Control': 'no-store'})
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    connection = WebSocketConnection(
        ws,
        asr_factory=request.app["asr_factory"],
        agent_factory=request.app["agent_factory"],
        tts_factory=request.app["tts_factory"],
        resource_manager=request.app["resource_manager"],
        local_voice_runtime=request.app.get('local_voice_runtime'),
        curriculum_agent_factory=lambda: request.app["bot_service"].session(request.app["text_bot_factory"]()),
    )
    connection.account_token = request.app["accounts"].token(request)
    with trace_context(connection.trace_id):
        log_event("websocket.connected", peer=request.remote)
        await connection.emit_event("connection_opened", "websocket", attributes={"peer": request.remote})
        try:
            async for message in ws:
                if request.app["accounts"].enabled and not request.app["accounts"].user(request):
                    await ws.close(code=1008, message=b"Login expired")
                    break
                if message.type == WSMsgType.TEXT:
                    try:
                        await connection.handle_control(json.loads(message.data))
                    except json.JSONDecodeError:
                        await connection.send_json({"type": "error", "version": 1, "code": "INVALID_MESSAGE", "message": "invalid JSON"})
                elif message.type == WSMsgType.BINARY:
                    await connection.handle_audio(message.data)
                elif message.type == WSMsgType.ERROR:
                    await connection.emit_event("connection_lost", "websocket", level="error")
                    break
        finally:
            cleanup = asyncio.create_task(connection.finish())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The cleanup task survives handler_cancellation=True.
                # Always release capacity; never strand a disconnected call.
                await asyncio.shield(cleanup)
                raise
    return ws


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_ROOT / "home.html")


async def voice_page_handler(request: web.Request) -> web.FileResponse:
    if not request.app["voice_enabled"]:
        raise web.HTTPFound("/text.html")
    return web.FileResponse(WEB_ROOT / "index.html")


async def health_handler(request: web.Request) -> web.Response:
    manager = request.app["resource_manager"]
    return web.json_response({
        "ok": not request.app["bot_service"].storage_failed,
        "service": "real-time-voice",
        **request.app["service_info"],
        "warmup": request.app.get("warmup_metrics", {}),
        "protocolVersion": 1,
        "textBotProtocolVersion": 2,
        "textBotCapabilities": BOT_CAPABILITIES,
        "textBotStateOwner": "bot",
        "textBotPreparation": (request.app["voice_enabled"] and
                               os.getenv("AGENT_BACKEND", "demo") == "english_chat" and
                               prefetch_enabled()),
        "voiceEnabled": request.app["voice_enabled"],
        "browserAuth": 'account' if request.app["accounts"].enabled else ('cookie' if request.app['browser_access'].enabled else 'none'),
        "grades": [3, 4, 5, 6, 7, 8, 9],
        "audio": {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1, "frameDurationMs": 100},
        "capabilities": {
            "asr": os.getenv("ASR_BACKEND", "funasr") if request.app["voice_enabled"] else "disabled",
            "agent": os.getenv("AGENT_BACKEND", "demo"),
            "tts": os.getenv("TTS_BACKEND", "piper") if request.app["voice_enabled"] else "disabled",
            "avatar": "rms-images",
        },
        "resources": {"capacity": manager.capacity, "inUse": manager.in_use},
    }, status=503 if request.app["bot_service"].storage_failed else 200)


async def warmup_capabilities(app: web.Application) -> None:
    app.setdefault("warmup_metrics", {})

    async def warm(name: str, factory: Callable) -> None:
        capability = factory()
        warmup = getattr(capability, "warmup", None)
        if warmup is None:
            return
        started = time.perf_counter()
        await warmup()
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        app["warmup_metrics"][name] = duration_ms

        log_event("capability.warmed", capability=name, durationMs=duration_ms)

    tasks=[warm("asr",app["asr_factory"]),warm("tts",app["tts_factory"])]
    from . import local_llm
    if (app['asr_factory'] is create_asr_engine and os.getenv('ASR_BACKEND') in {'mlx','parakeet'} and local_llm.enabled('chat')
            and os.getenv('LOCAL_VOICE_WARMUP_LLM','1')=='1'):
        async def warm_chat():
            started=time.perf_counter()
            from types import SimpleNamespace
            from .curriculum_chat import voice_chat_prompt
            prompt=voice_chat_prompt(SimpleNamespace(chapter={'grade':'8年级','unit':'English conversation'}))
            await local_llm.complete([{'role':'system','content':prompt},
                                      {'role':'user','content':'Hello'}],purpose='voice_chat',max_tokens=128,timeout=60)
            app['warmup_metrics']['local_chat']=round((time.perf_counter()-started)*1000,2)
        tasks.append(warm_chat())
    await asyncio.gather(*tasks)


async def warmup_chat_runtime(app):
    from .chat_library import chat_library
    from .curriculum_chat import warmup_fallback
    started=time.perf_counter()
    await asyncio.to_thread(chat_library)
    try:
        await asyncio.to_thread(warmup_fallback)
    except ImportError:
        log_event('chat.fallback_not_installed')
    app['warmup_metrics']['chat']=round((time.perf_counter()-started)*1000,2)

def create_app(
    asr_factory: Callable = create_asr_engine,
    agent_factory: Callable = create_agent,
    tts_factory: Callable = create_tts,
    warmup: bool = True,
    text_bot_factory: Optional[Callable] = None,
    voice_enabled: bool = True,
    bot_state_path: Optional[str] = None,
) -> web.Application:
    configure_observability()
    @web.middleware
    async def llm_scope(request, handler):
        service = request.app.get("bot_service")
        if request.path == "/ws" and service and service.storage_failed:
            return web.json_response({"code": "BOT_STORAGE_FAILED"}, status=503)
        with use_llm_pool(request.app["llm_http_pool"]):
            return await handler(request)

    app = web.Application(client_max_size=64 * 1024, middlewares=[account_access, llm_scope])
    app["llm_http_pool"] = LLMHttpPool()
    from . import local_llm
    from .local_inference import LocalVoiceRuntime
    app['local_voice_runtime']=(LocalVoiceRuntime(app['llm_http_pool'].local_inference)
        if voice_enabled and asr_factory is create_asr_engine and
        os.getenv('ASR_BACKEND') in {'mlx','parakeet'} and local_llm.enabled('chat') else None)
    app["asr_factory"] = asr_factory
    if bot_state_path is None:
        bot_state_path = os.getenv("BOT_STATE_DB", str(PROJECT_ROOT / ".local" / "bot-state.sqlite3")
                                   if agent_factory is create_agent else ":memory:")
    service = BotService(BotStore(bot_state_path))
    app["bot_service"] = service
    app["agent_factory"] = lambda: service.session(agent_factory())
    app["text_bot_factory"] = text_bot_factory or CurriculumChatBot
    app["voice_enabled"] = voice_enabled
    app["tts_factory"] = tts_factory
    app["resource_manager"] = ResourceManager()
    app['browser_access'] = BrowserAccess()
    register_accounts(app)
    app['connection_event_limits'] = {}
    app['disconnect_tasks'] = set()
    app["service_info"] = {
        "version": SERVICE_VERSION,
        "gitCommit": _git_commit(),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    app["warmup_metrics"] = {}
    if os.getenv('CHAT_WARMUP_ENABLED','0').lower() in {'1','true','on'}:
        app.on_startup.append(warmup_chat_runtime)
    if warmup and voice_enabled:
        app.on_startup.append(warmup_capabilities)
    if voice_enabled:
        app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post('/api/browser-session', bootstrap)
    app.router.add_post('/api/connection-events', connection_event)
    app.router.add_post('/api/browser-disconnect', browser_disconnect)
    app.router.add_post("/api/text-bot/replies", text_bot_handler)
    app.router.add_get("/api/text-bot/scopes/{chapter_id}", chat_scope_handler)
    app.router.add_post("/api/text-bot/interactions", interactions_handler)
    register_practice(app)
    register_writing(app)
    async def close_bot_service(app):
        try:
            if app['disconnect_tasks']:
                await asyncio.gather(*app['disconnect_tasks'], return_exceptions=True)
            await app["bot_service"].close()
        finally:
            from .providers.speech_worker import close_speech_workers
            await close_speech_workers()
            await app["llm_http_pool"].close()
            app["accounts"].close()
    app.on_cleanup.append(close_bot_service)
    app.router.add_get("/", index_handler)
    app.router.add_get("/voice.html", voice_page_handler)
    app.router.add_static("/", WEB_ROOT, show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(voice_enabled=os.getenv("VOICE_ENABLED", "1") != "0"),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8765")),
        handler_cancellation=True,
    )
