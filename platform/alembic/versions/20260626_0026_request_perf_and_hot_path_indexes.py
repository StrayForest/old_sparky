"""Add request performance hot path indexes.

Revision ID: 20260626_0026
Revises: 20260625_0025
Create Date: 2026-06-26 09:30:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260626_0026"
down_revision = "20260625_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournament_participants_active_tournament "
            "ON platform.tournament_participants (tournament_id) "
            "WHERE status NOT IN ('withdrawn', 'disqualified')"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournament_deadlock_ready_rounds_tournament_status_latest "
            "ON platform.tournament_deadlock_ready_rounds "
            "(tournament_id, status, created_at DESC, id DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "platform.ix_tournament_deadlock_ready_rounds_tournament_status_latest"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "platform.ix_tournament_participants_active_tournament"
        )
