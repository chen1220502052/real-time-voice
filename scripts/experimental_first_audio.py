"""Opt-in reply buffering; keeps text and numeric expressions intact."""
import re

from backend.reply_sentences import DANGLING, sentences
from backend.speech_buffer import SpeechBuffer, NUMBER_WORDS, phrase_cut


def first_phrase_cut(text, *, flush=False, final=False):
    """Try three stable words; retain numbers with their units/nouns.

    Existing downstream policy still controls all subsequent segments. Mixed
    Chinese output uses the existing policy, retaining its language routing.
    """
    if re.search(r'[\u4e00-\u9fff]', text):
        return phrase_cut(text, 4, flush=flush, final=final)
    if re.search(r'\d|\b(?:'+'|'.join(NUMBER_WORDS)+r')\b', text, re.I):
        # Keep a complete numeric assertion, including amounts, adjective
        # modifiers, units and correction clauses. Text already emitted in
        # earlier nonnumeric chunks is never retracted.
        complete, tail = sentences(text, final=final)
        return len(text)-len(tail) if complete else len(text) if final else None
    for target in range(3, 8):
        cut = phrase_cut(text, target, flush=flush, final=final)
        if cut is None:
            continue
        prefix = text[:cut].rstrip()
        word = prefix.split()[-1].lower().strip('.,!?;:\"') if prefix else ''
        # Keep a number attached to its following unit/noun, and do not
        # leave a negation or a possessive hanging before the first pause.
        unsafe = word in NUMBER_WORDS or bool(re.search(r'\d', word))
        unsafe |= word in DANGLING | {'not', 'never', 'no', 'cannot', "can't", "don't", "doesn't",
                                      'has', 'have', 'had', 'do', 'does', 'did', 'can', 'should',
                                      'must', 'might', 'may', 'used', 'like', 'use', 'uses',
                                      'keep', 'keeps', 'take', 'takes', 'touch', 'protect',
                                      'protects', 'sounds', 'ask', 'asks', 'give', 'gives',
                                      'get', 'gets', 'need', 'needs', 'want', 'wants', 'ride',
                                      'assess', 'make', 'makes'}
        if not unsafe:
            return cut
    return len(text) if final else None


class FirstPhraseBuffer(SpeechBuffer):
    async def next(self, remaining_ms):
        if not self.first:
            return await super().next(remaining_ms)
        import asyncio
        import time
        async with self.condition:
            while True:
                if not self.text and self.done:
                    return None
                age = time.monotonic() - (self.since or time.monotonic())
                cut = first_phrase_cut(self.text, flush=self.done or age >= self.max_wait,
                                       final=self.done)
                if cut:
                    segment, self.text = self.text[:cut], self.text[cut:]
                    self.first = False
                    self.since = time.monotonic() if self.text else None
                    if segment.strip():
                        return segment
                wait = self.max_wait - age if self.text and age < self.max_wait else None
                try:
                    await asyncio.wait_for(self.condition.wait(), wait)
                except asyncio.TimeoutError:
                    pass
