"""Independent 600ms code-switch pause probes, preserving full source samples."""
import hashlib,json,subprocess
from pathlib import Path
import numpy as np,soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/code-switch-pause/inputs');ROOT.mkdir(exist_ok=True);PACK=Path('evals/code_switch_pause')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
items=[('请告诉我英语单词','teacher','的中文意思。','教师或老师。'),('请用中文解释','window','的意思，并给一个英语例句。','窗户及包含 window 的完整英语例句。'),('我想知道英语单词','monkey','中文怎么说。','猴子。'),('请用中文说','music','的意思。','音乐。')]
rows=[]
for i,(prefix,word,suffix,rubric) in enumerate(items,1):
 id=f'p{i:02}';pieces=[np.zeros(3200,dtype=np.int16)];cursor=3200;source=[];word_end=None
 for j,text in enumerate([prefix,word,suffix]):
  voice='Samantha' if j==1 else 'Tingting';path=ROOT/f'{id}-{j}.aiff'
  subprocess.run(['/usr/bin/say','-v',voice,'-r','155','-o',str(path),text],check=True)
  audio,sr=sf.read(path,dtype='float32');audio=np.rint(np.clip(resample_poly(audio,16000,sr),-1,1-1/32768)*32768).astype('<i2')
  if j==1:pieces.append(np.zeros(9600,dtype=np.int16));cursor+=9600
  source.append({'text':text,'startFrame':cursor,'frames':len(audio),'pcmSha256':hashlib.sha256(audio.tobytes()).hexdigest(),'sourceSha256':sha(path)})
  pieces.append(audio);cursor+=len(audio)
  if j==1:word_end=cursor
 path=ROOT/(id+'.wav');sf.write(path,np.concatenate(pieces),16000,subtype='PCM_16')
 check,sr=sf.read(path,dtype='int16')
 for s in source:assert hashlib.sha256(check[s['startFrame']:s['startFrame']+s['frames']].tobytes()).hexdigest()==s['pcmSha256']
 rows.append({'id':id,'language':'mixed','kind':'task','text':prefix+word+suffix,'target':word,'rubric':rubric,'audio':str(path),'sha256':sha(path),'seconds':len(check)/16000,'history':[],'segments':source,'pauseMs':600,'targetEndMediaMs':word_end/16})
for id in ['silence','quiet-noise']:
 audio=np.zeros(48000) if id=='silence' else np.random.default_rng(518820).normal(0,.001,48000)
 p=ROOT/(id+'.wav');sf.write(p,audio,16000,subtype='PCM_16');rows.append({'id':id,'language':'control','kind':'noise','text':'','target':'','rubric':'不转写、不回复。','audio':str(p),'sha256':sha(p),'seconds':3,'history':[],'segments':[]})
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print('Frozen independent pause probes',sha(PACK/'cases.json'))
