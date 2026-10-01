from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
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
from unittest.mock import patch
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
REDIS_DELETE_BATCH_SIZE = 64
MAX_LOG_CHUNKS = 64
MAX_LOG_CHUNK_BYTES = 1024
LOG_READ_CHUNK_BYTES = 4096
KOMBU_PRIORITY_STEPS = (0, 3, 6, 9)
KOMBU_PRIORITY_SEPARATOR = b"\x06\x16"


class ForeignRedisKeyError(AssertionError):
    """A test namespace was not empty or gained an unowned key."""


class CleanupFailure(AssertionError):
    """The bounded worker/Redis cleanup contract could not be proven."""


class _BoundedWorkerLog:
    """Drain worker output continuously while retaining bounded diagnostics."""

    _READY_PATTERN = re.compile(r"\bready\.\s*(?:\n|$)", re.IGNORECASE)

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._chunks: deque[str] = deque(maxlen=MAX_LOG_CHUNKS)
        self._rolling_text = ""
        self._lock = threading.Lock()
        self._drain_error: BaseException | None = None
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
        except BaseException as exc:
            # Closing the pipe during bounded teardown can interrupt a blocked
            # read.  Record that fact for the caller instead of leaking an
            # unhandled exception from the daemon drain thread.
            with self._lock:
                self._drain_error = exc
        finally:
            self.eof.set()

    def diagnostics(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def join(self, deadline: float) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        self._thread.join(timeout=remaining)

    @property
    def thread_alive(self) -> bool:
        return self._thread.is_alive()

    @property
    def drain_error(self) -> BaseException | None:
        with self._lock:
            return self._drain_error


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
    send_signal: Callable[[int, signal.Signals], None] | None = None,
) -> None:
    """TERM, bounded wait, KILL fallback and bounded log-thread join."""

    send_signal = send_signal or os.killpg
    errors: list[str] = []
    running = True
    must_kill = False
    try:
        running = process.poll() is None
    except BaseException as exc:
        errors.append(f"worker poll: {exc}")
        # Treat an unreadable process state as potentially live and make the
        # escalation path the safe default.
        running = True
        must_kill = True

    if running:
        try:
            send_signal(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except BaseException as exc:
            errors.append(f"worker TERM: {exc}")
            must_kill = True
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=min(PROCESS_TERM_TIMEOUT_SECONDS, remaining))
            must_kill = must_kill or process.poll() is None
        except subprocess.TimeoutExpired:
            # Expected escalation path: the hard deadline still governs the
            # KILL wait below.
            must_kill = True
        except BaseException as exc:
            errors.append(f"worker TERM wait: {exc}")
            must_kill = True

    if must_kill:
        try:
            send_signal(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as exc:
            errors.append(f"worker KILL: {exc}")
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining))
        except BaseException as exc:
            errors.append(f"worker KILL wait: {exc}")
            # A wait implementation can fail after the signal was delivered.
            # Make one final bounded reap attempt before reporting the child
            # as live; this also exercises the no-zombie contract in tests.
            try:
                remaining = max(0.0, deadline - time.monotonic())
                process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining))
            except BaseException as reap_exc:
                errors.append(f"worker final reap: {reap_exc}")
    else:
        # ``poll`` may reap an exited child, but this explicit bounded wait
        # keeps the contract true for every path and for test doubles.
        try:
            remaining = max(0.0, deadline - time.monotonic())
            process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining))
        except BaseException as exc:
            errors.append(f"worker reap: {exc}")

    try:
        if process.poll() is None:
            errors.append("worker remained live after TERM/KILL cleanup")
    except BaseException as exc:
        errors.append(f"worker final poll: {exc}")

    try:
        if log is not None:
            stream = getattr(log, "_stream", None)
            if stream is None:
                stream = getattr(process, "stdout", None)
            if stream is not None:
                try:
                    stream.close()
                except BaseException as exc:
                    errors.append(f"worker stdout close: {exc}")
            log.join(deadline)
            if log.thread_alive:
                errors.append("worker log-drain thread remained live after EOF deadline")
            eof = getattr(log, "eof", None)
            if eof is not None and not eof.is_set():
                errors.append("worker log-drain did not reach EOF before deadline")
            drain_error = getattr(log, "drain_error", None)
            if drain_error is not None:
                errors.append(f"worker log drain: {drain_error}")
        elif process.stdout is not None:
            process.stdout.close()
    except BaseException as exc:
        errors.append(f"worker log join: {exc}")

    if errors:
        raise CleanupFailure("; ".join(errors))


