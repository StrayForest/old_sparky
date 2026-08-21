"""Add ready vote scale indexes.

Revision ID: 20260625_0025
Revises: 20260625_0024
Create Date: 2026-06-25 16:29:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260625_0025"
down_revision = "20260625_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_tournament_deadlock_ready_votes_round_choice",
        "tournament_deadlock_ready_votes",
        ["round_id", "choice"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_deadlock_ready_votes_round_choice",
        table_name="tournament_deadlock_ready_votes",
        schema="platform",
    )
