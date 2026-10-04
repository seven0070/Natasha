"""Mission data model."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any

from ..core import new_id
from ..core.clock import iso
from ..core.risk import RiskLevel
from ..verification.checks import VerificationCheck


class Priority(int, enum.Enum):
    """Scheduling priority. Lower numbers run first (matches the SQLite ordering)."""

    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    BACKGROUND = 5

    @classmethod
    def parse(cls, value: object) -> "Priority":
        if isinstance(value, Priority):
            return value
        if isinstance(value, int):
            return cls(min(5, max(1, value)))
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            return cls.NORMAL


class MissionState(str, enum.Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    BLOCKED = "blocked"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"

    @property
    def terminal(self) -> bool:
        return self in (MissionState.SUCCEEDED, MissionState.FAILED, MissionState.CANCELLED,
                        MissionState.ROLLED_BACK)

    @property
    def active(self) -> bool:
        return not self.terminal


class StepState(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"
    WAITING_APPROVAL = "waiting_approval"


class StepKind(str, enum.Enum):
    TOOL = "tool"
    MODEL = "model"
    VERIFY = "verify"
    DELEGATE = "delegate"
    NOTE = "note"


@dataclass
class MissionStep:
    """One unit of work inside a mission."""

    title: str
    kind: StepKind = StepKind.TOOL
    id: str = field(default_factory=lambda: new_id("stp"))
    description: str = ""
    tool: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)     # model/delegate inputs
    state: StepState = StepState.PENDING
    order: int = 0
    risk: RiskLevel = RiskLevel.LOW
    reversible: bool = True
    requires_approval: bool = False
    attempts: int = 0
    max_attempts: int = 3
    result: Any = None
    error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "kind": self.kind.value, "description": self.description,
                "tool": self.tool, "arguments": self.arguments, "payload": self.payload,
                "state": self.state.value, "order": self.order, "risk": self.risk.name,
                "reversible": self.reversible, "requires_approval": self.requires_approval,
                "attempts": self.attempts, "max_attempts": self.max_attempts,
                "result": _bound(self.result), "error": self.error, "evidence": self.evidence,
                "started_at": self.started_at, "finished_at": self.finished_at, "depends_on": self.depends_on}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MissionStep":
        risk = data.get("risk", "LOW")
        return cls(
            title=data.get("title", ""), kind=StepKind(data.get("kind", "tool")), id=data.get("id", new_id("stp")),
            description=data.get("description", ""), tool=data.get("tool", ""),
            arguments=data.get("arguments") or {}, payload=data.get("payload") or {},
            state=StepState(data.get("state", "pending")), order=int(data.get("order", 0)),
            risk=RiskLevel[risk] if isinstance(risk, str) and risk in RiskLevel.__members__ else RiskLevel.LOW,
            reversible=bool(data.get("reversible", True)),
            requires_approval=bool(data.get("requires_approval", False)),
            attempts=int(data.get("attempts", 0)), max_attempts=int(data.get("max_attempts", 3)),
            result=data.get("result"), error=data.get("error", ""), evidence=data.get("evidence") or {},
            started_at=data.get("started_at", ""), finished_at=data.get("finished_at", ""),
            depends_on=list(data.get("depends_on") or []),
        )


@dataclass
class Mission:
    """A durable goal the owner gave Natasha."""

    objective: str
    id: str = field(default_factory=lambda: new_id("msn"))
    title: str = ""
    state: MissionState = MissionState.DRAFT
    steps: list[MissionStep] = field(default_factory=list)
    plan_summary: str = ""
    success_criteria: list[str] = field(default_factory=list)
    verification_plan: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    deadline: str = ""
    priority: int = 3
    parent_id: str = ""
    created_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    started_at: str = ""
    finished_at: str = ""
    attempts: int = 0
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_by: str = "owner"

    # -- progress -------------------------------------------------------------- #
    @property
    def progress(self) -> float:
        if not self.steps:
            return 1.0 if self.state is MissionState.SUCCEEDED else 0.0
        done = sum(1 for step in self.steps if step.state in (StepState.DONE, StepState.SKIPPED))
        return round(done / len(self.steps), 4)

    @property
    def current_step(self) -> MissionStep | None:
        for step in sorted(self.steps, key=lambda item: item.order):
            if step.state in (StepState.PENDING, StepState.READY, StepState.FAILED, StepState.RUNNING):
                return step
        return None

    def step(self, step_id: str) -> MissionStep | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id, "objective": self.objective, "title": self.title or self.objective[:80],
            "state": self.state.value, "plan_summary": self.plan_summary,
            "success_criteria": self.success_criteria, "verification_plan": self.verification_plan,
            "constraints": self.constraints, "scope": self.scope, "artifacts": self.artifacts,
            "deadline": self.deadline, "priority": self.priority, "parent_id": self.parent_id,
            "created_at": self.created_at, "updated_at": self.updated_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "attempts": self.attempts, "error": self.error,
            "result": _bound(self.result), "verification": self.verification, "progress": self.progress,
            "created_by": self.created_by, "metadata": self.metadata,
        }
        if include_steps:
            data["steps"] = [step.to_dict() for step in sorted(self.steps, key=lambda item: item.order)]
        return data


def _bound(value: Any, limit: int = 6000) -> Any:
    """Keep stored results small; the full payload lives in the artifacts/event log."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}
