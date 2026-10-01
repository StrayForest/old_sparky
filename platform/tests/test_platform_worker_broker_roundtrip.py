from __future__ import annotations

from collections import deque
from contextlib import nullcontext
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit

from tests.worker_registry_manifest import EXPECTED_TASKS
from tools.platform_test_runner import (
    TestCeleryResourceConfiguration,
    validate_test_celery_resource_configuration,
)


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
QUEUE_SEMANTICS = ("high", "default", "low")
WORKER_READY_TIMEOUT_SECONDS = 12.0
PING_TIMEOUT_SECONDS = 5.0
ROUNDTRIP_TOTAL_TIMEOUT_SECONDS = 40.0
# Reserve a hard cleanup window after readiness/probe work.  The process and
# Redis cleanup helpers receive the absolute total deadline and never extend
# it with a fresh relative timeout.
WORK_PHASE_TIMEOUT_SECONDS = ROUNDTRIP_TOTAL_TIMEOUT_SECONDS - 5.0
PROCESS_TERM_TIMEOUT_SECONDS = 2.0
PROCESS_KILL_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 0.75
MAX_REDIS_SNAPSHOT_KEYS = 512
MAX_LOG_CHUNKS = 64
MAX_LOG_CHUNK_BYTES = 1024
LOG_READ_CHUNK_BYTES = 4096


class _BoundedWorkerLog:
    """Drain worker output continuously while retaining bounded diagnostics."""

    _READY_PATTERN = re.compile(r"\bready\.\s*(?:\n|$)", re.IGNORECASE)

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._chunks: deque[str] = deque(maxlen=MAX_LOG_CHUNKS)
        self._rolling_text = ""
        self._lock = threading.Lock()
        self.ready = threading.Event()
        self.eof = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name="platform-celery-log-drain",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(LOG_READ_CHUNK_BYTES)
                if not chunk:
                    return
                if isinstance(chunk, bytes):
                    decoded = chunk.decode("utf-8", errors="replace")
                else:
                    decoded = str(chunk)
                with self._lock:
                    self._chunks.append(decoded[-MAX_LOG_CHUNK_BYTES:])
                    self._rolling_text = (self._rolling_text + decoded)[-8192:]
                    ready = self._READY_PATTERN.search(self._rolling_text) is not None
                if ready:
                    self.ready.set()
        finally:
            self.eof.set()

    def diagnostics(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def join(self, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        self._thread.join(timeout=remaining)


def _wait_for_worker_ready(
    process: subprocess.Popen[bytes],
    log: _BoundedWorkerLog,
    *,
    deadline: float,
) -> None:
    """Wait for Celery's deterministic ready line within an absolute budget."""

    while not log.ready.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                "ephemeral Celery worker did not reach ready before the work "
                f"deadline:\n{log.diagnostics()}"
            )
        if process.poll() is not None:
            log.join(deadline)
            raise AssertionError(
                "ephemeral Celery worker exited before ready:\n" + log.diagnostics()
            )
        log.ready.wait(timeout=min(0.25, remaining))


def _terminate_worker_process(
    process: subprocess.Popen[bytes],
    log: _BoundedWorkerLog | None,
    *,
    deadline: float,
) -> None:
    """TERM, then KILL if necessary, and reap the process group by deadline."""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=min(PROCESS_TERM_TIMEOUT_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining))
    else:
        # ``poll`` does not itself reap a child; wait is required on every
        # exit path so a failed readiness check cannot leave a zombie.
        remaining = max(0.0, deadline - time.monotonic())
        process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining))
    if log is not None:
        log.join(deadline)
    elif process.stdout is not None:
        process.stdout.close()


def _snapshot_redis_keys(client: object) -> tuple[bytes, ...]:
    """Take a bounded key snapshot before a dedicated test DB is flushed."""

    keys: list[bytes] = []
    for index, key in enumerate(client.scan_iter(match="*", count=100)):
        if index >= MAX_REDIS_SNAPSHOT_KEYS:
            break
        keys.append(key)
    return tuple(sorted(keys))


def _flush_isolated_redis_db(client: object, *, label: str) -> None:
    """Flush one prevalidated CI-only logical DB and prove it is empty."""

    client.flushdb()
    if client.dbsize() != 0:
        raise AssertionError(f"{label} Redis DB was not empty after FLUSHDB")
    leftovers = _snapshot_redis_keys(client)
    if leftovers:
        raise AssertionError(
            f"{label} Redis DB retained keys after cleanup: {leftovers!r}"
        )


