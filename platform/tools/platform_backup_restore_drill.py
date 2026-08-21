#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.parse
from typing import Any


DEFAULT_ENV_FILE = pathlib.Path("/opt/oldsparky/platform/shared/.env.platform")
DEFAULT_OUTPUT_DIR = pathlib.Path("/opt/oldsparky/platform/shared/backups")
LOCAL_DATABASE_HOSTS = {None, "", "127.0.0.1", "localhost", "::1"}
REQUIRED_PLATFORM_EXTENSIONS = ("pg_trgm",)


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
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture_output,
        env=command_env(target) if target is not None else None,
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
        if len(revisions) != 1:
            raise RuntimeError("Restore drill did not recover exactly one Alembic revision.")
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
    required = ["pg_dump", "pg_restore"]
    if not args.dump_only:
        required.extend(["createdb", "dropdb", "psql"])
    require_commands(*required)

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now()
    timestamp_slug = timestamp.strftime("%Y%m%dT%H%M%SZ")
    dump_path = output_dir / f"platformdb-{timestamp_slug}.dump"
    temporary_dump_path = output_dir / f".{dump_path.name}.{os.getpid()}.tmp"
    metadata_path = dump_path.with_suffix(".json")
    started_at = utc_now()
    restore_verified = False
    restored_table_count: int | None = None
    restore_error: str | None = None

    try:
        run_command(
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
        )
        if not temporary_dump_path.is_file() or temporary_dump_path.stat().st_size <= 0:
            raise RuntimeError("pg_dump did not produce a non-empty archive.")
        run_command(["pg_restore", "--list", str(temporary_dump_path)], capture_output=True)
        temporary_dump_path.replace(dump_path)
        dump_path.chmod(0o600)

        if not args.dump_only:
            try:
                restored_table_count = perform_restore_drill(
                    dump_path,
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
            "size_bytes": dump_path.stat().st_size,
            "sha256": sha256_file(dump_path),
            "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed_at.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
            "restore_verified": restore_verified,
            "alembic_revision_verified": restore_verified,
            "restored_table_count": restored_table_count,
            "restore_error": restore_error,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        metadata_path.chmod(0o600)
        removed: list[str] = []
        if restore_verified:
            removed.extend(prune_unverified_backups(output_dir, preserve_metadata=metadata_path))
        removed.extend(prune_backups(output_dir, keep=args.keep))
        result = {"ok": restore_error is None, **metadata, "metadata_file": str(metadata_path), "removed": removed}
        if restore_error is not None:
            raise RuntimeError(f"Platform backup was created but restore verification failed: {restore_error}")
        return result
    finally:
        if temporary_dump_path.exists():
            temporary_dump_path.unlink()


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
            result = verify_existing_dump(args)
        elif args.check_latest:
            result = check_latest_backup(pathlib.Path(args.output_dir), max_age_hours=args.max_age_hours)
        else:
            result = create_backup(args)
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
