#!/usr/bin/env python3
"""Run the separate, guarded authenticated workspace SSR capacity diagnostic."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping

try:
    from tools import platform_external_load as external_load
    from tools import platform_load_runtime as load_runtime
    from tools.platform_ssr_capacity_control import (
        CapacityControlError,
        write_control,
        stop_reason_from_control,
    )
except ModuleNotFoundError:  # direct execution from platform/tools
    import platform_external_load as external_load  # type: ignore[no-redef]
    import platform_load_runtime as load_runtime  # type: ignore[no-redef]
    from platform_ssr_capacity_control import (  # type: ignore[no-redef]
        CapacityControlError,
        write_control,
        stop_reason_from_control,
    )


WORKLOAD = "authenticated_workspace_read_capacity_v1"
FIXTURE_KIND = "ssr_capacity_diagnostic_v1"
HTML_WORKLOAD = "authenticated_html_ssr_capacity_v1"
HTML_FIXTURE_KIND = "ssr_html_capacity_diagnostic_v1"
SCHEMA = 1
CONCURRENCY_PRESETS = frozenset({8, 16})
HTML_CONCURRENCY_PRESETS = frozenset({8, 16, 32, 64})
CAPACITY_ERROR_CODES = frozenset({
    "auth", "http_error", "other", "page_marker_missing", "rate_limited",
    "server_error", "timeout", "transport", "unexpected_content_type", "validation",
})
FIXTURE_TOURNAMENTS = 1
FIXTURE_USERS = 14
ON_WINDOW_MS = 20_000
CONTROL_BASENAME = "capacity-control.json"
REMOTE_WATCH_PREFIX = b"CAPACITY_MEMORY_GUARD "
REMOTE_WATCH_ERROR_PREFIX = b"CAPACITY_MEMORY_GUARD_ERROR "
# Match the dispatcher's fixed 130s cap: at most 522 sampled/terminal rows
# at 250ms. The parent caps each complete prefixed wire line at 2048 bytes;
# 522 lines plus newlines remain comfortably within this cumulative 2MiB cap.
REMOTE_WATCH_CAP = 2 * 1024 * 1024
REMOTE_ROW_CAP = 2_048
MEMORY_OBSERVER_FAILURE_STAGES = frozenset({
    "ssh_material_validation", "ssh_spawn", "input_write", "first_row_read",
    "row_decode", "row_accept", "thread_start", "sample_loop",
    "terminal_drain", "terminal_wait", "stop_join", "unknown",
})
MEMORY_OBSERVER_ERROR_CLASSES = frozenset({
    "OSError", "TimeoutError", "ValueError", "TypeError",
    "RuntimeError", "SubprocessError", "unexpected",
})
REMOTE_WATCH_FAILURE_STAGES = frozenset({
    "helper_validation", "helper_spawn", "input_write", "watch_timeout",
    "helper_read", "output_limit", "row_framing", "row_protocol",
    "helper_exit", "unknown",
})
REMOTE_WATCH_FAILURE_REASONS = frozenset({
    "none", "refused", "spawn_error", "write_error", "timeout",
    "read_error", "limit", "malformed", "nonzero_exit", "missing_terminal",
    "unexpected", "unknown",
})
REMOTE_WATCH_FAILURE_PAIRS = frozenset({
    ("helper_validation", "refused"), ("helper_spawn", "spawn_error"),
    ("input_write", "write_error"), ("watch_timeout", "timeout"),
    ("helper_read", "read_error"), ("output_limit", "limit"),
    ("row_framing", "malformed"), ("row_protocol", "malformed"),
    ("helper_exit", "nonzero_exit"), ("helper_exit", "missing_terminal"),
    ("helper_exit", "unexpected"), ("unknown", "unknown"),
})
CPU_EDGE_TOLERANCE_NS = 250_000_000
CPU_WINDOW_TOLERANCE_NS = 500_000_000
# The plan is accepted only when its +52s requested start is 45–60s ahead
# remotely, which bounds host-minus-runner clock offset to [-8s, +7s]. Its expiry
# is requested on_end (+97s from plan creation); add 8s for that offset and 1s
# so the host's strict `expires_at_ms < now` cleanup guard is satisfied.
PLAN_EXPIRY_SKEW_MARGIN_MS = 9_000
PLAN_EXPIRY_HORIZON_MS = 97_000
PLAN_EXPIRY_WAIT_LIMIT_MS = 107_000
SANITIZER_FAILURE_STAGES = frozenset({
    "manifest_read", "report_read", "report_projection", "report_binding", "output_write",
})
SANITIZER_FAILURE_REASONS = frozenset({
    "manifest_unavailable", "report_unavailable", "report_metadata_rejected",
    "report_changed", "report_noncanonical", "report_parse_rejected",
    "report_projection_rejected", "runner_identity_shape", "report_binding_rejected",
    "output_write_failed", "unexpected",
})
RUNNER_CONTEXT_REASONS = frozenset({
    "missing", "none",
    "trusted_runner_context_unavailable", "workload_identity_changed",
    "workload_cgroup_mismatch", "runner_namespace_binding_invalid",
    "runner_cgroup_snapshot_incomplete", "runner_cgroup_identity_changed",
    "runner_context_unavailable", "runner_final_snapshot_invalid",
    "runner_samples_incomplete", "runner_hierarchy_changed",
    "runner_cpuset_incomplete", "runner_cpu_quota_changed",
    "runner_cpu_quota_incomplete", "runner_sample_clock_invalid",
    "runner_cpu_stat_incomplete", "runner_cpu_counter_reset",
    "runner_throttle_evidence_unavailable", "runner_phase_window_invalid",
    "runner_phase_samples_incomplete", "runner_membership_changed",
    "runner_sample_limit", "runner_sampler_join_timeout", "runner_snapshot_invalid",
    "runner_snapshot_unavailable",
})
CAPACITY_ABORT_REASONS = frozenset({
    "none", "window_complete", "off_warmup_failed", "off_probe_window_missed",
    "off_probe_failed", "response_failure", "worker_error", "guard_unavailable",
    "control_file_invalid", "memory_floor", "memory_event", "identity_changed",
    "collection_error", "observer_failed",
})
WARMUP_FAILURE_FIELDS = frozenset({
    "schema", "submitted", "results", "future_exceptions", "future_timeouts",
    "marker_parsed_results", "max_elapsed_ms", "http_status_counts", "content_encoding_counts",
    "failure_counts", "exception_class_counts",
})
WARMUP_FAILURE_CATEGORIES = frozenset({
    "request_exception", "non_200", "content_type_mismatch", "page_marker_missing",
    "marker_lifecycle_missing", "marker_authenticated_header_missing",
    "marker_workspace_not_ready", "marker_detail_fallback_missing",
    "marker_parse_unavailable", "elapsed_limit",
    "future_timeout", "unexpected_worker_exception", "unclassified_result_failure",
    "invalid_timing", "invalid_result_status",
})
WARMUP_EXCEPTION_CLASSES = frozenset({
    "TimeoutError", "OSError", "HTTPException", "ValueError", "TypeError",
    "RuntimeError", "SubprocessError", "other",
})
WARMUP_CONTENT_ENCODINGS = frozenset({
    "identity", "gzip", "deflate", "br", "other", "unknown",
})
WARMUP_MARKER_FIELDS = {
    "lifecycle": "marker_lifecycle_missing",
    "authenticated_header": "marker_authenticated_header_missing",
    "workspace_ready": "marker_workspace_not_ready",
    "detail_fallback": "marker_detail_fallback_missing",
}
WARMUP_HTTP_EXCEPTION_NAMES = frozenset({
    "HTTPException", "RemoteDisconnected", "IncompleteRead", "BadStatusLine",
    "ResponseNotReady",
})
WARMUP_OS_ERROR_NAMES = frozenset({
    "OSError", "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "BrokenPipeError",
})
SUPERVISOR_REASONS = frozenset({
    "none", "max_duration_seconds", "max_runner_minutes", "namespace_unavailable",
    "namespace_start_failed", "external_signal", "worker_failed", "worker_signal",
    "namespace_unclosed", "supervisor_error",
})
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^[0-9a-f]{32}$")
ATTEMPT_RE = re.compile(r"^[1-9][0-9]{0,8}$")
PARENT_FAILURE_STAGES = frozenset({
    "identity", "manifest_read", "binding", "remote_plan_prepare",
    "worker_supervision", "plan_cleanup", "report_write",
})
PARENT_ERROR_CLASSES = frozenset({
    "OSError", "ValueError", "KeyError", "TypeError", "RuntimeError",
    "PlanError", "UntrustedHelper", "unexpected",
})
SUPERVISOR_REPORT_ERROR_CLASSES = frozenset({
    "AttributeError", "NamespaceCapabilityError", "NamespaceIntegrityError",
    "OSError", "preflight_deadline_exceeded", "preflight_setup_deadline_exceeded",
})
SUPERVISOR_REPORT_PHASES = frozenset({"namespace_probe", "supervisor"})
PLAN_PREPARE_STATUSES = frozenset({
    "not_attempted", "prepared", "refused", "transport_failed", "bad_protocol",
})
PLAN_CLEANUP_STATUSES = frozenset({
    "not_attempted", "pending", "complete", "refused", "transport_failed",
    "bad_protocol", "failed",
})
PLAN_REFUSAL_CLASSES = frozenset({
    "PlanError", "OSError", "ValueError", "KeyError", "UntrustedHelper", "unexpected", "none",
})
SSH_TRANSPORT_STAGES = frozenset({
    "not_started", "spawn_failed", "input", "read", "timeout", "output_limit",
    "wait", "protocol", "complete", "transport_failed",
})
SSH_CHILD_STATES = frozenset({"not_started", "running", "reaped", "unknown"})
SSH_STDOUT_SHAPES = frozenset({
    "none", "single_line", "prepared_marker", "refusal_marker", "summary_marker", "malformed",
})


class _RemoteWatchFailure(ValueError):
    """One fixed, closed marker emitted by the remote memory observer."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__("remote memory observer failed")
        self.stage = stage
        self.reason = reason


class _RemoteCapacityPlanError(ValueError):
    """Closed remote-plan failure retaining only the helper's lifecycle frame."""

    def __init__(
        self, lifecycle: Mapping[str, Any], error_class: str,
        expires_at_ms: int | None = None,
        plan_created_at_ms: int | None = None,
        plan_created_monotonic: float | None = None,
    ) -> None:
        super().__init__("capacity plan preparation failed")
        self.lifecycle = dict(lifecycle)
        self.error_class = error_class if error_class in PARENT_ERROR_CLASSES else "unexpected"
        self.expires_at_ms = expires_at_ms
        self.plan_created_at_ms = plan_created_at_ms
        self.plan_created_monotonic = plan_created_monotonic


