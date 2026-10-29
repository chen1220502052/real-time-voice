"""Reproducible offline authored dialogue expansion; no model/student records."""
import argparse
import hashlib
import json
from pathlib import Path
from backend.chat_library import DATA, question_key, lexical_tokens

# Human-authored topic slots; grammar and factual descriptions are deliberately small.
TOPICS = {
 'food': [('apples','fruit'),('bananas','fruit'),('oranges','fruit'),('pears','fruit'),('grapes','fruit'),('watermelon','fruit'),('rice','food'),('bread','food'),('noodles','food'),('eggs','food'),('milk','drink'),('water','drink'),('juice','drink'),('tea','drink')],
 'animals': [('cats','animals'),('dogs','animals'),('birds','animals'),('fish','animals'),('rabbits','animals'),('pandas','animals'),('monkeys','animals'),('horses','animals'),('ducks','animals')],
 'games': [('football','sport'),('basketball','sport'),('volleyball','sport'),('badminton','sport'),('ping-pong','sport'),('tennis','sport'),('swimming','sport'),('running','sport')],
 'subjects': [('English','subject'),('Chinese','subject'),('maths','subject'),('music','subject'),('art','subject'),('science','subject'),('PE','subject')],
 'weather': [('rain','weather'),('snow','weather'),('sunny days','weather'),('windy days','weather')],
 'activities': [('reading','activity'),('drawing','activity'),('singing','activity'),('dancing','activity'),('games','activity'),('books','activity')],
}

