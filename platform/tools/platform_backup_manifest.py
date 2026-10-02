#!/usr/bin/env python3
"""Canonical version-3 contract for local platform database backup manifests.

The backup creator and every local consumer use this module for the same
closed, versioned contract.  Version 2 is accepted only as a read-only
legacy shape; writers always emit version 3 with explicit cleanup fields.
The retired singular ``schema`` field is rejected rather than being silently
interpreted as the ordered ``schemas`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Mapping


MANIFEST_FORMAT_VERSION = 3
LEGACY_MANIFEST_FORMAT_VERSION = 2
DATABASE_NAME = "platformdb"
EXPECTED_SCHEMAS: tuple[str, ...] = ("platform", "public")
REQUIRED_EXTENSIONS: tuple[str, ...] = ("pg_trgm",)
RUN_ID_RE = re.compile(r"^[a-f0-9]{32}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
BACKUP_NAME_RE = re.compile(
    r"^platformdb-(?P<timestamp>\d{8}T\d{6}Z)-(?P<run_id>[a-f0-9]{32})\.dump$"
)
MAX_MANIFEST_BYTES = 128 * 1024
MAX_DUMP_BYTES = 5 * 1024 * 1024 * 1024
RESTORE_DRILL_DATABASE_ID_RE = re.compile(r"^platform_restore_drill_[0-9a-f]{32}$")
CLEANUP_OPERATOR_ACTION = "inspect_ownership_before_drop"
RESTORE_ERROR_CODES = frozenset(
    {
        "cleanup_unproven",
        "backup_command_timeout",
        "backup_monitor_unavailable",
        "restore_verification_failed",
    }
)

# This is the one current top-level shape.  Keep the order stable in creator
# output; parsers intentionally do not make JSON object member order semantic.
MANIFEST_KEYS: tuple[str, ...] = (
    "format_version",
    "database",
    "schemas",
    "required_extensions",
    "run_id",
    "dump_file",
    "size_bytes",
    "sha256",
    "started_at_utc",
    "completed_at_utc",
    "duration_seconds",
    "restore_verified",
    "alembic_revision_verified",
    "restored_table_count",
    "restore_error",
    "cleanup_status",
    "database_id",
    "operator_action",
)
MANIFEST_KEY_SET = frozenset(MANIFEST_KEYS)
LEGACY_MANIFEST_KEY_SET = frozenset(MANIFEST_KEYS[:-3])


class BackupManifestError(ValueError):
    """Raised when a backup manifest or its protected file boundary is invalid."""


@dataclass(frozen=True, slots=True)
class BackupManifest:
    format_version: int
    database: str
    schemas: tuple[str, ...]
    required_extensions: tuple[str, ...]
    run_id: str
    dump_file: str
    size_bytes: int
    sha256: str
    started_at_utc: dt.datetime
    completed_at_utc: dt.datetime
    duration_seconds: float
    restore_verified: bool
    alembic_revision_verified: bool
    restored_table_count: int | None
    restore_error: str | None
    cleanup_status: str | None = None
    database_id: str | None = None
    operator_action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible representation."""

        return {
            "format_version": self.format_version,
            "database": self.database,
            "schemas": list(self.schemas),
            "required_extensions": list(self.required_extensions),
            "run_id": self.run_id,
            "dump_file": self.dump_file,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "started_at_utc": format_utc_timestamp(self.started_at_utc),
            "completed_at_utc": format_utc_timestamp(self.completed_at_utc),
            "duration_seconds": self.duration_seconds,
            "restore_verified": self.restore_verified,
            "alembic_revision_verified": self.alembic_revision_verified,
            "restored_table_count": self.restored_table_count,
            "restore_error": self.restore_error,
            "cleanup_status": self.cleanup_status,
            "database_id": self.database_id,
            "operator_action": self.operator_action,
        }


@dataclass(frozen=True, slots=True)
class ManifestFile:
    """A parsed manifest plus the exact bytes used for its content digest."""

    manifest: BackupManifest
    raw_bytes: bytes


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupManifestError(f"manifest contains duplicate key: {key}")
        result[key] = value
    return result


def _strict_int(value: object, *, field: str) -> int:
    if type(value) is not int:
        raise BackupManifestError(f"manifest field {field} must be an integer")
    return value


def _strict_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise BackupManifestError(f"manifest field {field} must be a boolean")
    return value


