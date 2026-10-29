"""Read-only public/proxy authentication checks; never reserves a voice slot."""
import argparse
import asyncio
import json
from pathlib import Path
import ssl
import aiohttp
import certifi


async def check(url,auth_file):
    labels=dict(line.split(':',1) for line in auth_file.read_text().splitlines() if ':' in line)
    auth=aiohttp.BasicAuth(labels['Username'].strip(),labels['Password'].strip())
    report={'checks':[]}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()))) as client:
        async with client.post(url+'/api/browser-session',headers={'X-Authenticated-User':'forged'}) as r:
            assert r.status==401;report['checks'].append('forged_login_header_denied')
        for headers in ({},{'Cookie':'rtv_browser_session=forged'}):
            async with client.get(url+'/ws',headers=headers) as r:
                assert r.status==401 and 'WWW-Authenticate' not in r.headers
        report['checks'].append('unauthenticated_ws_denied_without_basic_challenge')
        async with client.post(url+'/api/browser-session',auth=auth) as r:
            assert r.status==200,(r.status,await r.text())
            data=await r.json();assert data['authMode']=='cookie'
            cookie=r.cookies['rtv_browser_session']
            assert cookie['httponly'] and cookie['samesite']=='Strict'
            if url.startswith('https:'):assert cookie['secure']
            headers={'Cookie':f'rtv_browser_session={cookie.value}'}
        report['checks'].append('http_login_issues_httponly_cookie')
        async with client.get(url+'/ws',headers={**headers,'Origin':'https://evil.invalid'}) as r:
            assert r.status==401
        report['checks'].append('cross_origin_ws_denied')
        # Cookies alone establish WS; no Authorization header is needed here.
        async with client.ws_connect(url.replace('http','ws',1)+'/ws',headers=headers) as ws:
            assert (await ws.receive_json(timeout=8))['event']['eventType']=='connection_opened'
        report['checks'].append('cookie_only_upgrade_without_basic_auth')
        async with client.post(url+'/api/connection-events',headers=headers,json={
            'attemptId':'connection-diagnostic-test','code':'WEBSOCKET_TIMEOUT','elapsedMs':15001}) as r:
            assert r.status==200
        report['checks'].append('pre_session_failure_telemetry')
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
