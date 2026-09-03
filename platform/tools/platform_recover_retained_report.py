#!/usr/bin/env python3
"""Recover an interrupted retained load report from its durable QA row.

The production retained-load supervisor updates ``PreprodTestRun.report`` after
each material setup phase.  If the GitHub SSH client is canceled, the final
JSON files may never be copied to the run directory even though the database
still has the exact fixture identity.  This helper reconstructs only the
read-mix, write-burst, or transport-specific external-vote report and compact
summary for one exact ``gha-<run-id>`` root;
the normal exact cleanup validator remains the deletion authority.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import select

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import PreprodTestRun, User


EXPECTED_ORIGIN = "https://old-sparky.com"
MARKER_PATTERN = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
SYNTHETIC_EMAIL_PATTERN = re.compile(
    r"^(?P<marker>preprod[0-9]{12}[0-9a-f]{4})-[a-z0-9-]+@example\.com$"
)
RUN_ROOT_PATTERN = re.compile(
    r"^/opt/oldsparky/platform/shared/production-retained-matrix/gha-(?P<run_id>[0-9]+)$"
)
RECOVERY_MODES = ("read-mix", "write-burst", "external-vote")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--load-run-id", required=True)
    parser.add_argument("--control-email", required=True)
    parser.add_argument(
        "--mode",
        choices=RECOVERY_MODES,
        default="write-burst",
    )
    return parser.parse_args()


def _regular_file(path: Path, *, required: bool) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"required recovery file is missing: {path}") from None
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
        raise RuntimeError(f"recovery file must be a root-owned regular 0600 file: {path}")
    # The interrupted root supervisor can leave a report created by Python's
    # default 0644 mode before its final chmod pass. Tighten that exact file
    # before reading it; never relax permissions or follow a link.
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        path.chmod(0o600)
    return True


def _uuid_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeError(f"{field} must be a JSON list")
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise RuntimeError(f"{field} must contain UUID strings")
        try:
            normalized = str(UUID(raw))
        except ValueError as exc:
            raise RuntimeError(f"{field} contains an invalid UUID") from exc
        if raw != normalized:
            raise RuntimeError(f"{field} must contain canonical UUID strings")
        result.append(raw)
    if len(result) != len(set(result)):
        raise RuntimeError(f"{field} must not contain duplicate UUIDs")
    return result


async def _recover_progress_user_ids(
    db_session: Any,
    *,
    stored_report: dict[str, Any],
    marker: str,
) -> list[str]:
    """Rebuild an exact user inventory after an interrupted progress checkpoint.

    Progress checkpoints intentionally retain only a bounded ID sample. The
    marker-scoped email query is the recovery path when a process is killed
    before its final full report is written; it never broadens cleanup beyond
    the canonical synthetic fixture namespace.
    """

    raw_ids = stored_report.get("user_ids")
    if isinstance(raw_ids, list):
        return _uuid_list(raw_ids, field="user_ids")
    if not isinstance(raw_ids, dict):
        raise RuntimeError("durable QA row contains no recoverable user inventory")
    progress = stored_report.get("fixture_progress")
    if not isinstance(progress, dict) or progress.get("marker") != marker:
        raise RuntimeError("durable QA progress identity does not match its marker")
    expected_count = int(raw_ids.get("count") or progress.get("synthetic_user_count") or 0)
    if expected_count <= 0:
        raise RuntimeError("durable QA progress contains no synthetic user count")

    rows = list(
        (
            await db_session.execute(
                select(User.id, User.email).where(
                    User.email.like(f"{marker}-%@example.com")
                )
            )
        ).all()
    )
    user_ids: list[str] = []
    for row in rows:
        email = str(row.email or "").lower()
        match = SYNTHETIC_EMAIL_PATTERN.fullmatch(email)
        if match is None or match.group("marker") != marker:
            raise RuntimeError("durable QA recovery found an invalid synthetic email")
        user_ids.append(str(row.id))
    if len(user_ids) != expected_count or len(user_ids) != len(set(user_ids)):
        raise RuntimeError("durable QA recovery user count does not match its progress checkpoint")
    return _uuid_list(sorted(user_ids), field="user_ids")


def _write_root_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing to write through symlink: {path}")
    temporary = path.with_name(f".{path.name}.recovery-tmp")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(f"recovery temporary path already exists: {temporary}")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def build_recovered_summary(
    report: dict[str, Any],
    *,
    marker: str,
    report_path: Path,
    load_run_id: str,
    control_email: str,
) -> dict[str, Any]:
    user_ids = _uuid_list(report.get("user_ids"), field="user_ids")
    tournament_ids = _uuid_list(report.get("tournament_ids"), field="tournament_ids")
    performance = report.get("performance") if isinstance(report.get("performance"), dict) else {}
    http_client = performance.get("http_client") if isinstance(performance.get("http_client"), dict) else {}
    http_overall = http_client.get("overall") if isinstance(http_client.get("overall"), dict) else {}
    bottleneck = performance.get("bottleneck_summary") if isinstance(performance.get("bottleneck_summary"), dict) else {}
    mode = str(report.get("mode") or "write-burst")
    write_burst = report.get("write_burst") if isinstance(report.get("write_burst"), dict) else {}
    selection = str(write_burst.get("selection") or "all")
    planned_tournaments = (
        1
        if mode == "read-mix"
        else {"all": 26, "single-join": 3, "single-ready": 3, "multi-staggered": 20}.get(selection, 0)
    )
    planned_users = int(report.get("requested_users") or 10000)
    return {
        "mode": mode,
        "target_sha": "recovered-from-durable-run",
        "github_run_id": int(load_run_id),
        "control_email": control_email,
        "planned_tournaments": planned_tournaments,
        "completed_tournaments": len(tournament_ids),
        "planned_users": planned_users,
        "completed_users": len(user_ids),
        "passed": False,
        "recovered": True,
        "write_burst": {
            "profile": write_burst.get("profile"),
            "selection": selection,
            "users_per_tournament": write_burst.get("users_per_tournament"),
            "time_scale": write_burst.get("time_scale"),
        } if write_burst else None,
        "read_mix": report.get("read_mix") if mode == "read-mix" else None,
        "performance_summary": {
            "worst_http_p95_ms": http_overall.get("p95_ms"),
            "worst_http_p99_ms": http_overall.get("p99_ms"),
            "bottleneck_classes": bottleneck.get("likely_bottleneck_classes", []),
            "resource_flags": bottleneck.get("resource_flags", {}),
        },
        "rows": [{
            "synthetic_users": len(user_ids),
            "report_path": str(report_path),
            "result": {
                "passed": False,
                "marker": marker,
                "report_path": str(report_path),
            },
        }],
    }


def _recovery_paths(run_root: Path, mode: str) -> tuple[Path, Path]:
    """Return the report and summary paths for a supervisor profile."""

    if mode not in RECOVERY_MODES:
        raise RuntimeError("unsupported retained report recovery mode")
    profile_root = run_root / mode
    return profile_root / f"{mode}.json", profile_root / "matrix-summary.json"


def _expected_stored_mode(mode: str) -> str:
    if mode not in RECOVERY_MODES:
        raise RuntimeError("unsupported retained report recovery mode")
    return "write-burst" if mode == "external-vote" else mode


def _persist_recovered_identity(
    run: Any,
    *,
    stored_report: dict[str, Any],
    user_ids: list[str],
    tournament_ids: list[str],
) -> dict[str, Any]:
    """Make the durable row usable by the normal identity-checked cleanup."""

    recovered = dict(stored_report)
    recovered["user_ids"] = user_ids
    recovered["tournament_ids"] = tournament_ids
    recovered["recovered_from_preprod_test_run"] = str(run.id)
    recovered["recovered_at"] = datetime.now(UTC).isoformat()
    run.report = recovered
    return recovered


async def recover(args: argparse.Namespace) -> dict[str, Any]:
    run_root = args.run_root
    expected_run = RUN_ROOT_PATTERN.fullmatch(str(run_root))
    if expected_run is None or expected_run.group("run_id") != args.load_run_id:
        raise RuntimeError("run root must be the exact production gha load-run root")
    if not run_root.is_absolute() or run_root.is_symlink() or not run_root.is_dir():
        raise RuntimeError("selected production load run root is not a real directory")
    root_metadata = run_root.stat()
    if root_metadata.st_uid != 0 or stat.S_IMODE(root_metadata.st_mode) != 0o700:
        raise RuntimeError("selected production load run root must be root-owned mode 0700")
    control_email = args.control_email.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", control_email):
        raise RuntimeError("control email is invalid")

    settings = get_settings()
    validate_platform_settings(settings)
    if settings.platform_environment.strip().lower() != "production":
        raise RuntimeError("retained report recovery is forbidden outside production")
    if settings.platform_web_origin.rstrip("/") != EXPECTED_ORIGIN:
        raise RuntimeError("retained report recovery requires the canonical origin")

    report_path, summary_path = _recovery_paths(run_root, args.mode)
    generic_summary_path = run_root / "matrix-summary.json"

    async with session_factory()() as db_session:
        rows = list(
            (
                await db_session.scalars(
                    select(PreprodTestRun).where(
                        PreprodTestRun.report_path == str(report_path)
                    )
                )
            ).all()
        )
        if len(rows) != 1:
            raise RuntimeError(
                "database must contain exactly one PreprodTestRun for the exact retained report path"
            )
        run = rows[0]
        stored = dict(run.report or {})
        marker = str(stored.get("marker") or run.marker or "")
        if not MARKER_PATTERN.fullmatch(marker) or run.marker != marker:
            raise RuntimeError("durable QA marker is not a canonical retained-load marker")
        if run.origin != EXPECTED_ORIGIN or stored.get("origin") != EXPECTED_ORIGIN:
            raise RuntimeError("durable QA provenance is not the canonical production origin")
        if stored.get("mode") != _expected_stored_mode(args.mode):
            raise RuntimeError("durable QA row mode does not match the selected retained profile")
        user_ids = await _recover_progress_user_ids(
            db_session,
            stored_report=stored,
            marker=marker,
        )
        tournament_ids = _uuid_list(stored.get("tournament_ids"), field="tournament_ids")
        stored = _persist_recovered_identity(
            run,
            stored_report=stored,
            user_ids=user_ids,
            tournament_ids=tournament_ids,
        )
        await db_session.commit()
    if not user_ids:
        raise RuntimeError("durable QA row contains no synthetic users to recover")
    if str(stored.get("report_path") or run.report_path) != str(report_path):
        raise RuntimeError("durable report path does not match the selected run root")

    if _regular_file(report_path, required=False):
        try:
            existing = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing retained report is not valid JSON") from exc
        if not isinstance(existing, dict):
            raise RuntimeError("existing retained report must be a JSON object")
        if (
            existing.get("marker") != marker
            or existing.get("report_path") != str(report_path)
            or set(_uuid_list(existing.get("user_ids"), field="existing user_ids"))
            != set(user_ids)
            or set(_uuid_list(existing.get("tournament_ids"), field="existing tournament_ids"))
            != set(tournament_ids)
        ):
            raise RuntimeError("existing retained report does not match durable QA identity")
        report = existing
    else:
        report = stored
        report["marker"] = marker
        report["origin"] = EXPECTED_ORIGIN
        report["mode"] = args.mode
        report["report_path"] = str(report_path)
        report["user_ids"] = user_ids
        report["tournament_ids"] = tournament_ids
        report["passed"] = False
        report["recovered_from_preprod_test_run"] = str(run.id)
        report["recovered_at"] = stored.get("recovered_at")
        _write_root_json(report_path, report)

    if _regular_file(summary_path, required=False):
        try:
            existing_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("existing retained summary is not valid JSON") from exc
        if not isinstance(existing_summary, dict) or not existing_summary.get("rows"):
            raise RuntimeError("existing retained summary is not an exact cleanup manifest")
    else:
        summary = build_recovered_summary(
            report,
            marker=marker,
            report_path=report_path,
            load_run_id=args.load_run_id,
            control_email=control_email,
        )
        _write_root_json(summary_path, summary)

    if _regular_file(generic_summary_path, required=False):
        try:
            generic = json.loads(generic_summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("generic supervisor summary is not valid JSON") from exc
        if not (
            isinstance(generic, dict)
            and generic.get("error") == "production_external_load_summary_missing_or_ambiguous"
            and str(generic.get("github_run_id")) == args.load_run_id
        ):
            raise RuntimeError("refusing to remove a non-generic root summary")
        generic_summary_path.unlink()

    print(f"RECOVERED_RETAINED_REPORT={report_path}")
    print(f"RECOVERED_RETAINED_SUMMARY={summary_path}")
    print(f"RECOVERED_MARKER={marker}")
    print(f"RECOVERED_USERS={len(user_ids)}")
    print(f"RECOVERED_TOURNAMENTS={len(tournament_ids)}")
    return {
        "marker": marker,
        "users": len(user_ids),
        "tournaments": len(tournament_ids),
    }


async def _async_main(args: argparse.Namespace) -> int:
    try:
        await recover(args)
    finally:
        await dispose_engine()
    return 0


def main() -> int:
    return asyncio.run(_async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
