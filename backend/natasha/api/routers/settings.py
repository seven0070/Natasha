"""Settings: read and update configuration (owner-only, changes audit-logged)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, require_owner
from ..models import SettingsBody

router = APIRouter(prefix="/settings", tags=["settings"])

#: Fields the API may change. Governance, security roots and the constitution are deliberately absent:
#: those change through the constitutional process, not through a settings endpoint.
MUTABLE = {"log_level", "host", "port", "deployment", "profile", "provider", "providers", "brain",
           "memory", "executive", "features", "creation", "voice", "security"}


@router.get("")
async def get_settings(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    return runtime.settings.to_dict()


@router.patch("")
async def update_settings(body: SettingsBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    rejected = [key for key in body.updates if key not in MUTABLE]
    if rejected:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"these settings cannot be changed here: {', '.join(sorted(rejected))}")
    if "security" in body.updates or "providers" in body.updates:
        # Provider endpoints and security roots are sensitive: owner-only and audited, which we are.
        pass
    try:
        runtime.update_settings(body.updates)
    except Exception as exc:
        from ..deps import handle

        raise handle(exc) from exc
    audit(runtime, "settings_updated", {"keys": sorted(body.updates)}, risk="MEDIUM")  # type: ignore[arg-type]
    return {"ok": True, "settings": runtime.settings.to_dict()}


@router.post("/save")
async def save_settings(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    path = runtime.settings.save()
    audit(runtime, "settings_saved", {"path": str(path)}, risk="MEDIUM")  # type: ignore[arg-type]
    return {"ok": True, "path": str(path)}
