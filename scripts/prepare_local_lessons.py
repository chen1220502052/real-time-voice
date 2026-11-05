"""Reproducible local ASR alignment and Kokoro fallback for source-backed sentences.

Stages run in isolated environments: prepare/publish use .venv, transcribe uses
.local/asr-venv, synthesize uses .local/tts-venv. Only model download needs network.
Downloaded source files and existing reviewed dialogue intervals are preserved.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

from scripts.lesson_alignment import align_references, tokens

ROOT=Path(__file__).resolve().parents[1]
RES=ROOT/'英语'
WORK=RES/'本地语音处理'
MODEL='mlx-community/whisper-large-v3-turbo'

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    temporary.replace(path)

def load(name):return json.loads((WORK/name).read_text())

def prepare():
    from backend.chapter_practice import ChapterLibrary, _id
    library=ChapterLibrary(RES)
    recordings={}
    references=[]
    chapters={}
    source=ROOT/'backend/data/chat/textbook-sentences.json'
    reference_rows=json.loads(source.read_text())['sentences']
    supplement=ROOT/'backend/data/practice/supplemental-sentences.json'
    if supplement.exists():
        for page_index,page in enumerate(json.loads(supplement.read_text())['pages']):
            for sentence_index,text in enumerate(page['sentences']):
                reference_rows.append({'id':f'supplement:{page_index}:{sentence_index}','text':text,
                    'chapterId':page['chapterId'],'source':page['source'],
                    'evidenceSha256':page['evidenceSha256'],'sectionTitle':page['unit']})
    for number,row in enumerate(reference_rows):
        chapter=library.chapters.get(row['chapterId'])
        image=RES/'科普版'/row['source']
        if not chapter or not image.is_file() or digest(image)!=row['evidenceSha256']:continue
        ref={'id':row.get('id',f'verified:{number}'),'text':row['text'],'chapterId':chapter['id'],
             'evidencePath':str(image.relative_to(RES)),'evidenceSha256':row['evidenceSha256'],
             'textSource':'image-verified'}
        references.append(ref)
        page=next((p for p in chapter.get('textbookPages',[])
                   if library.textbook_pages.get(p['id'])==image),None)
        audio_id=page.get('recordingId') if page else None
        if audio_id not in library.audio:continue
        path=library.audio[audio_id]
        sha=digest(path)
        recording=recordings.setdefault(chapter['id']+sha,{'path':str(path.relative_to(RES)),'sha256':sha,
            'chapterId':chapter['id'],'chapterKey':{k:chapter[k] for k in ['publisher','grade','edition','unit']},
            'existingRecordingId':audio_id,'title':'教材对话','sourceLabel':'教材录音',
            'audioKind':'recorded','references':[]})
        recording['references'].append(ref)
    source=RES/'科普版/初中本地课文词表索引.json'
    books=json.loads(source.read_text()) if source.is_file() else []
    # Explicit edition mappings. The old grade-eight upper book is kept separate.
    editions={('7年级','上册'):'上册_2024年8月第1版_完整教材',
              ('7年级','下册'):'下册_2024年12月第1版_完整教材',
              ('8年级','下册'):'下册_2013年10月第1版_2024重印_完整教材',
              ('9年级','上册'):'上册_2014年7月第1版_2025重印_完整教材',
              ('9年级','下册'):'下册_2014年10月第1版_2024重印_完整教材'}
    def unit_number(value):
        match=re.match(r'unit\s*(\d+)\b',value,re.I)
        return int(match[1]) if match else None
    for book in books:
        for unit in book['chapters']:
            if not unit['sections']:continue
            number=unit_number(unit['unit'])
            chapter=next((c for c in library.chapters.values() if c['publisher']=='科普版'
                and c['grade']==book['grade'] and c['edition']==editions.get((book['grade'],book['term']))
                and number is not None and unit_number(c['unit'])==number),None)
            if chapter is None:
                chapter={'publisher':'科普版','grade':book['grade'],'edition':book['edition'],
                         'unit':unit['unit'],'id':_id('科普版',book['grade'],book['edition'],unit['unit']),
                         'sourcePage':unit['source_page'],'vocabulary':[],
                         'question':'What is this lesson about?','example':'Tell me what you learned.',
                         'tasks':[],'textKind':'第三方同步资料','vocabularyKind':'第三方词表，未核验为教材完整词表'}
                chapters[chapter['id']]=chapter
            for section in unit['sections']:
                path=(RES/'科普版'/section['local_audio']).resolve()
                if not path.is_relative_to(RES) or not path.is_file():continue
                sha=digest(path)
                refs=[]
                for i,sentence in enumerate(section['sentences']):
                    text=sentence.get('english')
                    if not isinstance(text,str) or not 2<=len(tokens(text))<=65:continue
                    refs.append({'id':f'{section["lesson_id"]}:{i}','text':text.strip(),
                                 'chinese':sentence.get('chinese','') if isinstance(sentence.get('chinese',''),str) else '',
                                 'textSource':'third-party-reference'})
                recordings[chapter['id']+sha]={'path':str(path.relative_to(RES)),'sha256':sha,
                    'chapterId':chapter['id'],'chapterKey':{k:chapter[k] for k in ['publisher','grade','edition','unit']},
                    'title':section['title'] or unit['unit'],'sourcePage':section['source_page'],
                    'sourceLabel':'第三方同步录音','audioKind':'recorded','references':refs,
                    'referencePath':str(source.relative_to(RES)),'referenceSha256':digest(source)}
    data={'version':1,'model':MODEL,'chapterAdditions':list(chapters.values()),
          'recordings':list(recordings.values()),'verifiedReferences':references}
    if not recordings and not references:
        raise ValueError('Install source-backed textbook text and media before preparing local lesson audio')
    save(WORK/'inventory.json',data)
    print(json.dumps({'recordings':len(recordings),'verifiedReferences':len(references),
                      'additionalEditionChapters':len(chapters)}),flush=True)

def transcribe(limit=None):
    import mlx_whisper
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    from huggingface_hub import snapshot_download
    model=Path(snapshot_download(MODEL,allow_patterns=['config.json','*.safetensors','*.npz'],local_files_only=True))
    weights=next(iter(model.glob('*.safetensors')),None) or next(model.glob('*.npz'))
    model_sha=digest(weights)
    rows=load('inventory.json')['recordings']
    if limit:rows=rows[:limit]
    started=time.monotonic()
    for index,row in enumerate(rows):
        target=WORK/'asr'/(row['sha256']+'.json')
        if target.exists() and json.loads(target.read_text()).get('modelSha256')==model_sha:continue
        source=RES/row['path']
        if digest(source)!=row['sha256']:raise ValueError('Changed ASR source')
        samples,rate=sf.read(source,dtype='float32',always_2d=True)
        mono=samples.mean(axis=1)
        if rate!=16000:
            factor=gcd(rate,16000);mono=resample_poly(mono,16000//factor,rate//factor)
        begun=time.monotonic()
        result=mlx_whisper.transcribe(np.asarray(mono,dtype=np.float32),path_or_hf_repo=str(model),
            language='en',word_timestamps=True,condition_on_previous_text=False,temperature=0,
            verbose=None,hallucination_silence_threshold=2)
        result.update(sha256=row['sha256'],model=MODEL,modelSha256=model_sha,
                      duration=len(mono)/16000,elapsedSeconds=round(time.monotonic()-begun,3),
                      peakAllocatedGB=round(mx.get_peak_memory()/1e9,3))
        save(target,result)
        print(json.dumps({'done':index+1,'total':len(rows),'audioSeconds':result['duration'],
                          'seconds':result['elapsedSeconds'],'peakGB':result['peakAllocatedGB'],
                          'elapsed':round(time.monotonic()-started,1)}),flush=True)

def align():
    inventory=load('inventory.json')
    matched=set();output=[];pending=[]
    for row in inventory['recordings']:
        target=WORK/'asr'/(row['sha256']+'.json')
        if not target.exists():
            pending.append(row['sha256']);continue
        asr=json.loads(target.read_text())
        if asr.get('sha256')!=row['sha256'] or digest(RES/row['path'])!=row['sha256']:
            raise ValueError('Stale ASR transcript')
        sentences=align_references(row['references'],asr,asr['duration'])
        matched.update(s['id'] for s in sentences)
        output.append({k:v for k,v in row.items() if k!='references'}|{
            'duration':asr['duration'],'sentences':sentences,
            'alignmentModel':asr['model'],'alignmentModelSha256':asr['modelSha256']})
    missing=[r for r in inventory['verifiedReferences'] if r['id'] not in matched]
    save(WORK/'alignment.json',{'version':1,'recordings':output,'missingVerifiedSentences':missing,
                              'pendingTranscripts':pending})
    print(json.dumps({'recordings':len(output),'alignedSentences':sum(len(r['sentences']) for r in output),
                      'missingVerifiedSentences':len(missing),'pendingTranscripts':len(pending)}),flush=True)

def synthesize():
    import numpy as np
    import soundfile as sf
    import torch
    from kokoro import KModel,KPipeline
    from huggingface_hub import snapshot_download
    torch.set_num_threads(4)
    model=Path(snapshot_download('hexgrad/Kokoro-82M',allow_patterns=['config.json','kokoro-v1_0.pth','voices/af_heart.pt'],local_files_only=True))
    model_sha=digest(model/'kokoro-v1_0.pth')
    engine=KModel(repo_id='hexgrad/Kokoro-82M',config=str(model/'config.json'),model=str(model/'kokoro-v1_0.pth')).eval()
    pipeline=KPipeline(lang_code='a',repo_id='hexgrad/Kokoro-82M',model=engine)
    generated=[]
    for row in load('alignment.json')['missingVerifiedSentences']:
        key=hashlib.sha256((row['text']+model_sha+'af_heart:.9').encode()).hexdigest()[:24]
        path=WORK/'tts'/(key+'.wav');meta=path.with_suffix('.json')
        if meta.exists():
            entry=json.loads(meta.read_text())
            if path.exists() and digest(path)==entry['sha256']:
                generated.append(entry|{'reference':row});continue
        if digest(RES/row['evidencePath'])!=row['evidenceSha256']:raise ValueError('Changed verified image')
        chunks=list(pipeline(row['text'],voice=str(model/'voices/af_heart.pt'),speed=.9))
        audio=np.concatenate([c.audio.cpu().numpy() for c in chunks if c.audio is not None])
        audio=np.pad(audio,(2400,3600))
        peak=float(np.max(np.abs(audio)))
        if not np.isfinite(audio).all() or not .2<len(audio)/24000<=18 or peak<.01:
            raise ValueError('Invalid synthesized sentence')
        if peak>.95:audio*=.95/peak
        path.parent.mkdir(parents=True,exist_ok=True);sf.write(path,audio,24000,subtype='PCM_16')
        entry={'path':str(path.relative_to(RES)),'sha256':digest(path),'duration':len(audio)/24000,
               'audioKind':'synthetic','model':'hexgrad/Kokoro-82M','modelSha256':model_sha,
               'voice':'af_heart','speed':.9,'reference':row}
        save(meta,entry);generated.append(entry)
        print(json.dumps({'generated':len(generated),'text':row['text']},ensure_ascii=False),flush=True)
    groups={}
    for entry in generated:
        reference=entry['reference']
        groups.setdefault((reference['chapterId'],reference['evidenceSha256']),[]).append(entry)
    combined=[]
    for (chapter_id,image_sha),entries in groups.items():
        chunks=[];sentences=[];cursor=0
        for index,entry in enumerate(entries):
            samples,rate=sf.read(RES/entry['path'],dtype='float32')
            if rate!=24000:raise ValueError('Unexpected sentence sample rate')
            if index:
                gap=np.zeros(3600,dtype=np.float32);chunks.append(gap);cursor+=len(gap)
            start=cursor/24000;chunks.append(samples);cursor+=len(samples)
            sentences.append({**entry['reference'],'start':round(start,3),'end':round(cursor/24000,3),
                              'alignment':'exact-generated-sentence'})
        identity=json.dumps([(e['sha256'],e['reference']['id']) for e in entries],sort_keys=True)
        key=hashlib.sha256(identity.encode()).hexdigest()[:24]
        path=WORK/'tts/groups'/(key+'.wav');path.parent.mkdir(parents=True,exist_ok=True)
        sf.write(path,np.concatenate(chunks),24000,subtype='PCM_16')
        combined.append({'chapterId':chapter_id,'path':str(path.relative_to(RES)),'sha256':digest(path),
            'duration':cursor/24000,'audioKind':'synthetic','sourceLabel':'本地 AI 合成示范音',
            'title':'教材节选 · '+sentences[0]['text'][:40]+'（本地合成）',
            'model':'hexgrad/Kokoro-82M','modelSha256':model_sha,'voice':'af_heart','speed':.9,
            'sentences':sentences})
    save(WORK/'synthesis.json',{'version':1,'sentences':generated,'recordings':combined})

def publish(allow_partial=False):
    inventory=load('inventory.json');alignment=load('alignment.json')
    if alignment['pendingTranscripts'] and not allow_partial:
        raise ValueError('Finish all inventoried ASR tasks before publishing')
    synthesis=load('synthesis.json')
    if {r['id'] for r in alignment['missingVerifiedSentences']}!={r['reference']['id'] for r in synthesis['sentences']}:
        raise ValueError('Finish synthesis for the current verified references before publishing')
    rows=list(alignment['recordings'])
    rows.extend(synthesis['recordings'])
    from scripts.assemble_shadow_audit_release import validate_intervals
    for row in rows:
        path=(RES/row['path']).resolve()
        if not path.is_relative_to(RES) or digest(path)!=row['sha256']:
            raise ValueError('Changed aligned audio before publication')
        validate_intervals(row['sentences'],row['duration'])
    def timestamp(seconds, separator='.'):
        milliseconds=round(seconds*1000)
        hours,rest=divmod(milliseconds,3600000);minutes,rest=divmod(rest,60000);seconds,ms=divmod(rest,1000)
        return f'{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}'
    for row in rows:
        if not row['sentences']:continue
        folder=WORK/'字幕'/row['chapterId'];folder.mkdir(parents=True,exist_ok=True)
        vtt=['WEBVTT',''];srt=[]
        for i,line in enumerate(row['sentences'],1):
            text=' '.join(line['text'].split())
            vtt.extend([str(i),f"{timestamp(line['start'])} --> {timestamp(line['end'])}",text,''])
            srt.extend([str(i),f"{timestamp(line['start'],',')} --> {timestamp(line['end'],',')}",text,''])
        path=folder/(row['sha256']+'.vtt');path.write_text('\n'.join(vtt))
        path.with_suffix('.srt').write_text('\n'.join(srt))
        row['subtitle']={'path':str(path.relative_to(RES)),'sha256':digest(path)}
    # A JPEG was indexed as a recording. Preserve it as a readable source image.
    bad_sha='49f9e1f33a7bcd6e868c20be607a37f9913c58a8ea6719085c51f4454277ec40'
    image_rows=[]
    source=RES/'科普版/本地课文音频总索引.json'
    from backend.chapter_practice import _id
    for chapter in json.loads(source.read_text()):
        for asset in chapter.get('assets',[]):
            if asset.get('sha256')!=bad_sha or asset.get('kind')!='分段音频':continue
            old=RES/'科普版'/asset['path']
            if digest(old)!=bad_sha:raise ValueError('Changed misclassified image')
            image=WORK/'recovered-images'/(bad_sha+'.jpg')
            image.parent.mkdir(parents=True,exist_ok=True);image.write_bytes(old.read_bytes())
            image_rows.append({'chapterId':_id('科普版',chapter['grade'],chapter['edition'],chapter['unit']),
                               'path':str(image.relative_to(RES)),'sha256':bad_sha,'label':'教材原图 · 已纠正资源类型'})
    pack={'version':1,'chapterAdditions':inventory['chapterAdditions'],'recordings':rows,
          'recoveredImages':image_rows,'excludedAudioSha256':[bad_sha]}
    save(WORK/'lesson-audio.json',pack)
    report={'recordings':len(rows),'alignedSourceSentences':sum(len(r['sentences']) for r in alignment['recordings']),
            'syntheticSentences':len(load('synthesis.json')['sentences']),
            'syntheticRecordings':len(load('synthesis.json')['recordings']),
            'additionalEditionChapters':len(pack['chapterAdditions']),'recoveredImages':len(image_rows),
            'pendingTranscripts':len(alignment['pendingTranscripts']),
            'model':MODEL,'scope':'Reference-matched sentences only; ASR drafts are not textbook text.'}
    transcripts=[json.loads(p.read_text()) for p in (WORK/'asr').glob('*.json')]
    report.update(asrAudioHours=round(sum(r['duration'] for r in transcripts)/3600,3),
                  asrComputeSeconds=round(sum(r['elapsedSeconds'] for r in transcripts),2),
                  asrPeakMetalGB=max((r['peakAllocatedGB'] for r in transcripts),default=0),
                  verifiedReferenceSentences=len(inventory['verifiedReferences']),
                  newlySupplementedChapters=len({r['reference']['chapterId'] for r in synthesis['sentences']
                                                 if r['reference']['id'].startswith('supplement:')}))
    save(WORK/'report.json',report);print(json.dumps(report),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=['prepare','transcribe','align','synthesize','publish'])
    parser.add_argument('--limit',type=int)
    parser.add_argument('--allow-partial',action='store_true',help='Preview only completed sources; report remaining ASR tasks')
    args=parser.parse_args()
    if args.stage=='transcribe':transcribe(args.limit)
    elif args.stage=='publish':publish(args.allow_partial)
    else:globals()[args.stage]()
