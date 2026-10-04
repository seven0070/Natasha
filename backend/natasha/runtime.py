"""Composition root: builds every Natasha subsystem once and wires them together.

The API, CLI and desktop shell all talk to this object, so there is exactly one place where the
system's dependencies are connected - and one place to see what is actually running.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .core import get_paths, load_settings, run_coroutine_sync
from .core.risk import RiskLevel
from .events import EventKind, get_event_log
from .security.policy import PolicyEngine


@dataclass
class RuntimeState:
    """What is attached, and what failed to attach (honest startup report)."""

    degraded: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"attached": sorted(self.attached), "degraded": sorted(self.degraded)}


def _feature_enabled(settings: Any, name: str, default: bool = True) -> bool:
    """Feature flags arrive either as a model or as a plain dict (hand-written config)."""
    features = getattr(settings, "features", None)
    if features is None:
        return default
    if isinstance(features, dict):
        return bool(features.get(name, default))
    value = getattr(features, name, default)
    return bool(getattr(value, "value", value))


class NatashaRuntime:
    """Owns the live subsystem graph."""

    def __init__(self, *, settings: Any = None, home: str | None = None) -> None:
        self.settings = settings or load_settings(home)
        self.paths = get_paths().ensure()
        self.state = RuntimeState()
        self.log = get_event_log()
        self.policy = PolicyEngine(self.settings.security)
        self.approvals = None
        self.credentials = None
        self.broker = None
        self.memory = None
        self.world = None
        self.working = None
        self.voice_loop = None
        self.brain = None
        self.tools = None
        self.mcp = None
        self.skills = None
        self.skill_runtime = None
        self.marketplace = None
        self.missions = None
        self.supervisor = None
        self.agents = None
        self.verification = None
        self.recovery = None
        self.executive = None
        self.affect = None
        self.vision = None
        self.hearing = None
        self.voice = None
        self.computer = None
        self.browser = None
        self.creation = None
        self.integrations = None
        self.observability = None
        self._started = False

    # ------------------------------------------------------------------ build
    def _attach(self, name: str, factory: Any) -> Any:
        """Attach a subsystem, recording (not hiding) anything that fails to start."""
        try:
            component = factory()
        except Exception as exc:
            self.state.degraded.append(f"{name}: {type(exc).__name__}: {exc}")
            return None
        setattr(self, name, component)
        self.state.attached.append(name)
        return component

    def start(self) -> "NatashaRuntime":
        if self._started:
            return self
        from .approvals import get_approval_engine
        from .credentials import get_credential_manager
        from .credentials.broker import CredentialBroker
        from .memory import get_memory_store, get_working_memory
        from .world import get_world_model
        from .brain import get_brain
        from .tools import get_tool_registry
        from .tools.builtin import register_builtins
        from .verification import get_verification_engine
        from .recovery import get_recovery_engine

        # The approval engine signs tokens with the master key; credentials share that key.
        self.credentials = self._attach("credentials", lambda: get_credential_manager())
        self.approvals = self._attach("approvals", lambda: get_approval_engine())
        self.broker = self._attach(
            "broker",
            lambda: CredentialBroker(self.credentials, policy=self.policy, log=self.log,
                                     approvals=self.approvals),
        )
        self.memory = self._attach("memory", lambda: get_memory_store(log=self.log, policy=self.policy))
        self.world = self._attach("world", lambda: get_world_model(log=self.log))
        self.working = self._attach("working", lambda: get_working_memory())
        self.brain = self._attach("brain", lambda: get_brain(self.settings))
        if self.brain is not None:
            # Without this the router has *no* models to choose from and silently degrades to the
            # offline placeholder even when a local model is running. Discovery is bounded and
            # failures are recorded as degradation instead of being swallowed.
            try:
                discovered = run_coroutine_sync(self._discover_providers(), timeout=25)
                if discovered:
                    self.state.attached.append(f"brain.models({discovered})")
            except Exception as exc:
                self.state.degraded.append(f"brain.discovery: {type(exc).__name__}: {exc}")
        self.verification = self._attach("verification", lambda: get_verification_engine(log=self.log))
        self.recovery = self._attach("recovery", lambda: get_recovery_engine(log=self.log))

        def build_tools() -> Any:
            registry = get_tool_registry(policy=self.policy, log=self.log, approvals=self.approvals)
            register_builtins(registry)
            return registry

        self.tools = self._attach("tools", build_tools)

        from .missions import get_mission_engine, register_default_checks
        from .missions.supervisor import get_supervisor
        from .agents import get_agent_team

        def build_missions() -> Any:
            engine = get_mission_engine(tools=self.tools, verification=self.verification,
                                        recovery=self.recovery, log=self.log, approvals=self.approvals,
                                        tool_context=self._tool_context())
            # A mission's verification plan may only name checks that are registered here.
            register_default_checks(engine)
            return engine

        self.missions = self._attach("missions", build_missions)
        self.supervisor = self._attach("supervisor", lambda: get_supervisor(tools=self.tools,
                                                                           missions=self.missions, log=self.log))
        # Model steps and delegation inside a mission must go through the same brain and the same
        # supervisor as everything else - otherwise a mission could quietly call a model directly.
        if self.missions is not None and self.brain is not None:
            try:
                self.missions.set_model_runner(self._mission_model_runner())
            except Exception as exc:
                self.state.degraded.append(f"missions.model_runner: {type(exc).__name__}: {exc}")
        if self.missions is not None and self.supervisor is not None:
            try:
                self.missions.set_delegate(self._mission_delegate())
            except Exception as exc:
                self.state.degraded.append(f"missions.delegate: {type(exc).__name__}: {exc}")
        self.agents = self._attach("agents", lambda: get_agent_team(missions=self.missions,
                                                                    supervisor=self.supervisor,
                                                                    tools=self.tools, brain=self.brain, log=self.log))
        if self.agents is not None:
            try:
                self.state.attached.append(f"agents.roles({len(self.agents.install_roles())})")
            except Exception as exc:
                self.state.degraded.append(f"agents.roles: {type(exc).__name__}: {exc}")

        from .executive import get_executive

        self.executive = self._attach(
            "executive",
            lambda: get_executive(brain=self.brain, tools=self.tools, memory=self.memory, world=self.world,
                                  working=self.working, missions=self.missions, supervisor=self.supervisor,
                                  verification=self.verification, recovery=self.recovery,
                                  approvals=self.approvals, log=self.log, affect=self.affect),
        )
        self._attach_optional()
        self._started = True
        self.log.append("system", {"action": "runtime_started", "state": self.state.to_dict()},
                        actor="system", source="runtime")
        return self

    # -- provider discovery ---------------------------------------------------- #
    async def _discover_providers(self) -> int:
        """Health-check every *enabled* provider and register the models it reports."""
        import asyncio

        registry = self.brain.providers
        targets = [adapter.name for adapter in registry.enabled()]
        if not targets:
            return 0
        await asyncio.gather(*(registry.check_health(name) for name in targets),
                             return_exceptions=True)
        healthy = [name for name in targets if registry.is_healthy(name) is not False] or targets
        return await registry.discover_models(names=healthy)

    # -- mission wiring -------------------------------------------------------- #
    def _mission_model_runner(self) -> Any:
        """Run a mission's MODEL step through the normal brain (routing, fallback, usage, audit)."""

        async def runner(mission: Any, step: Any, context: Any) -> dict[str, Any]:
            from .brain import ChatMessage, CompletionRequest

            payload = dict(getattr(step, "payload", {}) or {})
            prompt = str(payload.get("prompt") or payload.get("task") or step.description
                         or getattr(mission, "objective", ""))
            task = str(payload.get("task_kind") or "reasoning")
            response = await self.brain.complete(
                CompletionRequest(
                    messages=[
                        ChatMessage.system(
                            "You are executing one step of a Natasha mission. Answer for this step "
                            "only, and state plainly if you cannot."
                        ),
                        ChatMessage.user(prompt),
                    ],
                    task=task,
                    actor=getattr(context, "actor", "model:main") or "model:main",
                    trace_id=getattr(mission, "trace_id", "") or "",
                    mission_id=getattr(mission, "id", "") or "",
                    model=str(payload.get("model") or ""),
                    provider=str(payload.get("provider") or ""),
                    max_tokens=payload.get("max_tokens"),
                )
            )
            return {
                "text": response.content,
                "summary": response.content.strip()[:1000] or f"step {step.id} produced model text",
                "model": response.model,
                "provider": response.provider,
                "tokens": response.total_tokens,
                "criteria_covered": list(payload.get("criteria") or []),
            }

        return runner

    def _mission_delegate(self) -> Any:
        """Run a mission's DELEGATE step through the supervisor, which owns worker containment."""

        async def delegate(objective: str, payload: dict[str, Any], context: Any) -> Any:
            role = str(payload.get("role") or "researcher")
            result = await self.supervisor.delegate(
                role=role, objective=objective, context=payload,
                actor=getattr(context, "actor", "model:main") or "model:main",
            )
            return result.to_dict() if hasattr(result, "to_dict") else result

        return delegate

    def _tool_context(self) -> Any:
        from .tools.base import ToolContext

        return ToolContext(policy=self.policy, memory=self.memory, brain=self.brain, world=self.world,
                           broker=self.broker, approvals=self.approvals, workspace=self.paths.workspace)

    def _attach_optional(self) -> None:
        """Optional subsystems: absence degrades features, never the whole app."""
        from .observability import get_health_monitor

        self.observability = self._attach("observability", lambda: get_health_monitor(log=self.log))

        def build_affect() -> Any:
            from .affect import get_affect_engine

            return get_affect_engine(log=self.log)

        self.affect = self._attach("affect", build_affect)
        if self.executive is not None and self.affect is not None:
            self.executive.affect = self.affect

        def build_mcp() -> Any:
            from .mcp import get_mcp_registry

            return get_mcp_registry(tools=self.tools, log=self.log, approvals=self.approvals)

        self.mcp = self._attach("mcp", build_mcp)

        def build_skills() -> Any:
            from .skills import get_skill_lifecycle, get_skill_runtime

            lifecycle = get_skill_lifecycle(log=self.log)
            runtime = get_skill_runtime(lifecycle=lifecycle, policy=self.policy, tools=self.tools,
                                        approvals=self.approvals)
            runtime.register_tools()
            self.skill_runtime = runtime
            return lifecycle

        self.skills = self._attach("skills", build_skills)

        def build_marketplace() -> Any:
            from .marketplace import get_marketplace_installer

            return get_marketplace_installer(policy=self.policy, log=self.log, lifecycle=self.skills,
                                             tools=self.tools, approvals=self.approvals)

        self.marketplace = self._attach("marketplace", build_marketplace)

        def build_integrations() -> Any:
            from .integrations import default_connectors, get_integration_registry

            registry = get_integration_registry(broker=self.broker)
            for connector in default_connectors():
                connector.broker = self.broker
                registry.register(connector)
            registry.register_tools(self.tools)
            return registry

        self.integrations = self._attach("integrations", build_integrations)

        # NOTE: the perception, voice and computer engines are constructed *per runtime* instead of
        # going through the module-level caches. A cached engine would keep a reference to the
        # previous runtime's brain, memory store and event log, so a restart (or a second runtime in
        # one process, as the test-suite does) would quietly use a closed database. The runtime owns
        # exactly one of each, so it builds exactly one of each.
        def build_perception() -> Any:
            from .perception.vision import VisionEngine

            return VisionEngine(brain=self.brain)

        self.vision = self._attach("vision", build_perception)

        def build_hearing() -> Any:
            from .perception.hearing import HearingEngine

            return HearingEngine(brain=self.brain,
                                 default_language=getattr(self.settings.voice, "language", "en"))

        self.hearing = self._attach("hearing", build_hearing)

        def build_computer() -> Any:
            from .computer import get_computer_controller

            return get_computer_controller(policy=self.policy, approvals=self.approvals, log=self.log,
                                          tools=self.tools)

        self.computer = self._attach("computer", build_computer)

        def build_browser() -> Any:
            from .computer import get_browser_controller

            return get_browser_controller(policy=self.policy, approvals=self.approvals, log=self.log,
                                         tools=self.tools, artifacts=self.paths.artifacts)

        self.browser = self._attach("browser", build_browser)
        if self.tools is not None and (self.computer is not None or self.browser is not None):
            def register_control_tools() -> Any:
                from .computer.tools import register_computer_tools

                return register_computer_tools(self.tools, self.computer or _null_controller(),
                                               self.browser)

            self._attach("computer.tools", register_control_tools)

        def build_creation() -> Any:
            from .creation import get_creation_engine

            return get_creation_engine(brain=self.brain, tools=self.tools, memory=self.memory,
                                       artifacts=self.paths.artifacts, log=self.log)

        self.creation = self._attach("creation", build_creation)

        def build_voice() -> Any:
            from .voice.engine import VoiceEngine

            return VoiceEngine(brain=self.brain, hearing=self.hearing, log=self.log,
                               broker=self.broker, approvals=self.approvals,
                               artifacts=self.paths.artifacts, settings=self.settings)

        self.voice = self._attach("voice", build_voice)

        def build_voice_loop() -> Any:
            from .voice.conversation import VoiceLoop
            from .voice.wakeword import get_wakeword_detector

            voice_settings = getattr(self.settings, "voice", None)
            wake = get_wakeword_detector(
                phrases=tuple(getattr(voice_settings, "wake_phrases", ()) or ()),
                fuzzy=int(getattr(voice_settings, "wake_fuzzy", 1) or 0),
                log=self.log) if _feature_enabled(self.settings, "voice") else None
            return VoiceLoop(voice=self.voice, hearing=self.hearing, executive=self.executive,
                             computer=self.computer, log=self.log, wakewords=wake)

        if _feature_enabled(self.settings, "voice"):
            self.voice_loop = self._attach("voice_loop", build_voice_loop)

    # ------------------------------------------------------------------ introspection
    def status(self) -> dict[str, Any]:
        return {
            "home": str(self.paths.home),
            "started": self._started,
            "state": self.state.to_dict(),
            "settings": {
                "brain": {"prefer_local": getattr(self.settings.brain, "prefer_local", True),
                          "providers": sorted(getattr(self.settings, "providers", {}) or {})},
                "security": {"default_effect": self.settings.security.default_effect,
                             "require_approval_for": self.settings.security.require_approval_for},
            },
            "tools": self.tools.names() if self.tools is not None else [],
        }

    def health(self) -> dict[str, Any]:
        checks: dict[str, Any] = {"runtime": {"ok": self._started, "degraded": self.state.degraded}}
        if self.brain is not None:
            try:
                checks["providers"] = self.brain.providers.health_snapshot()
            except Exception as exc:
                checks["providers"] = {"error": str(exc)}
        if self.memory is not None:
            checks["memory"] = self.memory.stats()
        if self.missions is not None:
            checks["missions"] = self.missions.stats()
        if self.observability is not None:
            try:
                checks["observability"] = self.observability.snapshot()
            except Exception:
                pass
        return checks

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply a nested settings update in memory (persisting is a separate, explicit call)."""
        applied: dict[str, Any] = {}
        for key, value in (updates or {}).items():
            if not hasattr(self.settings, key):
                raise KeyError(f"unknown setting {key!r}")
            current = getattr(self.settings, key)
            if isinstance(value, dict) and not isinstance(current, dict) and hasattr(current, "__dataclass_fields__"):
                for inner_key, inner_value in value.items():
                    if not hasattr(current, inner_key):
                        raise KeyError(f"unknown setting {key}.{inner_key}")
                    setattr(current, inner_key, inner_value)
                    applied[f"{key}.{inner_key}"] = inner_value
            else:
                setattr(self.settings, key, value)
                applied[key] = value
        self.log.append(EventKind.SYSTEM, {"action": "settings_updated", "keys": sorted(applied)},
                        actor="owner", source="runtime", risk=RiskLevel.MEDIUM)
        return applied

    def shutdown(self) -> None:
        for component in (self.mcp, self.skill_runtime):
            close = getattr(component, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    from .core import run_coroutine_sync

                    run_coroutine_sync(result)
            except Exception:
                pass
        try:
            self.log.close()
        except Exception:
            pass
        self._started = False


def _null_controller() -> Any:
    """A controller with no backend, used only to keep tool names stable when the desktop is absent."""
    from .computer.controller import ComputerController, NullBackend

    return ComputerController(backend=NullBackend())


_RUNTIME: NatashaRuntime | None = None
_LOCK = threading.Lock()


def get_runtime(**kwargs: Any) -> NatashaRuntime:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is None:
            _RUNTIME = NatashaRuntime(**kwargs).start()
        return _RUNTIME


def reset_runtime() -> None:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            try:
                _RUNTIME.shutdown()
            except Exception:
                pass
        _RUNTIME = None
