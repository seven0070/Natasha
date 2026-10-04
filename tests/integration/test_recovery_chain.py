"""Recovery: bounded repair that never pretends, never loops and never replays the dangerous.

The interesting cases are not "retry until it works". They are: a governance or security failure must
never be auto-repaired, a permission failure must escalate to the owner, an irreversible action must
not be replayed, an identical failure repeating must stop the loop instead of burning the budget, and
an exhausted repair must surface as a failure with the original error attached.
"""

from __future__ import annotations

import pytest

from natasha.core import ConflictError, RecoveryExhausted, VerificationFailed
from natasha.events import EventKind
from natasha.recovery import (
    FailureClass,
    RecoveryEngine,
    RepairAction,
    classify,
    get_recovery_engine,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def engine(log):
    return RecoveryEngine(log=log, max_attempts=3, base_delay=0.0, max_delay=0.0)


async def _noop(_seconds: float = 0.0) -> None:
    """Sleep stand-in: repairs are tested without paying for real backoff delays."""


async def test_a_transient_failure_is_retried_and_recovered(engine):
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connection refused by the provider")
        return "payload"

    outcome = await engine.attempt("tool:http_fetch", flaky, sleep=_noop)
    assert outcome.recovered is True
    assert outcome.attempts == 2
    assert outcome.result == "payload"
    assert engine.require(outcome) == "payload"


async def test_terminal_failures_are_classified_and_refuse_automated_repair():
    governance = classify("GovernanceViolation: protected area 'security' was modified")
    assert governance.class_ is FailureClass.GOVERNANCE
    assert governance.repairable is False
    assert governance.needs_owner is True
    assert governance.retryable is False

    security = classify("PromptInjectionDetected: exfiltration pattern in tool output")
    assert security.class_ is FailureClass.SECURITY
    assert security.repairable is False

    permission = classify("AccessDenied: policy denied credential.use")
    assert permission.class_ is FailureClass.PERMISSION
    assert permission.needs_owner is True

    approval = classify("ApprovalRequired: shell.exec requires owner approval")
    assert approval.class_ is FailureClass.APPROVAL_REQUIRED


async def test_the_plan_never_auto_repairs_governance_or_security(engine):
    plan = engine.plan(classify("GovernanceViolation: constitution rule violated"))
    assert plan.action is RepairAction.ABORT
    assert "never auto-repaired" in plan.reason

    plan = engine.plan(classify("SecurityViolation: credential leak detected"))
    assert plan.action is RepairAction.ABORT


async def test_permission_and_approval_failures_escalate_to_the_owner(engine):
    plan = engine.plan(classify("AccessDenied: policy denied fs.write_outside_workspace"))
    assert plan.action is RepairAction.ESCALATE
    plan = engine.plan(classify("ApprovalRequired: computer control needs the owner"))
    assert plan.action is RepairAction.ESCALATE


async def test_rate_limits_back_off_and_provider_outages_reroute(engine):
    plan = engine.plan(classify("RateLimitError: 429 too many requests", attempt=1))
    assert plan.action is RepairAction.RETRY_WITH_BACKOFF
    assert plan.delay_seconds >= 0

    plan = engine.plan(classify("ProviderUnavailable: 503 provider error"), alternative_available=True)
    assert plan.action is RepairAction.REROUTE

    plan = engine.plan(classify("ProviderUnavailable: 503 provider error"), alternative_available=False)
    assert plan.action is RepairAction.RETRY_WITH_BACKOFF


async def test_a_timeout_narrows_the_request_instead_of_repeating_it(engine):
    plan = engine.plan(classify(TimeoutError("call timed out after 30s")))
    assert plan.action is RepairAction.REDUCE_SCOPE
    assert "narrow" in plan.reason


async def test_verification_failure_on_an_irreversible_action_escalates(engine):
    failure = classify("VerificationFailed: artifact did not pass the checksum check", reversible=False)
    plan = engine.plan(failure)
    assert plan.action is RepairAction.ESCALATE
    assert "irreversible" in plan.reason


async def test_an_exhausted_budget_with_compensation_compensates(engine):
    failure = classify("TransientError: temporarily unavailable", attempt=3, max_attempts=3,
                       reversible=False)
    assert failure.exhausted is True
    plan = engine.plan(failure, supports_compensation=True)
    assert plan.action is RepairAction.COMPENSATE
    assert "compensat" in plan.reason.lower()


async def test_an_alternative_provider_is_used_after_a_reroute(engine):
    async def broken():
        raise ConnectionError("connection refused: provider error 503")

    async def spare():
        return "served by the spare provider"

    outcome = await engine.attempt("brain:complete", broken, alternative=spare,
                                   sleep=_noop)
    assert outcome.recovered is True
    assert outcome.result == "served by the spare provider"
    assert outcome.action is RepairAction.REROUTE


async def test_a_replan_can_replace_the_operation(engine):
    attempts: list[int] = []

    async def bad_arguments():
        attempts.append(1)
        raise ValueError("invalid argument: 'limit' must be an integer")

    async def replan(_failure):
        async def fixed():
            return "replanned successfully"

        return fixed

    outcome = await engine.attempt("tool:query", bad_arguments, replan=replan,
                                   sleep=_noop)
    assert outcome.recovered is True
    assert outcome.result == "replanned successfully"
    assert outcome.history and outcome.history[0]["failure_class"] == FailureClass.INVALID_INPUT.value


async def test_the_same_failure_twice_stops_instead_of_looping(engine):
    calls = {"n": 0}

    async def always_same():
        calls["n"] += 1
        raise ConnectionError("connection refused: provider error 503")

    outcome = await engine.attempt("tool:http_fetch", always_same, sleep=_noop)
    assert outcome.recovered is False
    assert outcome.escalated is True
    assert calls["n"] == 2                      # not max_attempts: the loop was cut short
    assert "identical failure repeated" in outcome.error
    with pytest.raises(RecoveryExhausted):
        engine.require(outcome)


async def test_exhausting_the_budget_reports_the_original_error(engine):
    async def flaky_but_never_well():
        raise TimeoutError(f"call timed out after {40}ms")

    outcome = await engine.attempt("tool:slow", flaky_but_never_well, sleep=_noop)
    assert outcome.recovered is False
    assert "timed out" in outcome.error
    assert outcome.history
    assert all("attempt" in entry for entry in outcome.history)


async def test_every_repair_attempt_is_audited(engine, log):
    async def flaky():
        if not hasattr(flaky, "done"):
            flaky.done = True
            raise ConnectionError("connection refused: transient")
        return "ok"

    await engine.attempt("tool:audited", flaky, mission_id="mis_recovery", sleep=_noop)
    kinds = {event.kind for event in log.query(limit=100)}
    assert EventKind.FAILURE in kinds or EventKind.REPAIR in kinds
    repairs = [event for event in log.query(kinds=[EventKind.REPAIR], limit=50)]
    assert repairs
    assert any(event.mission_id == "mis_recovery" for event in repairs)


async def test_an_irreversible_action_is_not_replayed_by_a_retry(engine):
    """A retry must not repeat a side effect that already landed - only the safe path may repeat."""
    executed: list[str] = []

    async def irreversible():
        executed.append("charged the customer")
        raise VerificationFailed("verification failed: the ledger check did not pass")

    outcome = await engine.attempt("tool:charge", irreversible,
                                   classify_kwargs={"reversible": False}, sleep=_noop)
    assert outcome.recovered is False
    assert outcome.escalated is True
    # One attempt only: a side effect that already landed is never replayed by a retry.
    assert executed == ["charged the customer"]
    assert "ledger" in outcome.error


async def test_fingerprints_group_the_same_failure_and_separate_different_ones():
    first = classify(TimeoutError("call timed out after 30s"), operation="tool:http_fetch")
    second = classify(TimeoutError("call timed out after 99s"), operation="tool:http_fetch")
    other = classify(TimeoutError("call timed out after 30s"), operation="tool:fs_read")
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != other.fingerprint
    assert len(first.fingerprint) == 16


async def test_backoff_grows_and_is_capped(log):
    engine = RecoveryEngine(log=log, max_attempts=5, base_delay=1.0, max_delay=4.0)
    assert engine.backoff(1) == 1.0
    assert engine.backoff(2) == 2.0
    assert engine.backoff(3) == 4.0
    assert engine.backoff(9) == 4.0                 # capped, never unbounded


async def test_the_engine_singleton_is_resettable(home):
    from natasha.recovery import get_recovery_engine, reset_recovery_engine

    first = get_recovery_engine()
    reset_recovery_engine()
    assert get_recovery_engine() is not first


async def test_a_failure_record_explains_itself():
    record = classify("not found: /tmp/nothing.txt", operation="fs_read", attempt=2)
    payload = record.to_dict()
    assert payload["class"] == FailureClass.NOT_FOUND.value
    assert payload["attempt"] == 2
    assert payload["retryable"] is False          # repeating the same lookup cannot help
    assert payload["needs_owner"] is False        # it can be replanned without the owner

    exhausted = classify("not found: /tmp/nothing.txt", operation="fs_read", attempt=3, max_attempts=3)
    assert exhausted.needs_owner is True          # ... but an exhausted budget goes to the owner
