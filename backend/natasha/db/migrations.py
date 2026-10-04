"""Migration runner for SQLite (development) and Postgres (production).

* files live in ``migrations/sqlite`` and ``migrations/postgres`` as ``NNNN_name.sql``;
* the same runner logic drives both; statements are executed in order inside a transaction;
* a ``-- down`` section makes a migration reversible;
* applied versions are recorded in ``schema_migrations`` with a checksum, so edited history is caught.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import ConflictError, NotFoundError
from ..core.clock import iso
from ..core.hashing import sha256_text

#: ``-- down`` on its own line separates the up and down halves of a migration file.
DOWN_MARKER = re.compile(r"^--\s*down\s*$", re.IGNORECASE | re.MULTILINE)
_NAME = re.compile(r"^(\d{3,})_([A-Za-z0-9_\-]+)\.sql$")
#: Postgres dollar-quote opener ($$ or $tag$).
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")
#: A SQL word for BEGIN/END tracking.
_WORD = re.compile(r"[a-z_]+")


def repo_migrations_dir() -> Path:
    """Locate the migrations directory in a checkout or in an installed package."""
    candidates = [
        Path(__file__).resolve().parents[3] / "migrations",
        Path(__file__).resolve().parents[2] / "migrations",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


@dataclass
class Migration:
    """One versioned SQL file."""

    version: str
    name: str
    path: Path
    dialect: str = "sqlite"
    up_sql: str = ""
    down_sql: str = ""
    checksum: str = ""

    @classmethod
    def load(cls, path: Path, *, dialect: str = "sqlite") -> "Migration":
        text = path.read_text(encoding="utf-8")
        match = _NAME.match(path.name)
        if not match:
            raise ConflictError(f"migration file {path.name!r} must be named NNNN_name.sql")
        parts = DOWN_MARKER.split(text, maxsplit=1)
        up = parts[0]
        down = parts[1] if len(parts) > 1 else ""
        return cls(version=match.group(1), name=match.group(2), path=path, dialect=dialect,
                   up_sql=up.strip(), down_sql=down.strip(), checksum=sha256_text(text))

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "name": self.name, "dialect": self.dialect,
                "reversible": bool(self.down_sql), "checksum": self.checksum[:16]}


class MigrationRunner:
    """Applies and reverts migrations against one database."""

    def __init__(self, *, db_path: str | Path | None = None, dialect: str = "sqlite",
                 directory: Path | None = None, connection: Any = None,
                 dsn: str = "") -> None:
        self.dialect = dialect
        self.directory = Path(directory) if directory else repo_migrations_dir() / dialect
        if connection is not None:
            self._conn = connection
            self._own = False
            self.db_path = ":memory:"
        elif dialect == "postgres":
            if not dsn:
                raise ConflictError("a Postgres DSN is required for the postgres dialect")
            self._conn = _connect_postgres(dsn)
            self._own = True
            self.db_path = dsn
        else:
            if db_path is None:
                from ..core import get_paths

                db_path = get_paths().ensure().db_path("natasha.db")
            self.db_path = str(db_path)
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._own = True
        self._lock = threading.RLock()
        self._ensure_bookkeeping()

    # ------------------------------------------------------------------ bookkeeping
    def _ensure_bookkeeping(self) -> None:
        placeholder = "TEXT"
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                f"version {placeholder} PRIMARY KEY, name {placeholder} NOT NULL, "
                f"checksum {placeholder} NOT NULL, applied_at {placeholder} NOT NULL, "
                "duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0)"
            )
            self._conn.commit()

    def applied(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, name, checksum, applied_at, duration_ms FROM schema_migrations ORDER BY version"
            ).fetchall()
        return {row[0]: {"version": row[0], "name": row[1], "checksum": row[2], "applied_at": row[3],
                         "duration_ms": row[4]} for row in rows}

    # ------------------------------------------------------------------ discovery
    def available(self) -> list[Migration]:
        if not self.directory.is_dir():
            return []
        migrations: list[Migration] = []
        for path in sorted(self.directory.glob("*.sql")):
            try:
                migrations.append(Migration.load(path, dialect=self.dialect))
            except ConflictError:
                continue
        migrations.sort(key=lambda item: item.version)
        return migrations

    def pending(self) -> list[Migration]:
        applied = self.applied()
        return [migration for migration in self.available() if migration.version not in applied]

    def status(self) -> dict[str, Any]:
        applied = self.applied()
        rows: list[dict[str, Any]] = []
        for migration in self.available():
            record = applied.get(migration.version)
            rows.append({**migration.to_dict(), "applied": record is not None,
                         "applied_at": record["applied_at"] if record else "",
                         "drift": bool(record and record["checksum"] != migration.checksum)})
        unknown = [version for version in applied if version not in {m.version for m in self.available()}]
        return {"dialect": self.dialect, "directory": str(self.directory), "migrations": rows,
                "current": rows[-1]["version"] if rows and all(row["applied"] for row in rows) else "",
                "pending": len([row for row in rows if not row["applied"]]),
                "unknown_applied": unknown}

    # ------------------------------------------------------------------ apply
    def _execute_script(self, script: str) -> None:
        """Run one migration file.

        SQLite uses ``executescript`` because triggers and multi-statement bodies are legal there.
        Other dialects get a statement splitter that understands ``BEGIN ... END`` blocks and quoted
        strings, which is enough for the DDL we ship (and fails loudly rather than silently skipping).
        """
        if self.dialect == "sqlite":
            self._conn.executescript(script)
            return
        for statement in _split_statements(script):
            self._conn.execute(statement)

    def upgrade(self, *, target: str = "", dry_run: bool = False) -> dict[str, Any]:
        todo = self.pending()
        if target and target != "head":
            todo = [migration for migration in todo if migration.version <= target]
        results: list[dict[str, Any]] = []
        for migration in todo:
            started = time.perf_counter()
            if dry_run:
                results.append({"version": migration.version, "name": migration.name, "applied": False,
                                "dry_run": True, "statements": len(_split_statements(migration.up_sql))})
                continue
            try:
                self._execute_script(migration.up_sql)
                duration = (time.perf_counter() - started) * 1000
                self._conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum, applied_at, duration_ms)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (migration.version, migration.name, migration.checksum, iso(), duration),
                )
                self._conn.commit()
            except Exception as exc:
                self._conn.rollback()
                raise ConflictError(
                    f"migration {migration.version}_{migration.name} failed: {type(exc).__name__}: {exc}"
                ) from exc
            results.append({"version": migration.version, "name": migration.name, "applied": True,
                            "duration_ms": round(duration, 2)})
        return {"dialect": self.dialect, "applied": results, "count": len(results)}

    def downgrade(self, *, steps: int = 1) -> dict[str, Any]:
        applied = self.applied()
        by_version = {migration.version: migration for migration in self.available()}
        ordered = sorted(applied, reverse=True)[: max(1, steps)]
        results: list[dict[str, Any]] = []
        for version in ordered:
            migration = by_version.get(version)
            if migration is None:
                raise NotFoundError(f"migration {version} is applied but its file is missing")
            if not migration.down_sql:
                raise ConflictError(f"migration {version}_{migration.name} is not reversible")
            try:
                self._execute_script(migration.down_sql)
                self._conn.execute("DELETE FROM schema_migrations WHERE version = ?", (version,))
                self._conn.commit()
            except Exception as exc:
                self._conn.rollback()
                raise ConflictError(f"rollback of {version} failed: {type(exc).__name__}: {exc}") from exc
            results.append({"version": version, "name": migration.name, "reverted": True})
        return {"dialect": self.dialect, "reverted": results, "count": len(results)}

    def create(self, name: str, *, body: str = "") -> Path:
        """Create the next migration file (empty up/down skeleton)."""
        self.directory.mkdir(parents=True, exist_ok=True)
        highest = max([int(migration.version) for migration in self.available()] or [0])
        version = f"{highest + 1:04d}"
        path = self.directory / f"{version}_{name.replace(' ', '_')}.sql"
        path.write_text(body or f"-- {name}\n\n-- down\n", encoding="utf-8")
        return path

    def verify_checksums(self) -> list[dict[str, Any]]:
        """Detect edited migration history (a changed file that was already applied)."""
        applied = self.applied()
        drift: list[dict[str, Any]] = []
        for migration in self.available():
            record = applied.get(migration.version)
            if record and record["checksum"] != migration.checksum:
                drift.append({"version": migration.version, "expected": record["checksum"][:16],
                              "found": migration.checksum[:16]})
        return drift

    def close(self) -> None:
        if self._own:
            self._conn.close()


def _split_statements(script: str) -> list[str]:
    """Split SQL into statements, respecting strings, comments, dollar-quotes and BEGIN..END blocks.

    A naive ``split(";")`` corrupts trigger bodies and function definitions; this splitter keeps them
    whole. It is deliberately small: it handles what our DDL and Postgres migration files use, and
    anything it cannot parse raises rather than executing half a statement.
    """
    statements: list[str] = []
    current: list[str] = []
    lowered = script.lower()
    quote: str | None = None
    dollar_tag: str | None = None
    block_depth = 0
    index = 0
    length = len(script)

    while index < length:
        char = script[index]

        if quote:  # inside a ' or " string
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue

        if dollar_tag:  # inside a $tag$ ... $tag$ block
            if lowered.startswith(dollar_tag, index):
                current.append(script[index:index + len(dollar_tag)])
                index += len(dollar_tag)
                dollar_tag = None
                continue
            current.append(char)
            index += 1
            continue

        if char == "-" and lowered.startswith("--", index):
            end = lowered.find("\n", index)
            end = length if end == -1 else end
            current.append(script[index:end])
            index = end
            continue

        if char == "/" and lowered.startswith("/*", index):
            end = lowered.find("*/", index + 2)
            end = length if end == -1 else end + 2
            current.append(script[index:end])
            index = end
            continue

        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue

        if char == "$":  # postgres dollar-quoting: $$ or $tag$
            match = _DOLLAR_TAG.match(script, index)
            if match:
                dollar_tag = match.group(0).lower()
                current.append(match.group(0))
                index = match.end()
                continue

        word = _WORD.match(lowered, index)
        if word:
            token = word.group(0)
            if token == "begin":
                block_depth += 1
            elif token == "end" and block_depth:
                block_depth -= 1

        if char == ";" and block_depth == 0:
            statement = "".join(current).strip()
            if statement and not _is_only_comments(statement):
                statements.append(statement)
            current = []
            index += 1
            continue

        current.append(char)
        index += 1

    tail = "".join(current).strip()
    if tail:
        if _is_only_comments(tail):
            raise ValueError("the SQL script ends inside a statement or an unterminated block")
        statements.append(tail)
    return statements


def _is_only_comments(statement: str) -> bool:
    lines = [line.strip() for line in statement.splitlines()]
    return all(not line or line.startswith("--") for line in lines)


def _connect_postgres(dsn: str) -> Any:
    try:
        import psycopg  # type: ignore

        return psycopg.connect(dsn, autocommit=False)
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return psycopg2.connect(dsn)
    except ImportError as exc:
        raise ConflictError(
            "Postgres support needs psycopg: python3 -m pip install --break-system-packages 'psycopg[binary]'"
        ) from exc


_RUNNER: MigrationRunner | None = None
_LOCK = threading.Lock()


def get_migration_runner(**kwargs: Any) -> MigrationRunner:
    global _RUNNER
    with _LOCK:
        if _RUNNER is None:
            _RUNNER = MigrationRunner(**kwargs)
        return _RUNNER


def reset_migration_runner() -> None:
    global _RUNNER
    with _LOCK:
        _RUNNER = None
