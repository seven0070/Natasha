"""Shared FastAPI dependencies: the runtime, the owner session, and error shaping."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, WebSocket, status

from ..core import NatashaError, PolicyDenied, ValidationError
from ..events import EventKind
from ..core.risk import RiskLevel

#: Routes that must stay reachable before the owner has a session.
PUBLIC_PATHS = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/info"}


def get_runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the runtime is not started")
    return runtime


def get_auth(request: Request) -> Any:
    return getattr(request.app.state, "auth", None)


def _token_from(headers: Any, query_token: str = "") -> str:
    authorization = headers.get("authorization", "") if headers else ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header_token = headers.get("x-natasha-token", "") if headers else ""
    return header_token or query_token


def current_owner(request: Request, token: str = "") -> str:
    """Resolve the caller to an actor string, or raise 401."""
    runtime = getattr(request.app.state, "runtime", None)
    auth = getattr(request.app.state, "auth", None)
    auth_required = True
    if runtime is not None:
        auth_required = bool(getattr(runtime.settings, "auth_required", True))
    supplied = token or _token_from(request.headers)
    if auth is not None:
        session = auth.session(supplied)
        if session is not None:
            request.state.owner = session.owner_id
            return "owner"
    if not auth_required:
        return "local-owner"
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "owner authentication required",
                        headers={"WWW-Authenticate": "Bearer"})


def require_owner(request: Request) -> str:
    """Dependency: an authenticated owner (or an explicit auth-disabled local install)."""
    if request.url.path in PUBLIC_PATHS:
        return "owner"
    return current_owner(request)


def handle(exc: Exception) -> HTTPException:
    """Map internal errors onto honest HTTP responses."""
    from .ratelimit import RateLimited

    if isinstance(exc, RateLimited):
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc),
                             headers={"Retry-After": f"{exc.retry_after:.1f}"})
    if isinstance(exc, PolicyDenied):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    from ..core import ApprovalRequired, NotFoundError, ConflictError

    if isinstance(exc, ApprovalRequired):
        return HTTPException(status.HTTP_409_CONFLICT,
                             {"message": str(exc), "approval_request_id": getattr(exc, "request_id", "")})
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ValidationError):
        # 422 (Starlette renamed the constant UNPROCESSABLE_ENTITY -> UNPROCESSABLE_CONTENT; the
        # number is stable and reading the old attribute now emits a deprecation warning).
        return HTTPException(422, str(exc))
    if isinstance(exc, NatashaError):
        return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")


def audit(runtime: Any, action: str, payload: dict[str, Any], *, actor: str = "owner",
          risk: RiskLevel = RiskLevel.LOW, kind: EventKind = EventKind.SYSTEM) -> None:
    if runtime is None or getattr(runtime, "log", None) is None:
        return
    try:
        runtime.log.append(kind, {"action": action, **payload}, actor=actor, source="api", risk=risk)
    except Exception:
        pass


def owner_actor(actor: str) -> str:
    """Requests authenticated as the owner act as the owner; anything else keeps its identity."""
    return actor or "owner"


def _client_key(request: Request) -> str:
    """Identify the caller for rate limiting: address first, session second."""
    address = ""
    if request.client is not None:
        address = request.client.host or ""
    for header in ("X-Forwarded-For", "X-Real-IP"):
        forwarded = request.headers.get(header)
        if forwarded:
            address = forwarded.split(",")[0].strip()
            break
    token = request.headers.get("X-Natasha-Token", "")[:16]
    return f"{address or 'local'}|{token}"


def limiter_for(request: Any) -> Any:
    """The limiter for this application instance (one budget per server, not per process).

    Scoping it to the app also keeps two runtimes in one process (tests, embedded use) from spending
    each other's budget.
    """
    from .ratelimit import RateLimiter

    state = getattr(request.app, "state", None)
    existing = getattr(state, "limiter", None)
    if existing is not None:
        return existing
    runtime = getattr(state, "runtime", None)
    settings = getattr(runtime, "settings", None)
    limits = getattr(settings, "limits", None)
    limiter = RateLimiter(enabled=bool(getattr(limits, "enabled", True)),
                          multiplier=float(getattr(limits, "multiplier", 1.0) or 1.0),
                          overrides=dict(getattr(limits, "overrides", {}) or {}))
    if state is not None:
        state.limiter = limiter
    return limiter


def rate_limit(bucket: str, *, rate: float | None = None, burst: int | None = None) -> Any:
    """Dependency factory: guard an endpoint with a named bucket.

    The default table lives in ``api.ratelimit``; passing ``rate``/``burst`` overrides it for one
    route (used by the auth endpoints, which are the ones worth brute-forcing).
    """

    def dependency(request: Request) -> None:
        limiter = limiter_for(request)
        if rate is not None:
            limiter.overrides[bucket] = (float(rate), int(burst or max(1, rate / 6)))
        from .ratelimit import RateLimited

        try:
            limiter.check(bucket, _client_key(request))
        except RateLimited as exc:
            # Translate here as well as in the app handler: a dependency failure must be an honest
            # 429 with Retry-After, not a 500.
            raise handle(exc) from exc

    return dependency
