"""
Core DB layer — async SQLAlchemy persistence for run history, job recovery,
and multi-tenant quota.

Backend is chosen by config (core/config.py): Postgres in production (DB_* env
vars), async SQLite locally. All public functions are coroutines — callers must
await them. Return shapes are plain dicts, identical to the previous sqlite3
implementation, so call sites only changed by adding `await`.

Schema (see core/database.py for the ORM models):
  runs      — completed/failed evaluation runs (one row per run)
  projects  — saved project configs per user
  tenants   — one row per company/client. Tracks company-wide quota.
  users     — one row per Cognito user. Tracks per-user quota and role.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, inspect as sa_inspect, select, update

from core.database import Base, Project, Run, SessionLocal, Tenant, User, engine


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_dict(obj: Any) -> Dict[str, Any]:
    """Map an ORM instance to a plain column->value dict."""
    return {c.key: getattr(obj, c.key) for c in sa_inspect(obj).mapper.column_attrs}


def _current_period() -> str:
    """First day of the current UTC month — used as quota reset anchor."""
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1).isoformat()


def _period_expired(period_start: str) -> bool:
    """True if period_start belongs to a previous month."""
    return (period_start or "") < _current_period()


# ── Schema init + crash recovery ───────────────────────────────────────────────

async def init_db() -> None:
    """
    Create tables if they don't exist, run crash recovery, and restore
    tenant_admin roles. Call once at server startup.

    In production, Alembic owns schema changes; create_all here is an idempotent
    safety net (and the only setup needed for the local SQLite fallback).
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        # Mark any runs left in 'running' state as failed (crash recovery)
        await session.execute(
            update(Run).where(Run.status == "running").values(status="failed")
        )

        # Restore tenant_admin role: for each tenant with NO tenant_admin user,
        # promote the earliest-created user in that tenant to tenant_admin.
        tenant_ids = (await session.execute(select(Tenant.tenant_id))).scalars().all()
        admin_tenant_ids = set(
            (
                await session.execute(
                    select(User.tenant_id).where(
                        User.role == "tenant_admin", User.tenant_id.is_not(None)
                    )
                )
            ).scalars().all()
        )
        for tid in tenant_ids:
            if tid in admin_tenant_ids:
                continue
            earliest = (
                await session.execute(
                    select(User)
                    .where(User.tenant_id == tid)
                    .order_by(User.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if earliest:
                earliest.role = "tenant_admin"
                print(f"[DB] Restored tenant_admin role to {earliest.user_email} for tenant {tid}")

        await session.commit()


# ── Runs ────────────────────────────────────────────────────────────────────

async def save_run(
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
    """Persist a completed run (upsert by run_id)."""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            run = Run(run_id=run_id)
            session.add(run)
        run.project_id = project_id
        run.user_email = user_email
        run.timestamp = datetime.utcnow().isoformat()
        run.health_score = health_score
        run.domain = domain
        run.application_type = application_type
        run.status = status
        run.results_json = results
        run.total_passed = total_passed
        run.total_tests = total_tests
        await session.commit()


async def touch_run(
    run_id: str,
    project_id: str,
    user_email: str,
    domain: str,
    application_type: str,
) -> None:
    """Insert a 'running' placeholder so crashes leave a DB record (ignore if exists)."""
    async with SessionLocal() as session:
        existing = await session.get(Run, run_id)
        if existing is not None:
            return
        session.add(
            Run(
                run_id=run_id,
                project_id=project_id,
                user_email=user_email,
                timestamp=datetime.utcnow().isoformat(),
                domain=domain,
                application_type=application_type,
                status="running",
            )
        )
        await session.commit()


async def mark_run_failed(run_id: str, error: str) -> None:
    """Update a run's status to 'failed' and store the error message."""
    async with SessionLocal() as session:
        await session.execute(
            update(Run)
            .where(Run.run_id == run_id)
            .values(status="failed", results_json={"error": error})
        )
        await session.commit()


async def save_token_summary(run_id: str, token_summary: Dict[str, Any]) -> None:
    """Persist the LLMOps token summary for a run so it survives server restarts."""
    async with SessionLocal() as session:
        await session.execute(
            update(Run).where(Run.run_id == run_id).values(token_summary_json=token_summary)
        )
        await session.commit()


async def get_token_summary(run_id: str) -> Optional[Dict[str, Any]]:
    """Load the LLMOps token summary for a run. Returns None if not found."""
    async with SessionLocal() as session:
        val = (
            await session.execute(
                select(Run.token_summary_json).where(Run.run_id == run_id)
            )
        ).scalar_one_or_none()
        return val or None


async def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single run by run_id. Returns None if not found."""
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return None
        d = _to_dict(run)
        if d.get("results_json"):
            d["results"] = d.pop("results_json")
        else:
            d.pop("results_json", None)
        return d


async def get_runs_for_project(project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Return all runs for a project, newest-first (summary fields only — call
    get_run() for full results).
    """
    cols = [
        Run.run_id, Run.project_id, Run.timestamp, Run.health_score, Run.domain,
        Run.application_type, Run.status, Run.total_passed, Run.total_tests,
    ]
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(*cols)
                .where(Run.project_id == project_id)
                .order_by(Run.timestamp.desc())
                .limit(limit)
            )
        ).all()
        return [dict(r._mapping) for r in rows]


async def compare_runs(run_id_a: str, run_id_b: str) -> Dict[str, Any]:
    """
    Basic diff between two runs: health score delta and per-class pass-rate change.
    """
    a = await get_run(run_id_a)
    b = await get_run(run_id_b)

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


async def load_recent_jobs(hours: int = 24) -> List[Dict[str, Any]]:
    """
    Fetch runs from the last N hours for in-memory job store re-population on
    startup. Includes completed and failed runs only.
    """
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    cols = [
        Run.run_id, Run.status, Run.results_json, Run.token_summary_json,
        Run.user_email, Run.project_id,
    ]
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(*cols)
                .where(Run.timestamp >= cutoff, Run.status.in_(("completed", "failed")))
                .order_by(Run.timestamp.desc())
                .limit(200)
            )
        ).all()
        result = []
        for r in rows:
            d = dict(r._mapping)
            if d.get("results_json"):
                d["results"] = d.pop("results_json")
            else:
                d.pop("results_json", None)
            if d.get("token_summary_json"):
                d["token_summary"] = d.pop("token_summary_json")
            else:
                d.pop("token_summary_json", None)
            result.append(d)
        return result


