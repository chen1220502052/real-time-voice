import time
import uuid
from typing import Any, Dict, Optional


def make_session_event(
    event_type: str,
    component: str,
    *,
    level: str = "info",
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    reply_id: Optional[str] = None,
    generation_epoch: Optional[int] = None,
    sequence: Optional[int] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the common event envelope used by backend and browser.

    Wall-clock time is useful for operators, while monotonic time is the only
    safe basis for latency calculations within a process.
    """

    return {
        "eventId": "evt_{}".format(uuid.uuid4().hex[:16]),
        "eventType": event_type,
        "component": component,
        "level": level,
        "sessionId": session_id,
        "turnId": turn_id,
        "replyId": reply_id,
        "generationEpoch": generation_epoch,
        "sequence": sequence,
        "timestampMs": int(time.time() * 1000),
        "monotonicMs": round(time.monotonic() * 1000, 3),
        "clockDomain": "server_monotonic",
        "attributes": attributes or {},
    }
