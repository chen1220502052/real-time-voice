"""Application-owned HTTP reuse; request-local, content-free latency probes."""
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import time
import ssl

import aiohttp

from .observability import log_event

_pool = ContextVar("llm_http_pool", default=None)
_timing = ContextVar("bot_timing", default=None)


class Timing:
    """Milestones in ms since BotService.start, not stage durations.

    eventPersistenceMs is the cumulative durable-write duration. A prefetched
    hit has no provider milestones here: absent means unmeasured, never zero.
    Probes contain identifiers/counts only, not prompts or credentials.
    """
    def __init__(self, request_id):
        self.request_id = request_id
        self.started = time.perf_counter()
        self.metrics = {}

    def mark(self, name):
        self.metrics.setdefault(name, round((time.perf_counter() - self.started) * 1000, 2))

    def report(self):
        self.mark("terminalMs")
        log_event("bot.latency", requestId=self.request_id, **self.metrics)


def timing():
    return _timing.get()


@contextmanager
def use_timing(probe):
    token = _timing.set(probe)
    try:
        yield
    finally:
        _timing.reset(token)


@contextmanager
def use_llm_pool(pool):
    token = _pool.set(pool)
    try:
        yield
    finally:
        _pool.reset(token)


def _client():
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    trace = aiohttp.TraceConfig()

    async def reused(session, ctx, params):
        if ctx.trace_request_ctx is not None:
            ctx.trace_request_ctx.metrics["connectionReused"] = True

    async def connected(session, ctx, params):
        if ctx.trace_request_ctx is not None:
            ctx.trace_request_ctx.mark("connectionReadyMs")

    trace.on_connection_reuseconn.append(reused)
    trace.on_connection_create_end.append(connected)
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=120),
        connector=aiohttp.TCPConnector(ssl=context, limit=8, limit_per_host=4,
                                       ttl_dns_cache=300, keepalive_timeout=30),
        cookie_jar=aiohttp.DummyCookieJar(), trace_configs=[trace])


class LLMHttpPool:
    """One event-loop/application owner; close after all Bot tasks stop.

    Closing a streaming response may discard that connection if unread bytes
    remain. Never drain an abandoned generation merely to preserve keepalive.
    """
    def __init__(self):
        self.client = None

    def get(self):
        if self.client is None or self.client.closed:
            self.client = _client()
        return self.client

    async def close(self):
        if self.client is not None:
            await self.client.close()


@asynccontextmanager
async def llm_client():
    owner = _pool.get()
    if owner is not None:
        yield owner.get()
    else:
        # Standalone adapters retain explicit ownership and never leak across
        # asyncio.run() loops. App and CLI requests use the managed pool.
        async with _client() as client:
            yield client
