#!/usr/bin/env python3
"""Turn live-launch process output into a value-free fixed-schema report."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any, Iterable

try:
    from tools.platform_evidence_sanitizer import (
        BoundedLineReader,
        MAX_LOG_LINE_BYTES,
        MAX_LOG_TOTAL_BYTES,
        safe_error_class,
        safe_route_class,
        safe_status,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from platform_evidence_sanitizer import (
        BoundedLineReader,
        MAX_LOG_LINE_BYTES,
        MAX_LOG_TOTAL_BYTES,
        safe_error_class,
        safe_route_class,
        safe_status,
    )


MAX_LINES = 1000
KNOWN_TEST_NAMES = {
    "browser_public",
    "live_user_qa",
    "production_browser",
    "other",
}


def _test_name(line: str) -> str | None:
    upper = line.upper()
    if "LIVE_BROWSER_QA_SUCCESS" in upper or "LIVE_BROWSER_QA_FAILURE" in upper:
        return "browser_public"
    if "LIVE_USER_QA_SUCCESS" in upper or "LIVE_USER_QA_FAILURE" in upper:
        return "live_user_qa"
    match = re.search(r"\btest[_ -]([a-z0-9_]+)", line.lower())
    if match and match.group(1) in KNOWN_TEST_NAMES:
        return match.group(1)
    return None


def _line_status(line: str) -> int:
    match = re.search(r"\b(?:status|http_status)\s*[=:]\s*(\d{3})\b", line.lower())
    return safe_status(match.group(1)) if match else 0


def _line_route_class(line: str) -> str:
    # Route names are matched against the closed classifier; the input line is
    # never copied into the report.
    path_match = re.search(r"(?P<path>/(?:api/v1/)?(?:auth|users|profiles|tournaments)/[^\s,;]+)", line.lower())
    if path_match:
        return safe_route_class(path_match.group("path"))
    for route in (
        ("auth_bootstrap", "/auth/bootstrap"),
        ("auth_csrf", "/auth/csrf"),
        ("auth_session", "/auth/session"),
        ("users_me", "/users/me"),
        ("profiles_me", "/profiles/me"),
        ("tournament_page", "/tournaments/x"),
        ("ready_vote", "/tournaments/x/deadlock/ready-check/vote"),
        ("ready_check_state", "/tournaments/x/deadlock/ready-check"),
        ("tournament_workspace", "/tournaments/x/workspace"),
    ):
        if route[0] in line.lower():
            return safe_route_class(route[1])
    return "other"


def sanitize_live_launch_lines(
    lines: Iterable[str] | Any,
    *,
    max_lines: int = MAX_LINES,
    max_total_bytes: int = MAX_LOG_TOTAL_BYTES,
    max_line_bytes: int = MAX_LOG_LINE_BYTES,
) -> dict[str, Any]:
    """Reduce launch output through a bounded reader without retaining lines."""

    class_counts: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    tests: dict[str, dict[str, Any]] = {}
    reader = BoundedLineReader(
        lines,
        max_lines=max_lines,
        max_total_bytes=max_total_bytes,
        max_line_bytes=max_line_bytes,
    )
    for line in reader:
        lowered = line.lower()
        status = _line_status(line)
        route_class = _line_route_class(line)
        route_counts[route_class] += 1
        if status:
            status_counts[str(status)] += 1
        explicit_status = re.search(
            r'"(?:status|outcome)"\s*:\s*"([a-z_-]+)"', lowered
        )
        explicit_success = re.search(
            r'"(?:success|passed)"\s*:\s*(true|false)', lowered
        )
        explicit_value = explicit_status.group(1) if explicit_status else ""
        explicit_boolean = explicit_success.group(1) if explicit_success else ""
        if explicit_value in {"passed", "success", "complete", "completed", "ok"} or explicit_boolean == "true":
            line_class = "success"
            error_class = "none"
        elif explicit_value == "timeout":
            line_class = "timeout"
            error_class = "timeout"
        elif explicit_value in {"failed", "failure", "error", "unavailable"} or explicit_boolean == "false":
            line_class = "failure"
            error_class = safe_error_class("other", status=status)
        elif "timeout" in lowered or "timed out" in lowered:
            line_class = "timeout"
            error_class = "timeout"
        elif (
            "error" in lowered
            or "fail" in lowered
            or "exception" in lowered
            or "traceback" in lowered
            or '"passed": false' in lowered
            or '"success": false' in lowered
        ):
            line_class = "failure"
            error_class = safe_error_class("other", status=status)
        elif "success" in lowered or "passed" in lowered or "complete" in lowered:
            line_class = "success"
            error_class = "none"
        else:
            line_class = "other"
            error_class = "none"
        class_counts[line_class] += 1
        error_counts[error_class] += 1
        test_name = _test_name(line)
        if test_name:
            current = tests.setdefault(
                test_name,
                {
                    "name": test_name,
                    "status": "unknown",
                    "route_class": route_class,
                    "http_status": status,
                    "error_class": "none",
                },
            )
            if line_class in {"failure", "timeout"}:
                current["status"] = "failed"
                current["error_class"] = error_class
            elif current["status"] != "failed" and line_class == "success":
                current["status"] = "passed"
            if status:
                current["http_status"] = status
            if route_class != "other":
                current["route_class"] = route_class
    if reader.error:
        report_status = "error"
    elif any(test["status"] == "failed" for test in tests.values()) or class_counts["failure"] or class_counts["timeout"]:
        report_status = "failed"
    elif reader.truncated:
        report_status = "truncated"
    elif any(test["status"] == "passed" for test in tests.values()) or class_counts["success"]:
        report_status = "passed"
    elif reader.line_count:
        report_status = "unknown"
    else:
        report_status = "unavailable"
    return {
        "schema": 1,
        "status": report_status,
        "success": report_status == "passed",
        "line_count": reader.line_count,
        "retained_line_count": reader.line_count,
        "truncated": reader.truncated,
        "test_count": len(tests),
        "failed_test_count": sum(test["status"] == "failed" for test in tests.values()),
        "tests": list(tests.values())[:32],
        "class_counts": dict(sorted(class_counts.items())),
        "route_class_counts": dict(sorted(route_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "error_class_counts": dict(sorted(error_counts.items())),
    }


def sanitize_live_launch_file(
    source: Path,
    destination: Path,
    *,
    process_exit_code: int | None = None,
) -> dict[str, Any]:
    try:
        with source.open("rb") as handle:
            report = sanitize_live_launch_lines(handle)
    except OSError:
        report = sanitize_live_launch_lines(())
    if process_exit_code is not None:
        report["process_exit_code"] = process_exit_code if 0 <= process_exit_code <= 255 else 1
        if process_exit_code != 0:
            # The exit code is the only value needed to distinguish an SSH or
            # remote harness failure.  Do not preserve any raw process output
            # merely because it contained no recognizable error word.
            report["status"] = "failed"
            report["success"] = False
            report["error_class_counts"]["transport"] = (
                report["error_class_counts"].get("transport", 0) + 1
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, default=None)
    args = parser.parse_args()
    report = sanitize_live_launch_file(
        args.input,
        args.output,
        process_exit_code=args.exit_code,
    )
    return 0 if report["status"] not in {"unavailable", "error"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
