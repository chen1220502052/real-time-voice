"""Loopback-only diagnostic of prefill, decoding and batch size, with fixed prompts."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

import aiohttp
from scripts.evaluate_voice_chat import prompt_for, percentile

ROOT=Path(__file__).resolve().parents[1]


async def main(args):
    cases=json.loads((ROOT/'evals/local_voice_models/cases.json').read_text())
    chosen=[c for c in cases if c['id'] in {'q35-05','q35-08','q35-12','q35-23'}]
    output=Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    data={'scope':'Diagnostic raw model requests, same full voice prompts; excludes Bot/TTS and warmup. No production configuration changes.',
          'rows':[],'cases':chosen}
    async with aiohttp.ClientSession() as client:
        async def generate(payload):
            begin=time.perf_counter();first=None;parts=[];done=None;thinking=''
            async with client.post('http://127.0.0.1:11434/api/chat',json=payload) as response:
                response.raise_for_status()
                async for line in response.content:
                    if not line.strip():continue
                    event=json.loads(line)
                    if event.get('error'):raise RuntimeError(event['error'])
                    message=event.get('message',{})
                    thinking+=message.get('thinking','')
                    if message.get('content'):
                        if first is None:first=(time.perf_counter()-begin)*1000
                        parts.append(message['content'])
                    if event.get('done'):done=event
            if done is None:raise ValueError('Incomplete stream')
            return {'firstTokenMs':first,'totalMs':(time.perf_counter()-begin)*1000,'text':''.join(parts),'thinkingChars':len(thinking),
                    **{k:v for k,v in done.items() if k not in {'message','context','model','created_at'}}}
        try:
            for model in args.models:
                for batch in args.batches:
                    options={'temperature':.3,'num_ctx':4096,'num_predict':256,'num_batch':batch}
                    warm={'model':model,'messages':[{'role':'user','content':'Reply only with Hello.'}],
                          'stream':True,'think':False,'keep_alive':'30m','options':options}
                    await generate(warm)
                    for repeat in range(args.repeat):
                        for case in chosen:
                            prompt=prompt_for(case,'current',{})
                            payload={**warm,'messages':[{'role':'system','content':prompt},*case['messages']]}
                            row={'model':model,'batch':batch,'repeat':repeat,'id':case['id']}
                            try:row.update(await generate(payload))
                            except Exception as error:row['error']=type(error).__name__+': '+str(error)
                            data['rows'].append(row)
                            output.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
                            print(model,batch,case['id'],round(row.get('firstTokenMs') or 0),
                                  round(row.get('prompt_eval_duration',0)/1e6),row.get('error'),flush=True)
        finally:
            for model in args.models:
                if model!='qwen2.5:3b':
                    async with client.post('http://127.0.0.1:11434/api/generate',json={'model':model,'keep_alive':0}) as response:await response.read()
            async with client.post('http://127.0.0.1:11434/api/generate',json={'model':'qwen2.5:3b','keep_alive':'30m','options':{'num_ctx':4096}}) as response:await response.read()
    for model in args.models:
        for batch in args.batches:
            rows=[r for r in data['rows'] if r['model']==model and r['batch']==batch]
            print('SUMMARY',model,batch,'firstTokenMs',percentile([r.get('firstTokenMs') for r in rows],.5),
                  'promptMs',percentile([r.get('prompt_eval_duration',0)/1e6 for r in rows],.5),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models',nargs='+',default=['qwen3.5:4b'])
    parser.add_argument('--batches',nargs='+',type=int,default=[128,512,1024])
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--output',required=True)
    asyncio.run(main(parser.parse_args()))
