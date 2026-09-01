"""Optimize tournament catalog reads for keyset pagination and search."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260901_0050"
down_revision = "20260831_0049"
branch_labels = None
depends_on = None

INDEX_NAMES = (
    "ix_tournaments_public_solo_created_at_id",
    "ix_tournaments_public_solo_status_created_at_id",
    "ix_tournaments_public_solo_starts_nearest",
    "ix_tournaments_public_solo_starts_farthest",
    "ix_tournaments_organizer_created_at_id",
    "ix_tournament_participants_user_active_tournament",
    "ix_tournaments_allowed_ranks_gin",
    "ix_tournaments_name_lower_trgm",
)


def _drop_invalid_indexes() -> None:
    bind = op.get_bind()
    for index_name in INDEX_NAMES:
        is_valid = bind.execute(
            sa.text(
                """
                SELECT pg_index.indisvalid
                FROM pg_class
                JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
                JOIN pg_index ON pg_index.indexrelid = pg_class.oid
                WHERE pg_namespace.nspname = 'platform'
                  AND pg_class.relname = :index_name
                """
            ),
            {"index_name": index_name},
        ).scalar()
        if is_valid is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS platform.{index_name}")


def upgrade() -> None:
    op.alter_column(
        "tournaments",
        "allowed_ranks",
        existing_type=sa.JSON(),
        type_=postgresql.JSONB(),
        postgresql_using="allowed_ranks::jsonb",
        schema="platform",
    )

    with op.get_context().autocommit_block():
        _drop_invalid_indexes()
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_public_solo_created_at_id "
            "ON platform.tournaments (created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_public_solo_status_created_at_id "
            "ON platform.tournaments (status, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_public_solo_starts_nearest "
            "ON platform.tournaments "
            "(starts_at ASC NULLS LAST, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_public_solo_starts_farthest "
            "ON platform.tournaments "
            "(starts_at DESC NULLS LAST, created_at DESC, id DESC) "
            "WHERE visibility = 'public' AND format_slug = 'solo'"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_organizer_created_at_id "
            "ON platform.tournaments (organizer_user_id, created_at DESC, id DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournament_participants_user_active_tournament "
            "ON platform.tournament_participants (user_id, tournament_id) "
            "WHERE status NOT IN ('withdrawn', 'disqualified')"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_allowed_ranks_gin "
            "ON platform.tournaments USING gin (allowed_ranks)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_tournaments_name_lower_trgm "
            "ON platform.tournaments USING gin (lower(name) gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name in INDEX_NAMES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS platform.{index_name}")

    op.alter_column(
        "tournaments",
        "allowed_ranks",
        existing_type=postgresql.JSONB(),
        type_=sa.JSON(),
        postgresql_using="allowed_ranks::json",
        schema="platform",
    )
