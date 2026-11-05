import asyncio
import base64
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.providers.speech_local import MlxWhisperAsr,ParakeetAsr,KokoroTts
from backend.providers.speech_worker import SpeechWorker
from backend.interruption import InterruptionController
from scripts.local_speech_worker import accepted_asr_text,accepted_parakeet_text,trim_tts_leading_silence
from scripts.local_speech_worker import cache_tts
from scripts.local_speech_worker import configure_parakeet_precision,load_tts

SPEECH=struct.pack('<1600h',*([2000]*1600))
SILENCE=b'\0'*3200


def test_unvalidated_speech_modes_fail_before_loading_models(monkeypatch):
    configure_parakeet_precision(object(),16)
    with pytest.raises(ValueError,match='8 or 16'):configure_parakeet_precision(object(),4)
    monkeypatch.setenv('LOCAL_TTS_ENGINE','unknown')
    with pytest.raises(ValueError,match='torch or mlx'):load_tts()


def test_audio_cache_is_exact_bounded_and_never_changes_speed_or_volume():
    calls=[]
    def infer(request):
        calls.append(dict(request));return {'pcm':'a'*8,'sampleRate':16000,'stageMs':{'synthesis':12}}
    synth=cache_tts(infer,max_bytes=16,max_entries=2)
    request={'text':'Hello.','speed':1,'volume':.2}
    assert synth(request)['cacheHit'] is False
    assert synth(request)['cacheHit'] is True
    assert len(calls)==1
    assert synth({**request,'speed':.8})['cacheHit'] is False
    assert synth(request)['cacheHit'] is True  # Touch the oldest entry.
    assert synth({**request,'volume':.1})['cacheHit'] is False
    assert synth(request)['cacheHit'] is True
    assert synth({**request,'speed':.8})['cacheHit'] is False  # LRU victim.
    with pytest.raises(ValueError):synth({**request,'speed':float('nan')})


def test_audio_cache_does_not_retain_long_or_oversized_speech():
    calls=[]
    def infer(request):calls.append(request);return {'pcm':'a'*32,'sampleRate':16000}
    synth=cache_tts(infer,max_bytes=16)
    for _ in range(2):assert not synth({'text':'Hi.'})['cacheHit']
    synth=cache_tts(infer,max_bytes=128)
    for _ in range(2):assert not synth({'text':'a'*161})['cacheHit']
    assert len(calls)==4

class Vad:
    def is_speech(self,pcm,rate):return any(pcm)


def test_local_vad_preserves_onset_flushes_once_and_resets_between_calls():
    async def run():
        captured=[]
        async def infer(pcm):captured.append(pcm);return 'I like cats.'
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        results=[]
        for index,frame in enumerate([SPEECH]*4+[SILENCE]*7,1):results.append(await asr.feed(frame,index*100))
        assert [r.status for r in results[:2]]==[0,1]
        assert results[-1].status==3 and results[-1].text=='I like cats.'
        assert results[-1].start_time==0 and results[-1].end_time==400
        assert captured==[SPEECH*4+SILENCE*2]
        assert (await asr.feed(SILENCE,1200)).status==0
        await asr.reset();assert not asr.buffer and not asr.in_speech
    asyncio.run(run())


def test_one_frame_noise_never_invokes_recognition():
    async def run():
        async def forbidden(pcm):raise AssertionError('A click is not an utterance')
        asr=MlxWhisperAsr(vad=Vad(),inference=forbidden)
        for index,frame in enumerate([SPEECH]+[SILENCE]*10,1):
            assert (await asr.feed(frame,index*100)).status==0
    asyncio.run(run())


@pytest.mark.parametrize('text',['I like cats.','What is it made of?','What should I do?'])
def test_parakeet_final_is_ready_during_short_endpoint_window(monkeypatch,text):
    monkeypatch.setenv('LOCAL_VAD_END_MS','500')
    async def run():
        calls=[];clock=[0]
        async def infer(raw):calls.append(raw);return {'text':text,'confidence':.98}
        asr=ParakeetAsr(vad=Vad(),inference=infer);asr._now=lambda:clock[0]
        results=[]
        for seq,pcm in enumerate([SPEECH]*4+[SILENCE]*3,1):
            clock[0]=seq*100;results.append(await asr.feed(pcm,seq*100));await asyncio.sleep(0)
        final=results[-1]
        assert final.status==3 and final.text==text
        assert calls==[SPEECH*4+SILENCE]
        assert final.timing['responseReadyMs']<final.timing['endpointReachedMs']
        assert final.timing['endpointReason']=='stable_transcript'
    asyncio.run(run())


