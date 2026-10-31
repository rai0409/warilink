"""prb maintenance runs: 定期ジョブ(保持期限の削除)の最後の成功・失敗の時刻(個人情報なし)

Revision ID: 3c9d1e7f2a40
Revises: 8a3f6c2d9e14
Create Date: 2026-10-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c9d1e7f2a40'
down_revision: Union[str, Sequence[str], None] = '8a3f6c2d9e14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'maintenance_runs',
        sa.Column('job', sa.String(length=32), nullable=False),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_failure_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('job'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('maintenance_runs')
