"""Mark frontend UI changes (no DB schema changes).

This migration is a no-op for the database but records a new
revision so deployments that run migrations can synchronize
release history with frontend updates.
"""
from __future__ import annotations

revision = "20260429_0012"
down_revision = "20260429_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No schema changes required for this frontend-only update.
    return None


def downgrade() -> None:
    return None
