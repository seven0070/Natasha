"""SQLite-backed memory store with hybrid retrieval."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from ..core import ConflictError, NotFoundError, get_paths
from ..core.clock import iso, parse_iso
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from ..events.sanitizer import get_sanitizer
from ..security.policy import Capability, PolicyEngine, PolicyRequest
from .embeddings import Embedder, cosine, get_embedder
from .models import MemoryKind, MemoryRecord, Provenance, Retention
from .retrieval import HybridRetriever, RetrievalQuery, ScoredMemory

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    entities TEXT DEFAULT '[]',
    importance REAL DEFAULT 0.5,
    confidence REAL DEFAULT 0.8,
    retention TEXT DEFAULT 'long_term',
    provenance TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT DEFAULT '',
    access_count INTEGER DEFAULT 0,
    expires_at TEXT DEFAULT '',
    pinned INTEGER DEFAULT 0,
    superseded_by TEXT DEFAULT '',
    embedding TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_active ON memories(superseded_by) WHERE superseded_by = '';
CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT,
    action TEXT,
    detail TEXT,
    actor TEXT,
    at TEXT
);
"""


@dataclass
class _Candidate:
    record: MemoryRecord
    semantic: float = 0.0
    keyword: float = 0.0
    entity: float = 0.0
    temporal: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    task: float = 0.0


