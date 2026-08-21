"""Add persisted deadlock auto-assignment runs."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260422_0008"
down_revision = "20260422_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tournament_deadlock_assignment_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("source_captain_round_id", sa.Integer(), nullable=False),
        sa.Column("source_ready_round_id", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="generated"),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("result_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("candidate_pool_user_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("leftover_user_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_captain_round_id"],
            ["platform.tournament_deadlock_captain_rounds.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_ready_round_id"],
            ["platform.tournament_deadlock_ready_rounds.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_deadlock_assignment_runs"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_assignment_runs_tournament_id",
        "tournament_deadlock_assignment_runs",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_assignment_runs_source_captain_round_id",
        "tournament_deadlock_assignment_runs",
        ["source_captain_round_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_assignment_runs_source_ready_round_id",
        "tournament_deadlock_assignment_runs",
        ["source_ready_round_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_assignment_runs_created_by_user_id",
        "tournament_deadlock_assignment_runs",
        ["created_by_user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_deadlock_assignment_runs_created_by_user_id",
        table_name="tournament_deadlock_assignment_runs",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_assignment_runs_source_ready_round_id",
        table_name="tournament_deadlock_assignment_runs",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_assignment_runs_source_captain_round_id",
        table_name="tournament_deadlock_assignment_runs",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_assignment_runs_tournament_id",
        table_name="tournament_deadlock_assignment_runs",
        schema="platform",
    )
    op.drop_table("tournament_deadlock_assignment_runs", schema="platform")
