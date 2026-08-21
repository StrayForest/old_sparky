"""Add trigram indexes for participant nickname search.

Revision ID: 20260627_0027
Revises: 20260626_0026
Create Date: 2026-06-27 09:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260627_0027"
down_revision = "20260626_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_player_profiles_display_name_lower_trgm "
            "ON platform.player_profiles USING gin (lower(display_name) gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_player_profiles_handle_lower_trgm "
            "ON platform.player_profiles USING gin (lower(handle) gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_users_display_name_lower_trgm "
            "ON platform.users USING gin (lower(display_name) gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "platform.ix_users_display_name_lower_trgm"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "platform.ix_player_profiles_handle_lower_trgm"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "platform.ix_player_profiles_display_name_lower_trgm"
        )
