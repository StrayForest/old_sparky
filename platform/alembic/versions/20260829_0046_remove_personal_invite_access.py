"""Remove per-user invite access; invite codes are temporary room proofs.

Revision ID: 20260829_0046
Revises: 20260829_0045
Create Date: 2026-08-29
"""

from __future__ import annotations

from alembic import op


revision = "20260829_0046"
down_revision = "20260829_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("tournament_invite_accesses", schema="platform")


def downgrade() -> None:
    raise RuntimeError("The legacy per-user invite access table is intentionally not restorable.")
