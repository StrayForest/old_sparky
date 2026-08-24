#!/usr/bin/env python3
"""Render least-privilege runtime env files from the root-only canonical env."""

from __future__ import annotations

import argparse
import grp
import os
from pathlib import Path
import pwd
import shlex
import stat
import tempfile

from platform_safe_env_exec import load_env_file

DEFAULT_SOURCE = Path("/opt/oldsparky/platform/shared/.env.platform")
DEFAULT_OUTPUT_DIR = Path("/opt/oldsparky/platform/shared/env")

WEB_KEYS = frozenset({
    "PLATFORM_ENVIRONMENT",
    "PLATFORM_LOG_LEVEL",
    "PLATFORM_WEB_ORIGIN",
    "PLATFORM_API_PORT",
    "PLATFORM_WEB_BIND_HOST",
    "PLATFORM_WEB_PORT",
    "PLATFORM_API_INTERNAL_ORIGIN",
    "PLATFORM_API_BASE_URL",
    "PLATFORM_SESSION_COOKIE_NAME",
    "NEXT_PUBLIC_PLATFORM_API_BASE_URL",
})

# API owns the public/auth boundary and legitimately needs the complete PLATFORM_*
# configuration. Non-platform process variables are never copied from the source.
API_PREFIXES = ("PLATFORM_",)
RUNTIME_CONTROL_KEYS = frozenset({
    "PLATFORM_ENV_FILE",
    "PLATFORM_RUNTIME_SERVICE",
    "PLATFORM_SHARED_DIR",
    "PLATFORM_PYTHON_BIN",
    "PLATFORM_NODE_BIN",
})

WORKER_KEYS = frozenset({
    "PLATFORM_ENVIRONMENT",
    "PLATFORM_LOG_LEVEL",
    "PLATFORM_WEB_ORIGIN",
    "PLATFORM_DATABASE_URL",
    "PLATFORM_DB_SCHEMA",
    "PLATFORM_REDIS_URL",
    "PLATFORM_CELERY_BROKER_URL",
    "PLATFORM_CELERY_RESULT_BACKEND",
    "PLATFORM_DEADLOCK_AUTOMATION_MAX_TOURNAMENTS_PER_TICK",
    "PLATFORM_DEADLOCK_AUTOMATION_RETRY_BASE_MINUTES",
    "PLATFORM_DEADLOCK_AUTOMATION_RETRY_MAX_MINUTES",
    "PLATFORM_HOME_CONTENT_CACHE_SECONDS",
    "PLATFORM_HOME_CONTENT_STALE_SECONDS",
    "PLATFORM_EXTERNAL_CONTENT_TIMEOUT_SECONDS",
    "PLATFORM_OPENAI_API_KEY",
    "PLATFORM_OPENAI_MODEL",
    "PLATFORM_OPENAI_TIMEOUT_SECONDS",
    "PLATFORM_OBJECT_STORAGE_BACKEND",
    "PLATFORM_R2_ENDPOINT_URL",
    "PLATFORM_R2_ACCESS_KEY_ID",
    "PLATFORM_R2_SECRET_ACCESS_KEY",
    "PLATFORM_R2_BUCKET_NAME",
    "PLATFORM_R2_REGION",
    "PLATFORM_R2_CONNECT_TIMEOUT_SECONDS",
    "PLATFORM_R2_READ_TIMEOUT_SECONDS",
    "PLATFORM_R2_MAX_ATTEMPTS",
    "PLATFORM_MEDIA_PUBLIC_BASE_URL",
    "PLATFORM_MEDIA_STAGING_DIR",
    "PLATFORM_MEDIA_MAX_INPUT_BYTES",
    "PLATFORM_MEDIA_MAX_PIXELS",
    "PLATFORM_MEDIA_MAX_DIMENSION",
    "PLATFORM_MEDIA_MAX_VARIANT_BYTES",
    "PLATFORM_MEDIA_PROCESSING_TIMEOUT_SECONDS",
    "PLATFORM_MEDIA_PROCESSING_MAX_ATTEMPTS",
    "PLATFORM_MEDIA_PROCESSING_STALE_SECONDS",
    "PLATFORM_MEDIA_RETRY_BASE_SECONDS",
    "PLATFORM_MEDIA_RETRY_MAX_SECONDS",
    "PLATFORM_MEDIA_CLEANUP_GRACE_SECONDS",
    "PLATFORM_MEDIA_RECONCILIATION_BATCH_SIZE",
    "PLATFORM_MEDIA_STAGING_ORPHAN_GRACE_SECONDS",
    "PLATFORM_MEDIA_MAX_STAGED_BYTES",
    "PLATFORM_MEDIA_MAX_STAGED_FILES",
    "PLATFORM_MEDIA_PROCESSING_CONCURRENCY",
    "PLATFORM_WORKER_CONCURRENCY",
})

