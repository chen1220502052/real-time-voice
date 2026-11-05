import asyncio
import json
import math
import struct
from pathlib import Path

from backend.app import ResourceManager, WebSocketConnection, warmup_capabilities
from backend.atomic import AsrVadResult, DemoTts
from backend.domain import Session, SessionState
from backend.protocol import decode_audio_frame, encode_audio_frame


def test_favicon_asset_exists():
    assert (Path(__file__).parent.parent / "web" / "favicon.svg").is_file()


class MemoryWebSocket:
    def __init__(self):
        self.closed = False
        self.text = []
        self.binary = []
        self.binary_ready = asyncio.Event()
        self.reply_complete = asyncio.Event()

    async def send_str(self, value):
        message = json.loads(value)
        self.text.append(message)
        if message.get("type") == "reply_complete":
            self.reply_complete.set()

    async def send_bytes(self, value):
        self.binary.append(value)
        self.binary_ready.set()

    async def close(self):
        self.closed = True


class AudioAwareAsr:
    def __init__(self):
        self.in_speech = False

    async def feed(self, pcm, speech_time):
        peak = max(abs(sample) for sample in struct.unpack("<1600h", pcm))
        if peak and not self.in_speech:
            self.in_speech = True
            return AsrVadResult(speech_time, 1, speech_time - 100, speech_time, "")
        if peak:
            return AsrVadResult(speech_time, 2, 0, speech_time, "")
        self.in_speech = False
        return AsrVadResult(speech_time, 3, 0, speech_time - 100, "你好")

    async def reset(self):
        self.in_speech = False

    async def warmup(self):
        return None


class E2eAgent:
    async def stream_reply(self, messages, signal=None):
        if signal is not None:
            return
        # One character produces a single 100 ms DemoTts frame. This also
        # exercises the short-reply tail flush protocol (< 3 pre-roll frames).
        yield "好"


def test_real_pcm_crosses_websocket_session_and_returns_audio():
    async def run():
        ws = MemoryWebSocket()
        connection = WebSocketConnection(
            ws,
            asr_factory=AudioAwareAsr,
            agent_factory=E2eAgent,
            tts_factory=DemoTts,
        )
        await connection.handle_control({"type": "prepare", "requestId": "prepare-1"})
        await connection.handle_control({"type": "create_session", "requestId": "create-1"})
        session_id = connection.session.session_id

        samples = [int(9000 * math.sin(2 * math.pi * 440 * index / 16000)) for index in range(1600)]
        voiced = struct.pack("<1600h", *samples)
        silence = b"\x00" * 3200
        for sequence, pcm in enumerate((voiced, voiced, silence)):
            wire = encode_audio_frame(0, session_id, sequence, 1000 + sequence * 100, pcm)
            await connection.handle_audio(wire)

        await asyncio.wait_for(ws.binary_ready.wait(), 2)
        await asyncio.wait_for(ws.reply_complete.wait(), 2)
        downlink = decode_audio_frame(ws.binary[0])
        assert downlink.direction == 1
        assert downlink.sequence == 0
        assert any(downlink.payload)
        assert any(item["type"] == "reply_complete" for item in ws.text)
        metadata = next(item for item in ws.text if item["type"] == "avatar_frame")
        timing = metadata["timing"]
        assert timing["clockDomain"] == "server_monotonic"
        assert timing["processedSequence"] == 2
        assert timing["asrFinalMs"] <= timing["botRequestStartedMs"] <= timing["botFirstTextMs"]
        assert timing["ttsStartedMs"] <= timing["ttsFirstPcmMs"] <= timing["firstPcmEnqueuedMs"]
        assert timing["firstPcmEnqueuedMs"] <= timing["firstFrameSendQueuedMs"] <= timing["firstFrameSendStartedMs"]
        assert len(timing["inputReceipts"]) == 3
        event = next(item["event"] for item in ws.text if item["type"] == "session_event"
                     and item["event"]["eventType"] == "playback_stream_started")
        assert event["replyId"] == metadata["replyId"]
        assert event["attributes"]["timing"]["firstFrameSendCompletedMs"] >= timing["firstFrameSendStartedMs"]
        await connection.handle_control({"type": "close_session", "sessionId": session_id})
        await asyncio.sleep(0)
        assert ws.closed

    asyncio.run(run())


def test_startup_warmup_uses_injected_capabilities():
    class Warmable:
        calls = 0

        async def warmup(self):
            type(self).calls += 1

    async def run():
        app = {"asr_factory": Warmable, "tts_factory": Warmable}
        await warmup_capabilities(app)

    asyncio.run(run())
    assert Warmable.calls == 2