class MemoryStore:
    """Persistent memory. All reads respect policy; all writes are audited."""

    def __init__(self, *, db_path: str | None = None, embedder: Embedder | None = None,
                 log: EventLog | None = None, policy: PolicyEngine | None = None,
                 retriever: HybridRetriever | None = None) -> None:
        self.db_path = str(db_path or get_paths().db_path("memory"))
        self.embedder = embedder or get_embedder()
        self.log = log or get_event_log()
        self.policy = policy or PolicyEngine()
        self.retriever = retriever or HybridRetriever()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ write
    def add(
        self,
        kind: MemoryKind | str,
        content: str,
        *,
        summary: str = "",
        tags: Iterable[str] | None = None,
        entities: Iterable[str] | None = None,
        importance: float = 0.5,
        confidence: float = 0.8,
        retention: Retention | str = Retention.LONG_TERM,
        provenance: Provenance | None = None,
        actor: str = "owner",
        expires_at: str = "",
        pinned: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        """Store a memory. Refuses to write without provenance or with an empty body."""
        parsed_kind = kind if isinstance(kind, MemoryKind) else MemoryKind(str(kind))
        retention_value = retention if isinstance(retention, Retention) else Retention(str(retention))
        text = (content or "").strip()
        if not text:
            raise ConflictError("a memory needs content")
        self._check(Capability.MEMORY_WRITE, actor, f"{parsed_kind.value}:{text[:80]}")
        # A memory is model-readable context forever, so a live secret must never be written into it:
        # the sanitizer redacts known secrets, key-shaped values and key=value parameters in place.
        sanitized = get_sanitizer().sanitize_text(text)
        if sanitized != text:
            text = sanitized
            metadata = dict(metadata or {})
            metadata["sanitized"] = True
        record = MemoryRecord(
            kind=parsed_kind, content=text,
            summary=get_sanitizer().sanitize_text((summary or text[:160]).strip()),
            tags=sorted({tag.strip().lower() for tag in (tags or []) if tag.strip()}),
            entities=[entity.strip() for entity in (entities or []) if entity and entity.strip()],
            importance=max(0.0, min(1.0, float(importance))),
            confidence=max(0.0, min(1.0, float(confidence))), retention=retention_value,
            provenance=provenance or Provenance(actor=actor, source="conversation",
                                                trust="owner" if actor.startswith("owner") else "inferred"),
            expires_at=expires_at, pinned=pinned, metadata=dict(metadata or {}),
        )
        embedding = self.embedder.embed_sync([record.content])[0]
        record.embedding = embedding.vector
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memories (id, kind, content, summary, tags, entities, importance, confidence,"
                " retention, provenance, created_at, updated_at, last_accessed_at, access_count, expires_at,"
                " pinned, superseded_by, embedding, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.id, record.kind.value, record.content, record.summary, json.dumps(record.tags),
                 json.dumps(record.entities), record.importance, record.confidence, record.retention.value,
                 json.dumps(record.provenance.to_dict()), record.created_at, record.updated_at,
                 record.last_accessed_at, record.access_count, record.expires_at, int(record.pinned), "",
                 json.dumps(record.embedding), json.dumps(record.metadata)),
            )
            self._note(record.id, "added", f"{record.kind.value}: {record.summary[:120]}", actor)
        self.log.append(EventKind.MEMORY,
                        {"action": "added", "memory_id": record.id, "kind": record.kind.value,
                         "summary": record.summary[:200], "importance": record.importance,
                         "tags": record.tags, "provenance": record.provenance.to_dict()},
                        actor=actor, source="memory.store")
        return record

    def get(self, memory_id: str, *, actor: str = "owner", touch: bool = True) -> MemoryRecord:
        self._check(Capability.MEMORY_READ, actor, memory_id)
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"memory {memory_id!r} not found")
        record = MemoryRecord.from_row(row)
        if touch and record.active:
            record.touch()
            with self._lock, self._conn:
                self._conn.execute("UPDATE memories SET last_accessed_at = ?, access_count = ? WHERE id = ?",
                                   (record.last_accessed_at, record.access_count, record.id))
        return record

    def update(self, memory_id: str, *, content: str | None = None, summary: str | None = None,
               importance: float | None = None, confidence: float | None = None,
               tags: Iterable[str] | None = None, entities: Iterable[str] | None = None,
               pinned: bool | None = None, metadata: dict[str, Any] | None = None,
               actor: str = "owner") -> MemoryRecord:
        """Edit a memory in place (the owner's direct correction path)."""
        self._check(Capability.MEMORY_WRITE, actor, memory_id)
        record = self.get(memory_id, actor=actor, touch=False)
        if content is not None:
            record.content = content.strip()
            record.embedding = self.embedder.embed_sync([record.content])[0].vector
        if summary is not None:
            record.summary = summary
        if importance is not None:
            record.importance = max(0.0, min(1.0, float(importance)))
        if confidence is not None:
            record.confidence = max(0.0, min(1.0, float(confidence)))
        if tags is not None:
            record.tags = sorted({tag.strip().lower() for tag in tags if tag.strip()})
        if entities is not None:
            record.entities = [entity.strip() for entity in entities if entity and entity.strip()]
        if pinned is not None:
            record.pinned = bool(pinned)
        if metadata is not None:
            record.metadata = {**record.metadata, **metadata}
        record.updated_at = iso()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memories SET content=?, summary=?, tags=?, entities=?, importance=?, confidence=?,"
                " pinned=?, embedding=?, metadata=?, updated_at=? WHERE id=?",
                (record.content, record.summary, json.dumps(record.tags), json.dumps(record.entities),
                 record.importance, record.confidence, int(record.pinned), json.dumps(record.embedding),
                 json.dumps(record.metadata), record.updated_at, record.id),
            )
            self._note(record.id, "updated", record.summary[:120], actor)
        self.log.append(EventKind.MEMORY, {"action": "updated", "memory_id": record.id},
                        actor=actor, source="memory.store")
        return record

    def correct(self, memory_id: str, *, content: str, reason: str = "", actor: str = "owner",
                confidence: float = 0.95) -> MemoryRecord:
        """Owner correction: the old memory is superseded (kept for audit, excluded from recall)."""
        self._check(Capability.MEMORY_WRITE, actor, memory_id)
        old = self.get(memory_id, actor=actor, touch=False)
        replacement = self.add(
            old.kind, content, summary=content[:160], tags=old.tags, entities=old.entities,
            importance=max(old.importance, 0.6), confidence=confidence, retention=old.retention,
            provenance=Provenance(source="owner", actor=actor, reference=f"correction of {old.id}",
                                  trust="owner", mission_id=old.provenance.mission_id),
            actor=actor, metadata={**old.metadata, "corrects": old.id, "reason": reason},
        )
        with self._lock, self._conn:
            self._conn.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                               (replacement.id, iso(), old.id))
            self._note(old.id, "superseded", f"by {replacement.id}: {reason[:120]}", actor)
        self.log.append(EventKind.MEMORY,
                        {"action": "corrected", "memory_id": old.id, "replacement_id": replacement.id,
                         "reason": reason[:200]}, actor=actor, source="memory.store", risk=RiskLevel.LOW)
        return replacement

    def forget(self, memory_id: str | None = None, *, query: str = "", kind: MemoryKind | str = "",
               before: str = "", actor: str = "owner", hard: bool = False) -> dict[str, Any]:
        """Forget by id, by query match, by kind or before a date. Audit rows are kept."""
        self._check(Capability.MEMORY_DELETE, actor, memory_id or query or kind or before)
        removed: list[str] = []
        if memory_id:
            targets = [self.get(memory_id, actor=actor, touch=False)]
        else:
            targets = [scored.record for scored in self.recall(
                query, kinds=[kind] if kind else None, limit=500, actor=actor, record_access=False)]
            if before:
                cutoff = parse_iso(before)
                targets = [record for record in targets if parse_iso(record.created_at) < cutoff]
        for record in targets:
            removed.append(record.id)
            if hard:
                with self._lock, self._conn:
                    self._conn.execute("DELETE FROM memories WHERE id = ?", (record.id,))
            else:
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE memories SET superseded_by = 'forgotten', updated_at = ? WHERE id = ?",
                        (iso(), record.id))
            self._note(record.id, "forgotten" if not hard else "erased", query or kind or "explicit", actor)
        self.log.append(EventKind.MEMORY,
                        {"action": "forgotten", "count": len(removed), "ids": removed[:50],
                         "query": query[:120], "kind": kind, "hard": hard},
                        actor=actor, source="memory.store")
        return {"forgotten": len(removed), "ids": removed}

    def note_artifact(self, path: str, *, description: str = "artifact", mission_id: str = "",
                      actor: str = "owner") -> MemoryRecord:
        """Index a produced artifact so it can be recalled later."""
        return self.add(MemoryKind.ARTIFACT, f"Artifact at {path}: {description}",
                        tags=["artifact"], entities=[], importance=0.4,
                        provenance=Provenance(source="tool", actor=actor, reference=path,
                                              mission_id=mission_id),
                        actor=actor, metadata={"path": path})

    # ------------------------------------------------------------------ read
    def recall(self, query: str = "", *, kinds: Iterable[MemoryKind | str] | None = None, limit: int = 10,
               tags: Iterable[str] | None = None, entities: Iterable[str] | None = None,
               mission_id: str = "", actor: str = "owner", min_score: float = 0.0,
               include_superseded: bool = False, record_access: bool = True) -> list[ScoredMemory]:
        """Hybrid retrieval: semantic + keyword + entity + temporal + recency + importance + task."""
        self._check(Capability.MEMORY_READ, actor, query[:120])
        parsed_kinds = [kind if isinstance(kind, MemoryKind) else MemoryKind(str(kind)) for kind in (kinds or [])]
        where = ["1=1"]
        params: list[Any] = []
        if parsed_kinds:
            where.append(f"kind IN ({','.join('?' for _ in parsed_kinds)})")
            params.extend(kind.value for kind in parsed_kinds)
        if not include_superseded:
            where.append("superseded_by = ''")
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM memories WHERE {' AND '.join(where)}",
                                      params).fetchall()
        records = [MemoryRecord.from_row(row, json.loads(row["embedding"] or "[]")) for row in rows]
        now = iso()
        records = [record for record in records if not record.expires_at or record.expires_at > now]
        if tags:
            required = {tag.lower() for tag in tags}
            records = [record for record in records if required & set(record.tags)]
        if entities:
            required = {entity.lower() for entity in entities}
            records = [record for record in records if required & {item.lower() for item in record.entities}]
        if mission_id:
            records = [record for record in records
                       if record.provenance.mission_id in ("", mission_id) or
                       record.metadata.get("mission_id") in ("", mission_id)]
        scored = self.retriever.rank(
            RetrievalQuery(text=query, mission_id=mission_id, kinds=[kind.value for kind in parsed_kinds]),
            records, embed=self.embedder.embed_sync,
        )
        results = [item for item in scored if item.score >= min_score][:limit]
        if record_access:
            with self._lock, self._conn:
                for item in results:
                    item.record.touch()
                    self._conn.execute(
                        "UPDATE memories SET last_accessed_at = ?, access_count = ? WHERE id = ?",
                        (item.record.last_accessed_at, item.record.access_count, item.record.id))
        if results:
            self.log.append(EventKind.MEMORY,
                            {"action": "recalled", "count": len(results), "query": query[:160],
                             "top": [item.record.id for item in results[:5]],
                             "kinds": [item.record.kind.value for item in results[:5]]},
                            actor=actor, source="memory.store")
        return results

    def list(self, *, kind: MemoryKind | str = "", limit: int = 100, include_superseded: bool = False,
             actor: str = "owner") -> list[MemoryRecord]:
        self._check(Capability.MEMORY_READ, actor, "list")
        where, params = ["1=1"], []
        if kind:
            where.append("kind = ?")
            params.append((kind.value if isinstance(kind, MemoryKind) else str(kind)))
        if not include_superseded:
            where.append("superseded_by = ''")
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
                params).fetchall()
        return [MemoryRecord.from_row(row, json.loads(row["embedding"] or "[]")) for row in rows]

    def history(self, memory_id: str, *, actor: str = "owner") -> list[dict[str, Any]]:
        self._check(Capability.MEMORY_READ, actor, memory_id)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory_events WHERE memory_id = ? ORDER BY id", (memory_id,)).fetchall()
        return [dict(row) for row in rows]

    def stats(self, *, actor: str = "owner") -> dict[str, Any]:
        self._check(Capability.MEMORY_READ, actor, "stats")
        with self._lock:
            by_kind = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM memories WHERE superseded_by = '' GROUP BY kind").fetchall()
            totals = self._conn.execute(
                "SELECT COUNT(*) AS total, SUM(pinned) AS pinned, "
                "SUM(CASE WHEN superseded_by != '' THEN 1 ELSE 0 END) AS superseded FROM memories").fetchone()
            events = self._conn.execute("SELECT COUNT(*) AS n FROM memory_events").fetchone()
        return {
            "by_kind": {row["kind"]: row["n"] for row in by_kind},
            "total": totals["total"], "pinned": totals["pinned"] or 0,
            "superseded": totals["superseded"] or 0, "events": events["n"],
            "db_path": self.db_path, "embedder": getattr(self.embedder, "name", "unknown"),
        }

    # ------------------------------------------------------------------ maintenance
    def decay(self, *, actor: str = "system") -> dict[str, Any]:
        """Expire transient memories and mark stale low-value ones for the owner's review."""
        now = iso()
        with self._lock, self._conn:
            expired = self._conn.execute(
                "UPDATE memories SET superseded_by = 'expired', updated_at = ? "
                "WHERE superseded_by = '' AND expires_at != '' AND expires_at <= ? AND pinned = 0",
                (now, now)).rowcount
        if expired:
            self.log.append(EventKind.MEMORY, {"action": "decay", "expired": expired},
                            actor=actor, source="memory.store")
        return {"expired": expired}

    def consolidate(self, *, actor: str = "system", min_duplicates: int = 2) -> dict[str, Any]:
        """Merge near-identical memories of the same kind, keeping the newest and its provenance."""
        groups: dict[tuple[str, str], list[MemoryRecord]] = {}
        for record in self.list(limit=1000, actor=actor):
            key = (record.kind.value, " ".join(sorted(record.content.lower().split()))[:200])
            groups.setdefault(key, []).append(record)
        merged = 0
        for (_, key_text), records in groups.items():
            if len(records) < min_duplicates or not key_text:
                continue
            records.sort(key=lambda record: record.created_at, reverse=True)
            keep, duplicates = records[0], records[1:]
            with self._lock, self._conn:
                for duplicate in duplicates:
                    self._conn.execute("UPDATE memories SET superseded_by = ?, updated_at = ? WHERE id = ?",
                                       (keep.id, iso(), duplicate.id))
                    merged += 1
        if merged:
            self.log.append(EventKind.MEMORY, {"action": "consolidated", "merged": merged},
                            actor=actor, source="memory.store")
        return {"merged": merged}

    def export(self, *, actor: str = "owner") -> dict[str, Any]:
        self._check(Capability.MEMORY_READ, actor, "export")
        return {"version": 1, "exported_at": iso(),
                "memories": [record.to_dict() for record in self.list(limit=10_000, actor=actor)]}

    def import_(self, payload: dict[str, Any], *, actor: str = "owner") -> int:
        """Import an export. Existing ids are skipped so an import is idempotent."""
        imported = 0
        for item in payload.get("memories", []):
            with self._lock:
                exists = self._conn.execute("SELECT 1 FROM memories WHERE id = ?", (item.get("id"),)).fetchone()
            if exists:
                continue
            record = self.add(
                item.get("kind", "semantic"), item.get("content", ""), summary=item.get("summary", ""),
                tags=item.get("tags"), entities=item.get("entities"),
                importance=float(item.get("importance", 0.5)), confidence=float(item.get("confidence", 0.8)),
                retention=item.get("retention", "long_term"),
                provenance=Provenance.from_dict(item.get("provenance")), actor=actor,
                pinned=bool(item.get("pinned")), metadata=item.get("metadata"),
            )
            with self._lock, self._conn:
                self._conn.execute("UPDATE memories SET id = ?, created_at = ? WHERE id = ?",
                                   (item.get("id", record.id), item.get("created_at", record.created_at),
                                    record.id))
            imported += 1
        return imported

    # ------------------------------------------------------------------ internals
    def _check(self, capability: Capability, actor: str, resource: str) -> None:
        decision = self.policy.check(PolicyRequest(capability, resource, actor=actor))
        decision.raise_if_denied()

    def _note(self, memory_id: str, action: str, detail: str, actor: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memory_events (memory_id, action, detail, actor, at) VALUES (?,?,?,?,?)",
                (memory_id, action, detail[:500], actor, iso()))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORE: MemoryStore | None = None
_LOCK = threading.Lock()


def get_memory_store(**kwargs: Any) -> MemoryStore:
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = MemoryStore(**kwargs)
        return _STORE


def reset_memory_store() -> None:
    global _STORE
    with _LOCK:
        _STORE = None
