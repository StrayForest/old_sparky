"""Add tournament matches and organizer state control schema."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260422_0003"
down_revision = "20260421_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tournament_matches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sequence_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("home_label", sa.String(length=120), nullable=False),
        sa.Column("away_label", sa.String(length=120), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="scheduled"),
        sa.Column("home_score", sa.Integer(), nullable=True),
        sa.Column("away_score", sa.Integer(), nullable=True),
        sa.Column("winner_side", sa.String(length=10), nullable=True),
        sa.Column("report_note", sa.Text(), nullable=True),
        sa.Column("reported_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["reported_by_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tournament_id",
            "round_number",
            "sequence_number",
            name="uq_tournament_matches_tournament_round_sequence",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_matches_tournament_id",
        "tournament_matches",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_matches_reported_by_user_id",
        "tournament_matches",
        ["reported_by_user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_matches_reported_by_user_id",
        table_name="tournament_matches",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_matches_tournament_id",
        table_name="tournament_matches",
        schema="platform",
    )
    op.drop_table("tournament_matches", schema="platform")
