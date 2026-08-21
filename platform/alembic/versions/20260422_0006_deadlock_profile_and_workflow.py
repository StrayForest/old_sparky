"""Add deadlock profile and first web-native workflow tables."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260422_0006"
down_revision = "20260422_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deadlock_profiles",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("rank", sa.String(length=32), nullable=False),
        sa.Column("subrank", sa.Integer(), nullable=False),
        sa.Column("playtime", sa.String(length=20), nullable=False),
        sa.Column("roles", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("pool", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("captain_priority", sa.String(length=20), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name="pk_deadlock_profiles"),
        schema="platform",
    )

    op.create_table(
        "tournament_deadlock_ready_rounds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("eligible_user_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("initiated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["initiated_by_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_ready_rounds_tournament_id",
        "tournament_deadlock_ready_rounds",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_ready_rounds_initiated_by_user_id",
        "tournament_deadlock_ready_rounds",
        ["initiated_by_user_id"],
        unique=False,
        schema="platform",
    )

    op.create_table(
        "tournament_deadlock_ready_votes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("choice", sa.String(length=10), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["round_id"],
            ["platform.tournament_deadlock_ready_rounds.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_deadlock_ready_votes"),
        sa.UniqueConstraint(
            "round_id",
            "user_id",
            name="uq_tournament_deadlock_ready_votes_round_user",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_ready_votes_round_id",
        "tournament_deadlock_ready_votes",
        ["round_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_ready_votes_user_id",
        "tournament_deadlock_ready_votes",
        ["user_id"],
        unique=False,
        schema="platform",
    )

    op.create_table(
        "tournament_deadlock_dream_slots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("allowed_roles", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("desired_heroes", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tournament_deadlock_dream_slots"),
        sa.UniqueConstraint(
            "tournament_id",
            "user_id",
            "slot_number",
            name="uq_tournament_deadlock_dream_slots_user_slot",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_dream_slots_tournament_id",
        "tournament_deadlock_dream_slots",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_deadlock_dream_slots_user_id",
        "tournament_deadlock_dream_slots",
        ["user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_deadlock_dream_slots_user_id",
        table_name="tournament_deadlock_dream_slots",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_dream_slots_tournament_id",
        table_name="tournament_deadlock_dream_slots",
        schema="platform",
    )
    op.drop_table("tournament_deadlock_dream_slots", schema="platform")
    op.drop_index(
        "ix_tournament_deadlock_ready_votes_user_id",
        table_name="tournament_deadlock_ready_votes",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_ready_votes_round_id",
        table_name="tournament_deadlock_ready_votes",
        schema="platform",
    )
    op.drop_table("tournament_deadlock_ready_votes", schema="platform")
    op.drop_index(
        "ix_tournament_deadlock_ready_rounds_initiated_by_user_id",
        table_name="tournament_deadlock_ready_rounds",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_deadlock_ready_rounds_tournament_id",
        table_name="tournament_deadlock_ready_rounds",
        schema="platform",
    )
    op.drop_table("tournament_deadlock_ready_rounds", schema="platform")
    op.drop_table("deadlock_profiles", schema="platform")
