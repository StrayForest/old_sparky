#!/usr/bin/env python3
"""Bounded extraction for data-only CI ZIP handoffs."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import zipfile


MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 8
MAX_MEMBER_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_COMPRESSION_RATIO = 100


class UnsafeZipError(ValueError):
    """A ZIP handoff is malformed, ambiguous or too large."""


def _reject(condition: bool, message: str) -> None:
    if condition:
        raise UnsafeZipError(message)


def extract_single_manifest(archive_path: Path, destination: Path) -> Path:
    """Extract exactly one regular classifier manifest with bounded output."""

    try:
        archive_metadata = archive_path.lstat()
    except OSError as exc:
        raise UnsafeZipError("classifier artifact archive is unavailable") from exc
    _reject(
        stat.S_ISLNK(archive_metadata.st_mode)
        or not stat.S_ISREG(archive_metadata.st_mode)
        or archive_metadata.st_nlink != 1,
        "classifier artifact archive is not a regular file",
    )
    archive_size = archive_metadata.st_size
    _reject(archive_size > MAX_ARCHIVE_BYTES, "classifier artifact ZIP is oversized")
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise UnsafeZipError("classifier artifact extraction directory is unsafe") from exc

    try:
        with zipfile.ZipFile(archive_path, allowZip64=False) as archive:
            members = archive.infolist()
            _reject(len(members) == 0, "classifier artifact ZIP is empty")
            _reject(len(members) > MAX_ENTRIES, "classifier artifact ZIP has too many entries")
            names = [info.filename for info in members]
            _reject(len(set(names)) != len(names), "classifier artifact ZIP has duplicate entries")
            _reject(names != ["classifier-manifest.json"], "classifier artifact ZIP entry set is invalid")
            info = members[0]
            name = info.filename
            _reject(not name.isascii(), "classifier artifact ZIP name is not ASCII")
            _reject(
                any(ord(char) < 0x20 or ord(char) == 0x7F for char in name),
                "classifier artifact ZIP name contains control characters",
            )
            _reject("\\" in name, "classifier artifact ZIP name contains a backslash")
            _reject(name.startswith("/"), "classifier artifact ZIP name is absolute")
            parts = Path(name).parts
            _reject(".." in parts or "." in parts, "classifier artifact ZIP name traverses")
            _reject(info.is_dir() or name.endswith("/"), "classifier artifact ZIP entry is a directory")
            _reject(info.file_size > MAX_MEMBER_BYTES, "classifier artifact ZIP member is oversized")
            _reject(info.file_size > MAX_TOTAL_BYTES, "classifier artifact ZIP is oversized")
            _reject(info.compress_size == 0 and info.file_size != 0, "classifier artifact ZIP ratio is invalid")
            if info.file_size and info.compress_size:
                _reject(
                    info.file_size > info.compress_size * MAX_COMPRESSION_RATIO,
                    "classifier artifact ZIP compression ratio is unsafe",
                )
            _reject(bool(info.flag_bits & 0x1), "classifier artifact ZIP is encrypted")
            mode = (info.external_attr >> 16) & 0o170000
            _reject(mode not in (0, stat.S_IFREG), "classifier artifact ZIP entry is not regular")

            target = destination / name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            try:
                descriptor = os.open(target, flags, 0o600)
            except OSError as exc:
                raise UnsafeZipError("classifier manifest destination is unsafe") from exc
            try:
                copied = 0
                with archive.open(info, "r") as source, os.fdopen(descriptor, "wb") as output:
                    descriptor = -1
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        copied += len(chunk)
                        _reject(copied > MAX_MEMBER_BYTES, "classifier artifact member expanded beyond limit")
                        output.write(chunk)
                _reject(copied != info.file_size, "classifier artifact member size changed")
            except Exception:
                try:
                    target.unlink()
                except OSError:
                    pass
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    except (OSError, EOFError, OverflowError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        if isinstance(exc, UnsafeZipError):
            raise
        raise UnsafeZipError("classifier artifact ZIP is invalid") from exc

    try:
        metadata = (destination / "classifier-manifest.json").lstat()
    except OSError as exc:
        raise UnsafeZipError("classifier manifest extraction is incomplete") from exc
    _reject(
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600,
        "classifier manifest extraction is unsafe",
    )
    return destination / "classifier-manifest.json"


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        raise SystemExit(2)
    extract_single_manifest(Path(sys.argv[1]), Path(sys.argv[2]))
