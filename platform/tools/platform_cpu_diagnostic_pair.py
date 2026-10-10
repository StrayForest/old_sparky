#!/usr/bin/env python3
"""Trusted parent and isolated worker for a fixed zero-credit SSR CPU pair."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ALL_COMPLETED, FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import threading
import time
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
import platform_external_load as external_load  # noqa: E402
import platform_load_runtime as load_runtime  # noqa: E402

WORKLOAD = "authenticated_workspace_read_pair_v1"
TRACE_HEADER = "x-platform-ssr-trace"
EXPECTED_USERS = 20_000
EXPECTED_TOURNAMENTS = 40
USERS_PER_TOURNAMENT = 500
COHORT_USERS = 8
REQUESTS_PER_LEG = 500
LEG_MS = 20_000
IDLE_MS = 5_000
PLAN_LEAD_MS = 60_000
PLAN_CAP = 4_096
ACTOR_PAYLOAD_CAP = 16_384
PAIR_PAYLOAD_CAP = 24_576
WORKER_OUTPUT_CAP = 64_000
REPORT_CAP = 64_000
SSH_LIFECYCLE_RECEIPT_CAP = 2_048
ROOT_USAGE_OUTPUT_CAP = 8_192
USAGE_ROW_FIELDS = frozenset({
    "service", "phase", "expected_targets", "observed_targets", "event_count",
    "cpu_ns", "window_ms_min", "window_ms_max", "start_lag_ms_min",
    "start_lag_ms_max", "end_lag_ms_min", "end_lag_ms_max", "duplicate_count",
    "timing_complete",
})
PROFILE_ROW_FIELDS = frozenset({
    "service", "expected_targets", "observed_targets", "event_count", "timer",
    "observation_unit", "total_cpu_us", "sample_count", "start_lag_ms_min",
    "start_lag_ms_max", "elapsed_ms_min", "elapsed_ms_max", "end_lag_ms_min",
    "end_lag_ms_max", "categories",
})
PROFILE_CATEGORY_FIELDS = frozenset({"category", "cpu_us", "observations"})
PROFILE_CATEGORIES = frozenset({
    "repo.get_tournament_workspace", "repo.get_tournament_workspace_by_slug",
    "repo.workspace_conditional_preflight", "repo.get_current_user",
    "repo.get_current_user_optional", "repo.get_server_request_correlation_headers",
    "repo.run_with_ssr_trace", "repo.workspace_api_fetch", "repo.workspace_page",
    "orm_result", "db_driver", "async_event_loop", "serialization_validation",
    "crypto", "web_framework", "http_client", "other",
})
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,95}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
SAFE_ERRORS = frozenset(
    {
        "unexpected_status",
        "TimeoutError",
        "URLError",
        "transport_http_protocol",
        "transport_http_response_read",
        "transport_response_read",
        "transport_http_error",
        "other",
    }
)


class PairError(ValueError):
    """A fixed diagnostic input or execution contract failed."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PairError("duplicate JSON key")
        result[key] = value
    return result


def _decode_canonical(raw: bytes, *, cap: int) -> dict[str, Any]:
    if not raw or len(raw) > cap:
        raise PairError("bounded JSON input rejected")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PairError("JSON input rejected") from exc
    if not isinstance(value, dict):
        raise PairError("JSON input shape rejected")
    canonical = json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if canonical != raw:
        raise PairError("JSON input is not canonical")
    return value


