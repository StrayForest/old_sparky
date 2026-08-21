#!/usr/bin/env python3
"""Bounded reconciliation before exact automated live-QA cleanup.

The only discoverable object is the one tournament whose description is the
exact acceptance marker.  Its full owner/reference graph is validated against
an augmented exact inventory before that single ID is atomically recorded.
This tool never deletes rows and never performs marker-wide cleanup.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

from sqlalchemy import select

import platform_cleanup_live_user_qa as cleanup_tool
import platform_live_qa_guard as guard
from python_packages.platform_infra.config import (
    get_settings,
    validate_platform_settings,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import Tournament


EXPECTED_ORIGIN = "https://old-sparky.com"


class RecoveryError(RuntimeError):
    """A safe operator-facing recovery refusal."""


def _validate_runtime() -> None:
    settings = get_settings()
    try:
        validate_platform_settings(settings)
    except RuntimeError as exc:
        raise RecoveryError("platform runtime configuration is unsafe") from exc
    if (
        settings.platform_environment.strip().lower() != "production"
        or settings.platform_web_origin != EXPECTED_ORIGIN
    ):
        raise RecoveryError(
            "automated recovery requires the canonical production runtime"
        )


async def reconcile_marker_tournament(
    *,
    marker: str,
    inventory_path: Path,
) -> bool:
    try:
        inventory = cleanup_tool.load_inventory(
            inventory_path,
            expected_marker=marker,
        )
    except (OSError, ValueError) as exc:
        raise RecoveryError("retained exact inventory is invalid") from exc
    expected_description = f"Accelerated live browser acceptance {marker}."
    async with session_factory()() as db_session:
        rows = list(
            (
                await db_session.scalars(
                    select(Tournament).where(
                        Tournament.description == expected_description
                    )
                )
            ).all()
        )
        if len(rows) > 1:
            raise RecoveryError("marker tournament scope is ambiguous")
        if not rows:
            await cleanup_tool._validate_exact_scope(db_session, inventory)
            await db_session.rollback()
            return False
        tournament_id = str(rows[0].id)
        if tournament_id in inventory.tournament_ids:
            await cleanup_tool._validate_exact_scope(db_session, inventory)
            await db_session.rollback()
            return False
        if (
            len(inventory.tournament_ids)
            >= cleanup_tool.MAX_INVENTORY_IDS["tournament_ids"]
        ):
            raise RecoveryError("retained tournament inventory is full")
        augmented = cleanup_tool.CleanupInventory(
            marker=inventory.marker,
            user_ids=inventory.user_ids,
            tournament_ids=(*inventory.tournament_ids, tournament_id),
            media_ids=inventory.media_ids,
        )
        # This locks and validates owner, user links, tournament graph, media,
        # and audit boundaries before the newly discovered exact ID is trusted.
        await cleanup_tool._validate_exact_scope(db_session, augmented)
        await db_session.rollback()
    guard._publish_private_json(
        inventory_path,
        {
            "version": 1,
            "marker": augmented.marker,
            "user_ids": list(augmented.user_ids),
            "tournament_ids": list(augmented.tournament_ids),
            "media_ids": list(augmented.media_ids),
        },
    )
    return True


async def _run(args: argparse.Namespace) -> int:
    _validate_runtime()
    appended = await reconcile_marker_tournament(
        marker=args.marker,
        inventory_path=args.inventory,
    )
    print(
        "Automated live QA recovery inventory validated"
        + (" and one exact tournament ID was recorded." if appended else ".")
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile one exact marker tournament before live-QA cleanup."
    )
    parser.add_argument("--marker", required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.confirm != "recover-live-user-qa":
        parser.error("recovery requires --confirm recover-live-user-qa")
    if not cleanup_tool.MARKER_PATTERN.fullmatch(args.marker):
        parser.error("marker is invalid")
    if os.geteuid() != 0:
        parser.error("automated recovery requires root")

    async def run() -> int:
        try:
            return await _run(args)
        finally:
            await dispose_engine()

    try:
        return asyncio.run(run())
    except RecoveryError as exc:
        print(f"Automated live QA recovery refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("Automated live QA recovery failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
