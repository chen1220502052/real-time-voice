"""Paired frozen-source audio comparison, plus paced VAD endpoint probes.

Use project Python; speech workers use their existing dedicated environments.
Inputs are synthetic and frozen by build_bilingual_voice_eval.py. This is NOT a
browser or real child speech benchmark. All inference remains local.
"""
import argparse,asyncio,base64,hashlib,importlib.util,json,os,sys,time,wave
from pathlib import Path
from backend.english_chat import EnglishChatBot
from backend.text_bot import BotMessage,BotRequest
from backend.speech_text import take_speech_segments
from backend.providers.speech_worker import worker,close_speech_workers
from backend.llm_runtime import LLMHttpPool,use_llm_pool,Timing,use_timing
ROOT=Path('.local/bilingual-voice');PACK=Path('evals/bilingual_voice')
SOURCES=['backend/voice_grounding.py','backend/voice_prompt.py','backend/providers/speech_local.py','scripts/local_speech_worker.py','backend/english_chat.py','backend/reply_sentences.py']
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def configure():
 for line in Path('.env.local').read_text().splitlines():
  k,_,v=line.partition('=')
  if k.strip() in {'LOCAL_ASR_PYTHON','ASR_BACKEND','LOCAL_PARAKEET_QUANT_BITS','LOCAL_TTS_ENGINE','LOCAL_TTS_PYTHON'}:os.environ[k.strip()]=v.strip().strip('"')
 os.environ.update(LLM_PROVIDER='ollama',CHAT_PROVIDER='ollama',VOICE_CHAT_MODEL='qwen2.5:3b',LOCAL_TTS_CACHE='0',LOCAL_VAD_END_MS='500',LOCAL_ASR_MULTILINGUAL='1')
def load(name,path):
 spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module

def variant(name):
 global EnglishChatBot
 if name=='candidate':
  from backend.providers.speech_local import ParakeetAsr
  return ParakeetAsr
 import backend.voice_grounding as grounding
 import backend.voice_prompt as prompts
 old=load('backend._baseline_voice_grounding',ROOT/'baseline/voice_grounding.py')
 old.DATA=grounding.DATA
 for attr in ('resolve_voice_reply','grounding_note','ensure_chinese_reply'):setattr(grounding,attr,getattr(old,attr))
 old_prompt=load('backend._baseline_voice_prompt',ROOT/'baseline/voice_prompt.py')
 prompts.voice_prompt=old_prompt.voice_prompt;prompts.localized_voice_prompt=old_prompt.localized_voice_prompt
 if (ROOT/'baseline/english_chat.py').is_file():
  EnglishChatBot=load('backend._baseline_english_chat',ROOT/'baseline/english_chat.py').EnglishChatBot
 original=asyncio.create_subprocess_exec
 async def baseline_worker(*cmd,**kw):
  cmd=list(cmd)
  if 'scripts.local_speech_worker' in cmd:
   index=cmd.index('scripts.local_speech_worker');cmd[index-1:index+1]=[str((ROOT/'baseline/local_speech_worker.py').resolve())]
  return await original(*cmd,**kw)
 asyncio.create_subprocess_exec=baseline_worker
 return load('backend.providers._baseline_speech_local',ROOT/'baseline/speech_local.py').ParakeetAsr

def read_audio(case):
 path=Path(case['audio']);assert digest(path)==case['sha256']
 with wave.open(str(path),'rb') as f:
  assert f.getparams()[:3]==(1,2,16000)
  return f.readframes(f.getnframes())

