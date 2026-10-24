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
import base64
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from sqlalchemy import func, select

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import PreprodTestRun, User
from platform_workflow_input_guard import validate_never_started_cleanup_proof
from platform_cleanup_retained_matrix import (
    CONFIRMATION,
    EMAIL_PATTERN,
    EXPECTED_ORIGIN,
    MARKER_PATTERN,
    _uuid_list,
    add_control_email_argument,
    cleanup_manifest,
    emit_cleanup_completion_result,
    resolve_control_email,
    validate_cleanup_completion_result,
)


RUN_ROOT_BASE = Path("/opt/oldsparky/platform/shared/production-retained-matrix")
EXTERNAL_LOAD_EXPORT_BASE = Path("/tmp/old-sparky-production-retained-load-")
SUPPORTED_MODES = frozenset({"read-mix", "write-burst"})
LEGACY_EXTERNAL_VOTE_MODE = "external-vote"
MAX_COMPACT_USER_RECOVERY = 20_000
RUN_ID_PATTERN = r"[1-9][0-9]{0,31}"


def _external_load_export_path(load_run_id: str) -> Path:
    if not re.fullmatch(RUN_ID_PATTERN, load_run_id):
        raise RuntimeError("load run id must be numeric")
    return Path(f"{EXTERNAL_LOAD_EXPORT_BASE}{load_run_id}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load-run-id", required=True)
    add_control_email_argument(parser)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--never-started-proof-base64")
    args = parser.parse_args()
    args.control_email = resolve_control_email(args)
    return args


def _canonical_report_path(*, run_id: str, mode: str) -> str:
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        raise RuntimeError("load run id must be numeric")
    if mode not in SUPPORTED_MODES:
        raise RuntimeError("durable orphan cleanup supports only retained matrix modes")
    return str(RUN_ROOT_BASE / f"gha-{run_id}" / mode / f"{mode}.json")


def _legacy_external_vote_report_path(*, run_id: str) -> str:
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
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
    resolved_user_ids: list[str] | None = None,
    allow_already_cleaned: bool = False,
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
    if run.origin != EXPECTED_ORIGIN:
        raise RuntimeError("durable QA row is not from the canonical production origin")
    if stored.get("origin_class") != "production_origin":
        raise RuntimeError("durable QA report is not from the canonical production origin")
    marker = str(run.marker or stored.get("marker") or "")
    if not MARKER_PATTERN.fullmatch(marker) or stored.get("marker") != marker:
        raise RuntimeError("durable QA marker is not canonical")
    if (
        not allow_already_cleaned
        and (str(run.status or "").lower() == "cleaned" or run.cleanup_state)
    ):
        raise RuntimeError("durable QA row already records cleanup")
    user_ids = _uuid_list(
        stored.get("user_ids") if resolved_user_ids is None else resolved_user_ids,
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
        "_control_email": normalized_control_email,
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


async def _resolve_user_ids(db_session: Any, run: Any) -> tuple[list[str], bool]:
    """Resolve a complete user inventory, including compact progress reports."""

    stored = dict(run.report or {})
    marker = str(run.marker or stored.get("marker") or "")
    if not MARKER_PATTERN.fullmatch(marker):
        raise RuntimeError("durable QA marker is not canonical")
    raw_user_ids = stored.get("user_ids")
    if isinstance(raw_user_ids, list):
        return _uuid_list(raw_user_ids, field=f"{marker}.user_ids", allow_empty=False), False
    if not isinstance(raw_user_ids, dict):
        raise RuntimeError("durable QA user inventory is missing")
    if raw_user_ids.get("complete_inventory_in_final_report") is not True:
        raise RuntimeError("durable QA compact user inventory is not marked recoverable")
    expected_count = raw_user_ids.get("count")
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or not 1 <= expected_count <= MAX_COMPACT_USER_RECOVERY
    ):
        raise RuntimeError("durable QA compact user inventory count is invalid")
    first_ids = _uuid_list(
        raw_user_ids.get("first"),
        field=f"{marker}.user_ids.first",
        allow_empty=False,
    )
    last_ids = _uuid_list(
        raw_user_ids.get("last"),
        field=f"{marker}.user_ids.last",
        allow_empty=False,
    )
    sample_ids = set(first_ids) | set(last_ids)
    if len(sample_ids) > expected_count:
        raise RuntimeError("durable QA compact user inventory samples exceed its count")

    rows = (
        await db_session.execute(
            select(User.id, User.email).where(
                func.lower(User.email).like(f"{marker.lower()}-%@example.com")
            )
        )
    ).all()
    if len(rows) != expected_count:
        raise RuntimeError("durable QA compact user inventory count does not match production")
    recovered_ids: list[str] = []
    for row in rows:
        email = str(row.email or "").lower()
        match = EMAIL_PATTERN.fullmatch(email)
        if match is None or match.group("marker") != marker:
            raise RuntimeError("durable QA compact recovery found an invalid fixture email")
        recovered_ids.append(str(row.id))
    normalized_ids = _uuid_list(
        sorted(set(recovered_ids)),
        field=f"{marker}.user_ids",
        allow_empty=False,
    )
    if len(normalized_ids) != expected_count or not sample_ids.issubset(set(normalized_ids)):
        raise RuntimeError("durable QA compact recovery does not match its samples")
    return normalized_ids, True


