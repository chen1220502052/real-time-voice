import asyncio
import base64
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from backend.text_bot import BotEvent
from scripts import evaluate_first_audio as experiment


class FakeBot:
    async def stream(self, request):
        yield BotEvent(request.request_id, 'text_delta', text='A turtle has a shell. ')
        yield BotEvent(request.request_id, 'done', finish_reason='stop')


class PriorityCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, behavior):
        entered=asyncio.Event()

        class Worker:
            def __init__(self,kind):self.kind=kind
            async def request(self,payload):
                if self.kind=='asr':return {'text':'Why do turtles have shells?','confidence':1}
                entered.set()
                if behavior=='failure':raise RuntimeError('test synthesis failure')
                if behavior=='cancel':await asyncio.sleep(3600)
                return {'pcm':base64.b64encode(b'\x01\x00'*1600).decode(),
                        'sampleRate':16000,'inferenceMs':1,'queueWaitMs':0}

        with TemporaryDirectory() as folder:
            root=Path(folder)
            with wave.open(str(root/'input.wav'),'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'))
                wav.writeframes(b'\x00\x00'*1600)
            case={'id':'fixture','audio':'input.wav','messages':[{'role':'user','content':'Question'}],
                  'grade':6,'checks':{'required':[],'forbidden':[],'chinese':False,'max_questions':0,'max_words':65}}
            native=SimpleNamespace(process=SimpleNamespace(pid=12345678,returncode=None))
            with patch.object(experiment,'ROOT',root), patch.object(experiment,'EnglishChatBot',FakeBot), \
                 patch.object(experiment.speech_worker,'worker',side_effect=Worker), \
                 patch.object(experiment.os,'kill') as kill:
                task=asyncio.create_task(experiment.audio_case(case,0,'native_priority',root,native))
                if behavior=='cancel':
                    await asyncio.wait_for(entered.wait(),1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                elif behavior=='failure':
                    with self.assertRaisesRegex(RuntimeError,'test synthesis failure'):await task
                else:
                    result=await task
                    self.assertTrue(result['textPreserved'])
                self.assertEqual([call.args for call in kill.call_args_list],
                                 [(native.process.pid,signal.SIGUSR1),(native.process.pid,signal.SIGUSR2)])

    async def test_resume_after_success(self):await self.run_case('success')
    async def test_resume_after_failure(self):await self.run_case('failure')
    async def test_resume_after_cancellation(self):await self.run_case('cancel')


if __name__=='__main__':unittest.main()
