"""Marketplace catalogue: installed packages, versions and pins."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core import NotFoundError
from ..core.clock import iso
from ..core.paths import get_paths


@dataclass
class InstalledItem:
    kind: str
    name: str
    version: str
    checksum: str
    publisher: str = ""
    permissions: list[str] = field(default_factory=list)
    source: str = ""
    path: str = ""
    pinned: bool = False
    active: bool = True
    installed_at: str = field(default_factory=iso)
    security_report: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"key": self.key}


class MarketplaceRegistry:
    """Tracks what is installed, at which version and with which checksum."""

    def __init__(self, *, db_path: str | Path | None = None) -> None:
        target = Path(db_path) if db_path else get_paths().ensure().db_path("marketplace.db")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS marketplace_items (
                kind TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, pinned INTEGER NOT NULL DEFAULT 0,
                installed_at TEXT NOT NULL, PRIMARY KEY (kind, name, version)
            );
            CREATE TABLE IF NOT EXISTS marketplace_sources (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, trusted INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()
        self._lock = threading.RLock()

    def record(self, item: InstalledItem) -> InstalledItem:
        with self._lock:
            self._conn.execute(
                "INSERT INTO marketplace_items (kind, name, version, payload, active, pinned, installed_at)"
                " VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind, name, version) DO UPDATE SET"
                " payload=excluded.payload, active=excluded.active, pinned=excluded.pinned",
                (item.kind, item.name, item.version, json.dumps(item.to_dict(), default=str), int(item.active),
                 int(item.pinned), item.installed_at),
            )
            self._conn.commit()
        return item

    def get(self, kind: str, name: str, *, version: str = "") -> InstalledItem:
        if version:
            row = self._conn.execute(
                "SELECT payload FROM marketplace_items WHERE kind=? AND name=? AND version=?",
                (kind, name, version),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT payload FROM marketplace_items WHERE kind=? AND name=? AND active=1"
                " ORDER BY installed_at DESC LIMIT 1", (kind, name)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"{kind} {name!r} is not installed")
        data = json.loads(row["payload"])
        data.pop("key", None)
        return InstalledItem(**{k: v for k, v in data.items() if k in InstalledItem.__dataclass_fields__})

    def list(self, *, kind: str = "", active_only: bool = True) -> list[InstalledItem]:
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if active_only:
            clauses.append("active = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(f"SELECT payload FROM marketplace_items {where} ORDER BY name", params).fetchall()
        items = []
        for row in rows:
            data = json.loads(row["payload"])
            data.pop("key", None)
            items.append(InstalledItem(**{k: v for k, v in data.items() if k in InstalledItem.__dataclass_fields__}))
        return items

    def versions(self, kind: str, name: str) -> list[InstalledItem]:
        rows = self._conn.execute(
            "SELECT payload FROM marketplace_items WHERE kind=? AND name=? ORDER BY installed_at DESC", (kind, name)
        ).fetchall()
        items = []
        for row in rows:
            data = json.loads(row["payload"])
            data.pop("key", None)
            items.append(InstalledItem(**{k: v for k, v in data.items() if k in InstalledItem.__dataclass_fields__}))
        return items

    def deactivate(self, kind: str, name: str, *, version: str = "") -> None:
        target = self.get(kind, name, version=version)
        with self._lock:
            self._conn.execute(
                "UPDATE marketplace_items SET active = 0 WHERE kind=? AND name=? AND version=?",
                (kind, name, target.version),
            )
            self._conn.commit()

    def pin(self, kind: str, name: str, *, version: str, pinned: bool = True) -> InstalledItem:
        item = self.get(kind, name, version=version)
        item.pinned = pinned
        return self.record(item)

    def add_source(self, source_id: str, url: str, *, trusted: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO marketplace_sources (id, url, trusted, added_at) VALUES (?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET url=excluded.url, trusted=excluded.trusted",
                (source_id, url, int(trusted), iso()),
            )
            self._conn.commit()

    def sources(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute("SELECT * FROM marketplace_sources ORDER BY id")]

    def stats(self) -> dict[str, Any]:
        by_kind = {
            row["kind"]: row["n"]
            for row in self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM marketplace_items WHERE active = 1 GROUP BY kind"
            )
        }
        return {"installed": sum(by_kind.values()), "by_kind": by_kind,
                "sources": len(self.sources()), "pinned": sum(1 for item in self.list() if item.pinned)}

    def close(self) -> None:
        self._conn.close()


_REGISTRY: MarketplaceRegistry | None = None
_LOCK = threading.Lock()


def get_marketplace_registry(**kwargs: Any) -> MarketplaceRegistry:
    global _REGISTRY
    with _LOCK:
        if _REGISTRY is None:
            _REGISTRY = MarketplaceRegistry(**kwargs)
        return _REGISTRY


def reset_marketplace_registry() -> None:
    global _REGISTRY
    with _LOCK:
        _REGISTRY = None
