#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import sys
from typing import Any

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from sqlalchemy import delete, func, select

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import purge_deleted_media_metadata
from python_packages.platform_infra.models import AuditLog, PreprodTestRun, Tournament, User


CONFIRMATION = "delete-preprod-runs"
CHUNK_SIZE = 10_000


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


def preprod_run_user_ids(run: PreprodTestRun) -> set[str]:
    report = dict(run.report or {})
    raw_ids = report.get("user_ids") or []
    return {str(user_id) for user_id in raw_ids if str(user_id).strip()}


def preprod_run_tournament_ids(run: PreprodTestRun) -> set[str]:
    report = dict(run.report or {})
    raw_ids = set(report.get("tournament_ids") or [])
    for key in ("tournament_id", "targeted_tournament_id", "scale_tournament_id"):
        value = report.get(key)
        if value:
            raw_ids.add(value)
    return {str(tournament_id) for tournament_id in raw_ids if str(tournament_id).strip()}


def chunks(values: set[str]) -> list[list[str]]:
    ordered = sorted(values)
    return [ordered[index:index + CHUNK_SIZE] for index in range(0, len(ordered), CHUNK_SIZE)]


async def count_existing(db_session, model, ids: set[str]) -> int:
    total = 0
    for chunk in chunks(ids):
        total += int(
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.id.in_(chunk))
            )
            or 0
        )
    return total


async def delete_by_ids(db_session, model, ids: set[str]) -> int:
    total = 0
    for chunk in chunks(ids):
        result = await db_session.execute(delete(model).where(model.id.in_(chunk)))
        total += int(result.rowcount or 0)
    return total


async def cleanup_preprod_runs(*, dry_run: bool, note: str) -> dict[str, Any]:
    settings = get_settings()
    if settings.platform_db_schema != "platform":
        raise RuntimeError(f"Refusing to cleanup non-platform schema: {settings.platform_db_schema}")
    if "platformdb" not in settings.platform_database_url:
        raise RuntimeError("Refusing to cleanup a database URL that does not point at platformdb.")

    async with session_factory()() as db_session:
        runs = list(
            (
                await db_session.scalars(
                    select(PreprodTestRun)
                    .where(PreprodTestRun.status != "cleaned")
                    .order_by(PreprodTestRun.created_at.asc())
                )
            ).all()
        )
        user_ids: set[str] = set()
        tournament_ids: set[str] = set()
        for run in runs:
            user_ids.update(preprod_run_user_ids(run))
            tournament_ids.update(preprod_run_tournament_ids(run))

        existing_users = await count_existing(db_session, User, user_ids) if user_ids else 0
        existing_tournaments = (
            await count_existing(db_session, Tournament, tournament_ids)
            if tournament_ids
            else 0
        )

        summary: dict[str, Any] = {
            "dry_run": dry_run,
            "runs": len(runs),
            "first_markers": [run.marker for run in runs[:10]],
            "tracked_users": len(user_ids),
            "tracked_tournaments": len(tournament_ids),
            "existing_users": existing_users,
            "existing_tournaments": existing_tournaments,
        }
        if dry_run or not runs:
            return summary

        media_metadata_deleted = await purge_deleted_media_metadata(
            db_session,
            owner_user_ids=user_ids,
            tournament_ids=tournament_ids,
        )

        audit_logs_deleted = 0
        subject_ids = set(user_ids) | set(tournament_ids)
        for user_chunk in chunks(user_ids):
            audit_result = await db_session.execute(
                delete(AuditLog).where(AuditLog.actor_user_id.in_(user_chunk))
            )
            audit_logs_deleted += int(audit_result.rowcount or 0)
        for subject_chunk in chunks(subject_ids):
            audit_result = await db_session.execute(
                delete(AuditLog).where(AuditLog.subject_id.in_(subject_chunk))
            )
            audit_logs_deleted += int(audit_result.rowcount or 0)

        tournaments_deleted = await delete_by_ids(db_session, Tournament, tournament_ids) if tournament_ids else 0
        users_deleted = await delete_by_ids(db_session, User, user_ids) if user_ids else 0

        remaining_users = await count_existing(db_session, User, user_ids) if user_ids else 0
        remaining_tournaments = (
            await count_existing(db_session, Tournament, tournament_ids)
            if tournament_ids
            else 0
        )
        ok = remaining_users == 0 and remaining_tournaments == 0
        cleanup_state = {
            "ok": ok,
            "cleaned_at": datetime.now(UTC).isoformat(),
            "cleaned_by": "platform_cleanup_preprod_runs.py",
            "note": note,
            "tournaments_deleted": tournaments_deleted,
            "users_deleted": users_deleted,
            "audit_logs_deleted": audit_logs_deleted,
            "media_metadata_deleted": media_metadata_deleted,
            "remaining_users": remaining_users,
            "remaining_tournaments": remaining_tournaments,
        }
        for run in runs:
            run.cleanup_state = cleanup_state
            if ok:
                run.status = "cleaned"
        await db_session.commit()
        summary.update(cleanup_state)
        return summary


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup tracked platform preprod QA test data.")
    parser.add_argument("--dry-run", action="store_true", help="Print rows that would be removed.")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--note", default="Operator-requested preprod cleanup")
    parser.add_argument("--confirm", default="", help=f"Required confirmation token: {CONFIRMATION}")
    args = parser.parse_args()
    if args.env_file is not None:
        load_env_file(args.env_file)
    if not args.dry_run and args.confirm != CONFIRMATION:
        parser.error(f"cleanup requires --confirm {CONFIRMATION}")
    try:
        summary = await cleanup_preprod_runs(dry_run=args.dry_run, note=args.note)
        print(summary)
        return 0 if summary.get("ok", True) else 1
    finally:
        await dispose_engine()


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
