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
from array import array
import base64
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
import http.client
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from tools.platform_http_transport import HTTP11KeepAliveClient
    from tools.platform_load_runtime import (
        LoadRuntimeBudget,
    )
    from tools.platform_load_acceptance import (
        derive_expected_phase_plan,
        evaluate_acceptance,
        timing_summary_is_complete,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_http_transport import HTTP11KeepAliveClient
    from platform_load_runtime import LoadRuntimeBudget
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
SOURCE_BOUND_MANIFEST_SCHEMA = 2
MAX_USERS = 20_000
MAX_TOURNAMENTS = 64
MAX_CONCURRENCY = 512
RESPONSE_BODY_LIMIT = 2 * 1024 * 1024
ERROR_SAMPLE_LIMIT = 25
TIMEOUT_DIAGNOSTIC_LIMIT = 25
DIAGNOSTIC_HEADER_LIMIT = 128
RESPONSE_JSON_CAPTURE_LIMIT = 64 * 1024
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

_DEFAULT_HTTPS_CONTEXT_LOCK = threading.Lock()
_DEFAULT_HTTPS_CONTEXT: ssl.SSLContext | None = None


def _default_https_context() -> ssl.SSLContext:
    """Return the runner's immutable urllib-compatible HTTPS context.

    urllib creates a new default context for every ``urlopen`` call when no
    context is supplied.  Build the same verified HTTP/1.1 context once and
    share it across the runner's workers; do not mutate it after publication.
    """

    global _DEFAULT_HTTPS_CONTEXT
    context = _DEFAULT_HTTPS_CONTEXT
    if context is not None:
        return context
    with _DEFAULT_HTTPS_CONTEXT_LOCK:
        context = _DEFAULT_HTTPS_CONTEXT
        if context is None:
            context = ssl.create_default_context()
            context.set_alpn_protocols(["http/1.1"])
            if getattr(context, "post_handshake_auth", None) is not None:
                context.post_handshake_auth = True
            if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
                raise ExternalLoadError("default HTTPS verification context is unsafe")
            _DEFAULT_HTTPS_CONTEXT = context
    return context


SOURCE_BINDING_KEYS = frozenset(
    {
        "schema",
        "binding_mode",
        "runner_sha",
        "app_target_sha",
        "baseline_identity",
        "receipt_document_sha256",
        "receipt_artifact_id",
        "receipt_artifact_name",
        "receipt_artifact_digest",
        "receipt_archive_sha256",
        "cumulative_manifest_sha256",
        "source_security_run_id",
        "source_security_run_attempt",
        "autodeploy_run_id",
        "autodeploy_run_attempt",
        "production_deploy_run_id",
        "production_deploy_run_attempt",
    }
)


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
    # Internal submission order used only to make bounded diagnostics
    # deterministic.  It is never serialized into a report.
    submission_index: int | None = None


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
    submission_index: int | None = None

    @property
    def final(self) -> RequestResult:
        return self.attempts[-1]

    @property
    def retry_count(self) -> int:
        return max(0, len(self.attempts) - 1)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    return _percentile_from_ordered(sorted(values), percent)


def _percentile_from_ordered(ordered: list[float], percent: float) -> float | None:
    """Apply the historical linear interpolation to an already sorted list."""

    if not ordered:
        return None
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


class _NumericSamples:
    """Compact exact percentile samples.

    A Python float in a list costs several times more than its eight-byte
    payload.  The runner keeps only these compact arrays while a phase is
    active; request/result objects and response bodies are released after each
    completion.  The configured workload bounds the number of samples (the
    profile's planned attempt count), so this is an accounted metric buffer,
    not an unbounded object population.  Percentiles still use the historical
    sorted linear-interpolation algorithm in :func:`percentile`.
    """

    __slots__ = ("values",)

    def __init__(self) -> None:
        self.values = array("d")

    def append(self, value: float) -> None:
        self.values.append(float(value))

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self):
        return iter(self.values)


