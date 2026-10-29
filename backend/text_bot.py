"""Versioned, provider-neutral text bot contract shared by text and voice.

v1 is the internal stateless model adapter. The public stateful service uses
bot_contract.ConversationRequest v2 and owns semantic history itself.
"""

import asyncio
from dataclasses import dataclass, field
import time
from types import MappingProxyType
from typing import AsyncIterator, Literal, Mapping, Protocol, runtime_checkable


class TextBotError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class BotMessage:
    role: Literal["user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise ValueError("Only user and assistant messages are accepted")
        if not isinstance(self.content, str) or not self.content.strip() or len(self.content) > 4000:
            raise ValueError("Message content must contain 1 to 4000 characters")

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class BotRequest:
    request_id: str
    session_id: str
    turn_id: str
    messages: tuple[BotMessage, ...] = ()
    event: Literal["session_start", "user_message", "time_out", "hang_up"] = "user_message"
    options: Mapping = field(default_factory=dict)
    reply_id: str = ""
    generation_epoch: int = 0
    version: int = 1
    delivery_context: tuple = ()
    idle_count: int = 0
    # Internal projection of the existing v2 channel; not a new wire option.
    channel: str = "text"

    def __post_init__(self) -> None:
        if self.channel not in {"text", "voice"}:
            raise ValueError("Unsupported channel")
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported text bot protocol version")
        for value in (self.request_id, self.session_id, self.turn_id):
            if not isinstance(value, str) or not value or len(value) > 128 or not value.isascii() or not all(c.isalnum() or c in "_-" for c in value):
                raise ValueError("Identifiers must be 1 to 128 ASCII letters, digits, underscores or hyphens")
        if type(self.generation_epoch) is not int or self.generation_epoch < 0:
            raise ValueError("generationEpoch must be a nonnegative integer")
        if not isinstance(self.reply_id, str) or len(self.reply_id) > 128 or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in self.reply_id):
            raise ValueError("Invalid replyId")
        if self.event not in ("session_start", "user_message", "time_out", "hang_up"):
            raise ValueError("Unsupported text bot event")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))
        if len(self.delivery_context) > 2:
            raise ValueError("Too much delivery context")
        context = []
        for item in self.delivery_context:
            if not isinstance(item, Mapping) or set(item) != {"replyId", "generatedText", "deliveredText", "status"}:
                raise ValueError("Invalid delivery context")
            if any(not isinstance(v, str) or len(v) > 4096 for v in item.values()):
                raise ValueError("Invalid delivery context value")
            context.append(MappingProxyType(dict(item)))
        object.__setattr__(self, "delivery_context", tuple(context))
        if len(self.messages) > 32 or any(not isinstance(item, BotMessage) for item in self.messages):
            raise ValueError("At most 32 typed messages are accepted")
        if sum(len(item.content) for item in self.messages) > 12000:
            raise ValueError("Conversation exceeds the input limit")
        if self.event == "user_message" and (not self.messages or self.messages[-1].role != "user"):
            raise ValueError("A user message must end the conversation")
        if self.event == "session_start" and self.messages:
            raise ValueError("A session opening must have empty history")

    def to_dict(self) -> dict:
        value = {
            "version": self.version, "requestId": self.request_id,
            "sessionId": self.session_id, "turnId": self.turn_id,
            "replyId": self.reply_id, "generationEpoch": self.generation_epoch,
            "event": self.event, "options": dict(self.options),
            "messages": [item.to_dict() for item in self.messages],
        }
        if self.delivery_context:
            value["deliveryContext"] = [dict(item) for item in self.delivery_context]
        return value


@dataclass(frozen=True)
class BotEvent:
    request_id: str
    type: Literal["text_delta", "progress", "action", "done", "error"]
    text: str = ""
    finish_reason: str = "stop"
    code: str = ""
    message: str = ""
    version: int = 1
    event_seq: int = 0
    action: str = ""
    push_id: str = ""

    def to_dict(self) -> dict:
        value = {"version": self.version, "requestId": self.request_id, "type": self.type}
        if self.version == 2:
            value["eventSeq"] = self.event_seq
        if self.type in {"text_delta", "progress"}:
            value["text"] = self.text
            if self.push_id:
                value["pushId"] = self.push_id
        elif self.type == "action":
            value.update(action=self.action, when="after_playback")
        elif self.type == "done":
            value["finishReason"] = self.finish_reason
        else:
            value.update(code=self.code, message=self.message)
        return value


@runtime_checkable
class TextBot(Protocol):
    protocol_version: int

    def stream(self, request: BotRequest) -> AsyncIterator[BotEvent]: ...


@runtime_checkable
class PreparingTextBot(TextBot, Protocol):
    """Legacy v1 preparation capability, not required by ConversationBot v2.

    The current voice factory uses duplex-owned VadPrefetch instead.
    Cancellation clears background work; aclose releases session resources.
    """

    async def prepare(self, request: BotRequest) -> None: ...
    async def cancel_pending(self) -> None: ...
    async def aclose(self) -> None: ...


