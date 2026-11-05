"""Offline experimental MLX LM worker with immutable instruction-prefix caches.

Only fixed instructions supplied by preparation are cached. Each generation
clones a matching token-prefix state; no question or answer is retained.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import time


def main(args):
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    import mlx.core as mx
    from mlx_lm import load, stream_generate
    from mlx_lm.models.cache import make_prompt_cache
    from mlx_lm.sample_utils import make_sampler, make_logits_processors
    model,tokenizer=load(args.model,trust_remote_code=False)
    prefixes=[]
    schema_backend=None

    def send(value):
        print(json.dumps(value,ensure_ascii=False),flush=True)

    def matching(tokens):
        choices=[p for p in prefixes if len(p['tokens'])<len(tokens)
                 and tokens[:len(p['tokens'])]==p['tokens']]
        return max(choices,key=lambda p:len(p['tokens'])) if choices else None

    def generation(request,*,cached=True,temp=None):
        nonlocal schema_backend
        if request.get('rendered_prompt') is not None:
            tokens=tokenizer.encode(request['rendered_prompt'],add_special_tokens=False)
        else:
            tokens=tokenizer.apply_chat_template(request['messages'],tokenize=True,
                add_generation_prompt=True,enable_thinking=False)
        if len(tokens)>4096:
            raise ValueError('Prompt exceeds experimental 4096-token context')
        chosen=matching(tokens) if cached else None
        prefix_tokens=chosen['tokens'] if chosen else []
        cache=copy.deepcopy(chosen['cache']) if chosen else make_prompt_cache(model)
        suffix=tokens[len(prefix_tokens):]
        processors=make_logits_processors(repetition_penalty=1.0,repetition_context_size=64,
            presence_penalty=1.5,presence_context_size=64)
        if prefix_tokens:
            # Preserve the same penalty context as full-prompt inference.
            history=mx.array(prefix_tokens[-64:])
            processors=[(lambda ids,logits,processor=p:processor(mx.concatenate([history,ids]),logits)) for p in processors]
        if request.get('json_mode'):
            if schema_backend is None:
                import outlines
                from outlines.backends.outlines_core import OutlinesCoreBackend
                schema_backend=OutlinesCoreBackend(outlines.from_mlxlm(model,tokenizer))
            schema=request['json_mode'] if isinstance(request['json_mode'],dict) else {'type':'object'}
            constraint=schema_backend.get_json_schema_logits_processor(json.dumps(schema))
            # Outlines 0.2.11's Metal mask uses a float -INFINITY literal;
            # applying it directly to BF16 logits fails Metal compilation.
            processors.append(lambda ids,logits:constraint(ids,logits.astype(mx.float32)))
        sampler=make_sampler(temp=request.get('temperature',.3) if temp is None else temp,
                            top_p=.95,top_k=20,min_p=0)
        stream=stream_generate(model,tokenizer,prompt=suffix,prompt_cache=cache,
            sampler=sampler,logits_processors=processors,max_tokens=request.get('max_tokens',256))
        return stream,len(tokens),len(prefix_tokens)

    send({'ready':True,'engine':'mlx-lm','activeMemoryBytes':mx.get_active_memory()})
    for line in sys.stdin:
        if not line.strip():continue
        request=json.loads(line);identity=request['id']
        try:
            if request.get('op')=='prepare':
                records=[]
                for text in sorted(set(request['prefixes']),key=len):
                    tokens=tokenizer.encode(text,add_special_tokens=False)
                    if not tokens or len(tokens)>2048:raise ValueError('Invalid fixed-prefix length')
                    if '<|im_start|>user' in text:raise ValueError('User messages cannot be cached')
                    begin=time.perf_counter();parent=matching(tokens)
                    cache=copy.deepcopy(parent['cache']) if parent else make_prompt_cache(model)
                    offset=len(parent['tokens']) if parent else 0
                    for start in range(offset,len(tokens),256):
                        model(mx.array(tokens[start:start+256])[None],cache=cache)
                        mx.eval([c.state for c in cache])
                    prefixes.append({'tokens':tokens,'cache':cache})
                    records.append({'sha256':hashlib.sha256(text.encode()).hexdigest(),'tokens':len(tokens),
                        'milliseconds':(time.perf_counter()-begin)*1000})
                send({'id':identity,'done':True,'prefixes':records,'activeMemoryBytes':mx.get_active_memory()})
                continue
            if request.get('op')=='parity':
                results=[]
                for cached in [False,True]:
                    stream,total,hit=generation(request,cached=cached,temp=0)
                    output=[]
                    for item in stream:output.append(item.token)
                    results.append(output)
                send({'id':identity,'done':True,'parity':results[0]==results[1],
                      'tokensCompared':len(results[0]),'cachedPrefixTokens':hit})
                continue
            if 'experimentSeed' in request:mx.random.seed(int(request['experimentSeed']))
            begin=time.perf_counter();stream,total,hit=generation(request)
            last=None
            for item in stream:
                last=item
                if item.text:send({'id':identity,'text':item.text})
            send({'id':identity,'done':True,'reason':last.finish_reason,
                  'promptTokens':total,'cachedPromptTokens':hit,
                  'promptEvalMs':last.prompt_tokens/last.prompt_tps*1000,
                  'generationMs':last.generation_tokens/last.generation_tps*1000,
                  'generatedTokens':last.generation_tokens,'wallMs':(time.perf_counter()-begin)*1000,
                  'activeMemoryBytes':mx.get_active_memory()})
        except Exception as error:
            print(type(error).__name__+': '+str(error),file=sys.stderr,flush=True)
            send({'id':identity,'done':True,'error':type(error).__name__+': '+str(error)})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',required=True)
    main(parser.parse_args())
