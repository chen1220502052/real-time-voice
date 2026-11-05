import asyncio
from types import SimpleNamespace

import pytest

from backend.local_inference import LocalInferenceGate, LocalInferenceBusy, LocalVoiceRuntime
from backend.providers.speech_worker import SpeechWorker


def test_voice_admission_drains_background_and_prevents_new_background_until_release():
    async def run():
        gate=LocalInferenceGate();entered=asyncio.Event();finish=asyncio.Event()
        async def writing():
            async with gate.request('writing'):
                entered.set();await finish.wait()
        task=asyncio.create_task(writing());await entered.wait()
        voice=asyncio.create_task(gate.reserve_voice('call'));await asyncio.sleep(0)
        assert not voice.done()
        with pytest.raises(LocalInferenceBusy):
            async with gate.request('chat',timeout=.01):pytest.fail('Background inference overlapped voice preparation')
        finish.set();await task;await voice
        async with gate.request('voice_chat'):pass
        with pytest.raises(LocalInferenceBusy):
            async with gate.request('writing',timeout=.01):pytest.fail('Writing overlapped voice session')
        await gate.release_voice('call')
        async with gate.request('writing'):assert gate.background==1
        assert gate.background==0
    asyncio.run(run())


def test_cancelled_voice_preparation_releases_pending_reservation():
    async def run():
        gate=LocalInferenceGate()
        async with gate.request('writing'):
            task=asyncio.create_task(gate.reserve_voice('abandoned'));await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        assert not gate.voice_owners
        async with gate.request('chat'):pass
    asyncio.run(run())


def test_model_warmup_failure_releases_voice_capacity(monkeypatch):
    async def fail(*args,**kwargs):raise RuntimeError('model unavailable')
    monkeypatch.setattr('backend.local_llm.complete',fail)
    async def run():
        gate=LocalInferenceGate();runtime=LocalVoiceRuntime(gate)
        with pytest.raises(RuntimeError):await runtime.prepare('failed')
        assert not gate.voice_owners
    asyncio.run(run())


def test_cancelled_queued_speech_snapshot_is_not_dispatched_and_inflight_response_is_drained():
    async def run():
        import json
        worker=SpeechWorker('asr');entered=asyncio.Event();release=asyncio.Event();sent=[]
        class Input:
            def write(self,data):sent.append(json.loads(data))
            async def drain(self):pass
        class Output:
            async def readline(self):
                request=sent[-1]
                if request['text']=='old':entered.set();await release.wait()
                return json.dumps({'id':request['id'],'text':request['text'],'inferenceMs':1}).encode()+b'\n'
        async def noop():pass
        worker.process=SimpleNamespace(stdin=Input(),stdout=Output());worker._start=noop;worker._stop=noop
        first=asyncio.create_task(worker.request({'text':'old'}));await entered.wait()
        obsolete=asyncio.create_task(worker.request({'text':'obsolete'}));await asyncio.sleep(0)
        obsolete.cancel()
        with pytest.raises(asyncio.CancelledError):await obsolete
        first.cancel()
        with pytest.raises(asyncio.CancelledError):await first
        latest=asyncio.create_task(worker.request({'text':'latest'}));await asyncio.sleep(0)
        assert len(sent)==1
        release.set();result=await latest
        assert [x['text'] for x in sent]==['old','latest']
        assert result['text']=='latest' and result['queueWaitMs']>=0 and result['ipcWallMs']>=0
        await worker.close()
    asyncio.run(run())
