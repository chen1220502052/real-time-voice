"""Isolated, paired 4B first-audio experiment using actual dialogue buffering."""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import time
import wave
from unittest.mock import patch

from backend import local_llm
from backend.english_chat import EnglishChatBot
from backend.llm_runtime import LLMHttpPool, Timing, use_llm_pool, use_timing
from backend.providers import speech_worker
from backend.speech_buffer import SpeechBuffer
from backend.speech_text import take_speech_segments
from backend.text_bot import BotMessage, BotRequest
from scripts import compare_local_voice_models as base
from scripts.experimental_first_audio import FirstPhraseBuffer
from scripts.experimental_smooth_audio import SmoothPriorityBuffer
from scripts.mlx_voice_experiment import NativeWorker, MODEL

ROOT = Path(__file__).resolve().parents[1]


async def other_evaluations():
    process=await asyncio.create_subprocess_exec('ps','-axo','pid=,command=',stdout=asyncio.subprocess.PIPE)
    output,_=await process.communicate()
    return [line.strip() for line in output.decode().splitlines()
            if (' -m scripts.evaluate_' in line or ' -u -m scripts.evaluate_' in line
                or ' -m scripts.transcribe_' in line or ' -u -m scripts.transcribe_' in line
                or '/transcribe.py' in line)
            and 'scripts.evaluate_first_audio' not in line]


async def wait_for_gpu():
    waiting=False
    while await other_evaluations():
        if not waiting:print('WAIT_FOR_OTHER_EVALUATION',flush=True)
        waiting=True
        await asyncio.sleep(5)
    if waiting:
        print('OTHER_EVALUATION_FINISHED',flush=True)
        await asyncio.sleep(3)


class ExperimentalSpeechWorker(speech_worker.SpeechWorker):
    async def _start(self):
        if self.process and self.process.returncode is None:
            return
        python = Path(os.environ['LOCAL_'+self.kind.upper()+'_PYTHON'])
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false')
        self.log = (ROOT/f'.local/first-audio-{self.kind}.log').open('ab')
        self.process = await asyncio.create_subprocess_exec(str(python), '-u', '-m',
            'scripts.experimental_tts_worker', self.kind, cwd=ROOT, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log,
            limit=8*1024*1024)
        async with asyncio.timeout(120):
            line = await self.process.stdout.readline()
        if not line or not json.loads(line).get('ready'):
            raise RuntimeError('Experimental speech worker failed to start')


class SeededNativeWorker(NativeWorker):
    seed = 0

    async def begin(self, payload):
        return await super().begin({**payload, 'experimentSeed': self.seed})


