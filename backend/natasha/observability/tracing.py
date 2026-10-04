"""Lightweight spans, recorded into the append-only event log."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind


@dataclass
class Span:
    """One timed unit of work."""

    name: str
    trace_id: str = ""
    parent_id: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=iso)
    duration_ms: float = 0.0
    ok: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "trace_id": self.trace_id, "parent_id": self.parent_id,
                "attributes": self.attributes, "started_at": self.started_at,
                "duration_ms": round(self.duration_ms, 3), "ok": self.ok, "error": self.error}


class Tracer:
    """Creates spans and writes them to the event log (and metrics, if attached)."""

    def __init__(self, *, log: Any = None, metrics: Any = None, max_spans: int = 500) -> None:
        self.log = log
        self.metrics = metrics
        self.spans: list[Span] = []
        self.max_spans = max_spans
        self._stack: threading.local = threading.local()
        self._lock = threading.RLock()

    def _current(self) -> Span | None:
        stack = getattr(self._stack, "spans", None)
        return stack[-1] if stack else None

    @contextmanager
    def span(self, name: str, *, trace_id: str = "", **attributes: Any) -> Iterator[Span]:
        parent = self._current()
        span = Span(name=name, trace_id=trace_id or (parent.trace_id if parent else ""),
                    parent_id=(parent.name if parent else ""), attributes=attributes)
        stack = getattr(self._stack, "spans", None)
        if stack is None:
            stack = []
            self._stack.spans = stack
        stack.append(span)
        started = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.ok = False
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            stack.pop()
            span.duration_ms = (time.perf_counter() - started) * 1000
            self.record(span)

    def record(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span)
            if len(self.spans) > self.max_spans:
                self.spans = self.spans[-self.max_spans:]
        if self.metrics is not None:
            try:
                self.metrics.observe(f"span_ms:{span.name}", span.duration_ms)
            except Exception:
                pass
        if self.log is not None:
            try:
                self.log.append(EventKind.SYSTEM, {"action": "span", **span.to_dict()},
                                actor="system", source="tracing", trace_id=span.trace_id,
                                risk=RiskLevel.NONE)
            except Exception:
                pass

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [span.to_dict() for span in self.spans[-limit:]]


_TRACER: Tracer | None = None
_LOCK = threading.Lock()


def get_tracer(**kwargs: Any) -> Tracer:
    global _TRACER
    with _LOCK:
        if _TRACER is None:
            _TRACER = Tracer(**kwargs)
        return _TRACER


def reset_tracer() -> None:
    global _TRACER
    with _LOCK:
        _TRACER = None
