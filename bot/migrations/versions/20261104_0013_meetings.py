"""Встречи сообщества: анонс и напоминания в общем чате

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-04 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # новая таблица — существующие данные не трогаем
    op.create_table(
        'meetings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('details', sa.Text(), nullable=False),
        sa.Column('starts_at', sa.DateTime(), nullable=False),
        sa.Column('weekly', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=True),
        sa.Column('day_reminded_for', sa.DateTime(), nullable=True),
        sa.Column('hour_reminded_for', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_meetings')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('meetings')
