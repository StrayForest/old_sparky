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
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Any


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


class RestoreGuardStop(RuntimeError):
    """A fixed, non-sensitive reason for stopping an owned restore child."""

    def __init__(
        self,
        reason: str,
        *,
        free_bytes: int | None = None,
        required_free_bytes: int | None = None,
    ) -> None:
        if reason not in _RESTORE_GUARD_MESSAGES:
            raise ValueError("unknown restore guard reason")
        self.reason = reason
        self.free_bytes = free_bytes
        self.required_free_bytes = required_free_bytes
        super().__init__(_RESTORE_GUARD_MESSAGES[reason])


_RESTORE_GUARD_MESSAGES = {
    "disk_floor": "Restore drill stopped by the low-disk safety guard.",
    "disk_unavailable": "Restore drill stopped because disk space could not be verified.",
    "child_unstopped": "Backup or restore command could not be confirmed stopped.",
    "command_start_failed": "Backup or restore command could not be started.",
    "command_failed": "Backup or restore command exited unsuccessfully.",
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
            "and verify it by restoring into an isolated temporary database."
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
        help=(
            "Optional PostgreSQL URL for creating/dropping the temporary database. "
            "On a local root-run deployment, the script uses the postgres OS user."
        ),
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
        help="Restore and verify an existing custom-format platform backup, then remove the test DB.",
    )
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
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
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture_output,
        env=command_env(target) if target is not None else None,
    )


def _required_disk_floor(total_bytes: int) -> int:
    if type(total_bytes) is not int or total_bytes <= 0:
        raise RestoreGuardStop("disk_unavailable")
    fifteen_percent_ceil = (total_bytes * 15 + 99) // 100
    return max(RESTORE_DISK_MINIMUM_BYTES, fifteen_percent_ceil)


def require_restore_headroom(*paths: pathlib.Path) -> None:
    """Fail closed unless every affected filesystem has policy floor plus lead."""

    checked_paths = paths or (pathlib.Path("/"),)
    try:
        snapshots = [snapshot_for_path(path) for path in checked_paths]
    except OSError as exc:
        raise RestoreGuardStop("disk_unavailable") from exc
    for snapshot in snapshots:
        if not snapshot.valid:
            raise RestoreGuardStop("disk_unavailable")
        floor = _required_disk_floor(snapshot.total_bytes)
        required_free_bytes = floor + RESTORE_DISK_LEAD_BYTES
        if snapshot.free_bytes < required_free_bytes:
            raise RestoreGuardStop(
                "disk_floor",
                free_bytes=snapshot.free_bytes,
                required_free_bytes=required_free_bytes,
            )


def _stop_owned_child(process: subprocess.Popen[bytes]) -> None:
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
        raise RestoreGuardStop("child_unstopped") from exc


def run_disk_guarded_command(
    command: list[str],
    *,
    target: DatabaseTarget,
    stage: str,
    disk_paths: tuple[pathlib.Path, ...] = (pathlib.Path("/"),),
) -> None:
    """Run one owned dump/restore child under bounded disk monitoring."""

    if stage not in {"pg_dump", "pg_restore"}:
        raise ValueError("unknown disk-guarded command stage")
    require_restore_headroom(*disk_paths)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=command_env(target),
            close_fds=True,
        )
    except OSError as exc:
        raise RestoreGuardStop("command_start_failed") from exc

    try:
        # Always take a post-start sample, including when pg_restore is short.
        require_restore_headroom(*disk_paths)
        while process.poll() is None:
            time.sleep(RESTORE_DISK_SAMPLE_SECONDS)
            if process.poll() is not None:
                break
            require_restore_headroom(*disk_paths)
        return_code = process.wait()
        # Catch a floor crossing between the final sample and child exit.
        require_restore_headroom(*disk_paths)
    except BaseException:
        if process.poll() is None:
            _stop_owned_child(process)
        raise
    if return_code != 0:
        raise RestoreGuardStop("command_failed")


