"""Agents: the worker fleet, the supervisor queue, and delegation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import DelegationBody

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def roles(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.agents is None:
        return {"roles": []}
    return {"roles": runtime.agents.roles(), "status": runtime.agents.status()}


@router.get("/workers")
async def workers(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.supervisor is None:
        return {"workers": [], "tasks": []}
    return {"workers": runtime.supervisor.workers(), "tasks": runtime.supervisor.tasks(),
            "stats": runtime.supervisor.stats()}


@router.get("/profiles")
async def profiles(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.supervisor is None:
        return {"profiles": {}}
    return {"profiles": {role: profile.to_dict() for role, profile in runtime.supervisor.profiles.items()}}


@router.post("/delegate")
async def delegate(body: DelegationBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.agents is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the agent team is unavailable")
    try:
        result = await runtime.agents.delegate(body.role, body.objective, context=body.context,
                                               actor=actor, timeout_seconds=body.timeout_seconds)
    except Exception as exc:
        raise handle(exc) from exc
    audit(runtime, "delegated", {"role": body.role, "objective": body.objective[:200],
                                 "ok": bool(result.get("ok"))}, risk="MEDIUM")  # type: ignore[arg-type]
    return result
