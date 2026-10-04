"""The world-model store: entities, relations, beliefs, contradictions, timelines."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable

from ..core import ConflictError, NotFoundError, get_paths
from ..core.clock import iso
from ..events import EventKind, EventLog, get_event_log
from ..memory.models import Provenance
from .models import NAME_ALIASES, Belief, Entity, EntityKind, Predicate, Relation

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    attributes TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0.6,
    importance REAL DEFAULT 0.5,
    provenance TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT,
    mentions INTEGER DEFAULT 1,
    merged_into TEXT DEFAULT '',
    UNIQUE(name_key, kind)
);
CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias_key)
);
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL,
    confidence REAL DEFAULT 0.6,
    provenance TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT,
    observations INTEGER DEFAULT 1,
    valid_from TEXT DEFAULT '',
    valid_to TEXT DEFAULT '',
    UNIQUE(subject_id, predicate, object_id)
);
CREATE TABLE IF NOT EXISTS beliefs (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT,
    confidence REAL DEFAULT 0.6,
    provenance TEXT DEFAULT '{}',
    created_at TEXT,
    superseded_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_id);
CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_id);
CREATE INDEX IF NOT EXISTS idx_beliefs_entity ON beliefs(entity_id, attribute);
"""


class WorldModel:
    """Typed beliefs about the environment, with contradiction detection."""

    def __init__(self, *, db_path: str | None = None, log: EventLog | None = None) -> None:
        self.db_path = str(db_path or get_paths().db_path("world"))
        self.log = log or get_event_log()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ entities
    @staticmethod
    def normalise(name: str) -> str:
        """Canonical display name: whitespace collapsed, known aliases resolved, case preserved."""
        text = " ".join((name or "").split())
        return NAME_ALIASES.get(text.lower(), text)

    def upsert_entity(self, name: str, kind: EntityKind | str = EntityKind.OTHER, *,
                      aliases: Iterable[str] | None = None, attributes: dict[str, Any] | None = None,
                      confidence: float = 0.6, importance: float = 0.5,
                      provenance: Provenance | None = None, actor: str = "owner") -> Entity:
        """Create or strengthen an entity; repeated mentions raise confidence (capped)."""
        canonical = self.normalise(name)
        if not canonical:
            raise ConflictError("an entity needs a name")
        parsed_kind = kind if isinstance(kind, EntityKind) else EntityKind(str(kind))
        key = canonical.lower()
        with self._lock:
            existing = self._find_row(key, parsed_kind)
        if existing is not None:
            entity = self._load(existing)
            entity.confidence = min(1.0, max(entity.confidence, confidence) + 0.05)
            entity.mentions += 1
            entity.importance = max(entity.importance, importance)
            entity.attributes = {**entity.attributes, **dict(attributes or {})}
            entity.updated_at = iso()
            if provenance:
                entity.provenance = provenance
            new_aliases = sorted(set(entity.aliases) | {alias.strip() for alias in (aliases or []) if alias.strip()})
            entity.aliases = new_aliases
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE entities SET confidence=?, mentions=?, importance=?, attributes=?, updated_at=?,"
                    " provenance=? WHERE id=?",
                    (entity.confidence, entity.mentions, entity.importance, json.dumps(entity.attributes),
                     entity.updated_at, json.dumps(entity.provenance.to_dict()), entity.id))
                for alias in new_aliases:
                    self._conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_key) VALUES (?,?)",
                                       (entity.id, alias.lower()))
            self._audit("entity_updated", entity)
            return entity
        entity = Entity(name=canonical, kind=parsed_kind,
                        aliases=sorted({alias.strip() for alias in (aliases or []) if alias.strip()}),
                        attributes=dict(attributes or {}), confidence=confidence, importance=importance,
                        provenance=provenance or Provenance(source="inference", actor=actor))
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO entities (id, name, name_key, kind, attributes, confidence, importance,"
                " provenance, created_at, updated_at, mentions, merged_into) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (entity.id, entity.name, entity.key, entity.kind.value, json.dumps(entity.attributes),
                 entity.confidence, entity.importance, json.dumps(entity.provenance.to_dict()),
                 entity.created_at, entity.updated_at, entity.mentions, ""))
            for alias in entity.aliases:
                self._conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_key) VALUES (?,?)",
                                   (entity.id, alias.lower()))
        self._audit("entity_created", entity)
        return entity

    def _find_row(self, name_key: str, kind: EntityKind | None = None) -> Any:
        with self._lock:
            if kind is not None:
                row = self._conn.execute("SELECT * FROM entities WHERE name_key = ? AND kind = ?",
                                         (name_key, kind.value)).fetchone()
                if row is not None:
                    return row
            row = self._conn.execute("SELECT * FROM entities WHERE name_key = ?", (name_key,)).fetchone()
            if row is not None:
                return row
            alias = self._conn.execute(
                "SELECT e.* FROM entities e JOIN entity_aliases a ON a.entity_id = e.id WHERE a.alias_key = ?",
                (name_key,)).fetchone()
        return alias

    def _load(self, row: Any) -> Entity:
        with self._lock:
            aliases = [item["alias_key"] for item in self._conn.execute(
                "SELECT alias_key FROM entity_aliases WHERE entity_id = ?", (row["id"],)).fetchall()]
        return Entity.from_row(row, aliases)

    def get_entity(self, name_or_id: str) -> Entity:
        with self._lock:
            row = self._conn.execute("SELECT * FROM entities WHERE id = ?", (name_or_id,)).fetchone()
        if row is None:
            row = self._find_row(name_or_id.strip().lower())
        if row is None:
            raise NotFoundError(f"entity {name_or_id!r} is not in the world model")
        return self._load(row)

    def entities(self, *, kind: EntityKind | str = "", limit: int = 200, min_confidence: float = 0.0
                 ) -> list[Entity]:
        query = "SELECT * FROM entities WHERE merged_into = ''"
        params: list[Any] = []
        if kind:
            query += " AND kind = ?"
            params.append((kind.value if isinstance(kind, EntityKind) else str(kind)))
        if min_confidence:
            query += " AND confidence >= ?"
            params.append(min_confidence)
        query += " ORDER BY importance DESC, mentions DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._load(row) for row in rows]

    def search_entities(self, text: str, *, limit: int = 10) -> list[Entity]:
        needle = f"%{(text or '').strip().lower()}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT e.* FROM entities e LEFT JOIN entity_aliases a ON a.entity_id = e.id "
                "WHERE e.merged_into = '' AND (LOWER(e.name) LIKE ? OR a.alias_key LIKE ?) "
                "ORDER BY e.importance DESC LIMIT ?", (needle, needle, limit)).fetchall()
        return [self._load(row) for row in rows]

    # ------------------------------------------------------------------ relations
    def relate(self, subject: str, predicate: str, object_: str, *, confidence: float = 0.6,
               provenance: Provenance | None = None, actor: str = "owner",
               subject_kind: EntityKind | str | None = None, object_kind: EntityKind | str | None = None
               ) -> Relation:
        """Assert a typed relationship, validating the predicate against the entity kinds."""
        if predicate not in Predicate.all():
            raise ConflictError(f"unknown predicate {predicate!r}; allowed: {', '.join(Predicate.all())}")
        left = self.upsert_entity(subject, subject_kind or EntityKind.OTHER, provenance=provenance, actor=actor)
        right = self.upsert_entity(object_, object_kind or EntityKind.OTHER, provenance=provenance, actor=actor)
        if not Predicate.is_allowed(predicate, left.kind, right.kind):
            raise ConflictError(f"predicate {predicate!r} cannot connect {left.kind.value} -> {right.kind.value}")
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM relations WHERE subject_id=? AND predicate=? AND object_id=?",
                (left.id, predicate, right.id)).fetchone()
        if row is not None:
            relation = Relation.from_row(row)
            relation.confidence = min(1.0, max(relation.confidence, confidence) + 0.05)
            relation.observations += 1
            relation.updated_at = iso()
            with self._lock, self._conn:
                self._conn.execute("UPDATE relations SET confidence=?, observations=?, updated_at=? WHERE id=?",
                                   (relation.confidence, relation.observations, relation.updated_at, relation.id))
        else:
            relation = Relation(subject_id=left.id, predicate=predicate, object_id=right.id,
                                confidence=confidence, provenance=provenance or Provenance(source="inference"))
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO relations (id, subject_id, predicate, object_id, confidence, provenance,"
                    " created_at, updated_at, observations, valid_from, valid_to) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (relation.id, relation.subject_id, relation.predicate, relation.object_id,
                     relation.confidence, json.dumps(relation.provenance.to_dict()), relation.created_at,
                     relation.updated_at, relation.observations, relation.valid_from, relation.valid_to))
        with self._lock, self._conn:
            head = self._conn.execute("SELECT name FROM entities WHERE id = ?", (left.id,)).fetchone()
            tail = self._conn.execute("SELECT name FROM entities WHERE id = ?", (right.id,)).fetchone()
        self.log.append(EventKind.MEMORY,
                        {"action": "related", "subject": head["name"], "predicate": predicate,
                         "object": tail["name"], "confidence": relation.confidence},
                        actor=actor, source="world.model")
        return relation

    def relations_of(self, entity: str, *, predicate: str = "") -> list[dict[str, Any]]:
        """All edges touching an entity, with the other endpoint resolved."""
        target = self.get_entity(entity)
        query = ("SELECT r.*, s.name AS subject_name, o.name AS object_name FROM relations r "
                 "JOIN entities s ON s.id = r.subject_id JOIN entities o ON o.id = r.object_id "
                 "WHERE r.subject_id = ? OR r.object_id = ?")
        params: list[Any] = [target.id, target.id]
        if predicate:
            query += " AND r.predicate = ?"
            params.append(predicate)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [{**Relation.from_row(row).to_dict(), "subject": row["subject_name"],
                 "object": row["object_name"]} for row in rows]

    def neighbours(self, entity: str, *, depth: int = 1, limit: int = 50) -> dict[str, list[dict[str, Any]]]:
        """N-hop neighbourhood, grouped by predicate."""
        frontier = [self.get_entity(entity)]
        seen = {frontier[0].id}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for _ in range(max(1, depth)):
            next_frontier: list[Entity] = []
            for node in frontier:
                for edge in self.relations_of(node.name):
                    grouped.setdefault(edge["predicate"], []).append(edge)
                    other_id = edge["object_id"] if edge["subject_id"] == node.id else edge["subject_id"]
                    if other_id not in seen and len(seen) < limit:
                        seen.add(other_id)
                        with self._lock:
                            row = self._conn.execute("SELECT * FROM entities WHERE id = ?",
                                                     (other_id,)).fetchone()
                        if row is not None:
                            next_frontier.append(self._load(row))
            frontier = next_frontier
            if not frontier:
                break
        return grouped

    # ------------------------------------------------------------------ beliefs
    def set_belief(self, entity: str, attribute: str, value: Any, *, confidence: float = 0.6,
                   provenance: Provenance | None = None, actor: str = "owner") -> Belief:
        """Record an attribute belief; a conflicting higher-confidence value supersedes the old one."""
        target = self.upsert_entity(entity, provenance=provenance, actor=actor)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM beliefs WHERE entity_id = ? AND attribute = ? AND superseded_by = ''",
                (target.id, attribute)).fetchall()
        belief = Belief(entity_id=target.id, attribute=attribute, value=value, confidence=confidence,
                        provenance=provenance or Provenance(source="inference", actor=actor))
        superseded: list[str] = []
        for row in rows:
            existing_value = json.loads(row["value"])
            if existing_value == value:
                continue
            if float(row["confidence"]) >= confidence:
                # The older belief is stronger: keep it and store the new one as a lower-confidence
                # alternative so the contradiction stays visible instead of overwriting evidence.
                with self._lock, self._conn:
                    self._conn.execute(
                        "INSERT INTO beliefs (id, entity_id, attribute, value, confidence, provenance,"
                        " created_at, superseded_by) VALUES (?,?,?,?,?,?,?,?)",
                        (belief.id, belief.entity_id, belief.attribute, json.dumps(belief.value),
                         belief.confidence, json.dumps(belief.provenance.to_dict()), belief.created_at,
                         "conflict"))
                self._audit("belief_conflict", target, extra={"attribute": attribute, "value": value})
                return belief
            superseded.append(row["id"])
        with self._lock, self._conn:
            for old_id in superseded:
                self._conn.execute("UPDATE beliefs SET superseded_by = ? WHERE id = ?", (belief.id, old_id))
            self._conn.execute(
                "INSERT INTO beliefs (id, entity_id, attribute, value, confidence, provenance, created_at,"
                " superseded_by) VALUES (?,?,?,?,?,?,?,?)",
                (belief.id, belief.entity_id, belief.attribute, json.dumps(belief.value, default=str),
                 belief.confidence, json.dumps(belief.provenance.to_dict()), belief.created_at, ""))
        self._audit("belief_set", target, extra={"attribute": attribute, "superseded": len(superseded)})
        return belief

    def beliefs_of(self, entity: str, *, include_conflicts: bool = False) -> list[dict[str, Any]]:
        target = self.get_entity(entity)
        query = "SELECT * FROM beliefs WHERE entity_id = ?"
        if not include_conflicts:
            query += " AND superseded_by = ''"
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY created_at DESC", (target.id,)).fetchall()
        return [{"id": row["id"], "attribute": row["attribute"], "value": json.loads(row["value"] or "null"),
                 "confidence": float(row["confidence"]), "created_at": row["created_at"],
                 "provenance": json.loads(row["provenance"] or "{}"),
                 "conflict": row["superseded_by"] == "conflict"} for row in rows]

    def contradictions(self) -> list[dict[str, Any]]:
        """Where Natasha holds two incompatible beliefs (surfaced in the UI, never hidden)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT b.*, e.name AS entity_name FROM beliefs b JOIN entities e ON e.id = b.entity_id "
                "WHERE b.superseded_by = 'conflict' ORDER BY b.created_at DESC").fetchall()
        return [{"entity": row["entity_name"], "attribute": row["attribute"],
                 "value": json.loads(row["value"] or "null"), "confidence": float(row["confidence"]),
                 "created_at": row["created_at"]} for row in rows]

    # ------------------------------------------------------------------ analysis
    def timeline(self, *, limit: int = 50, since: str = "") -> list[dict[str, Any]]:
        """Chronological view of what Natasha learned about the world."""
        query = ("SELECT r.created_at AS at, s.name AS subject, r.predicate, o.name AS object,"
                 " r.confidence FROM relations r JOIN entities s ON s.id = r.subject_id "
                 "JOIN entities o ON o.id = r.object_id")
        params: list[Any] = []
        if since:
            query += " WHERE r.created_at >= ?"
            params.append(since)
        query += " ORDER BY r.created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def merge_entities(self, source: str, target: str, *, actor: str = "owner") -> dict[str, Any]:
        """Merge one entity into another: aliases, relations and beliefs move, the source is retired."""
        left = self.get_entity(source)
        right = self.get_entity(target)
        if left.id == right.id:
            raise ConflictError("cannot merge an entity into itself")
        moved_relations, moved_beliefs = 0, 0
        with self._lock, self._conn:
            self._conn.execute("UPDATE OR IGNORE relations SET subject_id = ? WHERE subject_id = ?",
                               (right.id, left.id))
            self._conn.execute("DELETE FROM relations WHERE subject_id = ?", (left.id,))
            self._conn.execute("UPDATE OR IGNORE relations SET object_id = ? WHERE object_id = ?",
                               (right.id, left.id))
            self._conn.execute("DELETE FROM relations WHERE object_id = ?", (left.id,))
            moved_relations = self._conn.execute("SELECT COUNT(*) AS n FROM relations WHERE subject_id = ? OR object_id = ?",
                                                 (right.id, right.id)).fetchone()["n"]
            self._conn.execute("UPDATE beliefs SET entity_id = ? WHERE entity_id = ?", (right.id, left.id))
            moved_beliefs = self._conn.execute("SELECT COUNT(*) AS n FROM beliefs WHERE entity_id = ?",
                                               (right.id,)).fetchone()["n"]
            self._conn.execute("INSERT OR IGNORE INTO entity_aliases (entity_id, alias_key) VALUES (?, ?)",
                               (right.id, left.key))
            self._conn.execute("UPDATE entity_aliases SET entity_id = ? WHERE entity_id = ?",
                               (right.id, left.id))
            self._conn.execute("UPDATE entities SET merged_into = ?, updated_at = ? WHERE id = ?",
                               (right.id, iso(), left.id))
            self._conn.execute("UPDATE entities SET mentions = mentions + ?, updated_at = ? WHERE id = ?",
                               (left.mentions, iso(), right.id))
        self.log.append(EventKind.MEMORY,
                        {"action": "merged_entities", "from": left.name, "into": right.name,
                         "moved_relations": moved_relations, "moved_beliefs": moved_beliefs},
                        actor=actor, source="world.model")
        return {"from": left.name, "into": right.name, "relations": moved_relations, "beliefs": moved_beliefs}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            entities = self._conn.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE merged_into = ''").fetchone()["n"]
            relations = self._conn.execute("SELECT COUNT(*) AS n FROM relations").fetchone()["n"]
            beliefs = self._conn.execute(
                "SELECT COUNT(*) AS n FROM beliefs WHERE superseded_by = ''").fetchone()["n"]
            conflicts = self._conn.execute(
                "SELECT COUNT(*) AS n FROM beliefs WHERE superseded_by = 'conflict'").fetchone()["n"]
            by_kind = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM entities WHERE merged_into = '' GROUP BY kind").fetchall()
        return {"entities": entities, "relations": relations, "beliefs": beliefs,
                "conflicts": conflicts, "by_kind": {row["kind"]: row["n"] for row in by_kind},
                "db_path": self.db_path}

    def _audit(self, action: str, entity: Entity, extra: dict[str, Any] | None = None) -> None:
        self.log.append(EventKind.MEMORY,
                        {"action": action, "entity": entity.name, "kind": entity.kind.value,
                         "confidence": entity.confidence, **(extra or {})},
                        actor=entity.provenance.actor, source="world.model")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_WORLD: WorldModel | None = None
_LOCK = threading.Lock()


def get_world_model(**kwargs: Any) -> WorldModel:
    global _WORLD
    with _LOCK:
        if _WORLD is None:
            _WORLD = WorldModel(**kwargs)
        return _WORLD


def reset_world_model() -> None:
    global _WORLD
    with _LOCK:
        _WORLD = None
