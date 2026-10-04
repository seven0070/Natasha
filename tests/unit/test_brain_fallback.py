"""Routing and fallback: the configured depth must be the depth that is used.

``brain.fallback_depth`` is exposed in the settings API, so a value a user sets has to change what
happens. These tests fail loudly if that setting drifts back into being decorative, and they check
the honest-failure half too: when every provider fails the error names each attempt.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from natasha.brain import BrainClient, ChatMessage, ChatResponse, CompletionRequest, ProviderRegistry
from natasha.brain.adapters.base import ModelDescriptor, ProviderAdapter, StreamChunk
from natasha.core.config import ProviderSettings, Settings
from natasha.core.errors import ProviderUnavailable


class _Flaky(ProviderAdapter):
    """A provider that fails a configurable number of times before answering."""

    supports_tools = False
    supports_streaming = False

    def __init__(self, name: str, settings: Any, *, fail: int = 0) -> None:
        super().__init__(settings)
        self.name = name
        self.default_model = f"{name}-1"
        self.fail = fail
        self.calls = 0

    async def list_models(self) -> list[ModelDescriptor]:
        return [ModelDescriptor(id=self.default_model, provider=self.name, local=True, quality=0.9,
                                context_window=32_000)]

    async def chat(self, messages: list[ChatMessage], *, model: str = "", **kwargs: Any) -> ChatResponse:
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError(f"{self.name} is down")
        return ChatResponse(content=f"answered by {self.name}", model=model or self.default_model,
                            provider=self.name, input_tokens=1, output_tokens=2, latency_ms=1.0)

    async def stream_chat(self, messages: list[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        response = await self.chat(messages, **kwargs)
        yield StreamChunk(delta=response.content, done=True, model=response.model, provider=self.name)


def _client(home, depths: dict[str, int], *, fails: dict[str, int] | None = None) -> BrainClient:
    settings = Settings()
    settings.providers = {name: ProviderSettings(enabled=True, local=True, privacy_tier="local")
                          for name in depths}
    settings.brain.fallback_depth = depths["__depth"]
    registry = ProviderRegistry(settings)
    for name in depths:
        if name == "__depth":
            continue
        adapter = _Flaky(name, settings.providers[name], fail=(fails or {}).get(name, 0))
        registry.register_adapter(adapter)
        asyncio.run(registry.discover_models(names=[name]))
    brain = BrainClient(providers=registry, settings=settings)
    return brain


def test_a_settings_change_changes_the_fallback_ladder(home):
    brain = _client(home, {"__depth": 3, "alpha": 0, "beta": 0})
    assert brain._fallback_depth() == 3
    brain.settings.brain.fallback_depth = 1
    assert brain._fallback_depth() == 1
    brain.settings.brain.fallback_depth = 99
    assert brain._fallback_depth() == 10, "an absurd depth is clamped, not honoured"


def test_depth_one_means_no_fallback(home):
    """The primary provider fails and, with depth 1, nothing else is tried."""
    brain = _client(home, {"__depth": 1, "alpha": 0, "beta": 0}, fails={"alpha": 1})
    with pytest.raises(ProviderUnavailable) as failure:
        asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("hi")])))
    assert "alpha" in str(failure.value)
    assert brain.providers.get("beta").calls == 0, "depth 1 must not reach the second provider"


def test_a_deeper_ladder_recovers_on_the_next_provider(home):
    brain = _client(home, {"__depth": 3, "alpha": 0, "beta": 0}, fails={"alpha": 1})
    response = asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("hi")])))
    assert response.content == "answered by beta"
    assert brain.providers.get("beta").calls == 1


def test_when_everything_fails_the_error_names_every_attempt(home):
    brain = _client(home, {"__depth": 2, "alpha": 0, "beta": 0}, fails={"alpha": 5, "beta": 5})
    with pytest.raises(ProviderUnavailable) as failure:
        asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("hi")])))
    message = str(failure.value)
    assert "alpha" in message and "beta" in message, message
    assert failure.value.details.get("attempts", 0) >= 2


def test_a_failed_provider_is_marked_unhealthy(home):
    brain = _client(home, {"__depth": 2, "alpha": 0, "beta": 0}, fails={"alpha": 5})
    asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("hi")])))
    assert brain.providers.is_healthy("alpha") is False
    assert "alpha" in {name for name, status in brain.providers.health_snapshot().items()
                       if status["healthy"] is False}
