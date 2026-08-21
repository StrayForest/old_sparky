#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from sqlalchemy import delete, select

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import purge_deleted_media_metadata
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainEntry,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentInvite,
    TournamentInviteAccess,
    TournamentMatch,
    TournamentParticipant,
)


CONFIRMATION = "delete-test-tournaments"


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


async def cleanup_test_tournaments(dry_run: bool) -> int:
    settings = get_settings()
    if settings.platform_db_schema != "platform":
        raise RuntimeError(f"Refusing to cleanup non-platform schema: {settings.platform_db_schema}")
    if "platformdb" not in settings.platform_database_url:
        raise RuntimeError("Refusing to cleanup a database URL that does not point at platformdb.")

    async with session_factory()() as db_session:
        tournament_rows = (
            await db_session.execute(select(Tournament.id, Tournament.slug).order_by(Tournament.created_at.asc()))
        ).all()
        tournament_ids = [str(row.id) for row in tournament_rows]
        tournament_slugs = [str(row.slug) for row in tournament_rows]
        if not tournament_ids:
            print("No platform tournaments found.")
            return 0

        ready_round_ids = list(
            (
                await db_session.scalars(
                    select(TournamentDeadlockReadyRound.id).where(
                        TournamentDeadlockReadyRound.tournament_id.in_(tournament_ids)
                    )
                )
            ).all()
        )
        captain_round_ids = list(
            (
                await db_session.scalars(
                    select(TournamentDeadlockCaptainRound.id).where(
                        TournamentDeadlockCaptainRound.tournament_id.in_(tournament_ids)
                    )
                )
            ).all()
        )
        subject_ids = set(tournament_ids)
        for model in (
            TournamentParticipant,
            TournamentMatch,
            TournamentInvite,
            TournamentInviteAccess,
            TournamentDeadlockAssignmentRun,
        ):
            subject_ids.update(
                str(row_id)
                for row_id in (
                    await db_session.scalars(
                        select(model.id).where(model.tournament_id.in_(tournament_ids))
                    )
                ).all()
            )
        if ready_round_ids:
            subject_ids.update(str(row_id) for row_id in ready_round_ids)
            subject_ids.update(
                str(row_id)
                for row_id in (
                    await db_session.scalars(
                        select(TournamentDeadlockReadyVote.id).where(
                            TournamentDeadlockReadyVote.round_id.in_(ready_round_ids)
                        )
                    )
                ).all()
            )
        if captain_round_ids:
            subject_ids.update(str(row_id) for row_id in captain_round_ids)
            subject_ids.update(
                str(row_id)
                for row_id in (
                    await db_session.scalars(
                        select(TournamentDeadlockCaptainEntry.id).where(
                            TournamentDeadlockCaptainEntry.round_id.in_(captain_round_ids)
                        )
                    )
                ).all()
            )

        print(f"Platform tournaments: {len(tournament_ids)}")
        print(f"First slugs: {', '.join(tournament_slugs[:8])}")
        print(f"Tournament-scoped audit subject ids: {len(subject_ids)}")
        if dry_run:
            print("Dry run only; no rows deleted.")
            return 0

        await purge_deleted_media_metadata(db_session, tournament_ids=tournament_ids)

        await db_session.execute(delete(AuditLog).where(AuditLog.subject_id.in_(subject_ids)))
        await db_session.execute(
            delete(TournamentDeadlockAssignmentRun).where(
                TournamentDeadlockAssignmentRun.tournament_id.in_(tournament_ids)
            )
        )
        if captain_round_ids:
            await db_session.execute(
                delete(TournamentDeadlockCaptainEntry).where(
                    TournamentDeadlockCaptainEntry.round_id.in_(captain_round_ids)
                )
            )
            await db_session.execute(
                delete(TournamentDeadlockCaptainRound).where(
                    TournamentDeadlockCaptainRound.id.in_(captain_round_ids)
                )
            )
        if ready_round_ids:
            await db_session.execute(
                delete(TournamentDeadlockReadyVote).where(
                    TournamentDeadlockReadyVote.round_id.in_(ready_round_ids)
                )
            )
            await db_session.execute(
                delete(TournamentDeadlockReadyRound).where(
                    TournamentDeadlockReadyRound.id.in_(ready_round_ids)
                )
            )
        await db_session.execute(
            delete(TournamentInviteAccess).where(TournamentInviteAccess.tournament_id.in_(tournament_ids))
        )
        await db_session.execute(delete(TournamentInvite).where(TournamentInvite.tournament_id.in_(tournament_ids)))
        await db_session.execute(delete(TournamentMatch).where(TournamentMatch.tournament_id.in_(tournament_ids)))
        await db_session.execute(
            delete(TournamentParticipant).where(TournamentParticipant.tournament_id.in_(tournament_ids))
        )
        await db_session.execute(delete(Tournament).where(Tournament.id.in_(tournament_ids)))
        await db_session.commit()
        print(f"Deleted {len(tournament_ids)} platform tournaments and dependent rows.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete platform test tournaments while preserving users/profiles.")
    parser.add_argument("--dry-run", action="store_true", help="Print the rows that would be removed.")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Required confirmation token for deletion: {CONFIRMATION}",
    )
    args = parser.parse_args()
    if args.env_file is not None:
        load_env_file(args.env_file)
    if not args.dry_run and args.confirm != CONFIRMATION:
        parser.error(f"deletion requires --confirm {CONFIRMATION}")
    return asyncio.run(run_cleanup(dry_run=args.dry_run))


async def run_cleanup(*, dry_run: bool) -> int:
    try:
        return await cleanup_test_tournaments(dry_run=dry_run)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
