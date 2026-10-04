"""The mission engine: plan, execute, verify, repair and roll back durable goals."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core import ApprovalRequired, ConflictError, NotFoundError, VerificationFailed
from ..core.clock import iso
from ..core.risk import RiskLevel, max_risk
from ..events import EventKind, EventLog, get_event_log
from ..recovery import RecoveryEngine, get_recovery_engine
from ..tools.base import ToolContext
from ..verification import VerificationEngine, VerificationReport, get_verification_engine
from ..verification.checks import CheckResult, CheckStatus, VerificationCheck
from .models import Mission, MissionState, MissionStep, StepKind, StepState
from .store import MissionStore, get_mission_store

#: A model step runner: takes the step and mission, returns a result payload.
ModelRunner = Callable[[Mission, MissionStep, ToolContext], Awaitable[Any]]
#: A delegation runner (multi-agent): takes objective + context, returns a result payload.
Delegate = Callable[[str, dict[str, Any], ToolContext], Awaitable[Any]]
#: A check factory registered by name for mission verification plans.
CheckFactory = Callable[[Mission, dict[str, Any]], VerificationCheck]


@dataclass
class MissionResult:
    """What running a mission produced."""

    mission: Mission
    state: MissionState
    verification: VerificationReport | None = None
    error: str = ""
    steps_run: int = 0

    @property
    def ok(self) -> bool:
        return self.state is MissionState.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {"mission": self.mission.to_dict(include_steps=True), "state": self.state.value,
                "ok": self.ok, "error": self.error, "steps_run": self.steps_run,
                "verification": self.verification.to_dict() if self.verification else None}


class MissionEngine:
    """Executes missions step by step, enforcing verification before success."""

    def __init__(
        self,
        *,
        store: MissionStore | None = None,
        tools: Any = None,
        verification: VerificationEngine | None = None,
        recovery: RecoveryEngine | None = None,
        log: EventLog | None = None,
        approvals: Any = None,
        tool_context: ToolContext | None = None,
        model_runner: ModelRunner | None = None,
        delegate: Delegate | None = None,
        max_parallel: int = 1,
    ) -> None:
        self.store = store or get_mission_store()
        self.tools = tools
        self.verification = verification or get_verification_engine(log=log)
        self.recovery = recovery or get_recovery_engine(log=log)
        self.log = log or get_event_log()
        self.approvals = approvals
        self.tool_context = tool_context or ToolContext()
        self.model_runner = model_runner
        self.delegate = delegate
        self.max_parallel = max_parallel
        self._checks: dict[str, CheckFactory] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ config
    def register_check(self, name: str, factory: CheckFactory) -> None:
        """Register a named check so missions can reference it in their verification plan."""
        self._checks[name] = factory

    def available_checks(self) -> list[str]:
        return sorted(self._checks)

    def set_tools(self, tools: Any) -> None:
        self.tools = tools

    def set_model_runner(self, runner: ModelRunner | None) -> None:
        self.model_runner = runner

    def set_delegate(self, delegate: Delegate | None) -> None:
        self.delegate = delegate

    # ------------------------------------------------------------------ authoring
    def create(
        self,
        objective: str,
        *,
        title: str = "",
        success_criteria: list[str] | None = None,
        verification_plan: list[str] | None = None,
        constraints: dict[str, Any] | None = None,
        scope: list[str] | None = None,
        priority: int = 3,
        parent_id: str = "",
        deadline: str = "",
        created_by: str = "owner",
        metadata: dict[str, Any] | None = None,
    ) -> Mission:
        if not objective.strip():
            raise ConflictError("a mission needs an objective")
        mission = Mission(
            objective=objective.strip(), title=title.strip(), success_criteria=list(success_criteria or []),
            verification_plan=list(verification_plan or []), constraints=dict(constraints or {}),
            scope=list(scope or []), priority=int(priority), parent_id=parent_id, deadline=deadline,
            created_by=created_by, metadata=dict(metadata or {}),
        )
        mission.state = MissionState.PLANNED
        self.store.save(mission)
        self._event(mission, "created", {"objective": mission.objective[:200],
                                         "verification_plan": mission.verification_plan,
                                         "priority": mission.priority})
        return mission

    def plan(self, mission_id: str, steps: list[MissionStep | dict[str, Any]], *, summary: str = "") -> Mission:
        """Attach (or replace) the plan for a mission."""
        mission = self.store.get(mission_id)
        if mission.state.terminal:
            raise ConflictError(f"mission {mission_id!r} is {mission.state.value} and cannot be replanned")
        parsed: list[MissionStep] = []
        for index, raw in enumerate(steps):
            step = raw if isinstance(raw, MissionStep) else MissionStep.from_dict(raw)
            step.order = index
            if step.risk is None:
                step.risk = RiskLevel.LOW
            parsed.append(step)
        mission.steps = parsed
        if summary:
            mission.plan_summary = summary
        mission.state = MissionState.PLANNED
        self.store.save(mission)
        self._event(mission, "planned", {"steps": len(parsed), "summary": summary[:200],
                                         "max_risk": max_risk(*[step.risk for step in parsed]).name
                                         if parsed else RiskLevel.LOW.name})
        return mission

    # ------------------------------------------------------------------ execution
    async def run(self, mission_id: str, *, actor: str = "model:main", max_steps: int = 200,
                  worker: str = "", stop_on_approval: bool = True) -> MissionResult:
        """Run until the mission finishes, blocks or needs the owner."""
        mission = self.store.get(mission_id)
        if mission.state.terminal:
            return MissionResult(mission, mission.state, error=mission.error)
        if not mission.steps:
            raise ConflictError(f"mission {mission_id!r} has no plan; call plan() first")
        if not mission.started_at:
            mission.started_at = iso()
        mission.state = MissionState.RUNNING
        mission.error = ""
        self.store.save(mission)
        self._event(mission, "started", {"steps": len(mission.steps), "worker": worker})

        steps_run = 0
        ordered = sorted(mission.steps, key=lambda item: item.order)
        while steps_run < max_steps:
            step = next((item for item in ordered if item.state in (StepState.PENDING, StepState.READY)), None)
            if step is None:
                break
            blocked_by = self._unmet_dependency(mission, step)
            if blocked_by:
                step.state = StepState.SKIPPED
                step.error = f"dependency {blocked_by!r} did not succeed"
                self.store.save(mission)
                continue
            steps_run += 1
            await self._run_step(mission, step, actor=actor, worker=worker)
            if step.state is StepState.WAITING_APPROVAL:
                if stop_on_approval:
                    mission.state = MissionState.WAITING_APPROVAL
                    self.store.save(mission)
                    self._event(mission, "waiting_approval", {"step_id": step.id, "reason": step.error[:200]},
                                risk=RiskLevel.HIGH)
                    return MissionResult(mission, mission.state, error=step.error, steps_run=steps_run)
            if step.state is StepState.FAILED:
                mission.state = MissionState.BLOCKED
                mission.error = f"step {step.title!r} failed: {step.error}"
                self._skip_blocked_steps(mission)
                self.store.save(mission)
                self._event(mission, "blocked", {"step_id": step.id, "error": step.error[:300]},
                            risk=RiskLevel.MEDIUM)
                return MissionResult(mission, mission.state, error=mission.error, steps_run=steps_run)
            if mission.artifacts:
                pass
        remaining = [item for item in mission.steps if item.state is StepState.PENDING]
        if remaining and steps_run >= max_steps:
            mission.state = MissionState.PAUSED
            mission.error = f"paused after {steps_run} steps (step budget reached)"
            self.store.save(mission)
            return MissionResult(mission, mission.state, error=mission.error, steps_run=steps_run)

        mission.state = MissionState.VERIFYING
        self.store.save(mission)
        report = await self.verify(mission, actor=actor)
        mission.verification = report.to_dict()
        if report.passed:
            mission.state = MissionState.SUCCEEDED
            mission.result = {"summary": report.summary, "steps": len(mission.steps),
                              "artifacts": mission.artifacts}
        else:
            mission.state = MissionState.FAILED
            mission.error = f"verification failed: {report.summary}"
            mission.result = {"summary": report.summary, "unproven": [item.name for item in report.unproven]}
        mission.finished_at = iso()
        self.store.save(mission)
        self._event(mission, mission.state.value,
                    {"verification": report.summary, "passed": report.passed}, risk=RiskLevel.LOW
                    if report.passed else RiskLevel.HIGH)
        return MissionResult(mission, mission.state, verification=report, error=mission.error,
                             steps_run=steps_run)

    async def run_until_done(self, mission_id: str, *, actor: str = "model:main",
                             max_rounds: int = 5, **kwargs: Any) -> MissionResult:
        """Run, resuming automatically while the mission keeps making progress."""
        result: MissionResult | None = None
        for _ in range(max_rounds):
            result = await self.run(mission_id, actor=actor, **kwargs)
            if result.mission.state in (MissionState.PAUSED, MissionState.WAITING_APPROVAL):
                break
            if result.mission.state.terminal:
                break
            if result.mission.state is MissionState.BLOCKED:
                break
        assert result is not None
        return result

    # ------------------------------------------------------------------ steps
    async def _run_step(self, mission: Mission, step: MissionStep, *, actor: str, worker: str = "") -> None:
        step.state = StepState.RUNNING
        step.started_at = iso()
        step.attempts += 1
        self.store.save(mission)
        self._event(mission, "step_started", {"step_id": step.id, "title": step.title,
                                              "kind": step.kind.value, "attempt": step.attempts})

        if step.requires_approval or step.risk >= RiskLevel.CRITICAL:
            request = self._request_approval(mission, step, actor=actor)
            step.state = StepState.WAITING_APPROVAL
            step.error = f"owner approval required for {step.title!r}"
            step.evidence["approval_request_id"] = getattr(request, "id", "")
            self.store.save(mission)
            self._event(mission, "step_waiting_approval", {"step_id": step.id,
                                                           "request_id": getattr(request, "id", "")},
                        risk=RiskLevel.HIGH)
            return

        context = self.tool_context.child(actor=actor, mission_id=mission.id, step_id=step.id,
                                          trace_id=mission.id, task=mission.objective,
                                          extra={**self.tool_context.extra, "mission_scope": mission.scope or None,
                                                 "worker": worker})

        try:
            if step.kind is StepKind.TOOL:
                await self._run_tool_step(mission, step, context)
            elif step.kind is StepKind.VERIFY:
                await self._run_verify_step(mission, step, context)
            elif step.kind is StepKind.MODEL:
                await self._run_model_step(mission, step, context)
            elif step.kind is StepKind.DELEGATE:
                await self._run_delegate_step(mission, step, context)
            elif step.kind is StepKind.NOTE:
                step.result = step.payload.get("note", step.description)
                self._succeed(mission, step)
            else:
                raise ConflictError(f"unsupported step kind {step.kind!r}")
        except ApprovalRequired as exc:
            step.state = StepState.WAITING_APPROVAL
            step.error = str(exc)
            step.evidence["approval_request_id"] = getattr(exc, "request_id", "")
            self.store.save(mission)
        except VerificationFailed as exc:
            step.state = StepState.FAILED
            step.error = str(exc)
            step.finished_at = iso()
            self.store.save(mission)
        except Exception as exc:
            step.state = StepState.FAILED
            step.error = f"{type(exc).__name__}: {exc}"
            step.finished_at = iso()
            self.store.save(mission)

    async def _run_tool_step(self, mission: Mission, step: MissionStep, context: ToolContext) -> None:
        if self.tools is None:
            raise ConflictError("no tool registry is attached to the mission engine")

        async def call() -> Any:
            result = await self.tools.execute(
                step.tool, dict(step.arguments), context=context,
                approval_id=str(step.payload.get("approval_id") or ""),
            )
            if not getattr(result, "ok", False):
                request_id = str((getattr(result, "metadata", {}) or {}).get("approval_request_id") or "")
                if request_id:
                    # The tool is not failing - it is waiting for the owner. Remember which request so
                    # the mission can wait and then resume once it is approved.
                    step.evidence["approval_request_id"] = request_id
                raise RuntimeError(f"{step.tool} failed: {getattr(result, 'error', 'unknown error')}")
            return result

        async def replan(failure: Any) -> Any:
            step.arguments = _shrink(step.arguments)
            return call

        outcome = await self.recovery.attempt(
            f"mission:{mission.id}:{step.tool}", call, alternative=None, replan=replan,
            mission_id=mission.id, trace_id=mission.id, actor=context.actor,
            classify_kwargs={"reversible": step.reversible},
            on_failure=self._repair_hook(mission, step),
        )
        if not outcome.recovered:
            request_id = str(step.evidence.get("approval_request_id") or "")
            if request_id:
                raise ApprovalRequired(f"{step.tool} requires owner approval",
                                       request_id=request_id)
            raise RuntimeError(outcome.error or "tool step failed after repairs")
        payload = outcome.result
        step.result = {"output": getattr(payload, "output", None), "artifacts": getattr(payload, "artifacts", []),
                       "attempts": outcome.attempts, "redacted": getattr(payload, "redacted", False)}
        for artifact in getattr(payload, "artifacts", []) or []:
            if artifact not in mission.artifacts:
                mission.artifacts.append(str(artifact))
        self._succeed(mission, step, attempts=outcome.attempts)

    async def _run_verify_step(self, mission: Mission, step: MissionStep, context: ToolContext) -> None:
        name = step.payload.get("check") or step.arguments.get("check") or ""
        factory = self._checks.get(name)
        if factory is None:
            raise ConflictError(f"no verification check registered under {name!r}")
        check = factory(mission, dict(step.arguments or step.payload))
        result = await check.run({"mission": mission.to_dict(), "context": dict(step.arguments)})
        step.evidence["check"] = result.to_dict()
        if not result.ok:
            raise VerificationFailed(f"check {name!r} failed: {result.detail}")
        step.result = result.to_dict()
        self._succeed(mission, step)

    async def _run_model_step(self, mission: Mission, step: MissionStep, context: ToolContext) -> None:
        if self.model_runner is None:
            raise ConflictError("no model runner is attached to the mission engine")
        output = await self.model_runner(mission, step, context)
        step.result = output if isinstance(output, dict) else {"output": output}
        self._succeed(mission, step)

    async def _run_delegate_step(self, mission: Mission, step: MissionStep, context: ToolContext) -> None:
        if self.delegate is None:
            raise ConflictError("no delegation handler is attached to the mission engine")
        objective = step.payload.get("objective") or step.description or step.title
        output = await self.delegate(objective, dict(step.payload), context)
        step.result = output if isinstance(output, dict) else {"output": output}
        self._succeed(mission, step)

    def _succeed(self, mission: Mission, step: MissionStep, *, attempts: int | None = None) -> None:
        step.state = StepState.DONE
        step.finished_at = iso()
        if attempts is not None:
            step.attempts = attempts
        self.store.save(mission)
        self._event(mission, "step_done", {"step_id": step.id, "title": step.title, "attempts": step.attempts})

    def _repair_hook(self, mission: Mission, step: MissionStep) -> Callable[[Any, Any], Awaitable[None]]:
        async def hook(failure: Any, plan: Any) -> None:
            step.evidence.setdefault("repairs", []).append(plan.to_dict())
            self._event(mission, "step_repair", {"step_id": step.id, "action": plan.action.value,
                                                 "reason": plan.reason[:200],
                                                 "failure_class": failure.class_.value},
                        risk=RiskLevel.MEDIUM)

        return hook

    def _skip_blocked_steps(self, mission: Mission) -> list[str]:
        """Mark every step that can no longer run because a dependency failed."""
        skipped: list[str] = []
        changed = True
        while changed:
            changed = False
            for step in mission.steps:
                if step.state is not StepState.PENDING:
                    continue
                if self._unmet_dependency(mission, step):
                    step.state = StepState.SKIPPED
                    step.error = "a dependency did not succeed"
                    skipped.append(step.id)
                    changed = True
        return skipped

    @staticmethod
    def _unmet_dependency(mission: Mission, step: MissionStep) -> str:
        for dependency in step.depends_on:
            other = mission.step(dependency)
            if other is None:
                return dependency
            if other.state not in (StepState.DONE, StepState.SKIPPED):
                return dependency
        return ""

    # ------------------------------------------------------------------ verification
    async def verify(self, mission: Mission, *, actor: str = "model:main") -> VerificationReport:
        """Run the mission's verification plan. An empty plan is not a free pass."""
        checks: list[VerificationCheck] = []
        aliases: dict[str, str] = {}
        # Plan-level checks run against the mission's own output: its artifacts, its constraint set and
        # any per-check arguments the plan recorded, so "artifact_exists" means something concrete.
        plan_payload: dict[str, Any] = {
            "mission": mission.to_dict(), "artifacts": list(mission.artifacts),
            "constraints": dict(mission.constraints), "objective": mission.objective,
            "check_arguments": dict(mission.metadata.get("check_arguments") or {}),
            "cwd": str(self.tool_context.workspace or "."),
        }
        for name in mission.verification_plan:
            factory = self._checks.get(name)
            if factory is None:
                checks.append(_MissingCheck(name))
                continue
            arguments = dict(plan_payload)
            extra = plan_payload["check_arguments"].get(name)
            if isinstance(extra, dict):
                arguments.update(extra)
            check = factory(mission, arguments)
            aliases[check.name] = name
            checks.append(check)
        checks.append(_StepsCompletedCheck(mission))
        if mission.success_criteria:
            checks.append(_CriteriaDeclaredCheck(mission))
        report = await self.verification.verify(
            f"mission:{mission.id}:{mission.objective[:60]}", checks,
            context={"mission": mission.to_dict(), "cwd": str(self.tool_context.workspace or "."),
                     "criteria_aliases": aliases},
            mission_id=mission.id, trace_id=mission.id, actor=actor,
        )
        return report

    # ------------------------------------------------------------------ control
    def pause(self, mission_id: str, *, reason: str = "") -> Mission:
        mission = self.store.get(mission_id)
        if mission.state.terminal:
            raise ConflictError(f"mission {mission_id!r} already finished")
        mission.state = MissionState.PAUSED
        mission.error = reason
        self.store.save(mission)
        self.store.add_checkpoint(mission, "paused", {"reason": reason})
        self._event(mission, "paused", {"reason": reason[:200]})
        return mission

    def cancel(self, mission_id: str, *, reason: str = "", actor: str = "owner") -> Mission:
        mission = self.store.get(mission_id)
        mission.state = MissionState.CANCELLED
        mission.error = reason
        mission.finished_at = iso()
        for step in mission.steps:
            if step.state in (StepState.PENDING, StepState.READY, StepState.RUNNING):
                step.state = StepState.SKIPPED
                step.error = "mission cancelled"
        self.store.save(mission)
        self._event(mission, "cancelled", {"reason": reason[:200]}, actor=actor, risk=RiskLevel.MEDIUM)
        return mission

    def rearm(self, mission_id: str, *, actor: str = "owner", step_id: str = "",
              only_approved: bool = True) -> dict[str, Any]:
        """Put approval-blocked steps back in the queue - but only where the owner really approved.

        Approval is verified against the approval engine, never taken on the caller's word, so a
        mission cannot be unblocked by asserting that it was approved. Each re-armed step carries the
        approved request id, which the tool registry consumes exactly once when the step runs.
        """
        from ..approvals import ApprovalStatus

        mission = self.store.get(mission_id)
        rearmed: list[str] = []
        refused: list[dict[str, str]] = []
        for step in mission.steps:
            if step.state is not StepState.WAITING_APPROVAL:
                continue
            if step_id and step.id != step_id:
                continue
            request_id = str(step.evidence.get("approval_request_id") or "")
            approved = False
            if self.approvals is not None and request_id:
                try:
                    status = self.approvals.status(request_id)
                except Exception as exc:
                    refused.append({"step_id": step.id, "reason": f"approval lookup failed: {exc}"})
                    continue
                approved = status is ApprovalStatus.APPROVED
            elif not only_approved:
                approved = True
            if not approved:
                refused.append({"step_id": step.id,
                                "reason": "no approved request for this step" if request_id
                                else "no approval request was recorded"})
                continue
            step.state = StepState.PENDING
            step.error = ""
            if request_id:
                step.payload["approval_id"] = request_id
            rearmed.append(step.id)
        if rearmed:
            if mission.state is MissionState.WAITING_APPROVAL:
                mission.state = MissionState.PAUSED
            mission.error = ""
            self.store.save(mission)
            self.store.add_checkpoint(mission, "rearmed", extra={"steps": rearmed, "actor": actor})
            self._event(mission, "steps_rearmed", {"steps": rearmed}, actor=actor, risk=RiskLevel.MEDIUM)
        return {"mission_id": mission.id, "rearmed": rearmed, "refused": refused}

    async def resume(self, mission_id: str, *, actor: str = "owner", **kwargs: Any) -> MissionResult:
        mission = self.store.get(mission_id)
        if mission.state.terminal:
            return MissionResult(mission, mission.state, error=mission.error)
        if mission.state is MissionState.WAITING_APPROVAL:
            mission.state = MissionState.PAUSED
            self.store.save(mission)
        return await self.run(mission_id, actor=actor, **kwargs)

    async def resume_all(self, *, actor: str = "system") -> list[dict[str, Any]]:
        """Pick up missions interrupted by a restart."""
        results: list[dict[str, Any]] = []
        for mission in self.store.resumable():
            if mission.state is MissionState.WAITING_APPROVAL:
                results.append({"id": mission.id, "state": mission.state.value, "skipped": "needs owner"})
                continue
            try:
                result = await self.run(mission.id, actor=actor)
                results.append({"id": mission.id, "state": result.state.value, "ok": result.ok})
            except Exception as exc:
                results.append({"id": mission.id, "error": f"{type(exc).__name__}: {exc}"})
        return results

    async def rollback(self, mission_id: str, *, actor: str = "owner", reason: str = "") -> dict[str, Any]:
        """Undo reversible steps in reverse order; report what could not be undone."""
        mission = self.store.get(mission_id)
        undone: list[str] = []
        skipped: list[dict[str, Any]] = []
        for step in sorted(mission.steps, key=lambda item: item.order, reverse=True):
            if step.state not in (StepState.DONE, StepState.FAILED, StepState.WAITING_APPROVAL):
                continue
            compensation = (step.payload or {}).get("compensation")
            if not step.reversible:
                skipped.append({"step_id": step.id, "reason": "irreversible"})
                continue
            if not compensation or self.tools is None:
                skipped.append({"step_id": step.id,
                                "reason": "no compensation registered" if step.state is StepState.DONE
                                else "never ran"})
                continue
            context = self.tool_context.child(actor=actor, mission_id=mission.id, step_id=step.id,
                                              extra={**self.tool_context.extra, "compensation": True})
            try:
                result = await self.tools.execute(compensation["tool"],
                                                 dict(compensation.get("arguments") or {}), context=context)
                if getattr(result, "ok", False):
                    undone.append(step.id)
                else:
                    skipped.append({"step_id": step.id, "reason": f"compensation failed: {result.error}"})
            except Exception as exc:
                skipped.append({"step_id": step.id, "reason": f"compensation raised: {exc}"})
        mission.state = MissionState.ROLLED_BACK
        mission.finished_at = iso()
        mission.result = {**mission.result, "rollback": {"undone": undone, "skipped": skipped,
                                                         "reason": reason}}
        self.store.save(mission)
        self._event(mission, "rolled_back", {"undone": undone, "skipped": skipped, "reason": reason[:200]},
                    actor=actor, risk=RiskLevel.MEDIUM)
        return {"mission_id": mission.id, "undone": undone, "skipped": skipped,
                "complete": not skipped}

    # ------------------------------------------------------------------ reads
    def get(self, mission_id: str) -> Mission:
        return self.store.get(mission_id)

    def list(self, *, state: str = "", active_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        return [mission.to_dict(include_steps=False)
                for mission in self.store.list(state=state, active_only=active_only, limit=limit)]

    def resumable(self) -> list[dict[str, Any]]:
        return [mission.to_dict(include_steps=False) for mission in self.store.resumable()]

    def stats(self) -> dict[str, Any]:
        return self.store.stats()

    # ------------------------------------------------------------------ internals
    def _request_approval(self, mission: Mission, step: MissionStep, *, actor: str) -> Any:
        if self.approvals is None:
            # No approval engine attached: the step still cannot run, and the mission waits for the
            # owner instead of failing silently or proceeding.
            self._event(mission, "approval_unavailable",
                        {"step_id": step.id, "reason": "no approval engine configured"}, risk=RiskLevel.HIGH)
            return None
        operation = f"mission.step.{step.kind.value}"
        arguments: dict[str, Any] = dict(step.arguments or step.payload)
        resources = [mission.id]
        if step.kind is StepKind.TOOL and step.tool and self.tools is not None:
            # Ask for the *tool's* operation so the single approval the owner grants is the one the
            # tool registry verifies when the step finally runs.
            try:
                target = self.tools.approval_target(step.tool, step.arguments)
                operation, arguments = target["operation"], dict(target["arguments"])
                resources = [target["resource"]] if target["resource"] else [mission.id]
            except Exception:
                pass
        return self.approvals.request(
            operation,
            reason=f"mission {mission.id}: {step.title}",
            risk=max(step.risk, RiskLevel.HIGH), actor=actor,
            permissions=[step.tool] if step.tool else [],
            resources=resources, arguments=arguments,
            reversibility="reversible" if step.reversible else "irreversible",
            mission_id=mission.id, step_id=step.id,
        )

    def _event(self, mission: Mission, action: str, payload: dict[str, Any], *,
               actor: str = "model:main", risk: RiskLevel = RiskLevel.LOW) -> None:
        try:
            self.log.append(EventKind.MISSION, {"action": action, "mission_id": mission.id,
                                                "objective": mission.objective[:160],
                                                "progress": mission.progress, **payload},
                            actor=actor, source="missions.engine", mission_id=mission.id,
                            trace_id=mission.id, risk=risk)
        except Exception:
            pass


class _StepsCompletedCheck(VerificationCheck):
    """The built-in honesty check: every step must actually have completed."""

    def __init__(self, mission: Mission) -> None:
        self.mission = mission
        self.name = "steps_completed"

    async def check(self, context: dict[str, Any]) -> CheckResult:
        total = len(self.mission.steps)
        done = sum(1 for step in self.mission.steps if step.state in (StepState.DONE, StepState.SKIPPED))
        unproven = [step.title for step in self.mission.steps
                    if step.state not in (StepState.DONE, StepState.SKIPPED)]
        if done != total:
            return CheckResult(self.name, CheckStatus.FAILED,
                               f"{done}/{total} steps completed; outstanding: {unproven[:5]}",
                               {"outstanding": unproven})
        return CheckResult(self.name, CheckStatus.PASSED, f"all {total} steps completed",
                           {"steps": total, "artifacts": self.mission.artifacts})


class _CriteriaDeclaredCheck(VerificationCheck):
    """Success criteria must be backed by a real check, otherwise they are unverified claims."""

    def __init__(self, mission: Mission) -> None:
        self.mission = mission
        self.name = "success_criteria_backed"

    async def check(self, context: dict[str, Any]) -> CheckResult:
        unbacked = [criterion for criterion in self.mission.success_criteria
                    if criterion not in (context.get("verified_criteria") or [])]
        if unbacked:
            return CheckResult(
                self.name, CheckStatus.FAILED,
                f"success criteria without a passing check: {unbacked[:4]} - declare a verification "
                "check for each criterion instead of asserting it",
                {"unbacked": unbacked},
            )
        return CheckResult(self.name, CheckStatus.PASSED, "every success criterion has a check")


class _MissingCheck(VerificationCheck):
    """A verification plan that names an unregistered check can never pass."""

    def __init__(self, name: str) -> None:
        self.name = f"missing_check:{name}"

    async def check(self, context: dict[str, Any]) -> CheckResult:
        return CheckResult(self.name, CheckStatus.FAILED,
                           "no verification check is registered under this name")


def _shrink(arguments: dict[str, Any]) -> dict[str, Any]:
    """A conservative 'try less' transformation used when a step times out."""
    shrunk = dict(arguments)
    if isinstance(shrunk.get("max_lines"), int):
        shrunk["max_lines"] = max(20, shrunk["max_lines"] // 4)
    if isinstance(shrunk.get("limit"), int):
        shrunk["limit"] = max(5, shrunk["limit"] // 4)
    if isinstance(shrunk.get("max_bytes"), int):
        shrunk["max_bytes"] = max(4096, shrunk["max_bytes"] // 4)
    return shrunk


_ENGINE: MissionEngine | None = None
_LOCK = threading.Lock()


def get_mission_engine(**kwargs: Any) -> MissionEngine:
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = MissionEngine(**kwargs)
        return _ENGINE


def reset_mission_engine() -> None:
    global _ENGINE
    with _LOCK:
        _ENGINE = None
