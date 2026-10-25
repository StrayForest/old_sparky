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
import tempfile
import time

import platform_ssr_capacity_diagnostic as capacity
from platform_ssr_capacity_control import write_control


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class _SyntheticWatcher:
    """Keep the parent callback path local and deterministic in this canary."""

    def start(self) -> None:
        return None

    def stop(self) -> dict[str, object]:
        return {
            "status": "incomplete",
            "terminal_decision": "abort",
            "ssh_material_restored": True,
            "memory_events_sampled_max_delta": None,
            "samples": 0,
        }

    def fail_closed(self) -> None:
        return None


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
        manifest, _actors = capacity.external_load.load_manifest_bytes(manifest_raw)
        namespace_binding = capacity._namespace_worker_binding(
            source_sha=source_sha,
            app_target_sha=source_sha,
            external_run_id=external_run_id,
            manifest=manifest,
        )
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

        stage_state: dict[str, object] = {}
        outcome = capacity.run_isolated_capacity_worker(
            stage_state=stage_state,
            capacity_config=capacity_config,
            worker_binding=namespace_binding,
            worker_report_path=worker_report_path,
            supervisor_report_path=report_path,
            source_sha=source_sha,
            watcher=_SyntheticWatcher(),
            hide_ssh_material=lambda: None,
            restore_ssh_material=lambda: None,
            maximum_runtime_seconds=12.0,
            on_start_ms=capacity.ON_WINDOW_MS + 5_000,
            on_end_ms=capacity.ON_WINDOW_MS * 2 + 5_000,
        )

        child_report = json.loads(worker_report_path.read_text(encoding="utf-8"))
        supervisor = stage_state.get("supervisor_evidence")
        if not isinstance(supervisor, dict):
            raise SystemExit("synthetic SSR canary omitted supervisor evidence")
        worker_report = outcome.get("worker")
        if not isinstance(worker_report, dict):
            child_failure = (
                child_report.get("worker_failure")
                if isinstance(child_report, dict)
                else None
            )
            safe_failure = child_failure if isinstance(child_failure, dict) else {}
            details = {
                "worker_present": False,
                "child_report_complete": (
                    child_report.get("report_complete") is True
                    if isinstance(child_report, dict) else False
                ),
                "child_status": (
                    child_report.get("status")
                    if isinstance(child_report, dict)
                    and child_report.get("status") in {"complete", "incomplete", "failed"}
                    else "unknown"
                ),
                "child_namespace_closed": (
                    child_report.get("namespace_closed")
                    if isinstance(child_report, dict)
                    and type(child_report.get("namespace_closed")) is bool
                    else None
                ),
                "child_observer_failed_abort": (
                    isinstance(child_report, dict)
                    and child_report.get("resource_abort_reason") == "observer_failed"
                ),
                "child_duration_ms": (
                    child_report.get("observed_duration_ms")
                    if isinstance(child_report, dict)
                    and type(child_report.get("observed_duration_ms")) is int
                    and 0 <= child_report["observed_duration_ms"] <= 30_000
                    else None
                ),
                "child_failure_stage": (
                    safe_failure.get("stage")
                    if safe_failure.get("stage") in {
                        "worker_config_read", "worker_config_validate",
                        "worker_identity", "worker_callback", "worker_report_write",
                    }
                    else "none"
                ),
                "supervisor": {
                    key: supervisor.get(key)
                    for key in (
                        "reason", "worker_started", "worker_exited",
                        "namespace_closed", "return_code", "signal",
                        "startup_failure_stage", "namespace_ready_failure_stage",
                        "namespace_post_ready_failure_stage",
                        "watchdog_startup_failure_stage",
                    )
                },
            }
            raise SystemExit(
                "synthetic SSR canary parent rejected worker report: "
                + json.dumps(details, sort_keys=True)
            )
        checks = {
            "supervisor_reason_none": supervisor.get("reason") == "none",
            "worker_started": supervisor.get("worker_started") is True,
            "worker_exited": supervisor.get("worker_exited") is True,
            "worker_exit_one_for_incomplete": supervisor.get("return_code") == 1,
            "no_supervisor_startup_failure": all(
                supervisor.get(key) is None for key in (
                    "startup_failure_stage", "namespace_ready_failure_stage",
                    "namespace_post_ready_failure_stage",
                    "watchdog_startup_failure_stage",
                )
            ),
            "namespace_closed": outcome.get("namespace_closed") is True,
            "descendants_reaped": outcome.get("namespace_closed") is True,
            "child_report_complete": child_report.get("report_complete") is True,
            "parent_proved_child_not_closed": child_report.get("namespace_closed") is False,
            "final_report_closed": outcome.get("namespace_closed") is True,
            "incomplete_status": outcome.get("status") == "resource_abort",
            "abort_reason": worker_report.get("resource_abort_reason") == "observer_failed",
            "zero_submissions": worker_report.get("submitted") == 0,
            "zero_completions": worker_report.get("completed") == 0,
            "zero_response_bytes": worker_report.get("response_bytes_total") == 0,
            "zero_observed_duration": worker_report.get("observed_duration_ms") == 0,
            "no_http_statuses": worker_report.get("http_status_counts") == {},
            "no_http_errors": worker_report.get("error_counts") == {},
            "zero_off_probe_submissions": (
                isinstance(worker_report.get("off_probe"), dict)
                and worker_report["off_probe"].get("submitted") == 0
                and worker_report["off_probe"].get("completed") == 0
            ),
            "runner_identity_proven": (
                isinstance(outcome.get("runner_headroom"), dict)
                and isinstance(outcome["runner_headroom"].get("identity"), dict)
                and set(outcome["runner_headroom"]["identity"])
                == set(capacity.external_load.RunnerCgroupSampler.IDENTITY_FIELDS)
                and all(value is True for value in outcome["runner_headroom"]["identity"].values())
            ),
            "runner_phase_window_remains_unknown": (
                isinstance(outcome.get("runner_headroom"), dict)
                and outcome["runner_headroom"].get("status") == "unknown"
                and outcome["runner_headroom"].get("reason") == "runner_phase_samples_incomplete"
            ),
            "runner_phase_has_no_capacity_claim": (
                isinstance(outcome.get("runner_headroom"), dict)
                and outcome["runner_headroom"].get("effective_cpu_capacity_cores") is None
                and outcome["runner_headroom"].get("max_cgroup_cpu_usage_cores") is None
            ),
            "no_capacity_credit": all(
                outcome.get(key) is False
                for key in ("matrix_credit", "slo_credit", "diagnostic_credit")
            ),
        }
        if not all(checks.values()):
            # Emit only closed, non-sensitive booleans and bounded status fields.
            details = {
                "checks": checks,
                "reason": supervisor.get("reason"),
                "returncode": supervisor.get("return_code"),
                "status": outcome.get("status"),
                "resource_abort_reason": worker_report.get("resource_abort_reason"),
                "namespace_closed": outcome.get("namespace_closed"),
                "runner_headroom": {
                    key: outcome.get("runner_headroom", {}).get(key)
                    for key in ("status", "reason", "sample_count", "identity")
                } if isinstance(outcome.get("runner_headroom"), dict) else None,
            }
            raise SystemExit(
                "synthetic SSR capacity worker-entry canary failed: "
                + json.dumps(details, sort_keys=True)
            )
        runner_headroom = outcome.get("runner_headroom")
        print(json.dumps({
            "checks": checks,
            "status": outcome["status"],
            "runner_headroom": {
                key: runner_headroom.get(key)
                for key in ("status", "reason", "sample_count", "identity")
            } if isinstance(runner_headroom, dict) else None,
        }, sort_keys=True))

    if temporary_root is not None and temporary_root.exists():
        raise SystemExit("synthetic SSR canary temporary directory was not cleaned")
    print(json.dumps({"temporary_directory_removed": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
