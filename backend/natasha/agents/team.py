"""The agent team: roles wired into the supervisor and the mission engine."""

from __future__ import annotations

import threading
from typing import Any

from ..core import ConflictError, NatashaError
from ..core.risk import RiskLevel
from ..events import EventKind, get_event_log
from .roles import AGENT_ROLES, AgentRole, get_role


class AgentTeam:
    """Creates missions for roles, delegates through the supervisor, and reports honestly."""

    def __init__(self, *, missions: Any = None, supervisor: Any = None, tools: Any = None,
                 brain: Any = None, log: Any = None) -> None:
        self.missions = missions
        self.supervisor = supervisor
        self.tools = tools
        self.brain = brain
        self.log = log or get_event_log()
        self._handlers_registered: set[str] = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    def install_roles(self) -> list[str]:
        """Register every role as a supervisor profile + handler."""
        if self.supervisor is None:
            return []
        from ..missions.supervisor import WorkerProfile

        installed: list[str] = []
        for name, role in AGENT_ROLES.items():
            try:
                self.supervisor.register_profile(WorkerProfile(
                    role=name, description=role.description, capabilities=role.capabilities,
                    max_risk=RiskLevel[role.max_risk], allowed_tools=role.tools,
                    timeout_seconds=role.default_timeout,
                ))
            except ConflictError:
                pass  # already registered
            if name not in self._handlers_registered:
                self.supervisor.register_handler(name, self._make_handler(role))
                self._handlers_registered.add(name)
            installed.append(name)
        return installed

    def _make_handler(self, role: AgentRole) -> Any:
        async def handler(task: Any, worker: Any) -> dict[str, Any]:
            return await self._run_role(role, task, worker)

        return handler

    async def _run_role(self, role: AgentRole, task: Any, worker: Any) -> dict[str, Any]:
        """Plan the objective as a mission and run it with the worker's narrowed identity."""
        if self.missions is None:
            return {"ok": False, "error": "no mission engine attached to the team", "output": None}
        context = dict(getattr(task, "context", {}) or {})
        steps = role.plan_builder(task.objective, context) if role.plan_builder else []
        mission = self.missions.create(
            task.objective, title=f"[{role.name}] {task.objective[:60]}",
            success_criteria=list(context.get("success_criteria") or []),
            verification_plan=list(role.verification_plan), scope=[capability.value for capability in role.capabilities],
            created_by=worker.actor, metadata={"role": role.name, "worker": worker.id},
        )
        if steps:
            mission = self.missions.plan(mission.id, steps, summary=f"{role.name} plan for {task.objective[:60]}")
        result = await self.missions.run(mission.id, actor=worker.actor, worker=worker.id)
        return {"ok": result.ok, "output": result.mission.result, "error": result.error,
                "verification": result.mission.verification, "mission_id": mission.id,
                "risk": role.max_risk}

    # ------------------------------------------------------------------ delegation
    async def delegate(self, role: str, objective: str, *, context: dict[str, Any] | None = None,
                       actor: str = "model:main", timeout_seconds: float | None = None) -> dict[str, Any]:
        """Hand work to a role. Without a supervisor, run the role's mission directly."""
        definition = get_role(role)
        if self.supervisor is not None:
            self.install_roles()
            result = await self.supervisor.delegate(role, objective, context=context, actor=actor,
                                                    timeout_seconds=timeout_seconds)
            return result.to_dict()
        if self.missions is None:
            raise NatashaError("no supervisor or mission engine is attached to the team")
        steps = definition.plan_builder(objective, context or {}) if definition.plan_builder else []
        mission = self.missions.create(objective, title=f"[{role}] {objective[:60]}",
                                       verification_plan=list(definition.verification_plan),
                                       scope=[capability.value for capability in definition.capabilities],
                                       created_by=actor)
        if steps:
            self.missions.plan(mission.id, steps)
        result = await self.missions.run(mission.id, actor=actor)
        return {"task_id": mission.id, "ok": result.ok, "output": result.mission.result,
                "error": result.error, "verification": result.mission.verification}

    def roles(self) -> list[dict[str, Any]]:
        return [role.to_dict() for role in AGENT_ROLES.values()]

    def status(self) -> dict[str, Any]:
        return {"roles": sorted(AGENT_ROLES),
                "supervisor": self.supervisor.stats() if self.supervisor is not None else None,
                "missions": self.missions.stats() if self.missions is not None else None}


_TEAM: AgentTeam | None = None
_LOCK = threading.Lock()


def get_agent_team(**kwargs: Any) -> AgentTeam:
    global _TEAM
    with _LOCK:
        if _TEAM is None:
            _TEAM = AgentTeam(**kwargs)
        return _TEAM


def reset_agent_team() -> None:
    global _TEAM
    with _LOCK:
        _TEAM = None
