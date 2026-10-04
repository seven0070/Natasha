"""Embeddings: a deterministic local embedder plus a provider-backed one."""

from __future__ import annotations

import hashlib
import math
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

TOKEN_RE = re.compile(r"[a-z0-9']+")
DEFAULT_DIM = 384


@dataclass
class Embedding:
    vector: list[float]
    model: str = "hashing"
    dim: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.dim = self.dim or len(self.vector)

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "dim": self.dim, "vector": self.vector}


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 when either is empty)."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (norm_left * norm_right)))


class Embedder:
    """Interface: text in, vectors out. Never raises for content reasons."""

    name = "embedder"
    dim = DEFAULT_DIM

    async def embed(self, texts: Iterable[str]) -> list[Embedding]:  # pragma: no cover - interface
        raise NotImplementedError

    def embed_sync(self, texts: Iterable[str]) -> list[Embedding]:
        raise NotImplementedError


def _tokenise(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


class HashingEmbedder(Embedder):
    """Deterministic, dependency-free embeddings (feature hashing with sub-word shingles).

    This is a *local lexical-semantic* embedder: identical wording scores high, related wording scores
    moderately through shared sub-words. It exists so memory works offline and reproducibly; when a
    provider with an embedding model is configured, :class:`ProviderEmbedder` is preferred.
    """

    name = "hashing-local"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = _tokenise(text)
        features: list[tuple[str, float]] = [(token, 1.0) for token in tokens]
        features += [(f"{a}_{b}", 0.6) for a, b in zip(tokens, tokens[1:])]
        for token in tokens:
            if len(token) > 4:
                features += [(token[index:index + 4], 0.35) for index in range(len(token) - 3)]
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    async def embed(self, texts: Iterable[str]) -> list[Embedding]:
        return self.embed_sync(texts)

    def embed_sync(self, texts: Iterable[str]) -> list[Embedding]:
        return [Embedding(self._vector(text), model=self.name, dim=self.dim) for text in texts]


class ProviderEmbedder(Embedder):
    """Embeddings from a configured provider model (falls back to the local embedder on failure)."""

    def __init__(self, brain: Any, *, model: str = "", provider: str = "", fallback: Embedder | None = None) -> None:
        self.brain = brain
        self.model = model
        self.provider = provider
        self.fallback = fallback or HashingEmbedder()
        self.name = f"provider:{provider or 'auto'}:{model or 'auto'}"

    async def embed(self, texts: Iterable[str]) -> list[Embedding]:
        items = list(texts)
        try:
            vectors = await self.brain.embed(items, model=self.model, provider=self.provider, actor="system")
            out: list[Embedding] = []
            for vector in vectors:
                data = getattr(vector, "vector", vector)
                out.append(Embedding(list(data), model=self.name, dim=len(data)))
            return out
        except Exception:
            # Local embeddings keep memory working when a provider is down: degrade, never break.
            return await self.fallback.embed(items)

    def embed_sync(self, texts: Iterable[str]) -> list[Embedding]:
        from ..core import run_coroutine_sync

        return run_coroutine_sync(self.embed(texts))


_EMBEDDER: Embedder | None = None
_LOCK = threading.Lock()


def get_embedder(kind: str = "auto", **kwargs: Any) -> Embedder:
    """Process-wide embedder. ``kind`` is ``auto``, ``local`` or ``provider``."""
    global _EMBEDDER
    with _LOCK:
        if _EMBEDDER is None:
            if kind == "provider":
                from ..brain import get_brain

                _EMBEDDER = ProviderEmbedder(get_brain(), **kwargs)
            else:
                _EMBEDDER = HashingEmbedder(**{k: v for k, v in kwargs.items() if k == "dim"})
        return _EMBEDDER


def reset_embedder() -> None:
    global _EMBEDDER
    with _LOCK:
        _EMBEDDER = None
