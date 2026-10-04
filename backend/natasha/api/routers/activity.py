"""Activity: the append-only event log, read and verified - never rewritten."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from ...core import get_paths
from ...core.risk import RiskLevel
from ..deps import audit, get_runtime, handle, require_owner

router = APIRouter(prefix="/activity", tags=["activity"])


def _log(request: Request) -> Any:
    log = get_runtime(request).log
    if log is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the event log is unavailable")
    return log


@router.get("")
async def events(request: Request, kind: str = "", mission_id: str = "", trace_id: str = "",
                 actor_filter: str = "", search: str = "", min_risk: str = "", limit: int = 200,
                 since_seq: int = 0, descending: bool = True,
                 actor: str = Depends(require_owner)) -> dict[str, Any]:
    log = _log(request)
    parsed_risk = RiskLevel.parse(min_risk) if min_risk else None
    rows = log.query(kinds=[kind] if kind else None, mission_id=mission_id, trace_id=trace_id,
                     actor=actor_filter, search=search, min_risk=parsed_risk, limit=min(limit, 2000),
                     since_seq=since_seq, descending=descending)
    return {"events": [event.to_dict() for event in rows], "count": len(rows)}


@router.get("/stats")
async def stats(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return _log(request).stats()


@router.get("/verify")
async def verify(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    ok, detail = _log(request).verify_chain()
    return {"ok": ok, "detail": detail}


@router.get("/export")
async def export(request: Request, actor: str = Depends(require_owner)) -> PlainTextResponse:
    """Export the whole log as JSON lines - with the chain re-verified first."""
    log = _log(request)
    target = get_paths().ensure().reports / "events-export.jsonl"
    log.export_jsonl(target, verify=True)
    audit(get_runtime(request), "audit_exported", {"path": str(target)}, risk="MEDIUM")  # type: ignore[arg-type]
    return PlainTextResponse(target.read_text(encoding="utf-8"), media_type="application/x-ndjson",
                             headers={"Content-Disposition": "attachment; filename=natasha-events.jsonl"})


@router.get("/{event_id}")
async def event(event_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    found = _log(request).get(event_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no event {event_id!r}")
    return found.to_dict()
