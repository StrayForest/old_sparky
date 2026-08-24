#!/usr/bin/env python3
"""Delete exactly one retained production load-matrix run.

The input manifest is the matrix summary and its detailed reports. No query
selects all historical QA rows: every marker, report path, synthetic email,
and tournament description must match the selected GitHub run before a delete
is allowed.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import purge_deleted_media_metadata
from python_packages.platform_infra.models import (
    AuditLog,
    PlayerTournamentCommitment,
    PreprodTestRun,
    Tournament,
    TournamentParticipant,
    User,
    UserSession,
)
_HELPER_PATH = Path(__file__).resolve().with_name("platform_cleanup_live_user_qa.py")
_HELPER_SPEC = importlib.util.spec_from_file_location(
    "platform_cleanup_live_user_qa_retained_helpers", _HELPER_PATH
)
if _HELPER_SPEC is None or _HELPER_SPEC.loader is None:
    raise RuntimeError("live-QA cleanup helper module is unavailable")
_HELPER_MODULE = importlib.util.module_from_spec(_HELPER_SPEC)
sys.modules[_HELPER_SPEC.name] = _HELPER_MODULE
_HELPER_SPEC.loader.exec_module(_HELPER_MODULE)
_audit_subject_ids_for_scope = _HELPER_MODULE._audit_subject_ids_for_scope
_audit_scope_chunks = _HELPER_MODULE._audit_scope_chunks
_validate_audit_scope = _HELPER_MODULE._validate_audit_scope
_validate_tournament_graph_boundary = _HELPER_MODULE._validate_tournament_graph_boundary


CONFIRMATION = "DELETE-PRODUCTION-RETAINED-LOAD"
EXPECTED_ORIGIN = "https://old-sparky.com"
MAX_MATRIX_ROWS = 20
MARKER_PATTERN = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
EMAIL_PATTERN = re.compile(
    r"^(?P<marker>preprod[0-9]{12}[0-9a-f]{4})-[a-z0-9-]+@example\.com$"
)


def _regular_root_file(path: Path, *, root: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("manifest paths must be absolute")
    root_resolved = root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("manifest path escapes the selected run root") from exc
    resolved = path.resolve(strict=True)
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("manifest files must be root-owned regular 0600 files")
    return resolved


def _uuid_list(value: object, *, field: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a JSON list")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError(f"{field} must contain UUID strings")
        try:
            normalized = str(UUID(raw))
        except ValueError as exc:
            raise ValueError(f"{field} must contain canonical UUID strings") from exc
        if raw != normalized:
            raise ValueError(f"{field} must contain canonical UUID strings")
        result.append(raw)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicate IDs")
    return result


def load_matrix_manifest(
    summary_path: Path,
    *,
    run_root: Path,
    expected_control_email: str,
) -> dict[str, Any]:
    summary_path = _regular_root_file(summary_path, root=run_root)
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("matrix summary must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("matrix summary must be a JSON object")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_MATRIX_ROWS:
        raise ValueError("matrix summary must contain one to twenty rows")
    control_email = str(payload.get("control_email") or "").strip().lower()
    if control_email != expected_control_email.strip().lower():
        raise ValueError("matrix control email does not match the cleanup input")

    markers: set[str] = set()
    user_ids: set[str] = set()
    tournament_ids: set[str] = set()
    manifests: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("matrix rows must be JSON objects")
        result = row.get("result")
        if not isinstance(result, dict):
            raise ValueError("matrix row result is missing")
        marker = str(result.get("marker") or "")
        if not MARKER_PATTERN.fullmatch(marker) or marker in markers:
            raise ValueError("matrix row has an invalid or duplicate marker")
        report_raw = result.get("report_path") or row.get("report_path")
        if not isinstance(report_raw, str):
            raise ValueError("matrix row report path is missing")
        report_path = _regular_root_file(Path(report_raw), root=run_root)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("matrix detail report must contain valid JSON") from exc
        if not isinstance(report, dict):
            raise ValueError("matrix detail report must be a JSON object")
        if report.get("marker") != marker or report.get("report_path") != str(report_path):
            raise ValueError("matrix report identity does not match its summary row")
        if report.get("mode") != "scale" or report.get("origin") != EXPECTED_ORIGIN:
            raise ValueError("matrix report is not a canonical production scale report")
        row_users = _uuid_list(
            report.get("user_ids"), field=f"{marker}.user_ids", allow_empty=True
        )
        row_tournaments = _uuid_list(
            report.get("tournament_ids"),
            field=f"{marker}.tournament_ids",
            allow_empty=True,
        )
        if len(row_tournaments) > 1:
            raise ValueError("a matrix row may own at most one tournament")
        if int(row.get("synthetic_users", len(row_users))) != len(row_users):
            raise ValueError("matrix synthetic user count does not match its report")
        if markers.intersection({marker}) or user_ids.intersection(row_users) or tournament_ids.intersection(row_tournaments):
            raise ValueError("matrix reports must not share fixture identity")
        markers.add(marker)
        user_ids.update(row_users)
        tournament_ids.update(row_tournaments)
        manifests.append(
            {
                "marker": marker,
                "report_path": str(report_path),
                "user_ids": row_users,
                "tournament_ids": row_tournaments,
                "visibility": report.get("tournament_visibility"),
            }
        )
    if not user_ids:
        raise ValueError("matrix manifest contains no synthetic users")
    if payload.get("completed_tournaments") != len(rows):
        raise ValueError("matrix completed tournament count does not match its rows")
    return {
        "control_email": control_email,
        "markers": markers,
        "user_ids": user_ids,
        "tournament_ids": tournament_ids,
        "rows": manifests,
        "summary_path": str(summary_path),
    }


async def _count_ids(db_session, model: Any, ids: set[str]) -> int:
    if not ids:
        return 0
    return int(
        await db_session.scalar(
            select(func.count()).select_from(model).where(model.id.in_(ids))
        )
        or 0
    )


async def cleanup_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    validate_platform_settings(settings)
    if settings.platform_environment.strip().lower() != "production":
        raise RuntimeError("retained production cleanup is forbidden outside production")
    if settings.platform_web_origin.rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("retained production cleanup requires the canonical origin")

    markers = set(manifest["markers"])
    user_ids = set(manifest["user_ids"])
    tournament_ids = set(manifest["tournament_ids"])
    control_email = str(manifest["control_email"])
    async with session_factory()() as db_session:
        runs = list(
            (
                await db_session.scalars(
                    select(PreprodTestRun)
                    .where(PreprodTestRun.marker.in_(markers))
                    .with_for_update()
                )
            ).all()
        )
        if {run.marker for run in runs} != markers:
            raise RuntimeError("database run markers do not exactly match the matrix manifest")
        report_by_marker = {row["marker"]: row for row in manifest["rows"]}
        for run in runs:
            row = report_by_marker[run.marker]
            if run.origin != EXPECTED_ORIGIN or run.report_path != row["report_path"]:
                raise RuntimeError("database run provenance does not match the matrix report")
            stored_report = dict(run.report or {})
            if (
                stored_report.get("marker") != run.marker
                or set(stored_report.get("user_ids") or []) != set(row["user_ids"])
                or set(stored_report.get("tournament_ids") or []) != set(row["tournament_ids"])
            ):
                raise RuntimeError("database run report identity does not match the matrix report")

        user_rows = (
            await db_session.execute(
                select(User.id, User.email).where(User.id.in_(user_ids)).with_for_update()
            )
        ).all()
        if {str(row.id) for row in user_rows} != user_ids:
            raise RuntimeError("some synthetic users are missing or outside the manifest")
        for row in user_rows:
            email = str(row.email or "").lower()
            match = EMAIL_PATTERN.fullmatch(email)
            if match is None or match.group("marker") not in markers:
                raise RuntimeError("a manifest user email is not a marked example.com fixture")

        control_rows = (
            await db_session.execute(
                select(User.id).where(func.lower(User.email) == control_email)
            )
        ).all()
        if len(control_rows) != 1 or str(control_rows[0].id) in user_ids:
            raise RuntimeError("control account is missing or overlaps the synthetic fixture")
        control_user_id = str(control_rows[0].id)

        tournament_rows = (
            await db_session.execute(
                select(Tournament.id, Tournament.description, Tournament.organizer_user_id)
                .where(Tournament.id.in_(tournament_ids))
                .with_for_update()
            )
        ).all()
        if {str(row.id) for row in tournament_rows} != tournament_ids:
            raise RuntimeError("some matrix tournaments are missing or outside the manifest")
        expected_tournament_ids: set[str] = set()
        for row in manifest["rows"]:
            if not row["tournament_ids"]:
                continue
            tournament_id = row["tournament_ids"][0]
            tournament = next(item for item in tournament_rows if str(item.id) == tournament_id)
            expected_description = f"Large preprod QA tournament {row['marker']}."
            if (
                tournament.description != expected_description
                or str(tournament.organizer_user_id) not in set(row["user_ids"])
            ):
                raise RuntimeError("matrix tournament ownership or marker does not match")
            expected_tournament_ids.add(tournament_id)
        if expected_tournament_ids != tournament_ids:
            raise RuntimeError("matrix tournament manifest is not one-to-one with reports")

        outside_participants = await db_session.scalar(
            select(TournamentParticipant.tournament_id)
            .where(
                TournamentParticipant.user_id.in_(user_ids),
                TournamentParticipant.tournament_id.not_in(tournament_ids),
            )
            .limit(1)
        )
        outside_commitments = await db_session.scalar(
            select(PlayerTournamentCommitment.tournament_id)
            .where(
                PlayerTournamentCommitment.user_id.in_(user_ids),
                PlayerTournamentCommitment.tournament_id.not_in(tournament_ids),
            )
            .limit(1)
        )
        if outside_participants is not None or outside_commitments is not None:
            raise RuntimeError("synthetic users are linked to a tournament outside this run")

        await _validate_tournament_graph_boundary(db_session, tournament_ids)
        audit_subject_ids = await _audit_subject_ids_for_scope(
            db_session, user_ids, tournament_ids
        )
        await _validate_audit_scope(
            db_session,
            user_ids,
            audit_subject_ids,
            preserved_actor_ids={control_user_id},
        )

        media_deleted = await purge_deleted_media_metadata(
            db_session,
            owner_user_ids=user_ids,
            tournament_ids=tournament_ids,
        )
        audit_logs_deleted = 0
        for user_chunk in _audit_scope_chunks(user_ids):
            audit_result = await db_session.execute(
                delete(AuditLog).where(AuditLog.actor_user_id.in_(user_chunk))
            )
            audit_logs_deleted += int(audit_result.rowcount or 0)
        for subject_type, subject_ids in audit_subject_ids.items():
            for subject_chunk in _audit_scope_chunks(subject_ids):
                audit_result = await db_session.execute(
                    delete(AuditLog).where(
                        AuditLog.subject_type == subject_type,
                        AuditLog.subject_id.in_(subject_chunk),
                        AuditLog.actor_user_id.is_(None),
                    )
                )
                audit_logs_deleted += int(audit_result.rowcount or 0)
        tournament_result = (
            await db_session.execute(delete(Tournament).where(Tournament.id.in_(tournament_ids)))
            if tournament_ids
            else None
        )
        await db_session.flush()
        user_result = await db_session.execute(delete(User).where(User.id.in_(user_ids)))
        deleted_at = datetime.now(UTC).isoformat()
        cleanup_state = {
            "ok": True,
            "cleaned_at": deleted_at,
            "cleaned_by": "platform_cleanup_retained_matrix.py",
            "control_account_preserved": control_email,
            "users_deleted": int(user_result.rowcount or 0),
            "tournaments_deleted": int(tournament_result.rowcount or 0) if tournament_result else 0,
            "audit_logs_deleted": audit_logs_deleted,
            "media_metadata_deleted": int(media_deleted),
        }
        for run in runs:
            run.cleanup_state = cleanup_state
            run.status = "cleaned"
        await db_session.commit()

    async with session_factory()() as verify_session:
        remaining_users = await _count_ids(verify_session, User, user_ids)
        remaining_tournaments = await _count_ids(verify_session, Tournament, tournament_ids)
        remaining_sessions = int(
            await verify_session.scalar(
                select(func.count()).select_from(UserSession).where(UserSession.user_id.in_(user_ids))
            )
            or 0
        )
        remaining_audit_ids: set[str] = set()
        for user_chunk in _audit_scope_chunks(user_ids):
            remaining_audit_ids.update(
                str(audit_id)
                for audit_id in (
                    await verify_session.scalars(
                        select(AuditLog.id).where(
                            AuditLog.actor_user_id.in_(user_chunk)
                        )
                    )
                ).all()
            )
        for subject_type, subject_ids in audit_subject_ids.items():
            for subject_chunk in _audit_scope_chunks(subject_ids):
                remaining_audit_ids.update(
                    str(audit_id)
                    for audit_id in (
                        await verify_session.scalars(
                            select(AuditLog.id).where(
                                AuditLog.subject_type == subject_type,
                                AuditLog.subject_id.in_(subject_chunk),
                                AuditLog.actor_user_id.is_(None),
                            )
                        )
                    ).all()
                )
        remaining_audit = len(remaining_audit_ids)
        control_remaining = int(
            await verify_session.scalar(
                select(func.count()).select_from(User).where(func.lower(User.email) == control_email)
            )
            or 0
        )
        if any((remaining_users, remaining_tournaments, remaining_sessions, remaining_audit)):
            raise RuntimeError("retained matrix cleanup left fixture rows behind")
        if control_remaining != 1:
            raise RuntimeError("retained matrix cleanup did not preserve the control account")

    return {
        "ok": True,
        "markers": len(markers),
        "users_deleted": len(user_ids),
        "tournaments_deleted": len(tournament_ids),
        "control_account_preserved": control_email,
        "remaining_users": 0,
        "remaining_tournaments": 0,
        "remaining_sessions": 0,
        "remaining_audit_logs": 0,
    }


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup one exact production retained load matrix.")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--control-email", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("retained matrix cleanup must run as root")
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"cleanup requires --confirm {CONFIRMATION}")
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", args.control_email):
        raise RuntimeError("control email is invalid")
    manifest = load_matrix_manifest(
        args.summary,
        run_root=args.run_root,
        expected_control_email=args.control_email,
    )
    result = await cleanup_manifest(manifest)
    args.result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    finally:
        asyncio.run(dispose_engine())


if __name__ == "__main__":
    raise SystemExit(main())
