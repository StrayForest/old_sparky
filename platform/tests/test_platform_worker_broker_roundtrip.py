from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from typing import TypeVar
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.worker_registry_manifest import EXPECTED_TASKS
from tests.worker_liveness_protocol import (
    PROBE_BROKER_ENV_NAME,
    PROBE_DEADLINE_ENV_NAME,
    PROBE_QUEUE_ENV_NAMES,
    PROBE_RESULT_ENV_NAME,
    PROBE_ROUTE_PRIORITIES,
    PROBE_RUN_ENV_NAME,
    PROBE_SUPERVISOR_FLAG,
    PROBE_TASK_NAMES,
)
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


MAX_CLEANUP_EVIDENCE = 64
_ALLOWED_SYSTEM_EXIT_CODES = frozenset({0, 1, 2})
_SAFE_SYSTEM_EXIT_CODE = 1
_SAFE_KEYBOARD_INTERRUPT_MESSAGE = "cleanup cancellation"


@dataclass(frozen=True, slots=True)
class _CleanupEvidence:
    """Safe, bounded cleanup evidence with no exception payload."""

    stage: str
    code: str

    def render(self) -> str:
        return f"{self.stage} [{self.code}]"

    def __str__(self) -> str:
        return self.render()

    def __contains__(self, fragment: object) -> bool:
        return isinstance(fragment, str) and fragment in self.render()


@dataclass(frozen=True, slots=True)
class _CleanupReport:
    """All bounded cleanup evidence, including safe fatal metadata."""

    evidence: tuple[_CleanupEvidence, ...]
    fatal_kind: str | None = None
    fatal_exit_code: int | None = None

    def __post_init__(self) -> None:
        # Keep the report safe even if a future caller constructs it directly:
        # only the two static cancellation kinds and conventional numeric exit
        # statuses are representable, never an exception object or its text.
        safe_kind = (
            self.fatal_kind
            if isinstance(self.fatal_kind, str)
            and self.fatal_kind in {"keyboard-interrupt", "system-exit"}
            else None
        )
        safe_exit_code = (
            self.fatal_exit_code
            if safe_kind == "system-exit"
            and type(self.fatal_exit_code) is int
            and self.fatal_exit_code in _ALLOWED_SYSTEM_EXIT_CODES
            else None
        )
        object.__setattr__(self, "fatal_kind", safe_kind)
        object.__setattr__(self, "fatal_exit_code", safe_exit_code)

    def __iter__(self):
        return iter(self.evidence)

    def __len__(self) -> int:
        return len(self.evidence)

    def __bool__(self) -> bool:
        return bool(self.evidence) or self.fatal_kind is not None


_CLEANUP_STAGE_ALLOWLIST = frozenset(
    {
        "cleanup",
        "worker cleanup",
        "worker log cleanup",
        "worker finalizer",
        "Celery broker Redis cleanup",
        "Celery broker Redis close",
        "Celery result Redis cleanup",
        "Celery result Redis close",
        "temporary-directory cleanup",
        "shielded worker TERM",
        "shielded worker TERM wait",
        "shielded worker KILL",
        "shielded worker KILL wait",
        "shielded worker final reap",
        "shielded worker final poll",
        "shielded worker stdin close",
        "shielded worker stdout close",
        "shielded worker log EOF join",
        "shielded worker log state",
    }
)

def _fatal_exception_metadata(
    exc: BaseException,
) -> tuple[str, int | None] | None:
    """Return only allowlisted cancellation metadata, never the exception."""

    if isinstance(exc, KeyboardInterrupt):
        return ("keyboard-interrupt", None)
    if isinstance(exc, SystemExit):
        code = exc.code
        if type(code) is int and code in _ALLOWED_SYSTEM_EXIT_CODES:
            return ("system-exit", code)
        return ("system-exit", None)
    return None


def _new_safe_fatal_exception(
    fatal_kind: str,
    fatal_exit_code: int | None,
) -> BaseException:
    """Construct a cancellation without copying any attacker-controlled text."""

    if fatal_kind == "keyboard-interrupt":
        return KeyboardInterrupt(_SAFE_KEYBOARD_INTERRUPT_MESSAGE)
    if fatal_kind == "system-exit":
        code = (
            fatal_exit_code
            if type(fatal_exit_code) is int
            and fatal_exit_code in _ALLOWED_SYSTEM_EXIT_CODES
            else _SAFE_SYSTEM_EXIT_CODE
        )
        return SystemExit(code)
    return CleanupFailure("cleanup cancellation")


def _cleanup_exception_code(exc: BaseException) -> str:
    """Map exception classes to a closed set of safe evidence codes."""

    if isinstance(exc, KeyboardInterrupt):
        return "keyboard-interrupt"
    if isinstance(exc, SystemExit):
        return "system-exit"
    if isinstance(exc, ForeignRedisKeyError):
        return "foreign-redis-key"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, (TimeoutError, CleanupFailure)):
        return "cleanup-failure"
    if isinstance(exc, ProcessLookupError):
        return "process-lookup"
    if isinstance(exc, RuntimeError):
        return "runtime-error"
    return "base-exception"


def _cleanup_evidence(
    stage: str,
    *,
    exc: BaseException | None = None,
    code: str | None = None,
) -> _CleanupEvidence:
    """Build evidence from allowlisted stage/code values only."""

    safe_stage = stage if stage in _CLEANUP_STAGE_ALLOWLIST else "cleanup"
    if exc is not None:
        safe_code = _cleanup_exception_code(exc)
    else:
        safe_code = code if code in {
            "keyboard-interrupt",
            "system-exit",
            "foreign-redis-key",
            "timeout",
            "cleanup-failure",
            "process-lookup",
            "runtime-error",
            "base-exception",
            "process-group-missing",
            "process-live",
            "postcondition-unproven",
            "thread-live",
            "eof-missing",
            "drain-error",
        } else "cleanup-failure"
    return _CleanupEvidence(safe_stage, safe_code)


def _dedupe_cleanup_evidence(
    evidence: Iterable[_CleanupEvidence],
) -> tuple[_CleanupEvidence, ...]:
    """Preserve first-seen order while enforcing a bounded report."""

    result: list[_CleanupEvidence] = []
    seen: set[tuple[str, str]] = set()
    for item in evidence:
        key = (item.stage, item.code)
        if key in seen:
            continue
        if len(result) >= MAX_CLEANUP_EVIDENCE:
            break
        seen.add(key)
        result.append(item)
    return tuple(result)


class _RoundtripDeadlineExceeded(TimeoutError):
    """The parent watchdog interrupted an operation at the total deadline."""


_BoundedResult = TypeVar("_BoundedResult")


