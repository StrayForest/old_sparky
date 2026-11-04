#!/usr/bin/env python3
"""Prepare a retained Ready Check vote fixture for an external HTTP runner.

Only deterministic fixture setup runs on the origin.  The returned manifest is
temporary credential material and must be copied only to the external runner's
private temporary directory; it is never part of a report or an artifact.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
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
    from platform_production_qa import (
        ProductionQa,
        QaFailure,
        dispose_engine,
        load_env_file,
        metric_stats,
    )
    from platform_evidence_sanitizer import safe_route_key
except ModuleNotFoundError as exc:  # Imported as ``tools.*`` by focused tests.
    if exc.name != "platform_production_qa":
        raise
    from tools.platform_production_qa import (
        ProductionQa,
        QaFailure,
        dispose_engine,
        load_env_file,
        metric_stats,
    )
    from tools.platform_evidence_sanitizer import safe_route_key


PUBLIC_ORIGIN = "https://old-sparky.com"
LOCAL_API_ORIGIN = "http://127.0.0.1:8010"
MIN_USERS_PER_TOURNAMENT = 14
MAX_USERS_PER_TOURNAMENT = 500
MAX_TOURNAMENTS = 40
MAX_USERS = MAX_USERS_PER_TOURNAMENT * MAX_TOURNAMENTS
SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
READ_MIX_SCHEDULE_PROFILE = "read-mix-concurrency-ramp-v1"
READY_WORKER_POLL_SECONDS = 5
READY_WORKER_WAIT_SECONDS = 360
FIXTURE_TIMING_STAGES = (
    "bulk_user_creation",
    "permission_grants",
    "tournament_api_setup",
    "ready_check_starts",
    "fixture_finalization",
)
MAX_FIXTURE_STAGE_DURATION_MS = 180 * 60 * 1000
FIXTURE_SETUP_ROUTE_KEYS = frozenset(
    {
        "POST tournaments_collection",
        "POST tournament_participants",
        "PATCH tournament_detail",
        "other",
    }
)


class FixtureStageTimings:
    """Keep one monotonic duration for each bounded fixture-setup stage."""

    def __init__(self) -> None:
        self._stages: dict[str, dict[str, Any]] = {}

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        if name not in FIXTURE_TIMING_STAGES or name in self._stages:
            raise ValueError("external fixture timing stage is invalid")
        started = time.perf_counter()
        started_at_utc = datetime.now(UTC).isoformat()
        completed = False
        try:
            yield
            completed = True
        finally:
            elapsed_seconds = max(0.0, time.perf_counter() - started)
            self._stages[name] = {
                "duration_ms": round(elapsed_seconds * 1000, 3),
                "completed": completed,
                "started_at_utc": started_at_utc,
                "finished_at_utc": datetime.now(UTC).isoformat(),
            }

    def snapshot(self) -> dict[str, dict[str, Any]]:
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


def write_fixture_timing_receipt(
    path: Path,
    timings: FixtureStageTimings,
    *,
    setup_http: dict[str, Any] | None = None,
) -> None:
    """Persist only bounded phase durations, separate from credential reports."""

    atomic_write_json(
        path,
        {
            "schema": 1,
            "clock": "time.perf_counter",
            "stages": timings.snapshot(),
            **({"setup_http": setup_http} if setup_http is not None else {}),
        },
        mode=0o600,
    )


def summarize_fixture_setup_http(qa: ProductionQa) -> dict[str, Any]:
    """Keep bounded route aggregates for tournament API fixture setup only."""

    rows: dict[str, list[Any]] = defaultdict(list)
    recorder = getattr(qa, "http_metrics", None)
    for sample in getattr(recorder, "samples", ()):
        if sample.phase != "external_vote_fixture_setup":
            continue
        route_key = safe_route_key(sample.method, sample.path)
        route = route_key if route_key in FIXTURE_SETUP_ROUTE_KEYS else "other"
        rows[route].append(sample)
    by_route: dict[str, Any] = {}
    for route, samples in sorted(rows.items()):
        statuses = Counter(str(sample.status_code) for sample in samples)
        by_route[route] = {
            "requests": len(samples),
            "errors": sum(not sample.ok for sample in samples),
            "status_counts": dict(sorted(statuses.items())),
            "latency_ms": metric_stats([sample.elapsed_ms for sample in samples]),
            "response_bytes_total": sum(sample.response_bytes for sample in samples),
        }
    return {
        "requests": sum(len(samples) for samples in rows.values()),
        "errors": sum(
            not sample.ok for samples in rows.values() for sample in samples
        ),
        "by_route": by_route,
    }


def project_fixture_setup_http(value: Any) -> dict[str, Any]:
    """Validate the fixed numeric route/status summary; discard all other keys."""

    if not isinstance(value, dict) or set(value) != {"requests", "errors", "by_route"}:
        raise ValueError("fixture setup HTTP summary schema is invalid")

    def bounded_int(raw: Any, maximum: int) -> int:
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= maximum:
            raise ValueError("fixture setup HTTP count is invalid")
        return raw

    requests = bounded_int(value["requests"], 50_000)
    errors = bounded_int(value["errors"], requests)
    raw_routes = value["by_route"]
    if not isinstance(raw_routes, dict) or set(raw_routes) - FIXTURE_SETUP_ROUTE_KEYS:
        raise ValueError("fixture setup HTTP route summary is invalid")
    by_route: dict[str, Any] = {}
    route_requests = 0
    route_errors = 0
    for route, raw in sorted(raw_routes.items()):
        if not isinstance(raw, dict) or set(raw) != {
            "requests",
            "errors",
            "status_counts",
            "latency_ms",
            "response_bytes_total",
        }:
            raise ValueError("fixture setup HTTP route row is invalid")
        count = bounded_int(raw["requests"], 50_000)
        route_error_count = bounded_int(raw["errors"], count)
        raw_statuses = raw["status_counts"]
        if not isinstance(raw_statuses, dict):
            raise ValueError("fixture setup HTTP status summary is invalid")
        statuses: dict[str, int] = {}
        for status, status_count in raw_statuses.items():
            if (
                not isinstance(status, str)
                or not re.fullmatch(r"[0-9]{1,3}", status)
                or int(status) > 599
            ):
                raise ValueError("fixture setup HTTP status summary is invalid")
            statuses[status] = bounded_int(status_count, count)
        if sum(statuses.values()) != count:
            raise ValueError("fixture setup HTTP status counts do not match requests")
        latency = raw["latency_ms"]
        latency_keys = {"count", "avg_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"}
        if not isinstance(latency, dict) or set(latency) != latency_keys:
            raise ValueError("fixture setup HTTP latency summary is invalid")
        if bounded_int(latency["count"], 50_000) != count:
            raise ValueError("fixture setup HTTP latency count does not match requests")
        safe_latency: dict[str, Any] = {"count": count}
        for key in latency_keys - {"count"}:
            number = latency[key]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or not 0 <= number <= 3_600_000
            ):
                raise ValueError("fixture setup HTTP latency value is invalid")
            safe_latency[key] = number
        response_bytes = bounded_int(raw["response_bytes_total"], 10_000_000_000)
        by_route[route] = {
            "requests": count,
            "errors": route_error_count,
            "status_counts": dict(sorted(statuses.items())),
            "latency_ms": safe_latency,
            "response_bytes_total": response_bytes,
        }
        route_requests += count
        route_errors += route_error_count
    if route_requests != requests or route_errors != errors:
        raise ValueError("fixture setup HTTP route totals do not match summary")
    return {"requests": requests, "errors": errors, "by_route": by_route}


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
        or set(payload) not in (
            {"schema", "clock", "stages"},
            {"schema", "clock", "stages", "setup_http"},
        )
        or type(payload.get("schema")) is not int
        or payload["schema"] != 1
        or payload.get("clock") != "time.perf_counter"
        or not isinstance(payload.get("stages"), dict)
    ):
        raise ValueError("fixture timing receipt schema is invalid")

    stages = payload["stages"]
    if set(stages) - set(FIXTURE_TIMING_STAGES):
        raise ValueError("fixture timing receipt has an unknown stage")
    safe_stages: dict[str, dict[str, Any]] = {}
    for stage_name in FIXTURE_TIMING_STAGES:
        if stage_name not in stages:
            continue
        stage = stages[stage_name]
        if not isinstance(stage, dict) or set(stage) != {
            "duration_ms",
            "completed",
            "started_at_utc",
            "finished_at_utc",
        }:
            raise ValueError("fixture timing stage schema is invalid")
        duration_ms = stage["duration_ms"]
        completed = stage["completed"]
        timestamps: dict[str, str] = {}
        parsed_timestamps: dict[str, datetime] = {}
        for key in ("started_at_utc", "finished_at_utc"):
            value = stage[key]
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError):
                raise ValueError("fixture timing timestamp is invalid") from None
            if parsed.tzinfo is None:
                raise ValueError("fixture timing timestamp is invalid")
            parsed_timestamps[key] = parsed.astimezone(UTC)
            timestamps[key] = parsed.astimezone(UTC).isoformat()
        if parsed_timestamps["finished_at_utc"] < parsed_timestamps["started_at_utc"]:
            raise ValueError("fixture timing timestamp order is invalid")
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
            **timestamps,
        }

    if qa_succeeded and (
        tuple(safe_stages) != FIXTURE_TIMING_STAGES
        or any(stage["completed"] is not True for stage in safe_stages.values())
    ):
        raise ValueError("successful fixture timing is incomplete")
    safe_receipt: dict[str, Any] = {
        "schema": 1,
        "clock": "time.perf_counter",
        "stages": safe_stages,
    }
    if "setup_http" in payload:
        safe_receipt["setup_http"] = project_fixture_setup_http(payload["setup_http"])
    elif qa_succeeded:
        raise ValueError("successful fixture timing has no setup HTTP metrics")
    return safe_receipt


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
    control_account_preserved = payload.get("control_account_preserved")
    if control_account_preserved is not None and type(control_account_preserved) is not bool:
        raise ValueError("control account projection value is invalid")
    safe_summary = {
        "schema": 1,
        "mode": mode,
        "control_account_preserved": control_account_preserved,
        "planned_tournaments": safe_count("planned_tournaments"),
        "completed_tournaments": safe_count("completed_tournaments"),
        "planned_users": safe_count("planned_users"),
        "completed_users": safe_count("completed_users"),
        "passed": payload.get("passed") is True,
        "rows": safe_rows,
        "external_vote_fixture": {
            "fixture_setup_timings": timing,
            **(
                {"fixture_setup_http": timing["setup_http"]}
                if "setup_http" in timing
                else {}
            ),
        },
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
    parser.add_argument("--fixture-schedule", choices=(READ_MIX_SCHEDULE_PROFILE,))
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
    fixture_schedule = getattr(args, "fixture_schedule", None)
    if fixture_schedule is not None and (
        fixture_schedule != READ_MIX_SCHEDULE_PROFILE
        or args.tournament_count != MAX_TOURNAMENTS
        or args.users_per_tournament != MAX_USERS_PER_TOURNAMENT
    ):
        raise QaFailure("scheduled Ready Check fixture dimensions are invalid")

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
        collect_http_metrics=True,
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
    fixture_setup_http: dict[str, Any] | None = None

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
                        scheduled_ready_window=(fixture_schedule is not None),
                    )
                prepared_tournaments.append(tournament)
                slug = str(tournament["slug"])
                tournament_entries.append(
                    {
                        "id": str(tournament["id"]),
                        "slug": slug,
                        "user_count": len(chunk),
                        **(
                            {
                                "ready_check_starts_at": tournament[
                                    "ready_check_starts_at"
                                ],
                                "ready_check_ends_at": tournament[
                                    "ready_check_ends_at"
                                ],
                            }
                            if fixture_schedule is not None
                            else {}
                        ),
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
        fixture_setup_http = summarize_fixture_setup_http(qa)

        # Manual windows are started only after the complete fixture exists.
        # The read-mix capacity profile instead uses the public scheduled
        # workflow and waits for the normal automation worker to materialize
        # every active round.
        with fixture_timings.measure("ready_check_starts"):
            if fixture_schedule is None:
                for tournament in prepared_tournaments:
                    with qa.phase("external_vote_fixture_start"):
                        await qa.request_as(
                            api_client,
                            tournament["organizer"],
                            "POST",
                            f"/tournaments/{tournament['slug']}/deadlock/ready-check/start",
                            expected=201,
                        )
            else:
                deadline = time.monotonic() + READY_WORKER_WAIT_SECONDS
                active_count = 0
                while True:
                    active_count = 0

                    async def check_active_round(tournament: dict[str, Any]) -> None:
                        nonlocal active_count
                        with qa.phase("external_vote_fixture_scheduled_ready_check_poll"):
                            state = await qa.request_as(
                                api_client,
                                tournament["organizer"],
                                "GET",
                                f"/tournaments/{tournament['slug']}/deadlock/ready-check",
                                expected=200,
                            )
                        active_round = (
                            state.get("active_round") if isinstance(state, dict) else None
                        )
                        if isinstance(active_round, dict) and active_round.get("status") == "active":
                            active_count += 1

                    await qa.bounded_each(prepared_tournaments, check_active_round)
                    if active_count == len(prepared_tournaments):
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise QaFailure(
                            "scheduled Ready Checks did not become active within the bounded worker wait"
                        )
                    await asyncio.sleep(min(READY_WORKER_POLL_SECONDS, remaining))
                now = datetime.now(UTC)
                scheduled_deadlines = [
                    datetime.fromisoformat(
                        str(tournament["ready_check_ends_at"]).replace("Z", "+00:00")
                    ).astimezone(UTC)
                    for tournament in tournament_entries
                ]
                minimum_remaining_seconds = min(
                    (deadline_at - now).total_seconds()
                    for deadline_at in scheduled_deadlines
                )
                if minimum_remaining_seconds <= 3_900:
                    raise QaFailure(
                        "scheduled Ready Checks do not leave the read-ramp runtime and startup margin"
                    )
                qa.report["external_vote"]["scheduled_ready_state"] = {
                    "expected_active_rounds": len(prepared_tournaments),
                    "active_rounds": active_count,
                    "minimum_remaining_seconds": round(minimum_remaining_seconds, 3),
                    "required_remaining_seconds": 3_900,
                    "passed": True,
                }

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
            if fixture_schedule is not None:
                manifest_payload["fixture_schedule"] = fixture_schedule
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
        write_fixture_timing_receipt(
            fixture_timing_path,
            fixture_timings,
            setup_http=fixture_setup_http,
        )
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
        write_fixture_timing_receipt(
            fixture_timing_path,
            fixture_timings,
            setup_http=fixture_setup_http,
        )
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
