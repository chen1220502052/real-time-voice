import asyncio
import json
import struct
import threading

import numpy as np
import pytest

from backend.atomic import (
    AliyunNlsAsrVadAdapter,
    AliyunNlsTokenProvider,
    CatAnimationDriver,
    DashScopeAsrVadAdapter,
    DemoAgent,
    DemoTts,
    FunAsrVadAdapter,
    PiperTts,
    SpeculativeAgent,
    VadAsrStub,
    resample_pcm16_mono,
)


def test_vad_returns_a_result_for_every_frame():
    async def run():
        vad = VadAsrStub(threshold=0.001)
        silence = b"\x00" * 3200
        speech = struct.pack("<1600h", *([10000] * 1600))
        assert (await vad.feed(silence, 100)).status == 0
        assert (await vad.feed(speech, 200)).status == 1
        assert (await vad.feed(speech, 300)).status == 2
        assert (await vad.feed(silence, 400)).status == 3
    asyncio.run(run())


def test_local_inference_pools_have_bounded_cpu_budgets(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    seen = {}
    def model(**kwargs):
        seen['vad'] = kwargs
        return object()
    monkeypatch.setitem(sys.modules, 'funasr', SimpleNamespace(AutoModel=model))
    monkeypatch.setattr(DashScopeAsrVadAdapter, '_vad_pool', {})
    monkeypatch.setattr(FunAsrVadAdapter, '_resolve_local_model', staticmethod(lambda name: 'vad-fixture'))
    monkeypatch.setenv('VAD_CPU_THREADS', '1')
    adapter = AliyunNlsAsrVadAdapter(transcriber_factory=lambda **kwargs: None)
    adapter._ensure_vad()
    assert seen['vad']['ncpu'] == 1
    class Options:
        def __init__(self):
            self.entries = {}
        def add_session_config_entry(self, key, value):
            self.entries[key] = value
    def session(path, **kwargs):
        seen['tts'] = kwargs
        return object()
    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(SessionOptions=Options, InferenceSession=session))
    monkeypatch.setitem(sys.modules, 'piper.config', SimpleNamespace(PiperConfig=SimpleNamespace(from_dict=lambda value: value)))
    monkeypatch.setenv('TTS_CPU_THREADS', '2')
    path = str(tmp_path / 'voice.onnx')
    (tmp_path / 'voice.onnx.json').write_text('{}')
    tts = PiperTts(model_path=path)
    tts._load_voice(lambda **kwargs: object(), path)
    options = seen['tts']['sess_options']
    assert options.intra_op_num_threads == 2 and options.inter_op_num_threads == 1
    assert options.entries['session.intra_op.allow_spinning'] == '0'
    assert seen['tts']['providers'] == ['CPUExecutionProvider']


def test_nls_slow_start_does_not_block_vad_and_stop_is_once(monkeypatch):
    monkeypatch.setenv("ALIYUN_NLS_APP_KEY", "test-app-key")
    monkeypatch.setenv("ALIYUN_NLS_TOKEN", "test-token")
    gate, starting = threading.Event(), threading.Event()
    stops, frames = [], []
    class Vad:
        def __init__(self):
            self.calls = 0
        def generate(self, **kwargs):
            self.calls += 1
            return [{"value": [[0, -1]] if self.calls == 1 else []}]
    class Transcriber:
        def __init__(self, **kwargs):
            self.callbacks = kwargs
        def start(self, **kwargs):
            starting.set()
            assert gate.wait(2)
            self.callbacks['on_start']({'header': {'name': 'TranscriptionStarted'}})
        def send_audio(self, pcm):
            frames.append(pcm)
        def stop(self):
            stops.append(1)
            self.callbacks['on_completed']({'payload': {'result': 'Hello.'}})
    async def run():
        adapter = AliyunNlsAsrVadAdapter(transcriber_factory=Transcriber)
        adapter._numpy, adapter._vad_model = np, Vad()
        try:
            result = await asyncio.wait_for(adapter.feed(b'\0' * 3200, 100), .3)
            assert result.status == 1
            assert await asyncio.to_thread(starting.wait, 1)
            # The paid connection is still blocked, but local VAD advances.
            result = await asyncio.wait_for(adapter.feed(b'\0' * 3200, 200), .3)
            assert result.status == 2
            gate.set()
            assert await asyncio.to_thread(adapter._stop_recognition) == 'Hello.'
            await asyncio.to_thread(adapter._stop_recognition)
            await adapter.reset()
            assert len(stops) == 1 and len(frames) == 10
        finally:
            gate.set()
            await adapter.reset()
    asyncio.run(run())


