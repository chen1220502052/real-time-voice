"""Fresh independent voice corpus. Preserve every rendered sample, including weak consonants."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/code-switch-release');OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/code_switch_voice_release');PACK.mkdir(exist_ok=True)
CASES=[
('mixed',['请用中文解释','dog','这个英语词。'],'狗。','dog','task',[]),
('mixed',['英语单词','pen','的中文含义是什么？'],'钢笔或笔。','pen','task',[]),
('mixed',['请解释','pig','的中文意思，并给一个英语例句。'],'猪，包含 pig 的完整英语例句。','pig','task',[]),
('mixed',['请用中文告诉我','sun','是什么意思。'],'太阳。','sun','task',[]),
('mixed',['我想知道','bed','这个词的中文意思。'],'床。','bed','task',[]),
('mixed',['请用中文解释','fox','，给两个英语例句。'],'狐狸，两个包含 fox 的完整英语例句。','fox','task',[]),
('mixed',['hat','的中文意思是什么？请用中文回答。'],'帽子。','hat','task',[]),
('mixed',['英语单词','bag','中文怎么说？'],'包或袋子。','bag','task',[]),
('mixed',['用中文解释','fun','，再给一个英语例句。'],'乐趣或有趣，包含 fun 的完整英语例句。','fun','task',[]),
('mixed',['请告诉我','panda','的中文意思。'],'熊猫。','panda','task',[]),
('mixed',['请用中文解释','kitchen','的意思，并给一个英语例句。'],'厨房，包含 kitchen 的完整英语例句。','kitchen','task',[]),
('mixed',['football','是什么意思？用中文回答。'],'足球或美式橄榄球，释义不能自行混淆。','football','task',[]),
('zh','你有真正的生日吗？请用中文告诉我。','AI 身份，没有人类生日和日期。','','task',[]),
('zh','我最喜欢的颜色是什么？用中文说。','只喜欢绿色，没有明确最喜欢的颜色。','','task',[('user','我喜欢绿色。')]),
('en','What does mountain mean in Chinese? Give two short English examples.','山，两个包含 mountain 的完整英语例句。','mountain','task',[]),
('en','Give a friend a short English invitation to swim with me.','完整可用的邀请游泳的英语句子。','','task',[]),
('zh','请用中文解释“早餐”这个词的意思。','原话为中文，ASR 不应将早餐改成英文；回答早餐释义。','','mandarin-word',[]),
('zh','请用中文说说“森林”是什么意思。','原话为中文，ASR 不应自行加入英语目标词；回答森林释义。','','mandarin-word',[]),
('zh','请用中文解释，并写两个英语例句。','原话确实缺词，应澄清，不得根据语境捏造目标词。','','missing-target',[]),
]
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def pcm16(audio):return np.rint(np.clip(audio,-1,1-1/32768)*32768).astype('<i2')
rows=[]
for i,(lang,text,rubric,target,kind,history) in enumerate(CASES,1):
 id=f'r{i:02}';parts=text if isinstance(text,list) else [text];chunks=[np.zeros(3200,dtype=np.int16)];voices=[];segments=[];cursor=3200
 for n,part in enumerate(parts):
  voice='Tingting' if re.search(r'[\u4e00-\u9fff]',part) else ('Samantha' if i%2 else 'Daniel')
  path=OUT/f'{id}-{n}.aiff';subprocess.run(['/usr/bin/say','-v',voice,'-r','155','-o',str(path),part],check=True)
  audio,sr=sf.read(path,dtype='float32')
  if audio.ndim>1:audio=audio.mean(axis=1)
  # NO amplitude threshold or sample removal: plosives/fricatives are quiet.
  audio=pcm16(resample_poly(audio,16000,sr));chunks.append(audio);voices.append(voice)
  segments.append({'text':part,'voice':voice,'startFrame':cursor,'frames':len(audio),'pcmSha256':hashlib.sha256(audio.tobytes()).hexdigest(),'sourceSha256':digest(path)})
  cursor+=len(audio)
 combined=np.concatenate(chunks);wav=OUT/f'{id}.wav';sf.write(wav,combined,16000,subtype='PCM_16')
 check,rate=sf.read(wav,dtype='int16');assert rate==16000
 for seg in segments:
  copied=check[seg['startFrame']:seg['startFrame']+seg['frames']]
  assert hashlib.sha256(copied.tobytes()).hexdigest()==seg['pcmSha256'],'Rendered segment was damaged'
 rows.append(dict(id=id,language=lang,kind=kind,text=''.join(parts),target=target,rubric=rubric,history=[dict(role=r,content=c) for r,c in history],audio=str(wav),sha256=digest(wav),seconds=len(combined)/16000,voices=voices,segments=segments))
rng=np.random.default_rng(800612)
for name,audio in [('silence',np.zeros(48000)),('quiet-noise',rng.normal(0,.001,48000)),('loud-noise',rng.normal(0,.025,48000))]:
 path=OUT/f'{name}.wav';sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=name,language='control',kind='noise',text='',target='',rubric='不转写、不回复。',history=[],audio=str(path),sha256=digest(path),seconds=3,voices=[],segments=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print('Preserved all source samples, verified byte-for-byte:',len(rows),'new inputs',digest(PACK/'cases.json'))
