"""Chat: one-shot turns, server-sent event streaming, and the websocket used by the UI."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import StreamingResponse

from ...core import NatashaError
from ...security.injection import ContentTrust, ExternalContent
from ..deps import audit, get_runtime, handle, require_owner
from ..models import ChatBody

router = APIRouter(tags=["chat"])


def _attachments(runtime: Any, body: ChatBody) -> list[ExternalContent]:
    """Turn base64 images and documents into untrusted content blocks."""
    blocks: list[ExternalContent] = []
    for data_url in body.images[:8]:
        blocks.append(ExternalContent(text="[image attached]", source="owner:image",
                                      trust=ContentTrust.OWNER, metadata={"data_url": data_url}))
    for document in body.documents[:8]:
        blocks.append(ExternalContent(text=document.get("text", "")[:200_000],
                                      source=document.get("source", "document"),
                                      trust=ContentTrust.EXTERNAL,
                                      metadata={"kind": document.get("kind", "document")}))
    return blocks


def _turn(runtime: Any, body: ChatBody, principal: str) -> Any:
    """Build the turn for an authenticated owner request.

    The *principal* is the authenticated owner, but the actor driving the turn is the agent: an
    authenticated chat request must not launder the model's tool calls into "owner" authority, or a
    page the model happens to read could get a dangerous command executed without the owner ever
    approving that command. Actions that need approval still ask - see the approvals router.
    """
    from ...executive import Turn

    return Turn(message=body.message, conversation_id=body.conversation_id or "",
                actor="model:main", mission_id=body.mission_id,
                attachments=_attachments(runtime, body),
                metadata={"extra_instructions": body.extra_instructions, "principal": principal})


@router.post("/chat")
async def chat(body: ChatBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    executive = runtime.executive
    if executive is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the executive is not available")
    try:
        result = await executive.run_turn(_turn(runtime, body, actor))
    except Exception as exc:
        raise handle(exc) from exc
    return result.to_dict()


@router.post("/chat/stream")
async def chat_stream(body: ChatBody, request: Request, actor: str = Depends(require_owner)) -> StreamingResponse:
    """Server-sent events: nicer for browsers that cannot hold a websocket open."""
    runtime = get_runtime(request)
    executive = runtime.executive
    if executive is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the executive is not available")
    turn = _turn(runtime, body, actor)

    async def events() -> Any:
        async for chunk in executive.stream_turn(turn):
            yield f"data: {json.dumps(chunk, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.websocket("/ws/chat")
async def chat_socket(websocket: WebSocket) -> None:
    """Websocket chat. The first frame must carry the owner token if auth is required."""
    await websocket.accept()
    runtime = getattr(websocket.app.state, "runtime", None)
    auth = getattr(websocket.app.state, "auth", None)
    if runtime is None:
        await websocket.send_json({"type": "error", "error": "runtime not started"})
        await websocket.close()
        return
    try:
        first = await websocket.receive_json()
    except Exception:
        await websocket.close()
        return
    token = str(first.get("token", ""))
    authorised = (auth is not None and auth.session(token) is not None) or \
        (not getattr(runtime.settings, "auth_required", True))
    if not authorised:
        await websocket.send_json({"type": "error", "error": "owner authentication required"})
        await websocket.close(code=4401)
        return

    async def handle_message(payload: dict[str, Any]) -> None:
        message = str(payload.get("message", "")).strip()
        if not message:
            await websocket.send_json({"type": "error", "error": "empty message"})
            return
        body = ChatBody(message=message, conversation_id=str(payload.get("conversation_id", "")),
                        mission_id=str(payload.get("mission_id", "")),
                        images=list(payload.get("images") or []),
                        documents=list(payload.get("documents") or []),
                        extra_instructions=str(payload.get("extra_instructions", "")))
        turn = _turn(runtime, body, "owner")
        async for chunk in runtime.executive.stream_turn(turn):
            await websocket.send_json(chunk)

    try:
        if first.get("message"):
            await handle_message(first)
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            await handle_message(payload)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        await websocket.close()


@router.get("/conversations")
async def conversations(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    executive = runtime.executive
    if executive is None:
        return {"conversations": []}
    with executive._lock:
        return {"conversations": [{"id": key, "messages": len(value),
                                   "updated_at": value[-1].get("at", "") if value else ""}
                                  for key, value in executive.conversations.items()]}


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    executive = runtime.executive
    if executive is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no executive")
    with executive._lock:
        messages = list(executive.conversations.get(conversation_id, []))
    if not messages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no conversation {conversation_id!r}")
    return {"id": conversation_id, "messages": messages}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    executive = runtime.executive
    if executive is None:
        return {"ok": True, "deleted": False}
    with executive._lock:
        existed = executive.conversations.pop(conversation_id, None) is not None
    audit(runtime, "conversation_deleted", {"conversation_id": conversation_id})
    return {"ok": True, "deleted": existed}