def test_nls_adapter_keeps_local_vad_contract_and_returns_cloud_text(monkeypatch):
    monkeypatch.setenv("ALIYUN_NLS_APP_KEY", "test-app-key")
    monkeypatch.setenv("ALIYUN_NLS_TOKEN", "test-token")

    class FakeVad:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            values = ([[0, -1]], [], [[-1, 250]])
            return [{"value": values[self.calls - 1]}]

    transcribers = []

    class FakeTranscriber:
        def __init__(self, **kwargs):
            self.callbacks = kwargs
            self.frames = []
            transcribers.append(self)

        def start(self, **kwargs):
            self.callbacks['on_start']({'header': {'name': 'TranscriptionStarted'}})
            return True

        def send_audio(self, pcm):
            self.frames.append(pcm)
            self.callbacks["on_result_changed"](
                json.dumps({"payload": {"result": "你"}})
            )
            return True

        def stop(self):
            self.callbacks["on_sentence_end"](
                json.dumps({"payload": {"result": "你好"}})
            )
            return True

    async def run():
        adapter = AliyunNlsAsrVadAdapter(transcriber_factory=FakeTranscriber)
        adapter._numpy = np
        adapter._vad_model = FakeVad()
        pcm = b"\x01\x00" * 1600
        assert (await adapter.feed(pcm, 100)).status == 1
        assert (await adapter.feed(pcm, 200)).status == 2
        final = await adapter.feed(pcm, 300)
        assert final.status == 3
        assert final.start_time == 0
        assert final.end_time == 250
        assert final.text == "你好"
        assert len(transcribers) == 1
        # Three application frames (100 ms each) are split into the NLS SDK's
        # recommended 20 ms writes without changing the public protocol.
        assert len(transcribers[0].frames) == 15
        assert all(len(frame) == 640 for frame in transcribers[0].frames)

    asyncio.run(run())


def test_nls_adapter_forces_end_after_bounded_silence_when_vad_end_is_missing(monkeypatch):
    monkeypatch.setenv("ALIYUN_NLS_APP_KEY", "test-app-key")
    monkeypatch.setenv("ALIYUN_NLS_TOKEN", "test-token")
    monkeypatch.setenv("VAD_FALLBACK_END_SILENCE_MS", "300")

    class StartOnlyVad:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            return [{"value": [[0, -1]] if self.calls == 1 else []}]

    class FakeTranscriber:
        def __init__(self, **kwargs):
            self.callbacks = kwargs

        def start(self, **kwargs):
            self.callbacks['on_start']({'header': {'name': 'TranscriptionStarted'}})
            return True

        def send_audio(self, pcm):
            return True

        def stop(self):
            self.callbacks["on_sentence_end"](
                json.dumps({"payload": {"result": "静音结束"}})
            )
            return True

    async def run():
        adapter = AliyunNlsAsrVadAdapter(transcriber_factory=FakeTranscriber)
        adapter._numpy = np
        adapter._vad_model = StartOnlyVad()
        speech = struct.pack("<1600h", *([8000] * 1600))
        silence = b"\x00" * 3200
        assert (await adapter.feed(speech, 100)).status == 1
        assert (await adapter.feed(silence, 200)).status == 2
        assert (await adapter.feed(silence, 300)).status == 2
        final = await adapter.feed(silence, 400)
        assert final.status == 3
        assert final.start_time == 0
        assert final.end_time == 100
        assert final.text == "静音结束"

    asyncio.run(run())


