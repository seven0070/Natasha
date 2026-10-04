"""Approval data model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from ..core import canonical_json, new_id, sha256_text
from ..core.clock import iso, parse_iso, utcnow
from ..core.risk import RiskLevel

DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 86_400


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {ApprovalStatus.APPROVED, ApprovalStatus.DENIED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED}


def operation_fingerprint(operation: str, arguments: dict[str, Any] | None = None, *, resource: str = "") -> str:
    """Hash the *exact* operation so an approval cannot be widened or replayed for another call."""
    return sha256_text(canonical_json({"operation": operation, "arguments": arguments or {}, "resource": resource}))


@dataclass
class ApprovalRequest:
    """What is being asked, why, and how dangerous it is."""

    operation: str
    reason: str
    risk: RiskLevel
    actor: str
    permissions: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    arguments: dict[str, Any] = field(default_factory=dict)
    reversibility: str = "reversible"  # reversible | partially_reversible | irreversible
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    mission_id: str = ""
    step_id: str = ""
    trace_id: str = ""
    id: str = field(default_factory=lambda: new_id("apr"))
    created_at: str = field(default_factory=iso)
    expires_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ttl_seconds = max(30, min(int(self.ttl_seconds), MAX_TTL_SECONDS))
        if not self.expires_at:
            self.expires_at = iso(utcnow() + timedelta(seconds=self.ttl_seconds))
        self.risk = RiskLevel.parse(self.risk)

    @property
    def fingerprint(self) -> str:
        return operation_fingerprint(self.operation, self.arguments, resource=self.resources[0] if self.resources else "")

    def matches(self, operation: str, arguments: dict[str, Any] | None = None, *, resource: str = "") -> bool:
        """True only for the exact operation/arguments/resource this request was raised for."""
        return self.fingerprint == operation_fingerprint(operation, arguments, resource=resource)

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= parse_iso(self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "operation": self.operation,
            "reason": self.reason,
            "risk": self.risk.name,
            "actor": self.actor,
            "permissions": self.permissions,
            "resources": self.resources,
            "arguments": self.arguments,
            "reversibility": self.reversibility,
            "ttl_seconds": self.ttl_seconds,
            "mission_id": self.mission_id,
            "step_id": self.step_id,
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "fingerprint": self.fingerprint,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        payload = dict(data)
        payload.pop("fingerprint", None)
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})


@dataclass
class ApprovalToken:
    """A signed, single-use grant for one exact operation."""

    request_id: str
    fingerprint: str
    granted_by: str
    granted_at: str
    expires_at: str
    scope: list[str] = field(default_factory=list)
    signature: str = ""
    id: str = field(default_factory=lambda: new_id("tok"))
    used_at: str = ""

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "scope": sorted(self.scope),
        }

    def to_dict(self, *, include_signature: bool = True) -> dict[str, Any]:
        data = self.payload | {"used_at": self.used_at}
        if include_signature:
            data["signature"] = self.signature
        return data

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return (now or utcnow()) >= parse_iso(self.expires_at)

    def matches(self, operation: str, arguments: dict[str, Any] | None = None, *, resource: str = "") -> bool:
        return self.fingerprint == operation_fingerprint(operation, arguments, resource=resource)


@dataclass
class Approval:
    """Decision record for a request."""

    request_id: str
    status: ApprovalStatus
    decided_by: str = ""
    decided_at: str = ""
    note: str = ""
    token: ApprovalToken | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status.value,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "note": self.note,
            "reason": self.reason,
            "token": self.token.to_dict() if self.token else None,
        }


def utcnow_iso() -> str:
    return iso(utcnow())


def _tz() -> timezone:  # pragma: no cover - convenience
    return timezone.utc
