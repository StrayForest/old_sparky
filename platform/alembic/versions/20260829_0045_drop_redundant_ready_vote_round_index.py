"""Drop the redundant standalone ready-vote round index.

Revision ID: 20260829_0045
Revises: 20260829_0044
Create Date: 2026-08-29
"""

from __future__ import annotations

from alembic import op


revision = "20260829_0045"
down_revision = "20260829_0044"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_tournament_deadlock_ready_votes_round_id"


def upgrade() -> None:
    # The unique (round_id, user_id) index already serves every query whose
    # leading predicate is round_id. Removing the duplicate index reduces
    # index maintenance for each hot-path vote insert/update.
    op.drop_index(
        INDEX_NAME,
        table_name="tournament_deadlock_ready_votes",
        schema="platform",
        if_exists=True,
    )


def downgrade() -> None:
    op.create_index(
        INDEX_NAME,
        "tournament_deadlock_ready_votes",
        ["round_id"],
        schema="platform",
        if_not_exists=True,
    )