WEB_FORBIDDEN_KEYS = frozenset({
    "PLATFORM_DATABASE_URL",
    "PLATFORM_SECRET_KEY",
    "PLATFORM_REDIS_URL",
    "PLATFORM_CELERY_BROKER_URL",
    "PLATFORM_CELERY_RESULT_BACKEND",
    "PLATFORM_TURNSTILE_SECRET_KEY",
    "PLATFORM_RESEND_API_KEY",
    "PLATFORM_SUPPORT_SMTP_PASSWORD",
    "PLATFORM_R2_ACCESS_KEY_ID",
    "PLATFORM_R2_SECRET_ACCESS_KEY",
    "PLATFORM_OPENAI_API_KEY",
})

WORKER_FORBIDDEN_KEYS = frozenset({
    "PLATFORM_SECRET_KEY",
    "PLATFORM_TURNSTILE_SECRET_KEY",
    "PLATFORM_RESEND_API_KEY",
    "PLATFORM_SUPPORT_SMTP_PASSWORD",
})

SERVICE_GROUPS = {
    "web": "oldsparky-web",
    "api": "oldsparky-api",
    "worker": "oldsparky-worker",
}


def selected_keys(service: str, values: dict[str, str]) -> list[str]:
    if service == "web":
        keys = WEB_KEYS & values.keys()
    elif service == "api":
        keys = {
            key
            for key in values
            if key.startswith(API_PREFIXES) and key not in RUNTIME_CONTROL_KEYS
        }
    elif service == "worker":
        keys = WORKER_KEYS & values.keys()
    else:
        raise ValueError(f"Unknown service: {service}")
    return sorted(keys)


def render_service_env(service: str, values: dict[str, str]) -> str:
    keys = selected_keys(service, values)
    if service == "web" and WEB_FORBIDDEN_KEYS & set(keys):
        raise RuntimeError("Web runtime env contains a backend credential.")
    if service == "worker" and WORKER_FORBIDDEN_KEYS & set(keys):
        raise RuntimeError("Worker runtime env contains an auth/delivery credential.")
    lines = [
        "# Generated from shared/.env.platform; do not edit.",
        f"# Runtime scope: {service}",
    ]
    lines.extend(f"{key}={shlex.quote(values[key])}" for key in keys)
    return "\n".join(lines) + "\n"


def validate_source_metadata(path: Path) -> None:
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != 0:
        raise RuntimeError("Canonical env must be owned by root.")
    if mode & 0o077:
        raise RuntimeError("Canonical env must not be readable or writable by group/others.")
    if not mode & 0o200:
        raise RuntimeError("Canonical env must remain owner-writable.")


def atomic_write(path: Path, content: str, *, gid: int) -> None:
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
            os.fchmod(temporary.fileno(), 0o640)
            os.fchown(temporary.fileno(), 0, gid)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def verify_runtime_files(output_dir: Path) -> None:
    for service, group_name in SERVICE_GROUPS.items():
        path = output_dir / f"{service}.env"
        metadata = path.stat()
        expected_gid = grp.getgrnam(group_name).gr_gid
        if (
            not path.is_file()
            or path.is_symlink()
            or metadata.st_uid != 0
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o640
        ):
            raise RuntimeError(f"Unsafe runtime env metadata: {path}")
        service_user = pwd.getpwnam(group_name)
        if service_user.pw_gid != expected_gid:
            raise RuntimeError(f"{group_name} must use its private primary group.")
        # Re-parse the generated file with the exact runtime parser. This proves
        # that rendering and runtime interpretation cannot silently diverge.
        load_env_file(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify rendered files match the canonical source without writing.",
    )
    args = parser.parse_args()

    values = load_env_file(args.source)
    rendered = {
        service: render_service_env(service, values)
        for service in SERVICE_GROUPS
    }

    if args.apply and args.verify:
        raise RuntimeError("--apply and --verify cannot be combined.")

    if args.verify:
        verify_runtime_files(args.output_dir)
        for service, content in rendered.items():
            path = args.output_dir / f"{service}.env"
            if path.read_text(encoding="utf-8") != content:
                raise RuntimeError(f"Runtime env is stale for service: {service}")
        print("Runtime service envs are current and metadata-safe.")
        return 0

    if not args.apply:
        for service in SERVICE_GROUPS:
            print(f"{service}: {len(selected_keys(service, values))} keys")
        return 0

    if os.geteuid() != 0:
        raise RuntimeError("--apply must run as root.")
    validate_source_metadata(args.source)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    os.chown(args.output_dir, 0, 0)
    os.chmod(args.output_dir, 0o711)
    for service, group_name in SERVICE_GROUPS.items():
        gid = grp.getgrnam(group_name).gr_gid
        atomic_write(args.output_dir / f"{service}.env", rendered[service], gid=gid)
    verify_runtime_files(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
