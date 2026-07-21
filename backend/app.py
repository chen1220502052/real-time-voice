import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

from .domain import Session, SessionState
from .duplex import DuplexDialogue
from .protocol import AudioFrame, ProtocolError, decode_audio_frame, encode_audio_frame, session_hash
from .observability import configure_observability, log_event, new_trace_id, span, trace_context


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class WebSocketConnection:
    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.prepared = False
        self.session: Optional[Session] = None
        self.duplex: Optional[DuplexDialogue] = None
        self.media_cache: "asyncio.Queue" = asyncio.Queue(maxsize=20)
        self.forward_task: Optional[asyncio.Task] = None
        self.ticker_task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.trace_id = new_trace_id()
        self.audio_received = 0
        self.audio_sent = 0
        self.last_vad_status = None
        self.audio_log_every = max(1, int(os.getenv("AUDIO_LOG_EVERY", "10")))

    async def handle_control(self, message: dict) -> None:
        message_type = message.get("type")
        with span("websocket.control", messageType=message_type):
            log_event("control.received", messageType=message_type)
            if message_type == "prepare":
                self.prepared = True
                await self.send_json({
                    "type": "prepare_result", "version": 1, "requestId": message.get("requestId"),
                    "ok": True, "code": "OK", "message": "resource available",
                })
                return
            if message_type == "create_session":
                if not self.prepared or self.session is not None:
                    await self.send_json({
                        "type": "create_session_result", "version": 1, "requestId": message.get("requestId"),
                        "ok": False, "code": "INVALID_STATE", "message": "prepare is required",
                    })
                    return
                session_id = "sess_{}".format(uuid.uuid4().hex)
                self.session = Session(session_id=session_id, state=SessionState.PICK_UP)
                self.duplex = DuplexDialogue(self.session, trace_id=self.trace_id)
                await self.duplex.start()
                self.forward_task = asyncio.create_task(self.forward_duplex_events())
                self.ticker_task = asyncio.create_task(self.playback_ticker())
                log_event("session.created", sessionId=session_id)
                await self.send_json({
                    "type": "create_session_result", "version": 1, "requestId": message.get("requestId"),
                    "ok": True, "code": "OK", "sessionId": session_id,
                    "audio": {"codec": "pcm_s16le", "sampleRate": 16000, "channels": 1, "frameDurationMs": 100},
                })
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
            await self.send_json({"type": "error", "version": 1, "code": "INVALID_MESSAGE", "message": "unknown control message"})

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
            await self.send_json({"type": "error", "version": 1, "code": "INVALID_MESSAGE", "message": str(exc)})
            return
        if frame.direction != 0 or frame.session_hash != session_hash(self.session.session_id):
            log_event("audio.rejected", sessionId=self.session.session_id, reason="invalid_audio_session")
            await self.send_json({"type": "error", "version": 1, "code": "INVALID_MESSAGE", "message": "invalid audio session"})
            return
        if self.audio_received % self.audio_log_every == 0:
            log_event("audio.received", sessionId=self.session.session_id, sequence=frame.sequence, speechTime=frame.speech_time,
                      count=self.audio_received)
        await self.duplex.push_audio(frame)

    async def forward_duplex_events(self) -> None:
        assert self.duplex is not None
        async for event in self.duplex.events():
            if event.kind == "playback_packet":
                # Backpressure is intentional: never discard already-generated robot speech.
                await self.media_cache.put(event.payload)
            elif event.kind == "playback_interrupt":
                reply_id = event.payload["replyId"]
                retained = []
                while not self.media_cache.empty():
                    packet = self.media_cache.get_nowait()
                    if packet["replyId"] != reply_id:
                        retained.append(packet)
                for packet in retained:
                    self.media_cache.put_nowait(packet)
                await self.send_json({"type": "playback_interrupt", "version": 1, "sessionId": self.session.session_id, **event.payload})
                log_event("playback.interrupt", sessionId=self.session.session_id, **event.payload)
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
                    log_event("asr.vad_status", sessionId=self.session.session_id, **event.payload)
            elif event.kind == "error":
                log_event("duplex.error", sessionId=self.session.session_id, **event.payload)
                await self.send_json({"type": "error", "version": 1, **event.payload})
            elif event.kind == "closed":
                await self.shutdown()
                return

    async def playback_ticker(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.1)
            if self.media_cache.empty() or self.session is None or self.duplex is None:
                continue
            packet = self.media_cache.get_nowait()
            await self.send_json({
                "type": "avatar_frame", "version": 1, "sessionId": self.session.session_id,
                "sequence": packet["sequence"], "avatarState": packet["avatarState"],
            })
            wire = encode_audio_frame(1, self.session.session_id, packet["sequence"], int(time.time() * 1000), packet["audio"])
            await self.send_bytes(wire)
            await self.duplex.mark_packet_sent(packet["replyId"], packet["sequence"])
            self.audio_sent += 1
            if self.audio_sent % self.audio_log_every == 0:
                log_event("audio.sent", sessionId=self.session.session_id, sequence=packet["sequence"],
                          speechTime=packet["speechTime"], count=self.audio_sent)

    async def send_json(self, value: dict) -> None:
        if not self.closed:
            async with self.send_lock:
                await self.ws.send_str(json.dumps(value, ensure_ascii=False))

    async def send_bytes(self, value: bytes) -> None:
        if not self.closed:
            async with self.send_lock:
                await self.ws.send_bytes(value)

    async def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        log_event("websocket.closed", sessionId=self.session.session_id if self.session else None,
                  audioReceived=self.audio_received, audioSent=self.audio_sent)
        current = asyncio.current_task()
        for task in (self.forward_task, self.ticker_task):
            if task and task is not current:
                task.cancel()
        if not self.ws.closed:
            await self.ws.close()


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    connection = WebSocketConnection(ws)
    with trace_context(connection.trace_id):
        log_event("websocket.connected", peer=request.remote)
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        await connection.handle_control(json.loads(message.data))
                    except json.JSONDecodeError:
                        await connection.send_json({"type": "error", "version": 1, "code": "INVALID_MESSAGE", "message": "invalid JSON"})
                elif message.type == WSMsgType.BINARY:
                    await connection.handle_audio(message.data)
                elif message.type == WSMsgType.ERROR:
                    break
        finally:
            if connection.duplex and connection.session and connection.session.state != SessionState.HANG_UP:
                await connection.duplex.close("websocket_closed")
            await connection.shutdown()
    return ws


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_ROOT / "index.html")


def create_app() -> web.Application:
    configure_observability()
    app = web.Application()
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/", index_handler)
    app.router.add_static("/", WEB_ROOT, show_index=False)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="127.0.0.1", port=8765)
