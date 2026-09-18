"""D1-a: 上传幂等、租约隔离、逐次记账、并发额度、持久消息底座

Revision ID: d1a2b3c4e5f6
Revises: 6e0d44ba8d56
Create Date: 2026-09-18

变更性质：增量。不重命名既有表，只把金额列改为 DECIMAL（B3 要求金额不用浮点），
并为 B1/B2/B3/B6 与持久对话补上新表与可空列。旧数据可读，回退只删除本轮新增结构。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d1a2b3c4e5f6"
down_revision = "6e0d44ba8d56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- B2：fencing 与意图
    op.add_column("agent_runs", sa.Column("intent", sa.String(length=32), nullable=False, server_default="analyze_image"))
    op.add_column("agent_runs", sa.Column("input_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("agent_runs", sa.Column("source_run_id", sa.String(length=36), nullable=True))
    op.add_column("agent_runs", sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("agent_runs", sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("agent_runs", sa.Column("reserved_cost", sa.Numeric(12, 6), nullable=False, server_default="0"))
    op.alter_column(
        "agent_runs",
        "media_asset_id",
        existing_type=sa.String(length=36),
        existing_nullable=False,
        nullable=True,
    )
    # B3：金额改用 DECIMAL，并按币种/价格版本区分
    op.alter_column(
        "agent_runs",
        "budget_max_cost_usd",
        new_column_name="budget_max_cost",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
    )
    op.alter_column(
        "agent_runs",
        "used_cost_usd",
        new_column_name="used_cost",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
        server_default="0",
    )

    # ---------------------------------------------------------------- B3：逐次调用记账
    op.alter_column(
        "tool_invocations",
        "cost_usd",
        new_column_name="cost",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
        nullable=True,
    )
    op.alter_column("tool_invocations", "tokens_prompt", existing_type=sa.Integer(), nullable=True)
    op.alter_column("tool_invocations", "tokens_completion", existing_type=sa.Integer(), nullable=True)
    op.add_column("tool_invocations", sa.Column("currency", sa.String(length=3), nullable=False, server_default="CNY"))
    op.add_column("tool_invocations", sa.Column("price_version", sa.String(length=32), nullable=False, server_default="v1"))
    op.add_column("tool_invocations", sa.Column("usage_known", sa.Boolean(), nullable=False, server_default="0"))

    op.alter_column(
        "usage_ledger",
        "cost_usd",
        new_column_name="cost",
        existing_type=sa.Float(),
        type_=sa.Numeric(12, 6),
        existing_nullable=False,
        nullable=True,
    )
    op.add_column("usage_ledger", sa.Column("currency", sa.String(length=3), nullable=False, server_default="CNY"))
    op.add_column("usage_ledger", sa.Column("price_version", sa.String(length=32), nullable=False, server_default="v1"))

    # ---------------------------------------------------------------- D4：相册可编辑
    op.add_column("media_assets", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column("media_assets", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("media_assets", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("media_assets", sa.Column("active_run_id", sa.String(length=36), nullable=True))
    op.create_index("ix_media_project_deleted", "media_assets", ["project_id", "deleted_at"])

    # ---------------------------------------------------------------- B4/版本：地点来源与版本
    op.add_column("places", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("places", sa.Column("source", sa.String(length=16), nullable=False, server_default="user"))

    # ---------------------------------------------------------------- B1：上传幂等命令
    op.create_table(
        "upload_commands",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_session_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="COMPLETED"),
        sa.Column("media_asset_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_session_id"], ["demo_sessions.id"], name="fk_upload_commands_owner_session_id_demo_sessions", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_upload_commands"),
        sa.UniqueConstraint("owner_session_id", "idempotency_key", name="uq_upload_cmd_owner_key"),
    )
    op.create_index("ix_upload_cmd_created", "upload_commands", ["created_at"])

    # ---------------------------------------------------------------- B3：每次调用单独一行
    op.create_table(
        "invocation_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("step_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RESERVED"),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_prompt", sa.Integer(), nullable=True),
        sa.Column("tokens_completion", sa.Integer(), nullable=True),
        sa.Column("usage_known", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(12, 6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="CNY"),
        sa.Column("price_version", sa.String(length=32), nullable=False, server_default="v1"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], name="fk_invocation_attempts_run_id_agent_runs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["run_steps.id"], name="fk_invocation_attempts_step_id_run_steps", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_invocation_attempts"),
    )
    op.create_index("ix_attempts_run", "invocation_attempts", ["run_id", "started_at"])
    op.create_index("ix_attempts_status", "invocation_attempts", ["status"])

    # ---------------------------------------------------------------- B6：并发安全额度桶
    op.create_table(
        "quota_buckets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("bucket_key", sa.String(length=64), nullable=False),
        sa.Column("bucket_day", sa.Date(), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("limit_value", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_quota_buckets"),
        sa.UniqueConstraint("scope", "bucket_key", "bucket_day", name="uq_quota_scope_key_day"),
    )

    # ---------------------------------------------------------------- D1：持久对话消息
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="READY"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("media_ids", sa.JSON(), nullable=True),
        sa.Column("place_ids", sa.JSON(), nullable=True),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["guide_projects.id"], name="fk_messages_project_id_guide_projects", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("project_id", "seq", name="uq_messages_project_seq"),
    )
    op.create_index("ix_messages_project_created", "messages", ["project_id", "created_at"])


def downgrade() -> None:
    # MySQL 不允许删除被外键依赖的索引（errno 1553），因此这里直接删表：
    # DROP TABLE 会连带删除索引与外键。回退顺序为依赖关系的逆序。
    op.drop_table("messages")
    op.drop_table("quota_buckets")
    op.drop_table("invocation_attempts")
    op.drop_table("upload_commands")

    op.drop_column("places", "source")
    op.drop_column("places", "version")

    op.drop_index("ix_media_project_deleted", table_name="media_assets")
    op.drop_column("media_assets", "active_run_id")
    op.drop_column("media_assets", "note")
    op.drop_column("media_assets", "version")
    op.drop_column("media_assets", "deleted_at")

    op.drop_column("usage_ledger", "price_version")
    op.drop_column("usage_ledger", "currency")
    op.alter_column("usage_ledger", "cost", new_column_name="cost_usd", existing_type=sa.Numeric(12, 6), type_=sa.Float(), existing_nullable=True, nullable=False, server_default="0")

    op.drop_column("tool_invocations", "usage_known")
    op.drop_column("tool_invocations", "price_version")
    op.drop_column("tool_invocations", "currency")
    op.alter_column("tool_invocations", "tokens_completion", existing_type=sa.Integer(), nullable=False, server_default="0")
    op.alter_column("tool_invocations", "tokens_prompt", existing_type=sa.Integer(), nullable=False, server_default="0")
    op.alter_column("tool_invocations", "cost", new_column_name="cost_usd", existing_type=sa.Numeric(12, 6), type_=sa.Float(), existing_nullable=True, nullable=False, server_default="0")

    op.alter_column("agent_runs", "used_cost", new_column_name="used_cost_usd", existing_type=sa.Numeric(12, 6), type_=sa.Float(), existing_nullable=False)
    op.alter_column("agent_runs", "budget_max_cost", new_column_name="budget_max_cost_usd", existing_type=sa.Numeric(12, 6), type_=sa.Float(), existing_nullable=False)
    op.alter_column("agent_runs", "media_asset_id", existing_type=sa.String(length=36), existing_nullable=True, nullable=False)
    op.drop_column("agent_runs", "reserved_cost")
    op.drop_column("agent_runs", "reserved_tokens")
    op.drop_column("agent_runs", "lease_epoch")
    op.drop_column("agent_runs", "source_run_id")
    op.drop_column("agent_runs", "input_version")
    op.drop_column("agent_runs", "intent")