def test_nls_token_provider_caches_and_refreshes_before_expiry():
    now = [1_000.0]
    calls = []

    def fetch():
        calls.append(now[0])
        return f"token-{len(calls)}", now[0] + 1_000

    provider = AliyunNlsTokenProvider(
        access_key_id="refresh-test-ak",
        access_key_secret="test-secret",
        refresh_margin_seconds=300,
        fetcher=fetch,
        clock=lambda: now[0],
    )
    assert provider.get_token() == "token-1"
    now[0] = 1_600
    assert provider.get_token() == "token-1"
    now[0] = 1_750
    assert provider.get_token() == "token-2"
    assert len(calls) == 2


def test_nls_prefers_refreshable_access_keys_over_stale_manual_token(monkeypatch):
    monkeypatch.setenv("ALIYUN_NLS_APP_KEY", "test-app-key")
    monkeypatch.setenv("ALIYUN_NLS_TOKEN", "stale-manual-token")
    monkeypatch.setenv("ALIYUN_AK_ID", "test-ak")
    monkeypatch.setenv("ALIYUN_AK_SECRET", "test-secret")
    monkeypatch.setattr(AliyunNlsTokenProvider, "get_token", lambda self: "fresh-token")
    received = {}

    class FakeVad:
        def generate(self, **kwargs):
            return [{"value": []}]

    class FakeTranscriber:
        def __init__(self, **kwargs):
            received["token"] = kwargs["token"]
            self.callbacks = kwargs

        def start(self, **kwargs):
            self.callbacks['on_start']({'header': {'name': 'TranscriptionStarted'}})
            return True

        def stop(self):
            self.callbacks['on_completed']({})

    adapter = AliyunNlsAsrVadAdapter(transcriber_factory=FakeTranscriber)
    adapter._numpy = np
    adapter._vad_model = FakeVad()
    adapter._ensure_dependencies()
    adapter._start_recognition()
    adapter._flush_pending_audio()
    assert received["token"] == "fresh-token"
    asyncio.run(adapter.reset())


def test_cat_driver_uses_hysteresis():
    driver = CatAnimationDriver(open_threshold=0.01, close_threshold=0.005)
    voice = struct.pack("<1600h", *([10000] * 1600))
    silence = b"\x00" * 3200
    assert driver.drive(voice, 0, 100).avatar_state == "mouth_open"
    assert driver.drive(silence, 1, 200).avatar_state == "mouth_open"
    assert driver.drive(silence, 2, 300).avatar_state == "mouth_closed"


def test_pcm_resampler_replaces_removed_audioop_and_preserves_duration():
    source = struct.pack("<2205h", *([1234] * 2205))
    output = resample_pcm16_mono(source, 22_050, 16_000)
    samples = struct.unpack("<1600h", output)
    assert len(samples) == 1600
    assert set(samples) == {1234}


def test_pcm_resampler_rejects_malformed_pcm16():
    with pytest.raises(ValueError, match="even"):
        resample_pcm16_mono(b"\x00", 22_050, 16_000)


def test_funasr_adapter_returns_final_text_on_vad_end():
    class FakeVad:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            values = ([[0, -1]], [], [[-1, 250]])
            return [{"value": values[self.calls - 1]}]

    class FakeAsr:
        def generate(self, **kwargs):
            return [{"text": "你好"}] if kwargs["is_final"] else []

    async def run():
        adapter = FunAsrVadAdapter()
        adapter._numpy = np
        adapter._vad_model = FakeVad()
        adapter._asr_model = FakeAsr()
        pcm = b"\x01\x00" * 1600
        assert (await adapter.feed(pcm, 100)).status == 1
        assert (await adapter.feed(pcm, 200)).status == 2
        final = await adapter.feed(pcm, 300)
        assert final.status == 3
        assert final.start_time == 0
        assert final.end_time == 250
        assert final.text == "你好"

    asyncio.run(run())