class _ParentDeadlineWatchdog:
    """Interrupt the parent at one absolute deadline, including teardown.

    Socket options and ``subprocess.wait(timeout=...)`` are useful local
    bounds, but they cannot bound arbitrary Python/C calls such as a pipe
    flush, ``Redis.close`` or recursive directory removal.  The integration
    test therefore installs one parent-owned ``SIGALRM`` for the complete
    work/cleanup window.  The handler raises (rather than returning), which
    also interrupts PEP-475-retried system calls.  Callers still check the
    deadline before each operation so that the one-shot alarm never becomes a
    reason to start another blocking cleanup action after expiry.

    This is deliberately parent-only and POSIX-specific: the test launches a
    process group and already relies on ``SIGTERM``/``SIGKILL`` semantics.
    """

    # A repeating alarm keeps the escalation/close best-effort path
    # interruptible even when the first alarm fires in the middle of a wait.
    _ALARM_INTERVAL_SECONDS = 0.01

    def __init__(self, deadline: float) -> None:
        self._deadline = deadline
        self._previous_handler: object | None = None
        self._previous_timer: tuple[float, float] | None = None
        self._installed_at: float | None = None
        self._cancelled = False

    def start(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("parent deadline watchdog must run in the main thread")
        self._previous_handler = signal.getsignal(signal.SIGALRM)
        self._previous_timer = signal.getitimer(signal.ITIMER_REAL)
        self._installed_at = time.monotonic()
        self._cancelled = False

        def _handle_alarm(_signum: int, _frame: object) -> None:
            if not self._cancelled:
                raise _RoundtripDeadlineExceeded(
                    "Celery liveness roundtrip exceeded its absolute parent deadline"
                )

        signal.signal(signal.SIGALRM, _handle_alarm)
        remaining = max(0.001, self._deadline - self._installed_at)
        signal.setitimer(
            signal.ITIMER_REAL,
            remaining,
            self._ALARM_INTERVAL_SECONDS,
        )

    def stop(self) -> None:
        if self._installed_at is None:
            return
        self._cancelled = True
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        previous_handler = self._previous_handler
        if previous_handler is not None:
            signal.signal(signal.SIGALRM, previous_handler)
        previous_timer = self._previous_timer
        installed_at = self._installed_at
        if previous_timer is not None and previous_timer[0] > 0:
            elapsed = max(0.0, time.monotonic() - installed_at)
            previous_remaining = max(0.001, previous_timer[0] - elapsed)
            signal.setitimer(
                signal.ITIMER_REAL,
                previous_remaining,
                previous_timer[1],
            )
        self._installed_at = None

    def __enter__(self) -> "_ParentDeadlineWatchdog":
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.stop()


def _bounded_call(
    operation: Callable[[], _BoundedResult],
    *,
    deadline: float,
    label: str,
) -> _BoundedResult:
    """Run one possibly blocking operation without extending the deadline.

    The parent watchdog supplies the hard interruption while this preflight
    check prevents any new operation from starting after the deadline.  The
    helper intentionally does not catch ordinary operation failures; callers
    retain their existing error handling and diagnostics.
    """

    if time.monotonic() >= deadline:
        raise CleanupFailure(f"{label} exceeded its absolute deadline")
    try:
        return operation()
    except _RoundtripDeadlineExceeded as exc:
        raise CleanupFailure(f"{label} exceeded its absolute deadline") from exc


def _maybe_bounded_call(
    operation: Callable[[], _BoundedResult],
    *,
    deadline: float | None,
    label: str,
) -> _BoundedResult:
    """Apply ``_bounded_call`` when a helper was given a deadline."""

    if deadline is None:
        return operation()
    return _bounded_call(operation, deadline=deadline, label=label)


def _best_effort_call(
    operation: Callable[[], _BoundedResult],
    *,
    label: str,
) -> _BoundedResult:
    """Attempt a final teardown operation while the parent alarm is active."""

    try:
        return operation()
    except _RoundtripDeadlineExceeded as exc:
        raise CleanupFailure(f"{label} exceeded its absolute deadline") from exc


def _best_effort_bounded_call(
    operation: Callable[[], _BoundedResult],
    *,
    deadline: float,
    label: str,
) -> _BoundedResult:
    """Bound normal calls and still try immediate teardown after expiry."""

    if time.monotonic() < deadline:
        return _bounded_call(operation, deadline=deadline, label=label)
    return _best_effort_call(operation, label=label)


class _BoundedWorkerLog:
    """Drain worker output continuously while retaining bounded diagnostics."""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._chunks: deque[str] = deque(maxlen=MAX_LOG_CHUNKS)
        self._line_buffer = ""
        self._events: deque[dict[str, object]] = deque(maxlen=128)
        self._lock = threading.Lock()
        self._drain_error: BaseException | None = None
        self.eof = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name="platform-celery-log-drain",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            read_chunk = getattr(self._stream, "read1", self._stream.read)
            while True:
                chunk = read_chunk(LOG_READ_CHUNK_BYTES)
                if not chunk:
                    return
                if isinstance(chunk, bytes):
                    decoded = chunk.decode("utf-8", errors="replace")
                else:
                    decoded = str(chunk)
                with self._lock:
                    self._chunks.append(decoded[-MAX_LOG_CHUNK_BYTES:])
                    self._line_buffer += decoded
                    self._line_buffer = self._line_buffer[-8192:]
                    while "\n" in self._line_buffer:
                        line, self._line_buffer = self._line_buffer.split("\n", 1)
                        try:
                            event = json.loads(line)
                        except (TypeError, json.JSONDecodeError):
                            continue
                        if isinstance(event, dict) and isinstance(event.get("event"), str):
                            self._events.append(event)
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

    def pop_matching_event(
        self,
        event_name: str,
        *,
        semantic: str | None = None,
    ) -> dict[str, object] | None:
        with self._lock:
            for index, event in enumerate(self._events):
                if event.get("event") != event_name:
                    continue
                if semantic is not None and event.get("semantic") != semantic:
                    continue
                del self._events[index]
                return event
        return None

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


def _wait_for_supervisor_event(
    process: subprocess.Popen[bytes],
    log: _BoundedWorkerLog,
    *,
    event_name: str,
    semantic: str | None = None,
    deadline: float,
) -> dict[str, object]:
    """Wait for one child protocol event within the shared absolute budget."""

    while True:
        event = log.pop_matching_event(event_name, semantic=semantic)
        if event is not None:
            return event
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"liveness supervisor did not emit {event_name!r} before the work "
                f"deadline:\n{log.diagnostics()}"
            )
        if _bounded_call(
            process.poll,
            deadline=deadline,
            label="liveness supervisor poll",
        ) is not None:
            _bounded_call(
                lambda: log.join(deadline),
                deadline=deadline,
                label="liveness supervisor log EOF join",
            )
            raise AssertionError(
                f"liveness supervisor exited before {event_name!r}:\n"
                + log.diagnostics()
            )
        time.sleep(min(0.05, remaining))


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
        running = (
            _best_effort_bounded_call(
                process.poll,
                deadline=deadline,
                label="worker poll",
            )
            is None
        )
    except BaseException:
        errors.append("worker poll [failure]")
        # Treat an unreadable process state as potentially live and make the
        # escalation path the safe default.
        running = True
        must_kill = True
    else:
        # Even an exited supervisor may have left a worker descendant in its
        # process group.  Probe the group once and use KILL escalation to
        # close that chain; ESRCH is the normal already-clean case.
        must_kill = not running

    if running:
        try:
            _best_effort_bounded_call(
                lambda: send_signal(process.pid, signal.SIGTERM),
                deadline=deadline,
                label="worker TERM signal",
            )
        except ProcessLookupError:
            pass
        except BaseException:
            errors.append("worker TERM [failure]")
            must_kill = True
        try:
            remaining = max(0.0, deadline - time.monotonic())
            _best_effort_bounded_call(
                lambda: process.wait(timeout=min(PROCESS_TERM_TIMEOUT_SECONDS, remaining)),
                deadline=deadline,
                label="worker TERM wait",
            )
            must_kill = must_kill or _best_effort_bounded_call(
                process.poll,
                deadline=deadline,
                label="worker TERM post-wait poll",
            ) is None
        except subprocess.TimeoutExpired:
            # Expected escalation path: the hard deadline still governs the
            # KILL wait below.
            must_kill = True
        except BaseException:
            errors.append("worker TERM wait [failure]")
            must_kill = True

    if not running and must_kill:
        try:
            _best_effort_bounded_call(
                lambda: send_signal(process.pid, signal.SIGTERM),
                deadline=deadline,
                label="worker descendant TERM signal",
            )
        except ProcessLookupError:
            pass
        except BaseException:
            errors.append("worker TERM group [failure]")

    if must_kill:
        try:
            _best_effort_bounded_call(
                lambda: send_signal(process.pid, signal.SIGKILL),
                deadline=deadline,
                label="worker KILL signal",
            )
        except ProcessLookupError:
            pass
        except BaseException:
            errors.append("worker KILL [failure]")
        try:
            remaining = max(0.0, deadline - time.monotonic())
            _best_effort_bounded_call(
                lambda: process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining)),
                deadline=deadline,
                label="worker KILL wait",
            )
        except BaseException:
            errors.append("worker KILL wait [failure]")
            # A wait implementation can fail after the signal was delivered.
            # Make one final bounded reap attempt before reporting the child
            # as live; this also exercises the no-zombie contract in tests.
            try:
                remaining = max(0.0, deadline - time.monotonic())
                _best_effort_bounded_call(
                    lambda: process.wait(
                        timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining)
                    ),
                    deadline=deadline,
                    label="worker final reap",
                )
            except BaseException:
                errors.append("worker final reap [failure]")
    else:
        # ``poll`` may reap an exited child, but this explicit bounded wait
        # keeps the contract true for every path and for test doubles.
        try:
            remaining = max(0.0, deadline - time.monotonic())
            _best_effort_bounded_call(
                lambda: process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining)),
                deadline=deadline,
                label="worker reap",
            )
        except BaseException:
            errors.append("worker reap [failure]")

    try:
        if _best_effort_bounded_call(
            process.poll,
            deadline=deadline,
            label="worker final poll",
        ) is None:
            errors.append("worker remained live after TERM/KILL cleanup")
    except BaseException:
        errors.append("worker final poll [failure]")

    try:
        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            try:
                _best_effort_bounded_call(
                    stdin.close,
                    deadline=deadline,
                    label="worker stdin close",
                )
            except BaseException:
                errors.append("worker stdin close [failure]")
        if log is not None:
            stream = getattr(log, "_stream", None)
            if stream is None:
                stream = getattr(process, "stdout", None)
            if stream is not None:
                try:
                    _best_effort_bounded_call(
                        stream.close,
                        deadline=deadline,
                        label="worker stdout close",
                    )
                except BaseException:
                    errors.append("worker stdout close [failure]")
            try:
                _best_effort_bounded_call(
                    lambda: log.join(deadline),
                    deadline=deadline,
                    label="worker log EOF join",
                )
            except BaseException:
                errors.append("worker log join [failure]")
            if log.thread_alive:
                errors.append("worker log-drain thread remained live after EOF deadline")
            eof = getattr(log, "eof", None)
            if eof is not None and not eof.is_set():
                errors.append("worker log-drain did not reach EOF before deadline")
            drain_error = getattr(log, "drain_error", None)
            if drain_error is not None:
                errors.append("worker log drain [failure]")
        elif process.stdout is not None:
            _best_effort_bounded_call(
                process.stdout.close,
                deadline=deadline,
                label="worker stdout close",
            )
    except BaseException:
        errors.append("worker log join [failure]")

    if errors:
        raise CleanupFailure("; ".join(errors))


