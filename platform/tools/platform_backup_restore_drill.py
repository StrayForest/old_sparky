#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import urllib.parse
import uuid
from typing import Any

try:
    from .platform_backup_manifest import (
        BackupManifestError,
        REQUIRED_EXTENSIONS,
        build_manifest,
        read_private_prefix,
        read_manifest_file,
        sha256_private_file,
        write_manifest,
    )
except ImportError:  # Direct execution from the tools directory.
    try:
        from tools.platform_backup_manifest import (
            BackupManifestError,
            REQUIRED_EXTENSIONS,
            build_manifest,
            read_private_prefix,
            read_manifest_file,
            sha256_private_file,
            write_manifest,
        )
    except ImportError:
        from platform_backup_manifest import (  # type: ignore[no-redef]
            BackupManifestError,
            REQUIRED_EXTENSIONS,
            build_manifest,
            read_private_prefix,
            read_manifest_file,
            sha256_private_file,
            write_manifest,
        )


DEFAULT_ENV_FILE = pathlib.Path("/opt/oldsparky/platform/shared/.env.platform")
DEFAULT_OUTPUT_DIR = pathlib.Path("/opt/oldsparky/platform/shared/backups")
LOCAL_DATABASE_HOSTS = {None, "", "127.0.0.1", "localhost", "::1"}
REQUIRED_PLATFORM_EXTENSIONS = REQUIRED_EXTENSIONS
ALEMBIC_REVISION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _trusted_alembic_head(source_root: pathlib.Path | None = None) -> str:
    """Resolve the one trusted migration head shipped in the deployed source.

    The release contract currently keeps provenance in ``RELEASE.json`` but
    does not duplicate migration metadata there.  Parsing the immutable
    deployed ``alembic/versions`` graph avoids treating any arbitrary single
    database row as the expected state and fails closed on a branch/missing
    migration graph.
    """

    root = pathlib.Path(source_root or "/opt/oldsparky/platform/current")
    try:
        versions = root.resolve(strict=True) / "alembic" / "versions"
        files = sorted(versions.glob("*.py"))
    except OSError as exc:
        raise RuntimeError("Trusted deployed Alembic source is unavailable.") from exc
    revisions: dict[str, set[str]] = {}
    for path in files:
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise RuntimeError("Trusted deployed Alembic source is invalid.") from exc
        revision: str | None = None
        parents: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in {"revision", "down_revision"}:
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                raise RuntimeError("Trusted deployed Alembic source has invalid revision metadata.")
            if target.id == "revision":
                if not isinstance(value, str) or ALEMBIC_REVISION_RE.fullmatch(value) is None:
                    raise RuntimeError("Trusted deployed Alembic revision is invalid.")
                revision = value
            elif value is None:
                continue
            elif isinstance(value, str):
                parents.add(value)
            elif isinstance(value, (tuple, list)) and all(isinstance(item, str) for item in value):
                parents.update(value)
            else:
                raise RuntimeError("Trusted deployed Alembic parent metadata is invalid.")
        if revision is None or revision in revisions:
            raise RuntimeError("Trusted deployed Alembic graph has duplicate or missing revisions.")
        revisions[revision] = parents
    if not revisions:
        raise RuntimeError("Trusted deployed Alembic graph is empty.")
    referenced = {parent for parents in revisions.values() for parent in parents}
    if referenced - set(revisions):
        raise RuntimeError("Trusted deployed Alembic graph references a missing parent.")
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        raise RuntimeError("Trusted deployed Alembic graph does not have exactly one head.")
    return heads[0]


def expected_alembic_head(source_root: pathlib.Path | None = None) -> str:
    """Public read-only helper for the exact trusted migration head."""

    return _trusted_alembic_head(source_root)


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
    stdout: int | None = None,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    if capture_output and stdout is not None:
        raise ValueError("capture_output and an explicit stdout descriptor are incompatible")
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture_output,
        stdout=stdout,
        pass_fds=pass_fds,
        env=command_env(target) if target is not None else None,
    )


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: pathlib.Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


SECURE_FILE_MODE = 0o600


@dataclasses.dataclass(frozen=True)
class BackupReservation:
    run_id: str
    dump_path: pathlib.Path
    metadata_path: pathlib.Path
    dump_reservation_fd: int
    metadata_reservation_fd: int


def _stat_identity(file_stat: os.stat_result) -> tuple[int, int, int]:
    return file_stat.st_dev, file_stat.st_ino, file_stat.st_nlink


