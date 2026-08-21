"""Add tournament quotas, remove draft status, and promote the operator."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260608_0020"
down_revision = "20260528_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "public_tournament_monthly_limit",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        schema="platform",
    )
    op.add_column(
        "users",
        sa.Column(
            "private_tournament_monthly_limit",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        schema="platform",
    )
    op.execute(
        """
        UPDATE platform.users
        SET public_tournament_monthly_limit = 1
        WHERE can_create_public_tournaments = TRUE
        """
    )
    op.execute(
        """
        UPDATE platform.tournaments
        SET status = 'registration_closed'
        WHERE status = 'draft'
        """
    )
    # Operator privileges are provisioned explicitly with
    # tools/platform_create_operator.py. Migrations must never grant roles
    # based on a reusable identity such as an email address.


def downgrade() -> None:
    op.drop_column("users", "private_tournament_monthly_limit", schema="platform")
    op.drop_column("users", "public_tournament_monthly_limit", schema="platform")
