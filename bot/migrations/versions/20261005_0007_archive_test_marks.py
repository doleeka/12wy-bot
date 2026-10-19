"""archive for test-mode marks and users.test_mode_since

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('archived_checkins',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('original_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tactic_id', sa.Integer(), nullable=False),
    sa.Column('week_start', sa.Date(), nullable=False),
    sa.Column('week_number', sa.Integer(), nullable=False),
    sa.Column('done', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('cycle_start_was', sa.Date(), nullable=True),
    sa.Column('reason', sa.String(length=32), nullable=False),
    sa.Column('archived_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_archived_checkins_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_archived_checkins'))
    )
    with op.batch_alter_table('archived_checkins', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_archived_checkins_user_id'), ['user_id'], unique=False)

    op.create_table('archived_daily_marks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('original_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tactic_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('reason', sa.String(length=32), nullable=False),
    sa.Column('archived_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_archived_daily_marks_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_archived_daily_marks'))
    )
    with op.batch_alter_table('archived_daily_marks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_archived_daily_marks_user_id'), ['user_id'], unique=False)

    # Простое ALTER TABLE ADD COLUMN (столбец допускает NULL) — таблица users не пересоздаётся
    op.add_column('users', sa.Column('test_mode_since', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('test_mode_since')
    with op.batch_alter_table('archived_daily_marks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_archived_daily_marks_user_id'))
    op.drop_table('archived_daily_marks')
    with op.batch_alter_table('archived_checkins', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_archived_checkins_user_id'))
    op.drop_table('archived_checkins')
