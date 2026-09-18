"""Persist actionable guide replies without changing existing messages.

Revision ID: d2e3f4a5b6c7
Revises: c0d1e2f3a4b5
"""
from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("messages", sa.Column("attachments", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("messages", "attachments")
