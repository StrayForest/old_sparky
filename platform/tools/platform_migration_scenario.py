"""Exercise the current persistence migrations against populated legacy data."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text, update

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, engine, session_factory
from python_packages.platform_infra.models import (
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentListReadModel,
    TournamentParticipantSlot,
    TournamentTeam,
    TournamentTeamMember,
    User,
)
from tools.platform_tournament_list_read_model_recovery import (
    INDEX_SPECS,
    _BACKFILL_SQL,
    repair_indexes_async,
    validate_projection_async,
)
from tools.platform_verification_lock import verification_resource_lock


TARGET_REVISION = "20260821_0039"
MID_REVISION = "20260901_0050"
HEAD_REVISION = "20260913_0053"


PARTIAL_PROJECTION_DDL = """
CREATE TABLE platform.tournament_list_read_models (
    id VARCHAR(36) NOT NULL,
    slug VARCHAR(140) NOT NULL,
    name VARCHAR(120) NOT NULL,
    description TEXT,
    cover_url VARCHAR(512),
    banner_asset_id VARCHAR(36),
    visibility VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    format_slug VARCHAR(64) NOT NULL,
    allowed_ranks JSONB NOT NULL,
    max_participants INTEGER,
    registration_starts_at TIMESTAMP WITH TIME ZONE,
    registration_closes_at TIMESTAMP WITH TIME ZONE,
    ready_check_starts_at TIMESTAMP WITH TIME ZONE,
    ready_check_ends_at TIMESTAMP WITH TIME ZONE,
    captain_selection_starts_at TIMESTAMP WITH TIME ZONE,
    starts_at TIMESTAMP WITH TIME ZONE,
    match_format VARCHAR(20) NOT NULL,
    final_format VARCHAR(20) NOT NULL,
    captain_response_deadline_minutes INTEGER,
    teams_count INTEGER,
    automation_ready_check_started_at TIMESTAMP WITH TIME ZONE,
    automation_ready_check_closed_at TIMESTAMP WITH TIME ZONE,
    automation_captain_round_started_at TIMESTAMP WITH TIME ZONE,
    automation_captain_round_finalized_at TIMESTAMP WITH TIME ZONE,
    automation_assignment_generated_at TIMESTAMP WITH TIME ZONE,
    automation_last_error TEXT,
    automation_failure_count INTEGER DEFAULT 0 NOT NULL,
    automation_retry_after TIMESTAMP WITH TIME ZONE,
    organizer_user_id VARCHAR(36) NOT NULL,
    organizer_display_name VARCHAR(40) NOT NULL,
    organizer_avatar_asset_id VARCHAR(36),
    participant_count INTEGER DEFAULT 0 NOT NULL,
    has_locked_deadlock_roster BOOLEAN DEFAULT FALSE NOT NULL,
    bracket_revision INTEGER DEFAULT 0 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_tournament_list_read_models PRIMARY KEY (id),
    CONSTRAINT uq_tournament_list_read_models_slug UNIQUE (slug),
    CONSTRAINT fk_tournament_list_read_models_id_tournaments
        FOREIGN KEY (id) REFERENCES platform.tournaments(id) ON DELETE CASCADE
)
"""


def _run_alembic(
    revision: str,
    *,
    expect_success: bool,
    operation: str = "upgrade",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if extra_env:
        command_env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", operation, revision],
        cwd=ROOT,
        env=command_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if (result.returncode == 0) != expect_success:
        output = result.stdout[-4000:]
        raise RuntimeError(
            f"alembic {operation} {revision!r} returned {result.returncode}; output:\n{output}"
        )
    return result


def _run_recovery(*, expect_success: bool, extra_env: dict[str, str] | None = None) -> str:
    command_env = os.environ.copy()
    if extra_env:
        command_env.update(extra_env)
    result = subprocess.run(
        [
            sys.executable,
            "tools/platform_tournament_list_read_model_recovery.py",
            "--recover-partial",
        ],
        cwd=ROOT,
        env=command_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if (result.returncode == 0) != expect_success:
        output = result.stdout[-4000:]
        raise RuntimeError(
            f"tournament catalog recovery returned {result.returncode}; output:\n{output}"
        )
    return result.stdout


async def _seed_legacy_rows() -> tuple[str, int, int]:
    prefix = f"migration-scenario-{uuid4().hex[:12]}"
    user_id = str(uuid4())
    tournament_id = str(uuid4())
    async with session_factory()() as db_session:
        db_session.add(User(id=user_id, email=f"{prefix}@example.test", display_name=prefix))
        await db_session.flush()
        tournament = Tournament(
            id=tournament_id,
            slug=prefix,
            name=f"Migration scenario {prefix}",
            description="Disposable populated migration fixture.",
            visibility="private",
            status="registration_closed",
            format_slug="solo",
            organizer_user_id=user_id,
            allowed_ranks=[],
            max_participants=16,
        )
        db_session.add(tournament)
        await db_session.flush()
        first = TournamentDeadlockReadyRound(
            tournament_id=tournament_id,
            status="active",
            eligible_user_ids=[],
            initiated_by_user_id=user_id,
        )
        second = TournamentDeadlockReadyRound(
            tournament_id=tournament_id,
            status="active",
            eligible_user_ids=[],
            initiated_by_user_id=user_id,
        )
        db_session.add_all([first, second])
        await db_session.flush()
        captain_round = TournamentDeadlockCaptainRound(
            tournament_id=tournament_id,
            source_ready_round_id=first.id,
            teams_count=1,
            status="finalized",
            initiated_by_user_id=user_id,
            finalized_at=datetime.now(UTC),
        )
        db_session.add(captain_round)
        await db_session.flush()
        now = datetime.now(UTC)
        db_session.add(
            TournamentDeadlockAssignmentRun(
                id=str(uuid4()),
                tournament_id=tournament_id,
                source_captain_round_id=captain_round.id,
                source_ready_round_id=first.id,
                created_by_user_id=user_id,
                status="locked",
                published_at=now,
                published_by_user_id=user_id,
                locked_at=now,
                locked_by_user_id=user_id,
                summary_text="Migration backfill fixture.",
                result_snapshot={
                    "teams": [
                        {
                            "team_id": "1",
                            "team_name": "Backfilled Team",
                            "starter_strength": 1000.0,
                            "starter_average_strength": 1000.0,
                            "captain": {"user_id": user_id, "strength": 1000.0},
                            "starter_slots": [],
                            "reserve_slot": None,
                        }
                    ]
                },
                candidate_pool_user_ids=[user_id],
                leftover_user_ids=[],
            )
        )
        await db_session.commit()
        return tournament_id, int(first.id), int(second.id)


async def _repair_duplicate(tournament_id: str, round_id: int) -> None:
    async with session_factory()() as db_session:
        await db_session.execute(
            update(TournamentDeadlockReadyRound)
            .where(
                TournamentDeadlockReadyRound.tournament_id == tournament_id,
                TournamentDeadlockReadyRound.id == round_id,
            )
            .values(status="closed")
        )
        await db_session.commit()


async def _reset_disposable_schema() -> None:
    """Reset only the explicitly guarded platformdb_test schema.

    The migration history contains an intentionally irreversible revision, so
    a downgrade-to-base cannot be the scenario reset mechanism.  Recreating
    the schema keeps the test deterministic without weakening that migration's
    production rollback contract.
    """

    async with session_factory()() as db_session:
        await db_session.execute(text("DROP SCHEMA IF EXISTS platform CASCADE"))
        await db_session.execute(text("CREATE SCHEMA platform"))
        await db_session.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
        await db_session.commit()
    await dispose_engine()


async def _create_partial_projection() -> None:
    """Recreate the exact committed 0051 table/data state for retry tests."""

    async with engine().connect() as db_connection:
        db_connection = await db_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await db_connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    async with engine().connect() as db_connection:
        async with db_connection.begin():
            await db_connection.execute(text(PARTIAL_PROJECTION_DDL))
            await db_connection.execute(_BACKFILL_SQL)


async def _create_prior_projection_indexes(index_count: int) -> None:
    """Seed indexes before a selected concurrent build failure point."""

    async with engine().connect() as db_connection:
        db_connection = await db_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        for index_spec in INDEX_SPECS[:index_count]:
            await db_connection.execute(
                text(
                    index_spec.create_sql.replace(
                        "CREATE INDEX CONCURRENTLY",
                        "CREATE INDEX",
                        1,
                    )
                )
            )


async def _install_index_failure_trigger(index_name: str) -> tuple[str, str]:
    """Install a disposable PostgreSQL event trigger for one index build."""

    trigger_name = f"migration_fail_{uuid4().hex}"
    function_name = f"migration_fail_{uuid4().hex}"
    function_sql = f"""
