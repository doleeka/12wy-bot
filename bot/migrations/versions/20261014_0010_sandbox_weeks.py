"""Persist sandbox clock and independent weekly completion."""
from alembic import op
import sqlalchemy as sa
revision = "0010"
down_revision = "0009"
branch_labels = depends_on = None

def upgrade():
    op.add_column("users", sa.Column("sandbox_week", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("sandbox_checked_weeks", sa.JSON(), nullable=True))

def downgrade():
    raise RuntimeError("Restore the pre-migration backup to retain test progress")
