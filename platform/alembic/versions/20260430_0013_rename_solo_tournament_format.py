"""Rename solo Deadlock tournament format slug."""
from __future__ import annotations

from alembic import op

revision = "20260430_0013"
down_revision = "20260429_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE platform.tournaments
        SET format_slug = 'solo'
        WHERE format_slug = 'solo_balanced_deadlock'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE platform.tournaments
        SET format_slug = 'solo_balanced_deadlock'
        WHERE format_slug = 'solo'
        """
    )
