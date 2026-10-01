"""Disposable child-only Celery liveness probe supervisor.

The parent integration test deliberately does not import this module: Celery
tasks, routes and connections are created only in this short-lived supervisor
and its official CLI worker child.  The supervisor publishes safe probe tasks,
waits for an explicit parent ACK before starting the worker, and reports JSON
events over stdout so the parent can independently inspect Redis keys.
"""

from __future__ import annotations

from collections import deque
import json
import os
import select
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping

from celery import Celery
from kombu import Exchange, Queue

from tests.worker_liveness_protocol import (
    PROBE_BROKER_ENV_NAME,
    PROBE_DEADLINE_ENV_NAME,
    PROBE_QUEUE_ENV_NAMES,
    PROBE_RESULT_ENV_NAME,
    PROBE_ROUTE_PRIORITIES,
    PROBE_RUN_ENV_NAME,
    PROBE_TASK_NAMES,
)


CHILD_SOCKET_TIMEOUT_SECONDS = 0.75
CHILD_TERM_TIMEOUT_SECONDS = 2.0
CHILD_KILL_TIMEOUT_SECONDS = 2.0
CHILD_PING_TIMEOUT_SECONDS = 5.0
CHILD_MAX_LOG_CHUNKS = 64
CHILD_MAX_LOG_CHUNK_BYTES = 1024
CHILD_LOG_READ_CHUNK_BYTES = 4096


def configure_probe_application(
    run_id: str,
    queue_names: Mapping[str, str],
    *,
    broker_url: str | None = None,
    result_backend: str | None = None,
) -> Celery:
    """Build a disposable CI-only app without importing the production app."""

    if not run_id or set(queue_names) != set(PROBE_TASK_NAMES):
        raise ValueError("liveness probe configuration must contain all three semantics")
    if any(not queue_names[semantic] for semantic in PROBE_TASK_NAMES):
        raise ValueError("liveness probe queue names must be non-empty")
    if broker_url is None:
        broker_url = os.environ.get(PROBE_BROKER_ENV_NAME)
    if result_backend is None:
        result_backend = os.environ.get(PROBE_RESULT_ENV_NAME)
    if not broker_url or not result_backend:
        raise ValueError("liveness probe requires explicit broker and result URLs")

    broker_key_prefix = f"platform-ci-{run_id}-"
    exchange = Exchange(f"platform-ci-{run_id}", type="direct", durable=False)
    task_routes: dict[str, dict[str, object]] = {}
    task_queues = []
    for semantic in ("high", "default", "low"):
        queue_name = queue_names[semantic]
        task_routes[PROBE_TASK_NAMES[semantic]] = {
            "queue": queue_name,
            "priority": PROBE_ROUTE_PRIORITIES[semantic],
        }
        task_queues.append(
            Queue(
                queue_name,
                exchange=exchange,
                routing_key=queue_name,
                durable=False,
            )
        )

    app = Celery(
        f"platform_ci_liveness_{run_id}",
        broker=broker_url,
        backend=result_backend,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        task_default_queue=queue_names["default"],
        task_default_priority=5,
        task_queue_max_priority=10,
        broker_url=broker_url,
        result_backend=result_backend,
        task_always_eager=False,
        task_routes=task_routes,
        task_queues=tuple(task_queues),
        broker_transport_options={
            "global_keyprefix": broker_key_prefix,
            "visibility_timeout": 900,
            "socket_connect_timeout": CHILD_SOCKET_TIMEOUT_SECONDS,
            "socket_timeout": CHILD_SOCKET_TIMEOUT_SECONDS,
        },
        result_backend_transport_options={
            "global_keyprefix": broker_key_prefix,
            "socket_connect_timeout": CHILD_SOCKET_TIMEOUT_SECONDS,
            "socket_timeout": CHILD_SOCKET_TIMEOUT_SECONDS,
        },
        task_send_sent_event=True,
        task_track_started=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
    )

    # The manifest remains the independent production ownership contract. A
    # worker launched by this test gets inert metadata registrations solely so
    # its control-plane view has the same task-name set; none is published.
    from tests.worker_registry_manifest import EXPECTED_TASKS

    for expected in EXPECTED_TASKS:
        options: dict[str, object] = {"ignore_result": expected.ignore_result}
        if expected.soft_time_limit is not None:
            options["soft_time_limit"] = expected.soft_time_limit
        if expected.time_limit is not None:
            options["time_limit"] = expected.time_limit

        def _metadata_probe() -> str:
            return "unused"

        app.task(name=expected.name, **options)(_metadata_probe)

    for semantic, task_name in PROBE_TASK_NAMES.items():
        def _probe() -> str:
            return "pong"

        app.task(name=task_name, ignore_result=False)(_probe)
    return app


