"""Isolated comparison of native cached-prefix 4B with unchanged Ollama 3B/4B."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import random
import time
from unittest.mock import patch

from scripts import compare_local_voice_models as base
from scripts.mlx_voice_experiment import NativeWorker,MODEL
from backend.llm_runtime import LLMHttpPool,use_llm_pool
from backend.providers.speech_worker import worker,close_speech_workers


async def main(args):
    root=Path(__file__).resolve().parents[1]
    outer=Path(args.outer).resolve();output=Path(args.output).resolve()
    cases=json.loads((root/'evals/local_voice_models/cases.json').read_text())
    regression=json.loads((root/'evals/voice_chat/v2/shipping.json').read_text())['cases']
    cases+=regression
    for case in cases:
        case['set']=('acceptance' if case['id'].startswith('opt-accept-') else
                     'prior_holdout' if case['id'].startswith('opt-hold-') else
                     'development' if case['id'].startswith('q35-') else 'regression')
    base.prepare(cases,root/'.local/qwen35-comparison/inputs')
    before={name:base.digest(root/name) for name in base.WATCHED}
    native=NativeWorker(outer/'.local/llm-mlx-venv/bin/python',outer/'.local/qwen35-optimization/affine-model',
                       root/'scripts/local_llm_mlx_worker.py',outer/'.local/qwen35-optimization/native-final-worker.log')
    pool=LLMHttpPool();calls=[];original=base.local_llm._stream_reply
    data={'date':'2026-11-04','executionRoot':str(root),'rows':[],'cases':cases,'warmups':[],
          'models':{},'beforeHashes':before,'scope':'Fixed source, exact original prompts and guards; native MLX uses only fixed system-prefix states. Every request clones state; no question/answer caching. First and repeated requests reported separately.',
          'settings':{'temperature':.3,'context':4096,'maxTokens':256,'thinking':False,'ttsCache':False,'ttsSpeed':.95,'ttsVolume':.2}}
    if args.reference_text:
        reference_path=Path(args.reference_text)
        reference=json.loads(reference_path.read_text())
        assert reference['beforeHashes']==before,'Reference product code changed'
        old_cases={c['id']:c for c in reference['cases']}
        for case in cases:
            assert all(case[k]==old_cases[case['id']][k] for k in ['messages','grade','route','checks'])
        inherited=[r for r in reference['rows'] if r['phase']=='text' and r['model']!=MODEL
                   and r['model'] in args.models and r['repeat']<args.repeat]
        data['rows'].extend(inherited)
        inherited_keys={(r['model'],r['repeat']) for r in inherited}
        data['warmups'].extend(w for w in reference['warmups']
                               if w['phase']=='text' and (w['model'],w['repeat']) in inherited_keys)
        data['referenceText']={'path':str(reference_path),'sha256':hashlib.sha256(reference_path.read_bytes()).hexdigest(),
                               'count':len(inherited),'scope':'Unchanged reference-model text blocks retained from the preceding run; only native adapter code was repaired.'}

    async def tracked(messages,**kwargs):
        begin=time.perf_counter();first=None;parts=[];record={'purpose':kwargs.get('purpose'),'complete':False};calls.append(record)
        source=(native.stream(messages,**kwargs) if os.getenv('VOICE_CHAT_MODEL')==MODEL else original(messages,**kwargs))
        try:
            async for part in source:
                if first is None:first=(time.perf_counter()-begin)*1000
                parts.append(part);yield part
            record['complete']=True
        finally:
            await source.aclose();record.update(firstTokenMs=first,totalMs=(time.perf_counter()-begin)*1000,rawText=''.join(parts))

    def save():
        data['summary']=base.summary(data['rows'])
        data['afterHashes']={name:base.digest(root/name) for name in base.WATCHED}
        data['configurationUnchanged']=before==data['afterHashes']
        output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

    try:
        with patch.dict(os.environ,base.environment()),use_llm_pool(pool),patch.object(base.local_llm,'_stream_reply',tracked):
            tags=(await base.api('tags'))['models']
            data['models']={m:next(t for t in tags if t['name']==m) for m in args.models if m!=MODEL}
            cfg=json.loads((outer/'.local/qwen35-optimization/affine-model/config.json').read_text())
            data['models'][MODEL]={'name':MODEL,'engine':'mlx-lm 0.32.2 / mlx 0.32.4','format':'safetensors',
                'source':'mlx-community/Qwen3.5-4B-4bit','revision':'0e7ffd5c629ef7719d4cbc04069232580bfa9d9c',
                'weightsSha256':'5fb9acd0246866381cf8c5c354c6db1019f6498eec4ccb4f5edcc71ffeacb2db',
                'quantization':cfg.get('quantization',cfg.get('quantization_config')),
                'sampler':{'temperature':.3,'top_k':20,'top_p':.95,'presence_penalty':1.5,'presence_context_size':64,'repeat_penalty':1}}
            grades=sorted({c['grade'] for c in cases})
            for phase in ['text','audio']:
                selected=cases if phase=='text' else [c for c in cases if c.get('audioTest')]
                if phase=='audio':await worker('asr').request();await worker('tts').request()
                for repeat in range(args.repeat):
                    for model in (args.models if repeat%2==0 else args.models[::-1]):
                        if sum(r['phase']==phase and r['model']==model and r['repeat']==repeat for r in data['rows'])==len(selected):
                            print('REUSE_REFERENCE',model,phase,repeat,len(selected),flush=True)
                            continue
                        await native.close()
                        for item in (await base.api('ps'))['models']:
                            if item['name'] in ['qwen2.5:3b','qwen3.5:4b','qwen3.5:4b-mlx']:
                                await base.api('generate',{'model':item['name'],'keep_alive':0})
                        os.environ['VOICE_CHAT_MODEL']=model
                        start=time.perf_counter()
                        await base.local_llm.complete([{'role':'user','content':'Reply with exactly this sentence: Hello.'}],purpose='voice_chat',max_tokens=128)
                        warm={'model':model,'phase':phase,'repeat':repeat}
                        if model==MODEL:warm['prefixPreparation']=await native.prepare(grades)
                        warm['milliseconds']=(time.perf_counter()-start)*1000;data['warmups'].append(warm)
                        directory=root/'.local/native-voice-output'/model.replace(':','-');directory.mkdir(parents=True,exist_ok=True)
                        if phase=='audio':await base.audio_case(selected[0],'warmup',directory)
                        ordered=list(selected);random.Random(20261104+repeat).shuffle(ordered)
                        for case in ordered:
                            calls.clear();start=time.perf_counter()
                            row={k:case[k] for k in ['id','set','category','route','grade']}
                            row.update(model=model,phase=phase,repeat=repeat)
                            try:
                                row.update(await base.generate_pipeline(case) if phase=='text' else await base.audio_case(case,repeat,directory))
                                row['score']=base.score(case,row['text'])
                            except Exception as error:
                                row.update(error=type(error).__name__+': '+str(error),text='',metrics={},totalMs=(time.perf_counter()-start)*1000,
                                           score={'passed':False,'failures':['runtime_error']})
                            row['calls']=list(calls);data['rows'].append(row);save()
                            print(model,phase,repeat,case['id'],round(row.get('firstAudioMs') or row.get('firstTextDeltaMs') or row['totalMs']),row.get('error'),flush=True)
    finally:
        await native.close();await close_speech_workers()
        with use_llm_pool(pool):
            for model in ['qwen3.5:4b','qwen3.5:4b-mlx']:await base.api('generate',{'model':model,'keep_alive':0})
            await base.api('generate',{'model':'qwen2.5:3b','keep_alive':'30m','options':{'num_ctx':4096}})
        await pool.close();save()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--outer',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--models',nargs='+',default=['qwen2.5:3b','qwen3.5:4b',MODEL])
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--reference-text')
    asyncio.run(main(parser.parse_args()))
