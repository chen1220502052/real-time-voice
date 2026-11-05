"""Isolated, loopback-only model comparison; never rewrites service configuration."""
import argparse
import asyncio
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import time
import wave
from unittest.mock import patch

from backend import local_llm
from backend.english_chat import EnglishChatBot
from backend.llm_runtime import LLMHttpPool, Timing, llm_client, use_llm_pool, use_timing
from backend.providers.speech_worker import worker, close_speech_workers
from backend.speech_text import take_speech_segments
from backend.text_bot import BotMessage, BotRequest
from scripts.evaluate_voice_chat import generate_pipeline, percentile, score

ROOT = Path(__file__).resolve().parents[1]
MODELS = ['qwen2.5:3b', 'qwen3.5:2b', 'qwen3.5:4b']
WATCHED = ['.env.local', 'scripts/run_learning_local.sh', 'backend/local_llm.py',
           'backend/voice_prompt.py', 'backend/voice_grounding.py',
           'backend/english_chat.py', 'backend/curriculum_chat.py']


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def environment():
    # Read speech settings only. Do not source credentials or modify .env.local.
    allowed = {'LOCAL_ASR_PYTHON', 'ASR_BACKEND', 'LOCAL_ASR_LANGUAGE',
               'LOCAL_PARAKEET_QUANT_BITS', 'LOCAL_TTS_ENGINE', 'LOCAL_TTS_PYTHON',
               'LOCAL_TTS_VOICE', 'TTS_CPU_THREADS', 'LOCAL_PARAKEET_MIN_CONFIDENCE'}
    values = {}
    for line in (ROOT / '.env.local').read_text().splitlines():
        key, _, value = line.partition('=')
        if key.strip() in allowed:
            values[key.strip()] = value.strip().strip('\"\'')
    values.update(LLM_PROVIDER='ollama', CHAT_PROVIDER='ollama',
                  OLLAMA_NATIVE_BASE_URL='http://127.0.0.1:11434',
                  VOICE_CHAT_NUM_CTX='4096', OLLAMA_KEEP_ALIVE='30m', LOCAL_TTS_CACHE='0')
    return values


async def api(path, payload=None):
    async with llm_client() as client:
        method = client.get if payload is None else client.post
        async with method('http://127.0.0.1:11434/api/' + path,
                          **({} if payload is None else {'json': payload}), allow_redirects=False) as response:
            response.raise_for_status()
            return await response.json()


def summary(rows):
    result = {}
    for model in sorted({r['model'] for r in rows}):
        groups = {}
        for phase in sorted({r['phase'] for r in rows}):
            selected = [r for r in rows if r['model'] == model and r['phase'] == phase]
            if not selected:
                continue
            groups[phase] = {}
            for label, items in [('all', selected),
                                 ('generated', [r for r in selected if r.get('calls')]),
                                 ('direct', [r for r in selected if not r.get('calls') and not r.get('error')])]:
                if not items:
                    continue
                groups[phase][label] = {
                    'count': len(items), 'rulePassed': sum(r.get('score', {}).get('passed', False) for r in items),
                    'errors': sum(bool(r.get('error')) for r in items),
                    'failureCounts': dict(Counter(f.split(':')[0] for r in items for f in r.get('score', {}).get('failures', []))),
                    'latency': {k: {'p50': percentile([r.get(k) for r in items], .5),
                                    'p95': percentile([r.get(k) for r in items], .95)}
                                for k in ['firstTextDeltaMs', 'totalMs', 'firstAudioMs', 'asrMs', 'firstAudioAfterAsrMs']},
                }
        result[model] = groups
    return result


