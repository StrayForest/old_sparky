from __future__ import annotations

import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

from tests.worker_registry_manifest import EXPECTED_TASKS, REQUIRED_WORKER_QUEUES
from tools.platform_test_runner import validate_test_celery_resource_configuration


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WORKER_READY_TIMEOUT_SECONDS = 15.0
PING_TIMEOUT_SECONDS = 5.0
ROUNDTRIP_TOTAL_TIMEOUT_SECONDS = 40.0


def _wait_for_worker_ready(
    process: subprocess.Popen[str],
    *,
    timeout_seconds: float,
) -> list[str]:
    """Wait for Celery's one deterministic ``ready`` bootstep log line."""

    if process.stdout is None:
        raise AssertionError("ephemeral Celery worker stdout was not captured")
    deadline = time.monotonic() + timeout_seconds
    output: list[str] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            remaining = process.stdout.read()
            if remaining:
                output.append(remaining)
            raise AssertionError(
                "ephemeral Celery worker exited before ready:\n" + "".join(output)
            )
        readable, _, _ = select.select(
            [process.stdout],
            [],
            [],
            min(0.25, max(0.0, deadline - time.monotonic())),
        )
        if not readable:
            continue
        line = process.stdout.readline()
        if not line:
            continue
        output.append(line)
        if re.search(r"\bready\.\s*$", line, re.IGNORECASE):
            return output
    raise AssertionError(
        "ephemeral Celery worker did not reach ready within "
        f"{timeout_seconds:.1f}s:\n" + "".join(output)
    )


def _terminate_worker_process(process: subprocess.Popen[str]) -> None:
    """TERM, then KILL if necessary, and always reap the worker process group."""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5.0)
    else:
        # ``poll`` does not itself reap a child; wait is required on every
        # exit path so a failed readiness check cannot leave a zombie.
        process.wait(timeout=5.0)
    if process.stdout is not None:
        process.stdout.close()


class PlatformWorkerBrokerRoundtripIntegrationTests(unittest.TestCase):
    """Exercise broker -> worker -> task -> result on isolated CI Redis."""

    def test_ephemeral_worker_processes_safe_ping_on_every_required_queue(self) -> None:
        started_at = time.monotonic()
        configuration = validate_test_celery_resource_configuration()

        # The validator establishes the namespace contract before importing
        # the worker application or constructing a Celery client.
        from apps.platform_worker import worker

        self.assertEqual(worker.celery_app.conf.broker_url, configuration.broker_url)
        self.assertEqual(
            worker.celery_app.conf.result_backend,
            configuration.result_backend_url,
        )

        hostname = f"platform-ci-{uuid4().hex[:12]}@%h"
        command = [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "apps.platform_worker.worker:celery_app",
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--queues=" + ",".join(REQUIRED_WORKER_QUEUES),
            "--hostname=" + hostname,
            "--loglevel=INFO",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ]
        self.assertNotIn("--beat", command)
        self.assertFalse(any(argument.startswith("--schedule") for argument in command))

        worker_environment = os.environ.copy()
        worker_environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(PLATFORM_ROOT), worker_environment.get("PYTHONPATH", ""))
            if value
        )
        worker_environment["PLATFORM_RUNTIME_SERVICE"] = "worker"
        worker_environment["CELERYD_LOG_COLOR"] = "false"

        with tempfile.TemporaryDirectory(prefix="platform-celery-roundtrip-") as temp_root:
            run_root = Path(temp_root)
            worker_environment["TMPDIR"] = str(run_root)
            worker_environment["HOME"] = str(run_root)
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    command,
                    cwd=PLATFORM_ROOT,
                    env=worker_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                _wait_for_worker_ready(
                    process,
                    timeout_seconds=WORKER_READY_TIMEOUT_SECONDS,
                )

                inspector = worker.celery_app.control.inspect()
                registered = inspector.registered(timeout=2.0) or {}
                active_queues = inspector.active_queues(timeout=2.0) or {}
                self.assertEqual(len(registered), 1)
                self.assertEqual(len(active_queues), 1)

                registered_tasks = {
                    task_name
                    for task_names in registered.values()
                    for task_name in task_names
                    if task_name.startswith("platform.")
                }
                self.assertEqual(
                    registered_tasks,
                    {task.name for task in EXPECTED_TASKS},
                )
                listening_queues = {
                    queue["name"]
                    for queues in active_queues.values()
                    for queue in queues
                }
                self.assertEqual(listening_queues, set(REQUIRED_WORKER_QUEUES))

                for queue_name in REQUIRED_WORKER_QUEUES:
                    task_id = "platform-ci-ping-" + uuid4().hex
                    result = worker.ping.apply_async(
                        args=(),
                        kwargs={},
                        queue=queue_name,
                        routing_key=queue_name,
                        task_id=task_id,
                        retry=False,
                    )
                    probe_started_at = time.monotonic()
                    try:
                        self.assertEqual(
                            result.get(timeout=PING_TIMEOUT_SECONDS, propagate=True),
                            "pong",
                        )
                    finally:
                        result.forget()
                    self.assertLessEqual(
                        time.monotonic() - probe_started_at,
                        PING_TIMEOUT_SECONDS,
                    )
            finally:
                if process is not None:
                    _terminate_worker_process(process)
                leftovers = tuple(run_root.iterdir())
                self.assertEqual(
                    leftovers,
                    (),
                    "ephemeral worker left schedule/state files: "
                    + ", ".join(str(path) for path in leftovers),
                )

        self.assertLessEqual(
            time.monotonic() - started_at,
            ROUNDTRIP_TOTAL_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