def _validate_secure_stat(
    file_stat: os.stat_result,
    *,
    label: str,
    expected_size: int | None = None,
) -> os.stat_result:
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"{label} must be a regular file.")
    if file_stat.st_nlink != 1:
        raise RuntimeError(f"{label} must not be a hardlink.")
    if stat.S_IMODE(file_stat.st_mode) != SECURE_FILE_MODE:
        raise RuntimeError(f"{label} must have mode 0600.")
    if file_stat.st_uid != os.geteuid() or file_stat.st_gid != os.getegid():
        raise RuntimeError(f"{label} has an unexpected owner or group.")
    if expected_size is not None and file_stat.st_size != expected_size:
        raise RuntimeError(f"{label} has an unexpected size.")
    return file_stat


def _secure_create(path: pathlib.Path, *, label: str) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, SECURE_FILE_MODE)
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Could not create secure {label}: {path}.") from exc
    try:
        file_stat = _validate_secure_stat(os.fstat(descriptor), label=label, expected_size=0)
        return descriptor, file_stat
    except Exception:
        os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _verify_path_matches_fd(
    path: pathlib.Path,
    descriptor: int,
    *,
    label: str,
    expected_size: int | None = None,
) -> os.stat_result:
    descriptor_stat = _validate_secure_stat(
        os.fstat(descriptor), label=label, expected_size=expected_size
    )
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} disappeared while it was being verified.") from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise RuntimeError(f"{label} must not be a symlink.")
    _validate_secure_stat(path_stat, label=label, expected_size=expected_size)
    if _stat_identity(path_stat) != _stat_identity(descriptor_stat):
        raise RuntimeError(f"{label} was replaced while it was being written.")
    return descriptor_stat