async def audio_case(case, repeat, directory):
    with wave.open(str(ROOT / case['audio']), 'rb') as f:
        assert f.getparams()[:3] == (1, 2, 16000)
        pcm = f.readframes(f.getnframes())
    started = time.perf_counter()
    recognized = await worker('asr').request({'pcm': base64.b64encode(pcm).decode()})
    asr_ms = (time.perf_counter() - started) * 1000
    result = {'recognized': recognized['text'], 'asrMs': asr_ms,
              'asrConfidence': recognized.get('confidence'), 'asrDetails': {k: v for k, v in recognized.items() if k not in {'id'}}}
    threshold = float(os.getenv('LOCAL_PARAKEET_MIN_CONFIDENCE', '.9'))
    if not recognized['text'].strip() or (recognized.get('confidence') or 0) < threshold:
        return {**result, 'error': 'asr_rejected', 'text': '', 'totalMs': (time.perf_counter()-started)*1000}
    messages = tuple(BotMessage(**m) for m in case['messages'][:-1]) + (BotMessage('user', recognized['text']),)
    request = BotRequest(case['id'], case['id'], case['id'], messages,
                         options={'grade': case['grade']}, channel='voice')
    probe = Timing(case['id'])
    pending, parts, jobs, tts_rows = '', [], [], []
    first_audio, first_text, terminal = None, None, False

    async def synth(text):
        nonlocal first_audio
        item = await worker('tts').request({'text': text, 'speed': .95, 'volume': .2})
        if first_audio is None:
            first_audio = (time.perf_counter()-started)*1000
        assert item['sampleRate'] == 16000
        tts_rows.append({k: v for k, v in item.items() if k not in {'pcm', 'id'}})
        return base64.b64decode(item['pcm'], validate=True)

    source = EnglishChatBot().stream(request)
    try:
        with use_timing(probe):
            async for event in source:
                if event.type == 'text_delta':
                    if first_text is None:
                        first_text = (time.perf_counter()-started)*1000
                    parts.append(event.text)
                    pending += event.text
                    segments, pending = take_speech_segments(pending, first_chunk=not jobs)
                    jobs.extend(asyncio.create_task(synth(segment)) for segment in segments)
                elif event.type == 'done':
                    terminal = True
            if pending.strip():
                jobs.append(asyncio.create_task(synth(pending)))
            audio = b''.join(await asyncio.gather(*jobs))
        if not terminal or not audio:
            raise ValueError('Missing terminal event or valid audio')
    finally:
        await source.aclose()
        await asyncio.gather(*jobs, return_exceptions=True)
    path = directory / f'{case["id"]}-{repeat}.wav'
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(audio)
    return {**result, 'text': ''.join(parts).strip(), 'firstTextDeltaMs': first_text,
            'firstAudioMs': first_audio, 'firstAudioAfterAsrMs': first_audio-asr_ms,
            'totalMs': (time.perf_counter()-started)*1000, 'metrics': probe.metrics,
            'ttsDetails': tts_rows, 'audioSeconds': len(audio)/32000,
            'output': str(path.relative_to(ROOT)), 'audioSha256': digest(path)}


