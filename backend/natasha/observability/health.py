"""Health checks that are allowed to say "degraded"."""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core import get_paths
from ..core.clock import iso

Status = str  # "ok" | "degraded" | "down"


@dataclass
class HealthReport:
    """Result of one check."""

    name: str
    status: Status
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    checked_at: str = field(default_factory=iso)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "data": self.data,
                "latency_ms": round(self.latency_ms, 3), "checked_at": self.checked_at}


class HealthMonitor:
    """Runs named health checks and aggregates them, worst-first."""

    def __init__(self, *, log: Any = None) -> None:
        self.log = log
        self._checks: dict[str, Callable[[], Any]] = {}
        self._lock = threading.RLock()
        self.register("disk", self._check_disk)
        self.register("paths", self._check_paths)
        self.register("event_log", self._check_event_log)
        self.register("process", self._check_process)

    # ------------------------------------------------------------------ checks
    def register(self, name: str, check: Callable[[], Any]) -> None:
        with self._lock:
            self._checks[name] = check

    def _check_disk(self) -> HealthReport:
        usage = shutil.disk_usage(str(get_paths().home))
        free_ratio = usage.free / usage.total if usage.total else 0.0
        status = "ok" if free_ratio > 0.05 else ("degraded" if free_ratio > 0.01 else "down")
        return HealthReport("disk", status, f"{free_ratio:.1%} free",
                            {"free_bytes": usage.free, "total_bytes": usage.total})

    def _check_paths(self) -> HealthReport:
        paths = get_paths()
        missing = [str(path) for path in (paths.home, paths.workspace, paths.artifacts, paths.db)
                   if not path.exists()]
        return HealthReport("paths", "degraded" if missing else "ok",
                            "missing: " + ", ".join(missing) if missing else "all runtime directories present",
                            {"home": str(paths.home), "missing": missing})

    def _check_event_log(self) -> HealthReport:
        if self.log is None:
            return HealthReport("event_log", "degraded", "no event log attached")
        try:
            stats = self.log.stats() if hasattr(self.log, "stats") else {}
            verify = self.log.verify() if hasattr(self.log, "verify") else {"ok": True}
            if not verify.get("ok", False):
                return HealthReport("event_log", "down", f"hash chain broken at {verify}", stats)
            return HealthReport("event_log", "ok", "hash chain intact", stats)
        except Exception as exc:
            return HealthReport("event_log", "degraded", f"{type(exc).__name__}: {exc}")

    def _check_process(self) -> HealthReport:
        import os

        try:
            import resource

            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            rss_mb = 0.0
        return HealthReport("process", "ok", f"pid {os.getpid()}", {"pid": os.getpid(),
                                                                   "max_rss_mb": round(rss_mb, 1)})

    # ------------------------------------------------------------------ aggregate
    def run(self, name: str) -> HealthReport:
        with self._lock:
            check = self._checks.get(name)
        if check is None:
            return HealthReport(name, "down", "no such check")
        started = time.perf_counter()
        try:
            report = check()
        except Exception as exc:
            return HealthReport(name, "down", f"{type(exc).__name__}: {exc}",
                                latency_ms=(time.perf_counter() - started) * 1000)
        report.latency_ms = (time.perf_counter() - started) * 1000
        return report

    def snapshot(self) -> dict[str, Any]:
        reports = {name: self.run(name) for name in sorted(self._checks)}
        worst = "ok"
        for report in reports.values():
            if report.status == "down":
                worst = "down"
                break
            if report.status == "degraded":
                worst = "degraded"
        return {"status": worst, "checks": {name: report.to_dict() for name, report in reports.items()},
                "at": iso()}


_MONITOR: HealthMonitor | None = None
_LOCK = threading.Lock()


def get_health_monitor(**kwargs: Any) -> HealthMonitor:
    global _MONITOR
    with _LOCK:
        if _MONITOR is None:
            _MONITOR = HealthMonitor(**kwargs)
        return _MONITOR


def reset_health_monitor() -> None:
    global _MONITOR
    with _LOCK:
        _MONITOR = None
