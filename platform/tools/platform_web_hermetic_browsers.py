#!/usr/bin/env python3
"""Provision the pinned Chromium browsers for the hermetic web contour."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

try:
    import platform_live_qa_guard as guard
except ModuleNotFoundError:  # Imported as ``tools.platform_web_hermetic_browsers``.
    from tools import platform_live_qa_guard as guard


HERMETIC_ARCHIVE_NAMES = (
    "chromium-1228",
    "chromium_headless_shell-1228",
    "ffmpeg-1011",
)
HERMETIC_BROWSER_ROOTS = frozenset(HERMETIC_ARCHIVE_NAMES)
EXPECTED_EXECUTABLES = {
    "chromium-1228/chrome-linux64/chrome",
    "chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell",
    "ffmpeg-1011/ffmpeg-linux",
}


def _assert_fresh_output(output: Path) -> None:
    if not output.is_absolute():
        raise guard.GuardError("hermetic browser output must be absolute")
    try:
        parent = output.parent.resolve(strict=True)
        parent_metadata = output.parent.lstat()
    except OSError as exc:
        raise guard.GuardError("hermetic browser output parent is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or output.parent != parent
        or output.name in {"", ".", ".."}
    ):
        raise guard.GuardError("hermetic browser output parent is unsafe")
    if output.exists() or output.is_symlink():
        raise guard.GuardError("hermetic browser output must be fresh")


@contextmanager
def _exclusive_output_parent(parent: Path):
    """Serialize promotions in one run directory without a lock artifact."""

    try:
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise guard.GuardError("hermetic browser output parent is unavailable") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise guard.GuardError("hermetic browser output parent is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _selected_archives() -> tuple[tuple[str, str, str, int], ...]:
    """Return exactly the Chromium/Headless/ffmpeg records from the guard."""

    selected = tuple(
        archive
        for archive in guard.PLAYWRIGHT_ARCHIVES
        if archive[0] in HERMETIC_BROWSER_ROOTS
    )
    if tuple(archive[0] for archive in selected) != HERMETIC_ARCHIVE_NAMES:
        raise guard.GuardError("pinned hermetic browser archives are incomplete")
    for directory_name, url, checksum, byte_size in selected:
        if (
            not directory_name
            or not isinstance(url, str)
            or not url.startswith("https://")
            or not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
            or not isinstance(byte_size, int)
            or byte_size <= 0
        ):
            raise guard.GuardError("pinned hermetic browser archive metadata is invalid")
    return selected


def _assert_inventory(root: Path) -> None:
    """Prove the promoted root has only the lock-selected browser packages."""

    try:
        entries = {entry.name: entry for entry in root.iterdir()}
    except OSError as exc:
        raise guard.GuardError("pinned hermetic browser inventory is unavailable") from exc
    if set(entries) != HERMETIC_BROWSER_ROOTS:
        raise guard.GuardError("pinned hermetic browser inventory is incomplete")
    for directory_name in HERMETIC_ARCHIVE_NAMES:
        metadata = entries[directory_name].lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise guard.GuardError("pinned hermetic browser inventory is unsafe")
        marker = entries[directory_name] / "INSTALLATION_COMPLETE"
        marker_metadata = marker.lstat()
        if (
            not stat.S_ISREG(marker_metadata.st_mode)
            or stat.S_ISLNK(marker_metadata.st_mode)
            or marker_metadata.st_nlink != 1
        ):
            raise guard.GuardError("pinned hermetic browser installation marker is unavailable")
    for relative in EXPECTED_EXECUTABLES:
        executable = root / relative
        metadata = executable.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise guard.GuardError("pinned hermetic browser executable is unavailable")


def provision(web_root: Path, output: Path) -> None:
    """Install the lock-selected Chromium archives into a fresh run directory."""

    web_root = web_root.resolve(strict=True)
    if not web_root.is_dir():
        raise guard.GuardError("hermetic web root is unavailable")
    guard._assert_playwright_revision(web_root)
    _assert_fresh_output(output)
    selected = _selected_archives()
    with _exclusive_output_parent(output.parent):
        _assert_fresh_output(output)
        stage = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
        )
        os.chmod(stage, 0o700)
        promoted = False
        try:
            for directory_name, url, checksum, byte_size in selected:
                guard._download_pinned_zip(
                    url,
                    checksum,
                    byte_size,
                    stage / directory_name,
                )
            _assert_inventory(stage)
            if output.exists() or output.is_symlink():
                raise guard.GuardError("hermetic browser output must remain fresh")
            os.rename(stage, output)
            promoted = True
        finally:
            if not promoted and stage.exists() and not stage.is_symlink():
                shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        provision(args.web_root, args.output)
    except (OSError, guard.GuardError, ValueError) as exc:
        print(
            f"WEB_HERMETIC_BROWSERS status=failed reason={type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print("WEB_HERMETIC_BROWSERS status=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
