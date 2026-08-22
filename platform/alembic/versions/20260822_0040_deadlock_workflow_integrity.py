"""Enforce persistent Deadlock workflow and tournament-state invariants.

Revision ID: 20260822_0040
Revises: 20260821_0039
Create Date: 2026-08-22 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
import sqlalchemy as sa


revision = "20260822_0040"
down_revision = "20260821_0039"
branch_labels = None
depends_on = None


_DATA_INVARIANT_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "dream-slot number outside 1..6",
        """
        SELECT count(*) FROM platform.deadlock_dream_slots
        WHERE slot_number NOT BETWEEN 1 AND 6
        """,
    ),
    (
        "invalid tournament visibility/status or non-positive capacity",
        """
        SELECT count(*) FROM platform.tournaments
        WHERE visibility NOT IN ('public', 'invite_only')
           OR status NOT IN ('registration_open', 'registration_closed', 'in_progress', 'completed', 'cancelled')
           OR (max_participants IS NOT NULL AND max_participants <= 0)
           OR bracket_revision < 0
        """,
    ),
    (
        "invalid tournament invite use counters",
        """
        SELECT count(*) FROM platform.tournament_invites
        WHERE max_uses <= 0 OR use_count < 0 OR use_count > max_uses
        """,
    ),
    (
        "invalid Deadlock ready-round state",
        """
        SELECT count(*) FROM platform.tournament_deadlock_ready_rounds
        WHERE status NOT IN ('active', 'closed', 'stopped')
        """,
    ),
    (
        "more than one active Deadlock ready round for a tournament",
        """
        SELECT count(*) FROM (
            SELECT tournament_id
            FROM platform.tournament_deadlock_ready_rounds
            WHERE status = 'active'
            GROUP BY tournament_id
            HAVING count(*) > 1
        ) AS duplicates
        """,
    ),
    (
        "invalid Deadlock ready vote choice",
        """
        SELECT count(*) FROM platform.tournament_deadlock_ready_votes
        WHERE choice NOT IN ('yes', 'no')
        """,
    ),
    (
        "invalid or duplicate Deadlock captain round",
        """
        SELECT count(*) FROM (
            SELECT source_ready_round_id
            FROM platform.tournament_deadlock_captain_rounds
            GROUP BY source_ready_round_id
            HAVING count(*) > 1
        ) AS duplicates
        """,
    ),
    (
        "invalid Deadlock captain round state",
        """
        SELECT count(*) FROM platform.tournament_deadlock_captain_rounds
        WHERE teams_count <= 0 OR status NOT IN ('active', 'closed', 'finalized')
        """,
    ),
    (
        "more than one active Deadlock captain round for a tournament",
        """
        SELECT count(*) FROM (
            SELECT tournament_id
            FROM platform.tournament_deadlock_captain_rounds
            WHERE status = 'active'
            GROUP BY tournament_id
            HAVING count(*) > 1
        ) AS duplicates
        """,
    ),
    (
        "invalid Deadlock captain entry state",
        """
        SELECT count(*) FROM platform.tournament_deadlock_captain_entries
        WHERE offer_order <= 0
           OR state NOT IN ('queued', 'offered', 'accepted', 'declined', 'cancelled', 'assigned')
        """,
    ),
    (
        "invalid Deadlock auto-assignment run state",
        """
        SELECT count(*) FROM platform.tournament_deadlock_assignment_runs
        WHERE status NOT IN ('generated', 'published', 'superseded', 'locked')
        """,
    ),
    (
        "more than one published or locked assignment run for a tournament",
        """
        SELECT count(*) FROM (
            SELECT tournament_id
            FROM platform.tournament_deadlock_assignment_runs
            WHERE status IN ('published', 'locked')
            GROUP BY tournament_id
            HAVING count(*) > 1
        ) AS duplicates
        """,
    ),
)


_CONCURRENT_UNIQUE_INDEXES: tuple[tuple[str, str], ...] = (
    (
        "uq_tournament_deadlock_ready_rounds_active_tournament",
        "platform.tournament_deadlock_ready_rounds (tournament_id) WHERE status = 'active'",
    ),
    (
        "uq_tournament_deadlock_captain_rounds_active_tournament",
        "platform.tournament_deadlock_captain_rounds (tournament_id) WHERE status = 'active'",
    ),
    (
        "uq_tournament_deadlock_assignment_runs_current_tournament",
        "platform.tournament_deadlock_assignment_runs (tournament_id) "
        "WHERE status IN ('published', 'locked')",
    ),
    (
        "uq_tournament_deadlock_captain_rounds_source_ready_round",
        "platform.tournament_deadlock_captain_rounds (source_ready_round_id)",
    ),
)


_CHECK_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    (
        "deadlock_dream_slots",
        "ck_deadlock_dream_slots_slot_number_in_range",
        "slot_number BETWEEN 1 AND 6",
    ),
    (
        "tournaments",
        "ck_tournaments_visibility_allowed",
        "visibility IN ('public', 'invite_only')",
    ),
    (
        "tournaments",
        "ck_tournaments_status_allowed",
        "status IN ('registration_open', 'registration_closed', 'in_progress', 'completed', 'cancelled')",
    ),
    (
        "tournaments",
        "ck_tournaments_max_participants_positive",
        "max_participants IS NULL OR max_participants > 0",
    ),
    (
        "tournaments",
        "ck_tournaments_bracket_revision_nonnegative",
        "bracket_revision >= 0",
    ),
    ("tournament_invites", "ck_tournament_invites_max_uses_positive", "max_uses > 0"),
    ("tournament_invites", "ck_tournament_invites_use_count_nonnegative", "use_count >= 0"),
    ("tournament_invites", "ck_tournament_invites_use_count_within_limit", "use_count <= max_uses"),
    (
        "tournament_deadlock_ready_rounds",
        "ck_tournament_deadlock_ready_rounds_status_allowed",
        "status IN ('active', 'closed', 'stopped')",
    ),
    (
        "tournament_deadlock_ready_votes",
        "ck_tournament_deadlock_ready_votes_choice_allowed",
        "choice IN ('yes', 'no')",
    ),
    (
        "tournament_deadlock_captain_rounds",
        "ck_tournament_deadlock_captain_rounds_teams_count_positive",
        "teams_count > 0",
    ),
    (
        "tournament_deadlock_captain_rounds",
        "ck_tournament_deadlock_captain_rounds_status_allowed",
        "status IN ('active', 'closed', 'finalized')",
    ),
    (
        "tournament_deadlock_captain_entries",
        "ck_tournament_deadlock_captain_entries_offer_order_positive",
        "offer_order > 0",
    ),
    (
        "tournament_deadlock_captain_entries",
        "ck_tournament_deadlock_captain_entries_state_allowed",
        "state IN ('queued', 'offered', 'accepted', 'declined', 'cancelled', 'assigned')",
    ),
    (
        "tournament_deadlock_assignment_runs",
        "ck_tournament_deadlock_assignment_runs_status_allowed",
        "status IN ('generated', 'published', 'superseded', 'locked')",
    ),
)


def _assert_data_invariants() -> None:
    bind = op.get_bind()
    for label, statement in _DATA_INVARIANT_CHECKS:
        invalid_count = int(bind.scalar(sa.text(statement)) or 0)
        if invalid_count:
            raise RuntimeError(
                "Cannot apply 20260822_0040: found "
                f"{invalid_count} row group(s) with {label}. Repair the data before retrying."
            )


def _normalize_legacy_private_visibility() -> None:
    """Upgrade the retired storage alias before enforcing API visibility values."""

    op.get_bind().execute(
        sa.text(
            """
            UPDATE platform.tournaments
            SET visibility = 'invite_only'
            WHERE visibility = 'private'
            """
        )
    )


def _drop_invalid_concurrent_index(index_name: str) -> None:
    bind = op.get_bind()
    is_invalid = bind.scalar(
        sa.text(
            """
            SELECT NOT pg_index.indisvalid
            FROM pg_class
            JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
            JOIN pg_index ON pg_index.indexrelid = pg_class.oid
            WHERE pg_namespace.nspname = 'platform'
              AND pg_class.relname = :index_name
            """
        ),
        {"index_name": index_name},
    )
    if is_invalid:
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY platform.{index_name}")


def _create_concurrent_unique_indexes(indexes: Iterable[tuple[str, str]]) -> None:
    for index_name, definition in indexes:
        _drop_invalid_concurrent_index(index_name)
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                f"{index_name} ON {definition}"
            )


def _constraint_exists(constraint_name: str) -> bool:
    return bool(
        op.get_bind().scalar(
            sa.text(
                """
                SELECT 1
                FROM pg_constraint
                JOIN pg_namespace ON pg_namespace.oid = pg_constraint.connamespace
                WHERE pg_namespace.nspname = 'platform'
                  AND pg_constraint.conname = :constraint_name
                """
            ),
            {"constraint_name": constraint_name},
        )
    )


def _create_check_constraints() -> None:
    for table_name, constraint_name, condition in _CHECK_CONSTRAINTS:
        if _constraint_exists(constraint_name):
            continue
        short_name = constraint_name.removeprefix(f"ck_{table_name}_")
        op.create_check_constraint(
            short_name,
            table_name,
            condition,
            schema="platform",
        )


def upgrade() -> None:
    _normalize_legacy_private_visibility()
    _assert_data_invariants()

    _create_check_constraints()

    _create_concurrent_unique_indexes(_CONCURRENT_UNIQUE_INDEXES)
    if not _constraint_exists("uq_tournament_deadlock_captain_rounds_source_ready_round"):
        op.execute(
            "ALTER TABLE platform.tournament_deadlock_captain_rounds "
            "ADD CONSTRAINT uq_tournament_deadlock_captain_rounds_source_ready_round "
            "UNIQUE USING INDEX uq_tournament_deadlock_captain_rounds_source_ready_round"
        )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform.tournament_deadlock_captain_rounds "
        "DROP CONSTRAINT IF EXISTS uq_tournament_deadlock_captain_rounds_source_ready_round"
    )
    for index_name, _ in _CONCURRENT_UNIQUE_INDEXES[:-1]:
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS platform.{index_name}")

    for table_name, constraint_name in (
        ("tournament_deadlock_assignment_runs", "ck_tournament_deadlock_assignment_runs_status_allowed"),
        ("tournament_deadlock_captain_entries", "ck_tournament_deadlock_captain_entries_state_allowed"),
        ("tournament_deadlock_captain_entries", "ck_tournament_deadlock_captain_entries_offer_order_positive"),
        ("tournament_deadlock_captain_rounds", "ck_tournament_deadlock_captain_rounds_status_allowed"),
        ("tournament_deadlock_captain_rounds", "ck_tournament_deadlock_captain_rounds_teams_count_positive"),
        ("tournament_deadlock_ready_votes", "ck_tournament_deadlock_ready_votes_choice_allowed"),
        ("tournament_deadlock_ready_rounds", "ck_tournament_deadlock_ready_rounds_status_allowed"),
        ("tournament_invites", "ck_tournament_invites_use_count_within_limit"),
        ("tournament_invites", "ck_tournament_invites_use_count_nonnegative"),
        ("tournament_invites", "ck_tournament_invites_max_uses_positive"),
        ("tournaments", "ck_tournaments_bracket_revision_nonnegative"),
        ("tournaments", "ck_tournaments_max_participants_positive"),
        ("tournaments", "ck_tournaments_status_allowed"),
        ("tournaments", "ck_tournaments_visibility_allowed"),
        ("deadlock_dream_slots", "ck_deadlock_dream_slots_slot_number_in_range"),
    ):
        op.execute(
            f"ALTER TABLE platform.{table_name} "
            f"DROP CONSTRAINT IF EXISTS {constraint_name}"
        )
