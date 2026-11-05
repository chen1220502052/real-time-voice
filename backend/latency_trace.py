"""Bounded per-session timing only; never controls media or dialogue state."""

from collections import OrderedDict
import math
import time


def milliseconds():
    return round(time.monotonic() * 1000, 3)


class ReplyTiming:
    def __init__(self):
        self.inputs = OrderedDict()
        self.turns = OrderedDict()
        self.replies = OrderedDict()

    @staticmethod
    def _put(values, key, value, limit):
        values[key] = value
        while len(values) > limit:
            values.popitem(last=False)

    def received(self, sequence):
        self._put(self.inputs, sequence, milliseconds(), 1200)

    def final(self, frame, result, turn_id, processing_started):
        end = result.end_time
        end_sequence = max(0, math.ceil(end / 100) - 1) if end is not None else frame.sequence
        values = {
            "asrFinalMs": milliseconds(), "finalProcessingStartedMs": processing_started,
            "processedSequence": frame.sequence, "speechEndSequence": end_sequence,
            "speechStartMediaMs": result.start_time, "speechEndMediaMs": end,
            "processedMediaMs": frame.speech_time,
            "finalInputReceivedMs": self.inputs.get(frame.sequence),
            "speechEndInputReceivedMs": self.inputs.get(end_sequence),
        }
        if getattr(result,'timing',None) is not None:
            values['localAsr']=dict(result.timing)
        self._put(self.turns, turn_id, values, 64)

    def begin(self, reply_id, turn_id):
        self._put(self.replies, reply_id, {**self.turns.get(turn_id, {}),
                                         "botRequestStartedMs": milliseconds()}, 64)

    def mark(self, reply_id, name):
        self.replies.get(reply_id, {}).setdefault(name, milliseconds())

    def snapshot(self, reply_id):
        values = self.replies.get(reply_id, {})
        upper = values.get("processedSequence")
        receipts = [[sequence, stamp] for sequence, stamp in self.inputs.items()
                    if upper is not None and sequence <= upper][-32:]
        return {"clockDomain": "server_monotonic", **values, "inputReceipts": receipts}