def test_audio_sequence_gap_is_reported_and_connection_can_still_close():
    async def run():
        ws = MemoryWebSocket()
        connection = WebSocketConnection(ws, asr_factory=AudioAwareAsr, agent_factory=E2eAgent, tts_factory=DemoTts)
        await connection.handle_control({"type": "prepare", "version": 1})
        await connection.handle_control({"type": "create_session", "version": 1})
        wire = encode_audio_frame(0, connection.session.session_id, 1, 1000, b"\x00" * 3200)
        await connection.handle_audio(wire)
        assert any(item.get("code") == "AUDIO_SEQUENCE_ERROR" for item in ws.text)
        await connection.duplex.close("test_done")
        await asyncio.sleep(0)
        assert ws.closed

    asyncio.run(run())


def test_disconnect_cleanup_cancels_all_connection_tasks():
    async def run():
        ws = MemoryWebSocket()
        connection = WebSocketConnection(ws, asr_factory=AudioAwareAsr, agent_factory=E2eAgent, tts_factory=DemoTts)
        await connection.handle_control({"type": "prepare", "version": 1})
        await connection.handle_control({"type": "create_session", "version": 1})
        await connection.duplex.close("websocket_closed")
        await asyncio.sleep(0.01)
        assert connection.closed
        assert ws.closed
        tasks = (connection.control_task, connection.media_task, connection.ticker_task,
                 connection.timeline_task, connection.stats_task)
        assert all(task.done() for task in tasks if task)

    asyncio.run(run())


def test_prepare_rejects_when_single_local_resource_is_reserved():
    async def run():
        manager = ResourceManager(capacity=1)
        first_ws, second_ws = MemoryWebSocket(), MemoryWebSocket()
        first = WebSocketConnection(first_ws, resource_manager=manager)
        second = WebSocketConnection(second_ws, resource_manager=manager)
        await first.handle_control({"type": "prepare", "version": 1})
        await second.handle_control({"type": "prepare", "version": 1})
        assert any(item.get("ok") is True for item in first_ws.text if item["type"] == "prepare_result")
        assert any(item.get("code") == "NO_RESOURCE" for item in second_ws.text)
        await first.shutdown()
        await second.handle_control({"type": "prepare", "version": 1})
        assert second.resource_reserved
        await second.shutdown()

    asyncio.run(run())


def test_local_voice_preparation_and_shutdown_share_one_resource_lifetime():
    async def run():
        class Runtime:
            def __init__(self):self.owners=set();self.preparations=0;self.fail=False
            async def prepare(self,owner):
                if self.fail:raise RuntimeError('warmup failed')
                self.owners.add(owner);self.preparations+=1;return {}
            async def release(self,owner):self.owners.discard(owner)
        manager=ResourceManager(capacity=1);runtime=Runtime()
        connection=WebSocketConnection(MemoryWebSocket(),resource_manager=manager,local_voice_runtime=runtime)
        await connection.handle_control({'type':'prepare','version':1})
        await connection.handle_control({'type':'prepare','version':1})
        assert runtime.preparations==1 and runtime.owners and manager.in_use==1
        await connection.shutdown();assert not runtime.owners and manager.in_use==0
        runtime.fail=True
        failed=WebSocketConnection(MemoryWebSocket(),resource_manager=manager,local_voice_runtime=runtime)
        await failed.handle_control({'type':'prepare','version':1})
        assert not failed.prepared and manager.in_use==0
        assert failed.ws.text[-1]['code']=='LOCAL_VOICE_NOT_READY'
        await failed.shutdown()
    asyncio.run(run())


def test_downlink_transport_sequence_stays_contiguous_after_internal_drops():
    class PlaybackAck:
        async def mark_packet_sent(self, reply_id, sequence):
            return None

    async def run():
        ws = MemoryWebSocket()
        connection = WebSocketConnection(ws)
        connection.session = Session("sess_sequence", SessionState.ON_SERVICE)
        connection.duplex = PlaybackAck()
        for internal_sequence in (5, 19):
            connection.media_cache.put_nowait({
                "replyId": "reply_test", "turnId": "turn_test",
                "generationEpoch": 1, "sequence": internal_sequence,
                "speechTime": (internal_sequence + 1) * 100,
                "audio": b"\x00" * 3200, "avatarState": "mouth_closed",
            })
        task = asyncio.create_task(connection.playback_ticker())
        try:
            await asyncio.wait_for(ws.binary_ready.wait(), 0.5)
            while len(ws.binary) < 2:
                await asyncio.sleep(0)
            assert [decode_audio_frame(item).sequence for item in ws.binary] == [0, 1]
            avatar_sequences = [item["sequence"] for item in ws.text if item.get("type") == "avatar_frame"]
            assert avatar_sequences == [0, 1]
        finally:
            connection.closed = True
            task.cancel()

    asyncio.run(run())
