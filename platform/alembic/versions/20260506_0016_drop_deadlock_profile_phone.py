"""Drop Deadlock profile phone."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260506_0016"
down_revision = "20260506_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("deadlock_profiles", "phone", schema="platform")


def downgrade() -> None:
    op.add_column("deadlock_profiles", sa.Column("phone", sa.String(length=20), nullable=True), schema="platform")
