"""Add player profile contact fields."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260506_0015"
down_revision = "20260430_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("player_profiles", sa.Column("contact_email", sa.String(length=255), nullable=True), schema="platform")
    op.add_column("player_profiles", sa.Column("steam_id", sa.String(length=64), nullable=True), schema="platform")
    op.add_column("player_profiles", sa.Column("discord_account", sa.String(length=64), nullable=True), schema="platform")


def downgrade() -> None:
    op.drop_column("player_profiles", "discord_account", schema="platform")
    op.drop_column("player_profiles", "steam_id", schema="platform")
    op.drop_column("player_profiles", "contact_email", schema="platform")
