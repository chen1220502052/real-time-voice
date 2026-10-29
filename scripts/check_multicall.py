"""Remote real ASR/Bot/TTS concurrency and busy admission checks, no local voice."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

import aiohttp
from scripts.browser_login import authenticated_ws
from scripts.check_english_voice import check as voice
from scripts.check_voice_interrupt import check as interrupt
from scripts.check_bot_recovery import check as recovery


async def run(args):
    labels=dict(line.split(':',1) for line in args.auth_file.read_text().splitlines() if ':' in line)
    auth=aiohttp.BasicAuth(labels['Username'].strip(),labels['Password'].strip())
    ws=args.url.replace('http','ws',1).rstrip('/')+'/ws'
    report={'mode':'real_remote_voice','capacity':args.capacity,'playbackReceipts':'simulated_not_device',
            'simultaneousCalls':[], 'busy':None}
    barrier=asyncio.Barrier(args.capacity+1)
    async def ready():await asyncio.wait_for(barrier.wait(),40)
    async def full():
        await ready()
        async with aiohttp.ClientSession(auth=auth) as client:
            async with client.get(args.url+'/health') as response:
                health=await response.json();assert health['resources']=={'capacity':args.capacity,'inUse':args.capacity}
            async with authenticated_ws(client,ws) as socket:
                await socket.send_json({'type':'prepare','version':1,'attemptId':'fourth-busy-probe'})
                async with asyncio.timeout(10):
                    while True:
                        data=await socket.receive_json()
                        if data.get('type')=='prepare_result':break
                assert not data['ok'] and data['code']=='NO_RESOURCE',data
                report['busy']={'code':data['code'],'resources':data['resources']}
    fixtures=[args.fixture_dir/name for name in ('en_hello_16k.wav','en_help_16k.wav','en_numbers_16k.wav')]
    tasks=[voice(ws,7,fixtures,auth=auth,on_session_ready=ready) for _ in range(args.capacity)]
    values=await asyncio.gather(*tasks,full(),return_exceptions=True)
    for index,value in enumerate(values[:args.capacity]):
        report['simultaneousCalls'].append(value if isinstance(value,dict) else {'errorType':type(value).__name__})
    report['concurrencyPassed']=all(not isinstance(v,BaseException) for v in values)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    assert report['concurrencyPassed'], 'Concurrent voice or busy admission failed; retained report'
    # Releasing calls must make room immediately, not strand busy slots.
    async with aiohttp.ClientSession(auth=auth) as client:
        async with client.get(args.url+'/health') as response:
            health=await response.json()
        assert health['resources']['inUse']==0,health['resources']
    report['postRelease']=await voice(ws,7,[fixtures[0]],auth=auth)
    report['interruptions']=await interrupt(ws,args.auth_file,fixture_dir=args.fixture_dir)
    report['recovery']=await recovery(args.url,args.auth_file)
    report['passed']=True
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'passed':True,'calls':args.capacity,'turnsPerCall':3,'busy':report['busy'],
                      'interruptions':len(report['interruptions']['interruptions'])}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--auth-file',type=Path,required=True)
    parser.add_argument('--fixture-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--capacity',type=int,choices=(2,3),default=2)
    parser.add_argument('--remote-ecs',action='store_true',required=True)
    args=parser.parse_args()
    if sys.platform!='linux' or not Path('/etc/real-time-voice.env').exists():parser.error('Cloud only')
    asyncio.run(run(args))


if __name__=='__main__':main()
