"""Artifacts: what Natasha has actually produced, with path, size, type and hash.

Every generated artifact carries an identity - the UI can preview it, the owner can download it, and
nothing outside the artifacts directory is readable through this route (path traversal is refused).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from ..deps import get_runtime, require_owner

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

#: Preview text is bounded so a huge file cannot be used to exhaust the browser or the API.
MAX_PREVIEW_BYTES = 256 * 1024


def _artifacts_root(request: Request) -> Path:
    runtime = get_runtime(request)
    root = Path(runtime.paths.artifacts)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _describe(path: Path, root: Path, *, mission_id: str = "") -> dict[str, Any]:
    from ...core.hashing import sha256_file

    stat = path.stat()
    mime, _ = mimetypes.guess_type(path.name)
    entry = {
        "name": path.name,
        "path": str(path),
        "relative": str(path.relative_to(root)),
        "mime": mime or "application/octet-stream",
        "bytes": stat.st_size,
        "modified_at": __import__("datetime").datetime.fromtimestamp(
            stat.st_mtime, tz=__import__("datetime").timezone.utc).isoformat(),
        "kind": "text" if (mime or "").startswith("text/") or path.suffix.lower() in {
            ".md", ".txt", ".json", ".csv", ".py", ".html", ".css", ".js", ".yaml", ".yml"} else "binary",
        "mission_id": mission_id,
    }
    try:
        entry["sha256"] = sha256_file(path)
    except Exception:
        entry["sha256"] = ""
    return entry


@router.get("")
async def list_artifacts(request: Request, limit: int = 200, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Artifacts on disk, with the missions that produced them where we can tell."""
    runtime = get_runtime(request)
    root = _artifacts_root(request)
    mission_of: dict[str, str] = {}
    if runtime.missions is not None:
        try:
            for row in runtime.missions.list(limit=500):
                mission = runtime.missions.get(row["id"])
                for artifact in mission.artifacts:
                    mission_of[str(Path(artifact).resolve())] = mission.id
        except Exception:
            mission_of = {}
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or len(entries) >= max(1, min(limit, 1000)):
            continue
        try:
            entries.append(_describe(path, root, mission_id=mission_of.get(str(path.resolve()), "")))
        except OSError:
            continue
    entries.sort(key=lambda item: item["modified_at"], reverse=True)
    return {"artifacts": entries, "count": len(entries), "root": str(root)}


@router.get("/content")
async def preview(path: str, request: Request, actor: str = Depends(require_owner)) -> dict[str, Any]:
    """Text preview of one artifact. Only files inside the artifacts directory are reachable."""
    root = _artifacts_root(request)
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that path is outside the artifacts directory")
    if not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    raw = candidate.read_bytes()[:MAX_PREVIEW_BYTES]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {**_describe(candidate, root), "text": "", "previewable": False}
    return {**_describe(candidate, root), "text": text, "previewable": True,
            "truncated": candidate.stat().st_size > MAX_PREVIEW_BYTES}


@router.get("/download")
async def download(path: str, request: Request, actor: str = Depends(require_owner)) -> FileResponse:
    root = _artifacts_root(request)
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "that path is outside the artifacts directory")
    if not candidate.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
    return FileResponse(candidate, filename=candidate.name)
