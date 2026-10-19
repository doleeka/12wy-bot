"""Preserve weekly plans and an append-only change journal."""
from alembic import op
import sqlalchemy as sa
revision = "0009"
down_revision = "0008"
branch_labels = depends_on = None

def upgrade():
    op.create_table("plan_week_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("week_number", sa.Integer(), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.UniqueConstraint("user_id", "cycle", "week_number"))
    op.create_index("ix_plan_week_snapshots_user_id", "plan_week_snapshots", ["user_id"])
    op.create_table("plan_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("effective_week", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("plan_before", sa.JSON(), nullable=False),
        sa.Column("marks_before", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_plan_changes_user_id", "plan_changes", ["user_id"])

def downgrade():
    raise RuntimeError("Plan history must not be dropped; restore the pre-migration backup instead")
