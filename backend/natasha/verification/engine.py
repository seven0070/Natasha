"""The verification engine: run checks, keep evidence, refuse unproven success."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..core import VerificationFailed
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from .checks import CheckResult, CheckStatus, VerificationCheck

#: Which risk level a failed check implies for the record.
_FAILURE_RISK = {"high": RiskLevel.HIGH}


@dataclass
class VerificationReport:
    """The verdict for one subject."""

    subject: str
    results: list[CheckResult] = field(default_factory=list)
    contract: str = "all_required"
    started_at: str = field(default_factory=iso)
    finished_at: str = ""
    notes: str = ""

    @property
    def passed(self) -> bool:
        required = [result for result in self.results if result.required]
        if not required:
            return True
        if self.contract == "any":
            return any(result.ok for result in required)
        return all(result.ok for result in required)

    @property
    def unproven(self) -> list[CheckResult]:
        """Required checks that did not pass - the reason success cannot be claimed."""
        return [result for result in self.results if result.required and not result.ok]

    @property
    def summary(self) -> str:
        total = len(self.results)
        passed = sum(1 for result in self.results if result.ok)
        if self.passed:
            return f"{passed}/{total} checks passed"
        failed = ", ".join(result.name for result in self.unproven[:5])
        return f"{passed}/{total} checks passed; unproven: {failed}"

    def to_dict(self) -> dict[str, Any]:
        return {"subject": self.subject, "passed": self.passed, "contract": self.contract,
                "summary": self.summary, "results": [result.to_dict() for result in self.results],
                "started_at": self.started_at, "finished_at": self.finished_at, "notes": self.notes,
                "unproven": [result.name for result in self.unproven]}


class VerificationEngine:
    """Executes verification plans and records every verdict."""

    def __init__(self, *, log: EventLog | None = None, default_timeout: float = 300.0) -> None:
        self.log = log or get_event_log()
        self.default_timeout = default_timeout
        self.history: list[VerificationReport] = []

    async def verify(
        self,
        subject: str,
        checks: list[VerificationCheck],
        *,
        contract: str = "all_required",
        context: dict[str, Any] | None = None,
        mission_id: str = "",
        trace_id: str = "",
        actor: str = "model:main",
    ) -> VerificationReport:
        """Run the checks in order; a failing check does not stop the others (evidence is complete)."""
        report = VerificationReport(subject=subject, contract=contract, started_at=iso())
        shared = dict(context or {})
        for check in checks:
            result = await check.run(shared)
            report.results.append(result)
            self.log.append(
                EventKind.VERIFICATION,
                {"subject": subject, "check": result.name, "status": result.status.value, "ok": result.ok,
                 "detail": result.detail[:300], "evidence": _bound(result.evidence)},
                actor=actor, source="verification.engine", mission_id=mission_id, trace_id=trace_id,
                risk=RiskLevel.LOW if result.ok else RiskLevel.MEDIUM,
            )
            if result.ok and isinstance(result.evidence, dict):
                shared.update({key: value for key, value in result.evidence.items()
                               if key.startswith("output_")})
            if result.ok:
                # Later checks (e.g. "every success criterion is backed by a passing check") read this.
                # A check may also stand for the plan-level name it was built from.
                names = [result.name]
                alias = (shared.get("criteria_aliases") or {}).get(result.name)
                if alias and alias not in names:
                    names.append(alias)
                shared.setdefault("verified_criteria", []).extend(names)
        report.finished_at = iso()
        self.history.append(report)
        self.history[:] = self.history[-200:]
        self.log.append(
            EventKind.VERIFICATION,
            {"subject": subject, "summary": report.summary, "passed": report.passed,
             "unproven": [result.name for result in report.unproven], "contract": contract},
            actor=actor, source="verification.engine", mission_id=mission_id, trace_id=trace_id,
            risk=RiskLevel.LOW if report.passed else RiskLevel.HIGH,
        )
        return report

    @staticmethod
    def require(report: VerificationReport) -> VerificationReport:
        """Raise unless the report passed. Call this before claiming anything is done."""
        if not report.passed:
            raise VerificationFailed(
                f"verification failed for {report.subject!r}: {report.summary}",
                subject=report.subject, unproven=[result.name for result in report.unproven],
            )
        return report

    @staticmethod
    async def verify_claim(subject: str, claim: Any, *, log: EventLog | None = None,
                           check: VerificationCheck | None = None) -> VerificationReport:
        """Verify a single claim against a concrete check."""
        engine = VerificationEngine(log=log)
        return await engine.verify(subject, [check] if check else [])


def _bound(evidence: Any, limit: int = 4000) -> Any:
    """Keep evidence small enough for the audit log."""
    import json

    try:
        text = json.dumps(evidence, default=str)
    except (TypeError, ValueError):
        return str(evidence)[:limit]
    if len(text) <= limit:
        return evidence
    return {"truncated": True, "preview": text[:limit]}


_ENGINE: VerificationEngine | None = None
_LOCK = threading.Lock()


def get_verification_engine(**kwargs: Any) -> VerificationEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = VerificationEngine(**kwargs)
        return _ENGINE


def reset_verification_engine() -> None:
    global _ENGINE
    with _LOCK:
        _ENGINE = None
