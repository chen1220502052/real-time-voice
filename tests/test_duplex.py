import asyncio
import os

from backend.atomic import AsrVadResult, DemoAgent, DemoTts, OPENING_MESSAGE, VadAsrStub
from backend.domain import Session, SessionState
from backend.duplex import DuplexDialogue
from backend.protocol import AudioFrame, session_hash


async def collect_one_reply(duplex):
    packets = []

    async def consume_media():
        async for event in duplex.media_events():
            packets.append(event.payload)
            await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])

    media_task = asyncio.create_task(consume_media())
    try:
        async for event in duplex.events():
            if event.kind == "reply_complete":
                await duplex.mark_playback_completed(event.payload["replyId"])
                return packets
    finally:
        media_task.cancel()


def make_stub_duplex(session_id="sess_test", **kwargs):
    return DuplexDialogue(
        Session(session_id, SessionState.PICK_UP),
        asr=kwargs.pop("asr", VadAsrStub()),
        agent=kwargs.pop("agent", DemoAgent()),
        tts=kwargs.pop("tts", DemoTts()),
        **kwargs,
    )


def test_duplex_development_reply_becomes_playback_packets():
    async def run():
        duplex = make_stub_duplex()
        await duplex.start()
        await duplex.submit_text_for_development("你好")
        packets = await asyncio.wait_for(collect_one_reply(duplex), 2)
        assert packets
        assert duplex.session.robot.status == 0
        assert [item.role for item in duplex.history.items] == ["user", "signal", "robot"]
        await duplex.close("test_done")

    asyncio.run(run())


def test_pick_up_signal_generates_the_fixed_opening_reply():
    async def run():
        duplex = make_stub_duplex("sess_pickup")
        await duplex.start()
        await duplex.pick_up()
        packets = await asyncio.wait_for(collect_one_reply(duplex), 2)
        assert packets
        assert [item.message for item in duplex.history.items] == ["pick_up", "reply_start", OPENING_MESSAGE]
        await duplex.close("test_done")

    asyncio.run(run())


def test_interrupt_control_event_is_not_blocked_by_full_old_media_queue():
    class SpeechStartAsr:
        async def feed(self, pcm, speech_time):
            return AsrVadResult(speech_time, 1, speech_time - 100, speech_time, "")

        async def reset(self):
            return None

    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "0"
        duplex = make_stub_duplex(asr=SpeechStartAsr(), media_queue_size=2)
        await duplex.start()
        await duplex.pick_up()
        await asyncio.sleep(0.02)
        frame = AudioFrame(0, session_hash("sess_test"), 0, 0, b"\x01\x00" * 1600)
        await duplex.push_audio(frame)

        async def wait_interrupt():
            async for event in duplex.events():
                if event.kind == "playback_interrupt":
                    return event

        interrupt = await asyncio.wait_for(wait_interrupt(), 0.5)
        assert interrupt.payload["reason"] == "barge_in"
        assert duplex._media_outputs.qsize() == 0
        await duplex.close("test_done")

    asyncio.run(run())


def test_llm_receives_no_internal_control_signals():
    class CapturingAgent:
        def __init__(self):
            self.calls = []

        async def stream_reply(self, messages, signal=None):
            self.calls.append((list(messages), signal))
            yield "好的"

    async def run():
        agent = CapturingAgent()
        duplex = make_stub_duplex(agent=agent)
        await duplex.start()
        await duplex.pick_up()
        await asyncio.wait_for(collect_one_reply(duplex), 2)
        await duplex.submit_text_for_development("请介绍一下")
        await asyncio.wait_for(collect_one_reply(duplex), 2)

        second_messages, second_signal = agent.calls[1]
        assert second_signal is None
        assert second_messages == [
            {"role": "machine", "content": "好的"},
            {"role": "user", "content": "请介绍一下"},
        ]
        await duplex.close("test_done")

    asyncio.run(run())


def test_duplex_queues_are_bounded():
    duplex = make_stub_duplex(input_queue_size=7, control_queue_size=5, media_queue_size=3, timeline_queue_size=11)
    assert duplex._inputs.maxsize == 7
    assert duplex._control_outputs.maxsize == 5
    assert duplex._media_outputs.maxsize == 3
    assert duplex._timeline_outputs.maxsize == 11