def test_dashscope_adapter_keeps_local_vad_contract_and_returns_cloud_text():
    class FakeVad:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            values = ([[0, -1]], [], [[-1, 250]])
            return [{"value": values[self.calls - 1]}]

    class Result:
        def __init__(self, text, sentence_end=False):
            self.sentence = {"text": text, "sentence_end": sentence_end}

        def get_sentence(self):
            return self.sentence

    recognitions = []

    class FakeRecognition:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.frames = []
            recognitions.append(self)

        def start(self):
            return None

        def send_audio_frame(self, pcm):
            self.frames.append(pcm)
            self.callback.on_event(Result("你"))

        def stop(self):
            self.callback.on_event(Result("你好", sentence_end=True))

    async def run():
        adapter = DashScopeAsrVadAdapter(
            recognition_factory=FakeRecognition,
            callback_base=object,
        )
        adapter._numpy = np
        adapter._vad_model = FakeVad()
        pcm = b"\x01\x00" * 1600
        first = await adapter.feed(pcm, 100)
        assert first.status == 1
        assert (await adapter.feed(pcm, 200)).status == 2
        final = await adapter.feed(pcm, 300)
        assert final.status == 3
        assert final.start_time == 0
        assert final.end_time == 250
        assert final.text == "你好"
        assert len(recognitions) == 1
        assert len(recognitions[0].frames) == 3

    asyncio.run(run())


def test_demo_capabilities_support_deterministic_error_injection(monkeypatch):
    async def run():
        monkeypatch.setenv("DEMO_ASR_ERROR", "1")
        with pytest.raises(RuntimeError, match="Stub ASR error"):
            await VadAsrStub().feed(b"\x00" * 3200, 100)

        monkeypatch.setenv("DEMO_LLM_ERROR", "1")
        with pytest.raises(RuntimeError, match="Stub LLM error"):
            await anext(DemoAgent().stream_reply([]))

        monkeypatch.setenv("DEMO_TTS_ERROR", "1")
        with pytest.raises(RuntimeError, match="Stub TTS error"):
            await anext(DemoTts().stream_pcm("你好"))

    asyncio.run(run())


def test_piper_cancelled_synthesis_never_overlaps_replacement(tmp_path):
    model_path = str(tmp_path / "exclusive-test.onnx")
    tts = PiperTts(model_path=model_path)
    tts._voices["primary"] = object()
    started = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    maximum = 0

    def synthesize(text, speed, volume, lang="primary"):
        nonlocal active, maximum
        with counter_lock:
            active += 1
            maximum = max(maximum, active)
        started.set()
        release.wait(timeout=2)
        with counter_lock:
            active -= 1
        yield b""

    tts._synthesize_chunks = synthesize

    async def consume(text):
        return [chunk async for chunk in tts.stream_pcm(text)]

    async def run():
        first = asyncio.create_task(consume("旧回复"))
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        second = asyncio.create_task(consume("新回复"))
        await asyncio.sleep(0.05)
        assert maximum == 1
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        assert maximum == 1
        assert active == 0

    try:
        asyncio.run(run())
    finally:
        executor = PiperTts._executor_pool.pop(str(tmp_path / "exclusive-test.onnx"), None)
        if executor:
            executor.shutdown(wait=True)


class _RecordingAgent:
    def __init__(self):
        self.requests = []

    async def stream_reply(self, messages, signal=None):
        self.requests.append(messages)
        yield "cached-reply"


def _await_first_slot(agent):
    assert agent._slots, "expected a speculative slot"
    return agent._slots[0]["task"]


def test_speculative_agent_hit_replays_cache_without_fresh_request():
    async def run():
        inner = _RecordingAgent()
        agent = SpeculativeAgent(inner)
        base = [{"role": "user", "content": "hello"}]
        agent.notify_partial("How are you today", base)
        await _await_first_slot(agent)
        out = [delta async for delta in agent.stream_reply(
            base + [{"role": "user", "content": "How are you today?"}])]
        assert "".join(out) == "cached-reply"
        assert len(inner.requests) == 1  # only the speculative request ran
        assert agent.hits == 1 and agent.misses == 0

    asyncio.run(run())


