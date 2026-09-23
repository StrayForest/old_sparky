#!/usr/bin/env python3
"""Closed-schema helpers for load and live-QA evidence.

The load/QA producers receive paths, headers and correlation material because
they need them while making a request.  This module is the boundary between
that transient state and anything written to a report, log or CI artifact.
Unknown values deliberately collapse to ``other``; this is preferable to
trying to redact arbitrary input after it has already become evidence.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit


SAFE_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
SAFE_ROUTE_CLASSES = frozenset(
    {
        "auth_bootstrap",
        "auth_csrf",
        "auth_session",
        "auth_logout",
        "users_me",
        "profiles_me",
        "profile_deadlock",
        "profile_dream_slots",
        "tournaments_collection",
        "tournament_detail",
        "tournament_workspace",
        "tournament_bracket",
        "tournament_participants",
        "ready_check_state",
        "ready_vote",
        "ready_lock",
        "auto_assignment",
        "tournament_page",
        "other",
    }
)
SAFE_ERROR_CLASSES = frozenset(
    {
        "none",
        "timeout",
        "transport",
        "http_error",
        "unexpected_status",
        "auth",
        "rate_limited",
        "validation",
        "server_error",
        "other",
    }
)
SAFE_CF_ERROR_CLASSES = frozenset(
    {"none", "cf_520", "cf_521", "cf_522", "cf_523", "cf_524", "origin", "other"}
)
SAFE_BACKENDS = frozenset({"api", "web", "worker", "qa", "observer", "postgres", "other"})
SAFE_PROFILE_SIGNAL_REASONS = frozenset(
    {
        "uid_unavailable",
        "invalid_pid",
        "identity_mismatch",
        "pidfd_unavailable",
        "pidfd_open_failed",
        "pidfd_send_failed",
    }
)
SAFE_WAIT_STATES = frozenset(
    {"active", "idle", "lock", "io", "lwlock", "client", "ipc", "timeout", "other"}
)
SAFE_PHASES = frozenset(
    {
        "auth",
        "register",
        "tournament",
        "participants",
        "ready",
        "assignment",
        "bracket",
        "workspace",
        "read_mix",
        "primary",
        "duplicate",
        "state",
        "ramp",
        "capacity_ramp",
        "manual_refresh",
        "authenticated_page_load",
        "write_burst",
        "page_load",
        "other",
    }
)
SAFE_ROUTE_TEMPLATES = {
    "auth_bootstrap": "/auth/bootstrap",
    "auth_csrf": "/auth/csrf",
    "auth_session": "/auth/session",
    "auth_logout": "/auth/logout",
    "users_me": "/users/me",
    "profiles_me": "/profiles/me",
    "profile_deadlock": "/profiles/me/deadlock",
    "profile_dream_slots": "/profiles/me/deadlock/dream-slots",
    "tournaments_collection": "/tournaments",
    "tournament_detail": "/tournaments/{slug}",
    "tournament_workspace": "/tournaments/{slug}/workspace",
    "tournament_bracket": "/tournaments/{slug}/bracket",
    "tournament_participants": "/tournaments/{slug}/participants",
    "ready_check_state": "/tournaments/{slug}/deadlock/ready-check",
    "ready_vote": "/tournaments/{slug}/deadlock/ready-check/vote",
    "ready_lock": "/tournaments/{slug}/deadlock/ready-check/lock",
    "auto_assignment": "/tournaments/{slug}/deadlock/assignment",
    "tournament_page": "/tournaments/{slug}",
    "other": "/other",
}
SAFE_STAGE_NAMES = frozenset(
    {
        "http_request_start",
        "auth_bootstrap",
        "auth_bootstrap_fetch",
        "auth_session",
        "tournament_detail",
        "tournament_detail_data_ready",
        "tournament_workspace",
        "proxy_start",
        "proxy_to_root_layout_start",
        "request_to_proxy",
        "request_start",
        "root_layout_start",
        "root_layout",
        "ready_check",
        "ready_vote",
        "assignment",
        "response_stream_start",
        "first_body_write_attempt",
        "response_finish",
        "response_close",
        "response_error",
        "response",
        "other",
    }
)

SAFE_OUTCOMES = frozenset({"ok", "error", "timeout", "shed", "other"})

_DYNAMIC_SEGMENT = r"[^/]+"
_ROUTE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^auth/bootstrap$"), "auth_bootstrap"),
    (re.compile(r"^auth/csrf$"), "auth_csrf"),
    (re.compile(r"^auth/session$"), "auth_session"),
    (re.compile(r"^auth/logout$"), "auth_logout"),
    (re.compile(r"^users/me$"), "users_me"),
    (re.compile(r"^profiles/me$"), "profiles_me"),
    (re.compile(r"^profiles/me/deadlock$"), "profile_deadlock"),
    (re.compile(r"^profiles/me/deadlock/dream-slots$"), "profile_dream_slots"),
    (re.compile(r"^tournaments$"), "tournaments_collection"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}$"), "tournament_detail"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/workspace$"), "tournament_workspace"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/bracket(?:/[^/]*)?$"), "tournament_bracket"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/participants$"), "tournament_participants"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/(?:join|invites?)$"), "tournament_participants"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/status$"), "tournament_detail"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/deadlock/ready-check$"), "ready_check_state"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/deadlock/ready-check/(?:start|close|captain-round(?:/start)?)$"), "ready_check_state"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/deadlock/ready-check/vote$"), "ready_vote"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/deadlock/ready-check/lock$"), "ready_lock"),
    (re.compile(rf"^tournaments/{_DYNAMIC_SEGMENT}/deadlock/assignment(?:/[^/]*)?$"), "auto_assignment"),
)


def _path_without_query(value: Any) -> str:
    """Return a path for matching only; never return it as evidence."""

    if not isinstance(value, str):
        return ""
    candidate = value.split("?", 1)[0].split("#", 1)[0]
    if "://" in candidate:
        try:
            candidate = urlsplit(candidate).path
        except ValueError:
            return ""
    return candidate


def safe_route_class(path: Any, *, page: bool = False) -> str:
    """Map a request path to a closed route class, with no path echo."""

    candidate = _path_without_query(path).strip()
    if page:
        if re.fullmatch(r"/tournaments/[^/]+", candidate):
            return "tournament_page"
        if re.fullmatch(r"/tournaments/[^/]+/workspace", candidate):
            return "tournament_workspace"
        return "other"
    candidate = candidate.lstrip("/")
    if candidate.startswith("api/v1/"):
        candidate = candidate[7:]
    elif candidate == "api/v1":
        candidate = ""
    for pattern, route_class in _ROUTE_RULES:
        if pattern.fullmatch(candidate):
            return route_class
    # The browser page route is accepted in server access logs as well.
    if re.fullmatch(r"tournaments/[^/]+", candidate):
        return "tournament_page"
    if re.fullmatch(r"tournaments/[^/]+/workspace", candidate):
        return "tournament_workspace"
    return "other"


def safe_method(value: Any) -> str:
    method = str(value or "").upper()
    return method if method in SAFE_METHODS else "OTHER"


def safe_route_key(method: Any, path: Any, *, page: bool = False) -> str:
    return f"{safe_method(method)} {safe_route_class(path, page=page)}"


def safe_route_template(path: Any, *, page: bool = False) -> str:
    """Return a fixed route template without a user-controlled segment."""

    return SAFE_ROUTE_TEMPLATES[safe_route_class(path, page=page)]


def safe_status(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        status = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return status if 0 <= status <= 599 else 0


def safe_phase(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in SAFE_PHASES:
        return raw
    if re.fullmatch(r"rate_[0-9]{1,4}", raw):
        return raw if int(raw.split("_", 1)[1]) <= 10_000 else "other"
    if raw in {
        "normal_before",
        "burst",
        "normal_after",
        "lifecycle_capacity_low",
        "lifecycle_capacity_mid",
        "lifecycle_capacity_high",
    }:
        return raw
    for prefix, phase in (
        ("write", "write_burst"),
        ("ready", "ready"),
        ("assign", "assignment"),
        ("bracket", "bracket"),
        ("workspace", "workspace"),
        ("auth", "auth"),
        ("register", "register"),
        ("participant", "participants"),
        ("read", "read_mix"),
        ("page", "page_load"),
        ("manual", "page_load"),
    ):
        if raw.startswith(prefix):
            return phase
    return "other"


def safe_stage(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return raw if raw in SAFE_STAGE_NAMES else "other"


def safe_outcome(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return raw if raw in SAFE_OUTCOMES else "other"


def safe_error_class(value: Any, *, status: Any = 0) -> str:
    raw = str(value or "").strip().lower()
    numeric_status = safe_status(status)
    if numeric_status == 408 or numeric_status == 504 or "timeout" in raw or "timedout" in raw:
        return "timeout"
    if numeric_status == 429 or "rate" in raw or "overload" in raw or "busy" in raw:
        return "rate_limited"
    if numeric_status in {401, 403} or "auth" in raw or "csrf" in raw:
        return "auth"
    if 400 <= numeric_status <= 499:
        return "validation" if numeric_status in {400, 404, 409, 422} else "http_error"
    if numeric_status >= 500:
        return "server_error"
    if "transport" in raw or "connection" in raw or "urlerror" in raw:
        return "transport"
    if "http" in raw or "status" in raw:
        return "http_error"
    if raw in {"", "none", "ok", "success"}:
        return "none"
    return "other"


def sanitize_public_load_report(value: Any, artifact_type: str) -> Any:
    """Compatibility wrapper requiring an explicit closed artifact schema."""

    return project_public_artifact(artifact_type, value)


def safe_cf_error_class(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw or raw in {"-", "none", "ok"}:
        return "none"
    if raw in {"520", "cf_520"}:
        return "cf_520"
    if raw in {"521", "cf_521"}:
        return "cf_521"
    if raw in {"522", "cf_522"}:
        return "cf_522"
    if raw in {"523", "cf_523"}:
        return "cf_523"
    if raw in {"524", "cf_524"}:
        return "cf_524"
    if raw in {"origin", "origin_error", "origin_failure", "connection_failure", "origin_timeout"}:
        return "origin"
    return "other"


def safe_backend(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"api", "deadlock-api", "oldsparky-api", "platform-api"} or "gunicorn" in raw:
        return "api"
    if raw in {"web", "deadlock-web", "platform-web", "next-server"} or "next" in raw:
        return "web"
    if raw in {
        "worker",
        "deadlock-worker",
        "oldsparky-worker",
        "celery",
        "platform-worker",
    }:
        return "worker"
    if raw in {"qa", "production-qa", "load"}:
        return "qa"
    if raw in {"observer", "external-load-observer"}:
        return "observer"
    if raw in {"postgres", "postgresql", "database", "db"}:
        return "postgres"
    return "other"


def safe_wait_state(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("_", "")
    if not raw or raw in {"-", "none"}:
        return "other"
    if raw == "active":
        return "active"
    if raw == "idle" or raw.startswith("idleintransaction"):
        return "idle"
    for token, normalized in (
        ("lock", "lock"),
        ("io", "io"),
        ("lwlock", "lwlock"),
        ("client", "client"),
        ("ipc", "ipc"),
        ("timeout", "timeout"),
    ):
        if token in raw:
            return normalized
    return "other"


def safe_query_category(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    allowed = {
        "auth": "auth",
        "bootstrap": "auth",
        "read": "read",
        "write": "write",
        "ready_vote": "ready_vote",
        "assignment": "assignment",
        "other": "other",
    }
    return allowed.get(raw, "other")


def finite_number(value: Any, *, maximum: float = 1_000_000_000_000.0) -> float | None:
    # Public numeric fields are accepted only when the producer supplied a
    # JSON number. Coercing arbitrary strings would make the projector a
    # permissive parser and could preserve attacker-owned data under an
    # allowlisted key.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or number > maximum:
        return None
    return number


def safe_int(value: Any, *, maximum: int = 1_000_000_000_000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= maximum else None


def _safe_numeric_map_key(value: Any, *, maximum: int) -> int | None:
    """Parse a bounded JSON object key without treating it as a metric."""

    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= maximum else None


PUBLIC_ARTIFACT_TYPES = frozenset(
    {"external_load", "server_observability", "timeout_diagnostics"}
)

# Text emitted by a failed load/QA command is hostile input.  Keep the reader
# bounded even when the producer hangs, emits one giant line, or never emits a
# newline.  The public summary never needs the tail of a diagnostic log: when
# a boundary is reached it is explicitly marked incomplete and the workflow
# rejects it for mandatory evidence.
MAX_LOG_TOTAL_BYTES = 1_048_576
MAX_LOG_LINE_BYTES = 64 * 1024
MAX_LOG_LINES = 10_000
LOG_READ_CHUNK_BYTES = 8 * 1024
MAX_JSON_INPUT_BYTES = 8 * 1024 * 1024


def _bounded_utf8_bytes(value: Any, limit: int) -> tuple[bytes | None, bool]:
    """Convert one input chunk without materializing an unbounded value."""

    if limit <= 0:
        return b"", True
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            view = memoryview(value)
            # ``len(memoryview)`` counts elements, not bytes, for formats
            # such as ``I``.  A byte cast keeps the boundary a true byte
            # limit without copying an attacker-sized source first.
            byte_view = view.cast("B")
            oversized = byte_view.nbytes > limit
            return byte_view[:limit].tobytes(), oversized
        except (TypeError, ValueError):
            return None, True
    if isinstance(value, str):
        # Slicing before encoding bounds work for a hostile text iterable.  A
        # multibyte prefix can still exceed the byte limit, so trim the
        # encoded result as a second byte-level guard.
        candidate = value[:limit]
        oversized = len(value) > len(candidate)
        try:
            encoded = candidate.encode("utf-8", "replace")
        except Exception:
            return None, True
        if len(encoded) > limit:
            oversized = True
            encoded = encoded[:limit]
        return encoded, oversized
    return None, True


@dataclass(slots=True)
class BoundedLineReader:
    """Read text or binary lines under hard byte/line limits.

    File handles are consumed with bounded ``read`` calls rather than an
    unbounded line read. Iterable inputs remain supported for unit tests and small
    in-memory callers, but unknown item types fail closed without stringifying
    the value.  Once any boundary is reached the reader stops consuming the
    source, so an endless producer cannot keep the sanitizer alive forever.
    """

    source: Any
    max_lines: int
    max_total_bytes: int = MAX_LOG_TOTAL_BYTES
    max_line_bytes: int = MAX_LOG_LINE_BYTES
    total_bytes: int = 0
    line_count: int = 0
    truncated: bool = False
    error: bool = False
    _started: bool = False

    def __post_init__(self) -> None:
        for field_name, default, ceiling in (
            ("max_lines", 1, MAX_LOG_LINES),
            ("max_total_bytes", MAX_LOG_TOTAL_BYTES, MAX_LOG_TOTAL_BYTES),
            ("max_line_bytes", MAX_LOG_LINE_BYTES, MAX_LOG_LINE_BYTES),
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                setattr(self, field_name, default)
            else:
                setattr(self, field_name, min(value, ceiling))

    def __iter__(self) -> Iterator[str]:
        if self._started:
            return iter(())
        self._started = True
        try:
            read = getattr(self.source, "read", None)
        except Exception:
            self.error = True
            self.truncated = True
            return iter(())
        if callable(read):
            return self._iter_stream()
        return self._iter_items()

    def _decode_line(self, raw_line: bytes) -> str:
        return raw_line.decode("utf-8", "replace")

    def _emit_partial(self, buffer: bytearray) -> Iterator[str]:
        if not buffer or self.line_count >= self.max_lines:
            return
        raw_line = bytes(buffer[: self.max_line_bytes])
        buffer.clear()
        self.line_count += 1
        yield self._decode_line(raw_line)

    def _iter_stream(self) -> Iterator[str]:
        buffer = bytearray()
        while True:
            if self.line_count >= self.max_lines:
                self.truncated = True
                return
            remaining = self.max_total_bytes - self.total_bytes
            if remaining <= 0:
                self.truncated = True
                yield from self._emit_partial(buffer)
                return
            try:
                chunk = self.source.read(min(LOG_READ_CHUNK_BYTES, remaining))
            except Exception:
                self.error = True
                self.truncated = True
                return
            raw_chunk, oversized = _bounded_utf8_bytes(chunk, remaining)
            if raw_chunk is None:
                self.error = True
                self.truncated = True
                return
            if not raw_chunk:
                if oversized:
                    self.truncated = True
                    yield from self._emit_partial(buffer)
                    return
                yield from self._emit_partial(buffer)
                return
            self.total_bytes += len(raw_chunk)
            buffer.extend(raw_chunk)

            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw_line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if len(raw_line) > self.max_line_bytes:
                    self.line_count += 1
                    self.truncated = True
                    yield self._decode_line(raw_line[: self.max_line_bytes])
                    return
                self.line_count += 1
                yield self._decode_line(raw_line.rstrip(b"\r"))
                if self.line_count >= self.max_lines:
                    self.truncated = True
                    return

            if len(buffer) > self.max_line_bytes:
                self.line_count += 1
                self.truncated = True
                yield self._decode_line(bytes(buffer[: self.max_line_bytes]))
                return
            if oversized or self.total_bytes >= self.max_total_bytes:
                self.truncated = True
                yield from self._emit_partial(buffer)
                return

    def _iter_items(self) -> Iterator[str]:
        try:
            items = iter(self.source)
        except Exception:
            self.error = True
            self.truncated = True
            return
        while True:
            # Check before asking the source for another item.  This matters
            # for endless generators: max_lines must not consume one hidden
            # tail item merely to discover that the limit was reached.
            if self.line_count >= self.max_lines:
                self.truncated = True
                return
            try:
                item = next(items)
            except StopIteration:
                return
            except Exception:
                self.error = True
                self.truncated = True
                return
            remaining = self.max_total_bytes - self.total_bytes
            if remaining <= 0:
                self.truncated = True
                return
            raw_line, oversized = _bounded_utf8_bytes(item, remaining)
            if raw_line is None:
                self.error = True
                self.truncated = True
                return
            self.total_bytes += len(raw_line)
            if len(raw_line) > self.max_line_bytes:
                self.line_count += 1
                self.truncated = True
                yield self._decode_line(raw_line[: self.max_line_bytes])
                return
            self.line_count += 1
            yield self._decode_line(raw_line.rstrip(b"\r\n"))
            if oversized or self.total_bytes >= self.max_total_bytes:
                self.truncated = True
                return


def _read_bounded_json(path: Path, *, maximum: int = MAX_JSON_INPUT_BYTES) -> bytes | None:
    """Read a JSON source with a hard byte ceiling before parsing it."""

    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(LOG_READ_CHUNK_BYTES, maximum - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    return None
                chunks.append(chunk)
                if total == maximum:
                    # A full budget is deliberately treated as incomplete;
                    # a final byte cannot be read without exceeding the hard
                    # ceiling, so fail closed rather than claiming complete.
                    return None
    except Exception:
        return None
    return b"".join(chunks)


PUBLIC_METRIC_FIELDS = frozenset(
    {
        "count",
        "avg",
        "p50",
        "p90",
        "p95",
        "p99",
        "max",
        "avg_ms",
        "p50_ms",
        "p90_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        "avg_bytes",
        "p50_bytes",
        "p95_bytes",
        "p99_bytes",
        "max_bytes",
        "total_bytes",
        "min",
        "last",
        "first",
        "delta",
        "avg_percent",
        "max_percent",
        "avg_used_mb",
        "max_used_mb",
        "total_mb",
        "avg_cpu_percent",
        "max_cpu_percent",
        "avg_rss_mb",
        "max_rss_mb",
        "avg_read_mb_per_second",
        "max_read_mb_per_second",
        "avg_write_mb_per_second",
        "max_write_mb_per_second",
        "percent",
    }
)
PUBLIC_SUMMARY_INT_FIELDS = frozenset(
    {
        "requests",
        "actions",
        "errors",
        "successful_responses",
        "successes",
        "final_successes",
        "final_failures",
        "temporary_overload_responses",
        "retry_attempts",
        "total_retries",
        "unexpected_statuses",
        "configured_actions",
        "submitted_actions",
        "missing_actions",
        "configured_logical_actions",
        "submitted_logical_actions",
        "missing_logical_actions",
        "state_read_requests",
        "total_requests_including_state",
        "primary_actions",
        "duplicate_actions",
        "configured_duplicate_actions",
        "submitted_duplicate_actions",
        "missing_duplicate_actions",
        "candidate_actions",
        "completed_actions",
        "configured_reads",
        "submitted_reads",
        "completed_reads",
        "missing_reads",
        "logged_requests",
        "sampled_requests",
        "logged_stages",
        "logged_events",
        "route_classes",
        "api_rows",
        "matched_by_request_id",
        "matched_by_diagnostic_id",
        "matched_by_cf_ray",
        "samples",
        "process_count",
        "process_count_last",
        "process_count_max",
        "new_processes",
        "missing_processes",
        "max_lock_waiters",
        "max_waiting_backends",
        "max_ungranted_locks",
        "mismatches",
        "cf_ray_present",
        "cf_ray_count",
    }
)
PUBLIC_SUMMARY_NUMBER_FIELDS = frozenset(
    {
        "final_failure_rate_percent",
        "temporary_overload_rate_percent",
        "retry_amplification_percent",
        "retries_per_action",
        "wall_seconds",
        "requests_per_second",
        "attempts_per_second",
        "throughput_per_second",
        "goodput_per_second",
        "successful_goodput_actions_per_second",
        "offered_requests_per_second",
        "actual_arrival_requests_per_second",
        "offered_logical_actions_per_second",
        "actual_arrival_logical_actions_per_second",
        "target_logical_actions_per_second",
        "offered_window_seconds",
        "duration_seconds",
        "opening_spread_seconds",
        "p95_ms",
        "p99_ms",
        "request_time_ms",
        "upstream_connect_time_ms",
        "upstream_header_time_ms",
        "upstream_time_ms",
        "unattributed_upstream_after_data_ms",
        "avg_sql_queries_per_request",
        "avg_db_time_ms",
        "avg_compute_time_ms",
        "max_sql_time_ms",
        "request_ms",
        "sql_ms",
        "pool_checkout_wait_ms",
        "pool_connection_hold_ms",
        "authenticated_read_admission_wait_ms",
        "non_sql_after_pool_time",
        "connection_after_sql_ms",
        "db_sql_ms",
        "compute_ms",
        "request_time_ms",
        "upstream_connect_time_ms",
        "upstream_header_time_ms",
        "upstream_time_ms",
        "planning_time_ms",
        "execution_time_ms",
    }
)
PUBLIC_SUMMARY_BOOL_FIELDS = frozenset(
    {
        "complete",
        "partial",
        "authoritative",
        "dispatchable",
        "stream_clock_aligned",
    }
)
PUBLIC_TIMING_INT_FIELDS = frozenset(
    {
        "timing_schema",
        "expected_count",
        "submitted_count",
        "completed_count",
        "scheduled_count",
        "started_count",
        "actual_request_start_count",
        "actual_start_count",
        "response_completion_count",
        "user_observed_count",
        "late_start_count",
        "dropped_work",
        "missing_schedule_context",
        "missing_timing_context",
        "invalid_timing_context",
    }
)
PUBLIC_TIMING_NUMBER_FIELDS = frozenset(
    {
        "late_start_percent",
        "response_completion_window_seconds",
        "actual_arrival_window_seconds",
        "offered_arrival_window_seconds",
        "offered_requests_per_second",
        "actual_arrival_requests_per_second",
        "offered_logical_actions_per_second",
        "actual_arrival_logical_actions_per_second",
    }
)
PUBLIC_TIMING_METRICS = frozenset(
    {
        "service_latency",
        "user_observed_latency",
        "executor_queue_wait",
        "schedule_delay",
        "late_start",
    }
)
PUBLIC_TRANSPORT_PHASES = frozenset(
    {
        "dns_ms",
        "tcp_connect_ms",
        "tls_handshake_ms",
        "request_write_ms",
        "edge_wait_ms",
        "ttfb_ms",
        "body_receive_ms",
        "total_ms",
    }
)
PUBLIC_TRANSPORT_NAMES = frozenset({"urllib-http1-close", "http1-keepalive", "other"})
PUBLIC_HTTP_VERSIONS = frozenset({"1.0", "1.1", "2", "3", "other"})
PUBLIC_PROCESS_LABELS = frozenset(
    {
        "deadlock-api",
        "deadlock-web",
        "deadlock-worker",
        "postgresql",
        "redis-server",
        "nginx",
        "load-generator",
    }
)
PUBLIC_SOCKET_STATES = frozenset(
    {
        "ESTAB",
        "LISTEN",
        "TIME_WAIT",
        "SYN_SENT",
        "SYN_RECV",
        "FIN_WAIT1",
        "FIN_WAIT2",
        "CLOSE_WAIT",
        "LAST_ACK",
        "CLOSING",
        "CLOSED",
        "other",
    }
)
PUBLIC_QUEUE_NAMES = frozenset(
    {
        "deadlock-platform-high",
        "deadlock-platform-default",
        "deadlock-platform-low",
    }
)
PUBLIC_CPU_CORE_KEYS = frozenset(f"cpu{index}" for index in range(256))
PUBLIC_REQUEST_PERF_STAGES = frozenset(
    {
        "auth_bootstrap_auth_query_ms",
        "auth_bootstrap_avatar_query_ms",
        "auth_bootstrap_response_build_ms",
        "workspace_auth_ms",
        "workspace_conditional_preflight_ms",
        "workspace_tournament_base_ms",
        "workspace_media_ms",
        "workspace_access_ms",
        "workspace_invite_ms",
        "workspace_bracket_ms",
        "workspace_ready_check_ms",
        "workspace_serialization_ms",
        "workspace_etag_ms",
        "ready_vote_auth_ms",
        "ready_vote_checkout_count",
        "ready_vote_checkout_ms",
        "ready_vote_admission_inflight",
        "ready_vote_admission_limit",
        "ready_vote_admission_wait_ms",
        "ready_vote_admitted_total",
        "ready_vote_shed_total",
        "ready_vote_controller_limit_changes",
        "ready_vote_cpu_pressure",
        "ready_vote_pool_wait_ms",
        "ready_vote_cpu_monitor_sample_ms",
        "ready_vote_cpu_monitor_samples",
        "ready_vote_preflight_ms",
        "ready_vote_upsert_ms",
        "ready_vote_commit_ms",
        "ready_vote_response_ms",
    }
)
PUBLIC_CONTROLLER_STATES = frozenset(
    {"open", "closed", "locked", "admitting", "pressure", "shed", "other"}
)
PUBLIC_ACCEPTANCE_CHECK_KEYS = frozenset(
    {
        "complete",
        "contract_ok",
        "timing_complete",
        "phase_present",
        "complete_flag",
        "raw_outcome_consistent",
        "logical_outcome_consistent",
        "raw_requests_match_timing",
        "raw_population_complete",
        "ready_count_evidence_present",
        "ready_count_maps_typed",
        "ready_count_maps_match",
        "duplicate_has_no_state_changes",
        "raw_unexpected_statuses_zero",
        "capacity_ramp_evidence",
        "successful_goodput_measured",
        "minimum_useful_goodput",
        "logical_final_failure",
        "phase_population_reconcilable",
        "phase_summaries_reconcilable",
        "phase_retry_totals_reconcilable",
        "raw_requests_reconcile_to_logical_population",
        "authoritative_binding",
    }
)
PUBLIC_BINDING_CHECK_KEYS = frozenset(
    {
        "report_schema",
        "measurement_schema",
        "timing_schema",
        "profile_id",
        "profile_version",
        "profile_digest",
        "contract_schema",
        "contract",
        "mode",
        "environment",
        "source_git_sha",
        "run_identity",
        "authoritative",
        "dispatchable",
        "runtime_budget",
        "evidence_envelope",
    }
)
PUBLIC_DECISIONS = frozenset(
    {
        "SLO PASS",
        "SLO FAIL",
        "STRESS BEHAVIOR PASS",
        "STRESS BEHAVIOR FAIL",
        "LOAD RUN FAILED",
        "LOAD REPORT BINDING FAIL",
        "LOAD PROFILE NON-AUTHORITATIVE",
        "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        "other",
    }
)
PUBLIC_TIMEOUT_CLASSIFICATIONS = frozenset(
    {
        "before_origin_observed",
        "nginx_or_edge_before_next",
        "api_or_next_after_api",
        "api_or_next_incomplete",
        "next_ssr_or_node_queue",
        "next_accept_or_node_queue",
        "client_or_edge_before_origin",
        "origin_completion_after_client_timeout",
        "client_or_edge_after_origin",
        "other",
    }
)
PUBLIC_TIMEOUT_REASON_CLASSES = frozenset(
    {
        "no_origin_observation",
        "next_not_accepted",
        "api_completed",
        "api_incomplete",
        "ssr_observed",
        "next_accepted_without_ssr",
        "origin_started_after_timeout",
        "origin_completed_after_timeout",
        "origin_completed_before_timeout",
        "other",
    }
)
PUBLIC_REQUEST_COMPLETIONS = frozenset(
    {"completed", "timeout", "aborted", "other"}
)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _allowed_string(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _copy_int(source: dict[str, Any], key: str) -> int | None:
    return safe_int(source.get(key)) if key in source else None


def _copy_number(source: dict[str, Any], key: str) -> float | None:
    return finite_number(source.get(key)) if key in source else None


def _copy_bool(source: dict[str, Any], key: str) -> bool | None:
    value = source.get(key)
    return value if type(value) is bool else None


def _copy_status(source: dict[str, Any], key: str) -> int | None:
    value = source.get(key)
    if type(value) is not int:
        return None
    return value if 0 <= value <= 599 else None


def _copy_fixed_fields(
    source: dict[str, Any],
    keys: Iterable[str],
    *,
    integer_keys: frozenset[str] = frozenset(),
    number_keys: frozenset[str] = frozenset(),
    boolean_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in keys:
        if key in integer_keys:
            value = _copy_int(source, key)
        elif key in number_keys:
            value = _copy_number(source, key)
        elif key in boolean_keys:
            value = _copy_bool(source, key)
        else:
            value = None
        if value is not None:
            output[key] = value
    return output


def _project_metric(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    for key in PUBLIC_METRIC_FIELDS:
        if key not in source:
            continue
        if key == "count":
            number = safe_int(source[key])
        else:
            number = finite_number(source[key])
        if number is not None:
            output[key] = number
    return output


def _project_counter(value: Any, allowed: frozenset[str]) -> dict[str, int]:
    source = _mapping(value)
    output: dict[str, int] = {}
    for raw_key, raw_value in source.items():
        if not isinstance(raw_key, str) or raw_key not in allowed:
            continue
        key = raw_key
        count = safe_int(raw_value)
        if count is not None:
            output[key] = count
    return dict(sorted(output.items()))


def _project_status_counter(value: Any) -> dict[str, int]:
    source = _mapping(value)
    output: dict[str, int] = {}
    for raw_key, raw_value in source.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key
        if not re.fullmatch(r"[0-9]{1,3}", key):
            continue
        status = safe_status(key)
        count = safe_int(raw_value)
        if count is not None and 100 <= status <= 599:
            output[str(status)] = count
    return dict(sorted(output.items(), key=lambda item: int(item[0])))


def _project_route_metric_map(value: Any) -> dict[str, dict[str, Any]]:
    source = _mapping(value)
    output: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in source.items():
        if not isinstance(raw_key, str):
            continue
        parts = raw_key.split(" ", 1)
        if len(parts) != 2:
            continue
        method, route_class = parts
        if (
            safe_method(method) == "OTHER"
            or _allowed_string(route_class, SAFE_ROUTE_CLASSES) is None
        ):
            continue
        metric = _project_metric(raw_value)
        if metric:
            output[f"{safe_method(method)} {route_class}"] = metric
    return dict(sorted(output.items()))


def _project_route_summary_map(
    value: Any,
    *,
    method_route: bool,
) -> dict[str, dict[str, Any]]:
    """Project diagnostic route summaries with bounded route keys."""

    source = _mapping(value)
    output: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in source.items():
        if not isinstance(raw_key, str):
            continue
        if method_route:
            parts = raw_key.split(" ", 1)
            if len(parts) != 2 or safe_method(parts[0]) == "OTHER":
                continue
            route_class = parts[1]
            if _allowed_string(route_class, SAFE_ROUTE_CLASSES) is None:
                continue
            key = f"{safe_method(parts[0])} {route_class}"
        else:
            if raw_key not in SAFE_ROUTE_CLASSES:
                continue
            key = raw_key
        raw_mapping = _mapping(raw_value)
        if not any(
            field in raw_mapping
            for field in ("requests", "total", "request", "by_route", "by_method_route")
        ):
            # Some producers use the compact route metric shape rather than
            # the request-performance summary shape.  It is still safe when
            # the route key is closed and the value is reduced to numeric
            # metric fields only.
            projected = _project_metric(raw_value)
        else:
            projected = _project_summary(raw_value)
        if projected:
            output[key] = projected
    return dict(sorted(output.items()))


def _project_phase_summary_map(value: Any) -> dict[str, dict[str, Any]]:
    source = _mapping(value)
    output: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in source.items():
        phase = safe_phase(raw_key)
        if phase == "other" and raw_key != "other":
            continue
        raw_mapping = _mapping(raw_value)
        if not any(
            field in raw_mapping
            for field in ("requests", "total", "request", "by_phase", "by_qa_phase")
        ):
            projected = _project_metric(raw_value)
        else:
            projected = _project_summary(raw_value)
        if projected:
            output[phase] = projected
    return dict(sorted(output.items()))


def _project_phase_metric_map(value: Any) -> dict[str, dict[str, Any]]:
    source = _mapping(value)
    output: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in source.items():
        phase = safe_phase(raw_key)
        if phase == "other" and raw_key != "other":
            continue
        metric = _project_metric(raw_value)
        if metric:
            output[phase] = metric
    return dict(sorted(output.items()))


def _project_timing(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output = _copy_fixed_fields(
        source,
        PUBLIC_TIMING_INT_FIELDS | PUBLIC_TIMING_NUMBER_FIELDS,
        integer_keys=PUBLIC_TIMING_INT_FIELDS,
        number_keys=PUBLIC_TIMING_NUMBER_FIELDS,
    )
    partial = _copy_bool(source, "partial")
    if partial is not None:
        output["partial"] = partial
    for key in PUBLIC_TIMING_METRICS:
        metric = _project_metric(source.get(key))
        if metric:
            output[key] = metric
    return output


def _project_transport(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    names = _project_counter(source.get("names"), PUBLIC_TRANSPORT_NAMES)
    versions = _project_counter(source.get("http_versions"), PUBLIC_HTTP_VERSIONS)
    if names:
        output["names"] = names
    if versions:
        output["http_versions"] = versions
    for key in ("connection_reused", "connection_new"):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    phase_timings = _mapping(source.get("phase_timings"))
    projected_phases: dict[str, dict[str, Any]] = {}
    for key in PUBLIC_TRANSPORT_PHASES:
        metric = _project_metric(phase_timings.get(key))
        if metric:
            projected_phases[key] = metric
    if projected_phases:
        output["phase_timings"] = dict(sorted(projected_phases.items()))
    return output


def _project_nested_metric_map(
    value: Any,
    allowed: frozenset[str],
) -> dict[str, Any]:
    """Project a fixed-key aggregate map whose values may be scalar/metric."""

    source = _mapping(value)
    output: dict[str, Any] = {}
    for raw_key, raw_value in source.items():
        if not isinstance(raw_key, str) or raw_key not in allowed:
            continue
        if isinstance(raw_value, dict):
            metric = _project_metric(raw_value)
            if metric:
                output[raw_key] = metric
            continue
        count = safe_int(raw_value)
        if count is not None:
            output[raw_key] = count
    return dict(sorted(output.items()))


def _project_named_metric_map(
    value: Any,
    allowed: frozenset[str],
) -> dict[str, dict[str, Any]]:
    source = _mapping(value)
    output: dict[str, dict[str, Any]] = {}
    for key in sorted(allowed):
        metric = _project_metric(source.get(key))
        if metric:
            output[key] = metric
    return output


def _project_error_row(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    status = _copy_status(source, "status")
    source_route_class = source.get("route_class")
    output: dict[str, Any] = {
        "phase": safe_phase(source.get("phase")),
        "method": safe_method(source.get("method")),
        "route_class": (
            source_route_class
            if _allowed_string(source_route_class, SAFE_ROUTE_CLASSES) is not None
            else safe_route_class(source.get("path"))
        ),
        "error_class": safe_error_class(source.get("error_class"), status=source.get("status")),
        "cf_error_class": safe_cf_error_class(source.get("cf_error_class")),
        "cf_error_origin_class": safe_cf_error_class(source.get("cf_error_origin_class")),
    }
    if status is not None:
        output["status"] = status
    for key in ("ttfb_ms", "elapsed_ms"):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    return output


def _project_error_rows(value: Any, *, limit: int = 64) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_project_error_row(item) for item in value[:limit] if isinstance(item, dict)]


def _project_summary(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    scope = source.get("scope")
    if _allowed_string(
        scope,
        frozenset(
            {"full_population", "logical_user_actions", "diagnostic_sample", "other"}
        ),
    ) is not None:
        output["scope"] = scope
    for key in PUBLIC_SUMMARY_INT_FIELDS:
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    for key in PUBLIC_SUMMARY_NUMBER_FIELDS:
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    for key in PUBLIC_SUMMARY_BOOL_FIELDS:
        flag = _copy_bool(source, key)
        if flag is not None:
            output[key] = flag
    for key in (
        "latency",
        "end_to_end_latency",
        "accepted_request_latency",
        "time_to_first_byte",
        "response_bytes",
        "overall",
        "request_ms",
        "sql_ms",
        "db_sql_ms",
        "compute_ms",
        "total",
        "request",
        "non_sql_time",
        "non_sql_after_pool_time",
        "connection_after_sql_ms",
        "pool_checkout_wait_ms",
        "pool_connection_hold_ms",
        "authenticated_read_admission_wait_ms",
        "request_time_ms",
        "upstream_connect_time_ms",
        "upstream_header_time_ms",
        "upstream_time_ms",
        "unattributed_upstream_after_data_ms",
    ):
        metric = _project_metric(source.get(key))
        if metric:
            output[key] = metric
    timing = _project_timing(source.get("timing"))
    if timing:
        output["timing"] = timing
    transport = _project_transport(source.get("transport"))
    if transport:
        output["transport"] = transport
    routes = _project_route_metric_map(source.get("by_route"))
    if routes:
        output["by_route"] = routes
    route_summaries = _project_route_summary_map(source.get("by_route"), method_route=False)
    if route_summaries:
        output["by_route"] = route_summaries
    method_route_summaries = _project_route_summary_map(
        source.get("by_method_route"), method_route=True
    )
    if method_route_summaries:
        output["by_method_route"] = method_route_summaries
    phases = _project_phase_metric_map(source.get("by_phase"))
    if phases:
        output["by_phase"] = phases
    phase_summaries = _project_phase_summary_map(source.get("by_qa_phase"))
    if phase_summaries:
        output["by_qa_phase"] = phase_summaries
    for key in ("status_counts", "final_status_counts", "statuses"):
        statuses = _project_status_counter(source.get(key))
        if statuses:
            output[key] = statuses
    for key in ("error_kinds", "cf_error_type_counts", "cf_error_origin_counts"):
        counters = _project_counter(source.get(key), SAFE_ERROR_CLASSES | SAFE_CF_ERROR_CLASSES)
        if counters:
            output[key] = counters
    changed = _project_counter(source.get("changed_counts"), frozenset({"False", "True"}))
    if changed:
        output["changed_counts"] = changed
    for key in ("error_samples", "timeout_diagnostics"):
        rows = _project_error_rows(source.get(key))
        if rows:
            output[key] = rows
    read_models = _mapping(source.get("redis_read_models"))
    if read_models:
        read_output: dict[str, Any] = {}
        events = _copy_int(read_models, "events")
        if events is not None:
            read_output["events"] = events
        for key, allowed in (
            ("by_model", frozenset({"teams", "workspace_detail", "bracket_summary", "bracket_full", "other"})),
            ("by_outcome", SAFE_OUTCOMES),
        ):
            counters = _project_counter(read_models.get(key), allowed)
            if counters:
                read_output[key] = counters
        for key in ("get_ms", "build_ms", "set_ms", "payload_bytes"):
            metric = _project_metric(read_models.get(key))
            if metric:
                read_output[key] = metric
        if read_output:
            output["redis_read_models"] = read_output
    for key in ("ready_vote", "workspace", "auth_bootstrap"):
        stage_metrics = _project_named_metric_map(
            source.get(key), PUBLIC_REQUEST_PERF_STAGES
        )
        if stage_metrics:
            output[key] = stage_metrics
    controller_states = _project_counter(
        source.get("ready_vote_controller_state_counts"), PUBLIC_CONTROLLER_STATES
    )
    if controller_states:
        output["ready_vote_controller_state_counts"] = controller_states
    return output


def _project_ramp_analysis(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    stages = source.get("stages")
    if isinstance(stages, list):
        projected_stages: list[dict[str, Any]] = []
        for item in stages[:128]:
            row = _mapping(item)
            projected: dict[str, Any] = {}
            for key in ("concurrency",):
                number = _copy_int(row, key)
                if number is not None:
                    projected[key] = number
            for key in (
                "p95_ms",
                "p99_ms",
                "requests_per_second",
            ):
                number = _copy_number(row, key)
                if number is not None:
                    projected[key] = number
            if projected:
                projected_stages.append(projected)
        if projected_stages:
            output["stages"] = projected_stages
    comparisons = source.get("comparisons")
    if isinstance(comparisons, list):
        projected_comparisons: list[dict[str, Any]] = []
        for item in comparisons[:128]:
            row = _mapping(item)
            projected: dict[str, Any] = {}
            for key in ("from_concurrency", "to_concurrency"):
                number = _copy_int(row, key)
                if number is not None:
                    projected[key] = number
            for key in ("p95_growth_percent", "throughput_growth_percent"):
                number = _copy_number(row, key)
                if number is not None:
                    projected[key] = number
            if projected:
                projected_comparisons.append(projected)
        if projected_comparisons:
            output["comparisons"] = projected_comparisons
    knee = _mapping(source.get("knee"))
    knee_output: dict[str, Any] = {}
    for key in ("stable_concurrency", "first_queued_concurrency"):
        number = _copy_int(knee, key)
        if number is not None:
            knee_output[key] = number
    reason = knee.get("reason")
    if _allowed_string(
        reason,
        frozenset(
            {"p95 grew at least 50% while throughput grew at most 10%"}
        ),
    ) is not None:
        knee_output["reason"] = reason
    if knee_output:
        output["knee"] = knee_output
    recommended = _copy_int(source, "recommended_max_concurrency")
    if recommended is not None:
        output["recommended_max_concurrency"] = recommended
    rollout_required = _copy_bool(source, "rollout_required")
    if rollout_required is not None:
        output["rollout_required"] = rollout_required
    return output


def _project_phase(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output = _project_summary(source)
    for key in ("configured_actions", "submitted_actions", "missing_actions", "candidate_actions", "completed_actions"):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    for key in ("target_logical_actions_per_second", "offered_window_seconds", "duration_seconds"):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    for key in ("complete",):
        flag = _copy_bool(source, key)
        if flag is not None:
            output[key] = flag
    status = source.get("status")
    if _allowed_string(status, frozenset({"complete", "incomplete", "other"})) is not None:
        output["status"] = status
    concurrency_stages = source.get("concurrency_stages")
    if isinstance(concurrency_stages, list):
        projected_stages: list[int] = []
        for item in concurrency_stages[:128]:
            number = safe_int(item, maximum=10_000)
            if number is None:
                projected_stages = []
                break
            projected_stages.append(number)
        if projected_stages:
            output["concurrency_stages"] = projected_stages
    analysis = _project_ramp_analysis(source.get("analysis"))
    if analysis:
        output["analysis"] = analysis
    for key in ("raw_http", "logical"):
        summary = _project_summary(source.get(key))
        if summary:
            output[key] = summary
    nested_phases = _mapping(source.get("phases"))
    phase_output: dict[str, Any] = {}
    for raw_key, raw_value in nested_phases.items():
        phase = safe_phase(raw_key)
        if phase != "other" or raw_key == "other":
            projected = _project_phase(raw_value)
            if projected:
                phase_output[phase] = projected
    if phase_output:
        output["phases"] = dict(sorted(phase_output.items()))
    stages = _mapping(source.get("stages"))
    stage_output: dict[str, Any] = {}
    # Stage keys are concurrency values rather than a closed enum. Keep the
    # producer's useful ramp shape, but bound the map before it becomes public
    # evidence; malformed/dynamic keys are still dropped below.
    for raw_key, raw_value in list(stages.items())[:128]:
        concurrency = _safe_numeric_map_key(raw_key, maximum=10_000)
        if concurrency is None:
            continue
        projected = _project_summary(raw_value)
        if projected:
            stage_output[str(concurrency)] = projected
    if stage_output:
        output["stages"] = dict(sorted(stage_output.items(), key=lambda item: int(item[0])))
    return output


def _project_acceptance(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    for key in (
        "passed",
        "contract_ok",
        "authoritative",
        "dispatchable",
        "complete",
        "experiment_complete",
        "target_passed",
        "pending_origin_evidence",
        "phase_completion",
    ):
        flag = _copy_bool(source, key)
        if flag is not None:
            output[key] = flag
    decision = source.get("decision")
    output["decision"] = (
        decision if _allowed_string(decision, PUBLIC_DECISIONS) is not None else "other"
    )
    for key in (
        "p95_ms",
        "p99_ms",
        "accepted_request_p50_ms",
        "accepted_request_p90_ms",
        "accepted_request_p95_ms",
        "accepted_request_p99_ms",
        "logical_p95_ms",
        "logical_p99_ms",
        "logical_final_failure_rate_percent",
        "shed_percent",
        "retry_amplification_percent",
        "slo_capacity_logical_actions_per_second",
        "max_stable_goodput_actions_per_second",
        "minimum_useful_goodput_actions_per_second",
        "successful_goodput_actions_per_second",
        "target_logical_actions_per_second",
    ):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    checks = _mapping(source.get("checks"))
    projected_checks: dict[str, bool] = {}
    for key in PUBLIC_ACCEPTANCE_CHECK_KEYS:
        value = checks.get(key)
        if type(value) is bool:
            projected_checks[key] = value
    if projected_checks:
        output["checks"] = dict(sorted(projected_checks.items()))
    outcome = _mapping(source.get("outcome_evidence"))
    outcome_output: dict[str, dict[str, Any]] = {}
    for key in ("logical", "raw_http"):
        summary = _project_summary(outcome.get(key))
        if summary:
            outcome_output[key] = summary
    if outcome_output:
        output["outcome_evidence"] = outcome_output
    return output


def _project_report_binding(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    complete = _copy_bool(source, "complete")
    if complete is not None:
        output["complete"] = complete
    checks = _mapping(source.get("checks"))
    projected_checks = {
        key: checks[key]
        for key in PUBLIC_BINDING_CHECK_KEYS
        if type(checks.get(key)) is bool
    }
    if projected_checks:
        output["checks"] = dict(sorted(projected_checks.items()))
    return output


def _safe_profile_id(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and len(value) <= 128
        and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]{1,4}", value)
    ):
        return value
    return None


def _safe_profile_digest(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _safe_queryid(value: Any) -> str | None:
    """Keep PostgreSQL's numeric query identifier without an unbounded echo."""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    raw = str(value)
    if re.fullmatch(r"-?[0-9]{1,19}", raw) is None:
        return None
    try:
        number = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    # PostgreSQL queryids are signed 64-bit values.  Constraining the value
    # here also bounds the public field if a malformed producer report is
    # presented to the projector.
    if not -(2**63) <= number <= (2**63 - 1):
        return None
    return str(number)


