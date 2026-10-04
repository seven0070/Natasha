"""In-process event fan-out for live UI streaming (WebSocket activity feed).

Deliberately simple: an async subscriber set plus a small backlog. Nothing in the runtime
depends on it, so a slow or absent subscriber can never block an audit write.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any

_BACKLOG = 200


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._backlog: deque[dict[str, Any]] = deque(maxlen=_BACKLOG)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- subscription ---------------------------------------------------------- #
    def subscribe(self, maxsize: int = 512) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def backlog(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._backlog)[-limit:]

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- publishing ------------------------------------------------------------ #
    def publish(self, event: Any) -> None:
        """Publish from inside a running event loop."""
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        with self._lock:
            self._backlog.append(payload)
            self._loop = asyncio.get_running_loop()
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Never let a slow consumer stall the runtime: drop for that subscriber.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except Exception:
                    pass

    def publish_threadsafe(self, event: Any) -> None:
        """Publish from a worker thread (the common case: sync code logging events)."""
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        with self._lock:
            self._backlog.append(payload)
            loop = self._loop
            subscribers = list(self._subscribers)
        if loop is None or loop.is_closed():
            return
        for queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except (RuntimeError, asyncio.QueueFull):
                pass

    def reset(self) -> None:
        with self._lock:
            self._subscribers.clear()
            self._backlog.clear()
            self._loop = None


_BUS: EventBus | None = None
_LOCK = threading.Lock()


def get_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        with _LOCK:
            if _BUS is None:
                _BUS = EventBus()
    return _BUS
