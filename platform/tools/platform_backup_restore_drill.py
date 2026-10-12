#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import pwd
import re
import selectors
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any, BinaryIO, Callable


def _load_staged_disk_policy() -> Any:
    helper_path = pathlib.Path(__file__).resolve().with_name("platform_disk_policy.py")
    spec = importlib.util.spec_from_file_location(
        "_oldsparky_backup_restore_disk_policy", helper_path
    )
    if spec is None or spec.loader is None:
        raise ImportError("platform disk policy helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:
    from .platform_disk_policy import (
        DEFAULT_MIN_FREE_GIB,
        minimum_free_bytes,
        snapshot_for_path,
    )
except ImportError:  # Direct execution from the tools directory.
    _disk_policy = _load_staged_disk_policy()
    DEFAULT_MIN_FREE_GIB = _disk_policy.DEFAULT_MIN_FREE_GIB
    minimum_free_bytes = _disk_policy.minimum_free_bytes
    snapshot_for_path = _disk_policy.snapshot_for_path


DEFAULT_ENV_FILE = pathlib.Path("/opt/oldsparky/platform/shared/.env.platform")
DEFAULT_OUTPUT_DIR = pathlib.Path("/opt/oldsparky/platform/shared/backups")
LOCAL_DATABASE_HOSTS = {None, "", "127.0.0.1", "localhost", "::1"}
REQUIRED_PLATFORM_EXTENSIONS = ("pg_trgm",)
RESTORE_DISK_LEAD_BYTES = 256 * 1024**2
RESTORE_DISK_SAMPLE_SECONDS = 0.25
RESTORE_TERMINATE_TIMEOUT_SECONDS = 1.0
RESTORE_DISK_MINIMUM_BYTES = minimum_free_bytes(DEFAULT_MIN_FREE_GIB)
SCRATCH_TMPFS_BYTES = 512 * 1024**2
SCRATCH_MEMORY_MAX_BYTES = 640 * 1024**2
SCRATCH_MEMORY_RESERVE_BYTES = 384 * 1024**2
SCRATCH_MEMORY_ADMISSION_BYTES = SCRATCH_MEMORY_MAX_BYTES + SCRATCH_MEMORY_RESERVE_BYTES
SCRATCH_MEMORY_STOP_MARGIN_BYTES = 32 * 1024**2
SCRATCH_TMPFS_STOP_BYTES = 64 * 1024**2
SCRATCH_RUNTIME_SECONDS = 15 * 60
SCRATCH_RUNTIME_SAMPLE_SECONDS = 0.25
SCRATCH_PG_PORT = 55432
SCRATCH_WORKER_MODE = "--_scratch-restore-worker"
SCRATCH_APP_ROLE = "platform_user"


class RestoreGuardStop(RuntimeError):
    """A fixed, non-sensitive reason for stopping an owned restore child."""

    def __init__(
        self,
        reason: str,
        *,
        free_bytes: int | None = None,
        required_free_bytes: int | None = None,
        phase: str | None = None,
    ) -> None:
        if reason not in _RESTORE_GUARD_MESSAGES:
            raise ValueError("unknown restore guard reason")
        self.reason = reason
        self.free_bytes = free_bytes
        self.required_free_bytes = required_free_bytes
        self.phase = phase
        super().__init__(_RESTORE_GUARD_MESSAGES[reason])

    def private_backup_disk_diagnostic(self) -> dict[str, Any] | None:
        if self.phase not in _BACKUP_DISK_DIAGNOSTIC_PHASES:
            return None
        value = {
            "schema": 1,
            "phase": self.phase,
            "guard_reason": self.reason,
            "free_bytes": self.free_bytes,
            "required_free_bytes": self.required_free_bytes,
        }
        return value if valid_backup_disk_diagnostic(value) else None


_RESTORE_GUARD_MESSAGES = {
    "disk_floor": "Restore drill stopped by the low-disk safety guard.",
    "disk_unavailable": "Restore drill stopped because disk space could not be verified.",
    "child_unstopped": "Backup or restore command could not be confirmed stopped.",
    "command_start_failed": "Backup or restore command could not be started.",
    "command_failed": "Backup or restore command exited unsuccessfully.",
}
_BACKUP_DISK_DIAGNOSTIC_PHASES = {
    "backup_admission",
    "pg_dump_pre_spawn",
    "pg_dump_post_start",
    "pg_dump_monitor",
    "pg_dump_post_exit",
}
_BACKUP_DISK_DIAGNOSTIC_REASONS = {
    "disk_floor",
    "disk_unavailable",
    "child_unstopped",
    "command_start_failed",
    "command_failed",
}
_RESTORE_DIAGNOSTIC_STAGES = {
    "pre_create_admission",
    "create_database",
    "extension_setup",
    "schema_setup",
    "restore_platform",
    "restore_public",
    "validate_table_count",
    "validate_connectivity",
    "validate_revision",
    "validate_extensions",
    "drop_database",
}
_RESTORE_DROP_OUTCOMES = {
    "not_required",
    "drop_failed",
    "database_present",
    "absence_unconfirmed",
    "confirmed_absent",
}


def valid_backup_disk_diagnostic(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "phase",
        "guard_reason",
        "free_bytes",
        "required_free_bytes",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("phase")) is not str
        or value["phase"] not in _BACKUP_DISK_DIAGNOSTIC_PHASES
        or type(value.get("guard_reason")) is not str
        or value["guard_reason"] not in _BACKUP_DISK_DIAGNOSTIC_REASONS
        or any(
            item is not None and (type(item) is not int or item < 0)
            for item in (value.get("free_bytes"), value.get("required_free_bytes"))
        )
    ):
        return False
    if value["guard_reason"] == "disk_floor":
        return (
            type(value["free_bytes"]) is int
            and type(value["required_free_bytes"]) is int
            and value["free_bytes"] < value["required_free_bytes"]
        )
    return True


def _valid_restore_diagnostic(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "restore_stage",
        "guard_reason",
        "free_bytes",
        "required_free_bytes",
        "temporary_database_created",
        "drop_outcome",
        "temporary_database_absent",
        "archive_sha256",
        "archive_size_bytes",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("restore_stage")) is not str
        or value["restore_stage"] not in _RESTORE_DIAGNOSTIC_STAGES
        or type(value.get("guard_reason")) is not str
        or value["guard_reason"] not in {"none", *_RESTORE_GUARD_MESSAGES.keys()}
        or type(value.get("drop_outcome")) is not str
        or value["drop_outcome"] not in _RESTORE_DROP_OUTCOMES
        or (
            value.get("temporary_database_created") is not None
            and type(value.get("temporary_database_created")) is not bool
        )
        or (
            value.get("temporary_database_absent") is not None
            and type(value.get("temporary_database_absent")) is not bool
        )
    ):
        return False
    if not all(
        value.get(key) is None or (type(value[key]) is int and value[key] >= 0)
        for key in ("free_bytes", "required_free_bytes")
    ):
        return False
    if (
        type(value.get("archive_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"]) is None
        or type(value.get("archive_size_bytes")) is not int
        or value["archive_size_bytes"] < 0
    ):
        return False
    if value["guard_reason"] == "disk_floor":
        if (
            type(value.get("free_bytes")) is not int
            or type(value.get("required_free_bytes")) is not int
            or value["free_bytes"] >= value["required_free_bytes"]
        ):
            return False
    if value["temporary_database_created"] is False or value["temporary_database_created"] is None:
        return (
            (value["temporary_database_created"] is False or value["restore_stage"] == "create_database")
            and
            value["drop_outcome"] == "not_required"
            and value["temporary_database_absent"] is None
        )
    if value["drop_outcome"] == "not_required":
        return False
    if value["drop_outcome"] == "confirmed_absent":
        return value["temporary_database_absent"] is True
    if value["drop_outcome"] == "database_present":
        return value["temporary_database_absent"] is False
    if value["drop_outcome"] in {"drop_failed", "absence_unconfirmed"}:
        return value["temporary_database_absent"] is None
    return False


