"""The recovery engine: decides and performs bounded repair."""

from __future__ import annotations

import asyncio
import enum
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core import RecoveryExhausted
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from .classifier import FailureClass, FailureRecord, classify


class RepairAction(str, enum.Enum):
    """What the engine may do about a failure, cheapest and safest first."""

    RETRY = "retry"                    # same call again (transient only)
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    REROUTE = "reroute"                # different provider/model/tool
    REDUCE_SCOPE = "reduce_scope"      # smaller request (fewer tokens/files/rows)
    REFRESH_STATE = "refresh_state"    # re-read the world before deciding again
    REPLAN = "replan"                  # new plan for the same objective
    COMPENSATE = "compensate"          # undo reversible side effects, then stop
    ESCALATE = "escalate"              # ask the owner
    ABORT = "abort"                    # give up and report honestly


@dataclass
class RecoveryPlan:
    """What the engine decided to do, and why."""

    failure: FailureRecord
    action: RepairAction
    reason: str
    delay_seconds: float = 0.0
    max_attempts: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {"failure": self.failure.to_dict(), "action": self.action.value, "reason": self.reason,
                "delay_seconds": self.delay_seconds, "max_attempts": self.max_attempts}


@dataclass
class RecoveryOutcome:
    """The result of a repair attempt."""

    recovered: bool
    attempts: int
    action: RepairAction
    result: Any = None
    error: str = ""
    escalated: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"recovered": self.recovered, "attempts": self.attempts, "action": self.action.value,
                "error": self.error[:500], "escalated": self.escalated, "history": self.history}


