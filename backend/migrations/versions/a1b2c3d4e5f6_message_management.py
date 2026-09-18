"""Message revisions, soft deletion and outdated answers; preserves existing rows."""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('messages', sa.Column('version', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('messages', sa.Column('edited_at', sa.DateTime(), nullable=True))
    op.add_column('messages', sa.Column('deleted_at', sa.DateTime(), nullable=True))
    op.add_column('messages', sa.Column('stale', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    for name in ('stale', 'deleted_at', 'edited_at', 'version'):
        op.drop_column('messages', name)
