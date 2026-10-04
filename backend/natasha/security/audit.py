"""Audit trail - a thin, opinionated facade over the event log.

Anything the owner may later need to reconstruct ("who did what, on whose authority, and was it
allowed?") goes through here so that the shape stays consistent and secrets stay out.
"""

from __future__ import annotations

from typing import Any

from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from .policy import Capability, Decision


class AuditTrail:
    def __init__(self, log: EventLog | None = None) -> None:
        self.log = log or get_event_log()

    def record(
        self,
        action: str,
        *,
        actor: str,
        outcome: str,
        capability: str = "",
        resource: str = "",
        risk: RiskLevel | str = RiskLevel.LOW,
        trace_id: str = "",
        mission_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> Any:
        return self.log.append(
            EventKind.SECURITY,
            {
                "action": action,
                "outcome": outcome,
                "capability": capability,
                "resource": resource,
                "details": details or {},
            },
            actor=actor,
            source="security.audit",
            trace_id=trace_id,
            mission_id=mission_id,
            risk=RiskLevel.parse(risk),
        )

    def policy_decision(self, decision: Decision, *, actor: str, mission_id: str = "", trace_id: str = "") -> Any:
        return self.record(
            action=f"policy.{decision.effect.value}",
            actor=actor,
            outcome=decision.effect.value,
            capability=decision.capability.value,
            resource=decision.resource,
            risk=decision.risk,
            mission_id=mission_id,
            trace_id=trace_id,
            details={"reason": decision.reason, "obligations": decision.obligations},
        )

    def capability_granted(self, capability: Capability | str, *, actor: str, scope: str = "", expires_at: str = "") -> Any:
        return self.record(
            action="capability.granted",
            actor=actor,
            outcome="granted",
            capability=Capability.parse(capability).value,
            resource=scope,
            risk=RiskLevel.MEDIUM,
            details={"expires_at": expires_at},
        )

    def denied(self, capability: Capability | str, *, actor: str, reason: str, resource: str = "") -> Any:
        return self.record(
            action="capability.denied",
            actor=actor,
            outcome="denied",
            capability=Capability.parse(capability).value,
            resource=resource,
            risk=RiskLevel.MEDIUM,
            details={"reason": reason},
        )

    def integrity(self, name: str, ok: bool, details: dict[str, Any] | None = None) -> Any:
        return self.record(
            action=f"integrity.{name}",
            actor="system",
            outcome="ok" if ok else "failed",
            risk=RiskLevel.LOW if ok else RiskLevel.CRITICAL,
            details=details or {},
        )

    def recent(self, limit: int = 100, *, min_risk: RiskLevel | None = None) -> list[dict[str, Any]]:
        events = self.log.query(
            kinds=[EventKind.SECURITY, EventKind.POLICY, EventKind.APPROVAL, EventKind.CREDENTIAL],
            limit=limit,
            min_risk=min_risk,
        )
        return [event.to_dict() for event in events]
