"""Add persisted tournament reference UI fields."""
from __future__ import annotations

from alembic import op

revision = "20260515_0018"
down_revision = "20260513_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform.tournaments ADD COLUMN IF NOT EXISTS cover_url VARCHAR(512)")
    op.execute(
        "ALTER TABLE platform.tournaments "
        "ADD COLUMN IF NOT EXISTS registration_starts_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute("ALTER TABLE platform.tournaments ADD COLUMN IF NOT EXISTS starts_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE platform.tournaments ADD COLUMN IF NOT EXISTS match_format VARCHAR(20)")
    op.execute("ALTER TABLE platform.tournaments ADD COLUMN IF NOT EXISTS final_format VARCHAR(20)")
    op.execute("UPDATE platform.tournaments SET match_format = 'bo1' WHERE match_format IS NULL")
    op.execute("UPDATE platform.tournaments SET final_format = 'bo3' WHERE final_format IS NULL")
    op.execute("ALTER TABLE platform.tournaments ALTER COLUMN match_format SET DEFAULT 'bo1'")
    op.execute("ALTER TABLE platform.tournaments ALTER COLUMN final_format SET DEFAULT 'bo3'")
    op.execute("ALTER TABLE platform.tournaments ALTER COLUMN match_format SET NOT NULL")
    op.execute("ALTER TABLE platform.tournaments ALTER COLUMN final_format SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE platform.tournaments DROP COLUMN IF EXISTS final_format")
    op.execute("ALTER TABLE platform.tournaments DROP COLUMN IF EXISTS match_format")
    op.execute("ALTER TABLE platform.tournaments DROP COLUMN IF EXISTS starts_at")
    op.execute("ALTER TABLE platform.tournaments DROP COLUMN IF EXISTS registration_starts_at")
    op.execute("ALTER TABLE platform.tournaments DROP COLUMN IF EXISTS cover_url")
