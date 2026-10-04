"""Observability: health, metrics and traces."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..deps import get_runtime, require_owner

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/health")
async def health(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    monitor = runtime.observability
    if monitor is None:
        return {"status": "degraded", "detail": "no health monitor attached"}
    return monitor.snapshot()


@router.get("/metrics")
async def metrics(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    from ...observability import get_metrics

    return get_metrics().snapshot()


@router.get("/traces")
async def traces(request: Request, limit: int = 100, actor: str = Depends(require_owner)) -> dict[str, Any]:
    from ...observability import get_tracer

    return {"spans": get_tracer(log=get_runtime(request).log).recent(limit=limit)}


@router.get("/usage")
async def usage(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.brain is None:
        return {"usage": {}, "note": "no brain attached"}
    from ...brain import get_usage_tracker

    tracker = get_usage_tracker()
    return {"usage": tracker.snapshot() if hasattr(tracker, "snapshot") else tracker.summary()
            if hasattr(tracker, "summary") else {},
            "providers": runtime.brain.providers.health_snapshot()}
