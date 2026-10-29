"""Pure delivery projection; the Bot owns semantic history, not the duplex."""

from .text_bot import BotRequest, TextBotError, history_messages


def delivered_text(payload, events, status, facts):
    """Project only confirmed delivered content; never estimate from sent bytes.

    Speech segments are TTS text/audio alignment facts. Partial segments stay
    in the audit ledger, not in assistant history. Stop is irreversible; a late
    completion cannot promote an interrupted draft to a fully heard reply.
    """
    text = "".join(e.get("text", "") for e in events if e["type"] in {"text_delta", "progress"})
    reply = payload["replyId"]
    values = [e for e in facts if e["replyId"] == reply]
    if payload["channel"] == "text":
        return text if status == "done" and any(e["type"] == "text_displayed" for e in values) else ""
    stopped = [e for e in values if e["type"] == "playback_stopped"]
    if not stopped and any(e["type"] == "playback_completed" for e in values) and status == "done":
        return text
    stop_seq = min((e["sequence"] for e in stopped), default=2**53)
    position = max((e.get("playedMs", 0) for e in values
                    if e["type"] in {"playback_stopped", "playback_progress"}
                    and e["sequence"] <= stop_seq), default=0)
    segments = sorted((e for e in values if e["type"] == "speech_segment"), key=lambda e: e["sequence"])
    heard = ""
    for segment in segments:
        if segment["audioEndMs"] > position:
            break
        heard += segment["text"]
    # Never allow transport metadata to inject ungenerated assistant text.
    return heard if text.startswith(heard) else ""


def project_request(query, rows, facts):
    history = []
    delivery_context = []
    found = False
    idle_count = 0
    for payload, events, status in rows:
        if dict(query.options) != payload["options"] or query.channel != payload["channel"]:
            raise TextBotError("BOT_SESSION_OPTIONS", "Start a new session to change bot options")
        if query.event == "session_start" and payload["requestId"] != query.request_id:
            raise TextBotError("BOT_SESSION_EXISTS", "Start a new session ID for a new conversation")
        if payload["event"] == "user_message":
            history.append({"role": "user", "content": payload["text"]})
            idle_count = 0
        heard = delivered_text(payload, events, status, facts)
        if heard:
            history.append({"role": "assistant", "content": heard})
            if payload["event"] == "time_out":
                idle_count += 1
        generated = "".join(e.get("text", "") for e in events if e["type"] in {"text_delta", "progress"})
        if generated and generated != heard:
            delivery_context.append({"replyId": payload["replyId"], "generatedText": generated,
                                     "deliveredText": heard, "status": "not_fully_delivered"})
        elif heard:
            # A newer fully delivered reply supersedes older drafts.
            delivery_context.clear()
        if payload["requestId"] == query.request_id:
            found = True
            break
    if not found and query.event == "user_message":
        history.append({"role": "user", "content": query.text})
    return BotRequest(
        query.request_id, query.session_id, query.turn_id,
        messages=() if query.event == "session_start" else history_messages(history),
        event=query.event, options=query.options, reply_id=query.reply_id or query.request_id,
        generation_epoch=query.generation_epoch,
        delivery_context=tuple(delivery_context[-2:]),
        idle_count=idle_count + 1 if query.event == "time_out" else 0,
        channel=query.channel,
    )
