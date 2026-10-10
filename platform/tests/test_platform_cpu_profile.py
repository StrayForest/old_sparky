from __future__ import annotations

import os
import json
import io
import tempfile
from concurrent.futures import ThreadPoolExecutor
from argparse import Namespace
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch

from python_packages.platform_infra.cpu_profile import ReadyVoteCpuProfiler
from tools import platform_cpu_diagnostic_pair as cpu_pair
from tools import platform_external_load as external_load
from python_packages.platform_infra.performance_diagnostic import (
    ApiCpuDiagnostic,
    parse_plan_payload,
)
from tools import platform_cpu_diagnostic_plan as diagnostic_plan_tool
from tools.platform_cpu_diagnostic_plan import (
    PlanError,
    _read_request,
    _validate_windows,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


def _diagnostic_payload(*, service: str = "api") -> dict[str, object]:
    return {
        "schema": 1,
        "run_id": "a" * 32,
        "source_sha": "b" * 40,
        "release_slug": "release-bbbbbbbbbbbb",
        "workload": "authenticated_workspace_read_pair_v1",
        "off_start_ms": 1_000,
        "off_end_ms": 21_000,
        "on_start_ms": 26_000,
        "on_end_ms": 46_000,
        "expires_at_ms": 46_000,
        "targets": [{
            "service": service,
            "pid": os.getpid(),
            "start_ticks": 123,
            "invocation_id": "c" * 32,
        }],
    }


def _canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"


class ReadyVoteCpuProfilerTests(PlatformIsolatedAsyncioTestCase):
    def test_pair_quality_gate_keeps_capture_and_overhead_separate(self) -> None:
        off = {"latency_ms": {"p95": 100.0}}
        on = {"latency_ms": {"p95": 104.0}}
        self.assertEqual(
            cpu_pair._quality_gate(
                off, on,
                service_cpu_delta_percentage_points={"api": 3.0, "web": 2.5},
            )["status"],
            "passed",
        )
        unavailable_cpu = cpu_pair._quality_gate(
            off, on, service_cpu_delta_percentage_points=None
        )
        self.assertEqual(unavailable_cpu["status"], "unmeasured")
        self.assertEqual(
            unavailable_cpu["service_cpu_within_limit"],
            {"api": None, "web": None},
        )
        excessive_latency = cpu_pair._quality_gate(
            off, {"latency_ms": {"p95": 106.0}},
            service_cpu_delta_percentage_points=None,
        )
        self.assertEqual(excessive_latency["status"], "failed")
        excessive_cpu = cpu_pair._quality_gate(
            off, on,
            service_cpu_delta_percentage_points={"api": 3.01, "web": 0.0},
        )
        self.assertEqual(excessive_cpu["status"], "failed")

    def test_cpu_usage_aggregation_uses_process_cpu_and_rejects_incomplete_targets(self) -> None:
        rows = []
        for service, phase, targets, cpu_ns in (
            ("api", "off", 2, 4_000_000_000),
            ("api", "on", 2, 4_400_000_000),
            ("web", "off", 1, 1_000_000_000),
            ("web", "on", 1, 1_200_000_000),
        ):
            rows.append({
                "service": service,
                "phase": phase,
                "expected_targets": targets,
                "observed_targets": targets,
                "event_count": targets,
                "cpu_ns": cpu_ns,
                "window_ms_min": 20_000,
                "window_ms_max": 20_000,
                "start_lag_ms_min": 0,
                "start_lag_ms_max": 0,
                "end_lag_ms_min": 0,
                "end_lag_ms_max": 0,
                "duplicate_count": 0,
                "timing_complete": True,
            })
        summary = {
            "status": "expired_plans_removed",
            "service_count": 2,
            "usage_status": "complete",
            "usage_reason": "none",
            "usage_rows": rows,
            "profile_status": "complete",
            "profile_reason": "none",
            "profile_rows": [
                {
                    "service": "api", "expected_targets": 2, "observed_targets": 2,
                    "event_count": 2, "timer": "thread_cpu", "observation_unit": "calls",
                    "total_cpu_us": 2_000_000, "sample_count": None,
                    "start_lag_ms_min": 0, "start_lag_ms_max": 0,
                    "elapsed_ms_min": 20_000, "elapsed_ms_max": 20_000,
                    "end_lag_ms_min": 0, "end_lag_ms_max": 0,
                    "categories": [{
                        "category": "repo.get_current_user", "cpu_us": 50_000,
                        "observations": 100,
                    }],
                },
                {
                    "service": "web", "expected_targets": 1, "observed_targets": 1,
                    "event_count": 1, "timer": "v8_cpu", "observation_unit": "samples",
                    "total_cpu_us": 1_000_000, "sample_count": 100,
                    "start_lag_ms_min": 0, "start_lag_ms_max": 0,
                    "elapsed_ms_min": 20_000, "elapsed_ms_max": 20_000,
                    "end_lag_ms_min": 0, "end_lag_ms_max": 0,
                    "categories": [{"category": "other", "cpu_us": 1_000_000, "observations": 100}],
                },
            ],
        }
        validated = cpu_pair._validate_usage_summary(summary)
        deltas = cpu_pair._usage_delta_percentage_points(validated)
        self.assertEqual(deltas, {"api": 2.0, "web": 1.0})
        self.assertEqual(
            cpu_pair._quality_gate(
                {"latency_ms": {"p95": 100.0}},
                {"latency_ms": {"p95": 104.0}},
                service_cpu_delta_percentage_points=deltas,
            )["status"],
            "passed",
        )
        rows[1]["duplicate_count"] = 1
        rows[1]["cpu_ns"] = None
        rows[1]["timing_complete"] = False
        incomplete = dict(summary, usage_status="incomplete", usage_reason="duplicate")
        validated_incomplete = cpu_pair._validate_usage_summary(incomplete)
        self.assertIsNotNone(validated_incomplete)
        self.assertIsNone(cpu_pair._usage_delta_percentage_points(validated_incomplete))
        rows[1]["duplicate_count"] = 0
        rows[1]["cpu_ns"] = 4_200_000_000
        rows[1]["timing_complete"] = True
        rows[1]["start_lag_ms_max"] = 251
        self.assertIsNone(cpu_pair._validate_usage_summary(summary))
        rows[1]["start_lag_ms_max"] = 0
        summary["profile_rows"][0]["categories"][0]["category"] = "raw_function_name"
        self.assertIsNone(cpu_pair._validate_usage_summary(summary))

    def test_pair_capture_completeness_preserves_http_500_as_observation(self) -> None:
        now_ms = time.time_ns() // 1_000_000
        plan = {
            "run_id": "a" * 32,
            "off_start_ms": now_ms + 40,
            "off_end_ms": now_ms + 1_040,
            "on_start_ms": now_ms + 1_290,
            "on_end_ms": now_ms + 2_290,
        }
        actors = [
            external_load.VirtualUser(
                "diagnostic-user", "same-workspace", f"s{index:031d}", f"c{index:031d}"
            )
            for index in range(8)
        ]
        self.assertIsNot(cpu_pair.external_load, external_load)

        def server_error(*_args: object, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status=500,
                error_kind="unexpected_status",
                elapsed_ms=12.0,
                finished_at_monotonic=time.monotonic(),
                ok=False,
            )

        with (
            patch.object(cpu_pair, "LEG_MS", 1_000),
            patch.object(cpu_pair, "REQUESTS_PER_LEG", 4),
            patch.object(cpu_pair.external_load, "_page_request", side_effect=server_error) as request,
            patch.object(
                cpu_pair.external_load, "_page_request_http11_keepalive",
                side_effect=AssertionError("network tripwire: HTTP transport reached"),
            ) as transport_tripwire,
            patch.object(
                cpu_pair.external_load, "urlopen",
                side_effect=AssertionError("network tripwire: urllib reached"),
            ) as urllib_tripwire,
            ThreadPoolExecutor(max_workers=8) as executor,
        ):
            capture = cpu_pair._leg(
                "https://old-sparky.com",
                actors,
                ("session", "csrf"),
                plan,
                "off",
                executor,
            )
        self.assertTrue(capture["complete"])
        self.assertFalse(capture["all_expected_statuses"])
        self.assertEqual(capture["scheduled"], 4)
        self.assertEqual(capture["submitted"], 4)
        self.assertEqual(capture["completed"], 4)
        self.assertEqual(capture["status_counts"], {"500": 4})
        self.assertEqual(request.call_count, 4)
        transport_tripwire.assert_not_called()
        urllib_tripwire.assert_not_called()

        # Exercise the complete worker callback without HTTP. In particular,
        # capture completion must be published before the parent-only CPU
        # quality gate runs; the callback must not read a field it has not
        # assigned yet or convert a complete capture to worker failure.
        actor_payload = {
            "schema": 1,
            "workload": cpu_pair.WORKLOAD,
            "origin": external_load.EXPECTED_ORIGIN,
            "session_cookie_name": "session",
            "csrf_cookie_name": "csrf",
            "actors": [
                {
                    "tournament_slug": "same-workspace",
                    "session_token": f"s{index:031d}",
                    "csrf_token": f"c{index:031d}",
                }
                for index in range(8)
            ],
        }
        now_ms = time.time_ns() // 1_000_000
        worker_plan = {
            "schema": 1,
            "run_id": "a" * 32,
            "source_sha": "b" * 40,
            "release_slug": "release-bbbbbbbbbbbb",
            "workload": cpu_pair.WORKLOAD,
            "off_start_ms": now_ms + 50_000,
            "off_end_ms": now_ms + 70_000,
            "on_start_ms": now_ms + 75_000,
            "on_end_ms": now_ms + 95_000,
        }
        worker_payload = {
            "schema": 1,
            "workload": cpu_pair.WORKLOAD,
            "actor_payload": actor_payload,
            "plan": worker_plan,
        }
        worker_bytes = json.dumps(
            worker_payload, sort_keys=True, separators=(",", ":")
        ).encode("ascii") + b"\n"

        class _ExecutorStub:
            def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
                self.shutdown_args = (wait, cancel_futures)

        executor_stub = _ExecutorStub()
        leg_result = {
            "scheduled": 500,
            "submitted": 500,
            "completed": 500,
            "completed_in_window": 500,
            "status_counts": {"200": 500},
            "error_kinds": {"none": 500},
            "latency_ms": {"count": 500, "p50": 10.0, "p95": 20.0, "p99": 30.0},
            "elapsed_seconds": 20.0,
            "drain_deadline_ms": worker_plan["on_end_ms"] + 5_000,
            "complete": True,
            "all_expected_statuses": True,
        }
        with (
            patch.object(cpu_pair.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(worker_bytes))),
            patch.dict(os.environ, {"SOURCE_GIT_SHA": "b" * 40}),
            patch.object(cpu_pair, "_warmup", return_value=(8, 8, 8, executor_stub)),
            patch.object(cpu_pair, "_leg", side_effect=[dict(leg_result), dict(leg_result)]),
        ):
            worker_report = cpu_pair._worker_callback({
                "pair_payload_sha256": cpu_pair.hashlib.sha256(worker_bytes).hexdigest(),
                "binding": {
                    "source_git_sha": "b" * 40,
                    "app_target_sha": "b" * 40,
                    "source_binding": {"schema": 1},
                    "source_binding_sha256": "c" * 64,
                    "external_run_id": "12345",
                    "external_run_attempt": "1",
                },
            })
        self.assertTrue(worker_report["capture_complete"])
        self.assertTrue(worker_report["all_expected_statuses"])
        self.assertTrue(worker_report["passed"])
        self.assertEqual(worker_report["status"], "complete")
        self.assertEqual(executor_stub.shutdown_args, (False, True))

        # The parent validates its early receipt before it launches the
        # namespace child. Bind the accepted receipt to this exact run/source
        # and original SSH-directory inode; wrong bindings fail closed.
        with tempfile.TemporaryDirectory() as temp_dir:
            receipt_dir = Path(temp_dir)
            os.chmod(receipt_dir, 0o700)
            receipt_path = receipt_dir / "ssh-lifecycle.json"
            receipt = {
                "schema": 1,
                "event": "cpu_diagnostic_ssh_lifecycle",
                "source_sha": "b" * 40,
                "run_id": "12345",
                "attempt": "1",
                "config_dir_dev": 27,
                "config_dir_ino": 1234,
                "material_hidden": True,
            }
            cpu_pair._write_report(receipt_path, receipt)
            expected_receipt_binding = {
                "source_sha": "b" * 40,
                "run_id": "12345",
                "attempt": "1",
                "config_dir_dev": 27,
                "config_dir_ino": 1234,
            }
            cpu_pair._validate_ssh_lifecycle_receipt(
                receipt_path, expected=expected_receipt_binding
            )
            with self.assertRaises(cpu_pair.PairError):
                cpu_pair._validate_ssh_lifecycle_receipt(
                    receipt_path,
                    expected={**expected_receipt_binding, "attempt": "2"},
                )
            receipt_path.unlink()
            receipt["material_hidden"] = "true"
            cpu_pair._write_report(receipt_path, receipt)
            with self.assertRaises(cpu_pair.PairError):
                cpu_pair._validate_ssh_lifecycle_receipt(
                    receipt_path, expected=expected_receipt_binding
                )
            receipt_path.unlink()
            receipt["material_hidden"] = True
            cpu_pair._write_report(receipt_path, receipt)
            hardlink_path = receipt_dir / "receipt-hardlink"
            os.link(receipt_path, hardlink_path)
            with self.assertRaises(cpu_pair.PairError):
                cpu_pair._validate_ssh_lifecycle_receipt(
                    receipt_path, expected=expected_receipt_binding
                )
            hardlink_path.unlink()
            receipt_path.unlink()
            target_path = receipt_dir / "receipt-target"
            cpu_pair._write_report(target_path, receipt)
            receipt_path.symlink_to(target_path.name)
            with self.assertRaises(cpu_pair.PairError):
                cpu_pair._validate_ssh_lifecycle_receipt(
                    receipt_path, expected=expected_receipt_binding
                )
            receipt_path.unlink()
            target_path.unlink()
            receipt_path.write_bytes(
                b'{"schema":1,"event":"cpu_diagnostic_ssh_lifecycle",'
                b'"source_sha":"' + (b"b" * 40) + b'","run_id":"12345",'
                b'"attempt":"1","config_dir_dev":27,"config_dir_ino":1234,'
                b'"material_hidden":true,"material_hidden":true}\n'
            )
            os.chmod(receipt_path, 0o600)
            with self.assertRaises(cpu_pair.PairError):
                cpu_pair._validate_ssh_lifecycle_receipt(
                    receipt_path, expected=expected_receipt_binding
                )

        # A partial unlink fails before any child is launched. The pre-marked
        # hidden state still restores the entire exact secret set.
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_dir = Path(temp_dir) / "ssh"
            secret_dir.mkdir(mode=0o700)
            secrets = {
                "config": bytearray(b"ssh config"),
                "id_ed25519": bytearray(b"private key"),
                "known_hosts": bytearray(b"host key"),
            }
            for name, value in secrets.items():
                (secret_dir / name).write_bytes(value)
                os.chmod(secret_dir / name, 0o600)
            secret_dir_stat = secret_dir.stat()
            lifecycle = {
                "closed": True,
                "directory_dev": secret_dir_stat.st_dev,
                "directory_ino": secret_dir_stat.st_ino,
            }
            hidden_state = {"value": False}

            def partial_remove(*_args: object) -> None:
                (secret_dir / "config").unlink()
                raise cpu_pair.PairError("simulated partial unlink")

            with (
                patch.object(cpu_pair, "_remove_ssh_files", side_effect=partial_remove),
                self.assertRaises(cpu_pair.PairError),
            ):
                cpu_pair._hide_ssh_material(
                    secret_dir, secrets, lifecycle, hidden_state,
                )
            self.assertTrue(hidden_state["value"])
            cpu_pair._restore_hidden_ssh_material(
                secret_dir, secrets, hidden_state, lifecycle
            )
            self.assertFalse(hidden_state["value"])
            for name, expected_bytes in secrets.items():
                restored = secret_dir / name
                self.assertEqual(restored.read_bytes(), bytes(expected_bytes))
                self.assertEqual(restored.stat().st_mode & 0o777, 0o600)

        # The remote plan uses one-shot SSH even if the supplied config is
        # changed later. The actual child is reaped before the lifecycle state
        # permits credential removal.
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_dir = Path(temp_dir) / "external-ssh"
            secret_dir.mkdir(mode=0o700)
            secrets = {
                "config": bytearray(b"ssh config"),
                "id_ed25519": bytearray(b"private key"),
                "known_hosts": bytearray(b"host key"),
            }
            for name, value in secrets.items():
                path = secret_dir / name
                path.write_bytes(value)
                os.chmod(path, 0o600)
            secret_dir_stat = secret_dir.stat()
            lifecycle = {
                "closed": True,
                "mux_off_enforced": False,
                "last_command_returned": False,
                "directory_dev": secret_dir_stat.st_dev,
                "directory_ino": secret_dir_stat.st_ino,
            }
            actual_popen = cpu_pair.subprocess.Popen
            captured: dict[str, object] = {}

            def fake_ssh(command: list[str], **kwargs: object):
                captured["command"] = command
                script = (
                    "import sys; sys.stdin.buffer.read(); "
                    "sys.stdout.write('CPU_DIAGNOSTIC_PLAN status=prepared "
                    "targets=3 release_slug=release-123456789012\\n')"
                )
                return actual_popen([cpu_pair.sys.executable, "-c", script], **kwargs)

            with patch.object(cpu_pair.subprocess, "Popen", side_effect=fake_ssh):
                prepared, release_slug, usage = cpu_pair._ssh_plan(
                    SimpleNamespace(), "example.org", "deploy", secret_dir / "config",
                    secret_dir / "id_ed25519", {"operation": "prepare"}, lifecycle,
                )
            self.assertTrue(prepared)
            self.assertEqual(release_slug, "release-123456789012")
            self.assertIsNone(usage)
            self.assertEqual(lifecycle, {
                "closed": True,
                "mux_off_enforced": True,
                "last_command_returned": True,
                "directory_dev": secret_dir_stat.st_dev,
                "directory_ino": secret_dir_stat.st_ino,
            })
            command = captured["command"]
            self.assertIsInstance(command, list)
            for option in ("ControlMaster=no", "ControlPersist=no", "ControlPath=none"):
                self.assertIn(option, command)
            cpu_pair._remove_ssh_files(secret_dir, secrets, lifecycle)
            self.assertFalse(secret_dir.exists())

        # If child death is not yet proven, cleanup refuses to unlink secrets.
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_dir = Path(temp_dir) / "external-ssh"
            secret_dir.mkdir(mode=0o700)
            secrets = {
                "config": bytearray(b"ssh config"),
                "id_ed25519": bytearray(b"private key"),
                "known_hosts": bytearray(b"host key"),
            }
            for name, value in secrets.items():
                path = secret_dir / name
                path.write_bytes(value)
                os.chmod(path, 0o600)
            secret_dir_stat = secret_dir.stat()
            lifecycle = {
                "closed": False,
                "directory_dev": secret_dir_stat.st_dev,
                "directory_ino": secret_dir_stat.st_ino,
            }
            process = cpu_pair.subprocess.Popen(
                [cpu_pair.sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=cpu_pair.subprocess.DEVNULL,
                stdout=cpu_pair.subprocess.DEVNULL,
                stderr=cpu_pair.subprocess.DEVNULL,
                start_new_session=True,
            )
            with self.assertRaises(cpu_pair.PairError):
                cpu_pair._remove_ssh_files(secret_dir, secrets, lifecycle)
            self.assertTrue((secret_dir / "id_ed25519").exists())
            cpu_pair._terminate_worker(process)
            lifecycle["closed"] = True
            self.assertIsNotNone(process.poll())
            cpu_pair._remove_ssh_files(secret_dir, secrets, lifecycle)
            self.assertFalse(secret_dir.exists())

    def test_pair_actor_payload_requires_eight_isolated_credentials_on_one_workspace(self) -> None:
        payload = {
            "schema": 1,
            "workload": cpu_pair.WORKLOAD,
            "origin": external_load.EXPECTED_ORIGIN,
            "session_cookie_name": "session",
            "csrf_cookie_name": "csrf",
            "actors": [
                {
                    "tournament_slug": "same-workspace",
                    "session_token": f"s{index:031d}",
                    "csrf_token": f"c{index:031d}",
                }
                for index in range(8)
            ],
        }
        actors = cpu_pair._validate_actor_payload(payload)
        self.assertEqual(len(actors), 8)
        self.assertEqual({actor.tournament_slug for actor in actors}, {"same-workspace"})
        duplicated = {**payload, "actors": [*payload["actors"][:-1], payload["actors"][0]]}
        with self.assertRaises(cpu_pair.PairError):
            cpu_pair._validate_actor_payload(duplicated)

    def test_controller_accepts_fixed_45_second_pair_and_rejects_bad_timing(self) -> None:
        approved = Namespace(
            off_start_ms=51_000,
            off_end_ms=71_000,
            on_start_ms=76_000,
            on_end_ms=96_000,
        )
        _validate_windows(approved, now_ms=1_000)
        lead_min = Namespace(
            off_start_ms=46_000,
            off_end_ms=66_000,
            on_start_ms=71_000,
            on_end_ms=91_000,
        )
        _validate_windows(lead_min, now_ms=1_000)
        lead_max = Namespace(
            off_start_ms=61_000,
            off_end_ms=81_000,
            on_start_ms=86_000,
            on_end_ms=106_000,
        )
        _validate_windows(lead_max, now_ms=1_000)
        with self.assertRaises(PlanError):
            _validate_windows(
                Namespace(**{**vars(approved), "on_end_ms": 112_000}), now_ms=1_000
            )
        with self.assertRaises(PlanError):
            _validate_windows(approved, now_ms=7_000)
        with self.assertRaises(PlanError):
            _validate_windows(
                Namespace(**{**vars(approved), "on_start_ms": 75_000}), now_ms=1_000
            )
        with self.assertRaises(PlanError):
            _validate_windows(
                Namespace(**{**vars(approved), "off_start_ms": 46_000}), now_ms=1_000
            )
        too_far = Namespace(**{**vars(approved), "off_start_ms": 62_000,
                                "off_end_ms": 82_000, "on_start_ms": 87_000,
                                "on_end_ms": 107_000})
        with self.assertRaises(PlanError):
            _validate_windows(too_far, now_ms=1_000)

    def test_controller_reads_private_values_only_from_closed_canonical_stdin(self) -> None:
        request = {
            "schema": 1,
            "run_id": "a" * 32,
            "source_sha": "b" * 40,
            "workload": "authenticated_workspace_read_pair_v1",
            "off_start_ms": 20_000,
            "off_end_ms": 40_000,
            "on_start_ms": 45_000,
            "on_end_ms": 65_000,
        }
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        parsed = _read_request("prepare-stdin", io.BytesIO(raw))
        self.assertEqual(parsed.command, "prepare")
        self.assertEqual(parsed.run_id, request["run_id"])
        for malformed in (
            b'{"schema":1,"schema":1}\n',
            raw[:-1],
            raw.replace(b'"schema":1', b'"schema":true'),
            raw[:-2] + b',"extra":true}\n',
            _canonical_payload({**request, "release_slug": "caller-supplied"}),
        ):
            with self.subTest(malformed=malformed[:32]), self.assertRaises(PlanError):
                _read_request("prepare-stdin", io.BytesIO(malformed))
        with self.assertRaises(PlanError):
            _read_request("cleanup-stdin", io.BytesIO(raw))

        targets = {
            "api": [
                {"service": "api", "pid": 101, "start_ticks": 11, "invocation_id": "c" * 32},
                {"service": "api", "pid": 102, "start_ticks": 12, "invocation_id": "c" * 32},
            ],
            "web": [
                {"service": "web", "pid": 201, "start_ticks": 21, "invocation_id": "d" * 32},
            ],
        }
        plans = {
            service: {**_diagnostic_payload(service=service), "targets": service_targets}
            for service, service_targets in targets.items()
        }
        api_journal_command = diagnostic_plan_tool._journal_command(plans["api"], "api")
        web_journal_command = diagnostic_plan_tool._journal_command(plans["web"], "web")
        self.assertIn("--unit=deadlock-api.service", api_journal_command)
        self.assertIn("--unit=deadlock-web.service", web_journal_command)
        self.assertIn("_SYSTEMD_INVOCATION_ID=" + "c" * 32, api_journal_command)
        self.assertIn("_SYSTEMD_INVOCATION_ID=" + "d" * 32, web_journal_command)
        self.assertIn("--grep=cpu_diagnostic_", api_journal_command)
        self.assertIn("--grep=cpu_diagnostic_", web_journal_command)
        journal_rows: list[dict[str, str]] = []
        for service, service_targets in targets.items():
            unit = f"deadlock-{service}.service"
            cgroup = f"/system.slice/{unit}"
            for target in service_targets:
                for phase in ("off", "on"):
                    cpu_ns = 4_000_000_000 if phase == "off" else 4_500_000_000
                    message = (
                        "cpu_diagnostic_usage "
                        f"service={service} run_id={'a' * 32} phase={phase} "
                        f"window_ms=20000 cpu_ns={cpu_ns} cpu_capacity_cpus=2.000000 "
                        "start_lag_ms=10 end_lag_ms=5 timing_complete=true"
                    )
                    journal_rows.append({
                        "MESSAGE": (
                            json.dumps({"message": message}, separators=(",", ":"))
                            if service == "api" else message
                        ),
                        "_PID": str(target["pid"]),
                        "_SYSTEMD_UNIT": unit,
                        "_SYSTEMD_INVOCATION_ID": str(target["invocation_id"]),
                        "_SYSTEMD_CGROUP": cgroup,
                        "__REALTIME_TIMESTAMP": "1000000",
                    })
                if service == "api":
                    profile_message = (
                        "cpu_diagnostic_complete service=api "
                        f"run_id={'a' * 32} timer=thread_cpu start_lag_ms=0 elapsed_ms=20000 "
                        "total_self_cpu_us=150 functions="
                        "orm_result:50:1,repo.get_current_user:100:2"
                    )
                    journal_rows.append({
                        "MESSAGE": json.dumps(
                            {"message": profile_message}, separators=(",", ":")
                        ),
                        "_PID": str(target["pid"]),
                        "_SYSTEMD_UNIT": unit,
                        "_SYSTEMD_INVOCATION_ID": str(target["invocation_id"]),
                        "_SYSTEMD_CGROUP": cgroup,
                        "__REALTIME_TIMESTAMP": "26000000",
                    })
                else:
                    profile_message = (
                        "cpu_diagnostic_complete service=web "
                        f"run_id={'a' * 32} timer=v8_cpu start_lag_ms=0 elapsed_ms=20000 "
                        "sample_interval_us=100000 "
                        "sample_count=5 categories=repo.workspace_page:100:3,web_framework:200:2"
                    )
                    journal_rows.append({
                        "MESSAGE": profile_message,
                        "_PID": str(target["pid"]),
                        "_SYSTEMD_UNIT": unit,
                        "_SYSTEMD_INVOCATION_ID": str(target["invocation_id"]),
                        "_SYSTEMD_CGROUP": cgroup,
                        "__REALTIME_TIMESTAMP": "26000000",
                    })

        service_rows = {
            service: [{**target} for target in service_targets]
            for service, service_targets in targets.items()
        }

        def systemd_unit(service: str, *, timeout_seconds: int = 3) -> dict[str, str]:
            self.assertEqual(timeout_seconds, 3)
            return {
                "ActiveState": "active",
                "InvocationID": str(targets[service][0]["invocation_id"]),
                "MainPID": "100",
                "ControlGroup": f"/system.slice/deadlock-{service}.service",
            }

        with (
            patch.object(diagnostic_plan_tool, "_release_identity", return_value=("b" * 40, "release-bbbbbbbbbbbb")),
            patch.object(diagnostic_plan_tool, "_systemd_unit", side_effect=systemd_unit),
            patch.object(diagnostic_plan_tool, "_service_targets", side_effect=lambda service, _unit: service_rows[service]),
            patch.object(diagnostic_plan_tool, "_process_start_ticks", side_effect={101: 11, 102: 12, 201: 21}.get),
            patch.object(diagnostic_plan_tool, "_journal_rows", return_value=("complete", journal_rows)),
        ):
            summary = diagnostic_plan_tool._usage_summary(plans)
        self.assertEqual(summary["usage_status"], "complete", summary)
        self.assertEqual(summary["usage_reason"], "none")
        rows = summary["usage_rows"]
        self.assertEqual([(row["service"], row["phase"]) for row in rows], [
            ("api", "off"), ("api", "on"), ("web", "off"), ("web", "on"),
        ])
        self.assertEqual(rows[0]["cpu_ns"], 8_000_000_000)
        self.assertEqual(summary["profile_status"], "complete")
        self.assertEqual(summary["profile_reason"], "none")
        api_profile, web_profile = summary["profile_rows"]
        self.assertEqual(api_profile["timer"], "thread_cpu")
        self.assertEqual(api_profile["observation_unit"], "calls")
        self.assertIsNone(api_profile["sample_count"])
        self.assertEqual(api_profile["total_cpu_us"], 300)
        self.assertEqual(
            [row["category"] for row in api_profile["categories"]],
            ["orm_result", "repo.get_current_user"],
        )
        self.assertEqual(web_profile["timer"], "v8_cpu")
        self.assertEqual(web_profile["sample_count"], 5)
        self.assertEqual(web_profile["total_cpu_us"], 300)
        self.assertEqual(
            [row["category"] for row in web_profile["categories"]],
            ["repo.workspace_page", "web_framework"],
        )
        serialized = json.dumps(summary, sort_keys=True)
        self.assertNotIn('"pid"', serialized)
        self.assertNotIn('"start_ticks"', serialized)

        with (
            patch.object(diagnostic_plan_tool, "_release_identity", return_value=("b" * 40, "release-bbbbbbbbbbbb")),
            patch.object(diagnostic_plan_tool, "_systemd_unit", side_effect=systemd_unit),
            patch.object(diagnostic_plan_tool, "_service_targets", side_effect=lambda service, _unit: service_rows[service]),
            patch.object(diagnostic_plan_tool, "_process_start_ticks", side_effect={101: 11, 102: 12, 201: 21}.get),
            patch.object(diagnostic_plan_tool, "_journal_rows", return_value=("complete", [*journal_rows, journal_rows[0]])),
        ):
            duplicate = diagnostic_plan_tool._usage_summary(plans)
        self.assertEqual(duplicate["usage_status"], "incomplete")
        self.assertEqual(duplicate["usage_reason"], "duplicate")
        self.assertIsNone(duplicate["usage_rows"][0]["cpu_ns"])

        import python_packages.platform_infra.performance_diagnostic as diagnostic_module

        counter_payload = _diagnostic_payload()
        counter_payload["run_id"] = "e" * 32
        counter_plan = parse_plan_payload(
            _canonical_payload(counter_payload), expected_service="api"
        )

        class CapturedLoop:
            callback = None

            def call_later(self, delay: float, callback: object) -> object:
                self.delay = delay
                self.callback = callback
                return SimpleNamespace(cancel=lambda: None)

        loop = CapturedLoop()
        with (
            patch.object(diagnostic_module, "_effective_cpu_capacity", return_value=2.0),
            patch.object(diagnostic_module.time, "time_ns", side_effect=[1_000_000_000, 21_000_000_000]),
            patch.object(diagnostic_module.time, "monotonic_ns", side_effect=[0, 20_000_000_000]),
            patch.object(diagnostic_module.time, "process_time_ns", side_effect=[100, 3_000_000_100]),
            patch.object(diagnostic_module.logger, "info") as usage_log,
        ):
            diagnostic_module._start_cpu_usage_window(counter_plan, "off", loop)
            self.assertAlmostEqual(loop.delay, 20.0)
            self.assertTrue(callable(loop.callback))
            loop.callback()
        self.assertIn("cpu_diagnostic_usage service=api", usage_log.call_args.args[0])
        self.assertEqual(usage_log.call_args.args[4], 3_000_000_000)
        self.assertEqual(usage_log.call_args.args[-1], "true")
        self.assertNotIn("pid", usage_log.call_args.args[0])

    def test_diagnostic_plan_is_closed_per_service_and_phase_bounded(self) -> None:
        plan = parse_plan_payload(_canonical_payload(_diagnostic_payload()), expected_service="api")
        self.assertEqual(plan.phase_at(1_500), "off")
        self.assertIsNone(plan.phase_at(23_000))
        self.assertEqual(plan.phase_at(26_000), "on")
        self.assertEqual(plan.phase_at(45_999), "on")
        self.assertIsNone(plan.phase_at(46_000))

    def test_api_release_identity_uses_real_released_platform_layout(self) -> None:
        import python_packages.platform_infra.performance_diagnostic as diagnostic_module

        with tempfile.TemporaryDirectory() as temporary_dir:
            release_root = Path(temporary_dir) / "releases" / "release-bbbbbbbbbbbb"
            module_path = (
                release_root / "platform" / "python_packages" / "platform_infra"
                / "performance_diagnostic.py"
            )
            module_path.parent.mkdir(parents=True)
            module_path.write_text("# fixture module path\n", encoding="ascii")
            (release_root / "RELEASE.json").write_text(
                json.dumps({
                    "source_git_commit": "b" * 40,
                    "release_slug": "release-bbbbbbbbbbbb",
                }),
                encoding="ascii",
            )
            release_info = (release_root / "RELEASE.json").stat()
            original_fstat = diagnostic_module.os.fstat

            def fstat_with_uid(fd: int, uid: int):
                observed = original_fstat(fd)
                if (observed.st_dev, observed.st_ino) != (
                    release_info.st_dev,
                    release_info.st_ino,
                ):
                    return observed
                fields = list(observed)
                fields[4] = uid
                return diagnostic_module.os.stat_result(fields)

            with patch.object(diagnostic_module, "__file__", str(module_path)):
                with patch.object(
                    diagnostic_module.os,
                    "fstat",
                    side_effect=lambda fd: fstat_with_uid(fd, 0),
                ):
                    self.assertEqual(
                        diagnostic_module._read_release_identity(),
                        ("b" * 40, "release-bbbbbbbbbbbb"),
                    )
                with patch.object(
                    diagnostic_module.os,
                    "fstat",
                    side_effect=lambda fd: fstat_with_uid(fd, 1),
                ):
                    self.assertIsNone(diagnostic_module._read_release_identity())

    def test_diagnostic_plan_rejects_other_service_duplicate_keys_and_long_window(self) -> None:
        with self.assertRaises(ValueError):
            parse_plan_payload(_canonical_payload(_diagnostic_payload(service="web")), expected_service="api")
        duplicate = b'{"schema":1,"schema":1}\n'
        with self.assertRaises(ValueError):
            parse_plan_payload(duplicate, expected_service="api")
        oversized_window = _diagnostic_payload()
        oversized_window["expires_at_ms"] = 61_001
        with self.assertRaises(ValueError):
            parse_plan_payload(_canonical_payload(oversized_window), expected_service="api")
        mismatched_expiry = _diagnostic_payload()
        mismatched_expiry["expires_at_ms"] = mismatched_expiry["on_end_ms"] + 1
        with self.assertRaises(ValueError):
            parse_plan_payload(_canonical_payload(mismatched_expiry), expected_service="api")

    def test_diagnostic_request_selector_is_exact_and_requires_internal_trace(self) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/safe-slug/workspace",
            "headers": [(b"x-platform-ssr-trace", b"d" * 32)],
        }
        with patch(
            "python_packages.platform_infra.performance_diagnostic.read_process_plan",
            return_value=None,
        ) as read_plan:
            plan, phase = ApiCpuDiagnostic.activate_for_scope(scope)
            self.assertIsNone(plan)
            self.assertIsNone(phase)
            read_plan.assert_called_once_with("api")
        for method, path, headers in (
            ("POST", "/api/v1/tournaments/safe-slug/workspace", [(b"x-platform-ssr-trace", b"1")]),
            ("GET", "/api/v1/tournaments/safe-slug/workspace", []),
            ("GET", "/api/v1/tournaments/safe-slug", [(b"x-platform-ssr-trace", b"1")]),
            ("GET", "/api/v1/tournaments/safe-slug/workspace", [(b"x-platform-ssr-trace", b"1")]),
        ):
            rejected_scope = {**scope, "method": method, "path": path, "headers": headers}
            with patch(
                "python_packages.platform_infra.performance_diagnostic.read_process_plan"
            ) as read_plan:
                self.assertEqual(ApiCpuDiagnostic.activate_for_scope(rejected_scope), (None, None))
                read_plan.assert_not_called()

    def test_profile_summary_exports_only_allowlisted_names_and_fixed_categories(self) -> None:
        import cProfile

        profile = cProfile.Profile(timer=time.thread_time_ns, timeunit=1e-9)
        profile.enable()
        sum(range(1000))
        profile.disable()
        rows, total_cpu_us = __import__(
            "python_packages.platform_infra.performance_diagnostic",
            fromlist=["_profile_summary"],
        )._profile_summary(profile)
        self.assertGreater(total_cpu_us, 0)
        self.assertLessEqual(len(rows), 10)
        self.assertTrue(all(name == "other" or name in {
            "serialization_validation", "orm_result", "db_driver",
            "async_event_loop", "crypto",
        } or name.startswith("repo.") for name, _, _ in rows))

    async def test_api_cpu_capture_is_one_use_and_stops_on_worker_loop(self) -> None:
        import python_packages.platform_infra.performance_diagnostic as diagnostic_module

        payload = _diagnostic_payload()
        payload["run_id"] = "d" * 32
        plan = parse_plan_payload(_canonical_payload(payload), expected_service="api")
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/safe-slug/workspace",
            "headers": [(b"x-platform-ssr-trace", b"d" * 32)],
        }
        with patch(
            "python_packages.platform_infra.performance_diagnostic.read_process_plan",
            return_value=plan,
        ), patch(
            "python_packages.platform_infra.performance_diagnostic.time.time_ns",
            return_value=30_000_000_000,
        ):
            bound_plan, phase = ApiCpuDiagnostic.activate_for_scope(scope)
            self.assertIs(bound_plan, plan)
            self.assertEqual(phase, "on")
            active = diagnostic_module._active_profile
            self.assertIsNotNone(active)
            self.assertIsNotNone(active.profile)
            self.assertIsNotNone(active.stop_handle)
            sum(range(5_000))
            second_plan, second_phase = ApiCpuDiagnostic.activate_for_scope(scope)
            self.assertIs(second_plan, plan)
            self.assertEqual(second_phase, "on")
            self.assertIs(diagnostic_module._active_profile, active)
            active.stop()
            self.assertTrue(active.stopped)
            self.assertIsNone(diagnostic_module._active_profile)

    async def test_profiler_is_disabled_without_explicit_output_directory(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ReadyVoteCpuProfiler.from_environment())

    async def test_signal_session_writes_worker_profile_and_text_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            profiler = ReadyVoteCpuProfiler(Path(temporary_dir))
            await profiler.start()
            profiler.arm()
            sum(range(10_000))
            with patch(
                "python_packages.platform_infra.cpu_profile.os.replace",
                wraps=os.replace,
            ) as atomic_replace:
                profiler.flush()
            await profiler.stop()

            self.assertIsNotNone(profiler._start_time_ticks)
            self.assertEqual(atomic_replace.call_count, 2)
            replace_sources = [Path(call.args[0]) for call in atomic_replace.call_args_list]
            replace_targets = [Path(call.args[1]) for call in atomic_replace.call_args_list]
            self.assertEqual(
                sorted(path.suffixes[-2:] for path in replace_sources),
                [[".pstats", ".tmp"], [".txt", ".tmp"]],
            )
            self.assertEqual(
                sorted(path.suffix for path in replace_targets),
                [".pstats", ".txt"],
            )
            self.assertTrue(all(path.parent == Path(temporary_dir) for path in replace_sources))
            self.assertEqual(
                sorted(path.name for path in Path(temporary_dir).iterdir()),
                [
                    f"ready-vote-cprofile-{os.getpid()}-{profiler._start_time_ticks}.pstats",
                    f"ready-vote-cprofile-{os.getpid()}-{profiler._start_time_ticks}.txt",
                ],
            )
            self.assertIn(
                "sum",
                Path(
                    temporary_dir,
                    f"ready-vote-cprofile-{os.getpid()}-{profiler._start_time_ticks}.txt",
                ).read_text(),
            )

    async def test_stop_before_arm_does_not_write_empty_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            profiler = ReadyVoteCpuProfiler(Path(temporary_dir))
            await profiler.start()
            await profiler.stop()
            self.assertEqual(list(Path(temporary_dir).iterdir()), [])
