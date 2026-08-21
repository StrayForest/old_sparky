#!/usr/bin/env python3
"""Prepare an isolated local platform test database and sanitized env file."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
from urllib.parse import quote, urlsplit, urlunsplit


TEST_DATABASE = "platformdb_test"
TEST_DATABASE_USER = "platform_test_user"
TEST_REDIS_DATABASE = 15
REMOVED_PRODUCTION_KEYS = {
    "PLATFORM_R2_ACCESS_KEY_ID",
    "PLATFORM_R2_SECRET_ACCESS_KEY",
    "PLATFORM_R2_ENDPOINT_URL",
    "PLATFORM_R2_BUCKET_NAME",
    "PLATFORM_SUPPORT_SMTP_HOST",
    "PLATFORM_SUPPORT_SMTP_USERNAME",
    "PLATFORM_SUPPORT_SMTP_PASSWORD",
    "PLATFORM_SUPPORT_SMTP_SENDER_EMAIL",
    "PLATFORM_TURNSTILE_SITE_KEY",
    "PLATFORM_TURNSTILE_SECRET_KEY",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create/rotate the isolated local test role and platformdb_test, then "
            "rewrite only the ignored local env file. Secrets are never printed."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env.platform",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_private_env(path: Path) -> tuple[list[str], dict[str, str]]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Local platform env must be a regular file, not a symlink.")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("Local platform env must be owned by the operator with mode 0600.")
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return lines, values


def loopback_database_parts(database_url: str) -> tuple[str, int | None, str]:
    normalized = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlsplit(normalized)
    database = parsed.path.lstrip("/")
    if (
        parsed.scheme not in {"postgresql", "postgres"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or database not in {"platformdb", TEST_DATABASE}
    ):
        raise RuntimeError(
            "Local env must currently target loopback platformdb or platformdb_test."
        )
    return parsed.hostname, parsed.port, parsed.query


def test_database_url(source_url: str, password: str) -> str:
    host, port, query = loopback_database_parts(source_url)
    host_literal = f"[{host}]" if ":" in host else host
    authority = f"{quote(TEST_DATABASE_USER)}:{quote(password, safe='')}@{host_literal}"
    if port is not None:
        authority = f"{authority}:{port}"
    return urlunsplit(
        ("postgresql+asyncpg", authority, f"/{TEST_DATABASE}", query, "")
    )


def test_redis_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    if (
        parsed.scheme not in {"redis", "rediss"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RuntimeError("Local env must use a loopback Redis endpoint.")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{TEST_REDIS_DATABASE}", parsed.query, "")
    )


def sanitized_env(
    lines: list[str],
    *,
    database_url: str,
    redis_url: str,
    test_secret: str,
) -> str:
    replacements = {
        "PLATFORM_ENVIRONMENT": "test",
        "PLATFORM_DATABASE_URL": database_url,
        "PLATFORM_REDIS_URL": redis_url,
        "PLATFORM_SECRET_KEY": test_secret,
        "PLATFORM_OBJECT_STORAGE_BACKEND": "local",
        "PLATFORM_MEDIA_PUBLIC_BASE_URL": "https://cdn.example.test",
        "PLATFORM_TURNSTILE_MODE": "off",
        "PLATFORM_PUBLIC_REGISTRATION_ENABLED": "true",
        "PLATFORM_EMAIL_VERIFICATION_REQUIRED": "false",
        "PLATFORM_SESSION_COOKIE_SECURE": "false",
        "PLATFORM_WEB_ORIGIN": "http://127.0.0.1:3000",
    }
    output: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(raw_line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in REMOVED_PRODUCTION_KEYS:
            continue
        if key in replacements:
            if key not in seen:
                output.append(f"{key}={replacements[key]}")
                seen.add(key)
            continue
        output.append(raw_line)
    for key, value in replacements.items():
        if key not in seen:
            output.append(f"{key}={value}")
    return "\n".join(output).rstrip() + "\n"


def postgres_command(*command: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["runuser", "-u", "postgres", "--", *command],
        check=True,
        capture_output=True,
        input=input_text,
        text=True,
    )
    return result.stdout.strip()


def prepare_postgres(password: str) -> None:
    escaped_password = password.replace("'", "''")
    role_sql = f"""
DO $body$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TEST_DATABASE_USER}') THEN
    ALTER ROLE {TEST_DATABASE_USER} WITH LOGIN PASSWORD '{escaped_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  ELSE
    CREATE ROLE {TEST_DATABASE_USER} WITH LOGIN PASSWORD '{escaped_password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END
$body$;
"""
    postgres_command(
        "psql",
        "--no-psqlrc",
        "--dbname=postgres",
        "--set=ON_ERROR_STOP=1",
        "--quiet",
        input_text=role_sql,
    )
    exists = postgres_command(
        "psql",
        "--no-psqlrc",
        "--dbname=postgres",
        "--tuples-only",
        "--no-align",
        "--command",
        f"SELECT 1 FROM pg_database WHERE datname = '{TEST_DATABASE}';",
    )
    if exists != "1":
        postgres_command("createdb", "--owner", TEST_DATABASE_USER, TEST_DATABASE)
    postgres_command(
        "psql",
        "--no-psqlrc",
        "--dbname=postgres",
        "--set=ON_ERROR_STOP=1",
        "--quiet",
        input_text=(
            f"ALTER DATABASE {TEST_DATABASE} OWNER TO {TEST_DATABASE_USER};\n"
            f"REVOKE CONNECT ON DATABASE {TEST_DATABASE} FROM PUBLIC;\n"
            f"GRANT CONNECT ON DATABASE {TEST_DATABASE} TO {TEST_DATABASE_USER};\n"
        ),
    )


def atomic_write_private(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary.name, 0o600)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    lines, values = read_private_env(args.env_file)
    source_database_url = values.get("PLATFORM_DATABASE_URL", "")
    source_redis_url = values.get("PLATFORM_REDIS_URL", "redis://127.0.0.1:6379/0")
    loopback_database_parts(source_database_url)
    test_redis_url(source_redis_url)
    if not args.apply:
        print(
            f"Test runtime dry-run: database={TEST_DATABASE}; role={TEST_DATABASE_USER}; "
            f"redis_db={TEST_REDIS_DATABASE}; env={args.env_file}; mutated=false."
        )
        return 0

    password = secrets.token_urlsafe(32)
    prepare_postgres(password)
    content = sanitized_env(
        lines,
        database_url=test_database_url(source_database_url, password),
        redis_url=test_redis_url(source_redis_url),
        test_secret=secrets.token_urlsafe(48),
    )
    atomic_write_private(args.env_file, content)
    print(
        f"Test runtime prepared: database={TEST_DATABASE}; role={TEST_DATABASE_USER}; "
        f"redis_db={TEST_REDIS_DATABASE}; env_mode=0600; secrets_printed=false."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