class LegacyTextBotAdapter:
    """Keep old demo/custom/speculative bots behind the same boundary."""

    protocol_version = 1

    def __init__(self, agent) -> None:
        self.agent = agent

    async def stream(self, request: BotRequest) -> AsyncIterator[BotEvent]:
        # Only this adapter translates the historical machine role/signals.
        messages = [{"role": "machine" if m.role == "assistant" else "user", "content": m.content}
                    for m in request.messages]
        iterator = self.agent.stream_reply(messages, signal="pick_up" if request.event == "session_start" else None)
        try:
            async for text in iterator:
                yield BotEvent(request.request_id, "text_delta", text=text)
            yield BotEvent(request.request_id, "done")
        finally:
            await iterator.aclose()

    def notify_partial(self, text: str, messages: list[dict]) -> None:
        extension = getattr(self.agent, "notify_partial", None)
        if extension:
            extension(text, messages)

    async def prepare(self, request: BotRequest) -> None:
        base = [{"role": "machine" if m.role == "assistant" else "user", "content": m.content}
                for m in request.messages[:-1]]
        self.notify_partial(request.messages[-1].content, base)

    async def cancel_pending(self) -> None:
        cancel = getattr(self.agent, "cancel_pending", None)
        if cancel:
            await cancel()

    async def aclose(self) -> None:
        await self.cancel_pending()


def adapt_text_bot(bot) -> TextBot:
    if isinstance(bot, TextBot):
        if bot.protocol_version != 1:
            raise ValueError("Unsupported text bot implementation version")
        return bot
    if callable(getattr(bot, "stream_reply", None)):
        return LegacyTextBotAdapter(bot)
    raise TypeError("Bot must implement TextBot v1 or the legacy stream_reply interface")


def history_messages(history: list[dict], max_chars: int = 6000) -> tuple[BotMessage, ...]:
    """Bound context without a second model call or cutting words/messages."""
    selected = []
    length = 0
    for item in reversed(history[-32:]):
        text = item.get("content", "")
        if not text.strip():
            continue
        if selected and length + len(text) > max_chars:
            break
        role = "assistant" if item.get("role") in ("machine", "robot", "assistant") else "user"
        selected.append(BotMessage(role, text))
        length += len(text)
    return tuple(reversed(selected))


async def stream_bot(bot: TextBot, request: BotRequest, *, timeout_seconds: float = 30,
                     max_output_chars: int = 4096) -> AsyncIterator[BotEvent]:
    """Validate correlation, terminal events, limits and cancellation once.

    wait_for applies to each iterator read, not across a yield into the TTS
    consumer, so a timer cannot unexpectedly cancel the media worker itself.
    """
    iterator = bot.stream(request)
    remaining = timeout_seconds
    total = 0
    action_seen = False
    try:
        while True:
            if remaining <= 0:
                raise TextBotError("BOT_TIMEOUT", "Text bot response timed out")
            try:
                started = time.monotonic()
                try:
                    event = await asyncio.wait_for(anext(iterator), remaining)
                finally:
                    # Charge only time spent waiting for the bot, never time
                    # the caller spends synthesizing/playing yielded text.
                    remaining -= time.monotonic() - started
            except StopAsyncIteration:
                raise TextBotError("BOT_PROTOCOL_ERROR", "Text bot ended without a terminal event") from None
            if not isinstance(event, BotEvent) or event.request_id != request.request_id:
                raise TextBotError("BOT_PROTOCOL_ERROR", "Text bot response correlation is invalid")
            if event.version != request.version:
                raise TextBotError("BOT_PROTOCOL_ERROR", "Text bot response version is invalid")
            if event.type == "error":
                raise TextBotError("BOT_FAILED", "Text bot could not complete this reply")
            if event.type == "done":
                if not total:
                    raise TextBotError("BOT_EMPTY_REPLY", "Text bot returned an empty reply")
                if event.finish_reason not in ("stop", "length"):
                    raise TextBotError("BOT_PROTOCOL_ERROR", "Invalid finish reason")
                yield event
                return
            if event.type == "action":
                if action_seen or event.action != "hang_up" or not total:
                    raise TextBotError("BOT_PROTOCOL_ERROR", "Invalid bot action")
                action_seen = True
                yield event
                continue
            if action_seen or event.type not in {"text_delta", "progress"} or not isinstance(event.text, str) or not event.text:
                raise TextBotError("BOT_PROTOCOL_ERROR", "Invalid text delta")
            if event.type == "progress" and (not event.text.strip() or len(event.text) > 240 or event.text.rstrip()[-1] not in ".!?。！？"):
                raise TextBotError("BOT_PROTOCOL_ERROR", "Progress must be a short complete utterance")
            total += len(event.text)
            if total > max_output_chars:
                raise TextBotError("BOT_OUTPUT_LIMIT", "Text bot output exceeds the limit")
            yield event
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise TextBotError("BOT_TIMEOUT", "Text bot response timed out") from None
    except TextBotError:
        raise
    except Exception:
        raise TextBotError("BOT_UNAVAILABLE", "Text bot provider is unavailable") from None
    finally:
        try:
            await iterator.aclose()
        except Exception:
            # A cleanup failure must not expose provider details or mask the
            # original error. Cancellation is a BaseException and propagates.
            pass
