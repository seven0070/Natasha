"""Providers and models: what is configured, what is healthy, what can do what.

The UI needs this to be honest: a provider that is configured but unreachable shows as unhealthy, a
local model shows as local, and discovery is an explicit action rather than a hidden side effect.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..deps import audit, get_runtime, handle, require_owner

router = APIRouter(prefix="/providers", tags=["providers"])


class RoutingBody(BaseModel):
    prefer_local: bool | None = None
    default_provider: str = ""
    routing_weights: dict[str, float] = Field(default_factory=dict)
    stream: bool | None = None
    fallback_depth: int | None = None


def _brain(request: Request) -> Any:
    brain = get_runtime(request).brain
    if brain is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the brain is unavailable")
    return brain


@router.get("")
async def list_providers(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Every configured provider with its health, capabilities and discovered models."""
    runtime = get_runtime(request)
    brain = runtime.brain
    if brain is None:
        return {"providers": [], "models": [], "brain_available": False}
    registry = brain.providers
    health = registry.health_snapshot()
    models_by_provider: dict[str, list[dict[str, Any]]] = {}
    for model in brain.router.models.all():
        models_by_provider.setdefault(model.provider, []).append(model.to_dict())
    providers = []
    for adapter in registry.all():
        name = adapter.name
        providers.append({
            "name": name,
            "enabled": bool(getattr(getattr(adapter, "settings", None), "enabled", True)),
            "local": bool(getattr(adapter, "local", False)),
            "privacy_tier": str(getattr(adapter, "privacy_tier", "")),
            "supports_tools": bool(getattr(adapter, "supports_tools", False)),
            "supports_streaming": bool(getattr(adapter, "supports_streaming", False)),
            "supports_vision": bool(getattr(adapter, "supports_vision", False)),
            "supports_embeddings": bool(getattr(adapter, "supports_embeddings", False)),
            "base_url": str(getattr(getattr(adapter, "settings", None), "base_url", "") or ""),
            "has_credential": bool(str(getattr(getattr(adapter, "settings", None), "credential_ref", "") or "")),
            "health": health.get(name, {}),
            "models": models_by_provider.get(name, []),
        })
    settings = runtime.settings
    brain_settings = getattr(settings, "brain", None)
    return {
        "providers": providers,
        "models": [model.to_dict() for model in brain.router.models.all()],
        "brain_available": True,
        "routing": {
            "prefer_local": bool(getattr(brain_settings, "prefer_local", True)),
            "default_provider": str(getattr(brain_settings, "default_provider", "") or ""),
            "routing_weights": dict(getattr(brain_settings, "routing_weights", {}) or {}),
            "stream": bool(getattr(brain_settings, "stream", True)),
            "fallback_depth": int(getattr(brain_settings, "fallback_depth", 3)),
            "health_ttl_seconds": float(getattr(brain_settings, "health_ttl_seconds", 90.0)),
        },
        "recent_usage": runtime.brain.usage.totals() if getattr(runtime.brain, "usage", None) else {},
    }


@router.post("/{name}/check")
async def check_provider(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    registry = _brain(request).providers
    try:
        result = await registry.check_health(name, force=True)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "provider_checked", {"provider": name, "healthy": result.healthy})
    payload = result.to_dict()
    payload.update({"provider": name, "ok": bool(result.healthy)})
    return payload


@router.post("/{name}/discover")
async def discover_models(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    brain = _brain(request)
    try:
        found = await brain.providers.discover_models(names=[name])
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "models_discovered", {"provider": name, "count": found})
    models = [model.to_dict() for model in brain.router.models.for_provider(name)]
    return {"provider": name, "ok": bool(models), "discovered": found, "models": models,
            "error": "" if models else f"{name} reported no models; is the server running?"}


@router.post("/{name}/enable")
async def enable_provider(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return _set_enabled(request, name, True)


@router.post("/{name}/disable")
async def disable_provider(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return _set_enabled(request, name, False)


@router.post("/routing")
async def set_routing(body: RoutingBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Owner routing preferences: local-first, weights, default provider and stream."""
    runtime = get_runtime(request)
    brain_settings = getattr(runtime.settings, "brain", None)
    if brain_settings is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "brain settings are unavailable")
    if body.prefer_local is not None:
        brain_settings.prefer_local = bool(body.prefer_local)
    if body.default_provider:
        brain_settings.default_provider = body.default_provider
    if body.routing_weights:
        merged = dict(getattr(brain_settings, "routing_weights", {}) or {})
        merged.update({str(key): float(value) for key, value in body.routing_weights.items()})
        brain_settings.routing_weights = merged
    if body.stream is not None:
        brain_settings.stream = bool(body.stream)
    if body.fallback_depth is not None:
        brain_settings.fallback_depth = max(0, min(int(body.fallback_depth), 10))
    try:
        runtime.settings.save()
    except Exception as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"could not save settings: {exc}") from exc
    audit(runtime, "routing_updated", {"prefer_local": brain_settings.prefer_local,
                                       "default_provider": brain_settings.default_provider},
          risk="MEDIUM")  # type: ignore[arg-type]
    return {"ok": True, "routing": {
        "prefer_local": brain_settings.prefer_local,
        "default_provider": brain_settings.default_provider,
        "routing_weights": dict(brain_settings.routing_weights),
        "stream": brain_settings.stream,
        "fallback_depth": brain_settings.fallback_depth,
    }}


def _set_enabled(request: Request, name: str, enabled: bool) -> dict[str, Any]:
    registry = _brain(request).providers
    try:
        result = registry.set_enabled(name, enabled)
    except Exception as exc:
        raise handle(exc) from exc
    runtime = get_runtime(request)
    try:
        runtime.settings.save()
    except Exception:
        pass
    audit(runtime, "provider_toggled", {"provider": name, "enabled": enabled}, risk="MEDIUM")  # type: ignore[arg-type]
    return {**result, "ok": True}
