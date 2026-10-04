"""The SQL migrations and the code's own schema must agree.

`scripts/generate_migrations.py` derives `migrations/sqlite/0001_initial_schema.sql` from each
subsystem's DDL, and fresh installs apply the SQL rather than the DDL. If the two drift, a clean
install is missing a table and only fails at runtime - in production, on a machine that has no
history. This test applies the migrations to an empty database, checks that every table the code
declares exists, and checks that re-running (and reverting) behaves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from natasha.db.migrations import MigrationRunner
from natasha.db.schema import schema_tables

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "migrations"


def _tables(db_path: Path) -> set[str]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def test_every_declared_table_exists_after_the_initial_migration(tmp_path):
    db_path = tmp_path / "fresh.db"
    runner = MigrationRunner(db_path=db_path)
    try:
        applied = runner.upgrade(target="head")
        assert applied["count"] >= 1, applied
    finally:
        runner.close()

    present = _tables(db_path)
    declared = set(schema_tables())
    assert declared, "the schema declares no tables; the check would be vacuous"
    missing = sorted(declared - present)
    assert not missing, f"migrations do not create these tables: {missing}"
    assert "schema_migrations" in present, "the migration bookkeeping table must exist"


def test_the_migration_files_and_the_declared_schema_stay_in_step():
    """The committed initial migration must contain every declared table.

    Catching this here means a new store added without regenerating the SQL fails in CI instead of on
    somebody's clean install.
    """
    initial = sorted((MIGRATIONS / "sqlite").glob("*.sql"))
    assert initial, "migrations/sqlite has no migrations"
    sql = "\n".join(path.read_text(encoding="utf-8") for path in initial)
    normalised = sql.lower()
    for table in schema_tables():
        assert f"create table if not exists {table.lower()}" in normalised or \
               f"create table {table.lower()}" in normalised, (
            f"table {table!r} is declared by the code but missing from the migration SQL; "
            "run scripts/generate_migrations.py"
        )


def test_migrations_are_ordered_and_idempotent(tmp_path):
    db_path = tmp_path / "twice.db"
    runner = MigrationRunner(db_path=db_path)
    try:
        first = runner.upgrade(target="head")
        assert first["count"] >= 1

        # Running again must apply nothing: a container restart runs this on every boot.
        second = runner.upgrade(target="head")
        assert second["count"] == 0, f"migrations are not idempotent: {second}"

        status = runner.status()
        assert status["pending"] == 0
        assert status["migrations"], status
        assert all(row["applied"] for row in status["migrations"]), status
        assert status["current"] == status["migrations"][-1]["version"]
    finally:
        runner.close()


def test_a_migration_can_be_reverted(tmp_path):
    db_path = tmp_path / "down.db"
    runner = MigrationRunner(db_path=db_path)
    try:
        runner.upgrade(target="head")
        before = runner.status()
        applied_before = [row for row in before["migrations"] if row["applied"]]
        assert applied_before
        result = runner.downgrade(steps=1)
        assert result["count"] == 1, result
        after = runner.status()
        applied_after = [row for row in after["migrations"] if row["applied"]]
        assert len(applied_after) == len(applied_before) - 1
        assert after["pending"] == 1
    finally:
        runner.close()


def test_migration_files_follow_the_naming_convention():
    """`NNNN_name.sql`, in both dialects, with matching names - otherwise drift is invisible."""
    for dialect in ("sqlite", "postgres"):
        files = sorted((MIGRATIONS / dialect).glob("*.sql"))
        assert files, f"migrations/{dialect} is empty"
        for path in files:
            stem = path.stem
            number, _, name = stem.partition("_")
            assert len(number) == 4 and number.isdigit(), f"{path.name} needs a 4-digit prefix"
            assert name and name.islower(), f"{path.name} needs a lower_snake_case name"
    sqlite_names = {path.name for path in (MIGRATIONS / "sqlite").glob("*.sql")}
    postgres_names = {path.name for path in (MIGRATIONS / "postgres").glob("*.sql")}
    assert sqlite_names == postgres_names, (
        f"the dialects have drifted: {sorted(sqlite_names ^ postgres_names)}")


@pytest.mark.parametrize("dialect", ["sqlite"])
def test_the_runner_rejects_a_dialect_it_cannot_reach(dialect):
    """A Postgres DSN is required for the Postgres dialect: fail loudly instead of silently using SQLite."""
    from natasha.core import ConflictError

    with pytest.raises(ConflictError):
        MigrationRunner(dialect="postgres")
