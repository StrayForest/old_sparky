"""Remove obsolete player profile visibility.

Revision ID: 20260729_0034
Revises: 20260720_0033
Create Date: 2026-07-29 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260729_0034"
down_revision = "20260720_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("player_profiles", "is_public", schema="platform")


def downgrade() -> None:
    op.add_column(
        "player_profiles",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        schema="platform",
    )
