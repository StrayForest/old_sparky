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
import http.client
import json
import math
import os
from pathlib import Path
import random
import re
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools.platform_http_transport import HTTP11KeepAliveClient
    from tools.platform_load_acceptance import (
        derive_expected_phase_plan,
        evaluate_acceptance,
        timing_summary_is_complete,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_http_transport import HTTP11KeepAliveClient
    from platform_load_acceptance import (
        derive_expected_phase_plan,
        evaluate_acceptance,
        timing_summary_is_complete,
    )

try:
    from tools.platform_evidence_sanitizer import (
        finite_number,
        safe_cf_error_class,
        safe_error_class,
        safe_method,
        safe_phase,
        safe_route_class,
        safe_route_key,
        safe_status,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_evidence_sanitizer import (
        finite_number,
        safe_cf_error_class,
        safe_error_class,
        safe_method,
        safe_phase,
        safe_route_class,
        safe_route_key,
        safe_status,
    )


EXPECTED_ORIGIN = "https://old-sparky.com"
MANIFEST_SCHEMA = 1
MAX_USERS = 20_000
MAX_TOURNAMENTS = 64
MAX_CONCURRENCY = 512
RESPONSE_BODY_LIMIT = 2 * 1024 * 1024
ERROR_SAMPLE_LIMIT = 25
DIAGNOSTIC_HEADER_LIMIT = 128
MAX_MEASUREMENT = 1_000_000_000_000.0
DEFAULT_CLIENT_TRANSPORT = "urllib-http1-close"
HTTP11_KEEPALIVE_TRANSPORT = "http1-keepalive"
SUPPORTED_PAGE_TRANSPORTS = frozenset(
    {DEFAULT_CLIENT_TRANSPORT, HTTP11_KEEPALIVE_TRANSPORT}
)
MARKER_RE = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,139}$")
COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]+$")
PROFILE_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
    time_to_first_byte_ms: float | None = None
    cf_ray: str | None = None
    cf_error_type: str | None = None
    cf_error_origin: str | None = None
    retry_after: str | None = None
    response_etag: str | None = None
    error_kind: str | None = None
    response_json: Any = None
    attempt_number: int = 1
    diagnostic_id: str | None = None
    started_at_utc: str | None = None
    exception_at_utc: str | None = None
    finished_at_utc: str | None = None
    transport_timing: dict[str, Any] | None = None
    # Monotonic timestamps are intentionally kept out of the JSON report.  The
    # runner uses them to separate generator scheduling/queueing from the
    # service time reported by the HTTP client.
    scheduled_at_monotonic: float | None = None
    enqueued_at_monotonic: float | None = None
    started_at_monotonic: float | None = None
    finished_at_monotonic: float | None = None
    executor_queue_wait_ms: float | None = None
    schedule_delay_ms: float | None = None
    late_start_ms: float | None = None
    user_observed_elapsed_ms: float | None = None


@dataclass(slots=True)
class LogicalRequestResult:
    """One user action, including every temporary-overload retry attempt."""

    attempts: list[RequestResult]
    elapsed_ms: float
    user_id: str | None = None
    scheduled_at_monotonic: float | None = None
    enqueued_at_monotonic: float | None = None
    started_at_monotonic: float | None = None
    finished_at_monotonic: float | None = None
    executor_queue_wait_ms: float | None = None
    schedule_delay_ms: float | None = None
    late_start_ms: float | None = None
    user_observed_elapsed_ms: float | None = None

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


