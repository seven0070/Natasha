"""Memory records, kinds and provenance."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

from ..core import ValidationError, new_id
from ..core.clock import iso


class MemoryKind(str, enum.Enum):
    """The ten kinds Natasha keeps separate - each with its own retention and retrieval weight."""

    WORKING = "working"            # the current task's scratchpad (minutes)
    EPISODIC = "episodic"          # what happened, when (conversations, events)
    SEMANTIC = "semantic"          # durable facts about the world and the owner
    PROCEDURAL = "procedural"      # how to do things: recipes, workflows, fixes
    PROFILE = "profile"            # stable facts about the owner (identity, preferences, goals)
    PREFERENCE = "preference"      # how the owner wants things done
    RELATIONSHIP = "relationship"  # people, organisations, roles, ties
    TASK = "task"                  # ongoing/completed tasks with state
    ARTIFACT = "artifact"          # things Natasha produced (files, documents, media)
    WORLD = "world"                # beliefs about the environment (devices, projects, services)

    @property
    def durable(self) -> bool:
        return self not in (MemoryKind.WORKING, MemoryKind.TASK)


class Retention(str, enum.Enum):
    TRANSIENT = "transient"   # expires quickly
    SESSION = "session"
    LONG_TERM = "long_term"
    PERMANENT = "permanent"


def parse_kind(kind: MemoryKind | str) -> MemoryKind:
    """Parse a caller-supplied memory kind, reporting an unknown value as invalid input.

    ``MemoryKind("bad")`` raises a bare ``ValueError``, which the API layer would surface as a 500.
    An unknown kind is a client error, so it is converted here, next to the enum it validates.
    """
    if isinstance(kind, MemoryKind):
        return kind
    try:
        return MemoryKind(str(kind).strip().lower())
    except ValueError as exc:
        known = ", ".join(item.value for item in MemoryKind)
        raise ValidationError(f"unknown memory kind {kind!r}; known kinds: {known}") from exc


@dataclass
class Provenance:
    """Where a memory came from - required for anything Natasha claims to know."""

    source: str = "conversation"      # conversation | tool | document | web | owner | inference | system
    actor: str = "owner"
    reference: str = ""               # file path, URL, tool name, event id...
    mission_id: str = ""
    trace_id: str = ""                # the turn/request that produced this memory
    event_id: str = ""
    trust: str = "owner"              # owner | verified | external | inferred

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "actor": self.actor, "reference": self.reference,
                "mission_id": self.mission_id, "trace_id": self.trace_id,
                "event_id": self.event_id, "trust": self.trust}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Provenance":
        data = data or {}
        return cls(**{key: data.get(key, getattr(cls(), key)) for key in
                      ("source", "actor", "reference", "mission_id", "trace_id", "event_id", "trust")})


@dataclass
class MemoryRecord:
    """One memory. ``content`` is what gets recalled; ``summary`` is the cheap handle."""

    kind: MemoryKind
    content: str
    id: str = field(default_factory=lambda: new_id("mem"))
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    importance: float = 0.5          # 0..1
    confidence: float = 0.8          # 0..1
    retention: Retention = Retention.LONG_TERM
    provenance: Provenance = field(default_factory=Provenance)
    created_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    last_accessed_at: str = ""
    access_count: int = 0
    expires_at: str = ""
    pinned: bool = False
    superseded_by: str = ""
    embedding: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return not self.superseded_by

    def touch(self) -> None:
        self.last_accessed_at = iso()
        self.access_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind.value, "content": self.content, "summary": self.summary,
            "tags": self.tags, "entities": self.entities, "importance": self.importance,
            "confidence": self.confidence, "retention": self.retention.value,
            "provenance": self.provenance.to_dict(), "created_at": self.created_at,
            "updated_at": self.updated_at, "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count, "expires_at": self.expires_at, "pinned": self.pinned,
            "superseded_by": self.superseded_by, "metadata": self.metadata,
        }

    @classmethod
    def from_row(cls, row: Any, embedding: list[float] | None = None) -> "MemoryRecord":
        return cls(
            kind=MemoryKind(row["kind"]), content=row["content"], id=row["id"],
            summary=row["summary"] or "", tags=json.loads(row["tags"] or "[]"),
            entities=json.loads(row["entities"] or "[]"), importance=float(row["importance"]),
            confidence=float(row["confidence"]), retention=Retention(row["retention"]),
            provenance=Provenance.from_dict(json.loads(row["provenance"] or "{}")),
            created_at=row["created_at"], updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"] or "", access_count=int(row["access_count"]),
            expires_at=row["expires_at"] or "", pinned=bool(row["pinned"]),
            superseded_by=row["superseded_by"] or "", embedding=list(embedding or []),
            metadata=json.loads(row["metadata"] or "{}"),
        )
