"""Skill execution: expose active skills as tools, gated by their declared permissions."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import SkillError
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log
from ..security.policy import Capability
from ..security.validation import Schema
from .isolation import SkillSandbox, run_isolated
from .lifecycle import SkillLifecycle, SkillState, get_skill_lifecycle
from .manifest import SkillManifest

#: Skill permission -> policy capability that must be granted for the skill to run.
PERMISSION_TO_CAPABILITY = {
    "fs.read": Capability.FS_READ,
    "fs.write": Capability.FS_WRITE,
    "net.http": Capability.NET_HTTP,
    "memory.read": Capability.MEMORY_READ,
    "memory.write": Capability.MEMORY_WRITE,
    "artifact.write": Capability.ARTIFACT_WRITE,
    "model.call": Capability.MODEL_CALL,
    "shell.exec": Capability.SHELL_EXEC,
    "code.exec": Capability.CODE_EXEC,
    "credential.use": Capability.CREDENTIAL_USE,
    "browser.control": Capability.BROWSER_CONTROL,
}

#: Permissions that always need explicit owner approval, whatever the skill declares.
ALWAYS_APPROVAL = {"shell.exec", "code.exec", "credential.use", "browser.control", "fs.write"}


@dataclass
class SkillResult:
    skill: str
    version: str
    ok: bool
    output: Any = None
    error: str = ""
    duration_ms: float = 0.0
    permissions_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill, "version": self.version, "ok": self.ok, "output": self.output,
            "error": self.error, "duration_ms": round(self.duration_ms, 2),
            "permissions_used": self.permissions_used,
        }


class SkillRuntime:
    """Runs skills and registers them as tools."""

    def __init__(self, *, lifecycle: SkillLifecycle | None = None, policy: Any = None, tools: Any = None,
                 approvals: Any = None) -> None:
        self.lifecycle = lifecycle or get_skill_lifecycle()
        self.policy = policy
        self.tools = tools
        self.approvals = approvals
        # Withdraw a skill's tool as soon as the skill is disabled or uninstalled.
        self.lifecycle.add_observer(self._on_lifecycle_event)

    # -- execution ------------------------------------------------------------- #
    async def run(self, skill_id: str, payload: dict[str, Any], *, version: str = "",
                  actor: str = "model:main", mission_id: str = "") -> SkillResult:
        """Run an active skill after checking its declared permissions."""
        import time

        record = self.lifecycle.get(skill_id, version=version)
        if record.state != SkillState.ACTIVE.value:
            return SkillResult(skill_id, record.version, False, error=f"skill is {record.state}, not ACTIVE")
        manifest = SkillManifest(**record.manifest)

        # Permission gate: each declared permission is checked against policy for this actor.
        granted: list[str] = []
        for permission in manifest.permissions:
            capability = PERMISSION_TO_CAPABILITY.get(permission)
            if capability is None or self.policy is None:
                granted.append(permission)
                continue
            from ..security.policy import PolicyRequest

            decision = self.policy.check(
                PolicyRequest(capability, f"skill://{manifest.id}", actor=actor, context={"skill": manifest.id},
                              mission_scope=[capability])
            )
            if decision.effect.value == "deny":
                self._log("permission_denied", manifest, actor, {"permission": permission, "reason": decision.reason})
                return SkillResult(skill_id, record.version, False,
                                   error=f"skill permission {permission!r} denied: {decision.reason}")
            if decision.effect.value == "approval" or permission in ALWAYS_APPROVAL:
                self._log("permission_approval_required", manifest, actor, {"permission": permission})
                return SkillResult(
                    skill_id, record.version, False,
                    error=f"skill requires approval for {permission!r}; request it via the approvals API",
                )
            granted.append(permission)

        sandbox = SkillSandbox(
            directory=Path(record.path), entrypoint=manifest.entrypoint, runtime=manifest.runtime,
            timeout_seconds=manifest.timeout_seconds, permissions=granted,
        )
        started = time.perf_counter()
        try:
            result = await run_isolated(sandbox, payload)
            ok = bool(result.get("ok", True))
            outcome = SkillResult(skill_id, record.version, ok, output=result.get("output", result),
                                  error=str(result.get("error", "")),
                                  duration_ms=(time.perf_counter() - started) * 1000, permissions_used=granted)
        except Exception as exc:
            outcome = SkillResult(skill_id, record.version, False, error=f"{type(exc).__name__}: {exc}",
                                  duration_ms=(time.perf_counter() - started) * 1000, permissions_used=granted)
        self._log("executed", manifest, actor,
                  {"ok": outcome.ok, "duration_ms": outcome.duration_ms, "mission_id": mission_id},
                  risk=RiskLevel.MEDIUM)
        return outcome

    def _on_lifecycle_event(self, skill_id: str, version: str, event: str) -> None:
        if self.tools is not None and event in ("disabled", "uninstalled", "rollback"):
            for name in (f"skill__{skill_id}", f"skill__{skill_id}@{version}"):
                try:
                    self.tools.unregister(name)
                except Exception:
                    continue

    # -- tool exposure --------------------------------------------------------- #
    def register_tools(self) -> list[str]:
        """Register every active skill as a callable tool."""
        if self.tools is None:
            raise SkillError("no tool registry configured")
        from ..tools.base import FunctionTool, ToolResult

        registered: list[str] = []
        for record in self.lifecycle.active_skills():
            manifest = SkillManifest(**record.manifest)
            tool_name = f"skill__{manifest.id}"
            high_risk = any(permission in ALWAYS_APPROVAL for permission in manifest.permissions)

            async def handler(arguments: dict[str, Any], context: Any, *, _id: str = manifest.id,
                              _version: str = manifest.version) -> ToolResult:
                result = await self.run(_id, arguments, version=_version, actor=context.actor,
                                        mission_id=context.mission_id)
                return (ToolResult.success(result.output, skill=result.skill, version=result.version)
                        if result.ok else ToolResult.failure(result.error, skill=result.skill))

            self.tools.register(
                FunctionTool(
                    tool_name, handler, description=f"[skill] {manifest.description or manifest.name}",
                    capability=Capability.SKILL_EXECUTE,
                    risk=RiskLevel.HIGH if high_risk else RiskLevel.MEDIUM,
                    schema=Schema.object({}, additional=True),
                    requires_approval=False, tags=("skill", manifest.id),
                    timeout_seconds=manifest.timeout_seconds,
                )
            )
            registered.append(tool_name)
        return registered

    def _log(self, action: str, manifest: SkillManifest, actor: str, details: dict[str, Any],
             risk: RiskLevel = RiskLevel.LOW) -> None:
        get_event_log().append(
            EventKind.SKILL,
            {"action": action, "skill": manifest.id, "version": manifest.version, **details},
            actor=actor, source="skills.runtime", risk=risk,
        )


_RUNTIME: SkillRuntime | None = None
_LOCK = threading.Lock()


def get_skill_runtime(**kwargs: Any) -> SkillRuntime:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is None:
            _RUNTIME = SkillRuntime(**kwargs)
        return _RUNTIME


def reset_skill_runtime() -> None:
    global _RUNTIME
    with _LOCK:
        _RUNTIME = None
