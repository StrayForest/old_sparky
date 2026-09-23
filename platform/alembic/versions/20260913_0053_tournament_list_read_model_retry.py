"""Validate and repair the tournament catalog projection after 0051 retries.

The historical 0051 revision is already applied in production and therefore
must not be edited.  This forward revision makes the repaired state explicit
for databases that recorded 0051/0052 before an index-build interruption was
noticed.  A failed concurrent build leaves this revision unstamped, so a
subsequent ``upgrade head`` re-enters the same validation/repair path.
"""

from __future__ import annotations

from alembic import op

from tools.platform_tournament_list_read_model_recovery import (
    backfill_projection_sync,
    repair_indexes_async,
    validate_projection_sync,
)


revision = "20260913_0053"
down_revision = "20260903_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    table_oid = validate_projection_sync(bind)
    backfill_projection_sync(bind)

    # PostgreSQL forbids CREATE/DROP INDEX CONCURRENTLY inside a transaction.
    # Alembic's block commits the validated/backfilled transaction, runs each
    # index operation in AUTOCOMMIT, and leaves the revision unstamped on any
    # failure so a retry can repair an invalid index.
    with op.get_context().autocommit_block():
        op.run_async(repair_indexes_async, table_oid)


def downgrade() -> None:
    """The forward validation has no independent reversible schema state."""