def run_restore_command(
    command: list[str], *, target: DatabaseTarget
) -> None:
    run_disk_guarded_command(command, target=target, stage="pg_restore")


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
    file_env = load_env(env_file)
    merged_env = {**file_env, **os.environ}
    database_url = merged_env.get("PLATFORM_DATABASE_URL")
    if not database_url:
        raise RuntimeError("PLATFORM_DATABASE_URL is required to run a restore drill.")
    app_target = parse_database_url(database_url)
    admin_url = admin_database_url or merged_env.get("PLATFORM_BACKUP_ADMIN_URL")
    admin_target = parse_database_url(admin_url, require_platformdb=False) if admin_url else None
    require_commands("pg_restore", "createdb", "dropdb", "psql")
    run_command(["pg_restore", "--list", str(dump_path)], capture_output=True)
    try:
        table_count = perform_restore_drill(
            dump_path,
            app_target=app_target,
            admin_target=admin_target,
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


def backup_inventory(output_dir: pathlib.Path) -> tuple[tuple[str, int, int, int, int, int, int], ...]:
    entries: list[tuple[str, int, int, int, int, int, int]] = []
    for path in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if not path.name.startswith("platformdb-") or path.suffix not in {".dump", ".json"}:
            continue
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("Refusing unsafe existing backup entry.")
        entries.append(
            (
                path.name,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        )
    return tuple(entries)


def backup_inventory_digest(
    entries: tuple[tuple[str, int, int, int, int, int, int], ...],
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


def perform_restore_drill(
    dump_path: pathlib.Path,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    timestamp_slug: str,
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
            run_restore_command(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    *selector,
                    *connection_args(restore_target),
                    str(dump_path),
                ],
                target=restore_target,
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


def create_backup(args: argparse.Namespace) -> dict[str, Any]:
    env_file = pathlib.Path(args.env_file)
    output_dir = pathlib.Path(args.output_dir)
    file_env = load_env(env_file)
    merged_env = {**file_env, **os.environ}
    database_url = merged_env.get("PLATFORM_DATABASE_URL")
    if not database_url:
        raise RuntimeError(f"PLATFORM_DATABASE_URL is missing from environment and {env_file}.")

    app_target = parse_database_url(database_url)
    admin_url = args.admin_database_url or merged_env.get("PLATFORM_BACKUP_ADMIN_URL")
    admin_target = parse_database_url(admin_url, require_platformdb=False) if admin_url else None
    output_dir.mkdir(parents=True, exist_ok=True)
    rotate_existing = bool(getattr(args, "rotate_existing", True))
    preserve_existing = not rotate_existing
    initial_inventory = backup_inventory(output_dir) if preserve_existing else ()
    initial_inventory_digest = backup_inventory_digest(initial_inventory)
    timestamp = utc_now()
    timestamp_slug = timestamp.strftime("%Y%m%dT%H%M%SZ")
    dump_path = output_dir / f"platformdb-{timestamp_slug}.dump"
    metadata_path = dump_path.with_suffix(".json")
    if os.path.lexists(dump_path) or os.path.lexists(metadata_path):
        raise RuntimeError("Backup destination already exists; refusing to replace it.")

    # Check before creating the temporary output file; the child monitor then
    # guards the actual archive write against the same filesystem policy.
    require_restore_headroom(pathlib.Path("/"), output_dir)

    required = ["pg_dump", "pg_restore"]
    if not args.dump_only:
        required.extend(["createdb", "dropdb", "psql"])
    require_commands(*required)

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
    restore_error: str | None = None

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
        run_command(["pg_restore", "--list", str(temporary_dump_path)], capture_output=True)

        if not args.dump_only:
            try:
                restored_table_count = perform_restore_drill(
                    temporary_dump_path,
                    app_target=app_target,
                    admin_target=admin_target,
                    timestamp_slug=timestamp_slug,
                )
                restore_verified = True
            except Exception as exc:
                restore_error = str(exc)

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
            "restore_error": restore_error,
        }
        if preserve_existing:
            before_publish_inventory = backup_inventory(output_dir)
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
        try:
            published_metadata = _publish_noclobber(temporary_metadata_path, metadata_path)
            published_metadata_identity = (published_metadata.st_dev, published_metadata.st_ino)
            temporary_metadata_path = None
        except Exception:
            _unlink_if_same_inode(dump_path, published_dump_identity)
            published_dump_identity = None
            raise

        removed: list[str] = []
        if restore_verified and rotate_existing:
            removed.extend(prune_unverified_backups(output_dir, preserve_metadata=metadata_path))
            removed.extend(prune_backups(output_dir, keep=args.keep))
        result = {
            "ok": restore_error is None,
            **metadata,
            "rotation_mode": "rotate-existing" if rotate_existing else "preserve-existing",
            "metadata_file": str(metadata_path),
            "removed": removed,
        }
        if restore_error is not None:
            raise RuntimeError(f"Platform backup was created but restore verification failed: {restore_error}")
        return result
    finally:
        active_exception = sys.exception()
        _unlink_if_same_inode(
            temporary_dump_path,
            (temporary_dump_identity.st_dev, temporary_dump_identity.st_ino),
        )
        _unlink_if_same_inode(temporary_metadata_path, temporary_metadata_identity)
        if preserve_existing:
            try:
                current_inventory = backup_inventory(output_dir)
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
                    setattr(
                        integrity_error,
                        "_backup_preservation_integrity",
                        "preexisting_inventory_changed",
                    )
                    raise
                setattr(
                    active_exception,
                    "_backup_preservation_integrity",
                    "preexisting_inventory_changed",
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
    file_env = load_env(pathlib.Path(args.env_file))
    merged_env = {**file_env, **os.environ}
    database_url = merged_env.get("PLATFORM_DATABASE_URL")
    if not database_url:
        raise RuntimeError("PLATFORM_DATABASE_URL is required to run a restore drill.")
    app_target = parse_database_url(database_url)
    admin_url = args.admin_database_url or merged_env.get("PLATFORM_BACKUP_ADMIN_URL")
    admin_target = parse_database_url(admin_url, require_platformdb=False) if admin_url else None
    require_commands("pg_restore", "createdb", "dropdb", "psql")
    run_command(["pg_restore", "--list", str(dump_path)], capture_output=True)
    table_count = perform_restore_drill(
        dump_path,
        app_target=app_target,
        admin_target=admin_target,
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
        if (
            getattr(exc, "_backup_preservation_integrity", None)
            == "preexisting_inventory_changed"
        ):
            error_message += " [backup_integrity=preexisting_inventory_changed]"
        if args.as_json:
            report: dict[str, Any] = {"ok": False, "error": error_message}
            diagnostic = getattr(exc, "restore_diagnostic", None)
            if _valid_restore_diagnostic(diagnostic):
                report["restore_diagnostic"] = diagnostic
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"[FAIL] {error_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
