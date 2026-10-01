from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from tools.platform_load_runtime import (
    LoadRuntimeBudget,
    LoadRuntimeBudgetExceeded,
    NamespaceCapabilityError,
    NamespaceIntegrityError,
    WORKER_REPORT_SCHEMA,
    _closed_failure_report,
    _await_namespace_ready,
    _captured_chain_closed,
    _namespace_command,
    _namespace_closed,
    _reap_captured_chain,
    _read_closed_report,
    _read_process_starttime,
    _read_process_state,
    _reap_after_signal,
    _verify_expected_parent,
    probe_pid_namespace_capability,
    run_supervised,
)


WORKER_SCRIPT = textwrap.dedent(
    """
    import json, os, signal, subprocess, sys, time
    from pathlib import Path

    config = json.loads(Path(os.environ['PLATFORM_LOAD_WORKER_CONFIG']).read_text())
    report = Path(config['worker_report_path'])
    mode = config.get('mode', 'success')
    if mode == 'success':
        report.write_text(json.dumps({
            'worker_report_schema': 2,
            'report_complete': True,
            'namespace_closed': False,
            'passed': True,
            'acceptance': {'passed': True, 'decision': 'PASS'},
        }) + '\\n')
        raise SystemExit(0)
    if mode == 'failed-report':
        report.write_text(json.dumps({
            'worker_report_schema': 2,
            'report_complete': True,
            'namespace_closed': False,
            'passed': False,
            'partial_work': True,
            'inflight_unknown': False,
            'acceptance': {'passed': False, 'decision': 'LOAD RUN FAILED'},
        }) + '\\n')
        raise SystemExit(1)
    if mode == 'malformed':
        report.write_text('{not-json')
        raise SystemExit(0)
    if mode == 'descendant':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(config['started_file']).write_text('started')
        heartbeat_file = Path(config['heartbeat_file'])
        child = subprocess.Popen([
            sys.executable, '-c',
            "from pathlib import Path; import signal,sys,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "path=Path(sys.argv[1])\\n"
            "while True:\\n"
            "    path.write_text(str(time.monotonic_ns())); time.sleep(0.02)",
            str(heartbeat_file),
        ])
        deadline = time.monotonic() + 10
        while not heartbeat_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(60)
    if mode in ('double-fork', 'setsid'):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(config['started_file']).write_text('started')
        heartbeat_file = Path(config['heartbeat_file'])
        if mode == 'setsid':
            child_code = (
                "import os,sys,time; os.setsid(); "
                "path=__import__('pathlib').Path(sys.argv[1])\\n"
                "while True:\\n"
                "    path.write_text(str(time.monotonic_ns())); time.sleep(0.02)"
            )
        else:
            child_code = (
                "import os,sys,time; "
                "first=os.fork(); "
                "first and os._exit(0); os.setsid(); "
                "second=os.fork(); "
                "second and os._exit(0); "
                "path=__import__('pathlib').Path(sys.argv[1])\\n"
                "while True:\\n"
                "    path.write_text(str(time.monotonic_ns())); time.sleep(0.02)"
            )
        subprocess.Popen([sys.executable, '-c', child_code, str(heartbeat_file)])
        deadline = time.monotonic() + 10
        while not heartbeat_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(60)
    if mode == 'pdeath-heartbeat':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(config['started_file']).write_text('started')
        Path(config['worker_pid_file']).write_text(str(os.getpid()))
        heartbeat_file = Path(config['heartbeat_file'])
        while True:
            heartbeat_file.write_text(str(time.monotonic_ns()))
            time.sleep(0.02)
    if mode == 'partial-report':
        Path(config['worker_report_path']).write_text(
            json.dumps({'worker_report_schema': 2, 'report_complete': False, 'namespace_closed': False})
        )
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    if mode == 'hang':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == 'external-term':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    time.sleep(60)
    """
)


