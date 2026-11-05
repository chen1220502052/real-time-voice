"""New target-word holdout; no threshold trimming or ASR-based input selection."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/code-switch-target-guard');OUT=ROOT/'inputs';OUT.mkdir(parents=True,exist_ok=True)
PACK=Path('evals/code_switch_target_guard');PACK.mkdir(parents=True,exist_ok=True)
# Frozen before running either recognizer; repetitions reuse the same input.
ITEMS=[
('mixed',['请告诉我','window','中文怎么说？'],'window','窗户。',0,[]),
('zh','请解释“房子”的中文意思。','','房屋、供人居住的建筑。',0,[]),
('en','What does apple mean in Chinese?','apple','苹果。',0,[]),
('zh','你有真实的人类生日吗？用中文回答。','','AI 没有人类生日。',0,[]),
('en','Tell me the Chinese meaning of dog.','dog','狗。',0,[]),
('zh','我想知道这个英语单词的中文意思。','','原话缺少实际单词，必须澄清。',0,[]),
('zh','请告诉我这个词的中文含义。','','原话缺少实际单词，必须澄清。',0,[]),
('zh','请用中文解释英文单词的意思，并给两个英语例句。','','原话缺少实际单词，必须澄清。',0,[]),
]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
rows=[]
for i,(language,parts,target,rubric,pause,history) in enumerate(ITEMS,1):
 id=f'g{i:02}';kind='task' if i<=5 else 'missing-target'
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
rng=np.random.default_rng(5110528)
for id,audio in [('silence',np.zeros(48000)),('quiet-noise',rng.normal(0,.001,48000)),('loud-noise',rng.normal(0,.025,48000))]:
 p=OUT/(id+'.wav');sf.write(p,audio,16000,subtype='PCM_16');rows.append(dict(id=id,language='control',kind='noise',text='',target='',rubric='不转写、不回复。',history=[],audio=str(p),sha256=sha(p),seconds=3,segments=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print('Built',len(rows),'new intact inputs',sha(PACK/'cases.json'))
