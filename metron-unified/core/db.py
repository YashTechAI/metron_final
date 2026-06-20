"""
Core DB layer — PostgreSQL persistence for run history and project configs.

Schema:
  runs     — completed/failed evaluation runs (one row per run)
  projects — saved project configs per user

Connection config (env vars):
  DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME
SSL is forced (sslmode=require) — Supabase and most managed Postgres require it.
"""

from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

# ── Connection pool ────────────────────────────────────────────────────────────
# Lazily created on first use. Small max — managed Postgres poolers (e.g. Supabase)
# cap the number of concurrent connections.
_pool: Optional[ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(
                minconn=1,
                maxconn=5,
                user=os.environ.get("DB_USER", ""),
                password=os.environ.get("DB_PASSWORD", ""),
                host=os.environ.get("DB_HOST", ""),
                port=os.environ.get("DB_PORT", "5432"),
                dbname=os.environ.get("DB_NAME", "postgres"),
                sslmode="require",
                cursor_factory=RealDictCursor,
            )
    return _pool


def _connect():
    """Borrow a connection from the pool. Always pair with _put()."""
    return _get_pool().getconn()


def _put(conn) -> None:
    """Return a connection to the pool."""
    if conn is not None:
        _get_pool().putconn(conn)


def _strip_nul(s: Optional[str]) -> Optional[str]:
    """Remove NUL (0x00) bytes — Postgres text columns reject them.

    Documents read as text (e.g. PDFs via FileReader.readAsText) can contain
    NUL bytes; SQLite tolerated them but Postgres raises ValueError. Strip them.
    """
    return s.replace("\x00", "") if isinstance(s, str) else s


def init_db() -> None:
    """Create tables if they don't exist. Call once at server startup."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id             TEXT PRIMARY KEY,
                    project_id         TEXT NOT NULL,
                    user_email         TEXT,
                    timestamp          TEXT NOT NULL,
                    health_score       DOUBLE PRECISION,
                    domain             TEXT,
                    application_type   TEXT,
                    status             TEXT DEFAULT 'completed',
                    results_json       TEXT,
                    total_passed       INTEGER,
                    total_tests        INTEGER,
                    token_summary_json TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_email)")
            # Mark any runs left in 'running' state as failed (crash recovery)
            cur.execute("UPDATE runs SET status='failed' WHERE status='running'")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id        TEXT PRIMARY KEY,
                    user_email        TEXT NOT NULL,
                    name              TEXT,
                    endpoint          TEXT,
                    api_key           TEXT,
                    document_text     TEXT,
                    document_name     TEXT,
                    created_at        TEXT NOT NULL,
                    profile_json      TEXT,
                    tech_profile_json TEXT,
                    document_sha      TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_email)")
            # Additive migration for pre-existing DBs (idempotent — safe to re-run).
            # Caches the LLM-extracted seed-doc profile so runs don't re-extract every time.
            cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS profile_json TEXT")
            cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS tech_profile_json TEXT")
            cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS document_sha TEXT")
        conn.commit()
    finally:
        _put(conn)


def save_run(
    run_id: str,
    project_id: str,
    health_score: Optional[float],
    domain: str,
    application_type: str,
    results: Dict[str, Any],
    status: str = "completed",
    user_email: str = "",
    total_passed: Optional[int] = None,
    total_tests: Optional[int] = None,
) -> None:
    """Persist a completed run to Postgres (upsert on run_id)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs
                    (run_id, project_id, user_email, timestamp, health_score, domain,
                     application_type, status, results_json, total_passed, total_tests)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    project_id       = EXCLUDED.project_id,
                    user_email       = EXCLUDED.user_email,
                    timestamp        = EXCLUDED.timestamp,
                    health_score     = EXCLUDED.health_score,
                    domain           = EXCLUDED.domain,
                    application_type = EXCLUDED.application_type,
                    status           = EXCLUDED.status,
                    results_json     = EXCLUDED.results_json,
                    total_passed     = EXCLUDED.total_passed,
                    total_tests      = EXCLUDED.total_tests
                """,
                (
                    run_id,
                    project_id,
                    user_email,
                    datetime.utcnow().isoformat(),
                    health_score,
                    domain,
                    application_type,
                    status,
                    json.dumps(results),
                    total_passed,
                    total_tests,
                ),
            )
        conn.commit()
    finally:
        _put(conn)


def touch_run(
    run_id: str,
    project_id: str,
    user_email: str,
    domain: str,
    application_type: str,
) -> None:
    """Insert a 'running' placeholder so crashes leave a DB record (no-op if it exists)."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs
                    (run_id, project_id, user_email, timestamp, domain, application_type, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'running')
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id, project_id, user_email, datetime.utcnow().isoformat(), domain, application_type),
            )
        conn.commit()
    finally:
        _put(conn)