@dataclasses.dataclass(frozen=True)
class DatabaseTarget:
    host: str | None
    port: int
    username: str
    password: str | None
    database: str

    def with_database(self, database: str) -> "DatabaseTarget":
        return dataclasses.replace(self, database=database)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create an atomic custom-format backup of platformdb's platform schema "
            "and verify it in a bounded isolated PostgreSQL cluster."
        )
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--keep", type=int, default=14)
    rotation = parser.add_mutually_exclusive_group()
    rotation.add_argument(
        "--preserve-existing",
        dest="rotate_existing",
        action="store_false",
        help="Keep every pre-existing archive and sidecar; do not rotate backups.",
    )
    rotation.add_argument(
        "--rotate-existing",
        dest="rotate_existing",
        action="store_true",
        help="Apply the requested retention policy to older verified backups.",
    )
    # Keep the low-level CLI's historical rotation behavior. Backup-only
    # maintenance opts into preservation explicitly through its own caller.
    parser.set_defaults(rotate_existing=True)
    parser.add_argument(
        "--admin-database-url",
        default=None,
        help="Accepted for CLI compatibility; isolated restore verification does not use this URL.",
    )
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="Create and validate the archive without performing the restore drill.",
    )
    parser.add_argument(
        "--check-latest",
        action="store_true",
        help="Only verify the newest retained backup metadata and checksum.",
    )
    parser.add_argument(
        "--verify-latest-existing",
        action="store_true",
        help=(
            "Restore and verify only the newest retained archive, then mark its existing "
            "metadata verified without creating or rotating a backup."
        ),
    )
    parser.add_argument(
        "--verify-dump",
        default=None,
        help="Restore an existing custom-format platform backup in an isolated cluster.",
    )
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(SCRATCH_WORKER_MODE, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-dev", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-ino", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-size", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-sha256", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-timestamp-slug", default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def parse_database_url(database_url: str, *, require_platformdb: bool = True) -> DatabaseTarget:
    normalized = database_url
    for scheme in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if normalized.startswith(scheme):
            normalized = "postgresql://" + normalized[len(scheme) :]
            break
    parsed = urllib.parse.urlsplit(normalized)
    database = urllib.parse.unquote(parsed.path.lstrip("/"))
    username = urllib.parse.unquote(parsed.username or "")
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("PLATFORM_DATABASE_URL must use a PostgreSQL scheme.")
    if not username or not database:
        raise ValueError("PLATFORM_DATABASE_URL must include a username and database name.")
    if require_platformdb and database != "platformdb":
        raise ValueError(
            f"Refusing to back up database {database!r}; expected the isolated platformdb database."
        )
    return DatabaseTarget(
        host=parsed.hostname,
        port=parsed.port or 5432,
        username=username,
        password=urllib.parse.unquote(parsed.password) if parsed.password else None,
        database=database,
    )


def connection_args(target: DatabaseTarget, *, include_database: bool = True) -> list[str]:
    args: list[str] = []
    if target.host:
        args.extend(["--host", target.host])
    args.extend(["--port", str(target.port), "--username", target.username])
    if include_database:
        args.extend(["--dbname", target.database])
    return args


def command_env(target: DatabaseTarget) -> dict[str, str]:
    env = dict(os.environ)
    if target.password:
        env["PGPASSWORD"] = target.password
    else:
        env.pop("PGPASSWORD", None)
    return env


def run_command(
    command: list[str],
    *,
    target: DatabaseTarget | None = None,
    capture_output: bool = False,
    stdin_handle: BinaryIO | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture_output,
        stdin=stdin_handle,
        env=command_env(target) if target is not None else None,
    )


def _required_disk_floor(total_bytes: int, *, phase: str | None = None) -> int:
    if type(total_bytes) is not int or total_bytes <= 0:
        raise RestoreGuardStop("disk_unavailable", phase=phase)
    fifteen_percent_ceil = (total_bytes * 15 + 99) // 100
    return max(RESTORE_DISK_MINIMUM_BYTES, fifteen_percent_ceil)


def require_restore_headroom(
    *paths: pathlib.Path, phase: str | None = None
) -> None:
    """Fail closed unless every affected filesystem has policy floor plus lead."""

    checked_paths = paths or (pathlib.Path("/"),)
    try:
        snapshots = [snapshot_for_path(path) for path in checked_paths]
    except OSError as exc:
        raise RestoreGuardStop("disk_unavailable", phase=phase) from exc
    for snapshot in snapshots:
        if not snapshot.valid:
            raise RestoreGuardStop("disk_unavailable", phase=phase)
        floor = _required_disk_floor(snapshot.total_bytes, phase=phase)
        required_free_bytes = floor + RESTORE_DISK_LEAD_BYTES
        if snapshot.free_bytes < required_free_bytes:
            raise RestoreGuardStop(
                "disk_floor",
                free_bytes=snapshot.free_bytes,
                required_free_bytes=required_free_bytes,
                phase=phase,
            )


def _stop_owned_child(
    process: subprocess.Popen[bytes], *, phase: str | None = None
) -> None:
    """Stop and reap only the child process started by this call."""

    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=RESTORE_TERMINATE_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        # Do not let finally/dropdb race a child that is still writing. SIGKILL
        # is followed by a wait on this exact Popen until it has been reaped.
        process.wait()
    except OSError as exc:
        raise RestoreGuardStop("child_unstopped", phase=phase) from exc


def run_disk_guarded_command(
    command: list[str],
    *,
    target: DatabaseTarget,
    stage: str,
    disk_paths: tuple[pathlib.Path, ...] = (pathlib.Path("/"),),
    stdin_handle: BinaryIO | None = None,
    resource_guard: Callable[[], None] | None = None,
) -> None:
    """Run one owned dump/restore child under bounded disk monitoring."""

    if stage not in {"pg_dump", "pg_restore"}:
        raise ValueError("unknown disk-guarded command stage")
    phase = f"{stage}_pre_spawn"
    backup_phase = phase if stage == "pg_dump" else None
    require_restore_headroom(*disk_paths, phase=backup_phase)
    if resource_guard is not None:
        resource_guard()
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=command_env(target),
            close_fds=True,
        )
    except OSError as exc:
        raise RestoreGuardStop("command_start_failed", phase=backup_phase) from exc

    try:
        # Always take a post-start sample, including when pg_restore is short.
        phase = f"{stage}_post_start"
        backup_phase = phase if stage == "pg_dump" else None
        require_restore_headroom(*disk_paths, phase=backup_phase)
        if resource_guard is not None:
            resource_guard()
        while process.poll() is None:
            time.sleep(RESTORE_DISK_SAMPLE_SECONDS)
            if process.poll() is not None:
                break
            phase = f"{stage}_monitor"
            backup_phase = phase if stage == "pg_dump" else None
            require_restore_headroom(*disk_paths, phase=backup_phase)
            if resource_guard is not None:
                resource_guard()
        return_code = process.wait()
        # Catch a floor crossing between the final sample and child exit.
        phase = f"{stage}_post_exit"
        backup_phase = phase if stage == "pg_dump" else None
        require_restore_headroom(*disk_paths, phase=backup_phase)
        if resource_guard is not None:
            resource_guard()
    except BaseException:
        if process.poll() is None:
            _stop_owned_child(process, phase=backup_phase)
        raise
    if return_code != 0:
        raise RestoreGuardStop("command_failed", phase=backup_phase)


