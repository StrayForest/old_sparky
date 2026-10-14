from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import tools.platform_external_load as external_load
from tools.platform_evidence_sanitizer import project_public_artifact
from tools.platform_external_load import (
    ExternalLoadError,
    LogicalRequestResult,
    RequestResult,
    VirtualUser,
    _route_for_read,
    analyze_concurrency_ramp,
    _ready_vote_action,
    _annotate_timing,
    _ResultAccumulator,
    _request,
    load_manifest,
    percentile,
    run_load,
    run_phase,
    run_rate_phase,
    spread_offsets,
    summarize_logical_results,
    summarize_results,
)


RSS_PROBE = Path(__file__).with_name("fixtures") / "external_load_rss_probe.py"
RSS_DEADLINE_SECONDS = 15.0
RSS_DEADLINES = {"live": RSS_DEADLINE_SECONDS, "accumulator": 30.0, "hostile": 0.5}
LIVE_HWM_LIMIT_BYTES = 192 * 1024 * 1024
ACCUMULATOR_HWM_LIMIT_BYTES = 24 * 1024 * 1024
PROBE_OUTPUT_LIMIT_BYTES = 64 * 1024


def _run_rss_probe(mode: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        stdout_path, stderr_path = Path(directory) / "out", Path(directory) / "err"
        started = time.monotonic()
        deadline_seconds = RSS_DEADLINES[mode]
        deadline = started + deadline_seconds
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, str(RSS_PROBE), mode],
                cwd=Path(__file__).parents[1],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                start_new_session=True,
                preexec_fn=(
                    (lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN) or os.write(1, b"hostile-ready\n"))
                    if mode == "hostile" else None
                ),
            )
            pgid = process.pid
            cleanup_budget = min(0.5, deadline_seconds / 2)
            def signal_group(sig: int) -> None:
                try:
                    if process.poll() is None:
                        if os.getpgid(process.pid) != pgid:
                            raise AssertionError("probe process-group identity changed")
                        os.killpg(pgid, sig)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic() - cleanup_budget))
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGTERM)
                try:
                    process.wait(timeout=max(0.0, min(0.25, cleanup_budget / 2, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    signal_group(signal.SIGKILL)
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
            finally:
                signal_group(signal.SIGKILL)
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        if elapsed > deadline_seconds:
            raise AssertionError("RSS probe exceeded its absolute deadline")
        if os.path.exists(f"/proc/{pgid}"):
            raise AssertionError("RSS probe process was not reaped")
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("RSS probe process group survived cleanup")
        if process.returncode != 0:
            raise AssertionError(f"RSS probe failed (returncode={process.returncode}, stdout_bytes={min(stdout_path.stat().st_size, PROBE_OUTPUT_LIMIT_BYTES)}, stderr_bytes={min(stderr_path.stat().st_size, PROBE_OUTPUT_LIMIT_BYTES)})")
        if stdout_path.stat().st_size > PROBE_OUTPUT_LIMIT_BYTES or stderr_path.stat().st_size > PROBE_OUTPUT_LIMIT_BYTES:
            raise AssertionError("RSS probe output exceeded its cap")
        if mode == "hostile" and b"hostile-ready\n" not in stdout_path.read_bytes():
            raise AssertionError("RSS probe ready marker missing")
        if stderr_path.read_bytes():
            raise AssertionError("RSS probe emitted stderr")
        report = json.loads(stdout_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or report.get("mode") != mode:
            raise AssertionError("RSS probe report is invalid")
        return report


def manifest_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "purpose": "external_ready_vote",
        "origin": "https://old-sparky.com",
        "session_cookie_name": "deadlock_platform_session",
        "csrf_cookie_name": "deadlock_platform_session_csrf",
        "marker": "preprod26082900000000ab",
        "tournaments": [
            {"id": "tournament-1", "slug": "qa-tournament", "user_count": 2}
        ],
        "users": [
            {
                "user_id": "user-00000001",
                "tournament_slug": "qa-tournament",
                "session_token": "s" * 64,
                "csrf_token": "c" * 64,
            },
            {
                "user_id": "user-00000002",
                "tournament_slug": "qa-tournament",
                "session_token": "t" * 64,
                "csrf_token": "d" * 64,
            },
        ],
    }


def load_manifest_from_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object], list[VirtualUser]]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_manifest(path)


class ExternalLoadTests(unittest.TestCase):
    @staticmethod
    def _cgroup_diagnostic_snapshot(
        *,
        usage_usec: int = 10,
        membership: str = "/tenant/job",
        mount_root: str = "/tenant",
        mountpoint: str = "/sys/fs/cgroup",
        mount_id: int = 36,
        namespace_identity: tuple[int, int] = (1, 2),
        max_files: dict[str, str] | None = None,
        stat_override: str | None = None,
    ) -> dict[str, object]:
        mountinfo = (
            f"{mount_id} 25 0:32 {mount_root} {mountpoint} rw - cgroup2 cgroup rw\n"
        )
        files = max_files or {}

        def reader(path: Path) -> str:
            if path.name == "cpu.max":
                return files.get(str(path), "max 100000\n")
            if path.name == "cpu.stat":
                if stat_override is not None:
                    return stat_override
                values = {
                    "usage_usec": usage_usec,
                    "user_usec": usage_usec // 2,
                    "system_usec": usage_usec // 2,
                    "nr_periods": 4,
                    "nr_throttled": 1,
                    "throttled_usec": 2,
                }
                return "".join(f"{key} {value}\n" for key, value in values.items())
            raise AssertionError("unexpected cgroup diagnostic file")

        return external_load._read_cgroup_diagnostic_snapshot(
            cgroup_text=f"0::{membership}\n",
            mountinfo_text=mountinfo,
            namespace_identity=namespace_identity,
            file_reader=reader,
        )

    def test_effective_cpu_capacity_stays_unknown_without_trusted_host_root(self) -> None:
        def unexpected_cpu_max_read(_path: Path) -> str:
            self.fail("visible cpu.max files cannot prove hidden ancestors absent")

        mount_cases = (
            ("/", "0::/tenant/job\n"),
            ("/tenant", "0::/tenant/job\n"),
        )
        for mount_root, cgroup_text in mount_cases:
            with self.subTest(mount_root=mount_root):
                mountinfo = (
                    f"36 25 0:32 {mount_root} /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
                )
                self.assertEqual(
                    external_load._effective_cpu_capacity_details(
                        cgroup_text=cgroup_text,
                        mountinfo_text=mountinfo,
                    ),
                    (None, "trusted_host_root_unproven"),
                )
                self.assertIsNone(
                    external_load._effective_cpu_capacity_cores(
                        cgroup_text=cgroup_text,
                        mountinfo_text=mountinfo,
                    )
                )

        ambiguous_mounts = (
            "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            "37 25 0:33 / /other/cgroup rw - cgroup2 cgroup rw\n"
        )
        self.assertEqual(
            external_load._effective_cpu_capacity_details(
                cgroup_text="0::/tenant/job\n",
                mountinfo_text=ambiguous_mounts,
            ),
            (None, "cgroup2_mount_ambiguous"),
        )
        self.assertEqual(
            external_load._effective_cpu_capacity_details(
                cgroup_text="0::/tenant/../private/path\n",
                mountinfo_text=(
                    "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
                ),
            ),
            (None, "cgroup_path_invalid"),
        )

    def test_cgroup_diagnostics_keep_subroot_max_unknown_and_report_visible_deltas(self) -> None:
        visible_limits = {
            "/sys/fs/cgroup/job/cpu.max": "max 100000\n",
            "/sys/fs/cgroup/cpu.max": "max 100000\n",
        }
        before = self._cgroup_diagnostic_snapshot(
            usage_usec=100, max_files=visible_limits
        )
        after = self._cgroup_diagnostic_snapshot(
            usage_usec=130, max_files=visible_limits
        )
        report = external_load._cgroup_diagnostic_report(before, after)
        self.assertEqual(report["visible_evidence_status"], "complete")
        self.assertEqual(report["quota_completeness"], "unknown")
        self.assertFalse(report["visible_mount_root_is_global"])
        self.assertEqual(report["visible_ancestor_count"], 2)
        self.assertEqual(
            [row["cpu_max_kind"] for row in report["visible_ancestors"]],
            ["max", "max"],
        )
        self.assertEqual(report["visible_ancestors"][0]["cpu_stat_delta"]["usage_usec"], 30)
        self.assertIn("trusted_host_root_unproven", report["reasons"])
        self.assertNotIn("/sys/fs/cgroup", json.dumps(report))
        self.assertNotIn("tenant", json.dumps(report))

        finite_limits = {
            "/sys/fs/cgroup/job/cpu.max": "99999999 100000000\n",
            "/sys/fs/cgroup/cpu.max": "max 100000\n",
        }
        finite_report = external_load._cgroup_diagnostic_report(
            self._cgroup_diagnostic_snapshot(usage_usec=100, max_files=finite_limits),
            self._cgroup_diagnostic_snapshot(usage_usec=130, max_files=finite_limits),
        )
        self.assertEqual(
            finite_report["visible_ancestors"][0]["cpu_max_quota_us"], 99_999_999
        )
        self.assertEqual(
            finite_report["visible_ancestors"][0]["cpu_max_period_us"], 100_000_000
        )

    def test_cgroup_diagnostic_mapping_rejects_malformed_or_ambiguous_mounts(self) -> None:
        with self.assertRaises(external_load._CgroupDiagnosticError) as mismatch:
            external_load._read_cgroup_diagnostic_snapshot(
                cgroup_text="0::/outside/job\n",
                mountinfo_text="36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
                namespace_identity=(1, 2),
                file_reader=lambda _path: "max 100000\n",
            )
        self.assertEqual(mismatch.exception.reason, "cgroup_mount_mapping_invalid")

        duplicate_mounts = (
            "36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
            "37 25 0:32 /tenant /other/cgroup rw - cgroup2 cgroup rw\n"
        )
        with self.assertRaises(external_load._CgroupDiagnosticError) as ambiguous:
            external_load._read_cgroup_diagnostic_snapshot(
                cgroup_text="0::/tenant/job\n",
                mountinfo_text=duplicate_mounts,
                namespace_identity=(1, 2),
                file_reader=lambda _path: "max 100000\n",
            )
        self.assertEqual(ambiguous.exception.reason, "cgroup2_mount_ambiguous")

        duplicate_stat = "".join(
            f"{key} {value}\n"
            for key, value in (
                ("usage_usec", 10),
                ("usage_usec", 11),
                ("user_usec", 5),
                ("system_usec", 5),
                ("nr_periods", 4),
                ("nr_throttled", 1),
                ("throttled_usec", 2),
            )
        )
        duplicate = self._cgroup_diagnostic_snapshot(stat_override=duplicate_stat)
        self.assertIn("cpu_stat_invalid", duplicate["reasons"])
        self.assertIsNone(duplicate["ancestors"][0]["cpu_stat"])
        partial_optional = self._cgroup_diagnostic_snapshot(
            stat_override="usage_usec 10\nuser_usec 5\nsystem_usec 5\nnr_periods 1\n"
        )
        self.assertIn("cpu_stat_invalid", partial_optional["reasons"])
        self.assertIsNone(partial_optional["ancestors"][0]["cpu_stat"])

    def test_cgroup_diagnostic_visible_root_omission_stays_unknown(self) -> None:
        root = Path("/sys/fs/cgroup")
        def snapshot(sequence: int) -> dict[str, object]:
            def reader(path: Path) -> str:
                if path == root / "job/cpu.max":
                    return "250000 100000\n"
                if path.name == "cpu.max":
                    raise FileNotFoundError
                if path.parent == root:
                    values = {
                        "usage_usec": 100 + sequence * 12,
                        "user_usec": 50 + sequence * 5,
                        "system_usec": 50 + sequence * 7,
                    }
                else:
                    values = {
                        "usage_usec": 10 + sequence * 15,
                        "user_usec": 5 + sequence * 6,
                        "system_usec": 5 + sequence * 9,
                        "nr_periods": 1 + sequence,
                        "nr_throttled": sequence,
                        "throttled_usec": sequence * 3,
                    }
                return "".join(f"{key} {value}\n" for key, value in values.items())

            return external_load._read_cgroup_diagnostic_snapshot(
                cgroup_text="0::/tenant/job\n",
                mountinfo_text="36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
                namespace_identity=(1, 2),
                file_reader=reader,
            )

        report = external_load._cgroup_diagnostic_report(snapshot(0), snapshot(1))
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["quota_completeness"], "unknown")
        self.assertIn("cpu_max_unavailable", report["reasons"])
        self.assertIn("cpu_stat_partial", report["reasons"])
        self.assertEqual(report["visible_ancestor_count"], 2)
        child, visible_root = report["visible_ancestors"]
        self.assertEqual(child["cpu_max_kind"], "quota")
        self.assertEqual(child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertEqual(visible_root["cpu_max_kind"], "unavailable")
        self.assertEqual(visible_root["cpu_stat_completeness"], "partial")
        self.assertEqual(visible_root["cpu_stat_delta"]["usage_usec"], 12)
        self.assertNotIn("nr_periods", visible_root["cpu_stat_delta"])

        stable_before = snapshot(0)
        stable_after = snapshot(1)
        child_before = stable_before["ancestors"][0]
        child_before["cpu_max"] = None
        stable_before["reasons"] = ["cpu_max_unavailable"]
        reverse_missing = external_load._cgroup_diagnostic_report(stable_before, stable_after)
        reverse_child = reverse_missing["visible_ancestors"][0]
        self.assertEqual(reverse_child["cpu_max_kind"], "unavailable")
        self.assertEqual(reverse_child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertIn("cpu_max_unavailable", reverse_missing["reasons"])

        stable_before = snapshot(0)
        stable_after = snapshot(1)
        stable_after["ancestors"][0]["cpu_max"] = None
        stable_after["reasons"] = ["cpu_max_unavailable"]
        forward_missing = external_load._cgroup_diagnostic_report(stable_before, stable_after)
        forward_child = forward_missing["visible_ancestors"][0]
        self.assertEqual(forward_child["cpu_max_kind"], "unavailable")
        self.assertEqual(forward_child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertIn("cpu_max_unavailable", forward_missing["reasons"])

        stable_before = snapshot(0)
        stable_after = snapshot(1)
        stable_after["ancestors"][0]["cpu_max"] = (300_000, 100_000)
        changed = external_load._cgroup_diagnostic_report(stable_before, stable_after)
        changed_child = changed["visible_ancestors"][0]
        self.assertEqual(changed["status"], "incomplete")
        self.assertEqual(changed_child["cpu_max_kind"], "unavailable")
        self.assertNotIn("cpu_max_quota_us", changed_child)
        self.assertEqual(changed_child["cpu_stat_delta"]["usage_usec"], 15)
        self.assertIn("cpu_max_changed", changed["reasons"])

    def test_cgroup_diagnostic_identity_changes_and_counter_resets_are_incomplete(self) -> None:
        before = self._cgroup_diagnostic_snapshot(usage_usec=100)
        variants = (
            (
                "namespace_identity_changed",
                self._cgroup_diagnostic_snapshot(usage_usec=130, namespace_identity=(1, 3)),
            ),
            (
                "membership_changed",
                self._cgroup_diagnostic_snapshot(usage_usec=130, membership="/tenant/other"),
            ),
            (
                "mount_identity_changed",
                self._cgroup_diagnostic_snapshot(usage_usec=130, mount_id=37),
            ),
        )
        for reason, after in variants:
            with self.subTest(reason=reason):
                report = external_load._cgroup_diagnostic_report(before, after)
                self.assertEqual(report["status"], "incomplete")
                self.assertIn(reason, report["reasons"])
                self.assertEqual(report["quota_completeness"], "unknown")

        changed_quota = self._cgroup_diagnostic_snapshot(
            usage_usec=130,
            max_files={"/sys/fs/cgroup/job/cpu.max": "50000 100000\n"},
        )
        changed_quota_report = external_load._cgroup_diagnostic_report(before, changed_quota)
        self.assertEqual(changed_quota_report["status"], "incomplete")
        self.assertIn("cpu_max_changed", changed_quota_report["reasons"])

        reset = self._cgroup_diagnostic_snapshot(usage_usec=20)
        reset_report = external_load._cgroup_diagnostic_report(before, reset)
        self.assertEqual(reset_report["status"], "incomplete")
        self.assertIn("cpu_stat_reset", reset_report["reasons"])

    def test_process_cpu_sampler_is_bounded_and_reports_truncation(self) -> None:
        with patch.object(external_load.time, "process_time_ns", side_effect=OSError):
            unavailable = external_load._ProcessCpuSampler()
            unavailable.start()
            unavailable_report = unavailable.stop()
        self.assertEqual(unavailable_report["status"], "unavailable")
        self.assertEqual(unavailable_report["samples"], [])

        with (
            patch.object(external_load, "CLIENT_CPU_SAMPLE_LIMIT", 4),
        ):
            sampler = external_load._ProcessCpuSampler()
            sampler.start()
            time.sleep(1.15)
            report = sampler.stop()
        self.assertEqual(report["scope"], "load_worker_process")
        self.assertIn(report["status"], {"complete", "truncated"})
        self.assertGreaterEqual(report["sample_count"], 1)
        self.assertLessEqual(report["sample_count"], 4)
        self.assertEqual(report["sample_count"], len(report["samples"]))
        for row in report["samples"]:
            self.assertLess(row["interval_started_at"], row["timestamp"])
            self.assertGreaterEqual(row["interval_elapsed_ms"], 100)  # sampler is still monotonic
            self.assertGreaterEqual(row["process_cpu_percent"], 0)

        with (
            patch.object(external_load, "CLIENT_CPU_SAMPLE_INTERVAL_SECONDS", 0.005),
            patch.object(external_load, "CLIENT_CPU_SAMPLE_LIMIT", 1),
        ):
            capped = external_load._ProcessCpuSampler()
            capped.start()
            time.sleep(0.03)
            capped_report = capped.stop()
        self.assertEqual(capped_report["status"], "truncated")
        self.assertEqual(capped_report["sample_count"], 1)

    def test_run_load_stops_cpu_sampler_when_workload_raises(self) -> None:
        sampler = Mock()
        sampler.stop.return_value = {"status": "unavailable", "samples": [], "sample_count": 0}
        with (
            patch.object(external_load, "_ProcessCpuSampler", return_value=sampler),
            patch.object(external_load, "_run_load_impl", side_effect=ExternalLoadError("fixture")),
            self.assertRaises(ExternalLoadError),
        ):
            run_load()
        sampler.start.assert_called_once_with()
        sampler.stop.assert_called_once_with()

    def test_run_load_attaches_only_cpu_intervals_inside_its_window(self) -> None:
        sampler = Mock()
        sampler.stop.return_value = {
            "schema": 1,
            "status": "complete",
            "scope": "load_worker_process",
            "sample_interval_ms": 1000,
            "affinity_cpu_count": 2,
            "effective_cpu_capacity_cores": None,
            "cpu_capacity_status": "unknown",
            "sample_count": 3,
            "samples": [
                {
                    "interval_started_at": "2026-10-11T10:00:00Z",
                    "timestamp": "2026-10-11T10:00:01Z",
                    "interval_elapsed_ms": 1000,
                    "process_cpu_percent": 20.0,
                },
                {
                    "interval_started_at": "2026-10-11T10:00:01Z",
                    "timestamp": "2026-10-11T10:00:02Z",
                    "interval_elapsed_ms": 1000,
                    "process_cpu_percent": 25.0,
                },
                {
                    "interval_started_at": "2026-10-11T10:00:02Z",
                    "timestamp": "2026-10-11T10:00:03Z",
                    "interval_elapsed_ms": 1000,
                    "process_cpu_percent": 30.0,
                },
            ],
        }
        report_source = {
            "started_at": "2026-10-11T10:00:00.5Z",
            "finished_at": "2026-10-11T10:00:02.5Z",
        }
        with (
            patch.object(external_load, "_ProcessCpuSampler", return_value=sampler),
            patch.object(external_load, "_run_load_impl", return_value=report_source),
        ):
            report = run_load()
        sampler.start.assert_called_once_with()
        sampler.stop.assert_called_once_with()
        usage = report["client_resource_usage"]
        self.assertEqual(usage["status"], "complete")
        self.assertEqual(usage["sample_count"], 1)
        self.assertEqual(usage["samples"][0]["timestamp"], "2026-10-11T10:00:02Z")

    def test_progress_callback_reports_only_completed_status_aggregates(self) -> None:
        users = [
            VirtualUser(f"progress-{index}", "synthetic", "s" * 32, "c" * 32)
            for index in range(2)
        ]
        callbacks: list[tuple[str, object | None]] = []

        def build(_origin: str, user: VirtualUser, phase: str, _timeout: float):
            first = RequestResult(phase, "GET", "/private", 200, 1.0, True, 0)
            if user.user_id.endswith("0"):
                return first
            retry = RequestResult(phase, "GET", "/private", 503, 1.0, False, 0)
            final = RequestResult(phase, "GET", "/private", 200, 1.0, True, 0)
            return LogicalRequestResult([retry, final], elapsed_ms=2.0)

        run_phase(
            "https://old-sparky.com",
            users,
            phase="synthetic-progress",
            spread_seconds=0,
            concurrency=2,
            timeout=1,
            request_builder=build,
            progress_callback=lambda phase, result: callbacks.append((phase, result)),
        )

        self.assertEqual(callbacks[0], ("http", None))
        completed = [result for phase, result in callbacks if phase == "http" and result is not None]
        self.assertEqual(len(completed), 2)
        self.assertEqual([len(getattr(item, "attempts", [item])) for item in completed], [1, 2])
        self.assertTrue(all(isinstance(item, (RequestResult, LogicalRequestResult)) for item in completed))
        # A broken diagnostic sink cannot change or stop measured requests.
        def fail_progress(_phase: str, _result: object | None) -> None:
            raise OSError("diagnostic-only failure")

        returned = run_phase(
            "https://old-sparky.com",
            users[:1],
            phase="synthetic-progress-failure",
            spread_seconds=0,
            concurrency=1,
            timeout=1,
            request_builder=build,
            progress_callback=fail_progress,
        )
        self.assertEqual(len(returned), 1)

        streamed = LogicalRequestResult(
            [
                RequestResult("stream", "GET", "/private", 503, 1.0, False, 1),
                RequestResult("stream", "GET", "/private", 200, 1.0, True, 1),
            ],
            elapsed_ms=2.0,
        )
        observed_statuses: list[list[int]] = []
        consumed: list[LogicalRequestResult] = []

        def capture_progress(phase: str, result: object | None) -> None:
            if phase == "http" and result is not None:
                observed_statuses.append([attempt.status for attempt in result.attempts])

        run_phase(
            "https://old-sparky.com",
            users[:1],
            phase="streamed-progress",
            spread_seconds=0,
            concurrency=1,
            timeout=1,
            request_builder=lambda *_args: streamed,
            result_consumer=consumed.append,
            progress_callback=capture_progress,
        )
        self.assertEqual(observed_statuses, [[503, 200]])
        self.assertEqual(consumed, [streamed])
        self.assertEqual(streamed.attempts, [])

        streamed_rate = LogicalRequestResult(
            [
                RequestResult("rate", "GET", "/private", 429, 1.0, False, 0),
                RequestResult("rate", "GET", "/private", 200, 1.0, True, 0),
            ],
            elapsed_ms=2.0,
        )
        rate_statuses: list[list[int]] = []

        def rate_progress(phase: str, result: object | None) -> None:
            if phase == "http" and result is not None:
                rate_statuses.append([attempt.status for attempt in result.attempts])

        run_rate_phase(
            "https://old-sparky.com",
            users[:1],
            phase="streamed-rate-progress",
            duration_seconds=0.1,
            concurrency=1,
            timeout=1,
            request_builder=lambda *_args: streamed_rate,
            result_consumer=lambda _result: None,
            progress_callback=rate_progress,
        )
        self.assertEqual(rate_statuses, [[429, 200]])
        self.assertEqual(streamed_rate.attempts, [])

    def test_body_read_transport_errors_are_counted_without_stopping_phase(self) -> None:
        users = [
            VirtualUser(f"read-{index:08d}", "synthetic", "s" * 64, "c" * 64)
            for index in range(5)
        ]
        calls = 0

        class Response:
            status = 200
            headers: dict[str, str] = {}

            def __init__(self, partial_failure: bool) -> None:
                self.partial_failure = partial_failure
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int) -> bytes:
                self.reads += 1
                if self.partial_failure:
                    if self.reads == 1:
                        return b"{"
                    if self.reads == 2:
                        raise http.client.IncompleteRead(b"private-body", 8)
                    raise AssertionError("failed response was read again")
                return (b"{" if self.reads == 1 else b"}" if self.reads == 2 else b"")

        def open_response(*_args: object, **_kwargs: object) -> Response:
            nonlocal calls
            calls += 1
            return Response(partial_failure=calls == 1)

        accumulator = _ResultAccumulator()

        def build_request(origin: str, user: VirtualUser, phase: str, timeout: float):
            return _request(
                origin,
                user,
                method="GET",
                path="/api/v1/users/me",
                phase=phase,
                timeout=timeout,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
                attempt_number=1,
            )

        with patch("tools.platform_external_load.urlopen", side_effect=open_response):
            run_phase(
                "https://old-sparky.com",
                users,
                phase="synthetic-transport",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                request_builder=build_request,
                result_consumer=accumulator.add,
            )

        summary = accumulator.summary()
        self.assertEqual(calls, len(users))
        self.assertEqual(summary["requests"], len(users))
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["status_counts"], {"200": len(users)})
        self.assertEqual(summary["retry_attempts"], 0)
        self.assertEqual(summary["error_kinds"], {"transport": 1})
        self.assertNotIn("private-body", repr(summary))

        class BrokenResponse:
            status = 200
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, _size: int) -> bytes:
                raise AssertionError("programming defect")

        with patch("tools.platform_external_load.urlopen", return_value=BrokenResponse()):
            with self.assertRaisesRegex(AssertionError, "programming defect"):
                _request(
                    "https://old-sparky.com",
                    users[0],
                    method="GET",
                    path="/users/me",
                    phase="synthetic-programming-error",
                    timeout=1,
                    session_cookie_name="session",
                    csrf_cookie_name="csrf",
                )

    def test_http_error_body_read_transport_failure_is_bounded(self) -> None:
        class PartialErrorBody:
            def read(self, _size: int) -> bytes:
                raise http.client.IncompleteRead(b"private-error-body", 32)

            def close(self) -> None:
                return None

        error = HTTPError(
            "https://old-sparky.com/api/v1/users/me",
            503,
            "service unavailable",
            {},
            PartialErrorBody(),
        )
        user = VirtualUser("read-error", "synthetic", "s" * 64, "c" * 64)
        with patch("tools.platform_external_load.urlopen", side_effect=error):
            result = _request(
                "https://old-sparky.com",
                user,
                method="GET",
                path="/users/me",
                phase="synthetic-http-error",
                timeout=1,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.error_kind, "transport_http_response_read")
        self.assertEqual(result.response_bytes, len(b"private-error-body"))
        self.assertIsNone(result.response_json)
        self.assertNotIn("private-error-body", repr(result))

    def test_streaming_accumulator_preserves_high_cardinality_counts_and_percentiles(self) -> None:
        accumulator = _ResultAccumulator()
        latencies = [float((index * 37) % 1000) for index in range(4096)]
        for index, elapsed_ms in enumerate(latencies):
            accumulator.add(
                RequestResult(
                    phase="synthetic",
                    method="GET",
                    path="/health",
                    status=200 if index % 17 else 503,
                    elapsed_ms=elapsed_ms,
                    ok=index % 17 != 0,
                    response_bytes=index,
                )
            )

        summary = accumulator.summary()

        self.assertEqual(summary["requests"], 4096)
        self.assertEqual(summary["errors"], 241)
        self.assertEqual(summary["status_counts"], {"200": 3855, "503": 241})
        self.assertEqual(
            summary["latency"]["p95_ms"],
            round(percentile(latencies, 95) or 0, 3),
        )
        self.assertEqual(summary["response_bytes"]["max_bytes"], 4095)

    def test_sliding_phase_bounds_live_work_and_reduces_each_completion(self) -> None:
        users = [
            VirtualUser(f"user-{index:08d}", "qa-tournament", "s" * 64, "c" * 64)
            for index in range(96)
        ]
        active = 0
        peak_active = 0
        completed = 0
        last_result: RequestResult | None = None
        lock = threading.Lock()

        def builder(
            _origin: str,
            user: VirtualUser,
            phase: str,
            _timeout: float,
        ) -> RequestResult:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.001)
            with lock:
                active -= 1
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/users/{user.user_id}",
                status=200,
                elapsed_ms=1.0,
                ok=True,
                response_bytes=1,
                response_json={"large": "x" * 4096},
            )

        def consume(result: RequestResult) -> None:
            nonlocal completed, last_result
            self.assertIsNotNone(result.response_json)
            completed += 1
            last_result = result

        class TrackingExecutor(ThreadPoolExecutor):
            last: "TrackingExecutor | None" = None

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.pending = set()
                self.peak_pending = 0
                self.submitted = 0
                type(self).last = self

            def submit(self, *args, **kwargs):
                future = super().submit(*args, **kwargs)
                self.submitted += 1
                self.pending.add(future)
                self.peak_pending = max(self.peak_pending, len(self.pending))

                def discard(completed_future) -> None:
                    self.pending.discard(completed_future)

                future.add_done_callback(discard)
                return future

        before = {
            thread.name
            for thread in threading.enumerate()
            if thread.name.startswith("external-load")
        }
        with patch(
            "tools.platform_external_load.ThreadPoolExecutor", TrackingExecutor
        ):
            returned = run_phase(
                "https://old-sparky.com",
                users,
                phase="synthetic",
                spread_seconds=0,
                concurrency=4,
                timeout=1,
                request_builder=builder,
                result_consumer=consume,
            )

        self.assertEqual(returned, [])
        self.assertEqual(completed, len(users))
        self.assertLessEqual(peak_active, 4)
        self.assertEqual(TrackingExecutor.last.submitted, len(users))
        self.assertLessEqual(TrackingExecutor.last.peak_pending, 4)
        self.assertEqual(TrackingExecutor.last.pending, set())
        self.assertIsNotNone(last_result)
        self.assertIsNone(last_result.response_json)
        self.assertEqual(active, 0)
        self.assertEqual(
            {
                thread.name
                for thread in threading.enumerate()
                if thread.name.startswith("external-load")
            },
            before,
        )

    def test_sliding_phase_cancels_pending_work_on_deadline_signal(self) -> None:
        users = [
            VirtualUser(f"user-{index:08d}", "qa-tournament", "s" * 64, "c" * 64)
            for index in range(8)
        ]
        started = threading.Event()

        class DeadlineBudget:
            def check(self, _phase: str, *, operation: str) -> None:
                if operation == "future_complete":
                    raise RuntimeError("synthetic deadline")

            def remaining_seconds(self) -> float:
                return 1.0

            def remaining_runner_seconds(self) -> float:
                return 1.0

            def sleep(self, delay: float, _phase: str, *, operation: str) -> None:
                time.sleep(delay)

        def builder(
            _origin: str,
            _user: VirtualUser,
            phase: str,
            _timeout: float,
        ) -> RequestResult:
            started.set()
            time.sleep(0.01)
            return RequestResult(
                phase=phase,
                method="GET",
                path="/health",
                status=200,
                elapsed_ms=1.0,
                ok=True,
                response_bytes=1,
            )

        with self.assertRaisesRegex(RuntimeError, "synthetic deadline"):
            run_phase(
                "https://old-sparky.com",
                users,
                phase="synthetic-deadline",
                spread_seconds=0,
                concurrency=2,
                timeout=1,
                request_builder=builder,
                budget=DeadlineBudget(),
            )
        self.assertTrue(started.is_set())
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(
            thread.name.startswith("external-load")
            for thread in threading.enumerate()
        ):
            time.sleep(0.005)
        self.assertFalse(
            any(
                thread.name.startswith("external-load")
                for thread in threading.enumerate()
            )
        )

    def test_no_consumer_phase_compatibility_keeps_exact_results_and_threads(self) -> None:
        users = [VirtualUser(f"compat-{index}", "synthetic", "s", "c") for index in range(8)]
        baseline = {thread.name for thread in threading.enumerate() if thread.name.startswith("external-load")}
        for name, runner, option in (("run_phase", run_phase, {"spread_seconds": 0}), ("run_rate_phase", run_rate_phase, {"duration_seconds": 0})):
            returned = runner("http://synthetic.invalid", users, phase=name, concurrency=2, timeout=1, request_builder=lambda _origin, user, phase, _timeout: RequestResult(phase=phase, method="GET", path="/synthetic/compat", status=200, elapsed_ms=1, ok=True, response_bytes=1, response_json=user.user_id), **option)
            self.assertEqual(sorted(result.response_json for result in (returned[0] if name == "run_rate_phase" else returned)), [f"compat-{index}" for index in range(8)])
            self.assertEqual({thread.name for thread in threading.enumerate() if thread.name.startswith("external-load")}, baseline)

    def test_rss_supervisor_reaps_hostile_child_with_short_deadline(self) -> None:
        with patch.object(os, "killpg", wraps=os.killpg) as killpg, self.assertRaisesRegex(AssertionError, "RSS probe failed"):
            _run_rss_probe("hostile")
        self.assertEqual([call.args[1] for call in killpg.call_args_list[:2]], [signal.SIGTERM, signal.SIGKILL])

    def test_real_rss_probe_bounds_live_phases_and_accumulators(self) -> None:
        parent_fd_count = len(os.listdir("/proc/self/fd"))
        live = _run_rss_probe("live")
        accumulator = _run_rss_probe("accumulator")
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), parent_fd_count)

        self.assertEqual(live["requests"], 4096)
        self.assertEqual(live["concurrency"], 512)
        self.assertEqual(live["payload_bytes"], 64 * 1024)
        self.assertEqual(live["baseline_external_thread_ids"], [])
        self.assertEqual(live["after_external_thread_ids"], [])
        self.assertEqual(live["baseline_thread_ids"], live["after_thread_ids"])
        self.assertEqual(live["direct_children_before"], [])
        self.assertEqual(live["direct_children_after"], [])
        for phase in live["runs"]:
            self.assertEqual(phase["submitted"], 4096)
            self.assertEqual(phase["completed"], 4096)
            self.assertLessEqual(phase["peak_pending"], 512)
            self.assertLessEqual(phase["peak_live_payloads"], 512)
            self.assertEqual(phase["pending_after"], 0)
            self.assertEqual(phase["live_payloads_after"], 0)
            self.assertTrue(phase["ready"])
            self.assertEqual(phase["returned_results"], 0)

        self.assertEqual(accumulator["accumulators"], 4)
        self.assertEqual(accumulator["requests_per_accumulator"], 16384)
        self.assertEqual(accumulator["completed"], 4 * 16384)
        self.assertEqual(accumulator["unique_cf_rays"], 4 * 16384)
        self.assertTrue(accumulator["timing_complete"])
        self.assertEqual(accumulator["after_external_thread_ids"], [])
        self.assertEqual(accumulator["baseline_thread_ids"], accumulator["after_thread_ids"])
        self.assertEqual(accumulator["direct_children_before"], [])
        self.assertEqual(accumulator["direct_children_after"], [])
        fields = ("dns_ms", "tcp_connect_ms", "tls_handshake_ms", "request_write_ms", "edge_wait_ms", "ttfb_ms", "body_receive_ms", "total_ms")
        self.assertEqual(accumulator["transport_field_counts"], {field: 4 * 16384 for field in fields})
        for report, limit in ((live, LIVE_HWM_LIMIT_BYTES), (accumulator, ACCUMULATOR_HWM_LIMIT_BYTES)):
            before, after = report["baseline"], report["after"]
            for sample in (before, after):
                self.assertGreaterEqual(sample["VmHWM"], sample["VmRSS"])
            self.assertEqual(report["rss_delta_bytes"], after["VmRSS"] - before["VmRSS"])
            self.assertEqual(report["hwm_delta_bytes"], max(after[key] - before[key] for key in ("VmHWM", "ru_maxrss")))
            self.assertLess(report["hwm_delta_bytes"], limit)
            self.assertLessEqual(report["after_fd_count"], report["baseline_fd_count"])

    def test_request_projects_only_route_correctness_fields(self) -> None:
        class FakeResponse:
            headers = {"etag": '"etag-1"'}

            def __init__(self, body: bytes, status: int = 200) -> None:
                self.body = body
                self.status = status

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self, size: int) -> bytes:
                chunk, self.body = self.body[:size], self.body[size:]
                return chunk

        user = VirtualUser("user-00000001", "qa-tournament", "s" * 64, "c" * 64)
        def request_with(
            body: bytes,
            *,
            method: str,
            path: str,
            status: int = 200,
        ) -> RequestResult:
            with patch(
                "tools.platform_external_load.urlopen",
                return_value=FakeResponse(body, status),
            ):
                return _request(
                    "https://old-sparky.com",
                    user,
                    method=method,
                    path=path,
                    phase="synthetic",
                    timeout=1.0,
                    session_cookie_name="session",
                    csrf_cookie_name="csrf",
                )

        read = request_with(
            json.dumps({"secret": "must-not-survive", "changed": True}).encode(),
            method="GET",
            path="/tournaments/qa-tournament/workspace",
        )
        self.assertIsNone(read.response_json)
        self.assertEqual(read.response_etag, '"etag-1"')
        read_overload = request_with(
            json.dumps(
                {
                    "code": "AUTHENTICATED_READ_OVERLOADED",
                    "detail": "must-not-survive",
                }
            ).encode(),
            method="GET",
            path="/tournaments/qa-tournament/workspace",
            status=503,
        )
        self.assertEqual(
            read_overload.response_json,
            {"code": "AUTHENTICATED_READ_OVERLOADED"},
        )
        unrelated = request_with(
            json.dumps(
                {"code": "OTHER_503", "detail": "must-not-survive"}
            ).encode(),
            method="GET",
            path="/tournaments/qa-tournament/workspace",
            status=503,
        )
        self.assertIsNone(unrelated.response_json)
        overload_summary = summarize_results([read_overload])
        self.assertEqual(overload_summary["temporary_overload_responses"], 1)
        self.assertEqual(overload_summary["unexpected_statuses"], 0)
        unrelated_summary = summarize_results([unrelated])
        self.assertEqual(unrelated_summary["temporary_overload_responses"], 0)
        self.assertEqual(unrelated_summary["unexpected_statuses"], 1)
        vote = request_with(
            json.dumps(
                {
                    "code": "READY_VOTE_OVERLOADED",
                    "retryable": True,
                    "retry_after_ms": 250,
                    "changed": True,
                    "secret": "must-not-survive",
                }
            ).encode(),
            method="POST",
            path="/tournaments/qa-tournament/deadlock/ready-check/vote",
        )
        self.assertEqual(
            vote.response_json,
            {
                "code": "READY_VOTE_OVERLOADED",
                "retryable": True,
                "retry_after_ms": 250,
                "changed": True,
            },
        )
        self.assertNotIn("secret", json.dumps(vote.response_json))
        huge_retry = request_with(
            json.dumps(
                {
                    "code": "READY_VOTE_OVERLOADED",
                    "retry_after_ms": 10**4000,
                }
            ).encode(),
            method="POST",
            path="/tournaments/qa-tournament/deadlock/ready-check/vote",
        )
        self.assertEqual(
            huge_retry.response_json,
            {"code": "READY_VOTE_OVERLOADED"},
        )

    def test_read_mix_uses_the_current_tournament_page_request(self) -> None:
        route = _route_for_read(0, "qa-tournament")

        self.assertEqual(
            route,
            "/tournaments/qa-tournament/workspace?participants_limit=0"
            "&participants_offset=0&workspace_view=detail&include_current_user=false",
        )

    def test_spread_offsets_are_bounded_and_deterministic(self) -> None:
        self.assertEqual(spread_offsets(5, 10), [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(spread_offsets(2, 0), [0.0, 0.0])
        self.assertEqual(spread_offsets(0, 10), [])

    def test_manifest_validates_identity_and_tournament_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_payload()), encoding="utf-8")
            payload, users = load_manifest(path)

        self.assertEqual(payload["marker"], "preprod26082900000000ab")
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0].tournament_slug, "qa-tournament")

    def test_manifest_accepts_production_host_cookie_names(self) -> None:
        payload = manifest_payload()
        payload["session_cookie_name"] = "__Host-deadlock_platform_session"
        payload["csrf_cookie_name"] = "__Host-deadlock_platform_session_csrf"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            load_manifest(path)

    def test_manifest_rejects_duplicate_session_material(self) -> None:
        payload = manifest_payload()
        users = payload["users"]
        assert isinstance(users, list)
        users[1] = dict(users[1])
        users[1]["session_token"] = users[0]["session_token"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ExternalLoadError):
                load_manifest(path)

    def test_summary_contains_metrics_but_not_session_material(self) -> None:
        secret = "session-secret-that-must-not-be-serialized"
        result = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=200,
            elapsed_ms=100.0,
            ok=True,
            response_bytes=120,
            response_json={"changed": True, "internal": secret},
        )
        summary = summarize_results([result])
        serialized = json.dumps(summary)
        self.assertEqual(summary["scope"], "full_population")
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertNotIn(secret, serialized)

    def test_timing_summary_separates_service_queue_and_user_observed_latency(self) -> None:
        result = RequestResult(
            phase="diagnostic",
            method="GET",
            path="/health",
            status=200,
            elapsed_ms=10.0,
            ok=True,
            response_bytes=1,
            started_at_monotonic=12.0,
            finished_at_monotonic=12.01,
            user_observed_elapsed_ms=30.0,
        )
        _annotate_timing(result, scheduled_at=10.0, enqueued_at=11.0)

        summary = summarize_results([result])

        self.assertEqual(summary["latency"]["p95_ms"], 10.0)
        self.assertEqual(summary["timing"]["service_latency"]["p95_ms"], 10.0)
        self.assertEqual(summary["timing"]["user_observed_latency"]["p95_ms"], 2010.0)
        self.assertEqual(summary["timing"]["executor_queue_wait"]["p95_ms"], 1000.0)
        self.assertEqual(summary["timing"]["late_start"]["p95_ms"], 2000.0)
        self.assertEqual(summary["timing"]["late_start_count"], 1)
        self.assertEqual(summary["timing"]["missing_schedule_context"], 0)

    def test_inline_retry_has_no_executor_queue_or_schedule_lateness(self) -> None:
        first = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/ready-check/vote",
            status=503,
            elapsed_ms=1_000.0,
            ok=False,
            response_bytes=1,
            attempt_number=1,
            started_at_monotonic=12.0,
            finished_at_monotonic=13.0,
        )
        retry = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/ready-check/vote",
            status=200,
            elapsed_ms=1_000.0,
            ok=True,
            response_bytes=1,
            attempt_number=2,
            started_at_monotonic=14.0,
            finished_at_monotonic=15.0,
        )
        logical = LogicalRequestResult(
            attempts=[first, retry],
            elapsed_ms=3_000.0,
            started_at_monotonic=12.0,
            finished_at_monotonic=15.0,
        )

        _annotate_timing(logical, scheduled_at=10.0, enqueued_at=11.0)

        self.assertEqual(first.executor_queue_wait_ms, 1_000.0)
        self.assertEqual(retry.executor_queue_wait_ms, 0.0)
        self.assertIsNone(retry.schedule_delay_ms)
        self.assertIsNone(retry.late_start_ms)
        self.assertEqual(logical.executor_queue_wait_ms, 1_000.0)
        self.assertEqual(logical.schedule_delay_ms, 2_000.0)
        self.assertEqual(logical.late_start_ms, 2_000.0)
        self.assertEqual(logical.user_observed_elapsed_ms, 5_000.0)

        raw_summary = summarize_results([first, retry])
        self.assertEqual(raw_summary["timing"]["executor_queue_wait"]["p95_ms"], 950.0)
        self.assertEqual(raw_summary["timing"]["schedule_delay"]["p95_ms"], 2_000.0)
        self.assertEqual(raw_summary["timing"]["late_start_count"], 1)
        self.assertFalse(raw_summary["timing"]["partial"])
        logical_summary = summarize_logical_results([logical])
        self.assertEqual(logical_summary["timing"]["user_observed_latency"]["p95_ms"], 5_000.0)

    def test_timing_summary_marks_uninstrumented_builder_context(self) -> None:
        result = RequestResult(
            phase="diagnostic",
            method="GET",
            path="/health",
            status=200,
            elapsed_ms=10.0,
            ok=True,
            response_bytes=1,
        )

        summary = summarize_results([result])

        self.assertEqual(summary["timing"]["missing_schedule_context"], 1)
        self.assertEqual(summary["timing"]["dropped_work"], 1)
        self.assertTrue(summary["timing"]["partial"])

    def test_summary_reports_explicit_client_transport_and_phase_timings(self) -> None:
        result = RequestResult(
            phase="authenticated_page_load",
            method="GET",
            path="/tournaments/qa-tournament",
            status=200,
            elapsed_ms=40.0,
            ok=True,
            response_bytes=120,
            time_to_first_byte_ms=18.0,
            transport_timing={
                "transport": "http1-keepalive",
                "http_version": "1.1",
                "connection_reused": True,
                "dns_ms": 1.0,
                "tcp_connect_ms": 2.0,
                "tls_handshake_ms": 3.0,
                "request_write_ms": 4.0,
                "edge_wait_ms": 5.0,
                "ttfb_ms": 18.0,
                "body_receive_ms": 22.0,
                "total_ms": 40.0,
            },
        )

        summary = summarize_results([result])

        self.assertEqual(summary["transport"]["names"], {"http1-keepalive": 1})
        self.assertEqual(summary["transport"]["http_versions"], {"1.1": 1})
        self.assertEqual(summary["transport"]["connection_reused"], 1)
        self.assertEqual(
            summary["transport"]["phase_timings"]["ttfb_ms"]["p95_ms"],
            18.0,
        )

    def test_summary_keeps_only_bounded_cloudflare_failure_diagnostics(self) -> None:
        result = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=522,
            elapsed_ms=30_000.0,
            ok=False,
            response_bytes=0,
            time_to_first_byte_ms=None,
            cf_ray="ray-522",
            cf_error_type="522",
            cf_error_origin="connection_failure",
            retry_after="60",
            error_kind="unexpected_status",
        )

        summary = summarize_results([result])

        self.assertEqual(summary["cf_error_type_counts"], {"cf_522": 1})
        self.assertEqual(summary["cf_error_origin_counts"], {"origin": 1})
        self.assertEqual(
            summary["error_samples"][0],
            {
                "phase": "write_burst",
                "method": "POST",
                "route_class": "ready_vote",
                "status": 522,
                "error_class": "server_error",
                "cf_error_class": "cf_522",
                "cf_error_origin_class": "origin",
                "ttfb_ms": None,
                "elapsed_ms": 30_000.0,
            },
        )

    def test_request_extracts_only_allowlisted_failure_headers(self) -> None:
        class FakeContext:
            verify_mode = ssl.CERT_REQUIRED
            check_hostname = True
            post_handshake_auth = False

            def __init__(self) -> None:
                self.alpn_protocols: list[str] = []

            def set_alpn_protocols(self, protocols: list[str]) -> None:
                self.alpn_protocols = protocols

        class FakeResponse:
            status = 522
            headers = {
                "cf-ray": "ray-522",
                "cf-error-type": "type-" + ("x" * 200),
                "cf-error-origin": "origin",
                "retry-after": "60",
                "x-private-debug": "must-not-be-captured",
            }

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            @staticmethod
            def read(_size: int) -> bytes:
                return b""

        user = VirtualUser("user-00000001", "qa-tournament", "s" * 64, "c" * 64)
        original_context = external_load._DEFAULT_HTTPS_CONTEXT
        fake_context = FakeContext()
        with external_load._DEFAULT_HTTPS_CONTEXT_LOCK:
            external_load._DEFAULT_HTTPS_CONTEXT = None
        try:
            with patch(
                "tools.platform_external_load.ssl.create_default_context",
                return_value=fake_context,
            ) as factory:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    contexts = list(
                        executor.map(
                            lambda _index: external_load._default_https_context(),
                            range(64),
                        )
                    )
                with patch(
                    "tools.platform_external_load.urlopen",
                    return_value=FakeResponse(),
                ) as opener:
                    result = _request(
                        "https://old-sparky.com",
                        user,
                        method="GET",
                        path="/tournaments/qa-tournament",
                        phase="diagnostic",
                        timeout=1.0,
                        session_cookie_name="session",
                        csrf_cookie_name="csrf",
                    )
                    _request(
                        "https://old-sparky.com",
                        user,
                        method="GET",
                        path="/tournaments/qa-tournament",
                        phase="diagnostic",
                        timeout=1.0,
                        session_cookie_name="session",
                        csrf_cookie_name="csrf",
                    )
            self.assertEqual(factory.call_count, 1)
            factory.assert_called_once_with()
            self.assertTrue(all(context is fake_context for context in contexts))
            self.assertEqual(fake_context.alpn_protocols, ["http/1.1"])
            self.assertTrue(fake_context.post_handshake_auth)
            self.assertTrue(fake_context.check_hostname)
            self.assertEqual(fake_context.verify_mode, ssl.CERT_REQUIRED)
            passed_contexts = [call.kwargs["context"] for call in opener.call_args_list]
            self.assertEqual(len(passed_contexts), 2)
            self.assertTrue(all(context is fake_context for context in passed_contexts))
        finally:
            with external_load._DEFAULT_HTTPS_CONTEXT_LOCK:
                external_load._DEFAULT_HTTPS_CONTEXT = original_context

        actual_context = external_load._default_https_context()
        self.assertIsInstance(actual_context, ssl.SSLContext)
        self.assertEqual(actual_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(actual_context.check_hostname)
        if actual_context.post_handshake_auth is not None:
            self.assertTrue(actual_context.post_handshake_auth)

        self.assertEqual(result.status, 522)
        self.assertEqual(result.cf_ray, "ray-522")
        self.assertEqual(result.cf_error_origin, "origin")
        self.assertEqual(result.retry_after, "60")
        self.assertEqual(len(result.cf_error_type or ""), 128)

    def test_diagnostic_request_carries_id_and_monotonic_timestamps_on_timeout(self) -> None:
        user = VirtualUser("user-00000001", "qa-tournament", "s" * 64, "c" * 64)
        captured: list[object] = []

        def fail(
            request: object,
            *,
            timeout: float,
            context: ssl.SSLContext,
        ) -> object:
            captured.append(request)
            raise TimeoutError("timed out")

        with patch("tools.platform_external_load.urlopen", side_effect=fail):
            result = _request(
                "https://old-sparky.com",
                user,
                method="GET",
                path="/tournaments/qa-tournament",
                phase="diagnostic",
                timeout=1.0,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
                diagnostic_id="tdiag-123-00001",
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].get_header("X-platform-timeout-diagnostic-id"),
            "tdiag-123-00001",
        )
        self.assertEqual(result.error_kind, "TimeoutError")
        self.assertEqual(result.status, 0)
        self.assertIsNotNone(result.started_at_utc)
        self.assertIsNotNone(result.exception_at_utc)
        self.assertIsNotNone(result.finished_at_utc)

    def test_diagnostic_summary_caps_rows_and_reports_totals(self) -> None:
        results = [
            RequestResult(
                phase="diagnostic",
                method="GET",
                path=f"/tournaments/qa-{index}",
                status=0,
                elapsed_ms=30_000.0,
                ok=False,
                response_bytes=0,
                error_kind="TimeoutError",
                diagnostic_id=f"tdiag-123-{index:05d}",
            )
            for index in range(26)
        ]

        summary = summarize_results(results)

        self.assertEqual(len(summary["error_samples"]), 25)
        self.assertEqual(len(summary["timeout_diagnostics"]), 25)
        self.assertEqual(summary["timeout_diagnostic_total"], 26)
        self.assertEqual(summary["timeout_diagnostic_truncated"], 1)
        self.assertEqual(
            summary["timeout_diagnostics"][0]["diagnostic_id"],
            "tdiag-123-00000",
        )

    def test_ready_vote_retries_only_explicit_overload_and_reports_logical_latency(self) -> None:
        _, users = load_manifest_from_payload(manifest_payload())
        responses = [
            RequestResult(
                phase="write_external_vote",
                method="POST",
                path="/tournaments/qa/deadlock/ready-check/vote",
                status=503,
                elapsed_ms=12.0,
                ok=False,
                response_bytes=88,
                response_json={
                    "code": "READY_VOTE_OVERLOADED",
                    "retryable": True,
                    "retry_after_ms": 250,
                },
            ),
            RequestResult(
                phase="write_external_vote",
                method="POST",
                path="/tournaments/qa/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=18.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            ),
        ]

        with (
            patch("tools.platform_external_load._ready_vote_request", side_effect=responses),
            patch("tools.platform_external_load.time.sleep") as sleep,
            patch("tools.platform_external_load.random.uniform", return_value=200.0),
        ):
            logical = _ready_vote_action(
                "https://old-sparky.com",
                users[0],
                "write_external_vote",
                1.0,
                session_cookie_name="session",
                csrf_cookie_name="csrf",
            )

        self.assertEqual(len(logical.attempts), 2)
        self.assertEqual(logical.retry_count, 1)
        sleep.assert_called_once_with(0.25)
        summary = summarize_logical_results([logical])
        self.assertEqual(summary["actions"], 1)
        self.assertEqual(summary["final_successes"], 1)
        self.assertEqual(summary["total_retries"], 1)
        self.assertEqual(summary["changed_counts"], {"True": 1})

    def test_logical_summary_keeps_final_failure_rate_separate_from_raw_overloads(self) -> None:
        overloaded = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=503,
            elapsed_ms=4.0,
            ok=False,
            response_bytes=80,
            response_json={
                "code": "READY_VOTE_OVERLOADED",
                "retryable": True,
                "retry_after_ms": 250,
            },
        )
        logical = LogicalRequestResult([overloaded], elapsed_ms=4.0)

        raw = summarize_results([overloaded])
        logical_summary = summarize_logical_results([logical])
        self.assertEqual(raw["temporary_overload_responses"], 1)
        self.assertEqual(logical_summary["final_failures"], 1)
        self.assertEqual(logical_summary["final_failure_rate_percent"], 100.0)

    def test_ready_vote_load_report_separates_primary_http_and_logical_layers(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=12.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            )
            return LogicalRequestResult([result], elapsed_ms=12.0)

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                authoritative_binding={
                    "profile_id": "ready-vote-slo-v2",
                    "profile_version": 2,
                    "profile_digest": "a" * 64,
                    "source_git_sha": "a" * 40,
                    "external_run_id": "123",
                },
            )

        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(
            report["acceptance"]["decision"],
            "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        )
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["raw_http"]["requests"], 2)
        self.assertEqual(report["logical"]["actions"], 2)
        self.assertEqual(report["logical"]["final_failures"], 0)
        self.assertGreater(report["logical"]["successful_goodput_actions_per_second"], 0)

    def test_stress_allows_shed_duplicates_but_requires_successful_noops(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            duplicate = phase.endswith("duplicate")
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=503 if duplicate else 200,
                elapsed_ms=12.0,
                ok=not duplicate,
                response_bytes=120,
                response_json=(
                    {"code": "READY_VOTE_OVERLOADED", "retryable": True}
                    if duplicate
                    else {"changed": True}
                ),
                error_kind="http_error" if duplicate else None,
            )
            return LogicalRequestResult(
                [result], elapsed_ms=12.0, user_id=user.user_id
            )

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=1,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                scenario_kind="stress",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 1500,
                        "p90_ms": 3000,
                        "p95_ms": 5000,
                        "p99_ms": 8000,
                    },
                    "max_shed_percent": 99.9,
                    "max_retry_amplification_percent": 200,
                },
            )

        self.assertTrue(report["acceptance"]["contract_ok"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(report["phases"]["duplicate"]["logical"]["final_failures"], 1)

    def test_stress_marks_duplicate_phase_incomplete_when_primary_successes_are_short(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> LogicalRequestResult:
            duplicate = phase.endswith("duplicate")
            primary_shed = not duplicate and user.user_id == "user-00000002"
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=503 if primary_shed else 200,
                elapsed_ms=12.0,
                ok=not primary_shed,
                response_bytes=120,
                response_json=(
                    {"code": "READY_VOTE_OVERLOADED", "retryable": True}
                    if primary_shed
                    else {"changed": not duplicate}
                ),
                error_kind="http_error" if primary_shed else None,
            )
            return LogicalRequestResult(
                [result], elapsed_ms=12.0, user_id=user.user_id
            )

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 1}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=2,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                scenario_kind="stress",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 1500,
                        "p90_ms": 3000,
                        "p95_ms": 5000,
                        "p99_ms": 8000,
                    },
                    "max_shed_percent": 99.9,
                    "max_retry_amplification_percent": 200,
                    "minimum_useful_goodput_actions_per_second": 0.1,
                },
            )

        duplicate = report["phases"]["duplicate"]
        self.assertEqual(duplicate["configured_actions"], 2)
        self.assertEqual(duplicate["candidate_actions"], 1)
        self.assertEqual(duplicate["submitted_actions"], 1)
        self.assertEqual(duplicate["missing_actions"], 1)
        self.assertFalse(duplicate["complete"])
        self.assertTrue(duplicate["logical"]["timing"]["partial"])
        self.assertEqual(report["logical"]["configured_duplicate_actions"], 2)
        self.assertEqual(report["logical"]["actions"], 3)
        self.assertEqual(report["raw_http"]["requests"], 3)
        self.assertFalse(report["acceptance"]["contract_ok"])
        self.assertFalse(report["acceptance"]["checks"]["timing_complete"])

    def test_read_mix_manual_refresh_uses_conditional_workspace_request(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        calls: list[dict[str, object]] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            calls.append(
                {
                    "phase": phase,
                    "path": path,
                    "expected_statuses": expected_statuses,
                    "extra_headers": extra_headers,
                }
            )
            is_refresh = extra_headers is not None
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=304 if is_refresh else 200,
                elapsed_ms=10.0,
                ok=(304 if is_refresh else 200) in expected_statuses,
                response_bytes=0,
                response_etag='"workspace-etag"',
            )

        with patch("tools.platform_external_load._trace", side_effect=fake_trace):
            with patch("tools.platform_external_load._request", side_effect=fake_request):
                report = run_load(
                    payload,
                    users,
                    mode="read-mix",
                    spread_seconds=0,
                    concurrency=1,
                    timeout=1,
                    duplicate_count=0,
                    manual_refresh_count=1,
                    p95_budget_ms=1000,
                    p99_budget_ms=2000,
                    acceptance_contract={
                        "kind": "slo",
                        "accepted_request_latency": {
                            "p50_ms": 100,
                            "p90_ms": 100,
                            "p95_ms": 100,
                            "p99_ms": 100,
                        },
                        "logical_latency": {"p95_ms": 100, "p99_ms": 100},
                        "logical_final_failure_percent": 0,
                        "max_shed_percent": 0,
                        "max_retry_amplification_percent": 0,
                    },
                )

        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(
            report["acceptance"]["decision"],
            "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        )
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["scope"], "full_population")
        self.assertFalse(report["acceptance"]["checks"]["authoritative_binding"])
        self.assertEqual(report["phases"]["read_mix"]["requests"], 2)
        self.assertEqual(report["phases"]["manual_refresh"]["requests"], 1)
        self.assertEqual(report["phases"]["manual_refresh"]["status_counts"], {"304": 1})
        refresh_call = next(call for call in calls if call["phase"] == "manual_workspace_refresh")
        self.assertEqual(refresh_call["expected_statuses"], frozenset({200, 304}))
        self.assertEqual(refresh_call["extra_headers"], {"If-None-Match": '"workspace-etag"'})

    def test_page_load_reports_html_ttfb_and_total_latency(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_page_request(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/tournaments/{user.tournament_slug}",
                status=200,
                elapsed_ms=40.0,
                ok=True,
                response_bytes=12_000,
                time_to_first_byte_ms=18.0,
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._page_request", side_effect=fake_page_request),
        ):
            report = run_load(
                payload,
                users,
                mode="page-load",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 100,
                        "p90_ms": 100,
                        "p95_ms": 100,
                        "p99_ms": 100,
                    },
                    "logical_latency": {"p95_ms": 100, "p99_ms": 100},
                    "max_shed_percent": 0,
                    "max_retry_amplification_percent": 0,
                },
            )

        self.assertTrue(report["acceptance"]["contract_ok"])
        self.assertEqual(report["phases"]["authenticated_page_load"]["requests"], 2)
        self.assertEqual(
            report["overall"]["time_to_first_byte"]["p95_ms"],
            18.0,
        )
        self.assertEqual(report["overall"]["response_bytes"]["max_bytes"], 12_000)

    def test_page_load_passes_explicit_transport_and_retains_phase_evidence(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        transports: list[str] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_page_request(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            transport: str = "urllib-http1-close",
        ) -> RequestResult:
            transports.append(transport)
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/tournaments/{user.tournament_slug}",
                status=200,
                elapsed_ms=40.0,
                ok=True,
                response_bytes=12_000,
                time_to_first_byte_ms=18.0,
                transport_timing={
                    "transport": transport,
                    "http_version": "1.1",
                    "connection_reused": len(transports) > 1,
                    "dns_ms": 1.0,
                    "tcp_connect_ms": 2.0,
                    "tls_handshake_ms": 3.0,
                    "request_write_ms": 4.0,
                    "edge_wait_ms": 5.0,
                    "ttfb_ms": 18.0,
                    "body_receive_ms": 22.0,
                    "total_ms": 40.0,
                },
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._page_request", side_effect=fake_page_request),
        ):
            report = run_load(
                payload,
                users,
                mode="page-load",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                client_transport="http1-keepalive",
                acceptance_contract={
                    "kind": "stress",
                    "accepted_request_latency": {
                        "p50_ms": 100,
                        "p90_ms": 100,
                        "p95_ms": 100,
                        "p99_ms": 100,
                    },
                    "logical_latency": {"p95_ms": 100, "p99_ms": 100},
                    "max_shed_percent": 0,
                    "max_retry_amplification_percent": 0,
                },
            )

        self.assertEqual(transports, ["http1-keepalive", "http1-keepalive"])
        self.assertEqual(report["client_transport"], "http1-keepalive")
        self.assertEqual(
            report["overall"]["transport"]["phase_timings"]["ttfb_ms"]["p95_ms"],
            18.0,
        )

    def test_page_load_diagnostic_mode_assigns_stable_per_user_ids(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        diagnostic_ids: list[str | None] = []

        def fake_page_request(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            diagnostic_id: str | None = None,
        ) -> RequestResult:
            diagnostic_ids.append(diagnostic_id)
            return RequestResult(
                phase=phase,
                method="GET",
                path=f"/tournaments/{user.tournament_slug}",
                status=200,
                elapsed_ms=40.0,
                ok=True,
                response_bytes=12_000,
                time_to_first_byte_ms=18.0,
                diagnostic_id=diagnostic_id,
            )

        with patch("tools.platform_external_load._page_request", side_effect=fake_page_request):
            report = run_load(
                payload,
                users,
                mode="page-load",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                timeout_diagnostics_run_id="123",
            )

        self.assertEqual(
            diagnostic_ids,
            ["tdiag-123-00000", "tdiag-123-00001"],
        )
        self.assertEqual(
            report["timeout_path_diagnostics"],
            {"enabled": True, "request_count": 2},
        )

    def test_concurrency_ramp_marks_latency_knee_as_operator_candidate(self) -> None:
        def stage(p95: float, rps: float) -> dict[str, object]:
            return {
                "latency": {"p95_ms": p95, "p99_ms": p95 * 1.2},
                "requests_per_second": rps,
            }

        analysis = analyze_concurrency_ramp(
            {"16": stage(500, 100), "32": stage(700, 105), "48": stage(1400, 106)}
        )

        self.assertEqual(analysis["knee"]["stable_concurrency"], 32)
        self.assertEqual(analysis["recommended_max_concurrency"], 32)
        self.assertTrue(analysis["rollout_required"])

    def test_read_mix_concurrency_ramp_reports_each_full_population_stage(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)
        calls: list[str] = []

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            calls.append(phase)
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=150,
                response_etag='"workspace-etag"' if user.user_id == "user-00000001" else None,
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._request", side_effect=fake_request),
        ):
            report = run_load(
                payload,
                users,
                mode="read-mix",
                spread_seconds=0,
                concurrency=2,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=1000,
                p99_budget_ms=2000,
                concurrency_stages=[1, 2],
            )

        self.assertFalse(report["acceptance"]["passed"])
        self.assertEqual(
            report["acceptance"]["decision"],
            "LEGACY DIAGNOSTIC NON-AUTHORITATIVE",
        )
        self.assertFalse(report["authoritative"])
        self.assertFalse(report["dispatchable"])
        self.assertEqual(report["phases"]["read_mix"]["requests"], 4)
        self.assertEqual(
            report["phases"]["capacity_ramp"]["concurrency_stages"],
            [1, 2],
        )
        self.assertEqual(
            [report["phases"]["capacity_ramp"]["stages"][str(stage)]["requests"] for stage in (1, 2)],
            [2, 2],
        )
        self.assertTrue(all(phase.startswith("scale_external_read_mix_c") for phase in calls))
        self.assertEqual(report["phase_windows_status"], "complete")
        self.assertEqual(
            [window["phase"] for window in report["phase_windows"]],
            ["read_mix_c1", "read_mix_c2"],
        )
        run_started = report["started_at"]
        run_finished = report["finished_at"]
        previous_finish = run_started
        for window in report["phase_windows"]:
            self.assertGreaterEqual(window["started_at"], run_started)
            self.assertLessEqual(window["finished_at"], run_finished)
            self.assertGreaterEqual(window["started_at"], previous_finish)
            self.assertLess(window["started_at"], window["finished_at"])
            previous_finish = window["finished_at"]

        public_report = project_public_artifact("external_load", report)
        self.assertEqual(public_report["run_window_status"], "complete")
        self.assertEqual(public_report["phase_windows_status"], "complete")
        self.assertEqual(
            [window["phase"] for window in public_report["phase_windows"]],
            ["read_mix_c1", "read_mix_c2"],
        )

    def test_ready_vote_rate_plan_reports_offered_rate_per_phase(self) -> None:
        payload = manifest_payload()
        _, users = load_manifest_from_payload(payload)

        def fake_trace(origin: str, timeout: float) -> dict[str, str]:
            return {"status": "200", "ip": "192.0.2.10", "colo": "TEST"}

        def fake_action(
            origin: str,
            user: VirtualUser,
            phase: str,
            timeout: float,
            *,
            session_cookie_name: str,
            csrf_cookie_name: str,
            retry_policy: dict[str, object] | None = None,
        ) -> LogicalRequestResult:
            result = RequestResult(
                phase=phase,
                method="POST",
                path=f"/tournaments/{user.tournament_slug}/deadlock/ready-check/vote",
                status=200,
                elapsed_ms=10.0,
                ok=True,
                response_bytes=120,
                response_json={"changed": True},
            )
            return LogicalRequestResult([result], elapsed_ms=10.0, user_id=user.user_id)

        def fake_state_request(
            origin: str,
            user: VirtualUser,
            *,
            method: str,
            path: str,
            phase: str,
            timeout: float,
            session_cookie_name: str,
            csrf_cookie_name: str,
            json_payload: dict[str, object] | None = None,
            expected_statuses: frozenset[int] = frozenset({200}),
            extra_headers: dict[str, str] | None = None,
        ) -> RequestResult:
            return RequestResult(
                phase=phase,
                method=method,
                path=path,
                status=200,
                elapsed_ms=8.0,
                ok=True,
                response_bytes=150,
                response_json={"active_round": {"ready_count": 2}},
            )

        with (
            patch("tools.platform_external_load._trace", side_effect=fake_trace),
            patch("tools.platform_external_load._ready_vote_action", side_effect=fake_action),
            patch("tools.platform_external_load._request", side_effect=fake_state_request),
        ):
            report = run_load(
                payload,
                users,
                mode="ready-vote",
                spread_seconds=0,
                concurrency=1,
                timeout=1,
                duplicate_count=0,
                manual_refresh_count=0,
                p95_budget_ms=600,
                p99_budget_ms=1000,
                phase_plan=[
                    {"name": "rate-1", "target_logical_actions_per_second": 1, "duration_seconds": 1, "logical_actions": 1},
                    {"name": "rate-1b", "target_logical_actions_per_second": 1, "duration_seconds": 1, "logical_actions": 1},
                ],
                scenario_kind="capacity",
                acceptance_contract={
                    "kind": "capacity",
                    "slo": {
                        "accepted_request_latency": {"p50_ms": 250, "p90_ms": 400, "p95_ms": 600, "p99_ms": 1000},
                        "logical_latency": {"p95_ms": 600, "p99_ms": 1000},
                        "logical_final_failure_percent": 0.5,
                        "max_shed_percent": 0,
                        "max_retry_amplification_percent": 0,
                    },
                    "capacity": {"target_logical_actions_per_second": [1, 1], "steady_duration_seconds": 1},
                },
            )

        phases = report["phases"]["ramp"]["phases"]
        self.assertEqual(set(phases), {"rate-1", "rate-1b"})
        self.assertEqual(phases["rate-1"]["logical"]["target_logical_actions_per_second"], 1)
        self.assertGreater(phases["rate-1"]["raw_http"]["attempts_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
