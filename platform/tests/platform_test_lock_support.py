"""Fail-closed helpers for privileged tests that exercise filesystem locks.

These helpers deliberately keep test locks away from production lock names and
pin the file identity for teardown.  A test must never silently clean up a
path that was replaced while it was running.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
from uuid import uuid4


TEST_LOCK_ROOT = Path("/run/lock")
TEST_LOCK_PREFIX = f"oldsparky-platform-test-{os.getpid()}-"
PRODUCTION_RELEASE_LOCK = Path("/run/lock/oldsparky-platform-release.lock")


def _metadata(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_uid,
        st.st_gid,
        st.st_nlink,
        stat.S_IMODE(st.st_mode),
    )


def _assert_lock_metadata(path: Path, st: os.stat_result) -> None:
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != 0
        or st.st_gid != 0
        or st.st_nlink != 1
        or stat.S_IMODE(st.st_mode) != 0o600
    ):
        raise AssertionError(f"unsafe test lock metadata: {path}")


def _open_root(root: Path) -> int:
    root_stat = root.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != 0
        or root_stat.st_gid != 0
        or (
            stat.S_IMODE(root_stat.st_mode) & 0o022
            and not stat.S_IMODE(root_stat.st_mode) & stat.S_ISVTX
        )
        or root.resolve(strict=True) != root
    ):
        raise AssertionError(f"unsafe test lock root: {root}")
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    opened = os.fstat(descriptor)
    if _metadata(opened)[:2] != _metadata(root_stat)[:2]:
        os.close(descriptor)
        raise AssertionError(f"test lock root changed during validation: {root}")
    return descriptor


@dataclass
class TestLock:
    """A securely created test lock with an identity-pinned cleanup path."""

    path: Path
    fd: int
    identity: tuple[int, int, int, int, int, int]
    root_fd: int

    def acquire(self, *, nonblocking: bool = False) -> None:
        flags = fcntl.LOCK_EX
        if nonblocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(self.fd, flags)

    def release(self) -> None:
        if self.fd >= 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def close(self) -> None:
        if self.fd >= 0:
            self.release()
            os.close(self.fd)
            self.fd = -1
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1

    def cleanup(self) -> None:
        """Close and remove only the exact file this helper created."""

        if self.fd >= 0:
            self.release()
            os.close(self.fd)
            self.fd = -1
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            if self.root_fd >= 0:
                os.close(self.root_fd)
                self.root_fd = -1
            return
        try:
            _assert_lock_metadata(self.path, current)
            if not self.path.name.startswith(TEST_LOCK_PREFIX):
                raise AssertionError(
                    f"refusing to clean unexpected test lock: {self.path}"
                )
            if _metadata(current) != self.identity:
                raise AssertionError(f"test lock identity changed: {self.path}")
            if self.root_fd < 0:
                self.root_fd = _open_root(self.path.parent)
            os.unlink(self.path.name, dir_fd=self.root_fd)
        finally:
            if self.root_fd >= 0:
                os.close(self.root_fd)
                self.root_fd = -1


def create_test_lock(label: str, *, root: Path = TEST_LOCK_ROOT) -> TestLock:
    """Create a unique root-owned 0600 regular test lock, fail-closed."""

    if not label or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for ch in label):
        raise ValueError("test lock label is invalid")
    root_fd = _open_root(root)
    name = f"{TEST_LOCK_PREFIX}{label}-{uuid4().hex}.lock"
    path = root / name
    if path == PRODUCTION_RELEASE_LOCK:
        os.close(root_fd)
        raise AssertionError("test lock resolved to production release lock")
    try:
        fd = os.open(
            name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=root_fd,
        )
    except BaseException:
        os.close(root_fd)
        raise
    try:
        opened = os.fstat(fd)
        opened_identity = _metadata(opened)
        _assert_lock_metadata(path, opened)
        path_metadata = path.lstat()
        _assert_lock_metadata(path, path_metadata)
        identity = opened_identity
        if _metadata(path_metadata) != identity:
            raise AssertionError(f"test lock identity changed during creation: {path}")
        return TestLock(path, fd, identity, root_fd)
    except BaseException:
        # Remove only the inode opened by this call.  If the pathname was
        # swapped before cleanup, leave the replacement untouched and fail
        # closed rather than deleting an attacker-controlled file.
        try:
            current = path.lstat()
            if (
                current.st_dev == opened_identity[0]
                and current.st_ino == opened_identity[1]
                and path.name.startswith(TEST_LOCK_PREFIX)
            ):
                os.unlink(path.name, dir_fd=root_fd)
        except (OSError, UnboundLocalError):
            pass
        os.close(fd)
        os.close(root_fd)
        raise
