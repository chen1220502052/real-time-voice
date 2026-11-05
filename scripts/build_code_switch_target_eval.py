"""New target-word holdout; no threshold trimming or ASR-based input selection."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/code-switch-v2');OUT=ROOT/'inputs';OUT.mkdir(parents=True,exist_ok=True)
PACK=Path('evals/code_switch_target');PACK.mkdir(parents=True,exist_ok=True)
# Frozen before running either recognizer; repetitions reuse the same input.
ITEMS=[
('mixed',['请用中文解释','cat','的中文意思。'],'cat','猫。',0,[]),
('mixed',['英语单词','cut','中文怎么说？'],'cut','切、割、剪；不能解释为 cat。',0,[]),
('mixed',['我想知道','cap','的中文意思。'],'cap','帽子或盖子；不能解释为 cup。',0,[]),
('mixed',['请解释','cup','的意思，给一个英语例句。'],'cup','杯子；一个含 cup 的完整自然英文例句。',0,[]),
('mixed',['请告诉我','ship','的中文意思。'],'ship','船、轮船；不能解释为 sheep。',0,[]),
('mixed',['sheep','的中文意思是什么？用中文回答。'],'sheep','羊、绵羊；不能解释为 ship。',0,[]),
('mixed',['请解释','tree','的中文意思。'],'tree','树；不能解释为 three。',300,[]),
('mixed',['英语单词','three','中文怎么说？'],'three','三；不能解释为 tree。',0,[]),
('mixed',['请用中文告诉我','duck','是什么意思。'],'duck','鸭子；不能解释为 dark。',0,[]),
('mixed',['我想知道','dark','的中文含义。'],'dark','黑暗、暗的；不能解释为 duck。',600,[]),
('mixed',['请用中文解释','door','的意思，并给两个英语例句。'],'door','门；两个含 door 的完整自然英语例句。',0,[]),
('mixed',['请告诉我','chair','的中文意思。'],'chair','椅子。',300,[]),
('mixed',['英语单词','window','中文怎么说？'],'window','窗户。',0,[]),
('mixed',['我想知道','window','的中文意思。'],'window','窗户。',600,[]),
('zh','你和我一样也是九岁吗？用中文回答。','','AI 没有人类年龄，不能自称九岁。',0,[('user','我今年九岁。')]),
('zh','我最喜欢什么运动？用中文回答。','','只提到喜欢跑步，没有声明最喜欢。',0,[('user','我喜欢跑步。')]),
('en','What does garden mean in Chinese? Give one English example.','garden','花园；一个含 garden 的自然完整英语例句。',0,[]),
('en','Say a short English greeting to a friend.','','自然完整的英语问候。',0,[]),
('zh','请解释“雨伞”的中文意思。','','解释中文雨伞，不在转写中编造英语目标词。',0,[]),
('zh','请告诉我“操场”的意思。','','解释中文操场，不在转写中编造英语目标词。',0,[]),
('zh','英语单词的中文含义是什么？','','确实缺词，应澄清，不得捏造单词。',0,[]),
('zh','请解释的意思，给一个英语例句。','','确实缺词，应澄清，不得捏造单词。',0,[]),
]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
rows=[]
for i,(language,parts,target,rubric,pause,history) in enumerate(ITEMS,1):
 id=f'n{i:02}';kind='task' if i<=18 else ('mandarin-word' if i<=20 else 'missing-target')
 parts=parts if isinstance(parts,list) else [parts];pieces=[np.zeros(3200,dtype='<i2')];cursor=3200;segments=[]
 for j,text in enumerate(parts):
  if j==1 and pause:pieces.append(np.zeros(pause*16,dtype='<i2'));cursor+=pause*16
  voice='Tingting' if re.search(r'[\u4e00-\u9fff]',text) else ('Samantha' if i%2 else 'Daniel')
  path=OUT/f'{id}-{j}.aiff';subprocess.run(['/usr/bin/say','-v',voice,'-r','160','-o',str(path),text],check=True)
  audio,sr=sf.read(path,dtype='float32')
  if audio.ndim>1:audio=audio.mean(axis=1)
  pcm=np.rint(np.clip(resample_poly(audio,16000,sr),-1,1-1/32768)*32768).astype('<i2')
  segments.append(dict(text=text,voice=voice,startFrame=cursor,frames=len(pcm),sourceSha256=sha(path),pcmSha256=hashlib.sha256(pcm.tobytes()).hexdigest()))
  pieces.append(pcm);cursor+=len(pcm)
 combined=np.concatenate(pieces);path=OUT/f'{id}.wav';sf.write(path,combined,16000,subtype='PCM_16');check,sr=sf.read(path,dtype='int16')
 for s in segments:assert hashlib.sha256(check[s['startFrame']:s['startFrame']+s['frames']].astype('<i2').tobytes()).hexdigest()==s['pcmSha256']
 rows.append(dict(id=id,language=language,kind=kind,text=''.join(parts),target=target,rubric=rubric,pauseMs=pause,history=[dict(role=r,content=c) for r,c in history],audio=str(path),sha256=sha(path),seconds=len(check)/16000,segments=segments))
rng=np.random.default_rng(5110526)
for id,audio in [('silence',np.zeros(48000)),('quiet-noise',rng.normal(0,.001,48000)),('loud-noise',rng.normal(0,.025,48000))]:
 p=OUT/(id+'.wav');sf.write(p,audio,16000,subtype='PCM_16');rows.append(dict(id=id,language='control',kind='noise',text='',target='',rubric='不转写、不回复。',history=[],audio=str(p),sha256=sha(p),seconds=3,segments=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print('Built',len(rows),'new intact inputs',sha(PACK/'cases.json'))