def test_input_queue_overflow_drops_media_without_blocking_control():
    async def run():
        duplex = make_stub_duplex(input_queue_size=1)
        frame = AudioFrame(0, session_hash("sess_test"), 0, 0, b"\x00" * 3200)
        assert await duplex.push_audio(frame) is True
        assert await duplex.push_audio(frame) is False

    asyncio.run(run())


def test_default_input_queue_is_one_second_of_audio():
    assert make_stub_duplex()._inputs.maxsize == 10


def test_single_packet_final_gets_a_stable_turn_id():
    class ImmediateFinalAsr:
        async def feed(self, pcm, speech_time):
            return AsrVadResult(speech_time, 3, speech_time - 70, speech_time - 20, "你好")

        async def reset(self):
            return None

    async def run():
        duplex = make_stub_duplex(asr=ImmediateFinalAsr())
        await duplex.start()
        frame = AudioFrame(0, session_hash("sess_test"), 0, 0, b"\x01\x00" * 1600)
        await duplex.push_audio(frame)
        event = await asyncio.wait_for(anext(duplex.events()), 0.5)
        assert event.kind == "asr_result"
        assert event.payload["status"] == 3
        assert event.payload["turnId"].startswith("turn_")
        assert duplex.history.items[0].turn_id == event.payload["turnId"]
        await duplex.close("test_done")

    asyncio.run(run())


def test_zero_timestamps_are_preserved_in_user_and_robot_history():
    async def run():
        duplex = make_stub_duplex()
        duplex.session.state = SessionState.ON_SERVICE
        duplex.session.current_time = 500
        duplex.session.user.start_time = 0
        duplex.session.user.end_time = 75
        await duplex._accept_final_text("首包开始")
        assert duplex.history.items[0].start_time == 0
        assert duplex.history.items[0].end_time == 75

        await duplex._interrupt(emit=False)
        duplex._active_reply_id = "reply_zero"
        duplex._active_turn_id = "turn_zero"
        duplex.session.robot.start_time = 0
        duplex.session.robot.text = "首包回复"
        await duplex._finalize_reply("reply_zero")
        robot_item = duplex.history.items[-1]
        assert robot_item.start_time == 0
        assert robot_item.end_time == 500
        await duplex.close("test_done")

    asyncio.run(run())


