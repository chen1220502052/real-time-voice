import asyncio
import json
from types import SimpleNamespace

from backend.app import WebSocketConnection
from backend.latency_trace import ReplyTiming
from backend.protocol import AudioFrame


def test_timing_is_bounded_and_joins_final_input_to_reply():
    timing=ReplyTiming()
    for sequence in range(1300):
        timing.received(sequence)
    assert len(timing.inputs)==1200
    frame=AudioFrame(0,0,1299,0,b'\0'*3200)
    result=SimpleNamespace(end_time=129000,start_time=128000)
    timing.final(frame,result,'turn',123.)
    timing.begin('reply','turn')
    timing.mark('reply','ttsStartedMs')
    first=timing.snapshot('reply')['ttsStartedMs']
    timing.mark('reply','ttsStartedMs')
    assert timing.snapshot('reply')['ttsStartedMs']==first
    assert timing.snapshot('reply')['speechEndSequence']==1289
    assert timing.snapshot('reply')['speechEndInputReceivedMs']==timing.inputs[1289]


def test_clock_probe_returns_two_server_monotonic_timestamps_without_a_session():
    class Socket:
        values=[]
        async def send_str(self,value):self.values.append(json.loads(value))
    async def run():
        socket=Socket();connection=WebSocketConnection(socket)
        await connection.handle_control({'type':'timing_ping','pingId':'p'})
        value=socket.values[-1]
        assert value['type']=='timing_pong' and value['pingId']=='p'
        assert value['serverSentMs']>=value['serverReceivedMs']
        assert value['clockDomain']=='server_monotonic'
        await connection.handle_control({'type':'timing_ping','pingId':'x'*100})
        assert len(socket.values)==1
    asyncio.run(run())
