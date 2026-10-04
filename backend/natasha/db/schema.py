"""The canonical schema, collected from the subsystems that own the tables.

Single source of truth: the ``SCHEMA`` constant a subsystem uses at runtime is the same text that
goes into the migration files. A drift test compares the two.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

#: module -> attribute holding its DDL
SCHEMA_SOURCES: dict[str, str] = {
    "natasha.events.log": "SCHEMA",
    "natasha.memory.store": "SCHEMA",
    "natasha.missions.store": "SCHEMA",
    "natasha.world.store": "SCHEMA",
    "natasha.approvals.engine": "SCHEMA",
    "natasha.mcp.registry": "SCHEMA",
    "natasha.credentials.vault": "SCHEMA",
    "natasha.api.auth": "SCHEMA",
}

#: Extra DDL that has no owning module (small, local tables).
EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS natasha_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS affect_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0
);
"""


def collect_schema(*, include_extra: bool = True) -> str:
    """Concatenate every subsystem's DDL, in dependency order."""
    parts: list[str] = []
    for module_name, attribute in SCHEMA_SOURCES.items():
        module = importlib.import_module(module_name)
        ddl = getattr(module, attribute, "")
        if not ddl:
            continue
        parts.append(f"-- {module_name}\n{ddl.strip()}\n")
    if include_extra:
        parts.append(f"-- shared runtime tables\n{EXTRA_DDL.strip()}\n")
    return "\n".join(parts)


def schema_tables(*, include_extra: bool = True) -> list[str]:
    """Table names in the collected schema, in creation order."""
    ddl = collect_schema(include_extra=include_extra)
    return re.findall(r"CREATE TABLE IF NOT EXISTS\s+([a-zA-Z_][\w]*)", ddl)


def render_sqlite_schema() -> str:
    """The SQLite DDL for a fresh install (the same text the subsystems execute at runtime)."""
    return collect_schema()


#: SQLite -> Postgres type translations applied when rendering the production DDL.
_TYPE_MAP = {
    "INTEGER PRIMARY KEY AUTOINCREMENT": "BIGSERIAL PRIMARY KEY",
    "INTEGER PRIMARY KEY": "INTEGER PRIMARY KEY",
    "BLOB": "BYTEA",
    "REAL": "DOUBLE PRECISION",
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
}


def render_postgres_schema() -> str:
    """Translate the collected SQLite DDL into Postgres DDL.

    Mechanical translation, deliberately conservative: names, columns, nullability and defaults are
    preserved so the two deployments cannot drift apart silently.
    """
    ddl = collect_schema()
    out = ddl
    out = out.replace("AUTOINCREMENT", "")
    out = re.sub(r"CREATE TABLE IF NOT EXISTS", "CREATE TABLE IF NOT EXISTS", out)
    # SQLite INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL
    out = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", out)
    out = re.sub(r"\bblob\b", "BYTEA", out, flags=re.IGNORECASE)
    out = re.sub(r"\bBLOB\b", "BYTEA", out)
    out = re.sub(r"\bREAL\b", "DOUBLE PRECISION", out)
    # SQLite partial-index syntax is valid in Postgres too; unixepoch() is not, so it is rejected early.
    if "unixepoch(" in out.lower():
        raise ValueError("the schema uses SQLite-only functions; translate those expressions by hand")
    return out


def schema_summary() -> dict[str, Any]:
    """A compact description used by `/api/doctor` and the migration tests."""
    tables = schema_tables()
    ddl = collect_schema()
    return {"tables": tables, "table_count": len(tables), "bytes": len(ddl),
            "sources": sorted(SCHEMA_SOURCES)}
