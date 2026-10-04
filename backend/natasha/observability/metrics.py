"""In-process metrics with a snapshot for the API and the dashboard."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..core.clock import iso


@dataclass
class Counter:
    name: str
    value: float = 0.0
    labels: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "labels": {key: value for key, value in sorted(self.labels.items())}}


@dataclass
class Histogram:
    name: str
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    samples: deque = field(default_factory=lambda: deque(maxlen=500))

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.samples.append(value)

    def percentile(self, fraction: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
        return ordered[index]

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "mean": round(self.total / self.count, 4) if self.count else 0.0,
                "min": self.minimum, "max": self.maximum, "p50": round(self.percentile(0.5), 4),
                "p95": round(self.percentile(0.95), 4)}


class Metrics:
    """Thread-safe counters and histograms."""

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.RLock()
        self.started_at = iso()

    def increment(self, name: str, *, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = _key(name, labels)
        with self._lock:
            counter = self._counters.setdefault(key, Counter(name=key))
            counter.value += amount
            for label, value in (labels or {}).items():
                counter.labels[f"{label}={value}"] = counter.labels.get(f"{label}={value}", 0.0) + amount

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            histogram = self._histograms.setdefault(name, Histogram(name=name))
            histogram.observe(float(value))

    def timer(self, name: str) -> "_Timer":
        return _Timer(self, name)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"started_at": self.started_at,
                    "counters": {key: counter.to_dict() for key, counter in sorted(self._counters.items())},
                    "histograms": {key: histogram.to_dict() for key, histogram in sorted(self._histograms.items())}}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


class _Timer:
    """Context manager that records elapsed milliseconds into a histogram."""

    def __init__(self, metrics: Metrics, name: str) -> None:
        self.metrics = metrics
        self.name = name
        self.started = 0.0

    def __enter__(self) -> "_Timer":
        self.started = time.perf_counter()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.metrics.observe(self.name, (time.perf_counter() - self.started) * 1000)


def _key(name: str, labels: dict[str, str] | None) -> str:
    if not labels:
        return name
    parts = ",".join(f"{label}={value}" for label, value in sorted(labels.items()))
    return f"{name}{{{parts}}}"


_METRICS: Metrics | None = None
_LOCK = threading.Lock()


def get_metrics() -> Metrics:
    global _METRICS
    with _LOCK:
        if _METRICS is None:
            _METRICS = Metrics()
        return _METRICS


def reset_metrics() -> None:
    global _METRICS
    with _LOCK:
        _METRICS = Metrics()
