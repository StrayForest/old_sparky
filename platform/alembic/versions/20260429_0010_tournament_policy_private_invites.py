"""Add private tournament policy fields and invite access grants."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260429_0010"
down_revision = "20260422_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "can_create_public_tournaments",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column(
            "allowed_ranks",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("max_participants", sa.Integer(), nullable=True),
        schema="platform",
    )

    op.create_table(
        "deadlock_dream_slots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("slot_number", sa.Integer(), nullable=False),
        sa.Column("allowed_roles", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("desired_heroes", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "slot_number", name="uq_deadlock_dream_slots_user_slot"),
        schema="platform",
    )
    op.create_index(
        "ix_deadlock_dream_slots_user_id",
        "deadlock_dream_slots",
        ["user_id"],
        unique=False,
        schema="platform",
    )

    op.create_table(
        "tournament_invite_accesses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("invite_id", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["invite_id"], ["platform.tournament_invites.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tournament_id",
            "user_id",
            name="uq_tournament_invite_accesses_tournament_user",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invite_accesses_tournament_id",
        "tournament_invite_accesses",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invite_accesses_user_id",
        "tournament_invite_accesses",
        ["user_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invite_accesses_invite_id",
        "tournament_invite_accesses",
        ["invite_id"],
        unique=False,
        schema="platform",
    )

    # Operator privileges are provisioned explicitly with
    # tools/platform_create_operator.py. Migrations must never grant roles
    # based on a reusable identity such as an email address.


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_invite_accesses_invite_id",
        table_name="tournament_invite_accesses",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_invite_accesses_user_id",
        table_name="tournament_invite_accesses",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_invite_accesses_tournament_id",
        table_name="tournament_invite_accesses",
        schema="platform",
    )
    op.drop_table("tournament_invite_accesses", schema="platform")
    op.drop_index("ix_deadlock_dream_slots_user_id", table_name="deadlock_dream_slots", schema="platform")
    op.drop_table("deadlock_dream_slots", schema="platform")
    op.drop_column("tournaments", "max_participants", schema="platform")
    op.drop_column("tournaments", "allowed_ranks", schema="platform")
    op.drop_column("users", "can_create_public_tournaments", schema="platform")
