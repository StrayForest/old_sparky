#!/usr/bin/env python3
"""Killable runtime boundary for the external load generator.

The measured generator is untrusted with respect to time: DNS, a socket,
response-body reads, a retry sleep, or a worker future can all stop making
progress.  The supervisor therefore owns two absolute monotonic deadlines and
executes the whole generator in a mandatory Linux PID namespace.  A thread
timeout is not used as a kill boundary; it cannot reclaim a blocked syscall.

The worker-facing :class:`LoadRuntimeBudget` is deliberately small.  It is
used to cap ordinary I/O and to stop before a new phase/retry, while the
parent-side :func:`run_supervised` selects one wall deadline from the scenario
and runner ceilings, reserves teardown/report time inside it, and remains the
final authority that can TERM then KILL the namespace wrapper, wait for PID 1
and reap it.  There is no process-group containment fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


RUNTIME_PROTOCOL_VERSION = 1
WORKER_REPORT_SCHEMA = 2
PID_NAMESPACE_ISOLATION = "pid_namespace"
SYSTEM_SUDO = "/usr/bin/sudo"
SYSTEM_SETPRIV = "/usr/bin/setpriv"
SYSTEM_UNSHARE = "/usr/bin/unshare"
SYSTEM_PYTHON = "/usr/bin/python3"
NAMESPACE_READY = b"READY\n"
NAMESPACE_ACK = b"ACK\n"
NAMESPACE_PROBE_OK = b"PROBE_OK\n"
DEFAULT_TERM_GRACE_SECONDS = 1.0
DEFAULT_POLL_SECONDS = 0.02
DEFAULT_NAMESPACE_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_NAMESPACE_CAPTURE_TIMEOUT_SECONDS = 5.0
MAX_CHILD_REPORT_BYTES = 16 * 1024 * 1024
MAX_REASON_LENGTH = 96
_SAFE_REASON = frozenset({"none", "max_duration_seconds", "max_runner_minutes"})


def _deadline_reason_at(
    now: float,
    *,
    scenario_deadline: float,
    runner_deadline: float,
    effective_deadline: float | None = None,
) -> str | None:
    """Return the primary absolute-budget reason at one monotonic instant.

    ``effective_deadline`` is the point at which the supervisor must begin
    bounded teardown so that teardown/report publication still fit inside the
    original wall deadline.  It never changes which authored budget owns the
    diagnosis: the earlier scenario/runner deadline remains primary.
    """

    earliest = min(scenario_deadline, runner_deadline)
    if effective_deadline is not None:
        if now < effective_deadline:
            return None
    elif now < earliest:
        return None
    if scenario_deadline <= runner_deadline:
        return "max_duration_seconds"
    return "max_runner_minutes"


class NamespaceCapabilityError(RuntimeError):
    """The required Linux PID-namespace containment cannot be proven."""


class NamespaceIntegrityError(RuntimeError):
    """The trusted namespace bootstrap observed an identity/race violation."""


class LoadRuntimeBudgetExceeded(RuntimeError):
    """The worker reached one of its absolute runtime deadlines."""

    def __init__(
        self,
        *,
        phase: str,
        operation: str,
        elapsed_seconds: float,
        max_duration_seconds: float,
        reason: str,
        partial_results: list[object] | None = None,
    ) -> None:
        self.phase = _safe_text(phase, fallback="unknown")
        self.operation = _safe_text(operation, fallback="unknown")
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        self.max_duration_seconds = float(max_duration_seconds)
        self.reason = reason if reason in _SAFE_REASON else "max_duration_seconds"
        self.partial_results = list(partial_results or [])
        super().__init__(
            f"load runtime budget exceeded during {self.phase}/{self.operation}: "
            f"reason={self.reason} elapsed={self.elapsed_seconds:.6f}s"
        )


def _safe_text(value: Any, *, fallback: str) -> str:
    if isinstance(value, str):
        value = value.strip()
        if value:
            return value[:MAX_REASON_LENGTH]
    return fallback


def _finite_positive(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite and positive")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite and positive") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{field} must be finite and positive")
    return numeric


def _read_process_starttime(pid: int) -> int:
    """Read Linux ``/proc/<pid>/stat`` starttime without trusting a PID alone."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise NamespaceIntegrityError("process PID is invalid")
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise NamespaceIntegrityError("process starttime is unavailable") from exc
    # ``comm`` may contain spaces and closing parentheses.  The final closing
    # parenthesis is therefore the only safe delimiter before field 3.
    closing = line.rfind(")")
    if closing < 0:
        raise NamespaceIntegrityError("process stat record is malformed")
    fields = line[closing + 2 :].split()
    # ``fields[0]`` is field 3 (state); starttime is field 22.
    if len(fields) <= 19:
        raise NamespaceIntegrityError("process stat record has no starttime")
    try:
        return int(fields[19])
    except (TypeError, ValueError) as exc:
        raise NamespaceIntegrityError("process starttime is malformed") from exc


def _read_process_state(pid: int) -> str | None:
    """Return the procfs state letter for a captured process identity.

    A process that has exited but has not yet been reaped still has a
    ``/proc`` record and its pidfd is readable.  Treating that record as live
    is a false containment failure; the caller must distinguish ``Z`` from a
    running process and reap it when it is our child.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    closing = line.rfind(")")
    if closing < 0:
        return None
    fields = line[closing + 2 :].split()
    if not fields:
        return None
    state = fields[0]
    return state if len(state) == 1 else None


def _pidfd_is_live(pidfd: int) -> bool:
    """Return whether an inherited pidfd still identifies a live parent."""

    if isinstance(pidfd, bool) or not isinstance(pidfd, int) or pidfd < 0:
        return False
    try:
        os.fstat(pidfd)
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        events = poller.poll(0)
    except (OSError, ValueError):
        return False
    return not any(mask & (select.POLLIN | select.POLLHUP | select.POLLERR) for _, mask in events)


def _pidfd_is_valid(pidfd: int) -> bool:
    """Return whether a descriptor still refers to a pidfd."""

    if isinstance(pidfd, bool) or not isinstance(pidfd, int) or pidfd < 0:
        return False
    try:
        os.fstat(pidfd)
    except (OSError, ValueError):
        return False
    return True


def _verify_expected_parent(
    *,
    expected_parent_pid: int,
    expected_parent_starttime: int | None,
    parent_pidfd: int,
    namespace_pid1: bool = False,
) -> None:
    """Perform the bootstrap's immediate parent-death and PID-reuse checks."""

    actual_parent = os.getppid()
    if namespace_pid1:
        if os.getpid() != 1 or actual_parent != 0:
            raise NamespaceIntegrityError("namespace worker is not PID 1")
    elif actual_parent != expected_parent_pid:
        raise NamespaceIntegrityError("bootstrap parent identity changed")
    if not _pidfd_is_live(parent_pidfd):
        raise NamespaceIntegrityError("supervisor pidfd is closed")
    if not namespace_pid1:
        if expected_parent_starttime is None:
            raise NamespaceIntegrityError("supervisor starttime is missing")
        current_starttime = _read_process_starttime(expected_parent_pid)
        if current_starttime != expected_parent_starttime:
            raise NamespaceIntegrityError("supervisor PID was reused")


def _set_parent_death_signal() -> None:
    """Require SIGKILL-on-parent-death for the trusted namespace helper."""

    if not sys.platform.startswith("linux"):
        raise NamespaceCapabilityError("PID namespace isolation requires Linux")
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.prctl(1, signal.SIGKILL, 0, 0, 0)
    except (AttributeError, OSError) as exc:
        raise NamespaceCapabilityError("PR_SET_PDEATHSIG is unavailable") from exc
    if result != 0:
        error_number = ctypes.get_errno()
        raise NamespaceCapabilityError(
            f"PR_SET_PDEATHSIG failed: {error_number or 'unknown'}"
        )


