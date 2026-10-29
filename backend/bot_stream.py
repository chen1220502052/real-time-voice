"""Eager subscription ownership, including failure before HTTP headers."""


class RunSubscription:
    def __init__(self, run, after=0):
        self.run, self.cursor, self.closed = run, after, False
        run.subscribers += 1

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            async with self.run.condition:
                await self.run.condition.wait_for(
                    lambda: len(self.run.events) > self.cursor or self.run.terminal)
                if self.cursor < len(self.run.events):
                    value = dict(self.run.events[self.cursor])
                    self.cursor = value['eventSeq']
                    return value
                failure = self.run.failure
            # Never await producer cleanup while holding its condition lock.
            await self.aclose()
            if failure:
                raise failure
            raise StopAsyncIteration
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        import asyncio
        if self.closed:
            return
        self.closed = True
        self.run.subscribers -= 1
        task = self.run.task
        if not self.run.subscribers and not self.run.terminal and task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