def _assert_safe_redis_mutation_targets(
    configuration: TestCeleryResourceConfiguration,
) -> None:
    """Recheck exact loopback/DB targets immediately before opening clients."""

    if configuration.app_redis_database != "15":
        raise AssertionError("application Redis DB must remain DB15")
    if configuration.broker_database != "13":
        raise AssertionError("Celery broker Redis DB must be DB13")
    if configuration.result_backend_database != "14":
        raise AssertionError("Celery result Redis DB must be DB14")
    if {
        configuration.app_redis_database,
        configuration.broker_database,
        configuration.result_backend_database,
    } != {"13", "14", "15"}:
        raise AssertionError("Celery and application Redis DB namespaces must be distinct")

    for label, url, expected_path in (
        ("broker", configuration.broker_url, "/13"),
        ("result", configuration.result_backend_url, "/14"),
    ):
        parts = urlsplit(url)
        if parts.scheme not in {"redis", "rediss"} or parts.path != expected_path:
            raise AssertionError(f"refusing unsafe {label} Redis target: {url!r}")
        if parts.username is not None or parts.password is not None:
            raise AssertionError(f"refusing {label} Redis URL with userinfo")
        if parts.hostname is None:
            raise AssertionError(f"refusing {label} Redis URL without a host")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError as exc:
            raise AssertionError(f"refusing non-literal {label} Redis host") from exc
        if not address.is_loopback or "%" in parts.hostname:
            raise AssertionError(f"refusing non-loopback {label} Redis host")


def _task_result_keys(client: object, task_id: str) -> tuple[bytes, ...]:
    return tuple(client.scan_iter(match=f"*{task_id}*", count=100))


