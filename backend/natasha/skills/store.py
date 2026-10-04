"""Persistence for installed skills: one SQLite row per (skill id, version).

This is the storage half of the skill subsystem. The lifecycle rules - what a state transition means,
who may approve, when a skill may run - live in :mod:`natasha.skills.lifecycle`; this module only
answers "give me the record" and "store the record", which is why it may be imported by data code and
never decides anything by itself.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from ..core import SkillError
from ..core.clock import iso
from ..core.paths import get_paths
from .models import SkillRecord

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS skills ("
    " id TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,"
    " installed_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (id, version))"
)


class SkillStore:
    """The ``skills.db`` table of installed skills."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        target = Path(db_path) if db_path else get_paths().ensure().db_path("skills.db")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    # -- writes ------------------------------------------------------------------ #
    def save(self, record: SkillRecord) -> SkillRecord:
        """Insert or update a record, stamping ``updated_at``."""
        record.updated_at = iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO skills (id, version, payload, state, installed_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(id, version) DO UPDATE SET payload=excluded.payload,"
                " state=excluded.state, installed_at=excluded.installed_at",
                (record.id, record.version, json.dumps(record.to_dict(), default=str), record.state,
                 record.installed_at),
            )
            self._conn.commit()
        return record

    # -- reads ------------------------------------------------------------------- #
    def get(self, skill_id: str, *, version: str = "") -> SkillRecord:
        """The newest (or the named) record for a skill. Raises ``SkillError`` when unknown."""
        with self._lock:
            if version:
                row = self._conn.execute(
                    "SELECT payload FROM skills WHERE id=? AND version=?", (skill_id, version)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT payload FROM skills WHERE id=? ORDER BY installed_at DESC LIMIT 1",
                    (skill_id,),
                ).fetchone()
        if row is None:
            raise SkillError(f"skill {skill_id!r} is not registered")
        return SkillRecord(**json.loads(row["payload"]))

    def list(self, *, state: str = "") -> list[SkillRecord]:
        """Every record, optionally filtered to one state, ordered by id."""
        clause, params = ("WHERE state = ?", [state]) if state else ("", [])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload FROM skills {clause} ORDER BY id", params).fetchall()
        return [SkillRecord(**json.loads(row["payload"])) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SCHEMA", "SkillStore"]
