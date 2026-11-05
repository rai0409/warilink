"""step01 deletion journal: 削除ジャーナルの操作(ID と時刻だけ)と、削除済み一覧の操作 ID

Revision ID: 4e1b7a9c2d55
Revises: 3c9d1e7f2a40
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4e1b7a9c2d55'
down_revision: Union[str, Sequence[str], None] = '3c9d1e7f2a40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'deletion_journal_ops',
        sa.Column('op_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finalized_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('op_id'),
    )
    op.create_index('ix_deletion_journal_ops_unfinalized', 'deletion_journal_ops', ['created_at'],
                    postgresql_where=sa.text('finalized_at IS NULL'))
    op.add_column('deletion_tombstones', sa.Column('op_id', postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index(op.f('ix_deletion_tombstones_op_id'), 'deletion_tombstones', ['op_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_deletion_tombstones_op_id'), table_name='deletion_tombstones')
    op.drop_column('deletion_tombstones', 'op_id')
    op.drop_index('ix_deletion_journal_ops_unfinalized', table_name='deletion_journal_ops')
    op.drop_table('deletion_journal_ops')
