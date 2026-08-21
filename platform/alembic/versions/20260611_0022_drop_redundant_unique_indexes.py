"""Drop indexes duplicated by unique constraints."""
from __future__ import annotations

from alembic import op

revision = "20260611_0022"
down_revision = "20260609_0021"
branch_labels = None
depends_on = None

REDUNDANT_INDEXES = (
    ("ix_users_email", "platform.users", "email"),
    ("ix_sessions_token_digest", "platform.sessions", "token_digest"),
    ("ix_tournaments_slug", "platform.tournaments", "slug"),
    ("ix_tournament_invites_code", "platform.tournament_invites", "code"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, _, _ in REDUNDANT_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS platform.{index_name}")

    op.drop_constraint(
        "uq_tournament_participants_unique_member",
        "tournament_participants",
        schema="platform",
        type_="unique",
    )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for index_name, table_name, column_name in REDUNDANT_INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table_name} ({column_name})"
            )
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
            "uq_tournament_participants_unique_member "
            "ON platform.tournament_participants (tournament_id, user_id)"
        )

    op.execute(
        "ALTER TABLE platform.tournament_participants "
        "ADD CONSTRAINT uq_tournament_participants_unique_member "
        "UNIQUE USING INDEX uq_tournament_participants_unique_member"
    )