def run_restore_command(
    command: list[str],
    *,
    target: DatabaseTarget,
    stdin_handle: BinaryIO | None = None,
    resource_guard: Callable[[], None] | None = None,
) -> None:
    run_disk_guarded_command(
        command,
        target=target,
        stage="pg_restore",
        stdin_handle=stdin_handle,
        resource_guard=resource_guard,
    )


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_utc_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def newest_metadata(output_dir: pathlib.Path) -> pathlib.Path:
    candidates = sorted(output_dir.glob("platformdb-*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise RuntimeError(f"No retained platform backup metadata found in {output_dir}.")
    return candidates[-1]


def check_latest_backup(output_dir: pathlib.Path, *, max_age_hours: float) -> dict[str, Any]:
    if max_age_hours <= 0:
        raise ValueError("--max-age-hours must be positive.")
    metadata_path = newest_metadata(output_dir)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not metadata.get("restore_verified"):
        raise RuntimeError(f"Latest platform backup was not restore-verified: {metadata_path}.")
    if int(metadata.get("format_version") or 1) >= 2 and not metadata.get(
        "alembic_revision_verified"
    ):
        raise RuntimeError(f"Latest platform backup did not verify Alembic state: {metadata_path}.")
    completed_at = parse_utc_timestamp(str(metadata["completed_at_utc"]))
    age_hours = (utc_now() - completed_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(
            f"Latest restore-verified platform backup is {age_hours:.2f} hours old; "
            f"maximum is {max_age_hours:.2f}."
        )
    dump_path = output_dir / str(metadata["dump_file"])
    if not dump_path.is_file():
        raise RuntimeError(f"Backup archive referenced by metadata is missing: {dump_path}.")
    actual_sha256 = sha256_file(dump_path)
    if actual_sha256 != metadata.get("sha256"):
        raise RuntimeError(f"Backup archive checksum does not match metadata: {dump_path}.")
    return {
        "ok": True,
        "metadata_file": str(metadata_path),
        "dump_file": str(dump_path),
        "age_hours": round(age_hours, 3),
        "format_version": int(metadata.get("format_version") or 1),
        "restore_verified": True,
        "alembic_revision_verified": bool(
            metadata.get("alembic_revision_verified")
        ),
        "restored_table_count": metadata.get("restored_table_count"),
        "sha256": actual_sha256,
    }


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _backup_directory_identity(path: pathlib.Path) -> tuple[int, int, int, int, int, int]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeError("Backup output directory has unsafe metadata.")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
    )


def _read_private_json(
    path: pathlib.Path,
    *,
    expected_gid: int | None = None,
    max_bytes: int = 1_048_576,
) -> tuple[bytes, dict[str, Any], tuple[int, ...]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or (expected_gid is not None and before.st_gid != expected_gid)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise RuntimeError("Latest backup metadata has unsafe file metadata.")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        identity = _stable_file_identity(before)
        if len(raw) > max_bytes or _stable_file_identity(after) != identity:
            raise RuntimeError("Latest backup metadata changed while being read.")
    finally:
        os.close(fd)

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate metadata key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Latest backup metadata is malformed.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Latest backup metadata must be a JSON object.")
    return raw, value, identity


def _verify_backup_age(metadata: dict[str, Any], *, max_age_hours: float) -> tuple[dt.datetime, float]:
    try:
        timestamp = metadata["completed_at_utc"]
        if not isinstance(timestamp, str):
            raise ValueError("timestamp type")
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
        completed_at = parsed.astimezone(dt.UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Latest backup creation timestamp is invalid.") from exc
    now = utc_now()
    age_hours = (now - completed_at).total_seconds() / 3600
    if age_hours < 0 or not math.isfinite(age_hours) or age_hours > max_age_hours:
        raise RuntimeError("Latest backup is outside the permitted creation-age window.")
    return completed_at, age_hours


def _latest_pair_for_verification(
    output_dir: pathlib.Path,
    *,
    max_age_hours: float,
) -> tuple[
    pathlib.Path,
    pathlib.Path,
    bytes,
    dict[str, Any],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, int, int, int, int, int],
    str,
    float,
]:
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise ValueError("--max-age-hours must be positive.")
    directory_identity = _backup_directory_identity(output_dir)
    metadata_path = newest_metadata(output_dir)
    metadata_raw, metadata, metadata_identity = _read_private_json(
        metadata_path, expected_gid=directory_identity[3]
    )
    dump_name = metadata.get("dump_file")
    if (
        not isinstance(dump_name, str)
        or pathlib.PurePath(dump_name).name != dump_name
        or not dump_name.startswith("platformdb-")
        or not dump_name.endswith(".dump")
        or metadata_path.name != pathlib.Path(dump_name).with_suffix(".json").name
    ):
        raise RuntimeError("Latest backup metadata has an invalid archive binding.")
    _, age_hours = _verify_backup_age(metadata, max_age_hours=max_age_hours)
    dump_path = output_dir / dump_name
    dump_stat = dump_path.lstat()
    if (
        not stat.S_ISREG(dump_stat.st_mode)
        or dump_stat.st_uid != os.geteuid()
        or dump_stat.st_gid != directory_identity[3]
        or stat.S_IMODE(dump_stat.st_mode) != 0o600
        or dump_stat.st_nlink != 1
        or dump_stat.st_size <= 0
    ):
        raise RuntimeError("Latest backup archive has unsafe file metadata.")
    dump_identity = _stable_file_identity(dump_stat)
    actual_sha256 = sha256_file(dump_path)
    version = metadata.get("format_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
        raise RuntimeError("Latest backup metadata format is unsupported.")
    if metadata.get("sha256") != actual_sha256:
        raise RuntimeError("Latest backup archive checksum does not match metadata.")
    size_bytes = metadata.get("size_bytes")
    if size_bytes is not None and (
        isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes != dump_stat.st_size
    ):
        raise RuntimeError("Latest backup archive size does not match metadata.")
    if metadata.get("database", "platformdb") != "platformdb":
        raise RuntimeError("Latest backup metadata does not identify platformdb.")
    schemas = metadata.get("schemas", ["platform", "public"])
    if not isinstance(schemas, list) or schemas != ["platform", "public"]:
        raise RuntimeError("Latest backup metadata has an unsupported schema set.")
    extensions = metadata.get("required_extensions", list(REQUIRED_PLATFORM_EXTENSIONS))
    if not isinstance(extensions, list) or extensions != list(REQUIRED_PLATFORM_EXTENSIONS):
        raise RuntimeError("Latest backup metadata has an unsupported extension set.")
    for flag in ("restore_verified", "alembic_revision_verified"):
        if flag in metadata and not isinstance(metadata[flag], bool):
            raise RuntimeError("Latest backup metadata has an invalid verification flag.")
    if "restore_error" in metadata and metadata["restore_error"] is not None and not isinstance(
        metadata["restore_error"], str
    ):
        raise RuntimeError("Latest backup metadata has an invalid restore diagnostic.")
    if newest_metadata(output_dir) != metadata_path:
        raise RuntimeError("Latest backup selection changed during verification.")
    return (
        metadata_path,
        dump_path,
        metadata_raw,
        metadata,
        metadata_identity,
        dump_identity,
        directory_identity,
        actual_sha256,
        age_hours,
    )


def _publish_verified_metadata(
    output_dir: pathlib.Path,
    metadata_path: pathlib.Path,
    *,
    original_raw: bytes,
    original_identity: tuple[int, ...],
    directory_identity: tuple[int, int, int, int, int, int],
    updated: dict[str, Any],
    max_age_hours: float,
) -> None:
    if _backup_directory_identity(output_dir) != directory_identity:
        raise RuntimeError("Backup output directory changed before metadata staging.")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{metadata_path.name}.", suffix=".verify.tmp", dir=output_dir
    )
    temporary_path: pathlib.Path | None = pathlib.Path(temporary_name)
    temp_identity: tuple[int, int] | None = None
    published_identity: tuple[int, int] | None = None
    rollback_path: pathlib.Path | None = None
    rollback_identity: tuple[int, int] | None = None
    try:
        expected_bytes = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        initial = os.fstat(fd)
        temp_identity = (initial.st_dev, initial.st_ino)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            written = handle.write(expected_bytes)
            if written != len(expected_bytes):
                raise RuntimeError("Verified backup metadata staging write was incomplete.")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600, follow_symlinks=False)
        if _backup_directory_identity(output_dir) != directory_identity:
            raise RuntimeError("Backup output directory changed during metadata staging.")
        staged = temporary_path.lstat()
        if (
            (staged.st_dev, staged.st_ino) != temp_identity
            or not stat.S_ISREG(staged.st_mode)
            or staged.st_uid != os.geteuid()
            or staged.st_gid != directory_identity[3]
            or stat.S_IMODE(staged.st_mode) != 0o600
            or staged.st_nlink != 1
            or staged.st_size != len(expected_bytes)
        ):
            raise RuntimeError("Verified backup metadata staging identity changed.")
        verify_fd = os.open(
            temporary_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            verify_before = os.fstat(verify_fd)
            staged_bytes = b""
            while len(staged_bytes) <= len(expected_bytes):
                chunk = os.read(verify_fd, min(65536, len(expected_bytes) + 1 - len(staged_bytes)))
                if not chunk:
                    break
                staged_bytes += chunk
            verify_after = os.fstat(verify_fd)
            if (
                _stable_file_identity(verify_before) != _stable_file_identity(verify_after)
                or staged_bytes != expected_bytes
            ):
                raise RuntimeError("Verified backup metadata staging bytes changed.")
        finally:
            os.close(verify_fd)
        current_raw, _, current_identity = _read_private_json(
            metadata_path, expected_gid=directory_identity[3]
        )
        if current_raw != original_raw or current_identity != original_identity:
            raise RuntimeError("Latest backup metadata changed before verification commit.")
        if _backup_directory_identity(output_dir) != directory_identity:
            raise RuntimeError("Backup output directory changed before verification commit.")
        if newest_metadata(output_dir) != metadata_path:
            raise RuntimeError("Latest backup selection changed before verification commit.")
        _verify_backup_age(updated, max_age_hours=max_age_hours)
        os.replace(temporary_path, metadata_path)
        temporary_path = None
        published_identity = temp_identity
        published_stat = metadata_path.lstat()
        published_identity = (published_stat.st_dev, published_stat.st_ino)
        if (
            not stat.S_ISREG(published_stat.st_mode)
            or published_stat.st_uid != os.geteuid()
            or published_stat.st_gid != directory_identity[3]
            or stat.S_IMODE(published_stat.st_mode) != 0o600
            or published_stat.st_nlink != 1
            or published_stat.st_size != len(expected_bytes)
        ):
            raise RuntimeError("Verified backup metadata publication metadata changed.")
        directory_fd = os.open(
            output_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if published_identity is not None:
            try:
                current = metadata_path.lstat()
                if (current.st_dev, current.st_ino) == published_identity and stat.S_ISREG(
                    current.st_mode
                ):
                    rollback_fd, rollback_name = tempfile.mkstemp(
                        prefix=f".{metadata_path.name}.", suffix=".rollback.tmp", dir=output_dir
                    )
                    rollback_path = pathlib.Path(rollback_name)
                    rollback_stat = os.fstat(rollback_fd)
                    rollback_identity = (rollback_stat.st_dev, rollback_stat.st_ino)
                    with os.fdopen(rollback_fd, "wb", closefd=True) as handle:
                        handle.write(original_raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(rollback_path, 0o600, follow_symlinks=False)
                    staged_rollback = rollback_path.lstat()
                    if (
                        (staged_rollback.st_dev, staged_rollback.st_ino) != rollback_identity
                        or not stat.S_ISREG(staged_rollback.st_mode)
                        or staged_rollback.st_uid != os.geteuid()
                        or staged_rollback.st_gid != directory_identity[3]
                        or staged_rollback.st_nlink != 1
                        or stat.S_IMODE(staged_rollback.st_mode) != 0o600
                    ):
                        raise RuntimeError("Backup metadata rollback staging identity changed.")
                    current = metadata_path.lstat()
                    if (current.st_dev, current.st_ino) != published_identity:
                        raise RuntimeError("Backup metadata changed before rollback.")
                    os.replace(rollback_path, metadata_path)
                    rollback_path = None
                    rollback_directory_fd = os.open(
                        output_dir,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        os.fsync(rollback_directory_fd)
                    finally:
                        os.close(rollback_directory_fd)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Backup metadata commit failed and exact rollback could not be confirmed."
                ) from rollback_error
        raise
    finally:
        if temp_identity is not None and temporary_path is not None:
            try:
                current = temporary_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == temp_identity and stat.S_ISREG(current.st_mode):
                    temporary_path.unlink()
        if rollback_path is not None and rollback_identity is not None:
            try:
                current = rollback_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == rollback_identity and stat.S_ISREG(
                    current.st_mode
                ):
                    rollback_path.unlink()


def verify_latest_existing_backup(
    output_dir: pathlib.Path,
    env_file: pathlib.Path,
    max_age_hours: float = 24.0,
    *,
    admin_database_url: str | None = None,
) -> dict[str, Any]:
    """Re-verify only the newest existing backup and update its sidecar on success.

    Callers are responsible for the canonical host lock. The archive bytes and original
    creation timestamp remain untouched; any failed restore leaves both retained files
    byte-for-byte unchanged.
    """
    (
        metadata_path,
        dump_path,
        original_raw,
        metadata,
        metadata_identity,
        dump_identity,
        directory_identity,
        archive_sha256,
        _,
    ) = _latest_pair_for_verification(output_dir, max_age_hours=max_age_hours)
    require_commands("systemd-run", "systemctl")
    try:
        table_count = _run_isolated_restore_drill(
            dump_path,
            timestamp_slug=utc_now().strftime("%Y%m%dT%H%M%SZ"),
        )
    except Exception as exc:
        diagnostic = getattr(exc, "restore_diagnostic", None)
        if isinstance(diagnostic, dict):
            diagnostic = {
                **diagnostic,
                "archive_sha256": archive_sha256,
                "archive_size_bytes": dump_identity[7],
            }
            if _valid_restore_diagnostic(diagnostic):
                setattr(exc, "restore_diagnostic", diagnostic)
        raise

    # A restore can cross the source-age deadline; check the original creation time again.
    _, age_hours = _verify_backup_age(metadata, max_age_hours=max_age_hours)
    latest = _latest_pair_for_verification(output_dir, max_age_hours=max_age_hours)
    if (
        latest[0] != metadata_path
        or latest[2] != original_raw
        or latest[4] != metadata_identity
        or latest[6] != directory_identity
    ):
        raise RuntimeError("Latest backup metadata changed during restore verification.")
    dump_after = dump_path.lstat()
    if (
        _stable_file_identity(dump_after) != dump_identity
        or sha256_file(dump_path) != archive_sha256
    ):
        raise RuntimeError("Latest backup archive changed during restore verification.")
    updated = dict(metadata)
    updated["restore_verified"] = True
    updated["alembic_revision_verified"] = True
    updated["restored_table_count"] = table_count
    _, age_hours = _verify_backup_age(metadata, max_age_hours=max_age_hours)
    verified_at = utc_now()
    updated["restore_verified_at_utc"] = verified_at.isoformat().replace("+00:00", "Z")
    _publish_verified_metadata(
        output_dir,
        metadata_path,
        original_raw=original_raw,
        original_identity=metadata_identity,
        directory_identity=directory_identity,
        updated=updated,
        max_age_hours=max_age_hours,
    )
    return {
        "ok": True,
        "metadata_file": str(metadata_path),
        "dump_file": str(dump_path),
        "age_hours": round(age_hours, 3),
        "restore_verified": True,
        "alembic_revision_verified": True,
        "restored_table_count": table_count,
        "sha256": archive_sha256,
        "restore_verified_at_utc": updated["restore_verified_at_utc"],
    }


def local_postgres_admin_command(action: str, target: DatabaseTarget, database: str) -> list[str]:
    if action == "create":
        command = ["createdb", "--owner", target.username, database]
    elif action == "drop":
        command = ["dropdb", "--if-exists", database]
    else:  # pragma: no cover - internal programming error
        raise ValueError(f"Unsupported database admin action: {action}")
    return ["runuser", "-u", "postgres", "--", *command]


def remote_admin_command(
    action: str,
    admin_target: DatabaseTarget,
    app_target: DatabaseTarget,
    database: str,
) -> list[str]:
    base = connection_args(admin_target, include_database=False)
    if action == "create":
        return ["createdb", *base, "--owner", app_target.username, database]
    if action == "drop":
        return ["dropdb", *base, "--if-exists", database]
    raise ValueError(f"Unsupported database admin action: {action}")


def prune_backups(output_dir: pathlib.Path, *, keep: int) -> list[str]:
    if keep < 1:
        raise ValueError("--keep must be at least 1.")
    dumps = sorted(output_dir.glob("platformdb-*.dump"), key=lambda path: path.stat().st_mtime)
    removed: list[str] = []
    for dump_path in dumps[:-keep]:
        metadata_path = dump_path.with_suffix(".json")
        dump_path.unlink()
        removed.append(str(dump_path))
        if metadata_path.exists():
            metadata_path.unlink()
            removed.append(str(metadata_path))
    return removed


def prune_unverified_backups(
    output_dir: pathlib.Path,
    *,
    preserve_metadata: pathlib.Path,
) -> list[str]:
    removed: list[str] = []
    for metadata_path in output_dir.glob("platformdb-*.json"):
        if metadata_path == preserve_metadata:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.get("restore_verified"):
            continue
        dump_file = metadata.get("dump_file")
        if isinstance(dump_file, str):
            dump_path = output_dir / dump_file
            if dump_path.exists():
                dump_path.unlink()
                removed.append(str(dump_path))
        metadata_path.unlink()
        removed.append(str(metadata_path))
    return removed


def _inventory_file(path: pathlib.Path) -> tuple[os.stat_result, str, bytes | None]:
    """Hash one closed regular file without following links or accepting races."""

    try:
        path_stat = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Refusing an unsafe or changing existing backup entry.") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise RuntimeError("Refusing unsafe existing backup entry.")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if path.suffix == ".json" else None
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if chunks is not None:
                if total > 1024 * 1024:
                    raise RuntimeError("Refusing oversized existing backup metadata.")
                chunks.append(chunk)
        after = os.fstat(descriptor)
        current_path = path.lstat()
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or any(
            getattr(after, field) != getattr(current_path, field) for field in stable_fields
        ) or total != before.st_size:
            raise RuntimeError("Existing backup entry changed while being inventoried.")
        return after, digest.hexdigest(), b"".join(chunks) if chunks is not None else None
    finally:
        os.close(descriptor)


def backup_inventory(
    output_dir: pathlib.Path,
) -> tuple[tuple[str, int, int, int, int, int, int, str, int, int, int], ...]:
    entries: list[tuple[str, int, int, int, int, int, int, str, int, int, int]] = []
    for path in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if not path.name.startswith("platformdb-") or path.suffix not in {".dump", ".json"}:
            continue
        metadata, content_sha256, _ = _inventory_file(path)
        entries.append(
            (
                path.name,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                content_sha256,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_ctime_ns,
            )
        )
    return tuple(entries)


def validate_complete_backup_pairs(
    output_dir: pathlib.Path,
    entries: tuple[tuple[str, int, int, int, int, int, int, str, int, int, int], ...],
) -> None:
    """Require exact dump/sidecar pairs whose recorded bytes match on disk."""

    by_name = {entry[0]: entry for entry in entries}
    if len(by_name) != len(entries):
        raise RuntimeError("Refusing duplicate existing backup entry names.")
    if not by_name:
        return
    dump_names = {name for name in by_name if name.endswith(".dump")}
    sidecar_names = {name for name in by_name if name.endswith(".json")}
    dump_stems = {name[:-5] for name in dump_names}
    sidecar_stems = {name[:-5] for name in sidecar_names}
    if not dump_names or dump_stems != sidecar_stems:
        raise RuntimeError("Refusing incomplete existing backup pairs.")
    for dump_name in sorted(dump_names):
        sidecar_name = f"{dump_name[:-5]}.json"
        try:
            sidecar_stat, sidecar_digest, sidecar_bytes = _inventory_file(output_dir / sidecar_name)
            metadata = json.loads((sidecar_bytes or b"").decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError("Refusing invalid existing backup metadata.") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("dump_file") != dump_name
            or type(metadata.get("size_bytes")) is not int
            or metadata.get("size_bytes") != by_name[dump_name][5]
            or not isinstance(metadata.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is None
            or metadata["sha256"] != by_name[dump_name][7]
            or sidecar_digest != by_name[sidecar_name][7]
            or (sidecar_stat.st_dev, sidecar_stat.st_ino) != (by_name[sidecar_name][1], by_name[sidecar_name][2])
        ):
            raise RuntimeError("Refusing mismatched existing backup pair.")


def backup_inventory_digest(
    entries: tuple[tuple[str, int, int, int, int, int, int, str, int, int, int], ...],
) -> str:
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _publish_noclobber(temporary_path: pathlib.Path, target_path: pathlib.Path) -> os.stat_result:
    temporary_identity = temporary_path.lstat()
    try:
        os.link(temporary_path, target_path, follow_symlinks=False)
    except FileExistsError as exc:
        raise RuntimeError("Backup destination already exists; refusing to replace it.") from exc
    try:
        published = target_path.lstat()
        temporary = temporary_path.lstat()
        if (published.st_dev, published.st_ino) != (temporary.st_dev, temporary.st_ino):
            raise RuntimeError("Backup publication identity changed.")
    except Exception:
        _unlink_if_same_inode(
            target_path,
            (temporary_identity.st_dev, temporary_identity.st_ino),
        )
        raise
    temporary_path.unlink()
    return published


def _unlink_if_same_inode(path: pathlib.Path, identity: tuple[int, int] | None) -> None:
    if path is None or identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity and stat.S_ISREG(current.st_mode):
        path.unlink()


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_published_backup_pair(
    dump_path: pathlib.Path,
    dump_identity: tuple[int, int] | None,
    metadata_path: pathlib.Path,
    metadata_identity: tuple[int, int] | None,
) -> None:
    """Remove only this operation's exact published inodes after a failed create."""
    removed = False
    failures: list[str] = []
    for path, identity in ((metadata_path, metadata_identity), (dump_path, dump_identity)):
        if identity is None:
            continue
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            failures.append(path.name)
            continue
        try:
            path.unlink()
            removed = True
        except OSError:
            failures.append(path.name)
    if removed:
        _fsync_directory(dump_path.parent)
    if failures:
        raise RuntimeError("Could not roll back the exact newly published backup pair.")


def perform_restore_drill(
    dump_path: pathlib.Path,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    timestamp_slug: str,
    archive_stdin: BinaryIO | None = None,
    restore_as_postgres: bool = False,
    restore_no_tablespaces: bool = False,
    resource_guard: Callable[[], None] | None = None,
) -> int:
    drill_database = f"platform_restore_drill_{timestamp_slug.lower()}_{os.getpid()}"
    use_local_admin = (
        admin_target is None
        and os.geteuid() == 0
        and app_target.host in LOCAL_DATABASE_HOSTS
        and shutil.which("runuser") is not None
    )
    if use_local_admin:
        create_command = local_postgres_admin_command("create", app_target, drill_database)
        drop_command = local_postgres_admin_command("drop", app_target, drill_database)
        admin_command_target = None
    else:
        effective_admin = admin_target or app_target.with_database("postgres")
        create_command = remote_admin_command("create", effective_admin, app_target, drill_database)
        drop_command = remote_admin_command("drop", effective_admin, app_target, drill_database)
        admin_command_target = effective_admin

    created: bool | None = False
    failure_stage = "pre_create_admission"
    failure: BaseException | None = None
    failure_traceback = None
    failure_reason = "none"
    failure_free_bytes: int | None = None
    failure_required_free_bytes: int | None = None
    drop_outcome = "not_required"
    database_absent: bool | None = None
    table_count: int | None = None
    try:
        require_restore_headroom()
        failure_stage = "create_database"
        # A failed connection can leave the server-side create outcome uncertain.
        created = None
        run_command(create_command, target=admin_command_target)
        created = True
        restore_target = app_target.with_database(drill_database)
        failure_stage = "extension_setup"
        for extension in REQUIRED_PLATFORM_EXTENSIONS:
            run_command(
                [
                    "psql",
                    "--no-psqlrc",
                    *connection_args(restore_target),
                    "--command",
                    f"CREATE EXTENSION IF NOT EXISTS {extension} WITH SCHEMA public;",
                ],
                target=restore_target,
                capture_output=True,
            )
        failure_stage = "schema_setup"
        run_command(
            [
                "psql",
                "--no-psqlrc",
                *connection_args(restore_target),
                "--command",
                "CREATE SCHEMA platform AUTHORIZATION CURRENT_USER;",
            ],
            target=restore_target,
            capture_output=True,
        )
        failure_stage = "restore_platform"
        for selector in (
            ("--schema=platform",),
            ("--schema=public",),
        ):
            if selector == ("--schema=public",):
                failure_stage = "restore_public"
            restore_command = [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                *selector,
                *connection_args(restore_target),
            ]
            if restore_no_tablespaces:
                restore_command.append("--no-tablespaces")
            if archive_stdin is None:
                restore_command.append(str(dump_path))
            else:
                archive_stdin.seek(0)
            if restore_as_postgres:
                restore_command = ["runuser", "-u", "postgres", "--", *restore_command]
            run_restore_command(
                restore_command,
                target=restore_target,
                stdin_handle=archive_stdin,
                resource_guard=resource_guard,
            )
        failure_stage = "validate_table_count"
        table_count_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'platform';",
            ],
            target=restore_target,
            capture_output=True,
        )
        table_count = int(table_count_result.stdout.strip())
        if table_count <= 0:
            raise RuntimeError("Restore drill produced no tables in the platform schema.")
        failure_stage = "validate_connectivity"
        connectivity_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT 1;",
            ],
            target=restore_target,
            capture_output=True,
        )
        if connectivity_result.stdout.strip() != "1":
            raise RuntimeError("Restore drill connectivity verification failed.")
        failure_stage = "validate_revision"
        revision_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT version_num FROM public.alembic_version;",
            ],
            target=restore_target,
            capture_output=True,
        )
        revisions = [line.strip() for line in revision_result.stdout.splitlines() if line.strip()]
        if len(revisions) != 1:
            raise RuntimeError("Restore drill did not recover exactly one Alembic revision.")
        failure_stage = "validate_extensions"
        extension_count_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm';",
            ],
            target=restore_target,
            capture_output=True,
        )
        if int(extension_count_result.stdout.strip()) != len(REQUIRED_PLATFORM_EXTENSIONS):
            raise RuntimeError("Restore drill is missing a required platform PostgreSQL extension.")
    except BaseException as exc:
        failure = exc
        failure_traceback = exc.__traceback__
        if isinstance(exc, RestoreGuardStop):
            failure_reason = exc.reason
            failure_free_bytes = exc.free_bytes
            failure_required_free_bytes = exc.required_free_bytes
    if created is True:
        primary_failure_stage = failure_stage if failure is not None else None
        failure_stage = "drop_database"
        try:
            run_command(drop_command, target=admin_command_target)
        except BaseException as exc:
            drop_outcome = "drop_failed"
            if failure is None:
                failure = exc
                failure_traceback = exc.__traceback__
                failure_reason = exc.reason if isinstance(exc, RestoreGuardStop) else "none"
                failure_stage = "drop_database"
        else:
            try:
                absence_command, absence_target = _database_absence_command(
                    drill_database,
                    app_target=app_target,
                    admin_target=admin_command_target,
                    use_local_admin=use_local_admin,
                )
                absence_result = run_command(
                    absence_command,
                    target=absence_target,
                    capture_output=True,
                )
                absence_value = absence_result.stdout.strip()
                if absence_value == "0":
                    database_absent = True
                    drop_outcome = "confirmed_absent"
                elif absence_value == "1":
                    database_absent = False
                    drop_outcome = "database_present"
                    if failure is None:
                        failure = RuntimeError("Restore drill database remained after drop.")
                        failure_traceback = failure.__traceback__
                        failure_stage = "drop_database"
                else:
                    drop_outcome = "absence_unconfirmed"
                    if failure is None:
                        failure = RuntimeError("Restore drill database absence was not confirmed.")
                        failure_traceback = failure.__traceback__
                        failure_stage = "drop_database"
            except BaseException as exc:
                drop_outcome = "absence_unconfirmed"
                if failure is None:
                    failure = exc
                    failure_traceback = exc.__traceback__
                    failure_stage = "drop_database"
        if failure is not None and primary_failure_stage is not None:
            # Preserve the primary failure stage and reason when cleanup also fails.
            failure_stage = primary_failure_stage
    if failure is not None:
        diagnostic = {
            "schema": 1,
            "restore_stage": failure_stage,
            "guard_reason": failure_reason,
            "free_bytes": failure_free_bytes,
            "required_free_bytes": failure_required_free_bytes,
            "temporary_database_created": created,
            "drop_outcome": drop_outcome,
            "temporary_database_absent": database_absent,
        }
        try:
            setattr(failure, "restore_diagnostic", diagnostic)
        except Exception:
            pass
        raise failure.with_traceback(failure_traceback)
    if table_count is None:
        raise RuntimeError("Restore drill did not produce a table count.")
    return table_count


