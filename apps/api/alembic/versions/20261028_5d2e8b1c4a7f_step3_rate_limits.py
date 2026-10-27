"""step3 rate limits: 濫用対策の回数制限の記録(IP・ID・token は鍵つきハッシュだけ。一時データ)

Revision ID: 5d2e8b1c4a7f
Revises: 69e7bf0883a7
Create Date: 2026-10-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d2e8b1c4a7f'
down_revision: Union[str, Sequence[str], None] = '69e7bf0883a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'rate_limit_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('bucket', sa.String(length=48), nullable=False),
        sa.Column('key_hash', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rate_limit_lookup', 'rate_limit_events', ['bucket', 'key_hash', 'created_at'], unique=False)
    op.create_index('ix_rate_limit_created', 'rate_limit_events', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_rate_limit_created', table_name='rate_limit_events')
    op.drop_index('ix_rate_limit_lookup', table_name='rate_limit_events')
    op.drop_table('rate_limit_events')
