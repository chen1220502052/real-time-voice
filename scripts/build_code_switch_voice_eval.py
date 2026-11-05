"""Create fresh target-word holdout audio; never filter using ASR output."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/code-switch-eval');OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/code_switch_voice');PACK.mkdir(exist_ok=True)
# New utterances and target words, authored after development and before inference.
CASES=[
('mixed',['英语里面','cat','这个词的中文含义是什么？'],'猫，正确中文播报。','cat',[]),
('mixed',['请告诉我','cup','的中文意思。'],'杯子，不混淆 cap。','cup',[]),
('mixed',['用中文解释','red','，再给一个英语例句。'],'红色及包含 red 的完整英语例句。','red',[]),
('mixed',['wet','中文是什么意思？请回答中文。'],'湿的，不混淆 white 等词。','wet',[]),
('mixed',['请用中文解释','sad','，并给两个英语例句。'],'悲伤或难过，两个包含 sad 的完整英语例句。','sad',[]),
('mixed',['英语单词','map','中文怎么说？'],'地图，正确中文播报。','map',[]),
('mixed',['computer','这个词用中文怎么说？'],'电脑或计算机。','computer',[]),
('mixed',['butterfly','是什么意思？请用中文解释。'],'蝴蝶，不能只翻译 fly。','butterfly',[]),
('mixed',['请解释','breakfast','的中文意思，然后给一个英语例句。'],'早餐及包含 breakfast 的完整英语例句。','breakfast',[]),
('mixed',['英语','vegetable','的中文意思是什么？'],'蔬菜。','vegetable',[]),
('mixed',['请用中文解释','different','并给一个英语例句。'],'不同的及包含 different 的完整英语例句。','different',[]),
('mixed',['请用中文解释','comfortable','，再用英语写两个例句。'],'舒服或舒适，两个包含 comfortable 的完整英语例句。','comfortable',[]),
('zh','请用中文回答，你的真实生日是什么时候？','AI 身份，没有人类生日，不编造日期。','',[]),
('zh','我最喜欢的水果是哪一个？请用中文回答。','只喜欢葡萄，没有明确最喜欢的水果。','',[('user','我喜欢葡萄。')]),
('zh','故事里的小雨是十四岁。请用中文说她现在几岁。','小雨十四岁，不添加另一个年龄。','',[]),
('en','What does river mean in Chinese? Give an English example.','河流及包含 river 的完整英语例句。','river',[]),
('en','Invite me to play tennis in one short English sentence.','完整可用的邀请打网球的英语句子。','',[]),
('en','Have I said what my favourite animal is?','只说 enjoy dolphins，没有明确 favourite。','',[('user','I enjoy dolphins.')]),
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
for i,(language,text,rubric,target,history) in enumerate(CASES,1):
 id=f'c{i:02}';parts=text if isinstance(text,list) else [text];chunks=[];voices=[]
 for n,part in enumerate(parts):
  voice='Tingting' if re.search(r'[\u4e00-\u9fff]',part) else ('Samantha' if i%2 else 'Daniel')
  voices.append(voice);chunks.append(render(part,voice,OUT/f'{id}-{n}.aiff'))
 audio=np.concatenate([np.zeros(3200,dtype='float32'),*chunks]);path=OUT/f'{id}.wav';sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=id,language=language,text=''.join(parts),target=target,rubric=rubric,history=[dict(role=r,content=c) for r,c in history],audio=str(path),sha256=digest(path),seconds=len(audio)/16000,voices=voices))
rng=np.random.default_rng(626301)
for name,audio in [('silence',np.zeros(48000)),('quiet-noise',rng.normal(0,.001,48000)),('loud-noise',rng.normal(0,.025,48000))]:
 path=OUT/f'{name}.wav';sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=name,language='control',text='',target='',rubric='不产生转写或回复。',history=[],audio=str(path),sha256=digest(path),seconds=3,voices=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print('Saved',len(rows),'fresh inputs; no ASR used for input selection:',digest(PACK/'cases.json'))
