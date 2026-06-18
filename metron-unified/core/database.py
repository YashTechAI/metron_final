"""
Database engine + ORM models (SQLAlchemy 2.0, async).

One async engine + session factory for the whole app. Models mirror the original
SQLite schema 1:1 so the public API in core/db.py is unchanged. JSON payload
columns map to JSONB on Postgres and JSON-as-TEXT on SQLite, so the same code
runs against either backend.

Production: Postgres (set DB_* env vars). Local dev: async SQLite fallback.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import get_settings

_settings = get_settings()

# JSONB on Postgres (queryable/indexable), JSON-as-TEXT on SQLite.
JSONPayload = JSON().with_variant(JSONB, "postgresql")

# asyncpg prepared-statement cache must be off behind a transaction-mode pooler.
_connect_args: dict = {}
if _settings.is_postgres:
    _connect_args = {"statement_cache_size": _settings.db_statement_cache_size}

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    run_id:           Mapped[str] = mapped_column(String, primary_key=True)
    project_id:       Mapped[str] = mapped_column(String, nullable=False)
    user_email:       Mapped[Optional[str]] = mapped_column(String)
    tenant_id:        Mapped[Optional[str]] = mapped_column(String)
    timestamp:        Mapped[str] = mapped_column(String, nullable=False)
    health_score:     Mapped[Optional[float]] = mapped_column(Float)
    domain:           Mapped[Optional[str]] = mapped_column(String)
    application_type: Mapped[Optional[str]] = mapped_column(String)
    status:           Mapped[str] = mapped_column(String, default="completed")
    results_json:     Mapped[Optional[dict]] = mapped_column(JSONPayload)
    total_passed:     Mapped[Optional[int]] = mapped_column(Integer)
    total_tests:      Mapped[Optional[int]] = mapped_column(Integer)
    token_summary_json: Mapped[Optional[dict]] = mapped_column(JSONPayload)

    __table_args__ = (
        Index("idx_runs_project", "project_id"),
        Index("idx_runs_timestamp", "timestamp"),
        Index("idx_runs_user", "user_email"),
        Index("idx_runs_tenant", "tenant_id"),
    )


class Project(Base):
    __tablename__ = "projects"

    project_id:    Mapped[str] = mapped_column(String, primary_key=True)
    user_email:    Mapped[str] = mapped_column(String, nullable=False)
    name:          Mapped[Optional[str]] = mapped_column(String)
    endpoint:      Mapped[Optional[str]] = mapped_column(String)
    api_key:       Mapped[Optional[str]] = mapped_column(String)
    document_text: Mapped[Optional[str]] = mapped_column(Text)
    document_name: Mapped[Optional[str]] = mapped_column(String)
    created_at:    Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_projects_user", "user_email"),)


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id:    Mapped[str] = mapped_column(String, primary_key=True)
    name:         Mapped[str] = mapped_column(String, nullable=False)
    quota_limit:  Mapped[int] = mapped_column(Integer, default=50)
    quota_used:   Mapped[int] = mapped_column(Integer, default=0)
    period_start: Mapped[str] = mapped_column(String, nullable=False)
    created_at:   Mapped[str] = mapped_column(String, nullable=False)


class User(Base):
    __tablename__ = "users"

    user_email:   Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id:    Mapped[Optional[str]] = mapped_column(String, ForeignKey("tenants.tenant_id"))
    role:         Mapped[str] = mapped_column(String, default="viewer")
    run_limit:    Mapped[int] = mapped_column(Integer, default=10)
    runs_used:    Mapped[int] = mapped_column(Integer, default=0)
    period_start: Mapped[str] = mapped_column(String, nullable=False)
    created_at:   Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_users_tenant", "tenant_id"),)