def _snapshot_redis_keys(
    client: object,
    *,
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    """Take a bounded key snapshot from a dedicated Redis DB."""

    keys: list[bytes] = []
    for index, key in enumerate(client.scan_iter(match="*", count=100)):
        if deadline is not None and time.monotonic() >= deadline:
            raise CleanupFailure("Redis key snapshot exceeded its absolute deadline")
        if index >= MAX_REDIS_SNAPSHOT_KEYS:
            break
        keys.append(key)
    return tuple(sorted(keys))


def _require_empty_redis_namespace(client: object, *, label: str) -> None:
    """Fail closed on any pre-existing key without mutating the DB."""

    key_count = client.dbsize()
    keys = _snapshot_redis_keys(client)
    if key_count or keys:
        raise ForeignRedisKeyError(
            f"{label} Redis DB is not empty before the liveness run: {keys!r}"
        )


def _record_owned_redis_keys(owned_keys: set[bytes], client: object) -> None:
    owned_keys.update(_snapshot_redis_keys(client))


def _physical_queue_key(queue_name: str, priority: int) -> bytes:
    """Mirror Kombu Redis ``_q_for_pri`` with its reviewed priority steps."""

    effective_priority = max(
        step for step in KOMBU_PRIORITY_STEPS if step <= priority
    )
    queue_key = queue_name.encode("utf-8")
    if effective_priority == 0:
        return queue_key
    return queue_key + KOMBU_PRIORITY_SEPARATOR + str(effective_priority).encode("ascii")


def _all_physical_queue_keys(queue_name: str, key_prefix: bytes) -> frozenset[bytes]:
    return frozenset(
        key_prefix + _physical_queue_key(queue_name, priority)
        for priority in KOMBU_PRIORITY_STEPS
    )


def _observe_broker_priority_keys(
    client: object,
    *,
    queue_name: str,
    priority: int,
    key_prefix: bytes,
) -> tuple[bytes, ...]:
    """Observe the exact Redis list key selected by Kombu priority routing."""

    snapshot = set(_snapshot_redis_keys(client))
    candidates = _all_physical_queue_keys(queue_name, key_prefix)
    observed = tuple(sorted(snapshot.intersection(candidates)))
    expected_key = key_prefix + _physical_queue_key(queue_name, priority)
    if expected_key not in observed:
        raise AssertionError(
            f"priority {priority} publish did not create Kombu key {expected_key!r}; "
            f"observed={observed!r}"
        )
    if client.type(expected_key) != b"list":
        raise AssertionError(f"Kombu priority key is not a Redis list: {expected_key!r}")
    for observed_key in observed:
        if client.type(observed_key) != b"list":
            raise AssertionError(
                f"observed Kombu priority key is not a Redis list: {observed_key!r}"
            )
    return observed


def _delete_owned_redis_keys(
    client: object,
    *,
    label: str,
    initially_empty: bool,
    owned_keys: Iterable[bytes],
    allow_prefixes: Iterable[bytes],
    allow_fragments: Iterable[bytes] = (),
    deadline: float,
) -> None:
    """Delete only observed/run-allowlisted keys; never use FLUSHDB."""

    if not initially_empty:
        return
    current = set(_snapshot_redis_keys(client, deadline=deadline))
    owned = set(owned_keys)
    prefixes = tuple(allow_prefixes)
    fragments = tuple(allow_fragments)
    allowed = {
        key
        for key in current
        if key in owned
        or any(key.startswith(prefix) for prefix in prefixes)
        or any(fragment in key for fragment in fragments)
    }
    foreign = current - allowed
    if foreign:
        # Do not delete even the owned subset when foreign data is present:
        # this makes the sentinel/failure contract visibly non-destructive.
        raise ForeignRedisKeyError(
            f"{label} Redis DB gained unowned keys; refusing deletion: {sorted(foreign)!r}"
        )
    ordered_keys = tuple(sorted(allowed))
    for offset in range(0, len(ordered_keys), REDIS_DELETE_BATCH_SIZE):
        if time.monotonic() >= deadline:
            raise CleanupFailure(f"{label} Redis cleanup exceeded its absolute deadline")
        batch = ordered_keys[offset : offset + REDIS_DELETE_BATCH_SIZE]
        if batch:
            client.delete(*batch)
    if time.monotonic() >= deadline:
        raise CleanupFailure(f"{label} Redis cleanup exceeded its absolute deadline")
    if client.dbsize() != 0 or _snapshot_redis_keys(client, deadline=deadline):
        raise CleanupFailure(f"{label} Redis DB retained keys after owned-key cleanup")


def _cleanup_roundtrip_resources(
    *,
    process: subprocess.Popen[bytes] | None,
    log: _BoundedWorkerLog | None,
    broker_client: object | None,
    result_client: object | None,
    broker_initially_empty: bool,
    result_initially_empty: bool,
    broker_owned_keys: Iterable[bytes],
    result_owned_keys: Iterable[bytes],
    broker_allow_prefixes: Iterable[bytes],
    result_allow_fragments: Iterable[bytes],
    run_root: Path | None,
    deadline: float,
) -> list[str]:
    """Run idempotent nested cleanup and return every bounded failure."""

    errors: list[str] = []
    try:
        if process is not None:
            try:
                _terminate_worker_process(process, log, deadline=deadline)
            except BaseException as exc:
                errors.append(f"worker cleanup: {exc}")
        elif log is not None:
            try:
                stream = getattr(log, "_stream", None)
                if stream is not None:
                    stream.close()
                log.join(deadline)
                if log.thread_alive:
                    errors.append("worker log-drain thread remained live")
                eof = getattr(log, "eof", None)
                if eof is not None and not eof.is_set():
                    errors.append("worker log-drain did not reach EOF")
            except BaseException as exc:
                errors.append(f"worker log cleanup: {exc}")
    finally:
        for client, label, initially_empty, owned_keys, prefixes, fragments in (
            (
                broker_client,
                "Celery broker",
                broker_initially_empty,
                broker_owned_keys,
                broker_allow_prefixes,
                (),
            ),
            (
                result_client,
                "Celery result",
                result_initially_empty,
                result_owned_keys,
                (),
                result_allow_fragments,
            ),
        ):
            if client is None:
                continue
            try:
                _delete_owned_redis_keys(
                    client,
                    label=label,
                    initially_empty=initially_empty,
                    owned_keys=owned_keys,
                    allow_prefixes=prefixes,
                    allow_fragments=fragments,
                    deadline=deadline,
                )
            except BaseException as exc:
                errors.append(f"{label} Redis cleanup: {exc}")
            finally:
                try:
                    client.close()
                except BaseException as exc:
                    errors.append(f"{label} Redis close: {exc}")

        if run_root is not None:
            try:
                if run_root.exists():
                    leftovers = tuple(run_root.iterdir())
                    if leftovers:
                        errors.append(
                            "ephemeral worker left schedule/state files: "
                            + ", ".join(str(path) for path in leftovers)
                        )
                    if time.monotonic() >= deadline:
                        errors.append(
                            "temporary-directory cleanup exceeded its absolute deadline"
                        )
                    else:
                        shutil.rmtree(run_root)
                        if run_root.exists():
                            errors.append("temporary directory remained after cleanup")
            except BaseException as exc:
                errors.append(f"temporary-directory cleanup: {exc}")
    return errors


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

        from redis import Redis
        from apps.platform_worker.worker import celery_app as production_celery_app
        from tests.worker_liveness_probe import (
            PROBE_QUEUE_ENV_NAMES,
            PROBE_ROUTE_PRIORITIES,
            PROBE_RUN_ENV_NAME,
            PROBE_TASK_NAMES,
            configure_probe_application,
        )

        broker_client: object | None = None
        result_client: object | None = None
        process: subprocess.Popen[bytes] | None = None
        log: _BoundedWorkerLog | None = None
        run_root: Path | None = None
        broker_initially_empty = False
        result_initially_empty = False
        broker_owned_keys: set[bytes] = set()
        result_owned_keys: set[bytes] = set()
        broker_allow_prefixes: tuple[bytes, ...] = ()
        task_ids: list[str] = []
        probe_app: object | None = None
        probe_app_snapshot: tuple[object, dict, tuple, dict, object] | None = None
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
            assert broker_client is not None
            assert result_client is not None
            broker_client.ping()
            result_client.ping()
            _require_empty_redis_namespace(broker_client, label="Celery broker")
            broker_initially_empty = True
            _require_empty_redis_namespace(result_client, label="Celery result")
            result_initially_empty = True

            run_id = secrets.token_hex(16)
            queue_specs = tuple(
                (semantic, f"platform-ci-{run_id}-{semantic}")
                for semantic in QUEUE_SEMANTICS
            )
            queue_names = tuple(queue_name for _, queue_name in queue_specs)
            queue_map = dict(queue_specs)
            broker_key_prefix = f"platform-ci-{run_id}-".encode("ascii")
            broker_allow_prefixes = (broker_key_prefix,)
            self.assertEqual(
                tuple(semantic for semantic, _ in queue_specs),
                QUEUE_SEMANTICS,
            )
            self.assertEqual(len(set(queue_names)), len(QUEUE_SEMANTICS))
            self.assertTrue(all(run_id in queue_name for queue_name in queue_names))

            probe_app_snapshot = (
                production_celery_app,
                dict(production_celery_app.conf.task_routes or {}),
                tuple(production_celery_app.conf.task_queues or ()),
                dict(production_celery_app.conf.broker_transport_options or {}),
                production_celery_app.conf.task_always_eager,
            )
            probe_app = configure_probe_application(run_id, queue_map)
            self.assertEqual(probe_app.conf.broker_url, configuration.broker_url)
            self.assertEqual(probe_app.conf.result_backend, configuration.result_backend_url)
            self.assertIs(
                probe_app.conf.task_always_eager,
                False,
                "roundtrip must publish through the broker, never execute eagerly",
            )

            # Resolve each test-only safe task through Celery's actual router.
            # No queue/routing_key override is supplied to apply_async below.
            probe_results: list[tuple[str, str, object, float]] = []
            for semantic, queue_name in queue_specs:
                if time.monotonic() >= work_deadline:
                    raise AssertionError("roundtrip work deadline expired before publish")
                task_name = PROBE_TASK_NAMES[semantic]
                route = probe_app.amqp.router.route({}, task_name, (), {})
                resolved_queue = route.get("queue")
                resolved_queue_name = getattr(resolved_queue, "name", resolved_queue)
                self.assertEqual(
                    resolved_queue_name,
                    queue_name,
                    f"{semantic} route did not resolve to its unique physical queue",
                )
                self.assertEqual(route.get("priority"), PROBE_ROUTE_PRIORITIES[semantic])
                task_id = f"platform-ci-ping-{run_id}-{secrets.token_hex(12)}"
                task_ids.append(task_id)
                published_at = time.monotonic()
                result = probe_app.tasks[task_name].apply_async(
                    args=(),
                    kwargs={},
                    task_id=task_id,
                    retry=False,
                )
                observed_priority_keys = _observe_broker_priority_keys(
                    broker_client,
                    queue_name=queue_name,
                    priority=PROBE_ROUTE_PRIORITIES[semantic],
                    key_prefix=broker_key_prefix,
                )
                self.assertTrue(observed_priority_keys)
                _record_owned_redis_keys(broker_owned_keys, broker_client)
                self.assertEqual(
                    _task_result_keys(result_client, task_id),
                    (),
                    f"{semantic} result appeared before worker execution",
                )
                probe_results.append((semantic, task_id, result, published_at))

            hostname = f"platform-ci-{run_id}@%h"
            command = [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "tests.worker_liveness_probe:celery_app",
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
            worker_environment[PROBE_RUN_ENV_NAME] = run_id
            for semantic, queue_name in queue_specs:
                worker_environment[PROBE_QUEUE_ENV_NAMES[semantic]] = queue_name

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
                if process.stdout is None:
                    raise AssertionError("ephemeral Celery worker stdout was not captured")
                log = _BoundedWorkerLog(process.stdout)
                _wait_for_worker_ready(
                    process,
                    log,
                    deadline=min(work_deadline, started_at + WORKER_READY_TIMEOUT_SECONDS),
                )

                inspector = probe_app.control.inspect()
                inspect_timeout = min(2.0, max(0.1, work_deadline - time.monotonic()))
                registered = inspector.registered(timeout=inspect_timeout) or {}
                active_queues = inspector.active_queues(timeout=inspect_timeout) or {}
                self.assertEqual(len(registered), 1)
                self.assertEqual(len(active_queues), 1)
                registered_platform = {
                    task_name
                    for task_names in registered.values()
                    for task_name in task_names
                    if task_name.startswith("platform.")
                }
                expected_platform_tasks = {task.name for task in EXPECTED_TASKS}
                probe_task_names = set(PROBE_TASK_NAMES.values())
                self.assertEqual(
                    registered_platform - probe_task_names,
                    expected_platform_tasks,
                )
                self.assertEqual(registered_platform & probe_task_names, probe_task_names)
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
                _record_owned_redis_keys(broker_owned_keys, broker_client)

                for semantic, task_id, result, published_at in probe_results:
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
                    self.assertLessEqual(
                        time.monotonic() - published_at,
                        WORKER_READY_TIMEOUT_SECONDS + PING_TIMEOUT_SECONDS,
                    )
                    _record_owned_redis_keys(result_owned_keys, result_client)

                _record_owned_redis_keys(broker_owned_keys, broker_client)
                _record_owned_redis_keys(result_owned_keys, result_client)
                self.assertLessEqual(time.monotonic(), work_deadline)
        except BaseException as exc:
            primary_exception = exc
            raise
        finally:
            cleanup_errors.extend(
                _cleanup_roundtrip_resources(
                    process=process,
                    log=log,
                    broker_client=broker_client,
                    result_client=result_client,
                    broker_initially_empty=broker_initially_empty,
                    result_initially_empty=result_initially_empty,
                    broker_owned_keys=broker_owned_keys,
                    result_owned_keys=result_owned_keys,
                    broker_allow_prefixes=broker_allow_prefixes,
                    result_allow_fragments=tuple(
                        task_id.encode("ascii") for task_id in task_ids
                    ),
                    run_root=run_root,
                    deadline=total_deadline,
                )
            )
            if probe_app_snapshot is not None:
                try:
                    app, original_routes, original_queues, original_transport, original_eager = (
                        probe_app_snapshot
                    )
                    for probe_task_name in (
                        "platform.ci_liveness_probe_high",
                        "platform.ci_liveness_probe_default",
                        "platform.ci_liveness_probe_low",
                    ):
                        app.tasks.pop(probe_task_name, None)
                    app.conf.update(
                        task_routes=original_routes,
                        task_queues=original_queues,
                        broker_transport_options=original_transport,
                        task_always_eager=original_eager,
                    )
                except BaseException as exc:
                    cleanup_errors.append(f"probe application restore: {exc}")
            if cleanup_errors:
                if primary_exception is None:
                    raise CleanupFailure("; ".join(cleanup_errors))
                primary_exception.add_note(
                    "roundtrip cleanup errors: " + "; ".join(cleanup_errors)
                )

        self.assertLessEqual(
            time.monotonic() - started_at,
            ROUNDTRIP_TOTAL_TIMEOUT_SECONDS,
        )


class _MemoryRedis:
    """Tiny deterministic client double for namespace cleanup contracts."""

    def __init__(self, keys: Iterable[bytes] = ()) -> None:
        self.keys = set(keys)
        self.delete_calls: list[tuple[bytes, ...]] = []
        self.closed = False
        self.delete_error: BaseException | None = None

    def scan_iter(self, *, match: str, count: int) -> Iterable[bytes]:
        return iter(sorted(self.keys))

    def dbsize(self) -> int:
        return len(self.keys)

    def delete(self, *keys: bytes) -> int:
        self.delete_calls.append(tuple(keys))
        if self.delete_error is not None:
            raise self.delete_error
        before = len(self.keys)
        self.keys.difference_update(keys)
        return before - len(self.keys)

    def type(self, key: bytes) -> bytes:
        return b"list"

    def close(self) -> None:
        self.closed = True


class _FakeLog:
    thread_alive = False

    def __init__(self, *, join_error: BaseException | None = None) -> None:
        self.join_calls = 0
        self.join_error = join_error

    def join(self, deadline: float) -> None:
        self.join_calls += 1
        if self.join_error is not None:
            raise self.join_error


class _FakeProcess:
    pid = 7171
    stdout = None

    def __init__(self, wait_errors: Iterable[BaseException] = ()) -> None:
        self.running = True
        self.wait_errors = list(wait_errors)
        self.wait_calls: list[float] = []

    def poll(self) -> int | None:
        return None if self.running else 0

    def wait(self, *, timeout: float) -> int:
        self.wait_calls.append(timeout)
        if self.wait_errors:
            error = self.wait_errors.pop(0)
            raise error
        self.running = False
        return 0


class PlatformWorkerCleanupContractTests(unittest.TestCase):
    def test_foreign_sentinel_fails_closed_without_mutation(self) -> None:
        client = _MemoryRedis({b"foreign-sentinel"})

        with self.assertRaises(ForeignRedisKeyError):
            _require_empty_redis_namespace(client, label="sentinel")

        self.assertEqual(client.keys, {b"foreign-sentinel"})
        self.assertEqual(client.delete_calls, [])

    def test_cleanup_refuses_foreign_key_before_deleting_owned_keys(self) -> None:
        client = _MemoryRedis({b"owned", b"foreign"})

        with self.assertRaises(ForeignRedisKeyError):
            _delete_owned_redis_keys(
                client,
                label="sentinel",
                initially_empty=True,
                owned_keys={b"owned"},
                allow_prefixes=(),
                deadline=time.monotonic() + 2,
            )

        self.assertEqual(client.keys, {b"owned", b"foreign"})
        self.assertEqual(client.delete_calls, [])

    def test_cleanup_deletes_owned_priority_and_result_keys_only(self) -> None:
        client = _MemoryRedis(
            {
                b"platform-ci-run-queue",
                b"platform-ci-run-queue\x06\x169",
                b"celery-task-meta-platform-ci-ping-run-id",
            }
        )

        _delete_owned_redis_keys(
            client,
            label="owned",
            initially_empty=True,
            owned_keys={b"platform-ci-run-queue"},
            allow_prefixes=(b"platform-ci-run-",),
            allow_fragments=(b"platform-ci-ping-run-id",),
            deadline=time.monotonic() + 2,
        )

        self.assertEqual(client.keys, set())
        self.assertTrue(client.delete_calls)

    def test_term_wait_timeout_escalates_to_kill_and_reaps(self) -> None:
        process = _FakeProcess(
            [subprocess.TimeoutExpired(cmd="fake-worker", timeout=1)]
        )
        log = _FakeLog()
        signals: list[signal.Signals] = []

        def send_signal(pid: int, sig: signal.Signals) -> None:
            signals.append(sig)
            if sig == signal.SIGKILL:
                process.running = False

        _terminate_worker_process(
            process,
            log,
            deadline=time.monotonic() + 2,
            send_signal=send_signal,
        )

        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(log.join_calls, 1)
        self.assertFalse(process.running)

    def test_term_signal_failure_still_attempts_kill_and_reports_error(self) -> None:
        process = _FakeProcess()
        log = _FakeLog()
        signals: list[signal.Signals] = []

        def send_signal(pid: int, sig: signal.Signals) -> None:
            signals.append(sig)
            if sig == signal.SIGTERM:
                raise RuntimeError("injected TERM failure")
            process.running = False

        with self.assertRaises(CleanupFailure):
            _terminate_worker_process(
                process,
                log,
                deadline=time.monotonic() + 2,
                send_signal=send_signal,
            )

        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(log.join_calls, 1)

    def test_kill_wait_and_log_join_failures_are_bounded_and_reported(self) -> None:
        process = _FakeProcess(
            [
                subprocess.TimeoutExpired(cmd="fake-worker", timeout=1),
                RuntimeError("injected KILL wait failure"),
            ]
        )
        log = _FakeLog(join_error=RuntimeError("injected log join failure"))

        with self.assertRaises(CleanupFailure) as raised:
            _terminate_worker_process(
                process,
                log,
                deadline=time.monotonic() + 2,
                send_signal=lambda pid, sig: None,
            )

        self.assertIn("KILL wait", str(raised.exception))
        self.assertIn("log join", str(raised.exception))
        self.assertEqual(log.join_calls, 1)

    def test_nested_cleanup_preserves_all_process_and_redis_errors(self) -> None:
        process = _FakeProcess()
        log = _FakeLog()
        broker = _MemoryRedis({b"owned"})
        broker.delete_error = RuntimeError("injected Redis delete failure")
        result = _MemoryRedis()

        with patch(
            __name__ + "._terminate_worker_process",
            side_effect=RuntimeError("injected worker cleanup failure"),
        ):
            errors = _cleanup_roundtrip_resources(
                process=process,
                log=log,
                broker_client=broker,
                result_client=result,
                broker_initially_empty=True,
                result_initially_empty=True,
                broker_owned_keys={b"owned"},
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=None,
                deadline=time.monotonic() + 2,
            )

        self.assertTrue(any("worker cleanup" in error for error in errors))
        self.assertTrue(any("Redis cleanup" in error for error in errors))
        self.assertTrue(broker.closed)
        self.assertTrue(result.closed)


if __name__ == "__main__":
    unittest.main()
