"""Add tournament participant moderation fields."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260422_0005"
down_revision = "20260422_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tournament_participants",
        sa.Column("moderation_note", sa.Text(), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournament_participants",
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        schema="platform",
    )
    op.add_column(
        "tournament_participants",
        sa.Column("moderated_by_user_id", sa.String(length=36), nullable=True),
        schema="platform",
    )
    op.create_foreign_key(
        "fk_tournament_participants_moderated_by_user_id_users",
        "tournament_participants",
        "users",
        ["moderated_by_user_id"],
        ["id"],
        source_schema="platform",
        referent_schema="platform",
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tournament_participants_moderated_by_user_id",
        "tournament_participants",
        ["moderated_by_user_id"],
        unique=False,
        schema="platform",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tournament_participants_moderated_by_user_id",
        table_name="tournament_participants",
        schema="platform",
    )
    op.drop_constraint(
        "fk_tournament_participants_moderated_by_user_id_users",
        "tournament_participants",
        schema="platform",
        type_="foreignkey",
    )
    op.drop_column("tournament_participants", "moderated_by_user_id", schema="platform")
    op.drop_column("tournament_participants", "moderated_at", schema="platform")
    op.drop_column("tournament_participants", "moderation_note", schema="platform")