def test_parakeet_incomplete_phrase_allows_600ms_pause_and_rejects_stale_snapshot(monkeypatch):
    monkeypatch.setenv('LOCAL_VAD_END_MS','500')
    async def run():
        clock=[0];calls=[];tail=struct.pack('<1600h',*([3000]*1600))
        async def infer(raw):
            calls.append(raw)
            return {'text':'I want to be a teacher.' if tail in raw else 'I want to.', 'confidence':.98}
        asr=ParakeetAsr(vad=Vad(),inference=infer);asr._now=lambda:clock[0]
        frames=[SPEECH]*4+[SILENCE]*6+[tail]*3+[SILENCE]*5
        finals=[]
        for seq,pcm in enumerate(frames,1):
            clock[0]=seq*100;result=await asr.feed(pcm,seq*100);await asyncio.sleep(0)
            if result.status==3:finals.append(result)
        assert len(finals)==1 and finals[0].text=='I want to be a teacher.'
        assert finals[0].start_time==0 and finals[0].end_time==1300
        assert calls[-1]==b''.join(frames[:13])+SILENCE
    asyncio.run(run())


def test_parakeet_rolling_recognition_never_queues_multiple_native_requests():
    async def run():
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def infer(raw):calls.append(raw);entered.set();await release.wait();return {'text':'I like','confidence':.98}
        asr=ParakeetAsr(vad=Vad(),inference=infer)
        for seq in range(1,7):await asr.feed(SPEECH,seq*100)
        await entered.wait()
        for seq in range(7,25):
            assert (await asr.feed(SPEECH,seq*100)).status==2
            await asyncio.sleep(0)
        assert len(calls)==1
        await asr.reset();release.set();await asyncio.sleep(0)
        assert not asr.buffer and not asr._partial_text
    asyncio.run(run())


def test_parakeet_confidence_rejects_observed_noise_without_blacklisting_answers(monkeypatch):
    monkeypatch.delenv('LOCAL_PARAKEET_MIN_CONFIDENCE',raising=False)
    def result(text,confidence):
        return SimpleNamespace(text=text,tokens=[SimpleNamespace(confidence=confidence)])
    assert accepted_parakeet_text(result('Yeah.',.822))['text']==''
    assert accepted_parakeet_text(result('Yeah.',.98))['text']=='Yeah.'
    assert accepted_parakeet_text(result('You.',.97))['text']=='You.'
    assert accepted_parakeet_text(SimpleNamespace(text='',tokens=[]))['text']==''


def test_asr_computes_during_endpoint_wait_and_reuses_the_result():
    async def run():
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def infer(raw):
            calls.append(raw);entered.set();await release.wait();return 'I like cats.'
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        for seq,pcm in enumerate([SPEECH]*4+[SILENCE]*2,1):result=await asr.feed(pcm,seq*100)
        assert result.status==2
        await asyncio.wait_for(entered.wait(),.5)
        assert calls==[SPEECH*4+SILENCE*2]
        release.set();await asyncio.sleep(0)
        for seq in range(7,12):result=await asr.feed(SILENCE,seq*100)
        assert result.status==3 and result.text=='I like cats.' and len(calls)==1
        assert result.timing['speculative'] is True
        assert result.timing['requestStartedMs']<=result.timing['responseReadyMs']<=result.timing['endpointReachedMs']
        assert result.timing['snapshotEndMediaMs']==600
    asyncio.run(run())


def test_resumed_speech_discards_speculative_text_and_transcribes_latest_audio():
    async def run():
        entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def infer(raw):
            calls.append(raw)
            if len(calls)==1:
                entered.set();await release.wait();return 'Stale partial.'
            return 'The complete new utterance.'
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        for seq,pcm in enumerate([SPEECH]*4+[SILENCE]*2,1):await asr.feed(pcm,seq*100)
        await entered.wait()
        for seq in (7,8):await asr.feed(SPEECH,seq*100)
        release.set();await asyncio.sleep(0)
        for seq in range(9,16):result=await asr.feed(SILENCE,seq*100)
        assert result.text=='The complete new utterance.' and len(calls)==2
        assert calls[1]==SPEECH*4+SILENCE*2+SPEECH*2+SILENCE*2
    asyncio.run(run())


def test_reset_cancels_pending_speculative_asr():
    async def run():
        entered=asyncio.Event();closed=asyncio.Event()
        async def infer(raw):
            entered.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        for seq,pcm in enumerate([SPEECH]*3+[SILENCE]*2,1):await asr.feed(pcm,seq*100)
        await entered.wait();await asr.reset();await asyncio.sleep(0)
        assert closed.is_set() and asr._draft is None and not asr.buffer
    asyncio.run(run())


