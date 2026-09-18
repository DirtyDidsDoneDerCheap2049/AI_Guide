"""B5 / D4: 媒体生命周期、笔记与讲解过期标记

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-09-19

变更性质：增量。guide_cards 增加 stale / stale_reason 两列（地点改动后旧讲解保留但标记过期）；
media_assets 的 note / version / deleted_at / active_run_id 已在 D1-a 迁移中加入，这里只补索引。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "guide_cards",
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("guide_cards", sa.Column("stale_reason", sa.String(length=300), nullable=True))

    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("media_assets")}
    if "ix_media_project_deleted" not in indexes:
        op.create_index("ix_media_project_deleted", "media_assets", ["project_id", "deleted_at"])


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("guide_cards")}
    if "stale_reason" in columns:
        op.drop_column("guide_cards", "stale_reason")
    if "stale" in columns:
        op.drop_column("guide_cards", "stale")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("media_assets")}
    if "ix_media_project_deleted" in indexes:
        op.drop_index("ix_media_project_deleted", table_name="media_assets")