def _project_external_load(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {
        "schema": 1,
        "public_projection": True,
        "authoritative": False,
    }
    passed = _copy_bool(source, "passed")
    if passed is not None:
        output["passed"] = passed
    status = source.get("status")
    if _allowed_string(
        status, frozenset({"complete", "incomplete", "failed", "unavailable", "other"})
    ) is not None:
        output["status"] = status
    for key in ("measurement_schema", "timing_schema"):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    mode = source.get("mode")
    output["mode"] = (
        mode
        if _allowed_string(mode, frozenset({"ready-vote", "read-mix", "page-load", "other"}))
        is not None
        else "other"
    )
    scope = source.get("scope")
    if _allowed_string(
        scope, frozenset({"full_population", "logical_user_actions", "other"})
    ) is not None:
        output["scope"] = scope
    output["origin_class"] = "production_origin" if source.get("origin_class") == "production_origin" else "other"
    profile = _mapping(source.get("load_contract"))
    profile_id = _safe_profile_id(profile.get("profile_id")) or _safe_profile_id(source.get("profile_id"))
    if profile_id is not None:
        output["profile_id"] = profile_id
    profile_version = _copy_int(profile, "profile_version")
    if profile_version is None:
        profile_version = _copy_int(source, "profile_version")
    if profile_version is not None:
        output["profile_version"] = profile_version
    profile_digest = _safe_profile_digest(profile.get("profile_digest")) or _safe_profile_digest(source.get("profile_digest"))
    if profile_digest is not None:
        output["profile_digest"] = profile_digest
    for key in (
        "users",
        "tournaments",
        "duplicate_count",
        "manual_refresh_count",
        "concurrency",
        "dropped_work",
        "late_start_count",
    ):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    for key in (
        "wall_seconds",
        "opening_spread_seconds",
        "offered_logical_actions_per_second",
        "actual_arrival_logical_actions_per_second",
        "actual_arrival_requests_per_second",
        "offered_requests_per_second",
    ):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    concurrency_stages = source.get("concurrency_stages")
    if isinstance(concurrency_stages, list):
        projected_stages: list[int] = []
        for item in concurrency_stages[:128]:
            number = safe_int(item, maximum=10_000)
            if number is None:
                projected_stages = []
                break
            projected_stages.append(number)
        if projected_stages:
            output["concurrency_stages"] = projected_stages
    for key in ("partial_work", "dispatchable"):
        flag = _copy_bool(source, key)
        if flag is not None:
            output[key] = flag
    scenario = source.get("scenario_kind")
    if _allowed_string(
        scenario, frozenset({"slo", "stress", "spike", "capacity", "other"})
    ) is not None:
        output["scenario_kind"] = scenario
    transport = source.get("client_transport")
    if _allowed_string(transport, PUBLIC_TRANSPORT_NAMES) is not None:
        output["client_transport"] = transport
    trace = _mapping(source.get("trace"))
    if trace:
        trace_output: dict[str, Any] = {}
        available = _copy_bool(trace, "available")
        if available is not None:
            trace_output["available"] = available
        status = _copy_status(trace, "status")
        if status is not None:
            trace_output["status"] = status
        if "error_class" in trace:
            trace_output["error_class"] = safe_error_class(trace.get("error_class"))
        if trace_output:
            output["trace"] = trace_output
    for key in ("overall", "raw_http", "logical"):
        summary = _project_summary(source.get(key))
        if summary:
            output[key] = summary
    phases = _mapping(source.get("phases"))
    phase_output: dict[str, Any] = {}
    for raw_key, raw_value in phases.items():
        phase = safe_phase(raw_key)
        if phase != "other" or raw_key == "other":
            projected = _project_phase(raw_value)
            if projected:
                phase_output[phase] = projected
    if phase_output:
        output["phases"] = dict(sorted(phase_output.items()))
    timeout = _mapping(source.get("timeout_path_diagnostics"))
    if timeout:
        timeout_output: dict[str, Any] = {}
        enabled = _copy_bool(timeout, "enabled")
        if enabled is not None:
            timeout_output["enabled"] = enabled
        count = _copy_int(timeout, "request_count")
        if count is not None:
            timeout_output["request_count"] = count
        if timeout_output:
            output["timeout_path_diagnostics"] = timeout_output
    acceptance = _project_acceptance(source.get("acceptance"))
    if acceptance:
        output["acceptance"] = acceptance
    performance = _project_performance(source.get("performance"))
    if performance:
        output["performance"] = performance
    event_loop = _project_event_loop(source.get("event_loop"))
    if event_loop:
        output["event_loop"] = event_loop
    report_binding = _project_report_binding(source.get("report_binding"))
    if report_binding:
        output["report_binding"] = report_binding
    return output


def _project_connection_counts(value: Any) -> dict[str, int]:
    return _project_counter(value, frozenset({"total", "established", "limit", "used", "available"}))


def _project_waits(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    for key in (
        "samples",
        "lock_waiters",
        "waiting_backends",
        "active_backends",
        "ungranted_locks",
        "backend_connections",
        "max_lock_waiters",
        "max_waiting_backends",
        "max_ungranted_locks",
        "mismatches",
    ):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    for key in (
        "max_waiting_query_ms",
        "max_lock_waiting_query_ms",
    ):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    waits = _project_nested_metric_map(source.get("wait_state_counts"), SAFE_WAIT_STATES)
    ownership = _project_nested_metric_map(
        source.get("backend_ownership"), SAFE_BACKENDS
    )
    if waits:
        output["wait_state_counts"] = waits
    if ownership:
        output["backend_ownership"] = ownership
    consistency = _mapping(source.get("backend_ownership_consistency"))
    consistency_output = _copy_fixed_fields(
        consistency,
        {"samples", "mismatches"},
        integer_keys=frozenset({"samples", "mismatches"}),
    )
    all_match = _copy_bool(consistency, "all_match")
    if all_match is not None:
        consistency_output["all_match"] = all_match
    if consistency_output:
        output["backend_ownership_consistency"] = consistency_output
    error = source.get("error_class")
    if error is not None:
        output["error_class"] = safe_error_class(error)
    return output


def _project_process_metrics(value: Any) -> dict[str, dict[str, Any]]:
    source = _mapping(value)
    output: dict[str, dict[str, Any]] = {}
    for label in PUBLIC_PROCESS_LABELS:
        row = _mapping(source.get(label))
        if not row:
            continue
        projected: dict[str, Any] = {}
        for key in (
            "process_count",
            "samples",
            "process_count_last",
            "process_count_max",
        ):
            number = _copy_int(row, key)
            if number is not None:
                projected[key] = number
        for key in (
            "cpu_percent",
            "rss_bytes",
            "read_bytes_per_second",
            "write_bytes_per_second",
            "avg_cpu_percent",
            "max_cpu_percent",
            "avg_rss_mb",
            "max_rss_mb",
            "avg_read_mb_per_second",
            "max_read_mb_per_second",
            "avg_write_mb_per_second",
            "max_write_mb_per_second",
        ):
            number = _copy_number(row, key)
            if number is not None:
                projected[key] = number
        if projected:
            output[label] = projected
    return dict(sorted(output.items()))


def _project_system(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    for key in ("samples",):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    for key in ("interval_seconds", "postgres_cpu_percent"):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    for key in ("memory", "swap", "load_average_1m", "postgres_cpu_percent", "gunicorn_workers"):
        metric = _project_metric(source.get(key))
        if metric:
            output[key] = metric
    for key in (
        "nginx_established_connections",
        "postgres_tcp_established_connections",
        "postgres_backend_connections",
        "redis_established_connections",
    ):
        metric = _project_metric(source.get(key))
        if metric:
            output[key] = metric
    cpu_by_core = _mapping(source.get("cpu_per_core"))
    projected_cpu: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in cpu_by_core.items():
        if isinstance(raw_key, str) and raw_key in PUBLIC_CPU_CORE_KEYS:
            metric = _project_metric(raw_value)
            if metric:
                projected_cpu[raw_key] = metric
    if projected_cpu:
        output["cpu_per_core"] = dict(sorted(projected_cpu.items()))
    steal = _mapping(source.get("cpu_steal_per_core"))
    projected_steal: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in steal.items():
        if isinstance(raw_key, str) and raw_key in PUBLIC_CPU_CORE_KEYS:
            metric = _project_metric(raw_value)
            if metric:
                projected_steal[raw_key] = metric
    if projected_steal:
        output["cpu_steal_per_core"] = dict(sorted(projected_steal.items()))
    processes = _project_process_metrics(source.get("processes"))
    if processes:
        output["processes"] = processes
    waits_source = _mapping(source.get("postgres_waits"))
    if "postgres_backend_ownership" in source and "backend_ownership" not in waits_source:
        waits_source = {
            **waits_source,
            "backend_ownership": source.get("postgres_backend_ownership"),
        }
    if "postgres_backend_ownership_consistency" in source:
        waits_source = {
            **waits_source,
            "backend_ownership_consistency": source.get(
                "postgres_backend_ownership_consistency"
            ),
        }
    waits = _project_waits(waits_source)
    if waits:
        output["postgres_waits"] = waits
    socket_states = _project_counter(source.get("tcp_socket_states"), PUBLIC_SOCKET_STATES)
    if socket_states:
        output["tcp_socket_states"] = socket_states
    for key in ("redis_connections",):
        counters = _project_connection_counts(source.get(key))
        if counters:
            output[key] = counters
    listen_source = _mapping(source.get("tcp_listen_counters"))
    listen_output: dict[str, dict[str, Any]] = {}
    for key in ("ListenOverflows", "ListenDrops"):
        metric = _project_metric(listen_source.get(key))
        if metric:
            listen_output[key] = metric
    if listen_output:
        output["tcp_listen_counters"] = listen_output
    conntrack = _mapping(source.get("conntrack"))
    conntrack_output: dict[str, Any] = {}
    available = _copy_bool(conntrack, "available")
    if available is not None:
        conntrack_output["available"] = available
    max_percent = _copy_number(conntrack, "max_percent")
    if max_percent is not None:
        conntrack_output["max_percent"] = max_percent
    if conntrack_output:
        output["conntrack"] = conntrack_output
    queue_source = _mapping(source.get("celery_backlog"))
    queue_output: dict[str, Any] = {}
    samples = _copy_int(queue_source, "samples")
    if samples is not None:
        queue_output["samples"] = samples
    by_queue = _project_nested_metric_map(queue_source.get("by_queue"), PUBLIC_QUEUE_NAMES)
    if by_queue:
        queue_output["by_queue"] = by_queue
    backlog = queue_output
    if backlog:
        output["celery_backlog"] = backlog
    lifecycle_source = _mapping(source.get("process_lifecycle"))
    lifecycle_output: dict[str, Any] = {}
    for label in PUBLIC_PROCESS_LABELS:
        row = _mapping(lifecycle_source.get(label))
        if not row:
            continue
        projected = _copy_fixed_fields(
            row,
            {"new_processes", "missing_processes"},
            integer_keys=frozenset({"new_processes", "missing_processes"}),
        )
        if projected:
            lifecycle_output[label] = projected
    if lifecycle_output:
        output["process_lifecycle"] = dict(sorted(lifecycle_output.items()))
    return output


def _project_event_loop(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    samples = _copy_int(source, "samples")
    if samples is not None:
        output["samples"] = samples
    for key in ("p95_ms", "p99_ms", "max_ms", "elu", "cpu_pct", "gc_duration_ms", "gc_count"):
        metric = _project_metric(source.get(key))
        if metric:
            output[key] = metric
    memory = _mapping(source.get("memory"))
    memory_output: dict[str, Any] = {}
    for key in ("rss_bytes", "heap_total_bytes", "heap_used_bytes", "external_bytes", "array_buffers_bytes"):
        metric = _project_metric(memory.get(key))
        if metric:
            memory_output[key] = metric
    if memory_output:
        output["memory"] = memory_output
    details = source.get("samples_detail")
    if isinstance(details, list):
        safe_details: list[dict[str, Any]] = []
        for item in details[:256]:
            metric = _project_metric(item)
            if metric:
                safe_details.append(metric)
        if safe_details:
            output["samples_detail"] = safe_details
    return output


def _project_ssr(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    event_loop = _project_event_loop(source.get("event_loop"))
    if event_loop:
        output["event_loop"] = event_loop
    for section in ("ssr_stages", "ssr_stream"):
        section_source = _mapping(source.get(section))
        section_output: dict[str, Any] = {}
        for key in ("logged_stages", "logged_events", "sampled_requests"):
            number = _copy_int(section_source, key)
            if number is not None:
                section_output[key] = number
        if section == "ssr_stream":
            by_stage = _project_counter(section_source.get("by_stage"), SAFE_STAGE_NAMES)
        else:
            by_stage = _project_phase_metric_map(section_source.get("by_stage"))
        if by_stage:
            section_output["by_stage"] = by_stage
        integrity = _mapping(section_source.get("integrity"))
        integrity_output = _copy_fixed_fields(
            integrity,
            {
                "response_finish_requests",
                "response_close_requests",
                "response_error_requests",
                "close_without_finish_requests",
            },
            integer_keys=frozenset(
                {
                    "response_finish_requests",
                    "response_close_requests",
                    "response_error_requests",
                    "close_without_finish_requests",
                }
            ),
        )
        for key in ("write_count", "body_bytes"):
            metric = _project_metric(integrity.get(key))
            if metric:
                integrity_output[key] = metric
        if integrity_output:
            section_output["integrity"] = integrity_output
        if section_output:
            output[section] = section_output
    html = _mapping(source.get("nginx_html"))
    html_output = _project_summary(html)
    if html_output:
        output["nginx_html"] = html_output
    api = _mapping(source.get("nginx_api"))
    api_output: dict[str, Any] = {}
    for key in ("requests", "route_classes"):
        number = _copy_int(api, key)
        if number is not None:
            api_output[key] = number
    groups = _project_route_summary_map(api.get("by_method_route"), method_route=True)
    if not groups:
        groups = _project_route_metric_map(api.get("by_method_route"))
    if groups:
        api_output["by_method_route"] = groups
    if api_output:
        output["nginx_api"] = api_output
    correlated = _mapping(source.get("correlated_html"))
    correlated_output = _project_summary(correlated)
    for key in ("stage_presence", "stream_stage_presence"):
        counters = _project_counter(correlated.get(key), SAFE_STAGE_NAMES)
        if counters:
            correlated_output[key] = counters
    stage_ms = _project_phase_metric_map(correlated.get("stage_ms"))
    if stage_ms:
        correlated_output["stage_ms"] = stage_ms
    joins = _project_counter(
        correlated.get("api_request_perf_join"),
        frozenset({"api_rows", "matched_by_request_id", "matched_by_diagnostic_id", "matched_by_cf_ray"}),
    )
    if joins:
        correlated_output["api_request_perf_join"] = joins
    if correlated_output:
        output["correlated_html"] = correlated_output
    timeout = _project_timeout_collection(source.get("timeout_diagnostics"))
    if timeout:
        output["timeout_diagnostics"] = timeout
    return output


def _project_cpu_profile(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    enabled = _copy_bool(source, "enabled")
    if enabled is not None:
        output["enabled"] = enabled
    retention = _mapping(source.get("retention"))
    for key in (
        "max_profiles",
        "available_profiles",
        "bound_profiles",
        "summarized_profiles",
        "ignored_unbound_profiles",
        "ignored_stale_profiles",
        "invalid_profiles",
        "truncated_profiles",
    ):
        number = _copy_int(retention, key)
        if number is not None:
            output[key] = number
    artifacts = _copy_bool(retention, "artifacts_preserved")
    if artifacts is not None:
        output["artifacts_preserved"] = artifacts
    signal_delivery = _mapping(source.get("signal_delivery"))
    safe_signal_delivery: dict[str, Any] = {}
    for phase in ("arm", "flush"):
        phase_source = _mapping(signal_delivery.get(phase))
        phase_output: dict[str, Any] = {}
        for key in ("requested_count", "delivered_count", "rejected_count"):
            number = _copy_int(phase_source, key)
            if number is not None:
                phase_output[key] = number
        available = _copy_bool(phase_source, "pidfd_api_available")
        if available is not None:
            phase_output["pidfd_api_available"] = available
        reasons = _mapping(phase_source.get("rejection_reasons"))
        safe_reasons: dict[str, int] = {}
        for raw_reason, raw_count in reasons.items():
            reason = str(raw_reason)
            count = _copy_int(reasons, raw_reason)
            if reason in SAFE_PROFILE_SIGNAL_REASONS and count is not None:
                safe_reasons[reason] = count
        if safe_reasons:
            phase_output["rejection_reasons"] = dict(sorted(safe_reasons.items()))
        if phase_output:
            safe_signal_delivery[phase] = phase_output
    if safe_signal_delivery:
        output["signal_delivery"] = safe_signal_delivery
    if output:
        return output
    return {"enabled": False}


def _project_performance(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    for key in (
        "p50_ms",
        "p90_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        "avg_ms",
        "rss_bytes",
        "event_loop_lag_ms",
    ):
        number = _copy_number(source, key)
        if number is not None:
            output[key] = number
    return output


def _project_statement_snapshot(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    available = _copy_bool(source, "available")
    if available is not None:
        output["available"] = available
    if "error_class" in source:
        output["error_class"] = safe_error_class(source.get("error_class"))
    rows = source.get("rows")
    if isinstance(rows, list):
        projected_rows: list[dict[str, Any]] = []
        for item in rows[:500]:
            row = _mapping(item)
            queryid = _safe_queryid(row.get("queryid"))
            if queryid is None:
                continue
            projected: dict[str, Any] = {
                "queryid": queryid,
                "query_category": safe_query_category(row.get("query_category")),
                "backend": safe_backend(row.get("backend")),
            }
            for key in ("calls", "rows", "shared_blks_hit", "shared_blks_read", "temp_blks_written"):
                number = _copy_int(row, key)
                if number is not None:
                    projected[key] = number
            for key in ("total_exec_ms", "mean_exec_ms"):
                number = _copy_number(row, key)
                if number is not None:
                    projected[key] = number
            projected_rows.append(projected)
        output["rows"] = projected_rows
    return output


def _project_plan(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    available = _copy_bool(source, "available")
    if available is not None:
        output["available"] = available
    if "error_class" in source:
        output["error_class"] = safe_error_class(source.get("error_class"))
    plans = _mapping(source.get("plans"))
    plan_output: dict[str, Any] = {}
    allowed_nodes = frozenset(
        {
            "aggregate", "append", "bitmap_heap_scan", "bitmap_index_scan", "delete",
            "hash_join", "index_scan", "insert", "limit", "modify_table", "nested_loop",
            "result", "seq_scan", "sort", "update", "window_aggregate", "other",
        }
    )
    numeric_keys = frozenset(
        {
            "actual_rows", "actual_loops", "actual_total_time_ms", "actual_startup_time_ms",
            "plan_rows", "plan_width", "shared_hit_blocks", "shared_read_blocks",
            "shared_dirtied_blocks", "shared_written_blocks", "temp_read_blocks",
            "temp_written_blocks", "wal_records", "wal_fpi", "wal_bytes",
            "planning_time_ms", "execution_time_ms",
        }
    )
    for name in ("auth", "preflight", "upsert"):
        plan = _mapping(plans.get(name))
        if not plan:
            continue
        projected: dict[str, Any] = {}
        nodes = _project_counter(plan.get("node_type_counts"), allowed_nodes)
        if nodes:
            projected["node_type_counts"] = nodes
        for key in numeric_keys:
            number = _copy_number(plan, key)
            if number is not None:
                projected[key] = number
        if projected:
            plan_output[name] = projected
    if plan_output:
        output["plans"] = plan_output
    return output


def _project_observer(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {
        "schema": 1,
        "public_projection": True,
        "authoritative": False,
    }
    available = _copy_bool(source, "available")
    if available is None:
        available = any(
            isinstance(source.get(key), dict) and bool(source.get(key))
            for key in (
                "system",
                "server_request_perf_logs",
                "server_ssr_observability",
                "cpu_profile",
                "postgres_stat_statements",
                "postgres_explain",
            )
        )
    output["available"] = available
    for key in ("stop_file_seen", "timed_out"):
        flag = _copy_bool(source, key)
        if flag is not None:
            output[key] = flag
    system = _project_system(source.get("system"))
    if system:
        output["system"] = system
    request_perf = _project_summary(source.get("server_request_perf_logs"))
    if request_perf:
        output["server_request_perf_logs"] = request_perf
    ssr = _project_ssr(source.get("server_ssr_observability"))
    if ssr:
        output["server_ssr_observability"] = ssr
    cpu_profile = _project_cpu_profile(source.get("cpu_profile"))
    if cpu_profile:
        output["cpu_profile"] = cpu_profile
    statements = _mapping(source.get("postgres_stat_statements"))
    statement_output: dict[str, Any] = {}
    for key in ("before", "after", "delta"):
        projected = _project_statement_snapshot(statements.get(key))
        if projected:
            statement_output[key] = projected
    if statement_output:
        output["postgres_stat_statements"] = statement_output
    explain = _project_plan(source.get("postgres_explain"))
    if explain:
        output["postgres_explain"] = explain
    return output


def _project_timeout_row(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {
        "observation_index": _copy_int(source, "observation_index") or 0,
        "classification": (
            source.get("classification")
            if _allowed_string(
                source.get("classification"), PUBLIC_TIMEOUT_CLASSIFICATIONS
            )
            is not None
            else "other"
        ),
        "classification_reason_class": (
            source.get("classification_reason_class")
            if _allowed_string(
                source.get("classification_reason_class"),
                PUBLIC_TIMEOUT_REASON_CLASSES,
            )
            is not None
            else "other"
        ),
    }
    for key in (
        "server_completed_at_or_after_client_timeout",
        "upstream_completed_at_or_after_client_timeout",
        "origin_request_started_after_client_timeout",
        "origin_request_start_timing_ambiguous",
        "nearest_system_sample_available",
        "nearest_event_loop_sample_available",
    ):
        flag = _copy_bool(source, key)
        if flag is not None:
            output[key] = flag
    route_class = source.get("route_class")
    output["route_class"] = (
        route_class
        if _allowed_string(route_class, SAFE_ROUTE_CLASSES) is not None
        else "other"
    )
    output["method"] = safe_method(source.get("method"))
    status = _copy_status(source, "status")
    if status is not None:
        output["status"] = status
    if "error_class" in source:
        output["error_class"] = safe_error_class(
            source.get("error_class"), status=source.get("status")
        )
    request_completion = source.get("request_completion")
    if _allowed_string(request_completion, PUBLIC_REQUEST_COMPLETIONS) is not None:
        output["request_completion"] = request_completion
    for section_name in ("next", "ssr", "api"):
        section = _mapping(source.get(section_name))
        section_output: dict[str, Any] = {}
        for field in (
            "accepted",
            "upstream_completed",
            "started_observed",
            "call_started_observed",
            "call_completed_observed",
        ):
            flag = _copy_bool(section, field)
            if flag is not None:
                section_output[field] = flag
        for field in (
            "upstream_status",
            "request_perf_start_count",
            "stage_event_count",
            "stream_event_count",
            "request_perf_count",
        ):
            number = _copy_int(section, field)
            if number is not None:
                section_output[field] = number
        for field in (
            "request_time_ms",
            "upstream_connect_ms",
            "upstream_header_ms",
            "upstream_ms",
        ):
            number = _copy_number(section, field)
            if number is not None:
                section_output[field] = number
        if isinstance(section.get("stage_events"), list):
            section_output["stage_event_count"] = len(section["stage_events"])
        if isinstance(section.get("stream_events"), list):
            section_output["stream_event_count"] = len(section["stream_events"])
        if isinstance(section.get("request_perf"), list):
            section_output["request_perf_count"] = len(section["request_perf"])
        if section_output:
            output[section_name] = section_output
    client = _project_error_row(source.get("client")) if source.get("client") is not None else None
    if client:
        output["client"] = client
    origin = _mapping(source.get("origin"))
    if origin:
        origin_output: dict[str, Any] = {}
        for key in ("route_class", "method", "error_class", "request_completion"):
            raw = origin.get(key)
            if key == "route_class":
                origin_output[key] = (
                    raw if _allowed_string(raw, SAFE_ROUTE_CLASSES) is not None else "other"
                )
            elif key == "method":
                origin_output[key] = safe_method(raw)
            elif key == "error_class":
                origin_output[key] = safe_error_class(raw, status=origin.get("status"))
            elif _allowed_string(raw, PUBLIC_REQUEST_COMPLETIONS) is not None:
                origin_output[key] = raw
        for key in ("status",):
            number = _copy_status(origin, key)
            if number is not None:
                origin_output[key] = number
        for section_name in ("next", "ssr", "api"):
            section = _mapping(origin.get(section_name))
            section_output: dict[str, Any] = {}
            for field in (
                "accepted",
                "upstream_completed",
                "started_observed",
                "call_started_observed",
                "call_completed_observed",
            ):
                flag = _copy_bool(section, field)
                if flag is not None:
                    section_output[field] = flag
            for field in (
                "upstream_status",
                "request_perf_start_count",
                "stage_event_count",
                "stream_event_count",
            ):
                number = _copy_int(section, field)
                if number is not None:
                    section_output[field] = number
            for field in (
                "request_time_ms",
                "upstream_connect_ms",
                "upstream_header_ms",
                "upstream_ms",
            ):
                number = _copy_number(section, field)
                if number is not None:
                    section_output[field] = number
            if section_output:
                origin_output[section_name] = section_output
        if origin_output:
            output["origin"] = origin_output
    return output


def _project_timeout_collection(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {}
    for key in ("requested_ids", "nginx_records"):
        number = _copy_int(source, key)
        if number is not None:
            output[key] = number
    rows = source.get("rows")
    if isinstance(rows, list):
        output["rows"] = [_project_timeout_row(row) for row in rows[:1000] if isinstance(row, dict)]
    return output


def _project_timeout(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    output: dict[str, Any] = {
        "schema": 1,
        "public_projection": True,
        "authoritative": False,
        "kind": "timeout_path_diagnostics",
    }
    profile = _safe_profile_id(_mapping(source.get("load_contract")).get("profile_id"))
    if profile is None:
        profile = _safe_profile_id(source.get("profile_id"))
    if profile is not None:
        output["profile_id"] = profile
    status = source.get("status")
    if _allowed_string(
        status,
        frozenset({"complete", "incomplete", "failed", "not_requested", "unavailable", "other"}),
    ) is not None:
        output["status"] = status
    else:
        output["status"] = "other"
    summary = _mapping(source.get("summary"))
    summary_output: dict[str, Any] = {}
    for key in (
        "client_timeout_errors",
        "origin_rows",
        "nginx_matches",
        "next_accepted_matches",
        "api_completed_matches",
        "server_completed_at_or_after_client_timeout",
        "upstream_completed_at_or_after_client_timeout",
        "origin_requests_started_after_client_timeout",
        "origin_request_start_timing_ambiguous",
        "origin_completed_before_timeout",
        "origin_event_loop_samples",
    ):
        number = _copy_int(summary, key)
        if number is not None:
            summary_output[key] = number
    classes = _project_counter(summary.get("classifications"), PUBLIC_TIMEOUT_CLASSIFICATIONS)
    if classes:
        summary_output["classifications"] = classes
    if summary_output:
        output["summary"] = summary_output
    output["nginx_timeout_policy"] = {
        "proxy_connect_timeout_seconds": 5,
        "proxy_read_timeout_seconds": 30,
        "proxy_send_timeout_seconds": 30,
        "send_timeout_seconds": 30,
        "source": "canonical_nginx_timeout_policy",
    }
    rows = source.get("rows")
    if isinstance(rows, list):
        output["rows"] = [_project_timeout_row(row) for row in rows[:1000] if isinstance(row, dict)]
    return output


def project_public_artifact(artifact_type: str, value: Any) -> dict[str, Any]:
    """Project one named artifact through its closed public schema.

    This is deliberately not a recursive redactor.  Every producer has its
    own allowlisted shape; arbitrary keys, strings, lists and dynamic maps are
    dropped before the result can reach an artifact or step summary.
    """

    if not isinstance(artifact_type, str):
        raise ValueError("public artifact type is invalid")
    if artifact_type == "external_load":
        return _project_external_load(value)
    if artifact_type == "server_observability":
        return _project_observer(value)
    if artifact_type == "timeout_diagnostics":
        return _project_timeout(value)
    raise ValueError("unknown public artifact type")


def sanitized_log_summary(
    lines: Iterable[str] | Any,
    *,
    max_lines: int = 300,
    max_total_bytes: int = MAX_LOG_TOTAL_BYTES,
    max_line_bytes: int = MAX_LOG_LINE_BYTES,
) -> dict[str, Any]:
    """Reduce arbitrary process output to fixed counts and safe classifiers.

    The source is consumed through :class:`BoundedLineReader`; its tail is
    never read after a hard limit.  ``truncated`` is therefore an explicit
    signal that the aggregate is not complete.
    """

    counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    reader = BoundedLineReader(
        lines,
        max_lines=max_lines,
        max_total_bytes=max_total_bytes,
        max_line_bytes=max_line_bytes,
    )
    for line in reader:
        lowered = line.lower()
        # Prefer explicit fixed-schema status values over key names such as
        # ``error_class``.  A previously sanitized JSON line should not be
        # classified as a failure merely because its schema has an error
        # field whose value is ``none``.
        explicit_status = re.search(
            r'"(?:status|outcome)"\s*:\s*"([a-z_-]+)"', lowered
        )
        explicit_success = re.search(
            r'"(?:success|passed)"\s*:\s*(true|false)', lowered
        )
        explicit_value = explicit_status.group(1) if explicit_status else ""
        explicit_boolean = explicit_success.group(1) if explicit_success else ""
        if explicit_value in {"passed", "success", "complete", "completed", "ok"} or explicit_boolean == "true":
            counts["success"] += 1
        elif explicit_value in {"failed", "failure", "error", "timeout", "unavailable"} or explicit_boolean == "false":
            counts["timeout"] += 1 if explicit_value == "timeout" else 0
            counts["failure"] += 1 if explicit_value != "timeout" else 0
        elif (
            "timeout" in lowered
            or "timed out" in lowered
            or "error" in lowered
            or "fail" in lowered
            or "exception" in lowered
            or '"passed": false' in lowered
            or '"success": false' in lowered
        ):
            counts["timeout"] += 1 if "timeout" in lowered or "timed out" in lowered else 0
            counts["failure"] += 1 if "timeout" not in lowered and "timed out" not in lowered else 0
        elif "success" in lowered or "passed" in lowered or "complete" in lowered:
            counts["success"] += 1
        else:
            counts["other"] += 1
        route = "other"
        for candidate in SAFE_ROUTE_CLASSES:
            if candidate in lowered:
                route = candidate
                break
        route_counts[route] += 1
        status_match = re.search(r"\bstatus\s*[=:]\s*(\d{3})\b", lowered)
        if status_match:
            status_counts[str(safe_status(status_match.group(1)))] += 1
    if reader.error:
        status = "error"
    elif counts["failure"] or counts["timeout"]:
        status = "failed"
    elif reader.truncated:
        status = "truncated"
    elif counts["success"]:
        status = "passed"
    else:
        status = "empty" if not reader.line_count else "unknown"
    return {
        "schema": 1,
        "status": status,
        "line_count": reader.line_count,
        "retained_line_count": reader.line_count,
        "truncated": reader.truncated,
        "class_counts": dict(sorted(counts.items())),
        "route_class_counts": dict(sorted(route_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
    }


def sanitize_log_file(source: Path, destination: Path) -> dict[str, Any]:
    try:
        with source.open("rb") as handle:
            summary = sanitized_log_summary(handle)
    except OSError:
        summary = {
            "schema": 1,
            "status": "unavailable",
            "line_count": 0,
            "retained_line_count": 0,
            "truncated": False,
            "class_counts": {"other": 1},
            "route_class_counts": {"other": 1},
            "status_counts": {},
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sanitize process output into fixed evidence.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--json",
        action="store_true",
        help="sanitize a finished JSON evidence tree instead of summarizing text",
    )
    parser.add_argument(
        "--artifact-type",
        help="closed public schema to use with --json",
    )
    args = parser.parse_args()
    if args.json:
        if args.artifact_type is None:
            parser.error("--artifact-type is required with --json")
        if args.artifact_type not in PUBLIC_ARTIFACT_TYPES:
            # Do not let argparse include a caller-controlled artifact type in
            # its diagnostic; this command is also used at a privacy boundary.
            parser.error("unknown public artifact type")
        raw_json = _read_bounded_json(args.input)
        try:
            payload = json.loads(raw_json) if raw_json is not None else {}
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            # A malformed/private input must fail closed to the producer's
            # empty public schema; never preserve an error string from the
            # parser as evidence.
            payload = {}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                sanitize_public_load_report(payload, args.artifact_type),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    elif args.artifact_type is not None:
        parser.error("--artifact-type requires --json")
    else:
        sanitize_log_file(args.input, args.output)
