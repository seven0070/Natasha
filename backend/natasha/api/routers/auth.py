"""Owner authentication endpoints."""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import AuthError
from ..deps import audit, handle, rate_limit, require_owner
from ..models import ChangePassphraseBody, LoginBody, SetupBody

router = APIRouter(prefix="/auth", tags=["auth"])


def _auth(request: Request) -> Any:
    manager = getattr(request.app.state, "auth", None)
    if manager is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication is unavailable")
    return manager


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return client.host in ("localhost", "testclient")


@router.get("/status")
async def auth_status(request: Request) -> dict[str, Any]:
    manager = _auth(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
        request.headers.get("x-natasha-token", "")
    session = manager.session(token) if token else None
    return {"initialised": manager.initialised(), "authenticated": session is not None,
            "owner_id": session.owner_id if session else "",
            "loopback": _is_loopback(request)}


@router.post("/setup")
async def setup(body: SetupBody, request: Request,
                limited: None = Depends(rate_limit("login"))) -> dict[str, Any]:
    manager = _auth(request)
    if not _is_loopback(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner setup is only allowed from this machine")
    try:
        token = manager.setup(body.passphrase, owner_id=body.owner_id)
    except Exception as exc:
        raise handle(exc) from exc
    audit(getattr(request.app.state, "runtime", None), "owner_setup", {"owner_id": body.owner_id})
    return {"ok": True, "token": token, "owner_id": manager.owner_id}


@router.post("/login")
async def login(body: LoginBody, request: Request,
                limited: None = Depends(rate_limit("login"))) -> dict[str, Any]:
    manager = _auth(request)
    try:
        token = manager.login(body.passphrase, client=body.client)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return {"ok": True, "token": token, "owner_id": manager.owner_id}


@router.post("/logout")
async def logout(request: Request) -> dict[str, Any]:
    manager = _auth(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or \
        request.headers.get("x-natasha-token", "")
    manager.logout(token)
    return {"ok": True}


@router.get("/sessions")
async def sessions(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _auth(request)
    return {"sessions": manager.sessions()}


@router.post("/sessions/revoke")
async def revoke_sessions(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _auth(request)
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    removed = manager.revoke_all(keep=token)
    audit(getattr(request.app.state, "runtime", None), "sessions_revoked", {"count": removed})
    return {"ok": True, "revoked": removed}


@router.post("/passphrase")
async def change_passphrase(body: ChangePassphraseBody, request: Request,
                            actor: str = Depends(require_owner)) -> dict[str, Any]:
    manager = _auth(request)
    try:
        manager.change_passphrase(body.current, body.new)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    except Exception as exc:
        raise handle(exc) from exc
    audit(getattr(request.app.state, "runtime", None), "passphrase_changed", {})
    return {"ok": True, "note": "all sessions were revoked; log in again"}
