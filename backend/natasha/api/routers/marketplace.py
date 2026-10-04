"""Marketplace: review before install, checksum verification, rollback."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


def _installer(request: Request) -> Any:
    installer = get_runtime(request).marketplace
    if installer is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the marketplace is unavailable")
    return installer


@router.get("")
async def installed(request: Request, kind: str = "", actor: str = Depends(require_owner)) -> dict[str, Any]:
    installer = _installer(request)
    return {"installed": [item.to_dict() for item in installer.registry.list(kind=kind)],
            "stats": installer.registry.stats()}


@router.get("/sources")
async def sources(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    installer = _installer(request)
    return {"sources": installer.registry.sources()}


@router.post("/sources")
async def add_source(request: Request, source_id: str, url: str, trusted: bool = False,
                     actor: str = Depends(require_owner)) -> dict[str, Any]:
    installer = _installer(request)
    installer.registry.add_source(source_id, url, trusted=trusted)
    audit(get_runtime(request), "marketplace_source_added", {"source_id": source_id, "url": url,
                                                             "trusted": trusted}, risk="MEDIUM")  # type: ignore[arg-type]
    return {"ok": True, "sources": installer.registry.sources()}


@router.post("/review")
async def review(request: Request, ref: str, kind: str = "skill", expected_checksum: str = "",
                 actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Inspect a package without installing it: the security report is the point."""
    try:
        plan = _installer(request).review(ref, kind=kind, expected_checksum=expected_checksum)
    except Exception as exc:
        raise handle(exc) from exc
    return plan.to_dict()


@router.post("/install")
async def install(request: Request, ref: str, kind: str = "skill", expected_checksum: str = "",
                  agreement: bool = False, approval_id: str = "",
                  actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        result = _installer(request).install(ref, kind=kind, expected_checksum=expected_checksum,
                                             actor=actor, approval_id=approval_id,
                                             approve_permissions=agreement)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "marketplace_install", {"ref": ref, "kind": kind}, risk="HIGH")  # type: ignore[arg-type]
    return result


@router.post("/uninstall")
async def uninstall(request: Request, name: str, kind: str = "skill", version: str = "", purge: bool = False,
                    actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        result = _installer(request).uninstall(kind, name, version=version, actor=actor, purge=purge)
    except Exception as exc:
        raise handle(exc) from exc
    return result


@router.post("/rollback")
async def rollback(request: Request, name: str, to_version: str, kind: str = "skill",
                   actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _installer(request).rollback(kind, name, to_version=to_version, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
