"""Deterministic dialogue state, with model escalation for ambiguous turns.

State is projected from actual delivered history, never from unplayed drafts.
"""
from dataclasses import asdict, dataclass
import re
from .chat_library import question_key

TOPICS = {
    'animals': r'\b(?:cat|cats|dog|dogs|pet|pets|animal|animals)\b|动物|宠物|猫|狗',
    'food': r'\b(?:food|eat|apple|apples|rice|bread|milk|fruit)\b|食物|吃|水果',
    'games': r'\b(?:game|games|play|football|basketball|sport|sports)\b|游戏|运动|篮球|足球',
    'weather': r'\b(?:weather|rain|sunny|snow|wind)\b|天气|下雨',
    'space': r'\b(?:star|stars|space|moon|sun|planet|planets)\b|星星|太空|月亮',
}
PERSONAL = re.compile(r"\b(?:i|my|me|mine|we|our|us|your|you|his|her|their|name|named|live|address|school|phone|email|password)\b|我|你|他|她|名字|住址|学校|电话|邮箱", re.I)
FOLLOWUP = re.compile(r"^(?:why|how|what about\b.*|and\b.*|it\b.*|they\b.*|yes|no|really|tell me more|go on|continue|为什么|然后呢|它呢|继续|是|不是)[ ?.!。？！]*$", re.I)
SWITCH = re.compile(r'\b(?:change (?:the )?(?:topic|subject)|(?:talk|chat) about|something else)\b|换个?话题|聊聊|聊点别的', re.I)
PRACTICE = re.compile(r'\b(?:practi[cs]e|practicing|practising|repeat after|say it again|repeat that)\b|练习|跟读|再说一遍|重复一遍', re.I)
# Only these unambiguous, self-contained knowledge prompts can seed shared replies.
# Everything outside this small set is still stored, privately, without blocking chat.
PUBLIC_QUERIES = {
    'why do stars shine', 'why is the sky blue', 'why do cats sleep',
    'what do cats eat', 'what do dogs eat', 'what is rain', 'what is snow',
    'what is the sun', 'what is the moon', 'what are stars',
    'tell me about cats', 'tell me about dogs', 'tell me about rain',
    'tell me about the sun', 'tell me about the moon', 'tell me about stars',
}

@dataclass(frozen=True)
class DialogueState:
    topic: str
    previous_topic: str
    repeat_count: int
    action: str
    last_question: str
    recent_replies: tuple
    practice: bool
    facts: tuple = ()

    def to_dict(self):
        return asdict(self)


def topic_of(text):
    return next((topic for topic, pattern in TOPICS.items() if re.search(pattern, text, re.I)), '')


def project_state(messages):
    users = [m.content for m in messages if m.role == 'user']
    latest = users[-1] if users else ''
    previous = next((topic_of(m.content) for m in reversed(messages[:-1]) if topic_of(m.content)), '')
    explicit = topic_of(latest)
    repeats = 0
    for text in reversed(users):
        if question_key(text) != question_key(latest):
            break
        repeats += 1
    assistants = tuple(m.content for m in messages if m.role == 'assistant')
    # Practice mode lasts until a clear change of topic, not an arbitrary count.
    practice, practice_topic = False, ''
    for text in users:
        topic = topic_of(text)
        if re.search(r'\b(?:stop practicing|stop practising|stop practice|enough practice)\b|停止练习|不练了|不用练了', text, re.I):
            practice = False
        elif PRACTICE.search(text):
            practice, practice_topic = True, topic
        elif SWITCH.search(text) or (practice and topic and practice_topic and topic != practice_topic):
            practice = False
    if practice:
        action = 'practice'
    elif SWITCH.search(latest):
        action = 'switch_topic'
    elif repeats >= 3:
        action = 'offer_topics'
    elif repeats == 2:
        action = 'simplify_question'
    elif explicit and previous and explicit != previous:
        action = 'switch_topic'
    elif FOLLOWUP.fullmatch(latest.strip()):
        action = 'follow_up'
    else:
        action = 'answer'
    facts = tuple(t[:240] for t in users if re.fullmatch(
        r"(?:my pet (?:is named|is called)|i (?:like|do not like|don't like)|my favou?rite [a-z]+ is) [a-z][a-z '\-]{0,100}[.!]?", t.strip(), re.I))[-8:]
    return DialogueState(explicit or previous, previous, repeats, action,
                         assistants[-1] if assistants and '?' in assistants[-1] else '',
                         assistants[-6:], practice, facts)


def policy_reply(state, scope):
    """Use a small, vocabulary-checked selection only for predictable repeats."""
    from .writing_vocabulary import known, tokens
    if state.action == 'offer_topics':
        choices = [('food', 'Do you like apples?'), ('games', 'Do you like games?'),
                   ('animals', 'Do you like dogs?'), ('weather', 'Do you like rain?')]
    elif state.action == 'simplify_question':
        choices = {
            'animals': [('animals', 'Cats or dogs?'), ('animals', 'Do you like dogs?')],
            'food': [('food', 'Apples or rice?'), ('food', 'Do you like apples?')],
            'games': [('games', 'Do you like games?')],
        }.get(state.topic, [])
    else:
        return None
    for topic, reply in choices:
        if state.action == 'offer_topics' and topic == state.topic:
            continue
        if (question_key(reply) not in {question_key(t) for t in state.recent_replies}
                and all(known(w, scope.words) for w in tokens(reply))):
            return reply
    return None  # One model call handles sparse vocabulary or exhausted alternatives.


def sharing_kind(question, answer, messages):
    # Conservative boundary: private input anywhere in the visible context makes
    # even a generic-looking answer private. The model never decides this flag.
    if PERSONAL.search(question + ' ' + answer) or any(
            PERSONAL.search(m.content) for m in messages if m.role == 'user'):
        return 'personal'
    if (question_key(question) in PUBLIC_QUERIES
            and all(question_key(m.content) in PUBLIC_QUERIES for m in messages if m.role == 'user')
            and not re.search(r'[0-9@:/]|https?\b', answer)
            and not re.search(r'[\u4e00-\u9fff]', answer)
            and all(w in {'The','A','An','It','They','This','That','These','Those','Stars','Cats','Dogs','Rain','Snow','Water','Clouds','Sunlight','Light','Heat','Because','Some','Most','In','At','When','For','Yes','No'}
                    for w in re.findall(r'\b[A-Z][A-Za-z]*\b', answer))):
        return 'general'
    return 'contextual'


def model_guidance(state):
    instructions = {
        'answer': 'Answer the latest input directly.',
        'follow_up': 'Resolve short references and follow-up questions using the delivered history; do not use an unrelated stock answer.',
        'switch_topic': 'Follow the latest topic choice immediately. Do not drag the conversation back to the previous topic.',
        'simplify_question': 'The student repeated the same input. Acknowledge it gently and offer one easier two-choice question. Do not repeat your previous question.',
        'offer_topics': 'The conversation is stuck on repeated input. Offer a different simple topic or two choices. Do not scold or mention repetition counts.',
        'practice': 'The student wants repetition practice. Help with the requested phrase, gently; do not force a topic change or invent personal facts.',
    }
    return instructions[state.action]
