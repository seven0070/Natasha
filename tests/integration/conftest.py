"""Shared integration fixtures: a real runtime driven by a deterministic scripted provider.

Tests in this directory exercise the wiring between subsystems, so they use the *real* runtime,
database, event log, policy engine and tool registry - only the model provider is scripted, so a
turn is reproducible instead of depending on a network service.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import pytest

from natasha.brain import ChatResponse, ToolCall
from natasha.brain.adapters.base import ChatMessage, ModelDescriptor, ProviderAdapter, StreamChunk


class ScriptedProvider(ProviderAdapter):
    """Plays a script of (content, tool_calls) steps and records every request it receives."""

    name = "scripted"
    local = True
    privacy_tier = "local"
    supports_tools = True
    supports_streaming = True
    default_model = "scripted-1"
    models = ["scripted-1"]

    def __init__(self, script: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.script = list(script or [])
        self.requests: list[list[ChatMessage]] = []

    async def list_models(self) -> list[ModelDescriptor]:
        return [ModelDescriptor(id="scripted-1", provider=self.name, local=True, quality=0.99,
                                context_window=64_000, supports_tools=True, supports_vision=True,
                                supports_embeddings=True)]

    def _next(self, model: str) -> ChatResponse:
        step = self.script.pop(0) if self.script else {"content": "Nothing further to do."}
        calls = [ToolCall(id=f"call_{index}", name=item["name"],
                          arguments=dict(item.get("arguments") or {}))
                 for index, item in enumerate(step.get("tool_calls") or [])]
        content = str(step.get("content", ""))
        return ChatResponse(
            content=content, model=model or self.default_model, provider=self.name,
            finish_reason="tool_calls" if calls else "stop", tool_calls=calls,
            input_tokens=sum(len(message.content.split()) for message in self.requests[-1])
            if self.requests else 0,
            output_tokens=len(content.split()), latency_ms=2.0,
        )

    async def chat(self, messages: list[ChatMessage], *, model: str = "", temperature: float = 0.7,
                   max_tokens: int | None = None, tools: list[dict[str, Any]] | None = None,
                   **params: Any) -> ChatResponse:
        self.requests.append(messages)
        return self._next(model)

    async def stream_chat(self, messages: list[ChatMessage], *, model: str = "",
                          temperature: float = 0.7, max_tokens: int | None = None,
                          **params: Any) -> AsyncIterator[StreamChunk]:
        response = await self.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens)
        for index, word in enumerate(response.content.split(" ")):
            await asyncio.sleep(0)
            yield StreamChunk(delta=("" if index == 0 else " ") + word, model=response.model,
                              provider=self.name)
        yield StreamChunk(done=True, model=response.model, provider=self.name, finish_reason="stop")


@pytest.fixture()
def scripted():
    """A standalone scripted provider (for tests that bring their own brain)."""
    return ScriptedProvider(credential_provider=None)


@pytest.fixture()
def runtime(home):
    """A real runtime whose only *viable* provider is the scripted one."""
    from natasha.runtime import NatashaRuntime

    instance = NatashaRuntime(home=str(home)).start()
    adapter = ScriptedProvider(credential_provider=None)
    instance.brain.providers.register_adapter(adapter)
    for other in instance.brain.providers.all():
        if other.name != "scripted":
            other.settings = type("S", (), {"enabled": False})()
    # Model discovery is what the runtime does at startup; registering an adapter afterwards has to
    # do it explicitly, or the router has no model to choose.
    asyncio.run(instance.brain.providers.discover_models(names=["scripted"]))
    return instance, adapter


@pytest.fixture()
def rt(runtime):
    """The runtime alone, when the script does not matter."""
    return runtime[0]


@pytest.fixture()
def client(runtime):
    """A TestClient over the real app with the scripted runtime injected (no UI mounted)."""
    from fastapi.testclient import TestClient

    from natasha.api.app import create_app

    runtime, adapter = runtime
    app = create_app(runtime=runtime, serve_ui=False)
    with TestClient(app) as test_client:
        test_client.app.state.runtime = runtime
        test_client.adapter = adapter
        yield test_client


@pytest.fixture()
def ui_client(runtime):
    """The same app, but serving the console from ``frontend/`` as well."""
    from fastapi.testclient import TestClient

    from natasha.api.app import create_app

    runtime, adapter = runtime
    app = create_app(runtime=runtime, serve_ui=True)
    with TestClient(app) as test_client:
        test_client.app.state.runtime = runtime
        test_client.adapter = adapter
        yield test_client


@pytest.fixture()
def owner(client):
    """Set up the owner, log in, and hand back an authenticated client."""
    passphrase = "correct-horse-battery-staple"
    status = client.get("/api/auth/status").json()
    if not status.get("initialised"):
        created = client.post("/api/auth/setup",
                             json={"passphrase": passphrase, "owner_id": "owner"})
        assert created.status_code in (200, 201), created.text
    login = client.post("/api/auth/login", json={"passphrase": passphrase, "client": "test"})
    assert login.status_code == 200, login.text
    client.headers.update({"X-Natasha-Token": login.json()["token"]})
    return client
