#!/usr/bin/env python3
"""Safely append the canonical release-builder phase telemetry stream."""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
from pathlib import Path


MAX_MARKER_BYTES = 1024
MAX_STREAM_BYTES = 16 * 1024
MARKER_PREFIX = "RELEASE_BUILD_PHASE"
PHASES = (
    "canonical-preflight",
    "node-runtime",
    "source-stage",
    "web-dependencies",
    "live-qa-runtime",
    "python-wheelhouse",
    "dependency-baseline",
    "web-build",
    "release-metadata",
    "artifact-promote",
    "artifact-validate",
    "cleanup",
    "complete",
)
STATUSES = frozenset({"passed", "failed"})
REASONS = frozenset({"ok", "build_failed", "cleanup_failed", "interrupted"})
CLEANUP_STATES = frozenset({"not-run", "passed", "failed"})
BUILD_PHASES = PHASES[:-2]
SOURCE_SHA_LENGTHS = frozenset({40, 64})
REQUIRED_FIELDS = frozenset({"schema", "phase", "status", "reason", "cleanup"})
OPTIONAL_FIELDS = frozenset({"failed_phase", "source_sha", "artifact_sha256"})


class TelemetryError(ValueError):
    """The marker stream path or marker is not trusted."""


def _metadata(path: Path, *, allow_nonempty: bool) -> os.stat_result:
    try:
        parent = path.parent
        parent_metadata = parent.lstat()
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_ISLNK(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_gid != 0
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or parent.resolve(strict=True) != parent
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or path.resolve(strict=True) != path
            or metadata.st_size > MAX_STREAM_BYTES
            or (not allow_nonempty and metadata.st_size != 0)
        ):
            raise TelemetryError
        return metadata
    except FileNotFoundError as exc:
        raise TelemetryError from exc
    except OSError as exc:
        raise TelemetryError from exc


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
    )


def _open_checked(path: Path) -> tuple[int, os.stat_result]:
    before = _metadata(path, allow_nonempty=True)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise TelemetryError from exc
    except OSError as exc:
        raise TelemetryError from exc
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise TelemetryError
        return descriptor, opened
    except OSError as exc:
        os.close(descriptor)
        raise TelemetryError from exc
    except BaseException:
        os.close(descriptor)
        raise


def _validate_marker(line: str) -> bytes:
    try:
        encoded = line.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TelemetryError from exc
    if (
        not line.startswith(f"{MARKER_PREFIX} ")
        or len(encoded) > MAX_MARKER_BYTES
        or line != line.strip(" ")
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in line)
    ):
        raise TelemetryError
    tokens = line.split(" ")
    fields: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise TelemetryError
        key, value = token.split("=", 1)
        if not key or key in fields or key not in REQUIRED_FIELDS | OPTIONAL_FIELDS:
            raise TelemetryError
        if not value:
            raise TelemetryError
        fields[key] = value
    if set(fields) != REQUIRED_FIELDS | (set(fields) & OPTIONAL_FIELDS):
        raise TelemetryError
    if fields.get("schema") != "1":
        raise TelemetryError
    phase = fields.get("phase", "")
    status = fields.get("status", "")
    reason = fields.get("reason", "")
    cleanup = fields.get("cleanup", "")
    if phase not in PHASES or status not in STATUSES or reason not in REASONS:
        raise TelemetryError
    if cleanup not in CLEANUP_STATES:
        raise TelemetryError

    failed_phase = fields.get("failed_phase")
    source_sha = fields.get("source_sha")
    artifact_sha256 = fields.get("artifact_sha256")
    if failed_phase is not None and failed_phase not in PHASES:
        raise TelemetryError
    if source_sha is not None and (
        len(source_sha) not in SOURCE_SHA_LENGTHS
        or not all(character in "0123456789abcdef" for character in source_sha)
    ):
        raise TelemetryError
    if artifact_sha256 is not None and (
        len(artifact_sha256) != 64
        or not all(character in "0123456789abcdef" for character in artifact_sha256)
    ):
        raise TelemetryError

    if status == "passed":
        if reason != "ok" or failed_phase is not None:
            raise TelemetryError
        if phase == "complete":
            if cleanup != "passed" or source_sha is None or artifact_sha256 is None:
                raise TelemetryError
        elif phase == "cleanup":
            if cleanup != "passed" or source_sha is not None or artifact_sha256 is not None:
                raise TelemetryError
        elif cleanup != "not-run" or source_sha is not None or artifact_sha256 is not None:
            raise TelemetryError
    else:
        if reason == "ok" or source_sha is not None or artifact_sha256 is not None:
            raise TelemetryError
        if phase == "complete":
            if cleanup not in {"passed", "failed"} or failed_phase not in BUILD_PHASES:
                raise TelemetryError
        elif phase == "cleanup":
            if (
                cleanup != "failed"
                or reason != "cleanup_failed"
                or failed_phase not in BUILD_PHASES
            ):
                raise TelemetryError
        elif cleanup not in {"passed", "failed"}:
            raise TelemetryError
    return encoded + b"\n"


def check(path: Path) -> None:
    descriptor, opened = _open_checked(path)
    try:
        if opened.st_size != 0:
            raise TelemetryError
    finally:
        os.close(descriptor)


def append(path: Path, marker: str) -> None:
    payload = _validate_marker(marker)
    if len(payload) > MAX_MARKER_BYTES + 1:
        raise TelemetryError
    descriptor, opened = _open_checked(path)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = os.fstat(descriptor)
        if _identity(locked) != _identity(opened):
            raise TelemetryError
        if locked.st_size + len(payload) > MAX_STREAM_BYTES:
            raise TelemetryError
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise TelemetryError
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if (
            _identity(after)[:6] != _identity(locked)[:6]
            or after.st_size != locked.st_size + len(payload)
        ):
            raise TelemetryError
    except OSError as exc:
        raise TelemetryError from exc
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--marker")
    try:
        args = parser.parse_args(argv)
        if args.check == args.append or (args.check and args.marker is not None) or (
            args.append and args.marker is None
        ):
            return 1
        if args.check:
            check(args.log)
        else:
            append(args.log, args.marker)
    except (TelemetryError, OSError, ValueError, SystemExit):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