def _set_child_subreaper() -> int | None:
    """Enable Linux subreaper behavior as defense-in-depth only."""

    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int(0)
        if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
            return None
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            return None
        return int(previous.value)
    except (AttributeError, OSError):
        return None


def _restore_child_subreaper(previous: int | None) -> None:
    if previous is None or not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(36, int(previous), 0, 0, 0)
    except (AttributeError, OSError):
        return


def _system_binary(path: str) -> str | None:
    candidate = Path(path)
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return path
    except OSError:
        pass
    return None


def _runner_identity() -> tuple[int, int]:
    """Return the real runner identity used by the inner privilege drop.

    A root caller is rejected deliberately.  The production contract is a
    non-root GitHub runner invoking a narrowly allowed passwordless sudo
    command; treating root as an acceptable test identity would make the
    capability probe prove the wrong containment boundary.
    """

    uid = os.getuid()
    gid = os.getgid()
    if uid == 0 or gid == 0:
        raise NamespaceCapabilityError("runner_must_be_nonroot")
    return uid, gid


def _read_proc_status() -> dict[str, str]:
    try:
        lines = Path("/proc/self/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise NamespaceIntegrityError("worker proc status is unavailable") from exc
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator:
            values[key] = value.strip()
    return values


def _assert_namespace_worker_identity(expected_uid: int, expected_gid: int) -> None:
    """Machine-check every identity property required before a load starts."""

    try:
        expected_uid = int(expected_uid)
        expected_gid = int(expected_gid)
    except (TypeError, ValueError) as exc:
        raise NamespaceIntegrityError("expected runner identity is malformed") from exc
    if expected_uid <= 0 or expected_gid < 0:
        raise NamespaceIntegrityError("expected runner identity is unsafe")
    if os.getpid() != 1 or os.getppid() != 0:
        raise NamespaceIntegrityError("namespace worker is not PID 1")
    if any(
        value != expected_uid
        for value in (os.getuid(), os.geteuid(), *os.getresuid())
    ):
        raise NamespaceIntegrityError("namespace worker UID was not dropped exactly")
    if any(
        value != expected_gid
        for value in (os.getgid(), os.getegid(), *os.getresgid())
    ):
        raise NamespaceIntegrityError("namespace worker GID was not dropped exactly")
    if os.getgroups() != []:
        raise NamespaceIntegrityError("namespace worker supplementary groups remain")
    status = _read_proc_status()
    for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        value = status.get(field)
        if value is None:
            raise NamespaceIntegrityError(f"namespace worker capability field missing: {field}")
        try:
            zero = int(value, 16) == 0
        except ValueError as exc:
            raise NamespaceIntegrityError(f"namespace worker capability field malformed: {field}") from exc
        if not zero:
            raise NamespaceIntegrityError(f"namespace worker capability field is non-zero: {field}")
    if status.get("NoNewPrivs") != "1":
        raise NamespaceIntegrityError("namespace worker NoNewPrivs is not 1")


def _probe_python_source(expected_uid: int, expected_gid: int) -> str:
    """Return a closed, dependency-free probe executed after the root drop."""

    # Keep this source independent of the checkout.  The only program reached
    # while still privileged is /usr/bin/unshare; this probe is executed only
    # after the inner /usr/bin/setpriv has applied the non-root contract.
    return (
        "import os,sys\n"
        f"expected_uid={int(expected_uid)}\n"
        f"expected_gid={int(expected_gid)}\n"
        "if os.getpid()!=1 or os.getppid()!=0: raise SystemExit(21)\n"
        "if any(v!=expected_uid for v in (os.getuid(),os.geteuid(),*os.getresuid())): raise SystemExit(22)\n"
        "if any(v!=expected_gid for v in (os.getgid(),os.getegid(),*os.getresgid())): raise SystemExit(23)\n"
        "if os.getgroups()!=[]: raise SystemExit(24)\n"
        "status={}\n"
        "for line in open('/proc/self/status',encoding='ascii'):\n"
        " key,sep,value=line.partition(':')\n"
        " if sep: status[key]=value.strip()\n"
        "for field in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'):\n"
        " if int(status.get(field,'-1'),16)!=0: raise SystemExit(25)\n"
        "if status.get('NoNewPrivs')!='1': raise SystemExit(26)\n"
        f"os.write(1,{NAMESPACE_READY!r})\n"
        f"if os.read(0,{len(NAMESPACE_ACK)})!={NAMESPACE_ACK!r}: raise SystemExit(27)\n"
        f"os.write(1,{NAMESPACE_PROBE_OK!r})\n"
    )


def _namespace_command(
    *,
    worker_command: Sequence[str],
    config_path: Path | None,
    helper_path: Path | None,
    expected_uid: int,
    expected_gid: int,
    probe: bool = False,
) -> list[str]:
    """Build the only permitted root-to-worker execution chain."""

    if probe:
        if config_path is not None or helper_path is not None:
            raise ValueError("probe command cannot include checkout helper config")
        inner_command = [SYSTEM_PYTHON, "-c", _probe_python_source(expected_uid, expected_gid)]
    else:
        if config_path is None or helper_path is None:
            raise ValueError("worker command requires namespace helper config")
        inner_command = [
            str(worker_command[0]),
            str(helper_path),
            "--mode",
            "namespace-worker",
            "--config",
            str(config_path),
        ]
    return [
        SYSTEM_SUDO,
        "-n",
        SYSTEM_SETPRIV,
        "--pdeathsig",
        "SIGKILL",
        "--",
        SYSTEM_UNSHARE,
        "--pid",
        "--fork",
        "--mount-proc",
        "--kill-child=SIGKILL",
        "--",
        SYSTEM_SETPRIV,
        f"--reuid={expected_uid}",
        f"--regid={expected_gid}",
        "--clear-groups",
        "--no-new-privs",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--pdeathsig=SIGKILL",
        "--",
        *inner_command,
    ]


def probe_pid_namespace_capability(
    *,
    timeout_seconds: float = DEFAULT_NAMESPACE_PROBE_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Probe the exact mandatory unshare contract without a load fallback."""

    try:
        timeout = _finite_positive(timeout_seconds, field="namespace_probe_timeout")
    except ValueError as exc:
        return {"available": False, "reason": str(exc)}
    if not sys.platform.startswith("linux"):
        return {"available": False, "reason": "linux_required"}
    if not hasattr(os, "pidfd_open"):
        return {"available": False, "reason": "pidfd_unavailable"}
    try:
        uid, gid = _runner_identity()
    except NamespaceCapabilityError as exc:
        return {"available": False, "reason": str(exc)}
    missing = [
        path
        for path in (SYSTEM_SUDO, SYSTEM_SETPRIV, SYSTEM_UNSHARE, SYSTEM_PYTHON)
        if _system_binary(path) is None
    ]
    if missing:
        return {"available": False, "reason": "required_system_binary_missing", "missing": missing}
    try:
        version = subprocess.run(
            [SYSTEM_SUDO, "-n", SYSTEM_SETPRIV, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            close_fds=True,
            timeout=min(timeout, 2.0),
            text=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"sudo_capability_{type(exc).__name__}"}
    if version.returncode != 0:
        return {"available": False, "reason": "sudo_setpriv_not_passwordless"}
    command = _namespace_command(
        worker_command=(),
        config_path=None,
        helper_path=None,
        expected_uid=uid,
        expected_gid=gid,
        probe=True,
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            preexec_fn=_set_parent_death_signal,
        )
        if process.stdout is None or process.stdin is None:
            return {"available": False, "reason": "probe_stdio_unavailable"}
        ready_deadline = time.monotonic() + timeout
        if not _await_namespace_ready(process.stdout, process, deadline=ready_deadline):
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=max(1.0, timeout))
            except subprocess.TimeoutExpired:
                return {"available": False, "reason": "probe_timeout"}
            return {"available": False, "reason": "namespace_handshake_failed"}
        try:
            process.stdin.write(NAMESPACE_ACK)
            process.stdin.flush()
            process.stdin.close()
            # ``Popen.communicate`` otherwise attempts to flush an already
            # closed stream on Python 3.12.
            process.stdin = None  # type: ignore[assignment]
        except OSError:
            return {"available": False, "reason": "namespace_ack_failed"}
        remaining = max(0.1, ready_deadline - time.monotonic())
        try:
            stdout, _stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=max(1.0, timeout))
            return {"available": False, "reason": "probe_timeout"}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {
            "available": False,
            "reason": _safe_text(type(exc).__name__, fallback="probe_failed"),
        }
    finally:
        # Every failed preflight must reclaim the exact probe chain before it
        # returns a closed negative result.  The hosted watchdog owns the
        # longer-lived load chain; this direct system-only probe still needs a
        # bounded local cleanup on handshake/FD errors.
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=max(1.0, timeout))
            except subprocess.TimeoutExpired:
                pass
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
    if process is None or process.returncode != 0 or not stdout.endswith(NAMESPACE_PROBE_OK):
        return {
            "available": False,
            "reason": "namespace_probe_failed",
            "returncode": None if process is None else process.returncode,
        }
    return {
        "available": True,
        "sudo": SYSTEM_SUDO,
        "setpriv": SYSTEM_SETPRIV,
        "unshare": SYSTEM_UNSHARE,
        "runner_uid": uid,
        "runner_gid": gid,
        "protocol": "stdio",
    }


def require_pid_namespace_capability() -> str:
    result = probe_pid_namespace_capability()
    if result.get("available") is not True:
        raise NamespaceCapabilityError(str(result.get("reason") or "probe_failed"))
    unshare = result.get("unshare")
    if unshare != SYSTEM_UNSHARE:
        raise NamespaceCapabilityError("unshare path is missing after probe")
    return SYSTEM_UNSHARE


@dataclass(slots=True)
class LoadRuntimeBudget:
    """One monotonic budget shared by every phase, retry and I/O boundary."""

    max_duration_seconds: float
    max_runner_minutes: float | None = None
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    started_at_monotonic: float | None = None
    runner_started_at_monotonic: float | None = None
    scenario_deadline_monotonic: float = field(init=False)
    runner_deadline_monotonic: float | None = field(init=False)

    def __post_init__(self) -> None:
        duration = _finite_positive(
            self.max_duration_seconds,
            field="max_duration_seconds",
        )
        runner_minutes = (
            None
            if self.max_runner_minutes is None
            else _finite_positive(
                self.max_runner_minutes,
                field="max_runner_minutes",
            )
        )
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("clock must return a finite number")
        scenario_start = (
            now
            if self.started_at_monotonic is None
            else float(self.started_at_monotonic)
        )
        runner_start = (
            scenario_start
            if self.runner_started_at_monotonic is None
            else float(self.runner_started_at_monotonic)
        )
        if not math.isfinite(scenario_start) or not math.isfinite(runner_start):
            raise ValueError("budget start times must be finite")
        if runner_start > scenario_start:
            raise ValueError("runner budget cannot start after scenario budget")
        self.max_duration_seconds = duration
        self.max_runner_minutes = runner_minutes
        self.started_at_monotonic = scenario_start
        self.runner_started_at_monotonic = runner_start
        self.scenario_deadline_monotonic = scenario_start + duration
        self.runner_deadline_monotonic = (
            None if runner_minutes is None else runner_start + runner_minutes * 60.0
        )

    def now(self) -> float:
        current = float(self.clock())
        if not math.isfinite(current):
            raise RuntimeError("monotonic clock returned a non-finite value")
        return current

    def elapsed_seconds(self) -> float:
        assert self.started_at_monotonic is not None
        return max(0.0, self.now() - self.started_at_monotonic)

    def runner_elapsed_seconds(self) -> float:
        assert self.runner_started_at_monotonic is not None
        return max(0.0, self.now() - self.runner_started_at_monotonic)

    def remaining_seconds(self) -> float:
        """Remaining scenario time, retained for worker compatibility."""

        return self.scenario_deadline_monotonic - self.now()

    def remaining_runner_seconds(self) -> float | None:
        if self.runner_deadline_monotonic is None:
            return None
        return self.runner_deadline_monotonic - self.now()

    def exceeded_reason(self) -> str | None:
        if self.scenario_deadline_monotonic <= self.now():
            return "max_duration_seconds"
        if (
            self.runner_deadline_monotonic is not None
            and self.runner_deadline_monotonic <= self.now()
        ):
            return "max_runner_minutes"
        return None

    def check(self, phase: str, *, operation: str = "check") -> None:
        reason = self.exceeded_reason()
        if reason is None:
            return
        raise LoadRuntimeBudgetExceeded(
            phase=phase,
            operation=operation,
            elapsed_seconds=self.elapsed_seconds(),
            max_duration_seconds=self.max_duration_seconds,
            reason=reason,
        )

    def bound_timeout(
        self,
        requested_seconds: float,
        phase: str,
        *,
        operation: str = "io",
    ) -> float:
        requested = _finite_positive(requested_seconds, field="timeout")
        self.check(phase, operation=operation)
        remaining = self.remaining_seconds()
        runner_remaining = self.remaining_runner_seconds()
        if runner_remaining is not None:
            remaining = min(remaining, runner_remaining)
        if remaining <= 0:
            self.check(phase, operation=operation)
        # A positive value is needed by urllib/http.client, but never invent a
        # floor that extends the absolute deadline.
        return min(requested, max(1e-6, remaining))

    def sleep(self, seconds: float, phase: str, *, operation: str = "wait") -> None:
        delay = float(seconds)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("sleep duration must be finite and non-negative")
        self.check(phase, operation=operation)
        remaining = self.remaining_seconds()
        runner_remaining = self.remaining_runner_seconds()
        if runner_remaining is not None:
            remaining = min(remaining, runner_remaining)
        if delay:
            self.sleeper(min(delay, max(0.0, remaining)))
        self.check(phase, operation=operation)

    def runner_budget_status(
        self,
        *,
        phase: str = "complete",
        reason: str | None = None,
    ) -> dict[str, object]:
        elapsed = self.elapsed_seconds()
        runner_elapsed = self.runner_elapsed_seconds()
        within_duration = elapsed < self.max_duration_seconds
        within_runner = self.max_runner_minutes is None or (
            runner_elapsed / 60.0 < self.max_runner_minutes
        )
        selected_reason = reason
        if selected_reason not in _SAFE_REASON:
            if not within_duration:
                selected_reason = "max_duration_seconds"
            elif not within_runner:
                selected_reason = "max_runner_minutes"
            else:
                selected_reason = "none"
        return {
            "max_duration_seconds": _number_for_report(self.max_duration_seconds),
            "max_runner_minutes": (
                None
                if self.max_runner_minutes is None
                else _number_for_report(self.max_runner_minutes)
            ),
            "actual_elapsed_seconds": _number_for_report(elapsed),
            "actual_runner_seconds": _number_for_report(runner_elapsed),
            "actual_runner_minutes": _number_for_report(runner_elapsed / 60.0),
            "within_duration_budget": within_duration,
            "within_runner_budget": within_runner,
            "budget_exceeded": not (within_duration and within_runner),
            "phase": _safe_text(phase, fallback="unknown"),
            "reason": selected_reason,
        }


def _number_for_report(value: float) -> int | float:
    rounded = round(float(value), 6)
    return int(rounded) if rounded.is_integer() else rounded


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    """Closed parent-side outcome of one worker process."""

    returncode: int | None
    report: dict[str, Any]
    worker_started: bool
    worker_exited: bool
    killed: bool
    signal: int | None
    reason: str
    partial_work: bool
    inflight_unknown: bool
    descendants_reaped: bool
    isolation: str
    namespace_closed: bool
    namespace_init_pid: int | None


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_closed_report(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "missing_or_symlink_report"
        size = path.stat().st_size
        if size <= 0 or size > MAX_CHILD_REPORT_BYTES:
            return None, "report_size_invalid"
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "report_malformed"
    if not isinstance(payload, dict):
        return None, "report_not_object"
    if payload.get("worker_report_schema") != WORKER_REPORT_SCHEMA:
        return None, "report_schema_invalid"
    if payload.get("report_complete") is not True:
        return None, "report_not_complete"
    if type(payload.get("namespace_closed")) is not bool:
        return None, "namespace_closed_missing_or_invalid"
    return payload, None


def _closed_failure_report(
    *,
    reason: str,
    signal_number: int | None,
    returncode: int | None,
    partial_work: bool,
    inflight_unknown: bool,
    report_error: str | None = None,
    runtime_budget: Mapping[str, Any] | None = None,
    descendants_reaped: bool = True,
    namespace_closed: bool = False,
) -> dict[str, Any]:
    # A missing child report is evidence, not a replacement for the
    # supervisor's primary timeout/containment reason.  ``namespace_closed``
    # remains an independent hard gate in the decision below.
    decision = (
        "LOAD ISOLATION UNAVAILABLE"
        if reason in {"namespace_unavailable", "namespace_start_failed"}
        else "LOAD NAMESPACE NOT CLOSED"
        if not namespace_closed
        else "LOAD RUNTIME BUDGET EXCEEDED"
        if reason in {"max_duration_seconds", "max_runner_minutes"}
        else "LOAD RUN FAILED"
    )
    payload: dict[str, Any] = {
        "worker_report_schema": WORKER_REPORT_SCHEMA,
        "report_complete": True,
        "passed": False,
        "authoritative": False,
        "dispatchable": False,
        "acceptance": {"passed": False, "decision": decision, "contract_ok": False},
        "isolation": PID_NAMESPACE_ISOLATION,
        "namespace_closed": bool(namespace_closed),
        "runtime_supervisor": {
            "protocol": RUNTIME_PROTOCOL_VERSION,
            "isolation": PID_NAMESPACE_ISOLATION,
            "namespace_closed": bool(namespace_closed),
            "reason": reason,
            "containment_reason": None if namespace_closed else "namespace_unclosed",
            "signal": signal_number,
            "returncode": returncode,
            "partial_work": bool(partial_work),
            "inflight_unknown": bool(inflight_unknown),
            "descendants_reaped": bool(descendants_reaped),
            "report_error": report_error,
        },
        "partial_work": bool(partial_work),
        "inflight_unknown": bool(inflight_unknown),
    }
    if runtime_budget is not None:
        payload["runtime_budget"] = dict(runtime_budget)
    return payload


def _read_process_children(pid: int) -> tuple[int, ...]:
    """Read direct children of the unshare wrapper in the outer namespace."""

    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return ()
    children: list[int] = []
    for value in raw.split():
        try:
            child_pid = int(value)
        except ValueError:
            continue
        if child_pid > 0:
            children.append(child_pid)
    return tuple(children)


def _await_namespace_ready(
    stdout: Any,
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
) -> bool:
    """Read exactly ``READY`` from stdio without consuming the post-ACK data."""

    descriptor = stdout.fileno()
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    received = bytearray()
    while len(received) < len(NAMESPACE_READY):
        if process.poll() is not None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        events = poller.poll(max(1, int(min(remaining, 0.1) * 1000)))
        if not events:
            continue
        for _fd, mask in events:
            if mask & select.POLLIN:
                try:
                    chunk = os.read(descriptor, 1)
                except OSError:
                    return False
                if not chunk:
                    return False
                received.extend(chunk)
                if not NAMESPACE_READY.startswith(received):
                    return False
            elif mask & (select.POLLHUP | select.POLLERR):
                return False
    return bytes(received) == NAMESPACE_READY


def _read_process_descendants(pid: int) -> tuple[int, ...]:
    """Return a bounded snapshot of the complete sudo/setpriv/unshare chain."""

    pending = [pid]
    seen: set[int] = set()
    descendants: list[int] = []
    while pending and len(seen) < 256:
        current = pending.pop()
        if current in seen or current <= 0:
            continue
        seen.add(current)
        if current != pid:
            descendants.append(current)
        children = _read_process_children(current)
        pending.extend(child for child in children if child not in seen)
    return tuple(descendants)


def _is_namespace_pid1(pid: int) -> bool:
    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in lines:
        if line.startswith("NSpid:"):
            values = line.split()[1:]
            return len(values) >= 2 and values[-1] == "1"
    return False


def _capture_namespace_pid(wrapper_pid: int) -> int | None:
    for candidate in _read_process_descendants(wrapper_pid):
        if _is_namespace_pid1(candidate):
            return candidate
    return None


def _captured_chain_closed(identities: Mapping[int, tuple[int, int]]) -> bool:
    """Close the PID-reuse gap for every captured sudo-chain process.

    A start-time read alone cannot distinguish a transient procfs read failure
    from a live process.  Each captured wrapper therefore also carries its
    pidfd: a non-live pidfd proves that the original process exited, while a
    still-matching start time proves that a zombie/reused numeric PID is not
    yet safe to ignore.
    """

    for pid, (starttime, pidfd) in identities.items():
        if not _pidfd_is_valid(pidfd) or _pidfd_is_live(pidfd):
            return False
        try:
            if _read_process_starttime(pid) == starttime:
                return False
        except NamespaceIntegrityError:
            # The pidfd pins the original process identity and already proved
            # its exit; procfs disappearance is the expected post-reap state.
            continue
    return True


def _namespace_closed(
    process: subprocess.Popen[bytes],
    *,
    pidfd: int | None,
    starttime: int | None,
    namespace_pid: int | None = None,
    namespace_pidfd: int | None = None,
    namespace_starttime: int | None = None,
    namespace_reaped: bool = False,
    wrapper_chain_identities: Mapping[int, tuple[int, int]] | None = None,
) -> bool:
    """Prove the unshare wrapper and its PID namespace have torn down."""

    if (
        process.poll() is None
        or pidfd is None
        or starttime is None
        or namespace_pid is None
        or namespace_pidfd is None
        or namespace_starttime is None
    ):
        return False
    if not _pidfd_is_valid(pidfd) or not _pidfd_is_valid(namespace_pidfd):
        return False
    if _pidfd_is_live(pidfd) or _pidfd_is_live(namespace_pidfd):
        return False
    if wrapper_chain_identities is not None and not _captured_chain_closed(wrapper_chain_identities):
        return False
    try:
        current_starttime = _read_process_starttime(process.pid)
    except NamespaceIntegrityError:
        current_starttime = None
    namespace_state = _read_process_state(namespace_pid)
    try:
        namespace_current_starttime = _read_process_starttime(namespace_pid)
    except NamespaceIntegrityError:
        namespace_current_starttime = None
    # A reused PID is not the old namespace wrapper.  The pidfd event above
    # proves the captured process exited; starttime closes the PID-reuse gap.
    wrapper_gone = current_starttime != starttime
    # A namespace PID 1 zombie is dead but not yet contained: it still owns a
    # procfs record until this supervisor reaps it.  Do not claim closure from
    # pidfd readability alone; the caller must remove the zombie with
    # waitpid(2), after which the captured start-time record disappears.
    namespace_gone = namespace_reaped or namespace_current_starttime != namespace_starttime
    namespace_state_closed = namespace_reaped or namespace_state != "Z" or (
        namespace_current_starttime is None and not _pidfd_is_live(namespace_pidfd)
    )
    return wrapper_gone and namespace_gone and namespace_state_closed


def _reap_namespace_init(
    namespace_pid: int | None,
    *,
    namespace_starttime: int | None,
    namespace_pidfd: int | None,
    timeout_seconds: float,
    poll_seconds: float,
    deadline: float | None = None,
) -> bool:
    """Reap the namespace init after the unshare wrapper has exited.

    ``unshare --kill-child`` kills the namespace PID 1, but util-linux can
    leave that process visible as a zombie until its parent is reaped.  The
    supervisor enables child-subreaper mode, so the namespace init becomes
    our child when the wrapper exits.  ``waitpid`` is the only operation that
    removes that zombie; a dead pidfd without a successful reap is not enough
    to claim a closed containment boundary.
    """

    if (
        namespace_pid is None
        or namespace_starttime is None
        or namespace_pidfd is None
    ):
        return False
    if not _pidfd_is_valid(namespace_pidfd):
        return False
    phase_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        phase_deadline = min(phase_deadline, deadline)
    while True:
        if time.monotonic() >= phase_deadline:
            return False
        state = _read_process_state(namespace_pid)
        if state is None:
            return not _pidfd_is_live(namespace_pidfd)
        try:
            current_starttime = _read_process_starttime(namespace_pid)
        except NamespaceIntegrityError:
            # The process may have been reaped between the state and stat
            # reads.  Only accept that race when the captured pidfd also
            # proves exit; a live process with an unreadable identity still
            # fails closed.
            return not _pidfd_is_live(namespace_pidfd)
        if current_starttime != namespace_starttime:
            # The captured process is gone and the numeric PID was reused;
            # the pidfd still refers only to the original process.
            return True
        if state == "Z":
            try:
                waited_pid, _status = os.waitpid(namespace_pid, os.WNOHANG)
            except ChildProcessError:
                # This is not our child (for example, the wrapper has not
                # finished reparenting it yet).  Keep polling, but never
                # silently accept an unreapable zombie.
                waited_pid = 0
            except OSError as exc:
                waited_pid = 0 if exc.errno == errno.EINTR else -1
            if waited_pid == namespace_pid:
                return True
            if waited_pid < 0:
                return False
        elif not _pidfd_is_live(namespace_pidfd):
            # pidfd has observed exit, but procfs can briefly retain a
            # non-zombie state (for example, an uninterruptible task while
            # the kernel completes teardown).  Keep polling for the
            # waitable zombie; the bounded deadline still fails closed if it
            # never becomes reapable.
            pass
        if time.monotonic() >= phase_deadline:
            return False
        time.sleep(min(poll_seconds, max(0.0, phase_deadline - time.monotonic())))


def _reap_captured_chain(
    identities: Mapping[int, tuple[int, int]],
    *,
    timeout_seconds: float,
    poll_seconds: float,
    deadline: float | None = None,
) -> bool:
    """Reap wrapper zombies adopted by the supervisor subreaper.

    A short TERM grace can race the non-root watchdog's own waitpid.  Once
    that watchdog exits, its sudo/setpriv/unshare child may briefly remain a
    zombie under this supervisor.  Reap only captured identities whose
    start-time still matches; never use a numeric PID after reuse.
    """

    if not identities:
        return True
    phase_deadline = time.monotonic() + timeout_seconds
    if deadline is not None:
        phase_deadline = min(phase_deadline, deadline)
    while True:
        if time.monotonic() >= phase_deadline:
            return False
        pending = False
        for pid, (starttime, pidfd) in identities.items():
            if not _pidfd_is_valid(pidfd) or _pidfd_is_live(pidfd):
                pending = True
                continue
            try:
                current_starttime = _read_process_starttime(pid)
            except NamespaceIntegrityError:
                continue
            if current_starttime != starttime:
                continue
            state = _read_process_state(pid)
            if state == "Z":
                try:
                    waited_pid, _status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pending = True
                    continue
                except OSError as exc:
                    if exc.errno == errno.EINTR:
                        pending = True
                        continue
                    return False
                if waited_pid == pid:
                    continue
                pending = True
                continue
            pending = True
        if not pending:
            return True
        if time.monotonic() >= phase_deadline:
            return False
        time.sleep(min(poll_seconds, max(0.0, phase_deadline - time.monotonic())))


def _reap_after_signal(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
    poll_seconds: float,
    deadline: float | None = None,
) -> tuple[int | None, bool]:
    phase_deadline = time.monotonic() + grace_seconds
    if deadline is not None:
        phase_deadline = min(phase_deadline, deadline)
    result: int | None = process.poll()
    while time.monotonic() < phase_deadline:
        result = process.poll()
        if result is not None:
            return result, False
        time.sleep(min(poll_seconds, max(0.0, phase_deadline - time.monotonic())))
    killed = False
    if process.poll() is None:
        try:
            process.kill()
            killed = True
        except ProcessLookupError:
            pass
    try:
        if deadline is None:
            wait_timeout = max(1.0, grace_seconds)
        else:
            wait_timeout = max(0.0, phase_deadline - time.monotonic())
        result = process.wait(timeout=wait_timeout)
    except subprocess.TimeoutExpired:
        # ``unshare --kill-child=SIGKILL`` is the authoritative descendant
        # containment boundary.  If its wrapper does not reap, fail closed.
        result = process.poll()
    return result, killed


def _signal_pidfd(pidfd: int | None, signum: int) -> bool:
    """Signal a captured process identity without reopening a numeric PID."""

    if pidfd is None or not _pidfd_is_valid(pidfd):
        return False
    try:
        signal.pidfd_send_signal(pidfd, signum)
        return True
    except (AttributeError, OSError, ProcessLookupError):
        return False


def run_supervised(
    *,
    worker_command: Sequence[str],
    report_path: Path,
    worker_report_path: Path,
    max_duration_seconds: float,
    max_runner_minutes: float,
    worker_config: Mapping[str, Any],
    term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    env: Mapping[str, str] | None = None,
) -> SupervisorResult:
    """Run one worker in a mandatory, killable PID namespace.

    The only privileged chain is the absolute system path
    ``sudo -n → setpriv → unshare``.  The root side executes no checkout
    helper: after ``unshare`` forks PID 1, a second absolute ``setpriv``
    drops to the original runner UID/GID, clears groups, applies
    ``no-new-privs`` and clears every capability before it execs the checkout
    namespace helper.  The helper is therefore already non-root when it
    validates PID 1 and handshakes over stdin/stdout.  The parent tracks the
    sudo process (the exec-stable wrapper identity) and the captured namespace
    PID with pidfds/start-times.  ``unshare --kill-child=SIGKILL`` owns the
    descendant boundary; process groups are not a correctness fallback.  The
    final report is published only after the wrapper and a reaped namespace
    PID 1 prove teardown.
    """

    duration = _finite_positive(max_duration_seconds, field="max_duration_seconds")
    runner_minutes = _finite_positive(max_runner_minutes, field="max_runner_minutes")
    grace = _finite_positive(term_grace_seconds, field="term_grace_seconds")
    poll = _finite_positive(poll_seconds, field="poll_seconds")
    if not worker_command:
        raise ValueError("worker_command must not be empty")
    if worker_report_path == report_path:
        raise ValueError("worker report must be distinct from final report")
    if any(not isinstance(value, str) or not value for value in worker_command):
        raise ValueError("worker_command entries must be non-empty strings")
    if not Path(str(worker_command[0])).is_absolute():
        raise ValueError("worker interpreter must be an absolute path")

    started = time.monotonic()
    scenario_deadline = started + duration
    runner_deadline = started + runner_minutes * 60.0
    wall_deadline = min(scenario_deadline, runner_deadline)

    # Reserve a bounded slice of the single wall budget for TERM/KILL, pidfd
    # reaping, report validation and the final atomic envelope.  The reserve
    # is inside the authored deadline; it is not an extension or a second
    # timeout.  Without it, a worker that reaches the deadline would leave no
    # budget in which to prove namespace closure.
    teardown_reserve = min(
        max(grace + max(0.1, poll * 4.0), 0.25),
        max(duration, runner_minutes * 60.0),
    )
    worker_deadline = wall_deadline - teardown_reserve

    def primary_deadline_reason(*, effective: bool = False) -> str | None:
        return _deadline_reason_at(
            time.monotonic(),
            scenario_deadline=scenario_deadline,
            runner_deadline=runner_deadline,
            effective_deadline=worker_deadline if effective else None,
        )

    def early_failure(reason: str, error: str) -> SupervisorResult:
        selected_reason = reason
        deadline_reason = primary_deadline_reason()
        if deadline_reason is not None and reason in {
            "namespace_unavailable",
            "namespace_start_failed",
        }:
            selected_reason = deadline_reason
        payload = _closed_failure_report(
            reason=selected_reason,
            signal_number=None,
            returncode=None,
            partial_work=False,
            inflight_unknown=False,
            report_error=error,
            runtime_budget={
                "max_duration_seconds": _number_for_report(duration),
                "max_runner_minutes": _number_for_report(runner_minutes),
                "actual_elapsed_seconds": _number_for_report(time.monotonic() - started),
                "actual_runner_seconds": _number_for_report(time.monotonic() - started),
                "actual_runner_minutes": _number_for_report((time.monotonic() - started) / 60.0),
                "within_duration_budget": time.monotonic() < scenario_deadline,
                "within_runner_budget": time.monotonic() < runner_deadline,
                "budget_exceeded": selected_reason in {
                    "max_duration_seconds",
                    "max_runner_minutes",
                },
                "phase": "namespace_probe",
                "reason": selected_reason,
                "descendants_reaped": False,
            },
            descendants_reaped=False,
            namespace_closed=False,
        )
        # Pure preflight failures deliberately return an in-memory error
        # envelope.  The caller must not be allowed to create a directory,
        # remove a stale report, or write a misleading report before the
        # mandatory non-root capability boundary has been proven.
        return SupervisorResult(
            returncode=None,
            report=payload,
            worker_started=False,
            worker_exited=False,
            killed=False,
            signal=None,
            reason=selected_reason,
            partial_work=False,
            inflight_unknown=False,
            descendants_reaped=False,
            isolation=PID_NAMESPACE_ISOLATION,
            namespace_closed=False,
            namespace_init_pid=None,
        )

    try:
        require_pid_namespace_capability()
        runner_uid, runner_gid = _runner_identity()
    except NamespaceCapabilityError as exc:
        return early_failure("namespace_unavailable", type(exc).__name__)

    # Capability probing is pure with respect to the caller's filesystem, but
    # the resulting load budget is not allowed to be spent by setup.  Refuse
    # before any report directory/stale-file/config side effect if preflight
    # itself exhausted the one wall deadline.
    preflight_deadline_reason = primary_deadline_reason()
    if preflight_deadline_reason is not None:
        return early_failure(preflight_deadline_reason, "preflight_deadline_exceeded")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    worker_report_path.parent.mkdir(parents=True, exist_ok=True)
    for stale_path in (report_path, worker_report_path):
        if stale_path.is_symlink() or stale_path.exists():
            if stale_path.is_dir():
                raise ValueError("runtime report path must not be a directory")
            stale_path.unlink()
        if primary_deadline_reason() is not None:
            return early_failure(
                primary_deadline_reason() or "max_duration_seconds",
                "preflight_setup_deadline_exceeded",
            )

    try:
        parent_pidfd = os.pidfd_open(os.getpid(), 0)
        parent_starttime = _read_process_starttime(os.getpid())
    except (AttributeError, OSError, NamespaceIntegrityError) as exc:
        return early_failure("namespace_unavailable", type(exc).__name__)

    config_path = worker_report_path.with_name(f".{worker_report_path.name}.config")
    config = dict(worker_config)
    config.update(
        {
            "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
            "started_at_monotonic": started,
            "scenario_deadline_monotonic": scenario_deadline,
            "runner_deadline_monotonic": runner_deadline,
            "supervisor_wall_deadline_monotonic": wall_deadline,
            "supervisor_worker_deadline_monotonic": worker_deadline,
            "worker_report_path": str(worker_report_path),
            "namespace_required": True,
            "worker_command": [str(value) for value in worker_command],
            "runner_uid": runner_uid,
            "runner_gid": runner_gid,
        }
    )
    _write_json_atomic(config_path, config)
    setup_deadline_reason = primary_deadline_reason()
    if setup_deadline_reason is not None:
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass
        return early_failure(setup_deadline_reason, "setup_deadline_exceeded")
    process: subprocess.Popen[bytes] | None = None
    wrapper_pidfd: int | None = None
    wrapper_starttime: int | None = None
    namespace_init_pid: int | None = None
    namespace_init_pidfd: int | None = None
    namespace_init_starttime: int | None = None
    wrapper_chain_identities: dict[int, tuple[int, int]] = {}
    worker_started = False
    killed = False
    signal_number: int | None = None
    reason = "worker_failed"
    worker_budget_reason: str | None = None
    returncode: int | None = None
    report_error: str | None = None
    namespace_closed = False
    namespace_reaped = False
    external_signal: int | None = None
    previous_handlers: dict[int, Any] = {}
    previous_subreaper: int | None = None

    def on_parent_signal(signum: int, _frame: Any) -> None:
        nonlocal external_signal
        external_signal = signum

    def terminate_wrapper() -> None:
        if process is not None:
            try:
                process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        # sudo may be a monitor process rather than an exec-stable wrapper.
        # Signal the captured namespace init by pidfd as well; this is an
        # identity-checked containment operation, not a process-group fallback.
        _signal_pidfd(namespace_init_pidfd, signal.SIGTERM)

    def force_kill_captured_chain() -> None:
        _signal_pidfd(namespace_init_pidfd, signal.SIGKILL)
        for _starttime, pidfd in wrapper_chain_identities.values():
            _signal_pidfd(pidfd, signal.SIGKILL)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, on_parent_signal)
        except (ValueError, OSError):
            pass

    try:
        previous_subreaper = _set_child_subreaper()
        child_env = os.environ.copy()
        if env is not None:
            child_env.update({str(key): str(value) for key, value in env.items()})
        child_env["PLATFORM_LOAD_WORKER_CONFIG"] = str(config_path)
        child_env["PYTHONUNBUFFERED"] = "1"
        namespace_helper = Path(__file__).with_name("platform_load_namespace.py")
        watchdog_command = (
            sys.executable,
            str(namespace_helper),
            "--mode",
            "wrapper-watchdog",
            "--config",
            str(config_path),
            "--expected-parent-pid",
            str(os.getpid()),
            "--expected-parent-starttime",
            str(parent_starttime),
            "--parent-pidfd-fd",
            str(parent_pidfd),
        )
        process = subprocess.Popen(
            watchdog_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(parent_pidfd,),
            start_new_session=True,
            env=child_env,
        )
        worker_started = True
        try:
            wrapper_pidfd = os.pidfd_open(process.pid, 0)
            wrapper_starttime = _read_process_starttime(process.pid)
            capture_deadline = time.monotonic() + min(
                DEFAULT_NAMESPACE_CAPTURE_TIMEOUT_SECONDS,
                max(poll, worker_deadline - time.monotonic()),
            )
            if process.stdout is None or process.stdin is None:
                raise NamespaceIntegrityError("namespace stdio protocol is unavailable")
            if not _await_namespace_ready(
                process.stdout,
                process,
                deadline=capture_deadline,
            ):
                raise NamespaceIntegrityError("namespace PID 1 readiness handshake failed")
            while namespace_init_pid is None:
                if external_signal is not None:
                    raise NamespaceIntegrityError(
                        "external supervisor signal during namespace start"
                    )
                now = time.monotonic()
                effective_reason = primary_deadline_reason(effective=True)
                if now >= capture_deadline or effective_reason is not None:
                    if effective_reason is not None:
                        reason = effective_reason
                    raise NamespaceIntegrityError("namespace PID 1 identity is unavailable")
                candidate = _capture_namespace_pid(process.pid)
                if candidate is not None:
                    try:
                        candidate_starttime = _read_process_starttime(candidate)
                        candidate_pidfd = os.pidfd_open(candidate, 0)
                    except (AttributeError, OSError, NamespaceIntegrityError):
                        candidate = None
                    if candidate is not None:
                        namespace_init_pid = candidate
                        namespace_init_starttime = candidate_starttime
                        namespace_init_pidfd = candidate_pidfd
                if namespace_init_pid is None:
                    if process.poll() is not None:
                        raise NamespaceIntegrityError(
                            "namespace wrapper exited before PID 1 capture"
                        )
                    time.sleep(min(poll, max(0.0, capture_deadline - time.monotonic())))
            for candidate in _read_process_descendants(process.pid):
                try:
                    wrapper_chain_identities[candidate] = (
                        _read_process_starttime(candidate),
                        os.pidfd_open(candidate, 0),
                    )
                except (AttributeError, OSError, NamespaceIntegrityError):
                    continue
            if external_signal is not None:
                raise NamespaceIntegrityError(
                    "external supervisor signal during namespace start"
                )
            process.stdin.write(NAMESPACE_ACK)
            process.stdin.flush()
            if primary_deadline_reason(effective=True) is not None:
                raise NamespaceIntegrityError("namespace handshake exceeded wall deadline")
        except (AttributeError, OSError, NamespaceIntegrityError) as exc:
            if external_signal is not None:
                reason = "external_signal"
                signal_number = external_signal
            elif reason not in {"max_duration_seconds", "max_runner_minutes"}:
                reason = primary_deadline_reason(effective=True) or "namespace_start_failed"
            report_error = type(exc).__name__
            # Keep the non-root watchdog alive long enough to reclaim the
            # exact sudo/setpriv/unshare chain.  Killing the watchdog directly
            # would bypass its pidfd-driven descendant cleanup and could
            # strand a partially-started namespace.
            terminate_wrapper()
            returncode, killed = _reap_after_signal(
                process,
                grace_seconds=grace,
                poll_seconds=poll,
                deadline=wall_deadline,
            )
            if killed:
                force_kill_captured_chain()
            namespace_closed = False
        if process is not None and reason == "worker_failed":
            while process.poll() is None:
                if external_signal is not None:
                    signal_number = external_signal
                    reason = "external_signal"
                    terminate_wrapper()
                    returncode, killed = _reap_after_signal(
                        process,
                        grace_seconds=grace,
                        poll_seconds=poll,
                        deadline=wall_deadline,
                    )
                    break
                now = time.monotonic()
                budget_reason = primary_deadline_reason(effective=True)
                if budget_reason is not None:
                    reason = budget_reason
                    worker_budget_reason = budget_reason
                    terminate_wrapper()
                    returncode, killed = _reap_after_signal(
                        process,
                        grace_seconds=grace,
                        poll_seconds=poll,
                        deadline=wall_deadline,
                    )
                    break
                try:
                    process.wait(timeout=min(poll, max(0.0, worker_deadline - now)))
                except subprocess.TimeoutExpired:
                    continue
            if returncode is None:
                try:
                    returncode = process.wait(
                        timeout=max(0.0, wall_deadline - time.monotonic())
                    )
                except subprocess.TimeoutExpired:
                    reason = primary_deadline_reason() or reason
                    terminate_wrapper()
                    returncode, killed = _reap_after_signal(
                        process,
                        grace_seconds=grace,
                        poll_seconds=poll,
                        deadline=wall_deadline,
                    )
            if reason == "worker_failed":
                worker_budget_reason = primary_deadline_reason(effective=True)
                if worker_budget_reason is not None:
                    reason = worker_budget_reason
            if killed or _pidfd_is_live(namespace_init_pidfd or -1):
                force_kill_captured_chain()
        # The unshare wrapper can exit before its PID-namespace init is
        # reaped.  Reap that adopted child before checking /proc; a zombie is
        # terminated work, but an unreaped zombie is not a closed containment
        # boundary.  This cleanup also runs when the readiness handshake
        # fails, so a partially started namespace cannot linger as an
        # unverified child.
        if process is not None and returncode is not None:
            namespace_reaped = _reap_namespace_init(
                namespace_init_pid,
                namespace_starttime=namespace_init_starttime,
                namespace_pidfd=namespace_init_pidfd,
                timeout_seconds=max(1.0, grace),
                poll_seconds=poll,
                deadline=wall_deadline,
            )
            _reap_captured_chain(
                wrapper_chain_identities,
                timeout_seconds=max(1.0, grace),
                poll_seconds=poll,
                deadline=wall_deadline,
            )
            namespace_closed = _namespace_closed(
                process,
                pidfd=wrapper_pidfd,
                starttime=wrapper_starttime,
                namespace_pid=namespace_init_pid,
                namespace_pidfd=namespace_init_pidfd,
                namespace_starttime=namespace_init_starttime,
                namespace_reaped=namespace_reaped,
                wrapper_chain_identities=wrapper_chain_identities,
            )
    except BaseException:
        if process is not None and process.poll() is None:
            reason = primary_deadline_reason() or "supervisor_error"
            terminate_wrapper()
            returncode, killed = _reap_after_signal(
                process,
                grace_seconds=grace,
                poll_seconds=poll,
                deadline=wall_deadline,
            )
            if killed:
                force_kill_captured_chain()
        if process is not None and returncode is not None:
            namespace_reaped = _reap_namespace_init(
                namespace_init_pid,
                namespace_starttime=namespace_init_starttime,
                namespace_pidfd=namespace_init_pidfd,
                timeout_seconds=max(1.0, grace),
                poll_seconds=poll,
                deadline=wall_deadline,
            )
            _reap_captured_chain(
                wrapper_chain_identities,
                timeout_seconds=max(1.0, grace),
                poll_seconds=poll,
                deadline=wall_deadline,
            )
            namespace_closed = _namespace_closed(
                process,
                pidfd=wrapper_pidfd,
                starttime=wrapper_starttime,
                namespace_pid=namespace_init_pid,
                namespace_pidfd=namespace_init_pidfd,
                namespace_starttime=namespace_init_starttime,
                namespace_reaped=namespace_reaped,
                wrapper_chain_identities=wrapper_chain_identities,
            )
        raise
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        _restore_child_subreaper(previous_subreaper)
        chain_pidfds = tuple(
            pidfd for _starttime, pidfd in wrapper_chain_identities.values()
        )
        for descriptor in {
            parent_pidfd,
            namespace_init_pidfd,
            wrapper_pidfd,
            *chain_pidfds,
        }:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass

    assert process is not None
    if reason == "worker_failed" and returncode is not None and returncode < 0:
        signal_number = -returncode
        reason = "worker_signal"
    elif signal_number is None and reason in {"max_duration_seconds", "max_runner_minutes"}:
        if killed:
            signal_number = signal.SIGKILL
        elif returncode is not None and returncode < 0:
            signal_number = -returncode
    # A teardown phase, report parser or post-processing hook may consume the
    # final reserved wall slice.  Once that happens the authored deadline is
    # the primary reason; it is never legal to turn the overrun into a
    # successful/``none`` envelope.
    overrun_reason = primary_deadline_reason()
    if overrun_reason is not None and reason not in {"external_signal"}:
        reason = overrun_reason
        report_error = report_error or "wall_deadline_exceeded"

    # Once the worker's ordinary exit/signal status is known, an unclosed
    # namespace is the primary containment diagnosis unless an earlier
    # absolute timeout or externally delivered supervisor signal already owns
    # the outcome.  A missing child report must never turn either diagnosis
    # into a generic report error.
    if not namespace_closed and reason in {"worker_failed", "worker_signal"}:
        reason = "namespace_unclosed"
    report_gate_reason = primary_deadline_reason()
    if report_gate_reason is not None:
        if reason != "external_signal":
            reason = report_gate_reason
        report_error = report_error or "wall_deadline_before_report_read"
        worker_report = None
    else:
        worker_report, report_error = _read_closed_report(worker_report_path)
        after_report_reason = primary_deadline_reason()
        if after_report_reason is not None and reason != "external_signal":
            reason = after_report_reason
            report_error = report_error or "wall_deadline_during_report_read"
    acceptance_gate_reason = primary_deadline_reason()
    if acceptance_gate_reason is not None and reason != "external_signal":
        reason = acceptance_gate_reason
        report_error = report_error or "wall_deadline_before_acceptance"
    successful_worker = (
        worker_report is not None
        and reason == "worker_failed"
        and not killed
        and namespace_closed
        and acceptance_gate_reason is None
    )
    if successful_worker:
        final_payload = dict(worker_report)
        final_payload["isolation"] = PID_NAMESPACE_ISOLATION
        final_payload["namespace_closed"] = True
        final_payload["runtime_supervisor"] = {
            "protocol": RUNTIME_PROTOCOL_VERSION,
            "isolation": PID_NAMESPACE_ISOLATION,
            "namespace_closed": True,
            "reason": "none",
            "signal": None,
            "returncode": returncode,
            "partial_work": bool(final_payload.get("partial_work", False)),
            "inflight_unknown": bool(final_payload.get("inflight_unknown", False)),
            "descendants_reaped": True,
            "report_error": None,
        }
        # Final acceptance gate: this is intentionally adjacent to the
        # atomic publication.  A report parser or serializer that overruns
        # the one wall deadline is converted to a closed failure envelope.
        final_gate_reason = primary_deadline_reason()
        if final_gate_reason is not None:
            reason = final_gate_reason
            report_error = "wall_deadline_before_report_publication"
            successful_worker = False
        else:
            _write_json_atomic(report_path, final_payload)
            post_publish_reason = primary_deadline_reason()
            if post_publish_reason is not None:
                reason = post_publish_reason
                report_error = "wall_deadline_during_report_publication"
                successful_worker = False
            else:
                return SupervisorResult(
                    returncode=returncode,
                    report=final_payload,
                    worker_started=worker_started,
                    worker_exited=True,
                    killed=False,
                    signal=None,
                    reason="none",
                    partial_work=bool(final_payload.get("partial_work", False)),
                    inflight_unknown=bool(final_payload.get("inflight_unknown", False)),
                    descendants_reaped=True,
                    isolation=PID_NAMESPACE_ISOLATION,
                    namespace_closed=True,
                    namespace_init_pid=namespace_init_pid,
                )

    partial_work = worker_started
    inflight_unknown = killed or reason in {
        "max_duration_seconds",
        "max_runner_minutes",
        "external_signal",
        "worker_signal",
        "namespace_unclosed",
    } or report_error is not None or not namespace_closed
    runtime_status: dict[str, Any] = {
        "max_duration_seconds": _number_for_report(duration),
        "max_runner_minutes": _number_for_report(runner_minutes),
        "actual_elapsed_seconds": _number_for_report(time.monotonic() - started),
        "actual_runner_seconds": _number_for_report(time.monotonic() - started),
        "actual_runner_minutes": _number_for_report((time.monotonic() - started) / 60.0),
        "within_duration_budget": reason != "max_duration_seconds",
        "within_runner_budget": reason != "max_runner_minutes",
        "budget_exceeded": reason in {"max_duration_seconds", "max_runner_minutes"},
        "phase": "supervisor",
        "reason": reason if reason in _SAFE_REASON else "none",
        "descendants_reaped": namespace_closed,
    }
    final_payload = _closed_failure_report(
        reason=reason,
        signal_number=signal_number,
        returncode=returncode,
        partial_work=partial_work,
        inflight_unknown=inflight_unknown,
        report_error=report_error,
        runtime_budget=runtime_status,
        descendants_reaped=namespace_closed,
        namespace_closed=namespace_closed,
    )
    _write_json_atomic(report_path, final_payload)
    return SupervisorResult(
        returncode=returncode,
        report=final_payload,
        worker_started=worker_started,
        worker_exited=returncode is not None,
        killed=killed,
        signal=signal_number,
        # Preserve the supervisor's primary timeout/containment reason even
        # when the child was force-killed or its report is missing.  A report
        # parse error is evidence attached above, never a replacement reason.
        reason=reason,
        partial_work=partial_work,
        inflight_unknown=inflight_unknown,
        descendants_reaped=namespace_closed,
        isolation=PID_NAMESPACE_ISOLATION,
        namespace_closed=namespace_closed,
        namespace_init_pid=namespace_init_pid,
    )


def worker_entry(
    worker: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    *,
    config_path: Path | None = None,
) -> int:
    """Run a worker and atomically close its child report envelope."""

    _set_parent_death_signal()
    selected = config_path or Path(os.environ["PLATFORM_LOAD_WORKER_CONFIG"])
    try:
        config_raw = json.loads(selected.read_text(encoding="utf-8"))
        if not isinstance(config_raw, dict):
            raise ValueError("worker config must be an object")
        if config_raw.get("namespace_required") is True:
            try:
                _assert_namespace_worker_identity(
                    int(config_raw["runner_uid"]),
                    int(config_raw["runner_gid"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise NamespaceIntegrityError("worker identity config is invalid") from exc
        result = dict(worker(config_raw))
        result.setdefault("worker_report_schema", WORKER_REPORT_SCHEMA)
        result["report_complete"] = True
        # The worker cannot prove teardown; the parent sets this true only in
        # the final envelope after wrapper/pidfd/reap closure.
        result["namespace_closed"] = False
        result.setdefault("partial_work", False)
        result.setdefault("inflight_unknown", False)
        _write_json_atomic(Path(str(config_raw["worker_report_path"])), result)
        return 0 if result.get("passed") is True else 1
    except BaseException as exc:
        try:
            destination = Path(str(config_raw["worker_report_path"]))  # type: ignore[name-defined]
        except (NameError, KeyError, TypeError):
            return 2
        failed = _closed_failure_report(
            reason="worker_exception",
            signal_number=None,
            returncode=1,
            partial_work=True,
            inflight_unknown=True,
            report_error=type(exc).__name__,
        )
        try:
            _write_json_atomic(destination, failed)
        except OSError:
            return 2
        return 1
