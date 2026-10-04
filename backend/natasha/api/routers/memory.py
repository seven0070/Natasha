"""Memory: the ten memory kinds, retrieval, working context and forgetting."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ...memory.models import MemoryKind
from ..deps import audit, get_runtime, handle, require_owner
from ..models import CorrectMemoryBody, MemoryBody

router = APIRouter(prefix="/memory", tags=["memory"])


def _store(request: Request) -> Any:
    store = get_runtime(request).memory
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "memory is unavailable")
    return store


@router.get("")
async def list_memories(request: Request, kind: str = "", limit: int = 100, include_superseded: bool = False,
                        actor: str = Depends(require_owner)) -> dict[str, Any]:
    store = _store(request)
    parsed = MemoryKind(kind) if kind else ""
    records = store.list(kind=parsed, limit=min(limit, 1000), include_superseded=include_superseded)
    return {"memories": [record.to_dict() for record in records]}


@router.get("/kinds")
async def kinds(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return {"kinds": [kind.value for kind in MemoryKind]}


@router.get("/stats")
async def stats(request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    return _store(request).stats()


@router.post("")
async def add_memory(body: MemoryBody, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    store = _store(request)
    from ...memory.models import Provenance

    try:
        record = store.add(body.kind, body.content, summary=body.summary, tags=body.tags,
                           importance=body.importance, confidence=body.confidence,
                           retention=body.retention,
                           provenance=Provenance(actor=actor, source=body.source,
                                                 trust="owner"),
                           actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "memory_written", {"memory_id": record.id, "kind": body.kind},
          risk="MEDIUM")  # type: ignore[arg-type]
    return record.to_dict()


@router.post("/correct")
async def correct_memory(body: CorrectMemoryBody, request: Request,
                         actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Owner correction: the old memory is superseded, never silently overwritten."""
    store = _store(request)
    try:
        record = store.correct(body.memory_id, content=body.content, reason=body.reason, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "memory_corrected",
          {"memory_id": body.memory_id, "replacement_id": record.id}, risk="MEDIUM")  # type: ignore[arg-type]
    return {"replacement": record.to_dict(), "superseded": body.memory_id}


@router.get("/recall")
async def recall(request: Request, query: str, limit: int = 10, kinds: str = "",
                 actor: str = Depends(require_owner)) -> dict[str, Any]:
    store = _store(request)
    parsed = [MemoryKind(item) for item in kinds.split(",") if item] if kinds else None
    scored = store.recall(query, kinds=parsed, limit=min(limit, 100), actor=actor)
    return {"query": query, "hits": [item.to_dict() for item in scored]}


@router.post("/forget")
async def forget(request: Request, memory_id: str = "", query: str = "", hard: bool = False,
                 actor: str = Depends(require_owner)) -> dict[str, Any]:
    store = _store(request)
    try:
        result = store.forget(memory_id or None, query=query, hard=hard, actor=actor)
    except Exception as exc:
        raise handle(exc) from exc
    audit(get_runtime(request), "memory_forgotten", {"memory_id": memory_id, "query": query[:120], "hard": hard},
          risk="MEDIUM")  # type: ignore[arg-type]
    return result


@router.get("/working")
async def working(request: Request, mission_id: str = "", actor: str = Depends(require_owner)) -> dict[str, Any]:
    runtime = get_runtime(request)
    if runtime.working is None:
        return {"items": [], "rendered": ""}
    return {"items": [item.to_dict() for item in runtime.working.items(mission_id=mission_id)],
            "rendered": runtime.working.render(mission_id=mission_id)}


@router.get("/world")
async def world(request: Request, query: str = "", limit: int = 20,
                actor: str = Depends(require_owner)) -> dict[str, Any]:
    model = get_runtime(request).world
    if model is None:
        return {"entities": [], "facts": []}
    if query:
        entities = model.search_entities(query, limit=limit)
    else:
        entities = model.entities(limit=limit) if hasattr(model, "entities") else []
    facts: list[dict[str, Any]] = []
    for entity in entities[:5]:
        for edge in model.relations_of(entity.name)[:5]:
            facts.append({"subject": edge["subject"], "predicate": edge["predicate"],
                          "object": edge["object"], "confidence": edge["confidence"]})
        for belief in model.beliefs_of(entity.name)[:5]:
            facts.append({"subject": entity.name, "predicate": belief["attribute"],
                          "object": belief["value"], "confidence": belief["confidence"]})
    return {"entities": [entity.to_dict() for entity in entities], "facts": facts}
