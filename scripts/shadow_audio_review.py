"""Create inspectable sentence-boundary candidates from local ASR word timestamps.

This module only writes drafts. Publication requires an explicit review manifest
matching the transcript and candidate digests; a successful transcription alone
is never permission to publish audio intervals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

POLICY = 'sentence-review-v1'
NUMBER = r'(?:\d+[a-z]?|zero|one|two|too|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)'
HEADING = re.compile(r'^(?:(?:unit|lesson|chapter|track|page|part|section|activity|exercise|conversation|dialogue)\s+' + NUMBER + r'(?:\s*[,.]?\s*(?:section\s+)?[abc])?(?:\s+activity\s+' + NUMBER + r')?|section\s+[abc](?:\s*[,;]?\s*activity\s+' + NUMBER + r')?)[.!?:,\s]*$', re.I)
ACTIVITY = re.compile(r"^(?:(?:[abc]|\d+)\s*[.,:]?\s+)?(?:let['’]?s\s+(?:talk|learn|practice|practise|sing|chant|read|do|check|try|spell|write|play|repeat|listen|act)[.!?:,\s]*$|listen\s+(?:again\b|to\s+(?:the\s+)?(?:following\s+)?(?:conversations?|dialogues?|recordings?|story|stories)\b|and\s+(?:number|circle|tick|choose|repeat|match|write|read|say|answer|complete|sing|chant|do)\b|to\b.*\b(?:and|then|circle|tick|choose|hear|repeat|match|number|complete|answer)\b)|read\s+and\s+(?:tick|circle|number|answer|write|match|compare|underline)\b|(?:read|write|answer|complete|fill|match|choose|circle|tick|number)\s+(?:(?:the|these|each|a|an|your)\s+)?(?:questions?|answers?|sentences?|conversations?|dialogues?|recordings?|stories|story|text|passage|summary|summaries|table|blanks|words|pictures?|letters?)\b|(?:notice\b|pay\s+attention\s+to|work\s+in\s+pairs|role[- ]play\s+the\s+conversation|practi[cs]e\s+the\s+conversation)\b|(?:make|draw)\s+(?:a|an|your)\s+(?:mind map|poster|chart|table)\b)", re.I)
# Specific textbook task formats; conversation-like imperatives still need context review.
TASK_EXTENSION = '^(?:then\\s+)?(?:listen\\s+to\\s+(?:(?:the|these|those|three|four|two|five|six|first|second|third|following|short|humorous)\\s+)*(?:weather\\s+report|game\\s+show|conversations?|interview|words|letters|recordings?)\\b|listen[,]?\\s+(?:read|repeat|choose|circle|tick|look|write)\\b.*\\band\\s+(?:write|say|complete|circle|choose|repeat|tick|number)\\b|read\\s+the\\s+(?:interview|posts|coloured words)\\b|read\\s+and\\s+say[.!?\\s]*$|(?:in\\s+pairs,?\\s*)?read\\s+the\\s+conversation\\s+aloud\\b|put\\s+them\\s+into\\s+the\\s+correct\\s+groups\\b|then\\s+(?:practice|practise)\\s+(?:writing|reading)\\b)'
TASK_EXTENSION += r'|^(?:match|tick|circle|number)\s+(?:(?:the|these|each|different)\s+)*(?:places|parts|speakers|guests|people|students|names|subjects|inventions|plants|animals)\b'
ACTIVITY = re.compile(ACTIVITY.pattern.replace('^', r'^(?:then\s+)?', 1) + '|' + TASK_EXTENSION, re.I)
ABBREVIATIONS = {'mr.', 'mrs.', 'ms.', 'dr.', 'st.', 'u.s.', 'u.k.', 'e.g.', 'i.e.'}
MARKER_PHRASES = [
    'look listen and write', 'look listen and say', 'listen look and say', 'listen point and repeat',
    'listen repeat and chant', 'listen circle and write', 'listen write and say',
    'listen check and say', 'listen and repeat', 'listen and say', 'listen and read',
    'listen and write', 'listen and chant', 'listen and sing', 'listen and do',
    'listen and number', 'listen and match', 'listen and circle', 'listen and tick',
    'point and repeat', 'read listen and number', 'look listen and repeat',
    'read and say then listen and group', 'add one more word to each group',
    'revision exercises', 'revision exercise',
    'read listen and circle', 'read and match', 'read and write', 'ask and answer',
    'read and tick', 'read and compare', 'read and answer', 'read and number',
    'read and circle', 'read and underline', 'read tick and underline', 'read underline and listen',
    'read listen and tick', 'read and say then listen and tick', 'read and say then listen and number',
    'then read aloud', 'then read the story aloud', 'read look and match', 'words in each lesson', 'words in each unit', 'words and expressions',
    'start to read', 'get ready', 'story time', 'fun time', 'let\'s talk',
    'let\'s learn', 'let\'s practice', 'let\'s practise', 'let\'s check', 'let\'s spell',
    'let\'s sing', 'let\'s chant', 'let\'s play', 'let\'s do', 'let\'s try', 'let\'s challenge',
]


def marker_prefix(words):
    tokens = [re.sub(r'[^a-z0-9\']', '', w['word'].lower().replace('’', "'")) for w in words]
    for phrase in sorted(MARKER_PHRASES, key=len, reverse=True):
        prefix = phrase.split()
        if tokens[:len(prefix)] == prefix:
            if (prefix[0] == "let's" and len(tokens)>len(prefix)
                    and tokens[len(prefix)] in {'football','basketball','volleyball','tennis','baseball','soccer',
                                               'ball','together','later','now','outside','inside','again',
                                               'both','this','that','these','those','it','something','anything',
                                               'everything','our','my','his','her','their','through','us'}):
                continue
            if len(tokens)>len(prefix) and prefix[0] in {'listen','read'}:
                following=tokens[len(prefix)]
                gap=words[len(prefix)]['start']-words[len(prefix)-1]['end']
                if following in {'what','which','why','how','where','when','who','or','down','up','t','f','true','false'} and gap<.65:
                    continue
                if prefix[-1] in {'circle','choose','tick','match','number','write'} and gap<.65:
                    if following not in {'i','you','we','they','he','she','it',"let's",'listen','read','look','point','hi','hello','good','hey','oh','yes','no','please'} and not re.fullmatch(NUMBER,following,re.I):
                        continue
            # "Let's talk about our school" is a genuine sentence, not a header.
            if len(tokens) > len(prefix) and tokens[len(prefix)] in {'about', 'the', 'a', 'an', 'your', 'with', 'in', 'on', 'to', 'for'} and not (tokens[len(prefix)] == 'a' and (len(tokens)==len(prefix)+1 or len(tokens)>len(prefix)+1 and re.fullmatch(NUMBER,tokens[len(prefix)+1],re.I))):
                continue
            return len(prefix)
    if tokens and re.fullmatch(r'[abc]\d+', tokens[0]):
        return 1
    if len(tokens)>1 and tokens[0] in {'a','b','c'} and re.fullmatch(NUMBER,tokens[1],re.I):
        return 2
    if tokens and tokens[0] in {'a','b','c','see'} and len(tokens)>1 and tokens[1] in {'what','how','project'}:
        return 1 if tokens[1] != 'project' else 2
    if tokens and tokens[0] in {'phonics','pronunciation'}:
        return 2 if len(tokens) > 1 and re.fullmatch(NUMBER, tokens[1], re.I) else 1
    if len(tokens)>=2 and re.fullmatch(NUMBER,tokens[0],re.I) and tokens[1] in {'listen','look','read','point','repeat','say','write'}:
        return 1
    if len(tokens) >= 2 and tokens[0] in {'unit', 'lesson', 'section', 'activity', 'conversation', 'exercise', 'part','number'}:
        if re.fullmatch(NUMBER + '|[abc]', tokens[1], re.I):
            return 2
    return 0


def checksum(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text(words):
    return ''.join(w['word'] for w in words).strip()


def sentence_units(transcript):
    if transcript['segments'] and not any(s.get('words') for s in transcript['segments']):
        return [{'id':i,'start':s['start'],'end':s['end'],'text':s['text'].strip(),'words':[],
                 'boundary':'segment','alignment':'segment','probability':round(math.exp(s['avg_logprob']),3)}
                for i,s in enumerate(transcript['segments'])]
    units = []
    pending = []
    for segment in transcript['segments']:
        for word in segment.get('words', []):
            pending.append(word)
            token = word['word'].strip()
            if re.search(r'[.!?]["”’\']?$', token) and token.lower() not in ABBREVIATIONS:
                units.append({'words': pending, 'boundary': 'punctuation'})
                pending = []
    if pending:
        units.append({'words': pending, 'boundary': 'end'})
    expanded = []
    for unit in units:
        remaining = unit['words']
        while remaining:
            count = marker_prefix(remaining)
            if not count:
                expanded.append(dict(unit, words=remaining))
                break
            expanded.append({'words':remaining[:count], 'boundary':'marker', 'marker':True})
            remaining = remaining[count:]
    units = expanded
    for i, unit in enumerate(units):
        words = unit['words']
        unit.update(id=i, start=words[0]['start'], end=words[-1]['end'], text=text(words),
                    probability=round(sum(w['probability'] for w in words) / len(words), 3))
    return units


def roles(units):
    result = []
    for u in units:
        clean = re.sub(r'\s+', ' ', u['text']).strip()
        role = 'content'
        reason = 'spoken sentence'
        if u.get('marker') or HEADING.fullmatch(clean):
            role, reason = 'announcement', 'section/activity/conversation marker'
        elif ACTIVITY.match(clean):
            role, reason = 'instruction', 'task or activity instruction; inspect in context'
        elif re.search(r'\btrue\b.*\bfalse\b|\bfalse\b.*\btrue\b',clean,re.I) and u['start']<25:
            role, reason = 'instruction', 'true/false task answer format'
        result.append(dict(u, role=role, reason=reason))
    for i, u in enumerate(result):
        before=result[max(0,i-2):i]
        format_context=any(n['role']!='content' and re.search(r'listen|write|true|false|tick|circle',n['text'],re.I)
                           and u['start']-n['end']<2 for n in before)
        if (format_context and re.fullmatch(r'(?:t|f|true|false|true\s+or\s+f|t\s+true|f\s+false|or\s+(?:f|false|cross)|what\s+you\s+hear)[.!?:,\s]*',u['text'],re.I)):
            u.update(role='instruction',reason='continuation of the preceding task format')
        if re.fullmatch(r'(?:listen|repeat)[.!?:,\s]*', u['text'],re.I):
            u.update(role='instruction', reason='standalone recording instruction; inspect context')
        if re.fullmatch(r'[ABC][.!?:,\s]*', u['text']) and u['start'] < 12:
            u.update(role='announcement', reason='opening section letter')
        elif re.fullmatch(NUMBER + r'[.!?:,\s]*', u['text'], re.I):
            neighbours = result[max(0,i-2):i] + result[i+1:i+3]
            list_context = any(re.search(r'listen.*(?:write|number|circle|match|tick)|read.*number', n['text'], re.I)
                               for n in result[:i])
            follows_question = i > 0 and result[i-1]['text'].rstrip().endswith('?')
            # Repeated numbers / phone numbers are learning targets, not task
            # labels. A preceding task must not turn every later number into a
            # header, nor may numbered headers propagate that classification.
            numeric_neighbour = any(re.fullmatch(NUMBER + r'[.!?:,\s]*', n['text'], re.I)
                                    and abs(n['start']-u['start'])<8
                                    for n in result[max(0,i-1):i]+result[i+1:i+2])
            task_neighbour = any(n['role'] != 'content'
                                 and not re.fullmatch(NUMBER + r'[.!?:,\s]*',n['text'],re.I)
                                 for n in neighbours)
            if not follows_question and not numeric_neighbour and (task_neighbour or list_context):
                u.update(role='announcement', reason='exercise list number next to an instruction')
    # A title question between a leading section marker and the first task is
    # still a title, even if it looks like a conversational question in isolation.
    first_task = next((i for i, u in enumerate(result) if u['role'] == 'instruction'
                       or u.get('marker') and re.search(r'listen|read|talk|learn|practice|practise|write|spell|chant|say',u['text'],re.I)), None)
    if first_task is not None and result[first_task]['start'] < 20:
        first_heading = next((i for i, u in enumerate(result[:first_task])
                              if u['role'] == 'announcement' and
                              re.match(r'^(?:unit|lesson|section|chapter)\b|^[ABC][.,!?]?$',u['text'],re.I)), None)
        if first_heading is not None:
            for u in result[first_heading:first_task]:
                if u['role'] == 'content':
                    u.update(role='title', reason='title between opening section marker and activity instruction')
    # A numbered opening conversation begins after the narrator's setup.
    # Only listening tasks explicitly naming conversations qualify; preceding
    # word/sound practice must not be mistaken for narrator instructions.
    first_conversation = next((i for i,u in enumerate(result)
                               if u['role']=='announcement' and re.fullmatch(r'(?:conversation|dialogue)\s+(?:1|one)[.!?:,\s]*',u['text'],re.I)),None)
    if first_conversation is not None and result[first_conversation]['start']<=40:
        setup=result[:first_conversation]
        has_task=any(u['role']=='instruction' and re.search(r'^listen\b.*\bconversations?\b',u['text'],re.I) for u in setup)
        if has_task:
            for u in setup:
                if u['role']=='content':u.update(role='instruction',reason='narrator setup before the explicitly numbered first conversation')
    song=False
    for u in result:
        if u['role']!='content' and re.match(r'^listen\s+and\s+sing\b',u['text'],re.I):
            song=True
            continue
        if song and u['role']!='content' and re.match(r'^listen\b',u['text'],re.I):
            song=False
        if song and u['role']=='content':
            u.update(role='listening',reason='song section retained as full listening, outside dialogue follow-reading')
    return result


def split_long(unit, maximum=17.5):
    """Split long sentences only at a clause boundary with a measurable pause."""
    words = unit['words']
    tokens=[re.sub(r'[^a-z0-9]','',w['word'].lower()) for w in words]
    repeats=sum(a==b and bool(a) for a,b in zip(tokens,tokens[1:]))
    word_list=(len(words)>=8 and repeats/len(words)>=.3 and max(map(len,tokens),default=0)<24)
    if word_list and unit['end']-unit['start']>8.5:
        pieces=[];cursor=0
        for i in range(2,len(words)+1):
            pair_end=tokens[i-2]==tokens[i-1] and (i==len(words) or tokens[i]!=tokens[i-1])
            if pair_end and words[i-1]['end']-words[cursor]['start']>=4:
                subset=words[cursor:i]
                pieces.append(dict(unit,words=subset,start=subset[0]['start'],end=subset[-1]['end'],text=text(subset)))
                cursor=i
        if cursor<len(words):
            subset=words[cursor:]
            pieces.append(dict(unit,words=subset,start=subset[0]['start'],end=subset[-1]['end'],text=text(subset)))
        if pieces and all(p['end']-p['start']<=maximum for p in pieces):
            return pieces, []
    if unit['end'] - unit['start'] <= maximum:
        return [unit], []
    boundaries = [i for i in range(1, len(words))
                  if words[i]['start'] - words[i-1]['end'] >= .18
                  and (re.search(r'[,;:]$', words[i-1]['word'])
                       or words[i]['word'].strip().lower() in {'and', 'but', 'because', 'so', 'then', 'while', 'when', 'although'})]
    result = []
    cursor = 0
    while words[-1]['end'] - words[cursor]['start'] > maximum:
        options = [i for i in boundaries if i > cursor and 2 <= words[i-1]['end'] - words[cursor]['start'] <= maximum]
        if not options:
            return [], ['long_sentence_without_safe_clause_boundary']
        cut = options[-1]
        subset = words[cursor:cut]
        result.append(dict(unit, words=subset, start=subset[0]['start'], end=subset[-1]['end'], text=text(subset)))
        cursor = cut
    subset = words[cursor:]
    result.append(dict(unit, words=subset, start=subset[0]['start'], end=subset[-1]['end'], text=text(subset)))
    return result, []


def candidate(row, transcript, correction=None):
    original_digest=checksum(transcript)
    if correction and correction.get('wordBreaks'):
        import copy
        if correction['transcriptDigest']!=original_digest:
            raise ValueError('Sentence review is stale for source: '+row['sha256'])
        transcript=copy.deepcopy(transcript)
        words=[w for s in transcript['segments'] for w in (s.get('words') or [])]
        for edit in correction['wordBreaks']:
            matches=[w for w in words if w['word']==edit['word'] and abs(w['start']-edit['start'])<.03]
            if len(matches)!=1:
                raise ValueError('Reviewed sentence ending no longer matches its word')
            matches[0]['word']=matches[0]['word'].rstrip(' ,;:')+'.'
    units = roles(sentence_units(transcript))
    if correction:
        if correction['transcriptDigest'] != original_digest:
            raise ValueError('Semantic review is stale for source: '+row['sha256'])
        for edit in correction.get('prefixEdits',[]):
            matches=[(i,u) for i,u in enumerate(units) if u['text'].casefold().startswith(edit['prefix'].casefold())
                     and abs(u['start']-edit['start'])<.03]
            if len(matches)!=1:
                raise ValueError('Reviewed title/instruction prefix no longer matches')
            i,u=matches[0];count=len(edit['prefix'].split());head=u['words'][:count];tail=u['words'][count:]
            if text(head).casefold()!=edit['prefix'].casefold() or not tail:
                raise ValueError('Reviewed prefix does not end on the expected word')
            units[i:i+1]=[dict(u,words=head,text=text(head),end=head[-1]['end'],role=edit['role'],
                              reason=edit['reason'],reviewedRole=True),
                          dict(u,words=tail,text=text(tail),start=tail[0]['start'],role='content',
                               reason='正文在已核对的标题/指令前缀之后开始')]
        for i,u in enumerate(units):u['id']=i
        for edit in correction.get('roleEdits',[]):
            matches=[u for u in units if u['text']==edit['text'] and abs(u['start']-edit['start'])<.03]
            if len(matches)!=1 or edit['role'] not in {'content','title','instruction','announcement','listening'}:
                raise ValueError('Semantic correction no longer matches its utterance')
            matches[0].update(role=edit['role'],reason=edit['reason'],reviewedRole=True)
    name=row['uses'][0]['title']
    listen_only=row['fullLesson'] or row['kinds']==['words'] or bool(correction and correction.get('listenOnlyReason')) or (any(u['role']=='listening' for u in units) and not any(u['role']=='content' for u in units)) or bool(re.search(r'\bsing\b|\bsong\b|歌曲|唱一唱',name,re.I))
    if listen_only:
        return {'version':1,'policy':POLICY,'sha256':row['sha256'],'transcriptDigest':original_digest,
                'uses':row['uses'],'duration':transcript['duration'],'listenOnly':True,
                'units':units,'clips':[],'issues':[] if units else ['no_recognized_speech'],'status':'draft',
                'correctionDigest':checksum(correction) if correction else None}
    if any(u.get('alignment')=='segment' for u in units):
        raise ValueError('Word alignment is required for short follow-reading targets')
    issues = []
    if not units:
        issues.append('no_recognized_speech')
    # MP3 frame metadata may include about half a second of encoder padding.
    # Candidate bounds use decoded duration, not that approximate container value.
    if abs(transcript['duration'] - (row['duration'] or transcript['duration'])) > 1.0:
        issues.append('duration_mismatch')
    pieces = []
    for u in units:
        if u['role'] != 'content':
            if (not u.get('reviewedRole') and u['role'] in {'instruction','announcement'}
                    and (len(u['words'])>=18 or u['end']-u['start']>10)):
                issues.append('instruction_may_contain_unpunctuated_body')
            prior=units[:u['id']]
            later_invitation=(u.get('marker') and re.search(r"^let['’]?s",u['text'],re.I)
                              and any(p['role']=='instruction' or p.get('marker') and re.search(r"listen|let['’]?s|read",p['text'],re.I) for p in prior))
            inside_dialogue=(u['start']>15 and any(p['role']=='content' for p in prior)
                             and (u['role']=='instruction' or u.get('marker') and re.search(r"let['’]?s|get ready|story time",u['text'],re.I)))
            if not u.get('reviewedRole') and (later_invitation or inside_dialogue):
                issues.append('instruction_inside_content_needs_context_review')
            pieces.append(None)
            continue
        if not u.get('reviewedRole'):
            lowered=' '.join(re.sub(r'[^a-z0-9]','',w['word'].lower()) for w in u['words'])
            for phrase in MARKER_PHRASES:
                if phrase.startswith(('listen','read','look listen')) and lowered.find(phrase)>1:
                    issues.append('possible_embedded_instruction')
                    break
        parts, errors = split_long(u)
        issues.extend(errors)
        for part in parts:
            if part['probability'] < .55:
                issues.append('low_recognition_confidence')
            if part['end'] <= part['start']:
                issues.append('invalid_word_timestamp')
            pieces.append(part)
    # Pair very short replies with their neighbouring utterance, never across
    # an excluded announcement or task, and never beyond a child's short turn.
    merged = []
    barrier = False
    for piece in pieces:
        if piece is None:
            barrier = True
            continue
        if (merged and not barrier and piece['start'] - merged[-1]['end'] < 1.0
                and piece['end'] - merged[-1]['start'] <= 11.5
                and (merged[-1]['end'] - merged[-1]['start'] < 3.5 or piece['end'] - piece['start'] < 1.8)):
            merged[-1]['end'] = piece['end']
            merged[-1]['unitIds'].append(piece['id'])
            merged[-1]['text'] += ' ' + piece['text']
        else:
            merged.append({'start':piece['start'],'end':piece['end'],'unitIds':[piece['id']],'text':piece['text']})
        barrier = False
    # Word alignment can be early or late by a small margin. Add context padding
    # within the nearest recognised neighbour; no title/instruction can leak in.
    clips = []
    for item in merged:
        before = max((u['end'] for u in units if u['end'] <= item['start'] and u['id'] not in item['unitIds']), default=0)
        after = min((u['start'] for u in units if u['start'] >= item['end'] and u['id'] not in item['unitIds']), default=transcript['duration'])
        start = max(0, item['start'] - .18, (before + item['start']) / 2)
        end = min(transcript['duration'], item['end'] + .20, (item['end'] + after) / 2)
        if end - start > 18.05:
            issues.append('padded_clip_too_long')
        clips.append({'start':round(start,2),'end':round(end,2), 'spokenStart':item['start'],
                      'spokenEnd':item['end'], 'unitIds':item['unitIds'], 'text':item['text']})
    for left, right in zip(clips, clips[1:]):
        if left['end'] > right['start']:
            midpoint = round((left['end'] + right['start']) / 2, 2)
            left['end'] = right['start'] = midpoint
    # Songs, whole lessons and vocabulary-list tracks remain full listening
    # resources. The audit still records their recognised speech and removals.
    name = row['uses'][0]['title']
    listen_only = row['fullLesson'] or row['kinds'] == ['words'] or bool(correction and correction.get('listenOnlyReason')) or (any(u['role']=='listening' for u in units) and not any(u['role']=='content' for u in units)) or bool(re.search(r'\bsing\b|\bsong\b|歌曲|唱一唱', name, re.I))
    if listen_only:
        clips = []
    return {'version':1, 'policy':POLICY, 'sha256':row['sha256'], 'transcriptDigest':original_digest,
            'uses':row['uses'], 'duration':transcript['duration'], 'listenOnly':listen_only,
            'units':units, 'clips':clips, 'issues':sorted(set(issues)), 'status':'draft',
            'correctionDigest':checksum(correction) if correction else None}


def build(inventory, transcripts, output, corrections=None):
    output.mkdir(parents=True, exist_ok=True)
    rows = json.loads(inventory.read_text())['recordings']
    counts = {'ready':0,'pending':0,'missing':0,'clips':0}
    for row in rows:
        path = transcripts / (row['sha256'] + '.json')
        if not path.is_file():
            counts['missing'] += 1
            continue
        data = candidate(row, json.loads(path.read_text()),(corrections or {}).get(row['sha256']))
        data['candidateDigest'] = checksum(data)
        (output / path.name).write_text(json.dumps(data,ensure_ascii=False,indent=2))
        counts['pending' if data['issues'] else 'ready'] += 1
        counts['clips'] += len(data['clips'])
    print(json.dumps(counts), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path,required=True)
    parser.add_argument('--transcripts',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--corrections',type=Path)
    args = parser.parse_args()
    edits=json.loads(args.corrections.read_text())['recordings'] if args.corrections else None
    build(args.inventory,args.transcripts,args.output,edits)
