"""Text-backed local barge-in and a first-audio guard for resumed speech.

VAD establishes a candidate, never interruption intent by itself. Partial
recognition can stop playback without ending the user's utterance.
"""
import asyncio
import os
import re
import math
import struct
from collections import deque


def english_words(text):
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())


def unfinished_english(text):
    words = english_words(text)
    if not words:
        return False
    trailing = {'a', 'an', 'the', 'and', 'or', 'but', 'because', 'if', 'when',
        'while', 'to', 'of', 'with', 'for', 'from', 'at', 'in', 'on', 'my',
        'your', 'his', 'her', 'our', 'their', 'is', 'am', 'are', 'was', 'were',
        'be', 'been', 'have', 'has', 'had', 'can', 'could', 'would', 'should',
        'will', 'do', 'does', 'did', 'want', 'need', 'not'}
    # ASR punctuation is inferred: "I would like." still needs its complement.
    tails = ('would like', "i'd like", 'i think', 'i guess', 'let me think',
             'my favorite', 'my favourite', 'going to', 'want to', 'have to')
    normalized = ' '.join(words)
    question = (len(words) >= 4 and words[0] in
        {'what', "what's", 'how', 'who', 'where', 'why', 'when', 'which'}
        and text.rstrip().endswith('?') and words[-1] in
        {'for', 'of', 'with', 'from', 'at', 'in', 'on', 'do', 'have', 'be'})
    return ((words[-1] in trailing and not question)
            or any(normalized == tail or normalized.endswith(' ' + tail) for tail in tails))


class EchoEvidence:
    """Conservative waveform evidence, needed in addition to matching words.

    Keep only 10 seconds of the current reply and three voiced input windows.
    A different person repeating the same text must not be rejected as echo.
    This is a fallback for strong leakage, not acoustic echo cancellation.
    """
    def __init__(self):
        self.reference = deque(maxlen=5000)
        self.inputs = deque(maxlen=3)

    @staticmethod
    def fingerprint(pcm):
        samples = struct.unpack('<' + str(len(pcm)//2) + 'h', pcm)
        return [sum(samples[i:i+32])/32 for i in range(0, len(samples)-31, 32)]

    def output(self, pcm):
        self.reference.extend(self.fingerprint(pcm))

    def input(self, pcm):
        values = self.fingerprint(pcm)
        if values and sum(v*v for v in values)/len(values) > 100:
            self.inputs.append(values)

    def matches(self):
        if len(self.inputs) < 2:
            return False
        reference = list(self.reference)
        matched = 0
        for values in self.inputs:
            mean = sum(values)/len(values)
            query = [v-mean for v in values]
            energy = sum(v*v for v in query)
            if energy < 10000:
                continue
            width = len(query)
            for offset in range(len(reference)-width+1):
                window = reference[offset:offset+width]
                average = sum(window)/width
                norm = sum(v*v for v in window)-width*average*average
                if norm <= 0:
                    continue
                dot = sum(a*b for a,b in zip(query,window))
                if dot > .94*math.sqrt(energy*norm):
                    matched += 1
                    break
        return matched >= 2


class LocalTurnTaking:
    def __init__(self, dialogue):
        self.dialogue = dialogue
        self.minimum_confidence = float(os.getenv('LOCAL_BARGE_IN_MIN_CONFIDENCE', '.94'))
        self.minimum_voiced_ms = int(os.getenv('LOCAL_BARGE_IN_MIN_VOICED_MS', '300'))
        self._first_audio_allowed = asyncio.Event()
        self._first_audio_allowed.set()
        self._overlap_reply = None
        self._robot_words = []
        self.echo = EchoEvidence()

    def new_reply(self):
        self.echo = EchoEvidence()

    def release(self):
        self._first_audio_allowed.set()

    async def wait_before_first_audio(self):
        await self._first_audio_allowed.wait()

    def _hold_first_audio(self, active):
        d = self.dialogue
        if active and d.session.robot.status == 1 and self._first_audio_allowed.is_set():
            self._first_audio_allowed.clear()
            d._emit_timeline('reply_audio_held', 'strategy', reply_id=active,
                turn_id=d._active_user_turn_id, attributes={'reason': 'user_resumed'})

    def _rejection(self, text, *, final=False):
        words = english_words(text)
        if not words:
            return 'no_words'
        if all(word in {'um', 'uh', 'hmm', 'hm', 'mm', 'mhm', 'huh'} for word in words):
            return 'hesitation'
        # A single overlapping word cannot establish echo. Explicit stop
        # commands always retain priority, even if the robot said them too.
        if len(words) >= (3 if final else 2) and not set(words) & {'stop', 'wait', 'enough'}:
            size = len(words)
            if any(self._robot_words[i:i + size] == words
                   for i in range(len(self._robot_words) - size + 1)):
                if self.echo.matches():
                    return 'possible_echo'
                # Ambiguous matching partials wait for more words/final, but
                # a human repeating a teaching example still gets a response.
                if not final:
                    return 'matching_partial'
        return None

    async def update(self, result):
        """Return False only when an overlapping final must not start a reply."""
        d = self.dialogue
        active = d._active_reply_id if d.session.robot.status in (1, 2) else None
        if result.status == 1:
            self._overlap_reply = active
            self._robot_words = english_words(d.session.robot.text) if active else []
            # ASR/control keep running; only the first media packet waits.
            self._hold_first_audio(active)
            return True
        if result.status == 0:
            # One VAD candidate may hold an unsent first packet, but can never
            # cancel a reply. A rejected one-frame click releases it next frame.
            if (result.activity or {}).get('voiced'):
                self._hold_first_audio(active)
            else:
                self.release()
            return True
        activity = result.activity or {}
        text = result.text if result.status == 3 else activity.get('text', '')
        reason = self._rejection(text, final=result.status == 3) if self._overlap_reply else None
        if result.status == 3:
            self.release()
            if reason:
                d._emit_timeline('overlap_ignored', 'strategy', reply_id=self._overlap_reply,
                    turn_id=d._active_user_turn_id, attributes={'reason': reason, 'text': text})
                return False
        if not active or not text or reason:
            return True
        evidence = 'asr_final'
        if result.status == 2:
            words = english_words(text)
            command = len(words) == 1 and words[0] in {'stop', 'wait', 'enough'}
            if (len(words) < 2 and not command
                    or activity.get('confidence', 0) < self.minimum_confidence
                    or activity.get('voicedMs', 0) < self.minimum_voiced_ms
                    or activity.get('quietMs', 9999) > 200
                    or result.speech_time - (activity.get('textEndMediaMs') or -9999) > 500):
                return True
            evidence = 'asr_partial'
        d._emit_timeline('barge_in_confirmed', 'strategy',
            turn_id=d._active_user_turn_id, reply_id=active,
            attributes={'evidence': evidence, 'sawText': True,
                'heldMs': result.speech_time - (result.start_time or 0),
                'voicedMs': activity.get('voicedMs'), 'quietMs': activity.get('quietMs'),
                'confidence': activity.get('confidence')})
        await d._interrupt()
        return True
