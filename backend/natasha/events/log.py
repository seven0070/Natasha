"""Append-only event log backed by SQLite (Postgres-compatible dialect).

Guarantees
----------
* **Append-only** - database triggers reject ``UPDATE``/``DELETE`` on ``events``.
* **Hash-chained** - each event's hash covers its predecessor's hash, so tampering is detectable
  via :meth:`EventLog.verify_chain`.
* **Secret-free** - every payload passes through the :class:`SecretSanitizer` before persistence.
* **Durable** - WAL mode, synchronous=FULL for the audit trail.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..core import iso, new_id
from ..core.paths import get_paths
from ..core.risk import RiskLevel
from .bus import get_bus
from .models import GENESIS_HASH, Event, EventKind
from .sanitizer import SecretSanitizer, get_sanitizer

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT    NOT NULL UNIQUE,
    ts         TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    actor      TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT '',
    trace_id   TEXT    NOT NULL DEFAULT '',
    mission_id TEXT    NOT NULL DEFAULT '',
    parent_id  TEXT    NOT NULL DEFAULT '',
    risk       TEXT    NOT NULL DEFAULT 'NONE',
    payload    TEXT    NOT NULL DEFAULT '{}',
    prev_hash  TEXT    NOT NULL,
    hash       TEXT    NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_trace   ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_mission ON events(mission_id);
CREATE INDEX IF NOT EXISTS idx_events_kind    ON events(kind, seq);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);

CREATE TRIGGER IF NOT EXISTS events_append_only_update
BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'event log is append-only: UPDATE rejected');
END;
CREATE TRIGGER IF NOT EXISTS events_append_only_delete
BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'event log is append-only: DELETE rejected');
END;
"""


