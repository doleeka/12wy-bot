"""Оплата по месяцам: потоки, цены тарифов, личные цены, чеки; тариф и оплаченные месяцы у участниц;
пауза в чатах и напоминания закончить онбординг

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-03 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('key', name=op.f('pk_app_settings')),
    )
    op.create_table(
        'cohorts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('start', sa.Date(), nullable=False),
        sa.Column('enrolling', sa.Boolean(), server_default=sa.text('0'), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cohorts')),
        sa.UniqueConstraint('number', name=op.f('uq_cohorts_number')),
    )
    op.create_table(
        'cohort_prices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cohort_id', sa.Integer(), nullable=False),
        sa.Column('tariff', sa.String(length=16), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['cohort_id'], ['cohorts.id'], name=op.f('fk_cohort_prices_cohort_id_cohorts'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cohort_prices')),
        sa.UniqueConstraint('cohort_id', 'tariff', 'months', name=op.f('uq_cohort_prices_cohort_id_tariff_months')),
    )
    op.create_index(op.f('ix_cohort_prices_cohort_id'), 'cohort_prices', ['cohort_id'], unique=False)
    op.create_table(
        'price_overrides',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('tariff', sa.String(length=16), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_price_overrides_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_price_overrides')),
        sa.UniqueConstraint('user_id', 'tariff', 'months', name=op.f('uq_price_overrides_user_id_tariff_months')),
    )
    op.create_index(op.f('ix_price_overrides_user_id'), 'price_overrides', ['user_id'], unique=False)
    op.create_table(
        'payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('cohort_id', sa.Integer(), nullable=True),
        sa.Column('tariff', sa.String(length=16), nullable=False),
        sa.Column('months', sa.Integer(), nullable=False),
        sa.Column('month_from', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('file_id', sa.String(length=256), nullable=True),
        sa.Column('file_kind', sa.String(length=16), nullable=True),
        sa.Column('status', sa.Enum('PENDING', 'APPROVED', 'REJECTED', name='paymentstatus', native_enum=False), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decided_by', sa.BigInteger(), nullable=True),
        sa.Column('reminded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['cohort_id'], ['cohorts.id'], name=op.f('fk_payments_cohort_id_cohorts'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_payments_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    )
    op.create_index(op.f('ix_payments_user_id'), 'payments', ['user_id'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cohort_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tariff', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('paid_through', sa.Integer(), server_default=sa.text('0'), nullable=False))
        batch_op.add_column(sa.Column('pay_choice', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('chats_paused', sa.Boolean(), server_default=sa.text('0'), nullable=False))
        batch_op.add_column(sa.Column('onboarding_nudges', sa.Integer(), server_default=sa.text('0'), nullable=False))
        batch_op.add_column(sa.Column('onboarding_nudged_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_users_cohort_id'), ['cohort_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_users_cohort_id_cohorts'), 'cohorts', ['cohort_id'], ['id'], ondelete='SET NULL')
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cohort_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('is_focus', sa.Boolean(), server_default=sa.text('0'), nullable=False))
        batch_op.create_index(batch_op.f('ix_teams_cohort_id'), ['cohort_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_teams_cohort_id_cohorts'), 'cohorts', ['cohort_id'], ['id'], ondelete='SET NULL')

    # 1-й поток (старт 05.10) — все, кто уже в боте, и все тройки. Первый месяц у всех уже пройден.
    # Тариф: без тройки — «Приложение» (в 1-м потоке бесплатно), в тройке — «Базовый».
    op.execute("INSERT INTO cohorts (id, number, start, enrolling) VALUES (1, 1, '2026-10-05', 1)")
    op.execute("UPDATE users SET cohort_id = 1, paid_through = 1, "
               "tariff = CASE WHEN no_team THEN 'app' ELSE 'basic' END")
    op.execute("UPDATE teams SET cohort_id = 1")
    # Цены 1-го потока за месяц, ₸: приложение — бесплатно, базовый — 1990, фокус-группа — 9990
    op.execute("INSERT INTO cohort_prices (cohort_id, tariff, months, amount) VALUES "
               "(1, 'app', 1, 0), (1, 'basic', 1, 1990), (1, 'focus', 1, 9990)")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('teams', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_teams_cohort_id_cohorts'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_teams_cohort_id'))
        batch_op.drop_column('is_focus')
        batch_op.drop_column('cohort_id')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_users_cohort_id_cohorts'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_users_cohort_id'))
        batch_op.drop_column('onboarding_nudged_at')
        batch_op.drop_column('onboarding_nudges')
        batch_op.drop_column('chats_paused')
        batch_op.drop_column('pay_choice')
        batch_op.drop_column('paid_through')
        batch_op.drop_column('tariff')
        batch_op.drop_column('cohort_id')
    for table in ('payments', 'price_overrides', 'cohort_prices', 'cohorts', 'app_settings'):
        op.drop_table(table)
