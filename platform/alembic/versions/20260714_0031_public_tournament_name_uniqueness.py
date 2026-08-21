"""Enforce normalized public tournament name uniqueness.

Revision ID: 20260714_0031
Revises: 20260704_0030
Create Date: 2026-07-14 21:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260714_0031"
down_revision = "20260704_0030"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_tournaments_public_name_normalized"


def upgrade() -> None:
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
