"""Render and freeze the independent bilingual voice set BEFORE model inference."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/bilingual-voice-acceptance');ROOT.mkdir(exist_ok=True)
OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/bilingual_voice_acceptance');PACK.mkdir(exist_ok=True)
# Each tuple is a genuinely new utterance, with an authored acceptance rubric.
CASES=[
('zh','请用中文说明你的真实年龄。','AI 身份，无人类年龄。',[]),
('zh','我最喜欢哪种动物？请用中文告诉我。','只喜欢企鹅，不知道最喜欢哪种动物。',[('user','我喜欢企鹅。')]),
('zh','请用中文说，我最喜欢的运动是什么？','最新明确偏好为跳绳。',[('user','我最喜欢的运动是跑步。'),('assistant','我记住了。'),('user','我最喜欢的运动是跳绳。')]),
('zh','故事里的汤姆十二岁，他多大？请用中文回答。','故事人物十二岁。',[]),
('zh','请用中文解释，并给两个英语例句。','缺少目标词，要求补充词，不自行选择单词。',[]),
('mixed',['请告诉我','watermelon','用中文怎么说。'],'西瓜，正确中文播报。',[]),
('mixed',['我想知道','dinosaur','的中文意思是什么。'],'恐龙，正确中文播报。',[]),
('mixed',['英语单词','schoolbag','的中文意思是什么？'],'书包，正确中文播报。',[]),
('mixed',['请用中文解释','hot','，再给一个英语例句。'],'热的及包含 hot 的完整英语例句；若漏词则澄清，澄清不算任务完成。',[]),
('mixed',['请用中文解释','busy','，并写两个英语例句。'],'忙碌及包含 busy 的两个完整英语例句；漏词澄清不算任务完成。',[]),
('mixed',['请用中文解释','green','的意思，并给一个英语例句。'],'绿色及包含 green 的完整英语例句；漏词澄清不算任务完成。',[]),
('en','What does rabbit mean in Chinese? Give me one English example.','兔子及包含 rabbit 的完整英语例句。',[]),
('en','What is the Chinese meaning of beautiful? Give two English example sentences.','美丽的及包含 beautiful 的两个完整英语例句。',[]),
('en','Please translate the word carrot into Chinese.','胡萝卜，正确中文播报。',[]),
('en','What is my favourite drink?','只喜欢 tea，不知道最喜欢的饮料。',[('user','I like tea.')]),
('en','Please invite a friend to read with me in one English sentence.','完整、可用的邀请朋友读书的英语句子。',[]),
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