async def one(case,repeat,mode,Asr,folder):
 pcm=read_audio(case);started=time.perf_counter();boundary=started;endpoint=None
 if mode=='live':
  asr=Asr();frames=[pcm[n:n+3200].ljust(3200,b'\0') for n in range(0,len(pcm),3200)]+[b'\0'*3200]*14
  boundary=started+len(pcm)/32000
  recognized=None
  try:
   for index,frame in enumerate(frames):
    await asyncio.sleep(max(0,started+index*.1-time.perf_counter()))
    result=await asr.feed(frame,(index+1)*100)
    if result.status==3:
     recognized={'text':result.text,**(result.timing or {})};endpoint={'startMediaMs':result.start_time,'endMediaMs':result.end_time,'fedMediaMs':(index+1)*100};break
   if recognized is None:recognized={'text':''}
  finally:await asr.reset()
 else:recognized=await worker('asr').request({'pcm':base64.b64encode(pcm).decode()})
 after_asr=time.perf_counter()
 row={'id':case['id'],'repeat':repeat,'mode':mode,'language':case['language'],'recognized':recognized['text'],
      'asrMs':(after_asr-boundary)*1000,'asrDetail':{k:v for k,v in recognized.items() if k!='pcm'},'endpoint':endpoint}
 if not recognized['text']:
  return {**row,'text':'','firstAudioMs':None,'audioSeconds':0}
 messages=tuple(BotMessage(**m) for m in case['history'])+(BotMessage('user',recognized['text']),)
 query=BotRequest(case['id'],case['id'],case['id'],messages,options={'grade':4},channel='voice')
 probe=Timing(case['id']);parts=[];jobs=[];pending='';first_pcm=None
 async def synth(text):
  nonlocal first_pcm
  result=await worker('tts').request({'text':text,'speed':.95,'volume':.2})
  if first_pcm is None:first_pcm=(time.perf_counter()-boundary)*1000
  return base64.b64decode(result['pcm']),{k:v for k,v in result.items() if k!='pcm'},text
 with use_timing(probe):
  source=EnglishChatBot().stream(query)
  try:
   async for event in source:
    if event.type=='text_delta':
     parts.append(event.text);pending+=event.text
     segments,pending=take_speech_segments(pending,first_chunk=not jobs)
     jobs.extend(asyncio.create_task(synth(s)) for s in segments)
   if pending.strip():jobs.append(asyncio.create_task(synth(pending)))
   speech=await asyncio.gather(*jobs)
  except BaseException as error:
   error.evaluation_row=row
   for task in jobs:task.cancel()
   await asyncio.gather(*jobs,return_exceptions=True)
   raise
  finally:await source.aclose()
 audio=b''.join(s[0] for s in speech);path=folder/f'{case["id"]}-{mode}-{repeat}.wav'
 with wave.open(str(path),'wb') as f:f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(audio)
 return {**row,'text':''.join(parts).strip(),'firstAudioMs':first_pcm,'totalMs':(time.perf_counter()-boundary)*1000,
         'audioSeconds':len(audio)/32000,'output':str(path),'timing':probe.metrics,
         'speech':[{'text':s[2],'pcmBytes':len(s[0]),'detail':s[1]} for s in speech]}

async def main(args):
 configure();Asr=variant(args.variant);folder=ROOT/args.variant;folder.mkdir(exist_ok=True)
 cases=json.loads((PACK/'cases.json').read_text());frozen=json.loads((PACK/'frozen.json').read_text())
 assert digest(PACK/'cases.json')==frozen['casesSha256']
 if args.variant=='candidate':assert all(digest(p)==h for p,h in frozen['sources'].items()),'Product changed since freeze'
 selected=cases if args.mode=='offline' or getattr(args,'all_live',False) else [c for c in cases if c['id'] in getattr(args,'live_ids',{'b01','b03','b07','b10','b15','b19','silence','quiet-noise'})]
 if args.mode=='offline':selected=[c for c in selected if c['language']!='control']
 result={'variant':args.variant,'mode':args.mode,'startedUtc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'frozen':frozen,'rows':[]}
 pool=LLMHttpPool();path=PACK/f'{args.variant}-{args.mode}.json'
 try:
  with use_llm_pool(pool):
   t=time.perf_counter();await worker('asr').request();await worker('tts').request();result['workerWarmupMs']=(time.perf_counter()-t)*1000
   # Development utterance, excluded from the new holdout.
   dev={'id':'dev-warmup','audio':'.local/omni-eval/inputs/o01.wav','history':[],'language':'en'};dev['sha256']=digest(dev['audio'])
   await one(dev,'warmup','offline',Asr,folder)
   for repeat in range(args.repeat):
    # Alternate direction without changing the frozen set.
    for case in selected if repeat%2==0 else list(reversed(selected)):
     try:row=await one(case,repeat,args.mode,Asr,folder)
     except Exception as e:row={**getattr(e,'evaluation_row',{}),'id':case['id'],'repeat':repeat,'mode':args.mode,'language':case['language'],'error':type(e).__name__,'detail':str(e)}
     result['rows'].append(row);path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
     print(json.dumps({k:v for k,v in row.items() if k in {'id','repeat','recognized','text','firstAudioMs','error','detail'}},ensure_ascii=False),flush=True)
 finally:await close_speech_workers();await pool.close()
 result['completedUtc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime());path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--variant',choices=['baseline','candidate'],required=True);parser.add_argument('--mode',choices=['offline','live'],default='offline');parser.add_argument('--repeat',type=int,default=2)
 asyncio.run(main(parser.parse_args()))