class RecoveryEngine:
    """Bounded repair with an audit trail."""

    def __init__(self, *, log: EventLog | None = None, max_attempts: int = 3,
                 base_delay: float = 0.5, max_delay: float = 8.0) -> None:
        self.log = log or get_event_log()
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ policy
    def plan(self, failure: FailureRecord, *, alternative_available: bool = False,
             supports_compensation: bool = False, supports_replan: bool = True) -> RecoveryPlan:
        """Choose the least invasive action that could actually help."""
        attempt = failure.attempt
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))

        if failure.class_ in (FailureClass.GOVERNANCE, FailureClass.SECURITY):
            return RecoveryPlan(failure, RepairAction.ABORT,
                                f"{failure.class_.value} failures are never auto-repaired; "
                                "the owner must review them", max_attempts=self.max_attempts)
        if failure.class_ in (FailureClass.PERMISSION, FailureClass.APPROVAL_REQUIRED):
            return RecoveryPlan(failure, RepairAction.ESCALATE,
                                "permission and approval failures require the owner", max_attempts=self.max_attempts)
        if failure.class_ is FailureClass.INVALID_INPUT:
            if supports_replan:
                return RecoveryPlan(failure, RepairAction.REPLAN,
                                    "the arguments were invalid; the call must change, not repeat",
                                    max_attempts=self.max_attempts)
            return RecoveryPlan(failure, RepairAction.ESCALATE,
                                "invalid input cannot be retried unchanged", max_attempts=self.max_attempts)
        if failure.exhausted:
            if supports_compensation and not failure.reversible:
                return RecoveryPlan(failure, RepairAction.COMPENSATE,
                                    "budget exhausted after an irreversible action; compensating",
                                    max_attempts=self.max_attempts)
            return RecoveryPlan(failure, RepairAction.ESCALATE,
                                f"attempt budget exhausted ({attempt}/{failure.max_attempts})",
                                max_attempts=self.max_attempts)

        if failure.class_ is FailureClass.RATE_LIMITED:
            return RecoveryPlan(failure, RepairAction.RETRY_WITH_BACKOFF,
                                "rate limited: back off before retrying", delay, self.max_attempts)
        if failure.class_ is FailureClass.PROVIDER_UNAVAILABLE and alternative_available:
            return RecoveryPlan(failure, RepairAction.REROUTE,
                                "provider unavailable: route to another provider", 0.0, self.max_attempts)
        if failure.class_ is FailureClass.TIMEOUT:
            return RecoveryPlan(failure, RepairAction.REDUCE_SCOPE,
                                "timeout: narrow the request before retrying", delay, self.max_attempts)
        if failure.class_ is FailureClass.RESOURCE:
            return RecoveryPlan(failure, RepairAction.REFRESH_STATE,
                                "resource pressure: refresh state and try a smaller step", delay,
                                self.max_attempts)
        if failure.class_ is FailureClass.CONFLICT:
            return RecoveryPlan(failure, RepairAction.REFRESH_STATE,
                                "state changed underneath the action; re-read then retry", delay,
                                self.max_attempts)
        if failure.class_ is FailureClass.NOT_FOUND:
            return RecoveryPlan(failure, RepairAction.REPLAN if supports_replan else RepairAction.ESCALATE,
                                "the target does not exist; the plan must change", 0.0, self.max_attempts)
        if failure.class_ is FailureClass.VERIFICATION:
            if failure.reversible and supports_replan:
                return RecoveryPlan(failure, RepairAction.REPLAN,
                                    "verification failed; a different approach is required",
                                    0.0, self.max_attempts)
            return RecoveryPlan(failure, RepairAction.ESCALATE,
                                "verification failed on an irreversible action; owner review required",
                                0.0, self.max_attempts)
        if failure.retryable:
            return RecoveryPlan(failure, RepairAction.RETRY_WITH_BACKOFF,
                                f"{failure.class_.value} failure: retry with backoff", delay,
                                self.max_attempts)
        return RecoveryPlan(failure, RepairAction.ESCALATE if failure.needs_owner else RepairAction.ABORT,
                            f"no automated repair is defined for {failure.class_.value}",
                            max_attempts=self.max_attempts)

    def backoff(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))

    # ------------------------------------------------------------------ execution
    async def attempt(
        self,
        operation: str,
        call: Callable[[], Awaitable[Any]],
        *,
        classify_kwargs: dict[str, Any] | None = None,
        on_failure: Callable[[FailureRecord, RecoveryPlan], Awaitable[None]] | None = None,
        alternative: Callable[[], Awaitable[Any]] | None = None,
        replan: Callable[[FailureRecord], Awaitable[Any]] | None = None,
        mission_id: str = "",
        trace_id: str = "",
        actor: str = "model:main",
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> RecoveryOutcome:
        """Run *call*, repairing failures within the budget. Never fabricates a success."""
        history: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        current = call
        sleeper = sleep or asyncio.sleep
        last_failure: FailureRecord | None = None
        last_plan = RecoveryPlan(FailureRecord("not started", operation=operation), RepairAction.RETRY, "")

        for attempt_number in range(1, self.max_attempts + 1):
            try:
                result = await current()
                if attempt_number > 1:
                    self._log("recovered", operation, {"attempts": attempt_number, "history": history},
                              mission_id, trace_id, actor, RiskLevel.LOW)
                return RecoveryOutcome(True, attempt_number, last_plan.action, result=result, history=history)
            except Exception as exc:
                failure = classify(exc, operation=operation, attempt=attempt_number,
                                   max_attempts=self.max_attempts, **(classify_kwargs or {}))
                last_failure = failure
                plan = self.plan(failure, alternative_available=alternative is not None,
                                 supports_compensation=False,
                                 supports_replan=replan is not None or current is call)
                last_plan = plan
                entry = {"attempt": attempt_number, "action": plan.action.value, "reason": plan.reason,
                         "error": failure.message[:300], "failure_class": failure.class_.value}
                history.append(entry)
                self._log("failure", operation, entry, mission_id, trace_id, actor,
                          RiskLevel.MEDIUM if failure.class_.repairable else RiskLevel.HIGH)
                if on_failure is not None:
                    await on_failure(failure, plan)

                if plan.action is RepairAction.ABORT:
                    return RecoveryOutcome(False, attempt_number, plan.action, error=failure.message,
                                           history=history)
                seen[failure.fingerprint] = seen.get(failure.fingerprint, 0) + 1
                if (
                    seen[failure.fingerprint] >= 2
                    and plan.action in (RepairAction.RETRY, RepairAction.RETRY_WITH_BACKOFF)
                    and alternative is None
                    and replan is None
                ):
                    # The exact same failure twice with nothing new to try: more attempts would only
                    # burn the budget, so stop and report instead of looping.
                    reason = ("the identical failure repeated and no alternative repair exists; "
                              "escalating instead of retrying")
                    history.append({"attempt": attempt_number, "action": "escalate", "reason": reason,
                                    "failure_class": failure.class_.value})
                    self._log("escalated", operation,
                              {**entry, "reason": reason, "fingerprint": failure.fingerprint},
                              mission_id, trace_id, actor, RiskLevel.HIGH)
                    return RecoveryOutcome(False, attempt_number, RepairAction.ESCALATE,
                                           error=f"{failure.message} ({reason})", escalated=True,
                                           history=history)
                if plan.action is RepairAction.ESCALATE:
                    self._log("escalated", operation, entry, mission_id, trace_id, actor, RiskLevel.HIGH)
                    return RecoveryOutcome(False, attempt_number, plan.action, error=failure.message,
                                           escalated=True, history=history)
                if plan.delay_seconds:
                    await sleeper(plan.delay_seconds)
                if plan.action is RepairAction.REROUTE and alternative is not None:
                    current = alternative
                    alternative = None  # one reroute, then fall through to other strategies
                elif plan.action is RepairAction.REPLAN and replan is not None:
                    try:
                        replanned = await replan(failure)
                        if callable(replanned):
                            current = replanned
                    except Exception as exc:
                        history.append({"attempt": attempt_number, "action": "replan", "error": str(exc)[:200]})
                        self._log("replan_failed", operation, {"error": str(exc)[:200]}, mission_id, trace_id,
                                  actor, RiskLevel.MEDIUM)
                        return RecoveryOutcome(False, attempt_number, plan.action,
                                               error=f"{failure.message}; replan failed: {exc}",
                                               history=history)
                self._log("repair", operation, {**entry, "delay": plan.delay_seconds}, mission_id, trace_id,
                          actor, RiskLevel.LOW)

        error = last_failure.message if last_failure else "unknown failure"
        self._log("exhausted", operation, {"attempts": self.max_attempts, "error": error[:300]},
                  mission_id, trace_id, actor, RiskLevel.HIGH)
        return RecoveryOutcome(False, self.max_attempts,
                               last_plan.action, error=error, escalated=True, history=history)

    def require(self, outcome: RecoveryOutcome) -> Any:
        """Raise :class:`RecoveryExhausted` unless the repair succeeded."""
        if not outcome.recovered:
            raise RecoveryExhausted(
                f"could not recover after {outcome.attempts} attempt(s): {outcome.error}",
                attempts=outcome.attempts, history=outcome.history[-3:],
            )
        return outcome.result

    def _log(self, action: str, operation: str, payload: dict[str, Any], mission_id: str,
             trace_id: str, actor: str, risk: RiskLevel) -> None:
        try:
            self.log.append(EventKind.REPAIR if action in ("repair", "recovered") else EventKind.FAILURE,
                            {"action": action, "operation": operation, **payload},
                            actor=actor, source="recovery.engine", mission_id=mission_id,
                            trace_id=trace_id, risk=risk)
        except Exception:
            pass


_ENGINE: RecoveryEngine | None = None
_LOCK = threading.Lock()


def get_recovery_engine(**kwargs: Any) -> RecoveryEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = RecoveryEngine(**kwargs)
        return _ENGINE


def reset_recovery_engine() -> None:
    global _ENGINE
    with _LOCK:
        _ENGINE = None
