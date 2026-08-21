"""Add uniqueness guard for tournament participants."""

from __future__ import annotations

from alembic import op


revision = "20260421_0002"
down_revision = "20260421_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_tournament_participants_tournament_user",
        "tournament_participants",
        ["tournament_id", "user_id"],
        schema="platform",
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_tournament_participants_tournament_user",
        "tournament_participants",
        schema="platform",
        type_="unique",
    )
