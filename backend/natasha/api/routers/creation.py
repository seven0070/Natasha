"""The creation engine: documents, images, audio, video and slide decks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import CreationBody

router = APIRouter(prefix="/creation", tags=["creation"])


def _engine(request: Request) -> Any:
    engine = get_runtime(request).creation
    if engine is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the creation engine is unavailable")
    return engine


@router.get("/capabilities")
async def capabilities(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return _engine(request).capabilities()


@router.get("/jobs")
async def jobs(request: Request, kind: str = "", limit: int = 50, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"jobs": _engine(request).history(limit=limit, kind=kind)}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _engine(request).get(job_id).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/create")
async def create(body: CreationBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        job = await _engine(request).create(body.kind, body.brief, **body.options)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "creation_job", {"kind": body.kind, "status": job.status,
                                                 "artifacts": [item["path"] for item in job.artifacts]},
          risk="MEDIUM")  # type: ignore[arg-type]
    return job.to_dict()