# ── Project persistence ───────────────────────────────────────────────────────

async def save_project(
    project_id: str,
    user_email: str,
    name: str,
    endpoint: str,
    api_key: str,
    document_text: str,
    document_name: str,
) -> None:
    async with SessionLocal() as session:
        project = await session.get(Project, project_id)
        if project is None:
            project = Project(project_id=project_id)
            session.add(project)
        project.user_email = user_email
        project.name = name
        project.endpoint = endpoint
        project.api_key = api_key
        project.document_text = document_text
        project.document_name = document_name
        project.created_at = datetime.utcnow().isoformat()
        await session.commit()


async def get_projects_for_user(user_email: str) -> List[Dict[str, Any]]:
    cols = [
        Project.project_id, Project.name, Project.endpoint,
        Project.document_name, Project.created_at,
    ]
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(*cols)
                .where(Project.user_email == user_email)
                .order_by(Project.created_at.desc())
            )
        ).all()
        return [dict(r._mapping) for r in rows]


async def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    async with SessionLocal() as session:
        project = await session.get(Project, project_id)
        return _to_dict(project) if project else None


async def delete_project(project_id: str) -> bool:
    """Delete a project and all its runs. Returns True if the project existed."""
    async with SessionLocal() as session:
        result = await session.execute(
            delete(Project).where(Project.project_id == project_id)
        )
        await session.execute(delete(Run).where(Run.project_id == project_id))
        await session.commit()
        return result.rowcount > 0


# ── Multi-tenant: user management ─────────────────────────────────────────────

