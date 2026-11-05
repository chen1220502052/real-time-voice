"""New target-word holdout; no threshold trimming or ASR-based input selection."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/code-switch-target-final');OUT=ROOT/'inputs';OUT.mkdir(parents=True,exist_ok=True)
PACK=Path('evals/code_switch_target_final');PACK.mkdir(parents=True,exist_ok=True)
# Frozen before running either recognizer; repetitions reuse the same input.
ITEMS=[
('mixed',['请用中文解释','fish','的中文意思。'],'fish','鱼。',0,[]),
('mixed',['英语单词','dish','中文怎么说？'],'dish','盘子或一道菜；不能解释为 fish。',0,[]),
('mixed',['我想知道','coat','的中文意思。'],'coat','外套；不能解释为 goat。',0,[]),
('mixed',['请解释','goat','的意思，给一个英语例句。'],'goat','山羊；一个含 goat 的完整自然英文例句。',0,[]),
('mixed',['请告诉我','light','的中文意思。'],'light','光、轻的、明亮的。',0,[]),
('mixed',['night','的中文意思是什么？用中文回答。'],'night','夜晚；不能解释为 light。',0,[]),
('mixed',['请解释','park','的中文意思。'],'park','公园或停车；不能解释为 bark。',300,[]),
('mixed',['英语单词','bark','中文怎么说？'],'bark','树皮或吠叫；不能解释为 park。',0,[]),
('mixed',['请用中文告诉我','desk','是什么意思。'],'desk','书桌。',0,[]),
('mixed',['我想知道','flag','的中文含义。'],'flag','旗子。',600,[]),
('mixed',['请用中文解释英语单词','window','的意思，并给一个英语例句。'],'window','窗户；一个含 window 的自然完整英语例句。',0,[]),
('mixed',['请告诉我英语单词','window','的中文意思。'],'window','窗户。',600,[]),
('zh','你有真正的出生日期吗？用中文回答。','','AI 没有人类出生日期，不编造日期。',0,[]),
('zh','我最喜欢吃哪种水果？用中文回答。','','只提到喜欢梨，没有声明最喜欢。',0,[('user','我喜欢梨。')]),
('en','What does flower mean in Chinese? Give one English example.','flower','花；一个含 flower 的自然完整英语例句。',0,[]),
('en','Say a short English goodbye to a friend.','','自然完整的英语告别。',0,[]),
('zh','请解释“书包”的中文意思。','','解释中文书包，不在转写中编造英语目标词。',0,[]),
('zh','请告诉我“台灯”的意思。','','解释中文台灯，不在转写中编造英语目标词。',0,[]),
('zh','英文单词的中文意思是什么？','','确实缺词，应澄清，不得捏造单词。',0,[]),
('zh','请告诉我的中文含义。','','确实缺词，应澄清，不得捏造单词。',0,[]),
]
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
rows=[]
for i,(language,parts,target,rubric,pause,history) in enumerate(ITEMS,1):
 id=f'n{i:02}';kind='task' if i<=16 else ('mandarin-word' if i<=18 else 'missing-target')
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
rng=np.random.default_rng(5110527)
for id,audio in [('silence',np.zeros(48000)),('quiet-noise',rng.normal(0,.001,48000)),('loud-noise',rng.normal(0,.025,48000))]:
 p=OUT/(id+'.wav');sf.write(p,audio,16000,subtype='PCM_16');rows.append(dict(id=id,language='control',kind='noise',text='',target='',rubric='不转写、不回复。',history=[],audio=str(p),sha256=sha(p),seconds=3,segments=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n');print('Built',len(rows),'new intact inputs',sha(PACK/'cases.json'))
