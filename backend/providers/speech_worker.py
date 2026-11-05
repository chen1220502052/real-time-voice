"""Process-isolated local speech models, shared across calls and fenced on cancellation."""
import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
_workers={}


class SpeechWorker:
    def __init__(self,kind):
        self.kind=kind;self.process=None;self.lock=asyncio.Lock();self.sequence=0
        self.tasks=set();self.log=None
        self.dispatched=set()

    async def _start(self):
        if self.process and self.process.returncode is None:return
        python=Path(os.getenv('LOCAL_'+self.kind.upper()+'_PYTHON',str(ROOT/f'.local/{self.kind}-venv/bin/python')))
        if not python.is_file():raise RuntimeError(f'Local {self.kind.upper()} environment is not installed')
        env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',TOKENIZERS_PARALLELISM='false')
        (ROOT/'.local').mkdir(exist_ok=True)
        self.log=(ROOT/f'.local/live-{self.kind}-worker.log').open('ab')
        self.process=await asyncio.create_subprocess_exec(str(python),'-u','-m','scripts.local_speech_worker',self.kind,
            cwd=ROOT,env=env,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=self.log,limit=4*1024*1024)
        async with asyncio.timeout(90):
            line=await self.process.stdout.readline()
        if not line or not json.loads(line).get('ready'):
            raise RuntimeError(f'Local {self.kind.upper()} failed to start; check its worker log')

    async def _request(self,payload):
        queued=time.monotonic()
        async with self.lock:
            acquired=time.monotonic();self.dispatched.add(asyncio.current_task())
            try:
                await self._start()
                if payload is None:return {'ready':True}
                ready=time.monotonic()
                self.sequence+=1;request={'id':self.sequence,**payload}
                self.process.stdin.write(json.dumps(request,ensure_ascii=False).encode()+b'\n')
                async with asyncio.timeout(30):
                    await self.process.stdin.drain()
                    line=await self.process.stdout.readline()
                if not line:raise RuntimeError('Local speech worker closed unexpectedly')
                result=json.loads(line)
                if result.get('id')!=self.sequence:raise RuntimeError('Local speech response identity mismatch')
                if result.get('error'):raise RuntimeError(result['error'])
                result['queueWaitMs']=round((acquired-queued)*1000,3)
                result['startupMs']=round((ready-acquired)*1000,3)
                result['ipcWallMs']=round((time.monotonic()-ready)*1000,3)
                return result
            except BaseException:
                await self._stop()
                raise
            finally:
                self.dispatched.discard(asyncio.current_task())

    async def request(self,payload=None):
        # In-flight native inference cannot be cancelled. Finish/discard its
        # response before accepting the next request, without reloading models.
        task=asyncio.create_task(self._request(payload));self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled():done.exception()
        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A queued obsolete snapshot has not touched the worker protocol.
            # In-flight native calls still drain, preserving response fencing.
            if task not in self.dispatched:task.cancel()
            raise

    async def _stop(self):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):self.process.terminate()
            try:await asyncio.wait_for(self.process.wait(),3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):self.process.kill()
                await self.process.wait()
        self.process=None
        if self.log:self.log.close();self.log=None

    async def close(self):
        for task in list(self.tasks):task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        async with self.lock:await self._stop()


def worker(kind):
    key=(asyncio.get_running_loop(),kind)
    if key not in _workers:_workers[key]=SpeechWorker(kind)
    return _workers[key]


async def close_speech_workers():
    loop=asyncio.get_running_loop()
    for key in [key for key in _workers if key[0] is loop]:
        await _workers.pop(key).close()