def test_speculative_agent_miss_falls_back_to_real_request():
    async def run():
        inner = _RecordingAgent()
        agent = SpeculativeAgent(inner)
        base = [{"role": "user", "content": "hello"}]
        agent.notify_partial("What can you help", base)
        await _await_first_slot(agent)
        out = [delta async for delta in agent.stream_reply(
            base + [{"role": "user", "content": "What can you help me with and tell me a joke?"}])]
        assert "".join(out) == "cached-reply"
        assert len(inner.requests) == 2  # speculative + real fallback
        assert agent.misses == 1

    asyncio.run(run())


def test_speculative_agent_prefix_hit_when_partial_nearly_complete():
    async def run():
        inner = _RecordingAgent()
        agent = SpeculativeAgent(inner)
        base = [{"role": "user", "content": "hello"}]
        agent.notify_partial("Tell me a fun fact about cats", base)
        await _await_first_slot(agent)
        out = [delta async for delta in agent.stream_reply(
            base + [{"role": "user", "content": "Tell me a fun fact about cats now"}])]
        assert "".join(out) == "cached-reply"
        assert len(inner.requests) == 1
        assert agent.hits == 1

    asyncio.run(run())


def test_speculative_agent_runs_second_slot_while_first_in_flight():
    class SlowAgent:
        def __init__(self):
            self.requests = 0

        async def stream_reply(self, messages, signal=None):
            self.requests += 1
            yield "start-"
            await asyncio.sleep(0.4)
            yield "end"

    async def run():
        inner = SlowAgent()
        agent = SpeculativeAgent(inner)
        base = [{"role": "user", "content": "hello"}]
        agent.notify_partial("first words", base)
        await asyncio.sleep(0.05)
        # A later partial fires a CONCURRENT slot instead of restarting the
        # first request (restarting would throw away its TTFT head start).
        agent.notify_partial("first words keep growing", base)
        await asyncio.sleep(0.05)
        assert inner.requests == 2
        for slot in agent._slots:
            await slot["task"]
        assert set(agent._completed) == {"firstwords", "firstwordskeepgrowing"}
        out = [delta async for delta in agent.stream_reply(
            base + [{"role": "user", "content": "first words keep growing"}])]
        assert "".join(out) == "start-end"
        assert agent.hits == 1
        assert not [s for s in agent._slots if not s["task"].done()]

    asyncio.run(run())


def test_speculative_agent_adopts_live_stream_with_buffered_deltas():
    class SlowAgent:
        def __init__(self):
            self.requests = 0

        async def stream_reply(self, messages, signal=None):
            self.requests += 1
            yield "first-"
            await asyncio.sleep(0.3)
            yield "second-"
            await asyncio.sleep(0.3)
            yield "third"

    async def run():
        inner = SlowAgent()
        agent = SpeculativeAgent(inner)
        base = [{"role": "user", "content": "hello"}]
        agent.notify_partial("How are you today", base)
        await asyncio.sleep(0.05)
        out = []
        async for delta in agent.stream_reply(
                base + [{"role": "user", "content": "How are you today?"}]):
            out.append(delta)
        assert "".join(out) == "first-second-third"
        assert inner.requests == 1  # no fresh request needed
        assert agent.hits == 1

    asyncio.run(run())


def test_speculative_agent_failed_prefetch_is_never_adopted():
    class FailOnceAgent:
        def __init__(self):
            self.requests = 0

        async def stream_reply(self, messages, signal=None):
            self.requests += 1
            if self.requests == 1:
                yield "stale-"
                raise RuntimeError("speculative stream died")
            yield "real-reply"

    async def run():
        inner = FailOnceAgent()
        agent = SpeculativeAgent(inner)
        base = [{"role": "user", "content": "hello"}]
        agent.notify_partial("How are you today", base)
        await _await_first_slot(agent)
        out = [delta async for delta in agent.stream_reply(
            base + [{"role": "user", "content": "How are you today?"}])]
        assert "".join(out) == "real-reply"
        assert inner.requests == 2
        assert agent.misses == 1 and agent.hits == 0

    asyncio.run(run())
