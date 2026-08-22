"""Enforce normalized public tournament name uniqueness.

Revision ID: 20260714_0031
Revises: 20260704_0030
Create Date: 2026-07-14 21:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260714_0031"
down_revision = "20260704_0030"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_tournaments_public_name_normalized"


def _assert_no_normalized_public_name_duplicates() -> None:
    duplicate_count = int(
        op.get_bind().scalar(
            sa.text(
                """
                SELECT count(*) FROM (
                    SELECT lower(btrim(name))
                    FROM platform.tournaments
                    WHERE visibility = 'public'
                    GROUP BY lower(btrim(name))
                    HAVING count(*) > 1
                ) AS duplicates
                """
            )
        )
        or 0
    )
    if duplicate_count:
        raise RuntimeError(
            "Cannot apply 20260714_0031: normalized public tournament-name "
            f"duplicates exist in {duplicate_count} group(s). Repair them before retrying."
        )


def _drop_invalid_index_if_present() -> None:
    is_invalid = op.get_bind().scalar(
        sa.text(
            """
            SELECT NOT pg_index.indisvalid
            FROM pg_class
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            JOIN pg_index ON pg_index.indexrelid = pg_class.oid
            WHERE pg_namespace.nspname = 'platform'
              AND pg_class.relname = :index_name
            """
        ),
        {"index_name": INDEX_NAME},
    )
    if is_invalid:
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY platform.{INDEX_NAME}")


def upgrade() -> None:
    _assert_no_normalized_public_name_duplicates()
    _drop_invalid_index_if_present()
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            f"{INDEX_NAME} ON platform.tournaments (lower(btrim(name))) "
            "WHERE visibility = 'public'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            f"platform.{INDEX_NAME}"
        )
