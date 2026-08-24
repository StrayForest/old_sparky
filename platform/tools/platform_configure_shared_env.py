#!/usr/bin/env python3
"""Atomically apply the non-secret OldSparky production baseline to shared env."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile


DEFAULT_ENV_FILE = Path("/opt/oldsparky/platform/shared/.env.platform")
CONFIRMATION = "APPLY_PUBLIC_PRODUCTION_BASELINE"
PUBLIC_BASELINE = {
    "PLATFORM_LOG_LEVEL": "INFO",
    "PLATFORM_WEB_ORIGIN": "https://old-sparky.com",
    "PLATFORM_API_HOST": "127.0.0.1",
    "PLATFORM_API_PORT": "8010",
    "PLATFORM_API_WORKERS": "2",
    "PLATFORM_DB_POOL_SIZE": "3",
    "PLATFORM_DB_MAX_OVERFLOW": "1",
    "PLATFORM_DB_POOL_TIMEOUT_SECONDS": "5",
    "PLATFORM_DB_POOL_RECYCLE_SECONDS": "1800",
    "PLATFORM_WORKER_DB_POOL_SIZE": "2",
    "PLATFORM_WORKER_DB_MAX_OVERFLOW": "0",
    "PLATFORM_WORKER_DB_POOL_TIMEOUT_SECONDS": "5",
    "PLATFORM_WORKER_DB_POOL_RECYCLE_SECONDS": "1800",
    "PLATFORM_WORKER_CONCURRENCY": "2",
    "PLATFORM_DB_CONNECTION_BUDGET": "12",
    "PLATFORM_WEB_BIND_HOST": "127.0.0.1",
    "PLATFORM_WEB_PORT": "3000",
    "PLATFORM_API_FORWARDED_ALLOW_IPS": "127.0.0.1",
    "PLATFORM_LOAD_TEST_SOURCE_IPS": "95.217.190.107,2a01:4f9:c012:8011::1",
    "PLATFORM_SHARED_DIR": "/opt/oldsparky/platform/shared",
    "PLATFORM_UPLOAD_DIR": "/opt/oldsparky/platform/shared/uploads",
    "PLATFORM_DB_SCHEMA": "platform",
    "PLATFORM_SESSION_COOKIE_NAME": "__Host-old_sparky_session",
    "PLATFORM_SESSION_TTL_DAYS": "14",
    "PLATFORM_COOKIE_SECURE": "true",
    "PLATFORM_PUBLIC_REGISTRATION_ENABLED": "true",
    "PLATFORM_EMAIL_VERIFICATION_REQUIRED": "true",
    "PLATFORM_EMAIL_VERIFICATION_TTL_MINUTES": "10",
    "PLATFORM_PASSWORD_RESET_TTL_MINUTES": "10",
    "PLATFORM_AUTH_FLOW_TTL_MINUTES": "15",
    "PLATFORM_AUTH_DELIVERY_COOLDOWN_SECONDS": "60",
    "PLATFORM_STEAM_LOGIN_ENABLED": "false",
    "PLATFORM_STEAM_CALLBACK_URL": (
        "https://old-sparky.com/api/v1/auth/steam/callback"
    ),
    "PLATFORM_STEAM_OPENID_TIMEOUT_SECONDS": "5",
    "PLATFORM_CSRF_ENABLED": "true",
    "PLATFORM_AUTH_RATE_LIMIT_ENABLED": "true",
    "PLATFORM_AUTH_LOGIN_WINDOW_SECONDS": "600",
    "PLATFORM_AUTH_LOGIN_ACCOUNT_LIMIT": "8",
    "PLATFORM_AUTH_LOGIN_IP_LIMIT": "60",
    "PLATFORM_AUTH_REGISTER_WINDOW_SECONDS": "600",
    "PLATFORM_AUTH_REGISTER_IP_LIMIT": "5",
    "PLATFORM_AUTH_PROGRESSIVE_DELAY_BASE_SECONDS": "0.15",
    "PLATFORM_AUTH_PROGRESSIVE_DELAY_MAX_SECONDS": "1.5",
    "PLATFORM_AUTH_ADAPTIVE_TURNSTILE_THRESHOLD": "3",
    "PLATFORM_INVITE_RATE_LIMIT_ENABLED": "true",
    "PLATFORM_INVITE_RATE_WINDOW_SECONDS": "900",
    "PLATFORM_INVITE_LOOKUP_USER_LIMIT": "60",
    "PLATFORM_INVITE_LOOKUP_IP_LIMIT": "120",
    "PLATFORM_INVITE_CLAIM_USER_LIMIT": "12",
    "PLATFORM_INVITE_CLAIM_IP_LIMIT": "60",
    "PLATFORM_INVITE_MANAGE_USER_LIMIT": "30",
    "PLATFORM_INVITE_MANAGE_IP_LIMIT": "120",
    "PLATFORM_TURNSTILE_EXPECTED_HOSTNAME": "old-sparky.com",
    "PLATFORM_TURNSTILE_TIMEOUT_SECONDS": "3",
    "PLATFORM_OBJECT_STORAGE_BACKEND": "r2",
    "PLATFORM_R2_REGION": "auto",
    "PLATFORM_R2_CONNECT_TIMEOUT_SECONDS": "3",
    "PLATFORM_R2_READ_TIMEOUT_SECONDS": "10",
    "PLATFORM_R2_MAX_ATTEMPTS": "4",
    "PLATFORM_MEDIA_PUBLIC_BASE_URL": "https://cdn.old-sparky.com",
    "PLATFORM_MEDIA_STAGING_DIR": "/opt/oldsparky/platform/shared/media-staging",
    "PLATFORM_MEDIA_MAX_INPUT_BYTES": "5242880",
    "PLATFORM_MEDIA_MAX_PIXELS": "25000000",
    "PLATFORM_MEDIA_MAX_DIMENSION": "10000",
    "PLATFORM_MEDIA_MAX_VARIANT_BYTES": "524288",
    "PLATFORM_MEDIA_PROCESSING_TIMEOUT_SECONDS": "60",
    "PLATFORM_MEDIA_PROCESSING_MAX_ATTEMPTS": "3",
    "PLATFORM_MEDIA_PROCESSING_STALE_SECONDS": "300",
    "PLATFORM_MEDIA_RETRY_BASE_SECONDS": "10",
    "PLATFORM_MEDIA_RETRY_MAX_SECONDS": "300",
    "PLATFORM_MEDIA_CLEANUP_GRACE_SECONDS": "86400",
    "PLATFORM_MEDIA_RECONCILIATION_BATCH_SIZE": "32",
    "PLATFORM_MEDIA_STAGING_ORPHAN_GRACE_SECONDS": "3600",
    "PLATFORM_MEDIA_MAX_STAGED_BYTES": "536870912",
    "PLATFORM_MEDIA_MAX_STAGED_FILES": "256",
    "PLATFORM_MEDIA_PROCESSING_CONCURRENCY": "1",
    "PLATFORM_MEDIA_RATE_LIMIT_ENABLED": "true",
    "PLATFORM_MEDIA_UPLOAD_WINDOW_SECONDS": "3600",
    "PLATFORM_MEDIA_UPLOAD_USER_LIMIT": "20",
    "PLATFORM_MEDIA_UPLOAD_IP_LIMIT": "60",
    "PLATFORM_MEDIA_UPLOAD_USER_BYTE_LIMIT": "104857600",
    "PLATFORM_HOME_CONTENT_CACHE_SECONDS": "2100",
    "PLATFORM_HOME_CONTENT_STALE_SECONDS": "604800",
    "PLATFORM_EXTERNAL_CONTENT_TIMEOUT_SECONDS": "5",
    "PLATFORM_OPENAI_MODEL": "gpt-5.6-luna",
    "PLATFORM_OPENAI_TIMEOUT_SECONDS": "30",
    "PLATFORM_EMAIL_SENDER_EMAIL": "Old Sparky Arena <noreply@auth.old-sparky.com>",
    "PLATFORM_RESEND_TIMEOUT_SECONDS": "10",
    "PLATFORM_SUPPORT_RECIPIENT_EMAIL": "support@old-sparky.com",
    "PLATFORM_SUPPORT_SMTP_STARTTLS": "true",
    "PLATFORM_SUPPORT_SMTP_SSL": "false",
    "PLATFORM_SUPPORT_RATE_LIMIT_PER_HOUR": "3",
    "PLATFORM_PERF_LOG_ENABLED": "true",
    "PLATFORM_PERF_SLOW_REQUEST_MS": "1000",
    "PLATFORM_PERF_SLOW_DB_MS": "500",
    "PLATFORM_PERF_SQL_COUNT_THRESHOLD": "25",
    "PLATFORM_PERF_LOG_MUTATIONS": "true",
}
PRESERVED_ROLLOUT_FLAGS = frozenset({"PLATFORM_STEAM_LOGIN_ENABLED"})
ROLLOUT_FLAG_VALUES = frozenset({"true", "false"})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply only reviewed non-secret production values. Existing credentials, "
            "PLATFORM_ENVIRONMENT and Turnstile mode/keys are never changed."
        )
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="KEY",
        help="Update only the named reviewed baseline key; may be repeated.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not args.apply and args.confirm:
        parser.error("--confirm is valid only with --apply")
    return args


def read_env(path: Path) -> tuple[list[str], dict[str, str], os.stat_result]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("Shared env must be a regular file, not a symlink.")
    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != os.geteuid() or mode & 0o007 or mode & 0o200 == 0:
        raise RuntimeError(
            "Shared env must be operator-owned, owner-writable and inaccessible to others."
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    required_secrets = {
        "PLATFORM_DATABASE_URL",
        "PLATFORM_SECRET_KEY",
        "PLATFORM_R2_ACCESS_KEY_ID",
        "PLATFORM_R2_SECRET_ACCESS_KEY",
    }
    if any(not values.get(key) for key in required_secrets):
        raise RuntimeError("Shared env is missing an existing required secret setting.")
    return lines, values, metadata


def merge_baseline(
    lines: list[str],
    *,
    baseline: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    selected_baseline = PUBLIC_BASELINE if baseline is None else baseline
    output: list[str] = []
    seen: set[str] = set()
    changed: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(raw_line)
            continue
        key, old_value = stripped.split("=", 1)
        key = key.strip()
        if key not in selected_baseline:
            output.append(raw_line)
            continue
        if key in seen:
            changed.append(key)
            continue
        old_normalized = old_value.strip().strip("'\"")
        if (
            key in PRESERVED_ROLLOUT_FLAGS
            and old_normalized in ROLLOUT_FLAG_VALUES
        ):
            output.append(raw_line)
            seen.add(key)
            continue
        new_value = selected_baseline[key]
        output.append(f"{key}={shell_env_value(new_value)}")
        seen.add(key)
        if old_normalized != new_value:
            changed.append(key)
    for key, value in selected_baseline.items():
        if key in seen:
            continue
        output.append(f"{key}={shell_env_value(value)}")
        changed.append(key)
    return "\n".join(output).rstrip() + "\n", sorted(set(changed))


def shell_env_value(value: str) -> str:
    """Serialize one value for both POSIX shell source and dotenv readers."""

    return shlex.quote(value)


def atomic_write(path: Path, content: str, metadata: os.stat_result) -> None:
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
            os.fchmod(temporary.fileno(), stat.S_IMODE(metadata.st_mode))
            os.fchown(temporary.fileno(), metadata.st_uid, metadata.st_gid)
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


def sync_runtime_envs(env_file: Path) -> bool:
    """Refresh service-scoped env files when the service boundary exists."""

    output_dir = env_file.parent / "env"
    if not output_dir.is_dir():
        return False
    renderer = Path(__file__).with_name("platform_render_service_envs.py")
    subprocess.run(
        [
            sys.executable,
            str(renderer),
            "--source",
            str(env_file),
            "--output-dir",
            str(output_dir),
            "--apply",
        ],
        check=True,
    )
    return True


def main() -> int:
    args = parse_args()
    selected_keys = PUBLIC_BASELINE
    if args.only is not None:
        unknown_keys = sorted(set(args.only) - PUBLIC_BASELINE.keys())
        if unknown_keys:
            raise RuntimeError(
                "Unknown or unreviewed baseline key(s): " + ", ".join(unknown_keys)
            )
        selected_keys = {key: PUBLIC_BASELINE[key] for key in dict.fromkeys(args.only)}
    lines, _values, metadata = read_env(args.env_file)
    content, changed = merge_baseline(lines, baseline=selected_keys)
    if args.apply:
        previous_content = args.env_file.read_bytes()
        atomic_write(args.env_file, content, metadata)
        try:
            sync_runtime_envs(args.env_file)
        except Exception:
            atomic_write(args.env_file, previous_content.decode("utf-8"), metadata)
            try:
                sync_runtime_envs(args.env_file)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Shared env changed but its service-scoped rollback failed; "
                    "stop services and reconcile canonical/runtime envs manually."
                ) from rollback_error
            raise
    report = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "mutated": bool(args.apply and changed),
        "changed_keys": changed,
        "changed_count": len(changed),
        "preserved_credentials": True,
        "production_environment_unchanged": True,
        "turnstile_unchanged": True,
    }
    if args.as_json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"Shared env baseline: mode={report['mode']}; changed={len(changed)}; "
            "credentials_preserved=true."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
