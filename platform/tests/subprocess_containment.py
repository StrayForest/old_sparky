"""Bounded, identity-safe subprocess helpers for platform contract tests.

This module is intentionally test-only.  It is a small Linux supervisor for
the lock-holder tests, not a production process manager.  The child is
created with ``posix_spawnp`` and an explicit descriptor allow-list.  A
subreaper plus procfs start-time identities lets cleanup account for children
which detach from the original process group.  Every operation uses one
absolute monotonic deadline which starts before spawn.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import errno
import fcntl
import math
import os
from pathlib import Path
import select
import selectors
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Mapping, Sequence


DEFAULT_DIAGNOSTIC_BYTES = 4 * 1024
DEFAULT_TERM_GRACE_SECONDS = 0.15
DEFAULT_KILL_GRACE_SECONDS = 0.35
_READY_LINE_BYTES = 512
_PIPE_READ_BYTES = 64 * 1024
_FD_DUP_MIN = 10
# Reserve a small part of the original budget for TERM/KILL/reap.  This is
# inside the deadline (never an additive grace), so a timeout cannot begin
# cleanup at the exact edge with no budget left for the kernel syscall.
_CLEANUP_RESERVE_SECONDS = 0.15
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_LIFECYCLE_LOCK = threading.Lock()
_LIFECYCLE_OWNER_THREAD: int | None = None
_SHIELDED_SIGNALS = (
    signal.SIGINT,
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGQUIT,
)
_OWNERSHIP_ENV = "_OLD_SPARKY_TEST_SUPERVISOR"
_PROC_PRESENT = "present"
_PROC_UNKNOWN = "unknown"
_PROC_ABSENT = "absent"


def _finite_deadline(value: float, *, label: str) -> float:
    """Validate a positive relative deadline without accepting infinity."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite positive number")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return float(value)


class _DiagnosticBuffer:
    """Keep only bounded test diagnostics while continuing to drain a pipe."""

    __slots__ = ("cap", "data", "total", "truncated")

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.data = bytearray()
        self.total = 0
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = self.cap - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            self.truncated = True

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class BoundedDiagnostics:
    """Non-sensitive aggregate child output metadata."""

    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    ready_line: str | None


@dataclass(frozen=True, slots=True)
class BoundedCompletedProcess:
    """A completed child result with output bounded by the helper cap."""

    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    diagnostics: BoundedDiagnostics