async def audio_case(case, repeat, profile, directory, native=None):
    with wave.open(str(ROOT/case['audio'])) as wav:
        assert wav.getparams()[:3] == (1, 2, 16000)
        pcm = wav.readframes(wav.getnframes())
    started = time.perf_counter()
    def elapsed(): return (time.perf_counter()-started)*1000
    recognized = await speech_worker.worker('asr').request({'pcm':base64.b64encode(pcm).decode()})
    asr_ms = elapsed()
    if not recognized['text'].strip() or recognized.get('confidence', 0) < .9:
        raise ValueError('ASR rejected the input')
    messages = tuple(BotMessage(**m) for m in case['messages'][:-1]) + (BotMessage('user', recognized['text']),)
    request = BotRequest(case['id'], case['id'], case['id'], messages,
                         options={'grade':case['grade']}, channel='voice')
    source = EnglishChatBot().stream(request)
    probe = Timing(case['id'])
    events, chunks, audio, parts = [], [], [], []
    first_audio = first_text = None
    terminal = False

    async def synth(text):
        nonlocal first_audio
        item = {'text':text, 'queuedAtMs':elapsed()}
        chunks.append(item)
        index = len(chunks)-1
        priority = profile in ['native_priority','native_tuned','native_priority_smooth'] and index == 0
        paused = False
        pause_begin = None
        try:
            # Experimental child only. First audio has priority over computing
            # later words; always resume on success, failure or cancellation.
            if priority and native and native.process and native.process.returncode is None:
                os.kill(native.process.pid,signal.SIGUSR1)
                paused=True;pause_begin=time.perf_counter()
            result = await speech_worker.worker('tts').request({'text':text, 'speed':.95, 'volume':.2,
                                                               'experimentSeed':11000+index})
        finally:
            if paused:
                try:os.kill(native.process.pid,signal.SIGUSR2)
                except ProcessLookupError:pass
                item['llmPausedMs']=(time.perf_counter()-pause_begin)*1000
        ready = elapsed()
        if first_audio is None:
            first_audio = ready
        raw = base64.b64decode(result['pcm'], validate=True)
        item.update(readyAtMs=ready, seconds=len(raw)/32000,
                    details={k:v for k,v in result.items() if k not in ['id','pcm']},
                    pcmSha256=hashlib.sha256(raw).hexdigest())
        return raw

    buffer = FirstPhraseBuffer(max_wait_ms=80) if profile == 'native_tuned' else SpeechBuffer()
    if profile=='native_priority_smooth':buffer=SmoothPriorityBuffer()

    async def read():
        nonlocal first_text, terminal
        async for event in source:
            if event.type == 'text_delta':
                if first_text is None: first_text = elapsed()
                parts.append(event.text)
                events.append({'atMs':elapsed(), 'text':event.text})
                await buffer.append(event.text)
            elif event.type == 'done':
                terminal = True
        await buffer.finish()

    async def speak():
        def reserve():
            return max(0, sum(len(p) for p in audio)/32-(elapsed()-first_audio)) if first_audio is not None else 0
        while True:
            segment = await buffer.next(reserve)
            if segment is None: break
            audio.append(await synth(segment))

    try:
        with use_timing(probe):
            if profile == 'native_legacy':
                pending = ''; jobs = []
                async for event in source:
                    if event.type == 'text_delta':
                        if first_text is None: first_text = elapsed()
                        parts.append(event.text); events.append({'atMs':elapsed(),'text':event.text})
                        pending += event.text
                        segments, pending = take_speech_segments(pending, first_chunk=not jobs)
                        jobs.extend(asyncio.create_task(synth(s)) for s in segments)
                    elif event.type == 'done': terminal = True
                if pending.strip(): jobs.append(asyncio.create_task(synth(pending)))
                audio = await asyncio.gather(*jobs)
            else:
                tasks = [asyncio.create_task(read()), asyncio.create_task(speak())]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done(): task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        assert terminal and audio
    finally:
        await source.aclose()
    text = ''.join(parts).strip()
    assert ''.join(''.join(s['text'].split()) for s in chunks) == ''.join(text.split()), 'Speech text changed'
    joined = b''.join(audio)
    path = directory/f'{case["id"]}-{repeat}.wav'
    with wave.open(str(path),'wb') as wav:
        wav.setparams((1,2,16000,0,'NONE','not compressed')); wav.writeframes(joined)
    prior = 0
    for chunk in chunks:
        chunk['playbackUnderrunMs'] = max(0, chunk['readyAtMs']-first_audio-prior)
        prior += chunk['seconds']*1000
    return {'recognized':recognized['text'], 'asrMs':asr_ms,'text':text,
            'textSha256':hashlib.sha256(text.encode()).hexdigest(), 'firstTextDeltaMs':first_text,
            'firstAudioMs':first_audio, 'totalMs':elapsed(), 'metrics':probe.metrics,
            'events':events,'chunks':chunks,'output':str(path),'audioSha256':base.digest(path),
            'audioSeconds':len(joined)/32000,'textPreserved':True,'score':base.score(case,text)}


