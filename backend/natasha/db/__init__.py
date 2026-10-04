"""Database layer: connection helpers, schema collection and migrations.

Each subsystem owns its tables and declares them with ``CREATE TABLE IF NOT EXISTS``, so a fresh
install works without a migration step. Migrations exist for *evolution*: adding columns, moving data,
adding indexes, and keeping a Postgres deployment in step with the SQLite development one.
"""

from .migrations import Migration, MigrationRunner, get_migration_runner
from .schema import collect_schema, render_postgres_schema, schema_summary, schema_tables

__all__ = ["Migration", "MigrationRunner", "get_migration_runner",
           "collect_schema", "render_postgres_schema", "schema_tables", "schema_summary"]
