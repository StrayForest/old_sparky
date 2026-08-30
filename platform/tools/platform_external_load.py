#!/usr/bin/env python3
"""Implement the bounded public load client used by ``platform_load.py``.

The fixture is prepared on the production host, but this client is intended to
run on an external runner.  Its manifest contains temporary session material;
the client deliberately never prints or serializes that material.  This module
is an implementation detail; canonical scenario values and acceptance budgets
must come from a versioned profile through ``platform_load.py``.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import random
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools.platform_load_acceptance import evaluate_acceptance
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_load_acceptance import evaluate_acceptance


EXPECTED_ORIGIN = "https://old-sparky.com"
MANIFEST_SCHEMA = 1
MAX_USERS = 20_000
MAX_TOURNAMENTS = 64
MAX_CONCURRENCY = 512
RESPONSE_BODY_LIMIT = 2 * 1024 * 1024
ERROR_SAMPLE_LIMIT = 25
MARKER_RE = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,139}$")
COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")


class ExternalLoadError(RuntimeError):
    """Raised when a manifest or load contract is invalid."""


@dataclass(frozen=True, slots=True)
class VirtualUser:
    user_id: str
    tournament_slug: str
    session_token: str
    csrf_token: str


@dataclass(slots=True)
class RequestResult:
    phase: str
    method: str
    path: str
    status: int
    elapsed_ms: float
    ok: bool
    response_bytes: int
    cf_ray: str | None = None
    response_etag: str | None = None
    error_kind: str | None = None
    response_json: Any = None
    attempt_number: int = 1


@dataclass(slots=True)
class LogicalRequestResult:
    """One user action, including every temporary-overload retry attempt."""

    attempts: list[RequestResult]
    elapsed_ms: float
    user_id: str | None = None

    @property
    def final(self) -> RequestResult:
        return self.attempts[-1]

    @property
    def retry_count(self) -> int:
        return max(0, len(self.attempts) - 1)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def metric_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(values),
        "avg_ms": round(sum(values) / len(values), 3),
        "p50_ms": round(percentile(values, 50) or 0, 3),
        "p90_ms": round(percentile(values, 90) or 0, 3),
        "p95_ms": round(percentile(values, 95) or 0, 3),
        "p99_ms": round(percentile(values, 99) or 0, 3),
        "max_ms": round(max(values), 3),
    }


def spread_offsets(count: int, spread_seconds: float) -> list[float]:
    """Return deterministic starts in [0, spread_seconds) for a phase."""

    if count <= 0:
        return []
    if count == 1 or spread_seconds <= 0:
        return [0.0] * count
    step = spread_seconds / count
    return [round(index * step, 6) for index in range(count)]


def _required_text(value: Any, *, field: str, min_length: int = 1) -> str:
    if not isinstance(value, str) or len(value) < min_length:
        raise ExternalLoadError(f"manifest {field} is invalid")
    return value


def load_manifest(path: Path) -> tuple[dict[str, Any], list[VirtualUser]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalLoadError("external load manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ExternalLoadError("external load manifest must be an object")
    if payload.get("schema") != MANIFEST_SCHEMA:
        raise ExternalLoadError("external load manifest schema is unsupported")
    if payload.get("purpose") != "external_ready_vote":
        raise ExternalLoadError("external load manifest purpose is invalid")
    if str(payload.get("origin") or "").rstrip("/") != EXPECTED_ORIGIN:
        raise ExternalLoadError("external load must use the canonical public origin")
    marker = _required_text(payload.get("marker"), field="marker")
    if MARKER_RE.fullmatch(marker) is None:
        raise ExternalLoadError("external load marker is invalid")
    for field in ("session_cookie_name", "csrf_cookie_name"):
        cookie_name = _required_text(payload.get(field), field=field)
        if COOKIE_NAME_RE.fullmatch(cookie_name) is None:
            raise ExternalLoadError(f"manifest {field} is invalid")

    raw_tournaments = payload.get("tournaments")
    if not isinstance(raw_tournaments, list) or not 0 < len(raw_tournaments) <= MAX_TOURNAMENTS:
        raise ExternalLoadError("manifest tournaments are invalid")
    tournament_slugs: set[str] = set()
    tournament_expected_counts: dict[str, int] = {}
    for raw_tournament in raw_tournaments:
        if not isinstance(raw_tournament, dict):
            raise ExternalLoadError("manifest tournament entry is invalid")
        slug = _required_text(raw_tournament.get("slug"), field="tournament.slug")
        if SLUG_RE.fullmatch(slug) is None or slug in tournament_slugs:
            raise ExternalLoadError("manifest tournament slug is invalid or duplicated")
        try:
            expected_count = int(raw_tournament.get("user_count"))
        except (TypeError, ValueError) as exc:
            raise ExternalLoadError("manifest tournament user_count is invalid") from exc
        if expected_count <= 0:
            raise ExternalLoadError("manifest tournament user_count must be positive")
        tournament_slugs.add(slug)
        tournament_expected_counts[slug] = expected_count

    raw_users = payload.get("users")
    if not isinstance(raw_users, list) or not 0 < len(raw_users) <= MAX_USERS:
        raise ExternalLoadError("manifest users are invalid")
    users: list[VirtualUser] = []
    user_ids: set[str] = set()
    session_tokens: set[str] = set()
    actual_counts: Counter[str] = Counter()
    for raw_user in raw_users:
        if not isinstance(raw_user, dict):
            raise ExternalLoadError("manifest user entry is invalid")
        user_id = _required_text(raw_user.get("user_id"), field="user_id", min_length=8)
        slug = _required_text(raw_user.get("tournament_slug"), field="tournament_slug")
        session_token = _required_text(
            raw_user.get("session_token"), field="session_token", min_length=32
        )
        csrf_token = _required_text(
            raw_user.get("csrf_token"), field="csrf_token", min_length=32
        )
        if user_id in user_ids or session_token in session_tokens:
            raise ExternalLoadError("manifest users contain duplicate identity material")
        if slug not in tournament_slugs:
            raise ExternalLoadError("manifest user references an unknown tournament")
        user_ids.add(user_id)
        session_tokens.add(session_token)
        actual_counts[slug] += 1
        users.append(
            VirtualUser(
                user_id=user_id,
                tournament_slug=slug,
                session_token=session_token,
                csrf_token=csrf_token,
            )
        )
    if actual_counts != Counter(tournament_expected_counts):
        raise ExternalLoadError("manifest tournament counts do not match user entries")
    return payload, users


def _trace(origin: str, timeout: float) -> dict[str, Any]:
    request = Request(
        f"{origin}/cdn-cgi/trace",
        method="GET",
        headers={"User-Agent": "old-sparky-external-load/1"},
    )
    try:
        # The origin is validated against a fixed HTTPS allowlist before this
        # function is called; no user-controlled URL is accepted here.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            raw = response.read(16_384).decode("utf-8", errors="replace")
            values: dict[str, str] = {}
            for line in raw.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"ip", "colo", "loc", "http", "tls"}:
                    values[key] = value[:128]
            values["status"] = str(response.status)
            values["cf_ray"] = response.headers.get("cf-ray", "")[:128]
            return values
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return {"error": type(exc).__name__}


def _request(
    origin: str,
    user: VirtualUser,
    *,
    method: str,
    path: str,
    phase: str,
    timeout: float,
    session_cookie_name: str,
    csrf_cookie_name: str,
    json_payload: dict[str, Any] | None = None,
    expected_statuses: frozenset[int] = frozenset({200}),
    extra_headers: dict[str, str] | None = None,
    attempt_number: int = 1,
) -> RequestResult:
    body = None
    headers = {
        "Accept": "application/json",
        "Origin": origin,
        "User-Agent": "old-sparky-external-load/1",
        "Cookie": (
            f"{session_cookie_name}={user.session_token}; "
            f"{csrf_cookie_name}={user.csrf_token}"
        ),
        "X-CSRF-Token": user.csrf_token,
        "X-Platform-QA-Phase": phase,
    }
    if extra_headers:
        headers.update(extra_headers)
    if json_payload is not None:
        body = json.dumps(json_payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{origin}/api/v1{path}",
        data=body,
        method=method,
        headers=headers,
    )
    started_at = time.monotonic()
    status = 0
    response_bytes = 0
    cf_ray: str | None = None
    response_etag: str | None = None
    error_kind: str | None = None
    response_json: Any = None
    try:
        # URL is constructed only from the fixed manifest origin and a route
        # selected by this module; this is not an arbitrary fetch primitive.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            status = int(response.status)
            cf_ray = response.headers.get("cf-ray", "")[:128] or None
            response_etag = response.headers.get("etag", "")[:512] or None
            raw_body = response.read(RESPONSE_BODY_LIMIT)
            response_bytes = len(raw_body)
            if raw_body:
                try:
                    response_json = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response_json = None
    except HTTPError as exc:
        status = int(exc.code)
        cf_ray = exc.headers.get("cf-ray", "")[:128] or None
        with_error_body = exc.read(RESPONSE_BODY_LIMIT)
        response_bytes = len(with_error_body)
        error_kind = "http_error"
        if with_error_body:
            try:
                response_json = json.loads(with_error_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_json = None
    except (URLError, TimeoutError, OSError) as exc:
        error_kind = type(exc).__name__
    elapsed_ms = (time.monotonic() - started_at) * 1000
    ok = status in expected_statuses
    if not ok and error_kind is None:
        error_kind = "unexpected_status"
    return RequestResult(
        phase=phase,
        method=method,
        path=path,
        status=status,
        elapsed_ms=elapsed_ms,
        ok=ok,
        response_bytes=response_bytes,
        cf_ray=cf_ray,
        response_etag=response_etag,
        error_kind=error_kind,
        response_json=response_json,
        attempt_number=attempt_number,
    )


def _route_for_read(index: int, slug: str) -> str:
    bucket = index % 10
    if bucket < 5:
        # Match the current tournament page request exactly: the page loads
        # the detail shell and the schedule, while participant pagination is
        # requested separately only by views that actually render it.
        return (
            f"/tournaments/{slug}/workspace?participants_limit=0"
            "&participants_offset=0&workspace_view=detail&include_current_user=false"
        )
    if bucket < 8:
        return f"/tournaments/{slug}"
    if bucket == 8:
        return "/users/me"
    return "/tournaments"


def run_phase(
    origin: str,
    users: list[VirtualUser],
    *,
    phase: str,
    spread_seconds: float,
    concurrency: int,
    timeout: float,
    request_builder,
) -> list[Any]:
    offsets = spread_offsets(len(users), spread_seconds)
    phase_started_at = time.monotonic()
    results: list[Any] = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="external-load") as executor:
        futures: list[Future[Any]] = []
        for user, offset in zip(users, offsets, strict=True):
            delay = phase_started_at + offset - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            futures.append(executor.submit(request_builder, origin, user, phase, timeout))
        for future in as_completed(futures):
            results.append(future.result())
    return results


def run_rate_phase(
    origin: str,
    users: list[VirtualUser],
    *,
    phase: str,
    duration_seconds: float,
    concurrency: int,
    timeout: float,
    request_builder,
) -> tuple[list[Any], float]:
    """Run a paced phase and return its submission window separately from drain time."""

    offsets = spread_offsets(len(users), duration_seconds)
    phase_started_at = time.monotonic()
    first_submission_at: float | None = None
    last_submission_at: float | None = None
    futures: list[Future[Any]] = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="external-load") as executor:
        for user, offset in zip(users, offsets, strict=True):
            delay = phase_started_at + offset - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            submitted_at = time.monotonic()
            first_submission_at = submitted_at if first_submission_at is None else first_submission_at
            last_submission_at = submitted_at
            futures.append(executor.submit(request_builder, origin, user, phase, timeout))
        results = [future.result() for future in as_completed(futures)]
    if first_submission_at is None or last_submission_at is None:
        submission_window_seconds = 0.001
    elif len(users) == 1:
        submission_window_seconds = max(0.001, duration_seconds)
    else:
        submission_window_seconds = max(0.001, last_submission_at - first_submission_at)
    return results, submission_window_seconds


def summarize_results(results: list[RequestResult]) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    by_route: dict[str, list[float]] = defaultdict(list)
    latencies: list[float] = []
    errors = 0
    temporary_overloads = 0
    retry_attempts = 0
    error_kinds: Counter[str] = Counter()
    error_samples: list[dict[str, Any]] = []
    changed = Counter()
    for result in results:
        status_counts[str(result.status)] += 1
        route = f"{result.method} {result.path.split('?', 1)[0]}"
        by_route[route].append(result.elapsed_ms)
        latencies.append(result.elapsed_ms)
        retry_attempts += int(result.attempt_number > 1)
        if _ready_vote_overload(result):
            temporary_overloads += 1
        if not result.ok:
            errors += 1
            kind = result.error_kind or "unexpected"
            error_kinds[kind] += 1
            if len(error_samples) < ERROR_SAMPLE_LIMIT:
                error_samples.append(
                    {
                        "phase": result.phase,
                        "method": result.method,
                        "path": result.path.split("?", 1)[0],
                        "status": result.status,
                        "error_kind": kind,
                        "cf_ray": result.cf_ray,
                    }
                )
        if isinstance(result.response_json, dict) and "changed" in result.response_json:
            changed[str(bool(result.response_json.get("changed")))] += 1
    return {
        "scope": "full_population",
        "requests": len(results),
        "errors": errors,
        "successful_responses": len(results) - errors,
        "final_failure_rate_percent": round(
            errors * 100 / max(1, len(results)),
            4,
        ),
        "temporary_overload_responses": temporary_overloads,
        "temporary_overload_rate_percent": round(
            temporary_overloads * 100 / max(1, len(results)),
            4,
        ),
        "retry_attempts": retry_attempts,
        "unexpected_statuses": max(0, errors - temporary_overloads),
        "status_counts": dict(sorted(status_counts.items())),
        "error_kinds": dict(sorted(error_kinds.items())),
        "changed_counts": dict(sorted(changed.items())),
        "latency": metric_stats(latencies),
        "by_route": {
            route: metric_stats(values)
            for route, values in sorted(
                by_route.items(),
                key=lambda item: (len(item[1]), max(item[1])),
                reverse=True,
            )
        },
        "cf_ray_count": len({result.cf_ray for result in results if result.cf_ray}),
        "error_samples": error_samples,
    }


def _ready_vote_request(
    origin: str,
    user: VirtualUser,
    phase: str,
    timeout: float,
    *,
    session_cookie_name: str,
    csrf_cookie_name: str,
    attempt_number: int = 1,
) -> RequestResult:
    return _request(
        origin,
        user,
        method="POST",
        path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
        phase=phase,
        timeout=timeout,
        session_cookie_name=session_cookie_name,
        csrf_cookie_name=csrf_cookie_name,
        json_payload={"choice": "yes"},
        attempt_number=attempt_number,
    )


def _ready_vote_overload(result: RequestResult) -> bool:
    payload = result.response_json
    return bool(
        result.status == 503
        and isinstance(payload, dict)
        and payload.get("code") == "READY_VOTE_OVERLOADED"
        and payload.get("retryable") is True
    )


def _ready_vote_retry_delay_ms(
    result: RequestResult,
    retry_index: int,
    retry_policy: dict[str, Any] | None = None,
) -> float:
    if retry_policy is None:
        windows: list[list[int]] = [[150, 350], [400, 800]]
    else:
        windows = retry_policy["jitter_windows_ms"]
    lower_ms, upper_ms = windows[min(retry_index, len(windows) - 1)]
    jittered_ms = random.uniform(lower_ms, upper_ms)
    payload = result.response_json
    server_ms = (
        float(payload.get("retry_after_ms") or 0)
        if isinstance(payload, dict)
        and isinstance(payload.get("retry_after_ms"), (int, float))
        else 0.0
    )
    return min(2_000.0, max(jittered_ms, server_ms))


def _ready_vote_action(
    origin: str,
    user: VirtualUser,
    phase: str,
    timeout: float,
    *,
    session_cookie_name: str,
    csrf_cookie_name: str,
    retry_policy: dict[str, Any] | None = None,
) -> LogicalRequestResult:
    """Issue one request plus only the profile's explicit overload retries."""

    started_at = time.monotonic()
    attempts: list[RequestResult] = []
    max_retries = 2 if retry_policy is None else int(retry_policy["max_retries"])
    for retry_index in range(max_retries + 1):
        result = _ready_vote_request(
            origin,
            user,
            phase,
            timeout,
            session_cookie_name=session_cookie_name,
            csrf_cookie_name=csrf_cookie_name,
            attempt_number=retry_index + 1,
        )
        attempts.append(result)
        if not _ready_vote_overload(result) or retry_index >= max_retries:
            break
        time.sleep(
            _ready_vote_retry_delay_ms(result, retry_index, retry_policy) / 1000
        )
    return LogicalRequestResult(
        attempts=attempts,
        elapsed_ms=(time.monotonic() - started_at) * 1000,
        user_id=user.user_id,
    )