def make_pack():
 rows=[]
 def add(questions,reply,topic,required=(),contexts=None):
  row={'questions':questions,'reply':reply,'reviewed':True,'source':'原创常见多轮对话 v2 · 离线语法模板与词槽；自动词汇校验，非教材原文',
       'topic':topic,'requiredWords':list(required),'reviewMethod':'offline-template-and-vocabulary-check'}
  if contexts:row['contexts']=contexts
  rows.append(row)
 for topic,slots in TOPICS.items():
  for index,(noun,kind) in enumerate(slots):
   alternatives=[n for n,_ in slots if n!=noun]
   questions=[f'I like {noun}.',f'I love {noun}.',f'I really like {noun}.',f'I enjoy {noun}.']
   if topic=='food':
    questions.append(f"I like {'drinking' if kind=='drink' else 'eating'} {noun}.")
   elif topic=='games' and noun not in {'swimming','running'}:
    questions.append(f'I like playing {noun}.')
   elif topic=='subjects':
    questions.append(f'I like learning {noun}.')
   # Three independently eligible alternatives avoid a single cached-response loop.
   for other in alternatives[:3]:
    reply=f'Do you like {other}?'
    add(questions,reply,topic,(other,))
   follow={'food':'What do you like to eat?', 'animals':'What animals do you like?',
           'games':'What sports do you like?', 'subjects':'What subjects do you like?',
           'weather':'What weather do you like?', 'activities':'What do you like to do?'}[topic]
   context=f'Do you like {noun}?'
   add(['yes','yes I do','sure','of course'],follow,topic,(),[context])
   add(['no','no I do not','not really'],follow,topic,(),[context])
   add([f'I do not like {noun}.',f"I don't like {noun}."],follow,topic)
   if follow!='What do you like?':
    add(['yes','yes I do','sure','of course','no','no I do not','not really'],'What do you like?',topic,contexts=[context])
   # Bare nouns are answers only after the exact appropriate preceding question.
   bare_reply=f'Do you like {alternatives[0]}?'
   add([noun],bare_reply,topic,(alternatives[0],),[follow]+(['What food do you like?'] if topic=='food' else []))
   if kind in {'fruit','animals'}:
    fact=(f'{noun[0].upper()+noun[1:]} is a fruit.' if noun=='watermelon' else f'{noun[0].upper()+noun[1:]} are {kind}.')
    add([f'What are {noun}?',f'Tell me about {noun}.'],fact,topic,(noun,kind))
    if topic=='animals':
     add(['tell me more','what else'],f'What animals do you like?',topic,(noun,),[fact])
   elif kind in {'sport','subject'}:
    fact=f'{noun[0].upper()+noun[1:]} is a {kind}.'
    add([f'What is {noun}?',f'Tell me about {noun}.'],fact,topic,(noun,kind))
   if topic=='food':
    fact= f'{noun[0].upper()+noun[1:]} is a drink.' if kind=='drink' else (f'{noun[0].upper()+noun[1:]} is food.' if noun in {'rice','bread','watermelon'} else f'{noun[0].upper()+noun[1:]} are food.')
    add([f'Tell me about {noun}.'],fact,topic,(noun,))
 for topic,question in [('food','What food do you like?'),('animals','What animals do you like?'),('games','What sports do you like?'),('subjects','What subjects do you like?'),('weather','What weather do you like?')]:
  noun={'games':'sports','subjects':'school'}.get(topic,topic)
  add([f'Let us talk about {noun}.',f'Can we talk about {noun}?',f'I want to talk about {noun}.'],question,topic)
 # Short follow-ups are safe only in these explicit contexts.
 add(['why','why is that'], 'Sleep helps cats rest.', 'animals', ('cats','sleep','rest'), ['Cats sleep a lot.'])
 add(['Tell me a fun fact about cats.'], 'Cats sleep a lot.', 'animals', ('cats','sleep'))
 add(['I do not know',"I don't know",'I am not sure','我不知道'], 'What do you like?', 'social')
 add(['What about you?','And you?'], 'I am here to talk with you.', 'social', contexts=['How are you?','Hi! How are you?'])
 add(['thanks','thank you','thanks a lot','thank you very much'], 'You are welcome.', 'social')
 add(['I am fine','I am good','I am happy'], 'What do you like to do?', 'social', contexts=['How are you?','Hi! How are you?'])
 # Stable de-duplication, preserving explicit context and prerequisites.
 unique={json.dumps(r,sort_keys=True,ensure_ascii=False):r for r in rows}
 answers=list(unique.values())
 for row in sentence_pack()['sentences']:
  answers.append({'questions':['Read a sentence from this unit.','Give me a textbook sentence.','读一句本单元课文'],
    'reply':'课文原句：'+row['text'],'quote':row['text'],'origin':'textbook',
    'chapterId':row['chapterId'],'evidenceSha256':row['evidenceSha256'],
    'currentUnitOnly':True,'reviewed':True,'source':row['source']})
 pairs={
  'Whose skipping rope is it?':"It is Tom's skipping rope.",
  'What does she look like?':'She is young. She has long black hair.',
  'Do you like her class?':'Yes. She sings well.',
  'Does he like basketball?':"Yes, he does. It's fun for him to play sports.",
  'Sam, do you like PE?':'Yes, I do.',
  'Tim, do you like oranges?':'Yes, I do.',
  'Tom, what subjects do you like?':'I like Music and PE.',
  'Uncle Li, how many apple trees do you have?':'I have seventeen apple trees.',
  'Can I have some juice?':'Sure! Here you are.',
 }
 curriculum=json.loads((DATA/'curriculum-vocabulary.json').read_text())
 for chapter in curriculum['chapters']:
  for page in chapter['curriculum'].get('lessonEvidence',[]):
   lines=page.get('sentences',[])
   for question,answer in pairs.items():
    if question in lines and answer in lines:
     answers.append({'questions':['In the book, '+question,'课文里，'+question],
      'reply':'课文原句：'+answer,'quote':answer,'bookQuestion':question,'origin':'textbook',
      'chapterId':chapter['id'],'evidenceSha256':page['sha256'],'currentUnitOnly':True,
      'reviewed':True,'source':page['image']})
 return {'version':1,'answers':answers}


def sentence_pack():
 pack=json.loads((DATA/'curriculum-vocabulary.json').read_text());rows=[];seen=set()
 for chapter in pack['chapters']:
  for page in chapter['curriculum'].get('lessonEvidence',[]):
   for line in page.get('sentences',[]):
    # Keep complete transcribed sentences only; exclude labels, exercises and fragments.
    if not line.endswith(tuple('.!?')) or '...' in line or '/' in line or len(lexical_tokens(line))<2:
     continue
    key=(chapter['id'],page['sha256'],line)
    if key in seen:continue
    seen.add(key)
    rows.append({'chapterId':chapter['id'],'text':line,'evidenceSha256':page['sha256'],'source':page['image']})
 return {'version':1,'sentences':rows}


def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
 for name,pack in [('bulk-replies.json',make_pack()),('textbook-sentences.json',sentence_pack())]:
  path=DATA/name
  if args.check:
   assert json.loads(path.read_text())==pack,name+' is stale'
  else:path.write_text(json.dumps(pack,ensure_ascii=False,indent=2)+'\n')
  print(json.dumps({'file':str(path),'rows':len(pack.get('answers',pack.get('sentences',[])))},ensure_ascii=False))
if __name__=='__main__':main()
