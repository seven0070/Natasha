"""Test-time process hygiene: one call that drops every process-wide singleton.

Reset hooks live next to the cache they clear (``natasha.tools.registry`` rather than
``natasha.tools``), so resolution walks the package tree instead of doing a flat lookup, and any
hook that cannot be found or raises is recorded in :data:`RESET_PROBLEMS` - never swallowed.
``tests/unit/test_isolation.py`` asserts that list is empty.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from pathlib import Path
from typing import Any

#: Reset hooks that could not be resolved, or that raised while running.
RESET_PROBLEMS: list[str] = []

RESETTERS: list[tuple[str, str]] = [
    ("natasha.events", "reset_event_logs"),
    ("natasha.memory", "reset_memory_store"),
    ("natasha.memory.working", "reset_working_memory"),
    ("natasha.world", "reset_world_model"),
    ("natasha.tools", "reset_tool_registry"),
    ("natasha.brain", "reset_brain"),
    ("natasha.brain", "reset_model_registry"),
    ("natasha.brain", "reset_provider_registry"),
    ("natasha.brain", "reset_usage_tracker"),
    ("natasha.approvals", "reset_approval_engines"),
    ("natasha.credentials", "reset_credential_managers"),
    ("natasha.credentials.broker", "reset_brokers"),
    ("natasha.missions", "reset_mission_engine"),
    ("natasha.missions.store", "reset_mission_store"),
    ("natasha.missions.supervisor", "reset_supervisor"),
    ("natasha.verification", "reset_verification_engine"),
    ("natasha.recovery", "reset_recovery_engine"),
    ("natasha.skills", "reset_skill_lifecycle"),
    ("natasha.skills.runtime", "reset_skill_runtime"),
    ("natasha.marketplace", "reset_marketplace_installer"),
    ("natasha.marketplace.registry", "reset_marketplace_registry"),
    ("natasha.mcp", "reset_mcp_registry"),
    ("natasha.executive", "reset_executive"),
    ("natasha.agents", "reset_agent_team"),
    ("natasha.affect", "reset_affect_engine"),
    ("natasha.observability", "reset_metrics"),
    ("natasha.observability.health", "reset_health_monitor"),
    ("natasha.observability.tracing", "reset_tracer"),
    ("natasha.integrations", "reset_integration_registry"),
    ("natasha.api.auth", "reset_auth_manager"),
    ("natasha.db.migrations", "reset_migration_runner"),
    ("natasha.governance.constitution", "reset_constitutions"),
    ("natasha.governance.upgrade_governor", "reset_upgrade_governors"),
    ("natasha.perception", "reset_vision_engine"),
    ("natasha.perception", "reset_hearing_engine"),
    ("natasha.perception", "reset_document_reader"),
    ("natasha.voice", "reset_voice_engine"),
    ("natasha.voice", "reset_voice_loop"),
    ("natasha.voice.wakeword", "reset_wakeword_detector"),
    ("natasha.computer", "reset_computer_controller"),
    ("natasha.computer", "reset_browser_controller"),
]


def reset_everything(home: Path | None = None) -> None:
    """Drop every process-wide singleton so the next construction reads the new environment."""
    from natasha import runtime as runtime_module

    try:
        runtime_module.reset_runtime()
    except Exception:
        pass

    from natasha.core import reset_paths_cache

    if home is not None:
        os.environ["NATASHA_HOME"] = str(home)
    reset_paths_cache()

    for module_name, function_name in RESETTERS:
        function = _find_resetter(module_name, function_name)
        if function is None:
            # Never silently skip: a missed reset leaks state between tests, which is exactly the
            # kind of cross-test contamination that hides real bugs. tests/unit/test_isolation.py
            # asserts this list is empty.
            if function_name not in RESET_PROBLEMS:
                RESET_PROBLEMS.append(f"{module_name}.{function_name}")
            continue
        try:
            function()
        except Exception as exc:  # pragma: no cover - a failing resetter is reported by the test
            RESET_PROBLEMS.append(f"{module_name}.{function_name} raised {type(exc).__name__}: {exc}")


def _find_resetter(module_name: str, function_name: str) -> Any:
    """Find ``function_name`` in ``module_name`` or any of its submodules.

    Reset hooks live next to the cache they clear (``natasha.tools.registry`` rather than
    ``natasha.tools``), so a flat lookup would quietly miss them.
    """
    module = importlib.import_module(module_name)
    direct = getattr(module, function_name, None)
    if callable(direct):
        return direct
    search_path = getattr(module, "__path__", None)
    if search_path is None:
        return None
    for info in pkgutil.walk_packages(search_path, prefix=f"{module_name}."):
        try:
            submodule = importlib.import_module(info.name)
        except Exception:
            continue
        candidate = getattr(submodule, function_name, None)
        if callable(candidate):
            return candidate
    return None


# --------------------------------------------------------------------------- scripted runtime
#: The deterministic provider every integration/e2e test drives the runtime with. It is built
#: lazily so importing this module (which tests/conftest.py does before natasha is importable)
#: never imports natasha itself.
def scripted_provider_class() -> type:
    """Return the ScriptedProvider class, importing natasha on first use."""
    import asyncio
    from typing import Any, AsyncIterator

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
            response = await self.chat(messages, model=model, temperature=temperature,
                                       max_tokens=max_tokens)
            for index, word in enumerate(response.content.split(" ")):
                await asyncio.sleep(0)
                yield StreamChunk(delta=("" if index == 0 else " ") + word, model=response.model,
                                  provider=self.name)
            # Tool calls ride on the final chunk, the way real streaming APIs deliver them; a
            # streaming turn without this could never reach the tool/policy/approval stages.
            yield StreamChunk(done=True, model=response.model, provider=self.name,
                              finish_reason=response.finish_reason,
                              tool_calls=list(response.tool_calls),
                              input_tokens=response.input_tokens,
                              output_tokens=response.output_tokens)

    return ScriptedProvider


def build_scripted_runtime(home: Any) -> tuple[Any, Any]:
    """Start a real runtime whose only *viable* provider is the scripted one.

    Returns ``(runtime, adapter)``. Model discovery has to be re-run after registering the adapter,
    or the router has no model to choose.
    """
    import asyncio

    from natasha.runtime import NatashaRuntime

    instance = NatashaRuntime(home=str(home)).start()
    adapter = scripted_provider_class()(credential_provider=None)
    instance.brain.providers.register_adapter(adapter)
    for other in instance.brain.providers.all():
        if other.name != "scripted":
            other.settings = type("S", (), {"enabled": False})()
    asyncio.run(instance.brain.providers.discover_models(names=["scripted"]))
    return instance, adapter
