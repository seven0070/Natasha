"""Event model.

Every observable thing Natasha does becomes an event. Events are immutable, ordered, and
chained: ``hash = sha256(prev_hash + canonical_json(core_fields))``. Rewriting history requires
rewriting every subsequent hash, and the log refuses UPDATE/DELETE at the database level.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core import chain_hash, iso, new_id
from ..core.risk import RiskLevel

GENESIS_HASH = "0" * 64
_HASHED_FIELDS = ("id", "ts", "trace_id", "actor", "source", "kind", "risk", "payload", "mission_id", "parent_id")


class EventKind(str, Enum):
    """Canonical event kinds (spec section 24)."""

    PERCEPTION = "perception"
    THOUGHT = "thought"
    GOAL = "goal"
    PLAN = "plan"
    TOOL_REQUEST = "tool_request"
    APPROVAL = "approval"
    TOOL_EXECUTION = "tool_execution"
    OBSERVATION = "observation"
    FAILURE = "failure"
    REPAIR = "repair"
    VERIFICATION = "verification"
    MEMORY = "memory"
    SKILL = "skill"
    PLUGIN = "plugin"
    UPGRADE = "upgrade"
    # additional runtime kinds used by the executive loop and subsystems
    MESSAGE = "message"
    MISSION = "mission"
    CREDENTIAL = "credential"
    SECURITY = "security"
    POLICY = "policy"
    MCP = "mcp"
    MARKETPLACE = "marketplace"
    CREATION = "creation"
    VOICE = "voice"
    HEALTH = "health"
    SYSTEM = "system"

    @classmethod
    def parse(cls, value: object) -> "EventKind":
        if isinstance(value, EventKind):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        return cls.SYSTEM


@dataclass
class Event:
    """One immutable audit/telemetry record."""

    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    actor: str = "system"
    source: str = ""
    trace_id: str = ""
    mission_id: str = ""
    parent_id: str = ""
    risk: RiskLevel = RiskLevel.NONE
    id: str = field(default_factory=lambda: new_id("evt"))
    ts: str = field(default_factory=iso)
    seq: int = 0
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    def core(self) -> dict[str, Any]:
        """The fields covered by the chain hash, in canonical form."""
        return {
            "id": self.id,
            "ts": self.ts,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "source": self.source,
            "kind": self.kind.value if isinstance(self.kind, EventKind) else str(self.kind),
            "risk": int(self.risk),
            "payload": self.payload,
            "mission_id": self.mission_id,
            "parent_id": self.parent_id,
        }

    def seal(self, prev_hash: str, seq: int) -> "Event":
        """Bind this event to its predecessor and compute its hash."""
        self.prev_hash = prev_hash or GENESIS_HASH
        self.seq = seq
        self.hash = chain_hash(self.prev_hash, self.core())
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind.value if isinstance(self.kind, EventKind) else str(self.kind),
            "actor": self.actor,
            "source": self.source,
            "trace_id": self.trace_id,
            "mission_id": self.mission_id,
            "parent_id": self.parent_id,
            "risk": self.risk.name,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        import json

        event = cls(
            kind=EventKind.parse(row["kind"]),
            payload=json.loads(row["payload"] or "{}"),
            actor=row["actor"],
            source=row["source"],
            trace_id=row["trace_id"],
            mission_id=row["mission_id"] or "",
            parent_id=row["parent_id"] or "",
            risk=RiskLevel.parse(row["risk"]),
            id=row["id"],
            ts=row["ts"],
            seq=row["seq"],
            prev_hash=row["prev_hash"],
            hash=row["hash"],
        )
        return event