class LoadRuntimeBudgetTests(unittest.TestCase):
    def test_absolute_budget_bounds_io_and_distinguishes_runner_budget(self) -> None:
        now = [10.0]

        def clock() -> float:
            return now[0]

        budget = LoadRuntimeBudget(
            10,
            max_runner_minutes=1,
            clock=clock,
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )
        self.assertEqual(budget.bound_timeout(30, "connect"), 10.0)
        now[0] = 20.0
        with self.assertRaises(LoadRuntimeBudgetExceeded) as raised:
            budget.check("body", operation="read")
        self.assertEqual(raised.exception.reason, "max_duration_seconds")

        runner = LoadRuntimeBudget(
            20,
            max_runner_minutes=0.2,
            clock=clock,
            started_at_monotonic=0.0,
            runner_started_at_monotonic=0.0,
        )
        now[0] = 13.0
        with self.assertRaises(LoadRuntimeBudgetExceeded) as runner_error:
            runner.check("future", operation="wait")
        self.assertEqual(runner_error.exception.reason, "max_runner_minutes")

    def _supervise(
        self,
        worker_script: Path,
        final_report: Path,
        worker_report: Path,
        **config: object,
    ):
        return run_supervised(
            worker_command=(sys.executable, str(worker_script)),
            report_path=final_report,
            worker_report_path=worker_report,
            max_duration_seconds=float(config.pop('duration', 1.0)),
            max_runner_minutes=float(config.pop('runner_minutes', 1.0)),
            worker_config=config,
            term_grace_seconds=0.05,
            poll_seconds=0.01,
        )

    def test_namespace_preflight_rejects_before_any_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_parent = root / "new-report-parent"
            report_path = report_parent / "final.json"
            worker_report_path = report_parent / "child.json"
            stale_parent = root / "existing"
            stale_parent.mkdir()
            stale_report = stale_parent / "stale.json"
            stale_report.write_text("keep-me", encoding="ascii")

            with patch(
                "tools.platform_load_runtime.require_pid_namespace_capability",
                side_effect=NamespaceCapabilityError("runner_must_be_nonroot"),
            ):
                result = run_supervised(
                    worker_command=("/usr/bin/python3", "/checkout/worker.py"),
                    report_path=report_path,
                    worker_report_path=worker_report_path,
                    max_duration_seconds=10,
                    max_runner_minutes=1,
                    worker_config={},
                )

            self.assertEqual(result.reason, "namespace_unavailable")
            self.assertFalse(result.namespace_closed)
            self.assertFalse(report_parent.exists())
            self.assertFalse(report_path.exists())
            self.assertFalse(worker_report_path.exists())
            self.assertEqual(stale_report.read_text(encoding="ascii"), "keep-me")

    def test_run_profile_preflight_uses_pure_error_channel(self) -> None:
        from tools.platform_load import run_profile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_parent = root / "existing-report-parent"
            report_parent.mkdir()
            report_path = report_parent / "final.json"
            report_path.write_text("keep-me", encoding="ascii")
            with patch(
                "tools.platform_load_runtime.require_pid_namespace_capability",
                side_effect=NamespaceCapabilityError("runner_must_be_nonroot"),
            ):
                status = run_profile({}, root / "manifest.json", report_path)
            self.assertEqual(status, 2)
            self.assertTrue(report_path.parent.exists())
            self.assertEqual(report_path.read_text(encoding="ascii"), "keep-me")

    def test_reap_grace_is_bounded_by_absolute_deadline(self) -> None:
        from unittest.mock import Mock

        now = [100.0]
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("fake", 0)

        def monotonic() -> float:
            return now[0]

        def advance(seconds: float) -> None:
            now[0] += seconds

        with (
            patch("tools.platform_load_runtime.time.monotonic", side_effect=monotonic),
            patch("tools.platform_load_runtime.time.sleep", side_effect=advance),
        ):
            result, killed = _reap_after_signal(
                process,
                grace_seconds=10,
                poll_seconds=1,
                deadline=101.0,
            )

        self.assertIsNone(result)
        self.assertTrue(killed)
        self.assertEqual(now[0], 101.0)
        process.kill.assert_called_once()

    def _run_fake_success_with_deadline_hook(
        self,
        *,
        delay_report_read: bool = False,
        delay_report_publish: bool = False,
    ) -> tuple[object, dict[str, object]]:
        """Exercise final report gates without requiring a root-owned namespace."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            final_report = root / "final.json"
            worker_report = root / "child.json"
            worker_payload = {
                "worker_report_schema": 2,
                "report_complete": True,
                "namespace_closed": False,
                "passed": True,
                "acceptance": {"passed": True, "decision": "PASS"},
            }
            now = [1000.0]

            class FakeStream:
                def write(self, _value: object) -> None:
                    return None

                def flush(self) -> None:
                    return None

                def close(self) -> None:
                    return None

            class FakeProcess:
                pid = 1001
                returncode = 0
                stdin = FakeStream()
                stdout = FakeStream()

                def poll(self) -> int:
                    return 0

                def wait(self, *args: object, **kwargs: object) -> int:
                    return 0

                def send_signal(self, _signum: int) -> None:
                    return None

                def kill(self) -> None:
                    return None

            process = FakeProcess()

            def monotonic() -> float:
                return now[0]

            def read_report(_path: Path) -> tuple[dict[str, object], None]:
                if delay_report_read:
                    now[0] = 1002.0
                return dict(worker_payload), None

            from tools import platform_load_runtime as runtime

            real_write = runtime._write_json_atomic

            def write_report(path: Path, payload: object) -> None:
                real_write(path, payload)  # type: ignore[arg-type]
                if delay_report_publish and Path(path).name == "final.json":
                    now[0] = 1002.0

            with (
                patch("tools.platform_load_runtime.require_pid_namespace_capability"),
                patch("tools.platform_load_runtime._runner_identity", return_value=(65534, 65534)),
                patch("tools.platform_load_runtime._set_child_subreaper", return_value=None),
                patch("tools.platform_load_runtime._read_process_starttime", return_value=1),
                patch("tools.platform_load_runtime._capture_namespace_pid", return_value=1002),
                patch("tools.platform_load_runtime._read_process_descendants", return_value=()),
                patch("tools.platform_load_runtime._await_namespace_ready", return_value=True),
                patch("tools.platform_load_runtime._namespace_closed", return_value=True),
                patch("tools.platform_load_runtime._reap_namespace_init", return_value=True),
                patch("tools.platform_load_runtime._reap_captured_chain", return_value=True),
                patch("tools.platform_load_runtime._pidfd_is_live", return_value=False),
                patch("tools.platform_load_runtime.os.pidfd_open", return_value=11),
                patch("tools.platform_load_runtime.time.monotonic", side_effect=monotonic),
                patch("tools.platform_load_runtime.subprocess.Popen", return_value=process),
                patch("tools.platform_load_runtime._read_closed_report", side_effect=read_report),
                patch("tools.platform_load_runtime._write_json_atomic", side_effect=write_report),
            ):
                result = run_supervised(
                    worker_command=("/usr/bin/python3", "/checkout/worker.py"),
                    report_path=final_report,
                    worker_report_path=worker_report,
                    max_duration_seconds=1,
                    max_runner_minutes=1,
                    worker_config={},
                    term_grace_seconds=0.05,
                    poll_seconds=0.01,
                )
            return result, json.loads(final_report.read_text(encoding="utf-8"))

    def test_report_read_overrun_cannot_publish_success_envelope(self) -> None:
        result, payload = self._run_fake_success_with_deadline_hook(delay_report_read=True)
        self.assertEqual(result.reason, "max_duration_seconds")
        self.assertFalse(payload["passed"])
        self.assertNotEqual(payload["runtime_supervisor"]["reason"], "none")
        self.assertEqual(
            payload["runtime_supervisor"]["report_error"],
            "wall_deadline_during_report_read",
        )

    def test_report_publication_overrun_cannot_publish_success_envelope(self) -> None:
        result, payload = self._run_fake_success_with_deadline_hook(delay_report_publish=True)
        self.assertEqual(result.reason, "max_duration_seconds")
        self.assertFalse(payload["passed"])
        self.assertNotEqual(payload["runtime_supervisor"]["reason"], "none")
        self.assertEqual(
            payload["runtime_supervisor"]["report_error"],
            "wall_deadline_during_report_publication",
        )

    def test_success_report_publishes_only_after_child_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            from tools.platform_load_runtime import _read_process_children

            capture_calls = [0]

            def delayed_capture(pid: int) -> tuple[int, ...]:
                capture_calls[0] += 1
                if capture_calls[0] == 1:
                    return ()
                return _read_process_children(pid)

            with patch(
                'tools.platform_load_runtime._read_process_children',
                side_effect=delayed_capture,
            ):
                result = self._supervise(
                    script,
                    root / 'final.json',
                    root / 'child.json',
                    mode='success',
                )
            self.assertEqual(result.returncode, 0)
            self.assertGreaterEqual(capture_calls[0], 2)
            self.assertIsNotNone(result.namespace_init_pid)
            self.assertTrue(result.namespace_closed)
            self.assertTrue(result.report['report_complete'])
            self.assertTrue((root / 'final.json').is_file())

    def test_completed_failed_measurement_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            result = self._supervise(
                script,
                root / 'final.json',
                root / 'child.json',
                mode='failed-report',
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.report['acceptance']['decision'], 'LOAD RUN FAILED')
            self.assertTrue(result.report['partial_work'])

    def test_malformed_child_report_becomes_closed_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            result = self._supervise(script, root / 'final.json', root / 'child.json', mode='malformed')
            self.assertFalse(result.report['passed'])
            self.assertEqual(result.report['runtime_supervisor']['report_error'], 'report_malformed')
            self.assertTrue(result.report['inflight_unknown'])

    def test_measurement_deadline_kills_hung_dns_connect_header_body_and_future(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            for phase in ('dns', 'connect', 'header', 'body', 'future'):
                report = root / f'{phase}.json'
                result = self._supervise(
                    script,
                    report,
                    root / f'{phase}.child.json',
                    mode='hang',
                    duration=0.5,
                )
                self.assertEqual(result.reason, 'max_duration_seconds', phase)
                self.assertLess(result.returncode or 0, 0, phase)
                self.assertTrue(result.report['partial_work'], phase)
                self.assertTrue(result.report['inflight_unknown'], phase)

    def test_whole_runner_deadline_is_reachable_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            result = self._supervise(
                script,
                root / 'runner.json',
                root / 'runner.child.json',
                mode='hang',
                duration=10,
                runner_minutes=0.02,
            )
            self.assertEqual(result.reason, 'max_runner_minutes')
            self.assertLess(result.returncode or 0, 0)
            self.assertEqual(
                result.report['acceptance']['decision'],
                'LOAD RUNTIME BUDGET EXCEEDED',
            )
            self.assertFalse(result.report['runtime_budget']['within_runner_budget'])
            self.assertTrue(result.report['runtime_budget']['within_duration_budget'])

    def test_sigterm_ignoring_descendant_is_killed_with_the_pid_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            heartbeat_file = root / 'descendant.heartbeat'
            started_file = root / 'worker.started'
            result = self._supervise(
                script,
                root / 'final.json',
                root / 'child.json',
                mode='descendant',
                heartbeat_file=str(heartbeat_file),
                started_file=str(started_file),
                duration=2.0,
            )
            # The namespace wrapper owns the descendant kill boundary; TERM
            # may close the wrapper itself while --kill-child reclaims PID 1.
            self.assertIn(result.returncode, (-signal.SIGTERM, -signal.SIGKILL))
            self.assertTrue(result.namespace_closed)
            self.assertTrue(started_file.exists(), 'worker never entered descendant mode')
            self.assertTrue(heartbeat_file.exists(), 'descendant heartbeat was never created')
            heartbeat_before = heartbeat_file.read_text()
            time.sleep(0.15)
            self.assertEqual(
                heartbeat_before,
                heartbeat_file.read_text(),
                'descendant survived PID-namespace teardown',
            )

    def _assert_namespace_kills_escape_attempt(self, mode: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            heartbeat_file = root / f'{mode}.heartbeat'
            started_file = root / f'{mode}.started'
            result = self._supervise(
                script,
                root / f'{mode}.final.json',
                root / f'{mode}.child.json',
                mode=mode,
                heartbeat_file=str(heartbeat_file),
                started_file=str(started_file),
                duration=2.0,
            )
            self.assertEqual(result.reason, 'max_duration_seconds')
            self.assertTrue(result.namespace_closed)
            self.assertTrue(started_file.exists())
            self.assertTrue(heartbeat_file.exists())
            heartbeat_before = heartbeat_file.read_text()
            time.sleep(0.15)
            self.assertEqual(heartbeat_before, heartbeat_file.read_text(), mode)

    def test_double_fork_and_setsid_cannot_escape_namespace(self) -> None:
        for mode in ('double-fork', 'setsid'):
            with self.subTest(mode=mode):
                self._assert_namespace_kills_escape_attempt(mode)

    def test_forced_kill_preserves_timeout_when_child_report_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            result = self._supervise(
                script,
                root / 'final.json',
                root / 'child.json',
                mode='partial-report',
                duration=1.0,
            )
            self.assertEqual(result.reason, 'max_duration_seconds')
            self.assertEqual(
                result.report['runtime_supervisor']['reason'],
                'max_duration_seconds',
            )
            self.assertEqual(
                result.report['runtime_supervisor']['report_error'],
                'report_not_complete',
            )
            self.assertEqual(
                result.report['acceptance']['decision'],
                'LOAD RUNTIME BUDGET EXCEEDED',
            )

    def test_pdeathsig_reclaims_namespace_when_supervisor_is_sigkilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            supervisor = root / 'supervisor.py'
            supervisor.write_text(
                textwrap.dedent(
                    f"""
                    import sys
                    sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
                    from pathlib import Path
                    from tools.platform_load_runtime import run_supervised
                    run_supervised(
                        worker_command=(sys.executable, {str(script)!r}),
                        report_path=Path({str(root / 'final.json')!r}),
                        worker_report_path=Path({str(root / 'child.json')!r}),
                        max_duration_seconds=30,
                        max_runner_minutes=1,
                        worker_config={{
                            'mode': 'pdeath-heartbeat',
                            'started_file': {str(root / 'worker.started')!r},
                            'heartbeat_file': {str(root / 'pdeath.heartbeat')!r},
                            'worker_pid_file': {str(root / 'worker.pid')!r},
                        }},
                        term_grace_seconds=0.05,
                        poll_seconds=0.01,
                    )
                    """
                )
            )
            process = subprocess.Popen([sys.executable, str(supervisor)])
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not (root / 'worker.started').exists():
                    time.sleep(0.01)
                self.assertTrue((root / 'pdeath.heartbeat').exists())
                heartbeat_before = (root / 'pdeath.heartbeat').read_text()
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
                time.sleep(0.25)
                stopped_at = (root / 'pdeath.heartbeat').read_text()
                time.sleep(0.15)
                self.assertEqual(stopped_at, (root / 'pdeath.heartbeat').read_text())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)

    def test_namespace_probe_is_explicit_and_has_no_unsafe_fallback(self) -> None:
        with patch.dict('os.environ', {'PLATFORM_LOAD_UNSHARE': '/definitely/missing'}, clear=False):
            result = probe_pid_namespace_capability(timeout_seconds=0.2)
        self.assertFalse(result['available'])
        # The path override is intentionally ignored.  Root callers are a
        # local capability-test block, never an unsafe fallback to a root
        # worker; hosted non-root runners exercise the exact sudo chain.
        if os.getuid() == 0:
            self.assertEqual(result['reason'], 'runner_must_be_nonroot')
        else:
            self.assertNotEqual(result['reason'], 'unshare_unavailable')

    def test_namespace_command_is_exact_root_drop_without_user_namespace(self) -> None:
        command = _namespace_command(
            worker_command=("/usr/bin/python3", "/checkout/worker.py"),
            config_path=Path("/tmp/config.json"),
            helper_path=Path("/checkout/platform_load_namespace.py"),
            expected_uid=1234,
            expected_gid=1234,
        )
        self.assertEqual(command[:6], [
            "/usr/bin/sudo", "-n", "/usr/bin/setpriv", "--pdeathsig", "SIGKILL", "--",
        ])
        self.assertEqual(command[6:12], [
            "/usr/bin/unshare", "--pid", "--fork", "--mount-proc", "--kill-child=SIGKILL", "--",
        ])
        self.assertNotIn("--user", command)
        self.assertNotIn("--map-root-user", command)
        inner = command.index("--reuid=1234")
        self.assertEqual(command[inner:inner + 13], [
            "--reuid=1234", "--regid=1234", "--clear-groups", "--no-new-privs",
            "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all",
            "--pdeathsig=SIGKILL", "--", "/usr/bin/python3",
            "/checkout/platform_load_namespace.py", "--mode", "namespace-worker",
        ])

    def test_report_schema_requires_namespace_closed_and_timeout_reason_is_primary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps({
                "worker_report_schema": WORKER_REPORT_SCHEMA,
                "report_complete": True,
            }))
            payload, error = _read_closed_report(path)
            self.assertIsNone(payload)
            self.assertEqual(error, "namespace_closed_missing_or_invalid")
        payload = _closed_failure_report(
            reason="max_duration_seconds",
            signal_number=signal.SIGKILL,
            returncode=-signal.SIGKILL,
            partial_work=True,
            inflight_unknown=True,
            report_error="report_not_complete",
            namespace_closed=False,
        )
        self.assertEqual(payload["runtime_supervisor"]["reason"], "max_duration_seconds")
        self.assertEqual(payload["runtime_supervisor"]["report_error"], "report_not_complete")
        containment = _closed_failure_report(
            reason="namespace_unclosed",
            signal_number=signal.SIGKILL,
            returncode=-signal.SIGKILL,
            partial_work=True,
            inflight_unknown=True,
            report_error="missing_or_symlink_report",
            namespace_closed=False,
        )
        self.assertEqual(containment["runtime_supervisor"]["reason"], "namespace_unclosed")
        self.assertEqual(containment["runtime_supervisor"]["report_error"], "missing_or_symlink_report")

    def test_reaped_zombie_is_closed_after_procfs_race(self) -> None:
        from unittest.mock import Mock

        process = Mock()
        process.pid = 1234
        process.poll.return_value = 0
        with (
            patch("tools.platform_load_runtime._pidfd_is_valid", return_value=True),
            patch("tools.platform_load_runtime._pidfd_is_live", return_value=False),
            patch(
                "tools.platform_load_runtime._read_process_starttime",
                side_effect=NamespaceIntegrityError("already reaped"),
            ),
            patch("tools.platform_load_runtime._read_process_state", return_value=None),
        ):
            self.assertTrue(
                _namespace_closed(
                    process,
                    pidfd=10,
                    starttime=1,
                    namespace_pid=1235,
                    namespace_pidfd=11,
                    namespace_starttime=2,
                    namespace_reaped=True,
                )
            )
        with (
            patch("tools.platform_load_runtime._pidfd_is_valid", return_value=True),
            patch("tools.platform_load_runtime._pidfd_is_live", return_value=False),
            patch(
                "tools.platform_load_runtime._read_process_starttime",
                side_effect=NamespaceIntegrityError("already reaped"),
            ),
        ):
            self.assertTrue(_captured_chain_closed({1234: (1, 10)}))

        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child_pidfd = os.pidfd_open(child.pid, 0)
        try:
            child_starttime = _read_process_starttime(child.pid)
            self.assertTrue(
                _reap_captured_chain(
                    {child.pid: (child_starttime, child_pidfd)},
                    timeout_seconds=1.0,
                    poll_seconds=0.01,
                )
            )
            self.assertIsNone(_read_process_state(child.pid))
        finally:
            os.close(child_pidfd)
            if child.poll() is None:
                child.wait(timeout=1)

    def test_namespace_handshake_rejects_wrapper_early_exit(self) -> None:
        from unittest.mock import Mock

        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        stream = os.fdopen(read_fd, "rb")
        process = Mock()
        process.poll.return_value = 125
        try:
            self.assertFalse(
                _await_namespace_ready(
                    stream,
                    process,
                    deadline=time.monotonic() + 0.2,
                )
            )
        finally:
            stream.close()

    def test_namespace_handshake_rejects_malformed_ready_without_overread(self) -> None:
        from unittest.mock import Mock

        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"NOPE\n")
        os.close(write_fd)
        stream = os.fdopen(read_fd, "rb")
        process = Mock()
        process.poll.return_value = None
        try:
            self.assertFalse(
                _await_namespace_ready(
                    stream,
                    process,
                    deadline=time.monotonic() + 0.2,
                )
            )
        finally:
            stream.close()

    def test_namespace_bootstrap_rejects_pid_reuse_and_closed_pidfd(self) -> None:
        from tools.platform_load_runtime import (
            _read_process_starttime,
            _read_process_state,
            _reap_namespace_init,
        )

        parent_pid = os.getpid()
        parent_starttime = _read_process_starttime(parent_pid)
        parent_pidfd = os.pidfd_open(parent_pid, 0)
        try:
            with self.assertRaises(NamespaceIntegrityError):
                _verify_expected_parent(
                    expected_parent_pid=parent_pid + 1,
                    expected_parent_starttime=parent_starttime,
                    parent_pidfd=parent_pidfd,
                )
            os.close(parent_pidfd)
            with self.assertRaises(NamespaceIntegrityError):
                _verify_expected_parent(
                    expected_parent_pid=parent_pid,
                    expected_parent_starttime=parent_starttime,
                    parent_pidfd=parent_pidfd,
                )
        finally:
            try:
                os.close(parent_pidfd)
            except OSError:
                pass

        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child_pidfd = os.pidfd_open(child.pid, 0)
        child_starttime = _read_process_starttime(child.pid)
        try:
            self.assertTrue(
                _reap_namespace_init(
                    child.pid,
                    namespace_starttime=child_starttime,
                    namespace_pidfd=child_pidfd,
                    timeout_seconds=1.0,
                    poll_seconds=0.01,
                )
            )
            self.assertIsNone(_read_process_state(child.pid))
        finally:
            os.close(child_pidfd)
            if child.poll() is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=1)
                except ChildProcessError:
                    pass

    def test_external_term_is_reaped_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            supervisor = root / 'supervisor.py'
            supervisor.write_text(
                textwrap.dedent(
                    f"""
                    import sys
                    sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
                    from pathlib import Path
                    from tools.platform_load_runtime import run_supervised
                    result = run_supervised(
                        worker_command=(sys.executable, {str(script)!r}),
                        report_path=Path({str(root / 'final.json')!r}),
                        worker_report_path=Path({str(root / 'child.json')!r}),
                        max_duration_seconds=10,
                        max_runner_minutes=1,
                        worker_config={{'mode': 'external-term'}},
                        term_grace_seconds=0.05,
                        poll_seconds=0.01,
                    )
                    print(result.reason)
                    """
                )
            )
            process = subprocess.Popen([sys.executable, str(supervisor)], stdout=subprocess.PIPE)
            time.sleep(0.25)
            process.send_signal(signal.SIGTERM)
            stdout, _ = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(stdout.decode().strip(), 'external_signal')
            payload = json.loads((root / 'final.json').read_text())
            self.assertTrue(payload['inflight_unknown'])


if __name__ == '__main__':
    unittest.main()
