#!/usr/bin/env python3
"""The single owner of the live-QA machine-wide filesystem lock."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat


MACHINE_LOCK_PATH = Path("/run/lock/oldsparky-liveqa.lock")


class GuardError(RuntimeError):
    """A deliberately non-sensitive live-QA guard failure."""


def _open_machine_lock_directory(lock_path: Path) -> int:
    lock_parent = lock_path.parent
    descriptor: int | None = None
    try:
        path_metadata = lock_parent.lstat()
        descriptor = os.open(
            lock_parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GuardError("live QA machine lock directory is unavailable") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != 0
        or (opened.st_dev, opened.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
        or (stat.S_IMODE(opened.st_mode) & 0o022 and not opened.st_mode & stat.S_ISVTX)
    ):
        os.close(descriptor)
        raise GuardError("live QA machine lock directory metadata is unsafe")
    return descriptor


def open_machine_lock(lock_path: Path = MACHINE_LOCK_PATH) -> int:
    """Open, validate and exclusively acquire the canonical machine lock."""

    parent_fd = _open_machine_lock_directory(lock_path)
    try:
        descriptor = os.open(
            lock_path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
    finally:
        os.close(parent_fd)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise GuardError("live QA lock file metadata is unsafe")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise GuardError("another live QA operation holds the machine lock") from exc
    return descriptor
