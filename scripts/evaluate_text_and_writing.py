"""Paired loopback-only evaluation of the text bots and actual composition services."""
import argparse
import asyncio
import json
import hashlib
import os
from pathlib import Path
import random
import time
from unittest.mock import patch

from backend import local_llm, natural_writing
from backend.chat_library import chat_library
from backend.curriculum_chat import CurriculumChatBot
from backend.english_chat import EnglishChatBot
from backend.llm_runtime import LLMHttpPool, Timing, llm_client, use_llm_pool, use_timing
from backend.text_bot import BotMessage, BotRequest
from backend.writing_assistant import WritingAssistant, WritingError
from backend.writing_workflows import model_essay

ROOT = Path(__file__).resolve().parents[1]
MODELS = ('qwen2.5:3b', 'qwen3.5:9b')
CHAT_UNIT = '5175e30ef6f5f8a4933bdd06'


async def run_case(kind, case):
    if kind == 'text_chat':
        options = {'grade': 6}
        if case['route'] == 'curriculum':
            scope = chat_library().scope(CHAT_UNIT)
            options = {'chapterId': CHAT_UNIT}
            bot = CurriculumChatBot(scope_factory=lambda _: scope)
        else:
            bot = EnglishChatBot()
        request = BotRequest(case['id'], case['id'], case['id'],
                             tuple(BotMessage(**m) for m in case['messages']), options=options, channel='text')
        parts, first, done = [], None, False
        started = time.perf_counter()
        async for event in bot.stream(request):
            if event.type == 'text_delta':
                if first is None:
                    first = (time.perf_counter() - started) * 1000
                parts.append(event.text)
            elif event.type == 'done':
                done = True
        return {'reply': ''.join(parts).strip(), 'firstMs': first, 'done': done}

    assistant = WritingAssistant()
    scope = assistant.scope(case['chapterId'])
    args = (scope, case['instruction'], case['wordCount'])
    drafts, calls = [], []
    invoke, stream = local_llm.LocalAgent.ainvoke, local_llm.stream_reply

    async def track_invoke(agent, state, *args, **kwargs):
        result = await invoke(agent, state, *args, **kwargs)
        drafts.append(result['messages'][-1]['content'])
        return result

    async def track_stream(messages, **kwargs):
        started = time.perf_counter()
        try:
            async for part in stream(messages, **kwargs):
                yield part
        finally:
            calls.append(round((time.perf_counter() - started)*1000, 2))

    with patch.object(local_llm.LocalAgent, 'ainvoke', track_invoke), patch.object(local_llm, 'stream_reply', track_stream):
        try:
            if case['mode'] == 'natural':
                result = await natural_writing.generate(*args)
            else:
                result = await model_essay(assistant, *args, case['mode'] == 'strict', search_first=False)
            output = {k: result[k] for k in ('title', 'essay', 'glossary', 'audit')}
        except WritingError as error:
            output = {'error': error.code, 'audit': (error.details or {}).get('audit')}
    return dict(output, rewrites=max(0, len(drafts)-1), drafts=drafts, callMs=calls)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('text_chat', 'short_essay'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = ROOT / 'evals' / args.kind / 'cases.json'
    cases = json.loads(source.read_text())
    random.Random(238).shuffle(cases)
    # Four blocks; swap model order for the second half to limit order bias.
    pool = LLMHttpPool()
    output = {'kind': args.kind, 'datasetSha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'runs': []}
    if args.kind == 'short_essay':
        output['scopeHashes'] = {key: hashlib.sha256('\n'.join(sorted(chat_library().scope(key).words)).encode()).hexdigest()
                                 for key in sorted({case['chapterId'] for case in cases})}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with use_llm_pool(pool), patch.dict(os.environ, {'CHAT_PROVIDER':'ollama', 'WRITING_PROVIDER':'ollama',
            'OLLAMA_NATIVE_BASE_URL':'http://127.0.0.1:11434', 'OLLAMA_WRITING_THINK':'0'}):
            async with llm_client() as client:
                async with client.get('http://127.0.0.1:11434/api/tags') as response:
                    response.raise_for_status()
                    tags = await response.json()
            output['models'] = {m['name']: m['digest'] for m in tags['models'] if m['name'] in MODELS}
            if len(output['models']) != len(MODELS):
                raise ValueError('Both pinned local model tags must be installed')
            half = len(cases)//2
            for batch, models in ((cases[:half], MODELS), (cases[half:], MODELS[::-1])):
                for model in models:
                    with patch.dict(os.environ, {'CHAT_MODEL':model, 'WRITING_MODEL':model}):
                        await local_llm.complete([{'role':'user','content':'Say hello.'}], max_tokens=32)
                        for case in batch:
                            probe = Timing(case['id'])
                            with use_timing(probe):
                                try:
                                    result = await run_case(args.kind, case)
                                except Exception as error:
                                    result = {'error': type(error).__name__, 'message': str(error)}
                            run = dict(result, model=model, id=case['id'],
                                       totalMs=round((time.perf_counter()-probe.started)*1000, 2), timing=probe.metrics)
                            output['runs'].append(run)
                            args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n')
                            print(model, case['id'], run['totalMs'], result.get('rewrites'), result.get('error'), flush=True)
    finally:
        await pool.close()


if __name__ == '__main__':
    asyncio.run(main())
