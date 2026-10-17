#!/usr/bin/env python3
"""Prepare a retained Ready Check vote fixture for an external HTTP runner.

Only deterministic fixture setup runs on the origin.  The returned manifest is
temporary credential material and must be copied only to the external runner's
private temporary directory; it is never part of a report or an artifact.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Any, Iterator

try:
    from platform_production_qa import ProductionQa, QaFailure, dispose_engine, load_env_file
except ModuleNotFoundError as exc:  # Imported as ``tools.*`` by focused tests.
    if exc.name != "platform_production_qa":
        raise
    from tools.platform_production_qa import ProductionQa, QaFailure, dispose_engine, load_env_file


PUBLIC_ORIGIN = "https://old-sparky.com"
LOCAL_API_ORIGIN = "http://127.0.0.1:8010"
MIN_USERS_PER_TOURNAMENT = 14
MAX_USERS_PER_TOURNAMENT = 500
MAX_TOURNAMENTS = 40
MAX_USERS = MAX_USERS_PER_TOURNAMENT * MAX_TOURNAMENTS
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
FIXTURE_TIMING_STAGES = (
    "bulk_user_creation",
    "permission_grants",
    "tournament_api_setup",
    "ready_check_starts",
    "fixture_finalization",
)
MAX_FIXTURE_STAGE_DURATION_MS = 180 * 60 * 1000


class FixtureStageTimings:
    """Keep one monotonic duration for each bounded fixture-setup stage."""

    def __init__(self) -> None:
        self._stages: dict[str, dict[str, float | bool]] = {}

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        if name not in FIXTURE_TIMING_STAGES or name in self._stages:
            raise ValueError("external fixture timing stage is invalid")
        started = time.perf_counter()
        completed = False
        try:
            yield
            completed = True
        finally:
            elapsed_seconds = max(0.0, time.perf_counter() - started)
            self._stages[name] = {
                "duration_ms": round(elapsed_seconds * 1000, 3),
                "completed": completed,
            }

    def snapshot(self) -> dict[str, dict[str, float | bool]]:
        return {name: dict(stage) for name, stage in self._stages.items()}


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_fixture_timing_receipt(path: Path, timings: FixtureStageTimings) -> None:
    """Persist only bounded phase durations, separate from credential reports."""

    atomic_write_json(
        path,
        {
            "schema": 1,
            "clock": "time.perf_counter",
            "stages": timings.snapshot(),
        },
        mode=0o600,
    )


def read_fixture_timing_projection(
    path: Path,
    *,
    qa_succeeded: bool,
) -> dict[str, Any]:
    """Read only the fixed, non-sensitive timing fields for the run summary."""

    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if qa_succeeded:
            raise ValueError("successful fixture has no timing receipt") from None
        return {"schema": 1, "clock": "time.perf_counter", "stages": {}}
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 1
            or before.st_size > 16 * 1024
        ):
            raise ValueError("fixture timing receipt identity is invalid")
        raw = bytearray()
        while len(raw) <= 16 * 1024:
            chunk = os.read(descriptor, min(4096, 16 * 1024 + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or len(raw) != after.st_size
            or len(raw) > 16 * 1024
        ):
            raise ValueError("fixture timing receipt changed while reading")
    finally:
        os.close(descriptor)

    def reject_nonfinite(value: str) -> None:
        raise ValueError("fixture timing duration is not finite")

    try:
        payload = json.loads(raw, parse_constant=reject_nonfinite)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("fixture timing receipt is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "clock", "stages"}
        or type(payload.get("schema")) is not int
        or payload["schema"] != 1
        or payload.get("clock") != "time.perf_counter"
        or not isinstance(payload.get("stages"), dict)
    ):
        raise ValueError("fixture timing receipt schema is invalid")

    stages = payload["stages"]
    if set(stages) - set(FIXTURE_TIMING_STAGES):
        raise ValueError("fixture timing receipt has an unknown stage")
    safe_stages: dict[str, dict[str, float | bool]] = {}
    for stage_name in FIXTURE_TIMING_STAGES:
        if stage_name not in stages:
            continue
        stage = stages[stage_name]
        if not isinstance(stage, dict) or set(stage) != {"duration_ms", "completed"}:
            raise ValueError("fixture timing stage schema is invalid")
        duration_ms = stage["duration_ms"]
        completed = stage["completed"]
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, (int, float))
            or duration_ms < 0
            or duration_ms > MAX_FIXTURE_STAGE_DURATION_MS
            or (isinstance(duration_ms, float) and not math.isfinite(duration_ms))
            or not isinstance(completed, bool)
        ):
            raise ValueError("fixture timing stage value is invalid")
        safe_stages[stage_name] = {
            "duration_ms": duration_ms,
            "completed": completed,
        }

    if qa_succeeded and (
        tuple(safe_stages) != FIXTURE_TIMING_STAGES
        or any(stage["completed"] is not True for stage in safe_stages.values())
    ):
        raise ValueError("successful fixture timing is incomplete")
    return {"schema": 1, "clock": "time.perf_counter", "stages": safe_stages}


def project_fixture_timing_summary(
    receipt_path: Path,
    source_summary_path: Path,
    destination_path: Path,
    *,
    qa_succeeded: bool,
) -> None:
    """Build the fixed safe matrix export inside the root-private run directory."""

    timing = read_fixture_timing_projection(receipt_path, qa_succeeded=qa_succeeded)
    try:
        summary_descriptor = os.open(
            source_summary_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        payload: dict[str, Any] = {}
    else:
        try:
            summary_stat = os.fstat(summary_descriptor)
            if (
                not stat.S_ISREG(summary_stat.st_mode)
                or summary_stat.st_nlink != 1
                or summary_stat.st_uid != os.geteuid()
                or stat.S_IMODE(summary_stat.st_mode) != 0o600
                or summary_stat.st_size < 1
                or summary_stat.st_size > 64 * 1024
            ):
                raise ValueError("matrix summary identity is invalid")
            raw = bytearray()
            while len(raw) <= 64 * 1024:
                chunk = os.read(summary_descriptor, min(8192, 64 * 1024 + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(summary_descriptor)
            if (
                summary_stat.st_dev != after.st_dev
                or summary_stat.st_ino != after.st_ino
                or summary_stat.st_size != after.st_size
                or len(raw) != after.st_size
                or len(raw) > 64 * 1024
            ):
                raise ValueError("matrix summary changed while reading")
        finally:
            os.close(summary_descriptor)
        try:
            loaded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            loaded = {}
        payload = loaded if isinstance(loaded, dict) else {}

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    safe_rows = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        synthetic_users = row.get("synthetic_users")
        if (
            isinstance(synthetic_users, bool)
            or not isinstance(synthetic_users, int)
            or synthetic_users < 0
            or synthetic_users > 1_000_000_000
        ):
            synthetic_users = 0
        safe_rows.append(
            {
                "synthetic_users": synthetic_users,
                "result": {"passed": result.get("passed") is True},
            }
        )

    def safe_count(key: str) -> int:
        value = payload.get(key)
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 1_000_000_000
            else 0
        )

    mode = payload.get("mode")
    if not isinstance(mode, str) or mode not in {"scale", "read-mix", "write-burst"}:
        mode = "other"
    safe_summary = {
        "schema": 1,
        "mode": mode,
        "control_account_preserved": payload.get("control_account_preserved") is True,
        "planned_tournaments": safe_count("planned_tournaments"),
        "completed_tournaments": safe_count("completed_tournaments"),
        "planned_users": safe_count("planned_users"),
        "completed_users": safe_count("completed_users"),
        "passed": payload.get("passed") is True,
        "rows": safe_rows,
        "external_vote_fixture": {"fixture_setup_timings": timing},
    }
    try:
        parent_stat = os.lstat(destination_path.parent)
    except OSError as exc:
        raise ValueError("matrix export destination is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_ISLNK(parent_stat.st_mode)
        or parent_stat.st_uid != os.geteuid()
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
    ):
        raise ValueError("matrix export destination identity is invalid")
    atomic_write_json(destination_path, safe_summary, mode=0o600)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare one exact retained Ready Check fixture for an external runner."
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", default=PUBLIC_ORIGIN)
    parser.add_argument("--local-origin", default=LOCAL_API_ORIGIN)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--tournament-count", type=int, default=1)
    parser.add_argument("--users-per-tournament", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--runner-sha")
    parser.add_argument("--source-binding-base64")
    return parser.parse_args()


def project_fixture_timing_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Project the fixed external fixture timing receipt into its safe summary."
    )
    parser.add_argument("receipt_path", type=Path)
    parser.add_argument("source_summary_path", type=Path)
    parser.add_argument("destination_path", type=Path)
    parser.add_argument("qa_status", type=int)
    args = parser.parse_args(argv)
    if not 0 <= args.qa_status <= 255:
        raise ValueError("external fixture QA status is invalid")
    try:
        project_fixture_timing_summary(
            args.receipt_path,
            args.source_summary_path,
            args.destination_path,
            qa_succeeded=args.qa_status == 0,
        )
    except (OSError, ValueError):
        raise SystemExit("external fixture timing projection failed") from None
    return 0


async def prepare(args: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise QaFailure("external production fixture preparation must run as root")
    runner_sha = getattr(args, "runner_sha", None)
    source_binding_b64 = getattr(args, "source_binding_base64", None)
    source_binding: dict[str, Any] | None = None
    source_binding_sha256: str | None = None
    app_target_sha: str | None = None
    if runner_sha is not None:
        if not isinstance(runner_sha, str) or SOURCE_SHA_RE.fullmatch(runner_sha) is None:
            raise QaFailure("external fixture runner source identity is invalid")
        app_target_sha = runner_sha
        if source_binding_b64 is not None:
            try:
                from platform_noop_source_binding import validate_active_runtime_binding_argument

                source_binding = validate_active_runtime_binding_argument(
                    source_binding_b64,
                    expected_runner_sha=runner_sha,
                )
            except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
                raise QaFailure("external fixture source binding is invalid") from exc
            app_target_sha = str(source_binding["app_target_sha"])
            canonical_binding = json.dumps(
                source_binding,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            source_binding_sha256 = hashlib.sha256(canonical_binding).hexdigest()
    elif source_binding_b64 is not None:
        raise QaFailure("external fixture source binding has no runner identity")
    if args.origin.rstrip("/") != PUBLIC_ORIGIN:
        raise QaFailure("external fixture preparation requires the canonical public origin")
    if args.local_origin.rstrip("/") != LOCAL_API_ORIGIN:
        raise QaFailure("external fixture preparation requires the loopback API origin")
    if not 1 <= args.tournament_count <= MAX_TOURNAMENTS:
        raise QaFailure("tournament-count is outside the supported bound")
    if not MIN_USERS_PER_TOURNAMENT <= args.users_per_tournament <= MAX_USERS_PER_TOURNAMENT:
        raise QaFailure("users-per-tournament is outside the supported bound")
    if not 1 <= args.concurrency <= 256:
        raise QaFailure("concurrency is outside the supported bound")
    total_users = args.tournament_count * args.users_per_tournament
    if total_users > MAX_USERS:
        raise QaFailure("external fixture exceeds the supported user bound")

    load_env_file(args.env_file)
    os.environ["PLATFORM_RUNTIME_SERVICE"] = "qa"
    qa = ProductionQa(
        origin=args.origin,
        request_origin=args.origin,
        report_path=args.report_path,
        keep_data=True,
        http_timeout=args.http_timeout,
        mode="write-burst",
        scale_users=total_users,
        concurrency=args.concurrency,
        http_max_connections=max(100, args.concurrency),
        collect_performance=False,
        write_burst_profile="single-ready",
        write_burst_users_per_tournament=args.users_per_tournament,
    )
    # The write-burst constructor normally sizes one single-ready fixture.
    # This preparation mode deliberately creates a multi-tournament cohort.
    qa.scale_users = total_users
    qa.report["requested_users"] = total_users
    qa.report["external_vote"] = {
        "tournament_count": args.tournament_count,
        "users_per_tournament": args.users_per_tournament,
        "preparation_origin": args.local_origin,
        "measurement_origin": args.origin,
    }
    fixture_timings = FixtureStageTimings()
    fixture_timing_path = args.report_path.with_name("fixture-timing.json")

    api_client = await qa.new_client(args.local_origin)
    try:
        await qa.record_preprod_run(status="running", requested_users=total_users)
        with fixture_timings.measure("bulk_user_creation"):
            users = await qa.bulk_register_scale_users()
        qa.preseed_csrf_tokens(users)
        qa.scenario(
            "external_vote_fixture_users_created",
            len(users) == total_users,
            {"users": len(users), "expected": total_users},
        )

        chunks = [
            users[index : index + args.users_per_tournament]
            for index in range(0, total_users, args.users_per_tournament)
        ]
        with fixture_timings.measure("permission_grants"):
            await qa.grant_tournament_permissions(
                [chunk[0] for chunk in chunks],
                [],
            )
        tournament_entries: list[dict[str, Any]] = []
        manifest_users: list[dict[str, str]] = []
        prepared_tournaments: list[dict[str, Any]] = []
        # This aggregate includes creation, participant joins and registration
        # close performed by prepare_ready_burst_tournament for each cohort.
        with fixture_timings.measure("tournament_api_setup"):
            for index, chunk in enumerate(chunks, start=1):
                with qa.phase("external_vote_fixture_setup"):
                    tournament = await qa.prepare_ready_burst_tournament(
                        api_client,
                        organizer=chunk[0],
                        participants=chunk,
                        label="external_ready",
                        index=index,
                        start_ready_check=False,
                    )
                prepared_tournaments.append(tournament)
                slug = str(tournament["slug"])
                tournament_entries.append(
                    {
                        "id": str(tournament["id"]),
                        "slug": slug,
                        "user_count": len(chunk),
                    }
                )
                for user in chunk:
                    user_id = str(user["id"])
                    manifest_users.append(
                        {
                            "user_id": user_id,
                            "tournament_slug": slug,
                            "session_token": qa.session_tokens_by_user_id[user_id],
                            "csrf_token": qa.csrf_tokens_by_user_id[user_id],
                        }
                    )

        # Start every Ready Check only after the complete fixture exists. A
        # sequential setup of a 20k cohort can otherwise age the first
        # tournament past its ten-minute manual window before measurement
        # begins, making the duplicate phase test the timer instead of
        # idempotency.
        with fixture_timings.measure("ready_check_starts"):
            for tournament in prepared_tournaments:
                with qa.phase("external_vote_fixture_start"):
                    await qa.request_as(
                        api_client,
                        tournament["organizer"],
                        "POST",
                        f"/tournaments/{tournament['slug']}/deadlock/ready-check/start",
                        expected=201,
                    )

        with fixture_timings.measure("fixture_finalization"):
            qa.report["tournament_ids"] = list(qa.tournament_ids)
            qa.report["external_vote"]["manifest_user_count"] = len(manifest_users)
            qa.scenario(
                "external_vote_fixture_ready_rounds_created",
                len(tournament_entries) == args.tournament_count
                and len(manifest_users) == total_users,
                {
                    "tournaments": len(tournament_entries),
                    "users": len(manifest_users),
                },
            )

            manifest_payload: dict[str, Any] = {
                "schema": 1 if runner_sha is None else 2,
                "purpose": "external_ready_vote",
                "origin": PUBLIC_ORIGIN,
                "session_cookie_name": qa.session_cookie_name,
                "csrf_cookie_name": qa.csrf_cookie_name,
                "marker": qa.marker,
                "created_at": datetime.now(UTC).isoformat(),
                "tournaments": tournament_entries,
                "users": manifest_users,
            }
            if runner_sha is not None:
                manifest_payload.update(
                    {
                        "runner_sha": runner_sha,
                        "app_target_sha": app_target_sha,
                        "source_binding": source_binding,
                        "source_binding_sha256": source_binding_sha256,
                    }
                )
                qa.report["runner_sha"] = runner_sha
                qa.report["app_target_sha"] = app_target_sha
                qa.report["source_binding_sha256"] = source_binding_sha256
            atomic_write_json(
                args.manifest_path,
                manifest_payload,
                mode=0o600,
            )
            qa.report["finished_at"] = datetime.now(UTC).isoformat()
            qa.report["passed"] = True
            qa.report["report_path"] = str(args.report_path)
            args.report_path.parent.mkdir(parents=True, exist_ok=True)
            args.report_path.write_text(
                json.dumps(qa.report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            await qa.record_preprod_run(
                status="passed",
                requested_users=total_users,
                created_users=len(users),
                tournaments_created=len(tournament_entries),
                finished_at=datetime.now(UTC),
            )
        write_fixture_timing_receipt(fixture_timing_path, fixture_timings)
        return qa.report
    except Exception:
        qa.report["finished_at"] = datetime.now(UTC).isoformat()
        qa.report["passed"] = False
        qa.report["report_path"] = str(args.report_path)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(qa.report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            await qa.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
        except Exception:
            pass
        write_fixture_timing_receipt(fixture_timing_path, fixture_timings)
        raise
    finally:
        await api_client.aclose()


async def async_main() -> int:
    args = parse_args()
    report = await prepare(args)
    print(
        json.dumps(
            {
                "mode": "external-vote-prepare",
                "passed": report.get("passed"),
                "marker": report.get("marker"),
                "users": report.get("created_users"),
                "tournaments": len(report.get("tournament_ids") or []),
            },
            ensure_ascii=False,
        )
    )
    return 0


async def main() -> int:
    try:
        return await async_main()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--project-fixture-timing"]:
        raise SystemExit(project_fixture_timing_main(sys.argv[2:]))
    raise SystemExit(asyncio.run(main()))
