"""users.no_team — organiser outside of teams

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-05 16:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ADD COLUMN с значением по умолчанию — таблица users не пересоздаётся, у всех no_team = 0
    op.add_column('users', sa.Column('no_team', sa.Boolean(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('no_team')