class _BoundedChildLog:
    """Continuously drain worker output while retaining bounded diagnostics."""

    _READY_PATTERN = "ready."

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._chunks: deque[str] = deque(maxlen=CHILD_MAX_LOG_CHUNKS)
        self._rolling_text = ""
        self._lock = threading.Lock()
        self.ready = threading.Event()
        self.eof = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name="platform-ci-liveness-child-log-drain",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            read_chunk = getattr(self._stream, "read1", self._stream.read)
            while True:
                chunk = read_chunk(CHILD_LOG_READ_CHUNK_BYTES)
                if not chunk:
                    return
                decoded = (
                    chunk.decode("utf-8", errors="replace")
                    if isinstance(chunk, bytes)
                    else str(chunk)
                )
                with self._lock:
                    self._chunks.append(decoded[-CHILD_MAX_LOG_CHUNK_BYTES:])
                    self._rolling_text = (self._rolling_text + decoded)[-8192:]
                    ready = self._READY_PATTERN in self._rolling_text.lower()
                if ready:
                    self.ready.set()
        except BaseException:
            # Forced pipe close during teardown is expected; EOF is still the
            # required observable state and diagnostics remain bounded.
            return
        finally:
            self.eof.set()

    def diagnostics(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def join(self, deadline: float) -> None:
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))

    @property
    def thread_alive(self) -> bool:
        return self._thread.is_alive()


def _emit(event: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(dict(event), sort_keys=True) + "\n")
    sys.stdout.flush()


def _deadline() -> float:
    try:
        return float(os.environ[PROBE_DEADLINE_ENV_NAME])
    except (KeyError, ValueError) as exc:
        raise RuntimeError("liveness probe requires an absolute monotonic deadline") from exc


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("liveness probe child exceeded its absolute deadline")
    return remaining


def _wait_for_parent_ack(deadline: float) -> None:
    remaining = _remaining(deadline)
    readable, _, _ = select.select([sys.stdin], [], [], remaining)
    if not readable or sys.stdin.readline().strip() != "START_WORKER":
        raise RuntimeError("parent did not acknowledge broker observation")


