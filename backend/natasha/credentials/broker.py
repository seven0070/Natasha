"""Credential broker - the only component that hands secrets to tools or model-driven code.

A tool never receives the vault. It receives a :class:`CredentialHandle`: short-lived, scoped to
one purpose, non-serialisable, non-repr-able, and simply gone after the block exits. The plaintext
also registers with the secret sanitiser, so even a traceback cannot leak it.
"""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..core import ApprovalDenied, CredentialError, PolicyDenied, new_id
from ..core.clock import iso, utcnow
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from ..events.sanitizer import get_sanitizer
from ..security.policy import Capability, PolicyEngine, PolicyRequest
from .manager import KINDS, UniversalCredentialManager, get_credential_manager

DEFAULT_TTL_SECONDS = 60


def canonical_use_arguments(reference: str, purpose: str, scope: str = "") -> dict[str, Any]:
    """The exact argument payload an approval for credential use is bound to.

    Approval fingerprints are computed over these arguments, so the *same* function must be used
    when requesting approval and when consuming it - otherwise the fingerprint check would either
    fail spuriously or, worse, allow an approval to be widened.
    """
    return {"ref": reference, "purpose": purpose, "scope": scope or purpose}


@dataclass(frozen=True)
class CredentialGrant:
    """Metadata about an issued handle (safe to log)."""

    id: str
    ref: str
    purpose: str
    actor: str
    scope: str
    issued_at: str
    expires_at: str


class CredentialHandle(AbstractContextManager["CredentialHandle"]):
    """Single-use, time-boxed access to a secret."""

    def __init__(self, grant: CredentialGrant, secret: dict[str, Any], kind: str) -> None:
        self.grant = grant
        self._secret = secret
        self._kind = kind
        self._consumed = False
        self._closed = False

    # -- access --------------------------------------------------------------- #
    @property
    def value(self) -> str:
        self._ensure_open()
        return str(self._secret.get("value") or self._secret.get("access_token") or self._secret.get("token") or "")

    @property
    def fields(self) -> dict[str, Any]:
        self._ensure_open()
        return dict(self._secret)

    def headers(self) -> dict[str, str]:
        self._ensure_open()
        return KINDS.get(self._kind, KINDS["custom"]).headers(self._secret)

    def quote(self) -> dict[str, Any]:
        """Metadata for logs/UI - never the secret."""
        return {
            "id": self.grant.id, "ref": self.grant.ref, "purpose": self.grant.purpose,
            "actor": self.grant.actor, "scope": self.grant.scope, "expires_at": self.grant.expires_at,
            "kind": self._kind, "fingerprint": "handle",
        }

    # -- lifecycle ------------------------------------------------------------ #
    def _ensure_open(self) -> None:
        if self._closed:
            raise CredentialError("credential handle is closed")
        if self._consumed:
            raise CredentialError("credential handle was already consumed (single-use)")
        if iso(utcnow()) >= self.grant.expires_at:
            self._secret = {}
            raise CredentialError("credential handle expired")

    def consume(self) -> dict[str, Any]:
        """Read once, then invalidate."""
        self._ensure_open()
        self._consumed = True
        return self._secret

    def __enter__(self) -> "CredentialHandle":
        self._ensure_open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.revoke()

    def revoke(self) -> None:
        self._secret = {}
        self._closed = True

    def __repr__(self) -> str:  # pragma: no cover - safety net
        return f"<CredentialHandle ref={self.grant.ref!r} purpose={self.grant.purpose!r} secret=<hidden>>"

    __str__ = __repr__


