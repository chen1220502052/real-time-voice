"""Keep later phrase tails together while retaining the original first chunk."""
import asyncio
import re
import time

from backend.reply_sentences import DANGLING
from backend.speech_buffer import SpeechBuffer, phrase_cut

DEPENDENT_ENDINGS = DANGLING | {
    'their','its','some','any','these','those','at','by','into','onto','during',
    'can','cannot','has','have','had','do','does','did','not','should','may','might','must',
    'take','takes','taking','fall','falls','falling','put','puts','get','gets','turn','turns',
    'pick','picks','wake','wakes','look','looks','come','comes','set','sets',
    'soft','hard','red','green','blue','small','big','hot','cold',
}


def protected_cut(text,target,*,flush=False,final=False):
    for extra in range(5):
        cut=phrase_cut(text,target+extra,flush=flush,final=final)
        if cut is None:continue
        prefix=text[:cut].rstrip()
        if re.search(r'[.!?。！？]$',prefix):return cut
        word=prefix.split()[-1].lower().strip(',;:\"') if prefix else ''
        if word in DEPENDENT_ENDINGS:continue
        # Do not synthesize a one/two-word final tail as an isolated request.
        if final and len(text[cut:].split())<=2:return len(text)
        return cut
    return len(text) if final else None


class SmoothPriorityBuffer(SpeechBuffer):
    async def next(self,remaining_ms):
        if self.first:return await super().next(remaining_ms)
        async with self.condition:
            while True:
                if not self.text and self.done:return None
                reserve=remaining_ms()
                max_wait=.18 if reserve>=600 else .10
                age=time.monotonic()-(self.since or time.monotonic())
                target=16 if reserve>=1500 else 10 if reserve>=600 else 6
                cut=protected_cut(self.text,target,flush=self.done or age>=max_wait,final=self.done)
                if cut:
                    segment,self.text=self.text[:cut],self.text[cut:]
                    self.since=time.monotonic() if self.text else None
                    if segment.strip():return segment
                    continue
                wait=max_wait-age if self.text and age<max_wait else None
                try:await asyncio.wait_for(self.condition.wait(),wait)
                except asyncio.TimeoutError:pass
