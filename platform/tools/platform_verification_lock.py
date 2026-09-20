#!/usr/bin/env python3
"""Fail-closed lock for local verification contours sharing test services.

The lock is deliberately a small, host-local resource guard.  It is not a
database lock: migration resets the schema and backend integration tears down
the same database/Redis resources, so the wrapper must exclude both before
either process connects to them.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import time
from typing import Iterator


LOCK_NAME = "oldsparky-platformdb-test.lock"
RUNTIME_DIR_ENV = "PLATFORM_VERIFICATION_RUNTIME_DIR"
RUNTIME_DIR_PREFIX = ".oldsparky-platform-verification-"
LOCK_CONTOURS = frozenset(
    {"migration", "backend", "backend-integration", "backend-privileged"}
)
LOCK_CONTENTION_EXIT_CODE = 75


class VerificationLockError(RuntimeError):
    """Raised when the local shared-resource lock cannot be trusted/acquired."""


class _LockSignal(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"verification lock interrupted by signal {signum}")


def _no_symlink_path(path: Path, *, include_leaf: bool) -> bool:
    """Return whether existing components of *path* are ordinary directories."""

    current = path if include_leaf else path.parent
    components = current.parts
    if not path.is_absolute():
        return False
    cursor = Path(components[0])
    for component in components[1:]:
        cursor /= component
        try:
            info = os.lstat(cursor)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
    return True


def _validate_runtime_dir(root: Path) -> Path:
    """Validate one explicit runtime directory for the effective test user."""

    if (
        not root.is_absolute()
        or not _no_symlink_path(root, include_leaf=True)
        or not root.is_dir()
    ):
        raise VerificationLockError(
            "verification lock runtime directory is missing, unsafe or a symlink"
        )
    info = os.stat(root, follow_symlinks=False)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise VerificationLockError(
            "verification lock runtime directory must be mode 700 and owned by the test user"
        )
    return root


def _safe_tmp_root() -> Path:
    root = Path("/tmp")
    if not _no_symlink_path(root, include_leaf=True) or not root.is_dir():
        raise VerificationLockError("verification lock /tmp directory is missing or unsafe")
    info = os.stat(root, follow_symlinks=False)
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid != 0 or mode != 0o1777:
        raise VerificationLockError("verification lock /tmp directory is not root-owned sticky mode")
    return root


def _per_user_runtime_dir() -> Path:
    """Create the stable private fallback directory for this effective UID."""

    parent = _safe_tmp_root()
    root = parent / f"{RUNTIME_DIR_PREFIX}{os.geteuid()}"
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        # Never repair or chmod an existing path: validate its identity below so
        # a pre-created file/directory/symlink cannot become the lock root.
        pass
    return _validate_runtime_dir(root)


def _runtime_root() -> Path:
    # CI that crosses a sudo boundary must provide a directory owned by the
    # effective user.  This explicit variable prevents a runner-owned
    # XDG_RUNTIME_DIR from being inherited by a root test process.
    explicit = os.environ.get(RUNTIME_DIR_ENV)
    if explicit:
        return _validate_runtime_dir(Path(explicit))

    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        root = Path(configured)
        if not root.is_absolute() or not _no_symlink_path(root, include_leaf=True):
            raise VerificationLockError(
                "verification lock runtime directory is missing, unsafe or a symlink"
            )
        if not root.is_dir():
            raise VerificationLockError(
                "verification lock runtime directory is missing, unsafe or a symlink"
            )
        info = os.stat(root, follow_symlinks=False)
        if info.st_uid == os.geteuid():
            return _validate_runtime_dir(root)
        # A sudo invocation commonly retains the caller's XDG directory.  It
        # is safe to ignore that directory and create a stable per-UID root;
        # using it would either fail the ownership check or mix lock files
        # between effective users.
        return _per_user_runtime_dir()

    return _per_user_runtime_dir()


def default_lock_path() -> Path:
    """Return the exact lock pathname for the current safe runtime root."""

    return _runtime_root() / LOCK_NAME


def _validate_lock_file(path: Path, fd: int) -> tuple[int, int]:
    try:
        path_info = os.lstat(path)
        fd_info = os.fstat(fd)
    except OSError as exc:
        raise VerificationLockError("verification lock disappeared during validation") from exc
    if stat.S_ISLNK(path_info.st_mode) or not stat.S_ISREG(path_info.st_mode):
        raise VerificationLockError("verification lock is not a regular non-symlink file")
    if path_info.st_dev != fd_info.st_dev or path_info.st_ino != fd_info.st_ino:
        raise VerificationLockError("verification lock pathname/inode changed during open")
    if (
        fd_info.st_uid != os.geteuid()
        or fd_info.st_nlink != 1
        or stat.S_IMODE(fd_info.st_mode) != 0o600
    ):
        raise VerificationLockError("verification lock ownership, link count or mode is unsafe")
    return fd_info.st_dev, fd_info.st_ino


def _pid_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_marker(fd: int, *, expected_inode: int) -> dict[str, object] | None:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 4096)
    if not raw:
        return None
    try:
        marker = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationLockError("verification lock contains an invalid owner marker") from exc
    if not isinstance(marker, dict):
        raise VerificationLockError("verification lock owner marker is not an object")
    if marker.get("schema") != 1 or marker.get("inode") != expected_inode:
        raise VerificationLockError("verification lock owner marker is stale or unsafe")
    pid = marker.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise VerificationLockError("verification lock owner marker has an invalid PID")
    if _pid_is_alive(pid):
        raise VerificationLockError("verification lock is already held by another verification contour")
    raise VerificationLockError("verification lock has a stale owner marker")


def _write_marker(fd: int, *, inode: int, contour: str) -> None:
    marker = {
        "schema": 1,
        "pid": os.getpid(),
        "inode": inode,
        "contour": contour,
        "created_at": time.time(),
    }
    payload = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode()
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    os.fsync(fd)


@contextmanager
def verification_resource_lock(
    contour: str,
    *,
    path: Path | None = None,
) -> Iterator[None]:
    """Exclusively run one schema/resource-mutating verification contour.

    ``path`` exists only for isolated unit tests.  Production callers use the
    exact runtime/tmp pathname returned by :func:`default_lock_path`.
    """

    if contour not in LOCK_CONTOURS:
        yield
        return
    lock_path = default_lock_path() if path is None else Path(path)
    if not lock_path.is_absolute() or not _no_symlink_path(lock_path.parent, include_leaf=True):
        raise VerificationLockError("verification lock path or parent is unsafe")
    if not lock_path.parent.is_dir():
        raise VerificationLockError("verification lock parent directory is unavailable")
    fd = -1
    locked = False
    marker_written = False
    original_handlers: dict[int, object] = {}
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise VerificationLockError(f"unable to open verification lock: {exc}") from exc
        _dev, inode = _validate_lock_file(lock_path, fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise VerificationLockError(
                    "verification lock contention; refusing concurrent resource contours"
                ) from exc
            raise VerificationLockError("unable to acquire verification lock") from exc
        locked = True
        _read_marker(fd, expected_inode=inode)
        _write_marker(fd, inode=inode, contour=contour)
        marker_written = True

        def _signal_handler(signum: int, _frame: object) -> None:
            raise _LockSignal(signum)

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            original_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _signal_handler)
        yield
    finally:
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)
        if fd >= 0:
            if marker_written and locked:
                try:
                    _validate_lock_file(lock_path, fd)
                    os.ftruncate(fd, 0)
                    os.fsync(fd)
                except OSError:
                    # The process must never unlink or overwrite a path whose
                    # identity changed while the lock was held.
                    pass
                except VerificationLockError:
                    pass
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