def _flatten_logical_results(results: list[LogicalRequestResult]) -> list[RequestResult]:
    return [attempt for result in results for attempt in result.attempts]


def summarize_logical_results(results: list[LogicalRequestResult]) -> dict[str, Any]:
    finals = [result.final for result in results if result.attempts]
    successful = [result for result in results if result.final.ok]
    accepted_request_latencies = [result.final.elapsed_ms for result in successful]
    changed = Counter(
        str(bool(result.final.response_json.get("changed")))
        for result in results
        if isinstance(result.final.response_json, dict)
        and "changed" in result.final.response_json
    )
    return {
        "scope": "logical_user_actions",
        "actions": len(results),
        "final_successes": len(successful),
        "final_failures": len(results) - len(successful),
        "final_failure_rate_percent": round(
            (len(results) - len(successful)) * 100 / max(1, len(results)),
            4,
        ),
        "total_retries": sum(result.retry_count for result in results),
        "retry_amplification_percent": round(
            sum(result.retry_count for result in results) * 100 / max(1, len(results)),
            4,
        ),
        "retries_per_action": round(
            sum(result.retry_count for result in results) / max(1, len(results)),
            4,
        ),
        "final_status_counts": dict(
            sorted(Counter(str(result.status) for result in finals).items())
        ),
        "changed_counts": dict(sorted(changed.items())),
        "end_to_end_latency": metric_stats([result.elapsed_ms for result in results]),
        "accepted_request_latency": metric_stats(accepted_request_latencies),
    }


