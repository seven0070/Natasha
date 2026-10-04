"""Governance: the constitution, its verification, and the upgrade governor."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import UpgradeBody

router = APIRouter(prefix="/governance", tags=["governance"])


def _constitution(request: Request) -> Any:
    from ...governance import get_constitution

    runtime = get_runtime(request)
    return get_constitution(settings=runtime.settings, log=runtime.log)


def _governor(request: Request) -> Any:
    from ...governance import get_upgrade_governor

    runtime = get_runtime(request)
    return get_upgrade_governor(constitution=_constitution(request), approvals=runtime.approvals)


@router.get("/constitution")
async def constitution(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    document = _constitution(request)
    runtime = get_runtime(request)
    report = document.verify(vault=runtime.credentials, approvals=runtime.approvals)
    from ...governance.constitution import PROTECTED_AREAS

    return {"report": report.to_dict(), "protected_areas": PROTECTED_AREAS}


@router.get("/invariants")
async def invariants(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    document = _constitution(request)
    runtime = get_runtime(request)
    return {"invariants": [{"id": item.id, "statement": item.statement, "area": item.area,
                            "enforced_by": item.enforced_by}
                           for item in document.invariants(vault=runtime.credentials,
                                                           approvals=runtime.approvals)]}


@router.get("/manifest")
async def manifest(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    document = _constitution(request)
    ok, mismatches = document.verify_manifest()
    return {"ok": ok, "mismatches": mismatches, "path": str(document.manifest_path())}


@router.post("/manifest/refresh")
async def refresh_manifest(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    document = _constitution(request)
    path = document.write_manifest()
    audit(get_runtime(request), "manifest_refreshed", {"path": str(path)}, risk="HIGH")  # type: ignore[arg-type]
    return {"ok": True, "path": str(path), "entries": len(document.compute_manifest())}


@router.get("/upgrades")
async def upgrades(request: Request, status_filter: str = "", limit: int = 100,
                   actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"proposals": _governor(request).list(status=status_filter, limit=limit)}


@router.get("/upgrades/opportunities")
async def opportunities(request: Request, limit: int = 200, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"opportunities": _governor(request).detect_opportunities(limit=limit)}


@router.post("/upgrades")
async def propose(body: UpgradeBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Record a proposal. Applying it is a separate, owner-approved step."""
    governor = _governor(request)
    patch = body.changes[0].get("patch", "") if body.changes else ""
    target_files = body.changes[0].get("target_files", []) if body.changes else []
    if not patch:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a proposal needs a patch")
    try:
        proposal = governor.propose(body.summary, rationale=body.summary, patch=patch,
                                    target_files=target_files, actor=actor,
                                    metadata={"tests": body.tests})
    except Exception as exc:
        raise handle(exc) from exc
    return proposal.to_dict()


@router.post("/upgrades/{proposal_id}/analyse")
async def analyse(proposal_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    governor = _governor(request)
    try:
        proposal = governor.get(proposal_id)
        governor.analyse(proposal)
        governor.risk_report(proposal)
        return proposal.to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/upgrades/{proposal_id}/test")
async def test_proposal(proposal_id: str, request: Request, timeout: int = 600,
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    governor = _governor(request)
    try:
        proposal = governor.get(proposal_id)
        tested = governor.sandbox_test(proposal, timeout=timeout)
        return tested.to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/upgrades/{proposal_id}/request-approval")
async def request_upgrade_approval(proposal_id: str, request: Request, ttl_seconds: int = 3600,
                                   actor: str = Depends(require_owner)) -> dict[str, Any]:
    governor = _governor(request)
    try:
        approval = governor.request_approval(proposal_id, ttl_seconds=ttl_seconds)
    except Exception as exc:
        raise handle(exc) from exc
    return approval.to_dict() if hasattr(approval, "to_dict") else {"request": str(approval)}


@router.post("/upgrades/{proposal_id}/apply")
async def apply_upgrade(proposal_id: str, request: Request, approval_id: str = "",
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    governor = _governor(request)
    try:
        proposal = governor.apply(proposal_id, actor=actor, approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "upgrade_applied", {"proposal_id": proposal_id}, risk="CRITICAL")  # type: ignore[arg-type]
    return proposal.to_dict()


@router.post("/upgrades/{proposal_id}/rollback")
async def rollback_upgrade(proposal_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    governor = _governor(request)
    try:
        proposal = governor.rollback(proposal_id, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "upgrade_rolled_back", {"proposal_id": proposal_id}, risk="HIGH")  # type: ignore[arg-type]
    return proposal.to_dict()
