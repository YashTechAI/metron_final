"""
One-off data migration: copy rows from the legacy SQLite file into the
configured target database (Postgres in production).

Source : the old SQLite file (METRON_DB_PATH env var, or ../metron_runs.db).
Target : whatever core.config resolves from DB_* env vars (the async engine).

Usage (from metron-unified/):
    # 1. create the schema in Postgres first
    alembic upgrade head
    # 2. copy the data (point SQLITE_SOURCE at the old file if not the default)
    python -m scripts.migrate_sqlite_to_postgres
    python -m scripts.migrate_sqlite_to_postgres --source /path/to/metron_runs.db

The copy is idempotent (upsert by primary key) and ordered to respect the
users -> tenants foreign key. JSON TEXT blobs are parsed back into objects so
they land in JSONB columns. Nothing is written to the source.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys

# Make `core` importable when run as a script from metron-unified/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_settings  # noqa: E402
from core.database import Base, Project, Run, SessionLocal, Tenant, User, engine  # noqa: E402

_DEFAULT_SOURCE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "metron_runs.db")
)


def _open_source(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise SystemExit(f"Source SQLite file not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    if not _table_exists(conn, table):
        print(f"  [skip] source has no '{table}' table")
        return []
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _decode_json(value):
    """Old columns stored JSON as TEXT; parse to object for JSONB. Pass through dicts."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


async def _upsert(session, model, pk_attr: str, records: list[dict]) -> int:
    count = 0
    for rec in records:
        pk = rec.get(pk_attr)
        obj = await session.get(model, pk) if pk is not None else None
        if obj is None:
            obj = model(**{pk_attr: pk})
            session.add(obj)
        for key, val in rec.items():
            if key in ("results_json", "token_summary_json"):
                val = _decode_json(val)
            if hasattr(obj, key):
                setattr(obj, key, val)
        count += 1
    return count


async def migrate(source_path: str) -> None:
    settings = get_settings()
    target = "Postgres" if settings.is_postgres else "SQLite"
    print(f"Source : {source_path}")
    print(f"Target : {target} ({settings.database_url.split('@')[-1] if settings.is_postgres else settings.database_url})")

    # Ensure schema exists (no-op if Alembic already created it).
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    src = _open_source(source_path)
    try:
        tenants = _rows(src, "tenants")
        users = _rows(src, "users")
        projects = _rows(src, "projects")
        runs = _rows(src, "runs")
    finally:
        src.close()

    async with SessionLocal() as session:
        # FK order: tenants -> users, then projects, then runs.
        n_tenants = await _upsert(session, Tenant, "tenant_id", tenants)
        n_users = await _upsert(session, User, "user_email", users)
        n_projects = await _upsert(session, Project, "project_id", projects)
        n_runs = await _upsert(session, Run, "run_id", runs)
        await session.commit()

    await engine.dispose()

    print("Copied:")
    print(f"  tenants  : {n_tenants}")
    print(f"  users    : {n_users}")
    print(f"  projects : {n_projects}")
    print(f"  runs     : {n_runs}")
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy SQLite data into the configured DB.")
    parser.add_argument(
        "--source",
        default=os.environ.get("SQLITE_SOURCE", _DEFAULT_SOURCE),
        help="Path to the legacy SQLite file (default: ../metron_runs.db or $SQLITE_SOURCE).",
    )
    args = parser.parse_args()
    asyncio.run(migrate(args.source))


if __name__ == "__main__":
    main()
