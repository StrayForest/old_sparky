"""Add bounded retry state for tournament automation.

Revision ID: 20260720_0033
Revises: 20260720_0032
Create Date: 2026-07-20 20:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260720_0033"
down_revision = "20260720_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column(
            "automation_failure_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_retry_after", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.create_index(
        "ix_tournaments_automation_retry_after",
        "tournaments",
        ["automation_retry_after"],
        schema="platform",
        postgresql_where=sa.text("automation_assignment_generated_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournaments_automation_retry_after",
        table_name="tournaments",
        schema="platform",
    )
    op.drop_column("tournaments", "automation_retry_after", schema="platform")
    op.drop_column("tournaments", "automation_failure_count", schema="platform")
