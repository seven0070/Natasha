"""Model registry and capability model."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from ..core.clock import iso


class Capability(str, Enum):
    TEXT = "text"
    VISION = "vision"
    AUDIO = "audio"
    TOOLS = "tools"
    STREAMING = "streaming"
    EMBEDDINGS = "embeddings"
    REASONING = "reasoning"
    CODE = "code"

    @classmethod
    def parse(cls, value: object) -> "Capability":
        if isinstance(value, Capability):
            return value
        text = str(value or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        raise ValueError(f"unknown capability {value!r}")


@dataclass
class ModelInfo:
    """A model known to Natasha, with everything routing needs to compare it."""

    id: str
    provider: str
    label: str = ""
    context_window: int = 8192
    capabilities: set[Capability] = field(default_factory=lambda: {Capability.TEXT, Capability.STREAMING})
    local: bool = False
    quality: float = 0.5
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0
    privacy_tier: str = "cloud"
    last_seen: str = field(default_factory=iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.id}"

    @property
    def is_free(self) -> bool:
        return self.cost_per_1k_in == 0 and self.cost_per_1k_out == 0

    def supports(self, capability: Capability | str) -> bool:
        return Capability.parse(capability) in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "provider": self.provider, "label": self.label or self.id,
            "context_window": self.context_window,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "local": self.local, "quality": self.quality, "cost_per_1k_in": self.cost_per_1k_in,
            "cost_per_1k_out": self.cost_per_1k_out, "privacy_tier": self.privacy_tier,
            "last_seen": self.last_seen, "metadata": self.metadata,
        }


#: Aliases the UI, CLI and task profiles can use instead of hard-coding model ids.
ALIASES = {
    "fast": {"max_quality": 0.7, "prefer_local": True},
    "smart": {"min_quality": 0.85},
    "local": {"local_only": True},
    "cheap": {"max_cost": 0.0, "prefer_local": True},
    "vision": {"capability": Capability.VISION},
    "code": {"capability": Capability.CODE, "min_quality": 0.7},
}


class ModelRegistry:
    """In-memory registry of discovered models."""

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}
        self._lock = threading.RLock()

    def register(self, model: ModelInfo) -> ModelInfo:
        with self._lock:
            existing = self._models.get(model.key)
            if existing:
                model.last_seen = iso()
                model.metadata = {**existing.metadata, **model.metadata}
            self._models[model.key] = model
            return model

    def register_many(self, models: Iterable[ModelInfo]) -> int:
        count = 0
        for model in models:
            self.register(model)
            count += 1
        return count

    def all(self) -> list[ModelInfo]:
        with self._lock:
            return list(self._models.values())

    def for_provider(self, provider: str) -> list[ModelInfo]:
        return [model for model in self.all() if model.provider == provider]

    def get(self, model_id: str, *, provider: str = "") -> ModelInfo | None:
        with self._lock:
            if provider:
                return self._models.get(f"{provider}:{model_id}")
            for model in self._models.values():
                if model.id == model_id:
                    return model
            return None

    def find(
        self,
        *,
        capability: Capability | str | None = None,
        local_only: bool = False,
        min_quality: float = 0.0,
        max_quality: float | None = None,
        min_context: int = 0,
        max_cost: float | None = None,
        provider: str = "",
    ) -> list[ModelInfo]:
        results = []
        for model in self.all():
            if capability is not None and not model.supports(capability):
                continue
            if local_only and not model.local:
                continue
            if provider and model.provider != provider:
                continue
            if model.quality < min_quality or (max_quality is not None and model.quality > max_quality):
                continue
            if model.context_window < min_context:
                continue
            if max_cost is not None and model.cost_per_1k_in > max_cost:
                continue
            results.append(model)
        return results

    def with_capability(self, capability: Capability | str) -> list[ModelInfo]:
        """Every known model that declares *capability*."""
        parsed = Capability.parse(capability)
        return [model for model in self.all() if model.supports(parsed)]

    def resolve_alias(self, name: str) -> tuple[str, dict[str, Any]]:
        """``"smart"`` -> ``("smart", {...filters})``; model ids pass through untouched."""
        return name, ALIASES.get(name.lower(), {})

    def clear(self, *, provider: str = "") -> None:
        with self._lock:
            if provider:
                for key in [key for key, model in self._models.items() if model.provider == provider]:
                    self._models.pop(key, None)
            else:
                self._models.clear()

    def stats(self) -> dict[str, Any]:
        models = self.all()
        by_provider: dict[str, int] = {}
        for model in models:
            by_provider[model.provider] = by_provider.get(model.provider, 0) + 1
        return {
            "total": len(models), "by_provider": by_provider,
            "local": sum(1 for model in models if model.local),
            "vision": sum(1 for model in models if model.supports(Capability.VISION)),
            "embeddings": sum(1 for model in models if model.supports(Capability.EMBEDDINGS)),
        }


_REGISTRY: ModelRegistry | None = None
_LOCK = threading.Lock()


def get_model_registry() -> ModelRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = ModelRegistry()
    return _REGISTRY


def reset_model_registry() -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
