"""Precompute fixed voice instruction prefixes, without questions or answers.

This is opt-in preparation for a local MLX model. It never edits configuration.
"""
import argparse
import asyncio
import hashlib
import time
from types import SimpleNamespace

from backend import local_llm
from backend.curriculum_chat import voice_chat_prompt
from backend.english_chat import chat_prompt, grade_profile
from backend.llm_runtime import LLMHttpPool, llm_client, use_llm_pool
from backend.text_bot import BotMessage


async def prime_voice_prefixes(model, grades, *, include_curriculum=False, unit='Evaluation'):
    base=local_llm.endpoint().removesuffix('/chat')
    records=[]
    chinese=(BotMessage('user','请用中文回答。'),)
    async with llm_client() as client:
        async def post(path,payload):
            async with client.post(base+'/'+path,json=payload,allow_redirects=False) as response:
                response.raise_for_status()
                return await response.json()
        for grade in sorted(set(grades)):
            profile=grade_profile(grade)
            for language,messages in [('en',()),('zh',chinese)]:
                prompts=[('extension',chat_prompt(profile,messages,voice=True))]
                if include_curriculum:
                    scope=SimpleNamespace(chapter={'grade':f'{grade}年级','unit':unit})
                    prompts.append(('curriculum',voice_chat_prompt(scope,messages)))
                for route,prompt in prompts:
                    # Ask the model's actual renderer for the system header.
                    # The dummy user is never included in the cached prefix.
                    marker='PREFIX_RENDER_SENTINEL_NOT_A_STUDENT_QUESTION'
                    rendered=await post('chat',{'model':model,'messages':[
                        {'role':'system','content':prompt},{'role':'user','content':marker}],
                        'think':False,'stream':False,'_debug_render_only':True})
                    template=rendered['_debug_info']['rendered_template']
                    position=template.find(prompt)
                    if position<0:
                        raise ValueError('Renderer did not preserve fixed instructions')
                    prefix=template[:position]+prompt.rstrip()
                    if marker in prefix or '<|im_start|>user' in prefix:
                        raise ValueError('Refusing to prime user messages')
                    started=time.perf_counter()
                    result=await post('generate',{'model':model,'prompt':prefix,'raw':True,
                        'think':False,'stream':False,'keep_alive':'30m',
                        'options':{'temperature':.3,'num_ctx':4096,'num_predict':1}})
                    if result.get('error') or not result.get('done'):
                        raise ValueError('Prefix preparation did not complete')
                    records.append({'model':model,'grade':grade,'language':language,'route':route,
                        'prefixChars':len(prefix),'prefixSha256':hashlib.sha256(prefix.encode()).hexdigest(),
                        'milliseconds':(time.perf_counter()-started)*1000,
                        'promptTokens':result.get('prompt_eval_count'),
                        'generatedTokensDiscarded':result.get('eval_count')})
    return records


async def main(args):
    pool=LLMHttpPool()
    try:
        with use_llm_pool(pool):
            rows=await prime_voice_prefixes(args.model,args.grades,
                include_curriculum=args.curriculum,unit=args.unit)
            for row in rows:
                print(row['grade'],row['language'],row['route'],round(row['milliseconds']),'ms')
    finally:
        await pool.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',default='qwen3.5:4b-mlx')
    parser.add_argument('--grades',nargs='+',type=int,default=[6])
    parser.add_argument('--curriculum',action='store_true')
    parser.add_argument('--unit',default='Evaluation')
    asyncio.run(main(parser.parse_args()))