class _CapacitySanitizationError(ValueError):
    """A closed sanitizer failure record; never carries untrusted text."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        super().__init__("capacity report validation failed")
        self.payload = dict(payload)


class _RemoteMemoryWatcher:
    """One fixed SSH stream that publishes safe memory decisions to a worker."""

    def __init__(
        self, *, ssh_host: str, ssh_user: str, ssh_config: Path, ssh_key: Path,
        control_path: Path, binding: Mapping[str, Any], run_id: str, source_sha: str,
        expected_plan: tuple[int, int, int], workload: str = WORKLOAD,
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_config = ssh_config
        self.ssh_key = ssh_key
        self.control_path = control_path
        self.binding = dict(binding)
        self.run_id = run_id
        self.source_sha = source_sha
        self.process: subprocess.Popen[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.selector: selectors.BaseSelector | None = None
        self.pending = bytearray()
        self.total = 0
        self.sequence = 0
        self.expected_plan = expected_plan
        self.workload = workload
        self.expected_target: tuple[int, int, str] | None = None
        self.terminal = False
        self.terminal_decision: str | None = None
        self.terminal_reason: str | None = None
        self.failure = False
        self.samples = 0
        self.current_min: int | None = None
        self.current_max: int | None = None
        self.peak_max: int | None = None
        self.event_max: dict[str, int] = {}
        self._failure_lock = threading.Lock()
        self.failure_stage: str | None = None
        self.error_class: str | None = None
        self.remote_failure_stage: str | None = None
        self.remote_failure_reason: str | None = None
        self._stage_local = threading.local()
        self._set_stage("ssh_material_validation")

    def _set_stage(self, stage: str) -> None:
        self._stage_local.name = stage

    def _stage(self) -> str:
        stage = getattr(self._stage_local, "name", "unknown")
        return stage if isinstance(stage, str) else "unknown"

    @staticmethod
    def _safe_error_class(error: BaseException) -> str:
        if isinstance(error, TimeoutError):
            return "TimeoutError"
        if isinstance(error, OSError):
            return "OSError"
        if isinstance(error, ValueError):
            return "ValueError"
        if isinstance(error, TypeError):
            return "TypeError"
        if isinstance(error, RuntimeError):
            return "RuntimeError"
        if isinstance(error, subprocess.SubprocessError):
            return "SubprocessError"
        return "unexpected"

    def _record_failure(self, stage: str, error: BaseException | None = None) -> None:
        with self._failure_lock:
            if self.failure_stage is not None:
                return
            self.failure_stage = stage if stage in MEMORY_OBSERVER_FAILURE_STAGES else "unknown"
            if error is not None:
                self.error_class = self._safe_error_class(error)

    def _capture_remote_failure(self, error: _RemoteWatchFailure) -> None:
        child_status = None
        if self.process is not None:
            try:
                child_status = self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child_status = None
        with self._failure_lock:
            if child_status not in (None, 0):
                self.remote_failure_stage = error.stage
                self.remote_failure_reason = error.reason
                return
            self.remote_failure_stage = "unknown"
            self.remote_failure_reason = "unexpected"

    def start(self) -> None:
        self._set_stage("ssh_material_validation")
        try:
            self._start()
        except Exception as exc:
            if isinstance(exc, _RemoteWatchFailure):
                self._capture_remote_failure(exc)
            self._record_failure(self._stage(), exc)
            raise

    def _read_row(
        self, timeout_seconds: float, *, allow_eof: bool = False,
    ) -> dict[str, Any] | None:
        assert self.process is not None and self.process.stdout is not None
        if self.selector is None:
            self.selector = selectors.DefaultSelector()
            descriptor = self.process.stdout.fileno()
            os.set_blocking(descriptor, False)
            self.selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            if b"\n" in self.pending:
                line, _, remainder = self.pending.partition(b"\n")
                self.pending[:] = remainder
                if (not line.startswith(REMOTE_WATCH_PREFIX)
                        or len(line) > REMOTE_ROW_CAP):
                    if line.startswith(REMOTE_WATCH_ERROR_PREFIX) and len(line) <= REMOTE_ROW_CAP:
                        marker_bytes = line[len(REMOTE_WATCH_ERROR_PREFIX):]
                        try:
                            marker = json.loads(marker_bytes.decode("ascii"))
                        except (UnicodeError, json.JSONDecodeError):
                            marker = None
                        if isinstance(marker, dict):
                            canonical = json.dumps(
                                marker, ensure_ascii=True, allow_nan=False,
                                sort_keys=True, separators=(",", ":"),
                            ).encode("ascii")
                        else:
                            canonical = b""
                        if (isinstance(marker, dict) and canonical == marker_bytes
                                and set(marker) == {"schema", "stage", "reason"}
                                and type(marker.get("schema")) is int and marker["schema"] == 1
                                and isinstance(marker.get("stage"), str)
                                and isinstance(marker.get("reason"), str)
                                and (marker["stage"], marker["reason"]) in REMOTE_WATCH_FAILURE_PAIRS):
                            raise _RemoteWatchFailure(marker["stage"], marker["reason"])
                    raise ValueError("memory observer row prefix or length rejected")
                raw = bytes(line[len(REMOTE_WATCH_PREFIX):])
                self._set_stage("row_decode")
                value = json.loads(raw.decode("ascii"))
                if (not isinstance(value, dict)
                        or json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                                      separators=(",", ":")).encode("ascii") != raw):
                    raise ValueError("memory observer row is not canonical")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.selector is None:
                raise TimeoutError("memory observer row deadline exceeded")
            ready = self.selector.select(min(remaining, 0.25))
            if not ready:
                if self.process.poll() is not None:
                    if allow_eof and not self.pending:
                        return None
                    raise ValueError("memory observer exited without terminal row")
                continue
            chunk = os.read(ready[0][0].fd, min(4096, REMOTE_WATCH_CAP + 1 - self.total))
            if not chunk:
                self.selector.unregister(ready[0][0].fd)
                if allow_eof and not self.pending:
                    return None
                raise ValueError("memory observer stream ended early")
            self.total += len(chunk)
            if self.total > REMOTE_WATCH_CAP:
                raise ValueError("memory observer output cap exceeded")
            self.pending.extend(chunk)
            # A single pipe read may contain several complete short rows.
            # Bound the current record, not the aggregate buffered bytes;
            # the cumulative stream cap above bounds the whole pending data.
            trailing_fragment = self.pending.rsplit(b"\n", 1)[-1]
            if len(trailing_fragment) > REMOTE_ROW_CAP:
                raise ValueError("memory observer row cap exceeded")

    def _drain_after_terminal(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        if self.selector is None:
            raise ValueError("memory observer selector is unavailable")
        if self.pending:
            raise ValueError("memory observer emitted output after terminal row")
        descriptor = self.process.stdout.fileno()
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("memory observer terminal drain deadline exceeded")
            ready = self.selector.select(min(remaining, 0.25))
            if not ready:
                continue
            chunk = os.read(descriptor, min(4096, REMOTE_WATCH_CAP + 1 - self.total))
            if not chunk:
                self.selector.unregister(descriptor)
                return
            self.total += len(chunk)
            if self.total > REMOTE_WATCH_CAP:
                raise ValueError("memory observer output cap exceeded")
            raise ValueError("memory observer emitted output after terminal row")

    def _accept(self, row: Mapping[str, Any]) -> None:
        expected_fields = {
            "schema", "run_id", "source_sha", "workload", "sequence", "elapsed_ms",
            "phase", "sampled_at_ms", "plan_off_start_ms", "plan_on_start_ms", "plan_on_end_ms",
            "decision", "reason", "memory_current_bytes", "memory_peak_bytes",
            "memory_max_bytes", "memory_events_delta", "target_pid", "target_start_ticks",
            "target_invocation_id", "identity_status",
        }
        if (set(row) != expected_fields or type(row.get("schema")) is not int
                or row["schema"] != 1 or row.get("run_id") != self.run_id
                or row.get("source_sha") != self.source_sha or row.get("workload") != self.workload
                or type(row.get("sequence")) is not int or row["sequence"] != self.sequence + 1
                or row.get("decision") not in {"continue", "abort", "complete"}
                or row.get("reason") not in {
                    "none", "memory_floor", "memory_event", "identity_changed", "collection_error",
                }
                or row.get("identity_status") not in {"complete", "invalid"}
                or type(row.get("elapsed_ms")) is not int or row["elapsed_ms"] < 0
                or type(row.get("sampled_at_ms")) is not int
                or type(row.get("plan_off_start_ms")) is not int
                or type(row.get("plan_on_start_ms")) is not int
                or type(row.get("plan_on_end_ms")) is not int
                or row["plan_off_start_ms"] >= row["plan_on_start_ms"]
                or row["plan_on_start_ms"] - row["plan_off_start_ms"] != 25_000
                or row["plan_on_end_ms"] - row["plan_on_start_ms"] != ON_WINDOW_MS
                or row["elapsed_ms"] != max(0, row["sampled_at_ms"] - row["plan_off_start_ms"])
                or row.get("phase") not in {"off", "idle", "on", "complete", "abort"}
                or row["phase"] != (
                    "off" if row["sampled_at_ms"] < row["plan_off_start_ms"] + 20_000
                    else "idle" if row["sampled_at_ms"] < row["plan_on_start_ms"]
                    else "on" if row["sampled_at_ms"] < row["plan_on_end_ms"]
                    else "complete" if row.get("decision") == "complete"
                    else "abort"
                )
                or (row.get("decision") in {"continue", "complete"} and row.get("reason") != "none")
                or row.get("identity_status") != "complete" and row.get("decision") != "abort"
                or row.get("decision") == "complete" and row.get("reason") != "none"
                or row.get("decision") == "complete" and row["sampled_at_ms"] > row["plan_on_end_ms"] + 1_000
                or row.get("decision") == "abort" and row.get("reason") == "none"
                or self.terminal):
            raise ValueError("memory observer binding or schema rejected")
        plan_tuple = (row["plan_off_start_ms"], row["plan_on_start_ms"], row["plan_on_end_ms"])
        target_tuple = (row.get("target_pid"), row.get("target_start_ticks"), row.get("target_invocation_id"))
        invalid_identity_abort = (
            row.get("decision") == "abort"
            and row.get("identity_status") == "invalid"
            and row.get("reason") in {"identity_changed", "collection_error"}
            and all(row.get(name) is None for name in (
                "memory_current_bytes", "memory_peak_bytes", "memory_max_bytes", "memory_events_delta",
            ))
        )
        valid_metrics = (
            all(type(row.get(name)) is int and row[name] >= 0 for name in (
                "memory_current_bytes", "memory_peak_bytes", "memory_max_bytes",
            ))
            and isinstance(row.get("memory_events_delta"), dict)
            and set(row["memory_events_delta"]) == {
                "low", "high", "max", "oom", "oom_kill", "oom_group_kill",
            }
            and all(type(value) is int and value >= 0 for value in row["memory_events_delta"].values())
        )
        if (type(target_tuple[0]) is not int or target_tuple[0] <= 1
                or type(target_tuple[1]) is not int or target_tuple[1] <= 0
                or not isinstance(target_tuple[2], str)
                or re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f-]{36})", target_tuple[2]) is None
                or not (valid_metrics or invalid_identity_abort)
                or self.expected_plan != plan_tuple
                or self.expected_target not in (None, target_tuple)):
            raise ValueError("memory observer identity changed")
        if valid_metrics:
            events = row["memory_events_delta"]
            event_triggered = any(events[name] > 0 for name in ("high", "max", "oom", "oom_kill", "oom_group_kill"))
            current_triggered = row["memory_current_bytes"] >= 768 * 1024 * 1024
            if (row["memory_max_bytes"] != 1_073_741_824
                    or current_triggered and not (row["decision"] == "abort" and row["reason"] == "memory_floor")
                    or event_triggered and not (row["decision"] == "abort" and row["reason"] == "memory_event")
                    or row["decision"] == "abort" and row["reason"] == "memory_floor" and not current_triggered
                    or row["decision"] == "abort" and row["reason"] == "memory_event" and not event_triggered):
                raise ValueError("memory observer threshold decision is inconsistent")
        elif not invalid_identity_abort:
            raise ValueError("memory observer metrics unavailable without a typed identity abort")
        self.expected_target = target_tuple
        self.sequence = row["sequence"]
        decision = row["decision"]
        reason = row["reason"]
        control = {
            "schema": 1,
            **self.binding,
            "decision": decision,
            "reason": reason,
            "sequence": self.sequence,
            "updated_monotonic_ns": time.monotonic_ns(),
        }
        write_control(self.control_path, control, expected=self.binding)
        self.samples += 1
        current = row.get("memory_current_bytes")
        peak = row.get("memory_peak_bytes")
        if type(current) is int:
            self.current_min = current if self.current_min is None else min(self.current_min, current)
            self.current_max = current if self.current_max is None else max(self.current_max, current)
        if type(peak) is int:
            self.peak_max = peak if self.peak_max is None else max(self.peak_max, peak)
        events = row.get("memory_events_delta")
        if isinstance(events, dict):
            for name, value in events.items():
                if type(value) is int:
                    self.event_max[name] = max(self.event_max.get(name, 0), value)
        if decision in {"abort", "complete"}:
            self.terminal = True
            self.terminal_decision = decision
            self.terminal_reason = reason

    def _start(self) -> None:
        try:
            from tools import platform_cpu_diagnostic_pair as cpu_pair
        except ModuleNotFoundError:  # direct execution from platform/tools
            import platform_cpu_diagnostic_pair as cpu_pair  # type: ignore[no-redef]
        ssh_dir = self.ssh_config.parent
        self._set_stage("ssh_material_validation")
        if (ssh_dir.name != "external-ssh" or self.ssh_config.name != "config"
                or self.ssh_key.parent != ssh_dir or self.ssh_key.name != "id_ed25519"
                or cpu_pair.HOST_RE.fullmatch(self.ssh_host) is None
                or cpu_pair.USER_RE.fullmatch(self.ssh_user) is None):
            raise ValueError("memory observer SSH endpoint rejected")
        cpu_pair._validate_private_ssh_directory(ssh_dir)
        for path in (self.ssh_config, self.ssh_key):
            metadata = path.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise ValueError("memory observer SSH material rejected")
        command = [
            "/usr/bin/ssh", "-F", str(self.ssh_config), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=no", "-o", "ControlPersist=no", "-o", "ControlPath=none",
            "-i", str(self.ssh_key), f"{self.ssh_user}@{self.ssh_host}",
            "/opt/oldsparky/platform/current/tools/platform_workflow_remote_dispatch.py",
            "cpu-diagnostic-memory-watch",
        ]
        request = {
            "schema": 1, "operation": "memory-watch",
            "run_id": self.run_id, "source_sha": self.source_sha,
            "workload": self.workload,
        }
        data = json.dumps(request, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
        self._set_stage("ssh_spawn")
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
            close_fds=True, start_new_session=True,
        )
        if self.process.stdin is None:
            raise OSError("memory observer stdin unavailable")
        self._set_stage("input_write")
        self.process.stdin.write(data)
        self.process.stdin.flush()
        self.process.stdin.close()
        self._set_stage("first_row_read")
        first_row = self._read_row(15)
        if first_row is None:
            raise ValueError("memory observer ended before first row")
        self._set_stage("row_accept")
        self._accept(first_row)
        self._set_stage("thread_start")
        self.thread = threading.Thread(target=self._consume, name="ssr-memory-watch", daemon=True)
        self.thread.start()

    def _consume(self) -> None:
        try:
            while not self.terminal:
                self._set_stage("sample_loop")
                row = self._read_row(5)
                if row is None:
                    raise ValueError("memory observer ended before terminal row")
                self._set_stage("row_accept")
                self._accept(row)
            self._set_stage("terminal_drain")
            self._drain_after_terminal()
            self._set_stage("terminal_wait")
            if self.process is None or self.process.wait(timeout=5) != 0:
                raise ValueError("memory observer exit status rejected")
        except Exception as exc:
            if isinstance(exc, _RemoteWatchFailure):
                self._capture_remote_failure(exc)
                if self.remote_failure_stage in REMOTE_WATCH_FAILURE_STAGES:
                    self._record_failure(self._stage(), ValueError())
            self._record_failure(self._stage(), exc)
            with self._failure_lock:
                self.failure = True
            if self.process is not None and self.process.poll() is None:
                self._terminate()
            try:
                self.sequence += 1
                write_control(self.control_path, {
                    "schema": 1, **self.binding, "decision": "abort",
                    "reason": "observer_failed", "sequence": self.sequence,
                    "updated_monotonic_ns": time.monotonic_ns(),
                }, expected=self.binding)
            except Exception:
                pass

    def _terminate(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    def stop(self) -> dict[str, Any]:
        if self.thread is not None:
            self._set_stage("stop_join")
            self.thread.join(timeout=3)
        if self.thread is not None and self.thread.is_alive():
            self._record_failure("stop_join", TimeoutError())
            with self._failure_lock:
                self.failure = True
            self._terminate()
            self.thread.join(timeout=3)
        if not self.terminal and not self.failure:
            self._record_failure("terminal_wait", TimeoutError())
            with self._failure_lock:
                self.failure = True
        if self.selector is not None:
            self.selector.close()
        if self.process is not None and self.process.poll() is None:
            self._terminate()
        with self._failure_lock:
            failure = self.failure
            failure_stage = self.failure_stage
            error_class = self.error_class
            remote_failure_stage = self.remote_failure_stage
            remote_failure_reason = self.remote_failure_reason
        return {
            "status": "incomplete" if failure or not self.terminal else "complete",
            "samples": self.samples,
            "current_min_bytes": self.current_min,
            "current_max_bytes": self.current_max,
            "memory_peak_lifetime_max_bytes": self.peak_max,
            "memory_events_sampled_max_delta": dict(sorted(self.event_max.items())),
            "target_identity_status": "complete" if self.expected_target is not None and not failure else "unknown",
            "terminal_decision": self.terminal_decision,
            "terminal_reason": "observer_failed" if failure else self.terminal_reason or "missing_terminal",
            "ssh_material_restored": True,
            "failure_stage": failure_stage,
            "error_class": error_class,
            "remote_failure_stage": remote_failure_stage,
            "remote_failure_reason": remote_failure_reason,
        }

    def fail_closed(self, reason: str = "observer_failed") -> None:
        if reason not in {"observer_failed", "collection_error", "identity_changed"}:
            reason = "observer_failed"
        self._record_failure(self._stage(), RuntimeError())
        with self._failure_lock:
            self.failure = True
        self.sequence += 1
        try:
            write_control(self.control_path, {
                "schema": 1, **self.binding, "decision": "abort", "reason": reason,
                "sequence": self.sequence, "updated_monotonic_ns": time.monotonic_ns(),
            }, expected=self.binding)
        except Exception:
            pass
        self._terminate()


def _profile_spec(workload: str) -> tuple[str, frozenset[int], int]:
    if workload == WORKLOAD:
        return FIXTURE_KIND, CONCURRENCY_PRESETS, 8
    if workload == HTML_WORKLOAD:
        return HTML_FIXTURE_KIND, HTML_CONCURRENCY_PRESETS, FIXTURE_USERS
    raise ValueError("capacity workload rejected")


def _capacity_error_code(result: Any) -> str:
    """Reduce one request failure to a closed, identity-free error label."""

    if isinstance(result.error_kind, str) and result.error_kind in {
        "unexpected_content_type", "page_marker_missing",
    }:
        return result.error_kind
    code = external_load.safe_error_class(
        result.error_kind or "unexpected", status=result.status,
    )
    return code if code in CAPACITY_ERROR_CODES else "other"


def _worker_counts_are_closed(payload: Mapping[str, Any]) -> bool:
    statuses = payload.get("http_status_counts")
    errors = payload.get("error_counts")
    completed = payload.get("completed")
    if (not isinstance(statuses, dict) or not isinstance(errors, dict)
            or type(completed) is not int or completed < 0):
        return False
    if any(
        not isinstance(key, str)
        or re.fullmatch(r"(?:[1-5][0-9]{2}|unknown)", key) is None
        or type(count) is not int or count < 0
        for key, count in statuses.items()
    ) or sum(statuses.values()) != completed:
        return False
    if any(
        not isinstance(key, str) or key not in CAPACITY_ERROR_CODES
        or type(count) is not int or count < 0
        for key, count in errors.items()
    ) or sum(errors.values()) > completed:
        return False
    return payload.get("status") != "complete" or not errors


def _warmup_exception_class(value: Any) -> str:
    name = type(value).__name__ if isinstance(value, BaseException) else value
    if not isinstance(name, str):
        return "other"
    if name == "TimeoutError":
        return "TimeoutError"
    if name in WARMUP_HTTP_EXCEPTION_NAMES:
        return "HTTPException"
    if name in WARMUP_OS_ERROR_NAMES:
        return "OSError"
    if name == "ValueError":
        return "ValueError"
    if name == "TypeError":
        return "TypeError"
    if name == "RuntimeError":
        return "RuntimeError"
    if name == "SubprocessError":
        return "SubprocessError"
    return "other"


def _new_warmup_failure(submitted: int) -> dict[str, Any]:
    """Start bounded counters; result counts stop at the existing aggregate deadline."""

    return {
        "schema": 1,
        "submitted": submitted,
        "results": 0,
        "future_exceptions": 0,
        "future_timeouts": 0,
        # Counts consumed results whose bounded marker parse completed.
        "marker_parsed_results": 0,
        "max_elapsed_ms": None,
        "http_status_counts": {},
        "content_encoding_counts": {key: 0 for key in sorted(WARMUP_CONTENT_ENCODINGS)},
        "failure_counts": {key: 0 for key in sorted(WARMUP_FAILURE_CATEGORIES)},
        "exception_class_counts": {key: 0 for key in sorted(WARMUP_EXCEPTION_CLASSES)},
    }


def _record_warmup_result(summary: dict[str, Any], result: Any) -> bool:
    """Project one warmup RequestResult into bounded, identity-free counters."""

    if not isinstance(result, external_load.RequestResult):
        raise TypeError("warmup future returned an invalid request result")
    summary["results"] += 1
    encoding = result.html_content_encoding
    if not isinstance(encoding, str) or encoding not in WARMUP_CONTENT_ENCODINGS:
        encoding = "unknown"
    summary["content_encoding_counts"][encoding] += 1
    failures = summary["failure_counts"]
    prior_failures = dict(failures)
    status = result.status
    if type(status) is int and 100 <= status <= 599:
        status_key = str(status)
        status_counts = summary["http_status_counts"]
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        if status != 200:
            failures["non_200"] += 1
    elif type(status) is int and status == 0:
        failures["request_exception"] += 1
        summary["exception_class_counts"][_warmup_exception_class(result.error_kind)] += 1
    else:
        failures["invalid_result_status"] += 1

    if result.html_content_type_ok is False:
        failures["content_type_mismatch"] += 1
    flags = result.html_marker_flags
    flags_valid = (
        isinstance(flags, Mapping)
        and set(flags) == set(WARMUP_MARKER_FIELDS)
        and all(type(flags[key]) is bool for key in WARMUP_MARKER_FIELDS)
    )
    if flags_valid:
        summary["marker_parsed_results"] += 1
        marker_missing = result.html_page_marker_ok is False or any(
            flags[marker_name] is False for marker_name in WARMUP_MARKER_FIELDS
        )
        if marker_missing:
            failures["page_marker_missing"] += 1
        for marker_name, category in WARMUP_MARKER_FIELDS.items():
            if flags[marker_name] is False:
                failures[category] += 1
    elif result.html_page_marker_ok is False:
        failures["marker_parse_unavailable"] += 1

    elapsed = result.elapsed_ms
    elapsed_type_ok = type(elapsed) in (int, float) and not isinstance(elapsed, bool)
    elapsed_finite = elapsed_type_ok and (
        type(elapsed) is int or math.isfinite(elapsed)
    )
    if elapsed_finite and elapsed >= 0:
        bounded_elapsed = min(30_000.0, elapsed)
        prior = summary["max_elapsed_ms"]
        summary["max_elapsed_ms"] = math.ceil(max(prior or 0.0, bounded_elapsed) * 1000) / 1000
        if elapsed > 5_000:
            failures["elapsed_limit"] += 1
    else:
        failures["invalid_timing"] += 1

    success = (
        result.ok is True and status == 200
        and elapsed_finite and elapsed <= 5_000
    )
    if result.ok is not True and not any(
        failures[key] > prior_failures[key] for key in (
            "request_exception", "non_200", "content_type_mismatch",
            "page_marker_missing", "elapsed_limit", "invalid_timing",
            "marker_lifecycle_missing", "marker_authenticated_header_missing",
            "marker_workspace_not_ready", "marker_detail_fallback_missing",
            "marker_parse_unavailable",
        )
    ):
        failures["unclassified_result_failure"] += 1
    return success


def _warmup_failure_is_closed(value: Any, *, concurrency: int) -> bool:
    if (not isinstance(value, dict) or set(value) != WARMUP_FAILURE_FIELDS
            or type(value.get("schema")) is not int or value["schema"] != 1
            or type(concurrency) is not int or not 1 <= concurrency <= 64
            or type(value.get("submitted")) is not int or value["submitted"] != concurrency
            or type(value.get("results")) is not int or not 0 <= value["results"] <= concurrency
            or type(value.get("marker_parsed_results")) is not int
            or not 0 <= value["marker_parsed_results"] <= value["results"]
            or type(value.get("future_exceptions")) is not int
            or not 0 <= value["future_exceptions"] <= concurrency
            or type(value.get("future_timeouts")) is not int
            or not 0 <= value["future_timeouts"] <= concurrency
            or value["results"] + value["future_exceptions"] + value["future_timeouts"] != concurrency
            or (value["future_timeouts"] and value["future_exceptions"] + value["results"] >= concurrency)):
        return False
    maximum = value.get("max_elapsed_ms")
    if (maximum is not None and (type(maximum) not in (int, float)
            or isinstance(maximum, bool) or not 0 <= maximum <= 30_000
            or (type(maximum) is float and not math.isfinite(maximum)))):
        return False
    statuses = value.get("http_status_counts")
    if (not isinstance(statuses, dict) or len(statuses) > concurrency
            or any(not isinstance(key, str) or re.fullmatch(r"[1-5][0-9]{2}", key) is None
                   or type(count) is not int or not 0 <= count <= concurrency
                   for key, count in statuses.items())
            or sum(statuses.values()) > value["results"]):
        return False
    encodings = value.get("content_encoding_counts")
    if (not isinstance(encodings, dict) or set(encodings) != WARMUP_CONTENT_ENCODINGS
            or any(type(count) is not int or not 0 <= count <= concurrency
                   for count in encodings.values())
            or sum(encodings.values()) != value["results"]):
        return False
    failures = value.get("failure_counts")
    if (not isinstance(failures, dict) or set(failures) != WARMUP_FAILURE_CATEGORIES
            or any(type(count) is not int or not 0 <= count <= concurrency
                   for count in failures.values())
            or any(failures[key] > value["results"] for key in (
                "request_exception", "non_200", "content_type_mismatch",
                "page_marker_missing", "marker_lifecycle_missing",
                "marker_authenticated_header_missing", "marker_workspace_not_ready",
                "marker_detail_fallback_missing", "marker_parse_unavailable", "elapsed_limit",
                "unclassified_result_failure", "invalid_timing", "invalid_result_status",
            ))
            or failures["future_timeout"] != value["future_timeouts"]
            or failures["unexpected_worker_exception"] != value["future_exceptions"]
            or sum(statuses.values()) + failures["request_exception"]
            + failures["invalid_result_status"] != value["results"]
            or failures["non_200"] != sum(
                count for status, count in statuses.items() if status != "200"
            )
            or any(failures[marker] > failures["page_marker_missing"] for marker in (
                "marker_lifecycle_missing", "marker_authenticated_header_missing",
                "marker_workspace_not_ready", "marker_detail_fallback_missing",
            ))
            or failures["page_marker_missing"] > value["marker_parsed_results"]
            or failures["marker_parse_unavailable"] > (
                value["results"] - value["marker_parsed_results"]
            )
            or (failures["elapsed_limit"] > 0) != (
                maximum is not None and maximum > 5_000
            )):
        return False
    classes = value.get("exception_class_counts")
    if (not isinstance(classes, dict) or set(classes) != WARMUP_EXCEPTION_CLASSES
            or any(type(count) is not int or not 0 <= count <= concurrency
                   for count in classes.values())
            or sum(classes.values()) != value["future_exceptions"] + failures["request_exception"]):
        return False
    if not any(failures.values()):
        return False
    if (value["results"] == 0 and (maximum is not None or statuses)):
        return False
    return True


def _project_warmup_failure(value: Any, *, concurrency: int) -> dict[str, Any] | None:
    if not _warmup_failure_is_closed(value, concurrency=concurrency):
        return None
    assert isinstance(value, dict)
    maximum = value["max_elapsed_ms"]
    return {
        "schema": 1,
        "submitted": concurrency,
        "results": value["results"],
        "future_exceptions": value["future_exceptions"],
        "future_timeouts": value["future_timeouts"],
        "marker_parsed_results": value["marker_parsed_results"],
        "max_elapsed_ms": round(float(maximum), 3) if maximum is not None else None,
        "http_status_counts": dict(sorted(value["http_status_counts"].items())),
        "content_encoding_counts": {
            key: value["content_encoding_counts"][key]
            for key in sorted(WARMUP_CONTENT_ENCODINGS)
        },
        "failure_counts": {
            key: value["failure_counts"][key]
            for key in sorted(WARMUP_FAILURE_CATEGORIES)
        },
        "exception_class_counts": {
            key: value["exception_class_counts"][key]
            for key in sorted(WARMUP_EXCEPTION_CLASSES)
        },
    }


def profile_payload(concurrency: int, workload: str = WORKLOAD) -> dict[str, Any]:
    fixture_kind, presets, actors = _profile_spec(workload)
    if type(concurrency) is not int or concurrency not in presets:
        raise ValueError("capacity preset rejected")
    return {
        "schema": SCHEMA,
        "workload": workload,
        "fixture_kind": fixture_kind,
        "fixture_tournaments": FIXTURE_TOURNAMENTS,
        "fixture_users": FIXTURE_USERS,
        "actors": actors,
        "concurrency": concurrency,
        "max_inflight": concurrency,
        "load_model": "closed_loop_best_effort",
        "window_ms": ON_WINDOW_MS,
        "matrix_credit": False,
        "slo_credit": False,
    }


def profile_digest(concurrency: int, workload: str = WORKLOAD) -> str:
    raw = json.dumps(
        profile_payload(concurrency, workload), ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _guard_reason_for_local_window(
    reason: str | None, *, local_window_end_monotonic: float,
    now_monotonic: float,
) -> str | None:
    if reason == "window_complete" and now_monotonic < local_window_end_monotonic:
        return "observer_timing_error"
    return reason


def _read_manifest(path: Path) -> tuple[bytes, dict[str, Any], list[external_load.VirtualUser]]:
    try:
        path_stat = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("capacity manifest unavailable") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size <= 0 or before.st_size > 24 * 1024 * 1024
                or (path_stat.st_dev, path_stat.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("capacity manifest metadata rejected")
        raw = bytearray()
        while len(raw) <= 24 * 1024 * 1024:
            block = os.read(fd, min(65536, 24 * 1024 * 1024 + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        current = path.lstat()
        identity_before = (before.st_dev, before.st_ino, before.st_mode, before.st_uid,
                           before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                          after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        identity_path = (current.st_dev, current.st_ino, current.st_mode, current.st_uid,
                         current.st_nlink, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
        if len(raw) != before.st_size or identity_before != identity_after or identity_before != identity_path:
            raise ValueError("capacity manifest changed")
    finally:
        os.close(fd)
    manifest_raw = bytes(raw)
    manifest, actors = external_load.load_manifest_bytes(manifest_raw)
    return manifest_raw, manifest, actors


def _worker_callback(config: Mapping[str, Any]) -> Mapping[str, Any]:
    capacity = config.get("capacity_diagnostic")
    expected_fields = {
        "schema", "workload", "fixture_kind", "manifest_path", "manifest_sha256",
        "manifest_bytes", "control_path", "binding", "concurrency", "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms",
    }
    if (not isinstance(capacity, dict) or set(capacity) != expected_fields
            or type(capacity.get("schema")) is not int or capacity["schema"] != SCHEMA
            or not isinstance(capacity.get("workload"), str)
            or capacity.get("workload") not in {WORKLOAD, HTML_WORKLOAD}
            or capacity.get("fixture_kind") != _profile_spec(capacity["workload"])[0]
            or type(capacity.get("concurrency")) is not int
            or capacity["concurrency"] not in _profile_spec(capacity["workload"])[1]
            or type(capacity.get("manifest_bytes")) is not int
            or not 0 < capacity["manifest_bytes"] <= 256 * 1024
            or type(capacity.get("on_start_ms")) is not int
            or type(capacity.get("on_end_ms")) is not int
            or type(capacity.get("off_start_ms")) is not int
            or type(capacity.get("off_end_ms")) is not int
            or capacity["off_end_ms"] - capacity["off_start_ms"] != ON_WINDOW_MS
            or capacity["on_start_ms"] - capacity["off_end_ms"] != 5_000
            or capacity["on_end_ms"] - capacity["on_start_ms"] != ON_WINDOW_MS):
        raise ValueError("capacity worker config rejected")
    binding = capacity.get("binding")
    binding_fields = {
        "run_id", "source_sha", "app_target_sha", "profile_digest",
        "external_run_id", "external_run_attempt",
    }
    if (not isinstance(binding, dict) or set(binding) != binding_fields
            or not isinstance(binding.get("run_id"), str) or RUN_RE.fullmatch(binding["run_id"]) is None
            or not isinstance(binding.get("source_sha"), str) or SHA_RE.fullmatch(binding["source_sha"]) is None
            or not isinstance(binding.get("app_target_sha"), str) or SHA_RE.fullmatch(binding["app_target_sha"]) is None
            or binding.get("profile_digest") != profile_digest(capacity["concurrency"], capacity["workload"])
            or not isinstance(binding.get("external_run_id"), str)
            or not binding["external_run_id"].isdecimal()
            or not isinstance(binding.get("external_run_attempt"), str)
            or ATTEMPT_RE.fullmatch(binding["external_run_attempt"]) is None
            or os.environ.get("SOURCE_GIT_SHA") != binding["source_sha"]):
        raise ValueError("capacity worker binding rejected")
    manifest_path = Path(str(capacity["manifest_path"]))
    manifest_raw, manifest, actors = _read_manifest(manifest_path)
    if (hashlib.sha256(manifest_raw).hexdigest() != capacity["manifest_sha256"]
            or manifest.get("runner_sha") != binding["source_sha"]
            or manifest.get("app_target_sha") != binding["app_target_sha"]):
        raise ValueError("capacity manifest binding rejected")
    tournaments = manifest.get("tournaments")
    if (not isinstance(tournaments, list) or len(tournaments) != FIXTURE_TOURNAMENTS
            or len(actors) != FIXTURE_USERS
            or any(item.get("user_count") != FIXTURE_USERS for item in tournaments)):
        raise ValueError("capacity fixture dimensions rejected")
    slug = tournaments[0].get("slug")
    actor_count = _profile_spec(capacity["workload"])[2]
    selected = [actor for actor in actors if actor.tournament_slug == slug][:actor_count]
    if len(selected) != actor_count:
        raise ValueError("capacity actor cohort rejected")
    worker_report = Path(str(config.get("worker_report_path", "")))
    control_path = Path(str(capacity["control_path"]))
    if (not worker_report.is_absolute() or control_path.name != CONTROL_BASENAME
            or control_path.parent.resolve() != worker_report.parent.resolve()):
        raise ValueError("capacity control path rejected")

    sequence = -1

    def guard() -> str | None:
        nonlocal sequence
        reason, sequence_value = stop_reason_from_control(
            control_path, expected=binding, previous_sequence=sequence,
        )
        sequence = max(sequence, sequence_value)
        return reason

    cookie_names = (manifest["session_cookie_name"], manifest["csrf_cookie_name"])
    probe_count = capacity["concurrency"]
    initial_reason = guard()
    if initial_reason is not None:
        return _incomplete_report(
            capacity, binding, initial_reason, submitted=0, completed=0,
            observed_duration_ms=0,
            manifest_sha256=capacity["manifest_sha256"],
        )
    capacity_executor = ThreadPoolExecutor(
        max_workers=probe_count, thread_name_prefix="ssr-capacity",
    )

    def request_builder(origin: str, actor: external_load.VirtualUser, phase: str, timeout: float):
        return external_load._page_request(
            origin, actor, phase, timeout,
            session_cookie_name=cookie_names[0],
            csrf_cookie_name=cookie_names[1],
            transport=external_load.HTTP11_KEEPALIVE_TRANSPORT,
            diagnostic_trace_run_id=binding["run_id"],
            require_authenticated_html=capacity["workload"] == HTML_WORKLOAD,
        )

    off_results: list[Any] = []
    off_failed = False

    def untraced_warmup(actor: external_load.VirtualUser):
        return external_load._page_request(
            manifest["origin"], actor, "authenticated_workspace_capacity_warmup", 5.0,
            session_cookie_name=cookie_names[0], csrf_cookie_name=cookie_names[1],
            transport=external_load.HTTP11_KEEPALIVE_TRANSPORT,
            require_authenticated_html=capacity["workload"] == HTML_WORKLOAD,
        )

    def marked_probe(actor: external_load.VirtualUser, barrier: threading.Barrier):
        try:
            barrier.wait(timeout=5.0)
        except threading.BrokenBarrierError:
            return None
        return external_load._page_request(
            manifest["origin"], actor, "authenticated_workspace_capacity_off_probe", 5.0,
            session_cookie_name=cookie_names[0], csrf_cookie_name=cookie_names[1],
            transport=external_load.HTTP11_KEEPALIVE_TRANSPORT,
            diagnostic_trace_run_id=binding["run_id"],
            require_authenticated_html=capacity["workload"] == HTML_WORKLOAD,
        )

    def fail_off(
        reason: str, *, submitted: int, completed: int,
        warmup_failure: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        capacity_executor.shutdown(wait=True, cancel_futures=True)
        return _incomplete_report(
            capacity, binding, reason, submitted=0, completed=0,
            observed_duration_ms=0,
            off_probe_submitted=submitted, off_probe_completed=completed,
            off_probe_status=None, manifest_sha256=capacity["manifest_sha256"],
            warmup_failure=warmup_failure,
        )

    warm_futures = [
        capacity_executor.submit(untraced_warmup, selected[index % len(selected)])
        for index in range(probe_count)
    ]
    warmup_failure = _new_warmup_failure(probe_count)
    try:
        for future in as_completed(warm_futures, timeout=5.5):
            try:
                result = future.result()
            except Exception as exc:
                off_failed = True
                warmup_failure["future_exceptions"] += 1
                warmup_failure["failure_counts"]["unexpected_worker_exception"] += 1
                warmup_failure["exception_class_counts"][_warmup_exception_class(exc)] += 1
                continue
            if not isinstance(result, external_load.RequestResult):
                off_failed = True
                warmup_failure["future_exceptions"] += 1
                warmup_failure["failure_counts"]["unexpected_worker_exception"] += 1
                warmup_failure["exception_class_counts"]["TypeError"] += 1
                continue
            if not _record_warmup_result(warmup_failure, result):
                off_failed = True
    except TimeoutError:
        off_failed = True
        # shutdown() may later drain these futures, but they had no result at
        # the existing 5.5s observation deadline and remain timeouts here.
        warmup_failure["future_timeouts"] = (
            probe_count - warmup_failure["results"] - warmup_failure["future_exceptions"]
        )
        warmup_failure["failure_counts"]["future_timeout"] = warmup_failure["future_timeouts"]
    if off_failed:
        return fail_off(
            "off_warmup_failed", submitted=0, completed=0,
            warmup_failure=warmup_failure,
        )

    # Warm the same thread-local HTTP/1.1 clients before the plan's OFF
    # boundary. The marked cohort is synchronized to the immutable plan time;
    # warmup and its TLS latency are excluded from the measured probe window.
    while True:
        initial = guard()
        if initial is not None:
            capacity_executor.shutdown(wait=True, cancel_futures=True)
            return _incomplete_report(
                capacity, binding, initial, submitted=0, completed=0,
                observed_duration_ms=0,
                manifest_sha256=capacity["manifest_sha256"],
            )
        now_ms = time.time_ns() // 1_000_000
        if now_ms >= capacity["off_start_ms"]:
            break
        time.sleep(min(0.025, max(0.0, (capacity["off_start_ms"] - now_ms) / 1000)))

    if time.time_ns() // 1_000_000 - capacity["off_start_ms"] > 250:
        return fail_off("off_probe_window_missed", submitted=0, completed=0)
    off_start_edge = _capture_cpu_edge(capacity["off_start_ms"])
    off_probe_start = time.monotonic()
    barrier = threading.Barrier(probe_count)
    probe_futures = [
        capacity_executor.submit(marked_probe, selected[index % len(selected)], barrier)
        for index in range(probe_count)
    ]
    try:
        for future in as_completed(probe_futures, timeout=5.5):
            try:
                result = future.result()
            except Exception:
                off_failed = True
                continue
            if result is None:
                off_failed = True
            else:
                off_results.append(result)
                if result.ok is not True or result.status != 200 or result.elapsed_ms > 5_000:
                    off_failed = True
    except TimeoutError:
        off_failed = True
    off_probe_elapsed = time.monotonic() - off_probe_start
    off_end_edge = _capture_cpu_edge(capacity["off_end_ms"])
    off_cpu_window = _cpu_window_row("off", off_start_edge, off_end_edge)
    if len(off_results) != probe_count or off_failed or off_probe_elapsed > 11.0:
        return fail_off(
            "off_probe_failed", submitted=probe_count, completed=len(off_results),
        )
    on_start_monotonic = time.monotonic() + max(
        0.0, (capacity["on_start_ms"] - time.time_ns() // 1_000_000) / 1000,
    )
    while time.monotonic() < on_start_monotonic:
        reason = guard()
        if reason is not None:
            capacity_executor.shutdown(wait=True, cancel_futures=True)
            return _incomplete_report(
                capacity, binding, reason, submitted=0, completed=0,
                observed_duration_ms=0,
                off_probe_submitted=probe_count, off_probe_completed=probe_count,
                off_probe_status=200,
                manifest_sha256=capacity["manifest_sha256"],
            )
        time.sleep(min(0.025, on_start_monotonic - time.monotonic()))

    on_start_edge = _capture_cpu_edge(capacity["on_start_ms"])

    local_window_end_monotonic = on_start_monotonic + ON_WINDOW_MS / 1000

    def timed_guard() -> str | None:
        return _guard_reason_for_local_window(
            guard(), local_window_end_monotonic=local_window_end_monotonic,
            now_monotonic=time.monotonic(),
        )

    try:
        phase_result = external_load.run_authenticated_workspace_capacity_phase(
            manifest["origin"], selected,
            phase="authenticated_workspace_read_capacity",
            duration_seconds=20,
            start_at_epoch_ms=capacity["on_start_ms"],
            end_at_epoch_ms=capacity["on_end_ms"],
            concurrency=capacity["concurrency"],
            timeout=5,
            request_builder=request_builder,
            stop_requested=timed_guard,
            abort_on_result=(
                (lambda result: "response_failure" if result.ok is not True else None)
                if capacity["workload"] == HTML_WORKLOAD else None
            ),
            max_supported_concurrency=(64 if capacity["workload"] == HTML_WORKLOAD else 16),
            executor=capacity_executor,
        )
    except (external_load.ExternalLoadError, OSError, CapacityControlError):
        return _incomplete_report(
            capacity, binding, "worker_error", submitted=0, completed=0,
            observed_duration_ms=max(
                0, min(30_000, round((time.monotonic() - on_start_monotonic) * 1000)),
            ),
            off_probe_submitted=probe_count, off_probe_completed=probe_count,
            off_probe_status=200,
            manifest_sha256=capacity["manifest_sha256"],
        )
    finally:
        capacity_executor.shutdown(wait=True, cancel_futures=True)
    on_end_edge = _capture_cpu_edge(capacity["on_end_ms"])
    on_cpu_window = _cpu_window_row("on", on_start_edge, on_end_edge)
    results = phase_result["results"]
    statuses = Counter(
        str(item.status) if type(item.status) is int and 100 <= item.status <= 599 else "unknown"
        for item in results
    )
    errors = Counter(
        _capacity_error_code(item)
        for item in results if item.error_kind is not None
    )
    latencies = [float(item.elapsed_ms) for item in results if type(item.elapsed_ms) in (int, float)]
    all_200 = len(results) == phase_result["submitted"] and all(
        item.ok is True and item.status == 200
        and (capacity["workload"] != HTML_WORKLOAD or (
            item.html_content_type_ok is True and item.html_page_marker_ok is True
        ))
        for item in results
    )
    final_reason = phase_result["abort_reason"]
    status = "incomplete" if phase_result["aborted"] else "complete" if all_200 else "failed"
    return {
        **profile_payload(capacity["concurrency"], capacity["workload"]),
        "report_complete": True,
        "status": status,
        "passed": status == "complete" and final_reason in {None, "window_complete"},
        "matrix_credit": False,
        "slo_credit": False,
        "source_sha": binding["source_sha"],
        "app_target_sha": binding["app_target_sha"],
        "external_run_id": binding["external_run_id"],
        "external_run_attempt": binding["external_run_attempt"],
        "run_id": binding["run_id"],
        "profile_digest": binding["profile_digest"],
        "fixture_manifest_sha256": capacity["manifest_sha256"],
        "fixture_tournaments": FIXTURE_TOURNAMENTS,
        "fixture_users": FIXTURE_USERS,
        "actors": actor_count,
        "concurrency": capacity["concurrency"],
        "max_inflight": capacity["concurrency"],
        "fixture_manifest_bytes": len(manifest_raw),
        "on_window_start_ms": phase_result["window_start_at_ms"],
        "on_window_end_ms": phase_result["window_end_at_ms"],
        "observed_duration_ms": phase_result["duration_ms"],
        "submitted": phase_result["submitted"],
        "completed": phase_result["completed"],
        "response_bytes_total": sum(
            item.response_bytes for item in results
            if type(getattr(item, "response_bytes", None)) is int
            and item.response_bytes >= 0
        ),
        "http_status_counts": dict(sorted(statuses.items())),
        "error_counts": dict(sorted(errors.items())),
        "latency_ms": {
            "sample_count": len(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": round(max(latencies), 3) if latencies else None,
        },
        "off_probe": {
            "submitted": probe_count, "completed": probe_count, "http_status": 200,
        },
        "process_cpu_windows": [off_cpu_window, on_cpu_window],
        "resource_abort_reason": final_reason,
        "generator_cpu_evidence": _generator_cpu_evidence_status(
            [off_cpu_window, on_cpu_window],
        ),
        "server_capacity_claim": None,
        "server_capacity_claim_reason": "resource_correlation_not_provided",
        "diagnostic_credit": False,
        "namespace_closed": False,
    }


def _incomplete_report(
    capacity: Mapping[str, Any], binding: Mapping[str, Any], reason: str,
    *, submitted: int, completed: int, observed_duration_ms: int,
    manifest_sha256: str,
    off_probe_submitted: int = 0, off_probe_completed: int = 0,
    off_probe_status: int | None = None,
    warmup_failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if type(observed_duration_ms) is not int or not 0 <= observed_duration_ms <= 30_000:
        raise ValueError("capacity incomplete duration is invalid")
    report = {
        **profile_payload(capacity["concurrency"], capacity["workload"]),
        "report_complete": True,
        "status": "incomplete", "passed": False,
        "matrix_credit": False, "slo_credit": False, "diagnostic_credit": False,
        "source_sha": binding["source_sha"], "app_target_sha": binding["app_target_sha"],
        "external_run_id": binding["external_run_id"],
        "external_run_attempt": binding["external_run_attempt"],
        "run_id": binding["run_id"], "profile_digest": binding["profile_digest"],
        "fixture_manifest_sha256": manifest_sha256,
        "fixture_tournaments": FIXTURE_TOURNAMENTS, "fixture_users": FIXTURE_USERS,
        "actors": _profile_spec(capacity["workload"])[2], "concurrency": capacity["concurrency"],
        "max_inflight": capacity["concurrency"],
        "fixture_manifest_bytes": capacity.get("manifest_bytes", 0),
        "on_window_start_ms": capacity["on_start_ms"],
        "on_window_end_ms": capacity["on_end_ms"],
        # This field measures the ON window; pre-ON aborts pass zero explicitly.
        "observed_duration_ms": observed_duration_ms,
        "submitted": submitted, "completed": completed,
        "response_bytes_total": 0,
        "http_status_counts": {}, "error_counts": {},
        "latency_ms": {"sample_count": 0, "p50": None, "p95": None, "p99": None, "max": None},
        "off_probe": {
            "submitted": off_probe_submitted, "completed": off_probe_completed,
            "http_status": off_probe_status,
        },
        "process_cpu_windows": _unknown_cpu_windows(),
        "resource_abort_reason": reason,
        "generator_cpu_evidence": "incomplete",
        "server_capacity_claim": None,
        "server_capacity_claim_reason": "resource_correlation_not_provided",
        "namespace_closed": False,
    }
    if warmup_failure is not None:
        if (reason != "off_warmup_failed"
                or not _warmup_failure_is_closed(
                    dict(warmup_failure), concurrency=capacity["concurrency"],
                )):
            raise ValueError("capacity warmup failure projection rejected")
        report["warmup_failure"] = dict(warmup_failure)
    return report


def _unknown_cpu_windows() -> list[dict[str, Any]]:
    return [
        {"window": name, "status": "incomplete", "cpu_ns": None,
         "elapsed_ns": None, "cores": None}
        for name in ("off", "on")
    ]


def _valid_cpu_window_rows(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != 2:
        return False
    if [row.get("window") if isinstance(row, dict) else None for row in value] != ["off", "on"]:
        return False
    for row in value:
        if not isinstance(row, dict) or set(row) != {
            "window", "status", "cpu_ns", "elapsed_ns", "cores",
        }:
            return False
        if row["status"] == "incomplete":
            if any(row[key] is not None for key in ("cpu_ns", "elapsed_ns", "cores")):
                return False
        elif row["status"] == "complete":
            if (type(row["cpu_ns"]) is not int or row["cpu_ns"] < 0
                    or type(row["elapsed_ns"]) is not int or row["elapsed_ns"] <= 0
                    or abs(row["elapsed_ns"] - 20_000_000_000) > CPU_WINDOW_TOLERANCE_NS
                    or type(row["cores"]) not in (int, float)
                    or isinstance(row["cores"], bool) or not math.isfinite(row["cores"])
                    or row["cores"] < 0 or row["cores"] > 128
                    or abs(row["cores"] - row["cpu_ns"] / row["elapsed_ns"]) > 0.00001):
                return False
        else:
            return False
    return True


def _generator_cpu_evidence_status(rows: Any) -> str:
    if (not _valid_cpu_window_rows(rows)
            or any(row["status"] != "complete" for row in rows)):
        return "incomplete"
    return "measured"


def _capture_cpu_edge(target_ms: int) -> dict[str, int]:
    target_ns = target_ms * 1_000_000
    while True:
        remaining_ns = target_ns - time.time_ns()
        if remaining_ns <= 0:
            break
        time.sleep(min(0.025, remaining_ns / 1_000_000_000))
    wall_before = time.time_ns()
    mono_before = time.monotonic_ns()
    cpu_ns = time.process_time_ns()
    mono_after = time.monotonic_ns()
    wall_after = time.time_ns()
    return {
        "target_ns": target_ns, "wall_before_ns": wall_before,
        "wall_after_ns": wall_after, "mono_before_ns": mono_before,
        "mono_after_ns": mono_after, "cpu_ns": cpu_ns,
    }


def _cpu_window_row(
    window: str, start: Mapping[str, int], end: Mapping[str, int],
) -> dict[str, Any]:
    elapsed_ns = end["mono_before_ns"] - start["mono_before_ns"]
    cpu_ns = end["cpu_ns"] - start["cpu_ns"]
    duration_ns = end["target_ns"] - start["target_ns"]
    aligned = all(
        abs((edge["wall_before_ns"] + edge["wall_after_ns"]) // 2 - edge["target_ns"])
        <= CPU_EDGE_TOLERANCE_NS
        for edge in (start, end)
    )
    complete = (
        aligned and cpu_ns >= 0
        and abs(elapsed_ns - duration_ns) <= CPU_WINDOW_TOLERANCE_NS
    )
    return {
        "window": window,
        "status": "complete" if complete else "incomplete",
        "cpu_ns": cpu_ns if complete else None,
        "elapsed_ns": elapsed_ns if complete else None,
        "cores": round(cpu_ns / elapsed_ns, 6) if complete and elapsed_ns > 0 else None,
    }


def worker_entrypoint() -> int:
    return load_runtime.worker_entry(_worker_callback)


def _namespace_worker_binding(
    *, source_sha: str, app_target_sha: str, external_run_id: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Carry the already-validated manifest attestation into the launcher config."""

    return {
        "source_git_sha": source_sha,
        "app_target_sha": app_target_sha,
        "source_binding": manifest.get("source_binding"),
        "source_binding_sha256": manifest.get("source_binding_sha256"),
        "external_run_id": external_run_id,
    }


