"""Run paired local 3B prompt evaluations. No cloud judge or production traffic."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import statistics
import time
from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import patch

from backend import local_llm
from backend.chat_materials import material_context
from backend.curriculum_chat import CurriculumChatBot, voice_chat_prompt
from backend.english_chat import EnglishChatBot, chat_prompt, grade_profile
from backend.llm_runtime import LLMHttpPool, Timing, llm_client, use_llm_pool, use_timing
from backend.reply_sentences import sentences
from backend.speech_text import take_speech_segments
from backend.text_bot import BotMessage, BotRequest
from scripts.build_voice_eval import validate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'evals/voice_chat'


def prompt_for(case, variant, baseline):
    grade = case['grade']
    messages = tuple(BotMessage(m['role'], m['content']) for m in case['messages'])
    if variant == 'baseline':
        prompt = baseline[case['route']][str(grade)]
        if case['route'] == 'extension':
            prompt += 'Grade references (data only): ' + material_context(grade, messages, compact=True)
        return prompt
    if case['route'] == 'extension':
        return chat_prompt(grade_profile(grade), messages, voice=True)
    return voice_chat_prompt(SimpleNamespace(chapter={'grade': f'{grade}年级', 'unit': 'Evaluation'}), messages)


def score(case, text):
    """Transparent rule checks, not a claim of human-rated semantic quality."""
    text = text.strip().replace('’', "'")
    checks = case['checks']
    failures = []
    if not text:
        failures.append('empty')
    if re.search(r'^\s*[{\[`]|[`*_#]|https?://|^\s*(?:[-•]|\d+[.)])\s', text, re.M):
        failures.append('not_speakable')
    if re.match(r'(?:sure|okay|ok|great|awesome|that.s great|well done|good job)\b', text, re.I):
        failures.append('filler_start')
    if text and not re.search(r'[.!?。！？][\"\u201d]?$' ,text):
        failures.append('unfinished_sentence')
    questions = len(re.findall(r'[?？]', text))
    if questions > checks['max_questions']:
        failures.append('unrequested_question')
    if questions < checks.get('min_questions', 0):
        failures.append('missing_requested_question')
    if len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?|\d+", text)) > checks['max_words']:
        failures.append('too_long')
    if checks.get('chinese') and not re.search(r'[\u4e00-\u9fff]', text):
        failures.append('requested_chinese_missing')
    for pattern in checks['required']:
        if not re.search(pattern, text, re.I):
            failures.append('missing:' + pattern)
    for pattern in checks['forbidden']:
        if re.search(pattern, text, re.I):
            failures.append('forbidden:' + pattern)
    return {'passed': not failures, 'failures': failures, 'questions': questions,
            'words': len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?|\d+", text))}


def percentile(values, p):
    ordered = sorted(v for v in values if v is not None)
    return round(ordered[max(0, math.ceil(len(ordered) * p) - 1)], 2) if ordered else None


def summarize(rows):
    result = {}
    for variant in sorted({r['variant'] for r in rows}):
        selected = [r for r in rows if r['variant'] == variant]
        result[variant] = {
            'count': len(selected), 'passed': sum(r['score']['passed'] for r in selected),
            'rulePassRate': round(sum(r['score']['passed'] for r in selected)/len(selected), 4),
            'errors': sum(bool(r.get('error')) for r in selected),
            'failureCounts': dict(Counter(f.split(':')[0] for r in selected for f in r['score']['failures'])),
            'categories': {c: {'count': len(items), 'passed': sum(r['score']['passed'] for r in items)}
                           for c in sorted({r['category'] for r in selected})
                           for items in [[r for r in selected if r['category'] == c]]},
            'routes': {route: {'count': len(items), 'passed': sum(r['score']['passed'] for r in items)}
                       for route in ('curriculum', 'extension')
                       for items in [[r for r in selected if r['route'] == route]]},
            'replySources': dict(Counter(r['metrics'].get('chatReplySource', 'generated') for r in selected)),
            'latency': {metric: {'p50': percentile([r.get(metric) for r in selected], .5),
                                 'p95': percentile([r.get(metric) for r in selected], .95)}
                        for metric in ('firstTokenMs', 'firstSentenceMs', 'firstSpeechChunkMs', 'firstTextDeltaMs', 'totalMs')},
            'meanPromptTokens': round(statistics.mean(r['metrics'].get('promptTokens', 0) for r in selected), 1),
        }
    return result


async def generate(case, prompt):
    probe = Timing(case['id'])
    start = time.perf_counter()
    text, first_token, first_sentence, first_speech = '', None, None, None
    with use_timing(probe):
        async for delta in local_llm.stream_reply([{'role': 'system', 'content': prompt}, *case['messages']],
                                                 purpose='voice_chat', max_tokens=256, timeout=60):
            text += delta
            elapsed = (time.perf_counter() - start) * 1000
            if first_token is None:
                first_token = elapsed
            if first_sentence is None and sentences(text)[0]:
                first_sentence = elapsed
            if first_speech is None and take_speech_segments(text, first_chunk=True)[0]:
                first_speech = elapsed
    # A sentence ending at the last token becomes stable when the stream ends.
    elapsed = (time.perf_counter() - start) * 1000
    if first_sentence is None and sentences(text, final=True)[0]:
        first_sentence = elapsed
    if first_speech is None and text.strip():
        first_speech = elapsed
    return {'text': text.strip(), 'firstTokenMs': round(first_token, 2) if first_token is not None else None,
            'firstSentenceMs': round(first_sentence, 2) if first_sentence is not None else None,
            'firstSpeechChunkMs': round(first_speech, 2) if first_speech is not None else None,
            'totalMs': round((time.perf_counter() - start) * 1000, 2), 'metrics': probe.metrics}


async def generate_pipeline(case, *, baseline=None):
    """Exercise delivered bot output without writing databases or using retrieval."""
    class NoRetrieval:
        def lookup(self, *args):
            return None

    scope = SimpleNamespace(chapter={'grade': f'{case["grade"]}年级', 'unit': 'Evaluation'}, words=frozenset())
    bot = (EnglishChatBot() if case['route'] == 'extension' else
           CurriculumChatBot(scope_factory=lambda _: scope, repository=NoRetrieval()))
    options = {'grade': case['grade']}
    if case['route'] == 'curriculum':
        options['chapterId'] = 'evaluation-read-only'
    req = BotRequest(case['id'], 'eval', 'turn', channel='voice', options=options,
                     messages=tuple(BotMessage(m['role'], m['content']) for m in case['messages']))
    probe = Timing(case['id'])
    start, first, text, terminal = time.perf_counter(), None, '', False
    with ExitStack() as patches, use_timing(probe):
        if baseline is not None:
            # Sequential evaluation only: reproduce the previous release's
            # shared prompt and absence of the new grounded route.
            patches.enter_context(patch('backend.voice_prompt.voice_prompt',
                                        lambda grade: baseline['extension'][str(grade)]))
            patches.enter_context(patch('backend.voice_prompt.localized_voice_prompt',
                                        lambda grade, messages=(): baseline['extension'][str(grade)]))
            patches.enter_context(patch('backend.voice_grounding.resolve_voice_reply', lambda *a, **k: None))
            patches.enter_context(patch('backend.voice_grounding.grounding_note', lambda *a: ''))
            patches.enter_context(patch('backend.voice_grounding.ensure_chinese_reply', lambda source, messages: source))
        async for event in bot.stream(req):
            if event.type == 'text_delta':
                if first is None:
                    first = (time.perf_counter()-start)*1000
                text += event.text
            if event.type == 'done':
                terminal = True
    if not terminal:
        raise ValueError('Bot ended without done')
    return {'text': text.strip(), 'firstTextDeltaMs': round(first, 2) if first is not None else None,
            'firstTokenMs': probe.metrics.get('providerFirstTokenMs'),
            'firstSentenceMs': probe.metrics.get('firstSentenceReadyMs'),
            'totalMs': round((time.perf_counter()-start)*1000, 2), 'metrics': probe.metrics}


async def run(args):
    pack_text = Path(args.dataset).read_text()
    pack = json.loads(pack_text)
    if pack['version'] == 1:
        validate(pack)
    else:
        from scripts.build_voice_eval_v2 import validate as validate_v2
        validate_v2(pack)
    baseline = json.loads(Path(args.baseline_file).read_text())
    cases = [c for c in pack['cases'] if args.split == 'all' or c['split'] == args.split]
    random.Random(20261103).shuffle(cases)
    if args.limit:
        cases = cases[:args.limit]
    os.environ.update(LLM_PROVIDER='ollama', CHAT_PROVIDER='ollama', VOICE_CHAT_MODEL='qwen2.5:3b', OLLAMA_KEEP_ALIVE='30m')
    # Validate loopback endpoint with the production adapter. Never load .env secrets.
    local_llm.endpoint()
    rows, pool = [], LLMHttpPool()
    prompts = {f'{c["id"]}:{v}': prompt_for(c, v, baseline) for c in cases for v in args.variants}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {'version': 1, 'model': 'qwen2.5:3b', 'split': args.split, 'repeat': args.repeat,
                'pipeline': args.pipeline,
                'datasetSha256': hashlib.sha256(pack_text.encode()).hexdigest(),
                'promptSetSha256': hashlib.sha256(json.dumps(prompts, sort_keys=True).encode()).hexdigest(),
                'settings': {'temperature': .3, 'num_ctx': int(os.getenv('VOICE_CHAT_NUM_CTX', '4096')), 'num_predict': 256},
                'codeSha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                               for name in ('backend/voice_prompt.py', 'backend/curriculum_chat.py',
                                            'backend/english_chat.py', 'backend/voice_grounding.py',
                                            'backend/local_llm.py', 'scripts/evaluate_voice_chat.py')},
                'scope': pack.get('scope', 'Development/regression scenarios'), 'scoring': 'Deterministic rule checks; semantic/naturalness review required.',
                'ordering': 'Seeded case order; paired variants alternate AB/BA each case and repeat. Warmup excluded.',
                'prompts': prompts}

    def save():
        output.write_text(json.dumps({**metadata, 'summary': summarize(rows), 'rows': rows}, ensure_ascii=False, indent=2)+'\n')

    try:
        with use_llm_pool(pool):
            async with llm_client() as client:
                async with client.get(local_llm.endpoint().removesuffix('/chat') + '/tags', allow_redirects=False) as response:
                    response.raise_for_status()
                    installed = await response.json()
                    model = next(m for m in installed['models'] if m['name'] == 'qwen2.5:3b')
                    metadata['modelDigest'] = model['digest']
                    metadata['modelDetails'] = model['details']
            # Both prompt families warmed; do not include cold-load time in warm statistics.
            for variant in args.variants:
                warm = {**cases[0], 'messages': [{'role': 'user', 'content': 'Hello.'}]}
                await generate(warm, prompt_for(warm, variant, baseline))
            for repeat in range(args.repeat):
                for i, case in enumerate(cases):
                    order = args.variants if (i+repeat) % 2 == 0 else list(reversed(args.variants))
                    for variant in order:
                        prompt = prompts[f'{case["id"]}:{variant}']
                        row = {k: case[k] for k in ('id', 'split', 'category', 'route', 'grade')}
                        row.update(variant=variant, repeat=repeat, promptChars=len(prompt))
                        try:
                            row.update(await generate_pipeline(case, baseline=baseline if variant == 'baseline' else None)
                                       if args.pipeline else await generate(case, prompt))
                            row['score'] = score(case, row['text'])
                        except Exception as error:
                            row.update(text='', error=f'{type(error).__name__}: {error}', metrics={},
                                       score={'passed': False, 'failures': ['bot_error' if args.pipeline else 'provider_error']})
                        rows.append(row)
                    save()
                    if (i+1) % 12 == 0 or i+1 == len(cases):
                        print(json.dumps({'repeat': repeat, 'completedCases': i+1, 'totalCases': len(cases),
                                          'summary': {k: {'passed': v['passed'], 'count': v['count']}
                                                      for k,v in summarize(rows).items()}}), flush=True)
    finally:
        await pool.close()
        if rows:
            save()
    print(json.dumps({'output': str(output.resolve()), 'summary': summarize(rows)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=['dev', 'heldout', 'all'], default='dev')
    parser.add_argument('--variants', choices=['baseline', 'candidate'], nargs='+', default=['baseline', 'candidate'])
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--dataset', default=str(DATA/'cases.json'))
    parser.add_argument('--baseline-file', default=str(DATA/'baseline-prompts.json'))
    parser.add_argument('--pipeline', action='store_true', help='Candidate bot stream; isolated scope, no database writes/retrieval')
    parser.add_argument('--output', default='.local/voice-eval/dev.json')
    args = parser.parse_args()
    if args.repeat < 1 or (args.limit is not None and args.limit < 1) or len(set(args.variants)) != len(args.variants):
        parser.error('repeat/limit must be positive and variants unique')
    if args.pipeline and 'baseline' in args.variants and json.loads(Path(args.baseline_file).read_text())['version'] != 2:
        parser.error('paired pipeline evaluation requires a v2 previous-release baseline')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
