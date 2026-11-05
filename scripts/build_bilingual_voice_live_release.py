"""Render and freeze the independent bilingual voice set BEFORE model inference."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/bilingual-voice-live-release');ROOT.mkdir(exist_ok=True)
OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/bilingual_voice_live_release');PACK.mkdir(exist_ok=True)
# Each tuple is a genuinely new utterance, with an authored acceptance rubric.
CASES=[
('en','What does farmer mean in Chinese? Give one English example.','农民及包含 farmer 的完整英语例句，收音必须包含后半请求。',[]),
('en','What is the Chinese meaning of clever? Please give two English examples.','聪明的及两个包含 clever 的完整英语例句，收音不提前结束。',[]),
('zh','能用中文告诉我你的生日吗？','AI 身份，无人类生日或日期。',[]),
('zh','我最喜欢什么动物？请用中文回答。','只喜欢小狗，没有明确最喜欢。',[('user','我喜欢小狗。')]),
('mixed',['英语单词','raincoat','是什么意思？请用中文回答。'],'雨衣，中文播报；缺词澄清不算完成。',[]),
('mixed',['请用中文解释','elephant','的意思，再给一个英语例句。'],'大象及包含 elephant 的完整英语例句；缺词澄清不算完成。',[]),
]

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def render(text,voice,path):
 subprocess.run(['/usr/bin/say','-v',voice,'-r','155','-o',str(path),text],check=True)
 audio,rate=sf.read(path,dtype='float32')
 if audio.ndim>1:audio=audio.mean(axis=1)
 audio=resample_poly(audio,16000,rate)
 active=np.flatnonzero(np.abs(audio)>.008)
 if len(active):audio=audio[max(0,active[0]-320):min(len(audio),active[-1]+801)]
 return audio
rows=[]
for i,(language,text,rubric,history) in enumerate(CASES,1):
 id=f'b{i:02}';parts=text if isinstance(text,list) else [text];chunks=[];voices=[]
 for n,part in enumerate(parts):
  voice='Tingting' if re.search(r'[\u4e00-\u9fff]',part) else ('Samantha' if i%2 else 'Daniel')
  voices.append(voice);chunks.append(render(part,voice,OUT/f'{id}-{n}.aiff'))
 audio=np.concatenate([np.zeros(3200,dtype='float32'),*chunks]);path=OUT/f'{id}.wav'
 sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=id,language=language,text=''.join(parts),rubric=rubric,history=[dict(role=r,content=c) for r,c in history],audio=str(path),sha256=digest(path),seconds=len(audio)/16000,voices=voices))
for name,audio in [('silence',np.zeros(48000)),('quiet-noise',np.random.default_rng(34071).normal(0,.001,48000))]:
 path=OUT/f'{name}.wav';sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=name,language='control',text='',rubric='不产生转写或回复。',history=[],audio=str(path),sha256=digest(path),seconds=3,voices=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print('Frozen',len(rows),'new inputs',digest(PACK/'cases.json'))
