"""Computer and browser control endpoints.

Every action is gated by the policy engine (and the approval engine when required). When the host has
no automation backend, the endpoints say so - they never pretend a click happened.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import rate_limit, audit, get_runtime, handle, require_owner
from ..models import BrowserBody

router = APIRouter(prefix="/computer", tags=["computer"])


def _controller(request: Request) -> Any:
    controller = get_runtime(request).computer
    if controller is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the computer controller is unavailable")
    return controller


def _browser(request: Request) -> Any:
    browser = get_runtime(request).browser
    if browser is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the browser controller is unavailable")
    return browser


@router.get("/capabilities")
async def capabilities(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {"computer": runtime.computer.capabilities() if runtime.computer else {"available": False},
            "browser": runtime.browser.capabilities() if runtime.browser else {"available": False}}


@router.get("/screen/size")
async def screen_size(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _controller(request).screen_size()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/screenshot")
async def screenshot(request: Request, name: str = "", actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        record = _controller(request).screenshot(name=name, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "computer_screenshot", {"path": record.get("artifact", "")},
          risk="MEDIUM")  # type: ignore[arg-type]
    return record


@router.post("/click")
async def click(request: Request, x: int, y: int, button: str = "left", clicks: int = 1,
                approval_id: str = "", actor: str = Depends(require_owner),
                limited: None = Depends(rate_limit("computer"))) -> dict[str, Any]:
    try:
        return _controller(request).click(x, y, button=button, clicks=clicks, actor=actor,
                                          approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/type")
async def type_text(request: Request, text: str, approval_id: str = "",
                    actor: str = Depends(require_owner),
                    limited: None = Depends(rate_limit("computer"))) -> dict[str, Any]:
    try:
        return _controller(request).type_text(text, actor=actor, approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/key")
async def press(request: Request, key: str, presses: int = 1, approval_id: str = "",
                actor: str = Depends(require_owner),
                limited: None = Depends(rate_limit("computer"))) -> dict[str, Any]:
    try:
        return _controller(request).press(key, presses=presses, actor=actor, approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/launch")
async def launch(request: Request, command: str, approval_id: str = "",
                 actor: str = Depends(require_owner),
                 limited: None = Depends(rate_limit("computer"))) -> dict[str, Any]:
    try:
        return _controller(request).launch(command, actor=actor, approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc


@router.get("/clipboard")
async def clipboard(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _controller(request).clipboard_read(actor=actor)
    except Exception as exc:
        raise handle(exc) from exc


@router.get("/history")
async def history(request: Request, limit: int = 50, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"actions": _controller(request).history(limit=limit)}


# --------------------------------------------------------------------------- browser
@router.post("/browser/open")
async def browser_open(body: BrowserBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _browser(request).open(body.url, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/browser/read")
async def browser_read(body: BrowserBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _browser(request).read(body.url, max_chars=body.max_chars, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/browser/search")
async def browser_search(request: Request, query: str, engine: str = "duckduckgo",
                         actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _browser(request).search(query, engine=engine, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/browser/click")
async def browser_click(body: BrowserBody, request: Request, approval_id: str = "",
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _browser(request).click(body.url, body.selector, actor=actor, approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/browser/screenshot")
async def browser_screenshot(body: BrowserBody, request: Request,
                             actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _browser(request).screenshot(body.url, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