def _terminate_worker(
    process: subprocess.Popen[bytes],
    log: _BoundedChildLog | None,
    *,
    deadline: float,
) -> list[str]:
    """Escalate TERM -> KILL, reap and close the child log pipe by deadline."""

    errors: list[str] = []
    try:
        running = process.poll() is None
    except BaseException as exc:
        errors.append(f"worker poll: {exc}")
        running = True
    must_kill = False
    if running:
        try:
            os.kill(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except BaseException as exc:
            errors.append(f"worker TERM: {exc}")
            must_kill = True
        try:
            process.wait(timeout=min(CHILD_TERM_TIMEOUT_SECONDS, _remaining(deadline)))
        except subprocess.TimeoutExpired:
            must_kill = True
        except BaseException as exc:
            errors.append(f"worker TERM wait: {exc}")
            must_kill = True
        else:
            must_kill = process.poll() is None

    if must_kill:
        try:
            os.kill(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as exc:
            errors.append(f"worker KILL: {exc}")
        try:
            process.wait(timeout=min(CHILD_KILL_TIMEOUT_SECONDS, _remaining(deadline)))
        except BaseException as exc:
            errors.append(f"worker KILL wait: {exc}")
            try:
                process.wait(timeout=min(CHILD_KILL_TIMEOUT_SECONDS, _remaining(deadline)))
            except BaseException as reap_exc:
                errors.append(f"worker final reap: {reap_exc}")
    else:
        try:
            process.wait(timeout=min(CHILD_KILL_TIMEOUT_SECONDS, _remaining(deadline)))
        except BaseException as exc:
            errors.append(f"worker reap: {exc}")

    try:
        if process.poll() is None:
            errors.append("worker remained live after TERM/KILL cleanup")
    except BaseException as exc:
        errors.append(f"worker final poll: {exc}")

    try:
        if log is not None:
            if log.thread_alive:
                try:
                    log._stream.close()
                except BaseException as exc:
                    errors.append(f"worker stdout close: {exc}")
            log.join(deadline)
            if log.thread_alive:
                errors.append("worker log-drain thread remained live")
            if not log.eof.is_set():
                errors.append("worker log-drain did not reach EOF")
        elif process.stdout is not None:
            process.stdout.close()
    except BaseException as exc:
        errors.append(f"worker log cleanup: {exc}")
    return errors


def _configure_from_environment() -> Celery:
    run_id = os.environ.get(PROBE_RUN_ENV_NAME)
    queue_names = {
        semantic: os.environ.get(environment_name, "")
        for semantic, environment_name in PROBE_QUEUE_ENV_NAMES.items()
    }
    return configure_probe_application(
        run_id or "",
        queue_names,
        broker_url=os.environ.get(PROBE_BROKER_ENV_NAME),
        result_backend=os.environ.get(PROBE_RESULT_ENV_NAME),
    )


def _run_supervisor() -> int:
    deadline = _deadline()
    app = celery_app
    if app is None:
        raise RuntimeError("liveness supervisor requires an explicit run environment")
    run_id = os.environ[PROBE_RUN_ENV_NAME]
    queue_names = {
        semantic: os.environ[environment_name]
        for semantic, environment_name in PROBE_QUEUE_ENV_NAMES.items()
    }
    _emit(
        {
            "event": "SUPERVISOR_READY",
            "queues": queue_names,
            "task_always_eager": app.conf.task_always_eager,
        }
    )

    published: list[tuple[str, str, object, float]] = []
    for semantic in ("high", "default", "low"):
        _remaining(deadline)
        task_name = PROBE_TASK_NAMES[semantic]
        route = app.amqp.router.route({}, task_name, (), {})
        resolved_queue = route.get("queue")
        resolved_queue_name = getattr(resolved_queue, "name", resolved_queue)
        if resolved_queue_name != queue_names[semantic]:
            raise AssertionError(
                f"{semantic} probe route resolved to {resolved_queue_name!r}, "
                f"expected {queue_names[semantic]!r}"
            )
        resolved_priority = route.get("priority")
        if resolved_priority != PROBE_ROUTE_PRIORITIES[semantic]:
            raise AssertionError(f"{semantic} probe route priority is not explicit")
        task_id = f"platform-ci-ping-{run_id}-{secrets.token_hex(12)}"
        published_at = time.monotonic()
        result = app.tasks[task_name].apply_async(
            args=(),
            kwargs={},
            task_id=task_id,
            # Celery's task_default_priority is intentionally 5 for the
            # disposable app; carry the route's resolved priority explicitly
            # so Redis priority-step evidence proves each semantic route.
            priority=resolved_priority,
            retry=False,
        )
        _emit(
            {
                "event": "PUBLISHED",
                "semantic": semantic,
                "task_id": task_id,
                "queue": resolved_queue_name,
                "priority": resolved_priority,
            }
        )
        published.append((semantic, task_id, result, published_at))
    _emit({"event": "PUBLISH_COMPLETE"})
    _wait_for_parent_ack(deadline)

    worker_command = [
        sys.executable,
        "-m",
        "celery",
        "-A",
        "tests.worker_liveness_probe:celery_app",
        "worker",
        "--pool=solo",
        "--concurrency=1",
        "--queues=" + ",".join(queue_names.values()),
        "--hostname=" + f"platform-ci-{run_id}@%h",
        "--loglevel=INFO",
        "--without-gossip",
        "--without-mingle",
        "--without-heartbeat",
    ]
    worker_environment = os.environ.copy()
    worker = subprocess.Popen(
        worker_command,
        cwd=Path(__file__).resolve().parents[1],
        env=worker_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=False,
    )
    worker_log: _BoundedChildLog | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[str] = []
    try:
        if worker.stdout is None:
            raise RuntimeError("probe worker stdout was not captured")
        worker_log = _BoundedChildLog(worker.stdout)
        while not worker_log.ready.is_set():
            _remaining(deadline)
            if worker.poll() is not None:
                raise RuntimeError(
                    "probe worker exited before ready: " + worker_log.diagnostics()
                )
            worker_log.ready.wait(timeout=min(0.25, _remaining(deadline)))

        inspect_timeout = min(2.0, _remaining(deadline))
        inspector = app.control.inspect(timeout=inspect_timeout)
        registered = inspector.registered() or {}
        active_queues = inspector.active_queues() or {}
        registered_names = sorted(
            {
                task_name
                for task_names in registered.values()
                for task_name in task_names
                if task_name.startswith("platform.")
            }
        )
        listening_queues = sorted(
            {
                queue["name"]
                for queues in active_queues.values()
                for queue in queues
            }
        )
        _emit(
            {
                "event": "WORKER_READY",
                "registered": registered_names,
                "queues": listening_queues,
            }
        )

        for semantic, task_id, result, published_at in published:
            probe_started = time.monotonic()
            value = result.get(timeout=min(CHILD_PING_TIMEOUT_SECONDS, _remaining(deadline)))
            elapsed = time.monotonic() - probe_started
            if value != "pong" or result.state != "SUCCESS":
                raise AssertionError(f"{semantic} probe returned {value!r}/{result.state!r}")
            _emit(
                {
                    "event": "RESULT",
                    "semantic": semantic,
                    "task_id": task_id,
                    "state": result.state,
                    "value": value,
                    "elapsed": elapsed,
                    "published_elapsed": time.monotonic() - published_at,
                }
            )
        _emit({"event": "RESULTS_COMPLETE"})
    except BaseException as exc:
        primary_error = exc
        try:
            _emit({"event": "ERROR", "error": repr(exc)})
        except BaseException:
            pass
    finally:
        cleanup_errors.extend(_terminate_worker(worker, worker_log, deadline=deadline))
        if cleanup_errors:
            try:
                _emit({"event": "CLEANUP_ERROR", "errors": cleanup_errors})
            except BaseException:
                pass

    if primary_error is not None:
        raise primary_error
    if cleanup_errors:
        raise RuntimeError("probe worker cleanup failed: " + "; ".join(cleanup_errors))
    _emit({"event": "COMPLETE"})
    return 0


if os.environ.get(PROBE_RUN_ENV_NAME):
    celery_app = _configure_from_environment()
else:
    # The official worker import always receives the explicit run environment.
    # Keeping this unset prevents an ordinary module import from constructing
    # a Celery app or touching any production configuration.
    celery_app = None


if __name__ == "__main__":
    if sys.argv[1:] != ["--supervise"]:
        raise SystemExit("usage: python -m tests.worker_liveness_probe --supervise")
    raise SystemExit(_run_supervisor())


__all__ = ["celery_app", "configure_probe_application"]
