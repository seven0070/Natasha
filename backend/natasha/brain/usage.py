"""Usage accounting: tokens, cost, latency and quota visibility."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..core.clock import iso


@dataclass
class UsageRecord:
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    task: str = ""
    actor: str = ""
    success: bool = True
    at: str = field(default_factory=iso)
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens, "total_tokens": self.total_tokens, "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms, "task": self.task, "actor": self.actor, "success": self.success,
            "at": self.at, "error": self.error,
        }


class UsageTracker:
    """Aggregates usage; keeps a bounded recent history in memory."""

    def __init__(self, *, history: int = 1000, daily_budget_usd: float | None = None) -> None:
        self.history_limit = history
        self.daily_budget_usd = daily_budget_usd
        self._records: list[UsageRecord] = []
        self._totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._lock = threading.RLock()

    def record(self, record: UsageRecord) -> UsageRecord:
        with self._lock:
            self._records.append(record)
            if len(self._records) > self.history_limit:
                self._records = self._records[-self.history_limit // 2:]
            bucket = self._totals[f"{record.provider}:{record.model}"]
            bucket["calls"] += 1
            bucket["input_tokens"] += record.input_tokens
            bucket["output_tokens"] += record.output_tokens
            bucket["cost_usd"] += record.cost_usd
            bucket["latency_ms_sum"] += record.latency_ms
            bucket["latency_ms_max"] = max(bucket["latency_ms_max"], record.latency_ms)
            if not record.success:
                bucket["failures"] += 1
        return record

    def totals(self, *, provider: str = "") -> dict[str, Any]:
        with self._lock:
            if provider:
                keys = [key for key in self._totals if key.startswith(f"{provider}:")]
            else:
                keys = list(self._totals)
            summary = {
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "failures": 0,
            }
            for key in keys:
                bucket = self._totals[key]
                summary["calls"] += int(bucket["calls"])
                summary["input_tokens"] += int(bucket["input_tokens"])
                summary["output_tokens"] += int(bucket["output_tokens"])
                summary["cost_usd"] += float(bucket["cost_usd"])
                summary["failures"] += int(bucket["failures"])
        summary["cost_usd"] = round(summary["cost_usd"], 6)
        summary["total_tokens"] = summary["input_tokens"] + summary["output_tokens"]
        return summary

    def by_model(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                key: {
                    "calls": int(bucket["calls"]), "input_tokens": int(bucket["input_tokens"]),
                    "output_tokens": int(bucket["output_tokens"]), "cost_usd": round(float(bucket["cost_usd"]), 6),
                    "avg_latency_ms": round(float(bucket["latency_ms_sum"]) / max(1, bucket["calls"]), 1),
                    "max_latency_ms": round(float(bucket["latency_ms_max"]), 1),
                    "failures": int(bucket["failures"]),
                }
                for key, bucket in self._totals.items()
            }

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict() for record in self._records[-limit:]][::-1]

    def budget_status(self) -> dict[str, Any]:
        spent = self.totals()["cost_usd"]
        remaining = None if self.daily_budget_usd is None else round(self.daily_budget_usd - spent, 6)
        return {
            "daily_budget_usd": self.daily_budget_usd, "spent_usd": spent, "remaining_usd": remaining,
            "over_budget": bool(self.daily_budget_usd is not None and spent >= self.daily_budget_usd),
        }

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._totals.clear()


_TRACKER: UsageTracker | None = None
_LOCK = threading.Lock()


def get_usage_tracker() -> UsageTracker:
    global _TRACKER
    if _TRACKER is None:
        with _LOCK:
            if _TRACKER is None:
                _TRACKER = UsageTracker()
    return _TRACKER


def reset_usage_tracker() -> None:
    global _TRACKER
    with _LOCK:
        _TRACKER = None
