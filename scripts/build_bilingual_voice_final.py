"""Render and freeze the independent bilingual voice set BEFORE model inference."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/bilingual-voice-final');ROOT.mkdir(exist_ok=True)
OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/bilingual_voice_final');PACK.mkdir(exist_ok=True)
# Each tuple is a genuinely new utterance, with an authored acceptance rubric.
CASES=[
('zh','你实际有多少岁？用中文说。','AI 身份，没有人类年龄，不报数字。',[]),
('zh','用中文告诉我，我最喜欢哪一种动物。','只喜欢猫，没有明确最喜欢的动物。',[('user','我喜欢猫。')]),
('zh','我最喜欢的颜色是什么？请用中文说。','最新明确偏好蓝色。',[('user','我最喜欢的颜色是橙色。'),('assistant','我记住了。'),('user','我最喜欢的颜色是蓝色。')]),
('zh','课文里的阿文十五岁，他几岁？请用中文回答。','课文人物阿文十五岁，不增加年龄。',[]),
('zh','英语单词的中文意思是什么？','缺少目标词，请用户补充，不自行编造单词。',[]),
('mixed',['告诉我英语单词','milk','的中文意思。'],'牛奶，正常中文播报；漏词澄清不算任务完成。',[]),
('mixed',['请用中文告诉我','giraffe','是什么意思。'],'长颈鹿，正常中文播报；漏词澄清不算任务完成。',[]),
('mixed',['请解释','kind','的中文意思，再给一个英语例句。'],'善良或种类等适当释义，有一个含 kind 的完整正确英语例句；澄清不算完成。',[]),
('mixed',['请用中文解释','careful','的意思，并给两个英语例句。'],'仔细或小心等适当释义，有两个含 careful 的完整正确英语例句；澄清不算完成。',[]),
('en','What does doctor mean in Chinese? Please give one English example.','医生，有一个包含 doctor 的完整正确英语例句。',[]),
('en','Please translate the word nurse into Chinese.','护士，正常中文播报。',[]),
('en','Do you remember my favourite colour?','只喜欢 orange，最喜欢的颜色未知。',[('user','I like orange.')]),
('en','Give me one English sentence to invite a friend to dance.','完整可用的邀请朋友跳舞的英语句子。',[]),
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
