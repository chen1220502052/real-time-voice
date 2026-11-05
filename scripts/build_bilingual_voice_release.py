"""Render and freeze the independent bilingual voice set BEFORE model inference."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/bilingual-voice-release');ROOT.mkdir(exist_ok=True)
OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/bilingual_voice_release');PACK.mkdir(exist_ok=True)
# Each tuple is a genuinely new utterance, with an authored acceptance rubric.
CASES=[
('zh','请用中文回答，你现在多大了？','AI 身份，没有人类年龄。',[]),
('zh','你有真正的生日吗？用中文回答。','没有人类生日，不报日期。',[]),
('zh','请用中文说，我最喜欢的水果是什么？','只喜欢苹果，不知道最喜欢的水果。',[('user','我喜欢苹果。')]),
('zh','我最喜欢的科目是什么？用中文回答。','按最新明确偏好回答美术。',[('user','我最喜欢的科目是音乐。'),('assistant','我记住了。'),('user','我最喜欢的科目是美术。')]),
('zh','课文里，莉莉是九岁。请用中文回答，莉莉几岁？','课文人物九岁，不套用 AI 身份。',[]),
('mixed',['英语单词','pencil','是什么意思？请用中文回答。'],'铅笔，正常中文播报。',[]),
('mixed',['告诉我','tiger','的中文意思。'],'老虎，正常中文播报。',[]),
('mixed',['请用中文解释','happy','，并给一个英语例句。'],'高兴，包含 happy 的完整英语例句。',[]),
('mixed',['请用中文解释','cold','，再写两个英语例句。'],'寒冷或感冒等适当释义，两个正确的 cold 英语例句。',[]),
('mixed',['请解释','tall','的中文意思，并用它写一个英语句子。'],'高的，有包含 tall 的完整英语例句。',[]),
('mixed',['英语单词','bread','中文怎么说？'],'面包，正常中文播报。',[]),
('mixed',['请用中文解释','yellow','的意思，再用英语造一个句子。'],'黄色，有包含 yellow 的完整英语例句。',[]),
('en','Please translate the word potato into Chinese.','土豆或马铃薯，正常中文播报。',[]),
('en','How do you say banana in Chinese?','香蕉，正常中文播报。',[]),
('en','What does brave mean in Chinese? Give me an example in English.','勇敢，包含 brave 的完整英语例句。',[]),
('en','Tell me the Chinese meaning of friendly. Then make one English sentence.','友好的，包含 friendly 的完整英语例句。',[]),
('en','What does slow mean in Chinese? Write two short English example sentences.','缓慢，包含 slow 的两个完整正确英语例句。',[]),
('en','What is my favourite sport?','只喜欢 swimming，最喜欢的运动未知。',[('user','I like swimming.')]),
('en','In a story, Jo is ten years old. How old is Jo?','Jo 是十岁，不套用 AI 身份。',[]),
('en','Give me a short English question inviting a friend to sing.','完整、可用的邀请朋友唱歌的英语问句。',[]),
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
