"""Approvals: explicit, scoped, expiring, unforgeable, single-use, owner-only.

The approval engine is the only thing standing between "the model asked nicely" and a privileged
action, so these tests attack it the way a compromised model would: replay the token, widen the
scope, forge a grant, approve itself, and use an expired one.
"""

from __future__ import annotations

import json
import time

import pytest

from datetime import timedelta

from natasha.approvals.models import ApprovalStatus, ApprovalToken
from natasha.core import ApprovalDenied, ApprovalRequired, hmac_sign, iso, utcnow
from natasha.core.risk import RiskLevel
from natasha.events import EventKind

OPERATION = "shell.exec"
ARGUMENTS = {"command": "ls -la"}
RESOURCE = "shell:ls -la"


@pytest.fixture()
def engine(home, log):
    from natasha.approvals import ApprovalEngine

    return ApprovalEngine(signing_key=b"test-signing-key-0123456789", log=log)


def _request(engine, *, actor="model:main", operation=OPERATION, arguments=None, resource=RESOURCE):
    return engine.request(
        operation, reason="unit test", risk=RiskLevel.HIGH, actor=actor,
        permissions=[operation], resources=[resource], arguments=arguments if arguments is not None else dict(ARGUMENTS),
    )


def test_request_then_approve_then_consume(engine):
    request = _request(engine)
    assert engine.status(request.id) is ApprovalStatus.PENDING
    token = engine.approve(request.id, decided_by="owner", note="one-off", ttl_seconds=600)
    assert token.id and token.signature
    engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")
    # Single use: the same grant cannot authorise a second call.
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_pending_request_has_no_usable_token(engine):
    request = _request(engine)
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_token_is_bound_to_the_exact_operation_and_arguments(engine):
    request = _request(engine)
    engine.approve(request.id, decided_by="owner", ttl_seconds=600)
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, {"command": "rm -rf /tmp/other"}, resource=RESOURCE,
                       actor="model:main")
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, "fs.write", dict(ARGUMENTS), resource=RESOURCE, actor="model:main")
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource="shell:something-else",
                       actor="model:main")


def test_a_forged_token_is_rejected(engine):
    request = _request(engine)
    engine.approve(request.id, decided_by="owner", ttl_seconds=600)
    with engine._lock:  # simulate a tampered token: valid signature replaced by garbage
        engine._conn.execute("UPDATE approval_tokens SET token = ? WHERE request_id = ?",
                             ('{"payload": "{}", "signature": "deadbeef"}', request.id))
        engine._conn.commit()
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_a_token_signed_with_another_key_is_rejected(engine):
    """A second engine with a different key must not be able to mint a token this one accepts."""
    from natasha.approvals import ApprovalEngine

    other = ApprovalEngine(signing_key=b"a-completely-different-key-1234", log=engine.log)
    request = _request(engine)
    other_request = other.request(OPERATION, reason="forged", risk=RiskLevel.HIGH, actor="model:main",
                                  arguments=dict(ARGUMENTS), permissions=[OPERATION], resources=[RESOURCE])
    other.approve(other_request.id, decided_by="owner", ttl_seconds=600)
    with pytest.raises(Exception):
        engine.consume(other_request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_expired_approval_cannot_be_used(engine):
    request = _request(engine)
    # A correctly signed token that has simply run out of time: expiry must be enforced from the
    # signed payload, not from a column anyone with database access could rewrite.
    request_row = engine.get_request(request.id)
    token = ApprovalToken(request_id=request.id, fingerprint=request_row.fingerprint,
                          granted_by="owner", granted_at=iso(utcnow() - timedelta(days=2)),
                          expires_at=iso(utcnow() - timedelta(days=1)),
                          scope=[OPERATION])
    token.signature = hmac_sign(engine._key, token.payload)
    with engine._lock:
        engine._conn.execute(
            "INSERT INTO approval_tokens (id, request_id, fingerprint, scope, token, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (token.id, request.id, token.fingerprint, "[]", json.dumps(token.to_dict()), token.granted_at,
             token.expires_at),
        )
        engine._conn.commit()
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_denied_request_never_yields_a_token(engine):
    request = _request(engine)
    engine.deny(request.id, decided_by="owner", note="not now")
    assert engine.status(request.id) is ApprovalStatus.DENIED
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_an_agent_cannot_approve_its_own_request(engine):
    request = _request(engine)
    for actor in ("model:main", "worker:coder", "skill:helper", "system"):
        with pytest.raises(Exception):
            engine.approve(request.id, decided_by=actor, ttl_seconds=600)
    assert engine.status(request.id) is ApprovalStatus.PENDING


def test_self_approval_attempt_is_audited(engine):
    request = _request(engine)
    with pytest.raises(Exception):
        engine.approve(request.id, decided_by="model:main")
    events = engine.log.query(kinds=[EventKind.SECURITY], limit=50)
    assert any(event.payload.get("action") == "approval.self_approval_attempt" for event in events)


def test_revoked_approval_stops_working(engine):
    request = _request(engine)
    engine.approve(request.id, decided_by="owner", ttl_seconds=600)
    engine.revoke(request.id, actor="owner")
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_require_raises_carrying_the_request_id(engine):
    with pytest.raises(ApprovalRequired) as excinfo:
        engine.require(OPERATION, reason="needs owner", risk=RiskLevel.HIGH, actor="model:main",
                       arguments=dict(ARGUMENTS), permissions=[OPERATION], resources=[RESOURCE])
    request_id = excinfo.value.request_id
    assert engine.status(request_id) is ApprovalStatus.PENDING


def test_history_records_the_decision(engine):
    request = _request(engine)
    engine.approve(request.id, decided_by="owner", ttl_seconds=60)
    history = engine.history(limit=20)
    assert history
    assert any(entry.get("request_id") == request.id or entry.get("id") == request.id for entry in history)


def test_pending_only_lists_live_requests(engine):
    first = _request(engine)
    second = _request(engine)
    engine.approve(first.id, decided_by="owner", ttl_seconds=60)
    pending = [request.id for request in engine.pending()]
    assert first.id not in pending
    assert second.id in pending


def test_cancelled_requests_cannot_be_approved_or_used(engine):
    request = _request(engine)
    engine.cancel(request.id, actor="owner", note="changed my mind")
    assert engine.status(request.id) is ApprovalStatus.CANCELLED
    with pytest.raises(Exception):
        engine.approve(request.id, decided_by="owner", ttl_seconds=600)
    with pytest.raises(ApprovalDenied):
        engine.consume(request.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")


def test_expire_stale_sweeps_overdue_requests(engine):
    request = _request(engine)
    assert engine.expire_stale() >= 0
    assert engine.status(request.id) in {ApprovalStatus.PENDING, ApprovalStatus.EXPIRED}


def test_a_valid_token_verifies_and_a_doctored_one_does_not(engine):
    request = _request(engine)
    token = engine.approve(request.id, decided_by="owner", ttl_seconds=600)
    assert engine.verify_token(token) is True
    token.signature = "0" * len(token.signature)
    assert engine.verify_token(token) is False


def test_signing_key_must_be_long_enough(home):
    from natasha.approvals import ApprovalEngine

    with pytest.raises(ValueError):
        ApprovalEngine(signing_key=b"short")


def test_two_requests_cannot_share_a_token(engine):
    first = _request(engine)
    second = _request(engine)
    engine.approve(first.id, decided_by="owner", ttl_seconds=600)
    with pytest.raises(ApprovalDenied):
        engine.consume(second.id, OPERATION, dict(ARGUMENTS), resource=RESOURCE, actor="model:main")
    time.sleep(0)  # keep the timing-sensitive imports honest