@pytest.mark.parametrize('speech_packets,text',[(2,''),(25,''),(4,'Hello!')])
def test_batch_asr_only_interrupts_after_a_nonempty_final(speech_packets,text):
    async def run():
        async def infer(pcm):return text
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        interrupt=AsyncMock();timeline=[]
        dialogue=SimpleNamespace(asr=asr,_active_reply_id='reply',_active_user_turn_id='turn',
            session=SimpleNamespace(robot=SimpleNamespace(status=2)),_interrupt=interrupt,
            _emit_timeline=lambda *args,**kwargs:timeline.append((args,kwargs)))
        gate=InterruptionController(dialogue)
        # A 200 ms burst plus 700 ms endpoint silence used to cross the
        # acoustic gate and cancel the reply before Whisper rejected the noise.
        for index,frame in enumerate([SPEECH]*speech_packets+[SILENCE]*7,1):
            result=await asr.feed(frame,index*100)
            await gate.maybe_barge_in(result)
            if result.status!=3:interrupt.assert_not_awaited()
        assert interrupt.await_count==int(bool(text))
        if text:assert timeline[-1][1]['attributes']['evidence']=='asr_final'
    asyncio.run(run())


def test_uncertain_room_noise_is_rejected_without_blacklisting_short_words():
    def segment(text,logprob,no_speech=.01):
        return {'text':text,'avg_logprob':logprob,'no_speech_prob':no_speech}
    assert accepted_asr_text({'segments':[segment(' you',-1.1386)]})==''
    assert accepted_asr_text({'segments':[segment(' made up sentence',-1.55)]})==''
    assert accepted_asr_text({'segments':[segment(' Thank you.',-.4,.75)]})==''
    assert accepted_asr_text({'segments':[segment(' You.',-.3)]})=='You.'
    assert accepted_asr_text({'segments':[segment(" Hi, what's your name?",-.4557)]})=="Hi, what's your name?"


def test_tts_prefix_trim_keeps_quiet_consonant_preroll_and_all_speech():
    # 300 ms of padding followed by a quiet 50 ms onset and voiced speech.
    silence=b'\0'*9600;quiet=struct.pack('<800h',*([10]*800))
    voiced=struct.pack('<1600h',*([1000]*1600))
    result=trim_tts_leading_silence(silence+quiet+voiced)
    assert result==b'\0'*960+quiet+voiced
    assert trim_tts_leading_silence(voiced)==voiced
    assert trim_tts_leading_silence(silence)==silence
    # A normal onset needs only 30 ms padding. A weak consonant still remains
    # intact, even though its amplitude is below the previous threshold of 20.
    assert trim_tts_leading_silence(silence+voiced)==b'\0'*960+voiced
    spike=struct.pack('<h',4)+silence[2:]
    assert trim_tts_leading_silence(spike+voiced)==b'\0'*960+voiced


def test_continuous_speech_has_a_bounded_buffer(monkeypatch):
    monkeypatch.setenv('LOCAL_ASR_MAX_SECONDS','2')
    async def run():
        lengths=[]
        async def infer(pcm):lengths.append(len(pcm));return 'A long sentence.'
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        for index in range(1,21):result=await asr.feed(SPEECH,index*100)
        assert result.status==3 and lengths==[20*3200]
        assert not asr.buffer
    asyncio.run(run())


def test_cancelled_inference_is_drained_before_next_worker_request():
    async def run():
        worker=SpeechWorker('tts');entered=asyncio.Event();release=asyncio.Event();order=[]
        async def request(payload):
            async with worker.lock:
                worker.dispatched.add(asyncio.current_task())
                order.append(payload)
                if payload=='old':entered.set();await release.wait()
                order.append(payload+' done');return payload
        worker._request=request
        first=asyncio.create_task(worker.request('old'));await entered.wait();first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        second=asyncio.create_task(worker.request('new'));await asyncio.sleep(0)
        assert order==['old']
        release.set();assert await second=='new'
        assert order==['old','old done','new','new done']
        await worker.close()
    asyncio.run(run())


def test_kokoro_pcm_uses_protocol_rate_and_preserves_tail(monkeypatch):
    raw=SPEECH*2+SPEECH[:400]
    class Worker:
        async def request(self,payload):return {'sampleRate':16000,'pcm':base64.b64encode(raw).decode()}
    monkeypatch.setattr('backend.providers.speech_local.worker',lambda kind:Worker())
    async def run():
        chunks=[p async for p in KokoroTts().stream_pcm('Hello.')]
        assert b''.join(chunks)==raw and [len(p) for p in chunks]==[3200,3200,400]
    asyncio.run(run())


def test_bilingual_tts_keeps_examples_and_mandarin_numbers_intact():
    from scripts.local_speech_worker import tts_language_segments
    for text in ['Hello, I am Kitty.', '今天有12只猫。', "cherry 的意思是樱桃。I like cherries.",
                 '我是 Kitty，一只 AI 猫。']:
        parts=tts_language_segments(text)
        assert ''.join(segment for _,segment in parts)==text
    assert tts_language_segments('今天有12只猫。')==[('z','今天有12只猫。')]
    parts=tts_language_segments('樱桃。I like cherries.')
    assert parts==[('z','樱桃。'),('a','I like cherries.')]


