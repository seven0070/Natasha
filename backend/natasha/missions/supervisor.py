"""Multi-agent supervisor.

Natasha is one agent with a fleet of *bounded* workers. Workers are not free agents: they run behind
the supervisor with a narrowed identity (``worker:<name>``), a fixed capability scope and a tool
allow-list. Code - not prompt text - enforces that a worker cannot touch governance, approve its own
work, grant itself permissions, use credentials outside the broker, or bypass the supervisor's limits.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core import ConflictError, NotFoundError, PolicyDenied
from ..core.clock import iso
from ..core.risk import RiskLevel, max_risk
from ..events import EventKind, EventLog, get_event_log
from ..security.policy import Capability
from .engine import MissionEngine
from .models import Mission, Priority

#: Capabilities no worker may ever hold, in any profile - re-checked on registration *and* per call.
FORBIDDEN_WORKER_CAPABILITIES = {
    Capability.CREDENTIAL_ADMIN,
    Capability.GOVERNANCE_WRITE,
    Capability.IDENTITY_WRITE,
    Capability.SYSTEM_CONFIG,
    Capability.UPGRADE_APPLY,
    Capability.MCP_INSTALL,
    Capability.SKILL_INSTALL,
}


@dataclass(frozen=True)
class WorkerProfile:
    """What a worker role may do. Frozen: a worker cannot mutate its own profile."""

    role: str
    description: str
    capabilities: tuple[Capability, ...]
    max_risk: RiskLevel = RiskLevel.MEDIUM
    allowed_tools: tuple[str, ...] = ()
    max_concurrent_tasks: int = 1
    timeout_seconds: float = 600.0
    can_spawn: bool = False
    can_write_files: bool = False
    memory_scope: str = "task"
    requires_verification: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "description": self.description,
            "capabilities": [capability.value for capability in self.capabilities],
            "max_risk": self.max_risk.name, "allowed_tools": list(self.allowed_tools),
            "max_concurrent_tasks": self.max_concurrent_tasks, "timeout_seconds": self.timeout_seconds,
            "can_spawn": self.can_spawn, "can_write_files": self.can_write_files,
            "memory_scope": self.memory_scope, "requires_verification": self.requires_verification,
        }

    def permits(self, capability: Capability) -> bool:
        return capability in self.capabilities and capability not in FORBIDDEN_WORKER_CAPABILITIES

    def permits_tool(self, name: str) -> bool:
        if not self.allowed_tools:
            return True
        return any(name == allowed or (allowed.endswith("*") and name.startswith(allowed[:-1]))
                   for allowed in self.allowed_tools)


def _profile(role: str, description: str, capabilities: tuple[Capability, ...], **kwargs: Any) -> WorkerProfile:
    cleaned = tuple(capability for capability in capabilities
                    if capability not in FORBIDDEN_WORKER_CAPABILITIES)
    return WorkerProfile(role=role, description=description, capabilities=cleaned, **kwargs)


#: The shipped worker fleet. Roles are conservative by construction.
DEFAULT_WORKER_PROFILES: dict[str, WorkerProfile] = {
    "researcher": _profile(
        "researcher", "Search, fetch and read external sources, then summarise with citations.",
        (Capability.NET_HTTP, Capability.FS_READ, Capability.FS_LIST, Capability.MEMORY_READ,
         Capability.MEMORY_WRITE, Capability.MODEL_CALL, Capability.ARTIFACT_WRITE),
        max_risk=RiskLevel.MEDIUM,
        allowed_tools=("http_fetch", "read_document", "fs_read", "fs_list", "remember", "recall",
                       "current_time", "json_query", "write_artifact"),
        max_concurrent_tasks=3, timeout_seconds=900,
    ),
    "coder": _profile(
        "coder", "Write and modify code inside the workspace, run tests, report failures honestly.",
        (Capability.FS_READ, Capability.FS_WRITE, Capability.FS_LIST, Capability.CODE_EXEC,
         Capability.SHELL_EXEC, Capability.MEMORY_READ, Capability.MODEL_CALL, Capability.ARTIFACT_WRITE),
        max_risk=RiskLevel.HIGH,
        allowed_tools=("fs_read", "fs_write", "fs_list", "shell", "python_exec", "recall", "remember",
                       "write_artifact", "json_query", "current_time"),
        timeout_seconds=1800, can_write_files=True,
    ),
    "analyst": _profile(
        "analyst", "Read local data, compute, and produce structured analysis.",
        (Capability.FS_READ, Capability.FS_LIST, Capability.CODE_EXEC, Capability.MEMORY_READ,
         Capability.MODEL_CALL, Capability.ARTIFACT_WRITE),
        max_risk=RiskLevel.MEDIUM,
        allowed_tools=("fs_read", "fs_list", "python_exec", "read_document", "recall", "write_artifact",
                       "json_query", "current_time"),
        max_concurrent_tasks=2,
    ),
    "writer": _profile(
        "writer", "Draft and polish documents and artifacts from provided material.",
        (Capability.FS_READ, Capability.FS_WRITE, Capability.MEMORY_READ, Capability.MODEL_CALL,
         Capability.ARTIFACT_WRITE),
        max_risk=RiskLevel.LOW,
        allowed_tools=("fs_read", "recall", "write_artifact", "fs_write", "current_time"),
        can_write_files=True,
    ),
    "reviewer": _profile(
        "reviewer", "Independently verify another worker's output against the objective.",
        (Capability.FS_READ, Capability.FS_LIST, Capability.CODE_EXEC, Capability.MEMORY_READ,
         Capability.MODEL_CALL),
        max_risk=RiskLevel.LOW,
        allowed_tools=("fs_read", "fs_list", "python_exec", "recall", "current_time"),
        can_write_files=False,
    ),
    "operator": _profile(
        "operator", "Run approved system operations (browser/computer control) under close limits.",
        (Capability.BROWSER_CONTROL, Capability.NET_HTTP, Capability.SCREEN_CAPTURE,
         Capability.INPUT_CONTROL, Capability.MEMORY_READ, Capability.MODEL_CALL),
        max_risk=RiskLevel.HIGH,
        allowed_tools=("http_fetch", "recall", "current_time"),
        timeout_seconds=900,
    ),
}


@dataclass
class WorkerTask:
    """A single unit of delegated work."""

    id: str
    role: str
    objective: str
    mission_id: str = ""
    step_id: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    priority: Priority = Priority.NORMAL
    max_steps: int = 25
    created_at: str = field(default_factory=iso)
    state: str = "queued"          # queued | running | succeeded | failed | timed_out
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    worker: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.role, "objective": self.objective, "mission_id": self.mission_id,
                "step_id": self.step_id, "state": self.state, "priority": self.priority.name,
                "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
                "result": self.result, "error": self.error, "worker": self.worker}


@dataclass
class WorkerResult:
    task_id: str
    ok: bool
    output: Any = None
    error: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "ok": self.ok, "output": self.output, "error": self.error,
                "verification": self.verification, "metrics": self.metrics}


class Worker:
    """A leased agent slot bound to one profile. Cannot be constructed without a supervisor."""

    def __init__(self, supervisor: "FleetSupervisor", profile: WorkerProfile, worker_id: str) -> None:
        self._supervisor = supervisor
        self.profile = profile
        self.id = worker_id
        self.actor = f"worker:{profile.role}"
        self.state = "idle"
        self.current_task: str = ""
        self._started = time.time()
        self.tasks_completed = 0
        self.tasks_failed = 0

    # -- the worker API ------------------------------------------------------ #
    async def handle(self, task: WorkerTask) -> WorkerResult:
        """Execute a task under the supervisor's supervision."""
        if task.role != self.profile.role:
            raise ConflictError(f"worker {self.id} ({self.profile.role}) cannot run a {task.role} task")
        self.state, self.current_task = "running", task.id
        try:
            return await self._supervisor._run_task(self, task)
        finally:
            self.state, self.current_task = "idle", ""

    def can(self, capability: Capability | str) -> bool:
        return self.profile.permits(Capability.parse(capability))

    def require(self, capability: Capability | str) -> None:
        parsed = Capability.parse(capability)
        if not self.can(parsed):
            raise PolicyDenied(f"worker {self.id} may not use {parsed.value}",
                               worker=self.id, role=self.profile.role)

    def available_tools(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self._supervisor.tools.for_actor(
            self.actor, mission_scope=self.profile.capabilities) if self.profile.permits_tool(spec.name)]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "role": self.profile.role, "actor": self.actor, "state": self.state,
                "current_task": self.current_task, "tasks_completed": self.tasks_completed,
                "tasks_failed": self.tasks_failed, "uptime_seconds": round(time.time() - self._started, 1),
                "tools": [spec["name"] for spec in self.available_tools()]}


