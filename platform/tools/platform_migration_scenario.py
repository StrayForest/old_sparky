"""Exercise the current persistence migrations against populated legacy data."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    Tournament,
    TournamentDeadlockReadyRound,
    TournamentParticipantSlot,
    User,
)


TARGET_REVISION = "20260821_0039"
HEAD_REVISION = "20260824_0043"


def _run_alembic(
    revision: str,
    *,
    expect_success: bool,
    operation: str = "upgrade",
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", operation, revision],
        cwd=ROOT,
        env=os.environ.copy(),
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


async def _main() -> None:
    settings = get_settings()
    validate_platform_settings(settings)
    database_name = urlsplit(settings.platform_database_url).path.lstrip("/")
    if settings.platform_environment != "test" or database_name != "platformdb_test":
        raise RuntimeError("Migration scenario requires PLATFORM_ENVIRONMENT=test and platformdb_test")

    # The runner owns a disposable test database. Reset it so local reruns and
    # CI both exercise the same populated pre-0040 state.
    _run_alembic("base", expect_success=True, operation="downgrade")
    _run_alembic(TARGET_REVISION, expect_success=True)
    tournament_id, _first_round_id, second_round_id = await _seed_legacy_rows()
    try:
        failed = _run_alembic(HEAD_REVISION, expect_success=False)
        if "Repair the data before retrying" not in failed.stdout:
            raise RuntimeError("migration did not fail with the expected invariant message")
        await _repair_duplicate(tournament_id, second_round_id)
        _run_alembic(HEAD_REVISION, expect_success=True)
        await _assert_repaired_state(tournament_id)
        print("Migration scenario passed: populated legacy data failed fast, was repaired, and upgraded.")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