def _snapshot_redis_keys(
    client: object,
    *,
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    """Take a bounded key snapshot from a dedicated Redis DB."""

    if deadline is not None and time.monotonic() >= deadline:
        raise CleanupFailure("Redis key snapshot exceeded its absolute deadline")
    keys: list[bytes] = []
    iterator = iter(
        _maybe_bounded_call(
            lambda: client.scan_iter(match="*", count=100),
            deadline=deadline,
            label="Redis key scan start",
        )
    )
    for index in range(MAX_REDIS_SNAPSHOT_KEYS + 1):
        try:
            key = _maybe_bounded_call(
                lambda: next(iterator),
                deadline=deadline,
                label="Redis key scan read",
            )
        except StopIteration:
            break
        if index >= MAX_REDIS_SNAPSHOT_KEYS:
            break
        keys.append(key)
    return tuple(sorted(keys))


def _require_empty_redis_namespace(
    client: object,
    *,
    label: str,
    deadline: float | None = None,
) -> None:
    """Fail closed on any pre-existing key without mutating the DB."""

    if deadline is not None and time.monotonic() >= deadline:
        raise CleanupFailure(f"{label} Redis preflight exceeded its absolute deadline")
    key_count = _maybe_bounded_call(
        client.dbsize,
        deadline=deadline,
        label=f"{label} Redis DB size",
    )
    keys = _snapshot_redis_keys(client, deadline=deadline)
    if key_count or keys:
        raise ForeignRedisKeyError(
            f"{label} Redis DB is not empty before the liveness run: {keys!r}"
        )


def _record_owned_redis_keys(
    owned_keys: set[bytes],
    client: object,
    *,
    allow_prefixes: Iterable[bytes] = (),
    allow_fragments: Iterable[bytes] = (),
    deadline: float | None = None,
) -> None:
    """Record keys, optionally restricted to the current run allowlist."""

    keys = _snapshot_redis_keys(client, deadline=deadline)
    prefixes = tuple(allow_prefixes)
    fragments = tuple(allow_fragments)
    if not prefixes and not fragments:
        owned_keys.update(keys)
        return
    owned_keys.update(
        key
        for key in keys
        if any(key.startswith(prefix) for prefix in prefixes)
        or any(fragment in key for fragment in fragments)
    )


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
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    """Observe the exact Redis list key selected by Kombu priority routing."""

    snapshot = set(_snapshot_redis_keys(client, deadline=deadline))
    candidates = _all_physical_queue_keys(queue_name, key_prefix)
    observed = tuple(sorted(snapshot.intersection(candidates)))
    expected_key = key_prefix + _physical_queue_key(queue_name, priority)
    if expected_key not in observed:
        raise AssertionError(
            f"priority {priority} publish did not create Kombu key {expected_key!r}; "
            f"observed={observed!r}"
        )
    if _maybe_bounded_call(
        lambda: client.type(expected_key),
        deadline=deadline,
        label="Celery broker priority key type",
    ) != b"list":
        raise AssertionError(f"Kombu priority key is not a Redis list: {expected_key!r}")
    for observed_key in observed:
        if _maybe_bounded_call(
            lambda: client.type(observed_key),
            deadline=deadline,
            label="Celery broker priority key type",
        ) != b"list":
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
            _bounded_call(
                lambda: client.delete(*batch),
                deadline=deadline,
                label=f"{label} Redis delete",
            )
    if time.monotonic() >= deadline:
        raise CleanupFailure(f"{label} Redis cleanup exceeded its absolute deadline")
    if _bounded_call(
        client.dbsize,
        deadline=deadline,
        label=f"{label} Redis post-cleanup DB size",
    ) != 0 or _snapshot_redis_keys(client, deadline=deadline):
        raise CleanupFailure(f"{label} Redis DB retained keys after owned-key cleanup")


def _shielded_worker_finalizer(
    process: subprocess.Popen[bytes] | None,
    log: _BoundedWorkerLog | None,
    *,
    deadline: float,
    send_signal: Callable[[int, signal.Signals], None] | None = None,
) -> _CleanupReport:
    """Best-effort parent-owned finalization after cancellation/error.

    The ordinary terminator is deliberately injectable in contract tests and
    may be interrupted before it reaches escalation.  This finalizer has no
    dependency on that helper: it always attempts process-group TERM, KILL,
    wait/reap and log-pipe EOF in that order.  Every operation is independently
    guarded by the same absolute deadline, and failures are collected so a
    later Redis or temporary-directory postcondition still runs.
    """

    send_signal = send_signal or os.killpg
    errors: list[_CleanupEvidence] = []
    fatal_metadata: tuple[str, int | None] | None = None

    def attempt(
        label: str,
        operation: Callable[[], object],
        *,
        ignore_process_lookup: bool = False,
    ) -> object | None:
        nonlocal fatal_metadata
        try:
            return _best_effort_bounded_call(
                operation,
                deadline=deadline,
                label=label,
            )
        except ProcessLookupError:
            if not ignore_process_lookup:
                errors.append(_cleanup_evidence(label, code="process-group-missing"))
        except BaseException as exc:
            errors.append(_cleanup_evidence(label, exc=exc))
            if fatal_metadata is None:
                fatal_metadata = _fatal_exception_metadata(exc)
        return None

    if process is not None:
        attempt(
            "shielded worker TERM",
            lambda: send_signal(process.pid, signal.SIGTERM),
            ignore_process_lookup=True,
        )
        remaining = max(0.0, deadline - time.monotonic())
        attempt(
            "shielded worker TERM wait",
            lambda: process.wait(timeout=min(PROCESS_TERM_TIMEOUT_SECONDS, remaining)),
        )
        attempt(
            "shielded worker KILL",
            lambda: send_signal(process.pid, signal.SIGKILL),
            ignore_process_lookup=True,
        )
        remaining = max(0.0, deadline - time.monotonic())
        attempt(
            "shielded worker KILL wait",
            lambda: process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining)),
        )
        remaining = max(0.0, deadline - time.monotonic())
        attempt(
            "shielded worker final reap",
            lambda: process.wait(timeout=min(PROCESS_KILL_TIMEOUT_SECONDS, remaining)),
        )
        final_status = attempt("shielded worker final poll", process.poll)
        if final_status is None:
            errors.append(_cleanup_evidence("shielded worker final poll", code="process-live"))

        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            attempt("shielded worker stdin close", stdin.close)

    if log is not None:
        stream = getattr(log, "_stream", None)
        if stream is None and process is not None:
            stream = getattr(process, "stdout", None)
        if stream is not None:
            attempt("shielded worker stdout close", stream.close)
        attempt("shielded worker log EOF join", lambda: log.join(deadline))
        try:
            if log.thread_alive:
                errors.append(_cleanup_evidence("shielded worker log state", code="thread-live"))
            eof = getattr(log, "eof", None)
            if eof is not None and not eof.is_set():
                errors.append(_cleanup_evidence("shielded worker log state", code="eof-missing"))
            drain_error = getattr(log, "drain_error", None)
            if drain_error is not None:
                errors.append(_cleanup_evidence("shielded worker log state", code="drain-error"))
        except BaseException as exc:
            errors.append(_cleanup_evidence("shielded worker log state", exc=exc))
            if fatal_metadata is None:
                fatal_metadata = _fatal_exception_metadata(exc)
    elif process is not None and process.stdout is not None:
        attempt("shielded worker stdout close", process.stdout.close)

    fatal_kind, fatal_exit_code = fatal_metadata or (None, None)
    return _CleanupReport(
        evidence=_dedupe_cleanup_evidence(errors),
        fatal_kind=fatal_kind,
        fatal_exit_code=fatal_exit_code,
    )


