"""High-level memory API used by the executive loop, workers and the UI.

This is a deliberately thin façade: it never re-implements retrieval or storage, it only composes
the store, working memory and provenance so callers (tools, workers, UI helpers) do not have to
know the storage details. Everything it exposes is exercised by tests/unit/test_memory_manager.py,
because a stale convenience layer is worse than none at all.
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

from .models import MemoryKind, MemoryRecord, Provenance
from .store import MemoryStore, get_memory_store
from .working import get_working_memory


class MemoryManager:
    """Coordinates durable memory, working memory and honest recall."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self.store = store or get_memory_store()
        self.working = get_working_memory()

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
        """Store one durable memory, recording exactly where it came from."""
        return self.store.add(
            kind, content, importance=importance, confidence=confidence, tags=tags,
            entities=entities, actor=actor, metadata=metadata,
            provenance=Provenance(source=source, actor=actor, mission_id=mission_id,
                                  trace_id=trace_id, event_id=event_id),
        )

    def remember_turn(self, user: str, assistant: str, *, actor: str = "owner", mission_id: str = "",
                      trace_id: str = "") -> list[MemoryRecord]:
        """Store a conversation turn as episodic memory (one record per side)."""
        records = [
            self.store.add(
                MemoryKind.EPISODIC, f"Owner: {user}", importance=0.45, confidence=1.0,
                actor=actor, tags=["conversation", "owner"],
                provenance=Provenance(source="conversation", actor=actor, mission_id=mission_id,
                                      trace_id=trace_id),
            ),
        ]
        if assistant.strip():
            records.append(
                self.store.add(
                    MemoryKind.EPISODIC, f"Natasha: {assistant}", importance=0.35, confidence=0.7,
                    actor="model:main", tags=["conversation", "assistant"],
                    provenance=Provenance(source="conversation", actor="model:main",
                                          mission_id=mission_id, trace_id=trace_id),
                )
            )
        self.working.push(f"{user} || {assistant}"[:1000], role="turn", mission_id=mission_id)
        return records

    def learn_procedure(self, name: str, steps: list[str], *, confidence: float = 0.7,
                        actor: str = "owner") -> MemoryRecord:
        body = "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps))
        return self.store.add(
            MemoryKind.PROCEDURAL, f"{name}\n{body}", tags=["procedure", name], importance=0.7,
            confidence=confidence, actor=actor,
            provenance=Provenance(source="owner", actor=actor),
        )

    def note_artifact(self, path: str, *, description: str, mission_id: str = "",
                      actor: str = "system") -> MemoryRecord:
        return self.store.note_artifact(str(path), description=description,
                                        mission_id=mission_id, actor=actor)

    # -- reading --------------------------------------------------------------- #
    def recall(
        self,
        query: str,
        *,
        k: int = 12,
        kinds: list[MemoryKind | str] | None = None,
        mission_id: str = "",
        include_superseded: bool = False,
        actor: str = "model:main",
    ) -> list[Any]:
        """Hybrid recall, straight from the store (same path the orchestrator uses)."""
        return self.store.recall(query, kinds=kinds, limit=k, mission_id=mission_id,
                                 include_superseded=include_superseded, actor=actor)

    def recall_context(self, query: str, *, k: int = 8, mission_id: str = "") -> dict[str, Any]:
        """Recall plus score explanation - what the UI shows under "why I remember this"."""
        scored = self.store.recall(query, limit=k, mission_id=mission_id)
        return {
            "query": query,
            "memories": [
                {
                    "id": hit.record.id, "kind": hit.record.kind.value, "content": hit.record.content,
                    "summary": hit.record.summary, "score": round(hit.score, 4),
                    "breakdown": dict(hit.parts),
                    "confidence": hit.record.confidence, "source": hit.record.provenance.source,
                    "created_at": hit.record.created_at,
                    "superseded": bool(hit.record.superseded_by),
                }
                for hit in scored
            ],
            "count": len(scored),
        }

    # -- correction ------------------------------------------------------------ #
    def correct(self, memory_id: str, new_content: str, *, reason: str = "",
                actor: str = "owner") -> dict[str, Any]:
        """Correcting never overwrites: the original is superseded and both stay on the record."""
        replacement = self.store.correct(memory_id, content=new_content, reason=reason, actor=actor)
        original = self.store.get(memory_id, actor=actor, touch=False)
        return {"superseded": original.to_dict(), "replacement": replacement.to_dict(),
                "reason": reason or "owner correction"}

    def forget(self, **kwargs: Any) -> int:
        return self.store.forget(**kwargs)

    def stats(self) -> dict[str, Any]:
        return self.store.stats()


_MANAGERS: dict[str, MemoryManager] = {}
_LOCK = threading.Lock()


def get_memory_manager(store: MemoryStore | None = None) -> MemoryManager:
    with _LOCK:
        if "default" not in _MANAGERS or store is not None:
            _MANAGERS["default"] = MemoryManager(store)
        return _MANAGERS["default"]


def reset_memory_managers() -> None:
    with _LOCK:
        _MANAGERS.clear()
