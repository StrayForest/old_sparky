#!/usr/bin/env python3
"""Join bounded client timeout evidence with the matching origin observations.

This tool is diagnostic-only.  It never makes requests, changes runtime state,
or treats a missing layer record as proof that the layer was healthy.  The
client report is the complete timeout population; the origin report contains
only the bounded rows selected by the timeout diagnostic IDs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any


DIAGNOSTIC_ID_RE = re.compile(r"^tdiag-[0-9]{1,32}-[0-9]{5}$")
NGINX_TIMEOUT_POLICY = {
    "proxy_connect_timeout_seconds": 5,
    "proxy_read_timeout_seconds": 30,
    "proxy_send_timeout_seconds": 30,
    "send_timeout_seconds": 30,
    "source": "platform/deploy/nginx/deadlock-platform.conf",
}
# ``$time_iso8601`` in the access log has second-level precision.  Keep a
# one-second safety margin before claiming that the origin request started
# after the client timeout.
NGINX_TIMESTAMP_PRECISION_MS = 1_000.0


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def nearest_sample(samples: object, target: datetime | None) -> dict[str, Any] | None:
    if target is None or not isinstance(samples, list):
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        timestamp = parse_timestamp(sample.get("timestamp"))
        if timestamp is None:
            continue
        candidates.append((abs((timestamp - target).total_seconds()), sample))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def nearest_event_loop(samples: object, target: datetime | None) -> dict[str, Any] | None:
    if target is None or not isinstance(samples, list):
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        timestamp = parse_timestamp(sample.get("journal_timestamp"))
        if timestamp is None:
            continue
        candidates.append((abs((timestamp - target).total_seconds()), sample))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def safe_system_sample(sample: dict[str, Any] | None) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        key: sample[key]
        for key in (
            "timestamp",
            "cpu_per_core_percent",
            "postgres_cpu_percent",
            "tcp_socket_states",
            "tcp_listen_counters",
            "postgres_backend_connections",
            "postgres_backend_ownership",
            "postgres_waits",
            "api_process",
            "web_process",
            "process_lifecycle",
        )
        if key in sample
    }


def classify_timeout(
    server_row: dict[str, Any] | None,
    *,
    origin_request_started_after_client_timeout: bool = False,
    origin_completed_after_client_timeout: bool = False,
    origin_completed_before_client_timeout: bool = False,
) -> tuple[str, str]:
    if server_row is None:
        return (
            "before_origin_observed",
            "No matching Nginx access record was observed for this diagnostic ID.",
        )
    next_row = server_row.get("next") if isinstance(server_row.get("next"), dict) else {}
    ssr_row = server_row.get("ssr") if isinstance(server_row.get("ssr"), dict) else {}
    api_row = server_row.get("api") if isinstance(server_row.get("api"), dict) else {}
    if origin_request_started_after_client_timeout and next_row.get("upstream_completed"):
        return (
            "client_or_edge_before_origin",
            "The client timed out before the origin request could have started; "
            "Nginx later completed the correlated upstream request.",
        )
    if origin_completed_after_client_timeout and next_row.get("upstream_completed"):
        return (
            "origin_completion_after_client_timeout",
            "Nginx recorded a completed upstream request after the client timeout, "
            "but access-log precision leaves the origin start ordering ambiguous.",
        )
    if origin_completed_before_client_timeout and next_row.get("upstream_completed"):
        return (
            "client_or_edge_after_origin",
            "Nginx recorded a completed upstream request before the client timeout, "
            "but the client did not receive an HTTP response.",
        )
    if not next_row.get("accepted"):
        return (
            "nginx_or_edge_before_next",
            "Nginx recorded the request without an observed Next.js upstream connection.",
        )
    if api_row.get("call_completed_observed"):
        return (
            "api_or_next_after_api",
            "The request reached Next.js and a correlated API request_perf row completed.",
        )
    if api_row.get("call_started_observed"):
        return (
            "api_or_next_incomplete",
            "The request reached Next.js and the API accepted a correlated request, but no completion row was logged.",
        )
    if ssr_row.get("started_observed"):
        return (
            "next_ssr_or_node_queue",
            "The request reached Next.js and emitted SSR/stream evidence, but no correlated API completion was logged.",
        )
    return (
        "next_accept_or_node_queue",
        "Nginx connected to Next.js, but no SSR/stream journal event was observed.",
    )


def join_reports(client: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    client_summary = client.get("overall") or client.get("raw_http") or {}
    client_rows = [
        row
        for row in client_summary.get("timeout_diagnostics") or []
        if isinstance(row, dict)
        and isinstance(row.get("diagnostic_id"), str)
        and DIAGNOSTIC_ID_RE.fullmatch(row["diagnostic_id"])
        and row.get("error_kind") == "TimeoutError"
    ]
    server_section = server.get("server_ssr_observability") or {}
    server_rows = server_section.get("timeout_diagnostics") or {}
    origin_rows = {
        row["diagnostic_id"]: row
        for row in server_rows.get("rows") or []
        if isinstance(row, dict)
        and isinstance(row.get("diagnostic_id"), str)
        and DIAGNOSTIC_ID_RE.fullmatch(row["diagnostic_id"])
    }
    system = server.get("system") or {}
    system_timeline = system.get("timeline") if isinstance(system, dict) else []
    event_loop = server_section.get("event_loop") or {}
    event_loop_samples = event_loop.get("samples_detail") if isinstance(event_loop, dict) else []

    joined_rows: list[dict[str, Any]] = []
    classifications: Counter[str] = Counter()
    nginx_matches = 0
    next_matches = 0
    api_matches = 0
    post_timeout_completions = 0
    upstream_post_timeout_completions = 0
    origin_started_after_timeout = 0
    origin_start_timing_ambiguous = 0
    origin_completed_before_timeout = 0
    for client_row in client_rows:
        diagnostic_id = str(client_row["diagnostic_id"])
        origin_row = origin_rows.get(diagnostic_id)
        timeout_at = parse_timestamp(client_row.get("exception_at") or client_row.get("finished_at"))
        server_timestamp = parse_timestamp(origin_row.get("nginx_recorded_at")) if origin_row else None
        server_after_timeout = bool(
            timeout_at is not None
            and server_timestamp is not None
            and server_timestamp >= timeout_at
        )
        next_row = origin_row.get("next") if origin_row and isinstance(origin_row.get("next"), dict) else {}
        request_time_ms = next_row.get("request_time_ms")
        origin_start_delta_ms: float | None = None
        if timeout_at is not None and server_timestamp is not None and isinstance(request_time_ms, (int, float)):
            origin_start_delta_ms = (
                (server_timestamp - timeout_at).total_seconds() * 1000
                - float(request_time_ms)
            )
        origin_request_started_after_client_timeout = bool(
            origin_start_delta_ms is not None
            and origin_start_delta_ms >= NGINX_TIMESTAMP_PRECISION_MS
        )
        origin_start_is_ambiguous = bool(
            origin_start_delta_ms is not None
            and -NGINX_TIMESTAMP_PRECISION_MS < origin_start_delta_ms < NGINX_TIMESTAMP_PRECISION_MS
        )
        origin_timestamp_before_timeout = bool(
            server_timestamp is not None
            and timeout_at is not None
            and server_timestamp < timeout_at
        )
        if origin_request_started_after_client_timeout:
            origin_started_after_timeout += 1
        elif origin_start_is_ambiguous:
            origin_start_timing_ambiguous += 1
        elif origin_timestamp_before_timeout:
            origin_completed_before_timeout += 1
        classification, reason = classify_timeout(
            origin_row,
            origin_request_started_after_client_timeout=origin_request_started_after_client_timeout,
            origin_completed_after_client_timeout=server_after_timeout,
            origin_completed_before_client_timeout=origin_timestamp_before_timeout,
        )
        classifications[classification] += 1
        if origin_row is not None:
            nginx_matches += 1
        if next_row.get("accepted"):
            next_matches += 1
        api_row = origin_row.get("api") if origin_row and isinstance(origin_row.get("api"), dict) else {}
        if api_row.get("call_completed_observed"):
            api_matches += 1
        if server_after_timeout:
            post_timeout_completions += 1
        upstream_completed_after_timeout = bool(
            server_after_timeout
            and isinstance(next_row, dict)
            and next_row.get("upstream_completed")
        )
        if upstream_completed_after_timeout:
            upstream_post_timeout_completions += 1
        system_sample = nearest_sample(system_timeline, timeout_at)
        event_loop_sample = nearest_event_loop(event_loop_samples, timeout_at)
        joined_rows.append(
            {
                "diagnostic_id": diagnostic_id,
                "client": client_row,
                "classification": classification,
                "classification_reason": reason,
                "origin": origin_row,
                "server_completed_at_or_after_client_timeout": server_after_timeout,
                "upstream_completed_at_or_after_client_timeout": upstream_completed_after_timeout,
                "estimated_origin_request_start_delta_ms": (
                    round(origin_start_delta_ms, 3)
                    if origin_start_delta_ms is not None
                    else None
                ),
                "origin_request_started_after_client_timeout": origin_request_started_after_client_timeout,
                "origin_request_start_timing_ambiguous": origin_start_is_ambiguous,
                "nearest_system_sample": safe_system_sample(system_sample),
                "nearest_event_loop_sample": event_loop_sample,
            }
        )

    return {
        "schema": 1,
        "kind": "timeout_path_diagnostics",
        "source_git_sha": client.get("source_git_sha"),
        "profile_id": (client.get("load_contract") or {}).get("profile_id"),
        "load_window": {
            "client_started_at": client.get("started_at"),
            "client_finished_at": client.get("finished_at"),
            "origin_started_at": server.get("started_at"),
            "origin_finished_at": server.get("finished_at"),
            "timestamps": "UTC; runner/origin clock alignment is assumed from host time sync",
        },
        "summary": {
            "client_timeout_errors": len(client_rows),
            "origin_rows": len(origin_rows),
            "nginx_matches": nginx_matches,
            "next_accepted_matches": next_matches,
            "api_completed_matches": api_matches,
            "server_completed_at_or_after_client_timeout": post_timeout_completions,
            "upstream_completed_at_or_after_client_timeout": upstream_post_timeout_completions,
            "origin_requests_started_after_client_timeout": origin_started_after_timeout,
            "origin_request_start_timing_ambiguous": origin_start_timing_ambiguous,
            "origin_completed_before_client_timeout": origin_completed_before_timeout,
            "classifications": dict(sorted(classifications.items())),
            "origin_event_loop_samples": event_loop.get("samples", 0) if isinstance(event_loop, dict) else 0,
        },
        "nginx_timeout_policy": NGINX_TIMEOUT_POLICY,
        "rows": joined_rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Join bounded timeout-path evidence.")
    parser.add_argument("--client-report", type=Path, required=True)
    parser.add_argument("--server-observability", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = join_reports(load_object(args.client_report), load_object(args.server_observability))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
