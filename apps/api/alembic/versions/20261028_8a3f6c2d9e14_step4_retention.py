"""step4 retention: グループの参加者不在時刻と、復元後に削除を再適用するための削除済み一覧(ID のみ)

Revision ID: 8a3f6c2d9e14
Revises: 5d2e8b1c4a7f
Create Date: 2026-10-28 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '8a3f6c2d9e14'
down_revision: Union[str, Sequence[str], None] = '5d2e8b1c4a7f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('groups', sa.Column('orphaned_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'deletion_tombstones',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('target_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind IN ('identity','destination','group')", name='ck_tombstone_kind'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kind', 'target_id', name='uq_tombstone_target'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('deletion_tombstones')
    op.drop_column('groups', 'orphaned_at')
