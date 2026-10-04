"""Tools: what exists, what it can do, and running one through the choke point."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...tools.base import ToolContext
from ..deps import audit, get_runtime, handle, require_owner
from ..models import ToolCallBody

router = APIRouter(prefix="/tools", tags=["tools"])


def _registry(request: Request) -> Any:
    registry = get_runtime(request).tools
    if registry is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the tool registry is unavailable")
    return registry


@router.get("")
async def list_tools(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    registry = _registry(request)
    return {"tools": registry.describe(), "count": len(registry.names())}


@router.get("/{name}")
async def describe_tool(name: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    try:
        return _registry(request).spec(name).to_dict()
    except Exception as exc:
        raise handle(exc) from exc


@router.post("/{name}")
async def run_tool(name: str, body: ToolCallBody, request: Request,
                   actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Run a tool as the owner. The policy engine, not this route, decides whether it may run."""
    runtime = get_runtime(request)
    registry = _registry(request)
    context = ToolContext(actor=actor, policy=runtime.policy, memory=runtime.memory, brain=runtime.brain,
                          world=runtime.world, broker=runtime.broker, approvals=runtime.approvals,
                          workspace=runtime.paths.workspace, extra={"approval_id": body.approval_id})
    try:
        result = await registry.execute(name, body.arguments, context=context,
                                        approval_id=body.approval_id)
    except Exception as exc:
        raise handle(exc) from exc
    audit(runtime, "tool_run", {"tool": name, "ok": getattr(result, "ok", False)},
          risk="MEDIUM")  # type: ignore[arg-type]
    return result.to_dict()