def run_isolated_capacity_worker(
    *,
    stage_state: dict[str, Any],
    capacity_config: Mapping[str, Any],
    worker_binding: Mapping[str, Any],
    worker_report_path: Path,
    supervisor_report_path: Path,
    source_sha: str,
    watcher: _RemoteMemoryWatcher,
    hide_ssh_material,
    restore_ssh_material,
    maximum_runtime_seconds: float,
    on_start_ms: int,
    on_end_ms: int,
) -> dict[str, Any]:
    """Run the typed worker behind the repository's mandatory PID namespace."""

    if not SHA_RE.fullmatch(source_sha) or maximum_runtime_seconds <= 0 or maximum_runtime_seconds > 180:
        raise ValueError("capacity supervisor bounds rejected")
    runner = external_load.RunnerCgroupSampler(
        runner_environment=os.environ.get("PLATFORM_RUNNER_ENVIRONMENT"),
    )
    watcher_summary: list[dict[str, Any]] = []
    runner_summary: list[dict[str, Any]] = []
    hidden = {"value": False}
    wall_anchor_ms = time.time_ns() // 1_000_000
    monotonic_anchor = time.monotonic()
    on_window_monotonic = (
        monotonic_anchor + (on_start_ms - wall_anchor_ms) / 1000,
        monotonic_anchor + (on_end_ms - wall_anchor_ms) / 1000,
    )

    def on_namespace_init(pid: int, starttime: int) -> None:
        runner.start(pid, starttime)
        try:
            watcher.start()
            hide_ssh_material()
            hidden["value"] = True
        except Exception:
            watcher.fail_closed()

    def on_namespace_exit(pid: int, starttime: int) -> None:
        watcher_summary.append(watcher.stop())
        runner_summary.append(runner.stop(
            namespace_pid=pid, namespace_starttime=starttime,
            phase_window_monotonic=on_window_monotonic,
        ))
        if hidden["value"]:
            try:
                restore_ssh_material()
                hidden["value"] = False
            except Exception:
                watcher_summary[-1]["ssh_material_restored"] = False

    result = load_runtime.run_supervised(
        worker_command=(sys.executable, str(Path(__file__).resolve()), "worker-entry"),
        report_path=supervisor_report_path,
        worker_report_path=worker_report_path,
        max_duration_seconds=maximum_runtime_seconds,
        max_runner_minutes=4,
        worker_config={
            "binding": dict(worker_binding),
            "capacity_diagnostic": dict(capacity_config),
        },
        clear_worker_environment=True,
        env={"SOURCE_GIT_SHA": source_sha},
        on_namespace_init=on_namespace_init,
        on_namespace_exit=on_namespace_exit,
    )
    supervisor_report = getattr(result, "report", None)
    supervisor_runtime = (
        supervisor_report.get("runtime_supervisor")
        if isinstance(supervisor_report, Mapping) else None
    )
    runtime_budget = (
        supervisor_report.get("runtime_budget")
        if isinstance(supervisor_report, Mapping) else None
    )
    reason = getattr(result, "reason", None)
    stage_state["supervisor_evidence"] = _project_supervisor_evidence({
        "reason": reason,
        "worker_started": getattr(result, "worker_started", None),
        "worker_exited": getattr(result, "worker_exited", None),
        "namespace_closed": getattr(result, "namespace_closed", None),
        "return_code": getattr(result, "returncode", None),
        "signal": getattr(result, "signal", None),
        "startup_failure_stage": getattr(result, "startup_failure_stage", None),
        "namespace_ready_failure_stage": getattr(
            result, "namespace_ready_failure_stage", None,
        ),
        "namespace_post_ready_failure_stage": getattr(
            result, "namespace_post_ready_failure_stage", None,
        ),
        "watchdog_startup_failure_stage": getattr(
            result, "watchdog_startup_failure_stage", None,
        ),
        "report_error": (
            supervisor_runtime.get("report_error")
            if isinstance(supervisor_runtime, Mapping) else None
        ),
        "report_phase": (
            runtime_budget.get("phase")
            if isinstance(runtime_budget, Mapping) else None
        ),
    })
    if not watcher_summary:
        watcher_summary.append(watcher.stop())
    if not runner_summary:
        runner_summary.append(runner.stop(phase_window_monotonic=on_window_monotonic))
    try:
        worker_payload, _worker_report_error = load_runtime._read_closed_report(worker_report_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        worker_payload = None
    namespace_closed = result.namespace_closed is True and reason == "none"
    valid_worker = _worker_report_matches(worker_payload, capacity_config)
    memory = watcher_summary[0]
    observer_failure = {
        "failure_stage": memory.get("failure_stage"),
        "error_class": memory.get("error_class"),
        "remote_failure_stage": memory.get("remote_failure_stage"),
        "remote_failure_reason": memory.get("remote_failure_reason"),
    }
    if any(value is not None for value in observer_failure.values()):
        stage_state["memory_observer_failure"] = observer_failure
    memory = {
        key: value for key, value in memory.items()
        if key not in {
            "failure_stage", "error_class", "remote_failure_stage", "remote_failure_reason",
        }
    }
    if memory.get("memory_events_sampled_max_delta") == {}:
        memory = {**memory, "memory_events_sampled_max_delta": None}
    worker_status = worker_payload.get("status") if valid_worker else "incomplete"
    complete = bool(
        namespace_closed and valid_worker and worker_status == "complete"
        and memory.get("status") == "complete"
        and memory.get("terminal_decision") == "complete"
        and memory.get("ssh_material_restored", True) is True
    )
    runner_headroom = runner_summary[0]
    for field in (
        "window_seconds", "effective_cpu_capacity_cores",
        "max_cgroup_cpu_usage_cores", "max_visible_throttled_periods_delta",
        "window_scope", "window_start_offset_ms", "window_end_offset_ms",
    ):
        runner_headroom.setdefault(field, "on_window" if field == "window_scope" else None)
    outcome = {
        "schema": SCHEMA,
        "report_complete": True,
        "workload": capacity_config["workload"],
        "fixture_kind": capacity_config["fixture_kind"],
        "status": "complete" if complete else (
            "resource_abort" if memory.get("terminal_decision") == "abort" else "incomplete"
        ),
        "passed": complete and worker_payload.get("passed") is True if valid_worker else False,
        "matrix_credit": False,
        "slo_credit": False,
        "diagnostic_credit": False,
        "source_sha": capacity_config["binding"]["source_sha"],
        "app_target_sha": capacity_config["binding"]["app_target_sha"],
        "external_run_id": capacity_config["binding"]["external_run_id"],
        "external_run_attempt": capacity_config["binding"]["external_run_attempt"],
        "run_id": capacity_config["binding"]["run_id"],
        "profile_digest": capacity_config["binding"]["profile_digest"],
        "fixture_manifest_sha256": capacity_config["manifest_sha256"],
        "fixture_manifest_bytes": capacity_config["manifest_bytes"],
        "fixture_tournaments": FIXTURE_TOURNAMENTS,
        "fixture_users": FIXTURE_USERS,
        "actors": _profile_spec(capacity_config["workload"])[2],
        "concurrency": capacity_config["concurrency"],
        "max_inflight": capacity_config["concurrency"],
        "load_model": "closed_loop_best_effort",
        "window_ms": ON_WINDOW_MS,
        "namespace_closed": namespace_closed,
        "worker": {
            key: worker_payload.get(key, _unknown_cpu_windows() if key == "process_cpu_windows" else None) for key in (
                "status", "submitted", "completed", "http_status_counts", "error_counts",
                "latency_ms", "resource_abort_reason", "on_window_start_ms", "on_window_end_ms",
                "observed_duration_ms", "max_inflight", "fixture_manifest_bytes",
                "response_bytes_total", "off_probe", "process_cpu_windows",
            )
        } if valid_worker else None,
        "generator_cpu_evidence": (
            _generator_cpu_evidence_status(
                worker_payload.get("process_cpu_windows", _unknown_cpu_windows()),
            ) if valid_worker else "incomplete"
        ),
        "memory_guard": memory,
        "runner_headroom": runner_headroom,
        "server_capacity_claim": None,
        "server_capacity_claim_reason": "resource_correlation_not_provided",
    }
    if (valid_worker and isinstance(outcome.get("worker"), dict)
            and isinstance(worker_payload, Mapping)
            and "warmup_failure" in worker_payload):
        outcome["worker"]["warmup_failure"] = worker_payload["warmup_failure"]
    return outcome


def _worker_report_matches(payload: Any, capacity_config: Mapping[str, Any]) -> bool:
    binding = capacity_config.get("binding")
    if not isinstance(binding, Mapping) or not isinstance(payload, dict):
        return False
    expected_fields = {
        "worker_report_schema", "partial_work", "inflight_unknown",
        "schema", "workload", "fixture_kind", "fixture_tournaments", "fixture_users",
        "actors", "concurrency", "load_model", "window_ms", "matrix_credit", "slo_credit",
        "report_complete", "status", "passed", "source_sha", "app_target_sha",
        "external_run_id", "external_run_attempt", "run_id", "profile_digest",
        "fixture_manifest_sha256", "on_window_start_ms", "on_window_end_ms",
        "observed_duration_ms", "submitted", "completed", "max_inflight",
        "fixture_manifest_bytes", "response_bytes_total", "http_status_counts",
        "error_counts", "latency_ms", "off_probe", "process_cpu_windows", "resource_abort_reason",
        "generator_cpu_evidence", "server_capacity_claim", "server_capacity_claim_reason",
        "diagnostic_credit", "namespace_closed",
    }
    warmup_fields = expected_fields | {"warmup_failure"}
    if set(payload) not in (
        expected_fields, expected_fields - {"process_cpu_windows"},
        warmup_fields, warmup_fields - {"process_cpu_windows"},
    ):
        return False
    has_warmup_failure = "warmup_failure" in payload
    if (has_warmup_failure != (payload.get("resource_abort_reason") == "off_warmup_failed")
            or (has_warmup_failure and (
                payload.get("status") != "incomplete"
                or not _warmup_failure_is_closed(
                    payload.get("warmup_failure"),
                    concurrency=capacity_config.get("concurrency"),
                )
            ))):
        return False
    cpu_rows = payload.get("process_cpu_windows", _unknown_cpu_windows())
    return bool(
        type(payload.get("worker_report_schema")) is int
        and payload.get("worker_report_schema") == load_runtime.WORKER_REPORT_SCHEMA
        and type(payload.get("partial_work")) is bool
        and type(payload.get("inflight_unknown")) is bool
        and (
            payload.get("status") != "complete"
            or (
                payload.get("partial_work") is False
                and payload.get("inflight_unknown") is False
            )
        )
        and payload.get("report_complete") is True
        and type(payload.get("schema")) is int and payload.get("schema") == SCHEMA
        and payload.get("workload") == capacity_config.get("workload")
        and payload.get("fixture_kind") == capacity_config.get("fixture_kind")
        and payload.get("load_model") == "closed_loop_best_effort"
        and payload.get("window_ms") == ON_WINDOW_MS
        and payload.get("source_sha") == binding.get("source_sha")
        and payload.get("app_target_sha") == binding.get("app_target_sha")
        and payload.get("external_run_id") == binding.get("external_run_id")
        and payload.get("external_run_attempt") == binding.get("external_run_attempt")
        and payload.get("run_id") == binding.get("run_id")
        and payload.get("profile_digest") == binding.get("profile_digest")
        and payload.get("fixture_manifest_sha256") == capacity_config.get("manifest_sha256")
        and payload.get("fixture_manifest_bytes") == capacity_config.get("manifest_bytes")
        and payload.get("concurrency") == capacity_config.get("concurrency")
        and payload.get("max_inflight") == capacity_config.get("concurrency")
        and payload.get("actors") == _profile_spec(capacity_config["workload"])[2]
        and payload.get("generator_cpu_evidence") == _generator_cpu_evidence_status(cpu_rows)
        and _worker_counts_are_closed(payload)
        and payload.get("fixture_tournaments") == FIXTURE_TOURNAMENTS
        and payload.get("fixture_users") == FIXTURE_USERS
        and _valid_cpu_window_rows(cpu_rows)
        and payload.get("on_window_start_ms") == capacity_config.get("on_start_ms")
        and payload.get("on_window_end_ms") == capacity_config.get("on_end_ms")
        and isinstance(payload.get("off_probe"), dict)
        and set(payload["off_probe"]) == {"submitted", "completed", "http_status"}
        and type(payload["off_probe"].get("submitted")) is int
        and 0 <= payload["off_probe"]["submitted"] <= 64
        and type(payload["off_probe"].get("completed")) is int
        and 0 <= payload["off_probe"]["completed"] <= payload["off_probe"]["submitted"]
        and (payload["off_probe"].get("http_status") is None
             or type(payload["off_probe"].get("http_status")) is int)
        and (payload.get("status") != "complete" or payload["off_probe"] == {
            "submitted": capacity_config.get("concurrency"),
            "completed": capacity_config.get("concurrency"), "http_status": 200,
        })
        and payload.get("matrix_credit") is False
        and payload.get("slo_credit") is False
        and payload.get("diagnostic_credit") is False
        and payload.get("namespace_closed") is False
        and isinstance(payload.get("status"), str)
        and payload.get("status") in {"complete", "incomplete", "failed"}
        and type(payload.get("submitted")) is int
        and type(payload.get("completed")) is int
        and 0 <= payload["completed"] <= payload["submitted"] <= 100_000
        and type(payload.get("response_bytes_total")) is int
        and 0 <= payload["response_bytes_total"] <= 10**12
        and type(payload.get("observed_duration_ms")) is int
        and 0 <= payload["observed_duration_ms"] <= 30_000
        and isinstance(payload.get("http_status_counts"), dict)
        and isinstance(payload.get("error_counts"), dict)
    )


def _hide_parent_ssh_material(directory: Path) -> tuple[dict[str, bytearray], dict[str, int]]:
    """Remove the exact parent SSH files after its persistent observer connects."""
    try:
        from tools import platform_cpu_diagnostic_pair as cpu_pair
    except ModuleNotFoundError:  # direct execution from platform/tools
        import platform_cpu_diagnostic_pair as cpu_pair  # type: ignore[no-redef]
    if directory.name != "external-ssh":
        raise ValueError("capacity SSH directory rejected")
    before = cpu_pair._validate_private_ssh_directory(directory)
    expected_names = {"config", "id_ed25519", "known_hosts"}
    directory_fd = os.open(
        directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    contents: dict[str, bytearray] = {}
    try:
        opened = os.fstat(directory_fd)
        if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or set(os.listdir(directory_fd)) != expected_names):
            raise ValueError("capacity SSH inventory rejected")
        identities: dict[str, tuple[int, int, int, int, int]] = {}
        for name in sorted(expected_names):
            fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                metadata = os.fstat(fd)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                        or metadata.st_size <= 0 or metadata.st_size > 64 * 1024):
                    raise ValueError("capacity SSH file metadata rejected")
                raw = bytearray()
                while len(raw) <= 64 * 1024:
                    block = os.read(fd, min(4096, 64 * 1024 + 1 - len(raw)))
                    if not block:
                        break
                    raw.extend(block)
                after = os.fstat(fd)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                identity = (metadata.st_dev, metadata.st_ino, metadata.st_mode,
                            metadata.st_uid, metadata.st_size)
                if (len(raw) != metadata.st_size or identity != (
                        after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_size
                    ) or identity != (
                        current.st_dev, current.st_ino, current.st_mode,
                        current.st_uid, current.st_size
                    )):
                    raise ValueError("capacity SSH file changed")
                contents[name] = raw
                identities[name] = identity
            finally:
                os.close(fd)
        for name in sorted(expected_names):
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode, current.st_uid,
                    current.st_size) != identities[name]:
                raise ValueError("capacity SSH file changed before removal")
            os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        after_dir = os.fstat(directory_fd)
        current_dir = directory.lstat()
        if ((after_dir.st_dev, after_dir.st_ino) != (opened.st_dev, opened.st_ino)
                or (current_dir.st_dev, current_dir.st_ino) != (opened.st_dev, opened.st_ino)
                or set(os.listdir(directory_fd))):
            raise ValueError("capacity SSH directory changed during removal")
    except BaseException:
        if contents:
            try:
                cpu_pair._restore_ssh_files(directory, contents, {
                    "directory_dev": before.st_dev, "directory_ino": before.st_ino,
                })
            except Exception:
                pass
        for value in contents.values():
            value[:] = b"\0" * len(value)
        raise
    finally:
        os.close(directory_fd)
    try:
        directory.rmdir()
    except OSError as exc:
        try:
            cpu_pair._restore_ssh_files(directory, contents, {
                "directory_dev": before.st_dev, "directory_ino": before.st_ino,
            })
        except Exception:
            pass
        for value in contents.values():
            value[:] = b"\0" * len(value)
        raise ValueError("capacity SSH directory could not be hidden") from exc
    return contents, {"directory_dev": before.st_dev, "directory_ino": before.st_ino}


