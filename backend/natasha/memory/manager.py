"""High-level memory API used by the executive loop, workers and the UI."""

from __future__ import annotations

import threading
from typing import Any, Iterable

from ..core.risk import RiskLevel
from .models import MemoryKind, MemoryRecord, Provenance
from .store import MemoryStore, get_memory_store


class MemoryManager:
    """Coordinates working memory, durable memory and honest recall."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or get_memory_store()

    # -- writing --------------------------------------------------------------- #
    def remember(
        self,
        content: str,
        *,
        kind: MemoryKind | str = MemoryKind.SEMANTIC,
        importance: float = 0.5,
        confidence: float = 0.8,
        source: str = "conversation",
        actor: str = "system",
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        mission_id: str = "",
        trace_id: str = "",
        event_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        return self.store.add(
            kind, content, importance=importance, confidence=confidence, tags=tags, entities=entities,
            provenance=Provenance(source=source, actor=actor, mission_id=mission_id, trace_id=trace_id, event_id=event_id),
            metadata=metadata,
        )

    def remember_turn(self, user: str, assistant: str, *, actor: str = "owner", mission_id: str = "",
                      trace_id: str = "") -> list[MemoryRecord]:
        """Store a conversation turn as episodic memory (one record per side)."""
        records = [
            self.store.add(
                MemoryKind.EPISODIC, f"Owner: {user}", importance=0.45, confidence=1.0,
                provenance=Provenance(source="conversation", actor=actor, mission_id=mission_id, trace_id=trace_id),
                tags=["conversation", "owner"], embed=True,
            ),
        ]
        if assistant.strip():
            records.append(
                self.store.add(
                    MemoryKind.EPISODIC, f"Natasha: {assistant}", importance=0.35, confidence=0.7,
                    provenance=Provenance(source="conversation", actor="model:main", mission_id=mission_id, trace_id=trace_id),
                    tags=["conversation", "assistant"], embed=True,
                )
            )
        self.store.working.put("last_turn", f"{user} || {assistant}"[:1000])
        return records

    def learn_procedure(self, name: str, steps: list[str], *, confidence: float = 0.7,
                        actor: str = "owner") -> MemoryRecord:
        body = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps))
        return self.store.add(
            MemoryKind.PROCEDURAL, f"{name}\n{body}", tags=["procedure", name], importance=0.7,
            confidence=confidence, provenance=Provenance(source="owner", actor=actor),
        )

    def note_artifact(self, path: str, *, description: str, mission_id: str = "",
                      actor: str = "system") -> MemoryRecord:
        return self.store.add(
            MemoryKind.ARTIFACT, f"{path}: {description}", tags=["artifact"], importance=0.5,
            provenance=Provenance(source="creation", actor=actor, mission_id=mission_id, uri=path),
            metadata={"path": path},
        )

    # -- reading --------------------------------------------------------------- #
    def recall(
        self,
        query: str,
        *,
        k: int = 12,
        kinds: list[MemoryKind | str] | None = None,
        mission_id: str = "",
        tags: Iterable[str] = (),
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        return self.store.recall(
            query, k=k, kinds=kinds, mission_id=mission_id, tags=tags, include_superseded=include_superseded
        )

    def recall_context(self, query: str, *, k: int = 8, mission_id: str = "") -> dict[str, Any]:
        """Recall plus score explanation - what the UI shows under "why I remember this"."""
        hit = self.store.search(query, k=k, mission_id=mission_id)
        return {
            "query": query,
            "memories": [
                {
                    "id": scored.record.id, "kind": scored.record.kind.value, "content": scored.record.content,
                    "score": round(scored.score, 4), "breakdown": scored.breakdown,
                    "confidence": scored.record.confidence, "source": scored.record.provenance.source,
                    "created_at": scored.record.created_at, "superseded": bool(scored.record.superseded_by),
                }
                for scored in hit.memories
            ],
            "considered": hit.considered,
        }

    # -- correction ------------------------------------------------------------ #
    def correct(self, memory_id: str, new_content: str, *, reason: str = "", actor: str = "owner") -> dict[str, Any]:
        original, replacement = self.store.correct(memory_id, new_content, reason=reason, actor=actor)
        return {"superseded": original.id, "replacement": replacement.id, "reason": reason or "owner correction"}

    def forget(self, **kwargs: Any) -> int:
        return self.store.forget(**kwargs)

    def stats(self) -> dict[str, Any]:
        return self.store.stats()


_MANAGERS: dict[str, MemoryManager] = {}
_LOCK = threading.Lock()


def get_memory_manager() -> MemoryManager:
    with _LOCK:
        if "default" not in _MANAGERS:
            _MANAGERS["default"] = MemoryManager()
        return _MANAGERS["default"]


def reset_memory_managers() -> None:
    with _LOCK:
        _MANAGERS.clear()
