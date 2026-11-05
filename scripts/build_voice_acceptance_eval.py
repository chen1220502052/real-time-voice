"""Last independent acceptance after bounded Chinese-language repair was added."""
import argparse
import json
from scripts.build_voice_eval_v2 import ROOT, validate

DEST=ROOT/'evals/voice_chat/v2/acceptance.json'


def build():
    cases=[]
    def add(group,category,texts,required,chinese=False,questions=0):
        for grade in (4,8):
            cases.append({'id':f'accept-{group}-{grade}','group':group,'split':'heldout','category':category,
                          'grade':grade,'route':'extension' if len(cases)%2==0 else 'curriculum',
                          'messages':[{'role':'user','content':x} for x in texts],
                          'checks':{'required':required,'forbidden':[],'chinese':chinese,'max_questions':questions,'max_words':55},
                          'rubric':'Check meaning, requested language and unsupported facts, including the full answer after any matching word.'})
    passages=[
        ('The red pencil is beside the book.','Where is the pencil?','书.*旁|书.*边'),
        ('Anna has twelve ducks.','How many ducks does Anna have?','十二|12'),
        ('Our English lesson begins at nine.','Which school subject is the lesson about?','英语|英文'),
        ('The small box is heavy.','What does heavy say about the box?','重'),
        ('The birds are in the tall tree.','Where are the birds?','树'),
        ('Tom carries a green ruler.','What colour is the ruler?','绿'),
        ('The children eat oranges after school.','Which fruit do the children eat?','橙|橘'),
        ('The dog sleeps under the sofa.','Where does the dog sleep?','沙发.*下|下.*沙发'),
    ]
    for i,(text,q,expected) in enumerate(passages):
        add(f'passage-{i}','chinese_passage',[f'Read this passage: "{text}" {q} 请用中文回答，保留人物英文名字。'],[expected],True)
    unknown=r'not (?:said|told)|not.*like best|not.*favou?rite|unknown|don.t know|没有|未.*最喜欢'
    prefs=[
        (['I like gardening.', 'What is my favourite hobby?'],[unknown]),
        (['My favourite bird is a robin.', 'Did I say a robin is my favourite bird?'],['yes|是的','robin']),
        (['My favourite food is noodles.','My favourite colour is cyan.',"I don't have a favourite colour now.",'What is my favourite food?'],['noodles']),
        (['My friend says her favourite hobby is cycling.','What is my favourite hobby?'],[unknown]),
    ]
    for i,(texts,expected) in enumerate(prefs):add(f'preference-{i}','preference',texts,expected)
    ages=['Are you really a ten-year-old Kitty cat?','How old are you at this moment?',
          '请用中文回答，你现在多少岁？','Please tell me your real age, not a made-up number.']
    for i,text in enumerate(ages):add(f'identity-{i}','identity',[text],['AI|人工智能',r'no.*age|don.t.*age|not.*age|没有.*年龄'],i==2)
    for word,meaning in [('eraser','橡皮'),('pencil','铅笔'),('tiger','虎'),('sheep','羊')]:
        add('word-'+word,'glossary',[f'What does {word} mean in Chinese?'],[meaning],True)
    controls=[
        ('human',['The grandfather in our story is seventy-five. How old is he?'],['seventy.five|75'],False,0),
        ('fiction',['In a book the robot Kitty was made forty years ago. How long ago was that fictional robot made?'],['forty|40'],False,0),
        ('translation',['Translate "How old is the girl?" into Chinese.'],['女孩.*(?:几岁|多大|多少岁)'],True,1),
        ('unknown',['What is the Chinese meaning of "mivrelota"? Please do not invent an answer.'],['没有可靠|不确定|不知道|not know'],True,0),
    ]
    for group,texts,expected,chinese,questions in controls:add('control-'+group,'boundary',texts,expected,chinese,questions)
    pack={'version':2,'purpose':'acceptance_independent','date':'2026-11-03','cases':cases,
          'scope':'48 new cases after bounded language repair; text Bot, not ASR/TTS/audio.',
          'independence':'No history overlaps with any earlier set, even excluding grade. No post-run product or rule changes.'}
    validate(pack)
    return pack


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    text=json.dumps(build(),ensure_ascii=False,indent=2)+'\n'
    if args.check:assert DEST.read_text()==text
    else:DEST.write_text(text)
    print('48 acceptance-independent cases: Chinese passages 16, preference 8, identity 8, glossary 8, boundaries 8')


if __name__=='__main__':main()
