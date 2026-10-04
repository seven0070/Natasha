"""Working memory: the current task's short-lived scratchpad."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..core.clock import iso, parse_iso


@dataclass
class WorkingItem:
    content: str
    role: str = "note"          # note | observation | thought | plan | decision
    created_at: str = field(default_factory=iso)
    mission_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "role": self.role, "created_at": self.created_at,
                "mission_id": self.mission_id, "metadata": self.metadata}


class WorkingMemory:
    """Bounded, expiring scratchpad - never a durable store, always inspectable."""

    def __init__(self, *, capacity: int = 32, ttl_seconds: float = 3600.0) -> None:
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._items: deque[WorkingItem] = deque(maxlen=capacity)
        self._lock = threading.RLock()

    def push(self, content: str, *, role: str = "note", mission_id: str = "",
             metadata: dict[str, Any] | None = None) -> WorkingItem:
        item = WorkingItem(content=content.strip(), role=role, mission_id=mission_id,
                           metadata=dict(metadata or {}))
        with self._lock:
            self._items.append(item)
        return item

    def items(self, *, mission_id: str = "", limit: int = 50) -> list[WorkingItem]:
        self._expire()
        with self._lock:
            items = list(self._items)
        if mission_id:
            items = [item for item in items if item.mission_id in ("", mission_id)]
        return items[-limit:]

    def render(self, *, mission_id: str = "", limit: int = 12) -> str:
        items = self.items(mission_id=mission_id, limit=limit)
        if not items:
            return ""
        return "\n".join(f"[{item.role}] {item.content}" for item in items)

    def clear(self, *, mission_id: str = "") -> int:
        with self._lock:
            if not mission_id:
                count = len(self._items)
                self._items.clear()
                return count
            keep = [item for item in self._items if item.mission_id != mission_id]
            removed = len(self._items) - len(keep)
            self._items = deque(keep, maxlen=self.capacity)
        return removed

    def _expire(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        with self._lock:
            keep = [item for item in self._items
                    if (now - parse_iso(item.created_at)).total_seconds() < self.ttl_seconds]
            if len(keep) != len(self._items):
                self._items = deque(keep, maxlen=self.capacity)

    def stats(self) -> dict[str, Any]:
        self._expire()
        with self._lock:
            return {"items": len(self._items), "capacity": self.capacity, "ttl_seconds": self.ttl_seconds}


_WORKING: WorkingMemory | None = None
_LOCK = threading.Lock()


def get_working_memory(**kwargs: Any) -> WorkingMemory:
    global _WORKING
    with _LOCK:
        if _WORKING is None:
            _WORKING = WorkingMemory(**kwargs)
        return _WORKING


def reset_working_memory() -> None:
    global _WORKING
    with _LOCK:
        _WORKING = None
