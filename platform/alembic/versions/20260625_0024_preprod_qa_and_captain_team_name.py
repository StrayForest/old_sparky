"""Add preprod QA runs and captain team names."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260625_0024"
down_revision = "20260620_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "player_profiles",
        sa.Column("captain_team_name", sa.String(length=15), nullable=True),
        schema="platform",
    )
    op.create_table(
        "preprod_test_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("marker", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("origin", sa.String(length=255), nullable=True),
        sa.Column("requested_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tournaments_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_participants", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("teams_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matches_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report_path", sa.String(length=512), nullable=True),
        sa.Column("report", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("cleanup_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("marker", name="uq_preprod_test_runs_marker"),
        schema="platform",
    )
    op.create_index(
        "ix_preprod_test_runs_created_at",
        "preprod_test_runs",
        ["created_at"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_preprod_test_runs_status",
        "preprod_test_runs",
        ["status"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index("ix_preprod_test_runs_status", table_name="preprod_test_runs", schema="platform")
    op.drop_index("ix_preprod_test_runs_created_at", table_name="preprod_test_runs", schema="platform")
    op.drop_table("preprod_test_runs", schema="platform")
    op.drop_column("player_profiles", "captain_team_name", schema="platform")
