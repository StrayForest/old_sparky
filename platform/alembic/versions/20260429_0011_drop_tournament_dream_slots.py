"""Drop obsolete tournament-scoped Deadlock dream slots."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260429_0011"
down_revision = "20260429_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM platform.tournaments
        WHERE format_slug <> 'solo_balanced_deadlock'
        """
    )
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


def downgrade() -> None:
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
