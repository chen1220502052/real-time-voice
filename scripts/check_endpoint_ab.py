"""Remote-only paired endpoint test server; never starts voice on the laptop."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import struct
import sys
import time
import wave

from scripts.signal_metrics import last_onset, SIGNAL_THRESHOLD_PCM16


ENDPOINT_KEYS = ("VAD_MAX_END_SILENCE_MS", "NLS_MAX_SENTENCE_SILENCE_MS")


def arm_values(ms):
    if type(ms) is not int or ms not in (550, 400):
        raise ValueError("Only the reviewed 550/400 endpoint arms are permitted")
    return {key: str(ms) for key in ENDPOINT_KEYS}


def guardrails(row, fixture):
    reasons = []
    if row["errors"]:
        reasons.append("pipeline_error")
    if len(row["asrFinals"]) != 1 or not row["asrFinals"][0].strip():
        reasons.append("split_or_empty_final")
    if row["earlyTextReplies"] or row["earlyAudioReplies"]:
        reasons.append("premature_reply")
    replies = row["userReplies"]
    if len(replies) != 1 or any(not r.get("complete") or not r["text"].strip() or not r["audioFrames"] for r in replies):
        reasons.append("incomplete_or_extra_reply")
    transcript = " ".join(row["asrFinals"]).lower()
    answer = " ".join(r["text"] for r in replies).lower()
    if "pause" in fixture and not all(word in transcript for word in ("help", "joke")):
        reasons.append("missing_input_clause")
    if "numbers" in fixture and not any(word in answer for word in ("68", "sixty-eight", "sixty eight")):
        reasons.append("wrong_arithmetic_reply")
    if "numbers" in fixture and not (any(x in transcript.replace('-', ' ') for x in ('23','twenty three'))
                                      and any(x in transcript.replace('-', ' ') for x in ('45','forty five'))):
        reasons.append("missing_numeric_input")
    if replies and any(r["text"].rstrip()[-1:] not in ".!?。！？" for r in replies):
        reasons.append("dangling_reply")
    return reasons


def summarize(pairs):
    result = {"pairs": len(pairs), "arms": {}, "pairedSavingsMs": {}}
    for arm in ("550", "400"):
        rows = [pair[arm] for pair in pairs if arm in pair]
        result["arms"][arm] = {"attempts": len(rows), "failedGuardrails": sum(bool(r["guardrailFailures"]) for r in rows),
            "failures": [{"fixture": r["fixture"], "reasons": r["guardrailFailures"]} for r in rows if r["guardrailFailures"]]}
        for metric in ("sourceEndToFirstPacketMs", "endpointToFinalMs", "finalToFirstPacketMs"):
            values = [r.get(metric) for r in rows if r.get(metric) is not None]
            result["arms"][arm][metric] = {"n":len(values), "mean":statistics.mean(values) if values else None,
                                           "median":statistics.median(values) if values else None}
    for metric in ("sourceEndToFirstPacketMs", "endpointToFinalMs", "finalToFirstPacketMs"):
        # Guardrail failures stay in the denominator; latency never masks them.
        values = [p["550"][metric]-p["400"][metric] for p in pairs if all(
            a in p and p[a].get(metric) is not None for a in ("550", "400"))]
        result["pairedSavingsMs"][metric] = {"n":len(values), "mean":statistics.mean(values) if values else None,
            "median":statistics.median(values) if values else None, "min":min(values) if values else None,
            "max":max(values) if values else None}
    result["candidateSafe"] = bool(pairs) and all("400" in p and not p["400"]["guardrailFailures"] for p in pairs)
    return result


async def voice_case(url, fixture):
    import aiohttp
    from backend.protocol import encode_audio_frame, decode_audio_frame
    with wave.open(str(fixture), "rb") as source:
        assert (source.getframerate(),source.getsampwidth(),source.getnchannels()) == (16000,2,1)
        pcm = source.readframes(source.getnframes())
    signal_end = last_onset(struct.unpack("<"+str(len(pcm)//2)+"h",pcm))/16000
    row = {"fixture":fixture.name,"fixtureSha256":hashlib.sha256(fixture.read_bytes()).hexdigest(),
           "asrFinals":[],"errors":[],"userReplies":[],"earlyTextReplies":[],"earlyAudioReplies":[]}
    async with aiohttp.ClientSession() as client, client.ws_connect(url) as ws:
        async def expect(kind):
            async with asyncio.timeout(20):
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        value=json.loads(message.data)
                        if value.get("type")==kind:return value
        await ws.send_json({"type":"prepare"});assert (await expect("prepare_result"))["ok"]
        await ws.send_json({"type":"create_session","grade":7})
        created=await expect("create_session_result");session=created["sessionId"];row["sessionId"]=session
        opening=None;current=None;replies={};opened=asyncio.Event();completed=asyncio.Event();source_end=None
        async def read():
            nonlocal opening,current
            async for message in ws:
                now=time.monotonic()*1000
                if message.type==aiohttp.WSMsgType.BINARY:
                    frame=decode_audio_frame(message.data)
                    if current in replies:
                        r=replies[current];r["audioFrames"]+=1;r.setdefault("firstPacketMs",now)
                    continue
                if message.type!=aiohttp.WSMsgType.TEXT:continue
                value=json.loads(message.data);kind=value.get("type");reply=value.get("replyId")
                if kind=="assistant_delta":
                    if opening is None:opening=reply
                    r=replies.setdefault(reply,{"replyId":reply,"text":"","audioFrames":0})
                    r["text"]+=value["text"];r.setdefault("firstTextMs",now)
                elif kind=="avatar_frame":
                    current=reply
                    if reply in replies:replies[reply].setdefault("timing",{}).update(value.get("timing",{}))
                elif kind=="asr_result" and value.get("status")==3:
                    row["asrFinals"].append(value.get("text",""))
                elif kind=="session_event":
                    event=value["event"];rid=event.get("replyId")
                    if rid in replies and event.get("attributes",{}).get("timing"):
                        replies[rid].setdefault("timing",{}).update(event["attributes"]["timing"])
                elif kind=="reply_complete":
                    r=replies[reply];r["complete"]=True
                    await ws.send_json({"type":"client_event","event":{"eventType":"playback_completed","replyId":reply}})
                    if reply==opening:opened.set()
                    elif source_end is not None and now>=source_end:completed.set()
                elif kind in {"error","reply_failed"}:
                    row["errors"].append(value.get("code",kind));completed.set()
        reader=asyncio.create_task(read())
        try:
            await asyncio.wait_for(opened.wait(),30)
            started=time.monotonic();source_end=started*1000+signal_end*1000
            packets=[pcm[i:i+3200].ljust(3200,b"\0") for i in range(0,len(pcm),3200)]+[b"\0"*3200]*24
            for i,packet in enumerate(packets):
                await asyncio.sleep(max(0,started+(i+1)*.1-time.monotonic()))
                await ws.send_bytes(encode_audio_frame(0,session,i,int(time.time()*1000),packet))
            await asyncio.wait_for(completed.wait(),35)
            await asyncio.sleep(.15)
        finally:
            reader.cancel();await asyncio.gather(reader,return_exceptions=True)
            await ws.send_json({"type":"close_session"})
        row["userReplies"]=[r for rid,r in replies.items() if rid!=opening]
        for r in row["userReplies"]:
            if r.get("firstTextMs",float("inf"))<source_end-20:row["earlyTextReplies"].append(r["replyId"])
            if r.get("firstPacketMs",float("inf"))<source_end-20:row["earlyAudioReplies"].append(r["replyId"])
        first=next(iter(row["userReplies"]),{})
        row["sourceEndToFirstPacketMs"]=first["firstPacketMs"]-source_end if "firstPacketMs" in first else None
        t=first.get("timing",{});receipts=dict(t.get("inputReceipts",[]))
        # The last >=300 signal's actual upload frame, not an ASR-estimated tail.
        receipt=receipts.get(int(signal_end*10))
        row["endpointToFinalMs"]=t["asrFinalMs"]-receipt if receipt is not None and "asrFinalMs" in t else None
        row["finalToFirstPacketMs"]=t["firstFrameSendStartedMs"]-t["asrFinalMs"] if all(k in t for k in ("firstFrameSendStartedMs","asrFinalMs")) else None
        row["guardrailFailures"]=guardrails(row,fixture.name)
        return row


async def run(args):
    from aiohttp import web
    import backend.app as app_module
    assert Path(app_module.__file__).resolve().parent.parent==Path("/opt/real-time-voice")
    original=dict(os.environ)
    report={"status":"running","startedAt":datetime.now(timezone.utc).isoformat(),"pairs":[],
            "measurement":"paced_fixture_to_first_packet_not_speaker","physicalOutput":None,
            "signalThresholdPcm16":SIGNAL_THRESHOLD_PCM16,"playbackAcknowledgement":"simulated",
            "onlyTreatmentKeys":list(ENDPOINT_KEYS),"sourceHashes":{
                str(p.relative_to('/opt/real-time-voice')):hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in ('backend','web') for p in (Path('/opt/real-time-voice')/folder).rglob('*')
                if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('._')}}
    app=app_module.create_app(bot_state_path=str(args.output.with_suffix('.sqlite3')))
    def set_arm(ms):
        for key,value in arm_values(ms).items():os.environ[key]=value
        assert all(os.environ.get(k)==v for k,v in original.items() if k not in ENDPOINT_KEYS)
    async def control(request):
        if app['resource_manager'].in_use:return web.json_response({'busy':True},status=409)
        ms=(await request.json()).get('endpointMs');set_arm(ms)
        return web.json_response({'endpointMs':ms,'treatment':arm_values(ms)})
    app.router.add_post('/__endpoint_test/arm',control)
    runner=web.AppRunner(app,handler_cancellation=True,access_log=None)
    await runner.setup();await web.TCPSite(runner,'127.0.0.1',args.port).start()
    ws_url=f'ws://127.0.0.1:{args.port}/ws'
    async def released():
        async with asyncio.timeout(10):
            while app['resource_manager'].in_use:await asyncio.sleep(.1)
    def save():
        report['summary']=summarize(report['pairs']);args.output.write_text(json.dumps(report,indent=2)+'\n')
    try:
        for repeat in range(args.repeat):
            for index,name in enumerate(args.fixtures):
                pair={'fixture':name,'repeat':repeat,'order':[]};report['pairs'].append(pair)
                for ms in ((550,400) if (repeat+index)%2==0 else (400,550)):
                    await released();set_arm(ms)
                    try:
                        row=await voice_case(ws_url,args.fixture_dir/name)
                    except Exception as error:
                        row={'fixture':name,'errors':[type(error).__name__],
                             'guardrailFailures':['test_or_pipeline_failure'],
                             'sourceEndToFirstPacketMs':None,'endpointToFinalMs':None,'finalToFirstPacketMs':None}
                    pair[str(ms)]=row;pair['order'].append(ms)
                    save();print(json.dumps({'fixture':name,'arm':ms,'failures':row['guardrailFailures'],
                        'packetMs':row['sourceEndToFirstPacketMs'],'endpointMs':row['endpointToFinalMs']}),flush=True)
        from scripts.check_voice_interrupt import check as interrupt
        from scripts.check_english_voice import check as multi
        from scripts.check_bot_recovery import check as recovery
        report['guardrails']={}
        for ms in (550,400):
            await released();set_arm(ms)
            group={}
            try:
                group['multiturn']=await multi(ws_url,7,[args.fixture_dir/name for name in ('en_hello_16k.wav','en_help_16k.wav','en_numbers_16k.wav')])
                await released();group['interruptions']=await interrupt(ws_url, fixture_dir=args.fixture_dir)
                await released();group['recovery']=await recovery(f'http://127.0.0.1:{args.port}')
                group['passed']=True
            except Exception as error:group.update(passed=False,failureType=type(error).__name__)
            report['guardrails'][str(ms)]=group;save()
        report['status']='passed' if report['summary']['candidateSafe'] and all(g['passed'] for g in report['guardrails'].values()) else 'rejected'
        report['finishedAt']=datetime.now(timezone.utc).isoformat();save()
        if args.keep_serving:
            print(json.dumps({'phase':'browser_ready','status':report['status']}),flush=True)
            await asyncio.Event().wait()
    finally:await runner.cleanup()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote-ecs',action='store_true',required=True)
    parser.add_argument('--port',type=int,default=8876)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--fixture-dir',type=Path,required=True)
    parser.add_argument('--repeat',type=int,default=2)
    parser.add_argument('--keep-serving',action='store_true')
    parser.add_argument('--fixtures',nargs='+',default=['en_hello_16k.wav','en_numbers_16k.wav','en_pause300_16k.wav','en_pause400_16k.wav'])
    args=parser.parse_args()
    if sys.platform!='linux' or not Path('/etc/real-time-voice.env').is_file():parser.error('Remote ECS only; no local voice service')
    if args.repeat<2 or args.output.exists():parser.error('Two or more interleaved repeats and a new output path required')
    asyncio.run(run(args))


if __name__=='__main__':main()
