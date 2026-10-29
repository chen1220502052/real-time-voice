"""Bot-owned progress policy for slow asynchronous work, not media timers."""
import asyncio
import os

from .text_bot import BotEvent


async def with_progress(iterator, request, *, delay=None):
    """Keep one model/task read alive while yielding bounded speakable notices.

    No polling/restarting paid work. Closing this stream cancels the read and
    closes its source, including speculative drafts and interrupted tasks.
    """
    delay = max(.01, float(delay if delay is not None else os.getenv("BOT_PROGRESS_DELAY_SECONDS", "4")))
    pending = None
    notices = 0
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            done, _ = await asyncio.wait({pending}, timeout=delay if notices < 2 else None)
            if not done:
                notices += 1
                yield BotEvent(request.request_id, "progress", text="I'm still working on that. Thanks for waiting. ",
                               push_id=f"working_{notices}")
                continue
            try:
                value = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield value
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await iterator.aclose()