def _database_absence_command(
    database: str,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    use_local_admin: bool,
) -> tuple[list[str], DatabaseTarget | None]:
    if not re.fullmatch(r"platform_restore_drill_[a-z0-9_]{1,80}", database):
        raise ValueError("restore drill database identity is invalid")
    sql = f"SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_database WHERE datname = '{database}') THEN 1 ELSE 0 END;"
    if use_local_admin:
        command = [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--dbname",
            "postgres",
            "--command",
            sql,
        ]
        return command, None
    effective_admin = admin_target or app_target.with_database("postgres")
    command = [
        "psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        *connection_args(effective_admin),
        "--command",
        sql,
    ]
    return command, effective_admin


def require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"Missing required PostgreSQL command(s): {', '.join(missing)}")


def _scratch_mount_snapshot(path: pathlib.Path) -> dict[str, int]:
    target = str(path)
    mount_row: tuple[list[str], list[str]] | None = None
    for line in pathlib.Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        if len(left_fields) < 6 or left_fields[4].replace("\\040", " ") != target:
            continue
        mount_row = (left_fields, right.split())
        break
    if mount_row is None:
        raise RuntimeError("Isolated restore filesystem is not mounted.")
    left_fields, right_fields = mount_row
    if len(right_fields) < 3 or right_fields[0] != "tmpfs" or left_fields[3] != "/":
        raise RuntimeError("Isolated restore filesystem is not a private tmpfs mount.")
    mount_stat = path.stat()
    if (
        mount_stat.st_uid not in {0, pwd.getpwnam("postgres").pw_uid}
        or stat.S_IMODE(mount_stat.st_mode) != 0o700
        or mount_stat.st_dev == pathlib.Path("/run").stat().st_dev
    ):
        raise RuntimeError("Isolated restore filesystem mount properties are unsafe.")
    values = os.statvfs(path)
    total = values.f_blocks * values.f_frsize
    free = values.f_bavail * values.f_frsize
    if total != SCRATCH_TMPFS_BYTES or free <= SCRATCH_TMPFS_STOP_BYTES:
        raise RuntimeError("Isolated restore filesystem is below its bounded capacity.")
    return {"total_bytes": total, "free_bytes": free}