def _cleanup_temporary_directory(run_root: Path, *, deadline: float) -> None:
    """Check, remove and recheck one exact temporary run directory."""

    if not _bounded_call(
        run_root.exists,
        deadline=deadline,
        label="temporary-directory existence check",
    ):
        return
    leftovers = _bounded_call(
        lambda: tuple(run_root.iterdir()),
        deadline=deadline,
        label="temporary-directory listing",
    )
    leftover_error: str | None = None
    if leftovers:
        leftover_error = (
            "ephemeral worker left schedule/state files: "
            + ", ".join(str(path) for path in leftovers)
        )
    _bounded_call(
        lambda: shutil.rmtree(run_root),
        deadline=deadline,
        label="temporary-directory removal",
    )
    if _bounded_call(
        run_root.exists,
        deadline=deadline,
        label="temporary-directory post-cleanup existence check",
    ):
        raise CleanupFailure("temporary directory remained after cleanup")
    if leftover_error is not None:
        raise CleanupFailure(leftover_error)


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
    result_allow_prefixes: Iterable[bytes] = (),
    run_root: Path | None,
    deadline: float,
    send_signal: Callable[[int, signal.Signals], None] | None = None,
) -> _CleanupReport:
    """Run shielded process, Redis and temporary-state cleanup phases."""

    errors: list[_CleanupEvidence] = []
    fatal_metadata: tuple[str, int | None] | None = None

    def note_error(label: str, exc: BaseException) -> None:
        nonlocal fatal_metadata
        errors.append(_cleanup_evidence(label, exc=exc))
        if fatal_metadata is None:
            fatal_metadata = _fatal_exception_metadata(exc)

    process_cleanup_failed = False
    if process is not None:
        try:
            _best_effort_bounded_call(
                lambda: _terminate_worker_process(
                    process,
                    log,
                    deadline=deadline,
                    send_signal=send_signal,
                ),
                deadline=deadline,
                label="worker TERM/KILL/reap cleanup",
            )
        except BaseException as exc:
            note_error("worker cleanup", exc)
            process_cleanup_failed = True
    elif log is not None:
        try:
            stream = getattr(log, "_stream", None)
            if stream is not None:
                _best_effort_bounded_call(
                    stream.close,
                    deadline=deadline,
                    label="worker stdout close",
                )
            _best_effort_bounded_call(
                lambda: log.join(deadline),
                deadline=deadline,
                label="worker log EOF join",
            )
            if log.thread_alive:
                raise CleanupFailure("worker log-drain thread remained live")
            eof = getattr(log, "eof", None)
            if eof is not None and not eof.is_set():
                raise CleanupFailure("worker log-drain did not reach EOF")
        except BaseException as exc:
            note_error("worker log cleanup", exc)
            process_cleanup_failed = True

    if process_cleanup_failed:
        try:
            finalizer_report = _shielded_worker_finalizer(
                process,
                log,
                deadline=deadline,
                send_signal=send_signal,
            )
            errors.extend(finalizer_report.evidence)
            if fatal_metadata is None and finalizer_report.fatal_kind is not None:
                fatal_metadata = (
                    finalizer_report.fatal_kind,
                    finalizer_report.fatal_exit_code,
                )
        except BaseException as exc:
            note_error("worker finalizer", exc)

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
            result_allow_prefixes,
            result_allow_fragments,
        ),
    ):
        if client is None:
            continue
        delete_succeeded = False
        for _attempt in range(2):
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
                delete_succeeded = True
                break
            except BaseException as exc:
                note_error(f"{label} Redis cleanup", exc)
        for _attempt in range(2):
            try:
                _best_effort_bounded_call(
                    client.close,
                    deadline=deadline,
                    label=f"{label} Redis close",
                )
                break
            except BaseException as exc:
                note_error(f"{label} Redis close", exc)
        if not delete_succeeded:
            errors.append(
                _cleanup_evidence(
                    f"{label} Redis cleanup",
                    code="postcondition-unproven",
                )
            )

    if run_root is not None:
        for _attempt in range(2):
            try:
                _cleanup_temporary_directory(run_root, deadline=deadline)
                break
            except BaseException as exc:
                note_error("temporary-directory cleanup", exc)

    fatal_kind, fatal_exit_code = fatal_metadata or (None, None)
    return _CleanupReport(
        evidence=_dedupe_cleanup_evidence(errors),
        fatal_kind=fatal_kind,
        fatal_exit_code=fatal_exit_code,
    )


def _render_cleanup_evidence(evidence: Iterable[_CleanupEvidence]) -> str:
    """Render only bounded stage/code pairs for an exception note."""

    return "roundtrip cleanup evidence: " + "; ".join(
        item.render() for item in evidence
    )


def _finish_roundtrip_cleanup(
    cleanup: Callable[[], _CleanupReport],
    *,
    primary_exception: BaseException | None,
) -> None:
    """Complete cleanup without allowing it to replace a work exception."""

    # A caller may invoke cleanup from inside an ``except`` body without
    # passing the handled exception explicitly.  Preserve that active primary
    # and let an explicit primary take precedence when both are present.
    active_exception = sys.exception()
    effective_primary = (
        primary_exception if primary_exception is not None else active_exception
    )
    cleanup_report = _CleanupReport(())
    cleanup_exception: BaseException | None = None
    try:
        cleanup_report = cleanup()
    except BaseException as exc:
        cleanup_exception = exc

    evidence = list(cleanup_report.evidence)
    if cleanup_exception is not None:
        evidence.append(_cleanup_evidence("cleanup", exc=cleanup_exception))
    evidence = list(_dedupe_cleanup_evidence(evidence))

    if cleanup_report.fatal_kind is not None and not any(
        item.code == cleanup_report.fatal_kind for item in evidence
    ):
        evidence.append(
            _cleanup_evidence("cleanup", code=cleanup_report.fatal_kind)
        )
        evidence = list(_dedupe_cleanup_evidence(evidence))

    fatal_kind = cleanup_report.fatal_kind
    fatal_exit_code = cleanup_report.fatal_exit_code
    if fatal_kind is None and cleanup_exception is not None:
        fatal_metadata = _fatal_exception_metadata(cleanup_exception)
        if fatal_metadata is not None:
            fatal_kind, fatal_exit_code = fatal_metadata

    if evidence:
        evidence_note = _render_cleanup_evidence(evidence)
        if effective_primary is not None:
            effective_primary.add_note(evidence_note)
    else:
        evidence_note = "roundtrip cleanup evidence: cleanup [cleanup-failure]"

    if effective_primary is not None:
        return
    if fatal_kind is not None:
        safe_exception = _new_safe_fatal_exception(fatal_kind, fatal_exit_code)
        safe_exception.add_note(evidence_note)
        raise safe_exception from None
    if cleanup_exception is not None:
        safe_exception = CleanupFailure("roundtrip cleanup failed")
        safe_exception.add_note(evidence_note)
        raise safe_exception from None
    if evidence:
        safe_exception = CleanupFailure("roundtrip cleanup failed")
        safe_exception.add_note(evidence_note)
        raise safe_exception from None


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


def _task_result_keys(
    client: object,
    task_id: str,
    *,
    deadline: float | None = None,
) -> tuple[bytes, ...]:
    keys: list[bytes] = []
    if deadline is not None and time.monotonic() >= deadline:
        raise CleanupFailure("Celery result key lookup exceeded its absolute deadline")
    iterator = iter(
        _maybe_bounded_call(
            lambda: client.scan_iter(match=f"*{task_id}*", count=100),
            deadline=deadline,
            label="Celery result key scan start",
        )
    )
    for index in range(MAX_REDIS_SNAPSHOT_KEYS + 1):
        try:
            key = _maybe_bounded_call(
                lambda: next(iterator),
                deadline=deadline,
                label="Celery result key scan read",
            )
        except StopIteration:
            break
        if index >= MAX_REDIS_SNAPSHOT_KEYS:
            break
        keys.append(key)
    return tuple(keys)


