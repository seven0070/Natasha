"""Hybrid retrieval scoring."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from ..core import parse_iso
from .embeddings import cosine
from .models import MemoryRecord

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "of", "to", "in", "on", "for", "with", "is",
    "are", "was", "were", "be", "been", "being", "it", "its", "this", "that", "these", "those", "as",
    "at", "by", "from", "into", "about", "my", "me", "i", "you", "your", "we", "our", "they", "them",
    "do", "does", "did", "how", "what", "when", "where", "who", "why", "can", "could", "should", "would",
}
TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokens(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall((text or "").lower())
            if token not in STOPWORDS and len(token) > 1]


@dataclass
class RetrievalQuery:
    """What is being looked for, and in what context."""

    text: str = ""
    mission_id: str = ""
    kinds: list[str] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ScoredMemory:
    """A memory plus why it ranked where it did."""

    record: MemoryRecord
    score: float
    parts: dict[str, float] = field(default_factory=dict)

    def explain(self) -> dict[str, Any]:
        """Why this hit ranked where it did - the signals that fired, largest first."""
        return {
            "memory_id": self.record.id,
            "kind": self.record.kind.value,
            "score": round(self.score, 4),
            "signals": {key: round(value, 4)
                        for key, value in sorted(self.parts.items(), key=lambda kv: -kv[1])
                        if value},
            "provenance": self.record.provenance.to_dict(),
            "confidence": self.record.confidence,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"memory": self.record.to_dict(), "score": round(self.score, 4),
                "parts": {key: round(value, 4) for key, value in self.parts.items()}}


class HybridRetriever:
    """Weighted blend of independent signals. Weights are tunable and always explained."""

    def __init__(
        self,
        *,
        weights: dict[str, float] | None = None,
        recency_half_life_days: float = 45.0,
        confidence_floor: float = 0.5,
    ) -> None:
        self.weights = {
            "semantic": 0.34, "keyword": 0.22, "entity": 0.14, "temporal": 0.08,
            "recency": 0.10, "importance": 0.07, "task": 0.05,
            **(weights or {}),
        }
        self.half_life_days = recency_half_life_days
        self.confidence_floor = confidence_floor

    # -- individual signals ---------------------------------------------------- #
    @staticmethod
    def keyword_score(query_tokens: Sequence[str], record: MemoryRecord) -> float:
        if not query_tokens:
            return 0.0
        haystack = f"{record.summary} {record.content} {' '.join(record.tags)}".lower()
        hay_tokens = TOKEN_RE.findall(haystack)
        if not hay_tokens:
            return 0.0
        haystack_set = set(hay_tokens)
        matched = sum(1 for token in query_tokens if token in haystack_set)
        coverage = matched / len(query_tokens)
        density = min(1.0, matched / max(8, len(hay_tokens) ** 0.5))
        return round(0.7 * coverage + 0.3 * density, 6)

    @staticmethod
    def entity_score(query_tokens: Sequence[str], record: MemoryRecord) -> float:
        if not record.entities:
            return 0.0
        query_set = set(query_tokens)
        hits = 0
        for entity in record.entities:
            entity_tokens = set(tokens(entity))
            if entity_tokens and (entity_tokens & query_set or " ".join(entity_tokens) in " ".join(query_tokens)):
                hits += 1
        return min(1.0, hits / max(1, len(record.entities)))

    def recency_score(self, record: MemoryRecord, now: datetime) -> float:
        try:
            created = parse_iso(record.created_at)
        except Exception:
            return 0.3
        age_days = max(0.0, (now - created).total_seconds() / 86400)
        return round(0.5 ** (age_days / self.half_life_days), 6)

    @staticmethod
    def temporal_score(query_text: str, record: MemoryRecord) -> float:
        lowered = query_text.lower()
        if not lowered:
            return 0.0
        try:
            created = parse_iso(record.created_at)
        except Exception:
            return 0.0
        score = 0.0
        if any(word in lowered for word in ("today", "just now", "latest")):
            days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            score = 1.0 if days < 1 else 0.4 if days < 3 else 0.0
        if any(word in lowered for word in ("yesterday", "last week", "recent")):
            days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            score = max(score, 0.8 if days <= 2 else 0.5 if days <= 10 else 0.1)
        if "last month" in lowered:
            days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            score = max(score, 0.7 if 20 <= days <= 45 else 0.2)
        for month in ("january", "february", "march", "april", "may", "june", "july", "august",
                      "september", "october", "november", "december"):
            if month in lowered and month[:3] == created.strftime("%b").lower():
                score = max(score, 0.8)
        return score

    @staticmethod
    def task_score(record: MemoryRecord, query: RetrievalQuery) -> float:
        if not query.mission_id:
            return 0.0
        if record.provenance.mission_id == query.mission_id:
            return 1.0
        if record.metadata.get("mission_id") == query.mission_id:
            return 1.0
        return 0.2 if record.kind.value in ("task", "working") else 0.0

    # -- blend ----------------------------------------------------------------- #
    def rank(
        self,
        query: RetrievalQuery,
        records: Iterable[MemoryRecord],
        *,
        embed: Callable[[Iterable[str]], list[Any]] | None = None,
    ) -> list[ScoredMemory]:
        """Score and sort candidates, highest first."""
        items = list(records)
        if not items:
            return []
        query_tokens = tokens(query.text)
        query_vector: list[float] = []
        if embed is not None and query.text.strip():
            try:
                query_vector = list(embed([query.text])[0].vector)
            except Exception:
                query_vector = []
        now = query.now
        scored: list[ScoredMemory] = []
        for record in items:
            semantic = cosine(query_vector, record.embedding) if query_vector and record.embedding else 0.0
            semantic = max(0.0, semantic)
            parts = {
                "semantic": semantic,
                "keyword": self.keyword_score(query_tokens, record),
                "entity": self.entity_score(query_tokens, record),
                "temporal": self.temporal_score(query.text, record),
                "recency": self.recency_score(record, now),
                "importance": max(0.0, min(1.0, record.importance)),
                "task": self.task_score(record, query),
            }
            base = sum(self.weights[key] * parts[key] for key in self.weights)
            # Confidence scales the evidence: a guess never outranks a verified memory.
            confidence_factor = self.confidence_floor + (1 - self.confidence_floor) * record.confidence
            score = base * confidence_factor
            if record.pinned:
                score += 0.08
            parts["confidence"] = record.confidence
            parts["pinned"] = 1.0 if record.pinned else 0.0
            scored.append(ScoredMemory(record=record, score=round(min(1.0, score), 6), parts=parts))
        scored.sort(key=lambda item: (item.score, item.record.importance, item.record.created_at),
                    reverse=True)
        return scored
