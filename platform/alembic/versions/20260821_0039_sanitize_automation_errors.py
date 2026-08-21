"""Sanitize persisted tournament automation error text.

Revision ID: 20260821_0039
Revises: 20260813_0038
Create Date: 2026-08-21 23:59:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260821_0039"
down_revision = "20260813_0038"
branch_labels = None
depends_on = None

PUBLIC_AUTOMATION_FAILURE_MESSAGE = "Tournament automation failed. A retry is scheduled."


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE platform.tournaments
            SET automation_last_error = :message
            WHERE automation_last_error IS NOT NULL
              AND automation_last_error <> :message
            """
        ),
        {"message": PUBLIC_AUTOMATION_FAILURE_MESSAGE},
    )


def downgrade() -> None:
    # Sanitized exception text is intentionally irreversible.
    pass
