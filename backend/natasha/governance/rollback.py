"""Snapshot and rollback.

Every state-changing governed operation takes a snapshot first, so "undo" is always available
even when the change was wrong in a way nobody predicted. Snapshots are tar archives plus a
signed manifest of file hashes; restore verifies the manifest before overwriting anything.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import NotFoundError, hmac_sign, hmac_verify, new_id, sha256_file
from ..core.clock import iso
from ..core.paths import get_paths
from ..events import EventKind, get_event_log

EXCLUDED = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


@dataclass
class Snapshot:
    id: str
    label: str
    path: str
    size_bytes: int
    files: int
    created_at: str
    root: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "path": self.path, "size_bytes": self.size_bytes,
            "files": self.files, "created_at": self.created_at, "root": self.root, "metadata": self.metadata,
        }


class SnapshotManager:
    """Creates and restores point-in-time snapshots of the source tree or any directory."""

    def __init__(self, root: Path | None = None, *, key: bytes | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[3]
        self._key = key
        self._lock = threading.RLock()
        self.log = get_event_log()

    def _signing_key(self) -> bytes:
        if self._key is None:
            from ..credentials.crypto import get_master_key

            self._key = get_master_key()
        return self._key

    def _dir(self) -> Path:
        return get_paths().ensure().snapshots

    def create(self, label: str = "", *, paths: list[str] | None = None, metadata: dict[str, Any] | None = None) -> Snapshot:
        """Archive the tree (or a subset of it) with a signed manifest."""
        snapshot_id = new_id("snp")
        target = self._dir() / f"{snapshot_id}.tar.gz"
        manifest: dict[str, str] = {}
        selected = [self.root / p for p in paths] if paths else [self.root]

        def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            parts = Path(info.name).parts
            if any(part in EXCLUDED for part in parts):
                return None
            return info

        with self._lock, tarfile.open(target, "w:gz") as archive:
            for item in selected:
                if item.is_file():
                    manifest[str(item.relative_to(self.root))] = sha256_file(item)
                    archive.add(item, arcname=str(item.relative_to(self.root)), filter=_filter)
                elif item.is_dir():
                    for file in sorted(item.rglob("*")):
                        if not file.is_file() or any(part in EXCLUDED for part in file.relative_to(self.root).parts):
                            continue
                        manifest[str(file.relative_to(self.root))] = sha256_file(file)
                    archive.add(item, arcname=str(item.relative_to(self.root)), filter=_filter, recursive=True)

        manifest_path = self._dir() / f"{snapshot_id}.manifest.json"
        manifest_path.write_text(
            json.dumps(
                {"id": snapshot_id, "root": str(self.root), "files": manifest,
                 "signature": hmac_sign(self._signing_key(), manifest), "created_at": iso()},
                indent=2,
            )
        )
        snapshot = Snapshot(
            id=snapshot_id, label=label or snapshot_id, path=str(target), size_bytes=target.stat().st_size,
            files=len(manifest), created_at=iso(), root=str(self.root), metadata=metadata or {},
        )
        self.log.append(
            EventKind.SYSTEM,
            {"action": "snapshot.created", "snapshot_id": snapshot_id, "files": snapshot.files,
             "size_bytes": snapshot.size_bytes, "label": snapshot.label},
            actor="system", source="governance.rollback",
        )
        return snapshot

    def list(self) -> list[Snapshot]:
        found: list[Snapshot] = []
        for archive in sorted(self._dir().glob("*.tar.gz")):
            stats = archive.stat()
            manifest_file = archive.with_name(archive.name.replace(".tar.gz", ".manifest.json"))
            files = 0
            created = iso()
            if manifest_file.exists():
                try:
                    data = json.loads(manifest_file.read_text())
                    files = len(data.get("files", {}))
                    created = data.get("created_at", created)
                except json.JSONDecodeError:
                    pass
            found.append(
                Snapshot(id=archive.name.replace(".tar.gz", ""), label=archive.stem, path=str(archive),
                         size_bytes=stats.st_size, files=files, created_at=created, root=str(self.root))
            )
        return found

    def verify(self, snapshot_id: str) -> tuple[bool, list[str]]:
        manifest_file = self._dir() / f"{snapshot_id}.manifest.json"
        if not manifest_file.exists():
            return False, ["manifest missing"]
        data = json.loads(manifest_file.read_text())
        if not hmac_verify(self._signing_key(), data.get("files", {}), data.get("signature", "")):
            return False, ["manifest signature invalid"]
        problems: list[str] = []
        for name, digest in data.get("files", {}).items():
            current = self.root / name
            if not current.exists():
                continue
            if sha256_file(current) != digest:
                problems.append(f"modified:{name}")
        return (not problems), problems

    def restore(
        self,
        snapshot_id: str,
        *,
        target: Path | None = None,
        only: list[str] | None = None,
        verify_manifest: bool = True,
    ) -> dict[str, Any]:
        """Restore a snapshot into *target* (defaults to the snapshot root)."""
        archive_path = self._dir() / f"{snapshot_id}.tar.gz"
        if not archive_path.exists():
            raise NotFoundError(f"snapshot {snapshot_id} not found")
        manifest_file = self._dir() / f"{snapshot_id}.manifest.json"
        if manifest_file.exists() and verify_manifest:
            data = json.loads(manifest_file.read_text())
            if not hmac_verify(self._signing_key(), data.get("files", {}), data.get("signature", "")):
                raise NotFoundError(f"snapshot {snapshot_id} manifest signature invalid")
        dest = Path(target) if target else self.root
        dest.mkdir(parents=True, exist_ok=True)
        with self._lock, tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if only:
                allowed = tuple(only)
                members = [m for m in members if any(m.name == a or m.name.startswith(a.rstrip("/") + "/") for a in allowed)]
            for member in members:
                member_path = (dest / member.name).resolve()
                if not str(member_path).startswith(str(dest.resolve())):
                    raise NotFoundError(f"refusing to extract {member.name} outside {dest} (tar traversal)")
            # ``filter="data"`` (the hardened extraction mode) only exists on newer Pythons; the
            # members were validated against ``dest`` above, so dropping it on older runtimes keeps
            # behaviour identical to pre-filter tarfile without losing the traversal check.
            extract_kwargs: dict[str, Any] = {}
            if hasattr(tarfile, "data_filter"):
                extract_kwargs["filter"] = "data"
            archive.extractall(dest, members=members, **extract_kwargs)  # noqa: S202 - paths validated above
        self.log.append(
            EventKind.SYSTEM,
            {"action": "snapshot.restored", "snapshot_id": snapshot_id, "target": str(dest),
             "files": len(members)},
            actor="owner", source="governance.rollback", risk="MEDIUM",
        )
        return {"snapshot_id": snapshot_id, "target": str(dest), "files": len(members)}

    def delete(self, snapshot_id: str) -> None:
        archive = self._dir() / f"{snapshot_id}.tar.gz"
        manifest = self._dir() / f"{snapshot_id}.manifest.json"
        if not archive.exists():
            raise NotFoundError(f"snapshot {snapshot_id} not found")
        archive.unlink()
        manifest.exists() and manifest.unlink()
        self.log.append(
            EventKind.SYSTEM, {"action": "snapshot.deleted", "snapshot_id": snapshot_id},
            actor="owner", source="governance.rollback",
        )

    def prune(self, keep: int = 20) -> int:
        snapshots = self.list()
        removed = 0
        for snapshot in sorted(snapshots, key=lambda s: s.created_at)[:-keep] if len(snapshots) > keep else []:
            self.delete(snapshot.id)
            removed += 1
        return removed

    def stats(self) -> dict[str, Any]:
        snapshots = self.list()
        return {
            "count": len(snapshots),
            "total_bytes": sum(s.size_bytes for s in snapshots),
            "directory": str(self._dir()),
            "root": str(self.root),
        }


_MANAGERS: dict[str, SnapshotManager] = {}
_LOCK = threading.Lock()


def get_snapshot_manager() -> SnapshotManager:
    with _LOCK:
        if "default" not in _MANAGERS:
            _MANAGERS["default"] = SnapshotManager()
        return _MANAGERS["default"]


def reset_snapshot_managers() -> None:
    with _LOCK:
        _MANAGERS.clear()