def _finite_nonnegative_measurement(value: Any) -> float | None:
    """Parse a producer measurement without bool/string/overflow coercion."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return (
        numeric
        if math.isfinite(numeric) and 0 <= numeric <= MAX_MEASUREMENT
        else None
    )


def metric_stats(values: list[float]) -> dict[str, Any]:
    valid_values: list[float] = []
    for value in values:
        numeric = _finite_nonnegative_measurement(value)
        if numeric is not None:
            valid_values.append(numeric)
    values = valid_values
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


def _annotate_timing(
    result: Any,
    *,
    scheduled_at: float,
    enqueued_at: float,
    fallback_started_at: float | None = None,
    fallback_finished_at: float | None = None,
) -> Any:
    """Attach queue/schedule timing without changing request semantics.

    A worker can start after its intended arrival time when the executor is
    saturated.  That delay must remain visible instead of disappearing from a
    service-time-only percentile.  Fake builders used by focused tests may not
    expose monotonic timestamps; those samples are retained but explicitly
    counted as missing timing context.
    """

    if isinstance(result, LogicalRequestResult):
        attempts = [attempt for attempt in result.attempts if isinstance(attempt, RequestResult)]
        for attempt in attempts:
            if attempt.started_at_monotonic is None:
                attempt.started_at_monotonic = fallback_started_at
            if attempt.finished_at_monotonic is None:
                attempt.finished_at_monotonic = fallback_finished_at
        started_values = [
            attempt.started_at_monotonic
            for attempt in attempts
            if attempt.started_at_monotonic is not None
        ]
        finished_values = [
            attempt.finished_at_monotonic
            for attempt in attempts
            if attempt.finished_at_monotonic is not None
        ]
        result.scheduled_at_monotonic = scheduled_at
        result.enqueued_at_monotonic = enqueued_at
        result.started_at_monotonic = (
            min(started_values)
            if started_values
            else fallback_started_at
        )
        result.finished_at_monotonic = (
            max(finished_values)
            if finished_values
            else fallback_finished_at
        )
        result.executor_queue_wait_ms = (
            max(0.0, (result.started_at_monotonic - enqueued_at) * 1000)
            if result.started_at_monotonic is not None
            else None
        )
        result.schedule_delay_ms = (
            (result.started_at_monotonic - scheduled_at) * 1000
            if result.started_at_monotonic is not None
            else None
        )
        result.late_start_ms = (
            max(0.0, result.schedule_delay_ms)
            if result.schedule_delay_ms is not None
            else None
        )
        result.user_observed_elapsed_ms = (
            max(0.0, (result.finished_at_monotonic - scheduled_at) * 1000)
            if result.finished_at_monotonic is not None
            else None
        )
        for attempt in attempts:
            _annotate_timing(
                attempt,
                scheduled_at=scheduled_at,
                enqueued_at=enqueued_at,
            )
        return result
    if not isinstance(result, RequestResult):
        return result
    result.scheduled_at_monotonic = scheduled_at
    result.enqueued_at_monotonic = enqueued_at
    if result.started_at_monotonic is None:
        result.started_at_monotonic = fallback_started_at
    if result.finished_at_monotonic is None:
        result.finished_at_monotonic = fallback_finished_at
    if result.started_at_monotonic is not None:
        result.executor_queue_wait_ms = max(
            0.0, (result.started_at_monotonic - enqueued_at) * 1000
        )
        result.schedule_delay_ms = (result.started_at_monotonic - scheduled_at) * 1000
        result.late_start_ms = max(0.0, result.schedule_delay_ms)
    if result.finished_at_monotonic is not None:
        result.user_observed_elapsed_ms = max(
            0.0, (result.finished_at_monotonic - scheduled_at) * 1000
        )
    return result


def _timing_summary(
    results: list[Any],
    *,
    unit: str,
    expected_count: int | None = None,
    submitted_count: int | None = None,
) -> dict[str, Any]:
    """Return additive arrival/queue/user-observed measurements.

    ``unit`` is either ``requests`` or ``logical_actions`` and only affects
    the names of the throughput fields.  Existing ``latency`` fields remain
    service/end-to-end compatibility fields; callers can opt into this
    measurement schema explicitly.
    """

    def finite_timestamp(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(numeric) or not 0 <= numeric <= MAX_MEASUREMENT:
            return None
        return numeric

    def finite_elapsed(value: Any) -> float | None:
        numeric = finite_timestamp(value)
        return numeric

    observed: list[float] = []
    queue_wait: list[float] = []
    schedule_delay: list[float] = []
    late_start: list[float] = []
    starts: list[float] = []
    finishes: list[float] = []
    scheduled: list[float] = []
    started = 0
    response_completions = 0
    user_observed_count = 0
    scheduled_count = 0
    missing_schedule_context = 0
    missing_timing_context = 0
    invalid_timing_context = 0
    service_latency: list[float] = []
    for result in results:
        service_value = _finite_nonnegative_measurement(
            getattr(result, "elapsed_ms", None)
        )
        if service_value is not None:
            service_latency.append(service_value)
        scheduled_at = finite_timestamp(getattr(result, "scheduled_at_monotonic", None))
        enqueued_at = finite_timestamp(getattr(result, "enqueued_at_monotonic", None))
        started_at = finite_timestamp(getattr(result, "started_at_monotonic", None))
        finished_at = finite_timestamp(getattr(result, "finished_at_monotonic", None))
        user_observed = finite_elapsed(getattr(result, "user_observed_elapsed_ms", None))

        if scheduled_at is None or enqueued_at is None:
            missing_schedule_context += 1
        else:
            scheduled_count += 1
            scheduled.append(scheduled_at)
        if started_at is not None:
            started += 1
            starts.append(started_at)
        if finished_at is not None:
            response_completions += 1
            finishes.append(finished_at)
        if user_observed is not None:
            user_observed_count += 1
            observed.append(user_observed)

        queue = finite_elapsed(getattr(result, "executor_queue_wait_ms", None))
        if queue is not None:
            queue_wait.append(queue)
        delay = finite_elapsed(getattr(result, "schedule_delay_ms", None))
        if delay is not None:
            schedule_delay.append(delay)
        late = finite_elapsed(getattr(result, "late_start_ms", None))
        if late is not None:
            late_start.append(late)

        if any(value is None for value in (scheduled_at, enqueued_at, started_at, finished_at, user_observed)):
            missing_timing_context += 1
        elif finished_at < started_at:
            invalid_timing_context += 1
    arrival_window = (
        max(0.001, max(starts) - min(starts))
        if len(starts) >= 2
        else (0.001 if starts else None)
    )
    requests_per_second = (
        started / arrival_window if arrival_window is not None else None
    )
    offered_window = (
        max(0.001, max(scheduled) - min(scheduled))
        if len(scheduled) >= 2
        else (0.001 if scheduled else None)
    )
    offered_per_second = (
        len(scheduled) / offered_window if offered_window is not None else None
    )
    completion_window = (
        max(0.001, max(finishes) - min(finishes))
        if len(finishes) >= 2
        else (0.001 if finishes else None)
    )
    completed_count = len(results)
    expected = completed_count if expected_count is None else max(0, int(expected_count))
    submitted = completed_count if submitted_count is None else max(0, int(submitted_count))
    dropped_work = max(0, expected - started)
    timing_counts = (
        expected,
        submitted,
        completed_count,
        scheduled_count,
        started,
        response_completions,
        user_observed_count,
    )
    partial = (
        len(set(timing_counts)) != 1
        or missing_schedule_context > 0
        or missing_timing_context > 0
        or invalid_timing_context > 0
        or dropped_work != 0
    )
    return {
        "timing_schema": 2,
        "service_latency": metric_stats(service_latency),
        "user_observed_latency": metric_stats(observed),
        "executor_queue_wait": metric_stats(queue_wait),
        "schedule_delay": metric_stats(schedule_delay),
        "late_start": metric_stats(late_start),
        "late_start_count": sum(value > 0 for value in late_start),
        "late_start_percent": round(
            sum(value > 0 for value in late_start) * 100 / max(1, len(results)),
            4,
        ),
        "expected_count": expected,
        "submitted_count": submitted,
        "completed_count": completed_count,
        "partial": partial,
        "scheduled_count": scheduled_count,
        "started_count": started,
        "actual_request_start_count": started,
        "actual_start_count": started,
        "response_completion_count": response_completions,
        "user_observed_count": user_observed_count,
        "response_completion_window_seconds": (
            round(completion_window, 6) if completion_window is not None else None
        ),
        "dropped_work": dropped_work,
        "missing_schedule_context": missing_schedule_context,
        "missing_timing_context": missing_timing_context,
        "invalid_timing_context": invalid_timing_context,
        "actual_arrival_window_seconds": (
            round(arrival_window, 6) if arrival_window is not None else None
        ),
        "offered_arrival_window_seconds": (
            round(offered_window, 6) if offered_window is not None else None
        ),
        f"offered_{unit}_per_second": (
            round(offered_per_second, 3) if offered_per_second is not None else None
        ),
        f"actual_arrival_{unit}_per_second": (
            round(requests_per_second, 3) if requests_per_second is not None else None
        ),
    }


def spread_offsets(count: int, spread_seconds: float) -> list[float]:
    """Return deterministic starts in [0, spread_seconds) for a phase."""

    if count <= 0:
        return []
    if count == 1 or spread_seconds <= 0:
        return [0.0] * count
    step = spread_seconds / count
    return [round(index * step, 6) for index in range(count)]


def planned_http_attempts(
    *,
    mode: str,
    user_count: int,
    tournament_count: int,
    duplicate_count: int,
    manual_refresh_count: int,
    phase_plan: list[dict[str, Any]] | None,
    concurrency_stages: list[int] | tuple[int, ...] | None,
    retry_policy: dict[str, Any] | None,
) -> int | None:
    """Bound all requests before the first external request is submitted."""

    phases = phase_plan or []
    primary_actions = (
        sum(int(phase.get("logical_actions") or 0) for phase in phases)
        if phases
        else user_count
    )
    max_retries = 2 if retry_policy is None else int(retry_policy.get("max_retries") or 0)
    if mode == "ready-vote":
        return (
            (primary_actions + int(duplicate_count)) * (max_retries + 1)
            + int(tournament_count)
        )
    if mode == "read-mix":
        stages = concurrency_stages or (1,)
        return user_count * len(stages) + int(manual_refresh_count)
    if mode == "page-load":
        return user_count
    return None


def _required_text(value: Any, *, field: str, min_length: int = 1) -> str:
    if not isinstance(value, str) or len(value) < min_length:
        raise ExternalLoadError(f"manifest {field} is invalid")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object members instead of silently overwriting."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExternalLoadError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_manifest(path: Path) -> tuple[dict[str, Any], list[VirtualUser]]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
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
        expected_count = raw_tournament.get("user_count")
        if (
            isinstance(expected_count, bool)
            or not isinstance(expected_count, int)
            or expected_count <= 0
        ):
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
            # Read and discard the body.  It contains edge IP, location and
            # other unique request metadata which is useful only transiently
            # while debugging a live request.
            response.read(16_384)
            return {"available": True, "status": safe_status(response.status)}
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return {"available": False, "status": 0, "error_class": safe_error_class(type(exc).__name__)}


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
    url_prefix: str = "/api/v1",
    diagnostic_id: str | None = None,
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
    if diagnostic_id:
        headers["X-Platform-Timeout-Diagnostic-ID"] = diagnostic_id
    if json_payload is not None:
        body = json.dumps(json_payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{origin}{url_prefix}{path}",
        data=body,
        method=method,
        headers=headers,
    )
    started_at = time.monotonic()
    started_at_utc = datetime.now(UTC).isoformat()
    status = 0
    response_bytes = 0
    cf_ray: str | None = None
    response_etag: str | None = None
    error_kind: str | None = None
    response_json: Any = None
    time_to_first_byte_ms: float | None = None
    cf_error_type: str | None = None
    cf_error_origin: str | None = None
    retry_after: str | None = None
    exception_at_utc: str | None = None

    def diagnostic_headers(headers: Any) -> tuple[str | None, str | None, str | None]:
        if status < 400:
            return None, None, None
        return (
            (headers.get("cf-error-type", "")[:DIAGNOSTIC_HEADER_LIMIT] or None),
            (headers.get("cf-error-origin", "")[:DIAGNOSTIC_HEADER_LIMIT] or None),
            (headers.get("retry-after", "")[:DIAGNOSTIC_HEADER_LIMIT] or None),
        )

    try:
        # URL is constructed only from the fixed manifest origin and a route
        # selected by this module; this is not an arbitrary fetch primitive.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            status = int(response.status)
            cf_ray = response.headers.get("cf-ray", "")[:128] or None
            cf_error_type, cf_error_origin, retry_after = diagnostic_headers(response.headers)
            response_etag = response.headers.get("etag", "")[:512] or None
            first_chunk = response.read(1)
            time_to_first_byte_ms = (time.monotonic() - started_at) * 1000
            raw_body = first_chunk + response.read(
                max(0, RESPONSE_BODY_LIMIT - len(first_chunk))
            )
            response_bytes = len(raw_body)
            if raw_body:
                try:
                    response_json = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    response_json = None
    except HTTPError as exc:
        status = int(exc.code)
        cf_ray = exc.headers.get("cf-ray", "")[:128] or None
        cf_error_type, cf_error_origin, retry_after = diagnostic_headers(exc.headers)
        first_chunk = exc.read(1)
        time_to_first_byte_ms = (time.monotonic() - started_at) * 1000
        with_error_body = first_chunk + exc.read(
            max(0, RESPONSE_BODY_LIMIT - len(first_chunk))
        )
        response_bytes = len(with_error_body)
        error_kind = "http_error"
        if with_error_body:
            try:
                response_json = json.loads(with_error_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_json = None
    except (URLError, TimeoutError, OSError) as exc:
        exception_at_utc = datetime.now(UTC).isoformat()
        error_kind = type(exc).__name__
    finished_at = time.monotonic()
    elapsed_ms = (finished_at - started_at) * 1000
    finished_at_utc = datetime.now(UTC).isoformat()
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
        cf_error_type=cf_error_type,
        cf_error_origin=cf_error_origin,
        retry_after=retry_after,
        response_etag=response_etag,
        error_kind=error_kind,
        response_json=response_json,
        time_to_first_byte_ms=time_to_first_byte_ms,
        attempt_number=attempt_number,
        diagnostic_id=diagnostic_id,
        started_at_utc=started_at_utc if diagnostic_id else None,
        exception_at_utc=exception_at_utc if diagnostic_id else None,
        finished_at_utc=finished_at_utc if diagnostic_id else None,
        started_at_monotonic=started_at,
        finished_at_monotonic=finished_at,
        user_observed_elapsed_ms=elapsed_ms,
    )


def _page_request(
    origin: str,
    user: VirtualUser,
    phase: str,
    timeout: float,
    *,
    session_cookie_name: str,
    csrf_cookie_name: str,
    diagnostic_id: str | None = None,
    transport: str = DEFAULT_CLIENT_TRANSPORT,
) -> RequestResult:
    """Measure the real Next.js HTML response, including server TTFB."""

    if transport == HTTP11_KEEPALIVE_TRANSPORT:
        return _page_request_http11_keepalive(
            origin,
            user,
            phase,
            timeout,
            session_cookie_name=session_cookie_name,
            csrf_cookie_name=csrf_cookie_name,
            diagnostic_id=diagnostic_id,
        )
    if transport != DEFAULT_CLIENT_TRANSPORT:
        raise ExternalLoadError(f"unsupported page-load transport: {transport}")

    return _request(
        origin,
        user,
        method="GET",
        path=f"/tournaments/{user.tournament_slug}",
        phase=phase,
        timeout=timeout,
        session_cookie_name=session_cookie_name,
        csrf_cookie_name=csrf_cookie_name,
        expected_statuses=frozenset({200}),
        extra_headers={"Accept": "text/html"},
        url_prefix="",
        diagnostic_id=diagnostic_id,
    )


_page_transport_local = threading.local()


def _http11_keepalive_client(origin: str, timeout: float) -> HTTP11KeepAliveClient:
    client = getattr(_page_transport_local, "client", None)
    if (
        not isinstance(client, HTTP11KeepAliveClient)
        or client.origin != origin.rstrip("/")
    ):
        client = HTTP11KeepAliveClient(
            origin,
            timeout=timeout,
            max_response_bytes=RESPONSE_BODY_LIMIT,
        )
        _page_transport_local.client = client
    return client


def _page_request_http11_keepalive(
    origin: str,
    user: VirtualUser,
    phase: str,
    timeout: float,
    *,
    session_cookie_name: str,
    csrf_cookie_name: str,
    diagnostic_id: str | None = None,
) -> RequestResult:
    """Measure a page request over explicit HTTP/1.1 per-thread keep-alive."""

    path = f"/tournaments/{user.tournament_slug}"
    request_headers = {
        "Accept": "text/html",
        "Origin": origin,
        "User-Agent": "old-sparky-external-load/2",
        "Cookie": (
            f"{session_cookie_name}={user.session_token}; "
            f"{csrf_cookie_name}={user.csrf_token}"
        ),
        "X-CSRF-Token": user.csrf_token,
        "X-Platform-QA-Phase": phase,
    }
    client = _http11_keepalive_client(origin, timeout)
    started_at = time.monotonic()
    started_at_utc = datetime.now(UTC).isoformat()
    try:
        response = client.get(path, headers=request_headers)
        finished_at = time.monotonic()
        status = response.status
        error_kind = None if status == 200 else "unexpected_status"
        cf_error_type = response.headers.get("cf-error-type") or None
        cf_error_origin = response.headers.get("cf-error-origin") or None
        retry_after = response.headers.get("retry-after") or None
        return RequestResult(
            phase=phase,
            method="GET",
            path=path,
            status=status,
            elapsed_ms=float(response.timing.get("total_ms") or 0.0),
            ok=status == 200,
            response_bytes=len(response.body),
            time_to_first_byte_ms=(
                float(response.timing["ttfb_ms"])
                if isinstance(response.timing.get("ttfb_ms"), (int, float))
                else None
            ),
            cf_ray=response.headers.get("cf-ray") or None,
            cf_error_type=cf_error_type,
            cf_error_origin=cf_error_origin,
            retry_after=retry_after,
            response_etag=response.headers.get("etag") or None,
            error_kind=error_kind,
            diagnostic_id=diagnostic_id,
            started_at_utc=started_at_utc if diagnostic_id else None,
            finished_at_utc=datetime.now(UTC).isoformat() if diagnostic_id else None,
            transport_timing=response.timing,
            started_at_monotonic=started_at,
            finished_at_monotonic=finished_at,
            user_observed_elapsed_ms=max(0.0, finished_at - started_at) * 1000,
        )
    except (http.client.HTTPException, OSError, TimeoutError, ValueError) as error:
        timing = dict(client.last_timing)
        finished_at = time.monotonic()
        finished_at_utc = datetime.now(UTC).isoformat()
        ttfb_ms = timing.get("ttfb_ms")
        return RequestResult(
            phase=phase,
            method="GET",
            path=path,
            status=0,
            elapsed_ms=float(timing.get("total_ms") or 0.0),
            ok=False,
            response_bytes=0,
            time_to_first_byte_ms=(
                float(ttfb_ms)
                if isinstance(ttfb_ms, (int, float))
                else None
            ),
            error_kind=type(error).__name__,
            diagnostic_id=diagnostic_id,
            started_at_utc=started_at_utc if diagnostic_id else None,
            exception_at_utc=finished_at_utc if diagnostic_id else None,
            finished_at_utc=finished_at_utc if diagnostic_id else None,
            transport_timing=timing or None,
            started_at_monotonic=started_at,
            finished_at_monotonic=finished_at,
            user_observed_elapsed_ms=max(0.0, finished_at - started_at) * 1000,
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
            enqueued_at = time.monotonic()
            scheduled_at = phase_started_at + offset

            def invoke(
                *,
                user: VirtualUser = user,
                scheduled_at: float = scheduled_at,
                enqueued_at: float = enqueued_at,
            ) -> Any:
                fallback_started_at = time.monotonic()
                result = request_builder(origin, user, phase, timeout)
                fallback_finished_at = time.monotonic()
                return _annotate_timing(
                    result,
                    scheduled_at=scheduled_at,
                    enqueued_at=enqueued_at,
                    fallback_started_at=fallback_started_at,
                    fallback_finished_at=fallback_finished_at,
                )

            futures.append(executor.submit(invoke))
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
            if first_submission_at is None:
                first_submission_at = submitted_at
            last_submission_at = submitted_at
            scheduled_at = phase_started_at + offset

            def invoke(
                *,
                user: VirtualUser = user,
                scheduled_at: float = scheduled_at,
                enqueued_at: float = submitted_at,
            ) -> Any:
                fallback_started_at = time.monotonic()
                result = request_builder(origin, user, phase, timeout)
                fallback_finished_at = time.monotonic()
                return _annotate_timing(
                    result,
                    scheduled_at=scheduled_at,
                    enqueued_at=enqueued_at,
                    fallback_started_at=fallback_started_at,
                    fallback_finished_at=fallback_finished_at,
                )

            futures.append(executor.submit(invoke))
        results = [future.result() for future in as_completed(futures)]
    if first_submission_at is None or last_submission_at is None:
        submission_window_seconds = 0.001
    elif len(users) == 1:
        submission_window_seconds = max(0.001, duration_seconds)
    else:
        submission_window_seconds = max(0.001, last_submission_at - first_submission_at)
    return results, submission_window_seconds


def summarize_results(
    results: list[RequestResult],
    *,
    expected_count: int | None = None,
    submitted_count: int | None = None,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    by_route: dict[str, list[float]] = defaultdict(list)
    latencies: list[float] = []
    errors = 0
    temporary_overloads = 0
    retry_attempts = 0
    error_kinds: Counter[str] = Counter()
    cf_error_types: Counter[str] = Counter()
    cf_error_origins: Counter[str] = Counter()
    error_samples: list[dict[str, Any]] = []
    timeout_diagnostics: list[dict[str, Any]] = []
    first_byte_times: list[float] = []
    response_sizes: list[int] = []
    changed = Counter()
    transport_names: Counter[str] = Counter()
    transport_http_versions: Counter[str] = Counter()
    transport_reused = 0
    transport_new = 0
    transport_phase_values: dict[str, list[float]] = defaultdict(list)
    for result in results:
        status_counts[str(safe_status(result.status))] += 1
        route = safe_route_key(result.method, result.path)
        elapsed = _finite_nonnegative_measurement(result.elapsed_ms)
        if elapsed is not None:
            by_route[route].append(elapsed)
            latencies.append(elapsed)
        if (
            isinstance(result.response_bytes, int)
            and not isinstance(result.response_bytes, bool)
            and result.response_bytes >= 0
        ):
            response_sizes.append(result.response_bytes)
        if result.time_to_first_byte_ms is not None:
            first_byte_times.append(result.time_to_first_byte_ms)
        if result.transport_timing:
            transport = result.transport_timing
            transport_name = transport.get("transport")
            if transport_name in SUPPORTED_PAGE_TRANSPORTS:
                transport_names[str(transport_name)] += 1
            elif transport_name is not None:
                transport_names["other"] += 1
            http_version = transport.get("http_version")
            if http_version in {"1.0", "1.1", "2", "3"}:
                transport_http_versions[str(http_version)] += 1
            elif http_version is not None:
                transport_http_versions["other"] += 1
            if transport.get("connection_reused") is True:
                transport_reused += 1
            elif transport.get("connection_reused") is False:
                transport_new += 1
            for key in (
                "dns_ms",
                "tcp_connect_ms",
                "tls_handshake_ms",
                "request_write_ms",
                "edge_wait_ms",
                "ttfb_ms",
                "body_receive_ms",
                "total_ms",
            ):
                value = _finite_nonnegative_measurement(transport.get(key))
                if value is not None:
                    transport_phase_values[key].append(value)
        if (
            isinstance(result.attempt_number, int)
            and not isinstance(result.attempt_number, bool)
            and result.attempt_number > 1
        ):
            retry_attempts += 1
        if _ready_vote_overload(result) or _authenticated_read_overload(result):
            temporary_overloads += 1
        if result.ok is not True:
            errors += 1
            kind = safe_error_class(result.error_kind or "unexpected", status=result.status)
            error_kinds[kind] += 1
            if len(error_samples) < ERROR_SAMPLE_LIMIT:
                error_sample = {
                    "phase": safe_phase(result.phase),
                    "method": safe_method(result.method),
                    "route_class": safe_route_class(result.path),
                    "status": safe_status(result.status),
                    "error_class": kind,
                    "cf_error_class": safe_cf_error_class(result.cf_error_type),
                    "cf_error_origin_class": safe_cf_error_class(result.cf_error_origin),
                    "ttfb_ms": finite_number(result.time_to_first_byte_ms),
                    "elapsed_ms": finite_number(result.elapsed_ms),
                }
                error_samples.append(error_sample)
            if result.diagnostic_id and kind == "timeout":
                timeout_diagnostics.append(
                    {
                        "phase": safe_phase(result.phase),
                        "method": safe_method(result.method),
                        "route_class": safe_route_class(result.path),
                        "status": safe_status(result.status),
                        "error_class": "timeout",
                        "cf_error_class": safe_cf_error_class(result.cf_error_type),
                        "cf_error_origin_class": safe_cf_error_class(result.cf_error_origin),
                        "ttfb_ms": finite_number(result.time_to_first_byte_ms),
                        "elapsed_ms": finite_number(result.elapsed_ms),
                    }
                )
            if result.cf_error_type:
                cf_error_types[safe_cf_error_class(result.cf_error_type)] += 1
            if result.cf_error_origin:
                cf_error_origins[safe_cf_error_class(result.cf_error_origin)] += 1
        if (
            isinstance(result.response_json, dict)
            and type(result.response_json.get("changed")) is bool
        ):
            changed[str(result.response_json["changed"])] += 1
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
        "total_retries": retry_attempts,
        "retry_amplification_percent": round(
            retry_attempts * 100 / max(1, len(results) - retry_attempts),
            4,
        ),
        "unexpected_statuses": max(0, errors - temporary_overloads),
        "status_counts": dict(sorted(status_counts.items())),
        "error_kinds": dict(sorted(error_kinds.items())),
        "cf_error_type_counts": dict(sorted(cf_error_types.items())),
        "cf_error_origin_counts": dict(sorted(cf_error_origins.items())),
        "changed_counts": dict(sorted(changed.items())),
        "latency": metric_stats(latencies),
        "timing": _timing_summary(
            results,
            unit="requests",
            expected_count=expected_count,
            submitted_count=submitted_count,
        ),
        "time_to_first_byte": metric_stats(first_byte_times),
        "response_bytes": {
            "count": len(response_sizes),
            "avg_bytes": round(sum(response_sizes) / len(response_sizes), 3)
            if response_sizes
            else None,
            "max_bytes": max(response_sizes) if response_sizes else None,
        },
        "transport": {
            "names": dict(sorted(transport_names.items())),
            "http_versions": dict(sorted(transport_http_versions.items())),
            "connection_reused": transport_reused,
            "connection_new": transport_new,
            "phase_timings": {
                key: metric_stats(values)
                for key, values in sorted(transport_phase_values.items())
            },
        },
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
        "timeout_diagnostics": timeout_diagnostics,
    }


def _add_measured_goodput(summary: dict[str, Any], wall_seconds: float) -> None:
    """Attach the canonical useful-response goodput measurement to a summary."""

    if not isinstance(wall_seconds, (int, float)) or isinstance(wall_seconds, bool):
        raise ExternalLoadError("summary wall time must be numeric")
    wall = float(wall_seconds)
    if not math.isfinite(wall) or wall <= 0:
        raise ExternalLoadError("summary wall time must be finite and positive")
    summary["wall_seconds"] = round(wall, 6)
    successful = summary.get("successful_responses")
    if isinstance(successful, bool) or not isinstance(successful, int) or successful < 0:
        raise ExternalLoadError("summary successful response count is invalid")
    summary["successful_goodput_actions_per_second"] = round(successful / wall, 3)


def analyze_concurrency_ramp(
    stages: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Identify the first latency knee without turning it into a rollout."""

    ordered: list[dict[str, Any]] = []
    for raw_concurrency, summary in stages.items():
        try:
            concurrency = int(raw_concurrency)
        except (TypeError, ValueError):
            continue
        latency = summary.get("latency") or {}
        p95 = latency.get("p95_ms")
        p99 = latency.get("p99_ms")
        rps = summary.get("requests_per_second")
        if not isinstance(p95, (int, float)) or not isinstance(p99, (int, float)):
            continue
        ordered.append(
            {
                "concurrency": concurrency,
                "p95_ms": round(float(p95), 3),
                "p99_ms": round(float(p99), 3),
                "requests_per_second": round(float(rps or 0), 3),
            }
        )
    ordered.sort(key=lambda row: row["concurrency"])
    comparisons: list[dict[str, Any]] = []
    knee: dict[str, Any] | None = None
    for previous, current in zip(ordered, ordered[1:]):
        previous_p95 = float(previous["p95_ms"])
        current_p95 = float(current["p95_ms"])
        previous_rps = float(previous["requests_per_second"])
        current_rps = float(current["requests_per_second"])
        p95_growth_percent = round(
            (current_p95 - previous_p95) * 100 / max(1.0, previous_p95),
            3,
        )
        throughput_growth_percent = round(
            (current_rps - previous_rps) * 100 / max(1.0, previous_rps),
            3,
        )
        comparison = {
            "from_concurrency": previous["concurrency"],
            "to_concurrency": current["concurrency"],
            "p95_growth_percent": p95_growth_percent,
            "throughput_growth_percent": throughput_growth_percent,
        }
        comparisons.append(comparison)
        if knee is None and p95_growth_percent >= 50 and throughput_growth_percent <= 10:
            knee = {
                "stable_concurrency": previous["concurrency"],
                "first_queued_concurrency": current["concurrency"],
                "reason": "p95 grew at least 50% while throughput grew at most 10%",
            }
    return {
        "stages": ordered,
        "comparisons": comparisons,
        "knee": knee,
        "recommended_max_concurrency": (
            knee["stable_concurrency"] if knee is not None else (ordered[-1]["concurrency"] if ordered else None)
        ),
        "rollout_required": True,
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


def _authenticated_read_overload(result: RequestResult) -> bool:
    payload = result.response_json
    return bool(
        result.status == 503
        and isinstance(payload, dict)
        and payload.get("code") == "AUTHENTICATED_READ_OVERLOADED"
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
    finished_at = time.monotonic()
    return LogicalRequestResult(
        attempts=attempts,
        elapsed_ms=(finished_at - started_at) * 1000,
        user_id=user.user_id,
        started_at_monotonic=started_at,
        finished_at_monotonic=finished_at,
        user_observed_elapsed_ms=(finished_at - started_at) * 1000,
    )


def _flatten_logical_results(results: list[LogicalRequestResult]) -> list[RequestResult]:
    return [attempt for result in results for attempt in result.attempts]


def summarize_logical_results(
    results: list[LogicalRequestResult],
    *,
    expected_count: int | None = None,
    submitted_count: int | None = None,
) -> dict[str, Any]:
    finals = [result.final for result in results if result.attempts]
    successful = [result for result in results if result.final.ok is True]
    accepted_request_latencies = [result.final.elapsed_ms for result in successful]
    changed = Counter(
        str(result.final.response_json["changed"])
        for result in results
        if isinstance(result.final.response_json, dict)
        and type(result.final.response_json.get("changed")) is bool
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
        "timing": _timing_summary(
            results,
            unit="logical_actions",
            expected_count=expected_count,
            submitted_count=submitted_count,
        ),
    }


def _has_partial_timing(value: Any) -> bool:
    """Find missing or incomplete timing populations recursively."""

    found = False

    def visit(item: Any) -> bool:
        nonlocal found
        if isinstance(item, dict):
            if "timing" in item:
                found = True
                if not timing_summary_is_complete(item.get("timing")):
                    return True
            return any(visit(child) for key, child in item.items() if key != "timing")
        if isinstance(item, list):
            return any(visit(child) for child in item)
        return False

    incomplete = visit(value)
    # A current canonical run always has at least one timing summary.  Treat a
    # missing entire timing tree as incomplete instead of allowing an empty or
    # legacy-shaped report to become green through zero-valued metrics.
    return incomplete or not found


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
    concurrency_stages: list[int] | tuple[int, ...] | None = None,
    scenario_kind: str = "slo",
    acceptance_contract: dict[str, Any] | None = None,
    require_exact_observer_binding: bool = False,
    authoritative_binding: Mapping[str, Any] | None = None,
    expected_profile_id: str | None = None,
    expected_profile_version: int | None = None,
    expected_profile_digest: str | None = None,
    expected_primary_action_count: int | None = None,
    expected_total_logical_action_count: int | None = None,
    expected_state_read_count: int | None = None,
    expected_stage_action_counts: Mapping[str, Any] | None = None,
    timeout_diagnostics_run_id: str | None = None,
    client_transport: str = DEFAULT_CLIENT_TRANSPORT,
    max_http_attempts: int | None = None,
) -> dict[str, Any]:
    for value, field in (
        (duplicate_count, "duplicate_count"),
        (manual_refresh_count, "manual_refresh_count"),
        (concurrency, "concurrency"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExternalLoadError(f"{field} must be an integer")
    for value, field, minimum, maximum in (
        (spread_seconds, "spread_seconds", 0.0, 3_600.0),
        (timeout, "timeout", 0.1, 300.0),
        (p95_budget_ms, "p95_budget_ms", 0.0, 1_000_000.0),
        (p99_budget_ms, "p99_budget_ms", 0.0, 1_000_000.0),
    ):
        try:
            numeric = float(value)
        except (OverflowError, TypeError, ValueError):
            numeric = float("nan")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(numeric)
            or not minimum <= numeric <= maximum
        ):
            raise ExternalLoadError(f"{field} must be finite and within bounds")
    if failure_budget_percent is not None:
        try:
            failure_budget = float(failure_budget_percent)
        except (OverflowError, TypeError, ValueError):
            failure_budget = float("nan")
        if (
            isinstance(failure_budget_percent, bool)
            or not isinstance(failure_budget_percent, (int, float))
            or not math.isfinite(failure_budget)
            or not 0 <= failure_budget <= 100
        ):
            raise ExternalLoadError(
                "failure_budget_percent must be finite and between 0 and 100"
            )
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ExternalLoadError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    if duplicate_count < 0:
        raise ExternalLoadError("duplicate_count must not be negative")
    if duplicate_count > len(users):
        raise ExternalLoadError("duplicate_count exceeds the manifest user population")
    if expected_primary_action_count is not None and (
        isinstance(expected_primary_action_count, bool)
        or not isinstance(expected_primary_action_count, int)
        or expected_primary_action_count < 0
    ):
        raise ExternalLoadError("expected_primary_action_count must be a non-negative integer")
    if expected_primary_action_count is None and mode == "ready-vote":
        expected_primary_action_count = (
            sum(int(phase.get("logical_actions") or 0) for phase in phase_plan)
            if phase_plan
            else len(users)
        )
    for value, field in (
        (expected_total_logical_action_count, "expected_total_logical_action_count"),
        (expected_state_read_count, "expected_state_read_count"),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ExternalLoadError(f"{field} must be a non-negative integer")
    if expected_stage_action_counts is not None:
        if not isinstance(expected_stage_action_counts, Mapping):
            raise ExternalLoadError("expected_stage_action_counts must be an object")
        for stage_name, value in expected_stage_action_counts.items():
            if not isinstance(stage_name, str) or not stage_name:
                raise ExternalLoadError("expected_stage_action_counts has an invalid stage name")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExternalLoadError(
                    "expected_stage_action_counts values must be non-negative integers"
                )
    if manual_refresh_count < 0:
        raise ExternalLoadError("manual_refresh_count must not be negative")
    if mode == "ready-vote" and manual_refresh_count:
        raise ExternalLoadError("manual_refresh_count is only valid for read-mix")
    workspace_users = sum(index % 10 < 5 for index in range(len(users)))
    if manual_refresh_count > workspace_users:
        raise ExternalLoadError("manual_refresh_count exceeds the workspace read cohort")
    if concurrency_stages is not None:
        if mode != "read-mix":
            raise ExternalLoadError("concurrency_stages is only valid for read-mix")
        if not concurrency_stages:
            raise ExternalLoadError("concurrency_stages must not be empty")
        previous_stage = 0
        for stage in concurrency_stages:
            if isinstance(stage, bool) or not isinstance(stage, int):
                raise ExternalLoadError("concurrency_stages must contain integers")
            if not 1 <= stage <= MAX_CONCURRENCY or stage <= previous_stage:
                raise ExternalLoadError("concurrency_stages must be strictly ascending within bounds")
            previous_stage = stage
    if timeout_diagnostics_run_id is not None:
        if not re.fullmatch(r"[1-9][0-9]{0,31}", timeout_diagnostics_run_id):
            raise ExternalLoadError("timeout diagnostics run id must be numeric")
        if mode != "page-load":
            raise ExternalLoadError("timeout diagnostics are only supported for page-load")
    if client_transport not in SUPPORTED_PAGE_TRANSPORTS:
        raise ExternalLoadError(f"unsupported client transport: {client_transport}")
    if mode != "page-load" and client_transport != DEFAULT_CLIENT_TRANSPORT:
        raise ExternalLoadError(
            "non-default client transports are only supported for page-load"
        )
    planned_attempts = planned_http_attempts(
        mode=mode,
        user_count=len(users),
        tournament_count=len(manifest.get("tournaments") or []),
        duplicate_count=duplicate_count,
        manual_refresh_count=manual_refresh_count,
        phase_plan=phase_plan,
        concurrency_stages=concurrency_stages,
        retry_policy=retry_policy,
    )
    if (
        max_http_attempts is not None
        and planned_attempts is not None
        and planned_attempts > max_http_attempts
    ):
        raise ExternalLoadError(
            f"planned HTTP attempts {planned_attempts} exceed "
            f"max_http_attempts {max_http_attempts}"
        )
    read_concurrency_stages = tuple(concurrency_stages or (concurrency,))
    planned_primary_action_count = (
        sum(int(phase.get("logical_actions") or 0) for phase in (phase_plan or []))
        if mode == "ready-vote" and phase_plan
        else len(users) * len(read_concurrency_stages)
        if mode == "read-mix"
        else len(users)
        if mode == "page-load"
        else None
    )
    if expected_primary_action_count is None:
        expected_primary_action_count = planned_primary_action_count
    elif planned_primary_action_count is not None and (
        expected_primary_action_count != planned_primary_action_count
    ):
        raise ExternalLoadError(
            "expected_primary_action_count does not match the selected workload plan"
        )
    planned_total_logical_action_count = (
        expected_primary_action_count
        + duplicate_count
        + (manual_refresh_count if mode == "read-mix" else 0)
        if expected_primary_action_count is not None
        else None
    )
    if expected_total_logical_action_count is None:
        expected_total_logical_action_count = planned_total_logical_action_count
    elif planned_total_logical_action_count is not None and (
        expected_total_logical_action_count != planned_total_logical_action_count
    ):
        raise ExternalLoadError(
            "expected_total_logical_action_count does not match the selected workload plan"
        )
    planned_state_read_count = (
        len(manifest.get("tournaments") or []) if mode == "ready-vote" else 0
    )
    if expected_state_read_count is None:
        expected_state_read_count = planned_state_read_count
    elif expected_state_read_count != planned_state_read_count:
        raise ExternalLoadError(
            "expected_state_read_count does not match the selected fixture plan"
        )
    if mode == "read-mix" and expected_stage_action_counts is not None:
        planned_stage_counts = {
            str(stage): len(users) for stage in read_concurrency_stages
        }
        actual_stage_counts = {
            str(stage): value for stage, value in expected_stage_action_counts.items()
        }
        if actual_stage_counts != planned_stage_counts:
            raise ExternalLoadError(
                "expected_stage_action_counts does not match the selected fixture plan"
            )
    if mode == "ready-vote":
        expected_phase_action_counts: dict[str, int] = {
            str(phase.get("name")): int(phase.get("logical_actions") or 0)
            for phase in (phase_plan or [])
        }
        expected_phase_action_counts.update(
            {
                "primary": expected_primary_action_count,
                "duplicate": duplicate_count,
            }
        )
    elif mode == "read-mix":
        expected_phase_action_counts = {
            "read_mix": expected_primary_action_count,
            **(
                {"manual_refresh": manual_refresh_count}
                if manual_refresh_count > 0
                else {}
            ),
        }
    elif mode == "page-load":
        expected_phase_action_counts = {
            "authenticated_page_load": expected_primary_action_count,
        }
    else:
        expected_phase_action_counts = {}
    acceptance_phase_plan = derive_expected_phase_plan(
        mode=mode,
        authored_phase_plan=phase_plan,
        expected_phase_action_counts=expected_phase_action_counts,
    )
    expected_max_retries = (
        2
        if retry_policy is None
        else int(retry_policy.get("max_retries") or 0)
    )
    binding = authoritative_binding if isinstance(authoritative_binding, Mapping) else {}
    binding_is_authoritative = (
        acceptance_contract is not None
        and expected_profile_id is not None
        and expected_profile_version is not None
        and expected_profile_digest is not None
        and binding.get("profile_id") == expected_profile_id
        and binding.get("profile_version") == expected_profile_version
        and binding.get("profile_digest") == expected_profile_digest
        and isinstance(binding.get("profile_id"), str)
        and PROFILE_ID_RE.fullmatch(binding["profile_id"]) is not None
        and isinstance(binding.get("profile_version"), int)
        and not isinstance(binding.get("profile_version"), bool)
        and binding["profile_version"] > 0
        and isinstance(binding.get("profile_digest"), str)
        and PROFILE_DIGEST_RE.fullmatch(binding["profile_digest"]) is not None
        and isinstance(binding.get("source_git_sha"), str)
        and SOURCE_SHA_RE.fullmatch(binding["source_git_sha"]) is not None
        and binding["source_git_sha"] == os.environ.get("SOURCE_GIT_SHA", "").strip()
        and isinstance(binding.get("external_run_id"), str)
        and RUN_ID_RE.fullmatch(binding["external_run_id"]) is not None
        and binding["external_run_id"] == os.environ.get("GITHUB_RUN_ID", "").strip()
    )
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
                phase_logical = summarize_logical_results(
                    phase_results_for_users,
                    expected_count=action_count,
                    submitted_count=len(phase_results_for_users),
                )
                phase_logical["wall_seconds"] = round(phase_wall_seconds, 6)
                phase_logical["target_logical_actions_per_second"] = target_rate
                phase_logical["offered_logical_actions_per_second"] = round(
                    action_count / phase_submission_window, 3
                )
                phase_logical["actual_arrival_logical_actions_per_second"] = (
                    phase_logical.get("timing", {}).get(
                        "actual_arrival_logical_actions_per_second"
                    )
                )
                phase_logical["successful_goodput_actions_per_second"] = round(
                    float(phase_logical.get("final_successes") or 0)
                    / phase_wall_seconds,
                    3,
                )
                phase_raw = summarize_results(phase_attempts)
                _add_measured_goodput(phase_raw, phase_wall_seconds)
                phase_raw["attempts_per_second"] = round(
                    float(phase_raw.get("requests") or 0) / phase_submission_window,
                    3,
                )
                planned_phases[phase_name] = {
                    "configured_actions": action_count,
                    "submitted_actions": len(phase_results_for_users),
                    "missing_actions": max(
                        0, action_count - len(phase_results_for_users)
                    ),
                    "complete": (
                        len(phase_results_for_users) == action_count
                        and timing_summary_is_complete(phase_logical.get("timing"))
                        and timing_summary_is_complete(phase_raw.get("timing"))
                    ),
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
        primary_logical = summarize_logical_results(
            primary,
            expected_count=(
                sum(int(phase.get("logical_actions") or 0) for phase in phase_plan)
                if phase_plan
                else len(users)
            ),
            submitted_count=len(primary),
        )
        primary_logical["wall_seconds"] = round(primary_wall_seconds, 6)
        primary_logical["successful_goodput_actions_per_second"] = round(
            float(primary_logical.get("final_successes") or 0) / primary_wall_seconds,
            3,
        )
        if phase_plan:
            primary_logical["offered_logical_actions_per_second"] = round(
                len(primary) / max(0.001, offered_window_seconds),
                3,
            )
        primary_raw = summarize_results(primary_attempts)
        _add_measured_goodput(primary_raw, primary_wall_seconds)
        phase_results["primary"] = {
            "raw_http": primary_raw,
            "logical": primary_logical,
        }
        all_results.extend(primary_attempts)

        successful_primary_ids = {
            result.user_id
            for result in primary
            if result.attempts and result.final.ok is True and result.user_id
        }
        # Idempotency checks are meaningful only for actions that completed
        # successfully.  Bind the duplicate candidate pool to that exact
        # primary population for every scenario, including normal SLO runs.
        duplicate_candidates = [
            user for user in primary_users if user.user_id in successful_primary_ids
        ]
        # A duplicate is meaningful only for a primary action that reached the
        # service.  Under stress/spike shedding can therefore leave fewer
        # eligible candidates than the configured duplicate count.  Never
        # shrink the configured workload and call that a pass: run every
        # available candidate, retain the configured expected population, and
        # mark the phase incomplete when any duplicate action is missing.
        duplicate_users = duplicate_candidates[:duplicate_count]
        duplicate_started_at = time.monotonic()
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
        duplicate_logical = summarize_logical_results(
            duplicates,
            expected_count=duplicate_count,
            submitted_count=len(duplicates),
        )
        duplicate_wall_seconds = max(0.001, time.monotonic() - duplicate_started_at)
        duplicate_logical["wall_seconds"] = round(duplicate_wall_seconds, 6)
        duplicate_logical["successful_goodput_actions_per_second"] = round(
            float(duplicate_logical.get("final_successes") or 0)
            / duplicate_wall_seconds,
            3,
        )
        duplicate_raw = summarize_results(duplicate_attempts)
        _add_measured_goodput(duplicate_raw, duplicate_wall_seconds)
        duplicate_raw["configured_logical_actions"] = duplicate_count
        duplicate_raw["submitted_logical_actions"] = len(duplicates)
        duplicate_raw["missing_logical_actions"] = max(
            0, duplicate_count - len(duplicates)
        )
        duplicate_phase_complete = (
            len(duplicate_users) == duplicate_count
            and timing_summary_is_complete(duplicate_logical.get("timing"))
        )
        phase_results["duplicate"] = {
            "configured_actions": duplicate_count,
            "candidate_actions": len(duplicate_candidates),
            "submitted_actions": len(duplicates),
            "completed_actions": int(
                (duplicate_logical.get("timing") or {}).get("completed_count") or 0
            ),
            "missing_actions": max(0, duplicate_count - len(duplicates)),
            "complete": duplicate_phase_complete,
            "status": "complete" if duplicate_phase_complete else "incomplete",
            "incomplete_reason": (
                "primary_successes_below_duplicate_count"
                if len(duplicate_candidates) < duplicate_count
                else "duplicate_timing_incomplete"
                if len(duplicate_users) == duplicate_count
                else "duplicate_actions_not_submitted"
            ) if not duplicate_phase_complete else None,
            "raw_http": duplicate_raw,
            "logical": duplicate_logical,
        }
        all_results.extend(duplicate_attempts)

        # State reads are planned per manifest tournament, not merely for the
        # subset that happened to receive a primary action in a capacity
        # phase.  Keep untouched tournaments in the evidence with an expected
        # ready count of zero so the state-read population remains exactly
        # bound to the selected fixture plan.
        users_by_slug: dict[str, VirtualUser] = {}
        expected_by_slug: Counter[str] = Counter()
        successful_primary_by_slug: Counter[str] = Counter()
        for result in primary:
            if result.attempts and result.final.ok is True:
                slug = result.final.path.split("/", 3)[2]
                successful_primary_by_slug[slug] += 1
        for user in users:
            users_by_slug.setdefault(user.tournament_slug, user)
            expected_by_slug[user.tournament_slug] = successful_primary_by_slug.get(
                user.tournament_slug,
                0,
            )
        state_results: list[RequestResult] = []
        # Slugs are needed transiently to select the request fixture, but they
        # must never become evidence keys.  The acceptance contract only needs
        # a one-to-one bounded map, so expose deterministic numeric slots.
        expected_state_ready_counts: dict[str, int] = {}
        observed_state_ready_counts: dict[str, int | None] = {}
        state_ready_count_mismatches = 0
        for state_slot, (slug, user) in enumerate(sorted(users_by_slug.items())):
            evidence_key = str(state_slot)
            state_scheduled_at = time.monotonic()
            state_started_at = state_scheduled_at
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
            result = _annotate_timing(
                result,
                scheduled_at=state_scheduled_at,
                enqueued_at=state_scheduled_at,
                fallback_started_at=state_started_at,
                fallback_finished_at=time.monotonic(),
            )
            expected_count = expected_by_slug[slug]
            expected_state_ready_counts[evidence_key] = expected_count
            active_round = (
                result.response_json.get("active_round")
                if isinstance(result.response_json, dict)
                else None
            )
            observed_ready_count = (
                active_round.get("ready_count")
                if isinstance(active_round, dict)
                else None
            )
            if (
                isinstance(observed_ready_count, bool)
                or not isinstance(observed_ready_count, int)
                or observed_ready_count < 0
            ):
                observed_ready_count = None
            observed_state_ready_counts[evidence_key] = observed_ready_count
            if observed_ready_count != expected_count:
                state_ready_count_mismatches += 1
            result.ok = (
                result.ok is True
                and observed_ready_count is not None
                and observed_ready_count == expected_count
            )
            if not result.ok and result.error_kind is None:
                result.error_kind = "authoritative_state_mismatch"
            state_results.append(result)
        state_summary = summarize_results(
            state_results,
            expected_count=len(users_by_slug),
            submitted_count=len(state_results),
        )
        state_summary.update(
            {
                "configured_reads": len(users_by_slug),
                "submitted_reads": len(state_results),
                "completed_reads": len(state_results),
                "missing_reads": max(0, len(users_by_slug) - len(state_results)),
                "complete": (
                    len(state_results) == len(users_by_slug)
                    and timing_summary_is_complete(state_summary.get("timing"))
                    and state_ready_count_mismatches == 0
                ),
                "authoritative": True,
                "ready_count_evidence": {
                    "complete": state_ready_count_mismatches == 0
                    and all(value is not None for value in observed_state_ready_counts.values()),
                    "expected": expected_state_ready_counts,
                    "observed": observed_state_ready_counts,
                    "mismatches": state_ready_count_mismatches,
                },
            }
        )
        phase_results["state"] = state_summary
        all_results.extend(state_results)
        primary_summary = phase_results["primary"]["logical"]
        duplicate_summary = phase_results.get("duplicate", {}).get("logical", {})
        changed_counts = primary_summary.get("changed_counts", {})
        duplicate_changed = duplicate_summary.get("changed_counts", {})
        strict_primary_contract = scenario_kind not in {"stress", "spike"}
        strict_duplicate_contract = strict_primary_contract
        duplicate_successes = int(duplicate_summary.get("final_successes", 0))
        duplicate_failures = int(duplicate_summary.get("final_failures", 0))
        duplicate_raw = phase_results.get("duplicate", {}).get("raw_http", {})
        duplicate_correctness = (
            duplicate_phase_complete
            and duplicate_failures == 0
            and int(duplicate_changed.get("False", 0)) == duplicate_count
            if strict_duplicate_contract
            else (
                # Stress may shed a duplicate with the same bounded 503 as a
                # primary action. Every duplicate that is accepted must still
                # be an idempotent noop, and no unexpected status is allowed.
                duplicate_phase_complete
                and
                int(duplicate_changed.get("False", 0)) == duplicate_successes
                and int(duplicate_raw.get("unexpected_statuses") or 0) == 0
            )
        )
        contract_ok = (
            primary_summary["actions"] == len(primary_users)
            and (
                primary_summary["final_failures"] == 0
                if strict_primary_contract
                else int(changed_counts.get("True", 0)) == primary_summary["final_successes"]
            )
            and int(changed_counts.get("True", 0)) == primary_summary["final_successes"]
            and duplicate_correctness
            and int(phase_results["primary"]["raw_http"].get("unexpected_statuses") or 0) == 0
            and phase_results["state"]["errors"] == 0
        )
    elif mode == "page-load":
        user_indexes = {user.user_id: index for index, user in enumerate(users)}

        def page_builder(
            origin_value: str,
            user: VirtualUser,
            phase: str,
            request_timeout: float,
        ) -> RequestResult:
            request_kwargs: dict[str, Any] = {
                "session_cookie_name": session_cookie_name,
                "csrf_cookie_name": csrf_cookie_name,
            }
            if timeout_diagnostics_run_id:
                request_kwargs["diagnostic_id"] = (
                    f"tdiag-{timeout_diagnostics_run_id}-{user_indexes[user.user_id]:05d}"
                )
            if client_transport != DEFAULT_CLIENT_TRANSPORT:
                request_kwargs["transport"] = client_transport
            return _page_request(
                origin_value,
                user,
                phase,
                request_timeout,
                **request_kwargs,
            )

        page_started_at = time.monotonic()
        page_results = run_phase(
            origin,
            users,
            phase="authenticated_page_load",
            spread_seconds=spread_seconds,
            concurrency=concurrency,
            timeout=timeout,
            request_builder=page_builder,
        )
        page_summary = summarize_results(
            page_results,
            expected_count=len(users),
            submitted_count=len(page_results),
        )
        page_wall_seconds = max(0.001, time.monotonic() - page_started_at)
        page_summary["wall_seconds"] = round(page_wall_seconds, 6)
        page_summary["successful_goodput_actions_per_second"] = round(
            float(page_summary.get("successful_responses") or 0) / page_wall_seconds,
            3,
        )
        phase_results["authenticated_page_load"] = page_summary
        all_results.extend(page_results)
        page_summary = phase_results["authenticated_page_load"]
        contract_ok = (
            page_summary["requests"] == len(users)
            and page_summary["errors"] == 0
            and page_summary["unexpected_statuses"] == 0
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

        read_results: list[RequestResult] = []
        read_started_at = time.monotonic()
        ramp_stages: dict[str, dict[str, Any]] = {}
        for stage_concurrency in read_concurrency_stages:
            stage_started_at = time.monotonic()
            stage_results = run_phase(
                origin,
                users,
                phase=f"scale_external_read_mix_c{stage_concurrency}",
                spread_seconds=spread_seconds,
                concurrency=stage_concurrency,
                timeout=timeout,
                request_builder=read_builder,
            )
            read_results.extend(stage_results)
            stage_summary = summarize_results(
                stage_results,
                expected_count=len(users),
                submitted_count=len(stage_results),
            )
            stage_wall_seconds = max(0.001, time.monotonic() - stage_started_at)
            stage_summary["wall_seconds"] = round(stage_wall_seconds, 6)
            stage_summary["requests_per_second"] = round(
                float(stage_summary.get("requests") or 0) / stage_wall_seconds,
                3,
            )
            stage_summary["successful_goodput_actions_per_second"] = round(
                float(stage_summary.get("successful_responses") or 0)
                / stage_wall_seconds,
                3,
            )
            ramp_stages[str(stage_concurrency)] = stage_summary
        read_summary = summarize_results(
            read_results,
            expected_count=len(users) * len(read_concurrency_stages),
            submitted_count=len(read_results),
        )
        read_wall_seconds = max(0.001, time.monotonic() - read_started_at)
        read_summary["wall_seconds"] = round(read_wall_seconds, 6)
        read_summary["successful_goodput_actions_per_second"] = round(
            float(read_summary.get("successful_responses") or 0) / read_wall_seconds,
            3,
        )
        phase_results["read_mix"] = read_summary
        if concurrency_stages is not None:
            phase_results["capacity_ramp"] = {
                "concurrency_stages": list(read_concurrency_stages),
                "stages": ramp_stages,
                "analysis": analyze_concurrency_ramp(ramp_stages),
            }
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

            refresh_started_at = time.monotonic()
            refresh_results = run_phase(
                origin,
                refresh_users,
                phase="manual_workspace_refresh",
                spread_seconds=spread_seconds,
                concurrency=concurrency,
                timeout=timeout,
                request_builder=refresh_builder,
            )
            refresh_summary = summarize_results(
                refresh_results,
                expected_count=manual_refresh_count,
                submitted_count=len(refresh_results),
            )
            refresh_wall_seconds = max(0.001, time.monotonic() - refresh_started_at)
            refresh_summary["wall_seconds"] = round(refresh_wall_seconds, 6)
            refresh_summary["successful_goodput_actions_per_second"] = round(
                float(refresh_summary.get("successful_responses") or 0)
                / refresh_wall_seconds,
                3,
            )
            phase_results["manual_refresh"] = refresh_summary
            all_results.extend(refresh_results)
        read_mix_summary = phase_results["read_mix"]
        strict_read_contract = scenario_kind not in {"stress", "spike"}
        expected_read_requests = len(users) * len(read_concurrency_stages)
        read_errors_ok = (
            read_mix_summary["errors"] == 0
            if strict_read_contract
            else read_mix_summary["errors"] == read_mix_summary["temporary_overload_responses"]
        )
        contract_ok = (
            read_mix_summary["requests"] == expected_read_requests
            and read_errors_ok
            and read_mix_summary["unexpected_statuses"] == 0
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

    partial_work = _has_partial_timing(phase_results)
    if partial_work:
        # Never turn a partial result set into a green experiment merely
        # because the rows that did complete met their latency thresholds.
        contract_ok = False
    overall = summarize_results(all_results)
    if mode == "ready-vote":
        primary_logical_result = phase_results.get("primary", {}).get("logical", {})
        duplicate_logical_result = phase_results.get("duplicate", {}).get("logical", {})
        primary_action_results = primary
        duplicate_action_results = duplicates
        logical_summary = summarize_logical_results(
            primary_action_results + duplicate_action_results,
            expected_count=(
                int(
                    (primary_logical_result.get("timing") or {}).get(
                        "expected_count"
                    )
                    or len(primary)
                )
                + duplicate_count
            ),
            submitted_count=len(primary_action_results) + len(duplicate_action_results),
        )
        logical_summary["primary_actions"] = len(primary_action_results)
        logical_summary["duplicate_actions"] = len(duplicate_action_results)
        logical_summary["configured_duplicate_actions"] = duplicate_count
        logical_summary["duplicate_phase_complete"] = bool(
            phase_results.get("duplicate", {}).get("complete")
        )
        logical_summary["duplicate_offered_logical_actions_per_second"] = (
            (duplicate_logical_result.get("timing") or {}).get(
                "offered_logical_actions_per_second"
            )
        )
        logical_summary["offered_logical_actions_per_second"] = (
            primary_logical_result.get("offered_logical_actions_per_second")
        )
        action_attempts = primary_attempts + duplicate_attempts
        raw_http_summary = summarize_results(action_attempts)
        raw_http_summary["configured_duplicate_actions"] = duplicate_count
        raw_http_summary["submitted_duplicate_actions"] = len(duplicate_action_results)
        raw_http_summary["missing_duplicate_actions"] = max(
            0, duplicate_count - len(duplicate_action_results)
        )
    else:
        logical_summary = overall
        raw_http_summary = overall
        if mode == "read-mix":
            logical_summary["primary_actions"] = len(read_results)
            logical_summary["manual_refresh_actions"] = len(refresh_results)
        elif mode == "page-load":
            logical_summary["primary_actions"] = len(page_results)
    finished_at = datetime.now(UTC)
    wall_seconds = max(0.001, (finished_at - started_at).total_seconds())
    _add_measured_goodput(overall, wall_seconds)
    _add_measured_goodput(raw_http_summary, wall_seconds)
    raw_http_summary["requests_per_second"] = round(
        float(raw_http_summary.get("requests") or 0) / wall_seconds,
        3,
    )
    if mode == "ready-vote":
        # The aggregate logical population and raw HTTP population share this
        # one measured run window.  Persist it on the logical scope so the
        # evaluator never has to infer a different timing window for
        # successful logical goodput.
        logical_summary["wall_seconds"] = round(wall_seconds, 6)
        logical_summary["successful_goodput_actions_per_second"] = round(
            float(logical_summary.get("final_successes") or 0) / wall_seconds,
            3,
        )
        raw_http_summary["state_read_requests"] = len(state_results)
        raw_http_summary["total_requests_including_state"] = int(
            overall.get("requests") or 0
        )
    logical_timing = logical_summary.get("timing") or {}
    raw_timing = raw_http_summary.get("timing") or {}
    dropped_work_value = logical_timing.get("dropped_work")
    if not isinstance(dropped_work_value, int):
        dropped_work_value = raw_timing.get("dropped_work")
    if not isinstance(dropped_work_value, int):
        dropped_work_value = None
    acceptance_phase_summaries = (
        ((phase_results.get("ramp") or {}).get("phases") or {}).copy()
        if mode == "ready-vote" and isinstance(phase_results.get("ramp"), dict)
        else {}
    )
    if mode == "ready-vote":
        acceptance_phase_summaries["primary"] = phase_results["primary"]
        acceptance_phase_summaries["duplicate"] = phase_results["duplicate"]
        acceptance_phase_summaries["state"] = phase_results["state"]
    elif mode == "read-mix":
        ramp = phase_results.get("capacity_ramp")
        if isinstance(ramp, dict):
            acceptance_phase_summaries["capacity_ramp"] = ramp
        for phase_name in ("read_mix", "manual_refresh"):
            phase = phase_results.get(phase_name)
            if isinstance(phase, dict):
                acceptance_phase_summaries[phase_name] = {
                    "logical": phase,
                    "raw_http": phase,
                }
    elif mode == "page-load":
        phase = phase_results.get("authenticated_page_load")
        if isinstance(phase, dict):
            acceptance_phase_summaries["authenticated_page_load"] = {
                "logical": phase,
                "raw_http": phase,
            }
    allowed_phase_names = set(acceptance_phase_summaries)
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
        phase_summaries=acceptance_phase_summaries or None,
        require_exact_observer_binding=require_exact_observer_binding,
        canonical_evidence=binding_is_authoritative,
        expected_logical_scope=(
            "logical_user_actions" if mode == "ready-vote" else "full_population"
        ),
        allowed_phase_names=allowed_phase_names,
        expected_phase_plan=acceptance_phase_plan,
        expected_duplicate_count=duplicate_count if mode == "ready-vote" else None,
        expected_primary_action_count=expected_primary_action_count,
        expected_total_logical_action_count=expected_total_logical_action_count,
        expected_stage_action_counts=expected_stage_action_counts,
        expected_phase_action_counts=expected_phase_action_counts,
        expected_state_read_count=expected_state_read_count if mode == "ready-vote" else None,
        max_retries=expected_max_retries,
    )
    if acceptance_contract is not None and not binding_is_authoritative:
        acceptance = {
            **acceptance,
            "passed": False,
            "decision": "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
            "authoritative": False,
            "dispatchable": False,
            "checks": {
                **(acceptance.get("checks") or {}),
                "authoritative_binding": False,
            },
        }
    return {
        "schema": 1,
        "measurement_schema": 2,
        "timing_schema": 1,
        "scope": "full_population",
        "mode": mode,
        # The origin is used transiently for requests, but the persisted load
        # envelope carries only its closed deployment class.  A full URL could
        # include a host/path/query when this runner is reused outside CI.
        "origin_class": "production_origin",
        "fixture_marker": manifest["marker"],
        "users": len(users),
        "tournaments": len(manifest["tournaments"]),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "wall_seconds": round(wall_seconds, 6),
        "opening_spread_seconds": spread_seconds,
        "scenario_kind": scenario_kind,
        "client_transport": client_transport,
        "duplicate_count": duplicate_count,
        "manual_refresh_count": manual_refresh_count,
        "concurrency": concurrency,
        "concurrency_stages": (
            list(read_concurrency_stages)
            if mode == "read-mix" and concurrency_stages is not None
            else None
        ),
        "offered_logical_actions_per_second": round(
            float(logical_summary.get("offered_logical_actions_per_second") or 0)
            if phase_plan
            else float(logical_summary.get("actions") or 0) / wall_seconds,
            3,
        ) if mode == "ready-vote" else None,
        "actual_arrival_logical_actions_per_second": (
            (logical_summary.get("timing") or {}).get(
                "actual_arrival_logical_actions_per_second"
            )
            if mode == "ready-vote"
            else None
        ),
        "actual_arrival_requests_per_second": (
            (raw_http_summary.get("timing") or {}).get(
                "actual_arrival_requests_per_second"
            )
        ),
        "offered_requests_per_second": (
            (raw_http_summary.get("timing") or {}).get(
                "offered_requests_per_second"
            )
        ),
        "late_start_count": int(
            (logical_summary.get("timing") or {}).get("late_start_count")
            or (raw_http_summary.get("timing") or {}).get("late_start_count")
            or 0
        ),
        "dropped_work": int(
            dropped_work_value
        ) if dropped_work_value is not None else None,
        "partial_work": partial_work,
        "trace": trace,
        "timeout_path_diagnostics": {
            "enabled": timeout_diagnostics_run_id is not None,
            "request_count": len(all_results) if timeout_diagnostics_run_id else 0,
        },
        "phases": phase_results,
        "overall": overall,
        "raw_http": raw_http_summary,
        # Keep an explicit logical envelope for every dispatchable workload.
        # Read/page profiles use the full HTTP population as their logical
        # action population, but the field must not be inferred by the
        # evaluator from ``overall`` or substituted into ``raw_http``.
        "logical": logical_summary,
        "acceptance": acceptance,
        "authoritative": binding_is_authoritative,
        "dispatchable": binding_is_authoritative,
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
    parser.add_argument(
        "--mode",
        choices=("ready-vote", "read-mix", "page-load"),
        required=True,
    )
    parser.add_argument("--spread-seconds", type=float, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--duplicate-count", type=int, required=True)
    parser.add_argument("--manual-refresh-count", type=int, required=True)
    parser.add_argument("--p95-budget-ms", type=float, required=True)
    parser.add_argument("--p99-budget-ms", type=float, required=True)
    parser.add_argument("--failure-budget-percent", type=float, required=True)
    parser.add_argument(
        "--client-transport",
        choices=tuple(sorted(SUPPORTED_PAGE_TRANSPORTS)),
        default=DEFAULT_CLIENT_TRANSPORT,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report: dict[str, Any]
    try:
        if not math.isfinite(args.spread_seconds) or not 0 <= args.spread_seconds <= 3_600:
            raise ExternalLoadError("spread-seconds must be between 0 and 3600")
        if not 1 <= args.concurrency <= MAX_CONCURRENCY:
            raise ExternalLoadError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
        if (
            not math.isfinite(args.timeout)
            or args.timeout <= 0
            or args.timeout > 300
        ):
            raise ExternalLoadError("timeout must be between 0 and 300 seconds")
        for value, field in (
            (args.p95_budget_ms, "p95-budget-ms"),
            (args.p99_budget_ms, "p99-budget-ms"),
            (args.failure_budget_percent, "failure-budget-percent"),
        ):
            if not math.isfinite(value) or value < 0:
                raise ExternalLoadError(f"{field} must be finite and non-negative")
        manifest, users = load_manifest(args.manifest)
        if args.duplicate_count < 0:
            raise ExternalLoadError("duplicate-count must not be negative")
        if args.duplicate_count > len(users):
            raise ExternalLoadError(
                "duplicate-count exceeds the manifest user population"
            )
        duplicate_count = args.duplicate_count
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
            p95_budget_ms=args.p95_budget_ms,
            p99_budget_ms=args.p99_budget_ms,
            failure_budget_percent=args.failure_budget_percent,
            client_transport=args.client_transport,
        )
    except Exception as exc:
        error_class = safe_error_class(type(exc).__name__)
        report = {
            "schema": 1,
            "measurement_schema": 2,
            "timing_schema": 1,
            "mode": args.mode,
            "passed": False,
            "authoritative": False,
            "dispatchable": False,
            "error_class": error_class,
            "acceptance": {
                "passed": False,
                "decision": "LOAD RUN FAILED",
                "authoritative": False,
                "dispatchable": False,
                "contract_ok": False,
                "error_class": error_class,
            },
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
