"""Approvals: the owner's explicit, scoped, expiring permission for risky actions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import ApprovalDecisionBody

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _engine(request: Request) -> Any:
    engine = get_runtime(request).approvals
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the approval engine is unavailable")
    return engine


@router.get("")
async def pending(request: Request, mission_id: str = "", limit: int = 100,
                  actor: str = Depends(require_owner)) -> dict[str, Any]:
    engine = _engine(request)
    return {"pending": [item.to_dict() for item in engine.pending(mission_id=mission_id, limit=limit)]}


@router.get("/history")
async def history(request: Request, limit: int = 200, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"history": _engine(request).history(limit=min(limit, 1000))}


@router.get("/{request_id}")
async def get_request(request_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _engine(request).get_request(request_id).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{request_id}/approve")
async def approve(request_id: str, body: ApprovalDecisionBody, request: Request,
                  actor: str = Depends(require_owner)) -> dict[str, Any]:
    engine = _engine(request)
    try:
        approval = engine.approve(request_id, decided_by=actor, note=body.note,
                                  ttl_seconds=body.ttl_seconds)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "approval_granted", {"request_id": request_id, "note": body.note[:200]},
          risk="HIGH")  # type: ignore[arg-type]
    return approval.to_dict()


@router.post("/{request_id}/deny")
async def deny(request_id: str, body: ApprovalDecisionBody, request: Request,
               actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        approval = _engine(request).deny(request_id, decided_by=actor, note=body.note)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "approval_denied", {"request_id": request_id}, risk="MEDIUM")  # type: ignore[arg-type]
    return approval.to_dict()


@router.post("/{request_id}/revoke")
async def revoke(request_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        revoked = _engine(request).revoke(request_id, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    return {"ok": True, "revoked": revoked}


@router.post("/expire")
async def expire(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"expired": _engine(request).expire_stale()}