class PlatformWorkerBrokerRoundtripIntegrationTests(unittest.TestCase):
    """Exercise broker -> worker -> task -> result on isolated CI Redis."""

    def test_ephemeral_worker_processes_safe_ping_on_every_required_queue(self) -> None:
        started_at = time.monotonic()
        total_deadline = started_at + ROUNDTRIP_TOTAL_TIMEOUT_SECONDS
        work_deadline = started_at + WORK_PHASE_TIMEOUT_SECONDS
        configuration = validate_test_celery_resource_configuration()
        _assert_safe_redis_mutation_targets(configuration)

        # The validator and the target recheck establish the namespace
        # contract before importing the worker app or constructing clients.
        from redis import Redis

        from apps.platform_worker import worker

        self.assertEqual(worker.celery_app.conf.broker_url, configuration.broker_url)
        self.assertEqual(
            worker.celery_app.conf.result_backend,
            configuration.result_backend_url,
        )
        self.assertIs(
            worker.celery_app.conf.task_always_eager,
            False,
            "roundtrip must publish through the broker, never execute eagerly",
        )

        broker_client: object | None = None
        result_client: object | None = None
        process: subprocess.Popen[bytes] | None = None
        log: _BoundedWorkerLog | None = None
        run_root: Path | None = None
        cleanup_errors: list[str] = []
        primary_exception: BaseException | None = None

        try:
            broker_client = Redis.from_url(
                configuration.broker_url,
                decode_responses=False,
                socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            )
            result_client = Redis.from_url(
                configuration.result_backend_url,
                decode_responses=False,
                socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            )
            # These snapshots make the destructive operation auditable.  The
            # DBs are CI-dedicated and the exact loopback/DB checks above are
            # deliberately adjacent to the first mutation.  DB15 is never
            # opened, flushed or otherwise touched by this contour.
            assert broker_client is not None
            assert result_client is not None
            _snapshot_redis_keys(broker_client)
            _snapshot_redis_keys(result_client)
            broker_client.ping()
            result_client.ping()
            _flush_isolated_redis_db(broker_client, label="Celery broker")
            _flush_isolated_redis_db(result_client, label="Celery result")

            run_id = secrets.token_hex(16)
            queue_specs = tuple(
                (semantic, f"platform-ci-{run_id}-{semantic}")
                for semantic in QUEUE_SEMANTICS
            )
            self.assertEqual(
                tuple(semantic for semantic, _ in queue_specs),
                QUEUE_SEMANTICS,
            )
            queue_names = tuple(queue_name for _, queue_name in queue_specs)
            self.assertEqual(len(set(queue_names)), len(QUEUE_SEMANTICS))
            self.assertTrue(all(run_id in queue_name for queue_name in queue_names))

            # Publish while no worker is running.  This makes observation of
            # each broker list deterministic and independent of worker speed;
            # the subsequently launched worker consumes these exact messages.
            probe_results: list[tuple[str, str, object]] = []
            for semantic, queue_name in queue_specs:
                if time.monotonic() >= work_deadline:
                    raise AssertionError("roundtrip work deadline expired before publish")
                task_id = f"platform-ci-ping-{run_id}-{secrets.token_hex(12)}"
                result = worker.ping.apply_async(
                    args=(),
                    kwargs={},
                    queue=queue_name,
                    routing_key=queue_name,
                    task_id=task_id,
                    retry=False,
                )
                broker_keys = _snapshot_redis_keys(broker_client)
                matching_keys = tuple(
                    key for key in broker_keys if queue_name.encode() in key
                )
                self.assertTrue(
                    matching_keys,
                    f"{semantic} publish did not create an observable broker key",
                )
                self.assertGreaterEqual(
                    broker_client.llen(queue_name),
                    1,
                    f"{semantic} publish did not leave a broker message",
                )
                self.assertEqual(
                    _task_result_keys(result_client, task_id),
                    (),
                    f"{semantic} result appeared before worker execution",
                )
                probe_results.append((semantic, task_id, result))

            hostname = f"platform-ci-{run_id}@%h"
            command = [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "apps.platform_worker.worker:celery_app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--queues=" + ",".join(queue_names),
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

            # ``nullcontext`` deliberately leaves cleanup to the outer
            # ``finally``: the worker must be reaped before this exact
            # temporary directory is removed, including failure/cancellation.
            with nullcontext(tempfile.mkdtemp(prefix="platform-celery-roundtrip-")) as temp_root:
                run_root = Path(temp_root)
                worker_environment["TMPDIR"] = str(run_root)
                worker_environment["HOME"] = str(run_root)
                process = subprocess.Popen(
                    command,
                    cwd=PLATFORM_ROOT,
                    env=worker_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                assert process.stdout is not None
                log = _BoundedWorkerLog(process.stdout)
                _wait_for_worker_ready(
                    process,
                    log,
                    deadline=min(
                        work_deadline,
                        started_at + WORKER_READY_TIMEOUT_SECONDS,
                    ),
                )

                inspector = worker.celery_app.control.inspect()
                inspect_timeout = min(2.0, max(0.1, work_deadline - time.monotonic()))
                registered = inspector.registered(timeout=inspect_timeout) or {}
                active_queues = inspector.active_queues(timeout=inspect_timeout) or {}
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
                self.assertEqual(
                    listening_queues,
                    set(queue_names),
                    "the ephemeral worker must have no foreign consumer/queue",
                )

                for semantic, task_id, result in probe_results:
                    probe_started_at = time.monotonic()
                    remaining = work_deadline - probe_started_at
                    self.assertGreater(remaining, 0, f"{semantic} probe missed deadline")
                    self.assertEqual(
                        result.get(
                            timeout=min(PING_TIMEOUT_SECONDS, remaining),
                            propagate=True,
                        ),
                        "pong",
                        f"{semantic} queue returned an unexpected ping result",
                    )
                    self.assertEqual(result.state, "SUCCESS")
                    result_keys = _task_result_keys(result_client, task_id)
                    self.assertTrue(
                        result_keys,
                        f"{semantic} result key was not independently observable",
                    )
                    raw_result = result_client.get(result_keys[0])
                    self.assertIsNotNone(raw_result)
                    decoded_result = json.loads(raw_result.decode("utf-8"))
                    self.assertEqual(decoded_result["status"], "SUCCESS")
                    self.assertEqual(decoded_result["result"], "pong")
                    self.assertLessEqual(
                        time.monotonic() - probe_started_at,
                        PING_TIMEOUT_SECONDS,
                        f"{semantic} probe exceeded its five-second bound",
                    )

                self.assertLessEqual(time.monotonic(), work_deadline)
        except BaseException as exc:
            primary_exception = exc
            raise
        finally:
            if process is not None:
                try:
                    _terminate_worker_process(process, log, deadline=total_deadline)
                except BaseException as exc:
                    cleanup_errors.append(f"worker cleanup: {exc}")
            elif log is not None:
                try:
                    log.join(total_deadline)
                except BaseException as exc:
                    cleanup_errors.append(f"worker log cleanup: {exc}")

            for client, label in (
                (broker_client, "Celery broker"),
                (result_client, "Celery result"),
            ):
                if client is None:
                    continue
                try:
                    _flush_isolated_redis_db(client, label=label)
                except BaseException as exc:
                    cleanup_errors.append(f"{label} Redis cleanup: {exc}")
                finally:
                    try:
                        client.close()
                    except BaseException as exc:
                        cleanup_errors.append(f"{label} Redis close: {exc}")

            if run_root is not None:
                try:
                    leftovers = tuple(run_root.iterdir())
                    if leftovers:
                        cleanup_errors.append(
                            "ephemeral worker left schedule/state files: "
                            + ", ".join(str(path) for path in leftovers)
                        )
                except BaseException as exc:
                    cleanup_errors.append(f"temporary-state cleanup: {exc}")
                try:
                    if run_root.exists():
                        shutil.rmtree(run_root)
                except BaseException as exc:
                    cleanup_errors.append(f"temporary-directory cleanup: {exc}")

            if cleanup_errors and primary_exception is None:
                raise AssertionError("; ".join(cleanup_errors))

        self.assertLessEqual(
            time.monotonic() - started_at,
            ROUNDTRIP_TOTAL_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
