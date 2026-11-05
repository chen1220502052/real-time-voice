import asyncio
import random
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.atomic import DemoAgent, DemoTts
from backend.domain import Session, SessionState
from backend.duplex import DuplexDialogue
from backend.interruption import InterruptionController
from backend.local_turn_taking import unfinished_english, EchoEvidence
from backend.protocol import AudioFrame, session_hash
from backend.providers.common import AsrVadResult
from backend.providers.speech_local import ParakeetAsr


def overlap():
    events = []
    d = SimpleNamespace(asr=SimpleNamespace(supports_barge_in_partials=True),
        session=SimpleNamespace(robot=SimpleNamespace(status=2,
            text='Teachers help kids learn. What job would you like to do?')),
        _active_reply_id='reply', _active_user_turn_id='turn',
        _emit_timeline=lambda *args, **kw: events.append((args, kw)))
    async def stop():
        d._active_reply_id = None
        d.session.robot.status = 0
    d._interrupt = AsyncMock(side_effect=stop)
    return d, InterruptionController(d), events


def partial(text='I have another', **changes):
    return AsrVadResult(600, 2, 100, 600, text, activity={
        'text': text, 'confidence': .98, 'voicedMs': 400,
        'quietMs': 0, 'textEndMediaMs': 500, **changes})


def test_local_partial_stops_once_before_final_and_keeps_listening():
    async def run():
        d, gate, events = overlap()
        await gate.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        assert await gate.maybe_barge_in(partial()) is True
        await gate.maybe_barge_in(partial('I have another question'))
        assert await gate.maybe_barge_in(AsrVadResult(1800, 3, 100, 1500,
            'I have another question.')) is True
        d._interrupt.assert_awaited_once()
        assert events[-1][1]['attributes']['evidence'] == 'asr_partial'
    asyncio.run(run())


@pytest.mark.parametrize('text,changes', [
    ('I have', {'confidence': .91}), ('I have', {'voicedMs': 200}),
    ('I have', {'quietMs': 500}), ('I have', {'textEndMediaMs': 50}),
    ('I have', {'textEndMediaMs': None}), ('I', {}), ('um hmm', {}),
    ('Teachers help', {}), ('Teachers help kids learn', {}),
])
def test_local_noise_stale_text_and_echo_do_not_stop_playback(text, changes):
    async def run():
        d, gate, _ = overlap()
        await gate.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        await gate.maybe_barge_in(partial(text, **changes))
        d._interrupt.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('text', ['Stop', 'Wait', 'Enough', 'Wait a minute'])
def test_explicit_stop_intent_does_not_need_a_complete_sentence(text):
    async def run():
        d, gate, _ = overlap()
        await gate.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        await gate.maybe_barge_in(partial(text))
        d._interrupt.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('text', ['Yes.', 'No.', 'I like cats.'])
def test_short_real_answers_keep_the_final_fallback(text):
    async def run():
        d, gate, _ = overlap()
        await gate.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        assert await gate.maybe_barge_in(AsrVadResult(900, 3, 100, 600, text))
        d._interrupt.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize('text', ['Um, hmm.'])
def test_rejected_overlap_cannot_bypass_gate_through_final_acceptance(text):
    class Asr:
        supports_barge_in_partials = True
        def __init__(self): self.count = 0
        async def feed(self, pcm, media):
            self.count += 1
            return (AsrVadResult(media, 1, 0, media) if self.count == 1
                    else AsrVadResult(media, 3, 0, 200, text))
        async def reset(self): pass
    async def run():
        d = DuplexDialogue(Session('sess_test', SessionState.ON_SERVICE),
            asr=Asr(), agent=DemoAgent(), tts=DemoTts())
        d._active_reply_id = 'reply'
        d.session.robot.status = 2
        d.session.robot.text = 'Teachers help kids learn.'
        d._accept_final_text = AsyncMock()
        for seq in range(2):
            await d._decode_audio(AudioFrame(0, session_hash('sess_test'), seq, 0, b'\0'*3200))
        d._accept_final_text.assert_not_awaited()
        assert d._active_reply_id == 'reply'
        await d.close('test_done')
    asyncio.run(run())


def test_human_repetition_is_not_rejected_just_because_words_match():
    async def run():
        d, gate, _ = overlap()
        await gate.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        assert await gate.maybe_barge_in(AsrVadResult(1800, 3, 100, 1500,
            'Teachers help kids learn.')) is True
        d._interrupt.assert_awaited_once()
    asyncio.run(run())


