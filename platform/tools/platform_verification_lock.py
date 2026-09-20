#!/usr/bin/env python3
"""Fail-closed global lock for verification contours sharing test services.

The lock is a host-local guard, not a database lock.  Migration resets the
schema and backend integration tears down the same database/Redis resources,
so every process must lock the same inode before it connects to them.

The pathname is intentionally fixed and is never selected from the
environment.  Root provisions it once in a root-owned, non-writable parent;
all users then open the root-owned read-only file and contend on its inode
with ``flock``.  The file has no writable owner marker: lock contents are not
security identity and must not be trusted or modified by a less privileged
caller.
"""

from __future__ import annotations

from contextlib import contextmanager
import argparse
import errno
import fcntl
import os
from pathlib import Path
import signal
import stat
import sys
from typing import Iterator


LOCK_NAME = "oldsparky-platformdb-test.lock"
LOCK_PARENT = Path("/run/lock/oldsparky-platform-verification")
LOCK_PATH = LOCK_PARENT / LOCK_NAME
LOCK_CONTOURS = frozenset(
    {"migration", "backend", "backend-integration", "backend-privileged"}
)
ROOT_UID = 0
ROOT_DIR_MODE = 0o755
LOCK_FILE_MODE = 0o444


class VerificationLockError(RuntimeError):
    """Raised when the global verification lock cannot be trusted/acquired."""


class _LockSignal(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"verification lock interrupted by signal {signum}")


