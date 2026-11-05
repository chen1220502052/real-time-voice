"""Process adapter used only by the opt-in native MLX evaluation."""
import asyncio
import json
import os
import time
from pathlib import Path

from backend.llm_runtime import timing,llm_client
from backend.english_chat import chat_prompt,grade_profile
from backend.curriculum_chat import voice_chat_prompt
from backend.voice_prompt import localized_voice_prompt
from backend.text_bot import BotMessage
from types import SimpleNamespace

MODEL='qwen3.5:4b-native-mlx-affine4'


class NativeWorker:
    def __init__(self,python,model,script,log):
        self.python,self.model,self.script,self.log_path=map(str,[python,model,script,log])
        self.process=None;self.sequence=0;self.log=None;self.lock=asyncio.Lock()

    async def start(self):
        if self.process and self.process.returncode is None:return
        self.log=open(self.log_path,'ab')
        self.process=await asyncio.create_subprocess_exec(self.python,'-u',self.script,'--model',self.model,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=self.log,limit=8*1024*1024)
        async with asyncio.timeout(120):ready=json.loads(await self.process.stdout.readline())
        if not ready.get('ready'):raise RuntimeError('Native MLX worker not ready')

    async def begin(self,payload):
        await self.start();self.sequence+=1
        self.process.stdin.write(json.dumps({'id':self.sequence,**payload},ensure_ascii=False).encode()+b'\n')
        await self.process.stdin.drain();return self.sequence

    async def read(self,identity,timeout=90):
        async with asyncio.timeout(timeout):line=await self.process.stdout.readline()
        if not line:raise RuntimeError('Native MLX worker closed')
        event=json.loads(line)
        if event['id']!=identity:raise RuntimeError('Native MLX sequence mismatch')
        return event

    async def request(self,payload):
        async with self.lock:
            identity=await self.begin(payload)
            while True:
                event=await self.read(identity)
                if event.get('error'):raise RuntimeError(event['error'])
                if event.get('done'):return event

    async def prepare(self,grades):
        prompts=[]
        for grade in sorted(set(grades)):
            for messages in [(),(BotMessage('user','请用中文回答。'),)]:
                scope=SimpleNamespace(chapter={'grade':f'{grade}年级','unit':'Evaluation'})
                texts=[localized_voice_prompt(grade,messages),chat_prompt(grade_profile(grade),messages,voice=True),voice_chat_prompt(scope,messages)]
                prompts.extend('<|im_start|>system\n'+text.rstrip() for text in texts)
        return await self.request({'op':'prepare','prefixes':prompts})

    async def render(self,messages,json_mode=False):
        # The app supplies dynamic policy as additional system messages.
        # The HF reference Jinja template rejects these; use the original
        # Ollama renderer so neither roles nor instruction order are changed.
        from backend.local_llm import endpoint
        payload={'model':'qwen3.5:4b','messages':messages,'think':False,
                 'stream':False,'_debug_render_only':True,'options':{'num_ctx':4096}}
        if json_mode:payload['format']=json_mode if isinstance(json_mode,dict) else 'json'
        async with llm_client() as client:
            async with client.post(endpoint(),json=payload,allow_redirects=False) as response:
                response.raise_for_status();result=await response.json()
        rendered=result.get('_debug_info',{}).get('rendered_template')
        if not isinstance(rendered,str):raise RuntimeError('Original model renderer unavailable')
        return rendered

    async def stream(self,messages,*,purpose,json_mode,max_tokens,timeout):
        if purpose!='voice_chat':raise ValueError('Experimental worker supports voice requests only')
        probe=timing()
        if probe:
            probe.mark('providerRequestMs')
            probe.metrics.update(promptChars=sum(len(m['content']) for m in messages),
                systemPromptChars=sum(len(m['content']) for m in messages if m['role']=='system'),
                historyMessages=sum(m['role']!='system' for m in messages),connectionReused=self.process is not None)
        async with self.lock:
            deadline=time.monotonic()+timeout
            async with asyncio.timeout(timeout):rendered=await self.render(messages,json_mode)
            identity=await self.begin({'messages':messages,'rendered_prompt':rendered,
                                      'max_tokens':max_tokens,'temperature':.3,'json_mode':json_mode})
            terminal=False
            try:
                while True:
                    event=await self.read(identity,max(.001,deadline-time.monotonic()))
                    if event.get('text'):
                        if probe:probe.mark('providerFirstTokenMs')
                        yield event['text']
                    if event.get('done'):
                        terminal=True
                        if event.get('error'):raise RuntimeError(event['error'])
                        if probe:
                            probe.metrics.update(modelLoadMs=0,promptEvalMs=event['promptEvalMs'],
                                tokenGenerationMs=event['generationMs'],promptTokens=event['promptTokens'],
                                cachedPromptTokens=event['cachedPromptTokens'],generatedTokens=event['generatedTokens'])
                        if event['reason']!='stop':raise ValueError('Local model response was truncated')
                        break
            except TimeoutError:
                terminal=True
                await self.close()
                raise
            finally:
                # A Bot can stop after its third sentence; drain the worker so
                # the next request cannot receive an obsolete continuation.
                if not terminal:
                    try:
                        async with asyncio.timeout(5):
                            while True:
                                if (await self.read(identity)).get('done'):break
                    except (TimeoutError,RuntimeError):
                        await self.close()

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:await asyncio.wait_for(self.process.wait(),5)
            except asyncio.TimeoutError:self.process.kill();await self.process.wait()
        self.process=None
        if self.log:self.log.close();self.log=None