def test_live_partials_defer_multilingual_detection_until_quiet(monkeypatch):
    calls=[]
    class Worker:
        async def request(self,payload):
            calls.append(payload)
            return {'text':'请用中文回答。','confidence':.98,'engine':'whisper','language':'zh'}
    monkeypatch.setattr('backend.providers.speech_local.worker',lambda _:Worker())
    async def run():
        asr=ParakeetAsr(vad=Vad());asr.buffer=bytearray(SPEECH*6)
        asr.quiet=0;asr._prepare_final(600);await asr._draft
        assert calls[-1]['multilingual'] is False
        asr._draft_valid=False;asr.quiet=1;asr._prepare_final(700)
        text,details=await asr._draft
        assert calls[-1]['multilingual'] is True
        assert text=='请用中文回答。' and details['engine']=='whisper' and details['language']=='zh'
        await asr.reset()
    asyncio.run(run())


def test_meaning_request_keeps_listening_across_a_300ms_extra_task_pause():
    async def run():
        clock=[0];tail=struct.pack('<1600h',*([3000]*1600))
        async def infer(raw):
            return {'text':('What does doctor mean in Chinese? Give one English example.' if tail in raw else
                            'What does doctor mean in Chinese?'),'confidence':.99}
        asr=ParakeetAsr(vad=Vad(),inference=infer);asr._now=lambda:clock[0]
        frames=[SPEECH]*6+[SILENCE]*3+[tail]*5+[SILENCE]*5;finals=[]
        for seq,frame in enumerate(frames,1):
            clock[0]=seq*100;r=await asr.feed(frame,seq*100);await asyncio.sleep(0)
            if r.status==3:finals.append(r)
        assert len(finals)==1 and 'Give one English example' in finals[0].text
        assert finals[0].timing['endpointReason']=='compound_request_window'
        assert finals[0].timing['endpointSilenceMs']==500
    asyncio.run(run())


def test_preroll_preserves_three_quiet_consonant_frames_before_two_vad_votes():
    weak=struct.pack('<1600h',*([120]*1600));captured=[]
    async def infer(raw):captured.append(raw);return 'fox'
    async def run():
        asr=MlxWhisperAsr(vad=Vad(),inference=infer)
        for i,frame in enumerate([weak]*3+[SPEECH]*3+[SILENCE]*7,1):await asr.feed(frame,i*100)
        assert captured and captured[-1].startswith(weak*3+SPEECH*3)
    asyncio.run(run())


def test_mandarin_word_carrier_waits_for_the_actual_english_word():
    async def run():
        clock=[0];tail=struct.pack('<1600h',*([3000]*1600))
        async def infer(raw):return {'text':'请告诉我英语单词 cup 的中文意思。' if tail in raw else '请告诉我英语单词。','confidence':.99}
        asr=ParakeetAsr(vad=Vad(),inference=infer);asr._now=lambda:clock[0];finals=[]
        for i,frame in enumerate([SPEECH]*6+[SILENCE]*6+[tail]*4+[SILENCE]*5,1):
            clock[0]=i*100;r=await asr.feed(frame,i*100);await asyncio.sleep(0)
            if r.status==3:finals.append(r)
        assert len(finals)==1 and 'cup' in finals[0].text
    asyncio.run(run())


def test_slow_quiet_decoder_does_not_block_resumed_target_audio():
    async def run():
        release=asyncio.Event();tail=struct.pack('<1600h',*([3000]*1600));calls=[]
        async def infer(raw):
            calls.append(raw)
            if tail not in raw:await release.wait()
            return {'text':'请告诉我 window 的中文意思。' if tail in raw else '请告诉我。','confidence':.98}
        asr=ParakeetAsr(vad=Vad(),inference=infer);asr.interim_interval_packets=0
        seq=0
        for frame in [SPEECH]*4+[SILENCE]*5:
            seq+=1;r=await asyncio.wait_for(asr.feed(frame,seq*100),.1);await asyncio.sleep(0)
            assert r.status!=3
        for frame in [tail]*3:
            seq+=1;await asr.feed(frame,seq*100);await asyncio.sleep(0)
        release.set();await asyncio.sleep(0);finals=[]
        for frame in [SILENCE]*6:
            seq+=1;r=await asr.feed(frame,seq*100);await asyncio.sleep(0)
            if r.status==3:finals.append(r)
        assert len(finals)==1 and 'window' in finals[0].text and tail in calls[-1]
    asyncio.run(run())