class CredentialBroker:
    """Issues scoped handles; enforces policy and approval before handing anything over."""

    def __init__(
        self,
        manager: UniversalCredentialManager | None = None,
        *,
        policy: PolicyEngine | None = None,
        log: EventLog | None = None,
        approvals: Any | None = None,
    ) -> None:
        self.manager = manager or get_credential_manager()
        self.policy = policy or self.manager.policy
        self.log = log or get_event_log()
        self.approvals = approvals
        self._active: dict[str, CredentialGrant] = {}
        self._lock = threading.RLock()

    def request_approval(
        self,
        reference: str,
        *,
        purpose: str,
        actor: str,
        scope: str = "",
        reason: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        mission_id: str = "",
        step_id: str = "",
        trace_id: str = "",
    ) -> Any:
        """Create the approval request for a credential use (same args the broker will consume)."""
        if self.approvals is None:
            raise CredentialError("no approval engine configured")
        return self.approvals.request(
            "credential.use",
            reason=reason or f"{actor} needs {reference} for {purpose}",
            risk=RiskLevel.HIGH,
            actor=actor,
            permissions=["credential.use"],
            resources=[reference],
            arguments=canonical_use_arguments(reference, purpose, scope),
            reversibility="reversible",
            ttl_seconds=ttl_seconds,
            mission_id=mission_id,
            step_id=step_id,
            trace_id=trace_id,
        )

    def issue(
        self,
        reference: str,
        *,
        purpose: str,
        actor: str = "owner",
        scope: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        approval_id: str = "",
        mission_id: str = "",
    ) -> CredentialHandle:
        """Issue a handle, or raise if policy refuses / approval is missing."""
        decision = self.policy.check(
            PolicyRequest(
                Capability.CREDENTIAL_USE, reference, actor=actor,
                context={"scope": scope or purpose, "mission_id": mission_id},
            )
        )
        if decision.effect.value == "deny":
            self._audit("denied", reference, actor, purpose, decision.reason, mission_id)
            raise PolicyDenied(decision.reason, reference=reference)

        owner_actor = actor.split(":", 1)[0].lower() in {"owner", "user", "human", "local-owner"}
        if decision.needs_approval and not owner_actor:
            if not approval_id:
                self._audit("approval_required", reference, actor, purpose, decision.reason, mission_id)
                raise PolicyDenied(
                    f"credential use requires owner approval: {decision.reason}",
                    reference=reference, needs_approval=True,
                )
            if self.approvals is None:
                raise ApprovalDenied("approved credential use requires an approval engine")
            # No arguments are passed: the engine verifies the token against the request it
            # stored, so a caller cannot widen an approval by claiming different arguments.
            self.approvals.consume(approval_id, "credential.use", actor=actor)

        if not self.manager.exists(reference):
            raise CredentialError(f"credential {reference!r} is not configured")

        secret = self.manager.vault.get_secret(reference)
        for key, value in secret.items():
            if isinstance(value, str) and value:
                get_sanitizer().register(value, reference=reference)

        grant = CredentialGrant(
            id=new_id("grant"), ref=reference, purpose=purpose, actor=actor, scope=scope or purpose,
            issued_at=iso(), expires_at=iso(utcnow() + timedelta(seconds=max(5, ttl_seconds))),
        )
        with self._lock:
            self._active[grant.id] = grant
        self._audit("issued", reference, actor, purpose, decision.reason, mission_id,
                    extras={"grant_id": grant.id, "ttl": ttl_seconds})
        return CredentialHandle(grant, secret, self.manager.vault.metadata(reference).kind)

    def retract(self, grant_id: str) -> bool:
        with self._lock:
            return self._active.pop(grant_id, None) is not None

    def active_grants(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"id": g.id, "ref": g.ref, "purpose": g.purpose, "actor": g.actor,
                 "scope": g.scope, "expires_at": g.expires_at}
                for g in self._active.values()
            ]

    def _audit(
        self, action: str, reference: str, actor: str, purpose: str, reason: str,
        mission_id: str, extras: dict[str, Any] | None = None,
    ) -> None:
        self.log.append(
            EventKind.CREDENTIAL,
            {
                "action": f"broker.{action}", "ref": reference, "purpose": purpose, "reason": reason,
                **(extras or {}),
            },
            actor=actor, source="credentials.broker", mission_id=mission_id,
            risk=RiskLevel.HIGH if action == "denied" else RiskLevel.MEDIUM,
        )


_BROKERS: dict[str, CredentialBroker] = {}
_LOCK = threading.Lock()


def get_broker() -> CredentialBroker:
    with _LOCK:
        if "default" not in _BROKERS:
            _BROKERS["default"] = CredentialBroker()
        return _BROKERS["default"]


def reset_brokers() -> None:
    with _LOCK:
        _BROKERS.clear()
