"""Reply-local text buffering, independent of Bot state and the media queue."""

import asyncio
import os
import re
import time

from .reply_sentences import DANGLING
from .speech_text import take_speech_segments


def short_start_enabled():
    return os.getenv("VOICE_SHORT_START_ENABLED", "1").lower() not in {"0", "false", "off"}


NUMBER_WORDS = set("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million point".split())


def phrase_cut(text, target, *, flush=False, final=False):
    """Preserve exact text; prefer punctuation and never split a lexical token."""
    complete, _ = take_speech_segments(text)
    punctuation = len(complete[0]) if complete else None
    tokens = list(re.finditer(r"[\u4e00-\u9fff]|[^\s\u4e00-\u9fff]+", text))
    boundaries = []
    for i, token in enumerate(tokens):
        end = token.end()
        chinese = bool(re.fullmatch(r"[\u4e00-\u9fff]", token[0]))
        stable = chinese or (end < len(text) and text[end].isspace()) or final
        if not stable:
            continue
        word = token[0].lower().strip(".,!?;:'\"，。！？；")
        next_word = tokens[i + 1][0].lower().strip(".,!?;:") if i + 1 < len(tokens) else ""
        # Avoid an audible break inside 'forty five' / decimal expressions.
        numeric = word in NUMBER_WORDS or bool(re.search(r"\d", word))
        if word in DANGLING or (numeric and (not next_word and not final or next_word in NUMBER_WORDS)):
            continue
        while end < len(text) and text[end].isspace():
            end += 1
        boundaries.append((i + 1, end))
    eligible = [(n, end) for n, end in boundaries if n <= target]
    if punctuation is not None and (not eligible or punctuation <= eligible[-1][1]):
        return punctuation
    if eligible and (len(tokens) >= target and any(n >= target for n, _ in boundaries) or flush):
        return eligible[-1][1]
    # If the nominal boundary is a preposition or numeric expression, allow a
    # few more words rather than splitting it or waiting for a whole sentence.
    later = [(n, end) for n, end in boundaries if target < n <= target + 4]
    if later:
        return later[0][1]
    if final:
        return len(text)
    return None


class SpeechBuffer:
    """Bounded text handoff: model reading continues while TTS is synthesizing."""

    def __init__(self, *, adaptive=None, max_wait_ms=None):
        self.adaptive = short_start_enabled() if adaptive is None else adaptive
        self.max_wait = max(1, float(os.getenv("TTS_SEGMENT_MAX_WAIT_MS", "100"))
                            if max_wait_ms is None else max_wait_ms) / 1000
        self.condition = asyncio.Condition()
        self.text = ""
        self.done = False
        self.first = True
        self.since = None

    async def append(self, text):
        async with self.condition:
            if len(self.text) + len(text) > 4096:
                raise ValueError("Speech text buffer exceeded its limit")
            if not self.text:
                self.since = time.monotonic()
            self.text += text
            self.condition.notify_all()

    async def finish(self):
        async with self.condition:
            self.done = True
            self.condition.notify_all()

    async def next(self, remaining_ms):
        async with self.condition:
            while True:
                if not self.text and self.done:
                    return None
                age = time.monotonic() - (self.since or time.monotonic())
                if self.adaptive:
                    reserve = remaining_ms()
                    target = 4 if self.first else (16 if reserve >= 1500 else 10 if reserve >= 600 else 6)
                    cut = phrase_cut(self.text, target, flush=self.done or age >= self.max_wait,
                                     final=self.done)
                else:
                    segments, _ = take_speech_segments(self.text, first_chunk=self.first)
                    cut = len(segments[0]) if segments else len(self.text) if self.done else None
                if cut:
                    segment, self.text = self.text[:cut], self.text[cut:]
                    self.first = False
                    self.since = time.monotonic() if self.text else None
                    if segment.strip():
                        return segment
                    continue
                wait = self.max_wait - age if self.adaptive and self.text and age < self.max_wait else None
                try:
                    await asyncio.wait_for(self.condition.wait(), wait)
                except asyncio.TimeoutError:
                    pass
