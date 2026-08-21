"""Add publish/lock metadata for deadlock assignment runs."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260422_0009"
down_revision = "20260422_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournament_deadlock_assignment_runs",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournament_deadlock_assignment_runs",
        sa.Column("published_by_user_id", sa.String(length=36), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournament_deadlock_assignment_runs",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournament_deadlock_assignment_runs",
        sa.Column("locked_by_user_id", sa.String(length=36), nullable=True),
        schema="platform",
    )
    op.create_foreign_key(
        "fk_td_assignment_runs_published_by_user",
        "tournament_deadlock_assignment_runs",
        "users",
        ["published_by_user_id"],
        ["id"],
        source_schema="platform",
        referent_schema="platform",
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_td_assignment_runs_locked_by_user",
        "tournament_deadlock_assignment_runs",
        "users",
        ["locked_by_user_id"],
        ["id"],
        source_schema="platform",
        referent_schema="platform",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tournament_deadlock_assignment_runs_published_by_user_id",
        "tournament_deadlock_assignment_runs",
        ["published_by_user_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_assignment_runs_locked_by_user_id",
        "tournament_deadlock_assignment_runs",
        ["locked_by_user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_deadlock_assignment_runs_locked_by_user_id",
        table_name="tournament_deadlock_assignment_runs",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_assignment_runs_published_by_user_id",
        table_name="tournament_deadlock_assignment_runs",
        schema="platform",
    )
    op.drop_constraint(
        "fk_td_assignment_runs_locked_by_user",
        "tournament_deadlock_assignment_runs",
        schema="platform",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_td_assignment_runs_published_by_user",
        "tournament_deadlock_assignment_runs",
        schema="platform",
        type_="foreignkey",
    )
    op.drop_column("tournament_deadlock_assignment_runs", "locked_by_user_id", schema="platform")
    op.drop_column("tournament_deadlock_assignment_runs", "locked_at", schema="platform")
    op.drop_column("tournament_deadlock_assignment_runs", "published_by_user_id", schema="platform")
    op.drop_column("tournament_deadlock_assignment_runs", "published_at", schema="platform")
