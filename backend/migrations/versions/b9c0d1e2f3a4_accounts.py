"""D2: 账户体系（用户、登录会话、邮件令牌、工作区认领）

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-09-19

变更性质：增量。新增四张表；guide_projects 增加可空 owner_user_id，
历史访客工作区归属不变（仍由 owner_session_id 决定），直到被显式认领。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("email", sa.String(length=254), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("display_name", sa.String(length=60), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("email_verified_at", sa.DateTime(), nullable=True),
            sa.Column("last_login_at", sa.DateTime(), nullable=True),
            sa.Column("password_changed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )

    if not inspector.has_table("auth_sessions"):
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("user_agent", sa.String(length=200), nullable=True),
            sa.Column("ip_hash", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_reason", sa.String(length=32), nullable=True),
            sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token"),
        )
        op.create_index("ix_auth_sessions_user", "auth_sessions", ["user_id", "created_at"])

    if not inspector.has_table("email_tokens"):
        op.create_table(
            "email_tokens",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purpose", sa.String(length=24), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("token_hash", name="uq_email_tokens_token"),
        )
        op.create_index("ix_email_tokens_user_purpose", "email_tokens", ["user_id", "purpose"])

    if not inspector.has_table("workspace_claims"):
        op.create_table(
            "workspace_claims",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "project_id",
                sa.String(length=36),
                sa.ForeignKey("guide_projects.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("guest_session_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("project_id", name="uq_claims_project"),
        )
        op.create_index("ix_claims_user", "workspace_claims", ["user_id", "created_at"])

    columns = {item["name"] for item in sa.inspect(bind).get_columns("guide_projects")}
    if "owner_user_id" not in columns:
        op.add_column("guide_projects", sa.Column("owner_user_id", sa.String(length=36), nullable=True))
        op.create_foreign_key(
            "fk_projects_owner_user",
            "guide_projects",
            "users",
            ["owner_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index("ix_projects_owner_user", "guide_projects", ["owner_user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {item["name"] for item in sa.inspect(bind).get_columns("guide_projects")}
    if "owner_user_id" in columns:
        indexes = {item["name"] for item in sa.inspect(bind).get_indexes("guide_projects")}
        if "ix_projects_owner_user" in indexes:
            op.drop_index("ix_projects_owner_user", table_name="guide_projects")
        fks = {item["name"] for item in sa.inspect(bind).get_foreign_keys("guide_projects")}
        if "fk_projects_owner_user" in fks:
            op.drop_constraint("fk_projects_owner_user", "guide_projects", type_="foreignkey")
        op.drop_column("guide_projects", "owner_user_id")

    for table in ("workspace_claims", "email_tokens", "auth_sessions", "users"):
        if inspector.has_table(table):
            op.drop_table(table)
