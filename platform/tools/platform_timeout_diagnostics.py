#!/usr/bin/env python3
"""Join bounded client timeout evidence with bounded origin observations.

This tool is diagnostic-only.  It never makes requests, changes runtime state,
or treats a missing layer record as proof that the layer was healthy.  The
client report is the complete timeout population; the origin report contains
only the bounded rows selected for the same diagnostic window.  Correlation
identifiers are intentionally not required or emitted.  Rows are paired by
their bounded observation order and all public fields pass through the same
closed route/status/error schema used by the load producers.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any

try:
    from tools.platform_evidence_sanitizer import (
        SAFE_ROUTE_CLASSES,
        finite_number,
        safe_cf_error_class,
        safe_error_class,
        safe_method,
        safe_phase,
        safe_route_class,
        safe_status,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_evidence_sanitizer import (
        SAFE_ROUTE_CLASSES,
        finite_number,
        safe_cf_error_class,
        safe_error_class,
        safe_method,
        safe_phase,
        safe_route_class,
        safe_status,
    )


NGINX_TIMEOUT_POLICY = {
    "proxy_connect_timeout_seconds": 5,
    "proxy_read_timeout_seconds": 30,
    "proxy_send_timeout_seconds": 30,
    "send_timeout_seconds": 30,
    "source": "canonical_nginx_timeout_policy",
}
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]{1,4}$")


def load_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("timeout evidence input is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("timeout evidence input must contain a JSON object")
    return payload


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
            "no_origin_observation",
        )
    next_row = server_row.get("next") if isinstance(server_row.get("next"), dict) else {}
    ssr_row = server_row.get("ssr") if isinstance(server_row.get("ssr"), dict) else {}
    api_row = server_row.get("api") if isinstance(server_row.get("api"), dict) else {}
    if origin_request_started_after_client_timeout and next_row.get("upstream_completed"):
        return (
            "client_or_edge_before_origin",
            "origin_started_after_timeout",
        )
    if origin_completed_after_client_timeout and next_row.get("upstream_completed"):
        return (
            "origin_completion_after_client_timeout",
            "origin_completed_after_timeout",
        )
    if origin_completed_before_client_timeout and next_row.get("upstream_completed"):
        return (
            "client_or_edge_after_origin",
            "origin_completed_before_timeout",
        )
    if not next_row.get("accepted"):
        return (
            "nginx_or_edge_before_next",
            "next_not_accepted",
        )
    if api_row.get("call_completed_observed"):
        return (
            "api_or_next_after_api",
            "api_completed",
        )
    if api_row.get("call_started_observed"):
        return (
            "api_or_next_incomplete",
            "api_incomplete",
        )
    if ssr_row.get("started_observed"):
        return (
            "next_ssr_or_node_queue",
            "ssr_observed",
        )
    return (
        "next_accept_or_node_queue",
        "next_accepted_without_ssr",
    )


def _safe_client_timeout_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    raw_error = row.get("error_class") or row.get("error_kind")
    error_class = safe_error_class(raw_error, status=row.get("status"))
    if error_class != "timeout":
        return None
    raw_route_class = row.get("route_class")
    route_class = (
        raw_route_class
        if isinstance(raw_route_class, str) and raw_route_class in SAFE_ROUTE_CLASSES
        else safe_route_class(row.get("path"))
    )
    output: dict[str, Any] = {
        "phase": safe_phase(row.get("phase")),
        "method": safe_method(row.get("method")),
        "route_class": route_class,
        "status": safe_status(row.get("status")),
        "error_class": "timeout",
    }
    for key in ("cf_error_class", "cf_error_origin_class"):
        value = row.get(key)
        if value is not None:
            output[key] = safe_cf_error_class(value)
    for key in ("ttfb_ms", "elapsed_ms"):
        value = finite_number(row.get(key))
        if value is not None:
            output[key] = value
    return output


def _safe_origin_timeout_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    next_row = row.get("next") if isinstance(row.get("next"), dict) else {}
    ssr_row = row.get("ssr") if isinstance(row.get("ssr"), dict) else {}
    api_row = row.get("api") if isinstance(row.get("api"), dict) else {}
    raw_route_class = row.get("route_class")
    route_class = (
        raw_route_class
        if isinstance(raw_route_class, str) and raw_route_class in SAFE_ROUTE_CLASSES
        else safe_route_class(row.get("uri"), page=True)
    )
    output: dict[str, Any] = {
        "route_class": route_class,
        "method": safe_method(row.get("method")),
        "status": safe_status(row.get("status")),
        "error_class": "timeout",
        "request_completion": str(row.get("request_completion") or "other")
        if str(row.get("request_completion") or "other")
        in {"completed", "timeout", "aborted", "other"}
        else "other",
        "next": {
            "accepted": next_row.get("accepted") is True,
            "upstream_status": safe_status(next_row.get("upstream_status")),
            "upstream_completed": next_row.get("upstream_completed") is True,
        },
        "ssr": {
            "started_observed": ssr_row.get("started_observed") is True,
            "stage_event_count": len(ssr_row.get("stage_events") or []) if isinstance(ssr_row.get("stage_events"), list) else 0,
            "stream_event_count": len(ssr_row.get("stream_events") or []) if isinstance(ssr_row.get("stream_events"), list) else 0,
        },
        "api": {
            "call_started_observed": api_row.get("call_started_observed") is True,
            "request_perf_start_count": max(0, int(api_row.get("request_perf_start_count") or 0)),
            "call_completed_observed": api_row.get("call_completed_observed") is True,
        },
    }
    for source_key, output_key in (
        ("request_time_ms", "request_time_ms"),
        ("upstream_connect_ms", "upstream_connect_ms"),
        ("upstream_header_ms", "upstream_header_ms"),
        ("upstream_ms", "upstream_ms"),
    ):
        value = finite_number(next_row.get(source_key))
        if value is not None:
            output["next"][output_key] = value
    return output


def join_reports(client: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(client, dict):
        client = {}
    if not isinstance(server, dict):
        server = {}
    client_summary = client.get("overall") or client.get("raw_http") or {}
    if not isinstance(client_summary, dict):
        client_summary = {}
    client_rows = [
        sanitized
        for row in client_summary.get("timeout_diagnostics") or []
        if (sanitized := _safe_client_timeout_row(row)) is not None
    ]
    server_section = server.get("server_ssr_observability") or {}
    if not isinstance(server_section, dict):
        server_section = {}
    server_timeout_section = server_section.get("timeout_diagnostics") or {}
    if not isinstance(server_timeout_section, dict):
        server_timeout_section = {}
    origin_rows = [
        sanitized
        for row in server_timeout_section.get("rows") or []
        if (sanitized := _safe_origin_timeout_row(row)) is not None
    ]
    system = server.get("system") or {}
    event_loop = server_section.get("event_loop") or {}

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
    # No request/diagnostic/correlation identifier is persisted.  Pair only
    # bounded rows by observation order; an absent origin row remains explicit
    # and cannot be interpreted as a successful origin response.
    for index, client_row in enumerate(client_rows):
        origin_row = origin_rows[index] if index < len(origin_rows) else None
        next_row = origin_row.get("next") if origin_row else {}
        api_row = origin_row.get("api") if origin_row else {}
        classification, reason_class = classify_timeout(
            origin_row,
            origin_request_started_after_client_timeout=False,
            origin_completed_after_client_timeout=False,
            origin_completed_before_client_timeout=False,
        )
        classifications[classification] += 1
        if origin_row is not None:
            nginx_matches += 1
        if next_row.get("accepted") is True:
            next_matches += 1
        if api_row.get("call_completed_observed") is True:
            api_matches += 1
        # Absolute ordering cannot be established after timestamps and IDs are
        # removed.  Keep these counters present and conservatively zero.
        joined_rows.append(
            {
                "observation_index": index,
                "client": client_row,
                "classification": classification,
                "classification_reason_class": reason_class,
                "origin": origin_row,
                "server_completed_at_or_after_client_timeout": False,
                "upstream_completed_at_or_after_client_timeout": False,
                "estimated_origin_request_start_delta_ms": None,
                "origin_request_started_after_client_timeout": False,
                "origin_request_start_timing_ambiguous": False,
                "nearest_system_sample_available": bool(system.get("timeline")) if isinstance(system, dict) else False,
                "nearest_event_loop_sample_available": bool(event_loop.get("samples")) if isinstance(event_loop, dict) else False,
            }
        )

    load_contract = client.get("load_contract")
    load_contract = load_contract if isinstance(load_contract, dict) else {}
    raw_profile_id = load_contract.get("profile_id")
    profile_id = (
        raw_profile_id
        if isinstance(raw_profile_id, str) and PROFILE_ID_RE.fullmatch(raw_profile_id)
        else None
    )
    return {
        "schema": 1,
        "kind": "timeout_path_diagnostics",
        "profile_id": profile_id,
        "load_window": {
            "client_started_at": client.get("started_at"),
            "client_finished_at": client.get("finished_at"),
            "origin_started_at": server.get("started_at"),
            "origin_finished_at": server.get("finished_at"),
            "timestamps": "UTC; runner/origin clock alignment is not used for row correlation",
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