def test_llm_failure_converges_and_next_reply_can_succeed():
    class FailOnceAgent:
        def __init__(self):
            self.calls = 0

        async def stream_reply(self, messages, signal=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary llm failure")
            yield "恢复"

    async def run():
        duplex = make_stub_duplex(agent=FailOnceAgent())
        await duplex.start()
        await duplex.submit_text_for_development("第一轮")
        failed = await asyncio.wait_for(anext(duplex.events()), 0.5)
        assert failed.kind == "reply_failed"
        assert failed.payload["code"] == "LLM_FAILED"
        assert duplex.session.robot.status == 0
        assert duplex._active_reply_id is None
        error = await asyncio.wait_for(anext(duplex.events()), 0.5)
        assert error.kind == "error"
        await duplex.submit_text_for_development("第二轮")
        packets = await asyncio.wait_for(collect_one_reply(duplex), 2)
        assert packets
        await duplex.close("test_done")

    asyncio.run(run())


def test_tts_failure_converges_and_next_reply_can_succeed():
    class FailOnceTts:
        def __init__(self):
            self.calls = 0

        async def stream_pcm(self, text, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary tts failure")
            yield b"\x00" * 3200

    async def run():
        duplex = make_stub_duplex(tts=FailOnceTts())
        await duplex.start()
        await duplex.submit_text_for_development("第一轮")
        async def wait_failed():
            async for event in duplex.events():
                if event.kind == "reply_failed":
                    return event

        failed = await asyncio.wait_for(wait_failed(), 0.5)
        assert failed.kind == "reply_failed"
        assert failed.payload["code"] == "TTS_FAILED"
        assert duplex.session.robot.status == 0
        assert duplex._media_outputs.empty()
        # The public error event follows reply_failed.
        await asyncio.wait_for(anext(duplex.events()), 0.5)
        await duplex.submit_text_for_development("第二轮")
        packets = await asyncio.wait_for(collect_one_reply(duplex), 2)
        assert packets
        await duplex.close("test_done")

    asyncio.run(run())


def test_asr_reset_failure_does_not_block_session_close():
    class FailingResetAsr:
        async def feed(self, pcm, speech_time):
            return AsrVadResult(speech_time, 0)

        async def reset(self):
            raise RuntimeError("cloud recognizer already stopped")

    async def run():
        duplex = make_stub_duplex(asr=FailingResetAsr())
        await duplex.start()
        await duplex.close("user_hangup")

        kinds = []
        async for event in duplex.events():
            kinds.append(event.kind)

        assert kinds == ["close_session", "closed"]
        assert duplex.session.state == SessionState.HANG_UP

    asyncio.run(run())


def test_interrupt_while_llm_is_generating_cancels_reply_before_audio():
    class SlowAgent:
        async def stream_reply(self, messages, signal=None):
            await asyncio.sleep(10)
            yield "不会到达"

    class SpeechStartAsr:
        async def feed(self, pcm, speech_time):
            return AsrVadResult(speech_time, 1, 0, speech_time, "")

        async def reset(self):
            return None

    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "0"
        duplex = make_stub_duplex(agent=SlowAgent(), asr=SpeechStartAsr())
        await duplex.start()
        await duplex.pick_up()
        await asyncio.sleep(0)
        old_reply = duplex._active_reply_id
        old_epoch = duplex._epoch
        await duplex.push_audio(AudioFrame(0, session_hash("sess_test"), 0, 0, b"\x01\x00" * 1600))

        async def wait_interrupt():
            async for event in duplex.events():
                if event.kind == "playback_interrupt":
                    return event

        event = await asyncio.wait_for(wait_interrupt(), 0.5)
        assert event.payload["replyId"] == old_reply
        assert duplex._reply_task.cancelled() or duplex._reply_task.cancelling()
        assert not duplex._is_current(old_reply, old_epoch)
        await duplex.close("test_done")

    asyncio.run(run())


def test_interrupt_during_browser_tail_keeps_reply_active_until_playback_ack():
    class SpeechStartAsr:
        async def feed(self, pcm, speech_time):
            return AsrVadResult(speech_time, 1, 0, speech_time, "")

        async def reset(self):
            return None

    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "0"
        duplex = make_stub_duplex(asr=SpeechStartAsr())
        await duplex.start()

        async def consume_media():
            async for event in duplex.media_events():
                await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])

        media_task = asyncio.create_task(consume_media())
        await duplex.pick_up()
        stream_done = None
        async for event in duplex.events():
            if event.kind == "reply_complete":
                stream_done = event
                break
        old_reply = stream_done.payload["replyId"]
        assert duplex._active_reply_id == old_reply
        assert not any(item.role == "robot" for item in duplex.history.items)

        frame = AudioFrame(0, session_hash("sess_test"), 0, 0, b"\x01\x00" * 1600)
        await duplex.push_audio(frame)
        async for event in duplex.events():
            if event.kind == "playback_interrupt":
                assert event.payload["replyId"] == old_reply
                break
        # Sending all frames is not proof of playback. Duplex retains only
        # interaction facts; the Bot owns assistant-history projection.
        robot_items = [item for item in duplex.history.items if item.role == "robot"]
        assert not robot_items
        assert any(e["type"] == "generation_cancelled" and e["replyId"] == old_reply for e in duplex._bot_facts)
        media_task.cancel()
        await duplex.close("test_done")

    asyncio.run(run())


def test_goodbye_bot_action_waits_for_playback_before_closing():
    from backend.english_chat import EnglishChatBot
    async def run():
        duplex = make_stub_duplex(agent=EnglishChatBot())
        await duplex.start()
        await duplex.submit_text_for_development("再见")
        packets = await asyncio.wait_for(collect_one_reply(duplex), 2)
        assert packets
        assert duplex.session.state == SessionState.HANG_UP

    asyncio.run(run())


def test_five_turn_conversation_keeps_unique_turns_and_complete_history():
    async def run():
        duplex = make_stub_duplex()
        await duplex.start()
        for index in range(5):
            await duplex.submit_text_for_development(f"问题{index}")
            await asyncio.wait_for(collect_one_reply(duplex), 2)
        text_items = [item for item in duplex.history.items if item.message_type == "text"]
        assert len(text_items) == 10
        user_turns = [item.turn_id for item in text_items if item.role == "user"]
        assert len(set(user_turns)) == 5
        for turn_id in user_turns:
            assert any(item.role == "robot" and item.turn_id == turn_id for item in text_items)
        await duplex.close("test_done")

    asyncio.run(run())


