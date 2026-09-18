"""D3-b: 收藏地点与路线草稿/修订

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-09-19

变更性质：纯新增三张表（saved_places / route_drafts / route_revisions），不改动既有列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("saved_places"):
        op.create_table(
            "saved_places",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "project_id", sa.String(length=36), sa.ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("place_id", sa.String(length=36), sa.ForeignKey("places.id", ondelete="SET NULL"), nullable=True),
            sa.Column(
                "media_asset_id",
                sa.String(length=36),
                sa.ForeignKey("media_assets.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("dedupe_key", sa.String(length=160), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("address", sa.String(length=400), nullable=True),
            sa.Column("region", sa.String(length=200), nullable=True),
            sa.Column("latitude", sa.Float(), nullable=True),
            sa.Column("longitude", sa.Float(), nullable=True),
            sa.Column("provider", sa.String(length=32), nullable=False, server_default="user"),
            sa.Column("provider_place_id", sa.String(length=128), nullable=True),
            sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"),
            sa.Column("note", sa.String(length=500), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("project_id", "dedupe_key", name="uq_saved_places_project_dedupe"),
        )
        op.create_index("ix_saved_places_project_created", "saved_places", ["project_id", "created_at"])

    if not inspector.has_table("route_drafts"):
        op.create_table(
            "route_drafts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "project_id", sa.String(length=36), sa.ForeignKey("guide_projects.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False, server_default="walking"),
            sa.Column("stops", sa.JSON(), nullable=False),
            sa.Column("input_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("current_revision_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("project_id", "name", name="uq_route_drafts_project_name"),
        )

    if not inspector.has_table("route_revisions"):
        op.create_table(
            "route_revisions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("seq", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("draft_id", sa.String(length=36), sa.ForeignKey("route_drafts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("input_version", sa.Integer(), nullable=False),
            sa.Column("mode", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_route_id", sa.String(length=128), nullable=True),
            sa.Column("distance_meters", sa.Integer(), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("legs", sa.JSON(), nullable=True),
            sa.Column("geometry", sa.Text(), nullable=True),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.String(length=300), nullable=True),
            sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("computed_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_route_revisions_draft_created", "route_revisions", ["draft_id", "created_at"])
        op.create_unique_constraint("uq_route_revisions_draft_seq", "route_revisions", ["draft_id", "seq"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("route_revisions", "route_drafts", "saved_places"):
        if inspector.has_table(table):
            op.drop_table(table)
