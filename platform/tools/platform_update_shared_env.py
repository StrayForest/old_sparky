#!/usr/bin/env python3
"""Atomically update a fixed set of production env values from private stdin."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from tools.platform_configure_shared_env import (
    atomic_write,
    read_env,
    shell_env_value,
    sync_runtime_envs,
)


DEFAULT_ENV_FILE = Path("/opt/oldsparky/platform/shared/.env.platform")
CONFIRMATION = "APPLY_PRODUCTION_PRIVATE_VALUES"
ALLOWED_KEYS = frozenset(
    {
        "PLATFORM_ENVIRONMENT",
        "PLATFORM_OPENAI_API_KEY",
        "PLATFORM_RESEND_API_KEY",
        "PLATFORM_STEAM_LOGIN_ENABLED",
        "PLATFORM_GOOGLE_LOGIN_ENABLED",
        "PLATFORM_GOOGLE_CLIENT_ID",
        "PLATFORM_GOOGLE_CLIENT_SECRET",
        "PLATFORM_TURNSTILE_MODE",
        "PLATFORM_TURNSTILE_SECRET_KEY",
        "PLATFORM_TURNSTILE_SITE_KEY",
    }
)
FIXED_VALUES = {
    "PLATFORM_ENVIRONMENT": "production",
    "PLATFORM_TURNSTILE_MODE": "always",
}
ENUM_VALUES = {
    "PLATFORM_STEAM_LOGIN_ENABLED": frozenset({"true", "false"}),
    "PLATFORM_GOOGLE_LOGIN_ENABLED": frozenset({"true", "false"}),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read whitelisted production values as a JSON object from stdin."
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    if not args.apply and args.confirm:
        parser.error("--confirm is valid only with --apply")
    return args


def read_updates(raw: str) -> dict[str, str]:
    if len(raw.encode("utf-8")) > 16 * 1024:
        raise ValueError("Private env input is unexpectedly large.")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Private env input must be a non-empty JSON object.")
    updates: dict[str, str] = {}
    for key, value in payload.items():
        if key not in ALLOWED_KEYS:
            raise ValueError(f"Private env key is not allowed: {key}.")
        if not isinstance(value, str) or not value or "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"Private env value is invalid: {key}.")
        if key in FIXED_VALUES and value != FIXED_VALUES[key]:
            raise ValueError(f"Private env key must use the reviewed value: {key}.")
        if key in ENUM_VALUES and value not in ENUM_VALUES[key]:
            raise ValueError(f"Private env key has an invalid reviewed value: {key}.")
        updates[key] = value
    return updates


def merge_updates(lines: list[str], updates: dict[str, str]) -> tuple[str, list[str]]:
    output: list[str] = []
    remaining = dict(updates)
    changed: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(raw_line)
            continue
        key, old_value = stripped.split("=", 1)
        key = key.strip()
        if key not in updates:
            output.append(raw_line)
            continue
        if key not in remaining:
            changed.append(key)
            continue
        new_value = remaining.pop(key)
        output.append(f"{key}={shell_env_value(new_value)}")
        if old_value.strip().strip("'\"") != new_value:
            changed.append(key)
    for key, value in remaining.items():
        output.append(f"{key}={shell_env_value(value)}")
        changed.append(key)
    return "\n".join(output).rstrip() + "\n", sorted(set(changed))


def main() -> int:
    args = parse_args()
    updates = read_updates(sys.stdin.read())
    lines, _values, metadata = read_env(args.env_file)
    content, changed = merge_updates(lines, updates)
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
        "changed_keys": changed,
        "changed_count": len(changed),
        "values_redacted": True,
    }
    if args.as_json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(
            f"Private shared env update: mode={report['mode']}; "
            f"changed={len(changed)}; values_redacted=true."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
