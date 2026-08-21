"""Add consumable tournament creation credits."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260620_0023"
down_revision = "20260611_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "public_tournament_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="platform",
    )
    op.add_column(
        "users",
        sa.Column(
            "private_tournament_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="platform",
    )
    op.execute(
        """
        UPDATE platform.users
        SET public_tournament_credits = GREATEST(
                public_tournament_monthly_limit,
                CASE WHEN can_create_public_tournaments THEN 1 ELSE 0 END
            ),
            private_tournament_credits = GREATEST(
                private_tournament_monthly_limit - 1,
                0
            )
        """
    )


def downgrade() -> None:
    op.drop_column("users", "private_tournament_credits", schema="platform")
    op.drop_column("users", "public_tournament_credits", schema="platform")
