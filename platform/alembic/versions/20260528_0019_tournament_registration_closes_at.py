"""Add tournament registration close schedule field."""
from __future__ import annotations

from alembic import op

revision = "20260528_0019"
down_revision = "20260515_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform.tournaments "
        "ADD COLUMN IF NOT EXISTS registration_closes_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "UPDATE platform.tournaments "
        "SET registration_closes_at = ready_check_starts_at "
        "WHERE registration_closes_at IS NULL AND ready_check_starts_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE platform.tournaments DROP COLUMN IF EXISTS registration_closes_at")