class ChildContainmentError(RuntimeError):
    """Base error carrying a stable reason and bounded diagnostics only."""

    reason = "child_error"

    def __init__(
        self,
        message: str,
        *,
        diagnostics: BoundedDiagnostics,
        cleanup_proven: bool | None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
        self.cleanup_proven = cleanup_proven


class ChildTimeoutError(ChildContainmentError):
    """The finite deadline expired."""

    reason = "timeout"


class CleanupUnprovenError(ChildContainmentError):
    """The child tree was not fully reaped or its identity was ambiguous."""

    reason = "cleanup_unproven"


class ChildExitedBeforeReady(ChildContainmentError):
    """The child exited before emitting its requested ready marker."""

    reason = "exited_before_ready"


@dataclass(frozen=True, slots=True)
class _ProcRecord:
    pid: int
    ppid: int
    pgrp: int
    session: int
    start_time_ticks: int
    state: str

    @property
    def identity(self) -> tuple[int, int]:
        return self.pid, self.start_time_ticks


@dataclass(frozen=True, slots=True)
class _ProcObservation:
    """A procfs observation whose absence is never inferred from an error."""

    state: str
    record: _ProcRecord | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _ProcSnapshot:
    records: dict[int, _ProcRecord]
    unknown_pids: frozenset[int]
    listing_unknown: bool = False
    unknown_reasons: Mapping[int, str] | None = None


def _read_proc_record(pid: int) -> _ProcObservation:
    """Read a process identity, preserving every procfs uncertainty."""

    if pid <= 0:
        return _ProcObservation(_PROC_UNKNOWN, reason="invalid_pid")
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return _ProcObservation(_PROC_UNKNOWN, reason="proc_disappeared")
    except (OSError, UnicodeError) as error:
        return _ProcObservation(_PROC_UNKNOWN, reason=type(error).__name__)
    try:
        end = raw.rfind(")")
        if end < 0:
            return _ProcObservation(_PROC_UNKNOWN, reason="malformed_stat")
        fields = raw[end + 2 :].split()
        # fields[0] is stat field 3; starttime is field 22.
        if len(fields) < 20:
            return _ProcObservation(_PROC_UNKNOWN, reason="short_stat")
        return _ProcObservation(
            _PROC_PRESENT,
            _ProcRecord(
                pid=pid,
                ppid=int(fields[1]),
                pgrp=int(fields[2]),
                session=int(fields[3]),
                start_time_ticks=int(fields[19]),
                state=fields[0],
            ),
        )
    except (UnicodeError, ValueError, IndexError) as error:
        return _ProcObservation(_PROC_UNKNOWN, reason=type(error).__name__)


def _proc_snapshot() -> _ProcSnapshot:
    records: dict[int, _ProcRecord] = {}
    unknown_pids: set[int] = set()
    unknown_reasons: dict[int, str] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return _ProcSnapshot(records, frozenset(), listing_unknown=True)
    for entry in entries:
        if not entry.isdigit():
            continue
        observation = _read_proc_record(int(entry))
        if observation.state == _PROC_PRESENT and observation.record is not None:
            records[observation.record.pid] = observation.record
        elif observation.state == _PROC_UNKNOWN:
            pid = int(entry)
            unknown_pids.add(pid)
            unknown_reasons[pid] = observation.reason or _PROC_UNKNOWN
    return _ProcSnapshot(
        records,
        frozenset(unknown_pids),
        unknown_reasons=unknown_reasons,
    )


def _snapshot_unknown_is_ambiguous(snapshot: _ProcSnapshot, pid: int) -> bool:
    """Return whether an unreadable entry could still be a live process.

    A process disappearing between ``/proc`` listing and ``stat`` is a
    positive absence observation, not a candidate which can be adopted.  All
    other missing/error/parse outcomes remain ambiguous, including snapshots
    supplied by focused tests without a reason map.
    """

    reasons = snapshot.unknown_reasons
    return reasons is None or reasons.get(pid, _PROC_UNKNOWN) != "proc_disappeared"


def _prctl() -> Callable[..., int]:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    return prctl


def _get_subreaper() -> int | None:
    """Read Linux child-subreaper state without mutating it."""

    if not sys.platform.startswith("linux"):
        return None
    previous = ctypes.c_int()
    prctl = _prctl()
    if prctl(_PR_GET_CHILD_SUBREAPER, ctypes.addressof(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER failed")
    return int(previous.value)


def _set_subreaper(enabled: int) -> None:
    """Set Linux child-subreaper state after the caller captured the prior state."""

    if not sys.platform.startswith("linux"):
        return
    prctl = _prctl()
    if prctl(_PR_SET_CHILD_SUBREAPER, int(enabled), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")


def _restore_subreaper(previous: int | None) -> None:
    if previous is None:
        return
    _set_subreaper(previous)
    current = _get_subreaper()
    if current != previous:
        raise OSError(errno.EIO, "restoring child subreaper did not verify")


class _SpawnDeadlineExceeded(TimeoutError):
    """SIGALRM interrupted the spawn syscall at the absolute deadline."""


class _SpawnDeadlineAlarm:
    """Temporarily own SIGALRM/ITIMER_REAL without extending old alarms.

    ``ITIMER_REAL`` is process-global.  The previous timer therefore keeps
    running while this context owns the signal; restoring its original delay
    would silently postpone the caller's deadline by the time spent in the
    spawn operation.  Restore the handler first, then install the old timer
    at its original phase (coalescing an already-due firing to a short,
    positive delay so the old handler receives it after this context exits).
    """

    _TIMER_EPSILON_SECONDS = 0.001
    _active = False

    def __init__(self, seconds: float) -> None:
        self.seconds = _finite_deadline(seconds, label="spawn_alarm_seconds")
        self._previous_handler: object | None = None
        self._previous_timer: tuple[float, float] | None = None
        self._installed_at: float | None = None
        self._previous_captured = False
        self._armed = False

    def __enter__(self) -> "_SpawnDeadlineAlarm":
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("spawn deadline alarm requires the main thread")
        if type(self)._active:
            raise RuntimeError("nested spawn deadline alarms are not supported")
        try:
            self._previous_handler = signal.getsignal(signal.SIGALRM)
            self._previous_timer = signal.getitimer(signal.ITIMER_REAL)
            self._previous_captured = True
            self._installed_at = time.monotonic()

            def handle_alarm(_signum: int, _frame: object) -> None:
                raise _SpawnDeadlineExceeded("spawn deadline expired")

            signal.signal(signal.SIGALRM, handle_alarm)
            signal.setitimer(signal.ITIMER_REAL, self.seconds, 0.0)
            self._armed = True
            type(self)._active = True
        except BaseException:
            self._restore_after_failure()
            raise
        return self

    def _restored_timer(self) -> tuple[float, float]:
        previous_timer = self._previous_timer
        installed_at = self._installed_at
        if previous_timer is None or installed_at is None:
            return (0.0, 0.0)
        previous_delay, interval = previous_timer
        if previous_delay <= 0.0:
            # ``setitimer`` treats a zero first delay as disabled.  Preserve
            # the tuple's interval for callers which inspect it, although a
            # non-zero interval has no effect until a first delay is set.
            return (0.0, max(0.0, interval))
        elapsed = max(0.0, time.monotonic() - installed_at)
        if interval <= 0.0:
            remaining = previous_delay - elapsed
            return (
                max(self._TIMER_EPSILON_SECONDS, remaining),
                0.0,
            )
        if elapsed < previous_delay:
            return (previous_delay - elapsed, interval)
        # The periodic timer's phase is based on its original first firing,
        # not on context exit.  Coalesce skipped firings and schedule the
        # first future one in the same interval.
        overdue = elapsed - previous_delay
        cycles = math.floor(overdue / interval) + 1
        next_delay = previous_delay + (cycles * interval) - elapsed
        return (max(self._TIMER_EPSILON_SECONDS, next_delay), interval)

    def _restore_after_failure(self) -> None:
        if not self._previous_captured:
            return
        errors: list[BaseException] = []
        restored_timer = self._restored_timer()
        for action in (
            lambda: signal.setitimer(signal.ITIMER_REAL, 0.0, 0.0),
            lambda: signal.signal(signal.SIGALRM, self._previous_handler),
            lambda: signal.setitimer(signal.ITIMER_REAL, *restored_timer),
        ):
            try:
                action()
            except BaseException as error:
                errors.append(error)
        self._armed = False
        type(self)._active = False
        self._installed_at = None
        self._previous_captured = False
        if errors:
            raise errors[0]

    def __exit__(self, exc_type: object, exc_value: BaseException | None, traceback: object) -> bool:
        if not self._armed:
            return False
        try:
            self._restore_after_failure()
        except BaseException as restore_error:
            if exc_value is not None:
                exc_value.add_note("spawn alarm state restoration failed")
            else:
                raise restore_error
        return False


class _DiagnosticReadyScanner:
    """Recognise only complete lines beginning with the exact marker."""

    __slots__ = ("marker", "line", "found")

    def __init__(self, marker: bytes) -> None:
        if not marker or b"\r" in marker or b"\n" in marker:
            raise ValueError("ready_marker must be a non-empty single-line byte prefix")
        self.marker = marker
        self.line = bytearray()
        self.found = False

    def feed(self, chunk: bytes) -> str | None:
        if self.found:
            return bytes(self.line).decode("utf-8", errors="replace")
        self.line.extend(chunk)
        if len(self.line) > _READY_LINE_BYTES:
            del self.line[:-_READY_LINE_BYTES]
        while True:
            newline = self.line.find(b"\n")
            if newline < 0:
                # Keep an incomplete boundary line.  It is deliberately not
                # a ready result until the newline arrives.
                return None
            line = bytes(self.line[:newline])
            del self.line[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            if len(line) > _READY_LINE_BYTES:
                continue
            if line.startswith(self.marker):
                self.line = bytearray(line)
                self.found = True
                return line.decode("utf-8", errors="replace")
        return None


# Kept as the short private name used by focused tests and previous callers.
_ReadyScanner = _DiagnosticReadyScanner


class _PosixProcess:
    """Popen-compatible handle backed by waitpid and one owner callback."""

    def __init__(self, pid: int, poller: Callable[[], int | None], waiter: Callable[[float | None], int]) -> None:
        self.pid = pid
        self._poller = poller
        self._waiter = waiter
        self.returncode: int | None = None

    def poll(self) -> int | None:
        result = self._poller()
        self.returncode = result
        return result

    def wait(self, timeout: float | None = None) -> int:
        result = self._waiter(timeout)
        self.returncode = result
        return result


class BoundedChild:
    """Manage one Linux child with bounded I/O and identity-safe cleanup."""

    def __init__(
        self,
        args: Sequence[str],
        *,
        deadline_seconds: float,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        stdin_mode: str = "devnull",
        ready_marker: bytes | str | None = None,
        diagnostic_bytes: int = DEFAULT_DIAGNOSTIC_BYTES,
        term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS,
        kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
    ) -> None:
        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise RuntimeError("bounded subprocess containment requires Linux POSIX process groups")
        if not args or any(not isinstance(argument, str) for argument in args):
            raise ValueError("args must be a non-empty sequence of strings")
        if stdin_mode not in {"devnull", "pipe"}:
            raise ValueError("stdin_mode must be 'devnull' or 'pipe'")
        if isinstance(diagnostic_bytes, bool) or not isinstance(diagnostic_bytes, int):
            raise ValueError("diagnostic_bytes must be a positive integer")
        if diagnostic_bytes <= 0:
            raise ValueError("diagnostic_bytes must be a positive integer")
        self.args = tuple(args)
        self.deadline_seconds = _finite_deadline(deadline_seconds, label="deadline_seconds")
        self.term_grace_seconds = _finite_deadline(term_grace_seconds, label="term_grace_seconds")
        self.kill_grace_seconds = _finite_deadline(kill_grace_seconds, label="kill_grace_seconds")
        self.cwd = None if cwd is None else Path(cwd)
        self.env = None if env is None else dict(env)
        self.stdin_mode = stdin_mode
        if isinstance(ready_marker, str):
            ready_marker = ready_marker.encode("utf-8")
        self.ready_marker = ready_marker
        self.diagnostic_bytes = diagnostic_bytes

        self.process: _PosixProcess | None = None
        self._selector: selectors.BaseSelector | None = None
        self._stdin: object | None = None
        self._parent_stdin: object | None = None
        self._parent_fds: set[int] = set()
        self._parent_streams: tuple[tuple[int, str, str], ...] = ()
        self._streams: dict[int, object] = {}
        self._stdout = _DiagnosticBuffer(diagnostic_bytes)
        self._stderr = _DiagnosticBuffer(diagnostic_bytes)
        self._ready = None if ready_marker is None else _DiagnosticReadyScanner(ready_marker)
        self._deadline = 0.0
        self._closed = False
        self.cleanup_proven: bool | None = None
        self._leader_pid: int | None = None
        self._leader_identity: _ProcRecord | None = None
        self._leader_returncode: int | None = None
        self._leader_reap_lost = False
        self._tracked: dict[int, _ProcRecord] = {}
        self._reaped: set[int] = set()
        self._baseline_children: set[tuple[int, int]] = set()
        self._baseline_unknown_pids: set[int] = set()
        self._identity_changed = False
        self._unclaimed_adopted = False
        self._procfs_unknown: set[int] = set()
        self._procfs_listing_unknown = False
        self._signal_unproven = False
        self._absent_proven: set[int] = set()
        self._subreaper_previous: int | None = None
        self._lock_held = False
        self._spawned = False
        self._spawn_attempted = False
        self._pidfd: int | None = None
        self._pidfds: dict[int, int] = {}
        self._ownership_token = secrets.token_hex(16)

    def __enter__(self) -> "BoundedChild":
        global _LIFECYCLE_OWNER_THREAD
        if self.process is not None:
            raise RuntimeError("bounded child cannot be entered twice")
        self._deadline = time.monotonic() + self.deadline_seconds
        remaining = self._remaining()
        current_thread = threading.get_ident()
        if _LIFECYCLE_OWNER_THREAD == current_thread:
            raise RuntimeError("nested bounded child supervisors are not supported")
        if remaining <= 0 or not _LIFECYCLE_LOCK.acquire(timeout=remaining):
            raise ChildTimeoutError(
                "lifecycle lock deadline expired before spawn",
                diagnostics=self._diagnostics(),
                cleanup_proven=False,
            )
        self._lock_held = True
        _LIFECYCLE_OWNER_THREAD = current_thread
        try:
            # Capture before mutation so a set-after-read failure can still
            # restore the caller's exact subreaper state.
            self._subreaper_previous = _get_subreaper()
            if self._remaining() <= 0:
                raise ChildTimeoutError(
                    "child deadline expired before spawn",
                    diagnostics=self._diagnostics(),
                    cleanup_proven=False,
                )
            _set_subreaper(1)
            baseline = _proc_snapshot()
            self._procfs_listing_unknown = baseline.listing_unknown
            self._baseline_unknown_pids = set(baseline.unknown_pids)
            self._baseline_children = {
                record.identity
                for record in baseline.records.values()
                if record.ppid == os.getpid()
            }
            self._spawn()
            self._selector = selectors.DefaultSelector()
            assert self._leader_pid is not None
            self.process = _PosixProcess(
                self._leader_pid,
                self._poll_leader,
                self._wait_leader,
            )
            for fd, label, mode in self._parent_streams:
                stream = os.fdopen(fd, mode, buffering=0)
                try:
                    self._parent_fds.discard(fd)
                    os.set_blocking(fd, False)
                    self._selector.register(stream, selectors.EVENT_READ, label)
                    self._streams[fd] = stream
                except BaseException:
                    stream.close()
                    raise
            self._stdin = self._parent_stdin
            if self._stdin is not None:
                os.set_blocking(self._stdin.fileno(), False)  # type: ignore[union-attr]
            if self._remaining() <= 0:
                self._abort_for_timeout("child spawn deadline expired")
        except BaseException as setup_error:
            try:
                self.close()
            except BaseException as cleanup_error:
                if isinstance(cleanup_error, CleanupUnprovenError):
                    raise cleanup_error from setup_error
                setup_error.add_note("subprocess cleanup failed during setup")
            if isinstance(setup_error, TimeoutError):
                raise ChildTimeoutError(
                    "child spawn deadline expired",
                    diagnostics=self._diagnostics(),
                    cleanup_proven=self.cleanup_proven,
                ) from setup_error
            raise
        return self

    def _spawn(self) -> None:
        """Spawn with posix_spawn, explicit descriptors and signal shielding."""

        parent_stdin = parent_stdout = parent_stderr = None
        child_stdin = child_stdout = child_stderr = None
        try:
            # stdin is reversed: the child owns the read end and the parent
            # owns the write end; stdout/stderr retain the usual orientation.
            child_stdin, parent_stdin = self._pipe_pair()
            parent_stdout, child_stdout = self._pipe_pair()
            parent_stderr, child_stderr = self._pipe_pair()
            for fd in (child_stdin, child_stdout, child_stderr):
                os.set_inheritable(fd, True)
            file_actions: list[tuple[object, ...]] = [
                (os.POSIX_SPAWN_DUP2, child_stdin, 0),
                (os.POSIX_SPAWN_DUP2, child_stdout, 1),
                (os.POSIX_SPAWN_DUP2, child_stderr, 2),
            ]
            keep = {child_stdin, child_stdout, child_stderr}
            for entry in os.listdir("/proc/self/fd"):
                if not entry.isdigit():
                    continue
                fd = int(entry)
                if fd > 2 and fd not in keep:
                    file_actions.append((os.POSIX_SPAWN_CLOSE, fd))
            for fd in keep:
                file_actions.append((os.POSIX_SPAWN_CLOSE, fd))
            spawn_args: tuple[str, ...]
            if self.cwd is None:
                spawn_args = self.args
            else:
                # posix_spawn has no cwd parameter.  A fixed shell wrapper
                # performs only cd+exec, retaining the same session/PID.
                spawn_args = (
                    "/bin/sh",
                    "-c",
                    'cd "$1" && shift && exec "$@"',
                    "bounded-cwd",
                    str(self.cwd),
                    *self.args,
                )
            environment = dict(os.environ) if self.env is None else dict(self.env)
            # The token lets a subreaper distinguish an adopted detached
            # descendant from an unrelated process created concurrently.
            # Children which deliberately clear their environment are
            # treated as unclaimable and cleanup remains unproven.
            environment[_OWNERSHIP_ENV] = self._ownership_token
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _SHIELDED_SIGNALS)
            try:
                alarm_seconds = self._work_remaining()
                if alarm_seconds <= 0:
                    raise TimeoutError("spawn deadline expired")
                self._spawn_attempted = True
                with _SpawnDeadlineAlarm(alarm_seconds):
                    pid = os.posix_spawnp(
                        spawn_args[0],
                        spawn_args,
                        environment,
                        file_actions=file_actions,
                        setsid=True,
                        setsigmask=(),
                        setsigdef=_SHIELDED_SIGNALS,
                    )
                # Publish the child identity before restoring a pending
                # parent signal; a delivered KeyboardInterrupt cannot leave
                # a post-spawn PID unowned by cleanup.
                self._leader_pid = int(pid)
                self._spawned = True
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            self._pidfd = self._open_pidfd(pid)
            self._leader_identity = self._wait_for_identity(pid)
            self._tracked[pid] = self._leader_identity
            self._parent_streams = (
                (parent_stdout, "stdout", "rb"),
                (parent_stderr, "stderr", "rb"),
            )
            self._parent_fds.update((parent_stdout, parent_stderr))
            self._parent_stdin = (
                os.fdopen(parent_stdin, "wb", buffering=0)
                if self.stdin_mode == "pipe"
                else None
            )
            if self._parent_stdin is not None:
                self._parent_fds.discard(parent_stdin)
            if self.stdin_mode != "pipe":
                os.close(parent_stdin)
            parent_stdin = None
            parent_stdout = parent_stderr = None
        except BaseException:
            if self._parent_stdin is not None:
                try:
                    self._parent_stdin.close()
                except (OSError, ValueError):
                    pass
                self._parent_stdin = None
            for fd in (parent_stdin, parent_stdout, parent_stderr, child_stdin, child_stdout, child_stderr):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if self._spawn_attempted and self._leader_pid is None:
                try:
                    self._recover_spawned_child()
                except BaseException as recovery_error:
                    if isinstance(recovery_error, CleanupUnprovenError):
                        raise
            raise
        finally:
            for fd in (child_stdin, child_stdout, child_stderr):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @staticmethod
    def _pipe_pair() -> tuple[int, int]:
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        try:
            high_read = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, _FD_DUP_MIN)
            high_write = fcntl.fcntl(write_fd, fcntl.F_DUPFD_CLOEXEC, _FD_DUP_MIN)
        except BaseException:
            os.close(read_fd)
            os.close(write_fd)
            raise
        os.close(read_fd)
        os.close(write_fd)
        return high_read, high_write

    @staticmethod
    def _open_pidfd(pid: int) -> int | None:
        pidfd_open = getattr(os, "pidfd_open", None)
        if not callable(pidfd_open):
            return None
        try:
            return int(pidfd_open(pid, 0))
        except (OSError, ValueError):
            return None

    def _recover_spawned_child(self) -> None:
        """Recover a child whose spawn returned after an interrupt boundary."""

        recovery_end = min(self._deadline, time.monotonic() + self._cleanup_reserve())
        unknown_candidates: set[int] = set()
        while True:
            snapshot = _proc_snapshot()
            self._procfs_listing_unknown |= snapshot.listing_unknown
            candidates: list[_ProcRecord] = []
            zombie_candidates: list[_ProcRecord] = []
            unknown_candidates.clear()
            for pid in snapshot.unknown_pids:
                if (
                    pid in self._baseline_unknown_pids
                    or not _snapshot_unknown_is_ambiguous(snapshot, pid)
                ):
                    continue
                ownership = self._proc_has_ownership_token(pid)
                if ownership is None or ownership:
                    # A token without a readable proc identity is still an
                    # ambiguous candidate; never infer absence from either
                    # procfs or environ read failure.
                    unknown_candidates.add(pid)
            for record in snapshot.records.values():
                if record.identity in self._baseline_children:
                    continue
                ownership = self._proc_has_ownership_token(record.pid)
                if ownership is None:
                    if record.ppid == os.getpid() or record.pgrp == record.pid:
                        if record.state.lower() == "z" and record.ppid == os.getpid():
                            zombie_candidates.append(record)
                        else:
                            unknown_candidates.add(record.pid)
                elif ownership:
                    candidates.append(record)
            direct = [record for record in candidates if record.ppid == os.getpid()]
            self._procfs_unknown.update(unknown_candidates)
            if len(direct) == 1:
                record = direct[0]
                self._leader_pid = record.pid
                self._leader_identity = record
                for candidate in candidates:
                    self._tracked[candidate.pid] = candidate
                    if candidate.pid != record.pid:
                        pidfd = self._open_pidfd(candidate.pid)
                        if pidfd is not None:
                            self._pidfds[candidate.pid] = pidfd
                self._pidfd = self._open_pidfd(record.pid)
                self._spawned = True
                return
            if candidates:
                self._unclaimed_adopted = True
                return
            if len(zombie_candidates) == 1 and not candidates:
                record = zombie_candidates[0]
                self._leader_pid = record.pid
                self._leader_identity = record
                self._tracked[record.pid] = record
                self._spawned = True
                return
            remaining = recovery_end - time.monotonic()
            if remaining <= 0:
                self._procfs_unknown.update(unknown_candidates)
                return
            time.sleep(min(0.001, remaining))

    def _wait_for_identity(self, pid: int) -> _ProcRecord:
        while True:
            observation = _read_proc_record(pid)
            if observation.state == _PROC_PRESENT and observation.record is not None:
                return observation.record
            if observation.state == _PROC_UNKNOWN:
                self._procfs_unknown.add(pid)
            if self._remaining() <= 0:
                raise TimeoutError("child identity unavailable before deadline")
            time.sleep(min(0.001, self._remaining()))

    def _require_open(self) -> _PosixProcess:
        if self.process is None or self._closed:
            raise RuntimeError("bounded child is not active")
        return self.process

    def _diagnostics(self) -> BoundedDiagnostics:
        ready_line = None
        if self._ready is not None and self._ready.found:
            ready_line = bytes(self._ready.line).decode("utf-8", errors="replace")
        return BoundedDiagnostics(
            stdout_bytes=self._stdout.total,
            stderr_bytes=self._stderr.total,
            stdout_truncated=self._stdout.truncated,
            stderr_truncated=self._stderr.truncated,
            ready_line=ready_line,
        )

    @staticmethod
    def _decode_wait_status(status: int) -> int:
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return 1

    def _poll_leader(self) -> int | None:
        if self._leader_returncode is not None:
            return self._leader_returncode
        if self._leader_pid is None:
            return None
        try:
            pid, status = os.waitpid(self._leader_pid, os.WNOHANG)
        except ChildProcessError:
            self._leader_reap_lost = True
            self._leader_returncode = 1
            return self._leader_returncode
        if pid == 0:
            return None
        self._reaped.add(pid)
        self._procfs_unknown.discard(pid)
        self._leader_returncode = self._decode_wait_status(status)
        return self._leader_returncode

    def _wait_leader(self, timeout: float | None) -> int:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        end = None if timeout is None else time.monotonic() + timeout
        while self._poll_leader() is None:
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.001)
        return int(self._leader_returncode)

    def _read_stream(self, key: selectors.SelectorKey) -> None:
        if key.data == "stdin":
            return
        stream = key.fileobj
        try:
            chunk = os.read(stream.fileno(), _PIPE_READ_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._unregister(stream)
            return
        if key.data == "stdout":
            self._stdout.append(chunk)
            if self._ready is not None:
                self._ready.feed(chunk)
        else:
            self._stderr.append(chunk)

    def _unregister(self, stream: object) -> None:
        selector = self._selector
        if selector is None:
            return
        try:
            stream_fd = stream.fileno()  # type: ignore[union-attr]
        except (OSError, ValueError):
            stream_fd = -1
        try:
            selector.unregister(stream)
        except (KeyError, ValueError, OSError):
            pass
        try:
            stream.close()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass
        if stream_fd >= 0:
            self._streams.pop(stream_fd, None)

    def _pump(self, timeout: float) -> None:
        selector = self._selector
        if selector is None or not selector.get_map():
            return
        for key, _mask in selector.select(max(0.0, timeout)):
            self._read_stream(key)

    def _remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _cleanup_reserve(self) -> float:
        return min(_CLEANUP_RESERVE_SECONDS, self.deadline_seconds / 2)

    def _work_remaining(self) -> float:
        return max(0.0, self._remaining() - self._cleanup_reserve())

    def _wait_for_exit_until(self, end: float) -> bool:
        while self._poll_leader() is None:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self._pump(min(remaining, 0.05))
            self._discover_descendants()
            self._reap_known()
        while self._selector is not None and self._selector.get_map():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            self._pump(min(remaining, 0.05))
        return True

    def wait_for_ready(self) -> str:
        process = self._require_open()
        if self._ready is None:
            raise ValueError("wait_for_ready requires ready_marker")
        while not self._ready.found:
            self._discover_descendants()
            if process.poll() is not None:
                self._wait_for_exit_until(min(self._deadline, time.monotonic() + 0.1))
                self._discover_descendants()
                raise ChildExitedBeforeReady(
                    "child exited before ready marker",
                    diagnostics=self._diagnostics(),
                    cleanup_proven=self.cleanup_proven,
                )
            if self._work_remaining() <= 0:
                self._abort_for_timeout("ready marker deadline expired")
            self._pump(min(self._remaining(), 0.05))
            self._discover_descendants()
        return bytes(self._ready.line).decode("utf-8", errors="replace")

    def send_line(self, line: str) -> None:
        self._require_open()
        if self._stdin is None:
            raise ValueError("send_line requires stdin_mode='pipe'")
        payload = line.encode("utf-8") + b"\n"
        offset = 0
        selector = self._selector
        assert selector is not None
        while offset < len(payload):
            if self._work_remaining() <= 0:
                self._abort_for_timeout("stdin release deadline expired")
            try:
                offset += os.write(self._stdin.fileno(), payload[offset:])  # type: ignore[union-attr]
                continue
            except (BlockingIOError, InterruptedError):
                pass
            except BrokenPipeError as exc:
                raise ChildContainmentError(
                    "child closed its stdin before release",
                    diagnostics=self._diagnostics(),
                    cleanup_proven=self.cleanup_proven,
                ) from exc
            selector.register(self._stdin, selectors.EVENT_WRITE, "stdin")
            try:
                self._pump(min(self._remaining(), 0.05))
            finally:
                try:
                    selector.unregister(self._stdin)
                except (KeyError, ValueError, OSError):
                    pass
        try:
            self._stdin.close()  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass
        self._stdin = None

    def _leader_identity_matches(self) -> bool:
        expected = self._leader_identity
        pid = self._leader_pid
        if expected is None or pid is None:
            return False
        if pid in self._reaped or pid in self._absent_proven:
            return False
        observation = _read_proc_record(pid)
        if observation.state == _PROC_UNKNOWN:
            self._procfs_unknown.add(pid)
            return False
        current = observation.record
        return bool(
            current is not None
            and current.identity == expected.identity
            and current.pgrp == pid
            and current.session == pid
            and current.state.lower() not in {"z", "x"}
        )

    def _discover_descendants(self) -> None:
        if self._leader_pid is None:
            return
        snapshot = _proc_snapshot()
        self._procfs_listing_unknown |= snapshot.listing_unknown
        # A newly unreadable procfs entry may be a detached child which was
        # reparented after the previous scan.  Keep the ambiguity sticky; do
        # not kill it by numeric PID and do not later turn the error into a
        # false cleanup proof.  Entries already unknown at baseline are
        # foreign to this supervisor's ownership window.
        self._procfs_unknown.update(
            pid
            for pid in snapshot.unknown_pids
            if (
                pid not in self._baseline_unknown_pids
                and _snapshot_unknown_is_ambiguous(snapshot, pid)
            )
        )
        for pid, expected in self._tracked.items():
            if pid in self._reaped or pid in self._absent_proven:
                continue
            current = snapshot.records.get(pid)
            if current is None:
                observation = _read_proc_record(pid)
                if observation.state == _PROC_UNKNOWN:
                    self._procfs_unknown.add(pid)
                elif observation.record is not None:
                    current = observation.record
            if current is not None and current.identity != expected.identity:
                self._identity_changed = True
        # Once an anchor PID or tracked descendant has been reused, do not
        # turn the new process's children into kill candidates.
        if self._identity_changed:
            return
        known = set(self._tracked)
        changed = True
        while changed:
            changed = False
            for record in snapshot.records.values():
                if record.pid in self._tracked:
                    if record.identity != self._tracked[record.pid].identity:
                        self._identity_changed = True
                    continue
                if record.ppid in known and record.pid != os.getpid():
                    self._tracked[record.pid] = record
                    pidfd = self._open_pidfd(record.pid)
                    if pidfd is not None:
                        self._pidfds[record.pid] = pidfd
                    known.add(record.pid)
                    changed = True
        # A detached child reparented to this supervisor before it was seen
        # cannot be distinguished from an unrelated concurrent child.  Refuse
        # to claim cleanup or signal it; this is the fail-closed branch.
        for record in snapshot.records.values():
            if record.ppid != os.getpid() or record.pid in self._tracked:
                continue
            if record.identity not in self._baseline_children and record.pid != self._leader_pid:
                ownership = self._proc_has_ownership_token(record.pid)
                if ownership is None:
                    self._procfs_unknown.add(record.pid)
                elif ownership:
                    self._tracked[record.pid] = record
                    pidfd = self._open_pidfd(record.pid)
                    if pidfd is not None:
                        self._pidfds[record.pid] = pidfd
                else:
                    self._unclaimed_adopted = True

    def _proc_has_ownership_token(self, pid: int) -> bool | None:
        try:
            entries = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        except (OSError, UnicodeError):
            return None
        expected = f"{_OWNERSHIP_ENV}={self._ownership_token}".encode("ascii")
        return expected in entries

    def _reap_known(self) -> None:
        for pid in tuple(self._tracked):
            if pid == self._leader_pid or pid in self._reaped:
                continue
            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                # A descendant can be visible in procfs for a short interval
                # before Linux reparents it to this subreaper.  ECHILD in
                # that interval is not evidence of a lost reap; retry while
                # retaining its exact start-time identity.
                if self._pidfd_proves_exit(pid):
                    self._absent_proven.add(pid)
                    self._procfs_unknown.discard(pid)
                continue
            except OSError as exc:
                if exc.errno == errno.ECHILD:
                    # See the ChildProcessError branch above.  The identity
                    # and procfs proof still decide whether it is live.
                    if self._pidfd_proves_exit(pid):
                        self._absent_proven.add(pid)
                        self._procfs_unknown.discard(pid)
                continue
            if waited_pid == pid:
                self._reaped.add(pid)
                self._procfs_unknown.discard(pid)
                self._close_tracked_pidfd(pid)

    def _pidfd_proves_exit(self, pid: int) -> bool:
        waitid = getattr(os, "waitid", None)
        pidfd_selector = getattr(os, "P_PIDFD", None)
        if not callable(waitid) or pidfd_selector is None:
            return False
        # Never open a new pidfd for a numeric PID after the original handle
        # was unavailable: a reused PID would then be mistaken for the
        # tracked identity.  Descendants are retained in ``_pidfds`` at the
        # first verified procfs observation; otherwise cleanup is unproven.
        fd = self._pidfds.get(pid)
        if fd is None:
            return False
        try:
            result = waitid(
                pidfd_selector,
                fd,
                # Reap a waitable descendant when waitid owns it.  A
                # non-child returns ECHILD and is handled by the pidfd poll
                # proof below without pretending that a numeric PID is safe.
                os.WEXITED | os.WNOHANG,
            )
            if result is not None and getattr(result, "si_pid", 0) == pid:
                observation = _ProcObservation(
                    _PROC_ABSENT,
                    reason="pidfd_waitid_exit",
                )
            else:
                observation = _ProcObservation(
                    _PROC_UNKNOWN,
                    reason="pidfd_waitid_pending",
                )
            if observation.state == _PROC_ABSENT:
                return True
        except (ChildProcessError, OSError, ValueError):
            # A child may have been reaped by its original parent before the
            # subreaper can call waitpid.  P_PIDFD then reports ECHILD; the
            # pidfd remains an exact, non-reusable identity and its poll HUP
            # is the exit proof.  A live non-child also reports ECHILD, so do
            # not treat that exception alone as absence.
            pass
        try:
            poller = select.poll()
            poller.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            events = poller.poll(0)
            exited = any(
                event_fd == fd
                and event_mask & (select.POLLIN | select.POLLHUP | select.POLLERR)
                for event_fd, event_mask in events
            )
            if not exited:
                return False
            # A pidfd becoming readable identifies this exact process, but a
            # visible zombie still needs a parent reap before cleanup is
            # proven.  If procfs is already gone/uncertain, the pidfd itself
            # is the stronger non-reusable exit proof.
            observation = _read_proc_record(pid)
            if observation.state == _PROC_PRESENT and observation.record is not None:
                return observation.record.state.lower() in {"x"}
            return True
        except (OSError, ValueError):
            return False

    def _close_tracked_pidfd(self, pid: int) -> None:
        fd = self._pidfds.pop(pid, None)
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _tracked_live(self) -> list[_ProcRecord]:
        live: list[_ProcRecord] = []
        for pid, expected in self._tracked.items():
            if pid in self._reaped or pid in self._absent_proven:
                continue
            observation = _read_proc_record(pid)
            if observation.state == _PROC_UNKNOWN:
                self._procfs_unknown.add(pid)
                continue
            current = observation.record
            if current is None:
                continue
            if current.identity != expected.identity:
                self._identity_changed = True
                continue
            # A zombie is still an unreaped descendant and therefore cannot
            # count as cleanup proof.  The reaper loop must observe waitpid
            # before this record disappears.
            if current.state.lower() not in {"x"}:
                live.append(current)
        return live

    def _signal_identity(self, record: _ProcRecord, signum: signal.Signals) -> bool:
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if not callable(pidfd_send_signal):
            self._signal_unproven = True
            return False
        fd = self._pidfds.get(record.pid)
        transient = False
        if fd is None:
            fd = self._open_pidfd(record.pid)
            transient = fd is not None
        if fd is None:
            self._signal_unproven = True
            return False
        try:
            observation = _read_proc_record(record.pid)
            if observation.state == _PROC_UNKNOWN:
                self._procfs_unknown.add(record.pid)
                return False
            current = observation.record
            if current is None or current.identity != record.identity:
                self._identity_changed = True
                return False
            try:
                pidfd_send_signal(fd, signum)
            except (OSError, ValueError):
                self._signal_unproven = True
                return False
            return True
        finally:
            if transient:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _signal_group(self, signum: signal.Signals) -> bool:
        if self._leader_pid is None or not self._leader_identity_matches():
            return False
        try:
            os.killpg(self._leader_pid, signum)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            self._identity_changed = True
            return False

    def _group_exists(self) -> bool:
        # killpg(0) is safe only while the unreaped, identity-anchored leader
        # still owns the group.  Once it exits, rely on exact descendants.
        if self._leader_identity_matches() and self._leader_pid is not None:
            try:
                os.killpg(self._leader_pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
        return any(record.pgrp == self._leader_pid for record in self._tracked_live())

    def _signal_all(self, signum: signal.Signals) -> None:
        # A full /proc scan is useful while budget remains, but must not turn
        # an exhausted deadline into an unbounded cleanup tail.  All known
        # identities are still checked individually below.
        if self._work_remaining() > 0:
            self._discover_descendants()
        if self._identity_changed:
            return
        if self._leader_identity is not None:
            self._signal_group(signum)
        elif self._pidfd is not None:
            pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
            if callable(pidfd_send_signal):
                try:
                    pidfd_send_signal(self._pidfd, signum)
                except (OSError, ValueError):
                    self._signal_unproven = True
            else:
                self._signal_unproven = True
        for record in self._tracked_live():
            if (
                record.pid != self._leader_pid
                and record.pgrp != self._leader_pid
                and record.state.lower() not in {"z", "x"}
            ):
                self._signal_identity(record, signum)

    def _close_selector(self) -> None:
        selector = self._selector
        self._selector = None
        if selector is None:
            return
        for key in tuple(selector.get_map().values()):
            try:
                selector.unregister(key.fileobj)
            except (KeyError, ValueError, OSError):
                pass
            try:
                key.fileobj.close()
            except (OSError, ValueError):
                pass
        selector.close()
        self._streams.clear()

    def _proof(self) -> bool:
        self._poll_leader()
        if self._work_remaining() > 0:
            self._discover_descendants()
        self._reap_known()
        if self._work_remaining() > 0:
            self._discover_descendants()
        return bool(
            self._leader_returncode is not None
            and not self._leader_reap_lost
            and not self._identity_changed
            and not self._unclaimed_adopted
            and not self._procfs_listing_unknown
            and not self._procfs_unknown
            and not self._signal_unproven
            and not self._tracked_live()
            and not self._group_exists()
        )

    def _ensure_process_handle(self) -> None:
        """Recover the handle if setup was interrupted after spawn."""

        if self.process is not None or self._leader_pid is None:
            return
        if self._leader_identity is None:
            observation = _read_proc_record(self._leader_pid)
            if observation.state == _PROC_UNKNOWN:
                self._procfs_unknown.add(self._leader_pid)
            else:
                self._leader_identity = observation.record
        if self._leader_identity is not None:
            self._tracked[self._leader_pid] = self._leader_identity
        self.process = _PosixProcess(
            self._leader_pid,
            self._poll_leader,
            self._wait_leader,
        )

    def _terminate_and_reap(self) -> bool:
        self._discover_descendants()
        now = time.monotonic()
        reserve = self._cleanup_reserve()
        if self._remaining() > reserve:
            self._signal_all(signal.SIGTERM)
            term_end = min(self._deadline - reserve, now + self.term_grace_seconds)
            self._wait_for_exit_until(term_end)
        if self._work_remaining() > 0:
            self._discover_descendants()
        self._reap_known()
        if not self._proof():
            # If the total budget is already exhausted, KILL is immediate and
            # there is no additive grace period.  A short nonblocking proof
            # pass below accounts only for syscall scheduling jitter.
            self._signal_all(signal.SIGKILL)
        while time.monotonic() < self._deadline:
            self._discover_descendants()
            self._reap_known()
            if self._proof():
                break
            self._pump(min(self._remaining(), 0.02))
        if self._work_remaining() > 0:
            self._discover_descendants()
        self._reap_known()
        proven = self._proof()
        self.cleanup_proven = proven
        self._close_selector()
        if not proven:
            raise CleanupUnprovenError(
                "child cleanup could not be proven",
                diagnostics=self._diagnostics(),
                cleanup_proven=False,
            )
        return True

    def _abort_for_timeout(self, message: str) -> None:
        try:
            self._terminate_and_reap()
        except CleanupUnprovenError as cleanup_error:
            timeout = ChildTimeoutError(
                message,
                diagnostics=self._diagnostics(),
                cleanup_proven=False,
            )
            raise cleanup_error from timeout
        raise ChildTimeoutError(
            message,
            diagnostics=self._diagnostics(),
            cleanup_proven=self.cleanup_proven,
        )

    def wait(self) -> BoundedCompletedProcess:
        process = self._require_open()
        while process.poll() is None:
            if self._work_remaining() <= 0:
                self._abort_for_timeout("child deadline expired")
            self._pump(min(self._remaining(), 0.05))
            self._discover_descendants()
            self._reap_known()
        if not self._wait_for_exit_until(self._deadline):
            self._abort_for_timeout("child output did not reach EOF before deadline")
        # The leader may have exited and reparented a detached descendant at
        # the same boundary.  Take one final ancestry snapshot before a
        # successful result, even when the work reserve is exhausted.
        self._discover_descendants()
        if not self._proof():
            self._abort_for_timeout("child descendants remained after leader exit")
        diagnostics = self._diagnostics()
        return BoundedCompletedProcess(
            args=self.args,
            returncode=int(process.returncode),
            stdout=self._stdout.text(),
            stderr=self._stderr.text(),
            diagnostics=diagnostics,
        )

    def close(self) -> None:
        """Stop/reap/prove the full tree, restoring all process state."""

        if self._closed:
            return
        failure: BaseException | None = None
        try:
            self._ensure_process_handle()
            if self.process is None and (
                self._procfs_listing_unknown
                or self._procfs_unknown
                or self._unclaimed_adopted
            ):
                failure = CleanupUnprovenError(
                    "spawned child identity could not be recovered",
                    diagnostics=self._diagnostics(),
                    cleanup_proven=False,
                )
            elif self.process is None and self._spawn_attempted:
                # A failed spawn with a stable, complete procfs recovery pass
                # proves that no child identity was created.  Preserve that
                # distinction from an ambiguous recovery above.
                self.cleanup_proven = True
            elif self.process is not None and not self._proof():
                self._terminate_and_reap()
            elif self.process is not None:
                self.cleanup_proven = True
                self._close_selector()
        except BaseException as exc:
            failure = exc
            self.cleanup_proven = False
        finally:
            if self._stdin is not None:
                try:
                    self._stdin.close()  # type: ignore[union-attr]
                except (OSError, ValueError):
                    pass
                self._stdin = None
            if self._parent_stdin is not None:
                try:
                    self._parent_stdin.close()
                except (OSError, ValueError):
                    pass
                self._parent_stdin = None
            for fd in tuple(self._parent_fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
                self._parent_fds.discard(fd)
            if self._pidfd is not None:
                try:
                    os.close(self._pidfd)
                except OSError:
                    pass
                self._pidfd = None
            for pid in tuple(self._pidfds):
                self._close_tracked_pidfd(pid)
            try:
                _restore_subreaper(self._subreaper_previous)
            except BaseException as restore_error:
                if failure is None:
                    failure = restore_error
            self._subreaper_previous = None
            if self._lock_held:
                self._lock_held = False
                global _LIFECYCLE_OWNER_THREAD
                _LIFECYCLE_OWNER_THREAD = None
                _LIFECYCLE_LOCK.release()
            self._closed = True
        if failure is not None:
            if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                raise failure
            if isinstance(failure, CleanupUnprovenError):
                raise failure
            raise CleanupUnprovenError(
                "child cleanup failed before proof",
                diagnostics=self._diagnostics(),
                cleanup_proven=False,
            ) from failure

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except BaseException as cleanup_error:
            if exc_value is not None:
                # Do not mask a primary assertion/cancellation, and do not
                # expose arbitrary child/cleanup exception text.
                exc_value.add_note("subprocess cleanup did not complete")
            else:
                raise cleanup_error
        return False


def run_bounded(
    args: Sequence[str],
    *,
    deadline_seconds: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdin_mode: str = "devnull",
    capture_output: bool = False,
    diagnostic_bytes: int = DEFAULT_DIAGNOSTIC_BYTES,
) -> BoundedCompletedProcess:
    """Run one child with finite I/O, tree cleanup and bounded output."""

    with BoundedChild(
        args,
        deadline_seconds=deadline_seconds,
        cwd=cwd,
        env=env,
        stdin_mode=stdin_mode,
        diagnostic_bytes=diagnostic_bytes,
    ) as child:
        result = child.wait()
        if not capture_output:
            return BoundedCompletedProcess(
                args=result.args,
                returncode=result.returncode,
                stdout="",
                stderr="",
                diagnostics=result.diagnostics,
            )
        return result
