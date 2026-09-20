#!/usr/bin/env python3
"""Create the fixed production artifact directory without pathname traversal.

This helper is installed in the immutable production release and is invoked
through the fixed remote dispatcher under ``sudo``.  It accepts only the
canonical ``/tmp/old-sparky-platform-artifact-<run>-<attempt>`` shape, opens
each component with ``O_NOFOLLOW``, creates the leaf with ``mkdirat``-style
directory-relative operations, and verifies the inode after creation.  A
workflow caller can race a pathname check; it cannot turn this helper's
directory-relative open into a symlink traversal.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


ARTIFACT_RE = re.compile(
    r"^/tmp/old-sparky-platform-artifact-[1-9][0-9]{0,31}-[1-9][0-9]{0,31}$"
)
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _safe_component(metadata: os.stat_result, *, mode: int | None = None) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink < 2
        or metadata.st_uid != 0
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
        or (mode is None and stat.S_IMODE(metadata.st_mode) & 0o022)
    ):
        raise RuntimeError("artifact directory component is unsafe")


def _open_tmp() -> int:
    root_metadata = os.lstat("/")
    _safe_component(root_metadata)
    tmp_metadata = os.lstat("/tmp")
    _safe_component(tmp_metadata, mode=0o1777)
    descriptor = os.open("/tmp", DIR_FLAGS)
    try:
        metadata = os.fstat(descriptor)
        # /tmp is the only parent accepted by the canonical input contract.
        # Its sticky bit is part of the host boundary; group/world write is
        # expected only with that sticky bit set.
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o1777
            or (metadata.st_dev, metadata.st_ino)
            != (tmp_metadata.st_dev, tmp_metadata.st_ino)
        ):
            raise RuntimeError("artifact directory parent is unsafe")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def prepare(path: str) -> None:
    if not isinstance(path, str) or "\x00" in path or ARTIFACT_RE.fullmatch(path) is None:
        raise ValueError("artifact directory path is invalid")
    leaf = Path(path).name
    parent_fd = _open_tmp()
    try:
        before_parent = os.fstat(parent_fd)
        leaf_path = Path("/tmp") / leaf
        try:
            preflight = os.lstat(leaf_path)
            existing = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (
                preflight.st_dev != existing.st_dev
                or preflight.st_ino != existing.st_ino
                or preflight.st_mode != existing.st_mode
            ):
                raise RuntimeError("artifact directory leaf changed during preflight")
        except FileNotFoundError:
            os.mkdir(leaf, 0o700, dir_fd=parent_fd)
            existing = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        else:
            # Existing directories are accepted only when they already have
            # the exact root-owned private identity expected by deployment.
            _safe_component(existing, mode=0o700)

        child_fd = os.open(leaf, DIR_FLAGS, dir_fd=parent_fd)
        try:
            after_parent = os.fstat(parent_fd)
            current = os.fstat(child_fd)
            if (
                before_parent.st_dev != after_parent.st_dev
                or before_parent.st_ino != after_parent.st_ino
                or existing.st_dev != current.st_dev
                or existing.st_ino != current.st_ino
            ):
                raise RuntimeError("artifact directory identity changed")
            _safe_component(current, mode=0o700)
        finally:
            os.close(child_fd)
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 2
    try:
        prepare(arguments[0])
    except (OSError, RuntimeError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
