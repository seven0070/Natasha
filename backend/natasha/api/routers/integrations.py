"""Integrations: email, GitHub, Slack, webhooks and plain REST - always through the broker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import ConnectBody, IntegrationBody

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _registry(request: Request) -> Any:
    registry = get_runtime(request).integrations
    if registry is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "integrations are unavailable")
    return registry


@router.get("")
async def list_connectors(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"connectors": _registry(request).describe()}


@router.post("/{name}/perform")
async def perform(name: str, body: IntegrationBody, request: Request,
                  actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Run a connector action. Responses are returned marked as untrusted external content."""
    registry = _registry(request)
    try:
        connector = registry.get(name)
        result = await connector.perform(body.action, body.arguments)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "integration_action", {"connector": name, "action": body.action,
                                                       "ok": result.ok}, risk="MEDIUM")  # type: ignore[arg-type]
    return result.to_dict()


@router.post("/{name}/connect")
async def connect(name: str, body: ConnectBody, request: Request,
                  actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Attach a credential reference and settings to a connector, and remember the choice.

    The secret itself never passes through here: the UI stores it in the vault first (`credential://`
    reference) and this route only records which reference the connector should use.
    """
    runtime = get_runtime(request)
    registry = _registry(request)
    try:
        connector = registry.get(name)
    except Exception as exc:
        raise handle(exc) from exc
    if body.credential_ref and not body.credential_ref.startswith("credential://"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "credential_ref must be a credential:// reference, never an inline secret")
    connector.credential_ref = body.credential_ref or connector.credential_ref
    if body.settings:
        connector.settings = {**getattr(connector, "settings", {}), **body.settings}
    _persist(runtime, registry)
    audit(runtime, "integration_connected", {"connector": name, "credential_ref": bool(body.credential_ref)},
          risk="MEDIUM")  # type: ignore[arg-type]
    return {"connector": connector.describe(), "connected": True}


@router.post("/{name}/disconnect")
async def disconnect(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    registry = _registry(request)
    try:
        connector = registry.get(name)
    except Exception as exc:
        raise handle(exc) from exc
    connector.credential_ref = ""
    _persist(runtime, registry)
    audit(runtime, "integration_disconnected", {"connector": name}, risk="MEDIUM")  # type: ignore[arg-type]
    return {"connector": connector.describe(), "connected": False}


def _persist(runtime: Any, registry: Any) -> None:
    """Remember connector configuration so it survives a restart (references only, never secrets)."""
    import json

    from ...core import get_paths

    target = Path(get_paths().config) / "integrations.json"
    payload = {}
    for connector in registry.all():
        described = connector.describe()
        payload[connector.name] = {
            "credential_ref": str(getattr(connector, "credential_ref", "") or ""),
            "settings": {key: value for key, value in (getattr(connector, "settings", {}) or {}).items()
                         if "secret" not in key.lower() and "token" not in key.lower()},
            "actions": [action.get("name") for action in described.get("actions", [])],
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