def mark_run_failed(run_id: str, error: str) -> None:
    """Update a run's status to 'failed' and store the error message."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET status='failed', results_json=%s WHERE run_id=%s",
                (json.dumps({"error": error}), run_id),
            )
        conn.commit()
    finally:
        _put(conn)


def save_token_summary(run_id: str, token_summary: Dict[str, Any]) -> None:
    """Persist the LLMOps token summary for a run so it survives server restarts."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET token_summary_json=%s WHERE run_id=%s",
                (json.dumps(token_summary), run_id),
            )
        conn.commit()
    finally:
        _put(conn)


def get_token_summary(run_id: str) -> Optional[Dict[str, Any]]:
    """Load the LLMOps token summary for a run from DB. Returns None if not found."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token_summary_json FROM runs WHERE run_id=%s",
                (run_id,),
            )
            row = cur.fetchone()
        if row and row["token_summary_json"]:
            return json.loads(row["token_summary_json"])
        return None
    finally:
        _put(conn)


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single run by run_id. Returns None if not found."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("results_json"):
            try:
                d["results"] = json.loads(d.pop("results_json"))
            except Exception:
                d.pop("results_json", None)
        return d
    finally:
        _put(conn)


def get_runs_for_project(project_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Return all runs for a project, sorted newest-first.
    results_json is NOT decoded (summary only) — call get_run() for full results.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, project_id, timestamp, health_score, domain,
                       application_type, status, total_passed, total_tests
                FROM runs
                WHERE project_id = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (project_id, limit),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        _put(conn)


def load_recent_jobs(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Fetch runs from the last N hours for in-memory job store re-population on startup.
    Includes completed and failed runs (not 'running' — those were reset to 'failed' in init_db).
    """
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_id, status, results_json, token_summary_json, user_email, project_id
                FROM runs
                WHERE timestamp >= %s AND status IN ('completed', 'failed')
                ORDER BY timestamp DESC
                LIMIT 200
                """,
                (cutoff,),
            )
            rows = cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("results_json"):
                try:
                    d["results"] = json.loads(d.pop("results_json"))
                except Exception:
                    d.pop("results_json", None)
            else:
                d.pop("results_json", None)
            if d.get("token_summary_json"):
                try:
                    d["token_summary"] = json.loads(d.pop("token_summary_json"))
                except Exception:
                    d.pop("token_summary_json", None)
            else:
                d.pop("token_summary_json", None)
            result.append(d)
        return result
    finally:
        _put(conn)


# ── Project persistence ───────────────────────────────────────────────────────

def save_project(
    project_id: str,
    user_email: str,
    name: str,
    endpoint: str,
    api_key: str,
    document_text: str,
    document_name: str,
) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO projects
                    (project_id, user_email, name, endpoint, api_key,
                     document_text, document_name, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id) DO UPDATE SET
                    user_email    = EXCLUDED.user_email,
                    name          = EXCLUDED.name,
                    endpoint      = EXCLUDED.endpoint,
                    api_key       = EXCLUDED.api_key,
                    document_text = EXCLUDED.document_text,
                    document_name = EXCLUDED.document_name
                """,
                (project_id, user_email, name, endpoint, api_key,
                 _strip_nul(document_text), _strip_nul(document_name),
                 datetime.utcnow().isoformat()),
            )
        conn.commit()
    finally:
        _put(conn)


def save_project_profile(
    project_id: str,
    profile_json: str,
    tech_profile_json: str,
    document_sha: str,
) -> None:
    """Cache the LLM-extracted AppProfile + TechnicalProfile for a project's seed doc.

    Keyed by document_sha so the profile is reused across runs and re-extracted only when
    the seed document changes. Values are Pydantic model_dump_json() strings.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE projects
                SET profile_json = %s, tech_profile_json = %s, document_sha = %s
                WHERE project_id = %s
                """,
                (_strip_nul(profile_json), _strip_nul(tech_profile_json),
                 document_sha, project_id),
            )
        conn.commit()
    finally:
        _put(conn)


def get_projects_for_user(user_email: str) -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT project_id, name, endpoint, document_name, created_at
                FROM projects
                WHERE user_email = %s
                ORDER BY created_at DESC
                """,
                (user_email,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        _put(conn)


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM projects WHERE project_id = %s", (project_id,))
            row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _put(conn)


def delete_project(project_id: str) -> bool:
    """Delete a project and all its runs. Returns True if the project existed."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM projects WHERE project_id = %s", (project_id,))
            deleted = cur.rowcount > 0
            cur.execute("DELETE FROM runs WHERE project_id = %s", (project_id,))
        conn.commit()
        return deleted
    finally:
        _put(conn)
