"""Compare an experimental 4B backend against unchanged 3B/4B voice paths.

Run in the frozen optimization workspace with its prepared 80-case manifest.
Configuration changes are confined to this evaluation process.
"""
import argparse
import asyncio
import json
from pathlib import Path
import os
from unittest.mock import patch

from scripts import compare_local_voice_models as comparison

MODELS=['qwen2.5:3b','qwen3.5:4b','qwen3.5:4b-mlx']


async def main(args):
    comparison.MODELS=list(args.models)
    original=comparison.local_llm.complete
    priming=[]
    async def complete(messages,**kwargs):
        result=await original(messages,**kwargs)
        # Only model-block preparation; never the actual question/answer path.
        if (args.prime_prefixes and os.getenv('VOICE_CHAT_MODEL')=='qwen3.5:4b-mlx'
                and messages==[{'role':'user','content':'Reply with exactly this sentence: Hello.'}]):
            from scripts.prime_local_voice_model import prime_voice_prefixes
            cases=json.loads((comparison.ROOT/'evals/local_voice_models/cases.json').read_text())
            grades=sorted({case['grade'] for case in cases})
            priming.extend(await prime_voice_prefixes('qwen3.5:4b-mlx',grades,include_curriculum=True))
        return result
    with patch.object(comparison.local_llm,'complete',complete):
        await comparison.main(args)
    path=Path(args.output)
    if not path.is_absolute():path=comparison.ROOT/path
    data=json.loads(path.read_text())
    for group in [data['cases'],data['rows']]:
        for row in group:
            row['set']=('holdout' if row['id'].startswith('opt-hold-') else
                        'development' if row['id'].startswith('q35-') else 'regression')
    data['experiment']='Runtime/backend comparison; complete prompts, Bot guards, ASR, TTS, explicit sampling options and output limits unchanged.'
    data['prefixPriming']=priming
    data['prefixPrimingEnabled']=args.prime_prefixes
    data['executionRoot']=str(comparison.ROOT)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models',nargs='+',default=MODELS)
    parser.add_argument('--output',required=True)
    parser.add_argument('--mode',choices=['all','text','audio'],default='all')
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--limit',type=int)
    parser.add_argument('--prime-prefixes',action='store_true')
    asyncio.run(main(parser.parse_args()))