Handler = Callable[[WorkerTask, Worker], Awaitable[dict[str, Any]]]


class FleetSupervisor:
    """Owns the workers, the queue and the rules."""

    def __init__(self, *, tools: Any, missions: MissionEngine | None = None, log: EventLog | None = None,
                 profiles: dict[str, WorkerProfile] | None = None, max_workers: int = 8) -> None:
        self.tools = tools
        self.missions = missions
        self.log = log or get_event_log()
        self.profiles = dict(profiles or DEFAULT_WORKER_PROFILES)
        self.max_workers = max_workers
        self._workers: dict[str, Worker] = {}
        self._tasks: dict[str, WorkerTask] = {}
        self._handlers: dict[str, Handler] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ config
    def register_profile(self, profile: WorkerProfile) -> WorkerProfile:
        for capability in profile.capabilities:
            if capability in FORBIDDEN_WORKER_CAPABILITIES:
                raise ConflictError(f"profile {profile.role!r} requests forbidden capability "
                                    f"{capability.value!r}; workers cannot hold it")
        with self._lock:
            self.profiles[profile.role] = profile
        return profile

    def register_handler(self, role: str, handler: Handler) -> None:
        """The code that actually does a role's work (usually a mission + tool loop)."""
        with self._lock:
            self._handlers[role] = handler

    def profile(self, role: str) -> WorkerProfile:
        with self._lock:
            profile = self.profiles.get(role)
        if profile is None:
            raise NotFoundError(f"no worker profile for role {role!r}")
        return profile

    # ------------------------------------------------------------------ workers
    def spawn(self, role: str) -> Worker:
        profile = self.profile(role)
        with self._lock:
            live = [worker for worker in self._workers.values() if worker.profile.role == role]
            if len(live) >= profile.max_concurrent_tasks:
                if live:
                    return min(live, key=lambda worker: len(worker.current_task))
                raise ConflictError(f"worker limit reached for role {role!r}")
            worker = Worker(self, profile, f"wrk_{uuid.uuid4().hex[:8]}")
            self._workers[worker.id] = worker
        self.log.append(EventKind.MISSION,
                        {"action": "worker_spawned", "worker": worker.id, "role": role,
                         "capabilities": [capability.value for capability in profile.capabilities]},
                        actor="supervisor", source="missions.supervisor", risk=RiskLevel.LOW)
        return worker

    def release(self, worker_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(worker_id, None)
        if worker is not None:
            self.log.append(EventKind.MISSION,
                            {"action": "worker_released", "worker": worker_id, "role": worker.profile.role,
                             "completed": worker.tasks_completed, "failed": worker.tasks_failed},
                            actor="supervisor", source="missions.supervisor", risk=RiskLevel.LOW)

    def workers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [worker.to_dict() for worker in self._workers.values()]

    def worker(self, worker_id: str) -> Worker:
        with self._lock:
            worker = self._workers.get(worker_id)
        if worker is None:
            raise NotFoundError(f"worker {worker_id!r} is not running")
        return worker

    # ------------------------------------------------------------------ delegation
    def tasks(self, *, state: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._tasks.values())
        if state:
            items = [task for task in items if task.state == state]
        items.sort(key=lambda task: (task.priority.value, task.created_at), reverse=True)
        return [task.to_dict() for task in items[:limit]]

    async def delegate(self, role: str, objective: str, *, context: dict[str, Any] | None = None,
                       mission_id: str = "", step_id: str = "", priority: Priority = Priority.NORMAL,
                       actor: str = "model:main", timeout_seconds: float | None = None) -> WorkerResult:
        """Run a task on a worker of the given role, with a hard timeout and full audit."""
        profile = self.profile(role)
        task = WorkerTask(id=f"tsk_{uuid.uuid4().hex[:10]}", role=role, objective=objective,
                          mission_id=mission_id, step_id=step_id, context=dict(context or {}),
                          priority=priority)
        with self._lock:
            self._tasks[task.id] = task
            worker = self._find_idle(role) or self.spawn(role)
        task.worker = worker.id
        self.log.append(EventKind.MISSION,
                        {"action": "delegated", "task_id": task.id, "role": role, "worker": worker.id,
                         "objective": objective[:200], "mission_id": mission_id, "step_id": step_id},
                        actor=actor, source="missions.supervisor", mission_id=mission_id,
                        risk=RiskLevel.MEDIUM)
        limit = timeout_seconds or profile.timeout_seconds
        try:
            result = await asyncio.wait_for(worker.handle(task), timeout=limit)
            task.state = "succeeded" if result.ok else "failed"
            task.error = result.error
            task.result = result.to_dict()
        except asyncio.TimeoutError:
            task.state = "timed_out"
            task.error = f"worker exceeded {limit}s"
            worker.tasks_failed += 1
            result = WorkerResult(task.id, False, error=task.error)
        except Exception as exc:
            task.state = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            worker.tasks_failed += 1
            result = WorkerResult(task.id, False, error=task.error)
        else:
            if result.ok:
                worker.tasks_completed += 1
            else:
                worker.tasks_failed += 1
        finally:
            task.finished_at = iso()
            self.log.append(EventKind.MISSION,
                            {"action": "task_finished", "task_id": task.id, "role": role,
                             "worker": worker.id, "state": task.state, "error": task.error[:300]},
                            actor=worker.actor, source="missions.supervisor", mission_id=mission_id,
                            risk=RiskLevel.LOW if result.ok else RiskLevel.MEDIUM)
        return result

    def _find_idle(self, role: str) -> Worker | None:
        for worker in self._workers.values():
            if worker.profile.role == role and worker.state == "idle":
                return worker
        return None

    # ------------------------------------------------------------------ execution
    async def _run_task(self, worker: Worker, task: WorkerTask) -> WorkerResult:
        """Authorise, execute and verify one task."""
        profile = worker.profile
        task.started_at = iso()
        denials = self._denials(worker, task)
        if denials:
            raise PolicyDenied(f"worker {worker.id} ({profile.role}) may not run this task: "
                               + "; ".join(denials), worker=worker.id, role=profile.role, denials=denials)
        handler = self._handlers.get(profile.role, self._default_handler)
        started = time.perf_counter()
        payload = await handler(task, worker)
        result = WorkerResult(
            task_id=task.id, ok=bool(payload.get("ok", True)), output=payload.get("output"),
            error=payload.get("error", ""), verification=payload.get("verification", {}),
            metrics={"duration_ms": round((time.perf_counter() - started) * 1000, 2),
                     "role": profile.role, "risk": payload.get("risk") or self._task_risk(task).name,
                     "worker": worker.id},
        )
        # Workers never self-approve: anything needing approval comes back to the supervisor queue.
        if not result.ok and "approval" in result.error.lower():
            result.verification = {**result.verification, "escalated_to_supervisor": True}
        return result

    @staticmethod
    def _task_risk(task: WorkerTask) -> RiskLevel:
        raw = task.context.get("risks") or ["LOW"]
        levels: list[RiskLevel] = []
        for item in raw if isinstance(raw, list) else [raw]:
            text = str(item).upper()
            levels.append(RiskLevel[text] if text in RiskLevel.__members__ else RiskLevel.LOW)
        return max_risk(*levels) if levels else RiskLevel.LOW

    def _denials(self, worker: Worker, task: WorkerTask) -> list[str]:
        """Every reason this worker may not run this task (empty list means permitted)."""
        reasons: list[str] = []
        risk = self._task_risk(task)
        if risk > worker.profile.max_risk:
            reasons.append(f"risk {risk.name} exceeds the profile ceiling {worker.profile.max_risk.name}")
        for capability_text in task.context.get("requires_capabilities") or []:
            capability = Capability.parse(capability_text)
            if not worker.can(capability):
                extra = " (forbidden to all workers)" if capability in FORBIDDEN_WORKER_CAPABILITIES else ""
                reasons.append(f"capability {capability.value} is outside the profile{extra}")
        for tool in task.context.get("requires_tools") or []:
            if not worker.profile.permits_tool(tool):
                reasons.append(f"tool {tool!r} is not in the profile allow-list")
        return reasons

    def _authorised(self, worker: Worker, task: WorkerTask) -> bool:
        return not self._denials(worker, task)

    async def _default_handler(self, task: WorkerTask, worker: Worker) -> dict[str, Any]:
        """Default: hand the task to the mission engine with the worker's narrowed identity."""
        if self.missions is None:
            return {"ok": False, "error": "no mission engine attached to the supervisor", "output": None}
        mission = self.missions.create(
            task.objective, title=f"[{worker.profile.role}] {task.objective[:60]}",
            success_criteria=list(task.context.get("success_criteria") or []),
            parent_id=task.mission_id, created_by=worker.actor,
            metadata={**{key: value for key, value in task.context.items() if isinstance(key, str)},
                      "worker": worker.id, "role": worker.profile.role},
        )
        # The worker's own step budget bounds the sub-mission - a worker cannot outspend its lease.
        result = await self.missions.run(mission.id, actor=worker.actor, worker=worker.id,
                                         max_steps=max(1, int(task.max_steps)))
        return {"ok": result.state.value == "succeeded", "output": result.mission.result,
                "error": result.error, "verification": result.mission.verification,
                "mission_id": result.mission.id}

    # ------------------------------------------------------------------ introspection
    def stats(self) -> dict[str, Any]:
        with self._lock:
            workers = list(self._workers.values())
            tasks = list(self._tasks.values())
        return {
            "workers": len(workers),
            "by_role": {role: sum(1 for worker in workers if worker.profile.role == role)
                        for role in {worker.profile.role for worker in workers}},
            "tasks": {"total": len(tasks), **_count_by(tasks)},
            "profiles": sorted(self.profiles),
        }


def _count_by(tasks: list[WorkerTask]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.state] = counts.get(task.state, 0) + 1
    return counts


_SUPERVISOR: FleetSupervisor | None = None
_LOCK = threading.Lock()


def get_supervisor(**kwargs: Any) -> FleetSupervisor:
    global _SUPERVISOR
    with _LOCK:
        if _SUPERVISOR is None:
            _SUPERVISOR = FleetSupervisor(**kwargs)
        return _SUPERVISOR


def reset_supervisor() -> None:
    global _SUPERVISOR
    with _LOCK:
        _SUPERVISOR = None