def _scratch_cgroup_snapshot() -> dict[str, int]:
    cgroup_path: str | None = None
    for row in pathlib.Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            cgroup_path = fields[2]
            break
    if cgroup_path is None or ".." in pathlib.PurePosixPath(cgroup_path).parts:
        raise RuntimeError("Scratch restore cgroup could not be identified.")
    root = pathlib.Path("/sys/fs/cgroup").resolve(strict=True)
    directory = (root / cgroup_path.lstrip("/")).resolve(strict=True)
    directory.relative_to(root)

    def read_integer(name: str) -> int:
        value = (directory / name).read_text(encoding="ascii").strip()
        if not value.isdecimal():
            raise RuntimeError("Scratch restore cgroup limit is unavailable.")
        return int(value)

    memory_max = read_integer("memory.max")
    memory_current = read_integer("memory.current")
    swap_max = read_integer("memory.swap.max")
    swap_current = read_integer("memory.swap.current")
    oom_group = read_integer("memory.oom.group")
    if (
        memory_max != SCRATCH_MEMORY_MAX_BYTES
        or swap_max != 0
        or swap_current != 0
        or oom_group != 1
        or memory_current >= memory_max - SCRATCH_MEMORY_STOP_MARGIN_BYTES
    ):
        raise RuntimeError("Scratch restore memory guard stopped the operation.")
    return {
        "memory_max_bytes": memory_max,
        "memory_current_bytes": memory_current,
        "swap_max_bytes": swap_max,
        "swap_current_bytes": swap_current,
        "oom_group": oom_group,
    }


def _scratch_mem_available_bytes() -> int:
    for line in pathlib.Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) == 3 and fields[1].isdecimal() and fields[2] == "kB":
                return int(fields[1]) * 1024
    raise RuntimeError("Host memory availability could not be verified.")


def _scratch_resource_guard(root: pathlib.Path) -> None:
    require_restore_headroom(pathlib.Path("/"))
    if _scratch_mem_available_bytes() < SCRATCH_MEMORY_ADMISSION_BYTES:
        raise RuntimeError("Scratch restore stopped by the host-memory reserve guard.")
    _scratch_cgroup_snapshot()
    _scratch_mount_snapshot(root)


