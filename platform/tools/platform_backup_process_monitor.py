#!/usr/bin/env python3
"""One trusted helper, one PID namespace, and one bounded status record."""

from __future__ import annotations

import ctypes, errno, json, os, select, signal, subprocess, sys, time

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
    try:
        os.write(fd, data if len(data) <= STATUS_LIMIT else b'{"schema":1,"status":"protocol_error"}\n')
    except OSError:
        pass
def _rc(status: int) -> int:
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status) if os.WIFSIGNALED(status) else MONITOR_ERROR_EXIT
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
        return not os.read(fd, 4096) or True
    except (BlockingIOError, OSError, ValueError):
        return True
def _pid1(control: int, status_fd: int, command: list[str], pass_fds: tuple[int, ...], stdout_fd: int | None, work_deadline: int, deadline: int, env: dict[str, str] | None) -> None:
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
            _emit(status_fd, "cancelled")
            return
        target = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout_fd, stderr=None, env=env, close_fds=True, pass_fds=pass_fds, start_new_session=True, shell=False, preexec_fn=lambda: _pdeath(os.getppid()))
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
        target_rc, cleaned = _cleanup(target.pid, target_rc, deadline)
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
def _monitor(command: list[str], pass_fds: tuple[int, ...], stdout_fd: int | None, status_fd: int, deadline: int, reserve: int, env: dict[str, str] | None) -> int:
    control_write = -1
    try:
        _pdeath(os.getppid())
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.unshare(CLONE_NEWPID) != 0:
            _emit(status_fd, "namespace_unavailable")
            return MONITOR_ERROR_EXIT
        control_read, control_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(control_write)
            _pid1(control_read, status_fd, command, pass_fds, stdout_fd, deadline - reserve, deadline, env)
            os._exit(0)
        os.close(control_read)
        cancelled = False
        os.set_blocking(0, False)
        while True:
            try:
                result, child_status = os.waitpid(child, os.WNOHANG)
            except InterruptedError:
                continue
            if result == child:
                return 0 if os.WIFEXITED(child_status) and os.WEXITSTATUS(child_status) == 0 else MONITOR_ERROR_EXIT
            if not cancelled and time.monotonic_ns() >= deadline:
                cancelled = True
            if not cancelled:
                ready, _, _ = select.select([0], [], [], 0.02)
                if ready:
                    os.read(0, 4096)
                    cancelled = True
            if cancelled:
                try:
                    os.write(control_write, b"C")
                except OSError:
                    pass
                if time.monotonic_ns() >= deadline:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except OSError:
                        pass
                    os.waitpid(child, 0)
                    return MONITOR_ERROR_EXIT
    except BaseException:
        _emit(status_fd, "monitor_error")
        return MONITOR_ERROR_EXIT
    finally:
        if control_write >= 0:
            try:
                os.close(control_write)
            except OSError:
                pass
        try:
            os.close(status_fd)
        except OSError:
            pass
def _parse(argv: list[str]) -> tuple[list[str], tuple[int, ...], int | None, int, int, int]:
    def value(name: str) -> str:
        try:
            return argv[argv.index(name) + 1]
        except (ValueError, IndexError) as exc:
            raise ValueError("backup monitor protocol is invalid") from exc
    status_fd, deadline, reserve = int(value("--status-fd")), int(value("--deadline-ns")), int(value("--cleanup-reserve-ns"))
    stdout_fd = int(value("--stdout-fd")) if "--stdout-fd" in argv else None
    pass_fds = tuple(int(item.split("=", 1)[1]) for item in argv if item.startswith("--pass-fd="))
    if "--" not in argv:
        raise ValueError("backup monitor target is missing")
    command = argv[argv.index("--") + 1 :]
    if not command or status_fd < 0 or deadline <= 0 or reserve < 0 or any(fd < 0 for fd in pass_fds) or (stdout_fd is not None and stdout_fd < 0):
        raise ValueError("backup monitor protocol is invalid")
    return command, pass_fds, stdout_fd, status_fd, deadline, reserve

def main(argv: list[str] | None = None) -> int:
    try:
        command, pass_fds, stdout_fd, status_fd, deadline, reserve = _parse(argv or sys.argv[1:])
        return _monitor(command, pass_fds, stdout_fd, status_fd, deadline, reserve, None)
    except BaseException:
        return MONITOR_ERROR_EXIT

if __name__ == "__main__":
    raise SystemExit(main())
