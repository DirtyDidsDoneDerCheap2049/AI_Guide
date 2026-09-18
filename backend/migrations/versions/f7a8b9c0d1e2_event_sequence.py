"""D1-g / B7: 项目内事件序号（提交顺序一致的事件游标）

Revision ID: f7a8b9c0d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-09-19

变更性质：增量 + 回填。
- 新增 project_event_counters：每个项目一行序号分配器（事务内行锁分配）。
- workspace_events 增加 seq 列，按 (project_id, id) 顺序回填历史事件，
  再建 (project_id, seq) 唯一索引，保证序号在项目内唯一。

为什么必须回填：SSE 的 Last-Event-ID 换成序号后，旧客户端/旧快照里的 id 游标会失效；
回填让历史事件也有连续序号，(project_id, seq) 可用于补发与对账。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_events", sa.Column("seq", sa.BigInteger(), nullable=True))

    bind = op.get_bind()
    # 回填：按全局 id 顺序给每个项目编号（MySQL 8 窗口函数；8.0 是部署下限）。
    bind.execute(
        sa.text(
            """
            UPDATE workspace_events AS e
            JOIN (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY id) AS rn
                FROM workspace_events
            ) AS ranked ON ranked.id = e.id
            SET e.seq = ranked.rn
            """
        )
    )

    op.alter_column(
        "workspace_events",
        "seq",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_unique_constraint("uq_events_project_seq", "workspace_events", ["project_id", "seq"])

    if not sa.inspect(bind).has_table("project_event_counters"):
        op.create_table(
            "project_event_counters",
            sa.Column(
                "project_id",
                sa.String(length=36),
                sa.ForeignKey("guide_projects.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    # 播种分配器：已有项目的 last_seq = 当前最大序号。
    bind.execute(
        sa.text(
            """
            INSERT INTO project_event_counters (project_id, last_seq, updated_at)
            SELECT p.id, COALESCE(MAX(e.seq), 0), NOW()
            FROM guide_projects AS p
            LEFT JOIN workspace_events AS e ON e.project_id = p.id
            GROUP BY p.id
            ON DUPLICATE KEY UPDATE last_seq = VALUES(last_seq)
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("project_event_counters"):
        op.drop_table("project_event_counters")
    columns = {item["name"] for item in sa.inspect(bind).get_columns("workspace_events")}
    if "seq" in columns:
        constraints = {item["name"] for item in sa.inspect(bind).get_unique_constraints("workspace_events")}
        if "uq_events_project_seq" in constraints:
            op.drop_constraint("uq_events_project_seq", "workspace_events", type_="unique")
        op.drop_column("workspace_events", "seq")
