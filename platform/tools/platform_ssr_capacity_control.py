"""Private closed-schema control channel for the SSR capacity diagnostic only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Any, Mapping


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
REASONS = frozenset({
    "none", "memory_floor", "memory_event", "identity_changed",
    "collection_error", "observer_failed",
})
CONTROL_FIELDS = frozenset({
    "schema", "run_id", "source_sha", "app_target_sha", "profile_digest",
    "external_run_id", "external_run_attempt", "decision", "reason", "sequence",
    "updated_monotonic_ns",
})
MAX_CONTROL_BYTES = 2048
MAX_CONTROL_AGE_SECONDS = 1.5
CONTROL_READ_ATTEMPTS = 2


class CapacityControlError(ValueError):
    """The parent-to-worker control record is absent, stale, or inconsistent."""


class _ControlPathReplaced(CapacityControlError):
    """An atomic heartbeat replacement crossed one control-file snapshot."""


def write_control(
    path: Path,
    payload: Mapping[str, Any],
    *,
    expected: Mapping[str, Any],
    now_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    """Atomically publish one parent-owned control decision in a private dir."""

    try:
        parent = path.parent.lstat()
    except OSError as exc:
        raise CapacityControlError("control directory unavailable") from exc
    if (not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_nlink < 2):
        raise CapacityControlError("control directory metadata invalid")
    try:
        existing = path.lstat()
        if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()
                or existing.st_nlink != 1 or stat.S_IMODE(existing.st_mode) != 0o600
                or existing.st_size > MAX_CONTROL_BYTES):
            raise CapacityControlError("existing control file metadata invalid")
    except FileNotFoundError:
        existing = None
    except OSError as exc:
        raise CapacityControlError("control destination unavailable") from exc
    result = validate_control(payload, expected=expected, now_monotonic_ns=now_monotonic_ns)
    raw = canonical_json(result)
    if len(raw) > MAX_CONTROL_BYTES:
        raise CapacityControlError("control record too large")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current_parent = path.parent.lstat()
        if (current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino):
            raise CapacityControlError("control directory changed")
        if existing is not None:
            current_target = path.lstat()
            if (current_target.st_dev, current_target.st_ino) != (existing.st_dev, existing.st_ino):
                raise CapacityControlError("control target changed")
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return result


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=True, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii") + b"\n"


def validate_control(
    payload: Any,
    *,
    expected: Mapping[str, Any],
    now_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != CONTROL_FIELDS:
        raise CapacityControlError("control schema mismatch")
    if type(payload.get("schema")) is not int or payload["schema"] != 1:
        raise CapacityControlError("control schema mismatch")
    if type(payload.get("sequence")) is not int or payload["sequence"] < 0:
        raise CapacityControlError("control sequence invalid")
    if type(payload.get("updated_monotonic_ns")) is not int or payload["updated_monotonic_ns"] <= 0:
        raise CapacityControlError("control clock invalid")
    if payload.get("decision") not in {"continue", "abort", "complete"}:
        raise CapacityControlError("control decision invalid")
    if payload.get("reason") not in REASONS:
        raise CapacityControlError("control reason invalid")
    if ((payload["decision"] in {"continue", "complete"} and payload["reason"] != "none")
            or (payload["decision"] == "abort" and payload["reason"] == "none")):
        raise CapacityControlError("control abort is invalid")
    for key, regex in (
        ("run_id", RUN_ID_RE), ("source_sha", SHA_RE),
        ("app_target_sha", SHA_RE), ("profile_digest", DIGEST_RE),
        ("external_run_id", RUN_NUMBER_RE), ("external_run_attempt", RUN_NUMBER_RE),
    ):
        if not isinstance(payload.get(key), str) or regex.fullmatch(payload[key]) is None:
            raise CapacityControlError("control binding invalid")
        if payload[key] != expected.get(key):
            raise CapacityControlError("control binding mismatch")
    now = time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
    age = now - payload["updated_monotonic_ns"]
    if age < 0 or age > MAX_CONTROL_AGE_SECONDS * 1_000_000_000:
        raise CapacityControlError("control record is stale")
    return payload


def _read_control_once(
    path: Path,
    *,
    expected: Mapping[str, Any],
    previous_sequence: int = -1,
    now_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or before.st_uid != os.geteuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size <= 0 or before.st_size > MAX_CONTROL_BYTES):
            raise CapacityControlError("control file metadata invalid")
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise CapacityControlError("control file unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise _ControlPathReplaced("control path replaced during open")
        if ((opened.st_mode, opened.st_uid, opened.st_nlink, opened.st_size)
                != (before.st_mode, before.st_uid, before.st_nlink, before.st_size)):
            raise CapacityControlError("control file changed")
        raw = bytearray()
        while len(raw) <= MAX_CONTROL_BYTES:
            chunk = os.read(fd, min(1024, MAX_CONTROL_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        finished = os.fstat(fd)
        after = path.lstat()
    finally:
        os.close(fd)
    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
        raise _ControlPathReplaced("control path replaced during read")
    if (len(raw) > MAX_CONTROL_BYTES or not raw.endswith(b"\n")
            or raw.count(b"\n") != 1
            or (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
        raise CapacityControlError("control file changed")
    try:
        payload = json.loads(bytes(raw[:-1]).decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CapacityControlError("control JSON invalid") from exc
    if canonical_json(payload) != bytes(raw):
        raise CapacityControlError("control JSON noncanonical")
    result = validate_control(payload, expected=expected, now_monotonic_ns=now_monotonic_ns)
    if result["sequence"] < previous_sequence:
        raise CapacityControlError("control sequence moved backwards")
    return result


def read_control(
    path: Path,
    *,
    expected: Mapping[str, Any],
    previous_sequence: int = -1,
    now_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    """Read one fresh control snapshot, retrying one atomic path replacement."""

    for attempt in range(CONTROL_READ_ATTEMPTS):
        try:
            return _read_control_once(
                path,
                expected=expected,
                previous_sequence=previous_sequence,
                now_monotonic_ns=now_monotonic_ns,
            )
        except _ControlPathReplaced:
            if attempt + 1 == CONTROL_READ_ATTEMPTS:
                raise CapacityControlError("control path changed during snapshot") from None
    raise CapacityControlError("control snapshot unavailable")


def stop_reason_from_control(
    path: Path,
    *,
    expected: Mapping[str, Any],
    previous_sequence: int,
    now_monotonic_ns: int | None = None,
) -> tuple[str | None, int]:
    """Map one fresh parent decision into the closed-loop worker stop API."""

    try:
        record = read_control(
            path,
            expected=expected,
            previous_sequence=previous_sequence,
            now_monotonic_ns=now_monotonic_ns,
        )
    except (OSError, CapacityControlError):
        return "control_file_invalid", previous_sequence
    decision = record["decision"]
    if decision == "abort":
        return record["reason"], record["sequence"]
    if decision == "complete":
        return "window_complete", record["sequence"]
    return None, record["sequence"]
