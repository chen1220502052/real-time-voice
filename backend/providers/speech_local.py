"""Live English speech using WebRTC VAD, local MLX ASR and local Kokoro."""
import asyncio
import base64
from collections import deque
import os
import re
import time

from .common import AsrVadResult,AsrUnavailable,normalized_pcm16_rms
from .speech_worker import worker
from ..local_turn_taking import unfinished_english


class MlxWhisperAsr:
    # Batch Whisper has no partial transcripts. VAD (including its end-of-
    # speech hangover) cannot establish interruption intent on its own.
    barge_in_requires_final = True
    snapshot_quiet_packets = 2
    interim_interval_packets = 0
    model_kind = 'whisper'

    def __init__(self,*,vad=None,inference=None):
        if vad is None:
            import webrtcvad
            vad=webrtcvad.Vad(int(os.getenv('LOCAL_VAD_MODE','2')))
        self.vad=vad;self.inference=inference
        self.end_packets=max(3,int(os.getenv('LOCAL_VAD_END_MS','700'))//100)
        self.maximum_packets=min(300,max(20,int(os.getenv('LOCAL_ASR_MAX_SECONDS','30'))*10))
        self.threshold=float(os.getenv('LOCAL_VAD_MIN_RMS','.006'))
        self.speculative=os.getenv('LOCAL_ASR_SPECULATIVE','1').lower() not in {'0','false','off'}
        self._clear()

    def _clear(self):
        draft=getattr(self,'_draft',None)
        if draft is not None and not draft.done():draft.cancel()
        self._draft=None;self._draft_valid=False
        # Two positive VAD frames may follow several quiet consonant frames.
        # Preserve those frames rather than starting the ASR at the vowel.
        pre_roll=max(3,min(10,int(os.getenv('LOCAL_VAD_PRE_ROLL_MS','500'))//100))
        self.pre_roll=deque(maxlen=pre_roll);self.buffer=bytearray();self.in_speech=False
        self.start_time=None;self.onset=0;self.quiet=0;self.voiced=0;self.packets=0
        self._draft_quiet=0;self._last_snapshot_time=0;self._observed_draft=None
        self._partial_text='';self._partial_confidence=0;self._partial_changed_ms=None
        self._emitted_partial='';self._decode_count=0
        self._partial_end_media_ms=None

    def _now(self):return time.monotonic()*1000

    async def _transcribe(self,raw,snapshot_end,*,multilingual=True):
        started=self._now()
        if self.inference:
            result=await self.inference(raw)
            if isinstance(result,str):result={'text':result}
        else:
            result=await worker('asr').request({'pcm':base64.b64encode(raw).decode(),
                                                'multilingual':multilingual})
        return result['text'],{'requestStartedMs':started,'responseReadyMs':self._now(),
            'workerInferenceMs':result.get('inferenceMs'),'snapshotEndMediaMs':snapshot_end,
            'workerQueueWaitMs':result.get('queueWaitMs'),'workerStartupMs':result.get('startupMs'),
            'workerIpcWallMs':result.get('ipcWallMs'),'workerMemory':result.get('memory'),
            'confidence':result.get('confidence'),'engine':result.get('engine',self.model_kind),
            'targetVerification':result.get('targetVerification'),
            'language':result.get('language')}

    def _prepare_final(self,speech_time):
        if not self.speculative:return
        if self._draft is not None:
            if not self._draft.done():return
            if self._draft_valid and self._draft_quiet>=self.snapshot_quiet_packets:return
        raw=bytes(self.buffer)
        trim=max(0,self.quiet-self.snapshot_quiet_packets)
        if trim:raw=raw[:-trim*3200]
        snapshot_end=speech_time-trim*100
        self._draft=asyncio.create_task(self._transcribe(
            raw,snapshot_end,multilingual=self.quiet>=self.snapshot_quiet_packets));self._draft_valid=True
        self._draft_quiet=min(self.quiet,self.snapshot_quiet_packets)
        self._last_snapshot_time=speech_time;self._decode_count+=1
        def finished(task):
            if not task.cancelled():task.exception()
        self._draft.add_done_callback(finished)

    def _observe_partial(self):
        if not self.interim_interval_packets:return ''
        task=self._draft
        if task is not None and task.done() and task is not self._observed_draft:
            self._observed_draft=task
            if not task.cancelled() and task.exception() is None:
                text,detail=task.result()
                if text!=self._partial_text:self._partial_changed_ms=detail['responseReadyMs']
                self._partial_text=text;self._partial_confidence=detail.get('confidence') or 0
                self._partial_end_media_ms=detail['snapshotEndMediaMs']
        if self._partial_text!=self._emitted_partial:
            self._emitted_partial=self._partial_text
            return self._partial_text
        return ''

    def _endpoint_policy(self):return self.end_packets,'fixed_silence'

    def _activity(self,voiced):
        return {'voiced':voiced,'voicedMs':self.voiced*100,'quietMs':self.quiet*100,
            'text':self._partial_text,'confidence':self._partial_confidence,
            'textEndMediaMs':self._partial_end_media_ms}

    async def warmup(self):
        if self.inference is None:await worker('asr').request()

    async def feed(self,pcm,speech_time):
        if len(pcm)!=3200:raise ValueError('Expected 100ms mono 16kHz PCM16')
        self.pre_roll.append(pcm)
        votes=sum(self.vad.is_speech(pcm[i:i+640],16000) for i in range(0,3200,640))
        voiced=votes>=2 and normalized_pcm16_rms(pcm)>=self.threshold
        if not self.in_speech:
            self.onset=self.onset+1 if voiced else 0
            if self.onset<2:return AsrVadResult(speech_time,0,activity=self._activity(voiced))
            self.in_speech=True;self.buffer=bytearray(b''.join(self.pre_roll))
            self.start_time=max(0,speech_time-len(self.buffer)//32);self.voiced=self.onset;self.packets=len(self.pre_roll)
            return AsrVadResult(speech_time,1,self.start_time,speech_time,activity=self._activity(voiced))
        self.buffer.extend(pcm);self.packets+=1;self.voiced+=int(voiced)
        self.quiet=0 if voiced else self.quiet+1
        if voiced:self._draft_valid=False
        # Start encoding while the endpoint timer is still running. A resumed
        # utterance invalidates this snapshot; it can never publish stale text.
        partial=self._observe_partial()
        if (self.quiet>=self.snapshot_quiet_packets or
                (self.interim_interval_packets and self.voiced>=self.interim_interval_packets and voiced and
                 speech_time-self._last_snapshot_time>=self.interim_interval_packets*100)):
            self._prepare_final(speech_time)
        endpoint_packets,endpoint_reason=self._endpoint_policy()
        if self.quiet<endpoint_packets and self.packets<self.maximum_packets:
            return AsrVadResult(speech_time,2,self.start_time,speech_time,partial,activity=self._activity(voiced))
        endpoint_reached=self._now()
        start=self.start_time
        end=speech_time-self.quiet*100
        snapshot_end=end+min(self.quiet,self.snapshot_quiet_packets)*100
        raw=bytes(self.buffer)
        if self.quiet>self.snapshot_quiet_packets:raw=raw[:-(self.quiet-self.snapshot_quiet_packets)*3200]
        voiced_count=self.voiced
        draft=self._draft if self._draft_valid and self._draft_quiet>=self.snapshot_quiet_packets else None
        decode_count=self._decode_count
        if draft is not None:self._draft=None
        self._clear()
        if voiced_count<2:return AsrVadResult(speech_time,3,start,end,'')
        try:
            text,detail=await draft if draft is not None else await self._transcribe(raw,snapshot_end)
        except Exception as error:
            raise AsrUnavailable('本地语音识别暂不可用，请重新拨通后重试。') from error
        detail.update(endpointReachedMs=endpoint_reached,speculative=draft is not None,
                      endpointSilenceMs=endpoint_packets*100,endpointReason=endpoint_reason,
                      snapshotRequests=decode_count)
        return AsrVadResult(speech_time,3,start,max(start,end),text,detail,
            {'confidence':detail.get('confidence') or 0,'voicedMs':voiced_count*100})

    async def reset(self):self._clear()


class ParakeetAsr(MlxWhisperAsr):
    """Bounded rolling hypotheses, followed by a complete snapshot at 100 ms quiet.

    Complete snapshots retain the full utterance context. Only a current quiet
    snapshot can publish a final; resuming speech always invalidates it.
    """
    model_kind='parakeet'
    supports_barge_in_partials=True
    snapshot_quiet_packets=1
    interim_interval_packets=6

    def __init__(self,**kwargs):
        super().__init__(**kwargs)
        self.end_packets=max(3,int(os.getenv('LOCAL_VAD_END_MS','500'))//100)

    def set_robot_active(self,active):
        # More frequent hypotheses only during overlap; ordinary reply timing
        # and ASR workload keep their existing cadence.
        self.interim_interval_packets=3 if active else 6

    def _endpoint_policy(self):
        words=re.findall(r"[a-z]+(?:'[a-z]+)?",self._partial_text.lower())
        current=(self._draft_valid and self._draft_quiet>=self.snapshot_quiet_packets
                 and self._draft is not None and self._draft.done() and not self._draft.cancelled()
                 and self._draft.exception() is None)
        if self.speculative and self.quiet and not current:
            # Keep consuming new PCM while a quiet snapshot decodes. Awaiting
            # it inside finalization would discard speech that resumes during
            # inference, especially an English word after a Mandarin carrier.
            return max(self.end_packets,9),'await_current_transcript'
        if unfinished_english(self._partial_text):
            return max(self.end_packets,9),'unfinished_phrase'
        compact=re.sub(r'[\s，,。.!！？?：:"“”\x27]', '', self._partial_text)
        if (not re.search(r'[A-Za-z]',compact) and re.search(
                r'(?:请|帮我)?(?:用中文)?(?:告诉我|说说|说|解释|翻译)(?:一下)?(?:这个)?(?:英语|英文)?(?:单词|词)?$'
                r'|(?:英语|英文)单词$',compact)):
            return max(self.end_packets,9),'pending_target_word'
        if re.search(r'\b(?:mean|meaning|translate|example)\b|解释|意思|例句|造句', self._partial_text, re.I):
            # A natural question/extra-task pause can be 300 ms. Releasing at
            # that point loses "Give an example" before the Bot can see it.
            return max(self.end_packets,5),'compound_request_window'
        stable=(self._partial_changed_ms is not None and self._now()-self._partial_changed_ms>=180)
        complete=(len(words)>=3 or len(re.findall(r'[\u4e00-\u9fff]',self._partial_text))>=2
                  or self._partial_text.lower().strip(' .!?') in
                  {'yes','no','hello','hi','thank you','thanks','good morning','goodbye'})
        if current and stable and complete and self._partial_confidence>=.9:
            return min(self.end_packets,3),'stable_transcript'
        return self.end_packets,'normal_silence'


class KokoroTts:
    async def warmup(self):await worker('tts').request()

    async def stream_pcm(self,text,speed=1.0,volume=.2,voice='default'):
        started=time.monotonic()*1000
        result=await worker('tts').request({'text':text,'speed':speed,'volume':volume})
        self.last_timing={'requestWallMs':time.monotonic()*1000-started,
            'workerInferenceMs':result.get('inferenceMs'),'engine':result.get('engine'),
            'workerMemory':result.get('memory'),
            'cacheHit':result.get('cacheHit',False),'stageMs':result.get('stageMs',{})}
        if result.get('sampleRate')!=16000:raise ValueError('Local TTS must return 16kHz PCM')
        pcm=base64.b64decode(result['pcm'],validate=True)
        if not pcm or len(pcm)%2:raise ValueError('Local TTS returned invalid PCM16')
        for offset in range(0,len(pcm),3200):
            yield pcm[offset:offset+3200]
            await asyncio.sleep(0)
