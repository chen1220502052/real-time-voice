"""Real local PCM -> websocket -> local ASR -> chat -> Kokoro acceptance check.

Uses generated test speech, never captures the microphone. Credentials come
from LOCAL_TEST_USERNAME/PASSWORD and are accepted only for a loopback URL.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import wave

import aiohttp

from backend.protocol import decode_audio_frame,encode_audio_frame
from backend.providers.speech_local import KokoroTts
from backend.providers.speech_worker import close_speech_workers


async def check(base_url,username,password,output,*,grade=3,chapter_id='5175e30ef6f5f8a4933bdd06',interrupt=False):
    if urlsplit(base_url).hostname not in {'localhost','127.0.0.1','::1'}:
        raise ValueError('This acceptance check is for loopback services only')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    statements=['I like cats.','Why do turtles have shells?'][:1 if interrupt else 2]
    fixtures=[]
    for index,text in enumerate(statements):
        pcm=b''.join([chunk async for chunk in KokoroTts().stream_pcm(text)])
        fixtures.append(pcm)
        with wave.open(str(output/f'input-{index}.wav'),'wb') as wav:
            wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(pcm)
    report={'mode':'real_local_audio_fixture','physicalMicrophoneTested':False,'grade':grade,
            'interruptTest':interrupt,'asrFinals':[],'replies':[],'interruptions':[],
            'lateInterruptedAudio':0,'errors':[],'asrFinalMs':[]}
    started=time.monotonic()
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.post(base_url+'/api/auth/login',json={'username':username,'password':password}) as response:
                if response.status!=200:raise RuntimeError('Local test account login failed')
                await response.read()
            health=await (await client.get(base_url+'/health')).json()
            assert health['voiceEnabled'] and health['capabilities']['asr'] in {'mlx','parakeet'} and health['capabilities']['tts']=='kokoro'
            report['capabilities']=health['capabilities']
            async with client.ws_connect(base_url.replace('http','ws',1)+'/ws') as ws:
                async def expect(kind):
                    async with asyncio.timeout(30):
                        async for message in ws:
                            if message.type==aiohttp.WSMsgType.TEXT:
                                data=json.loads(message.data)
                                if data['type']==kind:return data
                    raise RuntimeError('Websocket setup did not complete')
                await ws.send_json({'type':'prepare','version':1})
                assert (await expect('prepare_result'))['ok']
                await ws.send_json({'type':'create_session','version':1,'grade':grade,
                                    **({'chapterId':chapter_id} if chapter_id else {})})
                created=await expect('create_session_result');assert created['ok']
                session=created['sessionId'];sequence=0;replies={};metadata={};interrupted=set()
                complete=asyncio.Queue();first_audio=asyncio.Event()
                async def read():
                    async for message in ws:
                        if message.type==aiohttp.WSMsgType.BINARY:
                            frame=decode_audio_frame(message.data);assert len(frame.payload)==3200
                            reply=metadata.pop(frame.sequence)['replyId']
                            if reply in interrupted:report['lateInterruptedAudio']+=1
                            row=replies.setdefault(reply,{'text':'','pcm':bytearray(),'audioFrames':0})
                            row['pcm'].extend(frame.payload);row['audioFrames']+=1
                            row.setdefault('firstAudioMs',round((time.monotonic()-started)*1000))
                            first_audio.set();continue
                        if message.type!=aiohttp.WSMsgType.TEXT:continue
                        data=json.loads(message.data);kind=data['type']
                        if kind=='avatar_frame':metadata[data['sequence']]=data
                        elif kind=='assistant_delta':
                            row=replies.setdefault(data['replyId'],{'text':'','pcm':bytearray(),'audioFrames':0})
                            row['text']+=data['text']
                        elif kind=='asr_result' and data['status']==3:
                            report['asrFinals'].append(data['text'])
                            report['asrFinalMs'].append(round((time.monotonic()-started)*1000))
                        elif kind=='playback_interrupt':
                            interrupted.add(data['replyId']);report['interruptions'].append(data['replyId'])
                        elif kind=='reply_complete':
                            row=replies[data['replyId']]
                            assert row['text'].strip() and row['audioFrames']>0
                            public={k:v for k,v in row.items() if k!='pcm'}
                            report['replies'].append(public)
                            with wave.open(str(output/f'reply-{len(report["replies"])}.wav'),'wb') as wav:
                                wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(row['pcm'])
                            await ws.send_json({'type':'client_event','event':{'eventType':'playback_completed',
                                'replyId':data['replyId'],'sessionId':session}})
                            await complete.put(public)
                        elif kind in {'error','reply_failed'}:
                            report['errors'].append({'type':kind,'code':data.get('code')})
                            await complete.put(None)
                reader=asyncio.create_task(read())
                async def send(frame):
                    nonlocal sequence
                    await ws.send_bytes(encode_audio_frame(0,session,sequence,int(time.time()*1000),frame))
                    sequence+=1;await asyncio.sleep(.1)
                try:
                    if interrupt:await asyncio.wait_for(first_audio.wait(),30)
                    else:assert await asyncio.wait_for(complete.get(),30)
                    for index,pcm in enumerate(fixtures):
                        for offset in range(0,len(pcm),3200):await send(pcm[offset:offset+3200].ljust(3200,b'\0'))
                        input_end_ms=round((time.monotonic()-started)*1000)
                        deadline=time.monotonic()+40
                        while complete.empty() and time.monotonic()<deadline:await send(b'\0'*3200)
                        answer=await asyncio.wait_for(complete.get(),1)
                        assert answer,'Voice reply failed'
                        answer['inputEndToFirstAudioMs']=answer['firstAudioMs']-input_end_ms
                        answer['asrFinalToFirstAudioMs']=answer['firstAudioMs']-report['asrFinalMs'][index]
                    await ws.send_json({'type':'close_session','version':1})
                    await asyncio.sleep(.2)
                finally:
                    reader.cancel();await asyncio.gather(reader,return_exceptions=True)
            await asyncio.sleep(.2)
            health=await (await client.get(base_url+'/health')).json()
            report['resourcesAfterClose']=health['resources']
        assert not report['errors'],report['errors']
        assert len(report['asrFinals'])==len(fixtures),report['asrFinals']
        assert 'cat' in report['asrFinals'][0].lower()
        if not interrupt:assert 'turtle' in report['asrFinals'][1].lower()
        else:assert report['interruptions'] and report['lateInterruptedAudio']==0
        assert report['resourcesAfterClose']['inUse']==0
        report['passed']=True
        return report
    finally:
        report['elapsedSeconds']=round(time.monotonic()-started,2)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


async def main(args):
    try:
        report=await check(args.url,os.environ['LOCAL_TEST_USERNAME'],os.environ['LOCAL_TEST_PASSWORD'],args.output,
                           interrupt=args.interrupt,grade=7 if args.interrupt else 3,
                           chapter_id=None if args.interrupt else '5175e30ef6f5f8a4933bdd06')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:await close_speech_workers()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8765')
    parser.add_argument('--output',type=Path,default=Path('.local/live-voice-e2e'))
    parser.add_argument('--interrupt',action='store_true')
    asyncio.run(main(parser.parse_args()))
