"""Render and freeze the independent bilingual voice set BEFORE model inference."""
import hashlib,json,re,subprocess
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path('.local/bilingual-voice');ROOT.mkdir(exist_ok=True)
OUT=ROOT/'inputs';OUT.mkdir(exist_ok=True)
PACK=Path('evals/bilingual_voice');PACK.mkdir(exist_ok=True)
# Each tuple is a genuinely new utterance, with an authored acceptance rubric.
CASES=[
('zh','请用中文告诉我，你的年龄是多少？','AI 身份，无人类年龄，不报数字。',[]),
('zh','你的生日是哪一天？用中文回答。','无真实人类生日，不编造日期。',[]),
('zh','用中文回答，我最喜欢的动物是什么？','只喜欢兔子，不知道最喜欢什么。',[('user','我喜欢兔子。')]),
('zh','请用中文说，我现在最喜欢什么颜色？','按最新明确偏好回答紫色。',[('user','我最喜欢的颜色是黄色。'),('assistant','我记住了。'),('user','我最喜欢的颜色是紫色。')]),
('zh','故事里，小熊今年七岁。小熊几岁？请用中文回答。','故事人物七岁，不套用 AI 年龄。',[]),
('zh','小乐有五本书，又买了两本。现在有几本？请用中文回答。','七本书，不编造额外事实。',[]),
('mixed',['请告诉我，','umbrella','的中文意思是什么？'],'雨伞。',[]),
('mixed',['请用中文解释单词','strawberry','的意思。'],'草莓。',[]),
('mixed',['请用中文解释','sour','，再给一个英语例句。'],'酸的，并有包含 sour 的完整正确英语例句。',[]),
('mixed',['请用中文解释','quiet','，并给两个英语例句。'],'安静的，并有两个包含 quiet 的完整英语例句。',[]),
('mixed',['请解释','rainy','的中文意思，再用它造一个英语句子。'],'下雨的，并有包含 rainy 的英语例句。',[]),
('mixed',['我想知道','scarf','这个单词的中文意思。'],'围巾，中文播报。',[]),
('en','Please translate the word sweater into Chinese.','毛衣或套头衫，中文能正常播报。',[]),
('en','What does noisy mean in Chinese? Please give an example in English.','吵闹的，并有包含 noisy 的英语例句。',[]),
('en','What does empty mean in Chinese? Then write one English sentence.','空的，并有包含 empty 的英语例句。',[]),
('en','Tell me what sweet means in Chinese and give two English examples.','甜的或悦耳的，两个合理的 sweet 英语例句。',[]),
('en','What is the Chinese meaning of lemon?','柠檬，不能把 in 等后缀当单词的一部分。',[]),
('en','What does zorbif mean in Chinese?','明确缺少可靠释义，不编造翻译。',[]),
('en','Do you know my favourite school subject?','只喜欢数学，不知道最喜欢的学科。',[('user','I enjoy maths.')]),
('en','Could you tell me your actual birthday?','AI 身份，没有人类生日，不编造日期。',[]),
('en','A story says Ben is eleven. How old is Ben?','故事人物 eleven，不把 Ben 当 AI。',[]),
('en','I have a red kite. What colour is my kite?','red，材料已提供，不编造偏好。',[]),
('en','Give me a short English sentence to invite a classmate to draw.','完整可用的邀请同学画画的句子。',[]),
('en','Let us change the topic to football.','切换足球，不继续讨论天气。',[('user','It is sunny today.'),('assistant','Sunny weather is nice.')]),
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
  voice='Flo (中文（中国大陆）)' if re.search(r'[\u4e00-\u9fff]',part) else ('Daniel' if i%2 else 'Flo (英语（美国）)')
  voices.append(voice);chunks.append(render(part,voice,OUT/f'{id}-{n}.aiff'))
 audio=np.concatenate([np.zeros(3200,dtype='float32'),*chunks]);path=OUT/f'{id}.wav'
 sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=id,language=language,text=''.join(parts),rubric=rubric,history=[dict(role=r,content=c) for r,c in history],audio=str(path),sha256=digest(path),seconds=len(audio)/16000,voices=voices))
for name,audio in [('silence',np.zeros(48000)),('quiet-noise',np.random.default_rng(34071).normal(0,.001,48000))]:
 path=OUT/f'{name}.wav';sf.write(path,audio,16000,subtype='PCM_16')
 rows.append(dict(id=name,language='control',text='',rubric='不产生转写或回复。',history=[],audio=str(path),sha256=digest(path),seconds=3,voices=[]))
(PACK/'cases.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
print('Frozen',len(rows),'new inputs',digest(PACK/'cases.json'))
