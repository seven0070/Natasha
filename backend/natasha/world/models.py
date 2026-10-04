"""World-model vocabulary: what kinds of things exist and how they relate."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from ..core import new_id
from ..core.clock import iso
from ..memory.models import Provenance


class EntityKind(str, enum.Enum):
    PERSON = "person"
    ORGANISATION = "organisation"
    DEVICE = "device"
    SERVICE = "service"
    PROJECT = "project"
    PLACE = "place"
    DOCUMENT = "document"
    ACCOUNT = "account"
    EVENT = "event"
    CONCEPT = "concept"
    ARTIFACT = "artifact"
    OTHER = "other"


class RelationKind(str, enum.Enum):
    WORKS_FOR = "works_for"
    OWNS = "owns"
    USES = "uses"
    DEPENDS_ON = "depends_on"
    PART_OF = "part_of"
    LOCATED_AT = "located_at"
    KNOWS = "knows"
    MANAGES = "manages"
    CREATED = "created"
    MENTIONS = "mentions"
    RELATES_TO = "relates_to"


class Predicate:
    """The allowed predicates for typed edges, with the kinds they connect."""

    ALLOWED: dict[str, tuple[set[EntityKind], set[EntityKind]]] = {
        "works_for": ({EntityKind.PERSON}, {EntityKind.ORGANISATION}),
        "employs": ({EntityKind.ORGANISATION}, {EntityKind.PERSON}),
        "owns": ({EntityKind.PERSON, EntityKind.ORGANISATION}, {EntityKind.DEVICE, EntityKind.SERVICE,
                                                               EntityKind.ACCOUNT, EntityKind.PROJECT,
                                                               EntityKind.ARTIFACT}),
        "uses": ({EntityKind.PERSON, EntityKind.SERVICE, EntityKind.PROJECT, EntityKind.DEVICE},
                 {EntityKind.SERVICE, EntityKind.DEVICE, EntityKind.CONCEPT}),
        "depends_on": ({EntityKind.SERVICE, EntityKind.PROJECT, EntityKind.ARTIFACT},
                       {EntityKind.SERVICE, EntityKind.DEVICE, EntityKind.CONCEPT, EntityKind.ACCOUNT}),
        "part_of": (set(EntityKind), set(EntityKind)),
        "located_at": (set(EntityKind), {EntityKind.PLACE}),
        "knows": ({EntityKind.PERSON}, {EntityKind.PERSON}),
        "manages": ({EntityKind.PERSON, EntityKind.ORGANISATION},
                    {EntityKind.PROJECT, EntityKind.SERVICE, EntityKind.PERSON}),
        "created": ({EntityKind.PERSON, EntityKind.ORGANISATION, EntityKind.SERVICE, EntityKind.PROJECT},
                    {EntityKind.ARTIFACT, EntityKind.DOCUMENT, EntityKind.CONCEPT}),
        "mentions": (set(EntityKind), set(EntityKind)),
        "relates_to": (set(EntityKind), set(EntityKind)),
    }

    @classmethod
    def is_allowed(cls, predicate: str, subject_kind: EntityKind, object_kind: EntityKind) -> bool:
        rule = cls.ALLOWED.get(predicate)
        if rule is None:
            return False
        subjects, objects = rule
        return subject_kind in subjects and object_kind in objects

    @classmethod
    def all(cls) -> list[str]:
        return sorted(cls.ALLOWED)


#: Human-readable aliases mapped to canonical entity names.
NAME_ALIASES: dict[str, str] = {
    "the owner": "Owner", "my laptop": "Owner Laptop", "billing": "Billing Service",
    "the billing service": "Billing Service", "postgres": "PostgreSQL", "pg": "PostgreSQL",
    "natasha": "Natasha",
}


@dataclass
class Entity:
    """A thing Natasha knows about."""

    name: str
    kind: EntityKind = EntityKind.OTHER
    id: str = field(default_factory=lambda: new_id("ent"))
    aliases: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.6
    importance: float = 0.5
    provenance: Provenance = field(default_factory=Provenance)
    created_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    mentions: int = 1
    merged_into: str = ""

    @property
    def key(self) -> str:
        return self.name.strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "kind": self.kind.value, "aliases": self.aliases,
                "attributes": self.attributes, "confidence": self.confidence,
                "importance": self.importance, "provenance": self.provenance.to_dict(),
                "created_at": self.created_at, "updated_at": self.updated_at, "mentions": self.mentions,
                "merged_into": self.merged_into}

    @classmethod
    def from_row(cls, row: Any, aliases: list[str] | None = None) -> "Entity":
        import json

        return cls(
            name=row["name"], kind=EntityKind(row["kind"]), id=row["id"], aliases=aliases or [],
            attributes=json.loads(row["attributes"] or "{}"), confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            provenance=Provenance.from_dict(json.loads(row["provenance"] or "{}")),
            created_at=row["created_at"], updated_at=row["updated_at"], mentions=int(row["mentions"]),
            merged_into=row["merged_into"] or "",
        )


@dataclass
class Relation:
    """A typed, directed edge between two entities."""

    subject_id: str
    predicate: str
    object_id: str
    id: str = field(default_factory=lambda: new_id("rel"))
    confidence: float = 0.6
    provenance: Provenance = field(default_factory=Provenance)
    created_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    observations: int = 1
    valid_from: str = ""
    valid_to: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "subject_id": self.subject_id, "predicate": self.predicate,
                "object_id": self.object_id, "confidence": self.confidence,
                "provenance": self.provenance.to_dict(), "created_at": self.created_at,
                "updated_at": self.updated_at, "observations": self.observations,
                "valid_from": self.valid_from, "valid_to": self.valid_to}

    @classmethod
    def from_row(cls, row: Any) -> "Relation":
        import json

        return cls(
            subject_id=row["subject_id"], predicate=row["predicate"], object_id=row["object_id"],
            id=row["id"], confidence=float(row["confidence"]),
            provenance=Provenance.from_dict(json.loads(row["provenance"] or "{}")),
            created_at=row["created_at"], updated_at=row["updated_at"],
            observations=int(row["observations"]), valid_from=row["valid_from"] or "",
            valid_to=row["valid_to"] or "",
        )


@dataclass
class Belief:
    """An attribute-level belief, so conflicting values stay visible."""

    entity_id: str
    attribute: str
    value: Any
    confidence: float = 0.6
    provenance: Provenance = field(default_factory=Provenance)
    id: str = field(default_factory=lambda: new_id("blf"))
    created_at: str = field(default_factory=iso)
    superseded_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "entity_id": self.entity_id, "attribute": self.attribute,
                "value": self.value, "confidence": self.confidence,
                "provenance": self.provenance.to_dict(), "created_at": self.created_at,
                "superseded_by": self.superseded_by}