async def get_or_create_user(
    user_email: str, super_admin_emails: List[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetch the DB user record for a verified Cognito login.
    - Super admin emails (from SUPER_ADMIN_EMAILS env): auto-created/synced on every login.
    - All other emails: must be pre-created by an admin. Returns None if not found.
    """
    super_admin_emails = super_admin_emails or []
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        if user is not None:
            if _period_expired(user.period_start or ""):
                user.runs_used = 0
                user.period_start = _current_period()
            if user_email in super_admin_emails and user.role != "super_admin":
                user.role = "super_admin"
            await session.commit()
            return _to_dict(user)

        # Super admins are auto-created from env var — no pre-registration needed
        if user_email in super_admin_emails:
            now = datetime.utcnow().isoformat()
            user = User(
                user_email=user_email, tenant_id=None, role="super_admin",
                run_limit=0, runs_used=0, period_start=_current_period(), created_at=now,
            )
            session.add(user)
            await session.commit()
            return _to_dict(user)

        # Unknown email — admin must pre-register this user
        return None


async def get_user(user_email: str) -> Optional[Dict[str, Any]]:
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        return _to_dict(user) if user else None


# ── Multi-tenant: quota enforcement ───────────────────────────────────────────

async def try_consume_quota(user_email: str) -> Tuple[bool, str]:
    """
    Atomically check quota AND increment counters in one transaction.
    Returns (allowed, reason). If allowed, counters are already incremented.
    Super admins are always allowed. Limits of 0 mean unlimited.

    Atomicity on Postgres comes from row-level locks (SELECT ... FOR UPDATE);
    SQLite serializes writes at the file level, so the same code is safe there.
    """
    async with SessionLocal() as session:
        async with session.begin():
            user = (
                await session.execute(
                    select(User).where(User.user_email == user_email).with_for_update()
                )
            ).scalar_one_or_none()
            if user is None:
                return True, ""

            # Reset user counter if new month
            if _period_expired(user.period_start or ""):
                user.runs_used = 0
                user.period_start = _current_period()

            if user.role == "super_admin":
                return True, ""

            run_limit = user.run_limit if user.run_limit is not None else 10
            runs_used = user.runs_used or 0
            if run_limit > 0 and runs_used >= run_limit:
                return False, (
                    f"You have used all {runs_used}/{run_limit} of your allocated runs this month. "
                    "Ask your team admin to increase your limit."
                )

            tenant_id = user.tenant_id
            if tenant_id:
                tenant = (
                    await session.execute(
                        select(Tenant).where(Tenant.tenant_id == tenant_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if tenant is not None:
                    if _period_expired(tenant.period_start or ""):
                        tenant.quota_used = 0
                        tenant.period_start = _current_period()

                    quota_limit = tenant.quota_limit if tenant.quota_limit is not None else 50
                    quota_used = tenant.quota_used or 0
                    if quota_limit > 0 and quota_used >= quota_limit:
                        return False, (
                            f"Your organization has used all {quota_used}/{quota_limit} runs for this month. "
                            "Contact your team admin to increase the org limit."
                        )
                    tenant.quota_used = quota_used + 1

            # Increment user quota
            user.runs_used = (user.runs_used or 0) + 1
            return True, ""


async def get_quota_status(user_email: str) -> Dict[str, Any]:
    """Return quota display info for the current user."""
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        if user is None:
            return {"role": "viewer", "run_limit": 10, "runs_used": 0,
                    "tenant_quota_limit": 0, "tenant_quota_used": 0, "tenant_name": ""}
        result: Dict[str, Any] = {
            "role":               user.role or "viewer",
            "run_limit":          user.run_limit if user.run_limit is not None else 10,
            "runs_used":          user.runs_used or 0,
            "tenant_id":          user.tenant_id or "",
            "tenant_name":        "",
            "tenant_quota_limit": 0,
            "tenant_quota_used":  0,
        }
        if user.tenant_id:
            tenant = await session.get(Tenant, user.tenant_id)
            if tenant is not None:
                result["tenant_name"]        = tenant.name or ""
                result["tenant_quota_limit"] = tenant.quota_limit or 0
                result["tenant_quota_used"]  = tenant.quota_used or 0
        return result


# ── Tenant Admin: user management within a tenant ─────────────────────────────

async def get_tenant_users(tenant_id: str) -> List[Dict[str, Any]]:
    cols = [User.user_email, User.role, User.run_limit, User.runs_used, User.created_at]
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(*cols).where(User.tenant_id == tenant_id))
        ).all()
        return [dict(r._mapping) for r in rows]


async def add_user_to_tenant(
    user_email: str, tenant_id: str, role: str = "viewer", run_limit: int = 10
) -> None:
    """Create or update a user's tenant assignment and role."""
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        if user is not None:
            user.tenant_id = tenant_id
            user.role = role
            user.run_limit = run_limit
        else:
            session.add(
                User(
                    user_email=user_email, tenant_id=tenant_id, role=role,
                    run_limit=run_limit, runs_used=0,
                    period_start=_current_period(), created_at=datetime.utcnow().isoformat(),
                )
            )
        await session.commit()


async def update_user_limit(user_email: str, run_limit: int, requesting_tenant_id: str) -> bool:
    """Tenant admin updates a user's run_limit. Returns False if user not in tenant."""
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        if user is None or user.tenant_id != requesting_tenant_id:
            return False
        user.run_limit = run_limit
        await session.commit()
        return True


async def update_user_role(user_email: str, role: str, requesting_tenant_id: str) -> bool:
    """Tenant admin updates a user's role. Returns False if user not in tenant or is the tenant admin."""
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        if user is None or user.tenant_id != requesting_tenant_id:
            return False
        # Protect the tenant admin — their role must never be changed via this path
        if user.role == "tenant_admin":
            return False
        user.role = role
        await session.commit()
        return True


async def remove_user(user_email: str, requesting_tenant_id: str) -> bool:
    """Tenant admin removes a user. Returns False if user not in tenant."""
    async with SessionLocal() as session:
        user = await session.get(User, user_email)
        if user is None or user.tenant_id != requesting_tenant_id:
            return False
        await session.delete(user)
        await session.commit()
        return True


async def get_tenant_runs(tenant_id: str) -> List[Dict[str, Any]]:
    """Return all runs for a tenant with user attribution, newest first."""
    cols = [
        Run.run_id, Run.user_email, Run.project_id, Run.timestamp,
        Run.health_score, Run.total_passed, Run.total_tests,
        Run.domain, Run.application_type, Run.status,
    ]
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(*cols)
                .join(User, Run.user_email == User.user_email)
                .where(User.tenant_id == tenant_id)
                .order_by(Run.timestamp.desc())
                .limit(200)
            )
        ).all()
        return [dict(r._mapping) for r in rows]


