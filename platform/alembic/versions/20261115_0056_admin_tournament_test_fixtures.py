"""Add durable admin tournament test fixture ownership.

Revision ID: 20261115_0056
Revises: 20260903_0052
Create Date: 2026-11-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261115_0056"
down_revision = "20261112_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournaments",
        sa.Column("admin_test_fixture", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        schema="platform",
    )
    op.create_table(
        "admin_tournament_test_fixtures",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("creator_user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'cleaned')", name="status_allowed"),
        sa.ForeignKeyConstraint(["creator_user_id"], ["platform.users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_admin_tournament_test_fixtures"),
        sa.UniqueConstraint("tournament_id", name="uq_admin_test_fixture_tournament"),
        schema="platform",
    )
    op.create_index(
        "ix_admin_tournament_test_fixtures_creator_user_id",
        "admin_tournament_test_fixtures",
        ["creator_user_id"],
        schema="platform",
    )
    op.create_table(
        "admin_tournament_test_players",
        sa.Column("fixture_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("prepared_ready", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(["fixture_id"], ["platform.admin_tournament_test_fixtures.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["platform.users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fixture_id", "user_id", name="pk_admin_tournament_test_players"),
        sa.UniqueConstraint("user_id", name="uq_admin_test_player_user"),
        schema="platform",
    )


def downgrade() -> None:
    op.drop_table("admin_tournament_test_players", schema="platform")
    op.drop_index(
        "ix_admin_tournament_test_fixtures_creator_user_id",
        table_name="admin_tournament_test_fixtures",
        schema="platform",
    )
    op.drop_table("admin_tournament_test_fixtures", schema="platform")
    op.drop_column("tournaments", "admin_test_fixture", schema="platform")
