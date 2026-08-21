"""Create tournaments with registration closed by default."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260430_0014"
down_revision = "20260430_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE platform.tournaments
        SET status = 'registration_closed'
        WHERE status = 'draft'
        """
    )
    op.alter_column(
        "tournaments",
        "status",
        schema="platform",
        existing_type=sa.String(length=20),
        server_default="registration_closed",
    )


def downgrade() -> None:
    op.alter_column(
        "tournaments",
        "status",
        schema="platform",
        existing_type=sa.String(length=20),
        server_default="draft",
    )
