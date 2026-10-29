from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class HistoryItem:
    message: str
    message_type: str
    start_time: int
    end_time: int
    role: str
    turn_id: Optional[str] = None
    reply_id: Optional[str] = None


class ConversationHistory:
    def __init__(self) -> None:
        self._items: List[HistoryItem] = []

    def append(self, item: HistoryItem) -> None:
        self._items.append(item)

    @property
    def items(self) -> List[HistoryItem]:
        return list(self._items)

    def to_agent_messages(self) -> List[dict]:
        """Return only user-visible dialogue for the LLM.

        Signals such as ``pick_up``, ``reply_start`` and ``barge_in`` are
        orchestration state.  They remain in the audit history but must never
        leak into the language-model conversation.
        """
        messages = []
        for item in self._items:
            if item.message_type != "text":
                continue
            if item.role == "user":
                messages.append({"role": "user", "content": item.message})
            elif item.role == "robot":
                messages.append({"role": "machine", "content": item.message})
        return messages
