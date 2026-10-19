"""users.excluded_at — removed from the community (reversible)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ADD COLUMN, допускает NULL — таблица users не пересоздаётся, у всех пусто (не исключены)
    op.add_column('users', sa.Column('excluded_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('excluded_at')