def test_echo_needs_correlated_audio_and_matching_words():
    async def run():
        d, gate, _ = overlap()
        rng = random.Random(71)
        samples = [rng.randrange(-3000,3001) for _ in range(6400)]
        pcm = struct.pack('<6400h', *samples)
        gate.local.echo.output(pcm)
        for i in (0,3200): gate.local.echo.input(pcm[i:i+3200])
        await gate.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        assert await gate.maybe_barge_in(AsrVadResult(1800, 3, 100, 1500,
            'Teachers help kids learn.')) is False
        d._interrupt.assert_not_awaited()
        # Explicit stop remains valid even with correlated audio.
        assert await gate.maybe_barge_in(partial('Stop')) is True
        d._interrupt.assert_awaited_once()
    asyncio.run(run())


def test_echo_reference_and_inputs_are_bounded_and_independent_speech_does_not_match():
    rng = random.Random(42)
    def sound(): return struct.pack('<1600h', *[rng.randrange(-3000,3001) for _ in range(1600)])
    echo = EchoEvidence()
    for _ in range(150): echo.output(sound())
    for _ in range(5): echo.input(sound())
    assert len(echo.reference) == 5000 and len(echo.inputs) == 3
    assert not echo.matches()


def test_resumed_voice_holds_first_audio_without_blocking_asr_then_noise_releases_it():
    async def run():
        d = DuplexDialogue(Session('sess_test', SessionState.ON_SERVICE),
            asr=ParakeetAsr(vad=object(), inference=AsyncMock()),
            agent=DemoAgent(), tts=DemoTts())
        d._active_reply_id = 'reply'
        d._active_turn_id = 'previous'
        d.session.robot.status = 1
        d._epoch = 2
        await d.interruption.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        task = asyncio.create_task(d._enqueue_playback('reply', 2, b'\0'*3200))
        await asyncio.sleep(0)
        assert not task.done() and d._media_outputs.empty()
        assert await d.interruption.maybe_barge_in(AsrVadResult(900, 3, 100, 400, '')) is False
        await asyncio.wait_for(task, .1)
        assert d._media_outputs.get_nowait().payload['replyId'] == 'reply'
        await d.close('test_done')
    asyncio.run(run())


def test_one_frame_vad_candidate_holds_unsent_audio_but_never_interrupts():
    async def run():
        d, gate, _ = overlap()
        d.session.robot.status = 1
        await gate.maybe_barge_in(AsrVadResult(100, 0, activity={'voiced': True}))
        waiting = asyncio.create_task(gate.local.wait_before_first_audio())
        await asyncio.sleep(0)
        assert not waiting.done()
        await gate.maybe_barge_in(AsrVadResult(200, 0, activity={'voiced': False}))
        await asyncio.wait_for(waiting, .1)
        d._interrupt.assert_not_awaited()
    asyncio.run(run())


def test_confirmed_speech_cannot_release_a_held_obsolete_reply_packet():
    async def run():
        d = DuplexDialogue(Session('sess_test', SessionState.ON_SERVICE),
            asr=ParakeetAsr(vad=object(), inference=AsyncMock()),
            agent=DemoAgent(), tts=DemoTts())
        d._active_reply_id = 'reply'
        d._active_turn_id = 'previous'
        d.session.robot.status = 1
        await d.interruption.maybe_barge_in(AsrVadResult(300, 1, 100, 300))
        task = asyncio.create_task(d._enqueue_playback('reply', 0, b'\0'*3200))
        await asyncio.sleep(0)
        await d.interruption.maybe_barge_in(partial())
        await asyncio.wait_for(task, .1)
        assert d._media_outputs.empty() and d._active_reply_id is None
        assert not any(e.kind == 'assistant_delta' for e in list(d._control_outputs._queue))
        await d.close('test_done')
    asyncio.run(run())


@pytest.mark.parametrize('text', ['I would like.', 'I think.', 'My favorite.',
    'I need.', 'I want to.', 'I am going to.'])
def test_incomplete_prefix_does_not_gain_a_fast_endpoint_from_asr_punctuation(text):
    assert unfinished_english(text)


@pytest.mark.parametrize('text', ["Hi, what's your name?", 'Tell me about three interesting jobs.',
    'I want to be a teacher.', 'How can I practice English every day?',
    'What is it made of?', 'What should I do?', 'I like cats.', 'Yes.'])
def test_complete_sentences_retain_the_existing_fast_endpoint(text):
    assert not unfinished_english(text)
