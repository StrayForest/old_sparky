#!/usr/bin/env python3
"""Clean one exact retained-load fixture whose report directory is gone.

An interrupted supervisor can publish its complete fixture inventory to the
durable ``PreprodTestRun`` row and then lose the filesystem manifest.  This
tool reconstructs only that one manifest from the row and delegates all
ownership, provenance, graph-boundary, and deletion checks to the normal
retained-matrix cleanup implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from sqlalchemy import select

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import PreprodTestRun
from platform_cleanup_retained_matrix import (
    CONFIRMATION,
    EXPECTED_ORIGIN,
    MARKER_PATTERN,
    _uuid_list,
    cleanup_manifest,
)


RUN_ROOT_BASE = Path("/opt/oldsparky/platform/shared/production-retained-matrix")
SUPPORTED_MODES = frozenset({"read-mix", "write-burst"})
LEGACY_EXTERNAL_VOTE_MODE = "external-vote"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load-run-id", required=True)
    parser.add_argument("--control-email", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    return parser.parse_args()


def _canonical_report_path(*, run_id: str, mode: str) -> str:
    if not re.fullmatch(r"[0-9]+", run_id):
        raise RuntimeError("load run id must be numeric")
    if mode not in SUPPORTED_MODES:
        raise RuntimeError("durable orphan cleanup supports only retained matrix modes")
    return str(RUN_ROOT_BASE / f"gha-{run_id}" / mode / f"{mode}.json")


def _legacy_external_vote_report_path(*, run_id: str) -> str:
    if not re.fullmatch(r"[0-9]+", run_id):
        raise RuntimeError("load run id must be numeric")
    return str(
        RUN_ROOT_BASE
        / f"gha-{run_id}"
        / LEGACY_EXTERNAL_VOTE_MODE
        / f"{LEGACY_EXTERNAL_VOTE_MODE}.json"
    )


def build_durable_manifest(
    run: Any,
    *,
    load_run_id: str,
    control_email: str,
) -> dict[str, Any]:
    """Build the exact cleanup manifest from one durable QA row."""

    stored = dict(run.report or {})
    mode = str(stored.get("mode") or "")
    canonical_report_path = _canonical_report_path(run_id=load_run_id, mode=mode)
    legacy_report_path = _legacy_external_vote_report_path(run_id=load_run_id)
    stored_run_path = str(run.report_path or "")
    if (
        mode == "write-burst"
        and stored_run_path == legacy_report_path
        and isinstance(stored.get("external_vote"), dict)
    ):
        # The first external-vote supervisor stored a write-burst report below
        # its transport-specific directory. Keep this narrow compatibility
        # path so an interrupted legacy run can be cleaned from its durable
        # inventory after the filesystem report is gone.
        report_path = legacy_report_path
    else:
        report_path = canonical_report_path
    if (
        stored_run_path != report_path
        or str(stored.get("report_path") or "") != report_path
    ):
        raise RuntimeError("durable QA report path does not match the exact load run")
    if str(run.origin or "").rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("durable QA row is not from the canonical production origin")
    if str(stored.get("origin") or "").rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("durable QA report is not from the canonical production origin")
    if str(stored.get("request_origin") or "").rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("durable QA request origin is not canonical")
    marker = str(run.marker or stored.get("marker") or "")
    if not MARKER_PATTERN.fullmatch(marker) or stored.get("marker") != marker:
        raise RuntimeError("durable QA marker is not canonical")
    if str(run.status or "").lower() == "cleaned" or run.cleanup_state:
        raise RuntimeError("durable QA row already records cleanup")
    user_ids = _uuid_list(
        stored.get("user_ids"),
        field=f"{marker}.user_ids",
        allow_empty=False,
    )
    tournament_ids = _uuid_list(
        stored.get("tournament_ids"),
        field=f"{marker}.tournament_ids",
        allow_empty=True,
    )
    if mode == "read-mix" and len(tournament_ids) > 1:
        raise RuntimeError("read-mix durable QA row owns too many tournaments")
    if mode == "write-burst" and len(tournament_ids) > 64:
        raise RuntimeError("write-burst durable QA row owns too many tournaments")
    normalized_control_email = control_email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", normalized_control_email):
        raise RuntimeError("control email is invalid")
    return {
        "control_email": normalized_control_email,
        "mode": mode,
        "markers": {marker},
        "user_ids": set(user_ids),
        "tournament_ids": set(tournament_ids),
        "rows": [{
            "marker": marker,
            "report_path": report_path,
            "user_ids": user_ids,
            "tournament_ids": tournament_ids,
            "visibility": stored.get("tournament_visibility"),
            "request_origin": EXPECTED_ORIGIN,
        }],
        "summary_path": report_path,
    }


async def clean_orphan(args: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("retained orphan cleanup must run as root")
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"cleanup requires --confirm {CONFIRMATION}")
    if not re.fullmatch(r"[0-9]+", args.load_run_id):
        raise RuntimeError("load run id must be numeric")
    run_root = RUN_ROOT_BASE / f"gha-{args.load_run_id}"
    if run_root.exists() or run_root.is_symlink():
        raise RuntimeError("exact retained run root still exists; use normal manifest cleanup")

    settings = get_settings()
    validate_platform_settings(settings)
    if settings.platform_environment.strip().lower() != "production":
        raise RuntimeError("retained orphan cleanup is forbidden outside production")
    if settings.platform_web_origin.rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("retained orphan cleanup requires the canonical origin")

    control_email = args.control_email.strip().lower()
    report_paths = {
        _canonical_report_path(run_id=args.load_run_id, mode=mode)
        for mode in SUPPORTED_MODES
    }
    report_paths.add(_legacy_external_vote_report_path(run_id=args.load_run_id))
    async with session_factory()() as db_session:
        rows = list(
            (
                await db_session.scalars(
                    select(PreprodTestRun).where(
                        PreprodTestRun.report_path.in_(report_paths)
                    )
                )
            ).all()
        )
        if len(rows) != 1:
            raise RuntimeError("exactly one durable QA row is required for the orphan run")
        manifest = build_durable_manifest(
            rows[0],
            load_run_id=args.load_run_id,
            control_email=control_email,
        )
    result = await cleanup_manifest(manifest)
    args.result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


async def _main() -> int:
    try:
        result = await clean_orphan(parse_args())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
