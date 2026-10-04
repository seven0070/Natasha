"""System information, health and the runtime report."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ..deps import get_runtime, require_owner

router = APIRouter(tags=["system"])


@router.get("/info")
async def info(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    """Unauthenticated basics, for the UI to know what it is talking to."""
    from ... import __version__, CODENAME

    return {"name": "Natasha", "version": __version__, "codename": CODENAME,
            "auth_required": bool(getattr(runtime.settings, "auth_required", True)),
            "home": str(runtime.paths.home)}


@router.get("/health")
async def health(runtime: Any = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.health()


@router.get("/status")
async def status(runtime: Any = Depends(get_runtime), actor: str = Depends(require_owner)) -> dict[str, Any]:
    report = runtime.status()
    return report


@router.get("/doctor")
async def doctor(runtime: Any = Depends(get_runtime), actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Everything a person needs to know when something does not work."""
    from ...observability import get_health_monitor

    findings: list[dict[str, Any]] = []
    monitor = get_health_monitor(log=runtime.log)
    snapshot = monitor.snapshot()
    for name, check in snapshot["checks"].items():
        findings.append({"area": name, "status": check["status"], "detail": check["detail"]})

    def probe(component: Any, attribute: str) -> Any:
        """Read a component flag whether it is a property, a method or a dict entry."""
        if component is None:
            return False
        value = getattr(component, attribute, None)
        if isinstance(value, dict):
            return value
        if callable(value):
            try:
                value = value()
            except Exception as exc:
                return f"unavailable: {type(exc).__name__}: {exc}"
        return value

    def capabilities_dict(component: Any) -> dict[str, Any]:
        if component is None:
            return {"available": False}
        method = getattr(component, "capabilities", None)
        if callable(method):
            try:
                return method()
            except Exception as exc:
                return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"available": probe(component, "available")}

    capabilities = {
        "brain": {"attached": runtime.brain is not None,
                  "providers": len(getattr(runtime.brain.providers, "adapters", {}) or {})
                  if runtime.brain is not None else 0},
        "vision": capabilities_dict(runtime.vision),
        "hearing": capabilities_dict(runtime.hearing),
        "voice": capabilities_dict(runtime.voice),
        "computer": capabilities_dict(runtime.computer),
        "browser": capabilities_dict(runtime.browser),
        "creation": capabilities_dict(runtime.creation),
        "documents": capabilities_dict(runtime.vision),
    }
    degraded = runtime.state.degraded
    for item in degraded:
        findings.append({"area": "subsystem", "status": "degraded", "detail": item})
    return {"home": str(runtime.paths.home), "overall": snapshot["status"] if not degraded else "degraded",
            "findings": findings, "capabilities": capabilities}
