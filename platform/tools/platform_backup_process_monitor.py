#!/usr/bin/env python3
"""One trusted helper, one PID namespace, and one bounded status record."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import select
import signal
import subprocess
import sys
import time

CLONE_NEWPID = 0x20000000
PR_SET_PDEATHSIG = 1
STATUS_LIMIT = 4096
TERM_GRACE_NS = 250_000_000
MONITOR_ERROR_EXIT = 125


def _pdeath(parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.prctl
    call.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    call.restype = ctypes.c_int
    if call(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno() or errno.EIO, "backup monitor prctl failed")
    if os.getppid() != parent:
        raise RuntimeError("backup monitor parent changed")
def _emit(fd: int, status: str, returncode: int | None = None) -> None:
    payload: dict[str, object] = {"schema": 1, "status": status}
    if returncode is not None:
        payload["returncode"] = int(returncode)
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    fallback = b'{"schema":1,"status":"protocol_error"}\n'
    try:
        os.write(fd, data if len(data) <= STATUS_LIMIT else fallback)
    except OSError:
        pass
def _rc(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return MONITOR_ERROR_EXIT
def _reap(target: int, target_rc: int | None) -> tuple[int | None, bool]:
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except InterruptedError:
            continue
        except ChildProcessError:
            return target_rc, False
        if pid == 0:
            return target_rc, True
        if pid == target:
            target_rc = _rc(status)
def _kill_all(signum: int) -> None:
    if os.getpid() != 1:
        raise RuntimeError("namespace process cleanup requires PID 1")
    try:
        os.kill(-1, signum)  # PID 1 is excluded; detached namespace children are not.
    except OSError:
        pass
def _cleanup(target: int, target_rc: int | None, deadline: int) -> tuple[int | None, bool]:
    _kill_all(signal.SIGTERM)
    grace = min(deadline, time.monotonic_ns() + TERM_GRACE_NS)
    while time.monotonic_ns() < grace:
        target_rc, pending = _reap(target, target_rc)
        if not pending:
            return target_rc, True
        time.sleep(0.005)
    _kill_all(signal.SIGKILL)
    while time.monotonic_ns() < deadline:
        target_rc, pending = _reap(target, target_rc)
        if not pending:
            return target_rc, True
        time.sleep(0.005)
    target_rc, pending = _reap(target, target_rc)
    return target_rc, not pending
def _cancelled(fd: int) -> bool:
    try:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return False
        os.read(fd, 4096)
        return True
    except BlockingIOError:
        return False
    except OSError as exc:
        return exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR)
    except ValueError:
        return True


def _forward_status(source: int, destination: int) -> bool:
    try:
        payload = os.read(source, STATUS_LIMIT + 1)
        if not payload or len(payload) > STATUS_LIMIT or not payload.endswith(b"\n"):
            return False
        os.write(destination, payload)
        return True
    except OSError:
        return False


def _pid1(
    control: int, status_fd: int, command: list[str],
    pass_fds: tuple[int, ...], stdout_fd: int | None,
    work_deadline: int, cleanup_deadline: int,
    env: dict[str, str] | None,
) -> None:
    try:
        _pdeath(os.getppid())
        if os.getpid() != 1 or not command or any(not isinstance(item, str) or not item for item in command):
            _emit(status_fd, "namespace_unavailable" if os.getpid() != 1 else "protocol_error")
            return
        if stdout_fd is not None and stdout_fd not in pass_fds:
            _emit(status_fd, "protocol_error")
            return
        os.set_blocking(control, False)
        if _cancelled(control) or time.monotonic_ns() >= work_deadline:
            _emit(status_fd, "timeout", 124)
            return
        target = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=stdout_fd,
            stderr=None, env=env, close_fds=True, pass_fds=pass_fds,
            start_new_session=True, shell=False,
            preexec_fn=lambda: _pdeath(os.getppid()),
        )
        target_rc: int | None = None
        cancelled = False
        while target_rc is None:
            target_rc, _ = _reap(target.pid, target_rc)
            if target_rc is not None:
                break
            if _cancelled(control) or time.monotonic_ns() >= work_deadline:
                cancelled = True
                break
            remaining = min(work_deadline - time.monotonic_ns(), 50_000_000)
            if remaining > 0:
                select.select([control], [], [], remaining / 1_000_000_000)
        target_rc, cleaned = _cleanup(target.pid, target_rc, cleanup_deadline)
        if not cleaned:
            _emit(status_fd, "monitor_error")
        elif time.monotonic_ns() >= work_deadline:
            _emit(status_fd, "timeout", target_rc)
        elif cancelled:
            _emit(status_fd, "cancelled", target_rc)
        else:
            _emit(status_fd, "completed", target_rc)
    except BaseException:
        _emit(status_fd, "monitor_error")
    finally:
        for fd in (control, status_fd):
            try:
                os.close(fd)
            except OSError:
                pass
def _monitor(
    command: list[str], pass_fds: tuple[int, ...], stdout_fd: int | None,
    status_fd: int, cleanup_deadline: int, reserve: int,
    env: dict[str, str] | None,
) -> int:
    control_write = status_read = status_write = -1
    work_deadline = cleanup_deadline - reserve
    try:
        _pdeath(os.getppid())
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(CLONE_NEWPID) != 0:
            _emit(status_fd, "namespace_unavailable")
            return MONITOR_ERROR_EXIT
        status_read, status_write = os.pipe()
        control_read, control_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(control_write)
            os.close(status_read)
            _pid1(control_read, status_write, command, pass_fds, stdout_fd,
                  work_deadline, cleanup_deadline, env)
            os._exit(0)
        os.close(control_read)
        os.close(status_write)
        status_write = -1
        cancelled = cancel_sent = False
        os.set_blocking(0, False)
        os.set_blocking(control_write, False)
        while True:
            try:
                result, child_status = os.waitpid(child, os.WNOHANG)
            except InterruptedError:
                continue
            if result == child:
                forwarded = _forward_status(status_read, status_fd)
                return 0 if forwarded and os.WIFEXITED(child_status) and os.WEXITSTATUS(child_status) == 0 else MONITOR_ERROR_EXIT
            now = time.monotonic_ns()
            if not cancelled and now >= work_deadline:
                cancelled = True
            if not cancelled:
                ready, _, _ = select.select([0], [], [], 0.02)
                if ready:
                    try:
                        os.read(0, 4096)
                    except BlockingIOError:
                        pass
                    except OSError as exc:
                        cancelled = exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR)
                    else:
                        cancelled = True
            if cancelled and not cancel_sent:
                cancel_sent = True
                try:
                    os.write(control_write, b"C")
                except OSError:
                    pass
            if cancelled and time.monotonic_ns() >= cleanup_deadline:
                try:
                    os.kill(child, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    os.waitpid(child, 0)
                except ChildProcessError:
                    pass
                if not _forward_status(status_read, status_fd):
                    _emit(status_fd, "timeout", 124)
                return 0
    except BaseException:
        _emit(status_fd, "monitor_error")
        return MONITOR_ERROR_EXIT
    finally:
        if control_write >= 0:
            try:
                os.close(control_write)
            except OSError:
                pass
        for fd in (status_read, status_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        try:
            os.close(status_fd)
        except OSError:
            pass
def _parse(argv: list[str]) -> tuple[list[str], tuple[int, ...], int | None, int, int, int]:
    if "--" not in argv:
        raise ValueError("backup monitor target is missing")
    separator = argv.index("--")
    controls, command = argv[:separator], argv[separator + 1 :]
    values: dict[str, str] = {}
    pass_fds: list[int] = []
    index = 0
    while index < len(controls):
        token = controls[index]
        if token.startswith("--pass-fd="):
            try:
                fd = int(token.split("=", 1)[1])
            except ValueError as exc:
                raise ValueError("backup monitor protocol is invalid") from exc
            if fd < 0 or fd in pass_fds:
                raise ValueError("backup monitor protocol is invalid")
            pass_fds.append(fd)
            index += 1
            continue
        if token not in {"--status-fd", "--deadline-ns", "--cleanup-reserve-ns", "--stdout-fd"} or token in values or index + 1 >= len(controls):
            raise ValueError("backup monitor protocol is invalid")
        values[token] = controls[index + 1]
        if values[token].startswith("-"):
            raise ValueError("backup monitor protocol is invalid")
        index += 2
    try:
        status_fd, deadline, reserve = (int(values[name]) for name in ("--status-fd", "--deadline-ns", "--cleanup-reserve-ns"))
        stdout_fd = int(values["--stdout-fd"]) if "--stdout-fd" in values else None
    except (KeyError, ValueError) as exc:
        raise ValueError("backup monitor protocol is invalid") from exc
    if not command or status_fd < 0 or deadline <= 0 or reserve < 0 or reserve >= deadline or any(fd < 0 for fd in pass_fds) or len(pass_fds) != len(set(pass_fds)) or (stdout_fd is not None and stdout_fd < 0) or status_fd in pass_fds or (stdout_fd is not None and stdout_fd == status_fd):
        raise ValueError("backup monitor protocol is invalid")
    return command, tuple(pass_fds), stdout_fd, status_fd, deadline, reserve


def main(argv: list[str] | None = None) -> int:
    try:
        command, pass_fds, stdout_fd, status_fd, deadline, reserve = _parse(argv or sys.argv[1:])
        return _monitor(command, pass_fds, stdout_fd, status_fd, deadline, reserve, None)
    except BaseException:
        return MONITOR_ERROR_EXIT

if __name__ == "__main__":
    raise SystemExit(main())
