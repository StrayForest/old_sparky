"""Add tournament invite workflow support."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260422_0004"
down_revision = "20260422_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tournament_invites",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tournament_id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("last_claimed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("last_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tournament_id"], ["platform.tournaments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["last_claimed_by_user_id"], ["platform.users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("code", name="uq_tournament_invites_code"),
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invites_tournament_id",
        "tournament_invites",
        ["tournament_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invites_code",
        "tournament_invites",
        ["code"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invites_created_by_user_id",
        "tournament_invites",
        ["created_by_user_id"],
        unique=False,
        schema="platform",
    )
    op.create_index(
        "ix_tournament_invites_last_claimed_by_user_id",
        "tournament_invites",
        ["last_claimed_by_user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_invites_last_claimed_by_user_id",
        table_name="tournament_invites",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_invites_created_by_user_id",
        table_name="tournament_invites",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_invites_code",
        table_name="tournament_invites",
        schema="platform",
    )
    op.drop_index(
        "ix_tournament_invites_tournament_id",
        table_name="tournament_invites",
        schema="platform",
    )
    op.drop_table("tournament_invites", schema="platform")