async def get_tenant_stats(tenant_id: str) -> Dict[str, Any]:
    """Tenant admin: summary of their org's users, runs, and quota."""
    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return {}
        result = _to_dict(tenant)
        user_cols = [User.user_email, User.role, User.run_limit, User.runs_used]
        users = (
            await session.execute(select(*user_cols).where(User.tenant_id == tenant_id))
        ).all()
        result["users"] = [dict(r._mapping) for r in users]
        result["total_runs_all_time"] = (
            await session.execute(
                select(func.count()).select_from(Run).where(Run.tenant_id == tenant_id)
            )
        ).scalar_one()
        return result


# ── Super Admin: tenant management ────────────────────────────────────────────

async def get_all_tenants() -> List[Dict[str, Any]]:
    async with SessionLocal() as session:
        tenants = (
            await session.execute(select(Tenant).order_by(Tenant.created_at.desc()))
        ).scalars().all()
        result = []
        for tenant in tenants:
            t = _to_dict(tenant)
            t["user_count"] = (
                await session.execute(
                    select(func.count()).select_from(User).where(User.tenant_id == tenant.tenant_id)
                )
            ).scalar_one()
            t["run_count"] = (
                await session.execute(
                    select(func.count()).select_from(Run).where(Run.tenant_id == tenant.tenant_id)
                )
            ).scalar_one()
            result.append(t)
        return result


async def create_tenant(name: str, quota_limit: int = 50) -> Dict[str, Any]:
    tenant_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with SessionLocal() as session:
        session.add(
            Tenant(
                tenant_id=tenant_id, name=name, quota_limit=quota_limit,
                quota_used=0, period_start=_current_period(), created_at=now,
            )
        )
        await session.commit()
        return {"tenant_id": tenant_id, "name": name, "quota_limit": quota_limit,
                "quota_used": 0, "created_at": now}


async def update_tenant(tenant_id: str, name: str = None, quota_limit: int = None) -> bool:
    if name is None and quota_limit is None:
        return False
    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return False
        if name is not None:
            tenant.name = name
        if quota_limit is not None:
            tenant.quota_limit = quota_limit
        await session.commit()
        return True


async def get_tenant_detail(tenant_id: str) -> Optional[Dict[str, Any]]:
    """Super admin: full tenant info with user list."""
    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return None
        result = _to_dict(tenant)
        user_cols = [
            User.user_email, User.role, User.run_limit, User.runs_used, User.created_at,
        ]
        users = (
            await session.execute(select(*user_cols).where(User.tenant_id == tenant_id))
        ).all()
        result["users"] = [dict(r._mapping) for r in users]
        return result


async def reset_tenant_quota(tenant_id: str) -> bool:
    """Super admin: manually reset a tenant's quota_used to 0."""
    async with SessionLocal() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return False
        tenant.quota_used = 0
        tenant.period_start = _current_period()
        await session.commit()
        return True


async def delete_tenant(tenant_id: str) -> bool:
    """Super admin: delete a tenant and all its users and runs."""
    async with SessionLocal() as session:
        await session.execute(delete(User).where(User.tenant_id == tenant_id))
        await session.execute(delete(Run).where(Run.tenant_id == tenant_id))
        result = await session.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
        await session.commit()
        return result.rowcount > 0