def prepare(cases, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if not case.get('audioTest'):
            continue
        path = directory / (case['id'] + '.wav')
        aiff = path.with_suffix('.aiff')
        text = case['messages'][-1]['content']
        if not path.exists():
            subprocess.run(['say', '-v', 'Eddy (中文（中国大陆）)' if re.search('[\u4e00-\u9fff]', text) else 'Samantha',
                            '-r', '165', '-o', str(aiff), text], check=True)
            subprocess.run(['afconvert', '-f', 'WAVE', '-d', 'LEI16@16000', '-c', '1', str(aiff), str(path)], check=True)
        case.update(audio=str(path.relative_to(ROOT)), audioSha256=digest(path))


async def main(args):
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    local = ROOT / '.local/qwen35-comparison'
    local.mkdir(parents=True, exist_ok=True)
    fresh = json.loads((ROOT / 'evals/local_voice_models/cases.json').read_text())
    regression = json.loads((ROOT / 'evals/voice_chat/v2/shipping.json').read_text())['cases']
    for case in fresh:
        case['set'] = 'fresh'
    for case in regression:
        case['set'] = 'regression'
    prepare(fresh, local / 'inputs')
    cases = fresh + regression
    if args.limit:
        cases = cases[:args.limit]
    before = {p: digest(ROOT/p) for p in WATCHED}
    result = {'date': '2026-11-04', 'models': {}, 'rows': [], 'warmups': [], 'beforeHashes': before,
              'settings': {'temperature': .3, 'num_ctx': 4096, 'num_predict': 256, 'think': False,
                           'ttsSpeed': .95, 'ttsVolume': .2, 'ttsCache': False},
              'environment': environment(), 'cases': cases, 'repeat': args.repeat,
              'scriptSha256': digest(__file__), 'modelOrdering': 'ABC then CBA, serial; one resident candidate; warmup excluded',
              'scope': 'Synthetic local comparison. Audio starts at complete input availability with known end boundary; excludes live VAD, browser, network, playback buffer. Text uses actual voice Bots with retrieval disabled and shared grounding retained. No prompt tuning.'}
    pool, calls = LLMHttpPool(), []
    original = local_llm._stream_reply

    async def tracked(*a, **kw):
        start, first, parts = time.perf_counter(), None, []
        record = {'purpose': kw.get('purpose'), 'complete': False}
        calls.append(record)
        stream = original(*a, **kw)
        try:
            async for delta in stream:
                if first is None:
                    first = (time.perf_counter()-start)*1000
                parts.append(delta)
                yield delta
            record['complete'] = True
        finally:
            await stream.aclose()
            record.update(firstTokenMs=first, totalMs=(time.perf_counter()-start)*1000, rawText=''.join(parts))

    def save():
        result['summary'] = summary(result['rows'])
        result['afterHashes'] = {p: digest(ROOT/p) for p in WATCHED}
        result['configurationUnchanged'] = result['afterHashes'] == before
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')

    try:
        with patch.dict(os.environ, environment()), use_llm_pool(pool), patch.object(local_llm, '_stream_reply', tracked):
            local_llm.endpoint()
            result['ollamaVersion'] = await api('version')
            tags = (await api('tags'))['models']
            result['models'] = {m: next(t for t in tags if t['name'] == m) for m in args.models}
            for phase in (['text', 'audio'] if args.mode == 'all' else [args.mode]):
                selected = cases if phase == 'text' else [c for c in cases if c.get('audioTest')]
                if phase == 'audio':
                    await worker('asr').request()
                    await worker('tts').request()
                for repeat in range(args.repeat):
                    ordered = args.models if repeat % 2 == 0 else args.models[::-1]
                    for model in ordered:
                        # No competing candidate resident during a measured block.
                        for item in (await api('ps'))['models']:
                            if item['name'] in MODELS and item['name'] != model:
                                await api('generate', {'model': item['name'], 'keep_alive': 0})
                        os.environ['VOICE_CHAT_MODEL'] = model
                        started = time.perf_counter()
                        await local_llm.complete([{'role':'user','content':'Reply with exactly this sentence: Hello.'}], purpose='voice_chat', max_tokens=128)
                        result['warmups'].append({'phase':phase,'repeat':repeat,'model':model,'ms':(time.perf_counter()-started)*1000,
                                                  'resident':await api('ps')})
                        directory = local / model.replace(':', '-')
                        directory.mkdir(exist_ok=True)
                        if phase == 'audio':
                            calls.clear()
                            await audio_case(selected[0], 'warmup', directory)
                        ordered_cases = list(selected)
                        random.Random(20261104 + repeat).shuffle(ordered_cases)
                        for i, case in enumerate(ordered_cases):
                            calls.clear()
                            row = {k:case[k] for k in ['id','set','category','route','grade']}
                            row.update(model=model, phase=phase, repeat=repeat)
                            started = time.perf_counter()
                            try:
                                row.update(await generate_pipeline(case) if phase == 'text' else await audio_case(case, repeat, directory))
                                row['score'] = score(case, row['text'])
                            except Exception as error:
                                row.update(error=type(error).__name__+': '+str(error), text='', totalMs=(time.perf_counter()-started)*1000,
                                           score={'passed':False,'failures':['runtime_error']})
                            row['calls'] = list(calls)
                            result['rows'].append(row)
                            save()
                            print(json.dumps({k:row.get(k) for k in ['phase','model','id','repeat','firstTextDeltaMs','firstAudioMs','totalMs','error']})+
                                  ' '+str(row['score']['passed']), flush=True)
    finally:
        await close_speech_workers()
        try:
            with use_llm_pool(pool):
                for model in args.models:
                    if model != 'qwen2.5:3b':
                        await api('generate', {'model':model,'keep_alive':0})
                # An empty generate request loads without producing a truncatable reply.
                await api('generate', {'model':'qwen2.5:3b','keep_alive':'30m','options':{'num_ctx':4096}})
        finally:
            await pool.close()
            save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='evals/local_voice_models/results.json')
    parser.add_argument('--models', nargs='+', choices=MODELS, default=MODELS)
    parser.add_argument('--mode', choices=['text','audio','all'], default='all')
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--limit', type=int)
    asyncio.run(main(parser.parse_args()))
