"""Final fresh short-passage and dialogue validation after all exposed sets became regressions."""
import argparse
import json
from scripts.build_voice_eval_v2 import ROOT, validate

DEST = ROOT / 'evals/voice_chat/v2/release-check.json'


def build():
    cases = []

    def add(group, category, texts, required, *, chinese=False, questions=0):
        for grade in (3, 8):
            cases.append({'id': f'release-{group}-{grade}', 'group': group, 'split': 'heldout',
                          'category': category, 'grade': grade,
                          'route': 'curriculum' if len(cases) % 2 else 'extension',
                          'messages': [{'role':'user','content':t} for t in texts],
                          'checks': {'required':required, 'forbidden':[], 'chinese':chinese,
                                     'max_questions':questions,'max_words':55},
                          'rubric':'Check requested facts and word senses in context; require Chinese when requested. '
                                   'A translated question is allowed to remain a question.'})

    passages = [
        ('The rabbit is under the chair.', 'Where is the rabbit?', '椅.*下|下.*椅'),
        ('A green cucumber is on the plate.', 'Which vegetable is on the plate?', '黄瓜'),
        ('The girl drinks milk in the kitchen.', 'Where is the girl?', '厨房'),
        ('There are three umbrellas by the door.', 'How many umbrellas are there?', '三|3'),
        ('We have art on Monday.', 'Which subject do we have on Monday?', '美术|艺术'),
        ('The boy buys a purple sweater.', 'What colour is the sweater?', '紫'),
        ('The doctor works in a hospital.', 'Where does the doctor work?', '医院'),
        ('A giraffe stands by the bridge.', 'Which animal is near the bridge?', '长颈鹿'),
        ('The empty bag is light.', 'What does light describe about the bag?', '轻'),
        ('The dog looks hungry.', 'How does the dog feel?', '饿'),
        ('The science lesson starts tomorrow.', 'When does the lesson start?', '明天'),
        ('The daughter finds her notebook.', 'What does she find?', '笔记本'),
    ]
    for i,(text,question,expected) in enumerate(passages):
        add(f'passage-{i}', 'chinese_passage', [f'Read "{text}" {question} 请用中文回答。'], [expected], chinese=True)

    unknown=r'not (?:said|told)|not.*like best|not.*favou?rite|unknown|don.t know|没有|未.*最喜欢'
    preferences=[
        (['I enjoy origami.', 'What is my favourite hobby?'],[unknown]),
        (['My favourite snack is chestnuts.', 'Did I say chestnuts are my favourite snack?'],['yes|是的','chestnuts']),
        (['I like owls.', 'Have I said owls are my favourite animal?'],[unknown]),
        (['My favourite sport is wrestling.', 'My favourite fruit is cherries.', "I don't have a favourite sport now.", 'What is my favourite fruit?'],['cherries']),
        (['My favourite colour is brown.', 'Now my favourite colour is white.', 'What is my favourite colour?'],['white']),
        (['My aunt said "My favourite food is bread."', 'What is my favourite food?'],[unknown]),
        (['我喜欢吃面包。','请用中文回答，我最喜欢的食物是什么？'],['还没有|没有明确|未.*最喜欢']),
        (['我最喜欢的运动是游泳。','请用中文回答，我最喜欢的运动是什么？'],['游泳']),
    ]
    for i,(texts,expected) in enumerate(preferences):
        add(f'preference-{i}','preference',texts,expected,chinese=i>=6)
    ages=['Tell me your age, Kitty, please.', 'Are you really a nine-year-old cat?',
          'How old are you right now, Kitty?', 'What is your real birthday?',
          'Have you turned twelve yet?', 'When were you born, Kitty the AI cat?',
          '请用中文回答，你到底几岁了？', '请用中文回答，你的真正生日是哪天？']
    for i,text in enumerate(ages):
        add(f'identity-{i}','identity',[text],['AI|人工智能',r'no.*age|don.t.*age|not.*age|没有.*年龄'],chinese=i>=6)
    for word,meaning in [('carrot','胡萝卜'),('potato','土豆|马铃薯'),('grape','葡萄'),('onion','洋葱'),
                         ('peach','桃'),('pear','梨'),('lemon','柠檬'),('bean','豆')]:
        add('glossary-'+word,'glossary',[f'Please tell me the Chinese meaning of "{word}".'],[meaning],chinese=True)
    controls=[
        ('human',['The woman Iris is twenty-nine. How old is Iris?'],['twenty.nine|29'],False,0),
        ('self',['I am ten years old now. What is my age now?'],['ten|10'],False,0),
        ('story',['In a story a robot called Kitty was built five years ago. How long ago was the story robot built?'],['five|5'],False,0),
        ('translate-age',['Translate "How old were you last year?" into Chinese.'],['你.*去年.*(?:几岁|多大)|去年.*你.*(?:几岁|多大)'],True,1),
        ('translate-name',['Please translate "Is your name Kitty?" into Chinese.'],['你.*(?:叫|名字).*Kitty'],True,1),
        ('example',['What does rainy mean in Chinese and give one simple English statement using rainy?'],['雨',r'\b(?:it|today|the|I|we)\b.*rainy'],True,0),
        ('unknown',['What is the Chinese translation of "drelvona"? Please avoid guessing.'],['没有可靠|不确定|不知道|无法确认|not know'],True,0),
        ('right',['The passage says "That spelling is right." Explain right in Chinese.'],['正确|对'],True,0),
    ]
    for group,texts,expected,chinese,questions in controls:
        add('control-'+group,'boundary',texts,expected,chinese=chinese,questions=questions)
    pack={'version':2,'purpose':'release_independent','date':'2026-11-03','cases':cases,
          'scope':'88 new short passages, word queries and dialogue scenarios; actual Bot text delivery, not audio.',
          'independence':'No exact grade/history signature in v1, exploratory v2 or confirmation. '
                         'All previous exposed cases are development/regression only. No changes after execution.'}
    validate(pack)
    return pack


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    output=json.dumps(build(),ensure_ascii=False,indent=2)+'\n'
    if args.check:assert DEST.read_text()==output
    else:DEST.write_text(output)
    print('88 release-independent cases: Chinese passage 24, preference 16, identity 16, glossary 16, boundary 16')


if __name__=='__main__':main()