CREATE FUNCTION platform."{function_name}"() RETURNS event_trigger
LANGUAGE plpgsql AS $$
DECLARE command record;
BEGIN
    FOR command IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        IF command.command_tag = 'CREATE INDEX'
           AND command.object_identity = 'platform.{index_name}' THEN
            RAISE EXCEPTION 'Injected concurrent index failure for {index_name}';
        END IF;
    END LOOP;
END;
$$
"""
    async with engine().connect() as db_connection:
        async with db_connection.begin():
            is_superuser = await db_connection.scalar(
                text(
                    "SELECT rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
            if not is_superuser:
                raise RuntimeError(
                    "Migration failure-injection requires the disposable PostgreSQL "
                    "role to be superuser; use a temporary test cluster or admin URL."
                )
            await db_connection.execute(text(function_sql))
            await db_connection.execute(
                text(
                    f'CREATE EVENT TRIGGER "{trigger_name}" '
                    "ON ddl_command_end "
                    f'EXECUTE FUNCTION platform."{function_name}"()'
                )
            )
    return trigger_name, function_name


async def _database_role_is_superuser() -> bool:
    async with engine().connect() as db_connection:
        return bool(
            await db_connection.scalar(
                text(
                    "SELECT rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        )


async def _remove_index_failure_trigger(trigger_name: str, function_name: str) -> None:
    async with engine().connect() as db_connection:
        async with db_connection.begin():
            await db_connection.execute(
                text(f'DROP EVENT TRIGGER IF EXISTS "{trigger_name}"')
            )
            await db_connection.execute(
                text(f'DROP FUNCTION IF EXISTS platform."{function_name}"()')
            )


async def _reset_partial_projection() -> None:
    async with engine().connect() as db_connection:
        async with db_connection.begin():
            await db_connection.execute(
                text("DROP TABLE platform.tournament_list_read_models CASCADE")
            )
            await db_connection.execute(
                text(
                    "UPDATE public.alembic_version "
                    "SET version_num = :revision"
                ),
                {"revision": MID_REVISION},
            )


async def _create_wrong_preexisting_index(index_name: str) -> None:
    """Occupy an expected index name with a valid index on the wrong table."""

    async with engine().connect() as db_connection:
        db_connection = await db_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await db_connection.execute(
            text(
                f'CREATE INDEX "{index_name}" '
                "ON platform.tournaments (id)"
            )
        )


async def _drop_wrong_preexisting_index(index_name: str) -> None:
    async with engine().connect() as db_connection:
        db_connection = await db_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await db_connection.execute(
            text(f'DROP INDEX CONCURRENTLY platform."{index_name}"')
        )


async def _create_wrong_definition_index(index_name: str) -> None:
    """Create a valid index with the expected name but incompatible keys."""

    async with engine().connect() as db_connection:
        db_connection = await db_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await db_connection.execute(
            text(
                f'CREATE INDEX "{index_name}" '
                "ON platform.tournament_list_read_models (id)"
            )
        )


async def _assert_preexisting_index_untouched(
    index_name: str,
    *,
    expected_rows: int | None,
    expected_table_name: str = "tournaments",
) -> None:
    async with engine().connect() as db_connection:
        version = await db_connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )
        projection_exists = await db_connection.scalar(
            text("SELECT to_regclass('platform.tournament_list_read_models') IS NOT NULL")
        )
        row_count = None
        if projection_exists:
            row_count = await db_connection.scalar(
                text("SELECT count(*) FROM platform.tournament_list_read_models")
            )
        wrong_owner = (
            await db_connection.execute(
                text(
                    "SELECT i.indisvalid, n_table.nspname AS table_schema, "
                    "table_class.relname AS table_name "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_index i ON i.indexrelid = c.oid "
                    "JOIN pg_class table_class ON table_class.oid = i.indrelid "
                    "JOIN pg_namespace n_table "
                    "ON n_table.oid = table_class.relnamespace "
                    "WHERE n.nspname = 'platform' AND c.relname = :index_name"
                ),
                {"index_name": index_name},
            )
        ).mappings().first()
    if version != MID_REVISION:
        raise RuntimeError("wrong preexisting index changed the partial migration state")
    if expected_rows is None and projection_exists:
        raise RuntimeError("wrong preexisting index changed the fresh migration state")
    if expected_rows is not None and (
        not projection_exists or int(row_count or 0) != expected_rows
    ):
        raise RuntimeError("wrong preexisting index changed the partial migration state")
    if (
        wrong_owner is None
        or not wrong_owner["indisvalid"]
        or wrong_owner["table_schema"] != "platform"
        or wrong_owner["table_name"] != expected_table_name
    ):
        raise RuntimeError("wrong preexisting index was not preserved for operator repair")


async def _assert_partial_index_failure(index_name: str, expected_rows: int) -> None:
    async with engine().connect() as db_connection:
        version = await db_connection.scalar(
            text("SELECT version_num FROM public.alembic_version")
        )
        row_count = await db_connection.scalar(
            text("SELECT count(*) FROM platform.tournament_list_read_models")
        )
        index_row = (
            await db_connection.execute(
                text(
                    "SELECT c.relkind, i.indisvalid, i.indisready, i.indislive "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_index i ON i.indexrelid = c.oid "
                    "WHERE n.nspname = 'platform' AND c.relname = :index_name"
                ),
                {"index_name": index_name},
            )
        ).mappings().first()
    if version != MID_REVISION:
        raise RuntimeError(f"failed migration stamped revision {version!r}")
    if int(row_count or 0) != expected_rows:
        raise RuntimeError("failed migration did not retain the projection backfill")
    if (
        index_row is None
        or (
            index_row["relkind"] not in {"i", b"i"}
            or index_row["indisvalid"] is not False
        )
    ):
        # A lock-timeout injection can fail before PostgreSQL allocates the
        # index relation; an event-trigger injection leaves an invalid one.
        if index_row is not None:
            raise RuntimeError(
                f"failed migration left an unexpected {index_name} index state"
            )


async def _fail_index_under_table_lock() -> None:
    """Force a real CREATE INDEX CONCURRENTLY lock-timeout without superuser."""

    async with engine().connect() as db_connection:
        table_oid = await validate_projection_async(db_connection)
    async with engine().connect() as lock_connection:
        lock_transaction = await lock_connection.begin()
        await lock_connection.execute(
            text(
                "LOCK TABLE platform.tournament_list_read_models "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
        try:
            async with engine().connect() as index_connection:
                index_connection = await index_connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                await index_connection.execute(text("SET lock_timeout = '100ms'"))
                try:
                    await repair_indexes_async(index_connection, table_oid)
                except Exception as exc:
                    if "lock timeout" not in str(exc).lower():
                        raise
                else:
                    raise RuntimeError("lock-timeout failure injection unexpectedly succeeded")
        finally:
            await lock_transaction.rollback()


async def _assert_projection_indexes_and_rows(expected_rows: int) -> None:
    async with engine().connect() as db_connection:
        table_oid = await validate_projection_async(db_connection)
        row_count = await db_connection.scalar(
            text("SELECT count(*) FROM platform.tournament_list_read_models")
        )
        if int(row_count or 0) != expected_rows:
            raise RuntimeError("projection backfill row count is not idempotent")
    async with engine().connect() as db_connection:
        db_connection = await db_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        # ``repair_indexes_async`` validates every catalog definition before
        # deciding that an already-valid index is a no-op.  Calling it here
        # therefore checks validity, key order, options, opclasses and the
        # partial predicate without hiding a missing index behind IF NOT EXISTS.
        await repair_indexes_async(db_connection, table_oid)


async def _run_retry_failure_injection_scenarios(expected_rows: int) -> None:
    """Fail each concurrent index build in PostgreSQL, then retry it."""

    is_superuser = await _database_role_is_superuser()
    for index_number, index_spec in enumerate(INDEX_SPECS):
        if index_number or not is_superuser:
            await _create_partial_projection()
            await _create_prior_projection_indexes(index_number)
        if is_superuser:
            trigger_name, function_name = await _install_index_failure_trigger(
                index_spec.name
            )
            try:
                if index_number == 0:
                    failed = _run_alembic(
                        "20260901_0051",
                        expect_success=False,
                    ).stdout
                else:
                    failed = _run_recovery(expect_success=False)
                if index_spec.name not in failed:
                    raise RuntimeError(
                        f"failure injection did not identify {index_spec.name}"
                    )
            finally:
                await _remove_index_failure_trigger(trigger_name, function_name)
        else:
            await _fail_index_under_table_lock()
        await _assert_partial_index_failure(index_spec.name, expected_rows)
        _run_recovery(expect_success=True)
        await _assert_projection_indexes_and_rows(expected_rows)
        if index_number != len(INDEX_SPECS) - 1:
            await _reset_partial_projection()


async def _run_wrong_preexisting_object_scenario(expected_rows: int) -> None:
    """Reject an expected index name that belongs to another table."""

    await _reset_partial_projection()
    index_name = INDEX_SPECS[0].name
    await _create_wrong_preexisting_index(index_name)
    try:
        failed = _run_recovery(expect_success=False)
        if "different table" not in failed.lower():
            raise RuntimeError(
                "recovery did not reject the valid index with the wrong owner table"
            )
        await _assert_preexisting_index_untouched(
            index_name,
            expected_rows=None,
        )
    finally:
        await _drop_wrong_preexisting_index(index_name)

    await _create_partial_projection()
    await _create_wrong_preexisting_index(index_name)
    try:
        failed = _run_recovery(expect_success=False)
        if "different table" not in failed.lower():
            raise RuntimeError(
                "recovery did not reject the valid index with the wrong owner table"
            )
        await _assert_preexisting_index_untouched(
            index_name,
            expected_rows=expected_rows,
        )
    finally:
        await _drop_wrong_preexisting_index(index_name)
    _run_recovery(expect_success=True)
    await _assert_projection_indexes_and_rows(expected_rows)

    await _reset_partial_projection()
    await _create_partial_projection()
    await _create_wrong_definition_index(index_name)
    try:
        failed = _run_recovery(expect_success=False)
        if "incompatible definition" not in failed.lower():
            raise RuntimeError(
                "recovery did not reject the valid index with the wrong definition"
            )
        await _assert_preexisting_index_untouched(
            index_name,
            expected_rows=expected_rows,
            expected_table_name="tournament_list_read_models",
        )
    finally:
        await _drop_wrong_preexisting_index(index_name)
    _run_recovery(expect_success=True)
    await _assert_projection_indexes_and_rows(expected_rows)


async def _assert_repaired_state(tournament_id: str) -> None:
    async with session_factory()() as db_session:
        tournament = await db_session.scalar(
            select(Tournament).where(Tournament.id == tournament_id)
        )
        if tournament is None or tournament.visibility != "invite_only":
            raise RuntimeError("legacy private visibility was not normalized")
        active_rounds = list(
            await db_session.scalars(
                select(TournamentDeadlockReadyRound).where(
                    TournamentDeadlockReadyRound.tournament_id == tournament_id,
                    TournamentDeadlockReadyRound.status == "active",
                )
            )
        )
        if len(active_rounds) != 1:
            raise RuntimeError(f"expected one active ready round, found {len(active_rounds)}")
        slot_count = len(
            list(
                await db_session.scalars(
                    select(TournamentParticipantSlot).where(
                        TournamentParticipantSlot.tournament_id == tournament_id
                    )
                )
            )
        )
        if slot_count != 16:
            raise RuntimeError(f"expected 16 participant capacity slots, found {slot_count}")
        team_rows = list(
            await db_session.scalars(
                select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id)
            )
        )
        member_rows = list(
            await db_session.scalars(
                select(TournamentTeamMember).where(
                    TournamentTeamMember.tournament_id == tournament_id
                )
            )
        )
        if len(team_rows) != 1 or team_rows[0].team_key != "1" or team_rows[0].name != "Backfilled Team":
            raise RuntimeError("published/locked assignment teams were not backfilled")
        if len(member_rows) != 1 or member_rows[0].roster_role != "captain":
            raise RuntimeError("published/locked assignment members were not backfilled")
        projection = await db_session.scalar(
            select(TournamentListReadModel).where(
                TournamentListReadModel.id == tournament_id
            )
        )
        organizer_name = await db_session.scalar(
            select(User.display_name).where(User.id == tournament.organizer_user_id)
        )
        if (
            projection is None
            or projection.slug != tournament.slug
            or projection.organizer_display_name != organizer_name
            or not projection.has_locked_deadlock_roster
        ):
            raise RuntimeError("tournament catalog read-model was not backfilled")


async def _migration_body() -> None:
    settings = get_settings()
    validate_platform_settings(settings)
    database_name = urlsplit(settings.platform_database_url).path.lstrip("/")
    if settings.platform_environment != "test" or database_name != "platformdb_test":
        raise RuntimeError("Migration scenario requires PLATFORM_ENVIRONMENT=test and platformdb_test")

    # The runner owns a disposable test database. Reset only its application
    # schema so local reruns and CI both exercise the same populated migration
    # states, including histories with irreversible downgrade revisions.
    await _reset_disposable_schema()
    _run_alembic(HEAD_REVISION, expect_success=True)
    _run_alembic(HEAD_REVISION, expect_success=True)

    # Exercise the historical populated-data repair path separately from the
    # 0051 retry cases below.
    await _reset_disposable_schema()
    _run_alembic(TARGET_REVISION, expect_success=True)
    tournament_id, _first_round_id, second_round_id = await _seed_legacy_rows()
    try:
        failed = _run_alembic(HEAD_REVISION, expect_success=False)
        if "Repair the data before retrying" not in failed.stdout:
            raise RuntimeError("migration did not fail with the expected invariant message")
        await _repair_duplicate(tournament_id, second_round_id)
        _run_alembic(HEAD_REVISION, expect_success=True)
        await _assert_repaired_state(tournament_id)

        # Start from 0050 with a committed projection and exercise every
        # concurrent-index failure boundary against PostgreSQL itself.  The
        # trigger raises after CREATE INDEX, leaving its catalog row invalid;
        # the retry must drop/rebuild it before stamping 0051.
        await _reset_disposable_schema()
        _run_alembic(TARGET_REVISION, expect_success=True)
        retry_tournament_id, _first_round_id, retry_second_round_id = await _seed_legacy_rows()
        await _repair_duplicate(retry_tournament_id, retry_second_round_id)
        _run_alembic(MID_REVISION, expect_success=True)
        await _run_retry_failure_injection_scenarios(expected_rows=1)
        await _run_wrong_preexisting_object_scenario(expected_rows=1)
        _run_alembic(HEAD_REVISION, expect_success=True)
        _run_alembic(HEAD_REVISION, expect_success=True)
        await _assert_repaired_state(retry_tournament_id)
        print(
            "Migration scenario passed: fresh and repeated upgrades, populated "
            "legacy repair, and every 0051 concurrent-index retry boundary passed."
        )
    finally:
        await dispose_engine()


async def _main() -> None:
    # The migration scenario resets and mutates the same host test resources
    # used by backend integration.  CI jobs have isolated service containers,
    # while local/canonical invocations must hold the shared contour lock.
    settings = get_settings()
    validate_platform_settings(settings)
    database_name = urlsplit(settings.platform_database_url).path.lstrip("/")
    if settings.platform_environment != "test" or database_name != "platformdb_test":
        raise RuntimeError("Migration scenario requires PLATFORM_ENVIRONMENT=test and platformdb_test")
    with verification_resource_lock("migration"):
        await _migration_body()


if __name__ == "__main__":
    asyncio.run(_main())
