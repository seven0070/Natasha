"""Provider registry and health monitoring."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..core import NotFoundError
from ..core.clock import iso
from .adapters import (
    AnthropicAdapter,
    EchoAdapter,
    GeminiAdapter,
    OllamaAdapter,
    OpenAICompatibleAdapter,
    ProviderAdapter,
)
from .models import Capability, ModelInfo, ModelRegistry, get_model_registry

#: provider name -> adapter class. ``OpenAICompatibleAdapter`` is parameterised by name.
ADAPTER_CLASSES: dict[str, type[ProviderAdapter]] = {
    "openai": OpenAICompatibleAdapter,
    "groq": OpenAICompatibleAdapter,
    "cerebras": OpenAICompatibleAdapter,
    "mistral": OpenAICompatibleAdapter,
    "xai": OpenAICompatibleAdapter,
    "openrouter": OpenAICompatibleAdapter,
    "nvidia": OpenAICompatibleAdapter,
    "huggingface": OpenAICompatibleAdapter,
    "vllm": OpenAICompatibleAdapter,
    "lmstudio": OpenAICompatibleAdapter,
    "llamacpp": OpenAICompatibleAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
    "custom": OpenAICompatibleAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
    "ollama": OllamaAdapter,
    "echo": EchoAdapter,
}


@dataclass
class ProviderStatus:
    """Cached health snapshot for a provider."""

    name: str
    healthy: bool = False
    detail: str = ""
    checked_at: float = 0.0
    latency_ms: float = 0.0
    models: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "healthy": self.healthy, "detail": self.detail,
            "checked_at": iso() if not self.checked_at else self.checked_at,
            "latency_ms": round(self.latency_ms, 1), "models": self.models,
        }


class ProviderRegistry:
    """Holds adapters, discovers their models and remembers health."""

    def __init__(
        self,
        settings: Any = None,
        *,
        credential_provider: Callable[[str, str], dict[str, str]] | None = None,
        models: ModelRegistry | None = None,
        health_ttl: float = 90.0,
    ) -> None:
        self.settings = settings
        self.credential_provider = credential_provider
        self.models = models or get_model_registry()
        self.health_ttl = health_ttl
        self._adapters: dict[str, ProviderAdapter] = {}
        self._health: dict[str, ProviderStatus] = {}
        self._lock = threading.RLock()
        if settings is not None:
            self.build_from_settings(settings)

    # -- construction ---------------------------------------------------------- #
    def build_from_settings(self, settings: Any) -> None:
        for name, provider_settings in (getattr(settings, "providers", {}) or {}).items():
            if not getattr(provider_settings, "enabled", False):
                continue
            self.register_provider(name, provider_settings)
        # Always keep the offline provider available, but it never wins routing by default.
        if "echo" not in self._adapters:
            self._adapters["echo"] = EchoAdapter(None, credential_provider=self.credential_provider)

    def register_provider(self, name: str, provider_settings: Any = None, *, adapter: ProviderAdapter | None = None) -> ProviderAdapter:
        if adapter is None:
            adapter_class = ADAPTER_CLASSES.get(name, OpenAICompatibleAdapter)
            if adapter_class is OpenAICompatibleAdapter:
                adapter = OpenAICompatibleAdapter(
                    name, provider_settings, credential_provider=self.credential_provider,
                    label=name, timeout=float(getattr(provider_settings, "timeout_seconds", 120.0) or 120.0),
                )
            else:
                adapter = adapter_class(provider_settings, credential_provider=self.credential_provider)
        with self._lock:
            self._adapters[name] = adapter
        return adapter

    def register_adapter(self, adapter: ProviderAdapter) -> ProviderAdapter:
        with self._lock:
            self._adapters[adapter.name] = adapter
        return adapter

    # -- access ---------------------------------------------------------------- #
    def get(self, name: str) -> ProviderAdapter:
        with self._lock:
            adapter = self._adapters.get(name)
        if adapter is None:
            raise NotFoundError(f"provider {name!r} is not registered")
        return adapter

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._adapters

    def all(self) -> list[ProviderAdapter]:
        with self._lock:
            return list(self._adapters.values())

    def enabled(self) -> list[ProviderAdapter]:
        return [adapter for adapter in self.all() if adapter.enabled]

    def names(self) -> list[str]:
        return [adapter.name for adapter in self.all()]

    def describe(self) -> list[dict[str, Any]]:
        return [adapter.describe() for adapter in self.all()]

    # -- health ---------------------------------------------------------------- #
    async def check_health(self, name: str, *, force: bool = False) -> ProviderStatus:
        adapter = self.get(name)
        cached = self._health.get(name)
        if cached and not force and (time.time() - cached.checked_at) < self.health_ttl:
            return cached
        started = time.perf_counter()
        try:
            healthy, detail = await asyncio.wait_for(adapter.health(), timeout=min(adapter.timeout, 20.0))
        except asyncio.TimeoutError:
            healthy, detail = False, "health check timed out"
        except Exception as exc:
            healthy, detail = False, f"{type(exc).__name__}: {exc}"
        latency = (time.perf_counter() - started) * 1000
        status = ProviderStatus(name=name, healthy=healthy, detail=detail, checked_at=time.time(), latency_ms=latency)
        if healthy:
            try:
                descriptors = await adapter.list_models()
                self.models.register_many(self._to_model_info(adapter, descriptors))
                status.models = len(descriptors)
            except Exception as exc:
                status.detail = f"{detail} (model discovery failed: {exc})"
        with self._lock:
            self._health[name] = status
        return status

    async def check_all(self, *, force: bool = False) -> dict[str, ProviderStatus]:
        names = self.names()
        if not names:
            return {}
        results = await asyncio.gather(*(self.check_health(name, force=force) for name in names), return_exceptions=True)
        out: dict[str, ProviderStatus] = {}
        for name, result in zip(names, results):
            if isinstance(result, ProviderStatus):
                out[name] = result
            else:
                out[name] = ProviderStatus(name=name, healthy=False, detail=str(result), checked_at=time.time())
        return out

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {name: status.to_dict() for name, status in self._health.items()}

    def is_healthy(self, name: str) -> bool | None:
        status = self._health.get(name)
        return None if status is None else status.healthy

    def mark_unhealthy(self, name: str, reason: str) -> None:
        with self._lock:
            self._health[name] = ProviderStatus(name=name, healthy=False, detail=reason, checked_at=time.time())

    # -- discovery ------------------------------------------------------------- #
    async def discover_models(self, *, names: Iterable[str] | None = None) -> int:
        targets = list(names) if names else self.names()
        discovered = 0
        for name in targets:
            try:
                adapter = self.get(name)
                descriptors = await adapter.list_models()
                discovered += self.models.register_many(self._to_model_info(adapter, descriptors))
            except Exception:
                continue
        return discovered

    def models_with_capability(self, capability: Any) -> list[ModelInfo]:
        """Models (from any provider) that support a modality such as vision or audio."""
        try:
            return self.models.with_capability(capability)
        except Exception:
            return []

    def capabilities(self) -> set[Any]:
        """The union of capabilities offered by every known model."""
        found: set[Any] = set()
        for model in self.models.all():
            found.update(model.capabilities)
        return found

    def _to_model_info(self, adapter: ProviderAdapter, descriptors: Iterable[Any]) -> list[ModelInfo]:
        infos: list[ModelInfo] = []
        settings = adapter.settings
        for descriptor in descriptors:
            capabilities = {Capability.TEXT}
            if descriptor.supports_streaming:
                capabilities.add(Capability.STREAMING)
            if descriptor.supports_vision or adapter.supports_vision:
                capabilities.add(Capability.VISION)
            if descriptor.supports_tools:
                capabilities.add(Capability.TOOLS)
            if descriptor.supports_embeddings:
                capabilities.add(Capability.EMBEDDINGS)
            lowered = descriptor.id.lower()
            if any(tag in lowered for tag in ("coder", "code", "deepseek", "codestral")):
                capabilities.add(Capability.CODE)
            if any(tag in lowered for tag in ("o1", "o3", "qwq", "r1", "reason", "think")):
                capabilities.add(Capability.REASONING)
            infos.append(
                ModelInfo(
                    id=descriptor.id, provider=adapter.name, label=descriptor.id,
                    context_window=descriptor.context_window or 8192, capabilities=capabilities,
                    local=descriptor.local or adapter.local,
                    quality=descriptor.quality,
                    cost_per_1k_in=float(getattr(settings, "cost_per_1k_in", 0.0) or 0.0),
                    cost_per_1k_out=float(getattr(settings, "cost_per_1k_out", 0.0) or 0.0),
                    privacy_tier=adapter.privacy_tier,
                    metadata=descriptor.metadata,
                )
            )
        return infos


_REGISTRY: ProviderRegistry | None = None
_LOCK = threading.Lock()


def get_provider_registry(
    settings: Any = None,
    *,
    credential_provider: Callable[[str, str], dict[str, str]] | None = None,
    rebuild: bool = False,
) -> ProviderRegistry:
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = ProviderRegistry(settings, credential_provider=credential_provider)
        elif rebuild and settings is not None:
            _REGISTRY = ProviderRegistry(settings, credential_provider=credential_provider)
        return _REGISTRY


def reset_provider_registry() -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
