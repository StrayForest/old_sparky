"""Add persisted deadlock captain rounds and entries."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260422_0007"
down_revision = "20260422_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tournament_deadlock_captain_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("source_ready_round_id", sa.Integer(), nullable=False),
        sa.Column("teams_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("initiated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["source_ready_round_id"],
            ["platform.tournament_deadlock_ready_rounds.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_captain_rounds_tournament_id",
        "tournament_deadlock_captain_rounds",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_captain_rounds_source_ready_round_id",
        "tournament_deadlock_captain_rounds",
        ["source_ready_round_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_captain_rounds_initiated_by_user_id",
        "tournament_deadlock_captain_rounds",
        ["initiated_by_user_id"],
        unique=False,
        schema="platform",
    )

    op.create_table(
        "tournament_deadlock_captain_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("offer_order", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("assigned_team_id", sa.String(length=20), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["platform.tournament_deadlock_captain_rounds.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_deadlock_captain_entries"),
        sa.UniqueConstraint(
            "round_id",
            "user_id",
            name="uq_tournament_deadlock_captain_entries_round_user",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_captain_entries_round_id",
        "tournament_deadlock_captain_entries",
        ["round_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_captain_entries_user_id",
        "tournament_deadlock_captain_entries",
        ["user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_deadlock_captain_entries_user_id",
        table_name="tournament_deadlock_captain_entries",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_captain_entries_round_id",
        table_name="tournament_deadlock_captain_entries",
        schema="platform",
    )
    op.drop_table("tournament_deadlock_captain_entries", schema="platform")
    op.drop_index(
        "ix_tournament_deadlock_captain_rounds_initiated_by_user_id",
        table_name="tournament_deadlock_captain_rounds",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_captain_rounds_source_ready_round_id",
        table_name="tournament_deadlock_captain_rounds",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_captain_rounds_tournament_id",
        table_name="tournament_deadlock_captain_rounds",
        schema="platform",
    )
    op.drop_table("tournament_deadlock_captain_rounds", schema="platform")
