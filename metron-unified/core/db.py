"""
Core DB layer — SQLite persistence for run history, job recovery, and multi-tenant quota.

Schema:
  runs      — completed/failed evaluation runs (one row per run)
  projects  — saved project configs per user
  tenants   — one row per company/client. Tracks company-wide quota.
  users     — one row per Cognito user. Tracks per-user quota and role.

DB path defaults to ./metron_runs.db; override via METRON_DB_PATH env var.
"""

from __future__ import annotations
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

_DB_PATH_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "metron_runs.db"))
_lock = threading.Lock()


def _db_path() -> str:
    return os.environ.get("METRON_DB_PATH", _DB_PATH_DEFAULT)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _current_period() -> str:
    """First day of the current UTC month — used as quota reset anchor."""
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1).isoformat()


def _period_expired(period_start: str) -> bool:
    """True if period_start belongs to a previous month."""
    return (period_start or "") < _current_period()


def init_db() -> None:
    """Create tables if they don't exist. Call once at server startup."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id           TEXT PRIMARY KEY,
                    project_id       TEXT NOT NULL,
                    user_email       TEXT,
                    tenant_id        TEXT,
                    timestamp        TEXT NOT NULL,
                    health_score     REAL,
                    domain           TEXT,
                    application_type TEXT,
                    status           TEXT DEFAULT 'completed',
                    results_json     TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs(timestamp)")
            # Migrations: add columns to existing DBs
            for col_sql in [
                "ALTER TABLE runs ADD COLUMN user_email TEXT",
                "ALTER TABLE runs ADD COLUMN tenant_id TEXT",
            ]:
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_email)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_tenant ON runs(tenant_id)")
            # Mark any runs left in 'running' state as failed (crash recovery)
            conn.execute("UPDATE runs SET status='failed' WHERE status='running'")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    project_id    TEXT PRIMARY KEY,
                    user_email    TEXT NOT NULL,
                    name          TEXT,
                    endpoint      TEXT,
                    api_key       TEXT,
                    document_text TEXT,
                    document_name TEXT,
                    created_at    TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_email)")

            # ── Multi-tenant tables ──────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id    TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    quota_limit  INTEGER DEFAULT 50,
                    quota_used   INTEGER DEFAULT 0,
                    period_start TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_email   TEXT PRIMARY KEY,
                    tenant_id    TEXT REFERENCES tenants(tenant_id),
                    role         TEXT DEFAULT 'viewer',
                    run_limit    INTEGER DEFAULT 10,
                    runs_used    INTEGER DEFAULT 0,
                    period_start TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)")
            conn.commit()
        finally:
            conn.close()


def save_run(
    run_id: str,
    project_id: str,
    health_score: float,
    domain: str,
    application_type: str,
    results: Dict[str, Any],
    status: str = "completed",
    user_email: str = "",
) -> None:
    """Persist a completed run to SQLite."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs
                    (run_id, project_id, user_email, timestamp, health_score, domain, application_type, status, results_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()
        finally:
            conn.close()


def touch_run(
    run_id: str,
    project_id: str,
    user_email: str,
    domain: str,
    application_type: str,
) -> None:
    """INSERT OR IGNORE a 'running' placeholder so crashes leave a DB record."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO runs
                    (run_id, project_id, user_email, timestamp, domain, application_type, status)
                VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (run_id, project_id, user_email, datetime.utcnow().isoformat(), domain, application_type),
            )
            conn.commit()
        finally:
            conn.close()


def mark_run_failed(run_id: str, error: str) -> None:
    """Update a run's status to 'failed' and store the error message."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE runs SET status='failed', results_json=? WHERE run_id=?",
                (json.dumps({"error": error}), run_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single run by run_id. Returns None if not found."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
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
            conn.close()


def get_runs_for_project(project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Return all runs for a project, sorted newest-first.
    results_json is NOT decoded (summary only) — call get_run() for full results.
    """
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT run_id, project_id, timestamp, health_score, domain,
                       application_type, status
                FROM runs
                WHERE project_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def compare_runs(run_id_a: str, run_id_b: str) -> Dict[str, Any]:
    """
    Basic diff between two runs: health score delta and per-class pass-rate change.
    Returns a summary dict suitable for the /api/runs/{a}/compare/{b} endpoint.
    """
    a = get_run(run_id_a)
    b = get_run(run_id_b)

    if not a or not b:
        missing = []
        if not a:
            missing.append(run_id_a)
        if not b:
            missing.append(run_id_b)
        return {"error": f"Run(s) not found: {missing}"}

    res_a = a.get("results", {})
    res_b = b.get("results", {})

    health_a = a.get("health_score", 0.0)
    health_b = b.get("health_score", 0.0)

    class_diff: Dict[str, Any] = {}
    classes_a = res_a.get("test_classes", {})
    classes_b = res_b.get("test_classes", {})
    all_classes = set(classes_a) | set(classes_b)

    for cls in sorted(all_classes):
        pr_a = classes_a.get(cls, {}).get("pass_rate")
        pr_b = classes_b.get(cls, {}).get("pass_rate")
        class_diff[cls] = {
            "run_a_pass_rate": pr_a,
            "run_b_pass_rate": pr_b,
            "delta": round((pr_b or 0.0) - (pr_a or 0.0), 4) if pr_a is not None and pr_b is not None else None,
        }

    return {
        "run_id_a":     run_id_a,
        "run_id_b":     run_id_b,
        "timestamp_a":  a.get("timestamp"),
        "timestamp_b":  b.get("timestamp"),
        "health_a":     health_a,
        "health_b":     health_b,
        "health_delta": round(health_b - health_a, 4),
        "class_diff":   class_diff,
    }


def load_recent_jobs(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Fetch runs from the last N hours for in-memory job store re-population on startup.
    Includes completed and failed runs (not 'running' — those were reset to 'failed' in init_db).
    """
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT run_id, status, results_json, user_email, project_id
                FROM runs
                WHERE timestamp >= ? AND status IN ('completed', 'failed')
                ORDER BY timestamp DESC
                LIMIT 200
                """,
                (cutoff,),
            ).fetchall()
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
                result.append(d)
            return result
        finally:
            conn.close()


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
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO projects
                    (project_id, user_email, name, endpoint, api_key,
                     document_text, document_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, user_email, name, endpoint, api_key,
                 document_text, document_name, datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()


def get_projects_for_user(user_email: str) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT project_id, name, endpoint, document_name, created_at
                FROM projects
                WHERE user_email = ?
                ORDER BY created_at DESC
                """,
                (user_email,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def delete_project(project_id: str) -> bool:
    """Delete a project and all its runs. Returns True if the project existed."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM projects WHERE project_id = ?", (project_id,)
            )
            conn.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ── Multi-tenant: user management ─────────────────────────────────────────────

def _ensure_default_tenant(conn: sqlite3.Connection) -> str:
    """Get or create the default tenant. Returns its tenant_id."""
    row = conn.execute("SELECT tenant_id FROM tenants WHERE name = 'Default'").fetchone()
    if row:
        return row["tenant_id"]
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tenants (tenant_id, name, quota_limit, quota_used, period_start, created_at) VALUES (?, ?, 100, 0, ?, ?)",
        (tid, "Default", _current_period(), datetime.utcnow().isoformat()),
    )
    return tid


def get_or_create_user(user_email: str, super_admin_emails: List[str] = None) -> Optional[Dict[str, Any]]:
    """
    Fetch the DB user record for a verified Cognito login.
    - Super admin emails (from SUPER_ADMIN_EMAILS env): auto-created/synced on every login.
    - All other emails: must be pre-created by an admin. Returns None if not found (access denied).
    """
    super_admin_emails = super_admin_emails or []
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if row:
                user = dict(row)
                updates = []
                params = []
                if _period_expired(user.get("period_start", "")):
                    updates.append("runs_used = 0")
                    updates.append("period_start = ?")
                    params.append(_current_period())
                    user["runs_used"] = 0
                    user["period_start"] = _current_period()
                # Always sync super_admin role from env
                if user_email in super_admin_emails and user.get("role") != "super_admin":
                    updates.append("role = 'super_admin'")
                    user["role"] = "super_admin"
                if updates:
                    params.append(user_email)
                    conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_email = ?", params)
                    conn.commit()
                return user

            # Super admins are auto-created from env var — no pre-registration needed
            if user_email in super_admin_emails:
                now = datetime.utcnow().isoformat()
                conn.execute(
                    "INSERT INTO users (user_email, tenant_id, role, run_limit, runs_used, period_start, created_at) "
                    "VALUES (?, NULL, 'super_admin', 0, 0, ?, ?)",
                    (user_email, _current_period(), now),
                )
                conn.commit()
                return {
                    "user_email": user_email, "tenant_id": None, "role": "super_admin",
                    "run_limit": 0, "runs_used": 0, "period_start": _current_period(), "created_at": now,
                }

            # Unknown email — admin must pre-register this user
            return None
        finally:
            conn.close()


def get_user(user_email: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_email = ?", (user_email,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# ── Multi-tenant: quota enforcement ───────────────────────────────────────────

def check_quota(user_email: str) -> Tuple[bool, str]:
    """
    Return (allowed, reason).
    Checks both the per-user run_limit and the tenant-wide quota_limit.
    Super admins are always allowed. Limits of 0 mean unlimited.
    Also resets counters if a new monthly period has started.
    """
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if not row:
                return True, ""  # unknown user — let get_or_create_user handle it

            user = dict(row)

            # Reset user counter if new month
            if _period_expired(user.get("period_start", "")):
                conn.execute(
                    "UPDATE users SET runs_used = 0, period_start = ? WHERE user_email = ?",
                    (_current_period(), user_email),
                )
                conn.commit()
                user["runs_used"] = 0

            if user.get("role") == "super_admin":
                return True, ""

            run_limit = user.get("run_limit", 10)
            runs_used = user.get("runs_used", 0)
            if run_limit > 0 and runs_used >= run_limit:
                return False, (
                    f"You have used all {run_limit} of your allocated runs this period. "
                    "Contact your administrator to increase your limit."
                )

            tenant_id = user.get("tenant_id")
            if tenant_id:
                tenant = conn.execute(
                    "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
                ).fetchone()
                if tenant:
                    tenant = dict(tenant)
                    if _period_expired(tenant.get("period_start", "")):
                        conn.execute(
                            "UPDATE tenants SET quota_used = 0, period_start = ? WHERE tenant_id = ?",
                            (_current_period(), tenant_id),
                        )
                        conn.commit()
                        tenant["quota_used"] = 0
                    quota_limit = tenant.get("quota_limit", 50)
                    quota_used  = tenant.get("quota_used", 0)
                    if quota_limit > 0 and quota_used >= quota_limit:
                        return False, (
                            "Your organization has reached its run limit for this period. "
                            "Contact your administrator."
                        )

            return True, ""
        finally:
            conn.close()


def increment_quota(user_email: str) -> None:
    """Increment runs_used for the user and their tenant. Call after a run completes."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE users SET runs_used = runs_used + 1 WHERE user_email = ?", (user_email,)
            )
            row = conn.execute("SELECT tenant_id FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if row and row["tenant_id"]:
                conn.execute(
                    "UPDATE tenants SET quota_used = quota_used + 1 WHERE tenant_id = ?",
                    (row["tenant_id"],),
                )
            conn.commit()
        finally:
            conn.close()


def get_quota_status(user_email: str) -> Dict[str, Any]:
    """Return quota display info for the current user."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if not row:
                return {"role": "viewer", "run_limit": 10, "runs_used": 0,
                        "tenant_quota_limit": 0, "tenant_quota_used": 0, "tenant_name": ""}
            user = dict(row)
            result: Dict[str, Any] = {
                "role":               user.get("role", "viewer"),
                "run_limit":          user.get("run_limit", 10),
                "runs_used":          user.get("runs_used", 0),
                "tenant_id":          user.get("tenant_id", ""),
                "tenant_name":        "",
                "tenant_quota_limit": 0,
                "tenant_quota_used":  0,
            }
            if user.get("tenant_id"):
                t = conn.execute(
                    "SELECT * FROM tenants WHERE tenant_id = ?", (user["tenant_id"],)
                ).fetchone()
                if t:
                    t = dict(t)
                    result["tenant_name"]        = t.get("name", "")
                    result["tenant_quota_limit"] = t.get("quota_limit", 0)
                    result["tenant_quota_used"]  = t.get("quota_used", 0)
            return result
        finally:
            conn.close()


# ── Tenant Admin: user management within a tenant ─────────────────────────────

def get_tenant_users(tenant_id: str) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT user_email, role, run_limit, runs_used, created_at FROM users WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def add_user_to_tenant(
    user_email: str, tenant_id: str, role: str = "viewer", run_limit: int = 10
) -> None:
    """Create or update a user's tenant assignment and role."""
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT user_email FROM users WHERE user_email = ?", (user_email,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET tenant_id = ?, role = ?, run_limit = ? WHERE user_email = ?",
                    (tenant_id, role, run_limit, user_email),
                )
            else:
                conn.execute(
                    "INSERT INTO users (user_email, tenant_id, role, run_limit, runs_used, period_start, created_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (user_email, tenant_id, role, run_limit, _current_period(), datetime.utcnow().isoformat()),
                )
            conn.commit()
        finally:
            conn.close()


def update_user_limit(user_email: str, run_limit: int, requesting_tenant_id: str) -> bool:
    """Tenant admin updates a user's run_limit. Returns False if user not in tenant."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT tenant_id FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if not row or row["tenant_id"] != requesting_tenant_id:
                return False
            conn.execute("UPDATE users SET run_limit = ? WHERE user_email = ?", (run_limit, user_email))
            conn.commit()
            return True
        finally:
            conn.close()


def update_user_role(user_email: str, role: str, requesting_tenant_id: str) -> bool:
    """Tenant admin updates a user's role. Returns False if user not in tenant."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT tenant_id FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if not row or row["tenant_id"] != requesting_tenant_id:
                return False
            conn.execute("UPDATE users SET role = ? WHERE user_email = ?", (role, user_email))
            conn.commit()
            return True
        finally:
            conn.close()


def remove_user(user_email: str, requesting_tenant_id: str) -> bool:
    """Tenant admin removes a user. Returns False if user not in tenant."""
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT tenant_id FROM users WHERE user_email = ?", (user_email,)).fetchone()
            if not row or row["tenant_id"] != requesting_tenant_id:
                return False
            conn.execute("DELETE FROM users WHERE user_email = ?", (user_email,))
            conn.commit()
            return True
        finally:
            conn.close()


def get_tenant_stats(tenant_id: str) -> Dict[str, Any]:
    """Tenant admin: summary of their org's users, runs, and quota."""
    with _lock:
        conn = _connect()
        try:
            tenant = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not tenant:
                return {}
            result = dict(tenant)
            result["users"] = [
                dict(r) for r in conn.execute(
                    "SELECT user_email, role, run_limit, runs_used FROM users WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchall()
            ]
            result["total_runs_all_time"] = conn.execute(
                "SELECT COUNT(*) as c FROM runs WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()["c"]
            return result
        finally:
            conn.close()


# ── Super Admin: tenant management ────────────────────────────────────────────

def get_all_tenants() -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
            tenants = []
            for r in rows:
                t = dict(r)
                t["user_count"] = conn.execute(
                    "SELECT COUNT(*) as c FROM users WHERE tenant_id = ?", (t["tenant_id"],)
                ).fetchone()["c"]
                t["run_count"] = conn.execute(
                    "SELECT COUNT(*) as c FROM runs WHERE tenant_id = ?", (t["tenant_id"],)
                ).fetchone()["c"]
                tenants.append(t)
            return tenants
        finally:
            conn.close()


def create_tenant(name: str, quota_limit: int = 50) -> Dict[str, Any]:
    tenant_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name, quota_limit, quota_used, period_start, created_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (tenant_id, name, quota_limit, _current_period(), now),
            )
            conn.commit()
            return {"tenant_id": tenant_id, "name": name, "quota_limit": quota_limit,
                    "quota_used": 0, "created_at": now}
        finally:
            conn.close()


def update_tenant(tenant_id: str, name: str = None, quota_limit: int = None) -> bool:
    updates, params = [], []
    if name is not None:
        updates.append("name = ?"); params.append(name)
    if quota_limit is not None:
        updates.append("quota_limit = ?"); params.append(quota_limit)
    if not updates:
        return False
    params.append(tenant_id)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                f"UPDATE tenants SET {', '.join(updates)} WHERE tenant_id = ?", params
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_tenant_detail(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Super admin: full tenant info with user list."""
    with _lock:
        conn = _connect()
        try:
            tenant = conn.execute("SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)).fetchone()
            if not tenant:
                return None
            result = dict(tenant)
            result["users"] = [
                dict(r) for r in conn.execute(
                    "SELECT user_email, role, run_limit, runs_used, created_at FROM users WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchall()
            ]
            return result
        finally:
            conn.close()


def reset_tenant_quota(tenant_id: str) -> bool:
    """Super admin: manually reset a tenant's quota_used to 0."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE tenants SET quota_used = 0, period_start = ? WHERE tenant_id = ?",
                (_current_period(), tenant_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_tenant(tenant_id: str) -> bool:
    """Super admin: delete a tenant and all its users and runs."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM users WHERE tenant_id = ?", (tenant_id,))
            conn.execute("DELETE FROM runs WHERE tenant_id = ?", (tenant_id,))
            cur = conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
