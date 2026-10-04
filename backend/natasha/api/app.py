"""The FastAPI application: local API + the UI it serves."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core import get_paths, load_settings
from ..events import EventKind
from .auth import get_auth_manager

#: Everything the UI is allowed to call from another origin. Empty by default: the UI is served by
#: this same server, so cross-origin access is a deliberate act, not an accident.
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = ()


def _frontend_dir() -> Path | None:
    """Locate the bundled UI (repo checkout, installed package, or packaged desktop app)."""
    candidates = [
        Path(__file__).resolve().parents[3] / "frontend",
        Path(__file__).resolve().parents[2] / "frontend",
        get_paths().home / "frontend",
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(runtime: Any = None, *, auth: Any = None, serve_ui: bool = True,
               cors_origins: list[str] | None = None) -> FastAPI:
    """Build the application. The runtime is created lazily on first use."""
    settings = getattr(runtime, "settings", None) or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        if app.state.runtime is None:
            from ..runtime import get_runtime

            app.state.runtime = get_runtime()
        if app.state.auth is None:
            app.state.auth = get_auth_manager(session_ttl_minutes=int(getattr(settings, "session_ttl_minutes", 720)),
                                              owner_id=getattr(settings, "owner_id", "") or "owner")
        yield
        # Shutdown: close what holds resources, but never delete state.
        from ..core import run_coroutine_sync

        for candidate in ("mcp", "skill_runtime", "credentials", "brain"):
            component = getattr(app.state.runtime, candidate, None)
            closer = getattr(component, "close", None)
            if component is None or not callable(closer):
                continue
            try:
                result = closer()
                if hasattr(result, "__await__"):
                    run_coroutine_sync(result)
            except Exception:
                pass

    app = FastAPI(title="Natasha", version="0.9.0", lifespan=lifespan,
                  description="Local-first personal agent: chat, missions, memory, tools, governance.")
    app.state.runtime = runtime
    app.state.auth = auth

    origins = list(DEFAULT_ALLOWED_ORIGINS if cors_origins is None else cors_origins)
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Natasha-Duration-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        if request.url.path.startswith("/api") and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            runtime_object = getattr(request.app.state, "runtime", None)
            if runtime_object is not None and getattr(runtime_object, "log", None) is not None:
                try:
                    runtime_object.log.append(
                        EventKind.SYSTEM,
                        {"action": "api_request", "method": request.method, "path": request.url.path,
                         "status": response.status_code},
                        actor="owner", source="api",
                    )
                except Exception:
                    pass
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    from .routers import ROUTERS

    for router in ROUTERS:
        app.include_router(router, prefix="/api")

    if serve_ui:
        static_dir = _frontend_dir()
        if static_dir is not None:
            app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

            @app.get("/", include_in_schema=False)
            async def index() -> FileResponse:
                return FileResponse(static_dir / "index.html")

            @app.get("/{path:path}", include_in_schema=False)
            async def spa(path: str) -> Any:
                """Serve the single-page UI; unknown /api paths still 404 honestly."""
                if path.startswith("api/"):
                    return JSONResponse(status_code=404, content={"detail": "not found"})
                candidate = static_dir / path
                if candidate.is_file():
                    return FileResponse(candidate)
                return FileResponse(static_dir / "index.html")

    return app


app = create_app()


def get_app() -> FastAPI:
    """Factory hook for ``uvicorn natasha.api.app:get_app``."""
    return create_app()
