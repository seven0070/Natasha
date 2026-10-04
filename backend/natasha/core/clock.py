"""Time sources.

All persisted timestamps are timezone-aware UTC. A :class:`Clock` is injectable so tests can
travel in time without sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    """RFC3339/ISO-8601 representation used everywhere in storage."""
    return (value or utcnow()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string produced by :func:`iso` (or a close cousin)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class Clock:
    """Injectable clock. ``advance``/``travel`` exist for deterministic tests."""

    _offset: timedelta = field(default_factory=timedelta)
    _monotonic: Callable[[], float] = field(default=time.monotonic)

    def now(self) -> datetime:
        return utcnow() + self._offset

    def timestamp(self) -> str:
        return iso(self.now())

    def advance(self, seconds: float) -> None:
        self._offset += timedelta(seconds=seconds)

    def travel(self, delta: timedelta) -> None:
        self._offset += delta

    def reset(self) -> None:
        self._offset = timedelta()

    def elapsed(self) -> float:
        return self._monotonic()