class EventLog:
    """Durable, verifiable record of everything that happened."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        sanitizer: SecretSanitizer | None = None,
        publish: bool = True,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._owns_connection = connection is None
        if connection is not None:
            self._conn = connection
            self.path = Path(":memory:")
        else:
            target = Path(path) if path else get_paths().ensure().db_path("events.db")
            target.parent.mkdir(parents=True, exist_ok=True)
            self.path = target
            self._conn = sqlite3.connect(target, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self._sanitizer = sanitizer or get_sanitizer()
        self._publish = publish
        self._head_hash, self._head_seq = self._load_head()

    # -- internals ------------------------------------------------------------- #
    def _load_head(self) -> tuple[str, int]:
        row = self._conn.execute("SELECT hash, seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            return GENESIS_HASH, 0
        return row["hash"], int(row["seq"])

    # -- writes ---------------------------------------------------------------- #
    def append(
        self,
        kind: EventKind | str,
        payload: dict[str, Any] | None = None,
        *,
        actor: str = "system",
        source: str = "",
        trace_id: str = "",
        mission_id: str = "",
        parent_id: str = "",
        risk: RiskLevel | int | str = RiskLevel.NONE,
        ts: str | None = None,
    ) -> Event:
        """Sanitise, seal and persist an event. Returns the sealed event."""
        event = Event(
            kind=EventKind.parse(kind),
            payload=self._sanitizer.sanitize(payload or {}),
            actor=actor,
            source=source,
            trace_id=trace_id or "",
            mission_id=mission_id,
            parent_id=parent_id,
            risk=RiskLevel.parse(risk),
            ts=ts or iso(),
        )
        with self._lock:
            event.seal(self._head_hash, self._head_seq + 1)
            try:
                self._conn.execute(
                    "INSERT INTO events (id, ts, kind, actor, source, trace_id, mission_id, parent_id,"
                    " risk, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.id, event.ts, event.kind.value, event.actor, event.source, event.trace_id,
                        event.mission_id, event.parent_id, event.risk.name,
                        json.dumps(event.payload, ensure_ascii=False, default=str), event.prev_hash, event.hash,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
            self._head_hash, self._head_seq = event.hash, event.seq
        if self._publish:
            get_bus().publish_threadsafe(event)
        return event

    def append_event(self, event: Event) -> Event:
        """Append a pre-built event (used by importers and tests)."""
        return self.append(
            event.kind, event.payload, actor=event.actor, source=event.source, trace_id=event.trace_id,
            mission_id=event.mission_id, parent_id=event.parent_id, risk=event.risk, ts=event.ts,
        )

    # -- reads ----------------------------------------------------------------- #
    def get(self, event_id: str) -> Event | None:
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return Event.from_row(row) if row else None

    def query(
        self,
        *,
        kinds: list[EventKind | str] | None = None,
        trace_id: str = "",
        mission_id: str = "",
        actor: str = "",
        min_risk: RiskLevel | None = None,
        since_seq: int = 0,
        limit: int = 200,
        descending: bool = True,
        search: str = "",
    ) -> list[Event]:
        """Structured query over the log."""
        clauses: list[str] = ["seq > ?"]
        params: list[Any] = [since_seq]
        if kinds:
            values = [EventKind.parse(k).value for k in kinds]
            clauses.append(f"kind IN ({','.join('?' * len(values))})")
            params.extend(values)
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if mission_id:
            clauses.append("mission_id = ?")
            params.append(mission_id)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if min_risk is not None:
            names = [level.name for level in RiskLevel if level >= min_risk]
            clauses.append(f"risk IN ({','.join('?' * len(names))})")
            params.extend(names)
        if search:
            clauses.append("payload LIKE ?")
            params.append(f"%{search}%")
        order = "DESC" if descending else "ASC"
        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY seq {order} LIMIT ?"
        params.append(max(1, min(int(limit), 10_000)))
        return [Event.from_row(row) for row in self._conn.execute(sql, params)]

    def iter_all(self, *, batch: int = 1000) -> Iterator[Event]:
        """Stream the whole log in sequence order (used by verifiers and exporters)."""
        last = 0
        while True:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?", (last, batch)
            ).fetchall()
            if not rows:
                return
            for row in rows:
                event = Event.from_row(row)
                last = event.seq
                yield event

    def trace(self, trace_id: str) -> list[Event]:
        return self.query(trace_id=trace_id, limit=10_000, descending=False)

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"])

    def head(self) -> tuple[str, int]:
        return self._head_hash, self._head_seq

    def stats(self) -> dict[str, Any]:
        by_kind = {
            row["kind"]: row["n"]
            for row in self._conn.execute("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind ORDER BY n DESC")
        }
        by_risk = {
            row["risk"]: row["n"]
            for row in self._conn.execute("SELECT risk, COUNT(*) AS n FROM events GROUP BY risk")
        }
        return {
            "total": self.count(),
            "head_hash": self._head_hash,
            "head_seq": self._head_seq,
            "by_kind": by_kind,
            "by_risk": by_risk,
            "path": str(self.path),
        }

    # -- integrity ------------------------------------------------------------- #
    def verify_chain(self) -> tuple[bool, dict[str, Any]]:
        """Recompute the chain. Returns ``(ok, report)``; never raises."""
        previous = GENESIS_HASH
        expected_seq = 1
        checked = 0
        for event in self.iter_all(batch=500):
            payload = self._sanitizer.sanitize(event.payload)
            probe = Event(
                kind=event.kind, payload=payload, actor=event.actor, source=event.source,
                trace_id=event.trace_id, mission_id=event.mission_id, parent_id=event.parent_id,
                risk=event.risk, id=event.id, ts=event.ts,
            )
            probe.seal(previous, event.seq)
            if event.prev_hash != previous:
                return False, {"ok": False, "reason": "prev_hash mismatch", "broken_at": event.seq,
                               "event_id": event.id, "checked": checked}
            if probe.hash != event.hash:
                return False, {"ok": False, "reason": "hash mismatch (payload or fields altered)",
                               "broken_at": event.seq, "event_id": event.id, "checked": checked}
            if event.seq != expected_seq:
                return False, {"ok": False, "reason": "sequence gap", "broken_at": event.seq,
                               "expected": expected_seq, "checked": checked}
            previous = event.hash
            expected_seq += 1
            checked += 1
        return True, {"ok": True, "checked": checked, "head_hash": previous}

    def export_jsonl(self, path: str | Path, *, verify: bool = True) -> Path:
        """Write a portable export; refuses to export a broken chain."""
        if verify:
            ok, report = self.verify_chain()
            if not ok:
                raise RuntimeError(f"refusing to export corrupt log: {report}")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            for event in self.iter_all():
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return target

    def import_jsonl(self, path: str | Path) -> tuple[int, bool]:
        """Import an export, re-verifying its chain. Returns ``(count, chain_ok)``."""
        previous = GENESIS_HASH
        count = 0
        chain_ok = True
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                probe = Event(
                    kind=EventKind.parse(raw["kind"]), payload=raw.get("payload", {}),
                    actor=raw.get("actor", "system"), source=raw.get("source", ""),
                    trace_id=raw.get("trace_id", ""), mission_id=raw.get("mission_id", ""),
                    parent_id=raw.get("parent_id", ""), risk=RiskLevel.parse(raw.get("risk", "NONE")),
                    id=raw.get("id") or new_id("evt"), ts=raw["ts"],
                )
                probe.seal(previous, count + 1)
                if probe.hash != raw.get("hash"):
                    chain_ok = False
                self.append_event(probe)
                previous = probe.hash
                count += 1
        return count, chain_ok

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()


_LOGS: dict[str, EventLog] = {}
_LOCK = threading.Lock()


def get_event_log(path: str | Path | None = None) -> EventLog:
    """Process-wide log (one instance per resolved path)."""
    key = str(Path(path).resolve()) if path else str(get_paths().ensure().db_path("events.db"))
    with _LOCK:
        if key not in _LOGS:
            _LOGS[key] = EventLog(path)
        return _LOGS[key]


def reset_event_logs() -> None:
    """Test hook: close and forget all open logs."""
    with _LOCK:
        for log in _LOGS.values():
            log.close()
        _LOGS.clear()
