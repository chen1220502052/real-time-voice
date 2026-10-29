"""Bot-owned, exact delivered-prefix suppression for explicit resume requests."""

import re


def continuation_prefix(request):
    if not request.messages or not request.delivery_context:
        return ""
    text = request.messages[-1].content.strip()
    intent = re.fullmatch(
        r"(?:(?:please|can you|could you)\s+)?(?:continue(?:\s+what you were saying)?|"
        r"finish what you were saying|go on|carry on)[.!?\s]*|(?:请)?(?:继续|接着说|继续说|说完)[。！？\s]*",
        text, re.I)
    context = request.delivery_context[-1]
    delivered = context["deliveredText"]
    if intent and delivered and context["generatedText"].startswith(delivered):
        return " ".join(delivered.split())
    return ""


async def without_delivered_prefix(source, prefix):
    """Only remove a complete exact echoed prefix, once; never fuzzy-match.

    Ordinary answers and new topics have an empty prefix and no added wait.
    On mismatch, release the original text unchanged. Own provider cleanup even
    when cancellation occurs while waiting to disambiguate a partial token.
    """
    pending = ""
    resolved = not prefix
    stripped = False
    new_text = False
    try:
        async for delta in source:
            if not isinstance(delta, str):
                raise TypeError("Model deltas must be text")
            if resolved:
                new_text = new_text or any(c.isalnum() for c in delta)
                yield delta
                continue
            pending += delta
            normalized = re.sub(r"\s+", " ", pending).lstrip()
            if prefix.casefold().startswith(normalized.casefold()):
                continue
            if normalized[:len(prefix)].casefold() == prefix.casefold() and (
                    len(normalized) > len(prefix) and not normalized[len(prefix)].isalnum()
                    and normalized[len(prefix)] not in "'’-_"):
                # Keep punctuation after the heard word; whitespace is just a
                # seam, not new spoken content. Subsequent text is untouched.
                tail = normalized[len(prefix):].lstrip()
                stripped = True
                if tail:
                    new_text = any(c.isalnum() for c in tail)
                    yield tail
            else:
                yield pending
            pending, resolved = "", True
        if pending:
            if " ".join(pending.split()).casefold() == prefix.casefold():
                from .text_bot import TextBotError
                raise TextBotError("BOT_INCOMPLETE_REPLY", "Continuation contains no new text")
            yield pending
        if stripped and not new_text:
            from .text_bot import TextBotError
            raise TextBotError("BOT_INCOMPLETE_REPLY", "Continuation contains no new text")
    finally:
        await source.aclose()