def run_load(
    manifest: dict[str, Any],
    users: list[VirtualUser],
    *,
    mode: str,
    spread_seconds: float,
    concurrency: int,
    timeout: float,
    duplicate_count: int,
    manual_refresh_count: int,
    p95_budget_ms: float,
    p99_budget_ms: float,
    failure_budget_percent: float | None = None,
    retry_policy: dict[str, Any] | None = None,
    phase_plan: list[dict[str, Any]] | None = None,
    scenario_kind: str = "slo",
    acceptance_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if manual_refresh_count < 0:
        raise ExternalLoadError("manual_refresh_count must not be negative")
    if mode == "ready-vote" and manual_refresh_count:
        raise ExternalLoadError("manual_refresh_count is only valid for read-mix")
    workspace_users = sum(index % 10 < 5 for index in range(len(users)))
    if manual_refresh_count > workspace_users:
        raise ExternalLoadError("manual_refresh_count exceeds the workspace read cohort")
    origin = str(manifest["origin"]).rstrip("/")
    session_cookie_name = str(manifest["session_cookie_name"])
    csrf_cookie_name = str(manifest["csrf_cookie_name"])
    started_at = datetime.now(UTC)
    trace = _trace(origin, timeout=min(timeout, 10.0))
    phase_results: dict[str, dict[str, Any]] = {}
    all_results: list[RequestResult] = []

    if mode == "ready-vote":
        def vote_builder(
            origin_value: str,
            user: VirtualUser,
            phase: str,
            request_timeout: float,
        ) -> LogicalRequestResult:
            kwargs: dict[str, Any] = {
                "session_cookie_name": session_cookie_name,
                "csrf_cookie_name": csrf_cookie_name,
            }
            if retry_policy is not None:
                kwargs["retry_policy"] = retry_policy
            return _ready_vote_action(origin_value, user, phase, request_timeout, **kwargs)

        primary_started_at = time.monotonic()
        primary: list[LogicalRequestResult] = []
        primary_users = users
        if phase_plan:
            cursor = 0
            offered_window_seconds = 0.0
            planned_phases: dict[str, dict[str, Any]] = {}
            for index, phase in enumerate(phase_plan, start=1):
                phase_name = str(phase.get("name") or f"phase-{index}")
                duration_seconds = float(phase.get("duration_seconds") or 0)
                target_rate = float(
                    phase.get("target_logical_actions_per_second") or 0
                )
                action_count = int(
                    phase.get("logical_actions")
                    or math.ceil(target_rate * duration_seconds)
                )
                if duration_seconds <= 0 or target_rate <= 0 or action_count <= 0:
                    raise ExternalLoadError(
                        f"phase {phase_name!r} must have a positive duration, rate and action count"
                    )
                phase_users = users[cursor : cursor + action_count]
                if len(phase_users) != action_count:
                    raise ExternalLoadError(
                        "phase plan requires more unique fixture users than the manifest provides"
                    )
                cursor += action_count
                phase_started_at_utc = datetime.now(UTC)
                phase_started = time.monotonic()
                phase_results_for_users, phase_submission_window = run_rate_phase(
                    origin,
                    phase_users,
                    phase=f"write_external_vote_{phase_name}",
                    duration_seconds=duration_seconds,
                    concurrency=concurrency,
                    timeout=timeout,
                    request_builder=vote_builder,
                )
                offered_window_seconds += phase_submission_window
                phase_attempts = _flatten_logical_results(phase_results_for_users)
                phase_wall_seconds = max(0.001, time.monotonic() - phase_started)
                phase_finished_at_utc = datetime.now(UTC)
                phase_logical = summarize_logical_results(phase_results_for_users)
                phase_logical["wall_seconds"] = round(phase_wall_seconds, 3)
                phase_logical["target_logical_actions_per_second"] = target_rate
                phase_logical["offered_logical_actions_per_second"] = round(
                    action_count / phase_submission_window, 3
                )
                phase_logical["successful_goodput_actions_per_second"] = round(
                    float(phase_logical.get("final_successes") or 0)
                    / phase_wall_seconds,
                    3,
                )
                phase_raw = summarize_results(phase_attempts)
                phase_raw["attempts_per_second"] = round(
                    float(phase_raw.get("requests") or 0) / phase_submission_window,
                    3,
                )
                planned_phases[phase_name] = {
                    "target_logical_actions_per_second": target_rate,
                    "duration_seconds": duration_seconds,
                    "started_at": phase_started_at_utc.isoformat(),
                    "finished_at": phase_finished_at_utc.isoformat(),
                    "offered_window_seconds": round(phase_submission_window, 3),
                    "raw_http": phase_raw,
                    "logical": phase_logical,
                }
                primary.extend(phase_results_for_users)
            primary_users = users[:cursor]
            phase_results["ramp"] = {
                "phases": planned_phases,
                "logical_actions": cursor,
                "offered_window_seconds": round(offered_window_seconds, 3),
            }
        else:
            primary = run_phase(
                origin,
                users,
                phase="write_external_vote",
                spread_seconds=spread_seconds,
                concurrency=concurrency,
                timeout=timeout,
                request_builder=vote_builder,
            )
        primary_attempts = _flatten_logical_results(primary)
        primary_wall_seconds = max(0.001, time.monotonic() - primary_started_at)
        primary_logical = summarize_logical_results(primary)
        primary_logical["wall_seconds"] = round(primary_wall_seconds, 3)
        primary_logical["successful_goodput_actions_per_second"] = round(
            float(primary_logical.get("final_successes") or 0) / primary_wall_seconds,
            3,
        )
        if phase_plan:
            primary_logical["offered_logical_actions_per_second"] = round(
                len(primary) / max(0.001, offered_window_seconds),
                3,
            )
        phase_results["primary"] = {
            "raw_http": summarize_results(primary_attempts),
            "logical": primary_logical,
        }
        all_results.extend(primary_attempts)

        successful_primary_ids = {
            result.user_id
            for result in primary
            if result.attempts and result.final.ok and result.user_id
        }
        duplicate_candidates = (
            [user for user in primary_users if user.user_id in successful_primary_ids]
            if scenario_kind in {"stress", "spike"}
            else primary_users
        )
        duplicate_users = duplicate_candidates[:duplicate_count]
        if duplicate_users:
            duplicates = run_phase(
                origin,
                duplicate_users,
                phase="write_external_vote_duplicate",
                spread_seconds=min(spread_seconds, 5.0),
                concurrency=concurrency,
                timeout=timeout,
                request_builder=vote_builder,
            )
            duplicate_attempts = _flatten_logical_results(duplicates)
            phase_results["duplicate"] = {
                "raw_http": summarize_results(duplicate_attempts),
                "logical": summarize_logical_results(duplicates),
            }
            all_results.extend(duplicate_attempts)

        users_by_slug: dict[str, VirtualUser] = {}
        expected_by_slug: Counter[str] = Counter()
        successful_primary_by_slug: Counter[str] = Counter()
        for result in primary:
            if result.attempts and result.final.ok:
                slug = result.final.path.split("/", 3)[2]
                successful_primary_by_slug[slug] += 1
        for user in primary_users:
            users_by_slug.setdefault(user.tournament_slug, user)
            expected_by_slug[user.tournament_slug] = successful_primary_by_slug.get(
                user.tournament_slug,
                0,
            )
        state_results: list[RequestResult] = []
        for slug, user in sorted(users_by_slug.items()):
            result = _request(
                origin,
                user,
                method="GET",
                path=f"/tournaments/{slug}/deadlock/ready-check",
                phase="read_external_vote_state",
                timeout=timeout,
                session_cookie_name=session_cookie_name,
                csrf_cookie_name=csrf_cookie_name,
            )
            expected_count = expected_by_slug[slug]
            active_round = (
                result.response_json.get("active_round")
                if isinstance(result.response_json, dict)
                else None
            )
            result.ok = bool(
                result.ok
                and isinstance(active_round, dict)
                and int(active_round.get("ready_count") or 0) == expected_count
            )
            if not result.ok and result.error_kind is None:
                result.error_kind = "authoritative_state_mismatch"
            state_results.append(result)
        phase_results["state"] = summarize_results(state_results)
        all_results.extend(state_results)
        primary_summary = phase_results["primary"]["logical"]
        duplicate_summary = phase_results.get("duplicate", {}).get("logical", {})
        changed_counts = primary_summary.get("changed_counts", {})
        duplicate_changed = duplicate_summary.get("changed_counts", {})
        strict_primary_contract = scenario_kind not in {"stress", "spike"}
        contract_ok = (
            primary_summary["actions"] == len(primary_users)
            and (
                primary_summary["final_failures"] == 0
                if strict_primary_contract
                else int(changed_counts.get("True", 0)) == primary_summary["final_successes"]
            )
            and int(changed_counts.get("True", 0)) == primary_summary["final_successes"]
            and duplicate_summary.get("final_failures", 0) == 0
            and int(duplicate_changed.get("False", 0)) == len(duplicate_users)
            and phase_results["state"]["errors"] == 0
        )
    else:
        user_indexes = {user.user_id: index for index, user in enumerate(users)}
        initial_workspace_etags: dict[str, str] = {}

        def read_builder(origin_value: str, user: VirtualUser, phase: str, request_timeout: float) -> RequestResult:
            index = user_indexes[user.user_id]
            result = _request(
                origin_value,
                user,
                method="GET",
                path=_route_for_read(index, user.tournament_slug),
                phase=phase,
                timeout=request_timeout,
                session_cookie_name=session_cookie_name,
                csrf_cookie_name=csrf_cookie_name,
            )
            if index % 10 < 5 and result.response_etag:
                initial_workspace_etags[user.user_id] = result.response_etag
            return result

        read_results = run_phase(
            origin,
            users,
            phase="scale_external_read_mix",
            spread_seconds=spread_seconds,
            concurrency=concurrency,
            timeout=timeout,
            request_builder=read_builder,
        )
        phase_results["read_mix"] = summarize_results(read_results)
        all_results.extend(read_results)
        refresh_users = [
            user
            for user in users
            if user_indexes[user.user_id] % 10 < 5
        ][:manual_refresh_count]
        refresh_results: list[RequestResult] = []
        if manual_refresh_count:
            def refresh_builder(
                origin_value: str,
                user: VirtualUser,
                phase: str,
                request_timeout: float,
            ) -> RequestResult:
                etag = initial_workspace_etags.get(user.user_id)
                if not etag:
                    result = RequestResult(
                        phase=phase,
                        method="GET",
                        path=_route_for_read(user_indexes[user.user_id], user.tournament_slug),
                        status=0,
                        elapsed_ms=0.0,
                        ok=False,
                        response_bytes=0,
                        error_kind="missing_initial_etag",
                    )
                    return result
                return _request(
                    origin_value,
                    user,
                    method="GET",
                    path=_route_for_read(user_indexes[user.user_id], user.tournament_slug),
                    phase=phase,
                    timeout=request_timeout,
                    session_cookie_name=session_cookie_name,
                    csrf_cookie_name=csrf_cookie_name,
                    expected_statuses=frozenset({200, 304}),
                    extra_headers={"If-None-Match": etag},
                )

            refresh_results = run_phase(
                origin,
                refresh_users,
                phase="manual_workspace_refresh",
                spread_seconds=spread_seconds,
                concurrency=concurrency,
                timeout=timeout,
                request_builder=refresh_builder,
            )
            phase_results["manual_refresh"] = summarize_results(refresh_results)
            all_results.extend(refresh_results)
        contract_ok = (
            phase_results["read_mix"]["requests"] == len(users)
            and phase_results["read_mix"]["errors"] == 0
            and len(initial_workspace_etags) >= manual_refresh_count
            and (
                not manual_refresh_count
                or (
                    phase_results["manual_refresh"]["requests"] == manual_refresh_count
                    and phase_results["manual_refresh"]["errors"] == 0
                    and all(result.status in {200, 304} for result in refresh_results)
                )
            )
        )

    overall = summarize_results(all_results)
    logical_summary = (
        phase_results.get("primary", {}).get("logical", {})
        if mode == "ready-vote"
        else overall
    )
    finished_at = datetime.now(UTC)
    wall_seconds = max(0.001, (finished_at - started_at).total_seconds())
    raw_http_summary = (
        phase_results.get("primary", {}).get("raw_http", overall)
        if mode == "ready-vote"
        else overall
    )
    raw_http_summary["wall_seconds"] = round(wall_seconds, 3)
    raw_http_summary["requests_per_second"] = round(
        float(raw_http_summary.get("requests") or 0) / wall_seconds,
        3,
    )
    raw_http_summary["successful_goodput_actions_per_second"] = round(
        float(raw_http_summary.get("successful_responses") or 0) / wall_seconds,
        3,
    )
    acceptance = evaluate_acceptance(
        contract_ok=contract_ok,
        logical_summary=logical_summary,
        p95_budget_ms=p95_budget_ms,
        p99_budget_ms=p99_budget_ms,
        final_failure_budget_percent=(
            0.5 if failure_budget_percent is None else failure_budget_percent
        ),
        raw_http_summary=raw_http_summary,
        acceptance_contract=acceptance_contract,
    )
    return {
        "schema": 1,
        "scope": "full_population",
        "mode": mode,
        "origin": origin,
        "fixture_marker": manifest["marker"],
        "users": len(users),
        "tournaments": len(manifest["tournaments"]),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_seconds": round(wall_seconds, 3),
        "opening_spread_seconds": spread_seconds,
        "scenario_kind": scenario_kind,
        "manual_refresh_count": manual_refresh_count,
        "concurrency": concurrency,
        "offered_logical_actions_per_second": round(
            float(logical_summary.get("offered_logical_actions_per_second") or 0)
            if phase_plan
            else float(logical_summary.get("actions") or 0) / wall_seconds,
            3,
        ) if mode == "ready-vote" else None,
        "trace": trace,
        "phases": phase_results,
        "overall": overall,
        "raw_http": raw_http_summary,
        "logical": logical_summary if mode == "ready-vote" else None,
        "acceptance": acceptance,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Debug-only external client implementation; use platform_load.py "
            "for canonical profiles."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--mode", choices=("ready-vote", "read-mix"), required=True)
    parser.add_argument("--spread-seconds", type=float, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--duplicate-count", type=int, required=True)
    parser.add_argument("--manual-refresh-count", type=int, required=True)
    parser.add_argument("--p95-budget-ms", type=float, required=True)
    parser.add_argument("--p99-budget-ms", type=float, required=True)
    parser.add_argument("--failure-budget-percent", type=float, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report: dict[str, Any]
    try:
        if not 0 <= args.spread_seconds <= 3_600:
            raise ExternalLoadError("spread-seconds must be between 0 and 3600")
        if not 1 <= args.concurrency <= MAX_CONCURRENCY:
            raise ExternalLoadError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
        if args.timeout <= 0 or args.timeout > 300:
            raise ExternalLoadError("timeout must be between 0 and 300 seconds")
        manifest, users = load_manifest(args.manifest)
        duplicate_count = max(0, min(args.duplicate_count, len(users)))
        if args.manual_refresh_count < 0:
            raise ExternalLoadError("manual-refresh-count must not be negative")
        if args.mode == "ready-vote" and args.manual_refresh_count:
            raise ExternalLoadError("manual-refresh-count is only valid for read-mix")
        workspace_users = sum(index % 10 < 5 for index in range(len(users)))
        if args.manual_refresh_count > workspace_users:
            raise ExternalLoadError(
                "manual-refresh-count exceeds the workspace read cohort"
            )
        manual_refresh_count = args.manual_refresh_count if args.mode == "read-mix" else 0
        report = run_load(
            manifest,
            users,
            mode=args.mode,
            spread_seconds=args.spread_seconds,
            concurrency=args.concurrency,
            timeout=args.timeout,
            duplicate_count=duplicate_count,
            manual_refresh_count=manual_refresh_count,
            p95_budget_ms=max(0.0, args.p95_budget_ms),
            p99_budget_ms=max(0.0, args.p99_budget_ms),
            failure_budget_percent=max(0.0, args.failure_budget_percent),
        )
    except Exception as exc:
        report = {
            "schema": 1,
            "mode": args.mode,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    raw_http = report.get("raw_http") or report.get("overall") or {}
    logical = report.get("logical") or {}
    print(
        json.dumps(
            {
                "mode": report.get("mode"),
                "passed": report.get("acceptance", {}).get("passed", False),
                "users": report.get("users"),
                "raw_http_attempts": raw_http.get("requests"),
                "raw_http_errors": raw_http.get("errors"),
                "temporary_overload_responses": raw_http.get(
                    "temporary_overload_responses",
                    0,
                ),
                "logical_actions": logical.get("actions"),
                "logical_final_failures": logical.get("final_failures"),
                "logical_retries": logical.get("total_retries"),
                "p95_ms": (report.get("acceptance") or {}).get("p95_ms"),
                "p99_ms": (report.get("acceptance") or {}).get("p99_ms"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report.get("acceptance", {}).get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
