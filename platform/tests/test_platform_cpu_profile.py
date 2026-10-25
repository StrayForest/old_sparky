from __future__ import annotations

import ast
import os
import json
import hashlib
import io
import subprocess
import sys
import tempfile
import stat
import threading
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import Future, ThreadPoolExecutor
from argparse import Namespace
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch

from python_packages.platform_infra.cpu_profile import ReadyVoteCpuProfiler
from tools import platform_cpu_diagnostic_pair as cpu_pair
from tools import platform_external_load as external_load
from tools import platform_ssr_capacity_control as capacity_control
from tools import platform_ssr_capacity_diagnostic as capacity_diagnostic
from tools import platform_workflow_remote_dispatch as remote_dispatch
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
    def test_html_ssr_profile_is_distinct_and_only_extends_concurrency_for_html(self) -> None:
        self.assertEqual(
            capacity_diagnostic.profile_payload(8),
            {
                "schema": capacity_diagnostic.SCHEMA,
                "workload": capacity_diagnostic.WORKLOAD,
                "fixture_kind": capacity_diagnostic.FIXTURE_KIND,
                "fixture_tournaments": 1,
                "fixture_users": 14,
                "actors": 8,
                "concurrency": 8,
                "max_inflight": 8,
                "load_model": "closed_loop_best_effort",
                "window_ms": 20_000,
                "matrix_credit": False,
                "slo_credit": False,
            },
        )
        html = capacity_diagnostic.profile_payload(
            64, capacity_diagnostic.HTML_WORKLOAD,
        )
        self.assertEqual(html["fixture_kind"], capacity_diagnostic.HTML_FIXTURE_KIND)
        self.assertEqual(html["actors"], 14)
        self.assertEqual(html["concurrency"], 64)
        self.assertNotEqual(
            capacity_diagnostic.profile_digest(8),
            capacity_diagnostic.profile_digest(8, capacity_diagnostic.HTML_WORKLOAD),
        )
        with self.assertRaises(ValueError):
            capacity_diagnostic.profile_payload(32, capacity_diagnostic.WORKLOAD)
        self.assertIn(
            capacity_diagnostic.HTML_WORKLOAD,
            diagnostic_plan_tool.SUPPORTED_WORKLOADS,
        )
        self.assertIn(
            capacity_diagnostic.HTML_WORKLOAD,
            diagnostic_plan_tool.MEMORY_WATCH_WORKLOADS,
        )

    def test_capacity_response_errors_use_closed_codes_without_untrusted_text(self) -> None:
        cases = (
            ("unexpected_content_type", 200, "unexpected_content_type"),
            ("page_marker_missing", 200, "page_marker_missing"),
            ("unexpected_status", 503, "server_error"),
            ("TimeoutError", 0, "timeout"),
            ("private header leaked", 200, "other"),
        )
        for error_kind, status, expected in cases:
            with self.subTest(error_kind=error_kind):
                code = capacity_diagnostic._capacity_error_code(
                    SimpleNamespace(error_kind=error_kind, status=status),
                )
                self.assertEqual(code, expected)
                self.assertIn(code, capacity_diagnostic.CAPACITY_ERROR_CODES)
        self.assertTrue(capacity_diagnostic._worker_counts_are_closed({
            "status": "incomplete", "completed": 2,
            "http_status_counts": {"200": 1, "503": 1},
            "error_counts": {"page_marker_missing": 1},
        }))
        self.assertFalse(capacity_diagnostic._worker_counts_are_closed({
            "status": "incomplete", "completed": 1,
            "http_status_counts": {"200": 1},
            "error_counts": {"private identity leaked": 1},
        }))

    def test_ssr_capacity_control_is_bound_atomic_fresh_and_accepts_complete(self) -> None:
        expected = {
            "run_id": "a" * 32,
            "source_sha": "b" * 40,
            "app_target_sha": "c" * 40,
            "profile_digest": "d" * 64,
            "external_run_id": "12345",
            "external_run_attempt": "1",
        }

        def record(sequence: int, decision: str = "continue", reason: str = "none"):
            return {
                "schema": 1, **expected, "decision": decision, "reason": reason,
                "sequence": sequence, "updated_monotonic_ns": 10_000_000_000 + sequence,
            }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capacity-control.json"
            capacity_control.write_control(
                path, record(1), expected=expected, now_monotonic_ns=10_000_000_001,
            )
            first = capacity_control.read_control(
                path, expected=expected, now_monotonic_ns=10_000_000_002,
            )
            self.assertEqual(first["decision"], "continue")
            self.assertEqual(
                capacity_control.stop_reason_from_control(
                    path, expected=expected, previous_sequence=first["sequence"],
                    now_monotonic_ns=10_000_000_002,
                ),
                (None, first["sequence"]),
            )
            capacity_control.write_control(
                path, record(2, "complete"), expected=expected, now_monotonic_ns=10_000_000_002,
            )
            complete = capacity_control.read_control(
                path, expected=expected, previous_sequence=1,
                now_monotonic_ns=10_000_000_003,
            )
            self.assertEqual(complete["decision"], "complete")
            self.assertEqual(
                capacity_control.stop_reason_from_control(
                    path, expected=expected, previous_sequence=1,
                    now_monotonic_ns=10_000_000_003,
                ),
                ("window_complete", 2),
            )
            with self.assertRaises(capacity_control.CapacityControlError):
                capacity_control.read_control(
                    path, expected={**expected, "source_sha": "e" * 40},
                    now_monotonic_ns=10_000_000_003,
                )
            with self.assertRaises(capacity_control.CapacityControlError):
                capacity_control.write_control(
                    path, record(3, "abort", "memory_floor"), expected=expected,
                    now_monotonic_ns=12_000_000_000,
                )

    def test_ssr_capacity_control_rejects_symlink_destination(self) -> None:
        expected = {
            "run_id": "a" * 32,
            "source_sha": "b" * 40,
            "app_target_sha": "c" * 40,
            "profile_digest": "d" * 64,
            "external_run_id": "12345",
            "external_run_attempt": "1",
        }
        record = {
            "schema": 1, **expected, "decision": "continue", "reason": "none",
            "sequence": 1, "updated_monotonic_ns": 10_000_000_000,
        }
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("retained", encoding="ascii")
            link = Path(directory) / "capacity-control.json"
            link.symlink_to(target)
            with self.assertRaises(capacity_control.CapacityControlError):
                capacity_control.write_control(
                    link, record, expected=expected, now_monotonic_ns=10_000_000_001,
                )
            self.assertEqual(target.read_text(encoding="ascii"), "retained")

    def test_ssr_capacity_watch_uses_bound_plan_wall_clock_windows(self) -> None:
        plan = {
            "off_start_ms": 2_000, "off_end_ms": 22_000,
            "on_start_ms": 27_000, "on_end_ms": 47_000,
            "workload": diagnostic_plan_tool.CAPACITY_WORKLOAD,
        }
        unit = {
            "ActiveState": "active", "InvocationID": "f" * 32,
            "MainPID": "345", "ControlGroup": "/system.slice/deadlock-web.service",
        }
        target = {"pid": 345, "start_ticks": 123, "invocation_id": "f" * 32}
        values = {
            "memory.current": "1024", "memory.peak": "2048",
            "memory.max": str(diagnostic_plan_tool.SSR_CAPACITY_MEMORY_MAX_BYTES),
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0",
        }
        for workload in (diagnostic_plan_tool.CAPACITY_WORKLOAD, diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD):
            with self.subTest(workload=workload):
                bound_plan = {**plan, "workload": workload}
                with (
                    patch.object(diagnostic_plan_tool.os, "geteuid", return_value=0),
                    patch.object(diagnostic_plan_tool, "_capacity_plan_identity", return_value=(unit, target, bound_plan)),
                    patch.object(diagnostic_plan_tool, "_systemd_memory_max", return_value=diagnostic_plan_tool.SSR_CAPACITY_MEMORY_MAX_BYTES),
                    patch.object(diagnostic_plan_tool, "_systemd_unit", return_value=unit),
                    patch.object(diagnostic_plan_tool, "_service_targets", return_value=[target]),
                    patch.object(diagnostic_plan_tool, "_read_cgroup_pseudo", side_effect=lambda path, maximum: values[path.name]),
                    patch.object(diagnostic_plan_tool.time, "time_ns", side_effect=[1_000_000_000, 22_000_000_000, 47_000_000_000]),
                    patch.object(diagnostic_plan_tool.time, "sleep"),
                ):
                    rows = list(diagnostic_plan_tool.watch_capacity_memory(Namespace(
                        run_id="a" * 32, source_sha="b" * 40, workload=workload,
                    )))
                self.assertEqual([row["decision"] for row in rows], ["continue", "continue", "complete"])
                self.assertEqual([row["phase"] for row in rows], ["off", "idle", "complete"])
                self.assertEqual([row["elapsed_ms"] for row in rows], [0, 20_000, 45_000])
                self.assertTrue(all(row["workload"] == workload for row in rows))
                self.assertTrue(all(row["plan_on_start_ms"] == 27_000 for row in rows))

        # One pipe read can contain several valid records whose aggregate
        # size exceeds the per-record cap. Split before applying that cap,
        # but still reject an oversized unterminated record.
        def watcher_with_chunk() -> capacity_diagnostic._RemoteMemoryWatcher:
            watcher = capacity_diagnostic._RemoteMemoryWatcher(
                ssh_host="example.invalid", ssh_user="tester",
                ssh_config=Path("/tmp/external-ssh/config"),
                ssh_key=Path("/tmp/external-ssh/id_ed25519"),
                control_path=Path("/tmp/capacity-control.json"), binding={},
                run_id="a" * 32, source_sha="b" * 40,
                expected_plan=(0, 25_000, 45_000),
                workload=diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            )
            watcher.process = SimpleNamespace(
                stdout=SimpleNamespace(fileno=lambda: 47), poll=lambda: None,
            )
            watcher.selector = SimpleNamespace(
                select=lambda _timeout: [(SimpleNamespace(fd=47), 1)],
                unregister=lambda _fd: None,
            )
            return watcher

        wire_prefix = capacity_diagnostic.REMOTE_WATCH_PREFIX
        first_wire = wire_prefix + json.dumps(
            {"sequence": 1, "padding": "x" * 1100}, sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        second_wire = wire_prefix + json.dumps(
            {"sequence": 2, "padding": "y" * 1100}, sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii") + b"\n"
        combined_wire = first_wire + second_wire
        self.assertGreater(len(combined_wire), capacity_diagnostic.REMOTE_ROW_CAP)
        coalesced = watcher_with_chunk()
        with (
            patch.object(capacity_diagnostic.os, "set_blocking"),
            patch.object(capacity_diagnostic.os, "read", return_value=combined_wire),
        ):
            self.assertEqual(coalesced._read_row(1)["sequence"], 1)
            self.assertEqual(coalesced._read_row(1)["sequence"], 2)

        oversized_wire = wire_prefix + b"x" * (capacity_diagnostic.REMOTE_ROW_CAP + 1)
        oversized_tail = watcher_with_chunk()
        with (
            patch.object(capacity_diagnostic.os, "set_blocking"),
            patch.object(capacity_diagnostic.os, "read", return_value=oversized_wire),
            self.assertRaisesRegex(ValueError, "row cap"),
        ):
            oversized_tail._read_row(1)

        remote_error = {
            "schema": 1, "stage": "helper_spawn", "reason": "spawn_error",
        }
        error_wire = (
            capacity_diagnostic.REMOTE_WATCH_ERROR_PREFIX
            + json.dumps(remote_error, sort_keys=True, separators=(",", ":")).encode("ascii")
            + b"\n"
        )
        remote_failed = watcher_with_chunk()
        remote_failed.process.wait = lambda timeout=None: 2
        remote_failed.process.poll = lambda: 2
        remote_failed.selector.close = lambda: None
        with (
            patch.object(capacity_diagnostic.os, "set_blocking"),
            patch.object(capacity_diagnostic.os, "read", return_value=error_wire),
            patch.object(capacity_diagnostic, "write_control"),
        ):
            remote_failed._consume()
            remote_summary = remote_failed.stop()
        self.assertEqual(remote_summary["status"], "incomplete")
        self.assertEqual(remote_summary["failure_stage"], "sample_loop")
        self.assertEqual(remote_summary["error_class"], "ValueError")
        self.assertEqual(remote_summary["remote_failure_stage"], "helper_spawn")
        self.assertEqual(remote_summary["remote_failure_reason"], "spawn_error")
        self.assertNotIn("stderr", json.dumps(remote_summary))
        self.assertNotIn("exception", json.dumps(remote_summary))

        remote_refusal_output = io.StringIO()
        with (
            patch.object(remote_dispatch, "_trusted_python_helper", return_value=False),
            redirect_stdout(remote_refusal_output),
        ):
            refusal_status = remote_dispatch._run_cpu_diagnostic_memory_watch({
                "schema": 1, "run_id": "a" * 32, "source_sha": "b" * 40,
                "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            })
        self.assertEqual(refusal_status, 2)
        refusal_line = remote_refusal_output.getvalue().strip()
        self.assertTrue(refusal_line.startswith(remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX))
        refusal_marker = json.loads(
            refusal_line[len(remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX):],
        )
        self.assertEqual(refusal_marker, {
            "schema": 1, "stage": "helper_validation", "reason": "refused",
        })
        self.assertNotIn("ssh", refusal_line.lower())
        self.assertNotIn(str(Path("/")), refusal_line)

        fallback_output = io.StringIO()
        with (
            patch.object(remote_dispatch, "_trusted_python_helper", return_value=False),
            patch.object(remote_dispatch, "CAPACITY_MEMORY_WATCH_FAILURE_PAIRS", frozenset()),
            redirect_stdout(fallback_output),
        ):
            fallback_status = remote_dispatch._run_cpu_diagnostic_memory_watch({
                "schema": 1, "run_id": "a" * 32, "source_sha": "b" * 40,
                "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            })
        self.assertEqual(fallback_status, 2)
        fallback_line = fallback_output.getvalue().strip()
        self.assertTrue(fallback_line.startswith(
            remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX,
        ))
        fallback_marker = json.loads(
            fallback_line[len(remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX):],
        )
        self.assertEqual(fallback_marker, {
            "schema": 1, "stage": "unknown", "reason": "unknown",
        })
        fallback_watcher = watcher_with_chunk()
        fallback_watcher.process.wait = lambda timeout=None: 2
        fallback_watcher.process.poll = lambda: 2
        fallback_watcher.selector.close = lambda: None
        with (
            patch.object(capacity_diagnostic.os, "set_blocking"),
            patch.object(capacity_diagnostic.os, "read", return_value=(fallback_line + "\n").encode("ascii")),
            patch.object(capacity_diagnostic, "write_control"),
        ):
            fallback_watcher._consume()
            fallback_summary = fallback_watcher.stop()
        self.assertEqual(fallback_summary["status"], "incomplete")
        self.assertEqual(fallback_summary["remote_failure_stage"], "unknown")
        self.assertEqual(fallback_summary["remote_failure_reason"], "unknown")

        malformed_error = {
            **remote_error, "private": "must-not-project",
        }
        malformed_wire = (
            capacity_diagnostic.REMOTE_WATCH_ERROR_PREFIX
            + json.dumps(malformed_error, sort_keys=True, separators=(",", ":")).encode("ascii")
            + b"\n"
        )
        malformed_remote = watcher_with_chunk()
        malformed_remote.process.wait = lambda timeout=None: 2
        malformed_remote.process.poll = lambda: 2
        malformed_remote.selector.close = lambda: None
        with (
            patch.object(capacity_diagnostic.os, "set_blocking"),
            patch.object(capacity_diagnostic.os, "read", return_value=malformed_wire),
            patch.object(capacity_diagnostic, "write_control"),
        ):
            malformed_remote._consume()
            malformed_summary = malformed_remote.stop()
        self.assertEqual(malformed_summary["status"], "incomplete")
        self.assertIsNone(malformed_summary["remote_failure_stage"])
        self.assertIsNone(malformed_summary["remote_failure_reason"])
        self.assertNotIn("must-not-project", json.dumps(malformed_summary))

        terminal_sample = {
            "schema": 1, "run_id": "a" * 32, "source_sha": "b" * 40,
            "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            "sequence": 1, "elapsed_ms": 45_000, "phase": "complete",
            "sampled_at_ms": 45_000, "plan_off_start_ms": 0,
            "plan_on_start_ms": 25_000, "plan_on_end_ms": 45_000,
            "decision": "complete", "reason": "none",
            "memory_current_bytes": 1024, "memory_peak_bytes": 2048,
            "memory_max_bytes": diagnostic_plan_tool.SSR_CAPACITY_MEMORY_MAX_BYTES,
            "memory_events_delta": {
                "low": 0, "high": 0, "max": 0, "oom": 0,
                "oom_kill": 0, "oom_group_kill": 0,
            },
            "target_pid": 345, "target_start_ticks": 123,
            "target_invocation_id": "f" * 32, "identity_status": "complete",
        }
        terminal_wire = (
            wire_prefix
            + json.dumps(terminal_sample, sort_keys=True, separators=(",", ":")).encode("ascii")
            + b"\n"
        )

        def consume_terminal_then_tail(chunks: list[bytes]):
            trailing = watcher_with_chunk()
            trailing.expected_plan = (0, 25_000, 45_000)
            trailing.process.poll = lambda: 0
            trailing.process.wait = lambda timeout=None: 0
            trailing.selector.close = lambda: None
            with (
                patch.object(capacity_diagnostic.os, "set_blocking"),
                patch.object(capacity_diagnostic.os, "read", side_effect=chunks),
                patch.object(capacity_diagnostic, "write_control"),
            ):
                trailing._consume()
                return trailing.stop()

        same_read_marker = (
            capacity_diagnostic.REMOTE_WATCH_ERROR_PREFIX
            + b'{"reason":"missing_terminal","schema":1,"stage":"helper_exit"}\n'
        )
        with self.subTest(terminal_tail="same_read_marker"):
            same_read_failure = consume_terminal_then_tail([terminal_wire + same_read_marker])
            self.assertEqual(same_read_failure["status"], "incomplete")
            self.assertEqual(same_read_failure["failure_stage"], "terminal_drain")
            self.assertIsNone(same_read_failure["remote_failure_stage"])
            self.assertIsNone(same_read_failure["remote_failure_reason"])

        with self.subTest(terminal_tail="malformed_bytes"):
            malformed_tail_failure = consume_terminal_then_tail([
                terminal_wire + b"\xff",
            ])
            self.assertEqual(malformed_tail_failure["status"], "incomplete")
            self.assertEqual(malformed_tail_failure["failure_stage"], "terminal_drain")
            self.assertIsNone(malformed_tail_failure["remote_failure_stage"])

        with self.subTest(terminal_tail="later_read_payload"):
            later_read_payload = wire_prefix + b'{"sequence":2}\n'
            later_read_failure = consume_terminal_then_tail([
                terminal_wire, later_read_payload,
            ])
            self.assertEqual(later_read_failure["status"], "incomplete")
            self.assertEqual(later_read_failure["failure_stage"], "terminal_drain")
            self.assertIsNone(later_read_failure["remote_failure_stage"])

        with self.subTest(first_fault="startup_timeout_preserved"):
            preserved = capacity_diagnostic._RemoteMemoryWatcher(
                ssh_host="example.invalid", ssh_user="tester",
                ssh_config=Path("/tmp/external-ssh/config"),
                ssh_key=Path("/tmp/external-ssh/id_ed25519"),
                control_path=Path("/tmp/capacity-control.json"), binding={},
                run_id="a" * 32, source_sha="b" * 40,
                expected_plan=(0, 25_000, 45_000),
                workload=diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            )
            def fail_initial_read() -> None:
                preserved._set_stage("first_row_read")
                raise TimeoutError()

            with patch.object(capacity_diagnostic, "write_control"):
                with (
                    patch.object(preserved, "_start", side_effect=fail_initial_read),
                    self.assertRaises(TimeoutError),
                ):
                    preserved.start()
                # A real startup refusal occurs before child or selector creation.
                preserved.fail_closed()
                preserved_summary = preserved.stop()
            self.assertEqual(preserved_summary["failure_stage"], "first_row_read")
            self.assertEqual(preserved_summary["error_class"], "TimeoutError")

        # Exercise the no-HTTP memory chain against temporary cgroup/systemd
        # fixtures. The child producer advances the real fixed 250ms sampling
        # loop with a controlled wall clock; the dispatcher uses a scaled
        # monotonic clock so its actual deadline path sees the full horizon.
        source_sha = "b" * 40
        run_id = "a" * 32
        off_start_ms = time.time_ns() // 1_000_000 + 60_000
        plan = {
            "off_start_ms": off_start_ms, "off_end_ms": off_start_ms + 20_000,
            "on_start_ms": off_start_ms + 25_000, "on_end_ms": off_start_ms + 45_000,
            "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
        }
        unit = {
            "ActiveState": "active", "InvocationID": "f" * 32,
            "MainPID": "345", "ControlGroup": "/system.slice/deadlock-web.service",
        }
        target = {"pid": 345, "start_ticks": 1234567890, "invocation_id": "f" * 32}
        pseudo_values = {
            "memory.current": "1024", "memory.peak": "2048",
            "memory.max": str(diagnostic_plan_tool.SSR_CAPACITY_MEMORY_MAX_BYTES),
            "memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0",
        }
        request = {
            "schema": 1, "operation": "memory-watch", "run_id": run_id, "source_sha": source_sha,
            "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
        }
        request_bytes = json.dumps(
            request, sort_keys=True, separators=(",", ":"),
        ).encode("ascii") + b"\n"

        with tempfile.TemporaryDirectory(prefix="ssr-memory-chain-") as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            ssh_dir = root / "external-ssh"
            ssh_dir.mkdir(mode=0o700)
            for name, content in (
                ("config", b"fixed ssh config"),
                ("id_ed25519", b"fixed test key"),
                ("known_hosts", b"fixed host key"),
            ):
                path = ssh_dir / name
                path.write_bytes(content)
                path.chmod(0o600)

            helper_path = root / "fake-sudo"
            helper_source = f"#!{sys.executable}\n" + textwrap.dedent(f"""\
                import sys
                import time
                sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
                from tools import platform_cpu_diagnostic_plan as plan_tool
                plan = {plan!r}
                unit = {unit!r}
                target = {target!r}
                values = {pseudo_values!r}
                clock = {{"ms": plan["off_start_ms"] - 60_000}}
                real_sleep = time.sleep
                plan_tool.os.geteuid = lambda: 0
                plan_tool._capacity_plan_identity = lambda _run, _sha: (unit, target, plan)
                plan_tool._systemd_memory_max = lambda: plan_tool.SSR_CAPACITY_MEMORY_MAX_BYTES
                plan_tool._systemd_unit = lambda _service, **_kw: unit
                plan_tool._service_targets = lambda _service, _unit: [target]
                plan_tool._read_cgroup_pseudo = lambda path, **_kw: values[path.name]
                plan_tool.time.time_ns = lambda: clock["ms"] * 1_000_000
                def fast_forward(seconds):
                    clock["ms"] = min(plan["on_end_ms"], clock["ms"] + round(seconds * 1000))
                    # 4.5ms real × the dispatcher's 60× monotonic scale
                    # keeps the 105s virtual plan inside 130s but beyond 55s.
                    real_sleep(0.0045)
                plan_tool.time.sleep = fast_forward
                raise SystemExit(plan_tool.main(["memory-watch-stdin"]))
            """)
            ast.parse(helper_source, filename="generated-fake-sudo-helper.py")
            helper_path.write_text(helper_source, encoding="utf-8")
            helper_path.chmod(0o700)

            def dispatcher_source(*, timeout_seconds: int, output_cap: int) -> str:
                source = textwrap.dedent(f"""\
                    import sys
                    import time
                    sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
                    from tools import platform_workflow_remote_dispatch as dispatch
                    real_monotonic = time.monotonic
                    clock_start = real_monotonic()
                    dispatch.time.monotonic = lambda: (real_monotonic() - clock_start) * 60
                    dispatch.SUDO = {str(helper_path)!r}
                    dispatch._trusted_python_helper = lambda _path: True
                    dispatch.CAPACITY_MEMORY_WATCH_TIMEOUT_SECONDS = {timeout_seconds}
                    dispatch.CAPACITY_MEMORY_WATCH_OUTPUT_CAP = {output_cap}
                    raise SystemExit(dispatch.main(["cpu-diagnostic-memory-watch"]))
                """)
                ast.parse(source, filename="generated-memory-watch-dispatcher.py")
                return source

            def run_dispatcher_direct(*, timeout_seconds: int, output_cap: int):
                return subprocess.run(
                    [sys.executable, "-c", dispatcher_source(
                        timeout_seconds=timeout_seconds, output_cap=output_cap,
                    )],
                    input=request_bytes, capture_output=True, check=False,
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                )

            def write_helper_output(output: bytes) -> None:
                fixture_source = f"#!{sys.executable}\n" + textwrap.dedent(f"""\
                    import sys
                    sys.stdin.buffer.read(4097)
                    sys.stdout.buffer.write({output!r})
                    sys.stdout.buffer.flush()
                """)
                ast.parse(fixture_source, filename="generated-malformed-memory-helper.py")
                helper_path.write_text(fixture_source, encoding="utf-8")
                helper_path.chmod(0o700)

            helper_row_prefix = remote_dispatch.CAPACITY_MEMORY_WATCH_ROW_PREFIX.encode("ascii")
            for helper_output_case, helper_output in (
                ("bare_json", b'{"schema":1}\n'),
                ("double_prefix", helper_row_prefix + helper_row_prefix + b'{"schema":1}\n'),
                ("malformed_prefixed_json", helper_row_prefix + b"not-json\n"),
            ):
                with self.subTest(helper_row=helper_output_case):
                    write_helper_output(helper_output)
                    malformed_child = run_dispatcher_direct(
                        timeout_seconds=130,
                        output_cap=remote_dispatch.CAPACITY_MEMORY_WATCH_OUTPUT_CAP,
                    )
                    self.assertTrue(
                        malformed_child.stdout,
                        "malformed child produced no dispatcher marker; "
                        f"rc={malformed_child.returncode} stderr={malformed_child.stderr[:1000]!r}",
                    )
                    self.assertEqual(
                        malformed_child.returncode, 2,
                        f"generated dispatcher stderr={malformed_child.stderr[:1000]!r}",
                    )
                    marker_line = malformed_child.stdout.splitlines()[-1]
                    self.assertTrue(marker_line.startswith(
                        remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX.encode("ascii"),
                    ))
                    marker = json.loads(
                        marker_line[len(remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX.encode("ascii")):],
                    )
                    self.assertEqual(marker, {
                        "schema": 1, "stage": "row_protocol", "reason": "malformed",
                    })

            helper_path.write_text(helper_source, encoding="utf-8")
            helper_path.chmod(0o700)

            with self.subTest(dispatcher_bound="old_timeout"):
                short_timeout = run_dispatcher_direct(
                    timeout_seconds=55,
                    output_cap=remote_dispatch.CAPACITY_MEMORY_WATCH_OUTPUT_CAP,
                )
                self.assertTrue(
                    short_timeout.stdout,
                    "generated dispatcher produced no diagnostic stdout; "
                    f"rc={short_timeout.returncode} stderr={short_timeout.stderr[:1000]!r}",
                )
                self.assertEqual(short_timeout.returncode, 2)
                timeout_marker_line = short_timeout.stdout.splitlines()[-1]
                self.assertTrue(timeout_marker_line.startswith(
                    remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX.encode("ascii"),
                ))
                timeout_marker = json.loads(
                    timeout_marker_line[
                        len(remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX.encode("ascii")):
                    ],
                )
                self.assertEqual(timeout_marker, {
                    "schema": 1, "stage": "watch_timeout", "reason": "timeout",
                })
            with self.subTest(dispatcher_bound="old_output_cap"):
                old_output_cap = run_dispatcher_direct(
                    timeout_seconds=130, output_cap=131_072,
                )
                self.assertTrue(
                    old_output_cap.stdout,
                    "generated dispatcher produced no diagnostic stdout; "
                    f"rc={old_output_cap.returncode} stderr={old_output_cap.stderr[:1000]!r}",
                )
                self.assertEqual(old_output_cap.returncode, 2)
                self.assertLessEqual(len(old_output_cap.stdout), 131_072)
                self.assertTrue(old_output_cap.stdout.startswith(b"CAPACITY_MEMORY_GUARD "))
                output_cap_marker_line = old_output_cap.stdout.splitlines()[-1]
                self.assertTrue(output_cap_marker_line.startswith(
                    remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX.encode("ascii"),
                ))
                output_cap_marker = json.loads(
                    output_cap_marker_line[
                        len(remote_dispatch.CAPACITY_MEMORY_WATCH_ERROR_PREFIX.encode("ascii")):
                    ],
                )
                self.assertEqual(output_cap_marker, {
                    "schema": 1, "stage": "output_limit", "reason": "limit",
                })

            full_dispatcher = dispatcher_source(
                timeout_seconds=130,
                output_cap=remote_dispatch.CAPACITY_MEMORY_WATCH_OUTPUT_CAP,
            )
            real_popen = subprocess.Popen

            def fake_ssh_popen(command, **kwargs):
                self.assertEqual(command[-1], "cpu-diagnostic-memory-watch")
                return real_popen([sys.executable, "-c", full_dispatcher], **kwargs)

            binding = {
                "run_id": run_id, "source_sha": source_sha, "app_target_sha": source_sha,
                "profile_digest": capacity_diagnostic.profile_digest(8),
                "external_run_id": "12345", "external_run_attempt": "1",
            }
            control_path = root / "capacity-control.json"
            watcher = capacity_diagnostic._RemoteMemoryWatcher(
                ssh_host="example.invalid", ssh_user="tester",
                ssh_config=ssh_dir / "config", ssh_key=ssh_dir / "id_ed25519",
                control_path=control_path, binding=binding,
                run_id=run_id, source_sha=source_sha,
                expected_plan=(plan["off_start_ms"], plan["on_start_ms"], plan["on_end_ms"]),
                workload=diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            )
            memory_result = None
            with patch.object(capacity_diagnostic.subprocess, "Popen", side_effect=fake_ssh_popen):
                with self.subTest(memory_chain="full"):
                    try:
                        watcher.start()
                        self.assertGreaterEqual(watcher.samples, 1)
                    finally:
                        memory_result = watcher.stop()
                    self.assertEqual(
                        memory_result["status"], "complete",
                        f"parent report={memory_result!r}; child_rc="
                        f"{watcher.process.poll() if watcher.process is not None else None}",
                    )
                    self.assertEqual(memory_result["terminal_decision"], "complete")
                    self.assertEqual(memory_result["terminal_reason"], "none")
                    self.assertGreaterEqual(memory_result["samples"], 180)
                    self.assertLessEqual(watcher.total, capacity_diagnostic.REMOTE_WATCH_CAP)

            # The parent reader has the same cumulative-stream bound. Prove
            # the previous bound rejects this actual full stream, while the
            # line cap remains independently enforced by _read_row.
            old_parent_watcher = capacity_diagnostic._RemoteMemoryWatcher(
                ssh_host="example.invalid", ssh_user="tester",
                ssh_config=ssh_dir / "config", ssh_key=ssh_dir / "id_ed25519",
                control_path=root / "capacity-control-old-cap.json", binding=binding,
                run_id=run_id, source_sha=source_sha,
                expected_plan=(plan["off_start_ms"], plan["on_start_ms"], plan["on_end_ms"]),
                workload=diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
            )
            with self.subTest(parent_bound="old_output_cap"):
                with (
                    patch.object(capacity_diagnostic, "REMOTE_WATCH_CAP", 131_072),
                    patch.object(capacity_diagnostic.subprocess, "Popen", side_effect=fake_ssh_popen),
                ):
                    try:
                        old_parent_watcher.start()
                    finally:
                        old_parent_result = old_parent_watcher.stop()
                self.assertEqual(
                    old_parent_result["status"], "incomplete",
                    f"parent report={old_parent_result!r}; child_rc="
                    f"{old_parent_watcher.process.poll() if old_parent_watcher.process is not None else None}",
                )
                self.assertEqual(old_parent_result["terminal_reason"], "observer_failed")
                fixture_row_bound = (
                    plan["on_end_ms"] - (plan["off_start_ms"] - 60_000) + 249
                ) // 250 + 2
                self.assertLessEqual(old_parent_result["samples"], fixture_row_bound)
                maximum_rows = (130_000 + 249) // 250 + 2
                parent_stream_bound = maximum_rows * (capacity_diagnostic.REMOTE_ROW_CAP + 1)
                helper_stream_bound = maximum_rows * (
                    remote_dispatch.CAPACITY_MEMORY_WATCH_LINE_CAP + 1
                )
                self.assertGreaterEqual(
                    remote_dispatch.CAPACITY_MEMORY_WATCH_OUTPUT_CAP, helper_stream_bound,
                )
                self.assertGreaterEqual(
                    capacity_diagnostic.REMOTE_WATCH_CAP, parent_stream_bound,
                )
                if memory_result is not None and memory_result.get("status") == "complete":
                    self.assertLess(old_parent_result["samples"], memory_result["samples"])

    def test_ssr_capacity_watch_rejects_non_root_before_reading_plan(self) -> None:
        with (
            patch.object(diagnostic_plan_tool.os, "geteuid", return_value=1000),
            patch.object(diagnostic_plan_tool, "_capacity_plan_identity") as plan_identity,
        ):
            with self.assertRaisesRegex(PlanError, "root identity required"):
                list(diagnostic_plan_tool.watch_capacity_memory(
                    Namespace(run_id="a" * 32, source_sha="b" * 40),
                ))
        plan_identity.assert_not_called()

    def test_ssr_capacity_watch_rejects_cross_workload_memory_request(self) -> None:
        plan = {
            "off_start_ms": 2_000, "off_end_ms": 22_000,
            "on_start_ms": 27_000, "on_end_ms": 47_000,
            "workload": diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
        }
        with (
            patch.object(diagnostic_plan_tool.os, "geteuid", return_value=0),
            patch.object(
                diagnostic_plan_tool, "_capacity_plan_identity",
                return_value=({}, {}, plan),
            ),
        ):
            with self.assertRaisesRegex(PlanError, "workload binding changed"):
                list(diagnostic_plan_tool.watch_capacity_memory(Namespace(
                    run_id="a" * 32, source_sha="b" * 40,
                    workload=diagnostic_plan_tool.CAPACITY_WORKLOAD,
                )))

    def test_ssr_capacity_remote_observation_maps_only_bound_abort_to_worker_control(self) -> None:
        binding = {
            "run_id": "a" * 32, "source_sha": "b" * 40,
            "app_target_sha": "c" * 40, "profile_digest": "d" * 64,
            "external_run_id": "12345", "external_run_attempt": "1",
        }
        with tempfile.TemporaryDirectory() as directory:
            control_path = Path(directory) / "capacity-control.json"
            watcher = capacity_diagnostic._RemoteMemoryWatcher(
                ssh_host="host", ssh_user="user", ssh_config=Path("/tmp/config"),
                ssh_key=Path("/tmp/key"), control_path=control_path,
                binding=binding, run_id=binding["run_id"], source_sha=binding["app_target_sha"],
                expected_plan=(1_000, 26_000, 46_000),
            )
            row = {
                "schema": 1, "run_id": binding["run_id"],
                "source_sha": binding["app_target_sha"],
                "workload": capacity_diagnostic.WORKLOAD, "sequence": 1,
                "elapsed_ms": 0, "phase": "off", "sampled_at_ms": 1_000,
                "plan_off_start_ms": 1_000, "plan_on_start_ms": 26_000,
                "plan_on_end_ms": 46_000, "decision": "abort", "reason": "memory_floor",
                "memory_current_bytes": 805_306_368, "memory_peak_bytes": 810_000_000,
                "memory_max_bytes": 1_073_741_824,
                "memory_events_delta": {name: 0 for name in (
                    "low", "high", "max", "oom", "oom_kill", "oom_group_kill",
                )},
                "target_pid": 345, "target_start_ticks": 123,
                "target_invocation_id": "f" * 32, "identity_status": "complete",
            }
            watcher._accept(row)
            control = capacity_control.read_control(
                control_path, expected=binding, now_monotonic_ns=time.monotonic_ns(),
            )
            self.assertEqual(control["decision"], "abort")
            self.assertEqual(control["reason"], "memory_floor")
            self.assertEqual(watcher.stop()["status"], "complete")
            tampered = {**row, "sequence": 2, "source_sha": "e" * 40}
            with self.assertRaises(ValueError):
                watcher._accept(tampered)
            mismatched_window = {**row, "sequence": 2, "plan_on_start_ms": 27_000, "plan_on_end_ms": 47_000}
            with self.assertRaises(ValueError):
                watcher._accept(mismatched_window)

    def test_ssr_capacity_remote_identity_failure_preserves_typed_abort_reason(self) -> None:
        binding = {
            "run_id": "a" * 32, "source_sha": "b" * 40,
            "app_target_sha": "c" * 40, "profile_digest": "d" * 64,
            "external_run_id": "12345", "external_run_attempt": "1",
        }
        with tempfile.TemporaryDirectory() as directory:
            control_path = Path(directory) / "capacity-control.json"
            watcher = capacity_diagnostic._RemoteMemoryWatcher(
                ssh_host="host", ssh_user="user", ssh_config=Path("/tmp/config"),
                ssh_key=Path("/tmp/key"), control_path=control_path,
                binding=binding, run_id=binding["run_id"], source_sha=binding["app_target_sha"],
                expected_plan=(1_000, 26_000, 46_000),
            )
            row = {
                "schema": 1, "run_id": binding["run_id"],
                "source_sha": binding["app_target_sha"], "workload": capacity_diagnostic.WORKLOAD,
                "sequence": 1, "elapsed_ms": 0, "phase": "off", "sampled_at_ms": 1_000,
                "plan_off_start_ms": 1_000, "plan_on_start_ms": 26_000,
                "plan_on_end_ms": 46_000, "decision": "abort", "reason": "identity_changed",
                "memory_current_bytes": None, "memory_peak_bytes": None,
                "memory_max_bytes": None, "memory_events_delta": None,
                "target_pid": 345, "target_start_ticks": 123,
                "target_invocation_id": "f" * 32, "identity_status": "invalid",
            }
            watcher._accept(row)
            control = capacity_control.read_control(
                control_path, expected=binding, now_monotonic_ns=time.monotonic_ns(),
            )
            self.assertEqual(control["decision"], "abort")
            self.assertEqual(control["reason"], "identity_changed")
            self.assertEqual(watcher.stop()["terminal_reason"], "identity_changed")

    def test_ssr_capacity_parent_rejects_worker_report_binding_mutations(self) -> None:
        binding = {
            "run_id": "a" * 32, "source_sha": "b" * 40,
            "app_target_sha": "c" * 40, "profile_digest": "d" * 64,
            "external_run_id": "12345", "external_run_attempt": "1",
        }
        config = {
            "binding": binding, "manifest_sha256": "e" * 64,
            "manifest_bytes": 2048,
            "concurrency": 8, "on_start_ms": 26_000, "on_end_ms": 46_000,
            "workload": capacity_diagnostic.WORKLOAD,
            "fixture_kind": capacity_diagnostic.FIXTURE_KIND,
        }
        report = {
            "report_complete": True, "schema": 1,
            "workload": capacity_diagnostic.WORKLOAD,
            "fixture_kind": capacity_diagnostic.FIXTURE_KIND,
            **binding,
            "fixture_manifest_sha256": "e" * 64,
            "concurrency": 8, "actors": 8,
            "fixture_tournaments": 1, "fixture_users": 14,
            "fixture_manifest_bytes": 2048,
            "load_model": "closed_loop_best_effort", "window_ms": 20_000,
            "on_window_start_ms": 26_000, "on_window_end_ms": 46_000,
            "matrix_credit": False, "slo_credit": False,
            "diagnostic_credit": False, "namespace_closed": False,
            "status": "complete", "submitted": 40, "completed": 40,
            "max_inflight": 8, "response_bytes_total": 8192,
            "observed_duration_ms": 20_000,
            "http_status_counts": {"200": 40}, "error_counts": {},
            "latency_ms": {"sample_count": 40, "p50": 10.0, "p95": 20.0, "p99": 25.0, "max": 30.0},
            "off_probe": {"submitted": 8, "completed": 8, "http_status": 200},
            "process_cpu_windows": [
                {"window": name, "status": "complete", "cpu_ns": 4_000_000_000,
                 "elapsed_ns": 20_000_000_000, "cores": 0.2}
                for name in ("off", "on")
            ],
            "resource_abort_reason": None, "passed": True,
            "generator_cpu_evidence": "measured",
            "server_capacity_claim": None,
            "server_capacity_claim_reason": "resource_correlation_not_provided",
        }
        def through_worker_entry(
            callback_report: dict[str, object],
        ) -> dict[str, object]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report_path = root / "worker-report.json"
                config_path = root / "worker-config.json"
                config_path.write_text(
                    json.dumps({"worker_report_path": str(report_path)}),
                    encoding="utf-8",
                )
                with patch.object(
                    capacity_diagnostic.load_runtime, "_set_parent_death_signal",
                ):
                    return_code = capacity_diagnostic.load_runtime.worker_entry(
                        lambda _config: callback_report,
                        config_path=config_path,
                    )
                self.assertEqual(
                    return_code,
                    0 if callback_report.get("passed") is True else 1,
                )
                return json.loads(report_path.read_text(encoding="utf-8"))

        html_binding = {
            **binding,
            "profile_digest": capacity_diagnostic.profile_digest(
                64, capacity_diagnostic.HTML_WORKLOAD,
            ),
        }
        html_config = {
            **config,
            "binding": html_binding,
            "workload": capacity_diagnostic.HTML_WORKLOAD,
            "fixture_kind": capacity_diagnostic.HTML_FIXTURE_KIND,
            "concurrency": 64,
        }
        html_report = through_worker_entry({
            **report,
            **capacity_diagnostic.profile_payload(64, capacity_diagnostic.HTML_WORKLOAD),
            **html_binding,
            "actors": 14,
            "concurrency": 64,
            "max_inflight": 64,
            "off_probe": {"submitted": 64, "completed": 64, "http_status": 200},
        })
        self.assertTrue(capacity_diagnostic._worker_report_matches(html_report, html_config))
        self.assertFalse(capacity_diagnostic._worker_report_matches(
            {**html_report, "workload": capacity_diagnostic.WORKLOAD}, html_config,
        ))

        report = through_worker_entry(report)
        self.assertTrue(capacity_diagnostic._worker_report_matches(report, config))
        for mutation in (
            {**report, "worker_report_schema": 999},
            {key: value for key, value in report.items() if key != "partial_work"},
            {**report, "partial_work": 0},
            {**report, "unknown_envelope_field": False},
            {**report, "status": []},
            {**report, "error_counts": {"private identity leaked": 1}},
            {**report, "http_status_counts": {"200": 39}},
            {**report, "partial_work": True},
            {**report, "inflight_unknown": True},
        ):
            with self.subTest(envelope=mutation):
                self.assertFalse(
                    capacity_diagnostic._worker_report_matches(mutation, config)
                )
        callback_abort = capacity_diagnostic._incomplete_report(
            config,
            binding,
            "observer_failed",
            submitted=0,
            completed=0,
            observed_duration_ms=0,
            manifest_sha256="e" * 64,
        )
        self.assertFalse(capacity_diagnostic._worker_report_matches(callback_abort, config))
        pre_request_abort = through_worker_entry(callback_abort)
        self.assertEqual(pre_request_abort["observed_duration_ms"], 0)
        self.assertTrue(
            capacity_diagnostic._worker_report_matches(pre_request_abort, config)
        )
        malformed_pre_request_abort = {
            key: value for key, value in pre_request_abort.items()
            if key != "observed_duration_ms"
        }
        self.assertFalse(
            capacity_diagnostic._worker_report_matches(
                malformed_pre_request_abort, config,
            )
        )
        measured_abort = through_worker_entry(
            capacity_diagnostic._incomplete_report(
                config,
                binding,
                "worker_error",
                submitted=0,
                completed=0,
                observed_duration_ms=1_234,
                manifest_sha256="e" * 64,
            )
        )
        self.assertEqual(measured_abort["observed_duration_ms"], 1_234)
        self.assertTrue(capacity_diagnostic._worker_report_matches(measured_abort, config))
        self.assertEqual(
            capacity_diagnostic._generator_cpu_evidence_status(report["process_cpu_windows"]),
            "measured",
        )
        one_probe = json.loads(json.dumps(report))
        one_probe["off_probe"] = {"submitted": 1, "completed": 1, "http_status": 200}
        self.assertFalse(capacity_diagnostic._worker_report_matches(one_probe, config))
        for key, value in (
            ("source_sha", "f" * 40), ("app_target_sha", "f" * 40),
            ("external_run_id", "54321"), ("external_run_attempt", "2"),
            ("run_id", "f" * 32), ("profile_digest", "f" * 64),
            ("fixture_manifest_sha256", "f" * 64), ("concurrency", 16),
            ("on_window_end_ms", 46_001),
        ):
            with self.subTest(field=key):
                self.assertFalse(capacity_diagnostic._worker_report_matches(
                    {**report, key: value}, config,
                ))

        incomplete_cpu = json.loads(json.dumps(report))
        incomplete_cpu["process_cpu_windows"][1] = {
            "window": "on", "status": "incomplete", "cpu_ns": None,
            "elapsed_ns": None, "cores": None,
        }
        incomplete_cpu["generator_cpu_evidence"] = "incomplete"
        self.assertEqual(
            capacity_diagnostic._generator_cpu_evidence_status(
                incomplete_cpu["process_cpu_windows"],
            ),
            "incomplete",
        )
        self.assertTrue(capacity_diagnostic._worker_report_matches(incomplete_cpu, config))
        missing_cpu = {key: value for key, value in report.items() if key != "process_cpu_windows"}
        missing_cpu["generator_cpu_evidence"] = "incomplete"
        self.assertTrue(capacity_diagnostic._worker_report_matches(missing_cpu, config))
        self.assertEqual(
            capacity_diagnostic.evaluate_capacity_report(
                {"not": "a full report"}, source_sha="b" * 40,
                app_target_sha="c" * 40, run_id="12345", attempt="1",
                manifest_sha256="e" * 64,
            ),
            "INVALID",
        )

        start = {
            "target_ns": 1_000_000_000, "wall_before_ns": 999_999_990,
            "wall_after_ns": 1_000_000_010, "mono_before_ns": 5_000_000_000,
            "mono_after_ns": 5_000_000_010, "cpu_ns": 1_000_000_000,
        }
        end = {
            "target_ns": 21_000_000_000, "wall_before_ns": 20_999_999_990,
            "wall_after_ns": 21_000_000_010, "mono_before_ns": 25_000_000_000,
            "mono_after_ns": 25_000_000_010, "cpu_ns": 5_000_000_000,
        }
        self.assertEqual(
            capacity_diagnostic._cpu_window_row("off", start, end),
            {"window": "off", "status": "complete", "cpu_ns": 4_000_000_000,
             "elapsed_ns": 20_000_000_000, "cores": 0.2},
        )
        late_end = {**end, "wall_before_ns": 21_300_000_000,
                    "wall_after_ns": 21_300_000_020}
        self.assertEqual(
            capacity_diagnostic._cpu_window_row("on", start, late_end),
            {"window": "on", "status": "incomplete", "cpu_ns": None,
             "elapsed_ns": None, "cores": None},
        )
        self.assertFalse(capacity_diagnostic._valid_cpu_window_rows([
            capacity_diagnostic._cpu_window_row("off", start, end),
            {"window": "on", "status": "complete", "cpu_ns": 200_000_000,
             "elapsed_ns": 1_000_000_000, "cores": 0.2},
        ]))

    def test_ssr_capacity_remote_complete_cannot_end_local_window_early(self) -> None:
        self.assertEqual(
            capacity_diagnostic._guard_reason_for_local_window(
                "window_complete", local_window_end_monotonic=20.0, now_monotonic=19.999,
            ),
            "observer_timing_error",
        )
        self.assertEqual(
            capacity_diagnostic._guard_reason_for_local_window(
                "window_complete", local_window_end_monotonic=20.0, now_monotonic=20.0,
            ),
            "window_complete",
        )
        self.assertIsNone(capacity_diagnostic._guard_reason_for_local_window(
            None, local_window_end_monotonic=20.0, now_monotonic=19.0,
        ))

    def test_ssr_parent_plan_refusal_preserves_closed_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            ssh_dir = root / "external-ssh"
            ssh_dir.mkdir(mode=0o700)
            manifest_path = root / "manifest.json"
            report_path = root / "report.json"
            manifest_raw = b"fixture manifest\n"
            manifest = {
                "runner_sha": "a" * 40,
                "app_target_sha": "b" * 40,
                "tournaments": [{"user_count": 14}],
            }
            actors = [object() for _ in range(14)]

            def fake_ssh_plan(_args, _host, _user, _config, _key, payload, lifecycle, **_kwargs):
                if payload["operation"] == "prepare":
                    lifecycle.update({
                        "plan_prepare_status": "refused",
                        "plan_prepare_refusal_class": "ValueError",
                        "prepare_transport": {
                            "stage": "complete", "return_code": 2,
                            "stdout_shape": "refusal_marker", "child_state": "reaped",
                        },
                    })
                    return False, None, None
                lifecycle.update({
                    "plan_cleanup_status": "complete",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "complete", "return_code": 0,
                        "stdout_shape": "summary_marker", "child_state": "reaped",
                    },
                })
                return True, None, {"service_count": 0}

            args = Namespace(
                source_sha="a" * 40, app_target_sha="b" * 40,
                external_run_id="12345", external_run_attempt="1",
                concurrency=8, report=str(report_path),
                ssh_config=str(ssh_dir / "config"), ssh_key=str(ssh_dir / "id_ed25519"),
                manifest=str(manifest_path), ssh_host="example.test", ssh_user="deploy",
            )
            with (
                patch.object(capacity_diagnostic, "_read_manifest", return_value=(manifest_raw, manifest, actors)),
                patch.object(cpu_pair, "_ssh_plan", side_effect=fake_ssh_plan) as ssh_plan,
            ):
                receipt = capacity_diagnostic._parent_run(args)
                self.assertEqual(ssh_plan.call_count, 1)

            self.assertIsNotNone(capacity_diagnostic._project_parent_failure_receipt(receipt))
            self.assertEqual(receipt["status"], "incomplete")
            self.assertEqual(receipt["stage"], "remote_plan_prepare")
            self.assertEqual(receipt["error_class"], "ValueError")
            self.assertEqual(receipt["plan_prepare_status"], "refused")
            self.assertEqual(receipt["plan_prepare_refusal_class"], "ValueError")
            self.assertEqual(receipt["ssh_prepare_transport"], {
                "stage": "complete", "return_code": 2,
                "stdout_shape": "refusal_marker", "child_state": "reaped",
            })
            self.assertEqual(receipt["plan_cleanup_status"], "not_attempted")
            self.assertEqual(receipt["remote_plan_cleanup"], "not_attempted")
            self.assertFalse(receipt["namespace_closed"])
            self.assertFalse(receipt["passed"])
            self.assertFalse(receipt["diagnostic_credit"])
            refusal_report_path = root / "parent-refusal-report.json"
            refusal_fingerprint_path = root / "failure-fingerprint.json"
            refusal_stdout = io.StringIO()
            with (
                patch.object(
                    capacity_diagnostic, "_read_manifest",
                    return_value=(manifest_raw, manifest, actors),
                ),
                patch.object(cpu_pair, "_ssh_plan", side_effect=fake_ssh_plan),
                redirect_stdout(refusal_stdout),
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "parent", "--manifest", str(manifest_path),
                    "--report", str(refusal_report_path),
                    "--failure-report", str(refusal_fingerprint_path),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                    "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                    "--ssh-key", str(ssh_dir / "id_ed25519"),
                    "--ssh-host", "example.test", "--ssh-user", "deploy",
                ]), 2)
            refusal_marker = json.loads(refusal_stdout.getvalue())
            self.assertEqual(refusal_marker["parent_phase"], "remote_plan_prepare")
            self.assertEqual(refusal_marker["parent_error_class"], "ValueError")
            self.assertEqual(refusal_marker["parent_return_code"], 2)
            self.assertEqual(refusal_marker["plan_prepare_status"], "refused")
            self.assertEqual(refusal_marker["plan_cleanup_status"], "not_attempted")
            self.assertEqual(
                refusal_marker["plan_prepare_refusal_class"], "ValueError",
            )
            self.assertEqual(refusal_marker["remote_plan_cleanup"], "not_attempted")
            self.assertFalse(refusal_marker["namespace_closed"])
            self.assertFalse(refusal_marker["diagnostic_credit"])
            refusal_fingerprint = json.loads(
                refusal_fingerprint_path.read_text(encoding="ascii")
            )
            self.assertEqual(refusal_fingerprint["parent_error_class"], "ValueError")
            self.assertEqual(refusal_fingerprint["parent_exit_code"], 2)
            self.assertEqual(
                refusal_fingerprint["parent_exit_code_basis"],
                "derived_from_passed_outcome",
            )
            self.assertFalse(refusal_fingerprint["passed"])
            self.assertFalse(refusal_fingerprint["diagnostic_credit"])
            observer_fingerprint = capacity_diagnostic._parent_failure_fingerprint(
                {
                    "status": "incomplete", "passed": False,
                    "namespace_closed": False, "runner_headroom": {
                        "status": "unknown", "reason": "runner_snapshot_unavailable",
                    },
                },
                args=Namespace(
                    source_sha="a" * 40, app_target_sha="b" * 40,
                    external_run_id="12345", external_run_attempt="1",
                    concurrency=8, workload=capacity_diagnostic.HTML_WORKLOAD,
                ),
                stage_state={
                    "memory_observer_failure": {
                        "failure_stage": "first_row_read",
                        "error_class": "TimeoutError",
                        "remote_failure_stage": "helper_spawn",
                        "remote_failure_reason": "spawn_error",
                    },
                },
            )
            self.assertEqual(observer_fingerprint["memory_observer_failure"], {
                "stage": "first_row_read", "error_class": "TimeoutError",
                "remote_stage": "helper_spawn", "remote_reason": "spawn_error",
            })
            self.assertFalse(observer_fingerprint["namespace_closed"])
            self.assertFalse(observer_fingerprint["diagnostic_credit"])
            self.assertNotIn("example.test", json.dumps(observer_fingerprint))
            self.assertNotIn("example.test", refusal_stdout.getvalue())
            self.assertNotIn(str(root), refusal_stdout.getvalue())
            self.assertEqual(capacity_diagnostic.evaluate_capacity_report(
                receipt, source_sha="a" * 40, app_target_sha="b" * 40,
                run_id="12345", attempt="1",
                manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            ), "INVALID")
            bad_profile_binding = {**receipt, "profile_digest": "0" * 64}
            self.assertIsNone(capacity_diagnostic._project_parent_failure_receipt(
                bad_profile_binding
            ))
            serialized = json.dumps(receipt, sort_keys=True)
            self.assertNotIn("example.test", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("argv", serialized)

            input_path = root / "input.json"
            output_path = root / "sanitized.json"
            capacity_diagnostic._write_report(input_path, receipt)
            with patch.object(
                capacity_diagnostic, "_read_manifest",
                return_value=(manifest_raw, manifest, actors),
            ):
                self.assertEqual(capacity_diagnostic.sanitize_capacity_report(
                    input_path=input_path, output_path=output_path,
                    manifest_path=manifest_path, source_sha="a" * 40,
                    app_target_sha="b" * 40, run_id="12345", attempt="1",
                ), "INVALID")
            public = json.loads(output_path.read_text(encoding="ascii"))
            self.assertEqual(public, receipt)
            self.assertEqual(stat.S_IMODE(output_path.stat().st_mode), 0o600)

            def fake_unknown_prepare(_args, _host, _user, _config, _key, payload, lifecycle, **_kwargs):
                self.assertEqual(payload["operation"], "prepare")
                lifecycle.update({
                    "closed": False,
                    "plan_prepare_status": "transport_failed",
                    "plan_prepare_refusal_class": "none",
                    "prepare_transport": {
                        "stage": "timeout", "return_code": None,
                        "stdout_shape": "none", "child_state": "unknown",
                    },
                })
                return False, None, None

            cli_output_path = root / "main-report.json"
            with (
                patch.object(capacity_diagnostic, "_read_manifest", return_value=(manifest_raw, manifest, actors)),
                patch.object(cpu_pair, "_ssh_plan", side_effect=fake_unknown_prepare) as unknown_ssh,
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "parent", "--manifest", str(manifest_path),
                    "--report", str(cli_output_path),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                    "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                    "--ssh-key", str(ssh_dir / "id_ed25519"),
                    "--ssh-host", "example.test", "--ssh-user", "deploy",
                ]), 2)
            unknown_ssh.assert_called_once()
            unknown_receipt = json.loads(cli_output_path.read_text(encoding="ascii"))
            self.assertEqual(stat.S_IMODE(cli_output_path.stat().st_mode), 0o600)
            self.assertEqual(unknown_receipt["plan_cleanup_status"], "not_attempted")
            self.assertEqual(unknown_receipt["remote_plan_cleanup"], "failed")
            self.assertEqual(unknown_receipt["profile_digest"], capacity_diagnostic.profile_digest(8))
            self.assertEqual(unknown_receipt["ssh_cleanup_transport"], {
                "stage": "not_started", "return_code": None,
                "stdout_shape": "none", "child_state": "unknown",
            })
            self.assertFalse(unknown_receipt["namespace_closed"])
            self.assertEqual(unknown_receipt["stage"], "remote_plan_prepare")

            early_report_path = root / "manifest-read-report.json"
            early_stdout = io.StringIO()
            with (
                patch.object(
                    capacity_diagnostic, "_read_manifest",
                    side_effect=ValueError("private manifest parse detail"),
                ),
                redirect_stdout(early_stdout),
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "parent", "--manifest", str(manifest_path),
                    "--report", str(early_report_path),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                    "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                    "--ssh-key", str(ssh_dir / "id_ed25519"),
                    "--ssh-host", "example.test", "--ssh-user", "deploy",
                ]), 2)
            early_receipt = json.loads(early_report_path.read_text(encoding="ascii"))
            self.assertEqual(early_receipt["stage"], "manifest_read")
            self.assertEqual(early_receipt["error_class"], "ValueError")
            self.assertIsNone(early_receipt["fixture_manifest_sha256"])
            self.assertNotIn("private manifest parse detail", early_stdout.getvalue())
            self.assertNotIn(str(root), early_report_path.read_text(encoding="ascii"))
            sanitized_early = root / "manifest-read-sanitized.json"
            with patch.object(
                capacity_diagnostic, "_read_manifest",
                return_value=(manifest_raw, manifest, actors),
            ):
                self.assertEqual(capacity_diagnostic.sanitize_capacity_report(
                    input_path=early_report_path, output_path=sanitized_early,
                    manifest_path=manifest_path, source_sha="a" * 40,
                    app_target_sha="b" * 40, run_id="12345", attempt="1",
                ), "INVALID")
            sanitized_receipt = json.loads(sanitized_early.read_text(encoding="ascii"))
            self.assertEqual(
                sanitized_receipt["fixture_manifest_sha256"],
                hashlib.sha256(manifest_raw).hexdigest(),
            )

            worker_report_path = root / "worker-failure-report.json"
            prepared_run_ids: list[str] = []

            def fake_prepare_plan(*, lifecycle, run_id, **_kwargs):
                prepared_run_ids.append(run_id)
                lifecycle.update({
                    "closed": True, "plan_prepare_status": "prepared",
                    "plan_prepare_refusal_class": "none",
                    "prepare_transport": {
                        "stage": "complete", "return_code": 0,
                        "stdout_shape": "prepared_marker", "child_state": "reaped",
                    },
                })
                return ({
                    "release_slug": "release-test", "off_start_ms": 100_000,
                    "off_end_ms": 120_000, "on_start_ms": 125_000,
                    "on_end_ms": 145_000,
                }, lifecycle)

            def fake_cleanup(_args, _host, _user, _config, _key, payload, lifecycle, **_kwargs):
                self.assertEqual(payload["operation"], "cleanup")
                lifecycle.update({
                    "plan_cleanup_status": "complete",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "complete", "return_code": 0,
                        "stdout_shape": "summary_marker", "child_state": "reaped",
                    },
                })
                return True, None, {"service_count": 0}

            worker_stdout = io.StringIO()
            with (
                patch.object(capacity_diagnostic, "_read_manifest", return_value=(manifest_raw, manifest, actors)),
                patch.object(capacity_diagnostic, "_prepare_remote_capacity_plan", side_effect=fake_prepare_plan),
                patch.object(capacity_diagnostic, "run_isolated_capacity_worker", side_effect=RuntimeError("private worker detail")),
                patch.object(cpu_pair, "_ssh_plan", side_effect=fake_cleanup) as cleanup_ssh,
                redirect_stdout(worker_stdout),
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "parent", "--manifest", str(manifest_path),
                    "--report", str(worker_report_path),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                    "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                    "--ssh-key", str(ssh_dir / "id_ed25519"),
                    "--ssh-host", "example.test", "--ssh-user", "deploy",
                ]), 2)
            worker_failure = json.loads(worker_report_path.read_text(encoding="ascii"))
            self.assertEqual(worker_failure["stage"], "worker_supervision")
            self.assertEqual(worker_failure["error_class"], "RuntimeError")
            self.assertEqual(worker_failure["plan_cleanup_status"], "not_attempted")
            self.assertEqual(worker_failure["remote_plan_cleanup"], "failed")
            self.assertFalse(worker_failure["namespace_closed"])
            cleanup_ssh.assert_not_called()
            self.assertNotIn("private worker detail", worker_stdout.getvalue())
            self.assertNotIn(str(root), worker_report_path.read_text(encoding="ascii"))

            events: list[tuple[str, int | None]] = []

            def fake_closed_worker(**_kwargs):
                return {
                    "namespace_closed": True,
                    "worker": {}, "runner_headroom": {},
                }

            def fake_wait(expiry_ms: int, **_clock_anchor) -> None:
                events.append(("wait", expiry_ms))

            def fake_exact_cleanup(*, run_id, lifecycle, **_kwargs):
                self.assertEqual(run_id, prepared_run_ids[-1])
                events.append(("cleanup", None))
                lifecycle.update({
                    "plan_cleanup_status": "complete",
                    "plan_cleanup_refusal_class": "none",
                    "cleanup_transport": {
                        "stage": "complete", "return_code": 0,
                        "stdout_shape": "summary_marker", "child_state": "reaped",
                    },
                })
                return True, {}

            closed_args = Namespace(**vars(args))
            with (
                patch.object(capacity_diagnostic, "_read_manifest", return_value=(manifest_raw, manifest, actors)),
                patch.object(capacity_diagnostic, "_prepare_remote_capacity_plan", side_effect=fake_prepare_plan),
                patch.object(capacity_diagnostic, "run_isolated_capacity_worker", side_effect=fake_closed_worker),
                patch.object(capacity_diagnostic, "_wait_for_remote_plan_expiry", side_effect=fake_wait),
                patch.object(capacity_diagnostic, "_cleanup_remote_capacity_plan", side_effect=fake_exact_cleanup) as exact_cleanup,
            ):
                closed_outcome = capacity_diagnostic._parent_run(closed_args)
            self.assertEqual(events, [("wait", 145_000), ("cleanup", None)])
            exact_cleanup.assert_called_once()
            self.assertTrue(closed_outcome["namespace_closed"])
            self.assertEqual(closed_outcome["remote_plan_cleanup"], "complete")

            fake_monotonic = [50.0]

            def advance(seconds: float) -> None:
                fake_monotonic[0] += seconds

            with (
                patch.object(capacity_diagnostic.time, "time_ns", return_value=9_999_999_999_000_000) as wall_clock,
                patch.object(capacity_diagnostic.time, "monotonic", side_effect=lambda: fake_monotonic[0]),
                patch.object(capacity_diagnostic.time, "sleep", side_effect=advance) as fake_sleep,
            ):
                capacity_diagnostic._wait_for_remote_plan_expiry(
                    1_097_000, plan_created_at_ms=1_000_000,
                    plan_created_monotonic=50.0,
                )
            self.assertLessEqual(sum(call.args[0] for call in fake_sleep.call_args_list), 106.1)
            wall_clock.assert_not_called()

            simulated_wait = sum(call.args[0] for call in fake_sleep.call_args_list)
            self.assertLessEqual(simulated_wait, capacity_diagnostic.PLAN_EXPIRY_WAIT_LIMIT_MS / 1000)

            fake_monotonic[0] = 157.0
            with (
                patch.object(capacity_diagnostic.time, "time_ns", return_value=0) as wall_clock,
                patch.object(
                    capacity_diagnostic.time, "monotonic",
                    side_effect=lambda: fake_monotonic[0],
                ),
                patch.object(capacity_diagnostic.time, "sleep") as no_sleep,
            ):
                capacity_diagnostic._wait_for_remote_plan_expiry(
                    1_097_000, plan_created_at_ms=1_000_000,
                    plan_created_monotonic=50.0,
                )
            no_sleep.assert_not_called()
            wall_clock.assert_not_called()

            producer_report = root / "namespace-open-parent-report.json"
            sanitized_report = root / "namespace-open-sanitized.json"
            with (
                patch.object(capacity_diagnostic, "_read_manifest", return_value=(manifest_raw, manifest, actors)),
                patch.object(capacity_diagnostic, "_prepare_remote_capacity_plan", side_effect=fake_prepare_plan),
                patch.object(
                    capacity_diagnostic.load_runtime, "run_supervised",
                    return_value=SimpleNamespace(
                        namespace_closed=False, reason="namespace_unavailable",
                        worker_started=False, worker_exited=False,
                        returncode=None, signal=None,
                        startup_failure_stage="unknown",
                        namespace_ready_failure_stage="namespace_config_read",
                        namespace_post_ready_failure_stage=None,
                        watchdog_startup_failure_stage="watchdog_supervision",
                        report={
                            "runtime_supervisor": {
                                "reason": "namespace_unavailable",
                                "report_error": "NamespaceCapabilityError",
                            },
                            "runtime_budget": {"phase": "namespace_probe"},
                        },
                    ),
                ),
                patch.object(capacity_diagnostic, "_cleanup_remote_capacity_plan") as no_namespace_cleanup,
            ):
                stage_state: dict[str, object] = {}
                composed = capacity_diagnostic._parent_run(
                    Namespace(**vars(args)), stage_state,
                )
                preflight_stdout = io.StringIO()
                with redirect_stdout(preflight_stdout):
                    self.assertEqual(capacity_diagnostic.main([
                        "parent", "--manifest", str(manifest_path),
                        "--report", str(root / "namespace-preflight-cli.json"),
                        "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                        "--external-run-id", "12345", "--external-run-attempt", "1",
                        "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                        "--ssh-key", str(ssh_dir / "id_ed25519"),
                        "--ssh-host", "example.test", "--ssh-user", "deploy",
                    ]), 2)
            no_namespace_cleanup.assert_not_called()
            preflight_marker = json.loads(preflight_stdout.getvalue())
            self.assertEqual(preflight_marker["parent_phase"], "worker_supervision")
            self.assertEqual(preflight_marker["supervisor_report_phase"], "namespace_probe")
            self.assertEqual(
                preflight_marker["supervisor_report_error_class"],
                "NamespaceCapabilityError",
            )
            self.assertIsNone(preflight_marker["supervisor_return_code"])
            self.assertEqual(preflight_marker["supervisor_reason"], "namespace_unavailable")
            self.assertFalse(preflight_marker["supervisor_process_started"])
            self.assertFalse(preflight_marker["supervisor_namespace_closed"])
            self.assertEqual(preflight_marker["remote_plan_cleanup"], "failed")
            self.assertEqual(preflight_marker["plan_cleanup_status"], "not_attempted")
            self.assertFalse(preflight_marker["matrix_credit"])
            self.assertFalse(preflight_marker["slo_credit"])
            self.assertFalse(preflight_marker["diagnostic_credit"])
            self.assertNotIn("example.test", preflight_stdout.getvalue())
            self.assertNotIn(str(root), preflight_stdout.getvalue())
            self.assertFalse(composed["namespace_closed"])
            self.assertFalse(composed["passed"])
            self.assertEqual(composed["remote_plan_cleanup"], "failed")
            self.assertEqual(composed["runner_headroom"]["status"], "unknown")
            self.assertEqual(composed["runner_headroom"]["reason"], "runner_context_unavailable")
            self.assertEqual(set(composed["runner_headroom"]["identity"]), set(
                external_load.RunnerCgroupSampler.IDENTITY_FIELDS
            ))
            self.assertFalse(any(composed["runner_headroom"]["identity"].values()))
            self.assertEqual(composed["memory_guard"]["memory_events_sampled_max_delta"], None)
            self.assertEqual(composed["server_telemetry"]["node_memory_reason"], "unavailable")
            self.assertIsNotNone(capacity_diagnostic._project_capacity_report(composed))
            self.assertEqual(stage_state["supervisor_evidence"], {
                "reason": "namespace_unavailable", "worker_started": False,
                "worker_exited": False, "namespace_closed": False,
                "return_code": None, "signal": None,
                "startup_failure_stage": "unknown",
                "namespace_ready_failure_stage": "namespace_config_read",
                "namespace_post_ready_failure_stage": None,
                "watchdog_startup_failure_stage": "watchdog_supervision",
                "report_error_class": "NamespaceCapabilityError",
                "report_phase": "namespace_probe",
            })
            preinit_fingerprint = capacity_diagnostic._parent_outcome_fingerprint(
                composed, stage_state["supervisor_evidence"],
                parent_phase=stage_state["stage"],
                prepare_lifecycle=stage_state["prepare_lifecycle"],
                cleanup_lifecycle=stage_state["cleanup_lifecycle"],
            )
            started_fingerprint = capacity_diagnostic._parent_outcome_fingerprint(
                composed, {
                    "reason": "namespace_unclosed", "worker_started": True,
                    "worker_exited": True, "namespace_closed": False,
                    "return_code": 2, "signal": None,
                },
            )
            self.assertFalse(preinit_fingerprint["supervisor_process_started"])
            self.assertEqual(preinit_fingerprint["parent_phase"], "worker_supervision")
            self.assertEqual(
                preinit_fingerprint["supervisor_report_phase"], "namespace_probe",
            )
            self.assertEqual(
                preinit_fingerprint["supervisor_report_error_class"],
                "NamespaceCapabilityError",
            )
            self.assertIsNone(preinit_fingerprint["supervisor_return_code"])
            self.assertEqual(preinit_fingerprint["plan_cleanup_status"], "not_attempted")
            self.assertEqual(preinit_fingerprint["remote_plan_cleanup"], "failed")
            self.assertEqual(
                preinit_fingerprint["supervisor_startup_failure_stage"], "unknown",
            )
            self.assertEqual(
                preinit_fingerprint["supervisor_namespace_ready_failure_stage"],
                "namespace_config_read",
            )
            self.assertEqual(
                preinit_fingerprint["supervisor_watchdog_startup_failure_stage"],
                "watchdog_supervision",
            )
            self.assertTrue(started_fingerprint["supervisor_process_started"])
            self.assertEqual(started_fingerprint["supervisor_reason"], "namespace_unclosed")
            self.assertNotEqual(preinit_fingerprint, started_fingerprint)
            self.assertEqual(capacity_diagnostic._project_supervisor_evidence({
                "reason": "supervisor_error",
            })["reason"], "supervisor_error")
            fingerprint_text = json.dumps(preinit_fingerprint, sort_keys=True)
            self.assertNotIn("private worker detail", fingerprint_text)
            self.assertNotIn(str(root), fingerprint_text)

            parent_cli_report = root / "parent-cli-report.json"
            parent_cli_stdout = io.StringIO()
            with (
                patch.object(
                    capacity_diagnostic, "_read_manifest",
                    return_value=(manifest_raw, manifest, actors),
                ),
                patch.object(capacity_diagnostic, "_prepare_remote_capacity_plan", side_effect=fake_prepare_plan),
                patch.object(
                    capacity_diagnostic.load_runtime, "run_supervised",
                    return_value=SimpleNamespace(
                        namespace_closed=False, reason="namespace_unclosed",
                        worker_started=True, worker_exited=True,
                        returncode=7, signal=None,
                        startup_failure_stage="namespace_config_read",
                        namespace_ready_failure_stage="namespace_config_read",
                        namespace_post_ready_failure_stage=None,
                        watchdog_startup_failure_stage=None,
                    ),
                ),
                redirect_stdout(parent_cli_stdout),
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "parent", "--manifest", str(manifest_path),
                    "--report", str(parent_cli_report),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                    "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                    "--ssh-key", str(ssh_dir / "id_ed25519"),
                    "--ssh-host", "example.test", "--ssh-user", "deploy",
                ]), 2)
            parent_cli_marker = json.loads(parent_cli_stdout.getvalue())
            self.assertEqual(parent_cli_marker["supervisor_reason"], "namespace_unclosed")
            self.assertTrue(parent_cli_marker["supervisor_process_started"])
            self.assertTrue(parent_cli_marker["supervisor_process_exited"])
            self.assertEqual(parent_cli_marker["supervisor_return_code"], 7)
            self.assertFalse(parent_cli_marker["supervisor_namespace_closed"])
            self.assertEqual(parent_cli_marker["supervisor_startup_failure_stage"], "namespace_config_read")
            self.assertEqual(
                parent_cli_marker["supervisor_namespace_ready_failure_stage"],
                "namespace_config_read",
            )
            self.assertIsNone(
                parent_cli_marker["supervisor_namespace_post_ready_failure_stage"],
            )
            self.assertIsNone(
                parent_cli_marker["supervisor_watchdog_startup_failure_stage"],
            )
            self.assertNotIn("supervisor_worker_started", parent_cli_marker)
            self.assertNotIn(str(root), parent_cli_stdout.getvalue())
            self.assertNotIn("example.test", parent_cli_stdout.getvalue())
            self.assertNotIn("id_ed25519", parent_cli_stdout.getvalue())
            self.assertNotIn("private worker detail", parent_cli_stdout.getvalue())

            def fail_after_supervisor(*, stage_state, **_kwargs):
                stage_state["supervisor_evidence"] = {
                    "reason": "worker_failed", "worker_started": True,
                    "worker_exited": True, "namespace_closed": False,
                    "return_code": 3, "signal": None,
                }
                raise RuntimeError("private worker detail")

            exception_report = root / "exception-report.json"
            exception_stdout = io.StringIO()
            with (
                patch.object(
                    capacity_diagnostic, "_read_manifest",
                    return_value=(manifest_raw, manifest, actors),
                ),
                patch.object(capacity_diagnostic, "_prepare_remote_capacity_plan", side_effect=fake_prepare_plan),
                patch.object(capacity_diagnostic, "run_isolated_capacity_worker", side_effect=fail_after_supervisor),
                redirect_stdout(exception_stdout),
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "parent", "--manifest", str(manifest_path),
                    "--report", str(exception_report),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                    "--concurrency", "8", "--ssh-config", str(ssh_dir / "config"),
                    "--ssh-key", str(ssh_dir / "id_ed25519"),
                    "--ssh-host", "example.test", "--ssh-user", "deploy",
                ]), 2)
            exception_marker = json.loads(exception_stdout.getvalue())
            self.assertEqual(exception_marker["supervisor_reason"], "worker_failed")
            self.assertEqual(exception_marker["supervisor_return_code"], 3)
            self.assertNotIn("private worker detail", exception_stdout.getvalue())
            self.assertNotIn(str(root), exception_stdout.getvalue())

            capacity_diagnostic._write_report(producer_report, composed)
            with patch.object(
                capacity_diagnostic, "_read_manifest",
                return_value=(manifest_raw, manifest, actors),
            ):
                decision = capacity_diagnostic.sanitize_capacity_report(
                    input_path=producer_report, output_path=sanitized_report,
                    manifest_path=manifest_path, source_sha="a" * 40,
                    app_target_sha="b" * 40, run_id="12345", attempt="1",
                )
            self.assertEqual(decision, "INVALID")
            closed_failure = json.loads(sanitized_report.read_text(encoding="ascii"))
            self.assertFalse(closed_failure["passed"])
            self.assertFalse(closed_failure["namespace_closed"])
            self.assertFalse(closed_failure["matrix_credit"])
            self.assertFalse(closed_failure["slo_credit"])
            self.assertFalse(closed_failure["diagnostic_credit"])

            rejected_report = root / "rejected-report.json"
            rejected_output = root / "rejected-public.json"
            malformed = dict(composed)
            malformed["runner_headroom"] = {
                **composed["runner_headroom"], "identity": {},
            }
            capacity_diagnostic._write_report(rejected_report, malformed)
            rejection_stdout = io.StringIO()
            with (
                patch.object(
                    capacity_diagnostic, "_read_manifest",
                    return_value=(manifest_raw, manifest, actors),
                ),
                redirect_stdout(rejection_stdout),
            ):
                self.assertEqual(capacity_diagnostic.main([
                    "sanitize-report", "--input", str(rejected_report),
                    "--output", str(rejected_output), "--manifest", str(manifest_path),
                    "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                    "--external-run-id", "12345", "--external-run-attempt", "1",
                ]), 2)
            rejection = json.loads(rejection_stdout.getvalue())
            self.assertEqual(rejection, {
                "status": "invalid", "stage": "report_projection",
                "reason": "runner_identity_shape", "section": "runner_headroom",
                "field": "identity", "present": True, "value_type": "object",
                "binding_status": "unknown", "matrix_credit": False,
                "slo_credit": False, "diagnostic_credit": False,
            })
            self.assertFalse(rejected_output.exists())
            self.assertNotIn(str(root), rejection_stdout.getvalue())
            self.assertNotIn("example.test", rejection_stdout.getvalue())
            self.assertNotIn("id_ed25519", rejection_stdout.getvalue())

            parent_fingerprint = capacity_diagnostic._parent_outcome_fingerprint(
                composed,
                prepare_lifecycle=stage_state["prepare_lifecycle"],
                cleanup_lifecycle=stage_state["cleanup_lifecycle"],
            )
            self.assertEqual(parent_fingerprint["stage"], "parent_outcome")
            self.assertFalse(parent_fingerprint["namespace_closed"])
            self.assertFalse(parent_fingerprint["worker_present"])
            self.assertEqual(parent_fingerprint["runner_context_reason"], "runner_context_unavailable")
            self.assertEqual(parent_fingerprint["remote_plan_cleanup"], "failed")
            self.assertEqual(parent_fingerprint["plan_cleanup_status"], "not_attempted")

    def test_ssr_capacity_evaluator_requires_complete_bound_zero_credit_evidence(self) -> None:
        report = {
            "schema": 1, "report_complete": True,
            "workload": capacity_diagnostic.WORKLOAD,
            "fixture_kind": capacity_diagnostic.FIXTURE_KIND,
            "status": "complete", "passed": True,
            "matrix_credit": False, "slo_credit": False, "diagnostic_credit": False,
            "source_sha": "b" * 40, "app_target_sha": "c" * 40,
            "external_run_id": "12345", "external_run_attempt": "1",
            "run_id": "a" * 32, "profile_digest": capacity_diagnostic.profile_digest(8),
            "fixture_manifest_sha256": "e" * 64,
            "fixture_manifest_bytes": 2048,
            "fixture_tournaments": 1, "fixture_users": 14, "actors": 8,
            "concurrency": 8, "max_inflight": 8,
            "load_model": "closed_loop_best_effort", "window_ms": 20_000,
            "namespace_closed": True,
            "worker": {
                "status": "complete", "submitted": 40, "completed": 40,
                "max_inflight": 8, "fixture_manifest_bytes": 2048,
                "response_bytes_total": 8192,
                "http_status_counts": {"200": 40}, "error_counts": {},
                "latency_ms": {"sample_count": 40, "p50": 10.0, "p95": 20.0, "p99": 25.0, "max": 30.0},
                "resource_abort_reason": None,
                "on_window_start_ms": 100_000, "on_window_end_ms": 120_000,
                "observed_duration_ms": 20_000,
                "off_probe": {"submitted": 8, "completed": 8, "http_status": 200},
                "process_cpu_windows": [
                    {"window": name, "status": "complete", "cpu_ns": 4_000_000_000,
                     "elapsed_ns": 20_000_000_000, "cores": 0.2}
                    for name in ("off", "on")
                ],
            },
            "memory_guard": {
                "status": "complete", "samples": 250,
                "current_min_bytes": 300_000_000, "current_max_bytes": 400_000_000,
                "memory_peak_lifetime_max_bytes": 500_000_000,
                "memory_events_sampled_max_delta": {key: 0 for key in (
                    "low", "high", "max", "oom", "oom_kill", "oom_group_kill",
                )},
                "target_identity_status": "complete", "terminal_decision": "complete",
                "terminal_reason": "none", "ssh_material_restored": True,
            },
            "runner_headroom": {
                "schema": 1, "status": "complete", "reason": "none",
                "runner_environment": "github-hosted",
                "identity": {key: True for key in (
                    "outer_pid_namespace_matches_pid1", "outer_mount_namespace_matches_pid1",
                    "outer_cgroup_namespace_matches_pid1", "target_cgroup_namespace_matches_outer",
                    "target_mount_namespace_is_isolated", "target_pid_namespace_is_isolated",
                    "global_cgroup2_mount", "workload_membership_matches_outer",
                )},
                "sample_count": 10, "window_seconds": 20.0,
                "effective_cpu_capacity_cores": 4.0,
                "max_cgroup_cpu_usage_cores": 1.2,
                "max_visible_throttled_periods_delta": 0,
                "window_scope": "on_window",
                "window_start_offset_ms": 0,
                "window_end_offset_ms": 20_000,
            },
            "server_telemetry": {
                "schema": 1, "cpu_status": "complete", "cpu_reason": "none",
                "cpu_rows": [
                    {
                        "service": service, "phase": phase,
                        "expected_targets": targets, "observed_targets": targets,
                        "event_count": targets, "cpu_ns": 1_000_000,
                        "window_ms_min": 20_000, "window_ms_max": 20_000,
                        "start_lag_ms_min": 0, "start_lag_ms_max": 0,
                        "end_lag_ms_min": 0, "end_lag_ms_max": 0,
                        "timing_complete": True,
                    }
                    for service, phase, targets in (
                        ("api", "off", 2), ("api", "on", 2),
                        ("web", "off", 1), ("web", "on", 1),
                    )
                ],
                "node_memory_status": "complete", "node_memory_reason": "none",
                "cgroup_memory_status": "complete", "cgroup_memory_reason": "none",
                "node_memory_rows": [
                    {
                        "service": "web", "phase": phase, "status": "complete",
                        "sample_count": 5, "rss_min_bytes": 1, "rss_max_bytes": 2,
                        "rss_mean_bytes": 1, "heap_total_min_bytes": 1,
                        "heap_total_max_bytes": 2, "heap_total_mean_bytes": 1,
                        "heap_used_min_bytes": 1, "heap_used_max_bytes": 2,
                        "heap_used_mean_bytes": 1, "heap_limit_min_bytes": 1,
                        "heap_limit_max_bytes": 2, "heap_limit_mean_bytes": 1,
                        "external_min_bytes": 1, "external_max_bytes": 2,
                        "external_mean_bytes": 1, "array_buffers_min_bytes": 1,
                        "array_buffers_max_bytes": 2, "array_buffers_mean_bytes": 1,
                        "sampled_window_start_elapsed_ms": 0,
                        "sampled_window_end_elapsed_ms": 16_000,
                        "sampled_window_duration_ms": 16_000,
                        "cgroup_sample_count": 5,
                        "cgroup_memory_current_start_bytes": 1,
                        "cgroup_memory_current_end_bytes": 1,
                        "cgroup_memory_current_sampled_max_bytes": 1,
                        "cgroup_memory_peak_start_bytes": 1,
                        "cgroup_memory_peak_end_bytes": 1,
                        "cgroup_memory_peak_sampled_max_bytes": 1,
                        "memory_events_low_sampled_delta": 0,
                        "memory_events_high_sampled_delta": 0,
                        "memory_events_max_sampled_delta": 0,
                        "memory_events_oom_sampled_delta": 0,
                        "memory_events_oom_kill_sampled_delta": 0,
                        "memory_events_oom_group_kill_sampled_delta": 0,
                        "gc_count": 0, "gc_duration_ms": 0, "gc_major_count": 0,
                        "gc_minor_count": 0, "gc_incremental_count": 0,
                        "gc_weakcb_count": 0, "gc_other_count": 0,
                        "gc_status": "complete", "cgroup_memory_status": "complete",
                        "cgroup_memory_reason": "none", "gc_overflow": False,
                    }
                    for phase in ("off", "on")
                ],
            },
            "server_capacity_claim": None,
            "server_capacity_claim_reason": "resource_correlation_not_provided",
            "generator_cpu_evidence": "measured",
            "remote_plan_cleanup": "complete",
        }
        self.assertIsNotNone(capacity_diagnostic._project_capacity_report(report))
        self.assertEqual(capacity_diagnostic.evaluate_capacity_report(
            report, source_sha="b" * 40, app_target_sha="c" * 40,
            run_id="12345", attempt="1", manifest_sha256="e" * 64,
        ), "PASS")
        complete_without_full_probe = json.loads(json.dumps(report))
        complete_without_full_probe["worker"]["off_probe"] = {
            "submitted": 0, "completed": 0, "http_status": None,
        }
        self.assertIsNone(capacity_diagnostic._project_capacity_report(
            complete_without_full_probe,
        ))
        html_report = json.loads(json.dumps(report))
        manifest_raw = json.dumps({
            "runner_sha": "b" * 40, "app_target_sha": "c" * 40,
        }, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        html_report.update({
            "workload": capacity_diagnostic.HTML_WORKLOAD,
            "fixture_kind": capacity_diagnostic.HTML_FIXTURE_KIND,
            "profile_digest": capacity_diagnostic.profile_digest(
                8, capacity_diagnostic.HTML_WORKLOAD,
            ),
            "fixture_manifest_sha256": manifest_sha,
            "fixture_manifest_bytes": len(manifest_raw),
            "actors": 14,
        })
        html_report["worker"]["fixture_manifest_bytes"] = len(manifest_raw)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "html-report.json"
            output_path = root / "html-sanitized.json"
            manifest_path = root / "manifest.json"
            capacity_diagnostic._write_report(input_path, html_report)
            manifest_path.write_bytes(manifest_raw)
            with patch.object(
                capacity_diagnostic, "_read_manifest",
                return_value=(manifest_raw, {
                    "runner_sha": "b" * 40, "app_target_sha": "c" * 40,
                }, []),
            ):
                self.assertEqual(capacity_diagnostic.sanitize_capacity_report(
                    input_path=input_path, output_path=output_path,
                    manifest_path=manifest_path, source_sha="b" * 40,
                    app_target_sha="c" * 40, run_id="12345", attempt="1",
                    workload=capacity_diagnostic.HTML_WORKLOAD,
                ), "PASS")
        incomplete_window_report = json.loads(json.dumps(report))
        incomplete_window_report["worker"]["process_cpu_windows"][1] = {
            "window": "on", "status": "incomplete", "cpu_ns": None,
            "elapsed_ns": None, "cores": None,
        }
        incomplete_window_report["generator_cpu_evidence"] = "incomplete"
        self.assertEqual(capacity_diagnostic.evaluate_capacity_report(
            incomplete_window_report, source_sha="b" * 40, app_target_sha="c" * 40,
            run_id="12345", attempt="1", manifest_sha256="e" * 64,
        ), "PASS")
        self.assertEqual(capacity_diagnostic.evaluate_capacity_report(
            {**report, "matrix_credit": True}, source_sha="b" * 40,
            app_target_sha="c" * 40, run_id="12345", attempt="1",
            manifest_sha256="e" * 64,
        ), "INVALID")
        self.assertEqual(capacity_diagnostic.evaluate_capacity_report(
            {**report, "status": "resource_abort", "passed": False},
            source_sha="b" * 40, app_target_sha="c" * 40,
            run_id="12345", attempt="1", manifest_sha256="e" * 64,
        ), "FAIL")
        missing_api_target = json.loads(json.dumps(report))
        api_off = next(
            row for row in missing_api_target["server_telemetry"]["cpu_rows"]
            if row["service"] == "api" and row["phase"] == "off"
        )
        api_off["observed_targets"] = 1
        api_off["event_count"] = 1
        self.assertEqual(capacity_diagnostic.evaluate_capacity_report(
            missing_api_target, source_sha="b" * 40, app_target_sha="c" * 40,
            run_id="12345", attempt="1", manifest_sha256="e" * 64,
        ), "INVALID")

    def test_parent_html_incomplete_report_reaches_sanitizer_cli(self) -> None:
        source_sha = "a" * 40
        runner_identity_fields = external_load.RunnerCgroupSampler.IDENTITY_FIELDS
        manifest = {
            "schema": 2,
            "purpose": "external_ready_vote",
            "origin": "https://old-sparky.com",
            "session_cookie_name": "session",
            "csrf_cookie_name": "csrf",
            "marker": "preprod261025120154ed2c",
            "created_at": "2026-10-25T12:00:00Z",
            "tournaments": [{"id": "fixture-0001", "slug": "ssr-fixture-0001", "user_count": 14}],
            "users": [
                {
                    "user_id": f"fixture-user-{index:04d}",
                    "tournament_slug": "ssr-fixture-0001",
                    "session_token": f"{index:02d}" + "s" * 62,
                    "csrf_token": f"{index:02d}" + "c" * 62,
                }
                for index in range(14)
            ],
            "runner_sha": source_sha,
            "app_target_sha": source_sha,
            "source_binding": None,
            "source_binding_sha256": None,
        }

        class FakeRunner:
            def __init__(self, **_kwargs):
                pass

            def start(self, _pid, _starttime):
                pass

            def stop(self, **_kwargs):
                return {
                    "schema": 1, "status": "unknown",
                    "reason": "runner_snapshot_unavailable",
                    "runner_environment": "github-hosted",
                    "identity": {key: False for key in runner_identity_fields},
                    "sample_count": 0, "window_scope": "on_window",
                    "window_seconds": None, "effective_cpu_capacity_cores": None,
                    "max_cgroup_cpu_usage_cores": None,
                    "max_visible_throttled_periods_delta": None,
                    "window_start_offset_ms": None, "window_end_offset_ms": None,
                }

        class FakeWatcher:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def fail_closed(self, *_args, **_kwargs):
                pass

            def stop(self):
                return {
                    "status": "incomplete", "samples": 0,
                    "current_min_bytes": None, "current_max_bytes": None,
                    "memory_peak_lifetime_max_bytes": None,
                    "memory_events_sampled_max_delta": None,
                    "target_identity_status": "unknown", "terminal_decision": None,
                    "terminal_reason": "observer_failed", "ssh_material_restored": True,
                }

        def fake_supervised(
            *, report_path, worker_report_path, worker_config,
            on_namespace_init, on_namespace_exit, **_kwargs,
        ):
            capacity = worker_config["capacity_diagnostic"]
            binding = capacity["binding"]
            config_path = Path(worker_report_path).with_name("worker-config.json")
            config = {**worker_config, "worker_report_path": str(worker_report_path)}
            capacity_diagnostic.load_runtime._write_json_atomic(config_path, config)
            pid, starttime = os.getpid(), 1
            on_namespace_init(pid, starttime)
            capacity_control.write_control(Path(capacity["control_path"]), {
                "schema": 1, **binding, "decision": "abort",
                "reason": "observer_failed", "sequence": 0,
                "updated_monotonic_ns": time.monotonic_ns(),
            }, expected=binding)
            with patch.dict(os.environ, {
                "SOURCE_GIT_SHA": binding["source_sha"],
                "APP_TARGET_SHA": binding["app_target_sha"],
                "SOURCE_BINDING_BASE64": "",
                "SOURCE_BINDING_SHA256": "",
            }):
                child_rc = capacity_diagnostic.load_runtime.worker_entry(
                    capacity_diagnostic._worker_callback, config_path=config_path,
                )
            on_namespace_exit(pid, starttime)
            child_payload, read_error = capacity_diagnostic.load_runtime._read_closed_report(
                Path(worker_report_path),
            )
            self.assertIsNone(read_error)
            final_payload = capacity_diagnostic.load_runtime._close_reaped_worker_report(
                child_payload, returncode=child_rc,
            )
            capacity_diagnostic.load_runtime._write_json_atomic(Path(report_path), final_payload)
            return SimpleNamespace(
                returncode=child_rc, report=final_payload,
                worker_started=True, worker_exited=True, killed=False, signal=None,
                reason="none", partial_work=True, inflight_unknown=False,
                descendants_reaped=True,
                isolation=capacity_diagnostic.load_runtime.PID_NAMESPACE_ISOLATION,
                namespace_closed=True, namespace_init_pid=pid,
                startup_failure_stage=None, namespace_ready_failure_stage=None,
                namespace_post_ready_failure_stage=None, watchdog_startup_failure_stage=None,
            )

        def fake_prepare(**kwargs):
            now_ms = time.time_ns() // 1_000_000
            off_start = now_ms + 52_000
            lifecycle = kwargs["lifecycle"]
            lifecycle.update({
                "closed": True, "plan_prepare_status": "prepared",
                "plan_prepare_refusal_class": "none",
                "prepare_transport": {
                    "stage": "complete", "return_code": 0,
                    "stdout_shape": "prepared_marker", "child_state": "reaped",
                },
            })
            return ({
                "off_start_ms": off_start, "off_end_ms": off_start + 20_000,
                "on_start_ms": off_start + 25_000, "on_end_ms": off_start + 45_000,
                "release_slug": "test-release",
            }, lifecycle)

        def fake_cleanup(**kwargs):
            kwargs["lifecycle"].update({
                "plan_cleanup_status": "complete", "plan_cleanup_refusal_class": "none",
                "cleanup_transport": {
                    "stage": "complete", "return_code": 0,
                    "stdout_shape": "summary_marker", "child_state": "reaped",
                },
            })
            return True, None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            manifest_path = root / "manifest.json"
            manifest_raw = json.dumps(
                manifest, sort_keys=True, separators=(",", ":"),
            ).encode("ascii") + b"\n"
            manifest_path.write_bytes(manifest_raw)
            manifest_path.chmod(0o600)
            ssh_dir = root / "external-ssh"
            ssh_dir.mkdir(mode=0o700)
            for name, value in (
                ("config", b"ssh config"),
                ("id_ed25519", b"private key"),
                ("known_hosts", b"host key"),
            ):
                path = ssh_dir / name
                path.write_bytes(value)
                path.chmod(0o600)
            report_path = root / "report.json"
            fingerprint_path = root / "failure-fingerprint.json"
            output_path = root / "ssr-capacity.json"
            args = [
                "parent", "--manifest", str(manifest_path), "--report", str(report_path),
                "--failure-report", str(fingerprint_path),
                "--source-sha", source_sha, "--app-target-sha", source_sha,
                "--external-run-id", "40191312869", "--external-run-attempt", "1",
                "--concurrency", "8", "--workload", capacity_diagnostic.HTML_WORKLOAD,
                "--ssh-config", str(ssh_dir / "config"),
                "--ssh-key", str(ssh_dir / "id_ed25519"),
                "--ssh-host", "example.invalid", "--ssh-user", "tester",
            ]
            with patch.dict(os.environ, {
                "SOURCE_GIT_SHA": source_sha, "APP_TARGET_SHA": source_sha,
                "SOURCE_BINDING_BASE64": "", "SOURCE_BINDING_SHA256": "",
            }), patch.object(
                capacity_diagnostic, "_prepare_remote_capacity_plan", side_effect=fake_prepare,
            ), patch.object(
                capacity_diagnostic, "_cleanup_remote_capacity_plan", side_effect=fake_cleanup,
            ), patch.object(
                capacity_diagnostic, "_wait_for_remote_plan_expiry", return_value=None,
            ), patch.object(
                capacity_diagnostic, "_RemoteMemoryWatcher", FakeWatcher,
            ), patch.object(
                capacity_diagnostic.external_load, "RunnerCgroupSampler", FakeRunner,
            ), patch.object(
                capacity_diagnostic.load_runtime, "run_supervised", side_effect=fake_supervised,
            ):
                self.assertEqual(capacity_diagnostic.main(args), 2)

            raw_report = json.loads(report_path.read_text(encoding="ascii"))
            self.assertEqual(raw_report["workload"], capacity_diagnostic.HTML_WORKLOAD)
            self.assertEqual(raw_report["worker"]["status"], "incomplete")
            self.assertEqual(raw_report["worker"]["resource_abort_reason"], "observer_failed")
            self.assertEqual(raw_report["worker"]["off_probe"], {
                "submitted": 0, "completed": 0, "http_status": None,
            })
            self.assertTrue(raw_report["namespace_closed"])
            self.assertEqual(raw_report["remote_plan_cleanup"], "complete")
            self.assertFalse(raw_report["diagnostic_credit"])

            with patch.dict(os.environ, {
                "SOURCE_GIT_SHA": source_sha, "APP_TARGET_SHA": source_sha,
                "SOURCE_BINDING_BASE64": "", "SOURCE_BINDING_SHA256": "",
            }):
                sanitizer = subprocess.run([
                    sys.executable, str(Path(capacity_diagnostic.__file__)),
                    "sanitize-report", "--input", str(report_path),
                    "--output", str(output_path), "--manifest", str(manifest_path),
                    "--source-sha", source_sha, "--app-target-sha", source_sha,
                    "--external-run-id", "40191312869", "--external-run-attempt", "1",
                    "--workload", capacity_diagnostic.HTML_WORKLOAD,
                ], capture_output=True, text=True, check=False)
            self.assertEqual(sanitizer.returncode, 0, sanitizer.stdout + sanitizer.stderr)
            self.assertEqual(sanitizer.stdout.strip(), "INVALID")
            sanitized_report = json.loads(output_path.read_text(encoding="ascii"))
            self.assertEqual(sanitized_report["status"], "incomplete")
            self.assertEqual(sanitized_report["worker"]["off_probe"], {
                "submitted": 0, "completed": 0, "http_status": None,
            })
            self.assertFalse(sanitized_report["passed"])
            self.assertFalse(sanitized_report["matrix_credit"])
            self.assertFalse(sanitized_report["slo_credit"])
            self.assertFalse(sanitized_report["diagnostic_credit"])

            fingerprint = json.loads(fingerprint_path.read_text(encoding="ascii"))
            self.assertEqual(fingerprint["event"], "ssr_capacity_parent_fingerprint")
            self.assertEqual(fingerprint["workload"], capacity_diagnostic.HTML_WORKLOAD)
            self.assertFalse(fingerprint["passed"])
            self.assertFalse(fingerprint["diagnostic_credit"])
            self.assertEqual(fingerprint["worker"]["reason"], "observer_failed")
            self.assertEqual(fingerprint["worker"]["submitted"], 0)
            self.assertEqual(fingerprint["worker"]["completed"], 0)
            self.assertEqual(fingerprint["runner"]["reason"], "runner_snapshot_unavailable")
            self.assertEqual(fingerprint["cleanup"]["transport"], {
                "stage": "complete", "return_code": 0,
                "stdout_shape": "summary_marker", "child_state": "reaped",
            })
            self.assertIsNone(capacity_diagnostic._project_capacity_report(fingerprint))
            fingerprint_text = json.dumps(fingerprint, sort_keys=True)
            self.assertNotIn("ssr-fixture-0001", fingerprint_text)
            self.assertNotIn("fixture-user-", fingerprint_text)
            self.assertNotIn("example.invalid", fingerprint_text)
            self.assertNotIn(str(root), fingerprint_text)

    def test_capacity_closed_loop_reuses_warmed_executor_and_never_zero_waits(self) -> None:
        local = threading.local()
        executor = ThreadPoolExecutor(max_workers=1)
        warmed_thread = executor.submit(
            lambda: (setattr(local, "http11_client", "warm"), threading.get_ident())[1]
        ).result(timeout=2)
        guard_calls = 0
        wait_timeouts: list[float] = []
        real_wait = external_load.wait

        def stop_after_submit() -> str | None:
            nonlocal guard_calls
            guard_calls += 1
            return None if guard_calls == 1 else "memory_floor"

        def request_builder(_origin, _actor, _phase, _timeout):
            self.assertEqual(getattr(local, "http11_client", None), "warm")
            time.sleep(0.08)
            return SimpleNamespace(
                ok=True, status=200, elapsed_ms=80.0, response_bytes=1, error_kind=None,
            )

        def observed_wait(*args, **kwargs):
            wait_timeouts.append(float(kwargs.get("timeout", args[1] if len(args) > 1 else 0)))
            return real_wait(*args, **kwargs)

        actor = external_load.VirtualUser("u", "t", "s" * 32, "c" * 32)
        now_ms = time.time_ns() // 1_000_000
        try:
            with patch.object(external_load, "wait", side_effect=observed_wait):
                result = external_load.run_authenticated_workspace_capacity_phase(
                    "http://127.0.0.1:3000", [actor],
                    phase="test_capacity", duration_seconds=20,
                    start_at_epoch_ms=now_ms, end_at_epoch_ms=now_ms + 20_000,
                    concurrency=1, timeout=1.0, request_builder=request_builder,
                    stop_requested=stop_after_submit, executor=executor,
                )
            self.assertTrue(result["aborted"])
            self.assertEqual(result["completed"], 1)
            self.assertTrue(wait_timeouts)
            self.assertTrue(all(timeout > 0 for timeout in wait_timeouts))
            self.assertEqual(executor.submit(threading.get_ident).result(timeout=2), warmed_thread)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    def test_ssr_capacity_parent_hides_and_restores_only_exact_ssh_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ssh_dir = Path(directory) / "external-ssh"
            ssh_dir.mkdir(mode=0o700)
            os.chmod(ssh_dir, 0o700)
            for name, contents in (
                ("config", b"Host *\nStrictHostKeyChecking yes\n"),
                ("id_ed25519", b"safe-test-key-material\n"),
                ("known_hosts", b"host ssh-ed25519 AAAA\n"),
            ):
                path = ssh_dir / name
                path.write_bytes(contents)
                os.chmod(path, 0o600)
            contents, lifecycle = capacity_diagnostic._hide_parent_ssh_material(ssh_dir)
            self.assertFalse(ssh_dir.exists())
            capacity_diagnostic._restore_parent_ssh_material(ssh_dir, contents, lifecycle)
            self.assertEqual(
                {path.name for path in ssh_dir.iterdir()},
                {"config", "id_ed25519", "known_hosts"},
            )
            for path in ssh_dir.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_ssr_capacity_parent_rejects_ssh_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ssh_dir = root / "external-ssh"
            ssh_dir.mkdir(mode=0o700)
            target = root / "target"
            target.write_bytes(b"unchanged")
            for name, contents in (
                ("id_ed25519", b"key\n"), ("known_hosts", b"host ssh-ed25519 AAAA\n"),
            ):
                path = ssh_dir / name
                path.write_bytes(contents)
                os.chmod(path, 0o600)
            (ssh_dir / "config").symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                capacity_diagnostic._hide_parent_ssh_material(ssh_dir)
            self.assertEqual(target.read_bytes(), b"unchanged")

    def test_node_memory_event_parser_is_closed_bounded_and_preserves_unknown_cgroup(self) -> None:
        fields = {
            "service": "web", "run_id": "a" * 32, "phase": "off", "sample_count": "5",
            "status": "complete", "cgroup_sample_count": "0",
            "gc_duration_ms": "1.250", "gc_status": "complete", "gc_overflow": "false",
        }
        for prefix in ("rss", "heap_total", "heap_used", "heap_limit", "external", "array_buffers"):
            for suffix in ("min", "max", "mean"):
                fields[f"{prefix}_{suffix}_bytes"] = "100"
        for key in (
            "cgroup_memory_current_start_bytes", "cgroup_memory_current_end_bytes",
            "cgroup_memory_current_sampled_max_bytes", "cgroup_memory_peak_start_bytes",
            "cgroup_memory_peak_end_bytes", "cgroup_memory_peak_sampled_max_bytes",
            "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
            "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
            "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
        ):
            fields[key] = "unknown"
        fields.update({
            "sampled_window_start_elapsed_ms": "0", "sampled_window_end_elapsed_ms": "16000",
            "sampled_window_duration_ms": "16000", "cgroup_memory_status": "unavailable",
            "cgroup_memory_reason": "hierarchy_unavailable",
        })
        for key in ("gc_count", "gc_major_count", "gc_minor_count", "gc_incremental_count", "gc_weakcb_count", "gc_other_count"):
            fields[key] = "0"
        message = "cpu_diagnostic_memory " + " ".join(f"{key}={value}" for key, value in fields.items())
        parsed = diagnostic_plan_tool._parse_node_memory_event(message)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNone(parsed["cgroup_memory_peak_end_bytes"])
        self.assertEqual(parsed["rss_max_bytes"], 100)
        self.assertIsNone(diagnostic_plan_tool._parse_node_memory_event(f"{message} token=private"))
        self.assertIsNone(diagnostic_plan_tool._parse_node_memory_event(message.replace("run_id=" + "a" * 32, "run_id=bad")))

    def test_cpu_only_evaluator_is_typed_zero_credit_and_fails_closed(self) -> None:
        protocol_fixture = Path(__file__).parent / "fixtures/cpu-diagnostic-memory-complete-v1.protocol"
        fixture_line = protocol_fixture.read_text(encoding="ascii").strip()
        node_event = diagnostic_plan_tool._parse_node_memory_event(fixture_line)
        self.assertIsNotNone(node_event)
        assert node_event is not None
        self.assertEqual(node_event["cgroup_memory_status"], "complete")
        self.assertEqual(node_event["sampled_window_duration_ms"], 16_000)
        self.assertIsNone(diagnostic_plan_tool._parse_node_memory_event(
            fixture_line.replace("cgroup_memory_current_sampled_max_bytes", "cgroup_memory_current_max_bytes")
        ))
        report = {
            "schema": 1,
            "workload": cpu_pair.WORKLOAD,
            "fixture_kind": "cpu_diagnostic_only_v1",
            "capacity_slo_credit": False,
            "diagnostic_credit": False,
            "source_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "external_run_id": "12345",
            "external_run_attempt": "1",
            "status": "complete",
            "fixture_manifest_sha256": "c" * 64,
            "namespace_closed": True,
            "plan_cleanup": "complete",
            "failure_stage": "none",
            "failure_code": "none",
            "canonical_fixture": {
                "fixture_kind": "cpu_diagnostic_only_v1",
                "tournaments": 1,
                "users": 14,
                "users_per_tournament": 14,
                "selected_actors": 8,
            },
            "actor_payload_bytes": 2048,
            "workload_contract": {
                "actors": 8,
                "requests_per_phase": 500,
                "phase_window_ms": 20_000,
                "idle_between_phases_ms": 5_000,
                "max_inflight": 8,
            },
            "service_cpu_usage": {"status": "complete", "reason": "none", "rows": [{"service": "api"}] * 2 + [{"service": "web"}] * 2},
            "cpu_profile_attribution": {
                "status": "complete",
                "reason": "none",
                "rows": [
                    {
                        "service": "api", "expected_targets": 2, "observed_targets": 2,
                        "event_count": 2, "total_cpu_us": 1_000_000, "sample_count": None,
                        "timer": "thread_cpu", "observation_unit": "calls",
                        "categories": [{"category": "other", "cpu_us": 1_000_000, "observations": 10}],
                    },
                    {
                        "service": "web", "expected_targets": 1, "observed_targets": 1,
                        "event_count": 1, "total_cpu_us": 1_000_000, "sample_count": 100,
                        "timer": "v8_cpu", "observation_unit": "samples",
                        "categories": [{"category": "other", "cpu_us": 1_000_000, "observations": 100}],
                    },
                ],
            },
            "memory_attribution": {
                "status": "complete", "reason": "none", "cgroup_memory_status": "complete",
                "cgroup_memory_reason": "none",
                "rows": [
                    {
                        **node_event, "phase": phase, "status": "complete",
                        "cgroup_memory_status": "complete", "cgroup_memory_reason": "none",
                        "expected_targets": 1,
                        "observed_targets": 1, "event_count": 1, "duplicate_count": 0,
                        "sample_count": 5, "rss_min_bytes": 100, "rss_max_bytes": 100,
                        "rss_mean_bytes": 1500, "heap_total_max_bytes": 900,
                        "heap_used_max_bytes": 700, "heap_limit_max_bytes": 8000,
                        "external_max_bytes": 100, "array_buffers_max_bytes": 100,
                        "gc_status": "complete", "gc_overflow": False,
                        "gc_count": 1, "gc_duration_ms": 0.5,
                    }
                    for phase in ("off", "on")
                ],
            },
            "result": {
                "capture_complete": True,
                "all_expected_statuses": True,
                "warmup": {"expected_actors": 8, "authenticated_200": 8, "page_warm_200": 8},
                **{
                    phase: {
                        "complete": True,
                        "all_expected_statuses": True,
                        "scheduled": 500,
                        "submitted": 500,
                        "completed": 500,
                        "completed_in_window": 500,
                        "status_counts": {"200": 500},
                        "latency_ms": {"count": 500, "p50": 20.0, "p95": 25.0, "p99": 30.0},
                    }
                    for phase in ("off", "on")
                },
                "quality_gate": {
                    "status": "passed",
                    "request_p95_delta_percent": 2.0,
                    "service_cpu_delta_percentage_points": {"api": 1.0, "web": -2.0},
                },
            },
        }
        expected = {
            "source_sha": "a" * 40,
            "app_target_sha": "b" * 40,
            "run_id": "12345",
            "attempt": "1",
            "manifest_sha256": "c" * 64,
        }
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "PASS")
        # Request 500 is scheduled at 19.96s; a 90ms response finishes at
        # 20.05s, inside the existing bounded drain. Keep the nominal-window
        # count truthful while still requiring all 500 successful responses.
        for phase in ("off", "on"):
            report["result"][phase]["completed_in_window"] = 499
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "PASS")
        report["result"]["off"]["completed"] = 499
        self.assertEqual(
            cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID"
        )
        report["result"]["off"]["completed"] = 500
        report["result"]["on"]["status_counts"] = {"200": 499, "500": 1}
        report["result"]["on"]["all_expected_statuses"] = False
        self.assertEqual(
            cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID"
        )
        report["result"]["on"]["status_counts"] = {"200": 500}
        report["result"]["on"]["all_expected_statuses"] = True
        for row in report["memory_attribution"]["rows"]:
            row["cgroup_sample_count"] = 4
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID")
        for row in report["memory_attribution"]["rows"]:
            row["cgroup_sample_count"] = 5
            row["cgroup_memory_reason"] = "file_unavailable"
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID")
        report["memory_attribution"]["cgroup_memory_reason"] = "none"
        for row in report["memory_attribution"]["rows"]:
            row["cgroup_memory_reason"] = "none"
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "PASS")
        report["memory_attribution"]["cgroup_memory_status"] = "unknown"
        report["memory_attribution"]["cgroup_memory_reason"] = "hierarchy_unavailable"
        for row in report["memory_attribution"]["rows"]:
            row["cgroup_memory_status"] = "unknown"
            row["cgroup_memory_reason"] = "hierarchy_unavailable"
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "PASS")
        report["memory_attribution"]["cgroup_memory_status"] = "complete"
        report["memory_attribution"]["cgroup_memory_reason"] = "none"
        for row in report["memory_attribution"]["rows"]:
            row["cgroup_memory_status"] = "complete"
            row["cgroup_memory_reason"] = "none"
        for row in report["memory_attribution"]["rows"]:
            row["sampled_window_end_elapsed_ms"] = 16_037
            row["sampled_window_duration_ms"] = 16_037
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "PASS")
        for row in report["memory_attribution"]["rows"]:
            row["sampled_window_start_elapsed_ms"] = 1_000
            row["sampled_window_end_elapsed_ms"] = 999
            row["sampled_window_duration_ms"] = -1
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID")
        for row in report["memory_attribution"]["rows"]:
            row["sampled_window_start_elapsed_ms"] = 0
            row["sampled_window_end_elapsed_ms"] = 16_000
            row["sampled_window_duration_ms"] = 16_000
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "PASS")
        for row in report["memory_attribution"]["rows"]:
            row["sampled_window_end_elapsed_ms"] = 20_001
            row["sampled_window_duration_ms"] = 20_001
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID")
        for row in report["memory_attribution"]["rows"]:
            row["sampled_window_end_elapsed_ms"] = 16_000
            row["sampled_window_duration_ms"] = 16_000
        report["result"]["quality_gate"]["request_p95_delta_percent"] = 5.1
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "FAIL")
        report["canonical_fixture"]["users"] = 20_000
        self.assertEqual(cpu_pair.evaluate_cpu_only_report(report, **expected), "INVALID")
        report["canonical_fixture"]["users"] = 14
        self.assertEqual(
            cpu_pair.evaluate_cpu_only_report(report, **{**expected, "source_sha": "d" * 40}),
            "INVALID",
        )

    def test_preflight_fallback_report_is_bound_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            raw_manifest = b'{"schema":1,"fixture":"private"}\n'
            manifest.write_bytes(raw_manifest)
            manifest.chmod(0o600)
            report = root / "report.json"
            report_value = {
                "schema": 1,
                "workload": cpu_pair.WORKLOAD,
                "fixture_kind": "cpu_diagnostic_only_v1",
                "source_sha": "a" * 40,
                "app_target_sha": "b" * 40,
                "external_run_id": "12345",
                "external_run_attempt": "1",
                "status": "incomplete",
                "failure_stage": "unknown",
                "failure_code": "unknown",
                "capacity_slo_credit": False,
                "diagnostic_credit": False,
                "fixture_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
                "canonical_fixture": {
                    "fixture_kind": "cpu_diagnostic_only_v1", "tournaments": 1,
                    "users": 14, "users_per_tournament": 14, "selected_actors": 8,
                },
                "namespace_closed": False,
                "plan_cleanup": "unknown",
                "memory_attribution": {
                    "status": "incomplete", "reason": "incomplete",
                    "cgroup_memory_status": "unknown",
                    "rows": [{
                        "service": "web", "phase": "off", "status": "incomplete",
                        "cgroup_memory_status": "unknown", "gc_status": "unavailable",
                        "gc_overflow": False, "sample_count": 3, "rss_max_bytes": 12345,
                        "cgroup_memory_peak_end_bytes": None, "secret": "must not escape",
                    }],
                },
            }
            cpu_pair._write_report(report, report_value)
            output = root / "public.json"
            command = [
                sys.executable, str(Path(cpu_pair.__file__)), "sanitize-report",
                "--input", str(report), "--output", str(output), "--manifest", str(manifest),
                "--source-sha", "a" * 40, "--app-target-sha", "b" * 40,
                "--external-run-id", "12345", "--external-run-attempt", "1",
                "--fixture-kind", "cpu_diagnostic_only_v1",
            ]
            accepted = subprocess.run(command, capture_output=True, check=False)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            projected = json.loads(output.read_text(encoding="ascii"))
            self.assertEqual(projected["status"], "incomplete")
            self.assertFalse(projected["capacity_slo_credit"])
            self.assertFalse(projected["diagnostic_credit"])
            self.assertEqual(projected["source_sha"], "a" * 40)
            self.assertEqual(projected["external_run_id"], "12345")
            self.assertEqual(projected["memory_attribution"]["rows"][0]["rss_max_bytes"], 12345)
            self.assertIsNone(projected["memory_attribution"]["rows"][0]["cgroup_memory_peak_end_bytes"])
            self.assertNotIn("private", output.read_text(encoding="ascii"))
            self.assertNotIn("must not escape", output.read_text(encoding="ascii"))
            mismatch = [*command]
            mismatch[mismatch.index("--manifest") + 1] = str(root / "wrong-manifest.json")
            wrong = root / "wrong-manifest.json"
            wrong.write_bytes(b'{"schema":1,"fixture":"different"}\n')
            wrong.chmod(0o600)
            rejected = subprocess.run(mismatch, capture_output=True, check=False)
            self.assertNotEqual(rejected.returncode, 0)

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
            "memory_status": "incomplete",
            "memory_reason": "incomplete",
            "cgroup_memory_status": "unknown",
            "cgroup_memory_reason": "hierarchy_unavailable",
            "memory_rows": [],
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

        def run_synthetic_leg(last_response_seconds: float) -> dict[str, object]:
            clock = [100.0]
            index = [0]
            synthetic_plan = {
                "run_id": "d" * 32,
                "off_start_ms": 100_000,
                "off_end_ms": 120_000,
                "on_start_ms": 125_000,
                "on_end_ms": 145_000,
            }

            def successful_request(
                *_args: object, **_kwargs: object
            ) -> SimpleNamespace:
                current_index = index[0]
                index[0] += 1
                latency = last_response_seconds if current_index == 499 else 0.01
                return SimpleNamespace(
                    status=200,
                    error_kind=None,
                    elapsed_ms=latency * 1000,
                    finished_at_monotonic=clock[0] + latency,
                    ok=True,
                )

            class ImmediateExecutor:
                def submit(self, callback):
                    future = Future()
                    future.set_result(callback())
                    return future

            def advance_wait(futures, *, timeout, return_when):
                del return_when
                end = clock[0] + max(timeout, 0.0)
                finished = {
                    future
                    for future in futures
                    if future.result().finished_at_monotonic <= end
                }
                if finished:
                    clock[0] = max(
                        clock[0],
                        max(
                            future.result().finished_at_monotonic
                            for future in finished
                        ),
                    )
                    return finished, set(futures) - finished
                clock[0] = end
                return set(), set(futures)

            with (
                patch.object(cpu_pair, "_wait_until", return_value=100.0),
                patch.object(cpu_pair.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(
                    cpu_pair.time,
                    "sleep",
                    side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay),
                ),
                patch.object(cpu_pair, "wait", side_effect=advance_wait),
                patch.object(
                    cpu_pair.external_load,
                    "_page_request",
                    side_effect=successful_request,
                ),
            ):
                return cpu_pair._leg(
                    "https://old-sparky.com",
                    actors,
                    ("session", "csrf"),
                    synthetic_plan,
                    "off",
                    ImmediateExecutor(),
                )

        in_drain = run_synthetic_leg(0.09)
        self.assertTrue(in_drain["complete"])
        self.assertTrue(in_drain["all_expected_statuses"])
        self.assertEqual(in_drain["submitted"], 500)
        self.assertEqual(in_drain["completed"], 500)
        self.assertEqual(in_drain["completed_in_window"], 499)
        self.assertEqual(in_drain["drain_deadline_ms"], 125_000)
        self.assertAlmostEqual(in_drain["elapsed_seconds"], 20.05, places=6)
        with self.assertRaises(cpu_pair.PairError):
            run_synthetic_leg(5.05)

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
                    "csrf_token": f"c{index:042d}.{'a' * 64}",
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
                "plan_prepare_status": "prepared",
                "plan_prepare_refusal_class": "none",
                "prepare_transport": {
                    "stage": "complete", "return_code": 0,
                    "stdout_shape": "prepared_marker", "child_state": "reaped",
                },
            })
            command = captured["command"]
            self.assertIsInstance(command, list)
            for option in ("ControlMaster=no", "ControlPersist=no", "ControlPath=none"):
                self.assertIn(option, command)
            cpu_pair._remove_ssh_files(secret_dir, secrets, lifecycle)
            self.assertFalse(secret_dir.exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            secret_dir = Path(temp_dir) / "external-ssh"
            secret_dir.mkdir(mode=0o700)
            config_path = secret_dir / "config"
            key_path = secret_dir / "id_ed25519"
            config_path.write_text("ssh config", encoding="ascii")
            key_path.write_text("private key", encoding="ascii")
            os.chmod(config_path, 0o600)
            os.chmod(key_path, 0o600)
            lifecycle = {"closed": True}
            real_popen = cpu_pair.subprocess.Popen

            def refusal_ssh(_command: list[str], **kwargs: object):
                script = (
                    "import sys; sys.stdin.buffer.read(); "
                    "sys.stdout.write('CPU_DIAGNOSTIC_PLAN status=refused class=PlanError\\n'); "
                    "sys.exit(2)"
                )
                return real_popen([cpu_pair.sys.executable, "-c", script], **kwargs)

            with patch.object(cpu_pair.subprocess, "Popen", side_effect=refusal_ssh):
                prepared, release_slug, usage = cpu_pair._ssh_plan(
                    SimpleNamespace(), "example.org", "deploy", config_path,
                    key_path, {"operation": "prepare"}, lifecycle,
                )
            self.assertFalse(prepared)
            self.assertIsNone(release_slug)
            self.assertIsNone(usage)
            self.assertEqual(lifecycle["plan_prepare_status"], "refused")
            self.assertEqual(lifecycle["plan_prepare_refusal_class"], "PlanError")

        with tempfile.TemporaryDirectory() as temp_dir:
            secret_dir = Path(temp_dir) / "external-ssh"
            secret_dir.mkdir(mode=0o700)
            config_path = secret_dir / "config"
            key_path = secret_dir / "id_ed25519"
            config_path.write_text("ssh config", encoding="ascii")
            key_path.write_text("private key", encoding="ascii")
            os.chmod(config_path, 0o600)
            os.chmod(key_path, 0o600)
            lifecycle = {"closed": True}
            real_popen = cpu_pair.subprocess.Popen

            def false_success_ssh(_command: list[str], **kwargs: object):
                script = (
                    "import sys; sys.stdin.buffer.read(); "
                    "sys.stdout.write('CPU_DIAGNOSTIC_PLAN status=refused class=PlanError\\n')"
                )
                return real_popen([cpu_pair.sys.executable, "-c", script], **kwargs)

            with patch.object(cpu_pair.subprocess, "Popen", side_effect=false_success_ssh):
                prepared, _release_slug, _usage = cpu_pair._ssh_plan(
                    SimpleNamespace(), "example.org", "deploy", config_path,
                    key_path, {"operation": "prepare"}, lifecycle,
                )
            self.assertFalse(prepared)
            self.assertEqual(lifecycle["plan_prepare_status"], "bad_protocol")

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
                    "csrf_token": f"c{index:042d}.{'a' * 64}",
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

    def test_pair_actor_payload_accepts_only_issued_csrf_token_shape(self) -> None:
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
                    "csrf_token": f"c{index:042d}.{'b' * 64}",
                }
                for index in range(8)
            ],
        }
        self.assertEqual(len(cpu_pair._validate_actor_payload(payload)), 8)

        malformed_tokens = (
            "c" * 43,
            f"{'c' * 42}.{'b' * 64}",
            f"{'c' * 43}.{'B' * 64}",
            f"{'c' * 43}.{'b' * 63}g",
        )
        for token in malformed_tokens:
            with self.subTest(token_shape=len(token)):
                candidate = {
                    **payload,
                    "actors": [
                        *payload["actors"][:-1],
                        {**payload["actors"][-1], "csrf_token": token},
                    ],
                }
                with self.assertRaises(cpu_pair.PairError):
                    cpu_pair._validate_actor_payload(candidate)

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

    def test_html_ssr_memory_watch_binding_is_a_separate_closed_workload(self) -> None:
        for workload in (
            diagnostic_plan_tool.CAPACITY_WORKLOAD,
            diagnostic_plan_tool.HTML_CAPACITY_WORKLOAD,
        ):
            payload = {
                "schema": 1, "run_id": "a" * 32, "source_sha": "b" * 40,
                "workload": workload,
            }
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            parsed = _read_request("memory-watch-stdin", io.BytesIO(raw))
            self.assertEqual(parsed.workload, workload)
        invalid = {
            "schema": 1, "run_id": "a" * 32, "source_sha": "b" * 40,
            "workload": "authenticated_workspace_read_pair_v1",
        }
        with self.assertRaises(PlanError):
            _read_request(
                "memory-watch-stdin",
                io.BytesIO(json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode() + b"\n"),
            )

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
                    for phase, timestamp in (("off", "21000000"), ("on", "46000000")):
                        memory_fields = {
                            "service": "web", "run_id": "a" * 32, "phase": phase,
                            "sample_count": "5", "status": "complete",
                            "cgroup_sample_count": "0",
                            "gc_duration_ms": "1.250", "gc_status": "complete", "gc_overflow": "false",
                        }
                        for prefix in ("rss", "heap_total", "heap_used", "heap_limit", "external", "array_buffers"):
                            for suffix in ("min", "max", "mean"):
                                memory_fields[f"{prefix}_{suffix}_bytes"] = "100"
                        for field in (
                            "cgroup_memory_current_start_bytes", "cgroup_memory_current_end_bytes",
                            "cgroup_memory_current_sampled_max_bytes", "cgroup_memory_peak_start_bytes",
                            "cgroup_memory_peak_end_bytes", "cgroup_memory_peak_sampled_max_bytes",
                            "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
                            "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
                            "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
                        ):
                            memory_fields[field] = "unknown"
                        memory_fields.update({
                            "sampled_window_start_elapsed_ms": "0",
                            "sampled_window_end_elapsed_ms": "16000",
                            "sampled_window_duration_ms": "16000",
                            "cgroup_memory_status": "unavailable",
                            "cgroup_memory_reason": "hierarchy_unavailable",
                        })
                        for field in ("gc_count", "gc_major_count", "gc_minor_count", "gc_incremental_count", "gc_weakcb_count", "gc_other_count"):
                            memory_fields[field] = "0"
                        memory_message = "cpu_diagnostic_memory " + " ".join(
                            f"{key}={value}" for key, value in memory_fields.items()
                        )
                        journal_rows.append({
                            "MESSAGE": memory_message,
                            "_PID": str(target["pid"]),
                            "_SYSTEMD_UNIT": unit,
                            "_SYSTEMD_INVOCATION_ID": str(target["invocation_id"]),
                            "_SYSTEMD_CGROUP": cgroup,
                            "__REALTIME_TIMESTAMP": timestamp,
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
        self.assertEqual(summary["memory_status"], "complete", summary)
        self.assertEqual(summary["cgroup_memory_status"], "unknown")
        self.assertEqual([row["phase"] for row in summary["memory_rows"]], ["off", "on"])
        self.assertEqual(summary["memory_rows"][0]["cgroup_memory_status"], "unknown")
        serialized = json.dumps(summary, sort_keys=True)
        self.assertNotIn('"pid"', serialized)
        self.assertNotIn('"start_ticks"', serialized)

        # Exercise the fixed remote projection and the _ssh_plan validator that
        # feeds the capacity telemetry projector.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_path = root / "cleanup-summary.json"
            summary_path.write_text(json.dumps({
                "status": "expired_plans_removed", "service_count": 2, **summary,
            }, sort_keys=True), encoding="ascii")
            fake_sudo = root / "sudo-fixture"
            fake_sudo.write_text(
                "#!/usr/bin/python3\n"
                "import json,sys\n"
                "sys.stdin.read()\n"
                f"value=json.load(open({str(summary_path)!r}, encoding='ascii'))\n"
                "print('CPU_DIAGNOSTIC_PLAN ' + json.dumps(value, sort_keys=True, separators=(',', ':')))\n",
                encoding="ascii",
            )
            fake_sudo.chmod(0o700)
            projected_stdout = io.StringIO()
            with (
                patch.object(
                    remote_dispatch,
                    "_trusted_python_helper",
                    return_value=True,
                ) as trusted_python_helper,
                patch.object(remote_dispatch, "SUDO", str(fake_sudo)),
                redirect_stdout(projected_stdout),
            ):
                self.assertEqual(remote_dispatch._run_cpu_diagnostic_plan({
                    "operation": "cleanup", "schema": 1, "run_id": "a" * 32,
                }), 0)
            trusted_python_helper.assert_called_once_with(
                remote_dispatch.CPU_DIAGNOSTIC_PLAN_HELPER
            )
            denied_stdout = io.StringIO()
            with (
                patch.object(
                    remote_dispatch,
                    "_trusted_python_helper",
                    return_value=False,
                ) as denied_helper,
                patch.object(remote_dispatch.subprocess, "Popen") as denied_popen,
                redirect_stdout(denied_stdout),
            ):
                self.assertEqual(remote_dispatch._run_cpu_diagnostic_plan({
                    "operation": "cleanup", "schema": 1, "run_id": "a" * 32,
                }), 2)
            denied_helper.assert_called_once_with(
                remote_dispatch.CPU_DIAGNOSTIC_PLAN_HELPER
            )
            denied_popen.assert_not_called()
            self.assertEqual(
                denied_stdout.getvalue(),
                "CPU_DIAGNOSTIC_PLAN status=refused class=UntrustedHelper\n",
            )
            projected_line = projected_stdout.getvalue().strip()
            self.assertTrue(projected_line.startswith("CPU_DIAGNOSTIC_PLAN {"))
            projected_value = json.loads(projected_line.removeprefix("CPU_DIAGNOSTIC_PLAN "))
            validated_cleanup = cpu_pair._validate_usage_summary(projected_value)
            self.assertIsNotNone(validated_cleanup)
            self.assertEqual(len(validated_cleanup["memory_rows"]), 2)
            self.assertEqual(validated_cleanup["memory_rows"][1]["sample_count"], 5)
            capacity_telemetry = capacity_diagnostic._project_server_telemetry(validated_cleanup)
            self.assertEqual(capacity_telemetry["node_memory_status"], "complete")
            self.assertEqual(capacity_telemetry["cgroup_memory_status"], "unknown")
            self.assertEqual(capacity_telemetry["node_memory_rows"][1]["gc_count"], 0)

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

    def test_capacity_plans_bind_api_usage_without_enabling_cprofile(self) -> None:
        import python_packages.platform_infra.performance_diagnostic as diagnostic_module

        scope = {
            "type": "http", "method": "GET",
            "path": "/api/v1/tournaments/safe-slug/workspace",
            "headers": [(b"x-platform-ssr-trace", b"a" * 32)],
        }
        for workload in (
            capacity_diagnostic.WORKLOAD,
            capacity_diagnostic.HTML_WORKLOAD,
        ):
            with self.subTest(workload=workload):
                payload = _diagnostic_payload()
                payload["workload"] = workload
                plan = parse_plan_payload(_canonical_payload(payload), expected_service="api")
                self.assertEqual(plan.workload, workload)
                with (
                    patch.object(diagnostic_module, "read_process_plan", return_value=plan),
                    patch.object(diagnostic_module.time, "time_ns", return_value=30_000_000_000),
                    patch.object(diagnostic_module, "_start_cpu_usage_window") as usage_window,
                    patch.object(diagnostic_module, "_active_profile", None),
                    patch("asyncio.get_running_loop", return_value=object()),
                ):
                    bound_plan, phase = ApiCpuDiagnostic.activate_for_scope(scope)
                self.assertIs(bound_plan, plan)
                self.assertEqual(phase, "on")
                usage_window.assert_called_once()
                self.assertEqual(usage_window.call_args.args[:2], (plan, "on"))
                self.assertIsNone(diagnostic_module._active_profile)

    def test_capacity_api_usage_summary_requires_both_workers_in_both_phases(self) -> None:
        targets = {
            "api": [
                {"service": "api", "pid": 101, "start_ticks": 11, "invocation_id": "c" * 32},
                {"service": "api", "pid": 102, "start_ticks": 12, "invocation_id": "c" * 32},
            ],
            "web": [
                {"service": "web", "pid": 201, "start_ticks": 21, "invocation_id": "d" * 32},
            ],
        }
        for workload in (capacity_diagnostic.WORKLOAD, capacity_diagnostic.HTML_WORKLOAD):
            with self.subTest(workload=workload):
                plans = {
                    service: {
                        **_diagnostic_payload(service=service),
                        "workload": workload,
                        "targets": service_targets,
                    }
                    for service, service_targets in targets.items()
                }
                records: list[dict[str, str]] = []
                for service, service_targets in targets.items():
                    for target in service_targets:
                        for phase in ("off", "on"):
                            message = (
                                "cpu_diagnostic_usage "
                                f"service={service} run_id={'a' * 32} phase={phase} "
                                "window_ms=20000 cpu_ns=4000000000 "
                                "cpu_capacity_cpus=2.000000 start_lag_ms=10 "
                                "end_lag_ms=5 timing_complete=true"
                            )
                            records.append({
                                "MESSAGE": json.dumps({"message": message}) if service == "api" else message,
                                "_PID": str(target["pid"]),
                                "_SYSTEMD_UNIT": f"deadlock-{service}.service",
                                "_SYSTEMD_INVOCATION_ID": str(target["invocation_id"]),
                                "_SYSTEMD_CGROUP": f"/system.slice/deadlock-{service}.service",
                                "__REALTIME_TIMESTAMP": "1000000" if phase == "off" else "26000000",
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
                    patch.object(diagnostic_plan_tool, "_journal_rows", return_value=("complete", records)),
                ):
                    summary = diagnostic_plan_tool._usage_summary(plans)

                self.assertEqual(summary["usage_status"], "complete", summary)
                self.assertEqual(len(summary["usage_rows"]), 4)
                self.assertEqual(
                    [row["observed_targets"] for row in summary["usage_rows"]],
                    [2, 2, 1, 1],
                )
                self.assertTrue(all(row["timing_complete"] for row in summary["usage_rows"]))

    def test_cpu_plan_process_environment_is_allowlisted_and_identity_checked(self) -> None:
        marker = b"SENTINEL_PRIVATE_ENV_MUST_NOT_ESCAPE"
        valid_env = (
            b"INVOCATION_ID=" + b"f" * 32 + b"\0"
            b"PLATFORM_RUNTIME_SERVICE=api\0UNRELATED_SECRET=" + marker + b"\0"
        )

        def read_environment(raw: bytes) -> dict[str, bytes]:
            with (
                patch.object(diagnostic_plan_tool.os, "open", return_value=71),
                patch.object(
                    diagnostic_plan_tool.os, "read", side_effect=(raw, b""),
                ),
                patch.object(diagnostic_plan_tool.os, "close"),
            ):
                return diagnostic_plan_tool._read_process_environ(101)

        projected = read_environment(valid_env)
        self.assertEqual(
            projected,
            {
                "INVOCATION_ID": b"f" * 32,
                "PLATFORM_RUNTIME_SERVICE": b"api",
            },
        )
        self.assertNotIn(marker, b"\0".join(projected.values()))

        with self.subTest(identity="duplicate-allowlisted-marker"):
            with self.assertRaisesRegex(PlanError, "identity marker is duplicated"):
                read_environment(
                    b"INVOCATION_ID=" + b"f" * 32 + b"\0"
                    b"INVOCATION_ID=" + b"e" * 32 + b"\0"
                )

        with self.subTest(identity="missing-runtime-marker"):
            self.assertEqual(
                read_environment(b"INVOCATION_ID=" + b"f" * 32 + b"\0"),
                {"INVOCATION_ID": b"f" * 32},
            )

        with patch.object(diagnostic_plan_tool.os, "open", return_value=71), patch.object(
            diagnostic_plan_tool.os, "read", side_effect=OSError("private read detail")
        ), patch.object(diagnostic_plan_tool.os, "close") as close_fd:
            with self.assertRaises(OSError):
                diagnostic_plan_tool._read_process_environ(101)
            close_fd.assert_called_once_with(71)

        with patch.object(diagnostic_plan_tool.os, "open", return_value=71), patch.object(
            diagnostic_plan_tool.os, "read", side_effect=(b"x" * 6, b"")
        ), patch.object(diagnostic_plan_tool.os, "close") as close_fd:
            with self.assertRaisesRegex(PlanError, "environment exceeds bound"):
                diagnostic_plan_tool._read_process_environ(101, maximum=5)
            close_fd.assert_called_once_with(71)

        wrong_role_env = valid_env.replace(
            b"PLATFORM_RUNTIME_SERVICE=api", b"PLATFORM_RUNTIME_SERVICE=web"
        )
        missing_role_env = b"INVOCATION_ID=" + b"f" * 32 + b"\0"
        unit = {
            "ActiveState": "active",
            "InvocationID": "f" * 32,
            "MainPID": "999",
            "ControlGroup": "/system.slice/deadlock-api.service",
        }
        fake_stat = SimpleNamespace(st_uid=os.getuid(), st_dev=1, st_ino=2)
        for environment, refusal in (
            (wrong_role_env, "service process role mismatch"),
            (missing_role_env, "service process role mismatch"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(diagnostic_plan_tool.Path, "read_text", return_value="101\n"),
                patch.object(diagnostic_plan_tool.Path, "stat", return_value=fake_stat),
                patch.object(diagnostic_plan_tool.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=os.getuid())),
                patch.object(diagnostic_plan_tool.os, "readlink", return_value="/usr/bin/python3"),
                patch.object(diagnostic_plan_tool, "_process_start_ticks", side_effect=(10, 10)),
                patch.object(diagnostic_plan_tool.os, "open", return_value=71),
                patch.object(diagnostic_plan_tool.os, "read", side_effect=(environment, b"")),
                patch.object(diagnostic_plan_tool.os, "close"),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                with self.assertRaisesRegex(PlanError, refusal) as caught:
                    diagnostic_plan_tool._service_targets("api", unit)
            self.assertNotIn(marker.decode("ascii"), str(caught.exception))
            self.assertNotIn(marker.decode("ascii"), stdout.getvalue() + stderr.getvalue())

    def test_cpu_plan_refusal_is_closed_and_cleanup_failure_preserves_primary(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(diagnostic_plan_tool, "_read_request", side_effect=PlanError("private detail")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(diagnostic_plan_tool.main(["prepare-stdin"]), 2)
        self.assertEqual(stdout.getvalue(), "CPU_DIAGNOSTIC_PLAN status=refused class=PlanError\n")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("private detail", stdout.getvalue() + stderr.getvalue())

        report = {
            "status": "incomplete",
            "failure_stage": "preflight",
            "failure_code": "ssh_preflight_failed",
            "plan_cleanup_status": "refused",
        }
        cpu_pair._record_plan_cleanup_failure(report)
        self.assertEqual(report["failure_stage"], "preflight")
        self.assertEqual(report["failure_code"], "ssh_preflight_failed")
        self.assertEqual(report["plan_cleanup_failure_code"], "plan_refused")
        public = cpu_pair._public_report(report)
        self.assertEqual(public["plan_cleanup_status"], "refused")
        self.assertEqual(public["plan_cleanup_failure_code"], "plan_refused")
        self.assertEqual(public["failure_code"], "ssh_preflight_failed")

        initial = {
            "schema": 1, "workload": cpu_pair.WORKLOAD,
            "fixture_kind": "cpu_diagnostic_only_v1", "source_sha": "a" * 40,
            "app_target_sha": "a" * 40, "external_run_id": "123", "external_run_attempt": "1",
            "status": "incomplete", "failure_stage": "unknown", "failure_code": "unknown",
            "capacity_slo_credit": False, "diagnostic_credit": False,
            "fixture_manifest_sha256": "b" * 64, "input_handoff_sha256": "c" * 64,
            "source_binding_sha256": None, "plan_cleanup": "unknown",
            "plan_prepare_status": "prepared", "plan_prepare_refusal_class": "none",
            "plan_cleanup_status": "not_attempted", "plan_cleanup_refusal_class": "none",
            "plan_cleanup_failure_code": "none", "namespace_closed": False,
            "ssh_material_safe_to_remove": False, "ssh_material_binding": None,
            "result": None,
        }
        child = {
            "status": "complete", "warmup": {}, "off": {}, "on": {},
            "capture_complete": True, "all_expected_statuses": True,
            "quality_gate": {"status": "passed"},
        }
        cpu_pair._merge_supervisor_report(
            initial, closed=True, valid_child=True,
            worker_result=SimpleNamespace(returncode=0, reason="none"),
            child_report=child, fixture_kind="cpu_diagnostic_only_v1",
            expected_tournaments=1, expected_users=14,
            expected_users_per_tournament=14, actor_payload_bytes=512,
        )
        initial["plan_cleanup"] = "complete"
        initial["plan_cleanup_status"] = "complete"
        sanitized = cpu_pair._public_report(initial)
        self.assertEqual(sanitized["plan_prepare_status"], "prepared")
        self.assertEqual(sanitized["plan_cleanup_status"], "complete")

        failed_worker = {
            **initial,
            "failure_stage": "unknown", "failure_code": "unknown",
            "plan_cleanup_status": "not_attempted",
        }
        cpu_pair._merge_supervisor_report(
            failed_worker, closed=True, valid_child=False,
            worker_result=SimpleNamespace(returncode=1, reason="worker_report_invalid"),
            child_report=None, fixture_kind="cpu_diagnostic_only_v1",
            expected_tournaments=1, expected_users=14,
            expected_users_per_tournament=14, actor_payload_bytes=512,
        )
        failed_worker["plan_cleanup_status"] = "refused"
        cpu_pair._record_plan_cleanup_failure(failed_worker)
        failed_public = cpu_pair._public_report(failed_worker)
        self.assertEqual(failed_public["failure_stage"], "supervisor")
        self.assertEqual(failed_public["failure_code"], "worker_failed")
        self.assertEqual(failed_public["plan_cleanup_failure_code"], "plan_refused")
        self.assertEqual(failed_public["plan_prepare_status"], "prepared")

        # Drive the real CLI parent path: a typed prepare refusal followed by
        # a cleanup refusal must retain the original preflight classification
        # in the final sanitized JSON, without leaking either remote detail.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.chmod(root, 0o700)
            manifest_path = root / "manifest.json"
            manifest_path.write_bytes(b"fixture manifest bytes\n")
            os.chmod(manifest_path, 0o600)
            handoff_path = root / "handoff.json"
            handoff_path.write_text(json.dumps({
                "schema": 1,
                "confirmation": "RUN-PRODUCTION-CPU-DIAGNOSTIC",
                "target_sha": "a" * 40,
                "control_email": "operator@example.test",
                "setup_concurrency": "8",
                "run_id": "123",
                "profile": "external-vote",
                "tournament_count": "1",
                "users_per_tournament": "14",
                "timeout_diagnostics": "false",
                "fixture_kind": "cpu_diagnostic_only_v1",
            }, sort_keys=True, separators=(",", ":")), encoding="ascii")
            os.chmod(handoff_path, 0o600)
            ssh_dir = root / "external-ssh"
            ssh_dir.mkdir(mode=0o700)
            for name, content in (
                ("config", b"ssh config"),
                ("id_ed25519", b"private key"),
                ("known_hosts", b"host key"),
            ):
                path = ssh_dir / name
                path.write_bytes(content)
                os.chmod(path, 0o600)
            report_path = root / "report.json"
            manifest = {
                "runner_sha": "a" * 40,
                "app_target_sha": "a" * 40,
                "source_binding": None,
                "source_binding_sha256": None,
                "origin": "https://old-sparky.com",
                "session_cookie_name": "session",
                "csrf_cookie_name": "csrf",
                "tournaments": [{"slug": "cpu-fixture", "user_count": 14}],
            }
            users = [SimpleNamespace(
                tournament_slug="cpu-fixture",
                session_token=f"s{index:031d}",
                csrf_token=f"c{index:042d}.{'a' * 64}",
            ) for index in range(14)]
            def fake_plan(_args, _host, _user, _config, _key, payload, lifecycle, **_kwargs):
                lifecycle["closed"] = True
                lifecycle["mux_off_enforced"] = True
                lifecycle["last_command_returned"] = True
                if payload["operation"] == "prepare":
                    lifecycle["plan_prepare_status"] = "refused"
                    lifecycle["plan_prepare_refusal_class"] = "PlanError"
                    lifecycle["prepare_transport"] = {
                        "stage": "complete", "return_code": 2,
                        "stdout_shape": "refusal_marker", "child_state": "reaped",
                    }
                else:
                    lifecycle["plan_cleanup_status"] = "refused"
                    lifecycle["plan_cleanup_refusal_class"] = "OSError"
                    lifecycle["cleanup_transport"] = {
                        "stage": "complete", "return_code": 2,
                        "stdout_shape": "refusal_marker", "child_state": "reaped",
                    }
                return False, None, None

            argv = [
                "parent", "--manifest", str(manifest_path),
                "--input-handoff", str(handoff_path), "--report", str(report_path),
                "--source-sha", "a" * 40, "--app-target-sha", "a" * 40,
                "--external-run-id", "123", "--external-run-attempt", "1",
                "--fixture-kind", "cpu_diagnostic_only_v1",
                "--ssh-config", str(ssh_dir / "config"),
                "--ssh-key", str(ssh_dir / "id_ed25519"),
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(cpu_pair.external_load, "load_manifest_bytes", return_value=(manifest, users)),
                patch.object(cpu_pair, "_ssh_plan", side_effect=fake_plan),
                patch.object(cpu_pair, "_wait_past"),
                patch.object(cpu_pair, "_restore_hidden_ssh_material"),
                patch.dict(os.environ, {"PROD_SSH_HOST": "example.test", "PROD_SSH_USER": "deploy"}),
                redirect_stdout(stdout), redirect_stderr(stderr),
            ):
                self.assertEqual(cpu_pair.main(argv), 2)
            public = json.loads(report_path.read_text(encoding="ascii"))
            self.assertEqual(public["failure_stage"], "preflight")
            self.assertEqual(public["failure_code"], "ssh_preflight_failed")
            self.assertEqual(public["plan_prepare_status"], "refused")
            self.assertEqual(public["plan_prepare_refusal_class"], "PlanError")
            self.assertEqual(public["plan_cleanup_status"], "refused")
            self.assertEqual(public["plan_cleanup_refusal_class"], "OSError")
            self.assertEqual(public["plan_cleanup_failure_code"], "plan_refused")
            self.assertEqual(public["ssh_prepare_transport"], {
                "stage": "complete", "return_code": 2,
                "stdout_shape": "refusal_marker", "child_state": "reaped",
            })
            self.assertEqual(
                cpu_pair._public_ssh_transport({
                    "stage": "transport_failed", "return_code": -9,
                    "stdout_shape": "none", "child_state": "reaped",
                }),
                {
                    "stage": "transport_failed", "return_code": -9,
                    "stdout_shape": "none", "child_state": "reaped",
                },
            )
            self.assertIsNone(cpu_pair._public_ssh_transport({
                "stage": "complete", "return_code": True,
                "stdout_shape": "single_line", "child_state": "reaped",
            })["return_code"])
            self.assertTrue(public["ssh_material_safe_to_remove"])
            lifecycle_path = root / "ssh-lifecycle.json"
            lifecycle_receipt = json.loads(lifecycle_path.read_text(encoding="ascii"))
            self.assertIs(lifecycle_receipt["material_hidden"], False)
            cpu_pair._validate_ssh_lifecycle_receipt(
                lifecycle_path,
                expected={
                    "source_sha": "a" * 40, "run_id": "123", "attempt": "1",
                    "config_dir_dev": ssh_dir.stat().st_dev,
                    "config_dir_ino": ssh_dir.stat().st_ino,
                },
            )
            self.assertFalse(public["capacity_slo_credit"])
            self.assertFalse(public["diagnostic_credit"])
            self.assertNotIn("operator@example.test", report_path.read_text(encoding="ascii"))
            self.assertNotIn("private key", report_path.read_text(encoding="ascii"))
            self.assertNotIn("root plan prepare failed", stdout.getvalue() + stderr.getvalue())

        def cleanup_fixture(
            services: tuple[str, ...], *, summary_raises: bool = False,
        ) -> dict[str, object]:
            with tempfile.TemporaryDirectory() as temporary_dir:
                plan_dir = Path(temporary_dir)
                for service in services:
                    plan = {
                        "schema": 1,
                        "run_id": "d" * 32,
                        "source_sha": "e" * 40,
                        "release_slug": "release-eeeeeeeeeeee",
                        "workload": diagnostic_plan_tool.CAPACITY_WORKLOAD,
                        "off_start_ms": 1,
                        "off_end_ms": 1,
                        "on_start_ms": 1,
                        "on_end_ms": 1,
                        "expires_at_ms": 1,
                        "targets": [{
                            "service": service,
                            "pid": 123,
                            "start_ticks": 456,
                            "invocation_id": "f" * 32,
                        }],
                    }
                    path = plan_dir / diagnostic_plan_tool.PLAN_NAMES[service]
                    path.write_bytes(diagnostic_plan_tool._canonical_json(plan))
                    os.chmod(path, 0o440)

                def open_plan_dir(*, create: bool) -> int:
                    self.assertFalse(create)
                    return os.open(plan_dir, os.O_RDONLY | os.O_DIRECTORY)

                def read_plan(_directory_fd: int, name: str, maximum: int):
                    self.assertIn(name, diagnostic_plan_tool.PLAN_NAMES.values())
                    self.assertEqual(maximum, diagnostic_plan_tool.MAX_PLAN_BYTES)
                    path = plan_dir / name
                    raw = path.read_bytes()
                    self.assertLessEqual(len(raw), maximum)
                    return raw, os.stat(path, follow_symlinks=False)

                patches = [
                    patch.object(diagnostic_plan_tool.os, "geteuid", return_value=0),
                    patch.object(
                        diagnostic_plan_tool,
                        "_validate_plan_directory",
                        side_effect=open_plan_dir,
                    ),
                    patch.object(
                        diagnostic_plan_tool,
                        "_read_bounded_at",
                        side_effect=read_plan,
                    ),
                    patch.object(
                        diagnostic_plan_tool.grp,
                        "getgrnam",
                        side_effect=lambda name: SimpleNamespace(
                            gr_gid=os.stat(
                                plan_dir
                                / diagnostic_plan_tool.PLAN_NAMES[
                                    name.removeprefix("oldsparky-")
                                ]
                            ).st_gid
                        ),
                    ),
                ]
                if summary_raises:
                    patches.append(
                        patch.object(
                            diagnostic_plan_tool,
                            "_usage_summary",
                            side_effect=RuntimeError("bounded summary unavailable"),
                        )
                    )
                with patches[0], patches[1], patches[2], patches[3]:
                    if summary_raises:
                        with patches[4]:
                            result = diagnostic_plan_tool.cleanup(
                                Namespace(run_id="d" * 32)
                            )
                    else:
                        result = diagnostic_plan_tool.cleanup(
                            Namespace(run_id="d" * 32)
                        )
                self.assertFalse(
                    any(
                        (plan_dir / name).exists()
                        for name in diagnostic_plan_tool.PLAN_NAMES.values()
                    )
                )
                return result

        def cleanup_dispatch_roundtrip(
            cleanup_result: dict[str, object],
            *, expected_dispatch_status: int = 0,
        ) -> dict[str, object] | None:
            """Run producer output through dispatcher and outer closed decoder."""
            with tempfile.TemporaryDirectory() as temporary_dir:
                root = Path(temporary_dir)
                fake_sudo = root / "sudo-fixture"
                helper_line = (
                    "CPU_DIAGNOSTIC_PLAN "
                    + json.dumps({
                        "status": "expired_plans_removed", **cleanup_result,
                    }, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                fake_sudo.write_text(
                    f"#!{sys.executable}\n"
                    "import sys\n"
                    "sys.stdin.buffer.read()\n"
                    f"sys.stdout.buffer.write({helper_line.encode('ascii')!r})\n",
                    encoding="ascii",
                )
                fake_sudo.chmod(0o700)
                dispatcher_stdout = io.StringIO()
                with (
                    patch.object(remote_dispatch, "SUDO", str(fake_sudo)),
                    patch.object(remote_dispatch, "_trusted_python_helper", return_value=True),
                    redirect_stdout(dispatcher_stdout),
                ):
                    dispatch_status = remote_dispatch._run_cpu_diagnostic_plan({
                        "operation": "cleanup", "schema": 1, "run_id": "d" * 32,
                    })
                self.assertEqual(
                    dispatch_status, expected_dispatch_status, dispatcher_stdout.getvalue(),
                )
                if expected_dispatch_status != 0:
                    self.assertEqual(dispatcher_stdout.getvalue(), "")
                    return None
                projected_line = dispatcher_stdout.getvalue().encode("ascii").strip()
                prefix = b"CPU_DIAGNOSTIC_PLAN "
                self.assertTrue(projected_line.startswith(prefix))
                projected = cpu_pair._decode_canonical(
                    projected_line[len(prefix):], cap=cpu_pair.ROOT_USAGE_OUTPUT_CAP,
                )
                validated = cpu_pair._validate_usage_summary(projected)
                self.assertIsNotNone(validated)
                return validated

        empty_cleanup = cleanup_fixture(())
        empty_validated = cleanup_dispatch_roundtrip(empty_cleanup)
        self.assertEqual(empty_cleanup["service_count"], 0)
        self.assertEqual(empty_validated["service_count"], 0)
        partial_cleanup = cleanup_fixture(("api",))
        self.assertEqual(partial_cleanup["service_count"], 1)
        self.assertEqual(partial_cleanup["usage_status"], "unavailable")
        validated_partial = cleanup_dispatch_roundtrip(partial_cleanup)
        self.assertEqual(validated_partial["service_count"], 1)
        self.assertIsNone(cpu_pair._usage_delta_percentage_points(validated_partial))

        fallback_cleanup = cleanup_fixture(("api", "web"), summary_raises=True)
        self.assertEqual(fallback_cleanup["service_count"], 2)
        self.assertEqual(fallback_cleanup["profile_status"], "incomplete")
        validated_fallback = cleanup_dispatch_roundtrip(fallback_cleanup)
        self.assertEqual(validated_fallback["profile_status"], "incomplete")
        self.assertTrue(
            all(
                row[field] is None
                for row in validated_fallback["profile_rows"]
                for field in (
                    "start_lag_ms_min", "start_lag_ms_max", "elapsed_ms_min",
                    "elapsed_ms_max", "end_lag_ms_min", "end_lag_ms_max",
                )
            )
        )
        partial_pair_as_single = {
            "status": "expired_plans_removed",
            **fallback_cleanup,
            "service_count": 1,
        }
        self.assertTrue(partial_pair_as_single["usage_rows"])
        self.assertIsNone(cpu_pair._validate_usage_summary(partial_pair_as_single))
        self.assertIsNone(cleanup_dispatch_roundtrip(
            partial_pair_as_single, expected_dispatch_status=2,
        ))

        complete_pair_as_single = json.loads(json.dumps(fallback_cleanup))
        complete_pair_as_single["usage_status"] = "complete"
        complete_pair_as_single["usage_reason"] = "none"
        for row in complete_pair_as_single["usage_rows"]:
            row.update({
                "observed_targets": row["expected_targets"],
                "event_count": row["expected_targets"],
                "cpu_ns": 1_000_000_000,
                "window_ms_min": 20_000,
                "window_ms_max": 20_000,
                "start_lag_ms_min": 0,
                "start_lag_ms_max": 0,
                "end_lag_ms_min": 0,
                "end_lag_ms_max": 0,
                "duplicate_count": 0,
                "timing_complete": True,
            })
        complete_pair_as_single["profile_status"] = "complete"
        complete_pair_as_single["profile_reason"] = "none"
        for row in complete_pair_as_single["profile_rows"]:
            row.update({
                "observed_targets": row["expected_targets"],
                "event_count": row["expected_targets"],
                "start_lag_ms_min": 0,
                "start_lag_ms_max": 0,
                "elapsed_ms_min": 20_000,
                "elapsed_ms_max": 20_000,
                "end_lag_ms_min": 0,
                "end_lag_ms_max": 0,
                "total_cpu_us": 100,
                "sample_count": 1 if row["service"] == "web" else None,
                "categories": [{
                    "category": "other", "cpu_us": 100, "observations": 1,
                }],
            })
        self.assertIsNotNone(cpu_pair._validate_usage_summary({
            "status": "expired_plans_removed",
            **complete_pair_as_single,
            "service_count": 2,
        }))
        complete_pair_as_single["service_count"] = 1
        malformed_complete_single = {
            "status": "expired_plans_removed", **complete_pair_as_single,
        }
        self.assertIsNone(cpu_pair._validate_usage_summary(malformed_complete_single))
        self.assertIsNone(cleanup_dispatch_roundtrip(
            malformed_complete_single, expected_dispatch_status=2,
        ))

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
