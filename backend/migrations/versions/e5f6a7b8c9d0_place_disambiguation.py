"""D1-e / B4: 地点消歧——用户确认身份与供应商匹配分离

Revision ID: e5f6a7b8c9d0
Revises: d1a2b3c4e5f6
Create Date: 2026-09-19

变更性质：增量。新增 place_match_candidates 表保存供应商候选，places 增加
match_status / match_note 两列说明匹配结论；不修改既有列，旧数据仍可读。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "places",
        sa.Column("match_status", sa.String(length=24), nullable=False, server_default="pending"),
    )
    op.add_column("places", sa.Column("match_note", sa.String(length=300), nullable=True))

    if not _has_table("place_match_candidates"):
        op.create_table(
            "place_match_candidates",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "media_asset_id",
                sa.String(length=36),
                sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "run_id",
                sa.String(length=36),
                sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("address", sa.String(length=400), nullable=True),
            sa.Column("region", sa.String(length=200), nullable=True),
            sa.Column("latitude", sa.Float(), nullable=True),
            sa.Column("longitude", sa.Float(), nullable=True),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_place_id", sa.String(length=128), nullable=True),
            sa.Column("match_kind", sa.String(length=16), nullable=False, server_default="other"),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="CANDIDATE"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("run_id", "rank", name="uq_match_candidates_run_rank"),
        )
        op.create_index(
            "ix_match_candidates_media",
            "place_match_candidates",
            ["media_asset_id", "created_at"],
        )


def downgrade() -> None:
    # 只删除本轮新增结构；已有地点行不做数据删除。
    if _has_table("place_match_candidates"):
        op.drop_table("place_match_candidates")
    _drop_column_if_exists("places", "match_note")
    _drop_column_if_exists("places", "match_status")


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def _drop_column_if_exists(table: str, column: str) -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns(table)}
    if column in columns:
        op.drop_column(table, column)
