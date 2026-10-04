"""Tool registry - the single choke point every tool call passes through."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Iterable

from ..core import ApprovalRequired, NotFoundError, PolicyDenied, ValidationError
from ..core.risk import RiskLevel
from ..events import EventKind, EventLog, get_event_log
from ..events.sanitizer import get_sanitizer
from ..security.policy import OWNER_ACTORS, Capability, PolicyEngine, PolicyRequest
from ..security.validation import validate_arguments, validate_tool_output
from .base import Tool, ToolContext, ToolResult, ToolSpec


class ToolRegistry:
    """Registers tools and executes them safely."""

    def __init__(
        self,
        *,
        policy: PolicyEngine | None = None,
        log: EventLog | None = None,
        approvals: Any = None,
        max_output_bytes: int = 262_144,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._specs: dict[str, ToolSpec] = {}
        self.policy = policy or PolicyEngine()
        self.log = log or get_event_log()
        self.approvals = approvals
        self.max_output_bytes = max_output_bytes
        self._lock = threading.RLock()

    # -- registration ---------------------------------------------------------- #
    def register(self, tool: Tool, *, replace: bool = True) -> ToolSpec:
        spec = tool.spec()
        with self._lock:
            if tool.name in self._tools and not replace:
                raise ValueError(f"tool {tool.name!r} already registered")
            self._tools[tool.name] = tool
            self._specs[tool.name] = spec
        return spec

    def register_function(self, name: str, handler: Any, **kwargs: Any) -> ToolSpec:
        from .base import FunctionTool

        return self.register(FunctionTool(name, handler, **kwargs))

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)
            self._specs.pop(name, None)

    def enable(self, name: str, enabled: bool = True) -> None:
        with self._lock:
            spec = self._specs.get(name)
            if spec is None:
                raise NotFoundError(f"tool {name!r} is not registered")
            spec.enabled = enabled

    def get(self, name: str) -> Tool:
        with self._lock:
            tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError(f"tool {name!r} is not registered")
        return tool

    def spec(self, name: str) -> ToolSpec:
        with self._lock:
            spec = self._specs.get(name)
        if spec is None:
            raise NotFoundError(f"tool {name!r} is not registered")
        return spec

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def specs(self, *, enabled_only: bool = True) -> list[ToolSpec]:
        with self._lock:
            specs = list(self._specs.values())
        return [spec for spec in specs if spec.enabled or not enabled_only]

    def describe(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self.specs(enabled_only=enabled_only)]

    def llm_tools(self, *, allowed: Iterable[Capability] | None = None) -> list[dict[str, Any]]:
        """Tool declarations for model tool-calling, optionally restricted by capability."""
        allow = set(allowed) if allowed is not None else None
        return [spec.to_llm_schema() for spec in self.specs()
                if allow is None or spec.capability in allow]

    def for_actor(self, actor: str, *, mission_scope: Iterable[Capability] | None = None) -> list[ToolSpec]:
        """Which tools this actor could *potentially* run (still policy-checked per call)."""
        scope = {Capability.parse(capability) for capability in mission_scope} if mission_scope else None
        available: list[ToolSpec] = []
        for spec in self.specs():
            if scope is not None and spec.capability not in scope:
                continue
            decision = self.policy.check(
                PolicyRequest(spec.capability, "", actor=actor, mission_scope=scope)
            )
            if decision.effect.value != "deny":
                available.append(spec)
        return available

    # -- execution ------------------------------------------------------------- #
    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
        approval_id: str = "",
        force: bool = False,
    ) -> ToolResult:
        """Validate, authorise, execute and audit a tool call."""
        context = context or ToolContext()
        try:
            tool = self.get(name)
            spec = self.spec(name)
        except NotFoundError as exc:
            return ToolResult.failure(str(exc), tool=name)

        if not spec.enabled:
            return ToolResult.failure(f"tool {name!r} is disabled", tool=name)

        # 1. argument validation (types, bounds, traversal, required fields)
        try:
            clean = validate_arguments(spec.schema, arguments or {})
        except ValidationError as exc:
            self._log("tool_request", name, context, "rejected", str(exc), spec.risk)
            return ToolResult.failure(f"invalid arguments: {exc}", tool=name, issues=exc.details.get("issues", []))

        resource = self._approval_resource(tool, spec, clean)

        # 2. policy
        decision = self.policy.check(
            PolicyRequest(
                spec.capability, resource, actor=context.actor, context={"tool": name, **clean},
                risk_hint=spec.risk, mission_scope=context.extra.get("mission_scope"),
                reversible=spec.reversible,
            )
        )
        if decision.effect.value == "deny":
            self._log("policy", name, context, "denied", decision.reason, decision.risk)
            return ToolResult.failure(f"policy denied: {decision.reason}", tool=name, risk=decision.risk.name)

        # 3. approval
        owner_actor = context.actor.split(":", 1)[0].lower() in OWNER_ACTORS
        if decision.effect.value == "approval" or spec.requires_approval:
            if owner_actor:
                # The owner acting directly *is* the authority (the policy engine encodes the same
                # rule). Autonomous actors - models, workers, skills - still need a real approval.
                self._log("approval", name, context, "owner_authority", decision.reason,
                          max(decision.risk, spec.risk), extra={"obligation": "owner_direct_action"})
            elif self.approvals is None:
                return ToolResult.failure(
                    f"tool {name!r} requires approval but no approval engine is configured", tool=name
                )
            elif not approval_id:
                request = self.approvals.request(
                    f"tool.{name}", reason=decision.reason or f"tool {name} requires approval",
                    risk=max(decision.risk, spec.risk), actor=context.actor,
                    permissions=[spec.capability.value], resources=[resource] if resource else [],
                    arguments=clean, reversibility="reversible" if spec.reversible else "irreversible",
                    mission_id=context.mission_id, step_id=context.step_id, trace_id=context.trace_id,
                )
                self._log("approval", name, context, "required", decision.reason, request.risk,
                          extra={"request_id": request.id})
                return ToolResult.failure(
                    f"approval required: {decision.reason}", tool=name,
                    approval_request_id=request.id, risk=request.risk.name,
                    operation=f"tool.{name}", arguments=clean,
                )
            else:
                try:
                    self.approvals.consume(approval_id, f"tool.{name}", clean, actor=context.actor)
                except Exception as exc:
                    self._log("approval", name, context, "rejected", str(exc), RiskLevel.HIGH)
                    return ToolResult.failure(f"approval rejected: {exc}", tool=name)

        # 4. execute with a timeout
        self._log("tool_request", name, context, "granted", decision.reason, max(spec.risk, decision.risk))
        try:
            result = await asyncio.wait_for(tool.run(clean, context), timeout=spec.timeout_seconds)
        except asyncio.TimeoutError:
            result = ToolResult.failure(f"tool {name!r} timed out after {spec.timeout_seconds}s", tool=name)
        except PolicyDenied as exc:
            result = ToolResult.failure(f"denied: {exc}", tool=name)
        except ApprovalRequired as exc:
            result = ToolResult.failure(f"approval required: {exc}", tool=name, approval_request_id=exc.request_id)
        except Exception as exc:
            result = ToolResult.failure(f"{type(exc).__name__}: {exc}", tool=name)

        # 5. output validation (bounded size, secrets stripped before it can reach a model)
        try:
            result.output = validate_tool_output(result.output, max_bytes=self.max_output_bytes)
        except ValidationError as exc:
            result = ToolResult.failure(f"tool output rejected: {exc}", tool=name)
        from ..events.sanitizer import get_sanitizer

        sanitizer = get_sanitizer()
        if not result.redacted:
            original = result.output
            result.output = sanitizer.sanitize(result.output)
            result.redacted = result.output != original

        self._log(
            "tool_execution", name, context, "ok" if result.ok else "failed",
            result.error, max(spec.risk, decision.risk),
            extra={"artifacts": result.artifacts, "redacted": result.redacted},
        )
        return result

    def approval_target(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """The (operation, resource, arguments) an approval must match to authorise this tool call.

        The mission engine uses this so a step approval and the tool's own approval check describe the
        same action - otherwise an approved step would still be refused when it finally runs.
        """
        tool = self.get(name)
        spec = self.spec(name)
        clean = validate_arguments(spec.schema, arguments or {})
        return {"operation": f"tool.{name}", "arguments": clean,
                "resource": self._approval_resource(tool, spec, clean)}

    def _approval_resource(self, tool: Any, spec: ToolSpec, clean: dict[str, Any]) -> str:
        """The resource string used for policy and approval fingerprinting of a tool call."""
        return tool.resource_for(clean) or self._primary_resource(clean, spec)

    @staticmethod
    def _primary_resource(arguments: dict[str, Any], spec: ToolSpec) -> str:
        """Which argument identifies the thing being acted on (for policy + approvals)."""
        for key in ("path", "url", "command", "file", "target", "reference", "query", "code", "name"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return value[:400]
        return ""

    def _log(
        self,
        action: str,
        tool_name: str,
        context: ToolContext,
        outcome: str,
        detail: str = "",
        risk: RiskLevel = RiskLevel.LOW,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        kind = {
            "tool_request": EventKind.TOOL_REQUEST,
            "tool_execution": EventKind.TOOL_EXECUTION,
            "policy": EventKind.POLICY,
            "approval": EventKind.APPROVAL,
        }.get(action, EventKind.TOOL_EXECUTION)
        payload = {
            "action": action, "tool": tool_name, "outcome": outcome, "detail": detail[:400],
            "risk": risk.name, **(extra or {}),
        }
        try:
            self.log.append(kind, payload, actor=context.actor, source="tools.registry",
                            mission_id=context.mission_id, trace_id=context.trace_id, risk=risk)
        except Exception:  # logging must never break a tool call
            pass


_REGISTRY: ToolRegistry | None = None
_LOCK = threading.Lock()


def get_tool_registry(**kwargs: Any) -> ToolRegistry:
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = ToolRegistry(**kwargs)
        return _REGISTRY


def reset_tool_registry() -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