def _open_verified_scratch_archive(args: argparse.Namespace) -> BinaryIO:
    archive_path = pathlib.Path(args._scratch_archive)
    expected_sha256 = args._scratch_archive_sha256
    expected = (
        args._scratch_archive_dev,
        args._scratch_archive_ino,
        args._scratch_archive_size,
    )
    if (
        any(type(value) is not int or value < 0 for value in expected)
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RuntimeError("Scratch restore archive identity is invalid.")
    descriptor = os.open(
        archive_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size) != expected
        ):
            raise RuntimeError("Scratch restore archive identity changed.")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or total != before.st_size
            or digest.hexdigest() != expected_sha256
        ):
            raise RuntimeError("Scratch restore archive content changed.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        header = os.read(descriptor, 5)
        if header != b"PGDMP":
            raise RuntimeError("Scratch restore archive format is invalid.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise


def _scratch_worker_main(args: argparse.Namespace) -> int:
    for name in tuple(os.environ):
        if name.startswith("PG"):
            os.environ.pop(name, None)
    root = pathlib.Path(args._scratch_root)
    if (
        not re.fullmatch(r"/run/oldsparky-backup-restore-[0-9a-f]{24}", str(root))
        or not root.is_dir()
    ):
        raise RuntimeError("Scratch restore root is invalid.")
    mount = _scratch_mount_snapshot(root)
    _scratch_cgroup_snapshot()
    if _scratch_mem_available_bytes() < SCRATCH_MEMORY_ADMISSION_BYTES:
        raise RuntimeError("Scratch restore admission memory reserve is unavailable.")
    if os.geteuid() != 0:
        raise RuntimeError("Scratch restore supervisor is not root.")
    archive = _open_verified_scratch_archive(args)
    archive_info = os.fstat(archive.fileno())
    if (archive_info.st_dev, archive_info.st_ino) != (args._scratch_archive_dev, args._scratch_archive_ino):
        archive.close()
        raise RuntimeError("Scratch restore archive bind identity does not match.")

    postgres = pwd.getpwnam("postgres")
    os.chown(root, postgres.pw_uid, postgres.pw_gid)
    pgdata = root / "data"
    socket_dir = root / "socket"
    pglog = root / "postgres.log"
    pgdata.mkdir(mode=0o700)
    socket_dir.mkdir(mode=0o700)
    for path in (pgdata, socket_dir):
        os.chown(path, postgres.pw_uid, postgres.pw_gid)
        os.chmod(path, 0o700)

    def as_postgres(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        return run_command(
            ["runuser", "-u", "postgres", "--", *command],
            capture_output=capture_output,
        )

    bindir_result = run_command(["pg_config", "--bindir"], capture_output=True)
    bindir = pathlib.Path(bindir_result.stdout.strip())
    version_result = run_command([str(bindir / "postgres"), "--version"], capture_output=True)
    if re.search(r"\b16(?:\.|\s|$)", version_result.stdout) is None:
        archive.close()
        raise RuntimeError("Scratch restore requires PostgreSQL 16 binaries.")
    require_commands("runuser")
    pgctl = str(bindir / "pg_ctl")
    initdb = str(bindir / "initdb")
    as_postgres(
        [
            initdb,
            "--pgdata",
            str(pgdata),
            "--username=postgres",
            "--no-locale",
            "--encoding=UTF8",
            "--auth-local=trust",
            "--auth-host=reject",
        ],
        capture_output=True,
    )
    started = False
    primary_error: BaseException | None = None
    table_count: int | None = None
    try:
        _scratch_resource_guard(root)
        server_options = shlex.join(
            [
                "-c",
                "listen_addresses=",
                "-c",
                f"unix_socket_directories={socket_dir}",
                "-c",
                f"port={SCRATCH_PG_PORT}",
                "-c",
                "fsync=on",
                "-c",
                "full_page_writes=on",
                "-c",
                "synchronous_commit=on",
                "-c",
                "dynamic_shared_memory_type=mmap",
                "-c",
                "shared_buffers=64MB",
                "-c",
                "work_mem=4MB",
                "-c",
                "maintenance_work_mem=32MB",
                "-c",
                "max_connections=10",
            ]
        )
        as_postgres(
            [pgctl, "--pgdata", str(pgdata), "--log", str(pglog), "--options", server_options,
             "--wait", "--timeout=30", "start"],
            capture_output=True,
        )
        started = True
        _scratch_resource_guard(root)
        admin_target = DatabaseTarget(
            host=str(socket_dir),
            port=SCRATCH_PG_PORT,
            username="postgres",
            password=None,
            database="postgres",
        )
        _scratch_create_application_role(admin_target)
        scratch_target = DatabaseTarget(
            host=str(socket_dir),
            port=SCRATCH_PG_PORT,
            username=SCRATCH_APP_ROLE,
            password=None,
            database="postgres",
        )
        table_count = perform_restore_drill(
            archive_path,
            app_target=scratch_target,
            admin_target=admin_target,
            timestamp_slug=args._scratch_timestamp_slug,
            archive_stdin=archive,
            restore_as_postgres=True,
            restore_no_tablespaces=True,
            resource_guard=lambda: _scratch_resource_guard(root),
        )
        _scratch_resource_guard(root)
    except BaseException as exc:
        primary_error = exc
    finally:
        if started or (pgdata / "postmaster.pid").exists():
            try:
                as_postgres(
                    [pgctl, "--pgdata", str(pgdata), "--mode=fast", "--wait", "--timeout=20", "stop"],
                    capture_output=True,
                )
            except BaseException as stop_error:
                if primary_error is None:
                    primary_error = stop_error
        archive.close()
    if primary_error is not None:
        raise primary_error
    if table_count is None or (pgdata / "postmaster.pid").exists():
        raise RuntimeError("Scratch restore server teardown was not confirmed.")
    print(json.dumps({"ok": True, "restored_table_count": table_count}, separators=(",", ":")))
    return 0


def _scratch_create_application_role(admin_target: DatabaseTarget) -> None:
    run_command(
        [
            "psql",
            "--no-psqlrc",
            *connection_args(admin_target),
            "--command",
            f"CREATE ROLE {SCRATCH_APP_ROLE} LOGIN;",
        ],
        target=admin_target,
        capture_output=True,
    )


def _scratch_unit_command(
    *,
    mountpoint: pathlib.Path,
    archive_path: pathlib.Path,
    archive_stat: os.stat_result,
    archive_sha256: str,
    timestamp_slug: str,
    unit_name: str,
) -> list[str]:
    source_script = pathlib.Path(__file__).resolve(strict=True)
    source_policy = source_script.with_name("platform_disk_policy.py").resolve(strict=True)
    python_binary = pathlib.Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    archive_target = mountpoint / "archive.dump"
    worker_target = mountpoint / "platform_backup_restore_drill.py"
    policy_target = mountpoint / "platform_disk_policy.py"
    python_target = mountpoint / "python3"
    command = [
        "systemd-run",
        "--system",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit_name}",
        "--property=Type=exec",
        f"--property=TemporaryFileSystem={mountpoint}:rw,size={SCRATCH_TMPFS_BYTES},mode=0700",
        f"--property=BindReadOnlyPaths={archive_path}:{archive_target}",
        f"--property=BindReadOnlyPaths={source_script}:{worker_target}",
        f"--property=BindReadOnlyPaths={source_policy}:{policy_target}",
        f"--property=BindReadOnlyPaths={python_binary}:{python_target}",
        f"--property=MemoryMax={SCRATCH_MEMORY_MAX_BYTES}",
        "--property=MemorySwapMax=0",
        "--property=RuntimeMaxSec=900s",
        "--property=TimeoutStopSec=5s",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=kill",
        "--property=PrivateNetwork=yes",
        "--property=PrivateDevices=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=SystemCallArchitectures=native",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=tmpfs",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=CAP_CHOWN CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE",
        "--property=InaccessiblePaths=/opt/oldsparky/platform/shared",
        "--property=InaccessiblePaths=/var/lib/postgresql",
        "--property=InaccessiblePaths=/var/run/postgresql",
        "--property=InaccessiblePaths=/run/postgresql",
        "--property=InaccessiblePaths=/etc/postgresql",
        "--property=InaccessiblePaths=/root",
        "--setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--setenv=LANG=C.UTF-8",
        "--setenv=LC_ALL=C.UTF-8",
        "--setenv=SYSTEMD_COLORS=0",
        "--setenv=SYSTEMD_PAGER=cat",
        "--",
        str(python_target),
        "-I",
        "-B",
        str(worker_target),
        SCRATCH_WORKER_MODE,
        "--_scratch-root",
        str(mountpoint),
        "--_scratch-archive",
        str(archive_target),
        "--_scratch-archive-dev",
        str(archive_stat.st_dev),
        "--_scratch-archive-ino",
        str(archive_stat.st_ino),
        "--_scratch-archive-size",
        str(archive_stat.st_size),
        "--_scratch-archive-sha256",
        archive_sha256,
        "--_scratch-timestamp-slug",
        timestamp_slug,
    ]
    if any("ReadWritePaths=" in part for part in command):
        raise RuntimeError("Scratch unit must not override its private tmpfs mount.")
    return command


def _scratch_control_group_snapshot(control_group: str) -> dict[str, int]:
    if not control_group.startswith("/system.slice/") or ".." in pathlib.PurePosixPath(control_group).parts:
        raise RuntimeError("Scratch restore service cgroup is outside system.slice.")
    root = pathlib.Path("/sys/fs/cgroup").resolve(strict=True)
    directory = (root / control_group.lstrip("/")).resolve(strict=True)
    directory.relative_to(root)

    def read(name: str) -> str:
        return (directory / name).read_text(encoding="ascii").strip()

    values = {
        "memory_max_bytes": int(read("memory.max")),
        "memory_current_bytes": int(read("memory.current")),
        "memory_swap_max_bytes": int(read("memory.swap.max")),
        "memory_swap_current_bytes": int(read("memory.swap.current")),
        "memory_oom_group": int(read("memory.oom.group")),
    }
    events = {}
    for row in read("memory.events").splitlines():
        fields = row.split()
        if len(fields) == 2 and fields[0] in {"oom", "oom_kill", "high", "max"} and fields[1].isdecimal():
            events[fields[0]] = int(fields[1])
    values["oom_kill_events"] = events.get("oom_kill", 0)
    if (
        values["memory_max_bytes"] != SCRATCH_MEMORY_MAX_BYTES
        or values["memory_swap_max_bytes"] != 0
        or values["memory_swap_current_bytes"] != 0
        or values["memory_oom_group"] != 1
        or values["memory_current_bytes"] >= SCRATCH_MEMORY_MAX_BYTES - SCRATCH_MEMORY_STOP_MARGIN_BYTES
        or values["oom_kill_events"] != 0
    ):
        raise RuntimeError("Scratch restore service exceeded its effective memory bounds.")
    return values


def _scratch_service_snapshot(unit_name: str) -> dict[str, Any]:
    keys = (
        "LoadState",
        "ActiveState",
        "SubState",
        "ControlGroup",
        "ExecMainPID",
        "ExecMainStatus",
        "Result",
        "TemporaryFileSystem",
        "BindReadOnlyPaths",
        "InaccessiblePaths",
        "RestrictAddressFamilies",
        "MemoryMax",
        "MemorySwapMax",
        "OOMPolicy",
        "PrivateNetwork",
        "ProtectSystem",
        "ProtectHome",
        "NoNewPrivileges",
        "ReadWritePaths",
    )
    completed = subprocess.run(
        ["systemctl", "show", "--no-pager", *[f"--property={key}" for key in keys], unit_name],
        check=False,
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_PAGER": "cat",
        },
        timeout=2,
    )
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError("Scratch restore service properties could not be verified.")
    properties = dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )
    return {"properties": properties, "load_state": properties.get("LoadState")}


def _scratch_expected_properties(snapshot: dict[str, Any], *, unit_name: str, mountpoint: pathlib.Path) -> str:
    properties = snapshot.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Scratch restore service properties are malformed.")
    expected = {
        "MemoryMax": str(SCRATCH_MEMORY_MAX_BYTES),
        "MemorySwapMax": "0",
        "OOMPolicy": "kill",
        "PrivateNetwork": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "ProtectSystem": "strict",
        "ProtectHome": "tmpfs",
        "NoNewPrivileges": "yes",
    }
    if any(properties.get(key, "").lower() != value.lower() for key, value in expected.items()):
        raise RuntimeError("Scratch restore service properties do not match the closed policy.")
    if properties.get("ReadWritePaths") not in {None, ""}:
        raise RuntimeError("Scratch restore service has an unexpected writable-path override.")
    if properties.get("LoadState") != "loaded" or properties.get("ActiveState") != "active":
        raise RuntimeError("Scratch restore service is not active while it is being checked.")
    control_group = properties.get("ControlGroup", "")
    if not control_group.startswith("/system.slice/") or ".." in pathlib.PurePosixPath(control_group).parts:
        raise RuntimeError("Scratch restore service cgroup is invalid.")
    if control_group != f"/system.slice/{unit_name}":
        raise RuntimeError("Scratch restore service cgroup does not match its unique unit.")
    pid_text = properties.get("ExecMainPID", "0")
    if pid_text in {"", "0"} or not pid_text.isdecimal():
        raise RuntimeError("Scratch restore worker process identity is unavailable.")
    try:
        process_cgroup = None
        for row in pathlib.Path(f"/proc/{pid_text}/cgroup").read_text(encoding="ascii").splitlines():
            fields = row.split(":", 2)
            if len(fields) == 3 and fields[0] == "0":
                process_cgroup = fields[2]
                break
    except OSError as exc:
        raise RuntimeError("Scratch restore worker process identity disappeared during verification.") from exc
    if process_cgroup != control_group:
        raise RuntimeError("Scratch restore worker is outside its reviewed systemd cgroup.")
    tmpfs = properties.get("TemporaryFileSystem", "")
    if str(mountpoint) not in tmpfs:
        raise RuntimeError("Scratch restore tmpfs request is not present in the active unit.")
    inaccessible = properties.get("InaccessiblePaths", "")
    for required in (
        "/opt/oldsparky/platform/shared",
        "/var/lib/postgresql",
        "/var/run/postgresql",
        "/run/postgresql",
        "/etc/postgresql",
        "/root",
    ):
        if required not in inaccessible:
            raise RuntimeError("Scratch restore service can see an out-of-scope credential or cluster path.")
    bind_paths = properties.get("BindReadOnlyPaths", "")
    for required in (
        str(mountpoint / "archive.dump"),
        str(mountpoint / "platform_backup_restore_drill.py"),
        str(mountpoint / "platform_disk_policy.py"),
        str(mountpoint / "python3"),
    ):
        if required not in bind_paths:
            raise RuntimeError("Scratch restore service is missing a required read-only input bind.")
    return control_group


def _scratch_host_guard() -> None:
    require_restore_headroom(pathlib.Path("/"))
    if _scratch_mem_available_bytes() < SCRATCH_MEMORY_ADMISSION_BYTES:
        raise RuntimeError("Scratch restore admission memory reserve is unavailable.")


def _scratch_mountpoint_is_mounted(path: pathlib.Path) -> bool:
    target = str(path)
    for line in pathlib.Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
        left, separator, _ = line.partition(" - ")
        fields = left.split()
        if separator and len(fields) >= 5 and fields[4].replace("\\040", " ") == target:
            return True
    return False


def _scratch_stop_unit(unit_name: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "stop", unit_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "SYSTEMD_PAGER": "cat"},
            timeout=6,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Scratch restore service could not be confirmed stopped.") from exc