def _restore_parent_ssh_material(
    directory: Path, contents: dict[str, bytearray], lifecycle: dict[str, int]
) -> None:
    try:
        from tools import platform_cpu_diagnostic_pair as cpu_pair
    except ModuleNotFoundError:  # direct execution from platform/tools
        import platform_cpu_diagnostic_pair as cpu_pair  # type: ignore[no-redef]
    cpu_pair._restore_ssh_files(directory, contents, lifecycle)
    for value in contents.values():
        value[:] = b"\0" * len(value)


def _prepare_remote_capacity_plan(
    *, host: str, user: str, config: Path, key: Path, run_id: str,
    source_sha: str, now_ms: int, plan_created_monotonic: float,
    workload: str = WORKLOAD,
    lifecycle: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        from tools import platform_cpu_diagnostic_pair as cpu_pair
    except ModuleNotFoundError:  # direct execution from platform/tools
        import platform_cpu_diagnostic_pair as cpu_pair  # type: ignore[no-redef]
    off_start = now_ms + 52_000
    plan = {
        "schema": 1, "operation": "prepare", "run_id": run_id,
        "source_sha": source_sha, "workload": workload,
        "off_start_ms": off_start, "off_end_ms": off_start + 20_000,
        "on_start_ms": off_start + 25_000, "on_end_ms": off_start + 45_000,
    }
    lifecycle = lifecycle if lifecycle is not None else {}
    lifecycle.update({
        "closed": True, "mux_off_enforced": False,
        "last_command_returned": False,
    })
    try:
        prepared, release_slug, summary = cpu_pair._ssh_plan(
            argparse.Namespace(), host, user, config, key, plan, lifecycle,
        )
    except Exception as exc:
        error_class = type(exc).__name__
        raise _RemoteCapacityPlanError(
            lifecycle, error_class, expires_at_ms=plan["on_end_ms"],
            plan_created_at_ms=now_ms,
            plan_created_monotonic=plan_created_monotonic,
        ) from None
    if (not prepared or not isinstance(release_slug, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", release_slug) is None
            or summary is not None):
        refusal = lifecycle.get("plan_prepare_refusal_class")
        error_class = refusal if refusal in PARENT_ERROR_CLASSES else "ValueError"
        raise _RemoteCapacityPlanError(
            lifecycle, error_class,
            expires_at_ms=(
                plan["on_end_ms"]
                if lifecycle.get("plan_prepare_status") == "prepared" else None
            ),
            plan_created_at_ms=now_ms,
            plan_created_monotonic=plan_created_monotonic,
        ) from None
    plan["release_slug"] = release_slug
    return plan, lifecycle


def _cleanup_remote_capacity_plan(
    *, host: str, user: str, config: Path, key: Path, run_id: str,
    lifecycle: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    try:
        from tools import platform_cpu_diagnostic_pair as cpu_pair
    except ModuleNotFoundError:  # direct execution from platform/tools
        import platform_cpu_diagnostic_pair as cpu_pair  # type: ignore[no-redef]
    lifecycle = lifecycle if lifecycle is not None else {}
    lifecycle.update({
        "closed": True, "mux_off_enforced": False,
        "last_command_returned": False,
    })
    payload = {"schema": 1, "operation": "cleanup", "run_id": run_id}
    result, _release_slug, summary = cpu_pair._ssh_plan(
        argparse.Namespace(), host, user, config, key, payload, lifecycle,
        allow_noop_cleanup=True,
    )
    return bool(result and isinstance(summary, dict)), summary if isinstance(summary, dict) else None


def _project_parent_ssh_transport(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        value = {}
    stage = value.get("stage")
    shape = value.get("stdout_shape")
    child_state = value.get("child_state")
    code = value.get("return_code")
    return {
        "stage": stage if isinstance(stage, str) and stage in SSH_TRANSPORT_STAGES else "transport_failed",
        "stdout_shape": shape if isinstance(shape, str) and shape in SSH_STDOUT_SHAPES else "malformed",
        "child_state": child_state if isinstance(child_state, str) and child_state in SSH_CHILD_STATES else "unknown",
        "return_code": code if type(code) is int and -255 <= code <= 255 else None,
    }


def _prepare_transport_is_closed(lifecycle: Mapping[str, Any]) -> bool:
    transport = lifecycle.get("prepare_transport")
    return (
        lifecycle.get("closed") is True
        and isinstance(transport, Mapping)
        and transport.get("child_state") in {"reaped", "not_started"}
    )


def _wait_for_remote_plan_expiry(
    expires_at_ms: int, *, plan_created_at_ms: int,
    plan_created_monotonic: float,
) -> None:
    """Wait against the plan-creation monotonic clock, with a fixed upper bound."""

    if (type(expires_at_ms) is not int or expires_at_ms < 0
            or type(plan_created_at_ms) is not int or plan_created_at_ms < 0
            or type(plan_created_monotonic) not in (int, float)
            or not math.isfinite(plan_created_monotonic)
            or expires_at_ms - plan_created_at_ms != PLAN_EXPIRY_HORIZON_MS):
        raise ValueError("capacity plan expiry rejected")
    target_monotonic = plan_created_monotonic + (
        PLAN_EXPIRY_HORIZON_MS + PLAN_EXPIRY_SKEW_MARGIN_MS
    ) / 1000
    deadline = plan_created_monotonic + PLAN_EXPIRY_WAIT_LIMIT_MS / 1000
    while True:
        remaining = target_monotonic - time.monotonic()
        if remaining <= 0:
            return
        remaining_budget = deadline - time.monotonic()
        if remaining_budget <= 0:
            raise TimeoutError("capacity plan expiry wait exceeded its bound")
        time.sleep(min(remaining, remaining_budget, 0.1))


def _parent_failure_receipt(
    *, error_class: str, stage: str, source_sha: str, app_target_sha: str,
    external_run_id: str, external_run_attempt: str, run_id: str,
    profile_digest_value: str, manifest_sha256: str | None, manifest_bytes: int | None,
    concurrency: int, prepare_lifecycle: Mapping[str, Any],
    cleanup_lifecycle: Mapping[str, Any], cleanup_ok: bool,
    workload: str = WORKLOAD,
) -> dict[str, Any]:
    prepare_status = prepare_lifecycle.get("plan_prepare_status")
    refusal_class = prepare_lifecycle.get("plan_prepare_refusal_class")
    cleanup_status = cleanup_lifecycle.get("plan_cleanup_status")
    cleanup_refusal = cleanup_lifecycle.get("plan_cleanup_refusal_class")
    result = {
        "schema": 1, "report_complete": True, "failure_only": True,
        "status": "incomplete", "passed": False,
        "matrix_credit": False, "slo_credit": False, "diagnostic_credit": False,
        "source_sha": source_sha, "app_target_sha": app_target_sha,
        "external_run_id": external_run_id, "external_run_attempt": external_run_attempt,
        "run_id": run_id, "profile_digest": profile_digest_value,
        "fixture_manifest_sha256": manifest_sha256,
        "fixture_manifest_bytes": manifest_bytes,
        "concurrency": concurrency, "namespace_closed": False,
        "stage": stage if stage in PARENT_FAILURE_STAGES else "remote_plan_prepare",
        "error_class": error_class if error_class in PARENT_ERROR_CLASSES else "unexpected",
        "plan_prepare_status": prepare_status if isinstance(prepare_status, str) and prepare_status in PLAN_PREPARE_STATUSES else "not_attempted",
        "plan_prepare_refusal_class": refusal_class if isinstance(refusal_class, str) and refusal_class in PLAN_REFUSAL_CLASSES else "none",
        "ssh_prepare_transport": _project_parent_ssh_transport(prepare_lifecycle.get("prepare_transport")),
        "plan_cleanup_status": cleanup_status if isinstance(cleanup_status, str) and cleanup_status in PLAN_CLEANUP_STATUSES else "not_attempted",
        "plan_cleanup_refusal_class": cleanup_refusal if isinstance(cleanup_refusal, str) and cleanup_refusal in PLAN_REFUSAL_CLASSES else "none",
        "ssh_cleanup_transport": _project_parent_ssh_transport(cleanup_lifecycle.get("cleanup_transport")),
        "remote_plan_cleanup": (
            "complete" if cleanup_ok
            else "not_attempted"
            if (cleanup_lifecycle.get("plan_cleanup_status") == "not_attempted"
                and prepare_status in {"not_attempted", "refused"})
            else "failed"
        ),
    }
    if workload != WORKLOAD:
        fixture_kind, _presets, _actors = _profile_spec(workload)
        result.update({"workload": workload, "fixture_kind": fixture_kind})
    return result


def _project_parent_failure_receipt(value: Any) -> dict[str, Any] | None:
    fields = {
        "schema", "report_complete", "failure_only", "status", "passed",
        "matrix_credit", "slo_credit", "diagnostic_credit", "source_sha",
        "app_target_sha", "external_run_id", "external_run_attempt", "run_id",
        "profile_digest", "fixture_manifest_sha256", "fixture_manifest_bytes",
        "concurrency", "namespace_closed", "stage", "error_class",
        "plan_prepare_status", "plan_prepare_refusal_class", "ssh_prepare_transport",
        "plan_cleanup_status", "plan_cleanup_refusal_class", "ssh_cleanup_transport",
        "remote_plan_cleanup",
    }
    if not isinstance(value, dict) or set(value) not in (fields, fields | {"workload", "fixture_kind"}):
        return None
    workload = value.get("workload", WORKLOAD)
    try:
        expected_fixture_kind, expected_presets, _actor_count = _profile_spec(workload)
    except ValueError:
        return None
    if set(value) == fields | {"workload", "fixture_kind"} and value.get("fixture_kind") != expected_fixture_kind:
        return None
    if (
        type(value.get("schema")) is not int or value["schema"] != 1
        or value.get("report_complete") is not True or value.get("failure_only") is not True
        or value.get("status") != "incomplete" or value.get("passed") is not False
        or any(value.get(key) is not False for key in ("matrix_credit", "slo_credit", "diagnostic_credit"))
        or not isinstance(value.get("source_sha"), str) or SHA_RE.fullmatch(value["source_sha"]) is None
        or not isinstance(value.get("app_target_sha"), str) or SHA_RE.fullmatch(value["app_target_sha"]) is None
        or not isinstance(value.get("external_run_id"), str) or not value["external_run_id"].isdecimal()
        or not isinstance(value.get("external_run_attempt"), str) or ATTEMPT_RE.fullmatch(value["external_run_attempt"]) is None
        or not isinstance(value.get("run_id"), str) or RUN_RE.fullmatch(value["run_id"]) is None
        or not isinstance(value.get("profile_digest"), str) or re.fullmatch(r"[0-9a-f]{64}", value["profile_digest"]) is None
        or type(value.get("concurrency")) is not int or value["concurrency"] not in expected_presets
        or value.get("namespace_closed") is not False
        or not isinstance(value.get("stage"), str) or value["stage"] not in PARENT_FAILURE_STAGES
        or not isinstance(value.get("error_class"), str) or value["error_class"] not in PARENT_ERROR_CLASSES
        or not isinstance(value.get("plan_prepare_status"), str) or value["plan_prepare_status"] not in PLAN_PREPARE_STATUSES
        or not isinstance(value.get("plan_prepare_refusal_class"), str) or value["plan_prepare_refusal_class"] not in PLAN_REFUSAL_CLASSES
        or not isinstance(value.get("plan_cleanup_status"), str) or value["plan_cleanup_status"] not in PLAN_CLEANUP_STATUSES
        or not isinstance(value.get("plan_cleanup_refusal_class"), str) or value["plan_cleanup_refusal_class"] not in PLAN_REFUSAL_CLASSES
        or not isinstance(value.get("remote_plan_cleanup"), str) or value["remote_plan_cleanup"] not in {"not_attempted", "complete", "failed"}
    ):
        return None
    manifest_sha = value.get("fixture_manifest_sha256")
    manifest_bytes = value.get("fixture_manifest_bytes")
    manifest_missing = manifest_sha is None and manifest_bytes is None
    manifest_bound = (
        isinstance(manifest_sha, str)
        and re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is not None
        and type(manifest_bytes) is int and 0 < manifest_bytes <= 256 * 1024
    )
    if not manifest_bound and not (
        manifest_missing and value["stage"] in {"identity", "manifest_read"}
    ):
        return None
    for key in ("ssh_prepare_transport", "ssh_cleanup_transport"):
        transport = value.get(key)
        if (not isinstance(transport, dict)
                or set(transport) != {"stage", "stdout_shape", "child_state", "return_code"}
                or not isinstance(transport.get("stage"), str) or transport["stage"] not in SSH_TRANSPORT_STAGES
                or not isinstance(transport.get("stdout_shape"), str) or transport["stdout_shape"] not in SSH_STDOUT_SHAPES
                or not isinstance(transport.get("child_state"), str) or transport["child_state"] not in SSH_CHILD_STATES
                or (transport.get("return_code") is not None
                    and (type(transport["return_code"]) is not int or not -255 <= transport["return_code"] <= 255))):
            return None
    try:
        failure_presets = _profile_spec(value.get("workload", WORKLOAD))[1]
    except ValueError:
        return None
    if (value["concurrency"] not in failure_presets
            or value["profile_digest"] != profile_digest(
                value["concurrency"], workload,
            )):
        return None
    return value


def _project_server_telemetry(summary: Any) -> dict[str, Any]:
    """Keep only closed CPU and Node-memory aggregates from the fixed plan collector."""

    cpu_rows: list[dict[str, Any]] = []
    memory_rows: list[dict[str, Any]] = []
    cpu_status = "incomplete"
    cpu_reason = "missing"
    memory_status = "incomplete"
    memory_reason = "unavailable"
    cgroup_status = "unknown"
    cgroup_reason = "hierarchy_unavailable"
    if isinstance(summary, dict):
        cpu_status = summary.get("usage_status") if summary.get("usage_status") in {"complete", "incomplete", "unavailable"} else "incomplete"
        cpu_reason = summary.get("usage_reason") if summary.get("usage_reason") in {"none", "incomplete", "unavailable", "missing", "identity_changed", "invalid_event", "invalid_profile", "journal_failed", "other"} else "other"
        memory_status = summary.get("memory_status") if summary.get("memory_status") in {"complete", "incomplete", "unavailable"} else "incomplete"
        memory_reason = summary.get("memory_reason") if summary.get("memory_reason") in {"none", "incomplete", "unavailable", "other"} else "other"
        cgroup_status = summary.get("cgroup_memory_status") if summary.get("cgroup_memory_status") in {"complete", "unknown", "invalid"} else "invalid"
        cgroup_reason = summary.get("cgroup_memory_reason") if summary.get("cgroup_memory_reason") in {"none", "file_unavailable", "hierarchy_unavailable", "invalid_snapshot"} else "invalid_snapshot"
        raw_cpu_rows = summary.get("usage_rows")
        if isinstance(raw_cpu_rows, list):
            for row in raw_cpu_rows[:4]:
                if not isinstance(row, dict) or row.get("service") not in {"api", "web"} or row.get("phase") not in {"off", "on"}:
                    continue
                clean = {"service": row["service"], "phase": row["phase"]}
                for field in ("expected_targets", "observed_targets", "event_count", "cpu_ns", "window_ms_min", "window_ms_max", "start_lag_ms_min", "start_lag_ms_max", "end_lag_ms_min", "end_lag_ms_max"):
                    item = row.get(field)
                    if item is None and field not in {"expected_targets", "observed_targets", "event_count"}:
                        clean[field] = None
                    elif type(item) is int and abs(item) <= 10**13:
                        clean[field] = item
                    else:
                        clean[field] = None
                clean["timing_complete"] = row.get("timing_complete") is True
                cpu_rows.append(clean)
        raw_memory_rows = summary.get("memory_rows")
        node_fields = (
            "sample_count", "rss_min_bytes", "rss_max_bytes", "rss_mean_bytes",
            "heap_total_min_bytes", "heap_total_max_bytes", "heap_total_mean_bytes",
            "heap_used_min_bytes", "heap_used_max_bytes", "heap_used_mean_bytes",
            "heap_limit_min_bytes", "heap_limit_max_bytes", "heap_limit_mean_bytes",
            "external_min_bytes", "external_max_bytes", "external_mean_bytes",
            "array_buffers_min_bytes", "array_buffers_max_bytes", "array_buffers_mean_bytes",
            "sampled_window_start_elapsed_ms", "sampled_window_end_elapsed_ms", "sampled_window_duration_ms",
            "cgroup_sample_count", "cgroup_memory_current_start_bytes", "cgroup_memory_current_end_bytes",
            "cgroup_memory_current_sampled_max_bytes", "cgroup_memory_peak_start_bytes",
            "cgroup_memory_peak_end_bytes", "cgroup_memory_peak_sampled_max_bytes",
            "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
            "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
            "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
            "gc_count", "gc_duration_ms", "gc_major_count", "gc_minor_count",
            "gc_incremental_count", "gc_weakcb_count", "gc_other_count",
        )
        if isinstance(raw_memory_rows, list):
            for row in raw_memory_rows[:2]:
                if not isinstance(row, dict) or row.get("service") != "web" or row.get("phase") not in {"off", "on"}:
                    continue
                clean = {"service": "web", "phase": row["phase"]}
                for field in node_fields:
                    item = row.get(field)
                    clean[field] = item if type(item) is int and 0 <= item <= 10**15 else None
                for field, allowed in (
                    ("status", {"complete", "incomplete"}),
                    ("gc_status", {"complete", "incomplete"}),
                    ("cgroup_memory_status", {"complete", "unknown", "invalid"}),
                    ("cgroup_memory_reason", {"none", "file_unavailable", "hierarchy_unavailable", "invalid_snapshot"}),
                ):
                    clean[field] = row.get(field) if row.get(field) in allowed else "invalid_snapshot" if field == "cgroup_memory_reason" else "incomplete"
                clean["gc_overflow"] = row.get("gc_overflow") if type(row.get("gc_overflow")) is bool else None
                memory_rows.append(clean)
    return {
        "schema": 1,
        "cpu_status": cpu_status,
        "cpu_reason": cpu_reason,
        "cpu_rows": cpu_rows,
        "node_memory_status": memory_status,
        "node_memory_reason": memory_reason,
        "cgroup_memory_status": cgroup_status,
        "cgroup_memory_reason": cgroup_reason,
        "node_memory_rows": memory_rows,
    }


def _write_report(path: Path, payload: Mapping[str, Any]) -> None:
    raw = json.dumps(
        dict(payload), ensure_ascii=True, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if len(raw) > 256 * 1024:
        raise ValueError("capacity report exceeds bound")
    directory = path.parent
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise ValueError("capacity report directory rejected")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("capacity report write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _parent_run(
    args: argparse.Namespace, stage_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = stage_state if stage_state is not None else {}
    state.update({
        "stage": "identity", "prepare_lifecycle": {},
        "cleanup_lifecycle": {}, "cleanup_ok": False,
        "namespace_closed": False,
        "source_sha": args.source_sha, "app_target_sha": args.app_target_sha,
        "external_run_id": args.external_run_id,
        "external_run_attempt": args.external_run_attempt,
        "concurrency": args.concurrency,
        "workload": getattr(args, "workload", WORKLOAD),
    })
    workload = getattr(args, "workload", WORKLOAD)
    try:
        _fixture_kind, workload_presets, _actor_count = _profile_spec(workload)
    except ValueError:
        workload_presets = frozenset()
    if type(args.concurrency) is int and args.concurrency in workload_presets:
        state["run_id"] = os.urandom(16).hex()
        state["profile_digest"] = profile_digest(args.concurrency, workload)
    if (SHA_RE.fullmatch(args.source_sha) is None or SHA_RE.fullmatch(args.app_target_sha) is None
            or not args.external_run_id.isdecimal() or ATTEMPT_RE.fullmatch(args.external_run_attempt) is None
            or workload not in {WORKLOAD, HTML_WORKLOAD}
            or type(args.concurrency) is not int or args.concurrency not in workload_presets):
        raise ValueError("capacity parent identity or preset rejected")
    report_path = Path(args.report)
    ssh_config, ssh_key = Path(args.ssh_config), Path(args.ssh_key)
    ssh_dir = ssh_config.parent
    if (ssh_dir.name != "external-ssh" or ssh_config.name != "config"
            or ssh_key.parent != ssh_dir or ssh_key.name != "id_ed25519"):
        raise ValueError("capacity parent SSH paths rejected")
    run_id = state["run_id"]
    digest = state["profile_digest"]
    state["stage"] = "manifest_read"
    raw, manifest, actors = _read_manifest(Path(args.manifest))
    manifest_sha = hashlib.sha256(raw).hexdigest()
    state.update({"manifest_sha256": manifest_sha, "manifest_bytes": len(raw)})
    state["stage"] = "binding"
    if (manifest.get("runner_sha") != args.source_sha
            or manifest.get("app_target_sha") != args.app_target_sha
            or len(manifest.get("tournaments", [])) != FIXTURE_TOURNAMENTS
            or len(actors) != FIXTURE_USERS
            or any(item.get("user_count") != FIXTURE_USERS for item in manifest["tournaments"])):
        raise ValueError("capacity manifest source or fixture binding rejected")
    binding = {
        "run_id": run_id, "source_sha": args.source_sha,
        "app_target_sha": args.app_target_sha, "profile_digest": digest,
        "external_run_id": args.external_run_id,
        "external_run_attempt": args.external_run_attempt,
    }
    lifecycle: dict[str, Any] = {}
    state["stage"] = "remote_plan_prepare"
    state["prepare_lifecycle"] = lifecycle
    plan_created_monotonic = time.monotonic()
    plan_created_at_ms = time.time_ns() // 1_000_000
    state["plan_created_monotonic"] = plan_created_monotonic
    state["plan_created_at_ms"] = plan_created_at_ms
    try:
        planned, lifecycle = _prepare_remote_capacity_plan(
            host=args.ssh_host, user=args.ssh_user, config=ssh_config, key=ssh_key,
            run_id=run_id, source_sha=args.app_target_sha,
            now_ms=plan_created_at_ms,
            workload=workload,
            plan_created_monotonic=plan_created_monotonic, lifecycle=lifecycle,
        )
    except _RemoteCapacityPlanError as exc:
        cleanup_lifecycle: dict[str, Any] = {}
        state["prepare_lifecycle"] = exc.lifecycle
        if (
            exc.lifecycle.get("plan_prepare_status") == "prepared"
            and type(exc.expires_at_ms) is int
            and _prepare_transport_is_closed(exc.lifecycle)
        ):
            try:
                state["stage"] = "plan_cleanup"
                if (type(exc.plan_created_at_ms) is not int
                        or not isinstance(exc.plan_created_monotonic, (int, float))):
                    raise ValueError("capacity plan clock anchor unavailable")
                _wait_for_remote_plan_expiry(
                    exc.expires_at_ms,
                    plan_created_at_ms=exc.plan_created_at_ms,
                    plan_created_monotonic=exc.plan_created_monotonic,
                )
            except TimeoutError:
                cleanup_ok = False
                cleanup_lifecycle.update({
                    "plan_cleanup_status": "not_attempted",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "not_started", "return_code": None,
                        "stdout_shape": "none", "child_state": "not_started",
                    },
                })
            else:
                try:
                    cleanup_ok, _ = _cleanup_remote_capacity_plan(
                        host=args.ssh_host, user=args.ssh_user,
                        config=ssh_config, key=ssh_key, run_id=run_id,
                        lifecycle=cleanup_lifecycle,
                    )
                except Exception:
                    cleanup_ok = False
                    cleanup_lifecycle.update({
                        "plan_cleanup_status": "transport_failed",
                        "plan_cleanup_refusal_class": "none",
                        "cleanup_transport": {
                            "stage": "transport_failed", "return_code": None,
                            "stdout_shape": "none", "child_state": "unknown",
                        },
                    })
            state["stage"] = "remote_plan_prepare"
        else:
            cleanup_ok = False
            cleanup_lifecycle.update({
                "plan_cleanup_status": "not_attempted",
                "plan_cleanup_refusal_class": "none",
                "cleanup_transport": {
                    "stage": "not_started", "return_code": None,
                    "stdout_shape": "none", "child_state": "unknown",
                },
            })
        state["cleanup_lifecycle"] = cleanup_lifecycle
        state["cleanup_ok"] = cleanup_ok
        return _parent_failure_receipt(
            error_class=exc.error_class, stage="remote_plan_prepare",
            source_sha=args.source_sha, app_target_sha=args.app_target_sha,
            external_run_id=args.external_run_id,
            external_run_attempt=args.external_run_attempt, run_id=run_id,
            profile_digest_value=digest, manifest_sha256=manifest_sha,
            manifest_bytes=len(raw), concurrency=args.concurrency,
            prepare_lifecycle=exc.lifecycle,
            cleanup_lifecycle=cleanup_lifecycle, cleanup_ok=cleanup_ok,
            workload=workload,
        )
    state["plan_expiry_ms"] = planned["on_end_ms"]
    control_path: Path | None = None
    ssh_contents: dict[str, bytearray] = {}
    ssh_lifecycle: dict[str, int] = {}
    hidden = {"value": False}
    outcome: dict[str, Any] | None = None
    cleanup_ok = False
    telemetry_summary: dict[str, Any] | None = None
    state["stage"] = "worker_supervision"
    with tempfile.TemporaryDirectory(prefix="ssr-capacity-", dir=report_path.parent) as private_dir:
        private = Path(private_dir)
        os.chmod(private, 0o700)
        control_path = private / CONTROL_BASENAME
        worker_report = private / "worker-report.json"
        supervisor_report = private / "supervisor-report.json"
        capacity_config = {
            "schema": SCHEMA, "workload": workload,
            "fixture_kind": _profile_spec(workload)[0],
            "manifest_path": str(Path(args.manifest).resolve()),
            "manifest_sha256": manifest_sha,
            "manifest_bytes": len(raw),
            "control_path": str(control_path), "binding": binding,
            "concurrency": args.concurrency,
            "off_start_ms": planned["off_start_ms"],
            "off_end_ms": planned["off_end_ms"],
            "on_start_ms": planned["on_start_ms"],
            "on_end_ms": planned["on_end_ms"],
        }
        watcher = _RemoteMemoryWatcher(
            ssh_host=args.ssh_host, ssh_user=args.ssh_user,
            ssh_config=ssh_config, ssh_key=ssh_key,
            control_path=control_path, binding=binding,
            run_id=run_id, source_sha=args.app_target_sha,
            expected_plan=(planned["off_start_ms"], planned["on_start_ms"], planned["on_end_ms"]),
            workload=workload,
        )

        def hide_material() -> None:
            nonlocal ssh_contents, ssh_lifecycle
            ssh_contents, ssh_lifecycle = _hide_parent_ssh_material(ssh_dir)
            hidden["value"] = True

        def restore_material() -> None:
            if hidden["value"]:
                _restore_parent_ssh_material(ssh_dir, ssh_contents, ssh_lifecycle)
                hidden["value"] = False

        try:
            outcome = run_isolated_capacity_worker(
                capacity_config=capacity_config,
                worker_binding=_namespace_worker_binding(
                    source_sha=args.source_sha,
                    app_target_sha=args.app_target_sha,
                    external_run_id=args.external_run_id,
                    manifest=manifest,
                ),
                stage_state=state,
                worker_report_path=worker_report,
                supervisor_report_path=supervisor_report,
                source_sha=args.source_sha,
                watcher=watcher,
                hide_ssh_material=hide_material,
                restore_ssh_material=restore_material,
                maximum_runtime_seconds=150,
                on_start_ms=planned["on_start_ms"],
                on_end_ms=planned["on_end_ms"],
            )
            state["namespace_closed"] = outcome.get("namespace_closed") is True
        finally:
            if hidden["value"]:
                try:
                    restore_material()
                except Exception:
                    watcher.fail_closed()
            cleanup_lifecycle: dict[str, Any] = {}
            state["stage"] = "plan_cleanup"
            state["cleanup_lifecycle"] = cleanup_lifecycle
            if (
                isinstance(outcome, dict)
                and outcome.get("namespace_closed") is True
                and lifecycle.get("plan_prepare_status") == "prepared"
                and _prepare_transport_is_closed(lifecycle)
            ):
                try:
                    _wait_for_remote_plan_expiry(
                        planned["on_end_ms"],
                        plan_created_at_ms=plan_created_at_ms,
                        plan_created_monotonic=plan_created_monotonic,
                    )
                except TimeoutError:
                    cleanup_ok = False
                    cleanup_lifecycle.update({
                        "plan_cleanup_status": "not_attempted",
                        "plan_cleanup_refusal_class": "none",
                        "cleanup_transport": {
                            "stage": "not_started", "return_code": None,
                            "stdout_shape": "none", "child_state": "not_started",
                        },
                    })
                else:
                    cleanup_ok, telemetry_summary = _cleanup_remote_capacity_plan(
                        host=args.ssh_host, user=args.ssh_user,
                        config=ssh_config, key=ssh_key, run_id=run_id,
                        lifecycle=cleanup_lifecycle,
                    )
            else:
                cleanup_ok = False
                cleanup_lifecycle.update({
                    "plan_cleanup_status": "not_attempted",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "not_started", "return_code": None,
                        "stdout_shape": "none", "child_state": "not_started",
                    },
                })
            state["cleanup_ok"] = cleanup_ok
            state["stage"] = "worker_supervision"
    if isinstance(outcome, dict):
        server_telemetry = _project_server_telemetry(telemetry_summary)
        outcome["server_telemetry"] = server_telemetry
        worker_row = outcome.get("worker")
        runner_row = outcome.get("runner_headroom")
        node_rows = server_telemetry.get("node_memory_rows", [])
        if (
            not isinstance(worker_row, dict)
            or not isinstance(runner_row, dict)
            or runner_row.get("status") != "complete"
            or server_telemetry.get("cpu_status") != "complete"
            or len(server_telemetry.get("cpu_rows", [])) != 4
            or server_telemetry.get("node_memory_status") != "complete"
            or server_telemetry.get("cgroup_memory_status") != "complete"
            or server_telemetry.get("cgroup_memory_reason") != "none"
            or len(node_rows) != 2
            or any(row.get("sample_count") != 5 or row.get("status") != "complete" for row in node_rows)
        ):
            outcome["status"] = "incomplete"
            outcome["passed"] = False
    if outcome is None:
        outcome = {
            "schema": SCHEMA, "report_complete": True,
            "workload": workload, "fixture_kind": _profile_spec(workload)[0],
            "status": "incomplete", "passed": False,
            "matrix_credit": False, "slo_credit": False, "diagnostic_credit": False,
            "source_sha": args.source_sha, "app_target_sha": args.app_target_sha,
            "external_run_id": args.external_run_id,
            "external_run_attempt": args.external_run_attempt,
            "run_id": run_id, "profile_digest": digest,
            "fixture_manifest_sha256": manifest_sha,
            "fixture_manifest_bytes": len(raw),
            "fixture_tournaments": FIXTURE_TOURNAMENTS,
            "fixture_users": FIXTURE_USERS, "actors": _profile_spec(workload)[2],
            "concurrency": args.concurrency, "max_inflight": args.concurrency,
            "load_model": "closed_loop_best_effort", "window_ms": ON_WINDOW_MS,
            "namespace_closed": False,
            "worker": None,
            "generator_cpu_evidence": "incomplete",
            "memory_guard": {
                "status": "incomplete", "samples": 0,
                "current_min_bytes": None, "current_max_bytes": None,
                "memory_peak_lifetime_max_bytes": None,
                "memory_events_sampled_max_delta": None,
                "target_identity_status": "unknown", "terminal_decision": None,
                "terminal_reason": "parent_error", "ssh_material_restored": True,
            },
            "runner_headroom": {
                "schema": 1, "status": "unknown", "reason": "parent_error",
                "runner_environment": "unknown", "identity": {key: False for key in (
                    "outer_pid_namespace_matches_pid1", "outer_mount_namespace_matches_pid1",
                    "outer_cgroup_namespace_matches_pid1", "target_cgroup_namespace_matches_outer",
                    "target_mount_namespace_is_isolated", "target_pid_namespace_is_isolated",
                    "global_cgroup2_mount", "workload_membership_matches_outer",
                )}, "sample_count": 0,
                "window_seconds": None, "effective_cpu_capacity_cores": None,
                "max_cgroup_cpu_usage_cores": None,
                "max_visible_throttled_periods_delta": None,
                "window_scope": "on_window", "window_start_offset_ms": None,
                "window_end_offset_ms": None,
            },
            "server_telemetry": _project_server_telemetry(None),
            "server_capacity_claim": None,
            "server_capacity_claim_reason": "resource_correlation_not_provided",
        }
    outcome["remote_plan_cleanup"] = "complete" if cleanup_ok else "failed"
    if not cleanup_ok:
        outcome["status"] = "incomplete"
        outcome["passed"] = False
    outcome["passed"] = bool(outcome.get("passed") is True and cleanup_ok)
    return outcome


def _project_capacity_report(value: Any) -> dict[str, Any] | None:
    top_fields = {
        "schema", "report_complete", "workload", "fixture_kind", "status", "passed",
        "matrix_credit", "slo_credit", "diagnostic_credit", "source_sha",
        "app_target_sha", "external_run_id", "external_run_attempt", "run_id",
        "profile_digest", "fixture_manifest_sha256", "fixture_tournaments",
        "fixture_manifest_bytes", "fixture_users", "actors", "concurrency",
        "max_inflight", "load_model", "window_ms", "namespace_closed", "worker",
        "memory_guard", "runner_headroom", "server_capacity_claim",
        "server_capacity_claim_reason", "remote_plan_cleanup", "server_telemetry",
        "generator_cpu_evidence",
    }
    if not isinstance(value, dict) or set(value) != top_fields:
        return None
    try:
        expected_fixture_kind, expected_presets, expected_actor_count = _profile_spec(value.get("workload"))
    except ValueError:
        return None
    if (type(value.get("schema")) is not int or value["schema"] != SCHEMA
            or value.get("report_complete") is not True
            or value.get("fixture_kind") != expected_fixture_kind
            or value.get("status") not in {"complete", "resource_abort", "incomplete"}
            or type(value.get("passed")) is not bool
            or any(value.get(key) is not False for key in ("matrix_credit", "slo_credit", "diagnostic_credit"))
            or not isinstance(value.get("source_sha"), str) or SHA_RE.fullmatch(value["source_sha"]) is None
            or not isinstance(value.get("app_target_sha"), str) or SHA_RE.fullmatch(value["app_target_sha"]) is None
            or not isinstance(value.get("external_run_id"), str) or not value["external_run_id"].isdecimal()
            or not isinstance(value.get("external_run_attempt"), str) or ATTEMPT_RE.fullmatch(value["external_run_attempt"]) is None
            or not isinstance(value.get("run_id"), str) or RUN_RE.fullmatch(value["run_id"]) is None
            or not isinstance(value.get("profile_digest"), str) or re.fullmatch(r"[0-9a-f]{64}", value["profile_digest"]) is None
            or not isinstance(value.get("fixture_manifest_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["fixture_manifest_sha256"]) is None
            or type(value.get("fixture_manifest_bytes")) is not int
            or not 0 < value["fixture_manifest_bytes"] <= 256 * 1024
            or type(value.get("fixture_tournaments")) is not int
            or value.get("fixture_tournaments") != FIXTURE_TOURNAMENTS
            or type(value.get("fixture_users")) is not int
            or value.get("fixture_users") != FIXTURE_USERS
            or type(value.get("actors")) is not int or value.get("actors") != expected_actor_count
            or type(value.get("concurrency")) is not int
            or value.get("concurrency") not in expected_presets
            or type(value.get("max_inflight")) is not int
            or value.get("max_inflight") != value.get("concurrency")
            or value.get("load_model") != "closed_loop_best_effort"
            or type(value.get("window_ms")) is not int or value.get("window_ms") != ON_WINDOW_MS
            or type(value.get("namespace_closed")) is not bool
            or value.get("server_capacity_claim") is not None
            or value.get("server_capacity_claim_reason") != "resource_correlation_not_provided"
            or value.get("generator_cpu_evidence") not in {"measured", "incomplete"}
            or value.get("remote_plan_cleanup") not in {"complete", "failed"}):
        return None
    worker = value.get("worker")
    if worker is not None:
        expected_worker = {
            "status", "submitted", "completed", "http_status_counts", "error_counts",
            "latency_ms", "resource_abort_reason", "on_window_start_ms",
            "on_window_end_ms", "observed_duration_ms", "off_probe", "max_inflight",
            "fixture_manifest_bytes", "response_bytes_total", "process_cpu_windows",
        }
        warmup_worker = expected_worker | {"warmup_failure"}
        if (not isinstance(worker, dict)
                or set(worker) not in (
                    expected_worker, expected_worker - {"process_cpu_windows"},
                    warmup_worker, warmup_worker - {"process_cpu_windows"},
                )):
            return None
        has_warmup_failure = "warmup_failure" in worker
        if (has_warmup_failure != (worker.get("resource_abort_reason") == "off_warmup_failed")
                or (has_warmup_failure and _project_warmup_failure(
                    worker.get("warmup_failure"), concurrency=value.get("concurrency"),
                ) is None)):
            return None
        expected_final_worker = expected_worker | (
            {"warmup_failure"} if has_warmup_failure else set()
        )
        if has_warmup_failure:
            worker = {
                **worker,
                "warmup_failure": _project_warmup_failure(
                    worker["warmup_failure"], concurrency=value["concurrency"],
                ),
            }
            value["worker"] = worker
        if "process_cpu_windows" not in worker:
            worker = {**worker, "process_cpu_windows": _unknown_cpu_windows()}
            value["worker"] = worker
        if (set(worker) != expected_final_worker
                or worker.get("status") not in {"complete", "incomplete", "failed"}
                or type(worker.get("submitted")) is not int
                or type(worker.get("completed")) is not int
                or not 0 <= worker["completed"] <= worker["submitted"] <= 100_000
                or type(worker.get("max_inflight")) is not int
                or worker.get("max_inflight") != value.get("concurrency")
                or type(worker.get("fixture_manifest_bytes")) is not int
                or worker.get("fixture_manifest_bytes") != value.get("fixture_manifest_bytes")
                or type(worker.get("response_bytes_total")) is not int
                or not 0 <= worker["response_bytes_total"] <= 10**12
                or not isinstance(worker.get("http_status_counts"), dict)
                or not isinstance(worker.get("error_counts"), dict)
                or type(worker.get("on_window_start_ms")) is not int
                or type(worker.get("on_window_end_ms")) is not int
                or worker["on_window_end_ms"] - worker["on_window_start_ms"] != ON_WINDOW_MS
                or type(worker.get("observed_duration_ms")) is not int
                or not 0 <= worker["observed_duration_ms"] <= 30_000
                or not isinstance(worker.get("off_probe"), dict)
                or set(worker["off_probe"]) != {"submitted", "completed", "http_status"}
                or type(worker["off_probe"].get("submitted")) is not int
                or not 0 <= worker["off_probe"]["submitted"] <= value.get("concurrency", 0)
                or type(worker["off_probe"].get("completed")) is not int
                or not 0 <= worker["off_probe"]["completed"] <= worker["off_probe"]["submitted"]
                or (worker.get("status") == "complete" and (
                    worker["off_probe"]["submitted"] != value.get("concurrency")
                    or worker["off_probe"]["completed"] != worker["off_probe"]["submitted"]
                ))
                or (worker["off_probe"].get("http_status") is not None
                    and (type(worker["off_probe"]["http_status"]) is not int
                         or not 100 <= worker["off_probe"]["http_status"] <= 599))
                or (worker.get("status") == "complete" and worker["off_probe"] != {
                    "submitted": value.get("concurrency"),
                    "completed": value.get("concurrency"), "http_status": 200,
                })
                or not _valid_cpu_window_rows(worker.get("process_cpu_windows"))):
            return None
        latency = worker.get("latency_ms")
        if (not isinstance(latency, dict)
                or set(latency) != {"sample_count", "p50", "p95", "p99", "max"}
                or type(latency.get("sample_count")) is not int
                or latency["sample_count"] != worker["completed"]
                or any(
                    item is not None and (
                        type(item) not in (int, float) or isinstance(item, bool)
                        or not math.isfinite(item) or item < 0
                    )
                    for key, item in latency.items() if key != "sample_count"
                )):
            return None
        if (any(not isinstance(key, str)
                or re.fullmatch(r"(?:[1-5][0-9]{2}|unknown)", key) is None
                or type(count) is not int or count < 0
                for key, count in worker["http_status_counts"].items())
                or sum(worker["http_status_counts"].values()) != worker["completed"]
                or any(not isinstance(key, str) or key not in CAPACITY_ERROR_CODES
                       or type(count) is not int or count < 0
                       for key, count in worker["error_counts"].items())):
            return None
    memory = value.get("memory_guard")
    if (not isinstance(memory, dict)
            or set(memory) != {
                "status", "samples", "current_min_bytes", "current_max_bytes",
                "memory_peak_lifetime_max_bytes", "memory_events_sampled_max_delta",
                "target_identity_status", "terminal_decision", "terminal_reason",
                "ssh_material_restored",
            }
            or memory.get("status") not in {"complete", "incomplete"}
            or type(memory.get("samples")) is not int or not 0 <= memory["samples"] <= 2_000
            or memory.get("target_identity_status") not in {"complete", "unknown"}
            or memory.get("terminal_decision") not in {"complete", "abort", None}
            or not isinstance(memory.get("terminal_reason"), str)
            or re.fullmatch(r"[a-z0-9_]{1,64}", memory["terminal_reason"]) is None
            or type(memory.get("ssh_material_restored")) is not bool
            or memory.get("ssh_material_restored") is not True):
        return None
    for key in ("current_min_bytes", "current_max_bytes", "memory_peak_lifetime_max_bytes"):
        if memory.get(key) is not None and (type(memory[key]) is not int or memory[key] < 0):
            return None
    events = memory.get("memory_events_sampled_max_delta")
    if events is not None and (
        not isinstance(events, dict)
        or set(events) != {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}
        or any(type(item) is not int or item < 0 for item in events.values())
    ):
        return None
    runner = value.get("runner_headroom")
    runner_fields = {
        "schema", "status", "reason", "runner_environment", "identity", "sample_count",
        "window_seconds", "effective_cpu_capacity_cores", "max_cgroup_cpu_usage_cores",
        "max_visible_throttled_periods_delta", "window_scope",
        "window_start_offset_ms", "window_end_offset_ms",
    }
    if (not isinstance(runner, dict) or set(runner) != runner_fields
            or type(runner.get("schema")) is not int or runner["schema"] != 1
            or runner.get("status") not in {"complete", "unknown"}
            or runner.get("window_scope") != "on_window"
            or runner.get("runner_environment") not in {"github-hosted", "unknown"}
            or type(runner.get("sample_count")) is not int or not 0 <= runner["sample_count"] <= 10_000
            or not isinstance(runner.get("reason"), str)
            or re.fullmatch(r"[a-z0-9_]{1,64}", runner["reason"]) is None
            or not isinstance(runner.get("identity"), dict)
            or set(runner["identity"]) != {
                "outer_pid_namespace_matches_pid1", "outer_mount_namespace_matches_pid1",
                "outer_cgroup_namespace_matches_pid1", "target_cgroup_namespace_matches_outer",
                "target_mount_namespace_is_isolated", "target_pid_namespace_is_isolated",
                "global_cgroup2_mount", "workload_membership_matches_outer",
            }
            or any(type(item) is not bool for item in runner["identity"].values())):
        return None
    if runner["status"] == "complete":
        if (runner.get("runner_environment") != "github-hosted"
                or not isinstance(runner.get("window_seconds"), (int, float))
                or type(runner.get("effective_cpu_capacity_cores")) not in (int, float)
                or type(runner.get("max_cgroup_cpu_usage_cores")) not in (int, float)
                or type(runner.get("max_visible_throttled_periods_delta")) is not int
                or type(runner.get("window_start_offset_ms")) is not int
                or type(runner.get("window_end_offset_ms")) is not int
                or not all(runner["identity"].values())
                or any(not math.isfinite(runner[key]) or runner[key] < 0 for key in (
                    "window_seconds", "effective_cpu_capacity_cores", "max_cgroup_cpu_usage_cores",
                ))
                or runner["max_visible_throttled_periods_delta"] < 0):
            return None
        if (runner["sample_count"] < 3 or runner["window_seconds"] < 1.0
                or runner["window_seconds"] > 20.5
                or not 0 <= runner["window_start_offset_ms"]
                < runner["window_end_offset_ms"] <= ON_WINDOW_MS):
            return None
    elif any(runner.get(key) is not None for key in (
        "window_seconds", "effective_cpu_capacity_cores", "max_cgroup_cpu_usage_cores",
        "max_visible_throttled_periods_delta",
    )):
        return None
    telemetry = value.get("server_telemetry")
    telemetry_fields = {
        "schema", "cpu_status", "cpu_reason", "cpu_rows", "node_memory_status",
        "node_memory_reason", "cgroup_memory_status", "cgroup_memory_reason", "node_memory_rows",
    }
    if (not isinstance(telemetry, dict) or set(telemetry) != telemetry_fields
            or type(telemetry.get("schema")) is not int or telemetry["schema"] != 1
            or telemetry.get("cpu_status") not in {"complete", "incomplete", "unavailable"}
            or telemetry.get("cpu_reason") not in {"none", "incomplete", "unavailable", "missing", "identity_changed", "invalid_event", "invalid_profile", "journal_failed", "other"}
            or telemetry.get("node_memory_status") not in {"complete", "incomplete", "unavailable"}
            or telemetry.get("node_memory_reason") not in {"none", "incomplete", "unavailable", "other"}
            or telemetry.get("cgroup_memory_status") not in {"complete", "unknown", "invalid"}
            or telemetry.get("cgroup_memory_reason") not in {"none", "file_unavailable", "hierarchy_unavailable", "invalid_snapshot"}
            or not isinstance(telemetry.get("cpu_rows"), list) or len(telemetry["cpu_rows"]) > 4
            or not isinstance(telemetry.get("node_memory_rows"), list) or len(telemetry["node_memory_rows"]) > 2):
        return None
    for row in telemetry["cpu_rows"]:
        if (not isinstance(row, dict) or set(row) != {
            "service", "phase", "expected_targets", "observed_targets", "event_count", "cpu_ns",
            "window_ms_min", "window_ms_max", "start_lag_ms_min", "start_lag_ms_max",
            "end_lag_ms_min", "end_lag_ms_max", "timing_complete",
        } or row.get("service") not in {"api", "web"} or row.get("phase") not in {"off", "on"}
                or any(type(row.get(key)) is not int or row[key] < 0 for key in ("expected_targets", "observed_targets", "event_count"))
                or type(row.get("timing_complete")) is not bool
                or any(item is not None and type(item) is not int for key, item in row.items() if key not in {"service", "phase", "expected_targets", "observed_targets", "event_count", "timing_complete"})):
            return None
    node_integer_fields = {
        "sample_count", "rss_min_bytes", "rss_max_bytes", "rss_mean_bytes",
        "heap_total_min_bytes", "heap_total_max_bytes", "heap_total_mean_bytes",
        "heap_used_min_bytes", "heap_used_max_bytes", "heap_used_mean_bytes",
        "heap_limit_min_bytes", "heap_limit_max_bytes", "heap_limit_mean_bytes",
        "external_min_bytes", "external_max_bytes", "external_mean_bytes",
        "array_buffers_min_bytes", "array_buffers_max_bytes", "array_buffers_mean_bytes",
        "sampled_window_start_elapsed_ms", "sampled_window_end_elapsed_ms", "sampled_window_duration_ms",
        "cgroup_sample_count", "cgroup_memory_current_start_bytes", "cgroup_memory_current_end_bytes",
        "cgroup_memory_current_sampled_max_bytes", "cgroup_memory_peak_start_bytes",
        "cgroup_memory_peak_end_bytes", "cgroup_memory_peak_sampled_max_bytes",
        "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
        "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
        "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
        "gc_count", "gc_duration_ms", "gc_major_count", "gc_minor_count",
        "gc_incremental_count", "gc_weakcb_count", "gc_other_count",
    }
    for row in telemetry["node_memory_rows"]:
        required_row = node_integer_fields | {
            "service", "phase", "status", "gc_status", "cgroup_memory_status",
            "cgroup_memory_reason", "gc_overflow",
        }
        if (not isinstance(row, dict) or set(row) != required_row
                or row.get("service") != "web" or row.get("phase") not in {"off", "on"}
                or row.get("status") not in {"complete", "incomplete"}
                or row.get("gc_status") not in {"complete", "incomplete"}
                or row.get("cgroup_memory_status") not in {"complete", "unknown", "invalid"}
                or row.get("cgroup_memory_reason") not in {"none", "file_unavailable", "hierarchy_unavailable", "invalid_snapshot"}
                or row.get("gc_overflow") not in {True, False, None}
                or any(item is not None and type(item) is not int for item in (row[key] for key in node_integer_fields))):
            return None
    cpu_rows = (value["worker"].get("process_cpu_windows", _unknown_cpu_windows())
                if value.get("worker") else _unknown_cpu_windows())
    if value.get("generator_cpu_evidence") != _generator_cpu_evidence_status(cpu_rows):
        return None
    return value


def evaluate_capacity_report(
    value: Any, *, source_sha: str, app_target_sha: str, run_id: str,
    attempt: str, manifest_sha256: str, workload: str = WORKLOAD,
) -> str:
    if _project_parent_failure_receipt(value) is not None:
        return "INVALID"
    report = _project_capacity_report(value)
    if report is None or any((
        report.get("workload") != workload,
        report.get("source_sha") != source_sha,
        report.get("app_target_sha") != app_target_sha,
        report.get("external_run_id") != run_id,
        report.get("external_run_attempt") != attempt,
        report.get("fixture_manifest_sha256") != manifest_sha256,
        report.get("profile_digest") != profile_digest(
            report.get("concurrency"), report.get("workload", WORKLOAD),
        ),
    )):
        return "INVALID"
    if (report.get("status") != "complete" or report.get("passed") is not True
            or report.get("namespace_closed") is not True
            or report.get("remote_plan_cleanup") != "complete"):
        return "FAIL" if report.get("status") == "resource_abort" else "INVALID"
    worker = report.get("worker")
    memory = report["memory_guard"]
    runner = report["runner_headroom"]
    telemetry = report["server_telemetry"]
    if not isinstance(worker, dict):
        return "INVALID"
    statuses = worker["http_status_counts"]
    events = memory["memory_events_sampled_max_delta"]
    expected_cpu = (("api", "off", 2), ("api", "on", 2), ("web", "off", 1), ("web", "on", 1))
    cpu_rows = telemetry["cpu_rows"]
    cpu_by_key = {(row["service"], row["phase"]): row for row in cpu_rows}
    cpu_complete = bool(
        telemetry["cpu_status"] == "complete"
        and len(cpu_rows) == len(expected_cpu)
        and len(cpu_by_key) == len(expected_cpu)
        and all(
            (row := cpu_by_key.get((service, phase))) is not None
            and row["expected_targets"] == targets
            and row["observed_targets"] == targets
            and row["event_count"] == targets
            and row["cpu_ns"] is not None and row["cpu_ns"] >= 0
            and row["timing_complete"] is True
            and isinstance(row["window_ms_min"], int)
            and isinstance(row["window_ms_max"], int)
            and 19_750 <= row["window_ms_min"] <= row["window_ms_max"] <= 20_500
            for service, phase, targets in expected_cpu
        )
    )
    memory_rows = telemetry["node_memory_rows"]
    memory_by_phase = {row["phase"]: row for row in memory_rows}
    required_node_values = (
        "rss_min_bytes", "rss_max_bytes", "rss_mean_bytes", "heap_total_min_bytes",
        "heap_total_max_bytes", "heap_total_mean_bytes", "heap_used_min_bytes",
        "heap_used_max_bytes", "heap_used_mean_bytes", "heap_limit_min_bytes",
        "heap_limit_max_bytes", "heap_limit_mean_bytes", "external_min_bytes",
        "external_max_bytes", "external_mean_bytes", "array_buffers_min_bytes",
        "array_buffers_max_bytes", "array_buffers_mean_bytes", "gc_count", "gc_duration_ms",
    )
    node_memory_complete = bool(
        telemetry["node_memory_status"] == "complete"
        and telemetry["cgroup_memory_status"] == "complete"
        and telemetry["cgroup_memory_reason"] == "none"
        and len(memory_rows) == 2 and set(memory_by_phase) == {"off", "on"}
        and all(
            row["status"] == "complete" and row["sample_count"] == 5
            and row["gc_status"] == "complete" and row["gc_overflow"] is False
            and row["cgroup_memory_status"] == "complete"
            and row["cgroup_memory_reason"] == "none"
            and row["cgroup_sample_count"] == 5
            and all(row[field] is not None for field in required_node_values)
            for row in memory_rows
        )
    )
    if (worker.get("status") != "complete" or worker["submitted"] < 1
            or worker["completed"] != worker["submitted"]
            or statuses != {"200": worker["completed"]} or worker["error_counts"]
            or worker["off_probe"] != {
                "submitted": report.get("concurrency"),
                "completed": report.get("concurrency"), "http_status": 200,
            }
            or worker["observed_duration_ms"] < 19_500
            or memory.get("status") != "complete" or memory.get("terminal_decision") != "complete"
            or memory.get("terminal_reason") != "none" or memory.get("target_identity_status") != "complete"
            or memory.get("samples", 0) < 2
            or type(memory.get("current_max_bytes")) is not int
            or memory["current_max_bytes"] >= 768 * 1024 * 1024
            or not isinstance(events, dict)
            or any(events[name] != 0 for name in ("high", "max", "oom", "oom_kill", "oom_group_kill"))
            or runner.get("status") != "complete"
            or runner.get("window_scope") != "on_window"
            or not cpu_complete or not node_memory_complete):
        return "INVALID"
    return "PASS"


def _safe_json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "other"


def _sanitization_failure(
    stage: str, reason: str, *, value: Any = None,
) -> _CapacitySanitizationError:
    """Describe only allowlisted structure, never values or unexpected keys."""

    stage = stage if stage in SANITIZER_FAILURE_STAGES else "report_projection"
    reason = reason if reason in SANITIZER_FAILURE_REASONS else "unexpected"
    section = "report"
    field = "report_shape"
    observed: Any = value
    present = value is not None
    if reason == "runner_identity_shape" and isinstance(value, dict):
        section = "runner_headroom"
        field = "identity"
        runner = value.get("runner_headroom")
        present = isinstance(runner, dict) and "identity" in runner
        observed = runner.get("identity") if isinstance(runner, dict) else None
    return _CapacitySanitizationError({
        "status": "invalid",
        "stage": stage,
        "reason": reason,
        "section": section,
        "field": field,
        "present": present,
        "value_type": _safe_json_type(observed),
        "binding_status": "unknown",
        "matrix_credit": False,
        "slo_credit": False,
        "diagnostic_credit": False,
    })


def _projection_rejection_reason(value: Any) -> str:
    """Recognize one known producer mismatch without relaxing projection."""

    if isinstance(value, dict):
        runner = value.get("runner_headroom")
        if isinstance(runner, dict) and set(runner) == {
            "schema", "status", "reason", "runner_environment", "identity", "sample_count",
            "window_seconds", "effective_cpu_capacity_cores", "max_cgroup_cpu_usage_cores",
            "max_visible_throttled_periods_delta", "window_scope",
            "window_start_offset_ms", "window_end_offset_ms",
        }:
            identity = runner.get("identity")
            expected_identity = {
                "outer_pid_namespace_matches_pid1", "outer_mount_namespace_matches_pid1",
                "outer_cgroup_namespace_matches_pid1", "target_cgroup_namespace_matches_outer",
                "target_mount_namespace_is_isolated", "target_pid_namespace_is_isolated",
                "global_cgroup2_mount", "workload_membership_matches_outer",
            }
            if (not isinstance(identity, dict) or set(identity) != expected_identity
                    or any(type(item) is not bool for item in identity.values())):
                return "runner_identity_shape"
    return "report_projection_rejected"


def _project_supervisor_evidence(value: Any) -> dict[str, Any]:
    evidence = value if isinstance(value, Mapping) else {}
    reason = evidence.get("reason")
    startup_stage = evidence.get("startup_failure_stage")
    report_error = evidence.get("report_error_class", evidence.get("report_error"))
    report_phase = evidence.get("report_phase")

    def project_stage(name: str, allowed_stages: frozenset[str]) -> str | None:
        stage = evidence.get(name)
        if stage is None:
            return None
        return (
            stage if isinstance(stage, str) and stage in allowed_stages
            else "unknown"
        )

    return {
        "reason": reason if isinstance(reason, str) and reason in SUPERVISOR_REASONS else "other",
        "worker_started": evidence.get("worker_started")
        if type(evidence.get("worker_started")) is bool else None,
        "worker_exited": evidence.get("worker_exited")
        if type(evidence.get("worker_exited")) is bool else None,
        "namespace_closed": evidence.get("namespace_closed")
        if type(evidence.get("namespace_closed")) is bool else None,
        "return_code": evidence.get("return_code")
        if type(evidence.get("return_code")) is int and -255 <= evidence["return_code"] <= 255 else None,
        "signal": evidence.get("signal")
        if type(evidence.get("signal")) is int and 1 <= evidence["signal"] <= 255 else None,
        "startup_failure_stage": (
            startup_stage if isinstance(startup_stage, str)
            and startup_stage in load_runtime.SUPERVISOR_STARTUP_FAILURE_STAGES
            else "unknown" if startup_stage is not None else None
        ),
        "namespace_ready_failure_stage": project_stage(
            "namespace_ready_failure_stage",
            frozenset(load_runtime.NAMESPACE_STARTUP_FAILURE_STAGES),
        ),
        "namespace_post_ready_failure_stage": project_stage(
            "namespace_post_ready_failure_stage",
            frozenset(load_runtime.NAMESPACE_STARTUP_FAILURE_STAGES),
        ),
        "watchdog_startup_failure_stage": project_stage(
            "watchdog_startup_failure_stage",
            load_runtime.WATCHDOG_STARTUP_FAILURE_STAGES,
        ),
        "report_error_class": (
            report_error
            if isinstance(report_error, str) and report_error in SUPERVISOR_REPORT_ERROR_CLASSES
            else "other" if report_error is not None else None
        ),
        "report_phase": (
            report_phase
            if isinstance(report_phase, str) and report_phase in SUPERVISOR_REPORT_PHASES
            else "other" if report_phase is not None else None
        ),
    }


def _parent_outcome_fingerprint(
    outcome: Mapping[str, Any], supervisor_evidence: Any = None,
    parent_phase: Any = None, prepare_lifecycle: Any = None,
    cleanup_lifecycle: Any = None,
) -> dict[str, Any]:
    worker = outcome.get("worker")
    worker_status = worker.get("status") if isinstance(worker, dict) else "missing"
    if (not isinstance(worker_status, str)
            or worker_status not in {"complete", "incomplete", "failed", "missing"}):
        worker_status = "invalid"
    runner = outcome.get("runner_headroom")
    runner_status = runner.get("status") if isinstance(runner, dict) else "missing"
    if (not isinstance(runner_status, str)
            or runner_status not in {"complete", "unknown", "missing"}):
        runner_status = "invalid"
    runner_reason = runner.get("reason") if isinstance(runner, dict) else "missing"
    if not isinstance(runner_reason, str) or runner_reason not in RUNNER_CONTEXT_REASONS:
        runner_reason = "other"
    namespace_closed = outcome.get("namespace_closed")
    if type(namespace_closed) is not bool:
        namespace_closed = None
    cleanup = outcome.get("remote_plan_cleanup")
    if (not isinstance(cleanup, str)
            or cleanup not in {"complete", "failed", "not_attempted"}):
        cleanup = "invalid"
    failure_only = outcome.get("failure_only") is True
    prepare_status = outcome.get("plan_prepare_status") if failure_only else None
    if (not isinstance(prepare_status, str)
            or prepare_status not in PLAN_PREPARE_STATUSES):
        prepare_status = None
    if prepare_status is None and isinstance(prepare_lifecycle, Mapping):
        candidate = prepare_lifecycle.get("plan_prepare_status")
        if isinstance(candidate, str) and candidate in PLAN_PREPARE_STATUSES:
            prepare_status = candidate
    if prepare_status is None:
        prepare_status = "not_attempted"
    cleanup_status = outcome.get("plan_cleanup_status") if failure_only else None
    if (not isinstance(cleanup_status, str)
            or cleanup_status not in PLAN_CLEANUP_STATUSES):
        cleanup_status = None
    if isinstance(cleanup_lifecycle, Mapping):
        candidate_cleanup = cleanup_lifecycle.get("plan_cleanup_status")
        if isinstance(candidate_cleanup, str) and candidate_cleanup in PLAN_CLEANUP_STATUSES:
            cleanup_status = candidate_cleanup
    if cleanup_status is None:
        cleanup_status = "not_attempted"
    elif cleanup_status == "complete":
        cleanup = "complete"
    elif cleanup_status in {"pending", "refused", "transport_failed", "bad_protocol", "failed"}:
        cleanup = "failed"
    elif prepare_status in {"not_attempted", "refused"}:
        cleanup = "not_attempted"
    else:
        cleanup = "failed"
    selected_parent_phase = outcome.get("stage") if failure_only else parent_phase
    if (not isinstance(selected_parent_phase, str)
            or selected_parent_phase not in PARENT_FAILURE_STAGES | {"report_write"}):
        selected_parent_phase = "parent_outcome"
    parent_error_class = outcome.get("error_class") if failure_only else None
    if (not isinstance(parent_error_class, str)
            or parent_error_class not in PARENT_ERROR_CLASSES):
        parent_error_class = None
    prepare_transport = outcome.get("ssh_prepare_transport") if failure_only else None
    prepare_transport = _project_parent_ssh_transport(prepare_transport)
    prepare_refusal_class = outcome.get("plan_prepare_refusal_class") if failure_only else None
    if (not isinstance(prepare_refusal_class, str)
            or prepare_refusal_class not in PLAN_REFUSAL_CLASSES):
        prepare_refusal_class = "none"
    fingerprint = {
        "status": "incomplete",
        "stage": "parent_outcome",
        "parent_phase": selected_parent_phase,
        "parent_error_class": parent_error_class,
        "parent_return_code": prepare_transport["return_code"],
        "plan_prepare_status": prepare_status,
        "plan_prepare_refusal_class": prepare_refusal_class,
        "plan_cleanup_status": cleanup_status,
        "reason": "capacity_report_incomplete",
        "namespace_closed": namespace_closed,
        "worker_present": isinstance(worker, dict),
        "worker_status": worker_status,
        "runner_status": runner_status,
        "runner_context_reason": runner_reason,
        "remote_plan_cleanup": cleanup,
        "matrix_credit": False,
        "slo_credit": False,
        "diagnostic_credit": False,
    }
    evidence = _project_supervisor_evidence(supervisor_evidence)
    for source, destination in (
        ("reason", "supervisor_reason"), ("worker_started", "supervisor_process_started"),
        ("worker_exited", "supervisor_process_exited"),
        ("namespace_closed", "supervisor_namespace_closed"),
        ("return_code", "supervisor_return_code"), ("signal", "supervisor_signal"),
        ("startup_failure_stage", "supervisor_startup_failure_stage"),
        ("namespace_ready_failure_stage", "supervisor_namespace_ready_failure_stage"),
        ("namespace_post_ready_failure_stage", "supervisor_namespace_post_ready_failure_stage"),
        ("watchdog_startup_failure_stage", "supervisor_watchdog_startup_failure_stage"),
        ("report_phase", "supervisor_report_phase"),
        ("report_error_class", "supervisor_report_error_class"),
    ):
        fingerprint[destination] = evidence[source]
    return fingerprint


def _parent_failure_fingerprint(
    outcome: Mapping[str, Any], *, args: argparse.Namespace,
    stage_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a capacity attempt into a bound, identity-free fingerprint."""

    worker = outcome.get("worker")
    worker_summary: dict[str, Any] | None = None
    if isinstance(worker, Mapping):
        worker_status = worker.get("status")
        if not isinstance(worker_status, str) or worker_status not in {"complete", "incomplete", "failed"}:
            worker_status = "invalid"
        submitted = worker.get("submitted")
        completed = worker.get("completed")
        counts_valid = _worker_counts_are_closed(worker)
        statuses = worker.get("http_status_counts")
        errors = worker.get("error_counts")
        off_probe = worker.get("off_probe")
        worker_summary = {
            "status": worker_status,
            "reason": (
                worker.get("resource_abort_reason")
                if isinstance(worker.get("resource_abort_reason"), str)
                and worker.get("resource_abort_reason") in CAPACITY_ABORT_REASONS
                else "other"
            ),
            "submitted": submitted if type(submitted) is int and 0 <= submitted <= 100_000 else None,
            "completed": completed if type(completed) is int and 0 <= completed <= 100_000 else None,
            "counts_valid": counts_valid,
            "http_status_counts": dict(sorted(statuses.items())) if counts_valid else {},
            "error_counts": dict(sorted(errors.items())) if counts_valid else {},
            "off_probe": {
                "submitted": off_probe.get("submitted"),
                "completed": off_probe.get("completed"),
                "http_status": off_probe.get("http_status"),
            } if isinstance(off_probe, Mapping)
                and set(off_probe) == {"submitted", "completed", "http_status"}
                and type(off_probe.get("submitted")) is int
                and 0 <= off_probe["submitted"] <= args.concurrency
                and type(off_probe.get("completed")) is int
                and 0 <= off_probe["completed"] <= off_probe["submitted"]
                and (off_probe.get("http_status") is None
                     or type(off_probe.get("http_status")) is int
                     and 100 <= off_probe["http_status"] <= 599)
                else None,
        }
        if worker.get("resource_abort_reason") == "off_warmup_failed":
            warmup_failure = _project_warmup_failure(
                worker.get("warmup_failure"), concurrency=args.concurrency,
            )
            if warmup_failure is not None:
                worker_summary["warmup_failure"] = warmup_failure
    cleanup = stage_state.get("cleanup_lifecycle")
    cleanup = cleanup if isinstance(cleanup, Mapping) else {}
    runner = outcome.get("runner_headroom")
    runner = runner if isinstance(runner, Mapping) else {}
    stage = stage_state.get("parent_phase", stage_state.get("stage"))
    parent_status = outcome.get("status")
    if not isinstance(parent_status, str) or parent_status not in {
        "complete", "resource_abort", "incomplete",
    }:
        parent_status = "incomplete"
    parent_passed = outcome.get("passed") is True and parent_status == "complete"
    parent_error_class = outcome.get("error_class")
    if not isinstance(parent_error_class, str) or parent_error_class not in PARENT_ERROR_CLASSES:
        parent_error_class = None
    observer = stage_state.get("memory_observer_failure")
    if not isinstance(observer, Mapping):
        observer = {}
    observer_failure = {
        "stage": observer.get("failure_stage")
        if isinstance(observer.get("failure_stage"), str)
        and observer.get("failure_stage") in MEMORY_OBSERVER_FAILURE_STAGES else "unknown",
        "error_class": observer.get("error_class")
        if isinstance(observer.get("error_class"), str)
        and observer.get("error_class") in MEMORY_OBSERVER_ERROR_CLASSES else "unknown",
        "remote_stage": observer.get("remote_failure_stage")
        if isinstance(observer.get("remote_failure_stage"), str)
        and observer.get("remote_failure_stage") in REMOTE_WATCH_FAILURE_STAGES else "unknown",
        "remote_reason": observer.get("remote_failure_reason")
        if isinstance(observer.get("remote_failure_reason"), str)
        and observer.get("remote_failure_reason") in REMOTE_WATCH_FAILURE_REASONS else "unknown",
    }
    return {
        "schema": 1,
        "event": "ssr_capacity_parent_fingerprint",
        "diagnostic_only": True,
        "status": "diagnostic_only",
        "parent_status": parent_status,
        "parent_passed": parent_passed,
        "parent_error_class": parent_error_class,
        # This is derived from the same pass predicate as the CLI return path;
        # it is not a separately observed process exit code.
        "parent_exit_code": 0 if parent_passed else 2,
        "parent_exit_code_basis": "derived_from_passed_outcome",
        "passed": False,
        "matrix_credit": False,
        "slo_credit": False,
        "diagnostic_credit": False,
        "source_sha": args.source_sha,
        "app_target_sha": args.app_target_sha,
        "external_run_id": args.external_run_id,
        "external_run_attempt": args.external_run_attempt,
        "workload": getattr(args, "workload", WORKLOAD),
        "fixture_kind": _profile_spec(getattr(args, "workload", WORKLOAD))[0],
        "run_id": outcome.get("run_id") if isinstance(outcome.get("run_id"), str)
        and RUN_RE.fullmatch(outcome["run_id"]) is not None else stage_state.get("run_id"),
        "profile_digest": stage_state.get("profile_digest"),
        "fixture_manifest_sha256": stage_state.get("manifest_sha256"),
        "concurrency": args.concurrency,
        "namespace_closed": outcome.get("namespace_closed") is True,
        "parent_phase": stage if isinstance(stage, str) and stage in PARENT_FAILURE_STAGES else "parent_outcome",
        "worker": worker_summary,
        "runner": {
            "status": runner.get("status") if isinstance(runner.get("status"), str)
            and runner.get("status") in {"complete", "unknown"} else "missing",
            "reason": runner.get("reason") if isinstance(runner.get("reason"), str)
            and runner.get("reason") in RUNNER_CONTEXT_REASONS else "other",
        },
        "memory_observer_failure": observer_failure,
        "cleanup": {
            "status": cleanup.get("plan_cleanup_status")
            if isinstance(cleanup.get("plan_cleanup_status"), str)
            and cleanup.get("plan_cleanup_status") in PLAN_CLEANUP_STATUSES else "not_attempted",
            "refusal_class": cleanup.get("plan_cleanup_refusal_class")
            if isinstance(cleanup.get("plan_cleanup_refusal_class"), str)
            and cleanup.get("plan_cleanup_refusal_class") in PLAN_REFUSAL_CLASSES else "none",
            "transport": _project_parent_ssh_transport(cleanup.get("cleanup_transport")),
        },
        "supervisor": _project_supervisor_evidence(stage_state.get("supervisor_evidence")),
    }


def _write_parent_failure_fingerprint(
    path: Path, payload: Mapping[str, Any],
) -> None:
    raw = json.dumps(
        dict(payload), ensure_ascii=True, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    if len(raw) > 16 * 1024:
        raise ValueError("capacity failure fingerprint exceeds bound")
    _write_report(path, payload)


def sanitize_capacity_report(
    *, input_path: Path, output_path: Path, manifest_path: Path,
    source_sha: str, app_target_sha: str, run_id: str, attempt: str,
    workload: str = WORKLOAD,
) -> str:
    if not isinstance(workload, str) or workload not in {WORKLOAD, HTML_WORKLOAD}:
        raise _sanitization_failure("report_binding", "report_binding_rejected")
    try:
        manifest_raw, manifest, _actors = _read_manifest(manifest_path)
    except Exception:
        raise _sanitization_failure("manifest_read", "manifest_unavailable") from None
    try:
        before = input_path.lstat()
    except OSError:
        raise _sanitization_failure("report_read", "report_unavailable") from None
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0 or before.st_size > 256 * 1024):
        raise _sanitization_failure("report_read", "report_metadata_rejected")
    try:
        fd = os.open(input_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        raise _sanitization_failure("report_read", "report_unavailable") from None
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, 256 * 1024 + 1)
        after = os.fstat(fd)
        current = input_path.lstat()
    finally:
        os.close(fd)
    if (len(raw) > 256 * 1024 or not raw.endswith(b"\n")
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)):
        raise _sanitization_failure("report_read", "report_changed")
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("capacity report has duplicate fields")
            result[key] = item
        return result

    try:
        value = json.loads(raw[:-1].decode("ascii"), object_pairs_hook=reject_duplicate_pairs)
        canonical = json.dumps(
            value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("ascii") + b"\n"
    except Exception:
        raise _sanitization_failure("report_read", "report_parse_rejected") from None
    if canonical != bytes(raw):
        raise _sanitization_failure("report_read", "report_noncanonical")
    failure_receipt = _project_parent_failure_receipt(value)
    if failure_receipt is not None:
        if any((
            failure_receipt.get("workload", WORKLOAD) != workload,
            failure_receipt.get("source_sha") != source_sha,
            failure_receipt.get("app_target_sha") != app_target_sha,
            failure_receipt.get("external_run_id") != run_id,
            failure_receipt.get("external_run_attempt") != attempt,
            manifest.get("runner_sha") != source_sha,
            manifest.get("app_target_sha") != app_target_sha,
        )):
            failure = _sanitization_failure("report_binding", "report_binding_rejected")
            failure.payload["binding_status"] = "rejected"
            raise failure
        actual_manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        if failure_receipt["fixture_manifest_sha256"] is None:
            failure_receipt = {
                **failure_receipt,
                "fixture_manifest_sha256": actual_manifest_sha,
                "fixture_manifest_bytes": len(manifest_raw),
            }
        elif failure_receipt["fixture_manifest_sha256"] != actual_manifest_sha:
            failure = _sanitization_failure("report_binding", "report_binding_rejected")
            failure.payload["binding_status"] = "rejected"
            raise failure
        try:
            _write_report(output_path, failure_receipt)
        except Exception:
            raise _sanitization_failure("output_write", "output_write_failed") from None
        return "INVALID"
    projected = _project_capacity_report(value)
    if (projected is None or projected.get("workload") != workload
            or projected.get("source_sha") != source_sha
            or projected.get("app_target_sha") != app_target_sha
            or projected.get("external_run_id") != run_id
            or projected.get("external_run_attempt") != attempt
            or projected.get("fixture_manifest_sha256") != hashlib.sha256(manifest_raw).hexdigest()
            or manifest.get("runner_sha") != source_sha
            or manifest.get("app_target_sha") != app_target_sha):
        if projected is None:
            reason = _projection_rejection_reason(value)
            raise _sanitization_failure("report_projection", reason, value=value)
        failure = _sanitization_failure("report_binding", "report_binding_rejected")
        failure.payload["binding_status"] = "rejected"
        raise failure
    try:
        _write_report(output_path, projected)
    except Exception:
        raise _sanitization_failure("output_write", "output_write_failed") from None
    return evaluate_capacity_report(
        projected, source_sha=source_sha, app_target_sha=app_target_sha,
        run_id=run_id, attempt=attempt,
        manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        workload=workload,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("worker-entry", "parent", "sanitize-report"))
    parser.add_argument("--manifest")
    parser.add_argument("--report")
    parser.add_argument("--failure-report")
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--source-sha")
    parser.add_argument("--app-target-sha")
    parser.add_argument("--external-run-id")
    parser.add_argument("--external-run-attempt")
    parser.add_argument("--concurrency", type=int, choices=tuple(sorted(CONCURRENCY_PRESETS | HTML_CONCURRENCY_PRESETS)))
    parser.add_argument("--workload", choices=(WORKLOAD, HTML_WORKLOAD), default=WORKLOAD)
    parser.add_argument("--ssh-host", default=os.environ.get("PROD_SSH_HOST", ""))
    parser.add_argument("--ssh-user", default=os.environ.get("PROD_SSH_USER", ""))
    parser.add_argument("--ssh-config")
    parser.add_argument("--ssh-key")
    args = parser.parse_args(argv)
    if args.command == "worker-entry":
        return worker_entrypoint()
    if args.command == "sanitize-report":
        if (not args.input or not args.output or not args.manifest or not args.source_sha
                or not args.app_target_sha or not args.external_run_id or not args.external_run_attempt):
            parser.error("sanitize-report command arguments are incomplete")
        try:
            decision = sanitize_capacity_report(
                input_path=Path(args.input), output_path=Path(args.output),
                manifest_path=Path(args.manifest), source_sha=args.source_sha,
                app_target_sha=args.app_target_sha, run_id=args.external_run_id,
                attempt=args.external_run_attempt, workload=args.workload,
            )
        except Exception as exc:
            if isinstance(exc, _CapacitySanitizationError):
                diagnostic = exc.payload
            else:
                diagnostic = _sanitization_failure(
                    "report_read", "unexpected",
                ).payload
            print(json.dumps(diagnostic, sort_keys=True, separators=(",", ":")))
            return 2
        print(decision)
        return 0
    if (not args.manifest or not args.report or not args.source_sha or not args.app_target_sha
            or not args.external_run_id or not args.external_run_attempt
            or args.concurrency is None or not args.ssh_config or not args.ssh_key
            or not isinstance(args.workload, str)
            or args.workload not in {WORKLOAD, HTML_WORKLOAD}
            or re.fullmatch(r"[A-Za-z0-9.-]{1,253}", args.ssh_host) is None
            or re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", args.ssh_user) is None):
        parser.error("parent command arguments are incomplete")
    if args.failure_report is not None:
        report_path = Path(args.report)
        failure_path = Path(args.failure_report)
        if failure_path.name != "failure-fingerprint.json" or failure_path.parent != report_path.parent:
            parser.error("parent failure report path is invalid")
    stage_state: dict[str, Any] = {}
    try:
        outcome = _parent_run(args, stage_state)
        parent_phase = stage_state.get("stage")
        stage_state["parent_phase"] = parent_phase
        stage_state["stage"] = "report_write"
        _write_report(Path(args.report), outcome)
        if args.failure_report is not None:
            try:
                _write_parent_failure_fingerprint(
                    Path(args.failure_report),
                    _parent_failure_fingerprint(outcome, args=args, stage_state=stage_state),
                )
            except Exception:
                # This diagnostic artifact must not change the capacity
                # outcome or the existing namespace/credit decision.
                pass
    except Exception as exc:
        stage = stage_state.get("stage", "identity")
        if not isinstance(stage, str) or stage not in PARENT_FAILURE_STAGES:
            stage = "identity"
        error_name = type(exc).__name__
        error_class = error_name if error_name in PARENT_ERROR_CLASSES else "unexpected"
        prepare_lifecycle = stage_state.get("prepare_lifecycle")
        if not isinstance(prepare_lifecycle, dict):
            prepare_lifecycle = {}
        cleanup_lifecycle = stage_state.get("cleanup_lifecycle")
        if not isinstance(cleanup_lifecycle, dict):
            cleanup_lifecycle = {}
        cleanup_ok = stage_state.get("cleanup_ok") is True
        run_id = stage_state.get("run_id")
        if (
            isinstance(run_id, str) and RUN_RE.fullmatch(run_id) is not None
            and prepare_lifecycle.get("plan_prepare_status") == "prepared"
            and cleanup_lifecycle.get("plan_cleanup_status") is None
            and stage_state.get("namespace_closed") is True
            and _prepare_transport_is_closed(prepare_lifecycle)
        ):
            try:
                cleanup_lifecycle = {}
                planned_expiry = stage_state.get("plan_expiry_ms")
                plan_created_at_ms = stage_state.get("plan_created_at_ms")
                plan_created_monotonic = stage_state.get("plan_created_monotonic")
                if (type(planned_expiry) is not int
                        or type(plan_created_at_ms) is not int
                        or not isinstance(plan_created_monotonic, (int, float))):
                    raise ValueError("capacity plan expiry unavailable")
                _wait_for_remote_plan_expiry(
                    planned_expiry,
                    plan_created_at_ms=plan_created_at_ms,
                    plan_created_monotonic=plan_created_monotonic,
                )
                cleanup_ok, _ = _cleanup_remote_capacity_plan(
                    host=args.ssh_host, user=args.ssh_user,
                    config=Path(args.ssh_config), key=Path(args.ssh_key),
                    run_id=run_id, lifecycle=cleanup_lifecycle,
                )
            except TimeoutError:
                cleanup_ok = False
                cleanup_lifecycle = {
                    "plan_cleanup_status": "not_attempted",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "not_started", "return_code": None,
                        "stdout_shape": "none", "child_state": "not_started",
                    },
                }
            except Exception:
                cleanup_ok = False
                cleanup_lifecycle = {
                    "plan_cleanup_status": "transport_failed",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "transport_failed", "return_code": None,
                        "stdout_shape": "none", "child_state": "unknown",
                    },
                }
        if not cleanup_lifecycle:
            cleanup_lifecycle = {
                "plan_cleanup_status": "not_attempted",
                "plan_cleanup_refusal_class": "none",
                "cleanup_transport": {
                    "stage": "not_started", "return_code": None,
                    "stdout_shape": "none", "child_state": "unknown",
                },
            }
        identity = (
            isinstance(stage_state.get("source_sha", args.source_sha), str)
            and SHA_RE.fullmatch(stage_state.get("source_sha", args.source_sha)) is not None
            and isinstance(stage_state.get("app_target_sha", args.app_target_sha), str)
            and SHA_RE.fullmatch(stage_state.get("app_target_sha", args.app_target_sha)) is not None
            and isinstance(stage_state.get("external_run_id", args.external_run_id), str)
            and str(stage_state.get("external_run_id", args.external_run_id)).isdecimal()
            and isinstance(stage_state.get("external_run_attempt", args.external_run_attempt), str)
            and ATTEMPT_RE.fullmatch(str(stage_state.get("external_run_attempt", args.external_run_attempt))) is not None
            and isinstance(run_id, str) and RUN_RE.fullmatch(run_id) is not None
            and isinstance(stage_state.get("profile_digest"), str)
            and re.fullmatch(r"[0-9a-f]{64}", stage_state["profile_digest"]) is not None
            and type(stage_state.get("concurrency", args.concurrency)) is int
            and stage_state.get("concurrency", args.concurrency) in _profile_spec(
                stage_state.get("workload", getattr(args, "workload", WORKLOAD)),
            )[1]
        )
        if identity:
            receipt = _parent_failure_receipt(
                error_class=error_class, stage=stage,
                source_sha=stage_state.get("source_sha", args.source_sha),
                app_target_sha=stage_state.get("app_target_sha", args.app_target_sha),
                external_run_id=stage_state.get("external_run_id", args.external_run_id),
                external_run_attempt=stage_state.get("external_run_attempt", args.external_run_attempt),
                run_id=run_id,
                profile_digest_value=stage_state["profile_digest"],
                manifest_sha256=stage_state.get("manifest_sha256"),
                manifest_bytes=stage_state.get("manifest_bytes"),
                concurrency=stage_state.get("concurrency", args.concurrency),
                prepare_lifecycle=prepare_lifecycle,
                cleanup_lifecycle=cleanup_lifecycle, cleanup_ok=cleanup_ok,
                workload=stage_state.get("workload", getattr(args, "workload", WORKLOAD)),
            )
            try:
                if not os.path.lexists(args.report):
                    _write_report(Path(args.report), receipt)
            except Exception:
                pass
            if args.failure_report is not None:
                try:
                    _write_parent_failure_fingerprint(
                        Path(args.failure_report),
                        _parent_failure_fingerprint(receipt, args=args, stage_state=stage_state),
                    )
                except Exception:
                    pass
        failure_fingerprint = {
            "status": "incomplete", "error_class": error_class, "stage": stage,
        }
        if isinstance(stage_state.get("supervisor_evidence"), Mapping):
            evidence = _project_supervisor_evidence(stage_state["supervisor_evidence"])
            failure_fingerprint.update({
                "supervisor_reason": evidence["reason"],
                "supervisor_process_started": evidence["worker_started"],
                "supervisor_process_exited": evidence["worker_exited"],
                "supervisor_namespace_closed": evidence["namespace_closed"],
                "supervisor_return_code": evidence["return_code"],
                "supervisor_signal": evidence["signal"],
                "supervisor_startup_failure_stage": evidence["startup_failure_stage"],
                "supervisor_namespace_ready_failure_stage": evidence[
                    "namespace_ready_failure_stage"
                ],
                "supervisor_namespace_post_ready_failure_stage": evidence[
                    "namespace_post_ready_failure_stage"
                ],
                "supervisor_watchdog_startup_failure_stage": evidence[
                    "watchdog_startup_failure_stage"
                ],
            })
        print(json.dumps(failure_fingerprint, sort_keys=True, separators=(",", ":")))
        return 2
    if outcome.get("passed") is True:
        return 0
    print(json.dumps(
        _parent_outcome_fingerprint(
            outcome, stage_state.get("supervisor_evidence"),
            parent_phase=parent_phase,
            prepare_lifecycle=stage_state.get("prepare_lifecycle"),
            cleanup_lifecycle=stage_state.get("cleanup_lifecycle"),
        ), sort_keys=True, separators=(",", ":"),
    ))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
