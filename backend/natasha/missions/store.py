"""Durable mission storage (SQLite). Missions survive restarts and crashes."""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from ..core import NotFoundError, get_paths
from ..core.clock import iso
from .models import Mission, MissionState, MissionStep

SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    priority INTEGER DEFAULT 3,
    parent_id TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missions_state ON missions(state, priority);
"""


class MissionStore:
    """SQLite-backed mission persistence."""

    def __init__(self, *, db_path: str | None = None) -> None:
        self.db_path = str(db_path or get_paths().db_path("missions"))
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes
    def save(self, mission: Mission) -> Mission:
        mission.updated_at = iso()
        payload = json.dumps(mission.to_dict(include_steps=True), default=str)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO missions (id, state, priority, parent_id, created_at, updated_at, started_at,"
                " finished_at, payload) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET state=excluded.state, priority=excluded.priority,"
                " updated_at=excluded.updated_at, started_at=excluded.started_at,"
                " finished_at=excluded.finished_at, payload=excluded.payload",
                (mission.id, mission.state.value, mission.priority, mission.parent_id, mission.created_at,
                 mission.updated_at, mission.started_at, mission.finished_at, payload),
            )
        return mission

    def add_checkpoint(self, mission: Mission, label: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        checkpoint = {"label": label, "at": iso(), "progress": mission.progress,
                      "state": mission.state.value, "steps_done": sum(
                          1 for step in mission.steps if step.state.value in ("done", "skipped")),
                      **(extra or {})}
        mission.checkpoints.append(checkpoint)
        mission.checkpoints[:] = mission.checkpoints[-50:]
        self.save(mission)
        return checkpoint

    # ------------------------------------------------------------------ reads
    def get(self, mission_id: str) -> Mission:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"mission {mission_id!r} not found")
        return self._deserialise(json.loads(row["payload"]))

    def list(self, *, state: str = "", active_only: bool = False, limit: int = 50) -> list[Mission]:
        query = "SELECT payload FROM missions"
        params: list[Any] = []
        if state:
            query += " WHERE state = ?"
            params.append(state)
        elif active_only:
            query += (" WHERE state NOT IN ('succeeded','failed','cancelled','rolled_back')")
        query += " ORDER BY priority ASC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._deserialise(json.loads(row["payload"])) for row in rows]

    def resumable(self) -> list[Mission]:
        """Missions interrupted by a restart: running, paused or blocked."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM missions WHERE state IN ('running','planned','paused','blocked',"
                "'verifying','waiting_approval') ORDER BY updated_at"
            ).fetchall()
        return [self._deserialise(json.loads(row["payload"])) for row in rows]

    def delete(self, mission_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM missions WHERE id = ?", (mission_id,))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM missions GROUP BY state").fetchall()
            total = self._conn.execute("SELECT COUNT(*) AS n FROM missions").fetchone()["n"]
        return {"total": total, "by_state": {row["state"]: row["n"] for row in rows},
                "db_path": self.db_path}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _deserialise(data: dict[str, Any]) -> Mission:
        steps = [MissionStep.from_dict(item) for item in data.get("steps", [])]
        mission = Mission(
            objective=data.get("objective", ""), id=data.get("id", ""), title=data.get("title", ""),
            state=MissionState(data.get("state", "draft")), steps=steps,
            plan_summary=data.get("plan_summary", ""),
            success_criteria=list(data.get("success_criteria") or []),
            verification_plan=list(data.get("verification_plan") or []),
            constraints=dict(data.get("constraints") or {}), scope=list(data.get("scope") or []),
            artifacts=list(data.get("artifacts") or []), deadline=data.get("deadline", ""),
            priority=int(data.get("priority", 3)), parent_id=data.get("parent_id", ""),
            created_at=data.get("created_at", iso()), updated_at=data.get("updated_at", iso()),
            started_at=data.get("started_at", ""), finished_at=data.get("finished_at", ""),
            attempts=int(data.get("attempts", 0)), error=data.get("error", ""),
            result=dict(data.get("result") or {}), verification=dict(data.get("verification") or {}),
            checkpoints=list(data.get("checkpoints") or []), metadata=dict(data.get("metadata") or {}),
            created_by=data.get("created_by", "owner"),
        )
        return mission

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_STORE: MissionStore | None = None
_LOCK = threading.Lock()


def get_mission_store(**kwargs: Any) -> MissionStore:
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = MissionStore(**kwargs)
        return _STORE


def reset_mission_store() -> None:
    global _STORE
    with _LOCK:
        _STORE = None