async def clean_orphan(args: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("retained orphan cleanup must run as root")
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"cleanup requires --confirm {CONFIRMATION}")
    if not re.fullmatch(RUN_ID_PATTERN, args.load_run_id):
        raise RuntimeError("load run id must be numeric")
    run_root = RUN_ROOT_BASE / f"gha-{args.load_run_id}"
    if run_root.exists() or run_root.is_symlink():
        raise RuntimeError("exact retained run root still exists; use normal manifest cleanup")

    never_started_proof = None
    if args.never_started_proof_base64 is not None:
        encoded = args.never_started_proof_base64
        if (
            not isinstance(encoded, str)
            or not 1 <= len(encoded) <= 8192
            or re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", encoded) is None
        ):
            raise RuntimeError("never-started cleanup proof is malformed")
        try:
            raw_proof = base64.b64decode(encoded, validate=True)
            if base64.b64encode(raw_proof).decode("ascii") != encoded or len(raw_proof) > 6144:
                raise ValueError

            def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                values: dict[str, Any] = {}
                for key, value in pairs:
                    if key in values:
                        raise ValueError
                    values[key] = value
                return values

            proof_document = json.loads(
                raw_proof.decode("ascii"), object_pairs_hook=reject_duplicate_keys
            )
            never_started_proof = validate_never_started_cleanup_proof(
                proof_document, expected_load_run_id=args.load_run_id
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("never-started cleanup proof is invalid") from exc

    settings = get_settings()
    validate_platform_settings(settings)
    if settings.platform_environment.strip().lower() != "production":
        raise RuntimeError("retained orphan cleanup is forbidden outside production")
    if settings.platform_web_origin.rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("retained orphan cleanup requires the canonical origin")

    control_email = args.control_email.strip().lower()
    external_export = _external_load_export_path(args.load_run_id)
    if never_started_proof is not None and (
        external_export.exists() or external_export.is_symlink()
    ):
        raise RuntimeError("never-started cleanup proof conflicts with a retained-load export")
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
        if not rows and never_started_proof is not None:
            result = {
                "ok": True,
                "markers": 0,
                "users_deleted": 0,
                "tournaments_deleted": 0,
                "control_account_preserved": True,
                "remaining_users": 0,
                "remaining_tournaments": 0,
                "remaining_sessions": 0,
                "remaining_audit_logs": 0,
                "read_models": {
                    "keys_expected": 0,
                    "keys_deleted": 0,
                    "keys_remaining": 0,
                },
                "never_started": True,
                "load_run_id": never_started_proof["load_run_id"],
                "load_run_attempt": never_started_proof["load_run_attempt"],
                "load_source_sha": never_started_proof["head_sha"],
            }
            args.result_path.write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
            return result
        if not rows or len(rows) != 1:
            raise RuntimeError("exactly one durable QA row is required for the orphan run")
        if never_started_proof is not None:
            raise RuntimeError("never-started cleanup proof conflicts with a durable QA row")
        stored_user_ids = (rows[0].report or {}).get("user_ids")
        already_cleaned = (
            str(rows[0].status or "").lower() == "cleaned"
            or bool(rows[0].cleanup_state)
        )
        if already_cleaned:
            if not isinstance(stored_user_ids, list):
                raise RuntimeError(
                    "already-cleaned durable QA row lacks a complete user inventory"
                )
            resolved_user_ids = _uuid_list(
                stored_user_ids,
                field=f"{rows[0].marker}.user_ids",
                allow_empty=False,
            )
            was_compact = False
        else:
            resolved_user_ids, was_compact = await _resolve_user_ids(db_session, rows[0])
        manifest = build_durable_manifest(
            rows[0],
            load_run_id=args.load_run_id,
            control_email=control_email,
            resolved_user_ids=resolved_user_ids,
            allow_already_cleaned=already_cleaned,
        )
    recovered_user_ids = (
        {str(rows[0].marker): set(resolved_user_ids)} if was_compact else None
    )
    result = await cleanup_manifest(manifest, recovered_user_ids=recovered_user_ids)
    args.result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


async def _main() -> int:
    try:
        result = await clean_orphan(parse_args())
        validate_cleanup_completion_result(result)
        emit_cleanup_completion_result(result)
        return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
