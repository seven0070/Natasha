"""Missions: create, inspect, run, pause, resume, verify and roll back."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...missions.models import MissionState
from ..deps import rate_limit, audit, get_runtime, handle, require_owner
from ..models import MissionBody

router = APIRouter(prefix="/missions", tags=["missions"])


def _engine(request: Request) -> Any:
    runtime = get_runtime(request)
    engine = runtime.missions
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the mission engine is not available")
    return engine


@router.get("")
async def list_missions(request: Request, state: str = "", limit: int = 50,
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    engine = _engine(request)
    parsed = MissionState(state) if state else None
    missions = engine.store.list(state=parsed, limit=min(limit, 500))
    return {"missions": [mission.to_dict(include_steps=False) for mission in missions]}


@router.post("")
async def create_mission(body: MissionBody, request: Request, actor: str = Depends(require_owner),
                         limited: None = Depends(rate_limit("missions"))) -> dict[str, Any]:
    engine = _engine(request)
    try:
        mission = engine.create(body.objective, title=body.title, success_criteria=body.success_criteria,
                                verification_plan=body.verification_plan, scope=body.scope, created_by=actor)
        if body.steps:
            engine.plan(mission.id, body.steps)
        audit(get_runtime(request), "mission_created",
              {"mission_id": mission.id, "steps": len(body.steps)})
        if body.auto_run and body.steps:
            result = await engine.run(mission.id, actor=actor)
            return {"mission": engine.get(mission.id).to_dict(), "result": result.to_dict()}
    except Exception as exc:
        raise handle(exc) from exc
    return {"mission": mission.to_dict()}


@router.get("/stats")
async def stats(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return _engine(request).stats()


@router.get("/{mission_id}")
async def get_mission(mission_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _engine(request).get(mission_id).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{mission_id}/run")
async def run_mission(mission_id: str, request: Request, actor: str = Depends(require_owner),
                      limited: None = Depends(rate_limit("missions"))) -> dict[str, Any]:
    try:
        result = await _engine(request).run(mission_id, actor=actor)
        return result.to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{mission_id}/pause")
async def pause_mission(mission_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _engine(request).pause(mission_id, actor=actor).summary()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{mission_id}/resume")
async def resume_mission(mission_id: str, request: Request, actor: str = Depends(require_owner),
                         limited: None = Depends(rate_limit("missions"))) -> dict[str, Any]:
    try:
        result = await _engine(request).resume(mission_id, actor=actor)
        return result.to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{mission_id}/cancel")
async def cancel_mission(mission_id: str, request: Request, reason: str = "",
                         actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _engine(request).cancel(mission_id, reason=reason, actor=actor).summary()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{mission_id}/verify")
async def verify_mission(mission_id: str, request: Request, actor: str = Depends(require_owner),
                         limited: None = Depends(rate_limit("missions"))) -> dict[str, Any]:
    engine = _engine(request)
    try:
        report = await engine.verify(engine.get(mission_id), actor=actor)
        return report.to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{mission_id}/rollback")
async def rollback_mission(mission_id: str, request: Request, reason: str = "",
                           actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return await _engine(request).rollback(mission_id, actor=actor, reason=reason)
    except Exception as exc:
        raise handle(exc) from exc


@router.get("/resumable/list")
async def resumable(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"missions": [mission.summary() for mission in _engine(request).store.resumable()]}
