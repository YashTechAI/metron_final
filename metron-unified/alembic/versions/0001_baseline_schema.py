"""baseline schema — runs, projects, tenants, users

Mirrors the original SQLite schema. JSON payload columns are JSONB on Postgres
and JSON on other backends.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# JSONB on Postgres, JSON elsewhere.
JSONPayload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("quota_limit", sa.Integer(), nullable=True),
        sa.Column("quota_used", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("user_email", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id"), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("run_limit", sa.Integer(), nullable=True),
        sa.Column("runs_used", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_users_tenant", "users", ["tenant_id"])

    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("user_email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("endpoint", sa.String(), nullable=True),
        sa.Column("api_key", sa.String(), nullable=True),
        sa.Column("document_text", sa.Text(), nullable=True),
        sa.Column("document_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("idx_projects_user", "projects", ["user_email"])

    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("timestamp", sa.String(), nullable=False),
        sa.Column("health_score", sa.Float(), nullable=True),
        sa.Column("domain", sa.String(), nullable=True),
        sa.Column("application_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("results_json", JSONPayload, nullable=True),
        sa.Column("total_passed", sa.Integer(), nullable=True),
        sa.Column("total_tests", sa.Integer(), nullable=True),
        sa.Column("token_summary_json", JSONPayload, nullable=True),
    )
    op.create_index("idx_runs_project", "runs", ["project_id"])
    op.create_index("idx_runs_timestamp", "runs", ["timestamp"])
    op.create_index("idx_runs_user", "runs", ["user_email"])
    op.create_index("idx_runs_tenant", "runs", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("runs")
    op.drop_index("idx_projects_user", table_name="projects")
    op.drop_table("projects")
    op.drop_index("idx_users_tenant", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
