import unittest

from scripts.mlx_voice_experiment import NativeWorker


class StubWorker(NativeWorker):
    def __init__(self,events):
        super().__init__('python','model','script','log')
        self.events=list(events);self.payload=None;self.reads=0

    async def begin(self,payload):
        self.payload=payload
        return 1

    async def render(self,messages,json_mode=False):
        return 'rendered original prompt'

    async def read(self,identity,timeout=90):
        self.reads+=1
        return self.events.pop(0)


class NativeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_error_is_not_drained_twice(self):
        worker=StubWorker([{'id':1,'done':True,'error':'original model error'}])
        with self.assertRaisesRegex(RuntimeError,'original model error'):
            async for _ in worker.stream([],purpose='voice_chat',json_mode=False,max_tokens=32,timeout=1):pass
        self.assertEqual(worker.reads,1)
        self.assertFalse(worker.lock.locked())

    async def test_early_bot_stop_drains_obsolete_output(self):
        worker=StubWorker([{'id':1,'text':'First sentence.'},{'id':1,'text':'Unheard tail.'},{'id':1,'done':True,'reason':'stop'}])
        source=worker.stream([],purpose='voice_chat',json_mode=False,max_tokens=32,timeout=1)
        self.assertEqual(await anext(source),'First sentence.')
        await source.aclose()
        self.assertEqual(worker.events,[])
        self.assertFalse(worker.lock.locked())

    async def test_schema_is_forwarded_for_same_model_repair(self):
        worker=StubWorker([{'id':1,'done':True,'reason':'stop','promptEvalMs':1,'generationMs':1,
                           'promptTokens':1,'cachedPromptTokens':0,'generatedTokens':1}])
        schema={'type':'object','properties':{'definition':{'type':'string'}}}
        async for _ in worker.stream([],purpose='voice_chat',json_mode=schema,max_tokens=32,timeout=1):pass
        self.assertEqual(worker.payload['json_mode'],schema)


if __name__=='__main__':unittest.main()
