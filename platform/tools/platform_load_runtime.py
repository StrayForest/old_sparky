#!/usr/bin/env python3
"""Killable runtime boundary for the external load generator.

The measured generator is untrusted with respect to time: DNS, a socket,
response-body reads, a retry sleep, or a worker future can all stop making
progress.  The supervisor therefore owns two absolute monotonic deadlines and
executes the whole generator in a new process group.  A thread timeout is not
used as a kill boundary; it cannot reclaim a blocked syscall.

The worker-facing :class:`LoadRuntimeBudget` is deliberately small.  It is
used to cap ordinary I/O and to stop before a new phase/retry, while the
parent-side :func:`run_supervised` remains the final authority that can TERM
then KILL the entire process group and reap it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


RUNTIME_PROTOCOL_VERSION = 1
WORKER_REPORT_SCHEMA = 1
DEFAULT_TERM_GRACE_SECONDS = 1.0
DEFAULT_POLL_SECONDS = 0.02
MAX_CHILD_REPORT_BYTES = 16 * 1024 * 1024
MAX_REASON_LENGTH = 96
_SAFE_REASON = frozenset({"none", "max_duration_seconds", "max_runner_minutes"})


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
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_report_schema": WORKER_REPORT_SCHEMA,
        "report_complete": True,
        "passed": False,
        "authoritative": False,
        "dispatchable": False,
        "acceptance": {
            "passed": False,
            "decision": "LOAD RUNTIME BUDGET EXCEEDED"
            if reason in {"max_duration_seconds", "max_runner_minutes"}
            else "LOAD RUN FAILED",
            "contract_ok": False,
        },
        "runtime_supervisor": {
            "protocol": RUNTIME_PROTOCOL_VERSION,
            "reason": reason,
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


def _set_parent_death_signal() -> None:
    """Make the worker die if its supervisor disappears on Linux."""

    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        # prctl(PR_SET_PDEATHSIG, SIGTERM)
        result = libc.prctl(1, signal.SIGTERM, 0, 0, 0)
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number not in {0, errno.EINVAL, errno.ENOSYS}:
                raise OSError(error_number, os.strerror(error_number))
    except (OSError, AttributeError):
        # The process-group boundary remains authoritative on platforms where
        # prctl is unavailable.  Never fail a load solely on optional PDEATHSIG.
        return


def _signal_group(process: subprocess.Popen[bytes], signal_number: int) -> bool:
    try:
        os.killpg(process.pid, signal_number)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        try:
            process.send_signal(signal_number)
            return True
        except ProcessLookupError:
            return False


def _group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A live group that cannot be inspected is safer to treat as live; the
        # caller will still attempt the TERM/KILL sequence.
        return True


def _wait_until(
    process: subprocess.Popen[bytes],
    *,
    deadline: float,
    poll_seconds: float,
) -> int | None:
    while True:
        returncode = process.poll()
        if returncode is not None:
            return returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return process.wait(timeout=min(poll_seconds, remaining))
        except subprocess.TimeoutExpired:
            continue


def _reap_after_signal(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
    poll_seconds: float,
) -> tuple[int | None, bool, bool]:
    deadline = time.monotonic() + grace_seconds
    result: int | None = process.poll()
    while time.monotonic() < deadline:
        result = process.poll()
        if result is not None and not _group_exists(process.pid):
            return result, False, True
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
    killed = _signal_group(process, signal.SIGKILL)
    try:
        result = process.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:
        # A process in the group should be gone after SIGKILL.  Keep the
        # parent from leaking a child even if the platform reports late.
        result = process.poll()
    reap_deadline = time.monotonic() + max(1.0, grace_seconds)
    while _group_exists(process.pid) and time.monotonic() < reap_deadline:
        time.sleep(min(poll_seconds, max(0.0, reap_deadline - time.monotonic())))
    return result, killed, not _group_exists(process.pid)


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
    """Run one worker, enforce both deadlines, and publish one closed report.

    The worker report is never copied into ``report_path`` before the worker
    has exited and its JSON envelope has been validated.  On every failure the
    parent writes a small closed report atomically, so workflow artifact upload
    and fixture finalization can proceed without trusting a half-written file.
    """

    duration = _finite_positive(max_duration_seconds, field="max_duration_seconds")
    runner_minutes = _finite_positive(max_runner_minutes, field="max_runner_minutes")
    grace = _finite_positive(term_grace_seconds, field="term_grace_seconds")
    poll = _finite_positive(poll_seconds, field="poll_seconds")
    if not worker_command:
        raise ValueError("worker_command must not be empty")
    if worker_report_path == report_path:
        raise ValueError("worker report must be distinct from final report")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    worker_report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        worker_report_path.unlink()
    except FileNotFoundError:
        pass
    started = time.monotonic()
    scenario_deadline = started + duration
    runner_deadline = started + runner_minutes * 60.0
    config = dict(worker_config)
    config.update(
        {
            "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
            "started_at_monotonic": started,
            "scenario_deadline_monotonic": scenario_deadline,
            "runner_deadline_monotonic": runner_deadline,
            "worker_report_path": str(worker_report_path),
        }
    )
    config_path = worker_report_path.with_name(f".{worker_report_path.name}.config")
    _write_json_atomic(config_path, config)
    process: subprocess.Popen[bytes] | None = None
    worker_started = False
    killed = False
    signal_number: int | None = None
    reason = "worker_failed"
    descendants_reaped = True
    external_signal: int | None = None
    previous_handlers: dict[int, Any] = {}

    def on_parent_signal(signum: int, _frame: Any) -> None:
        nonlocal external_signal
        external_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, on_parent_signal)
        except (ValueError, OSError):
            pass

    try:
        child_env = os.environ.copy()
        if env is not None:
            child_env.update({str(key): str(value) for key, value in env.items()})
        child_env["PLATFORM_LOAD_WORKER_CONFIG"] = str(config_path)
        child_env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [str(value) for value in worker_command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=child_env,
        )
        worker_started = True
        while process.poll() is None:
            if external_signal is not None:
                signal_number = external_signal
                reason = "external_signal"
                _signal_group(process, signal.SIGTERM)
                returncode, was_killed, group_reaped = _reap_after_signal(
                    process,
                    grace_seconds=grace,
                    poll_seconds=poll,
                )
                killed = was_killed
                descendants_reaped = group_reaped
                break
            now = time.monotonic()
            if now >= scenario_deadline:
                reason = "max_duration_seconds"
                _signal_group(process, signal.SIGTERM)
                returncode, was_killed, group_reaped = _reap_after_signal(
                    process,
                    grace_seconds=grace,
                    poll_seconds=poll,
                )
                killed = was_killed
                descendants_reaped = group_reaped
                break
            if now >= runner_deadline:
                reason = "max_runner_minutes"
                _signal_group(process, signal.SIGTERM)
                returncode, was_killed, group_reaped = _reap_after_signal(
                    process,
                    grace_seconds=grace,
                    poll_seconds=poll,
                )
                killed = was_killed
                descendants_reaped = group_reaped
                break
            try:
                process.wait(timeout=min(poll, scenario_deadline - now, runner_deadline - now))
            except subprocess.TimeoutExpired:
                continue
        else:
            returncode = process.returncode
        if reason == "worker_failed" and returncode is not None and _group_exists(process.pid):
            # A worker that exits while leaving a child, subprocess or helper
            # process behind is not a completed load.  Reclaim the same group
            # before accepting any report so later fixture cleanup cannot race
            # with a background mutator.
            reason = "worker_descendants"
            _signal_group(process, signal.SIGTERM)
            returncode, was_killed, group_reaped = _reap_after_signal(
                process,
                grace_seconds=grace,
                poll_seconds=poll,
            )
            killed = was_killed
            descendants_reaped = group_reaped
        if process.poll() is None:
            # Defensive final reap if a platform returned from wait without a
            # visible return code.
            returncode = process.wait(timeout=max(1.0, grace))
    except BaseException:
        if process is not None and process.poll() is None:
            reason = "supervisor_error"
            _signal_group(process, signal.SIGTERM)
            returncode, was_killed, group_reaped = _reap_after_signal(
                process,
                grace_seconds=grace,
                poll_seconds=poll,
            )
            killed = was_killed
            descendants_reaped = group_reaped
        raise
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass

    assert process is not None
    if reason == "worker_failed" and returncode is not None and returncode < 0:
        signal_number = -returncode
        reason = "worker_signal"
    elif signal_number is None and reason in {
        "max_duration_seconds",
        "max_runner_minutes",
        "worker_descendants",
    }:
        if killed:
            signal_number = signal.SIGKILL
        elif returncode is not None and returncode < 0:
            signal_number = -returncode
    worker_report, report_error = _read_closed_report(worker_report_path)
    # A completed load with a legitimate acceptance failure exits non-zero by
    # design.  The report is still authoritative evidence of that failed
    # measurement and must not be replaced by a generic supervisor error.
    successful_worker = (
        worker_report is not None
        and reason == "worker_failed"
        and not killed
        and descendants_reaped
    )
    if successful_worker:
        final_payload = dict(worker_report)
        final_payload["runtime_supervisor"] = {
            "protocol": RUNTIME_PROTOCOL_VERSION,
            "reason": "none",
            "signal": None,
            "returncode": returncode,
            "partial_work": bool(final_payload.get("partial_work", False)),
            "inflight_unknown": bool(final_payload.get("inflight_unknown", False)),
            "descendants_reaped": True,
            "report_error": None,
        }
        _write_json_atomic(report_path, final_payload)
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
            descendants_reaped=descendants_reaped,
        )

    partial_work = True
    inflight_unknown = killed or reason in {
        "max_duration_seconds",
        "max_runner_minutes",
        "external_signal",
        "worker_signal",
    } or report_error is not None
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
        "descendants_reaped": descendants_reaped,
    }
    final_payload = _closed_failure_report(
        reason=reason,
        signal_number=signal_number,
        returncode=returncode,
        partial_work=partial_work,
        inflight_unknown=inflight_unknown,
        report_error=report_error,
        runtime_budget=runtime_status,
        descendants_reaped=descendants_reaped,
    )
    _write_json_atomic(report_path, final_payload)
    return SupervisorResult(
        returncode=returncode,
        report=final_payload,
        worker_started=worker_started,
        worker_exited=True,
        killed=killed,
        signal=signal_number,
        reason=reason if reason != "worker_failed" else (report_error or reason),
        partial_work=partial_work,
        inflight_unknown=inflight_unknown,
        descendants_reaped=descendants_reaped,
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
        result = dict(worker(config_raw))
        result.setdefault("worker_report_schema", WORKER_REPORT_SCHEMA)
        result["report_complete"] = True
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