def _read_private_file(path: Path, *, cap: int, exact_mode: int | None = None) -> bytes:
    try:
        path_before = path.lstat()
        if stat.S_ISLNK(path_before.st_mode):
            raise PairError("input file path is a symbolic link")
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise PairError("input file unavailable") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size <= 0
            or before.st_size > cap
            or (exact_mode is not None and stat.S_IMODE(before.st_mode) != exact_mode)
            or (path_before.st_dev, path_before.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise PairError("input file metadata rejected")
        data = bytearray()
        while len(data) <= cap:
            block = os.read(fd, min(16_384, cap + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        after = os.fstat(fd)
        path_after = path.lstat()
        if (
            len(data) != before.st_size
            or len(data) > cap
            or (
                before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                before.st_gid, before.st_nlink, before.st_size,
                before.st_mtime_ns, before.st_ctime_ns,
            )
            != (
                after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                after.st_gid, after.st_nlink, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns,
            )
            or (
                path_after.st_dev, path_after.st_ino, path_after.st_mode,
                path_after.st_uid, path_after.st_gid, path_after.st_nlink,
                path_after.st_size, path_after.st_mtime_ns, path_after.st_ctime_ns,
            )
            != (
                after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                after.st_gid, after.st_nlink, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns,
            )
        ):
            raise PairError("input file changed during read")
        return bytes(data)
    finally:
        os.close(fd)


def _validate_plan(value: dict[str, Any], *, allow_future: bool) -> dict[str, Any]:
    if set(value) != {
        "schema", "run_id", "source_sha", "release_slug", "workload",
        "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms",
    }:
        raise PairError("plan shape rejected")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise PairError("plan schema rejected")
    if not isinstance(value["run_id"], str) or RUN_ID_RE.fullmatch(value["run_id"]) is None:
        raise PairError("plan run binding rejected")
    if not isinstance(value["source_sha"], str) or SOURCE_SHA_RE.fullmatch(value["source_sha"]) is None:
        raise PairError("plan source binding rejected")
    if not isinstance(value["release_slug"], str) or RELEASE_SLUG_RE.fullmatch(value["release_slug"]) is None:
        raise PairError("plan release binding rejected")
    if value["workload"] != WORKLOAD:
        raise PairError("plan workload rejected")
    times: list[int] = []
    for name in ("off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms"):
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise PairError("plan phase bound rejected")
        times.append(item)
    off_start, off_end, on_start, on_end = times
    now = time.time_ns() // 1_000_000
    lead_ms = off_start - now
    if (
        off_end - off_start != LEG_MS
        or on_end - on_start != LEG_MS
        or on_start - off_end != IDLE_MS
        or on_end - off_start > 60_000
        or (allow_future and not 0 <= lead_ms <= 60_000)
        or (not allow_future and not off_start <= now <= off_end)
    ):
        raise PairError("plan phase schedule rejected")
    return value


def _validate_actor_payload(value: dict[str, Any]) -> list[external_load.VirtualUser]:
    if set(value) != {
        "schema", "workload", "origin", "session_cookie_name", "csrf_cookie_name", "actors"
    }:
        raise PairError("actor payload shape rejected")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["workload"] != WORKLOAD:
        raise PairError("actor payload version rejected")
    if value["origin"] != external_load.EXPECTED_ORIGIN:
        raise PairError("actor payload origin rejected")
    for name in ("session_cookie_name", "csrf_cookie_name"):
        item = value[name]
        if not isinstance(item, str) or external_load.COOKIE_NAME_RE.fullmatch(item) is None:
            raise PairError("actor cookie binding rejected")
    raw_actors = value["actors"]
    if not isinstance(raw_actors, list) or len(raw_actors) != COHORT_USERS:
        raise PairError("actor cohort size rejected")
    actors: list[external_load.VirtualUser] = []
    slugs: set[str] = set()
    sessions: set[str] = set()
    csrf_values: set[str] = set()
    for raw in raw_actors:
        if not isinstance(raw, dict) or set(raw) != {"tournament_slug", "session_token", "csrf_token"}:
            raise PairError("actor entry rejected")
        slug, session, csrf = raw["tournament_slug"], raw["session_token"], raw["csrf_token"]
        if not isinstance(slug, str) or external_load.SLUG_RE.fullmatch(slug) is None:
            raise PairError("actor workspace rejected")
        if not isinstance(session, str) or TOKEN_RE.fullmatch(session) is None:
            raise PairError("actor session token rejected")
        if not isinstance(csrf, str) or TOKEN_RE.fullmatch(csrf) is None:
            raise PairError("actor CSRF token rejected")
        slugs.add(slug)
        sessions.add(session)
        csrf_values.add(csrf)
        actors.append(external_load.VirtualUser("diagnostic-user", slug, session, csrf))
    if len(slugs) != 1 or len(sessions) != COHORT_USERS or len(csrf_values) != COHORT_USERS:
        raise PairError("actor cohort consistency rejected")
    return actors


def _safe_error(value: Any) -> str:
    return value if isinstance(value, str) and value in SAFE_ERRORS else "other"


def _wait_until(target_ms: int) -> float:
    wall_now_ms = time.time_ns() // 1_000_000
    monotonic_target = time.monotonic() + max(0, target_ms - wall_now_ms) / 1000
    while time.monotonic() < monotonic_target:
        time.sleep(min(monotonic_target - time.monotonic(), 0.05))
    if time.monotonic() - monotonic_target > 0.050:
        raise PairError("phase start window missed")
    return monotonic_target


def _wait_past(target_ms: int) -> None:
    while True:
        remaining_ms = target_ms - time.time_ns() // 1_000_000
        if remaining_ms <= 0:
            return
        time.sleep(min(remaining_ms / 1000, 0.1))


def _leg(
    origin: str,
    actors: list[external_load.VirtualUser],
    cookie_names: tuple[str, str],
    plan: dict[str, Any],
    phase: str,
    executor: ThreadPoolExecutor,
) -> dict[str, Any]:
    monotonic_start = _wait_until(plan[f"{phase}_start_ms"])
    monotonic_end = monotonic_start + LEG_MS / 1000
    users = [actors[index % COHORT_USERS] for index in range(REQUESTS_PER_LEG)]
    results: list[Any] = []
    in_flight: dict[Any, int] = {}
    next_index = 0
    finish_deadline_ms = (
        plan["on_start_ms"] if phase == "off" else plan["on_end_ms"] + 5_000
    )
    finish_deadline = monotonic_start + (finish_deadline_ms - plan[f"{phase}_start_ms"]) / 1000

    def collect_one(*, block: bool) -> bool:
        if not in_flight:
            return False
        remaining = finish_deadline - time.monotonic()
        if remaining <= 0:
            return False
        done, _pending = wait(
            in_flight,
            timeout=remaining if block else 0,
            return_when=FIRST_COMPLETED,
        )
        for future in done:
            submission_index = in_flight.pop(future)
            result = future.result()
            result.submission_index = submission_index
            results.append(result)
        return bool(done)

    while next_index < REQUESTS_PER_LEG:
        scheduled = monotonic_start + (next_index * (LEG_MS / 1000)) / REQUESTS_PER_LEG
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if time.monotonic() >= monotonic_end:
            break
        while len(in_flight) >= COHORT_USERS:
            if not collect_one(block=True):
                break
        if len(in_flight) >= COHORT_USERS or time.monotonic() >= monotonic_end:
            break
        user = users[next_index]
        submission_index = next_index
        next_index += 1

        def invoke(selected_user: Any = user) -> Any:
            return external_load._page_request(
                origin,
                selected_user,
                "cpu_diagnostic_pair",
                5,
                session_cookie_name=cookie_names[0],
                csrf_cookie_name=cookie_names[1],
                transport=external_load.HTTP11_KEEPALIVE_TRANSPORT,
                diagnostic_trace_run_id=plan["run_id"],
            )

        future = executor.submit(invoke)
        in_flight[future] = submission_index

    while in_flight and time.monotonic() < finish_deadline:
        if not collect_one(block=True):
            break
    if in_flight:
        for future in in_flight:
            future.cancel()
        raise PairError("phase drain deadline exceeded")
    elapsed = time.monotonic() - monotonic_start
    status_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    latencies: list[float] = []
    in_window = 0
    for result in results:
        status_key = (
            str(result.status)
            if isinstance(result.status, int) and not isinstance(result.status, bool)
            and 100 <= result.status <= 599
            else "invalid"
        )
        status_counts[status_key] += 1
        error_counts[_safe_error(result.error_kind)] += 1
        if isinstance(result.elapsed_ms, (int, float)) and result.elapsed_ms >= 0:
            latencies.append(float(result.elapsed_ms))
        if result.finished_at_monotonic is not None and result.finished_at_monotonic <= monotonic_end:
            in_window += 1
    ordered = sorted(latencies)
    capture_complete = (
        len(results) == REQUESTS_PER_LEG
        and next_index == REQUESTS_PER_LEG
        and time.monotonic() <= finish_deadline
    )
    return {
        "scheduled": REQUESTS_PER_LEG,
        "submitted": next_index,
        "completed": len(results),
        "completed_in_window": in_window,
        "status_counts": dict(sorted(status_counts.items())),
        "error_kinds": dict(sorted(error_counts.items())),
        "latency_ms": {
            "count": len(ordered),
            "p50": external_load._percentile_from_ordered(ordered, 0.50),
            "p95": external_load._percentile_from_ordered(ordered, 0.95),
            "p99": external_load._percentile_from_ordered(ordered, 0.99),
        },
        "elapsed_seconds": round(elapsed, 6),
        "drain_deadline_ms": finish_deadline_ms,
        "complete": capture_complete,
        "all_expected_statuses": bool(
            capture_complete and all(result.status == 200 and result.ok for result in results)
        ),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    data = json.dumps(
        report, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    if len(data) > REPORT_CAP:
        raise PairError("report cap exceeded")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise PairError("report destination rejected") from exc
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise PairError("report write failed")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _quality_gate(
    off: dict[str, Any],
    on: dict[str, Any],
    *,
    service_cpu_delta_percentage_points: dict[str, float | None] | None,
) -> dict[str, Any]:
    """Keep capture completeness separate from instrumentation overhead."""

    off_p95 = off.get("latency_ms", {}).get("p95")
    on_p95 = on.get("latency_ms", {}).get("p95")
    p95_delta: float | None = None
    if (
        isinstance(off_p95, (int, float))
        and not isinstance(off_p95, bool)
        and off_p95 > 0
        and isinstance(on_p95, (int, float))
        and not isinstance(on_p95, bool)
        and on_p95 >= 0
    ):
        p95_delta = round(((float(on_p95) / float(off_p95)) - 1.0) * 100.0, 4)
    p95_pass = p95_delta is not None and p95_delta <= 5.0
    service_names = ("api", "web")
    cpu_deltas: dict[str, float | None] = {}
    cpu_checks: dict[str, bool | None] = {}
    for service in service_names:
        raw_delta = (
            service_cpu_delta_percentage_points.get(service)
            if isinstance(service_cpu_delta_percentage_points, dict)
            and set(service_cpu_delta_percentage_points) == set(service_names)
            else None
        )
        valid = (
            isinstance(raw_delta, (int, float))
            and not isinstance(raw_delta, bool)
        )
        cpu_delta = round(float(raw_delta), 4) if valid else None
        cpu_deltas[service] = cpu_delta
        cpu_checks[service] = abs(cpu_delta) <= 3.0 if cpu_delta is not None else None
    cpu_failed = any(value is False for value in cpu_checks.values())
    cpu_pass = all(value is True for value in cpu_checks.values())
    if (p95_delta is not None and not p95_pass) or cpu_failed:
        status = "failed"
    elif p95_pass and cpu_pass:
        status = "passed"
    else:
        status = "unmeasured"
    return {
        "status": status,
        "request_p95_delta_percent": p95_delta,
        "request_p95_limit_percent": 5.0,
        "request_p95_within_limit": p95_pass if p95_delta is not None else None,
        "service_cpu_delta_percentage_points": cpu_deltas,
        "service_cpu_delta_limit_percentage_points": 3.0,
        "service_cpu_within_limit": cpu_checks,
    }


def _validate_usage_summary(value: Any) -> dict[str, Any] | None:
    """Validate the closed root-controller CPU summary without retaining identities."""

    top_fields = {
        "status", "service_count", "usage_status", "usage_reason", "usage_rows",
        "profile_status", "profile_reason", "profile_rows",
    }
    if (
        not isinstance(value, dict)
        or set(value) != top_fields
        or value.get("status") != "expired_plans_removed"
        or type(value.get("service_count")) is not int
        or value["service_count"] not in {0, 2}
        or value.get("usage_status") not in {"complete", "incomplete", "unavailable"}
        or value.get("usage_reason") not in {
            "none", "missing", "duplicate", "identity_changed", "journal_failed",
            "byte_cap", "line_cap", "timeout", "invalid_event", "timing_incomplete",
        }
        or not isinstance(value.get("usage_rows"), list)
        or value.get("profile_status") not in {"complete", "incomplete", "unavailable"}
        or value.get("profile_reason") not in {
            "none", "missing", "duplicate", "identity_changed", "journal_failed",
            "byte_cap", "line_cap", "timeout", "invalid_profile",
        }
        or not isinstance(value.get("profile_rows"), list)
    ):
        return None
    rows = value["usage_rows"]
    if not rows:
        if not (
            value["service_count"] == 0
            and value["usage_status"] == "unavailable"
            and value["usage_reason"] == "missing"
            and value["profile_status"] == "unavailable"
            and value["profile_reason"] == "missing"
            and value["profile_rows"] == []
        ):
            return None
        return {
            "status": value["status"], "service_count": 0,
            "usage_status": "unavailable", "usage_reason": "missing", "usage_rows": [],
            "profile_status": "unavailable", "profile_reason": "missing", "profile_rows": [],
        }
    expected = (("api", "off", 2), ("api", "on", 2), ("web", "off", 1), ("web", "on", 1))
    if len(rows) != len(expected) or value["service_count"] != 2:
        return None
    clean_rows: list[dict[str, Any]] = []
    for row, (service, phase, targets) in zip(rows, expected):
        if (
            not isinstance(row, dict)
            or set(row) != USAGE_ROW_FIELDS
            or row.get("service") != service
            or row.get("phase") != phase
            or row.get("expected_targets") != targets
            or type(row.get("observed_targets")) is not int
            or not 0 <= row["observed_targets"] <= targets
            or type(row.get("event_count")) is not int
            or not 0 <= row["event_count"] <= 8
            or type(row.get("duplicate_count")) is not int
            or not 0 <= row["duplicate_count"] <= 8
            or type(row.get("timing_complete")) is not bool
        ):
            return None
        clean: dict[str, Any] = {key: row[key] for key in USAGE_ROW_FIELDS}
        for field in (
            "cpu_ns", "window_ms_min", "window_ms_max", "start_lag_ms_min",
            "start_lag_ms_max", "end_lag_ms_min", "end_lag_ms_max",
        ):
            item = clean[field]
            if item is not None and (type(item) is not int or abs(item) > 10**13):
                return None
        if clean["cpu_ns"] is not None and clean["cpu_ns"] < 0:
            return None
        clean_rows.append(clean)
    if value["usage_status"] == "complete" and (
        value["usage_reason"] != "none"
        or any(
            row["observed_targets"] != row["expected_targets"]
            or row["event_count"] != row["expected_targets"]
            or row["duplicate_count"] != 0
            or row["cpu_ns"] is None
            or row["timing_complete"] is not True
            or not isinstance(row["window_ms_min"], int)
            or not isinstance(row["window_ms_max"], int)
            or not 19_750 <= row["window_ms_min"] <= row["window_ms_max"] <= 20_500
            or not isinstance(row["start_lag_ms_min"], int)
            or not isinstance(row["start_lag_ms_max"], int)
            or not 0 <= row["start_lag_ms_min"] <= row["start_lag_ms_max"] <= 250
            or not isinstance(row["end_lag_ms_min"], int)
            or not isinstance(row["end_lag_ms_max"], int)
            or not -250 <= row["end_lag_ms_min"] <= row["end_lag_ms_max"] <= 250
            for row in clean_rows
        )
    ):
        return None
    expected_profiles = (("api", 2, "thread_cpu", "calls"), ("web", 1, "v8_cpu", "samples"))
    profile_rows = value["profile_rows"]
    if not profile_rows:
        return None
    if len(profile_rows) != 2 or value["service_count"] != 2:
        return None
    clean_profiles: list[dict[str, Any]] = []
    for profile, (service, targets, timer, observation_unit) in zip(profile_rows, expected_profiles):
        if (
            not isinstance(profile, dict)
            or set(profile) != PROFILE_ROW_FIELDS
            or profile.get("service") != service
            or profile.get("expected_targets") != targets
            or type(profile.get("observed_targets")) is not int
            or not 0 <= profile["observed_targets"] <= targets
            or type(profile.get("event_count")) is not int
            or not 0 <= profile["event_count"] <= 8
            or profile.get("timer") != timer
            or profile.get("observation_unit") != observation_unit
            or not isinstance(profile.get("categories"), list)
            or len(profile["categories"]) > 16
        ):
            return None
        profile_clean = {key: profile[key] for key in PROFILE_ROW_FIELDS}
        for name in (
            "total_cpu_us", "sample_count", "start_lag_ms_min", "start_lag_ms_max",
            "elapsed_ms_min", "elapsed_ms_max", "end_lag_ms_min", "end_lag_ms_max",
        ):
            item = profile_clean[name]
            if item is not None and (type(item) is not int or not 0 <= item <= 10**10):
                return None
        categories: list[dict[str, Any]] = []
        names: list[str] = []
        for category in profile_clean["categories"]:
            if (
                not isinstance(category, dict)
                or set(category) != PROFILE_CATEGORY_FIELDS
                or category.get("category") not in PROFILE_CATEGORIES
                or type(category.get("cpu_us")) is not int
                or not 0 <= category["cpu_us"] <= 10**10
                or type(category.get("observations")) is not int
                or not 0 <= category["observations"] <= 10**10
            ):
                return None
            names.append(category["category"])
            categories.append({
                "category": category["category"],
                "cpu_us": category["cpu_us"],
                "observations": category["observations"],
            })
        if names != sorted(names) or len(set(names)) != len(names):
            return None
        profile_clean["categories"] = categories
        clean_profiles.append(profile_clean)
    if value["profile_status"] == "complete" and (
        value["profile_reason"] != "none"
        or any(
            row["observed_targets"] != row["expected_targets"]
            or row["event_count"] != row["expected_targets"]
            or not isinstance(row["total_cpu_us"], int)
            or row["total_cpu_us"] <= 0
            or (row["service"] == "api" and row["sample_count"] is not None)
            or (row["service"] == "web" and (
                not isinstance(row["sample_count"], int) or row["sample_count"] <= 0
            ))
            or not row["categories"]
            or not isinstance(row["start_lag_ms_min"], int)
            or not isinstance(row["start_lag_ms_max"], int)
            or not 0 <= row["start_lag_ms_min"] <= row["start_lag_ms_max"] <= 250
            or not isinstance(row["elapsed_ms_min"], int)
            or not isinstance(row["elapsed_ms_max"], int)
            or not 19_750 <= row["elapsed_ms_min"] <= row["elapsed_ms_max"] <= 20_500
            or not isinstance(row["end_lag_ms_min"], int)
            or not isinstance(row["end_lag_ms_max"], int)
            or not -250 <= row["end_lag_ms_min"] <= row["end_lag_ms_max"] <= 250
            for row in clean_profiles
        )
    ):
        return None
    return {
        "status": value["status"], "service_count": value["service_count"],
        "usage_status": value["usage_status"], "usage_reason": value["usage_reason"],
        "usage_rows": clean_rows,
        "profile_status": value["profile_status"], "profile_reason": value["profile_reason"],
        "profile_rows": clean_profiles,
    }


def _usage_delta_percentage_points(summary: dict[str, Any] | None) -> dict[str, float | None] | None:
    """Convert proven per-target process CPU time to service CPU percentage points."""

    if summary is None or summary.get("usage_status") != "complete":
        return None
    rows = summary.get("usage_rows")
    if not isinstance(rows, list) or len(rows) != 4:
        return None
    by_key = {(row["service"], row["phase"]): row for row in rows if isinstance(row, dict)}
    if len(by_key) != 4:
        return None
    values: dict[tuple[str, str], float] = {}
    for service, phase, targets in (
        ("api", "off", 2), ("api", "on", 2), ("web", "off", 1), ("web", "on", 1),
    ):
        row = by_key.get((service, phase))
        if (
            row is None
            or row.get("expected_targets") != targets
            or row.get("observed_targets") != targets
            or row.get("event_count") != targets
            or row.get("duplicate_count") != 0
            or row.get("timing_complete") is not True
            or type(row.get("cpu_ns")) is not int
            or row["cpu_ns"] < 0
            or not isinstance(row.get("window_ms_min"), int)
            or not isinstance(row.get("window_ms_max"), int)
            or not 19_750 <= row["window_ms_min"] <= 20_500
            or not 19_750 <= row["window_ms_max"] <= 20_500
            or not isinstance(row.get("start_lag_ms_min"), int)
            or not isinstance(row.get("start_lag_ms_max"), int)
            or not 0 <= row["start_lag_ms_min"] <= row["start_lag_ms_max"] <= 250
            or not isinstance(row.get("end_lag_ms_min"), int)
            or not isinstance(row.get("end_lag_ms_max"), int)
            or not -250 <= row["end_lag_ms_min"] <= row["end_lag_ms_max"] <= 250
        ):
            return None
        values[(service, phase)] = row["cpu_ns"] / (LEG_MS * 1_000_000) * 100.0
    return {
        "api": values[("api", "on")] - values[("api", "off")],
        "web": values[("web", "on")] - values[("web", "off")],
    }


def _worker_callback(config: dict[str, Any]) -> dict[str, Any]:
    raw = sys.stdin.buffer.read(PAIR_PAYLOAD_CAP + 1)
    if len(raw) > PAIR_PAYLOAD_CAP or not raw.endswith(b"\n"):
        raise PairError("worker payload bound rejected")
    expected_digest = config.get("pair_payload_sha256")
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or hashlib.sha256(raw).hexdigest() != expected_digest
    ):
        raise PairError("worker payload binding rejected")
    binding = config.get("binding")
    if (
        not isinstance(binding, dict)
        or set(binding) != {
            "source_git_sha", "app_target_sha", "source_binding",
            "source_binding_sha256", "external_run_id", "external_run_attempt",
        }
        or not isinstance(binding.get("source_git_sha"), str)
        or SOURCE_SHA_RE.fullmatch(binding["source_git_sha"]) is None
        or not isinstance(binding.get("app_target_sha"), str)
        or SOURCE_SHA_RE.fullmatch(binding["app_target_sha"]) is None
        or not isinstance(binding.get("external_run_id"), str)
        or not binding["external_run_id"].isdecimal()
        or not 1 <= len(binding["external_run_id"]) <= 20
        or not isinstance(binding.get("external_run_attempt"), str)
        or not binding["external_run_attempt"].isdecimal()
        or not 1 <= len(binding["external_run_attempt"]) <= 6
        or os.environ.get("SOURCE_GIT_SHA") != binding.get("source_git_sha")
    ):
        raise PairError("worker source binding rejected")
    payload = _decode_canonical(raw[:-1], cap=PAIR_PAYLOAD_CAP)
    if set(payload) != {"schema", "workload", "actor_payload", "plan"}:
        raise PairError("worker payload shape rejected")
    if type(payload["schema"]) is not int or payload["schema"] != 1 or payload["workload"] != WORKLOAD:
        raise PairError("worker payload version rejected")
    actor_payload = payload["actor_payload"]
    plan = _validate_plan(payload["plan"], allow_future=True)
    if plan["source_sha"] != binding["app_target_sha"]:
        raise PairError("worker active-release binding rejected")
    actors = _validate_actor_payload(actor_payload)
    auth_200, page_200, warm_workers, executor = _warmup(actor_payload, actors)
    warm_ok = (
        auth_200 == COHORT_USERS
        and page_200 == COHORT_USERS
        and warm_workers == COHORT_USERS
    )
    report: dict[str, Any] = {
        "schema": 1,
        "workload": WORKLOAD,
        "status": "incomplete",
        "warmup": {
            "expected_actors": COHORT_USERS,
            "authenticated_200": auth_200,
            "page_warm_200": page_200,
            "worker_threads": warm_workers,
        },
        "off": None,
        "on": None,
        "capacity_slo_credit": False,
        "passed": False,
    }
    if not warm_ok:
        executor.shutdown(wait=False, cancel_futures=True)
        return report
    if plan["off_start_ms"] - (time.time_ns() // 1_000_000) < 10_000:
        executor.shutdown(wait=False, cancel_futures=True)
        report["warmup"]["lead_remaining_ms"] = max(
            0, plan["off_start_ms"] - (time.time_ns() // 1_000_000)
        )
        return report
    try:
        off = _leg(
            actor_payload["origin"], actors,
            (actor_payload["session_cookie_name"], actor_payload["csrf_cookie_name"]),
            plan, "off", executor,
        )
        report["off"] = off
        if not off["complete"] or (time.time_ns() // 1_000_000) > plan["on_start_ms"]:
            return report
        on = _leg(
            actor_payload["origin"], actors,
            (actor_payload["session_cookie_name"], actor_payload["csrf_cookie_name"]),
            plan, "on", executor,
        )
        report["on"] = on
        complete = bool(off["complete"] and on["complete"])
        report["capture_complete"] = complete
        report["quality_gate"] = _quality_gate(
            off, on, service_cpu_delta_percentage_points=None
        )
        report["all_expected_statuses"] = bool(
            complete and off["all_expected_statuses"] and on["all_expected_statuses"]
        )
        # The namespace worker reports capture completion only. The trusted
        # parent evaluates HTTP outcomes and the off/on quality gates after
        # collecting the root-owned process counters.
        report["passed"] = complete
        report["status"] = "complete" if complete else "incomplete"
        return report
    finally:
        # The namespace supervisor owns the hard kill/reap deadline. Never let
        # executor shutdown wait beyond the fixed five-second phase drain.
        executor.shutdown(wait=False, cancel_futures=True)


def _worker_entrypoint() -> int:
    return load_runtime.worker_entry(_worker_callback)


def _warmup(
    payload: dict[str, Any], actors: list[external_load.VirtualUser]
) -> tuple[int, int, int, ThreadPoolExecutor]:
    """Authenticate every actor and prime one keepalive connection per worker."""

    executor = ThreadPoolExecutor(max_workers=COHORT_USERS, thread_name_prefix="cpu-pair")

    barrier = threading.Barrier(COHORT_USERS)
    worker_ids: set[int] = set()
    worker_ids_lock = threading.Lock()

    def authenticate_and_warm(user: external_load.VirtualUser) -> tuple[bool, bool]:
        barrier.wait(timeout=10)
        with worker_ids_lock:
            worker_ids.add(threading.get_ident())
        auth = external_load._request(
            payload["origin"], user, method="GET", path="/users/me",
            phase="cpu_diagnostic_warmup_auth", timeout=30,
            session_cookie_name=payload["session_cookie_name"],
            csrf_cookie_name=payload["csrf_cookie_name"],
            expected_statuses=frozenset({200}), url_prefix="/api/v1",
        )
        if not auth.ok:
            return False, False
        page = external_load._page_request(
            payload["origin"], user, "cpu_diagnostic_warmup_page", 30,
            session_cookie_name=payload["session_cookie_name"],
            csrf_cookie_name=payload["csrf_cookie_name"],
            transport=external_load.HTTP11_KEEPALIVE_TRANSPORT,
        )
        return True, bool(page.ok and page.status == 200)

    try:
        futures = [executor.submit(authenticate_and_warm, actor) for actor in actors]
        done, pending = wait(futures, timeout=45, return_when=ALL_COMPLETED)
        if pending or len(done) != COHORT_USERS:
            for future in pending:
                future.cancel()
            raise PairError("actor warmup deadline exceeded")
        outcomes = [future.result() for future in futures]
        return (
            sum(auth_ok for auth_ok, _page_ok in outcomes),
            sum(page_ok for _auth_ok, page_ok in outcomes),
            len(worker_ids),
            executor,
        )
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise


def _parent(args: argparse.Namespace) -> int:
    # The manifest and SSH identity are only opened by this trusted parent.
    manifest_path = Path(args.manifest)
    handoff_path = Path(args.input_handoff)
    handoff_raw = _read_private_file(handoff_path, cap=16 * 1024)
    try:
        handoff = json.loads(
            handoff_raw.decode("ascii"), object_pairs_hook=_pairs
        )
    except (UnicodeError, json.JSONDecodeError, PairError) as exc:
        raise PairError("external input handoff rejected") from exc
    if not isinstance(handoff, dict):
        raise PairError("external input handoff shape rejected")
    base_handoff_keys = {
        "schema", "confirmation", "target_sha", "control_email", "setup_concurrency",
        "run_id", "profile", "tournament_count", "users_per_tournament", "timeout_diagnostics",
    }
    schema = handoff.get("schema")
    expected_handoff_keys = base_handoff_keys | (
        {"source_binding"} if schema in (2, "2") else set()
    )
    source_binding_handoff = handoff.get("source_binding")
    if (
        set(handoff) != expected_handoff_keys
        or schema not in (1, "1", 2, "2")
        or handoff.get("target_sha") != args.source_sha
        or handoff.get("run_id") != args.external_run_id
        or handoff.get("profile") != "external-vote"
        or handoff.get("tournament_count") != str(EXPECTED_TOURNAMENTS)
        or handoff.get("users_per_tournament") != str(USERS_PER_TOURNAMENT)
    ):
        raise PairError("external input handoff binding rejected")
    if schema in (1, "1"):
        if source_binding_handoff is not None or args.app_target_sha != args.source_sha:
            raise PairError("same-source input binding rejected")
    elif (
        not isinstance(source_binding_handoff, dict)
        or source_binding_handoff.get("runner_sha") != args.source_sha
        or source_binding_handoff.get("app_target_sha") != args.app_target_sha
    ):
        raise PairError("source-bound input handoff rejected")
    try:
        manifest_fd = os.open(
            manifest_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise PairError("fixture manifest is unavailable") from exc
    try:
        manifest_metadata = os.fstat(manifest_fd)
        if (
            not stat.S_ISREG(manifest_metadata.st_mode)
            or manifest_metadata.st_uid != os.geteuid()
            or manifest_metadata.st_nlink != 1
            or stat.S_IMODE(manifest_metadata.st_mode) & 0o077
            or manifest_metadata.st_size <= 0
            or manifest_metadata.st_size > 24 * 1024 * 1024
        ):
            raise PairError("fixture manifest metadata rejected")
        manifest_buffer = bytearray()
        while len(manifest_buffer) <= 24 * 1024 * 1024:
            block = os.read(manifest_fd, min(65_536, 24 * 1024 * 1024 + 1 - len(manifest_buffer)))
            if not block:
                break
            manifest_buffer.extend(block)
        after_read = os.fstat(manifest_fd)
        if (
            len(manifest_buffer) != manifest_metadata.st_size
            or (manifest_metadata.st_dev, manifest_metadata.st_ino, manifest_metadata.st_size,
                manifest_metadata.st_mtime_ns)
            != (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns)
        ):
            raise PairError("fixture manifest changed during read")
        manifest_bytes = bytes(manifest_buffer)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        manifest, users = external_load.load_manifest_bytes(manifest_bytes)
        after_parse = os.fstat(manifest_fd)
        if (
            (manifest_metadata.st_dev, manifest_metadata.st_ino, manifest_metadata.st_size,
             manifest_metadata.st_mtime_ns)
            != (after_parse.st_dev, after_parse.st_ino, after_parse.st_size, after_parse.st_mtime_ns)
        ):
            raise PairError("fixture manifest changed during parse")
    finally:
        os.close(manifest_fd)
    source_binding = manifest.get("source_binding")
    if (
        manifest.get("runner_sha") != args.source_sha
        or manifest.get("app_target_sha") != args.app_target_sha
    ):
        raise PairError("fixture source binding rejected")
    if source_binding is None:
        if (
            args.app_target_sha != args.source_sha
            or manifest.get("source_binding_sha256") is not None
            or source_binding_handoff is not None
        ):
            raise PairError("same-source fixture binding rejected")
    elif (
        not isinstance(source_binding, dict)
        or source_binding.get("runner_sha") != args.source_sha
        or source_binding.get("app_target_sha") != args.app_target_sha
        or not isinstance(manifest.get("source_binding_sha256"), str)
        or source_binding != source_binding_handoff
    ):
        raise PairError("fixture source binding rejected")
    if isinstance(source_binding, dict):
        binding_raw = json.dumps(
            source_binding,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if hashlib.sha256(binding_raw).hexdigest() != manifest.get("source_binding_sha256"):
            raise PairError("fixture source-binding digest rejected")
    tournaments = manifest.get("tournaments")
    if (
        not isinstance(tournaments, list)
        or len(tournaments) != EXPECTED_TOURNAMENTS
        or len(users) != EXPECTED_USERS
        or any(item.get("user_count") != USERS_PER_TOURNAMENT for item in tournaments)
    ):
        raise PairError("canonical fixture dimensions rejected")
    workspace_slug = tournaments[0]["slug"]
    cohort = [user for user in users if user.tournament_slug == workspace_slug][:COHORT_USERS]
    if len(cohort) != COHORT_USERS:
        raise PairError("fixed actor cohort unavailable")
    actors = [
        {
            "tournament_slug": user.tournament_slug,
            "session_token": user.session_token,
            "csrf_token": user.csrf_token,
        }
        for user in cohort
    ]
    actor_payload = {
        "schema": 1,
        "workload": WORKLOAD,
        "origin": manifest["origin"],
        "session_cookie_name": manifest["session_cookie_name"],
        "csrf_cookie_name": manifest["csrf_cookie_name"],
        "actors": actors,
    }
    actor_bytes = json.dumps(
        actor_payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    if len(actor_bytes) > ACTOR_PAYLOAD_CAP:
        raise PairError("actor payload exceeds cap")

    host = os.environ.get("PROD_SSH_HOST", "")
    user = os.environ.get("PROD_SSH_USER", "")
    if HOST_RE.fullmatch(host) is None or USER_RE.fullmatch(user) is None:
        raise PairError("remote control identity rejected")
    ssh_config = Path(args.ssh_config)
    ssh_key = Path(args.ssh_key)
    ssh_dir = ssh_config.parent
    if (
        ssh_key.parent != ssh_dir
        or ssh_dir.name != "external-ssh"
        or ssh_config.name != "config"
        or ssh_key.name != "id_ed25519"
    ):
        raise PairError("remote control key path rejected")
    for path in (ssh_dir, ssh_config, ssh_key):
        metadata = path.lstat()
        if metadata.st_uid != os.geteuid() or stat.S_ISLNK(metadata.st_mode):
            raise PairError("remote control file owner rejected")
    if not stat.S_ISDIR(ssh_dir.lstat().st_mode) or stat.S_IMODE(ssh_dir.lstat().st_mode) & 0o077:
        raise PairError("remote control directory permissions rejected")
    for path in (ssh_config, ssh_key):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PairError("remote control file permissions rejected")

    if Path(args.report).parent.stat().st_uid != os.geteuid():
        raise PairError("report directory owner rejected")
    report_directory = Path(args.report).parent.lstat()
    if (
        not stat.S_ISDIR(report_directory.st_mode)
        or stat.S_ISLNK(report_directory.st_mode)
        or stat.S_IMODE(report_directory.st_mode) & 0o077
    ):
        raise PairError("report directory permissions rejected")
    ssh_dir_identity = _validate_private_ssh_directory(ssh_dir)
    ssh_files = {
        name: bytearray(_read_private_file(ssh_dir / name, cap=64 * 1024))
        for name in ("config", "id_ed25519", "known_hosts")
    }
    ssh_lifecycle: dict[str, Any] = {
        "closed": True,
        "mux_off_enforced": False,
        "last_command_returned": False,
        "directory_dev": ssh_dir_identity.st_dev,
        "directory_ino": ssh_dir_identity.st_ino,
    }
    plan: dict[str, Any] = {
        "schema": 1,
        "operation": "prepare",
        "run_id": os.urandom(16).hex(),
        "source_sha": args.app_target_sha,
        "workload": WORKLOAD,
    }
    now_ms = time.time_ns() // 1_000_000
    plan.update({
        "off_start_ms": now_ms + PLAN_LEAD_MS,
        "off_end_ms": now_ms + PLAN_LEAD_MS + LEG_MS,
        "on_start_ms": now_ms + PLAN_LEAD_MS + LEG_MS + IDLE_MS,
        "on_end_ms": now_ms + PLAN_LEAD_MS + (2 * LEG_MS) + IDLE_MS,
    })
    plan_bytes = json.dumps(
        plan, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    if len(plan_bytes) > PLAN_CAP:
        raise PairError("plan exceeds cap")
    prepare_attempted = False
    prepared = False
    ssh_hidden = {"value": False}
    status = 2
    report: dict[str, Any] = {
        "schema": 1,
        "workload": WORKLOAD,
        "source_sha": args.source_sha,
        "app_target_sha": args.app_target_sha,
        "external_run_id": args.external_run_id,
        "external_run_attempt": args.external_run_attempt,
        "status": "incomplete",
        "capacity_slo_credit": False,
        "diagnostic_credit": False,
        "canonical_fixture": {
            "tournaments": EXPECTED_TOURNAMENTS,
            "users": EXPECTED_USERS,
            "users_per_tournament": USERS_PER_TOURNAMENT,
            "selected_actors": COHORT_USERS,
        },
        "fixture_manifest_sha256": manifest_sha,
        "input_handoff_sha256": hashlib.sha256(handoff_raw).hexdigest(),
        "namespace_closed": False,
        "plan_cleanup": "unknown",
        "ssh_material_safe_to_remove": False,
        "ssh_material_binding": None,
        "result": None,
    }
    worker_report_path = Path(args.report).with_name(Path(args.report).name + ".worker")
    ssh_lifecycle_receipt_path = Path(args.report).with_name("ssh-lifecycle.json")
    if _path_exists_nofollow(ssh_lifecycle_receipt_path):
        raise PairError("SSH lifecycle receipt destination already exists")
    try:
        prepare_attempted = True
        prepared, release_slug, _unused_usage = _ssh_plan(
            args, host, user, ssh_config, ssh_key, plan, ssh_lifecycle
        )
        if not prepared:
            raise PairError("root plan prepare failed")
        if release_slug is None or RELEASE_SLUG_RE.fullmatch(release_slug) is None:
            raise PairError("root release binding rejected")
        plan["release_slug"] = release_slug
        _hide_ssh_material(ssh_dir, ssh_files, ssh_lifecycle, ssh_hidden)
        lifecycle_receipt = {
            "schema": 1,
            "event": "cpu_diagnostic_ssh_lifecycle",
            "source_sha": args.source_sha,
            "run_id": args.external_run_id,
            "attempt": args.external_run_attempt,
            "config_dir_dev": ssh_dir_identity.st_dev,
            "config_dir_ino": ssh_dir_identity.st_ino,
            "material_hidden": True,
        }
        _require_path_absent(ssh_dir)
        _write_report(ssh_lifecycle_receipt_path, lifecycle_receipt)
        _validate_ssh_lifecycle_receipt(
            ssh_lifecycle_receipt_path,
            expected={
                "source_sha": args.source_sha,
                "run_id": args.external_run_id,
                "attempt": args.external_run_attempt,
                "config_dir_dev": ssh_dir_identity.st_dev,
                "config_dir_ino": ssh_dir_identity.st_ino,
            },
        )
        _require_path_absent(ssh_dir)

        pair_payload = {
            "schema": 1,
            "workload": WORKLOAD,
            "actor_payload": actor_payload,
            "plan": {key: value for key, value in plan.items() if key != "operation"},
        }
        pair_bytes = json.dumps(
            pair_payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"
        if len(pair_bytes) > PAIR_PAYLOAD_CAP:
            raise PairError("worker payload exceeds cap")
        binding = {
            "source_git_sha": args.source_sha,
            "app_target_sha": args.app_target_sha,
            "source_binding": source_binding,
            "source_binding_sha256": manifest.get("source_binding_sha256"),
            "external_run_id": args.external_run_id,
            "external_run_attempt": args.external_run_attempt,
        }
        report_path = Path(args.report)
        if worker_report_path.exists() or worker_report_path.is_symlink():
            raise PairError("worker report destination already exists")
        worker_budget_seconds = max(
            1.0,
            (plan["on_end_ms"] + 5_000 - time.time_ns() // 1_000_000) / 1000 + 8.0,
        )
        result = load_runtime.run_supervised(
            worker_command=(sys.executable, str(Path(__file__).resolve()), "worker-entry"),
            report_path=report_path.with_name(report_path.name + ".supervisor"),
            worker_report_path=worker_report_path,
            max_duration_seconds=worker_budget_seconds,
            max_runner_minutes=3,
            worker_config={
                "binding": binding,
                "pair_payload_sha256": hashlib.sha256(pair_bytes).hexdigest(),
            },
            worker_stdin_payload=pair_bytes,
            worker_stdin_payload_max_bytes=PAIR_PAYLOAD_CAP,
            clear_worker_environment=True,
            env={"SOURCE_GIT_SHA": args.source_sha},
        )
        child_report: dict[str, Any] | None = None
        try:
            child_raw = _read_private_file(worker_report_path, cap=REPORT_CAP)
            parsed = json.loads(child_raw.decode("ascii"))
            if isinstance(parsed, dict):
                child_report = parsed
        except (OSError, UnicodeError, json.JSONDecodeError):
            child_report = None
        closed = bool(result.namespace_closed and result.reason == "none")
        valid_child = bool(
            child_report is not None
            and child_report.get("report_complete") is True
            and child_report.get("workload") == WORKLOAD
            and child_report.get("capacity_slo_credit") is False
            and child_report.get("namespace_closed") is False
        )
        report: dict[str, Any] = {
            "schema": 1,
            "workload": WORKLOAD,
            "source_sha": args.source_sha,
            "app_target_sha": args.app_target_sha,
            "external_run_id": args.external_run_id,
            "external_run_attempt": args.external_run_attempt,
            "status": "incomplete",
            "capacity_slo_credit": False,
            "diagnostic_credit": False,
            "canonical_fixture": {
                "tournaments": EXPECTED_TOURNAMENTS,
                "users": EXPECTED_USERS,
                "users_per_tournament": USERS_PER_TOURNAMENT,
                "selected_actors": COHORT_USERS,
            },
            "fixture_manifest_sha256": manifest_sha,
            "namespace_closed": closed,
            "ssh_material_safe_to_remove": False,
            "worker_exit_code": (
                result.returncode if result.returncode in {0, 1, 2} else None
            ),
            "supervisor_reason": (
                result.reason
                if result.reason in {
                    "none", "max_duration_seconds", "max_runner_minutes",
                    "external_signal", "namespace_start_failed", "namespace_not_closed",
                    "worker_report_invalid", "worker_exit_nonzero",
                }
                else "other"
            ),
            "plan_cleanup": "pending",
            "ssh_material_binding": None,
            "result": {
                key: child_report.get(key)
                for key in (
                    "status", "warmup", "off", "on", "capture_complete",
                    "all_expected_statuses", "quality_gate",
                )
            } if valid_child and child_report is not None else None,
        }
        status = 0 if report["status"] == "complete" else 2
    finally:
        if prepare_attempted:
            cleaned = False
            usage_summary: dict[str, Any] | None = None
            try:
                if ssh_lifecycle.get("closed") is not True:
                    report["plan_cleanup"] = "failed"
                else:
                    _wait_past(plan["on_end_ms"] + 1_000)
                    _restore_hidden_ssh_material(
                        ssh_dir, ssh_files, ssh_hidden, ssh_lifecycle
                    )
                    cleanup_request = {"schema": 1, "operation": "cleanup", "run_id": plan["run_id"]}
                    cleaned, _cleanup_slug, usage_summary = _ssh_plan(
                        args, host, user, ssh_config, ssh_key, cleanup_request,
                        ssh_lifecycle,
                        allow_noop_cleanup=not prepared,
                    )
                    report["plan_cleanup"] = "complete" if cleaned else "failed"
            except (OSError, PairError, subprocess.SubprocessError):
                report["plan_cleanup"] = "failed"
            finally:
                if ssh_lifecycle.get("closed") is True:
                    try:
                        metadata = _validate_private_ssh_directory(ssh_dir)
                        can_remove_material = bool(
                            ssh_lifecycle.get("mux_off_enforced") is True
                            and ssh_lifecycle.get("last_command_returned") is True
                        )
                        if can_remove_material:
                            report["ssh_material_binding"] = {
                                "source_sha": args.source_sha,
                                "run_id": args.external_run_id,
                                "attempt": args.external_run_attempt,
                                "config_dir_dev": metadata.st_dev,
                                "config_dir_ino": metadata.st_ino,
                            }
                            report["ssh_material_safe_to_remove"] = True
                    except PairError:
                        cleaned = False
            if not cleaned:
                status = 2
                report["status"] = "incomplete"
            if cleaned and usage_summary is not None:
                report["service_cpu_usage"] = {
                    "status": usage_summary["usage_status"],
                    "reason": usage_summary["usage_reason"],
                    "rows": usage_summary["usage_rows"],
                }
                report["cpu_profile_attribution"] = {
                    "status": usage_summary["profile_status"],
                    "reason": usage_summary["profile_reason"],
                    "rows": usage_summary["profile_rows"],
                }
                pair_result = report.get("result")
                if isinstance(pair_result, dict):
                    off_leg = pair_result.get("off")
                    on_leg = pair_result.get("on")
                    off_leg = off_leg if isinstance(off_leg, dict) else {}
                    on_leg = on_leg if isinstance(on_leg, dict) else {}
                    deltas = _usage_delta_percentage_points(usage_summary)
                    quality = _quality_gate(
                        off_leg, on_leg, service_cpu_delta_percentage_points=deltas
                    )
                    pair_result["quality_gate"] = quality
                    completed_capture = pair_result.get("capture_complete") is True
                    all_statuses = pair_result.get("all_expected_statuses") is True
                    quality_ok = quality.get("status") == "passed"
                    overall_complete = bool(
                        closed and valid_child and completed_capture and all_statuses
                        and usage_summary.get("usage_status") == "complete" and quality_ok
                    )
                    report["status"] = "complete" if overall_complete else "incomplete"
                    status = 0 if overall_complete else 2
        for content in ssh_files.values():
            content[:] = b"\0" * len(content)
        try:
            _write_report(Path(args.report), report)
        except (OSError, PairError, ValueError):
            status = 2
    return status


def _ssh_plan(
    args: argparse.Namespace,
    host: str,
    user: str,
    config: Path,
    key: Path,
    payload: dict[str, Any],
    lifecycle: dict[str, Any],
    *,
    allow_noop_cleanup: bool = False,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    lifecycle["last_command_returned"] = False
    data = json.dumps(
        payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"
    command = [
        "/usr/bin/ssh", "-F", str(config), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
        "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ControlPath=none",
        "-i", str(key), f"{user}@{host}", "/usr/bin/python3.12", "-I", "-B",
        "/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py",
        "cpu-diagnostic-plan",
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        return False, None, None
    lifecycle["closed"] = False
    lifecycle["mux_off_enforced"] = True
    output = bytearray()
    selector: selectors.BaseSelector | None = None
    try:
        if process.stdin is None or process.stdout is None:
            raise PairError("SSH process pipes were not created")
        selector = selectors.DefaultSelector()
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        process.stdin.write(data)
        process.stdin.flush()
        process.stdin.close()
        deadline = time.monotonic() + 20
        eof = False
        while process.poll() is None or not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_worker(process)
                lifecycle["closed"] = True
                return False, None, None
            for key_event, _mask in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key_event.fd, ROOT_USAGE_OUTPUT_CAP + 1 - len(output))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key_event.fd)
                    eof = True
                    continue
                output.extend(chunk)
                if len(output) > ROOT_USAGE_OUTPUT_CAP:
                    _terminate_worker(process)
                    lifecycle["closed"] = True
                    return False, None, None
        _confirm_worker_closed(process)
        lifecycle["closed"] = True
        lifecycle["last_command_returned"] = True
        if process.returncode != 0 or not output.endswith(b"\n") or output.count(b"\n") != 1:
            return False, None, None
        if payload.get("operation") == "prepare":
            try:
                text = bytes(output).decode("ascii")
            except UnicodeError:
                return False, None, None
            match = re.fullmatch(
                r"CPU_DIAGNOSTIC_PLAN status=prepared targets=3 release_slug=([a-z0-9][a-z0-9-]{0,79})\n",
                text,
            )
            return (True, match.group(1), None) if match else (False, None, None)
        prefix = b"CPU_DIAGNOSTIC_PLAN "
        if not bytes(output).startswith(prefix):
            return False, None, None
        try:
            usage_value = _decode_canonical(bytes(output)[len(prefix):-1], cap=ROOT_USAGE_OUTPUT_CAP)
        except PairError:
            return False, None, None
        usage = _validate_usage_summary(usage_value)
        if usage is None or (usage["service_count"] == 0 and not allow_noop_cleanup):
            return False, None, None
        return True, None, usage
    except (OSError, subprocess.SubprocessError):
        _terminate_worker(process)
        lifecycle["closed"] = True
        return False, None, None
    finally:
        if selector is not None:
            selector.close()
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        if process.poll() is None:
            _terminate_worker(process)
            lifecycle["closed"] = True
        else:
            _confirm_worker_closed(process)
            lifecycle["closed"] = True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise PairError("SSH process group state is not observable") from exc
    except OSError as exc:
        raise PairError("SSH process group state is not observable") from exc
    return True


def _wait_process_group_closed(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group_id):
            return True
        time.sleep(0.025)
    return not _process_group_exists(process_group_id)


def _confirm_worker_closed(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=0)
    except subprocess.TimeoutExpired as exc:
        _terminate_worker(process)
        raise PairError("SSH child did not exit before returning") from exc
    if _process_group_exists(process.pid):
        _terminate_worker(process)
        raise PairError("SSH process group outlived its child")


def _validate_private_ssh_directory(directory: Path) -> os.stat_result:
    try:
        before = directory.lstat()
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            raise PairError("SSH private directory identity rejected")
        descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        try:
            opened = os.fstat(descriptor)
            after = directory.lstat()
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or stat.S_IMODE(opened.st_mode) != 0o700
                or (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (opened.st_dev, opened.st_ino)
                != (after.st_dev, after.st_ino)
            ):
                raise PairError("SSH private directory changed during validation")
            return opened
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PairError("SSH private directory identity unavailable") from exc


def _path_exists_nofollow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PairError("SSH lifecycle path state unavailable") from exc
    return True


def _require_path_absent(path: Path) -> None:
    if _path_exists_nofollow(path):
        raise PairError("SSH material path remains after hide")


def _validate_ssh_lifecycle_receipt(path: Path, *, expected: dict[str, Any]) -> None:
    raw = _read_private_file(path, cap=SSH_LIFECYCLE_RECEIPT_CAP, exact_mode=0o600)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise PairError("SSH lifecycle receipt encoding rejected")
    receipt = _decode_canonical(raw[:-1], cap=SSH_LIFECYCLE_RECEIPT_CAP)
    if set(receipt) != {
        "schema", "event", "source_sha", "run_id", "attempt",
        "config_dir_dev", "config_dir_ino", "material_hidden",
    }:
        raise PairError("SSH lifecycle receipt schema rejected")
    if (
        type(receipt.get("schema")) is not int
        or receipt["schema"] != 1
        or receipt.get("event") != "cpu_diagnostic_ssh_lifecycle"
        or receipt.get("material_hidden") is not True
        or not isinstance(receipt.get("source_sha"), str)
        or not isinstance(receipt.get("run_id"), str)
        or not isinstance(receipt.get("attempt"), str)
        or type(receipt.get("config_dir_dev")) is not int
        or type(receipt.get("config_dir_ino")) is not int
    ):
        raise PairError("SSH lifecycle receipt state rejected")
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise PairError("SSH lifecycle receipt binding rejected")


def _remove_ssh_files(
    directory: Path, contents: dict[str, bytearray], lifecycle: dict[str, Any]
) -> None:
    if lifecycle.get("closed") is not True:
        raise PairError("SSH process lifecycle is not closed")
    metadata = _validate_private_ssh_directory(directory)
    if (metadata.st_dev, metadata.st_ino) != (
        lifecycle.get("directory_dev"), lifecycle.get("directory_ino")
    ):
        raise PairError("SSH private directory identity changed")
    for name in contents:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise PairError("SSH private material path changed")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        if path.exists() or path.is_symlink():
            raise PairError("SSH private material remains")
    try:
        directory.rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PairError("SSH private directory is not empty") from exc


def _restore_ssh_files(
    directory: Path, contents: dict[str, bytearray], lifecycle: dict[str, Any]
) -> None:
    directory_fd = -1
    try:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory_fd = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
        directory_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise PairError("SSH private directory changed during restore")
        lifecycle["directory_dev"] = directory_metadata.st_dev
        lifecycle["directory_ino"] = directory_metadata.st_ino
        existing_names = set(os.listdir(directory_fd))
        if not existing_names <= set(contents):
            raise PairError("unexpected SSH material remains during restore")
        for name, data in contents.items():
            if name not in {"config", "id_ed25519", "known_hosts"} or not data:
                raise PairError("SSH private material set is invalid")
            try:
                fd = os.open(
                    name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
                )
            except FileNotFoundError:
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise PairError("SSH private material restore failed")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                continue
            try:
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size != len(data)
                ):
                    raise PairError("existing SSH private material changed")
                current = bytearray()
                while len(current) <= len(data):
                    block = os.read(fd, min(4096, len(data) + 1 - len(current)))
                    if not block:
                        break
                    current.extend(block)
                if current != data:
                    raise PairError("existing SSH private material mismatch")
            finally:
                os.close(fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise PairError("SSH private material restore failed") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _hide_ssh_material(
    directory: Path,
    contents: dict[str, bytearray],
    lifecycle: dict[str, Any],
    state: dict[str, bool],
) -> None:
    if lifecycle.get("closed") is not True:
        raise PairError("SSH process lifecycle is not closed")
    # Record the hidden state before unlinking any file so partial removal
    # always enters the restoration path.
    state["value"] = True
    _remove_ssh_files(directory, contents, lifecycle)


def _restore_hidden_ssh_material(
    directory: Path,
    contents: dict[str, bytearray],
    state: dict[str, bool],
    lifecycle: dict[str, Any],
) -> None:
    if state.get("value") is True:
        _restore_ssh_files(directory, contents, lifecycle)
        state["value"] = False


def _read_worker_line(stream: Any, process: subprocess.Popen[bytes], *, deadline: float) -> bytes:
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    result = bytearray()
    try:
        while time.monotonic() < deadline and len(result) <= 512:
            if not selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                if process.poll() is not None:
                    break
                continue
            block = os.read(stream.fileno(), 513 - len(result))
            if not block:
                break
            result.extend(block)
            if b"\n" in result:
                if result.endswith(b"\n") and result.count(b"\n") == 1:
                    return bytes(result)
                break
    finally:
        selector.close()
    raise PairError("worker readiness protocol rejected")


def _read_worker_all(stream: Any, process: subprocess.Popen[bytes], *, deadline: float) -> bytes:
    selector = selectors.DefaultSelector()
    selector.register(stream, selectors.EVENT_READ)
    result = bytearray()
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PairError("worker deadline exceeded")
            if not selector.select(min(remaining, 0.1)):
                if process.poll() is not None:
                    break
                continue
            block = os.read(stream.fileno(), min(4096, WORKER_OUTPUT_CAP + 1 - len(result)))
            if not block:
                selector.unregister(stream)
                continue
            result.extend(block)
            if len(result) > WORKER_OUTPUT_CAP:
                raise PairError("worker output exceeded cap")
    finally:
        selector.close()
    return bytes(result)


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, 15)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise PairError("SSH process group could not be terminated") from exc
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        except OSError as exc:
            raise PairError("SSH process group could not be killed") from exc
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise PairError("SSH child could not be reaped") from exc
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise PairError("SSH process group could not be killed") from exc
        if not _wait_process_group_closed(process.pid, 2):
            raise PairError("SSH process group remained after kill")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worker-entry")
    parent = sub.add_parser("parent")
    parent.add_argument("--manifest", required=True)
    parent.add_argument("--input-handoff", required=True)
    parent.add_argument("--report", required=True)
    parent.add_argument("--source-sha", required=True)
    parent.add_argument("--app-target-sha", required=True)
    parent.add_argument("--external-run-id", required=True)
    parent.add_argument("--external-run-attempt", required=True)
    parent.add_argument("--ssh-config", required=True)
    parent.add_argument("--ssh-key", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "worker-entry":
            return _worker_entrypoint()
        if (
            SOURCE_SHA_RE.fullmatch(args.source_sha) is None
            or SOURCE_SHA_RE.fullmatch(args.app_target_sha) is None
            or not args.external_run_id.isdecimal()
            or not 1 <= len(args.external_run_id) <= 20
            or not args.external_run_attempt.isdecimal()
            or not 1 <= len(args.external_run_attempt) <= 6
        ):
            raise PairError("parent source binding rejected")
        return _parent(args)
    except (PairError, external_load.ExternalLoadError, OSError, ValueError):
        if args.command == "parent":
            try:
                destination = Path(args.report)
                if not destination.exists() and not destination.is_symlink():
                    _write_report(
                        destination,
                        {
                            "schema": 1,
                            "workload": WORKLOAD,
                            "status": "incomplete",
                            "failure_stage": "parent_preflight_or_cleanup",
                            "capacity_slo_credit": False,
                            "diagnostic_credit": False,
                            "namespace_closed": False,
                            "plan_cleanup": "unknown",
                            "result": None,
                        },
                    )
            except (OSError, PairError, ValueError):
                pass
        print("CPU_DIAGNOSTIC_PAIR status=failed credit=false")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
