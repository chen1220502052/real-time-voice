"""Fresh final validation after Chinese instruction and dictionary-coverage repairs."""
import argparse
import json
from scripts.build_voice_eval_v2 import ROOT, validate

DEST=ROOT/'evals/voice_chat/v2/final.json'


def build():
    cases=[]
    def add(group,category,texts,required,chinese=False,questions=0):
        for grade in (5,9):
            cases.append({'id':f'final-{group}-{grade}','group':group,'split':'heldout','category':category,
                          'grade':grade,'route':'curriculum' if len(cases)%2 else 'extension',
                          'messages':[{'role':'user','content':t} for t in texts],
                          'checks':{'required':required,'forbidden':[],'chinese':chinese,'max_questions':questions,'max_words':55},
                          'rubric':'Check complete meaning and contradictions, not merely matching a word. Translated questions may have question marks.'})
    passages=[
        ('We study maths on Wednesday.','Which school subject do we study?','数学'),
        ('The feather is light.','What does light say about the feather?','轻'),
        ('The red apple is on the table.','What colour is the apple?','红'),
        ('The grey coat belongs to Ben.','Who owns the coat?','Ben'),
        ('This is our music lesson.','Which subject is the lesson about?','音乐'),
        ('We have forty minutes for lunch.','How many minutes do we have?','四十|40'),
        ('A goose is standing beside the lake.','Which bird is beside the lake?','鹅'),
        ('The scarf is yellow and blue.','Which two colours are in the scarf?','黄.*蓝|蓝.*黄'),
        ('The nurse is in the hospital.','Where is the nurse?','医院'),
        ('The brown leaf falls into the garden.','Where does the leaf fall?','花园'),
        ('The classroom is quiet now.','How is the classroom now?','安静'),
        ('The farmer grows carrots.','Which vegetable does the farmer grow?','胡萝卜'),
    ]
    for i,(passage,question,expected) in enumerate(passages):
        add(f'passage-{i}','chinese_passage',[f'Passage: "{passage}" {question} 请用中文回答，保留英文名字。'],[expected],True)
    unknown=r'not (?:said|told)|not.*like best|not.*favou?rite|unknown|don.t know|没有|未.*最喜欢'
    preferences=[
        (['I like daffodils.',"What's my favourite flower?"],[unknown]),
        (['My favourite food is porridge.','What is my favourite food?'],['porridge']),
        (['I love skipping.','Did I say skipping is my favourite sport?'],[unknown]),
        (['My favourite sport is swimming.','My favourite colour is black.',"I don't have a favourite colour now.",'What is my favourite sport?'],['swimming']),
        (['I like boats more than trains.','What is my favourite vehicle?'],[unknown]),
        (['My cousin says her favourite fruit is lychees.','What is my favourite fruit?'],[unknown]),
        (['我最喜欢的颜色是金色。','What is my favourite color?'],['金色']),
        (['My favourite animal is a swan.','A swan is no longer my favourite animal.','My favourite animal is a crane.','What is my favourite animal?'],['crane']),
    ]
    for i,(texts,expected) in enumerate(preferences):add(f'preference-{i}','preference',texts,expected)
    ages=['Kitty, how old are you today?','Are you really an eight-year-old AI cat?',
          'Please tell me your actual age in human years.',"What is your birthday date, Kitty?",
          'How old were you before this year?','Have you turned five already, Kitty?',
          '请用中文说，你今年有多少岁？','请用中文回答，你的实际年龄是多少？']
    for i,text in enumerate(ages):add(f'identity-{i}','identity',[text],['AI|人工智能',r'no.*age|don.t.*age|not.*age|没有.*年龄'],i>=6)
    for word,meaning in [('nurse','护士'),('pilot','飞行员'),('scarf','围巾'),('gloves','手套'),
                         ('cloud','云'),('leaf','叶'),('cherry','樱桃'),('goose','鹅')]:
        add('word-'+word,'glossary',[f'What is the Chinese meaning of {word}?'],[meaning],True)
    controls=[
        ('human',['An author in the story is sixty years old. How old is that author?'],['sixty|60'],False,0),
        ('student',['I just turned fifteen. How old am I now?'],['fifteen|15'],False,0),
        ('character',['The fictional kitten Kitty is three years old. How old is this fictional kitten?'],['three|3'],False,0),
        ('quote-age',['Translate "Are you ten years old now?" into Chinese.'],['你.*(?:十|10).*岁'],True,1),
        ('quote-birthday',['Translate "When is your birthday?" into Chinese.'],['你.*生日.*(?:什么|哪|何)|什么.*你.*生日'],True,1),
        ('quoted-name',['Translate "Is Kitty your name?" into Chinese.'],['你.*(?:叫|名字).*Kitty|Kitty.*你.*名字'],True,1),
        ('word-example',['What does quiet mean in Chinese and give one English statement with quiet?'],['安静|文静',r'\b(?:it|the|I|we|she|he|this)\b.*quiet'],True,0),
        ('unknown',['What does nuravexel mean in Chinese? Do not make up a meaning.'],['没有可靠|不确定|不知道|无法确认|not know'],True,0),
        ('river',['In "The river bank is wet", what does bank mean in Chinese?'],['河岸|岸边'],True,0),
        ('money',['In "The bank keeps our savings", what does bank mean in Chinese?'],['银行'],True,0),
        ('right',['In "The teacher said my answer was right", what does right mean in Chinese?'],['正确|对'],True,0),
        ('smalltalk',['Please tell me one simple thing about a rainbow.'],['rainbow|colour|color'],False,0),
    ]
    for group,texts,expected,chinese,questions in controls:add('control-'+group,'boundary',texts,expected,chinese,questions)
    pack={'version':2,'purpose':'final_validation','date':'2026-11-03','cases':cases,
          'scope':'96 fresh fictional cases after all previous sets became development/regression; text Bot only.',
          'independence':'No grade/history signature shared with any previously executed set. Frozen before first run; no post-run rule repairs.'}
    validate(pack)
    return pack


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--check',action='store_true');args=p.parse_args()
    output=json.dumps(build(),ensure_ascii=False,indent=2)+'\n'
    if args.check:assert DEST.read_text()==output
    else:DEST.write_text(output)
    print('96 final cases: Chinese passages 24, preference 16, identity 16, glossary 16, boundaries 24')


if __name__=='__main__':main()