def _lstat_directory(path: Path, *, expected_mode: int | None = None) -> os.stat_result:
    """Return a validated directory identity without following symlinks."""

    try:
        info = os.lstat(path)
    except OSError as exc:
        raise VerificationLockError(
            f"verification lock parent is unavailable: {path}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise VerificationLockError(f"verification lock parent is not a directory: {path}")
    if info.st_uid != ROOT_UID:
        raise VerificationLockError(
            f"verification lock parent has the wrong owner: {path}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if expected_mode is not None and mode != expected_mode:
        raise VerificationLockError(
            f"verification lock parent has the wrong mode: {path}"
        )
    if expected_mode is None and mode & 0o022:
        raise VerificationLockError(
            f"verification lock parent is writable by a non-root identity: {path}"
        )
    return info


def _parent_chain(path: Path) -> tuple[tuple[Path, int, int], ...]:
    """Validate every component up to the fixed lock parent.

    ``/run/lock`` is a standard root-owned sticky runtime anchor and is the
    only writable component permitted by this contract.  The lock's own
    parent is exact mode 0755, so a non-root user cannot pre-create, replace,
    chmod or unlink the lock pathname.
    """

    if not path.is_absolute():
        raise VerificationLockError("verification lock path must be absolute")
    parent = path.parent
    components = parent.parts
    current = Path(components[0])
    identities: list[tuple[Path, int, int]] = []
    root_info = _lstat_directory(current, expected_mode=0o755)
    identities.append((current, root_info.st_dev, root_info.st_ino))
    for component in components[1:]:
        current /= component
        expected_mode = 0o1777 if current in {Path("/run/lock"), Path("/tmp")} else None
        info = _lstat_directory(current, expected_mode=expected_mode)
        identities.append((current, info.st_dev, info.st_ino))
    if parent == LOCK_PARENT:
        _lstat_directory(parent, expected_mode=ROOT_DIR_MODE)
    else:
        # The alternate path is used only by focused tests after monkeypatching
        # LOCK_PATH.  It obeys the same root-owned/non-writable rule.
        _lstat_directory(parent, expected_mode=ROOT_DIR_MODE)
    return tuple(identities)


def _validate_existing_chain(path: Path) -> None:
    """Validate an existing directory and every ancestor without following links."""

    if not path.is_absolute() or not path.parts:
        raise VerificationLockError("verification lock parent must be absolute")
    current = Path(path.parts[0])
    _lstat_directory(current, expected_mode=0o755)
    for component in path.parts[1:]:
        current /= component
        expected_mode = 0o1777 if current in {Path("/run/lock"), Path("/tmp")} else None
        _lstat_directory(current, expected_mode=expected_mode)


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
        fd_info.st_uid != ROOT_UID
        or fd_info.st_nlink != 1
        or stat.S_IMODE(fd_info.st_mode) != LOCK_FILE_MODE
    ):
        raise VerificationLockError(
            "verification lock ownership, link count or mode is unsafe"
        )
    return fd_info.st_dev, fd_info.st_ino


def default_lock_path() -> Path:
    """Return the fixed global lock pathname.

    This function deliberately does not consult ``XDG_RUNTIME_DIR``,
    ``RUNNER_TEMP`` or any other caller-controlled environment variable.
    """

    return LOCK_PATH


def _ensure_provision_parent() -> None:
    if os.geteuid() != ROOT_UID:
        raise VerificationLockError(
            "verification lock provisioning requires root; run the CI provisioning step"
        )
    # Validate the anchor before creating anything below it.  /run/lock is
    # sticky, but an attacker may still pre-create the fixed child path.
    _validate_existing_chain(LOCK_PARENT.parent)
    parent = LOCK_PATH.parent
    created_parent = False
    try:
        os.mkdir(parent, ROOT_DIR_MODE)
        created_parent = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise VerificationLockError(
            f"unable to provision verification lock parent: {parent}"
        ) from exc
    # Never repair an existing path.  A wrong owner/mode is an explicit gate
    # failure, not an invitation to chmod a path an attacker may have swapped.
    if created_parent:
        # Make the owner and mode explicit even when the root process has a
        # restrictive umask.  This is only done for the path just created;
        # pre-existing paths are validated and never repaired.
        os.chown(parent, ROOT_UID, ROOT_UID)
        os.chmod(parent, ROOT_DIR_MODE)
    _lstat_directory(parent, expected_mode=ROOT_DIR_MODE)


def provision_verification_lock() -> Path:
    """Atomically provision and validate the root-owned global lock file."""

    _ensure_provision_parent()
    lock_path = LOCK_PATH
    before = _parent_chain(lock_path)
    flags = os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    created_file = False
    try:
        fd = os.open(lock_path, flags, LOCK_FILE_MODE)
        created_file = True
    except FileExistsError:
        # Existing files are never replaced or repaired.  Open below only
        # validates the identity and explicit root-owned mode.
        try:
            fd = os.open(lock_path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise VerificationLockError(
                f"unable to validate provisioned verification lock: {lock_path}"
            ) from exc
    except OSError as exc:
        raise VerificationLockError(
            f"unable to provision verification lock: {lock_path}"
        ) from exc
    try:
        if created_file:
            os.fchown(fd, ROOT_UID, ROOT_UID)
            os.fchmod(fd, LOCK_FILE_MODE)
        _validate_lock_file(lock_path, fd)
        if before != _parent_chain(lock_path):
            raise VerificationLockError(
                "verification lock parent identity changed during provisioning"
            )
        os.fsync(fd)
    finally:
        os.close(fd)
    return lock_path


def _open_validated_lock(lock_path: Path) -> tuple[int, tuple[tuple[Path, int, int], ...], int, int]:
    """Open the preprovisioned file and revalidate path identity after open."""

    try:
        before = _parent_chain(lock_path)
    except VerificationLockError as exc:
        if not lock_path.parent.exists():
            raise VerificationLockError(
                "LOCAL GATE BLOCKED: verification lock is not provisioned; "
                f"run the root provisioning step for {lock_path}"
            ) from exc
        raise
    try:
        fd = os.open(
            lock_path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError as exc:
        raise VerificationLockError(
            "LOCAL GATE BLOCKED: verification lock is not provisioned; "
            f"run the root provisioning step for {lock_path}"
        ) from exc
    except OSError as exc:
        raise VerificationLockError(f"unable to open verification lock: {exc}") from exc
    try:
        _dev, inode = _validate_lock_file(lock_path, fd)
        after = _parent_chain(lock_path)
        if before != after:
            raise VerificationLockError(
                "verification lock parent identity changed during open"
            )
        return fd, after, _dev, inode
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def verification_resource_lock(contour: str) -> Iterator[None]:
    """Exclusively run one schema/resource-mutating verification contour."""

    if contour not in LOCK_CONTOURS:
        yield
        return
    lock_path = default_lock_path()
    fd, parent_identity, _dev, _inode = _open_validated_lock(lock_path)
    locked = False
    original_handlers: dict[int, object] = {}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise VerificationLockError(
                    "verification lock contention; refusing concurrent resource contours "
                    f"({lock_path})"
                ) from exc
            raise VerificationLockError("unable to acquire verification lock") from exc
        locked = True
        # Revalidate after acquisition as well as after open.  No mutable
        # marker is written: the root-owned inode is the only security identity.
        if parent_identity != _parent_chain(lock_path):
            raise VerificationLockError(
                "verification lock parent identity changed before acquisition"
            )
        _validate_lock_file(lock_path, fd)

        def _signal_handler(signum: int, _frame: object) -> None:
            raise _LockSignal(signum)

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            original_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _signal_handler)
        yield
    finally:
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provision",
        action="store_true",
        help="atomically provision the fixed root-owned global lock (root only)",
    )
    args = parser.parse_args()
    if not args.provision:
        parser.error("--provision is required")
    try:
        path = provision_verification_lock()
    except VerificationLockError as exc:
        print(f"LOCAL GATE BLOCKED: {exc}", file=sys.stderr)
        return 2
    info = os.stat(path, follow_symlinks=False)
    print(
        f"Provisioned verification lock: {path} "
        f"(device={info.st_dev} inode={info.st_ino} owner={info.st_uid} "
        f"mode={stat.S_IMODE(info.st_mode):04o})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