def _hash_secure_fd(descriptor: int, *, label: str) -> tuple[str, os.stat_result]:
    before = _validate_secure_stat(os.fstat(descriptor), label=label)
    if before.st_size <= 0:
        raise RuntimeError(f"{label} must be non-empty.")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"Could not hash {label}.") from exc
    after = _validate_secure_stat(os.fstat(descriptor), label=label)
    if (
        _stat_identity(after) != _stat_identity(before)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError(f"{label} changed while it was hashed.")
    return digest.hexdigest(), after


def _best_effort_unlink(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _best_effort_cleanup(paths: tuple[pathlib.Path, ...], directory: pathlib.Path) -> None:
    for path in paths:
        _best_effort_unlink(path)
    try:
        _fsync_directory(directory)
    except OSError:
        pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def newest_metadata(output_dir: pathlib.Path) -> pathlib.Path:
    candidates = sorted(
        output_dir.glob("platformdb-*.json"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    if not candidates:
        raise RuntimeError(f"No retained platform backup metadata found in {output_dir}.")
    return candidates[-1]


def check_latest_backup(output_dir: pathlib.Path, *, max_age_hours: float) -> dict[str, Any]:
    if max_age_hours <= 0:
        raise ValueError("--max-age-hours must be positive.")
    metadata_path = newest_metadata(output_dir)
    try:
        manifest_file = read_manifest_file(
            metadata_path,
            expected_owner=os.geteuid(),
            expected_group=os.getegid(),
            expected_dump_file=metadata_path.with_suffix(".dump").name,
        )
    except BackupManifestError as exc:
        raise RuntimeError(
            f"Latest platform backup manifest is invalid: {metadata_path}: {exc}"
        ) from exc
    manifest = manifest_file.manifest
    if not manifest.restore_verified:
        raise RuntimeError(f"Latest platform backup was not restore-verified: {metadata_path}.")
    if not manifest.alembic_revision_verified:
        raise RuntimeError(f"Latest platform backup did not verify Alembic state: {metadata_path}.")
    completed_at = manifest.completed_at_utc
    age_hours = (utc_now() - completed_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(
            f"Latest restore-verified platform backup is {age_hours:.2f} hours old; "
            f"maximum is {max_age_hours:.2f}."
        )
    dump_path = output_dir / manifest.dump_file
    try:
        actual_sha256, dump_stat = sha256_private_file(
            dump_path,
            label="Platform backup dump",
            expected_owner=os.geteuid(),
            expected_group=os.getegid(),
        )
    except BackupManifestError as exc:
        raise RuntimeError(f"Backup archive referenced by metadata is invalid: {dump_path}.") from exc
    if actual_sha256 != manifest.sha256:
        raise RuntimeError(f"Backup archive checksum does not match metadata: {dump_path}.")
    if dump_stat.st_size != manifest.size_bytes:
        raise RuntimeError(f"Backup archive size does not match metadata: {dump_path}.")
    return {
        "ok": True,
        "metadata_file": str(metadata_path),
        "dump_file": str(dump_path),
        "age_hours": round(age_hours, 3),
        "format_version": manifest.format_version,
        "restore_verified": True,
        "alembic_revision_verified": manifest.alembic_revision_verified,
        "restored_table_count": manifest.restored_table_count,
        "run_id": manifest.run_id,
        "schemas": list(manifest.schemas),
        "sha256": actual_sha256,
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


def prune_backups(
    output_dir: pathlib.Path,
    *,
    keep: int,
    capability: object | None = None,
) -> list[str]:
    _require_supervisor_capability(capability)
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
    capability: object | None = None,
) -> list[str]:
    _require_supervisor_capability(capability)
    removed: list[str] = []
    for metadata_path in output_dir.glob("platformdb-*.json"):
        if metadata_path == preserve_metadata:
            continue
        try:
            manifest_file = read_manifest_file(
                metadata_path,
                expected_owner=os.geteuid(),
                expected_group=os.getegid(),
                expected_dump_file=metadata_path.with_suffix(".dump").name,
            )
        except BackupManifestError:
            continue
        if manifest_file.manifest.restore_verified:
            continue
        dump_path = output_dir / manifest_file.manifest.dump_file
        try:
            read_private_prefix(
                dump_path,
                label="Unverified platform backup dump",
                expected_owner=os.geteuid(),
                expected_group=os.getegid(),
                prefix_bytes=0,
            )
        except BackupManifestError:
            continue
        dump_path.unlink()
        removed.append(str(dump_path))
        metadata_path.unlink()
        removed.append(str(metadata_path))
    return removed


def _require_supervisor_capability(capability: object | None) -> None:
    if capability is None:
        raise RuntimeError("backup pruning is supervisor-owned and requires a capability")
    try:
        import tools.platform_backup_supervisor as supervisor
    except ImportError:
        try:
            from . import platform_backup_supervisor as supervisor
        except ImportError:
            import platform_backup_supervisor as supervisor  # type: ignore[no-redef]
    supervisor.require_mutation_capability(capability, "maintenance")


def perform_restore_drill(
    dump_path: pathlib.Path,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    timestamp_slug: str,
    trusted_alembic_head: object | None = None,
    expected_alembic_head: str | None = None,
    source_root: pathlib.Path | None = None,
) -> int:
    if trusted_alembic_head is not None:
        try:
            import tools.platform_backup_supervisor as supervisor
        except ImportError:
            try:
                from . import platform_backup_supervisor as supervisor
            except ImportError:
                import platform_backup_supervisor as supervisor  # type: ignore[no-redef]
        trusted = supervisor.require_trusted_alembic_head(
            trusted_alembic_head,
            source_root=source_root,
            expected=expected_alembic_head,
        )
        expected_head = trusted.value
    else:
        trusted_head = _trusted_alembic_head(source_root)
        if expected_alembic_head is not None and expected_alembic_head != trusted_head:
            raise RuntimeError(
                "Expected Alembic head does not match the trusted deployed source graph."
            )
        expected_head = trusted_head
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

    created = False
    try:
        run_command(create_command, target=admin_command_target)
        created = True
        restore_target = app_target.with_database(drill_database)
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
        for selector in (
            ("--schema=platform",),
            ("--schema=public",),
        ):
            run_command(
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
        if revisions != [expected_head]:
            raise RuntimeError(
                "Restore drill Alembic revision does not match the trusted deployed head."
            )
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
        return table_count
    finally:
        if created:
            run_command(drop_command, target=admin_command_target)


def require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"Missing required PostgreSQL command(s): {', '.join(missing)}")


def _new_backup_identity(
    output_dir: pathlib.Path,
    timestamp_slug: str,
) -> BackupReservation:
    """Reserve both final names with O_EXCL before any dump process starts."""

    for _ in range(8):
        run_id = uuid.uuid4().hex
        dump_path = output_dir / f"platformdb-{timestamp_slug}-{run_id}.dump"
        metadata_path = dump_path.with_suffix(".json")
        try:
            dump_reservation_fd, _ = _secure_create(
                dump_path, label="backup dump reservation"
            )
        except FileExistsError:
            continue
        try:
            metadata_reservation_fd, _ = _secure_create(
                metadata_path, label="backup manifest reservation"
            )
        except FileExistsError:
            os.close(dump_reservation_fd)
            _best_effort_unlink(dump_path)
            continue
        except Exception:
            os.close(dump_reservation_fd)
            _best_effort_unlink(dump_path)
            raise
        return BackupReservation(
            run_id=run_id,
            dump_path=dump_path,
            metadata_path=metadata_path,
            dump_reservation_fd=dump_reservation_fd,
            metadata_reservation_fd=metadata_reservation_fd,
        )
    raise RuntimeError("Could not allocate a unique platform backup run_id.")


def create_backup(
    args: argparse.Namespace,
    *,
    prune: bool = True,
    capability: object | None = None,
    trusted_alembic_head: object | None = None,
) -> dict[str, Any]:
    """Create one archive/manifest pair.

    Every mutating call requires the supervisor's private in-process
    capability, including the legacy ``prune=True`` default.  The command-line
    mutation path is routed by :func:`main` through the supervisor; focused
    producer tests inject the same private capability explicitly.
    """
    if capability is None:
        capability = getattr(args, "_supervisor_capability", None)
    if capability is None:
        raise RuntimeError(
            "supervisor-owned backup creation requires an in-process capability"
        )
    try:
        import tools.platform_backup_supervisor as supervisor
    except ImportError:
        try:
            from . import platform_backup_supervisor as supervisor
        except ImportError:
            import platform_backup_supervisor as supervisor  # type: ignore[no-redef]
    supervisor.require_mutation_capability(capability, "maintenance")
    trusted_head = None
    if not args.dump_only:
        trusted_head = supervisor.require_trusted_alembic_head(trusted_alembic_head)
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
    required = ["pg_dump", "pg_restore"]
    if not args.dump_only:
        required.extend(["createdb", "dropdb", "psql"])
    require_commands(*required)

    try:
        output_dir_stat = output_dir.lstat()
    except FileNotFoundError:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir_stat = output_dir.lstat()
    if stat.S_ISLNK(output_dir_stat.st_mode) or not stat.S_ISDIR(output_dir_stat.st_mode):
        raise RuntimeError(f"Backup output path must be a regular directory: {output_dir}.")
    timestamp = utc_now()
    timestamp_slug = timestamp.strftime("%Y%m%dT%H%M%SZ")
    # The timestamp is human-readable ordering metadata only.  The run ID is
    # the identity boundary that prevents same-second creators from sharing a
    # dump or manifest path.
    reservation = _new_backup_identity(output_dir, timestamp_slug)
    run_id = reservation.run_id
    dump_path = reservation.dump_path
    metadata_path = reservation.metadata_path
    temporary_dump_path = output_dir / f".{dump_path.name}.{os.getpid()}.tmp"
    started_at = utc_now()
    restore_verified = False
    restored_table_count: int | None = None
    alembic_revision: str | None = None
    restore_error: str | None = None
    temporary_dump_fd: int | None = None
    temporary_dump_created = False
    metadata_written = False

    try:
        try:
            temporary_dump_fd, _ = _secure_create(
                temporary_dump_path, label="temporary platform backup dump"
            )
            temporary_dump_created = True
        except FileExistsError as exc:
            raise RuntimeError(
                f"Temporary platform backup path is already occupied: {temporary_dump_path}."
            ) from exc
        run_command(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--schema=platform",
                "--schema=public",
                *connection_args(app_target),
            ],
            target=app_target,
            stdout=temporary_dump_fd,
        )
        dump_stat = _verify_path_matches_fd(
            temporary_dump_path,
            temporary_dump_fd,
            label="Temporary platform backup dump",
        )
        if dump_stat.st_size <= 0:
            raise RuntimeError("pg_dump did not produce a non-empty archive.")
        try:
            os.fsync(temporary_dump_fd)
        except OSError as exc:
            raise RuntimeError("Could not fsync the temporary platform backup dump.") from exc
        dump_stat = _verify_path_matches_fd(
            temporary_dump_path,
            temporary_dump_fd,
            label="Temporary platform backup dump",
            expected_size=dump_stat.st_size,
        )
        run_command(
            ["pg_restore", "--list", f"/proc/self/fd/{temporary_dump_fd}"],
            capture_output=True,
            pass_fds=(temporary_dump_fd,),
        )
        _verify_path_matches_fd(
            temporary_dump_path,
            temporary_dump_fd,
            label="Temporary platform backup dump",
            expected_size=dump_stat.st_size,
        )
        archive_sha256, dump_stat = _hash_secure_fd(
            temporary_dump_fd, label="Temporary platform backup dump"
        )
        _verify_path_matches_fd(
            dump_path,
            reservation.dump_reservation_fd,
            label="Reserved platform backup dump",
            expected_size=0,
        )
        _verify_path_matches_fd(
            metadata_path,
            reservation.metadata_reservation_fd,
            label="Reserved platform backup manifest",
            expected_size=0,
        )
        _verify_path_matches_fd(
            temporary_dump_path,
            temporary_dump_fd,
            label="Temporary platform backup dump",
            expected_size=dump_stat.st_size,
        )
        temporary_dump_path.replace(dump_path)
        temporary_dump_created = False
        os.close(temporary_dump_fd)
        temporary_dump_fd = None
        try:
            published_sha256, published_stat = sha256_private_file(
                dump_path,
                label="Published platform backup dump",
                expected_owner=os.geteuid(),
                expected_group=os.getegid(),
            )
        except BackupManifestError as exc:
            raise RuntimeError("Published platform backup dump failed identity validation.") from exc
        if published_sha256 != archive_sha256 or published_stat.st_size != dump_stat.st_size:
            raise RuntimeError("Published platform backup dump changed before manifest publication.")
        dump_stat = published_stat
        try:
            _fsync_directory(output_dir)
        except OSError as exc:
            raise RuntimeError("Could not fsync the backup directory after publishing the dump.") from exc

        if not args.dump_only:
            try:
                restored_table_count = perform_restore_drill(
                    dump_path,
                    app_target=app_target,
                    admin_target=admin_target,
                    timestamp_slug=timestamp_slug,
                    trusted_alembic_head=trusted_head,
                )
                restore_verified = True
                assert trusted_head is not None
                alembic_revision = trusted_head.value
            except Exception as exc:
                restore_error = str(exc)

        completed_at = utc_now()
        metadata = build_manifest(
            run_id=run_id,
            dump_file=dump_path.name,
            size_bytes=dump_stat.st_size,
            sha256=archive_sha256,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            duration_seconds=round((completed_at - started_at).total_seconds(), 3),
            restore_verified=restore_verified,
            alembic_revision_verified=restore_verified,
            restored_table_count=restored_table_count,
            restore_error=restore_error,
        )
        write_manifest(metadata_path, metadata)
        metadata_written = True
        removed: list[str] = []
        if restore_verified and prune:
            removed.extend(
                prune_unverified_backups(
                    output_dir,
                    preserve_metadata=metadata_path,
                    capability=capability,
                )
            )
            removed.extend(prune_backups(output_dir, keep=args.keep, capability=capability))
        result = {
            "ok": restore_error is None,
            **metadata,
            "metadata_file": str(metadata_path),
            "removed": removed,
            "alembic_revision": alembic_revision or "unknown",
        }
        if restore_error is not None:
            raise RuntimeError(f"Platform backup was created but restore verification failed: {restore_error}")
        return result
    except Exception:
        if not metadata_written:
            cleanup_paths: list[pathlib.Path] = [dump_path, metadata_path]
            if temporary_dump_created:
                cleanup_paths.append(temporary_dump_path)
            _best_effort_cleanup(tuple(cleanup_paths), output_dir)
        raise
    finally:
        if temporary_dump_fd is not None:
            os.close(temporary_dump_fd)
        os.close(reservation.dump_reservation_fd)
        os.close(reservation.metadata_reservation_fd)


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
        selected_modes = int(args.check_latest) + int(args.dump_only) + int(args.verify_dump is not None)
        if selected_modes > 1:
            raise ValueError("--dump-only, --check-latest, and --verify-dump are mutually exclusive.")
        if args.verify_dump is not None:
            # A production restore drill is a supervisor-owned mutation.  The
            # read-only ``--check-latest`` path below remains available to
            # health/preflight diagnostics.
            raise RuntimeError(
                "--verify-dump is a supervisor-owned restore drill; use platform_backup_supervisor"
            )
        elif args.check_latest:
            result = check_latest_backup(pathlib.Path(args.output_dir), max_age_hours=args.max_age_hours)
        else:
            try:
                from . import platform_backup_supervisor as supervisor
            except ImportError:
                import platform_backup_supervisor as supervisor  # type: ignore[no-redef]
            # No environment variable or numeric FD is accepted as a
            # capability.  The supervisor calls ``create_backup`` in-process.
            result = supervisor.run_backup_entrypoint(
                argparse.Namespace(
                    app_dir=pathlib.Path(args.output_dir).resolve().parents[1],
                    source_release_dir=pathlib.Path("/opt/oldsparky/platform/dist/releases"),
                    keep=args.keep,
                    max_age_hours=args.max_age_hours,
                    env_file=pathlib.Path(args.env_file),
                    output_dir=pathlib.Path(args.output_dir),
                    admin_database_url=args.admin_database_url,
                    as_json=args.as_json,
                ),
                app_dir=pathlib.Path(args.output_dir).resolve().parents[1],
            )
        print_result(result, as_json=args.as_json)
        return 0
    except Exception as exc:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
