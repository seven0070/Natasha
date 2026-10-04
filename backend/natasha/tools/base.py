"""Tool contract: schema, capability, risk, execution context and result."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Awaitable, Callable

from ..core.risk import RiskLevel
from ..security.policy import Capability
from ..security.validation import Schema


@dataclass
class ToolContext:
    """Everything a tool is allowed to know about its caller."""

    actor: str = "model:main"
    mission_id: str = ""
    step_id: str = ""
    trace_id: str = ""
    task: str = ""
    policy: Any = None
    memory: Any = None
    brain: Any = None
    world: Any = None
    broker: Any = None
    approvals: Any = None
    workspace: Path | None = None
    settings: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def child(self, **overrides: Any) -> "ToolContext":
        data = {
            "actor": self.actor, "mission_id": self.mission_id, "step_id": self.step_id,
            "trace_id": self.trace_id, "task": self.task, "policy": self.policy, "memory": self.memory,
            "brain": self.brain, "world": self.world, "broker": self.broker, "approvals": self.approvals,
            "workspace": self.workspace, "settings": self.settings, "extra": dict(self.extra),
        }
        data.update(overrides)
        return ToolContext(**data)


@dataclass
class ToolResult:
    """What a tool returns."""

    ok: bool
    output: Any = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "output": self.output, "error": self.error,
            "metadata": self.metadata, "artifacts": self.artifacts, "redacted": self.redacted,
        }

    @classmethod
    def success(cls, output: Any = None, artifacts: Iterable[str] | None = None,
                **metadata: Any) -> "ToolResult":
        """``artifacts`` is a first-class field (paths of files the tool produced), not metadata."""
        return cls(ok=True, output=output, artifacts=[str(item) for item in (artifacts or [])],
                   metadata=metadata)

    @classmethod
    def failure(cls, error: str, artifacts: Iterable[str] | None = None, **metadata: Any) -> "ToolResult":
        return cls(ok=False, error=error, artifacts=[str(item) for item in (artifacts or [])],
                   metadata=metadata)


class Tool(abc.ABC):
    """Base class for tools."""

    name: str = "tool"
    description: str = ""
    capability: Capability = Capability.CODE_EXEC
    risk: RiskLevel = RiskLevel.MEDIUM
    schema: Schema = Schema.object({})
    requires_approval: bool = False
    reversible: bool = True
    timeout_seconds: float = 120.0
    tags: tuple[str, ...] = ()

    @abc.abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool. Arguments have already been validated."""

    def resource_for(self, arguments: dict[str, Any]) -> str:
        """Override when the acted-on resource is not simply a caller argument.

        The registry uses this for the policy check, so a tool that always writes inside its own
        sandboxed directory (e.g. artifacts) reports the *real* path instead of a bare filename.
        """
        return ""

    def spec(self) -> "ToolSpec":
        return ToolSpec(
            name=self.name, description=self.description, capability=self.capability, risk=self.risk,
            schema=self.schema, requires_approval=self.requires_approval, reversible=self.reversible,
            timeout_seconds=self.timeout_seconds, tags=self.tags,
        )


@dataclass
class ToolSpec:
    """Public description of a tool (used by the registry, API and model tool-calling)."""

    name: str
    description: str
    capability: Capability
    risk: RiskLevel
    schema: Schema
    requires_approval: bool = False
    reversible: bool = True
    timeout_seconds: float = 120.0
    tags: tuple[str, ...] = ()
    source: str = "builtin"          # builtin | skill | mcp | plugin
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_llm_schema(self) -> dict[str, Any]:
        """OpenAI-style function declaration."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema.to_json_schema(),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "capability": self.capability.value,
            "risk": self.risk.name, "requires_approval": self.requires_approval, "reversible": self.reversible,
            "source": self.source, "enabled": self.enabled, "tags": list(self.tags),
            "schema": self.schema.to_json_schema(), "metadata": self.metadata,
        }


#: Handler signature for function-style tools.
Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


class FunctionTool(Tool):
    """Adapter that turns a plain async function into a tool."""

    def __init__(self, name: str, handler: Handler, *, description: str = "", capability: Capability = Capability.CODE_EXEC,
                 risk: RiskLevel = RiskLevel.MEDIUM, schema: Schema | None = None, requires_approval: bool = False,
                 reversible: bool = True, timeout_seconds: float = 120.0, tags: tuple[str, ...] = (),
                 resource_field: str = "") -> None:
        self.name = name
        self._handler = handler
        self.description = description or name
        self.capability = capability
        self.risk = risk
        self.schema = schema or Schema.object({}, additional=True)
        self.requires_approval = requires_approval
        self.reversible = reversible
        self.timeout_seconds = timeout_seconds
        self.tags = tags
        self._resource_field = resource_field

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._handler(arguments, context)

    def resource_for(self, arguments: dict[str, Any]) -> str:
        if self._resource_field:
            return str(arguments.get(self._resource_field, ""))
        return ""