def _parse_utc_timestamp(value: object, *, field: str) -> dt.datetime:
    if type(value) is not str or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise BackupManifestError(
            f"manifest field {field} must be a canonical UTC timestamp ending in Z"
        )
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BackupManifestError(f"manifest field {field} is not a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise BackupManifestError(f"manifest field {field} must be UTC")
    return parsed.astimezone(dt.UTC)


def format_utc_timestamp(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise BackupManifestError("manifest timestamp must be timezone-aware")
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def parse_manifest_payload(
    payload: object,
    *,
    expected_dump_file: str | None = None,
) -> BackupManifest:
    """Validate one current manifest or the exact read-only legacy shape."""

    if type(payload) is not dict:
        raise BackupManifestError("backup manifest must be a JSON object")
    if any(type(key) is not str for key in payload):
        raise BackupManifestError("backup manifest keys must be strings")
    keys = frozenset(payload)
    is_legacy = keys == LEGACY_MANIFEST_KEY_SET
    if keys not in {MANIFEST_KEY_SET, LEGACY_MANIFEST_KEY_SET}:
        if "schema" in keys and "schemas" not in keys:
            raise BackupManifestError(
                'legacy singular "schema" is not accepted by the manifest contract'
            )
        missing = sorted(MANIFEST_KEY_SET - keys)
        extra = sorted(keys - MANIFEST_KEY_SET)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise BackupManifestError("backup manifest keys are not closed: " + "; ".join(details))

    format_version = _strict_int(payload["format_version"], field="format_version")
    expected_version = (
        LEGACY_MANIFEST_FORMAT_VERSION if is_legacy else MANIFEST_FORMAT_VERSION
    )
    if format_version != expected_version:
        raise BackupManifestError("manifest format_version does not match its key shape")
    if payload["database"] != DATABASE_NAME or type(payload["database"]) is not str:
        raise BackupManifestError("manifest database must be platformdb")

    schemas = payload["schemas"]
    if type(schemas) is not list or schemas != list(EXPECTED_SCHEMAS):
        raise BackupManifestError(
            'manifest schemas must be the ordered list ["platform", "public"]'
        )
    if any(type(value) is not str for value in schemas):
        raise BackupManifestError("manifest schemas must contain only strings")

    required_extensions = payload["required_extensions"]
    if type(required_extensions) is not list or required_extensions != list(REQUIRED_EXTENSIONS):
        raise BackupManifestError(
            'manifest required_extensions must be the ordered list ["pg_trgm"]'
        )
    if any(type(value) is not str for value in required_extensions):
        raise BackupManifestError("manifest required_extensions must contain only strings")

    run_id = payload["run_id"]
    if type(run_id) is not str or RUN_ID_RE.fullmatch(run_id) is None:
        raise BackupManifestError("manifest run_id must be 32 lowercase hexadecimal characters")

    dump_file = payload["dump_file"]
    if type(dump_file) is not str or Path(dump_file).name != dump_file:
        raise BackupManifestError("manifest dump_file must be a direct-child filename")
    filename_match = BACKUP_NAME_RE.fullmatch(dump_file)
    if filename_match is None or filename_match.group("run_id") != run_id:
        raise BackupManifestError("manifest dump_file does not match run_id")
    if expected_dump_file is not None and dump_file != expected_dump_file:
        raise BackupManifestError("manifest dump_file does not match the selected archive")

    size_bytes = _strict_int(payload["size_bytes"], field="size_bytes")
    if not 0 < size_bytes <= MAX_DUMP_BYTES:
        raise BackupManifestError("manifest size_bytes is outside the allowed range")
    sha256 = payload["sha256"]
    if type(sha256) is not str or SHA256_RE.fullmatch(sha256) is None:
        raise BackupManifestError("manifest sha256 must be a lowercase SHA-256 digest")

    started_at = _parse_utc_timestamp(payload["started_at_utc"], field="started_at_utc")
    completed_at = _parse_utc_timestamp(payload["completed_at_utc"], field="completed_at_utc")
    if completed_at < started_at:
        raise BackupManifestError("manifest completion precedes its start")

    duration_seconds = payload["duration_seconds"]
    if type(duration_seconds) not in {int, float} or isinstance(duration_seconds, bool):
        raise BackupManifestError("manifest duration_seconds must be a number")
    if not math.isfinite(float(duration_seconds)) or duration_seconds < 0:
        raise BackupManifestError("manifest duration_seconds is invalid")
    elapsed_seconds = (completed_at - started_at).total_seconds()
    if abs(float(duration_seconds) - elapsed_seconds) > 1.01:
        raise BackupManifestError("manifest duration_seconds does not match its timestamps")

    restore_verified = _strict_bool(payload["restore_verified"], field="restore_verified")
    alembic_revision_verified = _strict_bool(
        payload["alembic_revision_verified"], field="alembic_revision_verified"
    )
    if alembic_revision_verified and not restore_verified:
        raise BackupManifestError("Alembic verification cannot pass before restore verification")

    restored_table_count = payload["restored_table_count"]
    if restored_table_count is not None:
        restored_table_count = _strict_int(restored_table_count, field="restored_table_count")
        if restored_table_count < 0 or (restore_verified and restored_table_count == 0):
            raise BackupManifestError("manifest restored_table_count is invalid")
    restore_error = payload["restore_error"]
    if restore_error is not None:
        if type(restore_error) is not str or restore_error not in RESTORE_ERROR_CODES:
            raise BackupManifestError("manifest restore_error is not an allowlisted code")
    if restore_verified and restore_error is not None:
        raise BackupManifestError("verified manifest cannot contain restore_error")

    cleanup_status = payload.get("cleanup_status")
    if cleanup_status is not None and cleanup_status != "unproven":
        raise BackupManifestError("manifest cleanup_status must be null or unproven")
    database_id = payload.get("database_id")
    if database_id is not None and (
        type(database_id) is not str or RESTORE_DRILL_DATABASE_ID_RE.fullmatch(database_id) is None
    ):
        raise BackupManifestError("manifest database_id is not a valid restore-drill identifier")
    operator_action = payload.get("operator_action")
    if operator_action is not None and operator_action != CLEANUP_OPERATOR_ACTION:
        raise BackupManifestError("manifest operator_action is not allowlisted")
    if cleanup_status is None and (database_id is not None or operator_action is not None):
        raise BackupManifestError("manifest cleanup evidence requires cleanup_status=unproven")
    if cleanup_status == "unproven" and ((database_id is None) != (operator_action is None)):
        raise BackupManifestError("manifest cleanup evidence must include both ID and action")
    if restore_verified and cleanup_status is not None:
        raise BackupManifestError("verified manifest cannot contain cleanup evidence")

    return BackupManifest(
        format_version=format_version,
        database=DATABASE_NAME,
        schemas=tuple(schemas),
        required_extensions=tuple(required_extensions),
        run_id=run_id,
        dump_file=dump_file,
        size_bytes=size_bytes,
        sha256=sha256,
        started_at_utc=started_at,
        completed_at_utc=completed_at,
        duration_seconds=float(duration_seconds),
        restore_verified=restore_verified,
        alembic_revision_verified=alembic_revision_verified,
        restored_table_count=restored_table_count,
        restore_error=restore_error,
        cleanup_status=cleanup_status,
        database_id=database_id,
        operator_action=operator_action,
    )


def parse_manifest_bytes(
    raw_bytes: bytes,
    *,
    expected_dump_file: str | None = None,
) -> BackupManifest:
    if type(raw_bytes) is not bytes or len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise BackupManifestError("backup manifest exceeds its size limit")
    try:
        payload = json.loads(
            raw_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupManifestError("backup manifest is not valid UTF-8 JSON") from exc
    return parse_manifest_payload(payload, expected_dump_file=expected_dump_file)


def _validate_stat(
    file_stat: os.stat_result,
    *,
    path: Path,
    label: str,
    expected_owner: int | None,
    expected_group: int | None,
    exact_mode: int | None,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise BackupManifestError(f"{label} must be a regular file: {path}")
    if file_stat.st_nlink != 1:
        raise BackupManifestError(f"{label} must not be a hardlink: {path}")
    if exact_mode is not None and stat.S_IMODE(file_stat.st_mode) != exact_mode:
        raise BackupManifestError(
            f"{label} must have mode {exact_mode:04o}: {path}"
        )
    if expected_owner is not None and file_stat.st_uid != expected_owner:
        raise BackupManifestError(f"{label} has an unexpected owner: {path}")
    if expected_group is not None and file_stat.st_gid != expected_group:
        raise BackupManifestError(f"{label} has an unexpected group: {path}")


def _open_private_file(
    path: Path,
    *,
    label: str,
    expected_owner: int | None,
    expected_group: int | None,
    exact_mode: int | None = 0o600,
) -> tuple[int, os.stat_result]:
    try:
        file_stat_before = path.lstat()
    except OSError as exc:
        raise BackupManifestError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(file_stat_before.st_mode):
        raise BackupManifestError(f"{label} must not be a symlink: {path}")
    _validate_stat(
        file_stat_before,
        path=path,
        label=label,
        expected_owner=expected_owner,
        expected_group=expected_group,
        exact_mode=exact_mode,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupManifestError(f"{label} could not be opened safely: {path}") from exc
    try:
        file_stat = os.fstat(descriptor)
        _validate_stat(
            file_stat,
            path=path,
            label=label,
            expected_owner=expected_owner,
            expected_group=expected_group,
            exact_mode=exact_mode,
        )
        if (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_nlink,
            file_stat.st_size,
            file_stat.st_mtime_ns,
        ) != (
            file_stat_before.st_dev,
            file_stat_before.st_ino,
            file_stat_before.st_nlink,
            file_stat_before.st_size,
            file_stat_before.st_mtime_ns,
        ):
            raise BackupManifestError(f"{label} changed while it was opened: {path}")
        return descriptor, file_stat
    except Exception:
        os.close(descriptor)
        raise


def _verify_path_unchanged(
    path: Path,
    *,
    label: str,
    expected_owner: int | None,
    expected_group: int | None,
    exact_mode: int | None,
    initial_stat: os.stat_result,
    activity: str,
) -> None:
    try:
        current_stat = path.lstat()
    except OSError as exc:
        raise BackupManifestError(f"{label} disappeared while it was {activity}: {path}") from exc
    if stat.S_ISLNK(current_stat.st_mode):
        raise BackupManifestError(f"{label} became a symlink while it was {activity}: {path}")
    _validate_stat(
        current_stat,
        path=path,
        label=label,
        expected_owner=expected_owner,
        expected_group=expected_group,
        exact_mode=exact_mode,
    )
    if (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_nlink,
        current_stat.st_size,
        current_stat.st_mtime_ns,
    ) != (
        initial_stat.st_dev,
        initial_stat.st_ino,
        initial_stat.st_nlink,
        initial_stat.st_size,
        initial_stat.st_mtime_ns,
    ):
        raise BackupManifestError(f"{label} changed while it was {activity}: {path}")


def read_private_file(
    path: Path,
    *,
    label: str,
    expected_owner: int | None,
    expected_group: int | None,
    max_bytes: int | None = None,
    exact_mode: int | None = 0o600,
) -> tuple[bytes, os.stat_result]:
    descriptor, file_stat = _open_private_file(
        path,
        label=label,
        expected_owner=expected_owner,
        expected_group=expected_group,
        exact_mode=exact_mode,
    )
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            raw = handle.read(None if max_bytes is None else max_bytes + 1)
        if max_bytes is not None and len(raw) > max_bytes:
            raise BackupManifestError(f"{label} exceeds its size limit: {path}")
        _verify_path_unchanged(
            path,
            label=label,
            expected_owner=expected_owner,
            expected_group=expected_group,
            exact_mode=exact_mode,
            initial_stat=file_stat,
            activity="read",
        )
        return raw, file_stat
    except Exception:
        # fdopen owns the descriptor after successful construction.  If it
        # failed before ownership transfer, close defensively.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def read_private_prefix(
    path: Path,
    *,
    label: str,
    expected_owner: int | None,
    expected_group: int | None,
    prefix_bytes: int,
    exact_mode: int | None = 0o600,
) -> tuple[bytes, os.stat_result]:
    if prefix_bytes < 0:
        raise ValueError("prefix_bytes must be non-negative")
    descriptor, file_stat = _open_private_file(
        path,
        label=label,
        expected_owner=expected_owner,
        expected_group=expected_group,
        exact_mode=exact_mode,
    )
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            raw = handle.read(prefix_bytes)
        _verify_path_unchanged(
            path,
            label=label,
            expected_owner=expected_owner,
            expected_group=expected_group,
            exact_mode=exact_mode,
            initial_stat=file_stat,
            activity="read",
        )
        return raw, file_stat
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def sha256_private_file(
    path: Path,
    *,
    label: str,
    expected_owner: int | None,
    expected_group: int | None,
    exact_mode: int | None = 0o600,
) -> tuple[str, os.stat_result]:
    descriptor, file_stat = _open_private_file(
        path,
        label=label,
        expected_owner=expected_owner,
        expected_group=expected_group,
        exact_mode=exact_mode,
    )
    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        _verify_path_unchanged(
            path,
            label=label,
            expected_owner=expected_owner,
            expected_group=expected_group,
            exact_mode=exact_mode,
            initial_stat=file_stat,
            activity="hashed",
        )
        return digest.hexdigest(), file_stat
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def read_manifest_file(
    path: Path,
    *,
    expected_owner: int | None,
    expected_group: int | None,
    expected_dump_file: str | None = None,
) -> ManifestFile:
    raw_bytes, _ = read_private_file(
        path,
        label="Platform backup manifest",
        expected_owner=expected_owner,
        expected_group=expected_group,
        max_bytes=MAX_MANIFEST_BYTES,
        exact_mode=0o600,
    )
    return ManifestFile(
        manifest=parse_manifest_bytes(raw_bytes, expected_dump_file=expected_dump_file),
        raw_bytes=raw_bytes,
    )


def _fsync_directory(path: Path) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(path, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def build_manifest(
    *,
    run_id: str,
    dump_file: str,
    size_bytes: int,
    sha256: str,
    started_at_utc: dt.datetime,
    completed_at_utc: dt.datetime,
    duration_seconds: int | float,
    restore_verified: bool,
    alembic_revision_verified: bool,
    restored_table_count: int | None,
    restore_error: str | None,
    cleanup_status: str | None = None,
    database_id: str | None = None,
    operator_action: str | None = None,
) -> dict[str, Any]:
    """Build and validate creator output, preserving canonical key order."""

    payload = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "database": DATABASE_NAME,
        "schemas": list(EXPECTED_SCHEMAS),
        "required_extensions": list(REQUIRED_EXTENSIONS),
        "run_id": run_id,
        "dump_file": dump_file,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "started_at_utc": format_utc_timestamp(started_at_utc),
        "completed_at_utc": format_utc_timestamp(completed_at_utc),
        "duration_seconds": duration_seconds,
        "restore_verified": restore_verified,
        "alembic_revision_verified": alembic_revision_verified,
        "restored_table_count": restored_table_count,
        "restore_error": restore_error,
        "cleanup_status": cleanup_status,
        "database_id": database_id,
        "operator_action": operator_action,
    }
    parse_manifest_payload(payload, expected_dump_file=dump_file)
    return payload


def write_manifest(path: Path, payload: Mapping[str, Any]) -> BackupManifest:
    """Write a validated manifest with fsync, atomic rename and dir fsync."""

    candidate = dict(payload)
    if (
        frozenset(candidate) != MANIFEST_KEY_SET
        or candidate.get("format_version") != MANIFEST_FORMAT_VERSION
    ):
        raise BackupManifestError(
            "manifest writer accepts only the current version-3 shape"
        )
    manifest = parse_manifest_payload(
        candidate, expected_dump_file=str(path.with_suffix(".dump").name)
    )
    canonical_payload = {key: manifest.as_dict()[key] for key in MANIFEST_KEYS}
    raw_bytes = (json.dumps(canonical_payload, ensure_ascii=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise BackupManifestError("backup manifest exceeds its size limit")
    parent = path.parent
    temporary_path: Path | None = None
    descriptor: int | None = None
    replaced = False
    published_identity: tuple[int, int, int] | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(raw_bytes)
            handle.flush()
            _fsync_file(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        replaced = True
        published_stat = path.lstat()
        published_identity = (
            published_stat.st_dev,
            published_stat.st_ino,
            published_stat.st_nlink,
        )
        _fsync_directory(parent)
    except OSError as exc:
        if replaced and published_identity is not None:
            try:
                current_stat = path.lstat()
                current_identity = (
                    current_stat.st_dev,
                    current_stat.st_ino,
                    current_stat.st_nlink,
                )
                if current_identity == published_identity:
                    path.unlink()
            except OSError:
                pass
            try:
                _fsync_directory(parent)
            except OSError:
                pass
        raise BackupManifestError(f"could not atomically write backup manifest: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    return manifest
