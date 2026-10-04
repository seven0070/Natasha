"""Token-bucket rate limiting for the expensive and security-sensitive endpoints.

The agent can start model calls, shell commands and long missions; without a limit a single client
(misbehaving UI, scripted loop, or an attacker who guessed the token) could exhaust the machine or
brute-force the owner passphrase. This is a small in-process limiter: no dependency, no shared state
between processes, and it fails *closed* for the buckets it guards while never breaking the rest of
the API.

Buckets are named by the caller (``chat``, ``missions``, ``login`` ...), and the key is the client
address plus, when available, the authenticated actor, so one session cannot spend another's budget.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import NatashaError


class RateLimited(NatashaError):
    """The caller exceeded the budget for this bucket. Retryable after ``retry_after`` seconds."""

    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after: float = 1.0, bucket: str = "") -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))
        self.bucket = bucket


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)
    hits: int = 0


class RateLimiter:
    """In-process token buckets with a fixed default table per bucket name."""

    #: bucket name -> (requests per minute, burst). Small but generous enough for a human UI.
    DEFAULTS: dict[str, tuple[float, int]] = {
        "chat": (60.0, 12),
        "models": (30.0, 6),
        "missions": (20.0, 4),
        "creation": (10.0, 3),
        "computer": (60.0, 10),
        "voice": (30.0, 6),
        "vision": (30.0, 6),
        "mcp": (30.0, 6),
        "skills": (30.0, 6),
        "tools": (60.0, 20),
        "login": (10.0, 5),
        "write": (120.0, 30),
        "default": (240.0, 60),
    }

    MAX_KEYS = 4096
    IDLE_TTL = 900.0

    def __init__(self, *, enabled: bool = True, multiplier: float = 1.0,
                 overrides: dict[str, Any] | None = None) -> None:
        self.enabled = bool(enabled)
        self.multiplier = max(0.05, float(multiplier))
        self.overrides: dict[str, tuple[float, int]] = {}
        for name, value in (overrides or {}).items():
            try:
                rate, burst = float(value[0]), int(value[1])
            except (TypeError, ValueError, IndexError):
                continue
            self.overrides[str(name)] = (rate, burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------- #
    def budget(self, name: str) -> tuple[float, int]:
        rate, burst = self.overrides.get(name, self.DEFAULTS.get(name, self.DEFAULTS["default"]))
        return rate * self.multiplier, max(1, int(burst * min(self.multiplier, 1.0)))

    # -- enforcement ----------------------------------------------------------- #
    def check(self, bucket: str, key: str) -> None:
        """Consume one token or raise :class:`RateLimited`. Never raises for internal reasons."""
        if not self.enabled:
            return
        rate, burst = self.budget(bucket)
        if rate <= 0:
            return
        now = time.monotonic()
        identifier = f"{bucket}:{key}"
        with self._lock:
            entry = self._buckets.get(identifier)
            if entry is None:
                if len(self._buckets) > self.MAX_KEYS:
                    self._evict(now)
                entry = _Bucket(tokens=float(burst))
                self._buckets[identifier] = entry
            elapsed = max(0.0, now - entry.updated)
            entry.tokens = min(float(burst), entry.tokens + elapsed * (rate / 60.0))
            entry.updated = now
            entry.hits += 1
            if entry.tokens < 1.0:
                wait = (1.0 - entry.tokens) / (rate / 60.0)
                raise RateLimited(f"too many requests for {bucket!r}; retry in {wait:.1f}s",
                                  retry_after=wait, bucket=bucket)
            entry.tokens -= 1.0

    def remaining(self, bucket: str, key: str) -> float:
        with self._lock:
            entry = self._buckets.get(f"{bucket}:{key}")
        return float("inf") if entry is None else round(entry.tokens, 3)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            rows = [
                {"bucket": key.split(":", 1)[0], "key": key.split(":", 1)[-1],
                 "tokens": round(entry.tokens, 3), "hits": entry.hits}
                for key, entry in self._buckets.items()
            ]
        return {"enabled": self.enabled, "multiplier": self.multiplier, "buckets": rows,
                "count": len(rows)}

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _evict(self, now: float) -> None:
        stale = [key for key, entry in self._buckets.items() if now - entry.updated > self.IDLE_TTL]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) > self.MAX_KEYS:  # still too big: drop the oldest half
            ordered = sorted(self._buckets.items(), key=lambda item: item[1].updated)
            for key, _ in ordered[: len(ordered) // 2]:
                self._buckets.pop(key, None)
