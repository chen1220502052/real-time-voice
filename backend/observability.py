"""Structured logging and optional OpenTelemetry spans for local development."""

import contextlib
import contextvars
import json
import logging
import os
import re
import time
import uuid
from typing import Dict, Iterator, Optional


_trace_id = contextvars.ContextVar("trace_id", default="-")
_configured = False

try:
    from opentelemetry import trace as otel_trace  # type: ignore
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor  # type: ignore
    OTEL_AVAILABLE = True
except ImportError:  # Optional dependency: application logging still works without it.
    otel_trace = None
    OTEL_AVAILABLE = False


def configure_observability() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
    if OTEL_AVAILABLE and os.getenv("OTEL_CONSOLE_EXPORTER", "0") == "1":
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        otel_trace.set_tracer_provider(provider)
    _configured = True


def new_trace_id() -> str:
    return uuid.uuid4().hex


@contextlib.contextmanager
def trace_context(trace_id: str) -> Iterator[None]:
    token = _trace_id.set(trace_id)
    try:
        yield
    finally:
        _trace_id.reset(token)


def log_event(event: str, level: int = logging.INFO, **fields: object) -> None:
    # Per-frame start/end pairs alone exhausted journald's service-wide budget,
    # hiding login, rejection and disconnect logs. Timelines sent to the browser
    # remain unchanged; detailed server spans can be explicitly re-enabled.
    if os.getenv('LOG_AUDIO_SPANS', '0') != '1':
        if event in {'span.start', 'span.end'} and (fields.get('span') in {'duplex.asr_vad', 'websocket.audio.decode'} or
                (fields.get('span') == 'websocket.control' and fields.get('messageType') == 'client_event')):
            return
        if event == 'control.received' and fields.get('messageType') == 'client_event':
            return
        if event == 'client.event' and fields.get('eventType') == 'playback_progress':
            return
    payload: Dict[str, object] = {"event": event, "traceId": _trace_id.get(),
                                  **{key: _sanitize(value) for key, value in fields.items()}}
    logging.getLogger("real_time_voice").log(level, json.dumps(payload, ensure_ascii=False, default=str))


def _sanitize(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = re.sub(r"\b(?:ghp_|sk-[A-Za-z0-9_-]*|sk-proj-)[A-Za-z0-9_-]+", "<secret>", value)
    home = os.path.expanduser("~")
    text = re.sub(re.escape(home) + r"/[^\s\"']+", "<path>", text)
    return text


@contextlib.contextmanager
def span(name: str, **attributes: object) -> Iterator[None]:
    started = time.perf_counter()
    log_event("span.start", span=name, **attributes)
    tracer_scope = None
    tracer_span = None
    try:
        if OTEL_AVAILABLE:
            tracer = otel_trace.get_tracer("real_time_voice")
            tracer_scope = tracer.start_as_current_span(name, attributes=attributes)
            tracer_span = tracer_scope.__enter__()
        yield
    except Exception as exc:
        log_event("span.error", level=logging.ERROR, span=name, error=str(exc), **attributes)
        if tracer_span is not None:
            tracer_span.record_exception(exc)
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if tracer_scope is not None:
            tracer_scope.__exit__(None, None, None)
        log_event("span.end", span=name, durationMs=elapsed_ms, **attributes)