def metric_stats(values: Iterable[float]) -> dict[str, Any]:
    valid_values = array("d")
    for value in values:
        numeric = _finite_nonnegative_measurement(value)
        if numeric is not None:
            valid_values.append(float(numeric))
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
    ordered = sorted(valid_values)
    return {
        "count": len(values),
        "avg_ms": round(sum(valid_values) / len(valid_values), 3),
        "p50_ms": round(_percentile_from_ordered(ordered, 50) or 0, 3),
        "p90_ms": round(_percentile_from_ordered(ordered, 90) or 0, 3),
        "p95_ms": round(_percentile_from_ordered(ordered, 95) or 0, 3),
        "p99_ms": round(_percentile_from_ordered(ordered, 99) or 0, 3),
        "max_ms": round(max(valid_values), 3),
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


class _TimingAccumulator:
    """Streaming timing summary with compact exact percentile samples."""

    __slots__ = (
        "service_latency",
        "observed",
        "queue_wait",
        "schedule_delay",
        "late_start",
        "starts",
        "finishes",
        "scheduled",
        "started",
        "response_completions",
        "user_observed_count",
        "scheduled_count",
        "missing_schedule_context",
        "missing_timing_context",
        "invalid_timing_context",
        "completed_count",
    )

    def __init__(self) -> None:
        self.service_latency = _NumericSamples()
        self.observed = _NumericSamples()
        self.queue_wait = _NumericSamples()
        self.schedule_delay = _NumericSamples()
        self.late_start = _NumericSamples()
        self.starts = _NumericSamples()
        self.finishes = _NumericSamples()
        self.scheduled = _NumericSamples()
        self.started = 0
        self.response_completions = 0
        self.user_observed_count = 0
        self.scheduled_count = 0
        self.missing_schedule_context = 0
        self.missing_timing_context = 0
        self.invalid_timing_context = 0
        self.completed_count = 0

    @staticmethod
    def _finite_timestamp(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            numeric = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(numeric) or not 0 <= numeric <= MAX_MEASUREMENT:
            return None
        return numeric

    def add(self, result: Any) -> None:
        self.completed_count += 1
        service_value = _finite_nonnegative_measurement(
            getattr(result, "elapsed_ms", None)
        )
        if service_value is not None:
            self.service_latency.append(service_value)
        scheduled_at = self._finite_timestamp(
            getattr(result, "scheduled_at_monotonic", None)
        )
        enqueued_at = self._finite_timestamp(
            getattr(result, "enqueued_at_monotonic", None)
        )
        started_at = self._finite_timestamp(
            getattr(result, "started_at_monotonic", None)
        )
        finished_at = self._finite_timestamp(
            getattr(result, "finished_at_monotonic", None)
        )
        user_observed = self._finite_timestamp(
            getattr(result, "user_observed_elapsed_ms", None)
        )

        if scheduled_at is None or enqueued_at is None:
            self.missing_schedule_context += 1
        else:
            self.scheduled_count += 1
            self.scheduled.append(scheduled_at)
        if started_at is not None:
            self.started += 1
            self.starts.append(started_at)
        if finished_at is not None:
            self.response_completions += 1
            self.finishes.append(finished_at)
        if user_observed is not None:
            self.user_observed_count += 1
            self.observed.append(user_observed)

        queue = self._finite_timestamp(
            getattr(result, "executor_queue_wait_ms", None)
        )
        if queue is not None:
            self.queue_wait.append(queue)
        delay = self._finite_timestamp(getattr(result, "schedule_delay_ms", None))
        if delay is not None:
            self.schedule_delay.append(delay)
        late = self._finite_timestamp(getattr(result, "late_start_ms", None))
        if late is not None:
            self.late_start.append(late)

        if any(
            value is None
            for value in (scheduled_at, enqueued_at, started_at, finished_at, user_observed)
        ):
            self.missing_timing_context += 1
        elif finished_at < started_at:
            self.invalid_timing_context += 1

    @staticmethod
    def _window(values: _NumericSamples) -> float | None:
        if len(values) >= 2:
            return max(0.001, max(values.values) - min(values.values))
        return 0.001 if values else None

    def summary(
        self,
        *,
        unit: str,
        expected_count: int | None = None,
        submitted_count: int | None = None,
    ) -> dict[str, Any]:
        arrival_window = self._window(self.starts)
        requests_per_second = (
            self.started / arrival_window if arrival_window is not None else None
        )
        offered_window = self._window(self.scheduled)
        offered_per_second = (
            self.scheduled_count / offered_window
            if offered_window is not None
            else None
        )
        completion_window = self._window(self.finishes)
        completed_count = self.completed_count
        expected = completed_count if expected_count is None else max(0, int(expected_count))
        submitted = (
            completed_count
            if submitted_count is None
            else max(0, int(submitted_count))
        )
        dropped_work = max(0, expected - self.started)
        timing_counts = (
            expected,
            submitted,
            completed_count,
            self.scheduled_count,
            self.started,
            self.response_completions,
            self.user_observed_count,
        )
        partial = (
            len(set(timing_counts)) != 1
            or self.missing_schedule_context > 0
            or self.missing_timing_context > 0
            or self.invalid_timing_context > 0
            or dropped_work != 0
        )
        late_count = sum(value > 0 for value in self.late_start)
        return {
            "timing_schema": 2,
            "service_latency": metric_stats(self.service_latency),
            "user_observed_latency": metric_stats(self.observed),
            "executor_queue_wait": metric_stats(self.queue_wait),
            "schedule_delay": metric_stats(self.schedule_delay),
            "late_start": metric_stats(self.late_start),
            "late_start_count": late_count,
            "late_start_percent": round(
                late_count * 100 / max(1, completed_count),
                4,
            ),
            "expected_count": expected,
            "submitted_count": submitted,
            "completed_count": completed_count,
            "partial": partial,
            "scheduled_count": self.scheduled_count,
            "started_count": self.started,
            "actual_request_start_count": self.started,
            "actual_start_count": self.started,
            "response_completion_count": self.response_completions,
            "user_observed_count": self.user_observed_count,
            "response_completion_window_seconds": (
                round(completion_window, 6)
                if completion_window is not None
                else None
            ),
            "dropped_work": dropped_work,
            "missing_schedule_context": self.missing_schedule_context,
            "missing_timing_context": self.missing_timing_context,
            "invalid_timing_context": self.invalid_timing_context,
            "actual_arrival_window_seconds": (
                round(arrival_window, 6) if arrival_window is not None else None
            ),
            "offered_arrival_window_seconds": (
                round(offered_window, 6) if offered_window is not None else None
            ),
            f"offered_{unit}_per_second": (
                round(offered_per_second, 3)
                if offered_per_second is not None
                else None
            ),
            f"actual_arrival_{unit}_per_second": (
                round(requests_per_second, 3)
                if requests_per_second is not None
                else None
            ),
        }


def _append_measurement(samples: _NumericSamples, value: Any) -> None:
    numeric = _finite_nonnegative_measurement(value)
    if numeric is not None:
        samples.append(numeric)


class _ResultAccumulator:
    """Reduce each completed HTTP result without retaining the result object."""

    __slots__ = (
        "status_counts",
        "by_route",
        "latencies",
        "errors",
        "temporary_overloads",
        "retry_attempts",
        "error_kinds",
        "cf_error_types",
        "cf_error_origins",
        "error_sample_entries",
        "timeout_diagnostic_entries",
        "error_sample_total",
        "timeout_diagnostic_total",
        "first_byte_times",
        "response_bytes_count",
        "response_bytes_total",
        "response_bytes_max",
        "changed",
        "transport_names",
        "transport_http_versions",
        "transport_reused",
        "transport_new",
        "transport_phase_values",
        "cf_rays",
        "timing",
        "requests",
    )

    def __init__(self) -> None:
        self.status_counts: Counter[str] = Counter()
        self.by_route: dict[str, _NumericSamples] = {}
        self.latencies = _NumericSamples()
        self.errors = 0
        self.temporary_overloads = 0
        self.retry_attempts = 0
        self.error_kinds: Counter[str] = Counter()
        self.cf_error_types: Counter[str] = Counter()
        self.cf_error_origins: Counter[str] = Counter()
        self.error_sample_entries: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.timeout_diagnostic_entries: list[
            tuple[tuple[Any, ...], dict[str, Any]]
        ] = []
        self.error_sample_total = 0
        self.timeout_diagnostic_total = 0
        self.first_byte_times = _NumericSamples()
        self.response_bytes_count = 0
        self.response_bytes_total = 0
        self.response_bytes_max: int | None = None
        self.changed: Counter[str] = Counter()
        self.transport_names: Counter[str] = Counter()
        self.transport_http_versions: Counter[str] = Counter()
        self.transport_reused = 0
        self.transport_new = 0
        self.transport_phase_values: dict[str, _NumericSamples] = {}
        self.cf_rays: set[str] = set()
        self.timing = _TimingAccumulator()
        self.requests = 0

    @staticmethod
    def _append_route_sample(
        by_route: dict[str, _NumericSamples], route: str, value: float
    ) -> None:
        samples = by_route.setdefault(route, _NumericSamples())
        samples.append(value)

    def add(self, result: RequestResult) -> None:
        self.requests += 1
        self.timing.add(result)
        status_counts = self.status_counts
        status_counts[str(safe_status(result.status))] += 1
        route = safe_route_key(result.method, result.path)
        elapsed = _finite_nonnegative_measurement(result.elapsed_ms)
        if elapsed is not None:
            self._append_route_sample(self.by_route, route, elapsed)
            self.latencies.append(elapsed)
        if (
            isinstance(result.response_bytes, int)
            and not isinstance(result.response_bytes, bool)
            and result.response_bytes >= 0
        ):
            self.response_bytes_count += 1
            self.response_bytes_total += result.response_bytes
            self.response_bytes_max = (
                result.response_bytes
                if self.response_bytes_max is None
                else max(self.response_bytes_max, result.response_bytes)
            )
        _append_measurement(self.first_byte_times, result.time_to_first_byte_ms)
        if result.transport_timing:
            transport = result.transport_timing
            transport_name = transport.get("transport")
            if transport_name in SUPPORTED_PAGE_TRANSPORTS:
                self.transport_names[str(transport_name)] += 1
            elif transport_name is not None:
                self.transport_names["other"] += 1
            http_version = transport.get("http_version")
            if http_version in {"1.0", "1.1", "2", "3"}:
                self.transport_http_versions[str(http_version)] += 1
            elif http_version is not None:
                self.transport_http_versions["other"] += 1
            if transport.get("connection_reused") is True:
                self.transport_reused += 1
            elif transport.get("connection_reused") is False:
                self.transport_new += 1
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
                    self.transport_phase_values.setdefault(key, _NumericSamples()).append(value)
        if (
            isinstance(result.attempt_number, int)
            and not isinstance(result.attempt_number, bool)
            and result.attempt_number > 1
        ):
            self.retry_attempts += 1
        if _ready_vote_overload(result) or _authenticated_read_overload(result):
            self.temporary_overloads += 1
        if result.ok is not True:
            self.errors += 1
            kind = safe_error_class(result.error_kind or "unexpected", status=result.status)
            self.error_kinds[kind] += 1
            self.error_sample_total += 1
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
            order = (
                safe_phase(result.phase),
                result.submission_index
                if isinstance(result.submission_index, int)
                and not isinstance(result.submission_index, bool)
                else self.error_sample_total,
                result.attempt_number
                if isinstance(result.attempt_number, int)
                and not isinstance(result.attempt_number, bool)
                else 1,
            )
            self.error_sample_entries.append((order, error_sample))
            self.error_sample_entries.sort(key=lambda item: item[0])
            del self.error_sample_entries[ERROR_SAMPLE_LIMIT:]
            if result.diagnostic_id and kind == "timeout":
                self.timeout_diagnostic_total += 1
                timeout_diagnostic = {
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
                if isinstance(result.diagnostic_id, str) and re.fullmatch(
                    r"tdiag-[0-9]{1,32}-[0-9]{5}", result.diagnostic_id
                ):
                    timeout_diagnostic["diagnostic_id"] = result.diagnostic_id
                timeout_order = (
                    safe_phase(result.phase),
                    result.submission_index
                    if isinstance(result.submission_index, int)
                    and not isinstance(result.submission_index, bool)
                    else self.timeout_diagnostic_total,
                    result.attempt_number
                    if isinstance(result.attempt_number, int)
                    and not isinstance(result.attempt_number, bool)
                    else 1,
                )
                self.timeout_diagnostic_entries.append(
                    (timeout_order, timeout_diagnostic)
                )
                self.timeout_diagnostic_entries.sort(key=lambda item: item[0])
                del self.timeout_diagnostic_entries[TIMEOUT_DIAGNOSTIC_LIMIT:]
            if result.cf_error_type:
                self.cf_error_types[safe_cf_error_class(result.cf_error_type)] += 1
            if result.cf_error_origin:
                self.cf_error_origins[safe_cf_error_class(result.cf_error_origin)] += 1
        if (
            isinstance(result.response_json, dict)
            and type(result.response_json.get("changed")) is bool
        ):
            self.changed[str(result.response_json["changed"])] += 1
        if result.cf_ray:
            self.cf_rays.add(result.cf_ray)

    def summary(
        self,
        *,
        expected_count: int | None = None,
        submitted_count: int | None = None,
    ) -> dict[str, Any]:
        error_samples = [row for _, row in self.error_sample_entries]
        timeout_diagnostics = [
            row for _, row in self.timeout_diagnostic_entries
        ]
        return {
            "scope": "full_population",
            "requests": self.requests,
            "errors": self.errors,
            "successful_responses": self.requests - self.errors,
            "final_failure_rate_percent": round(
                self.errors * 100 / max(1, self.requests), 4
            ),
            "temporary_overload_responses": self.temporary_overloads,
            "temporary_overload_rate_percent": round(
                self.temporary_overloads * 100 / max(1, self.requests), 4
            ),
            "retry_attempts": self.retry_attempts,
            "total_retries": self.retry_attempts,
            "retry_amplification_percent": round(
                self.retry_attempts * 100 / max(1, self.requests - self.retry_attempts),
                4,
            ),
            "unexpected_statuses": max(0, self.errors - self.temporary_overloads),
            "status_counts": dict(sorted(self.status_counts.items())),
            "error_kinds": dict(sorted(self.error_kinds.items())),
            "cf_error_type_counts": dict(sorted(self.cf_error_types.items())),
            "cf_error_origin_counts": dict(sorted(self.cf_error_origins.items())),
            "changed_counts": dict(sorted(self.changed.items())),
            "latency": metric_stats(self.latencies),
            "timing": self.timing.summary(
                unit="requests",
                expected_count=expected_count,
                submitted_count=submitted_count,
            ),
            "time_to_first_byte": metric_stats(self.first_byte_times),
            "response_bytes": {
                "count": self.response_bytes_count,
                "avg_bytes": round(
                    self.response_bytes_total / self.response_bytes_count, 3
                )
                if self.response_bytes_count
                else None,
                "max_bytes": self.response_bytes_max,
            },
            "transport": {
                "names": dict(sorted(self.transport_names.items())),
                "http_versions": dict(sorted(self.transport_http_versions.items())),
                "connection_reused": self.transport_reused,
                "connection_new": self.transport_new,
                "phase_timings": {
                    key: metric_stats(values)
                    for key, values in sorted(self.transport_phase_values.items())
                },
            },
            "by_route": {
                route: metric_stats(values)
                for route, values in sorted(
                    self.by_route.items(),
                    key=lambda item: (len(item[1]), max(item[1])),
                    reverse=True,
                )
            },
            "cf_ray_count": len(self.cf_rays),
            "error_samples": error_samples,
            "error_sample_total": self.error_sample_total,
            "error_sample_truncated": max(
                0, self.error_sample_total - len(error_samples)
            ),
            "timeout_diagnostics": timeout_diagnostics,
            "timeout_diagnostic_total": self.timeout_diagnostic_total,
            "timeout_diagnostic_truncated": max(
                0, self.timeout_diagnostic_total - len(timeout_diagnostics)
            ),
        }


class _LogicalAccumulator:
    """Streaming reduction for Ready Vote logical actions."""

    __slots__ = (
        "actions",
        "final_successes",
        "total_retries",
        "final_status_counts",
        "changed",
        "end_to_end_latency",
        "accepted_request_latency",
        "timing",
    )

    def __init__(self) -> None:
        self.actions = 0
        self.final_successes = 0
        self.total_retries = 0
        self.final_status_counts: Counter[str] = Counter()
        self.changed: Counter[str] = Counter()
        self.end_to_end_latency = _NumericSamples()
        self.accepted_request_latency = _NumericSamples()
        self.timing = _TimingAccumulator()

    def add(self, result: LogicalRequestResult) -> None:
        self.actions += 1
        self.timing.add(result)
        self.total_retries += result.retry_count
        if not result.attempts:
            return
        final = result.final
        self.final_status_counts[str(final.status)] += 1
        _append_measurement(self.end_to_end_latency, result.elapsed_ms)
        if final.ok is True:
            self.final_successes += 1
            _append_measurement(self.accepted_request_latency, final.elapsed_ms)
        if (
            isinstance(final.response_json, dict)
            and type(final.response_json.get("changed")) is bool
        ):
            self.changed[str(final.response_json["changed"])] += 1

    def summary(
        self,
        *,
        expected_count: int | None = None,
        submitted_count: int | None = None,
    ) -> dict[str, Any]:
        failures = self.actions - self.final_successes
        return {
            "scope": "logical_user_actions",
            "actions": self.actions,
            "final_successes": self.final_successes,
            "final_failures": failures,
            "final_failure_rate_percent": round(
                failures * 100 / max(1, self.actions), 4
            ),
            "total_retries": self.total_retries,
            "retry_amplification_percent": round(
                self.total_retries * 100 / max(1, self.actions), 4
            ),
            "retries_per_action": round(
                self.total_retries / max(1, self.actions), 4
            ),
            "final_status_counts": dict(sorted(self.final_status_counts.items())),
            "changed_counts": dict(sorted(self.changed.items())),
            "end_to_end_latency": metric_stats(self.end_to_end_latency),
            "accepted_request_latency": metric_stats(self.accepted_request_latency),
            "timing": self.timing.summary(
                unit="logical_actions",
                expected_count=expected_count,
                submitted_count=submitted_count,
            ),
        }


def _release_result_payload(result: Any) -> None:
    """Drop response payloads as soon as a streaming consumer has reduced them."""

    if isinstance(result, LogicalRequestResult):
        for attempt in result.attempts:
            attempt.response_json = None
            attempt.transport_timing = None
        result.attempts.clear()
    elif isinstance(result, RequestResult):
        result.response_json = None
        result.transport_timing = None


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


def _canonical_binding_bytes(binding: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(binding),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ExternalLoadError("source binding is not canonical JSON") from exc


def _manifest_source_binding(payload: Mapping[str, Any]) -> tuple[str, str, dict[str, Any] | None, str | None]:
    """Validate the runner/app binding carried by a schema-2 fixture.

    The workflow preflight authenticates receipt metadata and bytes. This
    client-side check binds those exact bytes and identities to the fixture
    manifest and report while preserving the ordinary schema-1 diagnostic
    manifest used by local tooling.
    """

    runner_sha = payload.get("runner_sha")
    app_target_sha = payload.get("app_target_sha")
    binding = payload.get("source_binding")
    binding_digest = payload.get("source_binding_sha256")
    if (
        not isinstance(runner_sha, str)
        or SOURCE_SHA_RE.fullmatch(runner_sha) is None
        or not isinstance(app_target_sha, str)
        or SOURCE_SHA_RE.fullmatch(app_target_sha) is None
    ):
        raise ExternalLoadError("fixture source identity is invalid")
    if (
        os.environ.get("SOURCE_GIT_SHA", "").strip() != runner_sha
        or os.environ.get("APP_TARGET_SHA", "").strip() != app_target_sha
    ):
        raise ExternalLoadError("fixture source identity does not match the workflow")

    encoded = os.environ.get("SOURCE_BINDING_BASE64", "")
    expected_digest = os.environ.get("SOURCE_BINDING_SHA256", "")
    if binding is None:
        if app_target_sha != runner_sha or binding_digest is not None or encoded or expected_digest:
            raise ExternalLoadError("same-source fixture has a substituted source binding")
        return runner_sha, app_target_sha, None, None

    if (
        not isinstance(binding, dict)
        or set(binding) != SOURCE_BINDING_KEYS
        or type(binding.get("schema")) is not int
        or binding.get("schema") != 1
        or binding.get("binding_mode") != "verified-noop"
        or binding.get("runner_sha") != runner_sha
        or binding.get("app_target_sha") != app_target_sha
        or app_target_sha == runner_sha
    ):
        raise ExternalLoadError("verified no-op fixture binding is malformed")
    canonical = _canonical_binding_bytes(binding)
    if (
        not encoded
        or base64.b64encode(canonical).decode("ascii") != encoded
        or not isinstance(binding_digest, str)
        or PROFILE_DIGEST_RE.fullmatch(binding_digest) is None
        or hashlib.sha256(canonical).hexdigest() != binding_digest
        or expected_digest != binding_digest
    ):
        raise ExternalLoadError("verified no-op fixture binding digest is invalid")
    try:
        try:
            from tools.platform_noop_source_binding import validate_active_runtime_tuple
        except ModuleNotFoundError:  # Direct execution from platform/tools.
            from platform_noop_source_binding import validate_active_runtime_tuple

        validate_active_runtime_tuple(
            binding,
            binding.get("baseline_identity"),
            expected_runner_sha=runner_sha,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ExternalLoadError("verified no-op fixture binding failed validation") from exc
    return runner_sha, app_target_sha, binding, binding_digest


def load_manifest(path: Path) -> tuple[dict[str, Any], list[VirtualUser]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExternalLoadError("external load manifest is not valid JSON") from exc
    return load_manifest_bytes(raw)


def load_manifest_bytes(raw: bytes) -> tuple[dict[str, Any], list[VirtualUser]]:
    """Parse a previously bounded manifest snapshot without reopening its path."""

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExternalLoadError("external load manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ExternalLoadError("external load manifest must be an object")
    schema = payload.get("schema")
    if type(schema) is not int or schema not in (MANIFEST_SCHEMA, SOURCE_BOUND_MANIFEST_SCHEMA):
        raise ExternalLoadError("external load manifest schema is unsupported")
    legacy_keys = {
        "schema",
        "purpose",
        "origin",
        "session_cookie_name",
        "csrf_cookie_name",
        "marker",
        "tournaments",
        "users",
    }
    if schema == MANIFEST_SCHEMA:
        if set(payload) not in (legacy_keys, legacy_keys | {"created_at"}):
            raise ExternalLoadError("legacy external load manifest schema is not closed")
    else:
        source_keys = {
            "runner_sha",
            "app_target_sha",
            "source_binding",
            "source_binding_sha256",
        }
        if set(payload) != legacy_keys | source_keys | {"created_at"}:
            raise ExternalLoadError("source-bound external load manifest schema is not closed")
        _manifest_source_binding(payload)
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


def _trace(
    origin: str,
    timeout: float,
    *,
    budget: LoadRuntimeBudget | None = None,
) -> dict[str, Any]:
    if budget is not None:
        timeout = budget.bound_timeout(timeout, "trace", operation="trace_io")
    request = Request(
        f"{origin}/cdn-cgi/trace",
        method="GET",
        headers={"User-Agent": "old-sparky-external-load/1"},
    )
    try:
        # The origin is validated against a fixed HTTPS allowlist before this
        # function is called; no user-controlled URL is accepted here.
        with urlopen(
            request,
            timeout=timeout,
            context=_default_https_context(),
        ) as response:  # nosec B310
            # Read and discard the body.  It contains edge IP, location and
            # other unique request metadata which is useful only transiently
            # while debugging a live request.
            response.read(16_384)
            if budget is not None:
                budget.check("trace", operation="trace_body_complete")
            return {"available": True, "status": safe_status(response.status)}
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        return {"available": False, "status": 0, "error_class": safe_error_class(type(exc).__name__)}


def _response_json_projection(
    *,
    method: str,
    path: str,
    body: bytes,
) -> dict[str, Any] | None:
    """Keep only the small correctness fields needed by the load contract."""

    is_vote = method == "POST" and path.endswith("/deadlock/ready-check/vote")
    is_state = method == "GET" and path.endswith("/deadlock/ready-check")
    is_authenticated_read = _is_authenticated_read_route(method, path)
    if not body or not (is_vote or is_state or is_authenticated_read):
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if is_vote:
        projection: dict[str, Any] = {}
        code = payload.get("code")
        if isinstance(code, str):
            projection["code"] = code[:128]
        retryable = payload.get("retryable")
        if type(retryable) is bool:
            projection["retryable"] = retryable
        retry_after_ms = payload.get("retry_after_ms")
        if isinstance(retry_after_ms, (int, float)) and not isinstance(
            retry_after_ms, bool
        ):
            try:
                retry_after_numeric = float(retry_after_ms)
            except (ValueError, TypeError, OverflowError):
                retry_after_numeric = None
            if retry_after_numeric is not None and math.isfinite(retry_after_numeric):
                projection["retry_after_ms"] = retry_after_ms
        changed = payload.get("changed")
        if type(changed) is bool:
            projection["changed"] = changed
        return projection or None
    if is_authenticated_read:
        if payload.get("code") == "AUTHENTICATED_READ_OVERLOADED":
            return {"code": "AUTHENTICATED_READ_OVERLOADED"}
        return None
    active_round = payload.get("active_round")
    if not isinstance(active_round, dict):
        return None
    ready_count = active_round.get("ready_count")
    if (
        isinstance(ready_count, int)
        and not isinstance(ready_count, bool)
        and ready_count >= 0
    ):
        return {"active_round": {"ready_count": ready_count}}
    return None


def _is_authenticated_read_route(method: str, path: str) -> bool:
    """Identify API/page GET paths that can emit read-admission overloads."""

    return (
        method == "GET"
        and not path.endswith("/deadlock/ready-check")
        and (
            path == "/users/me"
            or path == "/tournaments"
            or path.startswith("/tournaments/")
        )
    )


def _response_requires_json_projection(method: str, path: str) -> bool:
    return (
        method == "POST" and path.endswith("/deadlock/ready-check/vote")
    ) or (method == "GET" and path.endswith("/deadlock/ready-check")) or (
        _is_authenticated_read_route(method, path)
    )


_RESPONSE_READ_EXCEPTIONS = (
    http.client.HTTPException,
    URLError,
    TimeoutError,
    OSError,
)


class _BoundedResponseReadFailure(Exception):
    """Carry only a capped byte count and fixed transport classification."""

    def __init__(self, response_bytes: int, error_kind: str) -> None:
        self.response_bytes = response_bytes
        self.error_kind = error_kind


def _response_read_error_kind(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "TimeoutError"
    if isinstance(exc, URLError):
        return "URLError"
    if isinstance(exc, http.client.HTTPException):
        return "transport_http_response_read"
    return "transport_response_read"


def _bounded_partial_response_bytes(exc: BaseException, prior_bytes: int = 0) -> int:
    partial = getattr(exc, "partial", None)
    partial_bytes = len(partial) if isinstance(partial, (bytes, bytearray)) else 0
    return min(RESPONSE_BODY_LIMIT, prior_bytes + partial_bytes)


def _read_bounded_response(
    response: Any,
    *,
    first_chunk: bytes,
    capture_json: bool,
) -> tuple[int, bytes]:
    """Drain at most the historical body cap without retaining read bodies."""

    response_bytes = min(len(first_chunk), RESPONSE_BODY_LIMIT)
    captured = bytearray()
    if capture_json and first_chunk:
        captured.extend(first_chunk[:RESPONSE_JSON_CAPTURE_LIMIT])
    while response_bytes < RESPONSE_BODY_LIMIT:
        try:
            chunk = response.read(
                min(64 * 1024, RESPONSE_BODY_LIMIT - response_bytes)
            )
        except _RESPONSE_READ_EXCEPTIONS as exc:
            raise _BoundedResponseReadFailure(
                _bounded_partial_response_bytes(exc, response_bytes),
                _response_read_error_kind(exc),
            ) from None
        if not chunk:
            break
        accepted = min(len(chunk), RESPONSE_BODY_LIMIT - response_bytes)
        response_bytes += accepted
        if capture_json and len(captured) < RESPONSE_JSON_CAPTURE_LIMIT:
            captured.extend(
                chunk[: min(accepted, RESPONSE_JSON_CAPTURE_LIMIT - len(captured))]
            )
        if accepted < len(chunk):
            break
    return response_bytes, bytes(captured)


def _read_response_body(
    response: Any,
    *,
    capture_json: bool,
) -> tuple[int, bytes, str | None]:
    """Read one bounded body, returning no partial content after transport failure."""

    try:
        first_chunk = response.read(1)
    except _RESPONSE_READ_EXCEPTIONS as exc:
        return (
            _bounded_partial_response_bytes(exc),
            b"",
            _response_read_error_kind(exc),
        )
    try:
        response_bytes, captured_body = _read_bounded_response(
            response,
            first_chunk=first_chunk,
            capture_json=capture_json,
        )
    except _BoundedResponseReadFailure as exc:
        return exc.response_bytes, b"", exc.error_kind
    return response_bytes, captured_body, None


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
    budget: LoadRuntimeBudget | None = None,
) -> RequestResult:
    if budget is not None:
        timeout = budget.bound_timeout(timeout, phase, operation="http_io")
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
    response_body_error: str | None = None
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
        with urlopen(
            request,
            timeout=timeout,
            context=_default_https_context(),
        ) as response:  # nosec B310
            status = int(response.status)
            cf_ray = response.headers.get("cf-ray", "")[:128] or None
            cf_error_type, cf_error_origin, retry_after = diagnostic_headers(response.headers)
            response_etag = response.headers.get("etag", "")[:512] or None
            time_to_first_byte_ms = (time.monotonic() - started_at) * 1000
            response_bytes, captured_body, response_body_error = _read_response_body(
                response,
                capture_json=_response_requires_json_projection(method, path),
            )
            if response_body_error is not None:
                error_kind = response_body_error
                if error_kind == "TimeoutError" and diagnostic_id:
                    exception_at_utc = datetime.now(UTC).isoformat()
            if budget is not None:
                budget.check(phase, operation="body_complete")
            if response_body_error is None:
                response_json = _response_json_projection(
                    method=method,
                    path=path,
                    body=captured_body,
                )
    except HTTPError as exc:
        status = int(exc.code)
        cf_ray = exc.headers.get("cf-ray", "")[:128] or None
        cf_error_type, cf_error_origin, retry_after = diagnostic_headers(exc.headers)
        time_to_first_byte_ms = (time.monotonic() - started_at) * 1000
        response_bytes, captured_body, response_body_error = _read_response_body(
            exc,
            capture_json=_response_requires_json_projection(method, path),
        )
        if response_body_error is not None:
            error_kind = response_body_error
            if error_kind == "TimeoutError" and diagnostic_id:
                exception_at_utc = datetime.now(UTC).isoformat()
        if budget is not None:
            budget.check(phase, operation="error_body_complete")
        if error_kind is None:
            error_kind = "http_error"
        if response_body_error is None:
            response_json = _response_json_projection(
                method=method,
                path=path,
                body=captured_body,
            )
    except http.client.HTTPException:
        error_kind = "transport_http_protocol"
    except (URLError, TimeoutError, OSError) as exc:
        exception_at_utc = datetime.now(UTC).isoformat()
        error_kind = type(exc).__name__
    finished_at = time.monotonic()
    elapsed_ms = (finished_at - started_at) * 1000
    finished_at_utc = datetime.now(UTC).isoformat()
    ok = status in expected_statuses and response_body_error is None
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
    diagnostic_trace_run_id: str | None = None,
    budget: LoadRuntimeBudget | None = None,
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
            diagnostic_trace_run_id=diagnostic_trace_run_id,
            budget=budget,
        )
    if transport != DEFAULT_CLIENT_TRANSPORT:
        raise ExternalLoadError(f"unsupported page-load transport: {transport}")

    extra_headers = {"Accept": "text/html"}
    if diagnostic_trace_run_id is not None:
        if re.fullmatch(r"[0-9a-f]{32}", diagnostic_trace_run_id) is None:
            raise ExternalLoadError("diagnostic trace run id is invalid")
        extra_headers["x-platform-ssr-trace"] = diagnostic_trace_run_id
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
        extra_headers=extra_headers,
        url_prefix="",
        diagnostic_id=diagnostic_id,
        budget=budget,
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
    diagnostic_trace_run_id: str | None = None,
    budget: LoadRuntimeBudget | None = None,
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
    if diagnostic_trace_run_id is not None:
        if re.fullmatch(r"[0-9a-f]{32}", diagnostic_trace_run_id) is None:
            raise ExternalLoadError("diagnostic trace run id is invalid")
        request_headers["x-platform-ssr-trace"] = diagnostic_trace_run_id
    if budget is not None:
        timeout = budget.bound_timeout(timeout, phase, operation="http_io")
    client = _http11_keepalive_client(origin, timeout)
    if budget is not None:
        client.set_timeout(timeout)
    started_at = time.monotonic()
    started_at_utc = datetime.now(UTC).isoformat()
    try:
        response = client.get(
            path,
            headers=request_headers,
            timeout=timeout,
            deadline=(
                min(
                    budget.scenario_deadline_monotonic,
                    budget.runner_deadline_monotonic,
                )
                if budget is not None and budget.runner_deadline_monotonic is not None
                else budget.scenario_deadline_monotonic
                if budget is not None
                else None
            ),
        )
        if budget is not None:
            budget.check(phase, operation="body_complete")
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
    budget: LoadRuntimeBudget | None = None,
    result_consumer=None,
    progress_callback: Callable[[str, Any | None], None] | None = None,
) -> list[Any]:
    """Run a phase with at most ``concurrency`` live futures.

    When ``result_consumer`` is supplied, each result is reduced and released
    before the next future is submitted.  The no-consumer return-list mode is
    retained for small diagnostic callers and focused compatibility tests.
    """

    if budget is not None:
        budget.check(phase, operation="phase_start")
    offsets = spread_offsets(len(users), spread_seconds)
    phase_started_at = time.monotonic()
    def notify_progress(result: Any | None) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback("http", result)
        except Exception:
            # Diagnostic progress is deliberately outside the load result path.
            return

    if progress_callback is not None:
        notify_progress(None)
    next_progress_at = time.monotonic() + 30.0
    results: list[Any] = []
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="external-load")
    in_flight: dict[Future[Any], int] = {}
    next_index = 0

    def submit_next() -> bool:
        nonlocal next_index
        if next_index >= len(users):
            return False
        user = users[next_index]
        offset = offsets[next_index]
        submission_index = next_index
        next_index += 1
        delay = phase_started_at + offset - time.monotonic()
        if delay > 0:
            if budget is not None:
                budget.sleep(delay, phase, operation="phase_pacing")
            else:
                time.sleep(delay)
        if budget is not None:
            budget.check(phase, operation="request_submit")
        enqueued_at = time.monotonic()
        scheduled_at = phase_started_at + offset

        def invoke(
            *,
            user: VirtualUser = user,
            scheduled_at: float = scheduled_at,
            enqueued_at: float = enqueued_at,
            submission_index: int = submission_index,
        ) -> Any:
            fallback_started_at = time.monotonic()
            result = request_builder(origin, user, phase, timeout)
            fallback_finished_at = time.monotonic()
            result = _annotate_timing(
                result,
                scheduled_at=scheduled_at,
                enqueued_at=enqueued_at,
                fallback_started_at=fallback_started_at,
                fallback_finished_at=fallback_finished_at,
            )
            if isinstance(result, (RequestResult, LogicalRequestResult)):
                result.submission_index = submission_index
                if isinstance(result, LogicalRequestResult):
                    for attempt in result.attempts:
                        attempt.submission_index = submission_index
            return result

        future = executor.submit(invoke)
        in_flight[future] = submission_index
        return True

    try:
        while len(in_flight) < concurrency and submit_next():
            pass
        while in_flight:
            if budget is None:
                done, _ = wait(
                    in_flight,
                    timeout=(
                        max(0.0, next_progress_at - time.monotonic())
                        if progress_callback is not None
                        else None
                    ),
                    return_when=FIRST_COMPLETED,
                )
            else:
                budget.check(phase, operation="future_wait")
                remaining = budget.remaining_seconds()
                runner_remaining = budget.remaining_runner_seconds()
                if runner_remaining is not None:
                    remaining = min(remaining, runner_remaining)
                if progress_callback is not None:
                    remaining = min(remaining, max(0.0, next_progress_at - time.monotonic()))
                done, _ = wait(
                    in_flight,
                    timeout=max(0.0, remaining),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    if progress_callback is not None and time.monotonic() >= next_progress_at:
                        notify_progress(None)
                        next_progress_at = time.monotonic() + 30.0
                    budget.check(phase, operation="future_wait")
                    continue
            if not done:
                if progress_callback is not None and time.monotonic() >= next_progress_at:
                    notify_progress(None)
                    next_progress_at = time.monotonic() + 30.0
                continue
            if budget is not None:
                budget.check(phase, operation="future_complete")
            for future in sorted(done, key=in_flight.__getitem__):
                in_flight.pop(future, None)
                result = future.result()
                # Progress needs the completed HTTP statuses before a streaming
                # consumer releases response payloads and retry attempts.
                notify_progress(result)
                if result_consumer is None:
                    results.append(result)
                else:
                    try:
                        result_consumer(result)
                    finally:
                        _release_result_payload(result)
                # Refill one slot immediately after reduction.  The pacing
                # schedule remains deterministic while live work stays O(c).
                submit_next()
    except BaseException:
        for future in in_flight:
            future.cancel()
        # A thread blocked in DNS/socket I/O cannot be joined safely here.  The
        # process supervisor owns the kill boundary; do not let executor
        # context-manager shutdown consume the remaining cleanup window.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    if budget is not None:
        budget.check(phase, operation="phase_complete")
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
    budget: LoadRuntimeBudget | None = None,
    result_consumer=None,
    progress_callback: Callable[[str, Any | None], None] | None = None,
) -> tuple[list[Any], float]:
    """Run a paced phase and return its submission window separately from drain time."""

    offsets = spread_offsets(len(users), duration_seconds)
    phase_started_at = time.monotonic()
    def notify_progress(result: Any | None) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback("http", result)
        except Exception:
            # Diagnostic progress is deliberately outside the load result path.
            return

    if progress_callback is not None:
        notify_progress(None)
    next_progress_at = time.monotonic() + 30.0
    first_submission_at: float | None = None
    last_submission_at: float | None = None
    results: list[Any] = []
    in_flight: dict[Future[Any], int] = {}
    next_index = 0
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="external-load")

    def submit_next() -> bool:
        nonlocal next_index, first_submission_at, last_submission_at
        if next_index >= len(users):
            return False
        user = users[next_index]
        offset = offsets[next_index]
        submission_index = next_index
        next_index += 1
        delay = phase_started_at + offset - time.monotonic()
        if delay > 0:
            if budget is not None:
                budget.sleep(delay, phase, operation="phase_pacing")
            else:
                time.sleep(delay)
        if budget is not None:
            budget.check(phase, operation="request_submit")
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
            submission_index: int = submission_index,
        ) -> Any:
            fallback_started_at = time.monotonic()
            result = request_builder(origin, user, phase, timeout)
            fallback_finished_at = time.monotonic()
            result = _annotate_timing(
                result,
                scheduled_at=scheduled_at,
                enqueued_at=enqueued_at,
                fallback_started_at=fallback_started_at,
                fallback_finished_at=fallback_finished_at,
            )
            if isinstance(result, (RequestResult, LogicalRequestResult)):
                result.submission_index = submission_index
                if isinstance(result, LogicalRequestResult):
                    for attempt in result.attempts:
                        attempt.submission_index = submission_index
            return result

        future = executor.submit(invoke)
        in_flight[future] = submission_index
        return True

    try:
        while len(in_flight) < concurrency and submit_next():
            pass
        while in_flight:
            if budget is None:
                done, _ = wait(
                    in_flight,
                    timeout=(
                        max(0.0, next_progress_at - time.monotonic())
                        if progress_callback is not None
                        else None
                    ),
                    return_when=FIRST_COMPLETED,
                )
            else:
                budget.check(phase, operation="future_wait")
                remaining = budget.remaining_seconds()
                runner_remaining = budget.remaining_runner_seconds()
                if runner_remaining is not None:
                    remaining = min(remaining, runner_remaining)
                if progress_callback is not None:
                    remaining = min(remaining, max(0.0, next_progress_at - time.monotonic()))
                done, _ = wait(
                    in_flight,
                    timeout=max(0.0, remaining),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    if progress_callback is not None and time.monotonic() >= next_progress_at:
                        notify_progress(None)
                        next_progress_at = time.monotonic() + 30.0
                    budget.check(phase, operation="future_wait")
                    continue
            if not done:
                if progress_callback is not None and time.monotonic() >= next_progress_at:
                    notify_progress(None)
                    next_progress_at = time.monotonic() + 30.0
                continue
            if budget is not None:
                budget.check(phase, operation="future_complete")
            for future in sorted(done, key=in_flight.__getitem__):
                in_flight.pop(future, None)
                result = future.result()
                # Progress needs the completed HTTP statuses before a streaming
                # consumer releases response payloads and retry attempts.
                notify_progress(result)
                if result_consumer is None:
                    results.append(result)
                else:
                    try:
                        result_consumer(result)
                    finally:
                        _release_result_payload(result)
                submit_next()
    except BaseException:
        for future in in_flight:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
    if budget is not None:
        budget.check(phase, operation="phase_complete")
    if first_submission_at is None or last_submission_at is None:
        submission_window_seconds = 0.001
    elif len(users) == 1:
        submission_window_seconds = max(0.001, duration_seconds)
    else:
        submission_window_seconds = max(0.001, last_submission_at - first_submission_at)
    return results, submission_window_seconds


def summarize_results(
    results: Iterable[RequestResult],
    *,
    expected_count: int | None = None,
    submitted_count: int | None = None,
) -> dict[str, Any]:
    accumulator = _ResultAccumulator()
    for result in results:
        accumulator.add(result)
    return accumulator.summary(
        expected_count=expected_count,
        submitted_count=submitted_count,
    )


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
    budget: LoadRuntimeBudget | None = None,
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
        budget=budget,
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
    budget: LoadRuntimeBudget | None = None,
) -> LogicalRequestResult:
    """Issue one request plus only the profile's explicit overload retries."""

    started_at = time.monotonic()
    attempts: list[RequestResult] = []
    max_retries = 2 if retry_policy is None else int(retry_policy["max_retries"])
    for retry_index in range(max_retries + 1):
        if budget is not None:
            budget.check(phase, operation="retry_start")
        result = _ready_vote_request(
            origin,
            user,
            phase,
            timeout,
            session_cookie_name=session_cookie_name,
            csrf_cookie_name=csrf_cookie_name,
            attempt_number=retry_index + 1,
            budget=budget,
        )
        attempts.append(result)
        if not _ready_vote_overload(result) or retry_index >= max_retries:
            break
        delay = _ready_vote_retry_delay_ms(result, retry_index, retry_policy) / 1000
        if budget is not None:
            budget.sleep(delay, phase, operation="retry_backoff")
        else:
            time.sleep(delay)
    finished_at = time.monotonic()
    return LogicalRequestResult(
        attempts=attempts,
        elapsed_ms=(finished_at - started_at) * 1000,
        user_id=user.user_id,
        started_at_monotonic=started_at,
        finished_at_monotonic=finished_at,
        user_observed_elapsed_ms=(finished_at - started_at) * 1000,
    )


def summarize_logical_results(
    results: Iterable[LogicalRequestResult],
    *,
    expected_count: int | None = None,
    submitted_count: int | None = None,
) -> dict[str, Any]:
    accumulator = _LogicalAccumulator()
    for result in results:
        accumulator.add(result)
    return accumulator.summary(
        expected_count=expected_count,
        submitted_count=submitted_count,
    )


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
    runtime_budget: LoadRuntimeBudget | None = None,
    progress_callback: Callable[[str, Any | None], None] | None = None,
) -> dict[str, Any]:
    def run_progress_phase(*args: Any, **kwargs: Any) -> Any:
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        return run_phase(*args, **kwargs)

    def run_progress_rate_phase(*args: Any, **kwargs: Any) -> Any:
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        return run_rate_phase(*args, **kwargs)

    if runtime_budget is not None:
        runtime_budget.check("preflight", operation="run_start")
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
        and isinstance(binding.get("app_target_sha"), str)
        and SOURCE_SHA_RE.fullmatch(binding["app_target_sha"]) is not None
        and binding["app_target_sha"]
        == (os.environ.get("APP_TARGET_SHA", "").strip() or binding["source_git_sha"])
        and binding.get("app_target_sha") == manifest.get("app_target_sha", binding.get("app_target_sha"))
        and binding.get("source_binding") == manifest.get("source_binding")
        and binding.get("source_binding_sha256") == manifest.get("source_binding_sha256")
        and isinstance(binding.get("external_run_id"), str)
        and RUN_ID_RE.fullmatch(binding["external_run_id"]) is not None
        and binding["external_run_id"] == os.environ.get("GITHUB_RUN_ID", "").strip()
    )
    origin = str(manifest["origin"]).rstrip("/")
    session_cookie_name = str(manifest["session_cookie_name"])
    csrf_cookie_name = str(manifest["csrf_cookie_name"])
    started_at = datetime.now(UTC)
    if runtime_budget is None:
        trace = _trace(origin, timeout=min(timeout, 10.0))
    else:
        trace = _trace(
            origin,
            timeout=min(timeout, 10.0),
            budget=runtime_budget,
        )
    if runtime_budget is not None:
        runtime_budget.check("trace", operation="trace_complete")
    phase_results: dict[str, dict[str, Any]] = {}
    overall_raw_acc = _ResultAccumulator()

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
            if runtime_budget is not None:
                kwargs["budget"] = runtime_budget
            return _ready_vote_action(
                origin_value,
                user,
                phase,
                request_timeout,
                **kwargs,
            )

        primary_started_at = time.monotonic()
        primary_logical_acc = _LogicalAccumulator()
        primary_raw_acc = _ResultAccumulator()
        combined_logical_acc = _LogicalAccumulator()
        action_raw_acc = _ResultAccumulator()
        duplicate_logical_acc = _LogicalAccumulator()
        duplicate_raw_acc = _ResultAccumulator()
        successful_primary_ids: set[str] = set()
        successful_primary_by_slug: Counter[str] = Counter()
        user_by_id = {user.user_id: user for user in users}

        def make_vote_consumer(
            logical_acc: _LogicalAccumulator,
            raw_acc: _ResultAccumulator,
            *,
            mark_primary_success: bool = False,
        ):
            def consume(action: LogicalRequestResult) -> None:
                logical_acc.add(action)
                combined_logical_acc.add(action)
                if mark_primary_success and action.attempts and action.final.ok is True:
                    if action.user_id:
                        successful_primary_ids.add(action.user_id)
                        user = user_by_id.get(action.user_id)
                        if user is not None:
                            successful_primary_by_slug[user.tournament_slug] += 1
                for attempt in action.attempts:
                    raw_acc.add(attempt)
                    action_raw_acc.add(attempt)
                    overall_raw_acc.add(attempt)

            return consume

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
                phase_logical_acc = _LogicalAccumulator()
                phase_raw_acc = _ResultAccumulator()

                phase_consumer = make_vote_consumer(
                    phase_logical_acc,
                    phase_raw_acc,
                    mark_primary_success=True,
                )

                def consume_primary_phase(action: LogicalRequestResult) -> None:
                    phase_consumer(action)
                    primary_logical_acc.add(action)
                    for attempt in action.attempts:
                        primary_raw_acc.add(attempt)

                _, phase_submission_window = run_progress_rate_phase(
                    origin,
                    phase_users,
                    phase=f"write_external_vote_{phase_name}",
                    duration_seconds=duration_seconds,
                    concurrency=concurrency,
                    timeout=timeout,
                    request_builder=vote_builder,
                    budget=runtime_budget,
                    result_consumer=consume_primary_phase,
                )
                offered_window_seconds += phase_submission_window
                phase_wall_seconds = max(0.001, time.monotonic() - phase_started)
                phase_finished_at_utc = datetime.now(UTC)
                phase_logical = phase_logical_acc.summary(
                    expected_count=action_count,
                    submitted_count=phase_logical_acc.actions,
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
                phase_raw = phase_raw_acc.summary()
                _add_measured_goodput(phase_raw, phase_wall_seconds)
                phase_raw["attempts_per_second"] = round(
                    float(phase_raw.get("requests") or 0) / phase_submission_window,
                    3,
                )
                submitted_actions = phase_logical_acc.actions
                planned_phases[phase_name] = {
                    "configured_actions": action_count,
                    "submitted_actions": submitted_actions,
                    "missing_actions": max(0, action_count - submitted_actions),
                    "complete": (
                        submitted_actions == action_count
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
            primary_users = users[:cursor]
            phase_results["ramp"] = {
                "phases": planned_phases,
                "logical_actions": cursor,
                "offered_window_seconds": round(offered_window_seconds, 3),
            }
        else:
            run_progress_phase(
                origin,
                users,
                phase="write_external_vote",
                spread_seconds=spread_seconds,
                concurrency=concurrency,
                timeout=timeout,
                request_builder=vote_builder,
                budget=runtime_budget,
                result_consumer=make_vote_consumer(
                    primary_logical_acc,
                    primary_raw_acc,
                    mark_primary_success=True,
                ),
            )
        primary_wall_seconds = max(0.001, time.monotonic() - primary_started_at)
        primary_logical = primary_logical_acc.summary(
            expected_count=(
                sum(int(phase.get("logical_actions") or 0) for phase in phase_plan)
                if phase_plan
                else len(users)
            ),
            submitted_count=primary_logical_acc.actions,
        )
        primary_logical["wall_seconds"] = round(primary_wall_seconds, 6)
        primary_logical["successful_goodput_actions_per_second"] = round(
            float(primary_logical.get("final_successes") or 0) / primary_wall_seconds,
            3,
        )
        if phase_plan:
            primary_logical["offered_logical_actions_per_second"] = round(
                primary_logical_acc.actions / max(0.001, offered_window_seconds),
                3,
            )
        primary_raw = primary_raw_acc.summary()
        _add_measured_goodput(primary_raw, primary_wall_seconds)
        phase_results["primary"] = {"raw_http": primary_raw, "logical": primary_logical}

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
        run_progress_phase(
            origin,
            duplicate_users,
            phase="write_external_vote_duplicate",
            spread_seconds=min(spread_seconds, 5.0),
            concurrency=concurrency,
            timeout=timeout,
            request_builder=vote_builder,
            budget=runtime_budget,
            result_consumer=make_vote_consumer(duplicate_logical_acc, duplicate_raw_acc),
        )
        duplicate_logical = duplicate_logical_acc.summary(
            expected_count=duplicate_count,
            submitted_count=duplicate_logical_acc.actions,
        )
        duplicate_wall_seconds = max(0.001, time.monotonic() - duplicate_started_at)
        duplicate_logical["wall_seconds"] = round(duplicate_wall_seconds, 6)
        duplicate_logical["successful_goodput_actions_per_second"] = round(
            float(duplicate_logical.get("final_successes") or 0)
            / duplicate_wall_seconds,
            3,
        )
        duplicate_raw = duplicate_raw_acc.summary()
        _add_measured_goodput(duplicate_raw, duplicate_wall_seconds)
        duplicate_raw["configured_logical_actions"] = duplicate_count
        duplicate_raw["submitted_logical_actions"] = duplicate_logical_acc.actions
        duplicate_raw["missing_logical_actions"] = max(
            0, duplicate_count - duplicate_logical_acc.actions
        )
        duplicate_phase_complete = (
            len(duplicate_users) == duplicate_count
            and timing_summary_is_complete(duplicate_logical.get("timing"))
        )
        phase_results["duplicate"] = {
            "configured_actions": duplicate_count,
            "candidate_actions": len(duplicate_candidates),
            "submitted_actions": duplicate_logical_acc.actions,
            "completed_actions": int(
                (duplicate_logical.get("timing") or {}).get("completed_count") or 0
            ),
            "missing_actions": max(0, duplicate_count - duplicate_logical_acc.actions),
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

        # State reads are planned per manifest tournament, not merely for the
        # subset that happened to receive a primary action in a capacity
        # phase.  Keep untouched tournaments in the evidence with an expected
        # ready count of zero so the state-read population remains exactly
        # bound to the selected fixture plan.
        users_by_slug: dict[str, VirtualUser] = {}
        expected_by_slug: Counter[str] = Counter()
        for user in users:
            users_by_slug.setdefault(user.tournament_slug, user)
            expected_by_slug[user.tournament_slug] = successful_primary_by_slug.get(
                user.tournament_slug,
                0,
            )
        state_acc = _ResultAccumulator()
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
                **({"budget": runtime_budget} if runtime_budget is not None else {}),
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
            state_acc.add(result)
            overall_raw_acc.add(result)
            _release_result_payload(result)
        state_summary = state_acc.summary(
            expected_count=len(users_by_slug),
            submitted_count=state_acc.requests,
        )
        state_summary.update(
            {
                "configured_reads": len(users_by_slug),
                "submitted_reads": state_acc.requests,
                "completed_reads": state_acc.requests,
                "missing_reads": max(0, len(users_by_slug) - state_acc.requests),
                "complete": (
                    state_acc.requests == len(users_by_slug)
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
                **({"budget": runtime_budget} if runtime_budget is not None else {}),
                **request_kwargs,
            )

        page_started_at = time.monotonic()
        page_acc = _ResultAccumulator()

        def consume_page(result: RequestResult) -> None:
            page_acc.add(result)
            overall_raw_acc.add(result)

        run_progress_phase(
            origin,
            users,
            phase="authenticated_page_load",
            spread_seconds=spread_seconds,
            concurrency=concurrency,
            timeout=timeout,
            request_builder=page_builder,
            budget=runtime_budget,
            result_consumer=consume_page,
        )
        page_summary = page_acc.summary(
            expected_count=len(users),
            submitted_count=page_acc.requests,
        )
        page_wall_seconds = max(0.001, time.monotonic() - page_started_at)
        page_summary["wall_seconds"] = round(page_wall_seconds, 6)
        page_summary["successful_goodput_actions_per_second"] = round(
            float(page_summary.get("successful_responses") or 0) / page_wall_seconds,
            3,
        )
        phase_results["authenticated_page_load"] = page_summary
        page_summary = phase_results["authenticated_page_load"]
        contract_ok = (
            page_summary["requests"] == len(users)
            and page_summary["errors"] == 0
            and page_summary["unexpected_statuses"] == 0
        )
    else:
        user_indexes = {user.user_id: index for index, user in enumerate(users)}
        refresh_users = [
            user
            for user in users
            if user_indexes[user.user_id] % 10 < 5
        ][:manual_refresh_count]
        refresh_user_ids = {user.user_id for user in refresh_users}
        initial_workspace_etags: dict[str, str] = {}
        read_acc = _ResultAccumulator()

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
                **({"budget": runtime_budget} if runtime_budget is not None else {}),
            )
            if user.user_id in refresh_user_ids and result.response_etag:
                initial_workspace_etags[user.user_id] = result.response_etag
            return result

        read_started_at = time.monotonic()
        ramp_stages: dict[str, dict[str, Any]] = {}
        for stage_concurrency in read_concurrency_stages:
            stage_started_at = time.monotonic()
            stage_acc = _ResultAccumulator()

            def consume_read(result: RequestResult) -> None:
                stage_acc.add(result)
                read_acc.add(result)
                overall_raw_acc.add(result)

            run_progress_phase(
                origin,
                users,
                phase=f"scale_external_read_mix_c{stage_concurrency}",
                spread_seconds=spread_seconds,
                concurrency=stage_concurrency,
                timeout=timeout,
                request_builder=read_builder,
                budget=runtime_budget,
                result_consumer=consume_read,
            )
            stage_summary = stage_acc.summary(
                expected_count=len(users),
                submitted_count=stage_acc.requests,
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
        read_summary = read_acc.summary(
            expected_count=len(users) * len(read_concurrency_stages),
            submitted_count=read_acc.requests,
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
        refresh_acc = _ResultAccumulator()
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
                    **({"budget": runtime_budget} if runtime_budget is not None else {}),
                )

            refresh_started_at = time.monotonic()

            def consume_refresh(result: RequestResult) -> None:
                refresh_acc.add(result)
                overall_raw_acc.add(result)

            run_progress_phase(
                origin,
                refresh_users,
                phase="manual_workspace_refresh",
                spread_seconds=spread_seconds,
                concurrency=concurrency,
                timeout=timeout,
                request_builder=refresh_builder,
                budget=runtime_budget,
                result_consumer=consume_refresh,
            )
            refresh_summary = refresh_acc.summary(
                expected_count=manual_refresh_count,
                submitted_count=refresh_acc.requests,
            )
            refresh_wall_seconds = max(0.001, time.monotonic() - refresh_started_at)
            refresh_summary["wall_seconds"] = round(refresh_wall_seconds, 6)
            refresh_summary["successful_goodput_actions_per_second"] = round(
                float(refresh_summary.get("successful_responses") or 0)
                / refresh_wall_seconds,
                3,
            )
            phase_results["manual_refresh"] = refresh_summary
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
                    and set(phase_results["manual_refresh"]["status_counts"]).issubset(
                        {"200", "304"}
                    )
                )
            )
        )

    partial_work = _has_partial_timing(phase_results)
    if partial_work:
        # Never turn a partial result set into a green experiment merely
        # because the rows that did complete met their latency thresholds.
        contract_ok = False
    overall = overall_raw_acc.summary()
    if mode == "ready-vote":
        primary_logical_result = phase_results.get("primary", {}).get("logical", {})
        duplicate_logical_result = phase_results.get("duplicate", {}).get("logical", {})
        logical_summary = combined_logical_acc.summary(
            expected_count=(
                int(
                    (primary_logical_result.get("timing") or {}).get(
                        "expected_count"
                    )
                    or primary_logical_acc.actions
                )
                + duplicate_count
            ),
            submitted_count=combined_logical_acc.actions,
        )
        logical_summary["primary_actions"] = primary_logical_acc.actions
        logical_summary["duplicate_actions"] = duplicate_logical_acc.actions
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
        raw_http_summary = action_raw_acc.summary()
        raw_http_summary["configured_duplicate_actions"] = duplicate_count
        raw_http_summary["submitted_duplicate_actions"] = duplicate_logical_acc.actions
        raw_http_summary["missing_duplicate_actions"] = max(
            0, duplicate_count - duplicate_logical_acc.actions
        )
    else:
        logical_summary = overall
        raw_http_summary = overall
        if mode == "read-mix":
            logical_summary["primary_actions"] = read_acc.requests
            logical_summary["manual_refresh_actions"] = refresh_acc.requests
        elif mode == "page-load":
            logical_summary["primary_actions"] = page_acc.requests
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
        raw_http_summary["state_read_requests"] = state_acc.requests
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
        "runner_sha": binding.get("source_git_sha") if binding_is_authoritative else None,
        "app_target_sha": binding.get("app_target_sha") if binding_is_authoritative else None,
        "source_binding": manifest.get("source_binding"),
        "source_binding_sha256": manifest.get("source_binding_sha256"),
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
            "request_count": overall.get("requests", 0)
            if timeout_diagnostics_run_id
            else 0,
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
