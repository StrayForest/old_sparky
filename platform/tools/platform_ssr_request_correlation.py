"""Private, bounded join for authenticated HTML and its server-side calls.

The caller owns collection and sampling. This module accepts only the selected
client request IDs and bounded server rows, joins them transiently, and returns
low-cardinality aggregates without returning any correlation key.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import itertools
import math
import re
from typing import Any, Iterable, Mapping


REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_CONTEXT_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
CF_RAY_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
DEFAULT_MAX_ROWS = 200_000
DEFAULT_SAMPLE_LIMIT = 1_000
MAX_METRIC_VALUE = 1_000_000_000.0
SELECTION_SCHEME = "sha256-runid-requestid-1of8-v1"
KNOWN_SSR_STAGES = frozenset(
    {
        "auth_bootstrap_fetch",
        "tournament_workspace",
        "tournament_detail_data_ready",
        "http_request_start",
        "root_layout",
        "page_component",
        "page_render",
        "proxy_start",
        "proxy_to_root_layout_start",
        "request_to_proxy",
    }
)
KNOWN_STREAM_MILESTONES = frozenset(
    {
        "request_start",
        "response_stream_start",
        "first_body_write_attempt",
        "response_finish",
        "response_close",
        "response_error",
    }
)
KNOWN_API_ROUTES = frozenset({"auth_bootstrap", "tournament_workspace"})


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > MAX_METRIC_VALUE:
        return None
    return number


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _metric(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    ordered = sorted(values)

    def percentile(percent: float) -> float:
        rank = (len(ordered) - 1) * percent / 100
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "avg_ms": round(sum(ordered) / len(ordered), 3),
        "p50_ms": round(percentile(50), 3),
        "p95_ms": round(percentile(95), 3),
        "p99_ms": round(percentile(99), 3),
        "max_ms": round(ordered[-1], 3),
    }


def _scalar_metric(values: list[float]) -> dict[str, float | int | None]:
    """Summarize dimensionless SQL-count values without millisecond labels."""

    if not values:
        return {
            "count": 0,
            "avg": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    ordered = sorted(values)

    def percentile(percent: float) -> float:
        rank = (len(ordered) - 1) * percent / 100
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "avg": round(sum(ordered) / len(ordered), 3),
        "p50": round(percentile(50), 3),
        "p95": round(percentile(95), 3),
        "p99": round(percentile(99), 3),
        "max": round(ordered[-1], 3),
    }


def _bounded_rows(
    rows: Iterable[Mapping[str, Any]], limit: int
) -> tuple[list[Mapping[str, Any]], bool]:
    selected = list(itertools.islice(iter(rows), limit + 1))
    truncated = len(selected) > limit
    return selected[:limit], truncated


def _context_valid(
    row: Mapping[str, Any],
    *,
    run_id: str,
    phase: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[bool, str]:
    if row.get("run_id") != run_id or row.get("phase") != phase:
        return False, "context_mismatch"
    if row.get("window_start_utc") != window_start.isoformat() or row.get(
        "window_end_utc"
    ) != window_end.isoformat():
        # Accept the caller's equivalent Z spelling only after parsing it.
        row_start = _timestamp(row.get("window_start_utc"))
        row_end = _timestamp(row.get("window_end_utc"))
        if row_start != window_start or row_end != window_end:
            return False, "context_mismatch"
    event_time = _timestamp(row.get("event_at_utc"))
    if event_time is None:
        return False, "missing_or_invalid_timestamp"
    if event_time < window_start or event_time > window_end:
        return False, "outside_window"
    return True, "ok"


def _safe_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def response_id_selection_digest(run_id: str, request_id: str) -> str | None:
    """Return the full SHA-256 rank for an eligible-format response ID."""

    if (
        not isinstance(run_id, str)
        or not isinstance(request_id, str)
        or not REQUEST_ID_RE.fullmatch(request_id)
    ):
        return None
    try:
        raw = f"{run_id}:{request_id}".encode("ascii")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(raw).hexdigest()


def is_eligible_response_id(run_id: str, request_id: str) -> bool:
    """Apply the shared deterministic 1-in-8 diagnostic sampling predicate."""

    digest = response_id_selection_digest(run_id, request_id)
    return digest is not None and (int(digest[:2], 16) & 7) == 0


def summarize_ssr_request_correlation(
    *,
    selected_request_ids: Iterable[object],
    submitted_html_count: int,
    valid_response_id_count: int,
    eligible_response_id_count: int,
    selection_scheme: str,
    missing_response_id_count: int | None = None,
    duplicate_response_id_count: int = 0,
    malformed_response_id_count: int = 0,
    run_id: str,
    phase: str,
    window_start_utc: str,
    window_end_utc: str,
    nginx_rows: Iterable[Mapping[str, Any]],
    ssr_rows: Iterable[Mapping[str, Any]],
    stream_rows: Iterable[Mapping[str, Any]],
    api_rows: Iterable[Mapping[str, Any]],
    rows_truncated: bool | Mapping[str, bool] = False,
    max_rows: int = DEFAULT_MAX_ROWS,
    sample_limit: int = DEFAULT_SAMPLE_LIMIT,
    cf_ray_fallback_proven: bool = False,
) -> dict[str, Any]:
    """Join sampled HTML requests to Nginx, SSR, and all matching API calls.

    Each source row must carry ``run_id``, ``phase``, ``window_start_utc``,
    ``window_end_utc`` and an aware ``event_at_utc``. Request IDs are strict
    lowercase 32-hex tokens. CF-Ray fallback is disabled unless its exact
    equality contract has been independently established by the caller.

    The ``selected_request_ids`` iterable is expected to be the caller's
    deterministic lowest-rank sample across all eligible singleton response
    IDs, regardless of outcome or latency. The helper validates each selected
    ID against the shared 1-in-8 predicate, but cannot independently prove the
    producer selected the globally lowest ranks when the eligible population
    exceeds ``sample_limit``. Rows are bounded again here even if collection
    already applied a limit. Timing rows outside the capture window or without
    a valid timestamp are excluded from joins and reduce coverage. Stream
    offsets are separate from SSR stage durations; a first-body-write attempt
    is not evidence that bytes reached the client. The caller supplies
    ``valid_response_id_count`` as the number of IDs belonging to exactly one
    client request; every request involved in a duplicate-ID group belongs in
    ``duplicate_response_id_count`` and is excluded from the selected sample.
    """

    if not isinstance(run_id, str) or not SAFE_CONTEXT_RE.fullmatch(run_id):
        raise ValueError("run_id is invalid")
    if not isinstance(phase, str) or not SAFE_CONTEXT_RE.fullmatch(phase):
        raise ValueError("phase is invalid")
    if selection_scheme != SELECTION_SCHEME:
        raise ValueError("selection_scheme is unsupported")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        raise ValueError("max_rows must be positive")
    if not isinstance(sample_limit, int) or isinstance(sample_limit, bool) or sample_limit < 1:
        raise ValueError("sample_limit must be positive")
    submitted = _safe_count(submitted_html_count)
    valid_ids = _safe_count(valid_response_id_count)
    eligible_ids = _safe_count(eligible_response_id_count)
    duplicate_ids = _safe_count(duplicate_response_id_count)
    malformed_ids = _safe_count(malformed_response_id_count)
    if (
        submitted is None
        or valid_ids is None
        or eligible_ids is None
        or duplicate_ids is None
        or malformed_ids is None
        or eligible_ids > valid_ids
        or valid_ids + duplicate_ids + malformed_ids > submitted
    ):
        raise ValueError("cohort counts are invalid")
    missing_ids = (
        submitted - valid_ids - duplicate_ids - malformed_ids
        if missing_response_id_count is None
        else _safe_count(missing_response_id_count)
    )
    if (
        missing_ids is None
        or valid_ids + missing_ids + duplicate_ids + malformed_ids != submitted
    ):
        raise ValueError("cohort response-ID counts must cover every submitted request")
    window_start = _timestamp(window_start_utc)
    window_end = _timestamp(window_end_utc)
    if window_start is None or window_end is None or window_end < window_start:
        raise ValueError("capture window is invalid")

    raw_sample, selected_truncated = _bounded_rows(selected_request_ids, sample_limit + 1)
    selected_truncated = selected_truncated or len(raw_sample) > sample_limit
    raw_sample = raw_sample[:sample_limit]
    selected_ids: set[str] = set()
    malformed_sample_ids = 0
    ineligible_sample_ids = 0
    duplicate_sample_ids = 0
    for value in raw_sample:
        if not isinstance(value, str) or not REQUEST_ID_RE.fullmatch(value):
            malformed_sample_ids += 1
            continue
        if not is_eligible_response_id(run_id, value):
            ineligible_sample_ids += 1
            continue
        if value in selected_ids:
            duplicate_sample_ids += 1
            continue
        selected_ids.add(value)

    expected_sample_count = min(eligible_ids, sample_limit)
    sample_complete = (
        not selected_truncated
        and len(selected_ids) == expected_sample_count
        and malformed_sample_ids == 0
        and ineligible_sample_ids == 0
        and duplicate_sample_ids == 0
    )

    sources = {
        "nginx": nginx_rows,
        "ssr": ssr_rows,
        "stream": stream_rows,
        "api": api_rows,
    }
    bounded: dict[str, list[Mapping[str, Any]]] = {}
    source_truncated: dict[str, bool] = {}
    source_rejections: dict[str, Counter[str]] = {}
    for name, rows in sources.items():
        bounded[name], local_truncated = _bounded_rows(rows, max_rows)
        declared = (
            bool(rows_truncated.get(name, False))
            if isinstance(rows_truncated, Mapping)
            else bool(rows_truncated)
        )
        source_truncated[name] = local_truncated or declared
        source_rejections[name] = Counter()

    valid_rows: dict[str, list[Mapping[str, Any]]] = {name: [] for name in sources}
    for name, rows in bounded.items():
        for row in rows:
            valid, reason = _context_valid(
                row,
                run_id=run_id,
                phase=phase,
                window_start=window_start,
                window_end=window_end,
            )
            if not valid:
                source_rejections[name][reason] += 1
                continue
            request_id = row.get("request_id")
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                source_rejections[name]["malformed_request_id"] += 1
                continue
            valid_rows[name].append(row)

    nginx_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows["nginx"]:
        nginx_by_id[str(row["request_id"])].append(row)
    duplicate_nginx_ids = sum(1 for rows in nginx_by_id.values() if len(rows) > 1)

    ssr_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows["ssr"]:
        ssr_by_id[str(row["request_id"])].append(row)

    stream_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows["stream"]:
        stream_by_id[str(row["request_id"])].append(row)

    api_by_id: dict[str, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    api_by_cf_ray: dict[str, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(valid_rows["api"]):
        api_by_id[str(row["request_id"])].append((index, row))
        cf_ray = row.get("cf_ray")
        if isinstance(cf_ray, str) and CF_RAY_RE.fullmatch(cf_ray):
            api_by_cf_ray[cf_ray].append((index, row))

    matched_nginx_ids: set[str] = set()
    matched_ssr_ids: set[str] = set()
    matched_stream_ids: set[str] = set()
    matched_api_indexes: set[int] = set()
    matched_api_parent_ids: set[str] = set()
    matched_api_associations = 0
    api_join_by_request_id = 0
    api_join_by_cf_ray = 0
    nginx_times: list[float] = []
    ssr_by_stage: dict[str, list[float]] = defaultdict(list)
    stream_by_milestone: dict[str, list[float]] = defaultdict(list)
    stream_milestone_missing: Counter[str] = Counter()
    stream_milestone_unknown = 0
    api_by_route: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    api_route_row_count: Counter[str] = Counter()
    api_metric_missing: dict[str, Counter[str]] = defaultdict(Counter)
    for request_id in selected_ids:
        parents = nginx_by_id.get(request_id, [])
        if len(parents) != 1:
            continue
        matched_nginx_ids.add(request_id)
        parent = parents[0]
        request_time = _finite_nonnegative(parent.get("request_time_ms"))
        if request_time is not None:
            nginx_times.append(request_time)

        ssr_events = ssr_by_id.get(request_id, [])
        stream_events = stream_by_id.get(request_id, [])
        if stream_events:
            matched_stream_ids.add(request_id)
        if ssr_events or stream_events:
            matched_ssr_ids.add(request_id)
        for event in stream_events:
            milestone = event.get("stage")
            if not isinstance(milestone, str) or milestone not in KNOWN_STREAM_MILESTONES:
                stream_milestone_unknown += 1
                continue
            offset = _finite_nonnegative(event.get("elapsed_ms"))
            if offset is None:
                stream_milestone_missing[milestone] += 1
            else:
                stream_by_milestone[milestone].append(offset)
        if ssr_events:
            for event in ssr_events:
                stage = event.get("stage")
                duration = _finite_nonnegative(event.get("duration_ms"))
                if isinstance(stage, str) and stage in KNOWN_SSR_STAGES and duration is not None:
                    ssr_by_stage[stage].append(duration)

        # Require the client→SSR edge before accepting the SSR→API edge.
        if not ssr_events and not stream_events:
            continue

        api_events = api_by_id.get(request_id, [])
        if api_events:
            api_join_by_request_id += len(api_events)
        elif cf_ray_fallback_proven:
            cf_ray = parent.get("cf_ray")
            if isinstance(cf_ray, str) and CF_RAY_RE.fullmatch(cf_ray):
                api_events = api_by_cf_ray.get(cf_ray, [])
                if api_events:
                    api_join_by_cf_ray += len(api_events)
        if api_events:
            matched_api_parent_ids.add(request_id)
            matched_api_associations += len(api_events)
        for api_index, event in api_events:
            matched_api_indexes.add(api_index)
            route = event.get("route_class")
            route_key = route if isinstance(route, str) and route in KNOWN_API_ROUTES else "other"
            api_route_row_count[route_key] += 1
            for field in (
                "total_ms",
                "sql_ms",
                "max_sql_ms",
                "pool_checkout_wait_ms",
                "pool_connection_hold_ms",
                "compute_ms",
                "sql_count",
            ):
                value = _finite_nonnegative(event.get(field))
                if (
                    field == "sql_count"
                    and value is not None
                    and not value.is_integer()
                ):
                    value = None
                if value is None:
                    api_metric_missing[route_key][field] += 1
                else:
                    api_by_route[route_key][field].append(value)

    source_input_rows = {name: len(rows) for name, rows in bounded.items()}
    all_sources_complete = not any(source_truncated.values())
    all_context_rows_valid = all(not counts for counts in source_rejections.values())
    full_sample_coverage = (
        sample_complete
        and all_sources_complete
        and all_context_rows_valid
        and len(matched_nginx_ids) == len(selected_ids)
        and len(matched_ssr_ids) == len(selected_ids)
        and len(matched_stream_ids) == len(selected_ids)
        and len(matched_api_parent_ids) == len(selected_ids)
        and not stream_milestone_missing
        and stream_milestone_unknown == 0
    )
    valid_id_coverage = (100.0 * valid_ids / submitted) if submitted else 0.0

    return {
        "schema_version": 1,
        "phase": phase,
        "cohort": {
            "submitted_html_requests": submitted,
            "valid_response_ids": valid_ids,
            "eligible_response_ids": eligible_ids,
            "missing_response_ids": missing_ids,
            "duplicate_response_ids": duplicate_ids,
            "malformed_response_ids": malformed_ids,
            "valid_response_id_coverage_percent": round(valid_id_coverage, 3),
            "selected_sample_limit": sample_limit,
            "selection_scheme": selection_scheme,
            "selected_sample_expected": expected_sample_count,
            "selected_sample_received": len(raw_sample),
            "selected_sample_valid": len(selected_ids),
            "selected_sample_malformed_ids": malformed_sample_ids,
            "selected_sample_ineligible_ids": ineligible_sample_ids,
            "selected_sample_duplicate_ids": duplicate_sample_ids,
            "selected_sample_complete": sample_complete,
        },
        "join": {
            "matched_nginx_parents": len(matched_nginx_ids),
            "matched_ssr_parents": len(matched_ssr_ids),
            "matched_stream_parents": len(matched_stream_ids),
            "matched_api_rows": matched_api_associations,
            "matched_api_by_request_id": api_join_by_request_id,
            "matched_api_by_cf_ray": api_join_by_cf_ray,
            "unmatched_selected_to_nginx": len(selected_ids - matched_nginx_ids),
            "unmatched_selected_to_ssr": len(matched_nginx_ids - matched_ssr_ids),
            "unmatched_selected_to_stream": len(matched_nginx_ids - matched_stream_ids),
            "unmatched_selected_to_api": len(matched_nginx_ids - matched_api_parent_ids),
            "orphan_ssr_rows": sum(
                1 for row in valid_rows["ssr"] if str(row["request_id"]) not in matched_nginx_ids
            ),
            "orphan_stream_rows": sum(
                1 for row in valid_rows["stream"] if str(row["request_id"]) not in matched_nginx_ids
            ),
            "orphan_api_rows": len(valid_rows["api"]) - len(matched_api_indexes),
            "duplicate_nginx_request_ids": duplicate_nginx_ids,
        },
        "timing_ms": {
            "nginx_request": _metric(nginx_times),
            "ssr_stage": {
                stage: _metric(values)
                for stage, values in sorted(ssr_by_stage.items())
            },
            "ssr_stream_milestone_elapsed_ms": {
                milestone: _metric(stream_by_milestone.get(milestone, []))
                for milestone in sorted(KNOWN_STREAM_MILESTONES)
            },
            "stream_milestone_missing_or_invalid": dict(
                sorted(stream_milestone_missing.items())
            ),
            "stream_milestone_unknown_rows": stream_milestone_unknown,
            "api_request_by_route": {
                route: {
                    field: (
                        _scalar_metric(values)
                        if field == "sql_count"
                        else _metric(values)
                    )
                    for field in (
                        "total_ms",
                        "sql_ms",
                        "max_sql_ms",
                        "pool_checkout_wait_ms",
                        "pool_connection_hold_ms",
                        "compute_ms",
                        "sql_count",
                    )
                    for values in (metrics.get(field, []),)
                }
                for route in sorted(set(api_route_row_count) | set(api_by_route))
                for metrics in (api_by_route.get(route, {}),)
            },
            "api_request_rows_by_route": dict(sorted(api_route_row_count.items())),
            "api_metric_missing_or_invalid_by_route": {
                route: dict(sorted(fields.items()))
                for route, fields in sorted(api_metric_missing.items())
            },
        },
        "coverage": {
            "complete": full_sample_coverage,
            "scope": "selected_sample_only",
            "selection_rank_proof": (
                "all_eligible_ids_selected"
                if eligible_ids <= sample_limit
                else "producer_asserted_not_verifiable_from_selected_ids"
            ),
            "full_cohort": (
                full_sample_coverage
                and len(selected_ids) == submitted
                and valid_ids == submitted
            ),
            "source_rows_truncated": source_truncated,
            "source_input_rows": source_input_rows,
            "source_rejections": {
                name: dict(sorted(counts.items()))
                for name, counts in source_rejections.items()
            },
            "cf_ray_fallback_enabled": bool(cf_ray_fallback_proven),
        },
    }
