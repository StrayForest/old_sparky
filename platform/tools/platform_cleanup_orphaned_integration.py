#!/usr/bin/env python3
"""Remove one strictly identified orphan integration-test dataset from platformdb."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from python_packages.platform_infra.audit import write_audit_log
from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import purge_deleted_media_metadata
from python_packages.platform_infra.models import AuditLog, Tournament, User
from tools.platform_backup_restore_drill import check_latest_backup


MARKER_PATTERN = re.compile(r"^it-deadlock-[0-9a-f]{8}$")
MAX_USERS = 32
MAX_TOURNAMENTS = 4
CONFIRMATION = "DELETE_ORPHANED_INTEGRATION_DATA"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory or delete one bounded it-deadlock integration-test marker. "
            "Dry-run is the default."
        )
    )
    parser.add_argument("--marker", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("/opt/oldsparky/platform/shared/backups"),
    )
    parser.add_argument("--backup-max-age-hours", type=float, default=24.0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if not MARKER_PATTERN.fullmatch(args.marker):
        parser.error("marker must exactly match it-deadlock-<8 lowercase hex characters>")
    if args.backup_max_age_hours <= 0:
        parser.error("--backup-max-age-hours must be positive")
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not args.apply and args.confirm:
        parser.error("--confirm is valid only with --apply")
    return args


def load_env_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Platform env must be a regular file, not a symlink.")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o007:
        raise RuntimeError("Platform env must be operator-owned and inaccessible to others.")
    os.environ.setdefault("PLATFORM_SHARED_DIR", str(path.resolve().parent))
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'\"")


async def inventory(marker: str) -> tuple[list[User], list[Tournament]]:
    async with session_factory()() as db_session:
        users = list(
            await db_session.scalars(
                select(User)
                .where(func.lower(User.email).like(f"{marker}-%@example.com"))
                .order_by(User.email)
                .limit(MAX_USERS + 1)
            )
        )
        user_ids = {str(user.id) for user in users}
        tournament_filter = Tournament.slug.like(f"{marker}-%")
        if user_ids:
            tournament_filter = or_(
                tournament_filter,
                Tournament.organizer_user_id.in_(user_ids),
            )
        tournaments = list(
            await db_session.scalars(
                select(Tournament)
                .where(tournament_filter)
                .order_by(Tournament.slug)
                .limit(MAX_TOURNAMENTS + 1)
            )
        )
    validate_inventory(marker, users, tournaments)
    return users, tournaments


def validate_inventory(marker: str, users: list[User], tournaments: list[Tournament]) -> None:
    if len(users) > MAX_USERS or len(tournaments) > MAX_TOURNAMENTS:
        raise RuntimeError("Orphan integration inventory exceeds its hard safety bound.")
    email_pattern = re.compile(
        rf"^{re.escape(marker)}-[a-z0-9-]{{1,40}}@example[.]com$"
    )
    slug_pattern = re.compile(rf"^{re.escape(marker)}-[a-z0-9-]{{1,32}}$")
    user_ids = {str(user.id) for user in users}
    if any(
        not email_pattern.fullmatch(user.email.lower())
        or not user.display_name.startswith("test-")
        for user in users
    ):
        raise RuntimeError("A candidate user does not match the integration-test identity contract.")
    if any(
        str(tournament.organizer_user_id) not in user_ids
        and not slug_pattern.fullmatch(tournament.slug)
        for tournament in tournaments
    ):
        raise RuntimeError("A candidate tournament does not match the integration-test marker.")


async def cleanup(
    marker: str,
    *,
    backup: dict[str, Any],
) -> dict[str, Any]:
    users, tournaments = await inventory(marker)
    user_ids = {str(user.id) for user in users}
    tournament_ids = {str(tournament.id) for tournament in tournaments}
    if not user_ids and not tournament_ids:
        return {
            "ok": True,
            "marker": marker,
            "mutated": False,
            "users": 0,
            "tournaments": 0,
            "audit_logs": 0,
            "code": "nothing_to_cleanup",
        }

    async with session_factory()() as db_session:
        media_deleted = await purge_deleted_media_metadata(
            db_session,
            owner_user_ids=user_ids,
            tournament_ids=tournament_ids,
        )
        subject_ids = user_ids | tournament_ids
        audit_result = await db_session.execute(
            delete(AuditLog).where(
                or_(
                    AuditLog.actor_user_id.in_(user_ids),
                    AuditLog.subject_id.in_(subject_ids),
                )
            )
        )
        tournament_result = await db_session.execute(
            delete(Tournament).where(Tournament.id.in_(tournament_ids))
        ) if tournament_ids else None
        await db_session.flush()
        user_result = await db_session.execute(
            delete(User).where(User.id.in_(user_ids))
        ) if user_ids else None
        await write_audit_log(
            db_session,
            actor_user_id=None,
            action="ops.orphan_integration.cleaned",
            subject_type="maintenance_marker",
            subject_id=marker,
            payload={
                "users": len(user_ids),
                "tournaments": len(tournament_ids),
                "media_metadata": media_deleted,
                "backup_metadata": Path(str(backup["metadata_file"])).name,
                "backup_format": int(backup["format_version"]),
            },
        )
        await db_session.commit()

    remaining_users, remaining_tournaments = await inventory(marker)
    if remaining_users or remaining_tournaments:
        raise RuntimeError("Orphan integration cleanup verification failed.")
    return {
        "ok": True,
        "marker": marker,
        "mutated": True,
        "users": int(user_result.rowcount or 0) if user_result else 0,
        "tournaments": int(tournament_result.rowcount or 0) if tournament_result else 0,
        "audit_logs": int(audit_result.rowcount or 0),
        "media_metadata": media_deleted,
        "backup_metadata": Path(str(backup["metadata_file"])).name,
        "backup_format": int(backup["format_version"]),
    }


async def async_main(args: argparse.Namespace) -> dict[str, Any]:
    load_env_file(args.env_file)
    settings = get_settings()
    validate_platform_settings(settings)
    if settings.platform_environment.strip().lower() == "test":
        raise RuntimeError("Production orphan cleanup cannot run in the test environment.")

    users, tournaments = await inventory(args.marker)
    if not args.apply:
        return {
            "ok": True,
            "marker": args.marker,
            "mutated": False,
            "users": len(users),
            "tournaments": len(tournaments),
        }
    backup = check_latest_backup(
        args.backup_dir,
        max_age_hours=args.backup_max_age_hours,
    )
    if (
        int(backup.get("format_version") or 1) < 2
        or backup.get("alembic_revision_verified") is not True
    ):
        raise RuntimeError("Cleanup requires a fresh format-2 Alembic-verified backup.")
    return await cleanup(args.marker, backup=backup)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return await async_main(args)
    finally:
        await dispose_engine()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run(args))
        if args.as_json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(
                f"Orphan integration cleanup: marker={report['marker']}; "
                f"users={report['users']}; tournaments={report['tournaments']}; "
                f"mutated={str(report['mutated']).lower()}."
            )
        return 0
    except Exception as exc:
        constraint = None
        sqlstate = None
        if isinstance(exc, IntegrityError):
            diagnostic = getattr(exc.orig, "diag", None)
            constraint = getattr(diagnostic, "constraint_name", None)
            sqlstate = getattr(exc.orig, "sqlstate", None)
        if args.as_json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": type(exc).__name__,
                        "constraint": constraint,
                        "sqlstate": sqlstate,
                    }
                )
            )
        else:
            print(f"Orphan integration cleanup blocked: {type(exc).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