def test_interrupt_while_tts_is_synthesizing_cancels_before_first_audio():
    class SlowTts:
        def __init__(self):
            self.started = asyncio.Event()

        async def stream_pcm(self, text, **kwargs):
            self.started.set()
            await asyncio.sleep(10)
            yield b"\x00" * 3200

    class SpeechStartAsr:
        async def feed(self, pcm, speech_time):
            return AsrVadResult(speech_time, 1, 0, speech_time, "")

        async def reset(self):
            return None

    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "0"
        tts = SlowTts()
        duplex = make_stub_duplex(tts=tts, asr=SpeechStartAsr())
        await duplex.start()
        await duplex.pick_up()
        await asyncio.wait_for(tts.started.wait(), 0.5)
        await duplex.push_audio(AudioFrame(0, session_hash("sess_test"), 0, 0, b"\x01\x00" * 1600))
        async for event in duplex.events():
            if event.kind == "playback_interrupt":
                break
        assert duplex._media_outputs.empty()
        assert duplex._reply_task.cancelled() or duplex._reply_task.cancelling()
        await duplex.close("test_done")

    asyncio.run(run())


class ScriptedAsr:
    """ASR stub returning a scripted VAD status (and optional text) per feed."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._calls = 0

    async def feed(self, pcm, speech_time):
        index = min(self._calls, len(self._statuses) - 1)
        self._calls += 1
        item = self._statuses[index]
        if isinstance(item, tuple):
            status, text = item
        else:
            status, text = item, ""
        start = speech_time - 100 if status in (1, 2) else None
        end = speech_time if status == 3 else None
        return AsrVadResult(speech_time, status, start, end, text)

    async def reset(self):
        return None


async def _wait_robot_streaming(duplex):
    async def consume_media():
        async for event in duplex.media_events():
            await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])

    media_task = asyncio.create_task(consume_media())
    try:
        async for event in duplex.events():
            if event.kind == "reply_complete":
                break
    finally:
        media_task.cancel()
    # reply_complete only means every packet was sent; the robot keeps
    # "speaking" until the browser acks playback, so it is still barge-able.
    assert duplex.session.robot.status in (1, 2)


async def _push_speech_frames(duplex, count, start_seq=0):
    for seq in range(start_seq, start_seq + count):
        frame = AudioFrame(0, session_hash("sess_test"), seq, 0, b"\x01\x00" * 1600)
        await duplex.push_audio(frame)


def test_short_noise_burst_does_not_interrupt_robot():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "300"
        duplex = make_stub_duplex(asr=ScriptedAsr([1, 3]))
        await duplex.start()
        await duplex.pick_up()
        await _wait_robot_streaming(duplex)
        kinds, timeline = [], []

        async def collect_events():
            async for event in duplex.events():
                kinds.append(event.kind)

        async def collect_timeline():
            async for item in duplex.timeline_events():
                timeline.append(item)

        tasks = [asyncio.create_task(collect_events()),
                 asyncio.create_task(collect_timeline())]
        await _push_speech_frames(duplex, 2)
        await asyncio.sleep(0.3)
        for task in tasks:
            task.cancel()
        assert "playback_interrupt" not in kinds
        cancelled = [t for t in timeline if t.get("eventType") == "barge_in_cancelled"]
        assert cancelled and cancelled[-1]["attributes"]["reason"] == "too_short"
        await duplex.close("test_done")

    asyncio.run(run())


def test_sustained_speech_confirms_barge_in():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "300"
        os.environ["BARGE_IN_MIN_REPLY_AGE_MS"] = "0"
        duplex = make_stub_duplex(asr=ScriptedAsr([1, 2, 2, 2]))
        await duplex.start()
        await duplex.pick_up()
        await _wait_robot_streaming(duplex)
        await _push_speech_frames(duplex, 4)

        async def wait_interrupt():
            async for event in duplex.events():
                if event.kind == "playback_interrupt":
                    return event

        event = await asyncio.wait_for(wait_interrupt(), 1.0)
        assert event.payload["reason"] == "barge_in"
        await duplex.close("test_done")

    asyncio.run(run())


def test_barge_in_within_reply_grace_period_is_cancelled():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "300"
        os.environ["BARGE_IN_MIN_REPLY_AGE_MS"] = "999999"
        duplex = make_stub_duplex(asr=ScriptedAsr([1, 2, 2, 2]))
        await duplex.start()
        await duplex.pick_up()
        await _wait_robot_streaming(duplex)
        kinds, timeline = [], []

        async def collect_events():
            async for event in duplex.events():
                kinds.append(event.kind)

        async def collect_timeline():
            async for item in duplex.timeline_events():
                timeline.append(item)

        tasks = [asyncio.create_task(collect_events()),
                 asyncio.create_task(collect_timeline())]
        await _push_speech_frames(duplex, 4)
        await asyncio.sleep(0.3)
        for task in tasks:
            task.cancel()
        assert "playback_interrupt" not in kinds
        cancelled = [t for t in timeline if t.get("eventType") == "barge_in_cancelled"]
        assert cancelled and cancelled[-1]["attributes"]["reason"] == "reply_too_young"
        await duplex.close("test_done")

    asyncio.run(run())


def test_barge_in_cooldown_blocks_second_interruption():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "300"
        os.environ["BARGE_IN_MIN_REPLY_AGE_MS"] = "0"
        os.environ["BARGE_IN_COOLDOWN_MS"] = "999999"
        duplex = make_stub_duplex(asr=ScriptedAsr([1, 2, 2, 2, 1, 2, 2, 2]))
        await duplex.start()
        await duplex.pick_up()
        await _wait_robot_streaming(duplex)
        await _push_speech_frames(duplex, 4)

        async def wait_interrupt():
            async for event in duplex.events():
                if event.kind == "playback_interrupt":
                    return event

        await asyncio.wait_for(wait_interrupt(), 1.0)
        # Second reply starts; a new sustained burst must be rejected by cooldown.
        await duplex.submit_text_for_development("继续")
        await _wait_robot_streaming(duplex)
        kinds, timeline = [], []

        async def collect_events():
            async for event in duplex.events():
                kinds.append(event.kind)

        async def collect_timeline():
            async for item in duplex.timeline_events():
                timeline.append(item)

        tasks = [asyncio.create_task(collect_events()),
                 asyncio.create_task(collect_timeline())]
        await _push_speech_frames(duplex, 4, start_seq=4)
        await asyncio.sleep(0.3)
        for task in tasks:
            task.cancel()
        assert kinds.count("playback_interrupt") == 0
        cancelled = [t for t in timeline if t.get("eventType") == "barge_in_cancelled"]
        assert cancelled and cancelled[-1]["attributes"]["reason"] == "cooldown"
        await duplex.close("test_done")

    asyncio.run(run())


def test_barge_in_rejects_sustained_noise_without_text():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "300"
        os.environ["BARGE_IN_TEXT_GRACE_MS"] = "300"
        os.environ["BARGE_IN_MIN_REPLY_AGE_MS"] = "0"
        asr = ScriptedAsr([(1, ""), (2, "你好"), (3, "你好"),
                           (1, ""), (2, ""), (2, ""), (2, ""), (2, ""), (2, ""),
                           (2, ""), (2, ""), (2, ""), (3, "")])
        duplex = make_stub_duplex(asr=asr)
        await duplex.start()
        # Utterance 1 (robot still silent) proves the ASR can produce text.
        await _push_speech_frames(duplex, 3)
        await _wait_robot_streaming(duplex)
        kinds, timeline = [], []

        async def collect_events():
            async for event in duplex.events():
                kinds.append(event.kind)

        async def collect_timeline():
            async for item in duplex.timeline_events():
                timeline.append(item)

        tasks = [asyncio.create_task(collect_events()),
                 asyncio.create_task(collect_timeline())]
        # Utterance 2: sustained energy for 900 ms but the recognizer never
        # produces a single character — noise, not speech.
        await _push_speech_frames(duplex, 10, start_seq=3)
        await asyncio.sleep(0.4)
        for task in tasks:
            task.cancel()
        assert "playback_interrupt" not in kinds
        cancelled = [t for t in timeline if t.get("eventType") == "barge_in_cancelled"]
        assert cancelled and cancelled[-1]["attributes"]["reason"] == "no_text"
        await duplex.close("test_done")

    asyncio.run(run())


def test_barge_in_confirms_once_partial_text_arrives():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "300"
        os.environ["BARGE_IN_TEXT_GRACE_MS"] = "300"
        os.environ["BARGE_IN_MIN_REPLY_AGE_MS"] = "0"
        asr = ScriptedAsr([(1, ""), (2, "你好"), (3, "你好"),
                           (1, ""), (2, ""), (2, ""), (2, ""), (2, "在的"), (2, "")])
        duplex = make_stub_duplex(asr=asr)
        await duplex.start()
        await _push_speech_frames(duplex, 3)
        await _wait_robot_streaming(duplex)
        await _push_speech_frames(duplex, 6, start_seq=3)

        async def wait_interrupt():
            async for event in duplex.events():
                if event.kind == "playback_interrupt":
                    return event

        event = await asyncio.wait_for(wait_interrupt(), 1.0)
        assert event.payload["reason"] == "barge_in"
        await duplex.close("test_done")

    asyncio.run(run())


def test_speculative_prefetch_is_triggered_by_asr_partials():
    class PrefetchRecordingAgent:
        def __init__(self):
            self.partials = []
            self.requests = 0

        def notify_partial(self, text, messages):
            self.partials.append((text, list(messages)))

        async def stream_reply(self, messages, signal=None):
            self.requests += 1
            yield "speculative-reply"

    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "0"
        agent = PrefetchRecordingAgent()
        duplex = make_stub_duplex(
            agent=agent,
            asr=ScriptedAsr([(1, ""), (2, "你好呀"), (3, "你好呀")]),
        )
        offered = []
        duplex.prefetch.offer = lambda query: (offered.append(query), True)[-1]
        await duplex.start()
        await duplex.pick_up()
        await _wait_robot_streaming(duplex)

        # The media queue is bounded: a consumer must keep draining packets or
        # the replacement reply blocks before reply_complete.
        async def consume_media():
            async for event in duplex.media_events():
                await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])

        media_task = asyncio.create_task(consume_media())
        try:
            await _push_speech_frames(duplex, 3)
            async for event in duplex.events():
                if event.kind == "reply_complete":
                    await duplex.mark_playback_completed(event.payload["replyId"])
                    break
        finally:
            media_task.cancel()
        assert any(query.text == "你好呀" for query in offered)
        # ASR scheduling belongs to the duplex, not the legacy model hook.
        assert agent.partials == []
        await duplex.close("test_done")

    asyncio.run(run())


def test_duplex_no_longer_estimates_semantic_history_from_sent_audio():
    async def run():
        duplex = make_stub_duplex()
        assert not hasattr(duplex, "_estimate_played_text")
        assert not hasattr(duplex, "_format_interrupted_reply")
        await duplex.start()
        await duplex.submit_text_for_development("Hello")
        await collect_one_reply(duplex)
        before = list(duplex._bot_facts)
        await duplex.mark_packet_sent("stale_reply", 99999)
        assert list(duplex._bot_facts) == before
        await duplex.close("test_done")

    asyncio.run(run())


def test_duplex_forwards_raw_playback_progress_without_text_interpretation():
    async def run():
        duplex = make_stub_duplex()
        await duplex.start()
        await duplex.submit_text_for_development("Hello")
        await collect_one_reply(duplex)
        reply = duplex.history.items[-1].reply_id
        duplex.record_interaction("playback_progress", reply, playedMs=475)
        assert duplex._bot_facts[-1]["playedMs"] == 475
        assert "text" not in duplex._bot_facts[-1]
        await duplex.close("test_done")

    asyncio.run(run())


def test_interrupted_reply_is_a_fact_not_a_marked_up_assistant_message():
    async def run():
        os.environ["BARGE_IN_CONFIRM_MS"] = "0"
        duplex = make_stub_duplex(asr=ScriptedAsr([(1, ""), (2, "你好呀"), (3, "你好呀")]))
        await duplex.start()
        await duplex.pick_up()
        await _wait_robot_streaming(duplex)

        async def consume_media():
            async for event in duplex.media_events():
                await duplex.mark_packet_sent(event.payload["replyId"], event.payload["sequence"])

        media_task = asyncio.create_task(consume_media())
        try:
            await _push_speech_frames(duplex, 3)
            async for event in duplex.events():
                if event.kind == "reply_complete":
                    await duplex.mark_playback_completed(event.payload["replyId"])
                    break
        finally:
            media_task.cancel()
        robot_items = [item for item in duplex.history.items if item.role == "robot"]
        assert not any("打断" in item.message for item in robot_items)
        robot_messages = [m["content"] for m in duplex.history.to_agent_messages() if m["role"] == "machine"]
        assert not any("打断" in message for message in robot_messages)
        assert any(e["type"] == "generation_cancelled" for e in duplex._bot_facts)
        await duplex.close("test_done")

    asyncio.run(run())
