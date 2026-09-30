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

from tools.platform_load_runtime import (
    LoadRuntimeBudget,
    LoadRuntimeBudgetExceeded,
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
            'worker_report_schema': 1,
            'report_complete': True,
            'passed': True,
            'acceptance': {'passed': True, 'decision': 'PASS'},
        }) + '\\n')
        raise SystemExit(0)
    if mode == 'failed-report':
        report.write_text(json.dumps({
            'worker_report_schema': 1,
            'report_complete': True,
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
        child_pid_file = Path(config['child_pid_file'])
        child = subprocess.Popen([
            sys.executable, '-c',
            "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(os.environ['PID_FILE'], 'w').write(str(os.getpid())); time.sleep(60)",
        ], env={**os.environ, 'PID_FILE': str(child_pid_file)})
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
            max_duration_seconds=float(config.pop('duration', 0.15)),
            max_runner_minutes=float(config.pop('runner_minutes', 1.0)),
            worker_config=config,
            term_grace_seconds=0.05,
            poll_seconds=0.01,
        )

    def test_success_report_publishes_only_after_child_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            result = self._supervise(script, root / 'final.json', root / 'child.json', mode='success')
            self.assertEqual(result.returncode, 0)
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
                )
                self.assertEqual(result.reason, 'max_duration_seconds', phase)
                self.assertTrue(result.killed, phase)
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
                runner_minutes=0.001,
            )
            self.assertEqual(result.reason, 'max_runner_minutes')
            self.assertLess(result.returncode or 0, 0)
            self.assertEqual(
                result.report['acceptance']['decision'],
                'LOAD RUNTIME BUDGET EXCEEDED',
            )
            self.assertFalse(result.report['runtime_budget']['within_runner_budget'])
            self.assertTrue(result.report['runtime_budget']['within_duration_budget'])

    def test_sigterm_ignoring_descendant_is_killed_with_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'worker.py'
            script.write_text(WORKER_SCRIPT)
            child_pid_file = root / 'child.pid'
            result = self._supervise(
                script,
                root / 'final.json',
                root / 'child.json',
                mode='descendant',
                child_pid_file=str(child_pid_file),
            )
            # The worker ignores TERM, so the supervisor must report the
            # final process-group KILL after reaping the whole group.
            self.assertEqual(result.returncode, -9)
            self.assertEqual(result.signal, signal.SIGKILL)
            child_pid = int(child_pid_file.read_text())
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail('descendant survived process-group kill')

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
