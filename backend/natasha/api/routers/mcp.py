"""MCP servers: configure, install, enable, call tools, disable, uninstall."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..deps import audit, get_runtime, handle, require_owner
from ..models import MCPCallBody, MCPInstallBody

router = APIRouter(prefix="/mcp", tags=["mcp"])


def _registry(request: Request) -> Any:
    registry = get_runtime(request).mcp
    if registry is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the MCP registry is unavailable")
    return registry


@router.get("")
async def servers(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    registry = _registry(request)
    return {"servers": [server.to_dict() for server in registry.all()]}


@router.post("")
async def configure(body: MCPInstallBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Register (or update) an MCP server definition. Installing is a separate step."""
    from ...mcp.registry import MCPServer

    registry = _registry(request)
    server = MCPServer(name=body.name, transport=body.transport, command=body.command[0] if body.command else "",
                       args=list(body.command[1:]) + list(body.args), url=body.url, env=body.env)
    try:
        configured = registry.configure(server, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "mcp_configured", {"name": body.name, "transport": body.transport},
          risk="MEDIUM")  # type: ignore[arg-type]
    return configured.to_dict()


@router.get("/{name}")
async def get_server(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _registry(request).get(name).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{name}/install")
async def install_server(name: str, request: Request, approval_id: str = "",
                         actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        server = await _registry(request).install(name, actor=actor, approval_id=approval_id)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "mcp_installed", {"name": name}, risk="HIGH")  # type: ignore[arg-type]
    return server.to_dict()


@router.post("/{name}/enable")
async def enable_server(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return (await _registry(request).enable(name, actor=actor)).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{name}/disable")
async def disable_server(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return (await _registry(request).disable(name, actor=actor)).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.delete("/{name}")
async def uninstall_server(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        await _registry(request).uninstall(name, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "mcp_uninstalled", {"name": name}, risk="HIGH")  # type: ignore[arg-type]
    return {"ok": True, "uninstalled": name}


@router.get("/{name}/health")
async def server_health(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return await _registry(request).health(name)
    except Exception as exc:
        raise handle(exc) from exc


@router.get("/{name}/tools")
async def server_tools(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        server = _registry(request).get(name)
    except Exception as exc:
        raise handle(exc) from exc
    return {"tools": server.tools}


@router.post("/{name}/call/{tool_name}")
async def call_tool(name: str, tool_name: str, body: MCPCallBody, request: Request,
                    actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        result = await _registry(request).call(name, tool_name, body.arguments)
    except Exception as exc:
        raise handle(exc) from exc
    return result