def _run_isolated_restore_drill(dump_path: pathlib.Path, *, timestamp_slug: str) -> int:
    """Restore into a capped, disposable PostgreSQL cluster isolated from production."""
    if os.geteuid() != 0:
        raise RuntimeError("Isolated backup verification requires the root maintenance owner.")
    if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", timestamp_slug) is None:
        raise ValueError("Scratch restore timestamp identity is invalid.")
    require_commands("systemd-run", "systemctl", "pg_config", "runuser")
    _scratch_host_guard()
    archive_path = dump_path.absolute()
    archive_descriptor = os.open(
        archive_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        archive_before = os.fstat(archive_descriptor)
        path_before = archive_path.lstat()
        if (
            not stat.S_ISREG(archive_before.st_mode)
            or archive_before.st_uid != 0
            or stat.S_IMODE(archive_before.st_mode) != 0o600
            or archive_before.st_nlink != 1
            or (archive_before.st_dev, archive_before.st_ino)
            != (path_before.st_dev, path_before.st_ino)
            or archive_before.st_size <= 5
        ):
            raise RuntimeError("Scratch restore archive identity is unsafe.")
        archive_hash = hashlib.sha256()
        total_bytes = 0
        while True:
            chunk = os.read(archive_descriptor, 1024 * 1024)
            if not chunk:
                break
            archive_hash.update(chunk)
            total_bytes += len(chunk)
        archive_after = os.fstat(archive_descriptor)
        if (
            total_bytes != archive_before.st_size
            or os.pread(archive_descriptor, 5, 0) != b"PGDMP"
            or (archive_before.st_dev, archive_before.st_ino, archive_before.st_size,
                archive_before.st_mtime_ns, archive_before.st_ctime_ns)
            != (archive_after.st_dev, archive_after.st_ino, archive_after.st_size,
                archive_after.st_mtime_ns, archive_after.st_ctime_ns)
        ):
            raise RuntimeError("Scratch restore archive content or identity is invalid.")
        archive_sha256 = archive_hash.hexdigest()
        os.lseek(archive_descriptor, 0, os.SEEK_SET)
        host_run_stat = pathlib.Path("/run").stat()
        if not stat.S_ISDIR(host_run_stat.st_mode) or host_run_stat.st_uid != 0:
            raise RuntimeError("Scratch restore mount parent is not the trusted root-owned /run directory.")
        token = secrets.token_hex(12)
        mountpoint = pathlib.Path(f"/run/oldsparky-backup-restore-{token}")
        unit_name = f"oldsparky-backup-restore-{token}.service"
        os.mkdir(mountpoint, 0o700)
        mountpoint_stat = mountpoint.lstat()
        mountpoint_identity = (mountpoint_stat.st_dev, mountpoint_stat.st_ino)
        if (
            not stat.S_ISDIR(mountpoint_stat.st_mode)
            or mountpoint_stat.st_uid != 0
            or mountpoint_stat.st_gid != 0
            or stat.S_IMODE(mountpoint_stat.st_mode) != 0o700
            or _scratch_mountpoint_is_mounted(mountpoint)
        ):
            mountpoint.rmdir()
            raise RuntimeError("Scratch restore mountpoint identity is unsafe.")
        command = _scratch_unit_command(
            mountpoint=mountpoint,
            archive_path=archive_path,
            archive_stat=archive_before,
            archive_sha256=archive_sha256,
            timestamp_slug=timestamp_slug,
            unit_name=unit_name,
        )
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_PAGER": "cat",
        }
        process: subprocess.Popen[bytes] | None = None
        stdout_buffer = bytearray()
        stderr_size = 0
        active_control_group: str | None = None
        selector: selectors.BaseSelector | None = None
        launch_started = False
        last_tick = time.monotonic()
        deadline = last_tick + SCRATCH_RUNTIME_SECONDS + 30
        worker_result: dict[str, Any] | None = None
        try:
            _scratch_host_guard()
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                cwd="/",
                close_fds=True,
                bufsize=0,
            )
            launch_started = True
            selector = selectors.DefaultSelector()
            assert process.stdout is not None and process.stderr is not None
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline or now - last_tick > SCRATCH_RUNTIME_SAMPLE_SECONDS * 8:
                    raise RuntimeError("Scratch restore monitor exceeded its bounded runtime or sample interval.")
                _scratch_host_guard()
                snapshot = _scratch_service_snapshot(unit_name)
                properties = snapshot.get("properties", {})
                if properties.get("ActiveState") == "active" and properties.get("ExecMainPID", "0") not in {"", "0"}:
                    active_control_group = _scratch_expected_properties(
                        snapshot, unit_name=unit_name, mountpoint=mountpoint
                    )
                    _scratch_control_group_snapshot(active_control_group)
                elif properties.get("ActiveState") in {"failed", "inactive"}:
                    raise RuntimeError("Scratch restore service exited before its confinement was verified.")
                last_tick = now
                for key, _ in selector.select(timeout=SCRATCH_RUNTIME_SAMPLE_SECONDS):
                    try:
                        data = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                    elif key.data == "stdout":
                        stdout_buffer.extend(data)
                        if len(stdout_buffer) > 4096:
                            raise RuntimeError("Scratch restore worker output exceeded its closed bound.")
                    else:
                        stderr_size += len(data)
                        if stderr_size > 65536:
                            raise RuntimeError("Scratch restore worker error output exceeded its closed bound.")
            return_code = process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream:
                    while True:
                        try:
                            data = os.read(stream.fileno(), 65536)
                        except BlockingIOError:
                            break
                        if not data:
                            break
                        if stream is process.stdout:
                            stdout_buffer.extend(data)
                            if len(stdout_buffer) > 4096:
                                raise RuntimeError("Scratch restore worker output exceeded its closed bound.")
                        else:
                            stderr_size += len(data)
                            if stderr_size > 65536:
                                raise RuntimeError("Scratch restore worker error output exceeded its closed bound.")
            if return_code != 0 or stderr_size != 0:
                raise RuntimeError("Isolated PostgreSQL backup restore did not complete successfully.")
            try:
                decoded = json.loads(stdout_buffer.decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Isolated PostgreSQL backup restore returned an invalid result.") from exc
            if (
                not isinstance(decoded, dict)
                or set(decoded) != {"ok", "restored_table_count"}
                or decoded.get("ok") is not True
                or type(decoded.get("restored_table_count")) is not int
                or decoded["restored_table_count"] <= 0
            ):
                raise RuntimeError("Isolated PostgreSQL backup restore result failed validation.")
            worker_result = decoded
        except BaseException:
            if launch_started:
                _scratch_stop_unit(unit_name)
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            raise
        finally:
            if launch_started:
                final_snapshot = _scratch_service_snapshot(unit_name)
                final_props = final_snapshot["properties"]
                if final_props.get("ActiveState") not in {"inactive", "failed"}:
                    _scratch_stop_unit(unit_name)
                    final_snapshot = _scratch_service_snapshot(unit_name)
                    final_props = final_snapshot["properties"]
                service_stopped = (
                    final_props.get("LoadState") == "not-found"
                    or (
                        final_props.get("ActiveState") in {"inactive", "failed"}
                        and final_props.get("ExecMainPID", "0") in {"", "0"}
                        and final_props.get("ControlGroup", "") == ""
                    )
                )
            else:
                service_stopped = True
            mount_absent = not _scratch_mountpoint_is_mounted(mountpoint)
            try:
                current_mountpoint = mountpoint.lstat()
                mountpoint_stable = (
                    (current_mountpoint.st_dev, current_mountpoint.st_ino) == mountpoint_identity
                    and stat.S_ISDIR(current_mountpoint.st_mode)
                    and current_mountpoint.st_uid == 0
                    and stat.S_IMODE(current_mountpoint.st_mode) == 0o700
                    and not any(mountpoint.iterdir())
                )
                if service_stopped and mount_absent and mountpoint_stable:
                    mountpoint.rmdir()
                    mountpoint_removed = True
                else:
                    mountpoint_removed = False
            except FileNotFoundError:
                mountpoint_removed = False
            if selector is not None:
                selector.close()
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            if not service_stopped or not mount_absent or not mountpoint_removed:
                raise RuntimeError("Scratch restore unit or mount cleanup could not be confirmed.")
        archive_after = os.fstat(archive_descriptor)
        current_path = archive_path.lstat()
        if (
            (archive_after.st_dev, archive_after.st_ino, archive_after.st_size,
             archive_after.st_mtime_ns, archive_after.st_ctime_ns)
            != (archive_before.st_dev, archive_before.st_ino, archive_before.st_size,
                archive_before.st_mtime_ns, archive_before.st_ctime_ns)
            or (current_path.st_dev, current_path.st_ino) != (archive_before.st_dev, archive_before.st_ino)
        ):
            raise RuntimeError("Scratch restore archive changed while it was being verified.")
        if worker_result is None:
            raise RuntimeError("Scratch restore completed without a verified worker result.")
        _scratch_host_guard()
        return worker_result["restored_table_count"]
    finally:
        os.close(archive_descriptor)


def _validate_custom_archive_header(path: pathlib.Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 5 or os.read(descriptor, 5) != b"PGDMP":
            raise RuntimeError("Backup archive is not a valid custom-format PostgreSQL dump.")
    finally:
        os.close(descriptor)


def create_backup(args: argparse.Namespace) -> dict[str, Any]:
    env_file = pathlib.Path(args.env_file)
    output_dir = pathlib.Path(args.output_dir)
    file_env = load_env(env_file)
    merged_env = {**file_env, **os.environ}
    database_url = merged_env.get("PLATFORM_DATABASE_URL")
    if not database_url:
        raise RuntimeError(f"PLATFORM_DATABASE_URL is missing from environment and {env_file}.")

    app_target = parse_database_url(database_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    rotate_existing = bool(getattr(args, "rotate_existing", True))
    preserve_existing = not rotate_existing
    initial_inventory = backup_inventory(output_dir) if preserve_existing else ()
    if preserve_existing and initial_inventory:
        validate_complete_backup_pairs(output_dir, initial_inventory)
    initial_inventory_digest = backup_inventory_digest(initial_inventory)
    timestamp = utc_now()
    timestamp_slug = timestamp.strftime("%Y%m%dT%H%M%SZ")
    dump_path = output_dir / f"platformdb-{timestamp_slug}.dump"
    metadata_path = dump_path.with_suffix(".json")
    if os.path.lexists(dump_path) or os.path.lexists(metadata_path):
        raise RuntimeError("Backup destination already exists; refusing to replace it.")

    # Check before creating the temporary output file; the child monitor then
    # guards the actual archive write against the same filesystem policy.
    require_restore_headroom(
        pathlib.Path("/"), output_dir, phase="backup_admission"
    )

    require_commands("pg_dump")
    if not args.dump_only:
        require_commands("systemd-run", "systemctl")

    dump_fd, dump_tmp_name = tempfile.mkstemp(
        prefix=f".{dump_path.name}.", suffix=".tmp", dir=output_dir
    )
    temporary_dump_path = pathlib.Path(dump_tmp_name)
    os.close(dump_fd)
    temporary_dump_identity = temporary_dump_path.lstat()
    temporary_metadata_path: pathlib.Path | None = None
    temporary_metadata_identity: tuple[int, int] | None = None
    published_dump_identity: tuple[int, int] | None = None
    published_metadata_identity: tuple[int, int] | None = None
    started_at = utc_now()
    restore_verified = False
    restored_table_count: int | None = None

    try:
        run_disk_guarded_command(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--schema=platform",
                "--schema=public",
                *connection_args(app_target),
                "--file",
                str(temporary_dump_path),
            ],
            target=app_target,
            stage="pg_dump",
            disk_paths=(pathlib.Path("/"), output_dir),
        )
        if not temporary_dump_path.is_file() or temporary_dump_path.stat().st_size <= 0:
            raise RuntimeError("pg_dump did not produce a non-empty archive.")
        temporary_dump_stat = temporary_dump_path.lstat()
        if (temporary_dump_stat.st_dev, temporary_dump_stat.st_ino) != (
            temporary_dump_identity.st_dev,
            temporary_dump_identity.st_ino,
        ):
            raise RuntimeError("Temporary backup archive identity changed.")
        if not args.dump_only:
            restored_table_count = _run_isolated_restore_drill(
                temporary_dump_path,
                timestamp_slug=timestamp_slug,
            )
            restore_verified = True
        else:
            _validate_custom_archive_header(temporary_dump_path)

        completed_at = utc_now()
        metadata: dict[str, Any] = {
            "format_version": 2,
            "database": app_target.database,
            "schemas": ["platform", "public"],
            "required_extensions": list(REQUIRED_PLATFORM_EXTENSIONS),
            "dump_file": dump_path.name,
            "size_bytes": temporary_dump_path.stat().st_size,
            "sha256": sha256_file(temporary_dump_path),
            "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed_at.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
            "restore_verified": restore_verified,
            "alembic_revision_verified": restore_verified,
            "restored_table_count": restored_table_count,
            "restore_error": None,
        }
        if preserve_existing:
            before_publish_inventory = backup_inventory(output_dir)
            validate_complete_backup_pairs(output_dir, before_publish_inventory)
            before_publish_digest = backup_inventory_digest(before_publish_inventory)
            if before_publish_inventory != initial_inventory:
                raise RuntimeError("Pre-existing backup inventory changed before publication.")
            metadata.update(
                {
                    "preexisting_archive_count": sum(
                        1 for entry in initial_inventory if entry[0].endswith(".dump")
                    ),
                    "preexisting_sidecar_count": sum(
                        1 for entry in initial_inventory if entry[0].endswith(".json")
                    ),
                    "preexisting_archive_inventory_sha256": initial_inventory_digest,
                    "postexisting_archive_inventory_sha256": before_publish_digest,
                    "preexisting_archives_preserved": True,
                }
            )
        metadata_fd, metadata_tmp_name = tempfile.mkstemp(
            prefix=f".{metadata_path.name}.", suffix=".tmp", dir=output_dir
        )
        temporary_metadata_path = pathlib.Path(metadata_tmp_name)
        temporary_metadata_identity_stat = temporary_metadata_path.lstat()
        temporary_metadata_identity = (
            temporary_metadata_identity_stat.st_dev,
            temporary_metadata_identity_stat.st_ino,
        )
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_dump_path, 0o600)
        os.chmod(temporary_metadata_path, 0o600)
        metadata_stat = temporary_metadata_path.lstat()
        if (metadata_stat.st_dev, metadata_stat.st_ino) != temporary_metadata_identity:
            raise RuntimeError("Temporary backup metadata identity changed.")
        published_dump = _publish_noclobber(temporary_dump_path, dump_path)
        published_dump_identity = (published_dump.st_dev, published_dump.st_ino)
        published_metadata = _publish_noclobber(temporary_metadata_path, metadata_path)
        published_metadata_identity = (published_metadata.st_dev, published_metadata.st_ino)
        temporary_metadata_path = None

        removed: list[str] = []
        if restore_verified and rotate_existing:
            removed.extend(prune_unverified_backups(output_dir, preserve_metadata=metadata_path))
            removed.extend(prune_backups(output_dir, keep=args.keep))
        postexisting_archive_count: int | None = None
        if preserve_existing:
            post_publish_inventory = backup_inventory(output_dir)
            validate_complete_backup_pairs(output_dir, post_publish_inventory)
            postexisting_archive_count = sum(
                1 for entry in post_publish_inventory if entry[0].endswith(".dump")
            )
            expected_archive_count = sum(
                1 for entry in initial_inventory if entry[0].endswith(".dump")
            ) + 1
            if postexisting_archive_count != expected_archive_count:
                raise RuntimeError("Preserve mode did not retain exactly one new complete backup pair.")
        result = {
            "ok": True,
            **metadata,
            "rotation_mode": "rotate-existing" if rotate_existing else "preserve-existing",
            "metadata_file": str(metadata_path),
            "removed": removed,
            "postexisting_archive_count": postexisting_archive_count,
        }
        return result
    finally:
        active_exception = sys.exception()
        _unlink_if_same_inode(
            temporary_dump_path,
            (temporary_dump_identity.st_dev, temporary_dump_identity.st_ino),
        )
        _unlink_if_same_inode(temporary_metadata_path, temporary_metadata_identity)
        if preserve_existing:
            if active_exception is not None:
                try:
                    _rollback_published_backup_pair(
                        dump_path,
                        published_dump_identity,
                        metadata_path,
                        published_metadata_identity,
                    )
                except Exception as rollback_error:
                    setattr(
                        active_exception,
                        "_backup_preservation_integrity",
                        "new_pair_rollback_failed",
                    )
                    active_exception.add_note(str(rollback_error))
            try:
                current_inventory = backup_inventory(output_dir)
                validate_complete_backup_pairs(output_dir, current_inventory)
                owned = {dump_path.name: published_dump_identity}
                owned[metadata_path.name] = published_metadata_identity
                current_old_inventory = tuple(
                    entry
                    for entry in current_inventory
                    if owned.get(entry[0]) != (entry[1], entry[2])
                )
                if current_old_inventory != initial_inventory:
                    raise RuntimeError("Pre-existing backup inventory changed during preserve mode.")
            except Exception as integrity_error:
                if active_exception is None:
                    try:
                        _rollback_published_backup_pair(
                            dump_path,
                            published_dump_identity,
                            metadata_path,
                            published_metadata_identity,
                        )
                    except Exception as rollback_error:
                        setattr(
                            integrity_error,
                            "_backup_preservation_integrity",
                            "new_pair_rollback_failed",
                        )
                        integrity_error.add_note(str(rollback_error))
                    setattr(
                        integrity_error,
                        "_backup_preservation_integrity",
                        getattr(
                            integrity_error,
                            "_backup_preservation_integrity",
                            "preexisting_inventory_changed",
                        ),
                    )
                    raise
                setattr(
                    active_exception,
                    "_backup_preservation_integrity",
                    getattr(
                        active_exception,
                        "_backup_preservation_integrity",
                        "preexisting_inventory_changed",
                    ),
                )


def print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print("[OK] Platform database backup is valid")
    print(f"[OK] Archive: {result['dump_file']}")
    print(f"[OK] SHA256: {result['sha256']}")
    print(f"[OK] Restore verified: {result['restore_verified']}")
    if result.get("restored_table_count") is not None:
        print(f"[OK] Restored platform tables: {result['restored_table_count']}")
    if result.get("age_hours") is not None:
        print(f"[OK] Backup age: {result['age_hours']} hours")


def verify_existing_dump(args: argparse.Namespace) -> dict[str, Any]:
    dump_path = pathlib.Path(args.verify_dump).resolve()
    if not dump_path.is_file() or dump_path.suffix != ".dump":
        raise RuntimeError("--verify-dump must reference an existing .dump archive.")
    require_commands("systemd-run", "systemctl")
    table_count = _run_isolated_restore_drill(
        dump_path,
        timestamp_slug=utc_now().strftime("%Y%m%dT%H%M%SZ"),
    )
    return {
        "ok": True,
        "dump_file": str(dump_path),
        "sha256": sha256_file(dump_path),
        "restore_verified": True,
        "alembic_revision_verified": True,
        "restored_table_count": table_count,
    }


def main() -> int:
    args = parse_args()
    try:
        if getattr(args, "_scratch_restore_worker", False):
            required_worker_values = (
                args._scratch_root,
                args._scratch_archive,
                args._scratch_archive_dev,
                args._scratch_archive_ino,
                args._scratch_archive_size,
                args._scratch_archive_sha256,
                args._scratch_timestamp_slug,
            )
            if any(value is None for value in required_worker_values):
                raise ValueError("Scratch restore worker input is incomplete.")
            return _scratch_worker_main(args)
        selected_modes = (
            int(args.check_latest)
            + int(getattr(args, "verify_latest_existing", False))
            + int(args.dump_only)
            + int(args.verify_dump is not None)
        )
        if selected_modes > 1:
            raise ValueError(
                "--dump-only, --check-latest, --verify-latest-existing, and --verify-dump "
                "are mutually exclusive."
            )
        if args.verify_dump is not None:
            result = verify_existing_dump(args)
        elif getattr(args, "verify_latest_existing", False):
            result = verify_latest_existing_backup(
                pathlib.Path(args.output_dir),
                pathlib.Path(args.env_file),
                max_age_hours=args.max_age_hours,
                admin_database_url=args.admin_database_url,
            )
        elif args.check_latest:
            result = check_latest_backup(pathlib.Path(args.output_dir), max_age_hours=args.max_age_hours)
        else:
            result = create_backup(args)
        print_result(result, as_json=args.as_json)
        return 0
    except Exception as exc:
        error_message = str(exc)
        integrity_status = getattr(exc, "_backup_preservation_integrity", None)
        if integrity_status in {
            "preexisting_inventory_changed",
            "new_pair_rollback_failed",
        }:
            error_message += f" [backup_integrity={integrity_status}]"
        if args.as_json:
            report: dict[str, Any] = {"ok": False, "error": error_message}
            diagnostic = getattr(exc, "restore_diagnostic", None)
            if _valid_restore_diagnostic(diagnostic):
                report["restore_diagnostic"] = diagnostic
            backup_disk_diagnostic = (
                exc.private_backup_disk_diagnostic()
                if isinstance(exc, RestoreGuardStop)
                else None
            )
            if valid_backup_disk_diagnostic(backup_disk_diagnostic):
                report["backup_disk_diagnostic"] = backup_disk_diagnostic
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"[FAIL] {error_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
