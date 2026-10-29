"""Cloud transport lifecycle checks: no user hang-up and no fake ASR/TTS."""
import argparse
import asyncio
import json
from pathlib import Path
import time

import aiohttp
from scripts.browser_login import authenticated_ws


async def check(url,auth_file):
    labels=dict(line.split(':',1) for line in auth_file.read_text().splitlines() if ':' in line)
    auth=aiohttp.BasicAuth(labels['Username'].strip(),labels['Password'].strip())
    report={'checks':[]}
    async with aiohttp.ClientSession(auth=auth) as client:
        endpoint=url.replace('http','ws',1)+'/ws'
        async def create(ws):
            await ws.send_json({'type':'prepare','version':1})
            async def expect(kind):
                async with asyncio.timeout(15):
                    while True:
                        message=await ws.receive_json()
                        if message.get('type')==kind:return message
            assert (await expect('prepare_result'))['ok']
            await ws.send_json({'type':'create_session','grade':7})
            return await expect('create_session_result')
        async def occupancy(expected,timeout=9):
            async with asyncio.timeout(timeout):
                while True:
                    async with client.get(url+'/health') as r:health=await r.json()
                    if health['resources']['inUse']==expected:return
                    await asyncio.sleep(.1)
        await occupancy(0)
        async with authenticated_ws(client,endpoint) as ws:
            await create(ws)
            started=time.monotonic()
            ws._writer.transport.abort()  # No close_session and no WS close frame.
            await occupancy(0)
            report['abruptTransportReleaseMs']=round((time.monotonic()-started)*1000)
        report['checks'].append('abrupt_transport_without_hangup_releases_slot')
        async with authenticated_ws(client,endpoint) as ws:
            created=await create(ws)
            headers={'Cookie':ws._response.request_info.headers['Cookie']}
            body={'sessionId':created['sessionId'],'disconnectKey':'wrong'}
            async with client.post(url+'/api/browser-disconnect',json=body,headers=headers) as r:assert r.status==403
            await occupancy(1)
            body['disconnectKey']=created['disconnectKey']
            for _ in range(2):
                async with client.post(url+'/api/browser-disconnect',json=body,headers=headers) as r:assert r.status in (200,202)
            await occupancy(0)
        report['checks'].append('page_leave_key_and_duplicate_beacon_cleanup')
        async with authenticated_ws(client,endpoint,autoping=False) as dead, authenticated_ws(client,endpoint) as quiet:
            await create(dead);await create(quiet)
            async def drain(socket):
                async for message in socket:
                    # A dead client receives the ping but deliberately never pongs.
                    pass
            dead_reader=asyncio.create_task(drain(dead));quiet_reader=asyncio.create_task(drain(quiet))
            started=time.monotonic()
            try:
                await occupancy(1,timeout=30)
                report['heartbeatReleaseMs']=round((time.monotonic()-started)*1000)
                assert not quiet.closed,'Silent but heartbeat-alive call was incorrectly evicted'
                report['checks'].append('missing_pong_releases_dead_call_but_quiet_live_call_survives')
            finally:
                dead_reader.cancel();quiet_reader.cancel()
                await asyncio.gather(dead_reader,quiet_reader,return_exceptions=True)
        await occupancy(0)
    report['passed']=True
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',required=True)
    parser.add_argument('--auth-file',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();report=asyncio.run(check(args.url.rstrip('/'),args.auth_file))
    args.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))


if __name__=='__main__':main()
