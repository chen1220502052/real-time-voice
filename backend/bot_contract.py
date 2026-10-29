"""TextBot v2: interaction facts in, streamed content out.

The media engine never constructs semantic assistant history or stores bot state.
v1 BotRequest remains an internal model-adapter contract, not this service API.
"""

from dataclasses import dataclass, field
import json
import math
from types import MappingProxyType
from typing import AsyncIterator, Mapping, Protocol, runtime_checkable

from .text_bot import BotEvent, BotRequest


@runtime_checkable
class ConversationBot(Protocol):
    protocol_version: int

    def stream(self, request: "ConversationRequest") -> AsyncIterator[BotEvent]: ...
    async def observe(self, session_id: str, events: tuple) -> None: ...
    async def aclose(self) -> None: ...


def identifier(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not value.isascii() or any(not (c.isalnum() or c in "_-") for c in value)):
        raise ValueError("Invalid identifier")
    return value


def interaction(data):
    if not isinstance(data, dict) or set(data) - {
        "eventId", "type", "replyId", "sequence", "playedMs", "audioEndMs", "text",
    }:
        raise ValueError("Invalid interaction fields")
    value = dict(data)
    identifier(value.get("eventId"))
    identifier(value.get("replyId"))
    if value.get("type") not in {
        "playback_progress", "playback_completed", "playback_stopped",
        "generation_cancelled", "speech_segment",
        "text_displayed",
    }:
        raise ValueError("Invalid interaction type")
    if type(value.get("sequence")) is not int or not 0 <= value["sequence"] <= 2**53 - 1:
        raise ValueError("Invalid interaction sequence")
    for name in ("playedMs", "audioEndMs"):
        if name in value and (type(value[name]) not in (int, float)
                              or not math.isfinite(value[name]) or not 0 <= value[name] <= 3600000):
            raise ValueError("Invalid playback position")
    if value["type"] in {"playback_progress", "playback_stopped"} and "playedMs" not in value:
        raise ValueError("Playback position required")
    if value["type"] == "speech_segment":
        if "audioEndMs" not in value or not isinstance(value.get("text"), str) or not 1 <= len(value["text"]) <= 4096:
            raise ValueError("Invalid speech segment")
    elif "text" in value or "audioEndMs" in value:
        raise ValueError("Text belongs only to speech segments")
    return MappingProxyType(value)


@dataclass(frozen=True)
class ConversationRequest:
    request_id: str
    session_id: str
    turn_id: str
    text: str = ""
    event: str = "user_message"
    channel: str = "voice"
    options: Mapping = field(default_factory=dict)
    reply_id: str = ""
    generation_epoch: int = 0
    interactions: tuple = ()
    after_event_seq: int = 0
    version: int = 2

    def __post_init__(self):
        # Command aliases canonicalize before fingerprinting and persistence.
        object.__setattr__(self, "event", {"connect": "session_start", "pick_up": "session_start"}.get(self.event, self.event))
        for value in (self.request_id, self.session_id, self.turn_id):
            identifier(value)
        if self.reply_id:
            identifier(self.reply_id)
        if self.version != 2 or type(self.version) is not int:
            raise ValueError("Unsupported service protocol")
        if self.event not in {"session_start", "user_message", "time_out", "hang_up"} or self.channel not in {"voice", "text"}:
            raise ValueError("Invalid event or channel")
        if (not isinstance(self.text, str) or len(self.text) > 4000
                or (self.event == "user_message" and not self.text.strip())
                or (self.event != "user_message" and self.text)):
            raise ValueError("Invalid input text")
        if type(self.generation_epoch) is not int or self.generation_epoch < 0:
            raise ValueError("Invalid generation epoch")
        if type(self.after_event_seq) is not int or self.after_event_seq < 0:
            raise ValueError("Invalid resume cursor")
        if not isinstance(self.options, Mapping):
            raise ValueError("Invalid options")
        # Keep the immutable inner adapter's option/identifier validation.
        BotRequest(self.request_id, self.session_id, self.turn_id, event="session_start", options=self.options)
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))
        if len(self.interactions) > 256:
            raise ValueError("Too many interactions")
        object.__setattr__(self, "interactions", tuple(interaction(dict(e)) for e in self.interactions))

    def to_dict(self):
        return {
            "version": 2, "requestId": self.request_id, "sessionId": self.session_id,
            "turnId": self.turn_id, "replyId": self.reply_id or self.request_id,
            "generationEpoch": self.generation_epoch, "event": self.event,
            "channel": self.channel, "text": self.text, "options": dict(self.options),
            "interactions": [dict(e) for e in self.interactions],
        }

    def fingerprint(self):
        # The replay cursor is transport state, not a new generation request.
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def parse_request(data):
    if not isinstance(data, dict) or set(data) - {
        "version", "requestId", "sessionId", "turnId", "replyId", "generationEpoch",
        "event", "channel", "text", "options", "interactions", "afterEventSeq",
    }:
        raise ValueError("Invalid request fields")
    return ConversationRequest(
        request_id=data.get("requestId"), session_id=data.get("sessionId"),
        turn_id=data.get("turnId"), version=data.get("version", 2),
        reply_id=data.get("replyId", ""), generation_epoch=data.get("generationEpoch", 0),
        event=data.get("event", "user_message"), text=data.get("text", ""),
        channel=data.get("channel", "voice"), options=data.get("options", {}),
        interactions=data.get("interactions", ()), after_event_seq=data.get("afterEventSeq", 0),
    )
