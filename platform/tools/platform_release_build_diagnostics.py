#!/usr/bin/env python3
"""Extract one bounded, fail-closed release-builder diagnostic marker."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


MAX_LOG_BYTES = 4 * 1024 * 1024
MAX_MARKER_BYTES = 1024
MARKER_PREFIX = "RELEASE_BUILD_PHASE"
OUTPUT_PREFIX = "RELEASE_BUILD_DIAGNOSTIC"
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
PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}
STATUSES = frozenset({"passed", "failed"})
REASONS = frozenset({"ok", "build_failed", "cleanup_failed", "interrupted"})
CLEANUP_STATES = frozenset({"not-run", "passed", "failed"})
REQUIRED_FIELDS = frozenset({"schema", "phase", "status", "reason", "cleanup"})
OPTIONAL_FIELDS = frozenset({"failed_phase", "source_sha", "artifact_sha256"})


class DiagnosticError(ValueError):
    """The log is not a trusted release-builder telemetry stream."""


@dataclass(frozen=True)
class Marker:
    phase: str
    status: str
    reason: str
    cleanup: str
    failed_phase: str | None = None
    source_sha: str | None = None
    artifact_sha256: str | None = None

    def normalized(self) -> str:
        fields = [
            "schema=1",
            f"phase={self.phase}",
            f"status={self.status}",
            f"reason={self.reason}",
            f"cleanup={self.cleanup}",
        ]
        if self.failed_phase is not None:
            fields.append(f"failed_phase={self.failed_phase}")
        if self.source_sha is not None:
            fields.append(f"source_sha={self.source_sha}")
        if self.artifact_sha256 is not None:
            fields.append(f"artifact_sha256={self.artifact_sha256}")
        return f"{OUTPUT_PREFIX} " + " ".join(fields)


def _safe_failure() -> str:
    return (
        f"{OUTPUT_PREFIX} schema=1 phase=unknown status=failed "
        "reason=build_failed cleanup=unknown"
    )


def _read_bounded_log(path: Path) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise DiagnosticError from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > MAX_LOG_BYTES
    ):
        raise DiagnosticError

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DiagnosticError from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_nlink)
            != (before.st_dev, before.st_ino, before.st_uid, before.st_nlink)
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > MAX_LOG_BYTES
        ):
            raise DiagnosticError
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_LOG_BYTES:
                raise DiagnosticError
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_uid, after.st_nlink)
            != (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_nlink)
            or after.st_gid != 0
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_size != len(payload)
        ):
            raise DiagnosticError
        return bytes(payload)
    except OSError as exc:
        raise DiagnosticError from exc
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            raise DiagnosticError from exc


def _parse_marker(line: str) -> Marker:
    try:
        encoded = line.encode("ascii")
    except UnicodeEncodeError as exc:
        raise DiagnosticError from exc
    if len(encoded) > MAX_MARKER_BYTES or line != line.strip(" "):
        raise DiagnosticError
    tokens = line.split(" ")
    if not tokens or tokens[0] != MARKER_PREFIX:
        raise DiagnosticError
    fields: dict[str, str] = {}
    for token in tokens[1:]:
        if "=" not in token:
            raise DiagnosticError
        key, value = token.split("=", 1)
        if not key or key in fields or key not in REQUIRED_FIELDS | OPTIONAL_FIELDS:
            raise DiagnosticError
        if not value or any(character in value for character in ("\t", "\r", "\n")):
            raise DiagnosticError
        fields[key] = value
    if set(fields) != REQUIRED_FIELDS | (set(fields) & OPTIONAL_FIELDS):
        raise DiagnosticError
    if fields.get("schema") != "1":
        raise DiagnosticError
    phase = fields.get("phase", "")
    status = fields.get("status", "")
    reason = fields.get("reason", "")
    cleanup = fields.get("cleanup", "")
    if phase not in PHASE_INDEX or status not in STATUSES or reason not in REASONS:
        raise DiagnosticError
    if cleanup not in CLEANUP_STATES:
        raise DiagnosticError

    failed_phase = fields.get("failed_phase")
    source_sha = fields.get("source_sha")
    artifact_sha256 = fields.get("artifact_sha256")
    if failed_phase is not None and failed_phase not in PHASE_INDEX:
        raise DiagnosticError
    if source_sha is not None and not all(
        character in "0123456789abcdef" for character in source_sha
    ):
        raise DiagnosticError
    if source_sha is not None and len(source_sha) != 40:
        raise DiagnosticError
    if artifact_sha256 is not None or artifact_sha256 == "":
        if artifact_sha256 is None or len(artifact_sha256) != 64 or not all(
            character in "0123456789abcdef" for character in artifact_sha256
        ):
            raise DiagnosticError

    if status == "passed":
        if reason != "ok" or failed_phase is not None:
            raise DiagnosticError
        if phase == "complete":
            if cleanup != "passed" or source_sha is None or artifact_sha256 is None:
                raise DiagnosticError
        elif phase == "cleanup":
            if cleanup != "passed" or source_sha is not None or artifact_sha256 is not None:
                raise DiagnosticError
        elif cleanup != "not-run" or source_sha is not None or artifact_sha256 is not None:
            raise DiagnosticError
    else:
        if reason == "ok" or source_sha is not None or artifact_sha256 is not None:
            raise DiagnosticError
        if phase == "complete":
            if cleanup not in {"passed", "failed"} or failed_phase is None:
                raise DiagnosticError
        elif phase == "cleanup":
            if cleanup != "failed" or reason != "cleanup_failed":
                raise DiagnosticError
        elif cleanup not in {"passed", "failed"}:
            raise DiagnosticError

    return Marker(
        phase=phase,
        status=status,
        reason=reason,
        cleanup=cleanup,
        failed_phase=failed_phase,
        source_sha=source_sha,
        artifact_sha256=artifact_sha256,
    )


def extract(path: Path) -> Marker:
    payload = _read_bounded_log(path)
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in payload):
        raise DiagnosticError
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiagnosticError from exc

    markers: list[Marker] = []
    previous_index = -1
    seen_phases: set[str] = set()
    for line in text.splitlines():
        if not line.startswith(MARKER_PREFIX):
            continue
        marker = _parse_marker(line)
        current_index = PHASE_INDEX[marker.phase]
        if marker.phase in seen_phases or current_index <= previous_index:
            raise DiagnosticError
        seen_phases.add(marker.phase)
        previous_index = current_index
        markers.append(marker)
    if not markers or markers[-1].phase != "complete":
        raise DiagnosticError
    return markers[-1]


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--log":
        print(_safe_failure())
        return 2
    try:
        marker = extract(Path(argv[1]))
    except (DiagnosticError, OSError, ValueError):
        print(_safe_failure())
        return 1
    print(marker.normalized())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