class PlatformWorkerBrokerRoundtripIntegrationTests(unittest.TestCase):
    """Exercise broker -> worker -> task -> result on isolated CI Redis."""

    def test_ephemeral_worker_processes_safe_ping_on_every_required_queue(self) -> None:
        started_at = time.monotonic()
        total_deadline = started_at + ROUNDTRIP_TOTAL_TIMEOUT_SECONDS
        work_deadline = started_at + WORK_PHASE_TIMEOUT_SECONDS

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
        result_allow_prefixes: tuple[bytes, ...] = ()
        task_ids: list[str] = []
        primary_exception: BaseException | None = None
        watchdog = _ParentDeadlineWatchdog(total_deadline)
        watchdog.start()

        try:
            configuration = validate_test_celery_resource_configuration()
            _assert_safe_redis_mutation_targets(configuration)

            from redis import Redis

            broker_client = _bounded_call(
                lambda: Redis.from_url(
                    configuration.broker_url,
                    decode_responses=False,
                    socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                ),
                deadline=total_deadline,
                label="Celery broker Redis client creation",
            )
            result_client = _bounded_call(
                lambda: Redis.from_url(
                    configuration.result_backend_url,
                    decode_responses=False,
                    socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
                ),
                deadline=total_deadline,
                label="Celery result Redis client creation",
            )
            assert broker_client is not None
            assert result_client is not None
            _bounded_call(
                broker_client.ping,
                deadline=work_deadline,
                label="Celery broker Redis ping",
            )
            _bounded_call(
                result_client.ping,
                deadline=work_deadline,
                label="Celery result Redis ping",
            )
            _require_empty_redis_namespace(
                broker_client,
                label="Celery broker",
                deadline=work_deadline,
            )
            broker_initially_empty = True
            _require_empty_redis_namespace(
                result_client,
                label="Celery result",
                deadline=work_deadline,
            )
            result_initially_empty = True

            run_id = secrets.token_hex(16)
            queue_specs = tuple(
                (semantic, f"platform-ci-{run_id}-{semantic}")
                for semantic in QUEUE_SEMANTICS
            )
            queue_names = tuple(queue_name for _, queue_name in queue_specs)
            broker_key_prefix = f"platform-ci-{run_id}-".encode("ascii")
            broker_allow_prefixes = (broker_key_prefix,)
            result_allow_prefixes = (broker_key_prefix,)
            self.assertEqual(
                tuple(semantic for semantic, _ in queue_specs),
                QUEUE_SEMANTICS,
            )
            self.assertEqual(len(set(queue_names)), len(QUEUE_SEMANTICS))
            self.assertTrue(all(run_id in queue_name for queue_name in queue_names))

            run_root = _bounded_call(
                lambda: Path(tempfile.mkdtemp(prefix="platform-celery-roundtrip-")),
                deadline=work_deadline,
                label="temporary-directory creation",
            )
            worker_environment = os.environ.copy()
            worker_environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (str(PLATFORM_ROOT), worker_environment.get("PYTHONPATH", ""))
                if value
            )
            worker_environment["PLATFORM_RUNTIME_SERVICE"] = "worker"
            worker_environment["CELERYD_LOG_COLOR"] = "false"
            worker_environment[PROBE_BROKER_ENV_NAME] = configuration.broker_url
            worker_environment[PROBE_RESULT_ENV_NAME] = configuration.result_backend_url
            worker_environment["PLATFORM_CELERY_BROKER_URL"] = configuration.broker_url
            worker_environment["PLATFORM_CELERY_RESULT_BACKEND"] = (
                configuration.result_backend_url
            )
            worker_environment[PROBE_DEADLINE_ENV_NAME] = str(work_deadline)
            worker_environment[PROBE_RUN_ENV_NAME] = run_id
            for semantic, queue_name in queue_specs:
                worker_environment[PROBE_QUEUE_ENV_NAMES[semantic]] = queue_name

            worker_environment["TMPDIR"] = str(run_root)
            worker_environment["HOME"] = str(run_root)
            command = [sys.executable, "-m", "tests.worker_liveness_probe", PROBE_SUPERVISOR_FLAG]
            self.assertNotIn("--beat", command)
            self.assertFalse(any(argument.startswith("--schedule") for argument in command))
            process = _bounded_call(
                lambda: subprocess.Popen(
                    command,
                    cwd=PLATFORM_ROOT,
                    env=worker_environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                ),
                deadline=work_deadline,
                label="liveness supervisor process creation",
            )
            if process.stdout is None or process.stdin is None:
                raise AssertionError("liveness supervisor pipes were not captured")
            log = _bounded_call(
                lambda: _BoundedWorkerLog(process.stdout),
                deadline=work_deadline,
                label="liveness supervisor log drain creation",
            )

            supervisor_ready = _wait_for_supervisor_event(
                process,
                log,
                event_name="SUPERVISOR_READY",
                deadline=min(work_deadline, started_at + WORKER_READY_TIMEOUT_SECONDS),
            )
            self.assertIs(supervisor_ready.get("task_always_eager"), False)
            self.assertEqual(supervisor_ready.get("queues"), dict(queue_specs))

            observed_task_ids: dict[str, str] = {}
            for semantic, queue_name in queue_specs:
                published = _wait_for_supervisor_event(
                    process,
                    log,
                    event_name="PUBLISHED",
                    semantic=semantic,
                    deadline=work_deadline,
                )
                self.assertEqual(published.get("queue"), queue_name)
                self.assertEqual(published.get("priority"), PROBE_ROUTE_PRIORITIES[semantic])
                task_id = published.get("task_id")
                self.assertIsInstance(task_id, str)
                assert isinstance(task_id, str)
                observed_task_ids[semantic] = task_id
                task_ids.append(task_id)
                observed_priority_keys = _observe_broker_priority_keys(
                    broker_client,
                    queue_name=queue_name,
                    priority=PROBE_ROUTE_PRIORITIES[semantic],
                    key_prefix=broker_key_prefix,
                    deadline=work_deadline,
                )
                self.assertTrue(observed_priority_keys)
                _record_owned_redis_keys(
                    broker_owned_keys,
                    broker_client,
                    allow_prefixes=broker_allow_prefixes,
                    deadline=work_deadline,
                )
                self.assertEqual(
                    _task_result_keys(result_client, task_id, deadline=work_deadline),
                    (),
                )

            _wait_for_supervisor_event(
                process,
                log,
                event_name="PUBLISH_COMPLETE",
                deadline=work_deadline,
            )
            _bounded_call(
                lambda: process.stdin.write(b"START_WORKER\n"),
                deadline=work_deadline,
                label="liveness supervisor ACK write",
            )
            _bounded_call(
                process.stdin.flush,
                deadline=work_deadline,
                label="liveness supervisor ACK flush",
            )

            worker_ready = _wait_for_supervisor_event(
                process,
                log,
                event_name="WORKER_READY",
                deadline=min(work_deadline, started_at + WORKER_READY_TIMEOUT_SECONDS),
            )
            registered_platform = set(worker_ready.get("registered", ()))
            expected_platform_tasks = {task.name for task in EXPECTED_TASKS}
            self.assertEqual(
                registered_platform - set(PROBE_TASK_NAMES.values()),
                expected_platform_tasks,
            )
            self.assertEqual(
                registered_platform & set(PROBE_TASK_NAMES.values()),
                set(PROBE_TASK_NAMES.values()),
            )
            self.assertEqual(set(worker_ready.get("queues", ())), set(queue_names))
            _record_owned_redis_keys(
                broker_owned_keys,
                broker_client,
                allow_prefixes=broker_allow_prefixes,
                deadline=work_deadline,
            )

            for semantic, queue_name in queue_specs:
                result_event = _wait_for_supervisor_event(
                    process,
                    log,
                    event_name="RESULT",
                    semantic=semantic,
                    deadline=work_deadline,
                )
                task_id = observed_task_ids[semantic]
                self.assertEqual(result_event.get("task_id"), task_id)
                self.assertEqual(result_event.get("state"), "SUCCESS")
                self.assertEqual(result_event.get("value"), "pong")
                elapsed = result_event.get("elapsed")
                self.assertIsInstance(elapsed, (int, float))
                assert isinstance(elapsed, (int, float))
                self.assertLessEqual(elapsed, PING_TIMEOUT_SECONDS)
                result_keys = _task_result_keys(
                    result_client,
                    task_id,
                    deadline=work_deadline,
                )
                self.assertTrue(result_keys, f"{semantic} result key was not observable")
                raw_result = _bounded_call(
                    lambda: result_client.get(result_keys[0]),
                    deadline=work_deadline,
                    label="Celery result Redis get",
                )
                self.assertIsNotNone(raw_result)
                decoded_result = json.loads(raw_result.decode("utf-8"))
                self.assertEqual(decoded_result["status"], "SUCCESS")
                self.assertEqual(decoded_result["result"], "pong")
                _record_owned_redis_keys(
                    result_owned_keys,
                    result_client,
                    allow_prefixes=result_allow_prefixes,
                    allow_fragments=(task_id.encode("ascii"),),
                    deadline=work_deadline,
                )

            _wait_for_supervisor_event(
                process,
                log,
                event_name="RESULTS_COMPLETE",
                deadline=work_deadline,
            )
            _wait_for_supervisor_event(
                process,
                log,
                event_name="COMPLETE",
                deadline=total_deadline,
            )
            _bounded_call(
                lambda: process.wait(
                    timeout=max(0.0, total_deadline - time.monotonic())
                ),
                deadline=total_deadline,
                label="liveness supervisor final wait",
            )
            self.assertEqual(process.returncode, 0)
            _record_owned_redis_keys(
                broker_owned_keys,
                broker_client,
                allow_prefixes=broker_allow_prefixes,
                deadline=work_deadline,
            )
            _record_owned_redis_keys(
                result_owned_keys,
                result_client,
                allow_prefixes=result_allow_prefixes,
                allow_fragments=tuple(task_id.encode("ascii") for task_id in task_ids),
                deadline=work_deadline,
            )
            self.assertLessEqual(time.monotonic(), work_deadline)
        except BaseException as exc:
            primary_exception = exc
            raise
        finally:
            try:
                _finish_roundtrip_cleanup(
                    lambda: _cleanup_roundtrip_resources(
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
                        result_allow_prefixes=result_allow_prefixes,
                        run_root=run_root,
                        deadline=total_deadline,
                    ),
                    primary_exception=primary_exception,
                )
            finally:
                watchdog.stop()

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


