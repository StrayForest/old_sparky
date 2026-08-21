"""Add tournament Deadlock automation schedule."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260507_0017"
down_revision = "20260506_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column("ready_check_starts_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("ready_check_ends_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("captain_selection_starts_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("captain_response_deadline_minutes", sa.Integer(), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("teams_count", sa.Integer(), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_ready_check_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_ready_check_closed_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_captain_round_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_captain_round_finalized_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_assignment_generated_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournaments",
        sa.Column("automation_last_error", sa.Text(), nullable=True),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_column("tournaments", "automation_last_error", schema="platform")
    op.drop_column("tournaments", "automation_assignment_generated_at", schema="platform")
    op.drop_column("tournaments", "automation_captain_round_finalized_at", schema="platform")
    op.drop_column("tournaments", "automation_captain_round_started_at", schema="platform")
    op.drop_column("tournaments", "automation_ready_check_closed_at", schema="platform")
    op.drop_column("tournaments", "automation_ready_check_started_at", schema="platform")
    op.drop_column("tournaments", "teams_count", schema="platform")
    op.drop_column("tournaments", "captain_response_deadline_minutes", schema="platform")
    op.drop_column("tournaments", "captain_selection_starts_at", schema="platform")
    op.drop_column("tournaments", "ready_check_ends_at", schema="platform")
    op.drop_column("tournaments", "ready_check_starts_at", schema="platform")
