"""vision 3+ years and week-12 reflection

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27 13:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('visions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('work', sa.Text(), nullable=False),
    sa.Column('life', sa.Text(), nullable=False),
    sa.Column('me', sa.Text(), nullable=False),
    sa.Column('main', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_visions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_visions')),
    sa.UniqueConstraint('user_id', name=op.f('uq_visions_user_id'))
    )
    op.create_table('vision_reflections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('cycle', sa.Integer(), nullable=False),
    sa.Column('closer', sa.Text(), nullable=False),
    sa.Column('changed', sa.Text(), nullable=False),
    sa.Column('next', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_vision_reflections_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_vision_reflections')),
    sa.UniqueConstraint('user_id', 'cycle', name=op.f('uq_vision_reflections_user_id_cycle'))
    )
    with op.batch_alter_table('vision_reflections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_vision_reflections_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('vision_reflections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_vision_reflections_user_id'))
    op.drop_table('vision_reflections')
    op.drop_table('visions')
