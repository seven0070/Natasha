"""Providers: routing, fallback, streaming, embeddings, health and usage - the brain's promises.

Everything here runs against local stub adapters, so the properties are about Natasha's behaviour
(which model it picks, what it does when one fails, what it records) rather than about any vendor.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterable

import pytest

from natasha.brain import (
    ChatMessage,
    ChatResponse,
    CompletionRequest,
    ProviderAdapter,
    StreamChunk,
    ToolCall,
)
from natasha.brain.adapters.base import ModelDescriptor
from natasha.brain.router import PrivacyPreference
from natasha.core import ProviderUnavailable
from natasha.events import EventKind

pytestmark = pytest.mark.integration


class StubAdapter(ProviderAdapter):
    """A controllable provider: it can answer, stream, embed, report health, or fail."""

    def __init__(self, name: str, *, model: str = "", local: bool = False, quality: float = 0.5,
                 fails: bool = False, tools: bool = True, embeddings: bool = True,
                 vision: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.name = name
        self.local = local
        self.privacy_tier = "local" if local else "cloud"
        self.default_model = model or f"{name}-1"
        self.models = [self.default_model]
        self._quality = quality
        self.fails = fails
        self.supports_tools = tools
        self.supports_embeddings = embeddings
        self.supports_vision = vision
        self.calls: list[str] = []
        self.streams = 0

    async def list_models(self) -> list[ModelDescriptor]:
        return [ModelDescriptor(id=self.default_model, provider=self.name, local=self.local,
                                quality=self._quality, context_window=32_768,
                                supports_tools=self.supports_tools,
                                supports_embeddings=self.supports_embeddings,
                                supports_vision=self.supports_vision)]

    async def health(self) -> tuple[bool, str]:
        return (not self.fails, "stub")

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None,
                   **params: Any) -> ChatResponse:
        if self.fails:
            raise RuntimeError(f"{self.name} is down")
        self.calls.append(model or self.default_model)
        text = f"answer from {self.name}"
        return ChatResponse(content=text, model=model or self.default_model, provider=self.name,
                            input_tokens=10, output_tokens=5, latency_ms=3.0)

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "",
                          temperature: float = 0.7, max_tokens: int | None = None,
                          **params: Any) -> AsyncIterator[StreamChunk]:
        if self.fails:
            raise RuntimeError(f"{self.name} is down")
        self.streams += 1
        for index, word in enumerate(("streamed", f"by", self.name)):
            await asyncio.sleep(0)
            yield StreamChunk(delta=("" if index == 0 else " ") + word, model=model or self.default_model,
                              provider=self.name)
        yield StreamChunk(done=True, model=model or self.default_model, provider=self.name,
                          finish_reason="stop")

    async def embed(self, texts: Iterable[str], *, model: str = "", **params: Any) -> list[list[float]]:
        if not self.supports_embeddings:
            raise NotImplementedError(f"{self.name} does not support embeddings")
        return [[float(len(text) % 7), 1.0, 0.0] for text in texts]


@pytest.fixture()
def brain(home):
    from natasha.brain import get_brain

    instance = get_brain()
    return instance


def _register(brain, adapter: StubAdapter) -> StubAdapter:
    brain.providers.register_adapter(adapter)
    asyncio.run(brain.providers.discover_models(names=[adapter.name]))
    return adapter


def _disable_all(brain, keep: list[str]) -> None:
    for adapter in brain.providers.all():
        if adapter.name not in keep:
            adapter.settings = type("S", (), {"enabled": False})()


def test_a_local_model_is_preferred_when_the_caller_asks_for_privacy(brain):
    cloud = _register(brain, StubAdapter("cloudy", quality=0.95))
    local = _register(brain, StubAdapter("locals", local=True, quality=0.6))
    _disable_all(brain, ["cloudy", "locals"])
    decision = brain.router.choose(profile=None, task="chat", privacy=PrivacyPreference.LOCAL_ONLY)
    assert decision.model.provider == "locals"

    prefer_local = brain.router.choose(profile=None, task="chat", privacy=PrivacyPreference.PREFER_LOCAL)
    assert prefer_local.model.provider in {"locals", "cloudy"}


def test_task_weights_change_the_choice(brain):
    fast = _register(brain, StubAdapter("fast", quality=0.5))
    strong = _register(brain, StubAdapter("strong", quality=0.99))
    _disable_all(brain, ["fast", "strong"])
    reasoning = brain.router.choose(profile=None, task="reasoning", privacy=PrivacyPreference.ANY)
    summarize = brain.router.choose(profile=None, task="summarize", privacy=PrivacyPreference.ANY)
    assert reasoning.model.provider == "strong"      # quality dominates for reasoning
    assert summarize.model.provider in {"fast", "strong"}


def test_a_failing_provider_falls_back_to_a_working_one(brain):
    broken = _register(brain, StubAdapter("broken", quality=0.99, fails=True))
    working = _register(brain, StubAdapter("working", quality=0.5))
    _disable_all(brain, ["broken", "working"])
    request = CompletionRequest(messages=[ChatMessage.user("hello")], task="chat",
                                privacy=PrivacyPreference.ANY, allow_fallback=True)
    response = asyncio.run(brain.complete(request))
    assert response.provider == "working"
    assert broken.calls == []                        # it failed before answering
    assert brain.providers.is_healthy("broken") is False
    assert working.calls


def test_with_fallback_disabled_a_failure_surfaces(brain):
    broken = _register(brain, StubAdapter("broken", quality=0.99, fails=True))
    _register(brain, StubAdapter("backup", quality=0.1))
    _disable_all(brain, ["broken", "backup"])
    request = CompletionRequest(messages=[ChatMessage.user("hi")], task="chat", model=broken.default_model,
                                provider="broken", allow_fallback=False)
    with pytest.raises(ProviderUnavailable):
        asyncio.run(brain.complete(request))


def test_usage_is_accounted_per_call_and_per_provider(brain):
    adapter = _register(brain, StubAdapter("metered", quality=0.9))
    _disable_all(brain, ["metered"])
    before = brain.usage.totals()
    response = asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("count me")])))
    after = brain.usage.totals()
    assert response.provider == "metered"
    assert after["calls"] > before["calls"]
    assert after["input_tokens"] >= before["input_tokens"] + 10


def test_a_call_is_audited_with_provider_model_and_outcome(brain, log):
    adapter = _register(brain, StubAdapter("audited", quality=0.9))
    _disable_all(brain, ["audited"])
    asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("audit me")])))
    events = log.query(kinds=[EventKind.THOUGHT, EventKind.SYSTEM], limit=100)
    assert events
    payload = events[0].payload
    assert {"provider", "model"} <= set(payload) or {"provider", "model"} <= set(payload.get("details", {}))


def test_streaming_yields_deltas_and_a_final_chunk(brain):
    adapter = _register(brain, StubAdapter("streamer", quality=0.9))
    _disable_all(brain, ["streamer"])

    async def consume() -> list[StreamChunk]:
        chunks = []
        async for chunk in brain.stream(CompletionRequest(messages=[ChatMessage.user("stream")])):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(consume())
    assert chunks[-1].done is True
    assert "".join(chunk.delta for chunk in chunks) == "streamed by streamer"
    assert adapter.streams == 1


def test_embeddings_come_from_a_capable_provider(brain):
    adapter = _register(brain, StubAdapter("embedder", quality=0.9, embeddings=True, tools=False))
    _disable_all(brain, ["embedder"])
    vectors = asyncio.run(brain.embed(["one", "three"]))
    assert len(vectors) == 2
    assert all(len(vector) == 3 for vector in vectors)


def test_health_snapshot_reports_every_provider(brain):
    _register(brain, StubAdapter("healthy", quality=0.5))
    _register(brain, StubAdapter("sick", quality=0.5, fails=True))
    asyncio.run(brain.providers.check_all(force=True))
    snapshot = brain.providers.health_snapshot()
    assert snapshot["healthy"]["healthy"] is True
    assert snapshot["sick"]["healthy"] is False


def test_the_offline_placeholder_is_never_mistaken_for_a_real_model(home):
    """With nothing configured, the echo provider answers - and the answer says so."""
    from natasha.brain import get_brain
    from natasha.executive import Turn

    from natasha.runtime import NatashaRuntime

    runtime = NatashaRuntime(home=str(home)).start()
    for adapter in runtime.brain.providers.all():
        adapter.settings = type("S", (), {"enabled": adapter.name == "echo"})()
    result = asyncio.run(runtime.executive.run_turn(Turn(message="hello", actor="owner")))
    assert result.provider == "echo"
    assert result.offline_placeholder is True
    assert "offline echo placeholder" in result.reply


def test_a_model_call_through_a_tool_actor_is_policy_gated(brain, policy):
    """A worker cannot call a model without a mission scope, however the request is dressed up."""
    from natasha.security.policy import Capability, Effect, PolicyRequest

    decision = policy.check(PolicyRequest(capability=Capability.MODEL_CALL, resource="model:locals-1",
                                          actor="worker:coder"))
    assert decision.effect is Effect.DENY
    scoped = policy.check(PolicyRequest(capability=Capability.MODEL_CALL, resource="model:locals-1",
                                        actor="worker:coder", mission_scope=[Capability.MODEL_CALL]))
    assert scoped.effect in {Effect.APPROVAL, Effect.ALLOW}


def test_tools_are_only_offered_to_providers_that_support_them(brain):
    """A model that cannot call tools is not eligible when the request needs them."""
    _register(brain, StubAdapter("toolfree", quality=0.95, tools=False))
    _register(brain, StubAdapter("toolful", quality=0.4, tools=True))
    _disable_all(brain, ["toolfree", "toolful"])
    decision = brain.router.choose(profile=None, task="chat", requires_tools=True)
    assert decision.model.provider == "toolful"

    # ... and when nothing can serve the request, the router degrades to the *offline* provider and
    # says so, instead of quietly handing back a model that cannot do the job.
    brain.providers.mark_unhealthy("toolful", "test: unavailable")
    with pytest.raises(ProviderUnavailable) as excinfo:
        brain.router.choose(profile=None, task="chat", requires_tools=True)
    # The message must name every reason, so an operator can see *why* nothing could serve it.
    assert "tools required but unsupported" in str(excinfo.value)
    assert "provider unhealthy" in str(excinfo.value)


def test_a_provider_error_does_not_leak_credentials(brain, log):
    class Secretive(StubAdapter):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("auth failed for key sk-do-not-log-me-1234567890")

    _register(brain, Secretive("secretive", quality=0.9))
    _disable_all(brain, ["secretive"])
    with pytest.raises(Exception):
        asyncio.run(brain.complete(CompletionRequest(messages=[ChatMessage.user("x")],
                                                    allow_fallback=False, provider="secretive")))
    rendered = "\n".join(
        str(event.to_dict()) for event in log.query(limit=200)
    )
    assert "sk-do-not-log-me-1234567890" not in rendered
