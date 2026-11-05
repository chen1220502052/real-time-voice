"""Sentence-safe streaming: never turn a word/character quota into half a reply."""
import re

FALLBACK = "Sorry, I lost that thought. Could you say that again?"
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc", "e.g", "i.e"}
DANGLING = {"so", "and", "or", "but", "because", "although", "if", "when", "while", "that", "which",
            "to", "with", "from", "for", "of", "a", "an", "the", "my", "your", "his", "her", "our",
            "i", "you", "he", "she", "we", "they", "is", "are", "was", "were", "will", "would", "could"}


def plain_text(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*#>]+|\d+[.)])\s*", "", text)
    text = re.sub(r"[*_`{}\[\]]", "", text)
    return "".join(c for c in text if ord(c) < 0x1F000 and (c.isprintable() or c.isspace()))


def sentences(buffer, *, final=False):
    """Return stable complete sentences and uncommitted tail; retain decimals."""
    result, start = [], 0
    for match in re.finditer(r"[.!?。！？]+", buffer):
        end = match.end()
        if end == len(buffer) and not final:
            continue
        if match[0] == ".":
            before = buffer[:match.start()]
            after = buffer[end:end + 1]
            if before[-1:].isdigit() and after.isdigit():
                continue
            word = re.search(r"([A-Za-z.]+)$", before)
            if word and (word[1].lower() in ABBREVIATIONS or len(word[1]) == 1):
                continue
        if (not any(c in match[0] for c in '。！？') and end < len(buffer)
                and not buffer[end].isspace() and buffer[end] not in '\"”’）)'):
            continue
        sentence = " ".join(buffer[start:end].split()).strip('\"“” ')
        if sentence:
            result.append(sentence)
        start = end
    return result, buffer[start:]


def complete_tail(text):
    """Accept ordinary punctuation-free final sentences, reject dangling tails."""
    text = " ".join(text.split()).strip('\"“” ')
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())
    affirmation = bool(re.search(r"\b(?:think|hope|guess|suppose|say) so$", text, re.I))
    if not text or (words and words[-1] in DANGLING and not affirmation):
        return ""
    if words and len(words) < 3 and not re.search(r'[\u4e00-\u9fff]', text) and text.lower() not in {"hi", "hello", "yes", "no", "thanks", "great", "sure", "okay", "thank you", "of course", "sounds fun", "that's cool"}:
        return ""
    if text[-1:] in ",，;；:：-—":
        return ""
    if text[-1:] in ".!?。！？":
        return text
    return text + ("。" if any('\u4e00' <= c <= '\u9fff' for c in text) else ".")


def fallback_for(user):
    if re.search(r"how old are you|your age|你.*几岁|你.*多大", user, re.I):
        return "I'm an AI cat, so I don't have a human age."
    return FALLBACK