class _ScanFailureRedis(_MemoryRedis):
    def scan_iter(self, *, match: str, count: int) -> Iterable[bytes]:
        raise TimeoutError("injected hung Redis scan")


class _BlockingDbsizeRedis(_MemoryRedis):
    def dbsize(self) -> int:
        time.sleep(5)
        return super().dbsize()


class _BlockingCloseRedis(_MemoryRedis):
    def close(self) -> None:
        time.sleep(5)
        super().close()


class _FloodStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    def read(self, size: int) -> bytes:
        if not self.payload:
            return b""
        chunk, self.payload = self.payload[:size], self.payload[size:]
        return chunk

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


class _BlockingWaitProcess(_FakeProcess):
    def wait(self, *, timeout: float) -> int:
        self.wait_calls.append(timeout)
        time.sleep(5)
        return super().wait(timeout=timeout)


class _BlockingLog:
    thread_alive = True

    def __init__(self) -> None:
        self.eof = threading.Event()
        self.join_calls = 0

    def join(self, deadline: float) -> None:
        self.join_calls += 1
        time.sleep(5)


class _BlockingPipe:
    def flush(self) -> None:
        time.sleep(5)


class PlatformWorkerCleanupContractTests(unittest.TestCase):
    def test_parent_watchdog_restores_alarm_handler_and_timer(self) -> None:
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)

        def sentinel_handler(_signum: int, _frame: object) -> None:
            return None

        signal.signal(signal.SIGALRM, sentinel_handler)
        signal.setitimer(signal.ITIMER_REAL, 60.0, 0.0)
        watchdog = _ParentDeadlineWatchdog(time.monotonic() + 1.0)
        try:
            watchdog.start()
            self.assertIsNot(signal.getsignal(signal.SIGALRM), sentinel_handler)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0.0)
        finally:
            watchdog.stop()

        try:
            self.assertIs(signal.getsignal(signal.SIGALRM), sentinel_handler)
            restored_delay, restored_interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(restored_delay, 0.0)
            self.assertEqual(restored_interval, 0.0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)
            signal.signal(signal.SIGALRM, previous_handler)

    def test_parent_watchdog_bounds_blocking_redis_dbsize_and_still_closes(self) -> None:
        broker = _BlockingDbsizeRedis()
        result = _MemoryRedis()
        started_at = time.monotonic()
        deadline = started_at + 0.05
        watchdog = _ParentDeadlineWatchdog(deadline)
        watchdog.start()
        try:
            errors = _cleanup_roundtrip_resources(
                process=None,
                log=None,
                broker_client=broker,
                result_client=result,
                broker_initially_empty=True,
                result_initially_empty=True,
                broker_owned_keys=set(),
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=None,
                deadline=deadline,
            )
        finally:
            watchdog.stop()

        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertTrue(any("Celery broker Redis cleanup" in error for error in errors))
        self.assertTrue(broker.closed)
        self.assertTrue(result.closed)

    def test_parent_watchdog_bounds_blocking_redis_close(self) -> None:
        broker = _BlockingCloseRedis()
        started_at = time.monotonic()
        deadline = started_at + 0.05
        watchdog = _ParentDeadlineWatchdog(deadline)
        watchdog.start()
        try:
            errors = _cleanup_roundtrip_resources(
                process=None,
                log=None,
                broker_client=broker,
                result_client=None,
                broker_initially_empty=True,
                result_initially_empty=False,
                broker_owned_keys=set(),
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=None,
                deadline=deadline,
            )
        finally:
            watchdog.stop()

        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertTrue(any("Celery broker Redis close" in error for error in errors))

    def test_parent_watchdog_bounds_blocking_pipe_flush(self) -> None:
        pipe = _BlockingPipe()
        started_at = time.monotonic()
        deadline = started_at + 0.05
        watchdog = _ParentDeadlineWatchdog(deadline)
        watchdog.start()
        try:
            with self.assertRaises(CleanupFailure):
                _bounded_call(pipe.flush, deadline=deadline, label="probe pipe flush")
        finally:
            watchdog.stop()

        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_parent_watchdog_bounds_term_kill_wait_reap_and_log_eof(self) -> None:
        process = _BlockingWaitProcess()
        log = _BlockingLog()
        signals: list[signal.Signals] = []
        started_at = time.monotonic()
        deadline = started_at + 0.05

        def send_signal(pid: int, sig: signal.Signals) -> None:
            signals.append(sig)
            if sig == signal.SIGKILL:
                process.running = False

        watchdog = _ParentDeadlineWatchdog(deadline)
        watchdog.start()
        try:
            with self.assertRaises(CleanupFailure) as raised:
                _terminate_worker_process(
                    process,
                    log,
                    deadline=deadline,
                    send_signal=send_signal,
                )
        finally:
            watchdog.stop()

        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertIn("KILL wait", str(raised.exception))
        self.assertIn("log-drain thread remained live", str(raised.exception))
        self.assertIn("log-drain did not reach EOF", str(raised.exception))
        self.assertGreaterEqual(log.join_calls, 1)

    def test_parent_watchdog_bounds_blocking_temporary_directory_removal(self) -> None:
        run_root = Path(tempfile.mkdtemp(prefix="platform-celery-watchdog-"))
        started_at = time.monotonic()
        deadline = started_at + 0.05
        watchdog = _ParentDeadlineWatchdog(deadline)
        try:
            with patch(
                __name__ + ".shutil.rmtree",
                side_effect=lambda path: time.sleep(5),
            ):
                watchdog.start()
                try:
                    errors = _cleanup_roundtrip_resources(
                        process=None,
                        log=None,
                        broker_client=None,
                        result_client=None,
                        broker_initially_empty=False,
                        result_initially_empty=False,
                        broker_owned_keys=set(),
                        result_owned_keys=set(),
                        broker_allow_prefixes=(),
                        result_allow_fragments=(),
                        run_root=run_root,
                        deadline=deadline,
                    )
                finally:
                    watchdog.stop()
        finally:
            if run_root.exists():
                shutil.rmtree(run_root)

        self.assertLess(time.monotonic() - started_at, 0.5)
        self.assertTrue(any("temporary-directory cleanup" in error for error in errors))

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

    def test_leader_exit_still_escalates_process_group_for_descendants(self) -> None:
        process = _FakeProcess()
        process.running = False
        signals: list[signal.Signals] = []

        _terminate_worker_process(
            process,
            _FakeLog(),
            deadline=time.monotonic() + 2,
            send_signal=lambda pid, sig: signals.append(sig),
        )

        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])

    def test_cleanup_catches_cancellation_and_still_closes_redis(self) -> None:
        broker = _MemoryRedis()
        result = _MemoryRedis()

        class InjectedCancellation(BaseException):
            pass

        with patch(
            __name__ + "._terminate_worker_process",
            side_effect=InjectedCancellation("cancelled"),
        ):
            errors = _cleanup_roundtrip_resources(
                process=_FakeProcess(),
                log=_FakeLog(),
                broker_client=broker,
                result_client=result,
                broker_initially_empty=True,
                result_initially_empty=True,
                broker_owned_keys=set(),
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=None,
                deadline=time.monotonic() + 2,
            )

        self.assertTrue(any("worker cleanup" in error for error in errors))
        self.assertTrue(broker.closed)
        self.assertTrue(result.closed)

    def test_shielded_finalizer_reaps_after_process_cleanup_cancellation(self) -> None:
        process = _FakeProcess()
        broker = _MemoryRedis({b"owned"})
        run_root = Path(tempfile.mkdtemp(prefix="platform-celery-shielded-"))

        class InjectedCancellation(BaseException):
            pass

        with patch(
            __name__ + "._terminate_worker_process",
            side_effect=InjectedCancellation("cancelled"),
        ):
            errors = _cleanup_roundtrip_resources(
                process=process,
                log=_FakeLog(),
                broker_client=broker,
                result_client=None,
                broker_initially_empty=True,
                result_initially_empty=False,
                broker_owned_keys={b"owned"},
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=run_root,
                deadline=time.monotonic() + 2,
            )

        self.assertTrue(any("worker cleanup" in error for error in errors))
        self.assertFalse(process.running)
        self.assertEqual(broker.keys, set())
        self.assertFalse(run_root.exists())

    def test_shielded_finalizer_retries_log_eof_after_cancellation(self) -> None:
        class CancelOnceLog(_FakeLog):
            thread_alive = True

            def join(self, deadline: float) -> None:
                self.join_calls += 1
                if self.join_calls == 1:
                    class InjectedCancellation(BaseException):
                        pass

                    raise InjectedCancellation("cancelled log join")
                self.thread_alive = False

        log = CancelOnceLog()
        broker = _MemoryRedis({b"owned"})
        errors = _cleanup_roundtrip_resources(
            process=None,
            log=log,
            broker_client=broker,
            result_client=None,
            broker_initially_empty=True,
            result_initially_empty=False,
            broker_owned_keys={b"owned"},
            result_owned_keys=set(),
            broker_allow_prefixes=(),
            result_allow_fragments=(),
            run_root=None,
            deadline=time.monotonic() + 2,
        )

        self.assertTrue(any("worker log cleanup" in error for error in errors))
        self.assertEqual(log.join_calls, 2)
        self.assertFalse(log.thread_alive)
        self.assertEqual(broker.keys, set())

    def test_shielded_process_cleanup_preserves_foreign_sentinel(self) -> None:
        process = _FakeProcess()
        broker = _MemoryRedis({b"owned", b"foreign-sentinel"})

        class InjectedCancellation(BaseException):
            pass

        with patch(
            __name__ + "._terminate_worker_process",
            side_effect=InjectedCancellation("cancelled"),
        ):
            errors = _cleanup_roundtrip_resources(
                process=process,
                log=_FakeLog(),
                broker_client=broker,
                result_client=None,
                broker_initially_empty=False,
                result_initially_empty=False,
                broker_owned_keys={b"owned"},
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=None,
                deadline=time.monotonic() + 2,
            )

        self.assertTrue(any("worker cleanup" in error for error in errors))
        self.assertFalse(process.running)
        self.assertEqual(broker.keys, {b"owned", b"foreign-sentinel"})
        self.assertEqual(broker.delete_calls, [])

    def test_shielded_cleanup_retries_owned_redis_postcondition_after_cancellation(self) -> None:
        broker = _MemoryRedis({b"owned"})
        original_delete = _delete_owned_redis_keys
        calls = 0

        class InjectedCancellation(BaseException):
            pass

        def delete_once_then_continue(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InjectedCancellation("cancelled Redis cleanup")
            original_delete(*args, **kwargs)

        with patch(
            __name__ + "._delete_owned_redis_keys",
            side_effect=delete_once_then_continue,
        ):
            errors = _cleanup_roundtrip_resources(
                process=None,
                log=None,
                broker_client=broker,
                result_client=None,
                broker_initially_empty=True,
                result_initially_empty=False,
                broker_owned_keys={b"owned"},
                result_owned_keys=set(),
                broker_allow_prefixes=(),
                result_allow_fragments=(),
                run_root=None,
                deadline=time.monotonic() + 2,
            )

        self.assertTrue(any("Redis cleanup" in error for error in errors))
        self.assertEqual(calls, 2)
        self.assertEqual(broker.keys, set())
        self.assertTrue(broker.closed)

    def test_shielded_cleanup_retries_temporary_removal_after_cancellation(self) -> None:
        run_root = Path(tempfile.mkdtemp(prefix="platform-celery-shielded-"))
        original_rmtree = shutil.rmtree
        calls = 0

        class InjectedCancellation(BaseException):
            pass

        def rmtree_once_then_continue(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InjectedCancellation("cancelled temporary cleanup")
            original_rmtree(path)

        try:
            with patch(
                __name__ + ".shutil.rmtree",
                side_effect=rmtree_once_then_continue,
            ):
                errors = _cleanup_roundtrip_resources(
                    process=None,
                    log=None,
                    broker_client=None,
                    result_client=None,
                    broker_initially_empty=False,
                    result_initially_empty=False,
                    broker_owned_keys=set(),
                    result_owned_keys=set(),
                    broker_allow_prefixes=(),
                    result_allow_fragments=(),
                    run_root=run_root,
                    deadline=time.monotonic() + 2,
                )
        finally:
            if run_root.exists():
                original_rmtree(run_root)

        self.assertTrue(any("temporary-directory cleanup" in error for error in errors))
        self.assertEqual(calls, 2)
        self.assertFalse(run_root.exists())

    def test_cleanup_reraises_keyboard_interrupt_after_shielded_finalization(self) -> None:
        fatal_cases = (
            KeyboardInterrupt("SUPERSECRET/PRIVATE-LOG"),
            SystemExit("SUPERSECRET/PRIVATE-LOG"),
            SystemExit(2),
            SystemExit(99),
        )
        for fatal_exception in fatal_cases:
            fatal_type = type(fatal_exception)
            with self.subTest(
                fatal_type=fatal_type.__name__,
                fatal_code=getattr(fatal_exception, "code", None),
            ):
                process = _FakeProcess()
                broker = _MemoryRedis({b"owned"})
                run_root = Path(tempfile.mkdtemp(prefix="platform-celery-primary-"))

                try:
                    with patch(
                        __name__ + "._terminate_worker_process",
                        side_effect=fatal_exception,
                    ):
                        with self.assertRaises(fatal_type) as raised:
                            _finish_roundtrip_cleanup(
                                lambda: _cleanup_roundtrip_resources(
                                    process=process,
                                    log=_FakeLog(),
                                    broker_client=broker,
                                    result_client=None,
                                    broker_initially_empty=True,
                                    result_initially_empty=False,
                                    broker_owned_keys={b"owned"},
                                    result_owned_keys=set(),
                                    broker_allow_prefixes=(),
                                    result_allow_fragments=(),
                                    run_root=run_root,
                                    deadline=time.monotonic() + 2,
                                ),
                                primary_exception=None,
                            )

                    if fatal_type is KeyboardInterrupt:
                        self.assertEqual(str(raised.exception), "cleanup cancellation")
                    else:
                        expected_code = (
                            fatal_exception.code
                            if type(fatal_exception.code) is int
                            and fatal_exception.code in _ALLOWED_SYSTEM_EXIT_CODES
                            else _SAFE_SYSTEM_EXIT_CODE
                        )
                        self.assertEqual(raised.exception.code, expected_code)
                    self.assertIsNot(raised.exception, fatal_exception)
                    self.assertIsNone(raised.exception.__cause__)
                    self.assertIsNone(raised.exception.__context__)
                    self.assertTrue(raised.exception.__suppress_context__)
                    formatted = "".join(traceback.format_exception(raised.exception))
                    self.assertNotIn("SUPERSECRET", repr(raised.exception))
                    self.assertNotIn("PRIVATE-LOG", repr(raised.exception))
                    self.assertNotIn("SUPERSECRET", str(raised.exception))
                    self.assertNotIn("PRIVATE-LOG", str(raised.exception))
                    self.assertNotIn("SUPERSECRET", formatted)
                    self.assertNotIn("PRIVATE-LOG", formatted)

                    self.assertFalse(process.running)
                    self.assertEqual(broker.keys, set())
                    self.assertTrue(broker.closed)
                    self.assertFalse(run_root.exists())
                finally:
                    if run_root.exists():
                        shutil.rmtree(run_root)

    def test_primary_runtime_error_survives_cleanup_keyboard_interrupt(self) -> None:
        for cleanup_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(cleanup_type=cleanup_type.__name__):
                primary = RuntimeError("work body failed")
                process = _FakeProcess()
                broker = _MemoryRedis({b"owned"})
                run_root = Path(tempfile.mkdtemp(prefix="platform-celery-primary-"))

                with patch(
                    __name__ + "._terminate_worker_process",
                    side_effect=cleanup_type("worker-log=PRIVATE-LOG"),
                ):
                    try:
                        with self.assertRaises(RuntimeError) as raised:
                            try:
                                raise primary
                            except BaseException as caught:
                                _finish_roundtrip_cleanup(
                                    lambda: _cleanup_roundtrip_resources(
                                        process=process,
                                        log=_FakeLog(),
                                        broker_client=broker,
                                        result_client=None,
                                        broker_initially_empty=True,
                                        result_initially_empty=False,
                                        broker_owned_keys={b"owned"},
                                        result_owned_keys=set(),
                                        broker_allow_prefixes=(),
                                        result_allow_fragments=(),
                                        run_root=run_root,
                                        deadline=time.monotonic() + 2,
                                    ),
                                    primary_exception=caught,
                                )
                                raise
                        self.assertIs(raised.exception, primary)
                        self.assertEqual(str(raised.exception), "work body failed")
                        self.assertTrue(
                            any(
                                _cleanup_exception_code(cleanup_type("probe")) in note
                                for note in primary.__notes__
                            )
                        )
                        self.assertTrue(
                            any("worker cleanup" in note for note in primary.__notes__)
                        )
                        self.assertTrue(all("\n" not in note for note in primary.__notes__))
                        self.assertTrue(
                            all("PRIVATE-LOG" not in note for note in primary.__notes__)
                        )
                    finally:
                        if run_root.exists():
                            shutil.rmtree(run_root)

                self.assertFalse(process.running)
                self.assertEqual(broker.keys, set())
                self.assertTrue(broker.closed)
                self.assertFalse(run_root.exists())

    def test_primary_exception_gets_sanitized_ordinary_cleanup_note(self) -> None:
        primary = RuntimeError("work body failed")
        broker = _MemoryRedis({b"owned"})
        broker.delete_error = RuntimeError("redis://:SUPERSECRET/13")

        with self.assertRaises(RuntimeError) as raised:
            try:
                raise primary
            except BaseException as caught:
                _finish_roundtrip_cleanup(
                    lambda: _cleanup_roundtrip_resources(
                        process=None,
                        log=None,
                        broker_client=broker,
                        result_client=None,
                        broker_initially_empty=True,
                        result_initially_empty=False,
                        broker_owned_keys={b"owned"},
                        result_owned_keys=set(),
                        broker_allow_prefixes=(),
                        result_allow_fragments=(),
                        run_root=None,
                        deadline=time.monotonic() + 2,
                    ),
                    primary_exception=caught,
                )
                raise

        self.assertIs(raised.exception, primary)
        self.assertEqual(str(raised.exception), "work body failed")
        self.assertTrue(any("Redis cleanup" in note for note in primary.__notes__))
        self.assertTrue(all("\n" not in note for note in primary.__notes__))
        self.assertTrue(all("SUPERSECRET" not in note for note in primary.__notes__))
        self.assertTrue(all("redis://" not in note for note in primary.__notes__))
        self.assertTrue(broker.closed)

    def test_cleanup_report_retains_deduped_stage_evidence_after_fatal(self) -> None:
        def collect_report() -> _CleanupReport:
            process = _FakeProcess()
            broker = _MemoryRedis({b"broker-owned"})
            result = _MemoryRedis({b"result-owned"})
            broker.delete_error = RuntimeError("redis://:SUPERSECRET/13")
            result.delete_error = RuntimeError("worker-log=PRIVATE-LOG")
            run_root = Path(tempfile.mkdtemp(prefix="platform-celery-report-"))
            try:
                with patch(
                    __name__ + "._terminate_worker_process",
                    side_effect=KeyboardInterrupt("worker-log=PRIVATE-LOG"),
                ):
                    report = _cleanup_roundtrip_resources(
                        process=process,
                        log=_FakeLog(),
                        broker_client=broker,
                        result_client=result,
                        broker_initially_empty=True,
                        result_initially_empty=True,
                        broker_owned_keys={b"broker-owned"},
                        result_owned_keys={b"result-owned"},
                        broker_allow_prefixes=(),
                        result_allow_fragments=(),
                        result_allow_prefixes=(),
                        run_root=run_root,
                        deadline=time.monotonic() + 2,
                    )
                return report
            finally:
                if run_root.exists():
                    shutil.rmtree(run_root)

        first_report = collect_report()
        second_report = collect_report()
        first_evidence = tuple(item.render() for item in first_report.evidence)
        second_evidence = tuple(item.render() for item in second_report.evidence)

        self.assertEqual(first_evidence, second_evidence)
        self.assertEqual(first_report.fatal_kind, "keyboard-interrupt")
        self.assertIsNone(first_report.fatal_exit_code)
        self.assertEqual(second_report.fatal_kind, "keyboard-interrupt")
        self.assertIsNone(second_report.fatal_exit_code)
        serialized = repr(first_report) + str(first_report) + repr(asdict(first_report))
        self.assertNotIn("SUPERSECRET", serialized)
        self.assertNotIn("PRIVATE-LOG", serialized)
        self.assertEqual(len(first_evidence), len(set(first_evidence)))
        self.assertLessEqual(len(first_evidence), MAX_CLEANUP_EVIDENCE)
        self.assertTrue(
            any("worker cleanup [keyboard-interrupt]" in item for item in first_evidence)
        )
        self.assertTrue(
            any(
                "Celery broker Redis cleanup [runtime-error]" in item
                for item in first_evidence
            )
        )
        self.assertTrue(any("postcondition-unproven" in item for item in first_evidence))
        self.assertTrue(all("SUPERSECRET" not in item for item in first_evidence))
        self.assertTrue(all("PRIVATE-LOG" not in item for item in first_evidence))

        primary = RuntimeError("work body failed")
        _finish_roundtrip_cleanup(lambda: first_report, primary_exception=primary)
        self.assertTrue(any("worker cleanup" in note for note in primary.__notes__))
        self.assertTrue(
            any("Celery broker Redis cleanup" in note for note in primary.__notes__)
        )
        self.assertTrue(all("SUPERSECRET" not in note for note in primary.__notes__))
        self.assertTrue(all("PRIVATE-LOG" not in note for note in primary.__notes__))

        active_primary = RuntimeError("SUPERSECRET")
        with self.assertRaises(RuntimeError) as raised:
            try:
                raise active_primary
            except RuntimeError:
                _finish_roundtrip_cleanup(
                    lambda: first_report,
                    primary_exception=None,
                )
                raise
        self.assertIs(raised.exception, active_primary)
        self.assertIsNone(raised.exception.__context__)
        self.assertTrue(
            all("SUPERSECRET" not in note for note in active_primary.__notes__)
        )
        self.assertTrue(
            all("PRIVATE-LOG" not in note for note in active_primary.__notes__)
        )
        self.assertTrue(
            any(
                "worker cleanup [keyboard-interrupt]" in note
                for note in active_primary.__notes__
            )
        )

        explicit_primary = RuntimeError("explicit primary")
        active_other = RuntimeError("ACTIVE-SECRET")
        try:
            raise active_other
        except RuntimeError:
            _finish_roundtrip_cleanup(
                lambda: first_report,
                primary_exception=explicit_primary,
            )
        self.assertTrue(
            any(
                "worker cleanup [keyboard-interrupt]" in note
                for note in explicit_primary.__notes__
            )
        )
        self.assertEqual(getattr(active_other, "__notes__", ()), ())

    def test_cleanup_catches_hung_redis_scan_without_skipping_close(self) -> None:
        broker = _ScanFailureRedis()
        result = _MemoryRedis()

        errors = _cleanup_roundtrip_resources(
            process=None,
            log=None,
            broker_client=broker,
            result_client=result,
            broker_initially_empty=True,
            result_initially_empty=True,
            broker_owned_keys=set(),
            result_owned_keys=set(),
            broker_allow_prefixes=(),
            result_allow_fragments=(),
            run_root=None,
            deadline=time.monotonic() + 2,
        )

        self.assertTrue(any("Redis cleanup" in error for error in errors))
        self.assertTrue(broker.closed)
        self.assertTrue(result.closed)

    def test_log_drain_keeps_flood_diagnostics_bounded_and_reaches_eof(self) -> None:
        stream = _FloodStream(b"x" * (MAX_LOG_CHUNKS * MAX_LOG_CHUNK_BYTES * 8))
        log = _BoundedWorkerLog(stream)

        log.join(time.monotonic() + 2)

        self.assertFalse(log.thread_alive)
        self.assertTrue(log.eof.is_set())
        self.assertLessEqual(len(log.diagnostics()), MAX_LOG_CHUNKS * MAX_LOG_CHUNK_BYTES)


if __name__ == "__main__":
    unittest.main()
