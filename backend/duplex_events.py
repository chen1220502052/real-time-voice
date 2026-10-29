"""Shared internal event envelope; re-exported by backend.duplex."""

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class DuplexEvent:
    kind: str
    payload: Dict[str, Any]
