#!/usr/bin/env python3
"""Exercise the real SSR capacity worker-entry path with synthetic inputs.

This canary uses the mandatory PID namespace launcher but exits at the
capacity worker's fresh parent-control abort guard, before any HTTP request.
It does not create production fixtures or prepare an SSH/remote plan.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import platform_ssr_capacity_diagnostic as capacity
from platform_load_runtime import run_supervised
from platform_ssr_capacity_control import write_control


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,19}$")


def _synthetic_manifest(source_sha: str) -> bytes:
    payload = {
        "schema": capacity.external_load.SOURCE_BOUND_MANIFEST_SCHEMA,
        "purpose": "external_ready_vote",
        "origin": capacity.external_load.EXPECTED_ORIGIN,
        "session_cookie_name": "synthetic_session",
        "csrf_cookie_name": "synthetic_csrf",
        "marker": "preprod202610250001abcd",
        "tournaments": [{"slug": "synthetic-canary", "user_count": 14}],
        "users": [
            {
                "user_id": f"synthetic-user-{index:02d}",
                "tournament_slug": "synthetic-canary",
                "session_token": f"synthetic-session-{index:032d}",
                "csrf_token": f"synthetic-csrf-{index:032d}",
            }
            for index in range(14)
        ],
        "created_at": datetime.now(UTC).isoformat(),
        "runner_sha": source_sha,
        "app_target_sha": source_sha,
        "source_binding": None,
        "source_binding_sha256": None,
    }
    return json.dumps(
        payload, ensure_ascii=True, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"


def main() -> int:
    source_sha = os.environ.get("SOURCE_GIT_SHA", "")
    external_run_id = os.environ.get("GITHUB_RUN_ID", "")
    if SHA_RE.fullmatch(source_sha) is None:
        raise SystemExit("synthetic SSR canary source SHA is invalid")
    if RUN_NUMBER_RE.fullmatch(external_run_id) is None:
        raise SystemExit("synthetic SSR canary workflow run ID is invalid")
    if os.geteuid() == 0:
        raise SystemExit("synthetic SSR canary must run as the non-root runner")

    namespace_binding = {
        "source_git_sha": source_sha,
        "app_target_sha": source_sha,
        "source_binding": None,
        "source_binding_sha256": None,
        "external_run_id": external_run_id,
    }
    capacity_binding = {
        "run_id": os.urandom(16).hex(),
        "source_sha": source_sha,
        "app_target_sha": source_sha,
        "profile_digest": capacity.profile_digest(8),
        "external_run_id": external_run_id,
        "external_run_attempt": "1",
    }

    temporary_root: Path | None = None
    with tempfile.TemporaryDirectory(prefix="ssr-capacity-worker-canary-") as directory:
        root = Path(directory)
        temporary_root = root
        os.chmod(root, 0o700)
        manifest_path = root / "synthetic-manifest.json"
        manifest_raw = _synthetic_manifest(source_sha)
        manifest_path.write_bytes(manifest_raw)
        os.chmod(manifest_path, 0o600)
        control_path = root / capacity.CONTROL_BASENAME
        report_path = root / "supervisor-report.json"
        worker_report_path = root / "worker-report.json"

        write_control(
            control_path,
            {
                "schema": 1,
                **capacity_binding,
                "decision": "abort",
                "reason": "observer_failed",
                "sequence": 1,
                "updated_monotonic_ns": time.monotonic_ns(),
            },
            expected=capacity_binding,
        )
        capacity_config = {
            "schema": capacity.SCHEMA,
            "workload": capacity.WORKLOAD,
            "fixture_kind": capacity.FIXTURE_KIND,
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "manifest_bytes": len(manifest_raw),
            "control_path": str(control_path),
            "binding": capacity_binding,
            "concurrency": 8,
            "off_start_ms": 0,
            "off_end_ms": capacity.ON_WINDOW_MS,
            "on_start_ms": capacity.ON_WINDOW_MS + 5_000,
            "on_end_ms": capacity.ON_WINDOW_MS * 2 + 5_000,
        }

        result = run_supervised(
            worker_command=(
                sys.executable,
                str(Path(capacity.__file__).resolve()),
                "worker-entry",
            ),
            report_path=report_path,
            worker_report_path=worker_report_path,
            max_duration_seconds=12.0,
            max_runner_minutes=0.5,
            worker_config={
                "binding": namespace_binding,
                "capacity_diagnostic": capacity_config,
            },
            term_grace_seconds=0.5,
            poll_seconds=0.01,
            env={
                "SOURCE_GIT_SHA": source_sha,
                "APP_TARGET_SHA": source_sha,
                "GITHUB_RUN_ID": external_run_id,
            },
            clear_worker_environment=True,
        )

        child_report = json.loads(worker_report_path.read_text(encoding="utf-8"))
        final_report = result.report
        checks = {
            "supervisor_reason_none": result.reason == "none",
            "worker_started": result.worker_started,
            "worker_exited": result.worker_exited,
            "worker_exit_one_for_incomplete": result.returncode == 1,
            "namespace_closed": result.namespace_closed is True,
            "descendants_reaped": result.descendants_reaped is True,
            "child_report_complete": child_report.get("report_complete") is True,
            "parent_proved_child_not_closed": child_report.get("namespace_closed") is False,
            "final_report_closed": final_report.get("namespace_closed") is True,
            "incomplete_status": final_report.get("status") == "incomplete",
            "abort_reason": final_report.get("resource_abort_reason") == "observer_failed",
            "zero_submissions": final_report.get("submitted") == 0,
            "zero_completions": final_report.get("completed") == 0,
            "zero_response_bytes": final_report.get("response_bytes_total") == 0,
            "no_http_statuses": final_report.get("http_status_counts") == {},
            "no_http_errors": final_report.get("error_counts") == {},
            "zero_off_probe_submissions": (
                isinstance(final_report.get("off_probe"), dict)
                and final_report["off_probe"].get("submitted") == 0
                and final_report["off_probe"].get("completed") == 0
            ),
            "no_capacity_credit": all(
                final_report.get(key) is False
                for key in ("matrix_credit", "slo_credit", "diagnostic_credit")
            ),
        }
        if not all(checks.values()):
            # Emit only closed, non-sensitive booleans and bounded status fields.
            details = {
                "checks": checks,
                "reason": result.reason,
                "returncode": result.returncode,
                "status": final_report.get("status"),
                "resource_abort_reason": final_report.get("resource_abort_reason"),
                "namespace_closed": result.namespace_closed,
                "report_error": final_report.get("report_error"),
            }
            raise SystemExit(
                "synthetic SSR capacity worker-entry canary failed: "
                + json.dumps(details, sort_keys=True)
            )
        print(json.dumps({"checks": checks, "status": final_report["status"]}, sort_keys=True))

    if temporary_root is not None and temporary_root.exists():
        raise SystemExit("synthetic SSR canary temporary directory was not cleaned")
    print(json.dumps({"temporary_directory_removed": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
