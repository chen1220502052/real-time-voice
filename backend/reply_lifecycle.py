"""Reply-scoped delivery deadlines; timeout is never proof of playback."""
import asyncio


class PlaybackDeadline:
    def __init__(self, seconds):
        self.seconds = seconds
        self.task = None

    def cancel(self):
        task, self.task = self.task, None
        if task and task is not asyncio.current_task():
            task.cancel()

    def arm(self, on_timeout):
        self.cancel()
        async def wait():
            try:
                await asyncio.sleep(self.seconds)
                await on_timeout()
            except asyncio.CancelledError:
                pass
        self.task = asyncio.create_task(wait())
