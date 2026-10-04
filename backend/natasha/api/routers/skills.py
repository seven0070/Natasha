"""Skills: inspect, install, test, enable, disable, run and uninstall."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import SkillRunBody

router = APIRouter(prefix="/skills", tags=["skills"])


def _lifecycle(request: Request) -> Any:
    lifecycle = get_runtime(request).skills
    if lifecycle is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the skill subsystem is unavailable")
    return lifecycle


@router.get("")
async def list_skills(request: Request, state_filter: str = "", actor: str = Depends(require_owner)) -> dict[str, Any]:
    lifecycle = _lifecycle(request)
    return {"skills": [record.to_dict() for record in lifecycle.list(state=state_filter)]}


@router.get("/{skill_id}")
async def get_skill(skill_id: str, request: Request, version: str = "",
                    actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _lifecycle(request).get(skill_id, version=version).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/install")
async def install_skill(request: Request, path: str, activate: bool = True,
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Install a skill from a local directory. Validation and scans run first."""
    lifecycle = _lifecycle(request)
    try:
        record = lifecycle.install(path, activate=activate, approved_by=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "skill_installed", {"skill_id": record.skill_id, "path": path},
          risk="HIGH")  # type: ignore[arg-type]
    return record.to_dict()


@router.post("/{skill_id}/test")
async def test_skill(skill_id: str, request: Request, version: str = "",
                     actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return await _lifecycle(request).test_async(skill_id, version=version)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{skill_id}/enable")
async def enable_skill(skill_id: str, request: Request, version: str = "",
                       actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _lifecycle(request).activate(skill_id, version=version).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{skill_id}/disable")
async def disable_skill(skill_id: str, request: Request, version: str = "",
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _lifecycle(request).disable(skill_id, version=version).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{skill_id}/rollback")
async def rollback_skill(skill_id: str, request: Request, to_version: str,
                         actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _lifecycle(request).rollback(skill_id, to_version=to_version).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.delete("/{skill_id}")
async def uninstall_skill(skill_id: str, request: Request, version: str = "", purge: bool = False,
                          actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        record = _lifecycle(request).uninstall(skill_id, version=version, purge=purge)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "skill_uninstalled", {"skill_id": skill_id}, risk="MEDIUM")  # type: ignore[arg-type]
    return record.to_dict()


@router.post("/{skill_id}/run")
async def run_skill(skill_id: str, body: SkillRunBody, request: Request,
                    actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.skill_runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the skill runtime is unavailable")
    try:
        result = await runtime.skill_runtime.run(skill_id, body.payload, version=body.version, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    return result.to_dict()


@router.get("/{skill_id}/scan")
async def scan_skill(skill_id: str, request: Request, version: str = "",
                     actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _lifecycle(request).scan(skill_id, version=version)
    except Exception as exc:
        raise handle(exc) from exc
