"""Application-owned admission for shared local model memory and GPU time."""
import asyncio
from contextlib import asynccontextmanager
import time


class LocalInferenceBusy(RuntimeError):
    pass


class LocalInferenceGate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.voice_owners = set()
        self.background = 0

    async def reserve_voice(self, owner, timeout=20):
        async with self.condition:
            self.voice_owners.add(owner)
            try:
                async with asyncio.timeout(timeout):
                    await self.condition.wait_for(lambda: self.background == 0)
            except BaseException:
                self.voice_owners.discard(owner)
                self.condition.notify_all()
                raise

    async def release_voice(self, owner):
        async with self.condition:
            self.voice_owners.discard(owner)
            self.condition.notify_all()

    @asynccontextmanager
    async def request(self, purpose, timeout=15):
        if purpose == 'voice_chat':
            yield 0
            return
        started=time.perf_counter()
        async with self.condition:
            try:
                async with asyncio.timeout(timeout):
                    await self.condition.wait_for(lambda: not self.voice_owners)
            except TimeoutError as error:
                raise LocalInferenceBusy('正在进行实时语音通话，请挂断后再进行文字聊天或 AI 写作。') from error
            self.background += 1
        try:
            yield (time.perf_counter()-started)*1000
        finally:
            async with self.condition:
                self.background -= 1
                self.condition.notify_all()


class LocalVoiceRuntime:
    def __init__(self, gate):
        self.gate=gate

    async def prepare(self, owner):
        from . import local_llm
        from .curriculum_chat import voice_chat_prompt
        from .providers.speech_worker import worker
        from types import SimpleNamespace
        import base64
        started=time.perf_counter()
        try:
            async with asyncio.timeout(25):
                await self.gate.reserve_voice(owner)
                admitted=time.perf_counter()
                prompt=voice_chat_prompt(SimpleNamespace(chapter={'grade':'8年级','unit':'English conversation'}))
                await local_llm.complete([{'role':'system','content':prompt},
                    {'role':'user','content':'Hello'}],purpose='voice_chat',max_tokens=128,timeout=20)
                model_ready=time.perf_counter()
                # Exercise inference before accepting microphone frames.
                asr=await worker('asr').request({'pcm':base64.b64encode(bytes(64000)).decode()})
            return {'admissionWaitMs':round((admitted-started)*1000,3),
                'modelWarmupMs':round((model_ready-admitted)*1000,3),
                'asrWarmupMs':round((time.perf_counter()-model_ready)*1000,3),
                'asrMemory':asr.get('memory')}
        except BaseException:
            await self.gate.release_voice(owner)
            raise

    async def release(self, owner):
        await self.gate.release_voice(owner)
