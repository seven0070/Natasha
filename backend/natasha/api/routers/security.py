"""Security: credentials, the broker, the audit trail and secret hygiene."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import CredentialBody

router = APIRouter(prefix="/security", tags=["security"])


def _credentials(request: Request) -> Any:
    manager = get_runtime(request).credentials
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the credential manager is unavailable")
    return manager


@router.get("/credentials")
async def list_credentials(request: Request, provider: str = "",
                           actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _credentials(request)
    return {"credentials": manager.list(provider=provider), "stats": manager.stats()}


@router.post("/credentials")
async def store_credential(body: CredentialBody, request: Request,
                           actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Store a credential. The secret is written to the encrypted vault, never echoed back."""
    manager = _credentials(request)
    reference = body.name if body.name.startswith("credential://") else f"credential://{body.name}"
    try:
        metadata = manager.store(reference, body.secret, kind=body.kind, provider=body.name,
                                 metadata=body.metadata, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "credential_stored", {"reference": reference, "kind": body.kind},
          risk="HIGH")  # type: ignore[arg-type]
    return metadata.to_dict()


@router.post("/credentials/{name}/rotate")
async def rotate_credential(name: str, body: CredentialBody, request: Request,
                            actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _credentials(request)
    reference = name if name.startswith("credential://") else f"credential://{name}"
    try:
        metadata = manager.rotate(reference, body.secret, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "credential_rotated", {"reference": reference}, risk="HIGH")  # type: ignore[arg-type]
    return metadata.to_dict()


@router.post("/credentials/{name}/revoke")
async def revoke_credential(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _credentials(request)
    reference = name if name.startswith("credential://") else f"credential://{name}"
    try:
        manager.revoke(reference, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "credential_revoked", {"reference": reference}, risk="HIGH")  # type: ignore[arg-type]
    return {"ok": True, "reference": reference}


@router.delete("/credentials/{name}")
async def delete_credential(name: str, request: Request, purge: bool = True,
                            actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _credentials(request)
    reference = name if name.startswith("credential://") else f"credential://{name}"
    try:
        manager.delete(reference, actor=actor, purge_history=purge)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "credential_deleted", {"reference": reference}, risk="HIGH")  # type: ignore[arg-type]
    return {"ok": True, "deleted": reference}


@router.get("/credentials/{name}/health")
async def credential_health(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _credentials(request)
    reference = name if name.startswith("credential://") else f"credential://{name}"
    try:
        return manager.health(reference)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/credentials/{name}/test")
async def credential_test(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _credentials(request)
    reference = name if name.startswith("credential://") else f"credential://{name}"
    try:
        return manager.test_connection(reference, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc


@router.get("/broker")
async def broker(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """What the broker will and will not do. Secrets are never part of the answer."""
    runtime = get_runtime(request)
    broker_object = runtime.broker
    if broker_object is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the credential broker is unavailable")
    describe = getattr(broker_object, "describe", None)
    return describe() if callable(describe) else {"active_ttl_seconds": 60,
                                                  "note": "handles are short-lived and audience-scoped"}


@router.get("/audit")
async def audit_trail(request: Request, limit: int = 200, min_risk: str = "",
                      actor: str = Depends(require_owner)) -> dict[str, Any]:
    from ...core.risk import RiskLevel

    runtime = get_runtime(request)
    parsed = RiskLevel.parse(min_risk) if min_risk else None
    rows = runtime.log.query(min_risk=parsed, limit=min(limit, 2000))
    return {"events": [event.to_dict() for event in rows]}


@router.get("/sessions")
async def api_sessions(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    auth = getattr(request.app.state, "auth", None)
    return {"sessions": auth.sessions() if auth is not None else []}
