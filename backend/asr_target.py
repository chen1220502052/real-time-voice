"""Conservative acoustic verification of one English word in a Mandarin query.

No vocabulary, lesson, conversation history, or reference answer is consulted.
The supplied decoders must return their real, thresholded ASR confidences.
"""
import math
import re


def target_slot(text, words=None):
    """Locate an empty/ASCII-only word slot and its approximate acoustic bounds."""
    chars = [(c, i) for i, c in enumerate(text) if c.isalnum()]
    compact = ''.join(c for c, _ in chars)
    prefix = re.match(
        r'(?:(?:请|麻烦)?(?:用中文)?(?:帮我)?'
        r'(?:解释|翻译|告诉我|说说|说|我想知道|我想问)'
        r'(?:一下)?(?:这个)?(?:(?:英语|英文)?(?:单词|词))?'
        r'|(?:英语|英文)单词)', compact)
    start = prefix.end() if prefix else 0
    suffix = re.search(
        r'(?:(?:这个|这一个)(?:单词|词))?(?:的)?'
        r'(?:中文(?:意思|含义|释义|怎么说|是什么意思)|意思(?:是什么)?|是什么意思)',
        compact[start:])
    if suffix is None:
        return None
    end = start + suffix.start()
    target = compact[start:end]
    if not re.fullmatch(r'[A-Za-z]{0,24}', target) or (not prefix and not target):
        return None
    if not prefix and start == 0 and not re.search(r'[\u4e00-\u9fff]', compact[end:]):
        return None
    lo = chars[start][1] if start < len(chars) else len(text)
    hi = chars[end - 1][1] + 1 if target else lo
    if len(re.findall(r'[A-Za-z]+', text[lo:hi])) > 1:
        return None
    if words is None:
        return dict(target=target.lower(), start=lo, end=hi)
    aligned = []
    for word in words:
        aligned.extend([word] * sum(c.isalnum() for c in word['word']))
    if len(aligned) != len(chars) or end >= len(aligned):
        return None  # Never infer a crop from a mismatched character alignment.
    before = aligned[start] if target else aligned[max(0, start - 1)]
    after = aligned[end - 1] if target else aligned[end]
    left, right = float(before['start']), float(after['end'])
    if not all(math.isfinite(x) for x in (left, right)) or not 0 <= left < right:
        return None
    return dict(target=target.lower(), start=lo, end=hi, left=left, right=right)


def parakeet_words(tokens):
    """Retain word-level evidence even when Mandarin lowers whole-line scores."""
    groups = []
    for token in tokens:
        if not groups or token.text.startswith(' '):
            groups.append([])
        groups[-1].append(token)
    result = []
    for group in groups:
        text = ''.join(t.text for t in group).strip().strip('.,!?')
        if not re.fullmatch(r'[A-Za-z]{2,24}', text):
            continue
        score = math.exp(sum(math.log(max(1e-10, t.confidence)) for t in group) / len(group))
        if score >= .8:
            result.append(dict(text=text.lower(), start=group[0].start,
                               end=group[-1].end, confidence=score))
    return result


def needs_verification(text, hypotheses):
    slot = target_slot(text)
    if slot is None:
        return False
    # Two recognizers already agree. Re-decoding a shorter crop can damage a
    # correct answer; longer words retain the original serving behaviour.
    return not (slot['target'] and (len(slot['target']) > 4 or
                any(word['text'] == slot['target'] for word in hypotheses)))


def verify_target(accepted, words, tokens, decode):
    """At most two local English decodes; disagreement becomes an explicit gap.

decode(start_seconds, end_seconds) receives only audio coordinates, never the
candidate spelling. An empty slot requires agreement with the other recognizer;
an existing word can also be supported by an independent acoustic crop.
    """
    text = accepted['text']
    slot = target_slot(text, words)
    if slot is None:
        return accepted
    hypotheses = [w for w in parakeet_words(tokens)
                  if slot['left'] - .08 <= (w['start'] + w['end']) / 2 <= slot['right'] + .08]
    # The existing word's aligned crop is always checked first. A spelling is
    # not used as a prompt; all alternatives are derived from the same PCM.
    crops = []
    if slot['target']:
        crops.append((slot['left'] - .12, slot['right'] + .08, None))
    for word in sorted(hypotheses, key=lambda w: -w['confidence']):
        if len(crops) >= 2:
            break
        crops.append((word['start'] - .08, word['end'] + .32, word['text']))
    evidence = []
    supported = set()
    for left, right, other in crops:
        left = max(0, left)
        if not .12 <= right - left <= 2:
            continue
        result = decode(left, right)
        spelling = result['text'].strip().strip('.,!?').lower()
        evidence.append(dict(start=left, end=right, text=result['text'],
                             confidence=result.get('confidence', 0), parakeet=other))
        # A crop may retain a neighbouring "a" or "or" from Mandarin.
        # Such output may confirm an existing spelling, but cannot supply a
        # new spelling for an empty slot.
        if slot['target'] and re.search(r'\b' + re.escape(slot['target']) + r'\b', spelling):
            supported.add(slot['target'])
        if not re.fullmatch(r'[a-z]{2,24}', spelling):
            continue
        if spelling == slot['target'] or spelling == other:
            supported.add(spelling)
    detail = dict(originalTarget=slot['target'], evidence=evidence)
    if len(supported) == 1:
        spelling = supported.pop()
        detail['status'] = 'verified' if spelling == slot['target'] else 'recovered'
        return {**accepted, 'text': text[:slot['start']] + spelling + text[slot['end']:],
                'targetVerification': detail}
    if slot['target']:
        detail['status'] = 'uncertain'
        return {**accepted, 'text': text[:slot['start']] + '[英语词未听清]' + text[slot['end']:],
                'targetVerification': detail}
    detail['status'] = 'unresolved'
    return {**accepted, 'text': text[:slot['start']] + '[英语词未听清]' + text[slot['end']:],
            'targetVerification': detail}