async def main(args):
    outer = Path(args.outer).resolve(); dest = Path(args.output).resolve()
    cases = json.loads((ROOT/'evals/first_audio/cases.json').read_text())
    base.prepare(cases, ROOT/'.local/first-audio-inputs')
    native = SeededNativeWorker(outer/'.local/llm-mlx-venv/bin/python',
        outer/'.local/qwen35-optimization/affine-model',ROOT/'scripts/experimental_native_worker.py',
        outer/'.local/qwen35-first-audio/native-worker.log')
    pool = LLMHttpPool(); original = local_llm._stream_reply
    profiles = args.profiles
    data = {'date':'2026-11-05','cases':cases,'rows':[],'preparation':[],
            'scope':'Same frozen LLM/Bot/ASR/TTS weights. Native profiles use identical per-case/repeat RNG seeds; only segmentation/scheduling and TTS allocator budget differ. Actual buffer uses SpeechBuffer, legacy matches prior benchmark. PCM-ready latency excludes live VAD/browser/speaker buffering.',
            'settings':{'thinking':False,'temperature':.3,'maxTokens':256,'context':4096,'ttsSpeed':.95,'ttsVolume':.2,'ttsAudioCache':False},
            'executionRoot':str(ROOT)}
    if args.reference:
        reference_path=Path(args.reference)
        reference=json.loads(reference_path.read_text())
        assert reference['cases']==cases, 'Cases or input files changed'
        # Only unchanged controls survive a segmentation safety repair.
        data['rows']=[r for r in reference['rows'] if r['profile']!='native_tuned']
        data['preparation']=[p for p in reference['preparation'] if p['profile']!='native_tuned']
        data['reference']={'path':str(reference_path),'sha256':base.digest(reference_path),
                           'rows':len(data['rows']),'scope':'Unchanged 3B/legacy/actual controls from the interrupted run. No tuned rows inherited.'}
    async def routed(messages,**kwargs):
        stream = native.stream(messages,**kwargs) if os.getenv('VOICE_CHAT_MODEL') == MODEL else original(messages,**kwargs)
        try:
            async for delta in stream: yield delta
        finally:
            await stream.aclose()
    def save(): dest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    try:
        with patch.dict(os.environ,base.environment()), use_llm_pool(pool), \
             patch.object(speech_worker,'SpeechWorker',ExperimentalSpeechWorker), \
             patch.object(local_llm,'_stream_reply',routed):
            for repeat in range(args.repeat):
                for profile in (profiles if repeat%2==0 else profiles[::-1]):
                    await wait_for_gpu()
                    existing={r['id'] for r in data['rows'] if r['profile']==profile and r['repeat']==repeat}
                    if len(existing)==len(cases):
                        print('REUSE',profile,repeat,len(existing),flush=True)
                        continue
                    await native.close();await speech_worker.close_speech_workers()
                    for item in (await base.api('ps'))['models']:
                        if item['name'] in ['qwen2.5:3b','qwen3.5:4b','qwen3.5:4b-mlx']:
                            await base.api('generate',{'model':item['name'],'keep_alive':0})
                    os.environ.update(VOICE_CHAT_MODEL='qwen2.5:3b' if profile=='3b_actual' else MODEL,
                                      LOCAL_TTS_MLX_CACHE_MB='512' if profile in ['native_priority','native_tuned','native_priority_smooth'] else '128',
                                      EXPERIMENTAL_TTS_ALIGNMENT='baseline')
                    begin=time.perf_counter();native.seed=0
                    await local_llm.complete([{'role':'user','content':'Reply with exactly this sentence: Hello.'}],purpose='voice_chat',max_tokens=128)
                    prep = await native.prepare(sorted({c['grade'] for c in cases})) if profile!='3b_actual' else None
                    await speech_worker.worker('asr').request();await speech_worker.worker('tts').request()
                    directory=ROOT/'.local/first-audio-output'/profile;directory.mkdir(parents=True,exist_ok=True)
                    warm=await audio_case(cases[0],'warmup',profile,directory,native)
                    data['preparation'].append({'profile':profile,'repeat':repeat,'ms':(time.perf_counter()-begin)*1000,'prefixes':prep})
                    ordered=list(cases);random.Random(20261105+repeat).shuffle(ordered)
                    for case in ordered:
                        if case['id'] in existing:continue
                        await wait_for_gpu()
                        native.seed=int(hashlib.sha256((case['id']+str(repeat)).encode()).hexdigest()[:8],16)
                        row={'profile':profile,'repeat':repeat,'id':case['id'],'set':case['set'],'seed':native.seed}
                        try: row.update(await audio_case(case,repeat,profile,directory,native))
                        except Exception as error: row.update(error=type(error).__name__+': '+str(error))
                        contention=await other_evaluations()
                        if contention:
                            data.setdefault('contendedRows',[]).append({**row,'otherEvaluations':contention})
                            await wait_for_gpu()
                            row={'profile':profile,'repeat':repeat,'id':case['id'],'set':case['set'],'seed':native.seed}
                            try:row.update(await audio_case(case,repeat,profile,directory,native))
                            except Exception as error:row.update(error=type(error).__name__+': '+str(error))
                            if await other_evaluations():raise RuntimeError('Repeated concurrent evaluation; stop timings')
                        data['rows'].append(row);save()
                        print(profile,repeat,case['id'],round(row.get('firstAudioMs',0)),row.get('error'),flush=True)
    finally:
        await native.close();await speech_worker.close_speech_workers()
        with use_llm_pool(pool):
            for model in ['qwen3.5:4b','qwen3.5:4b-mlx']:
                await base.api('generate',{'model':model,'keep_alive':0})
            await base.api('generate',{'model':'qwen2.5:3b','keep_alive':'30m','options':{'num_ctx':4096}})
        await pool.close();save()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outer',required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--profiles',nargs='+',default=['3b_actual','native_legacy','native_actual','native_priority','native_tuned'])
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--reference')
    asyncio.run(main(parser.parse_args()))
