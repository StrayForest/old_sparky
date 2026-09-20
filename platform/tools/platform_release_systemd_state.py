#!/usr/bin/env python3
"""Capture and restore the owned systemd state around a release rollback.

This helper deliberately owns a closed unit list.  It never asks systemd to
enable a unit while capturing or installing release files, and it never
touches a unit outside that list.  The receipt is private, root-owned state;
callers keep it until the rollback transaction has reached its terminal
boundary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Final, cast
from uuid import uuid4


SCHEMA_VERSION: Final = 1
SYSTEMCTL: Final = "/usr/bin/systemctl"
SYSTEMCTL_PATH = SYSTEMCTL
RECEIPT_MAX_BYTES: Final = 64 * 1024
SLUG_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
ACTIVE_STATES: Final = frozenset(("active", "inactive"))
ENABLED_STATES: Final = frozenset(("enabled", "disabled", "static"))

# Keep this list synchronized with platform_install_systemd_units.sh.  These
# are the only units a release may install, start, stop, enable or disable.
OWNED_UNITS: Final = (
    "deadlock-api.service",
    "deadlock-worker.service",
    "deadlock-web.service",
    "deadlock-maintenance.service",
    "deadlock-maintenance.timer",
    "deadlock-logrotate.service",
    "deadlock-logrotate.timer",
    "deadlock-offsite-backup.service",
    "deadlock-offsite-backup.timer",
    "deadlock-cloudflare-ips.service",
    "deadlock-cloudflare-ips.timer",
    "deadlock-health-monitor.service",
    "deadlock-health-monitor.timer",
)

RECEIPT_KEYS: Final = frozenset(
    {
        "schema",
        "operation",
        "app_dir",
        "current_before",
        "previous_before",
        "current_before_identity",
        "previous_before_identity",
        "units",
    }
)
UNIT_KEYS: Final = frozenset(("name", "active", "enabled"))
TRANSACTION_KEYS: Final = frozenset(
    {
        "version",
        "operation",
        "phase",
        "app_dir",
        "current_before",
        "previous_before",
        "candidate_release",
        "shared_venv",
        "peer",
        "snapshot",
        "transition",
        "shared_before",
        "peer_before",
        "current_before_identity",
        "previous_before_identity",
        "candidate_identity",
        "remove_env_on_recovery",
        "service_state_before",
        "quiesced_services",
        "timer_active_before",
    }
)
TRANSACTION_SERVICE_UNITS: Final = (
    "deadlock-api.service",
    "deadlock-worker.service",
    "deadlock-web.service",
)
TRANSACTION_TIMER_UNIT: Final = "deadlock-cloudflare-ips.timer"


class StateError(RuntimeError):
    """The systemd receipt or an operation on it cannot be trusted."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise StateError("systemd receipt contains duplicate keys")
        value[key] = item
    return value


def _identity(path: Path, *, label: str) -> dict[str, int]:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StateError(f"{label} is unavailable") from exc
    if (
        not path.is_absolute()
        or resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise StateError(f"{label} metadata is unsafe")
    return {"dev": metadata.st_dev, "ino": metadata.st_ino}


def _valid_identity(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"dev", "ino"}
        and type(value["dev"]) is int
        and type(value["ino"]) is int
        and value["dev"] >= 0
        and value["ino"] > 0
    )


def _safe_receipt(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StateError("systemd receipt is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > RECEIPT_MAX_BYTES
    ):
        raise StateError("systemd receipt metadata is unsafe")
    return metadata


def _release_context(
    app_dir: Path,
    current_before: str,
    previous_before: str,
) -> tuple[Path, Path | None, Path | None, dict[str, int] | None, dict[str, int] | None]:
    if not app_dir.is_absolute() or app_dir == Path("/"):
        raise StateError("application directory is invalid")
    _identity(app_dir, label="application directory")
    releases = app_dir / "releases"
    _identity(releases, label="releases directory")

    def release(value: str, label: str) -> tuple[Path | None, dict[str, int] | None]:
        if not value:
            return None, None
        path = Path(value)
        if (
            not path.is_absolute()
            or path.parent != releases
            or SLUG_PATTERN.fullmatch(path.name) is None
        ):
            raise StateError(f"{label} escapes the releases directory")
        return path, _identity(path, label=label)

    current, current_identity = release(current_before, "original current release")
    previous, previous_identity = release(previous_before, "original previous release")
    return app_dir, current, previous, current_identity, previous_identity


def _run_systemctl(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            [SYSTEMCTL_PATH, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="ascii",
            errors="strict",
            timeout=30,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise StateError("systemd command failed") from exc
    output = result.stdout.strip()
    if "\n" in output or "\r" in output:
        raise StateError("systemd command returned multiple states")
    return result.returncode, output


def _read_active(unit: str) -> str:
    status, value = _run_systemctl("is-active", unit)
    if value not in ACTIVE_STATES:
        raise StateError("owned unit active state is unsupported")
    if (value == "active" and status != 0) or (
        value == "inactive" and status == 0
    ):
        raise StateError("owned unit active state/status mismatch")
    return value


def _read_enabled(unit: str) -> str:
    status, value = _run_systemctl("is-enabled", unit)
    if value not in ENABLED_STATES:
        raise StateError("owned unit enabled state is unsupported")
    # systemctl is-enabled returns zero for enabled and non-zero for disabled
    # and static.  Do not infer a state from the exit code alone: the text is
    # the authoritative state and both dimensions are retained in the receipt.
    if value == "enabled" and status != 0:
        raise StateError("owned unit enabled state/status mismatch")
    if value != "enabled" and status == 0:
        raise StateError("owned unit enabled state/status mismatch")
    return value


def _unit_snapshot(active_overrides: dict[str, str] | None = None) -> list[dict[str, str]]:
    return [
        {
            "name": unit,
            "active": (
                active_overrides[unit]
                if active_overrides is not None and unit in active_overrides
                else _read_active(unit)
            ),
            "enabled": _read_enabled(unit),
        }
        for unit in OWNED_UNITS
    ]


def _read_receipt(path: Path) -> dict[str, object]:
    _safe_receipt(path)
    try:
        raw = path.read_text(encoding="ascii")
        record = json.loads(raw, object_pairs_hook=_strict_object)
    except StateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("systemd receipt is invalid") from exc
    if not isinstance(record, dict) or set(record) != RECEIPT_KEYS:
        raise StateError("systemd receipt schema is invalid")
    if record.get("schema") != SCHEMA_VERSION or record.get("operation") != "rollback":
        raise StateError("systemd receipt schema is invalid")
    if not isinstance(record.get("app_dir"), str):
        raise StateError("systemd receipt application path is invalid")
    if not isinstance(record.get("current_before"), str) or not isinstance(
        record.get("previous_before"), str
    ):
        raise StateError("systemd receipt release paths are invalid")
    if not _valid_identity(record.get("current_before_identity")) or not _valid_identity(
        record.get("previous_before_identity")
    ):
        raise StateError("systemd receipt release identities are invalid")
    units = record.get("units")
    if not isinstance(units, list) or len(units) != len(OWNED_UNITS):
        raise StateError("systemd receipt unit set is invalid")
    names: list[str] = []
    for item in units:
        if not isinstance(item, dict) or set(item) != UNIT_KEYS:
            raise StateError("systemd receipt unit entry is invalid")
        name = item.get("name")
        active = item.get("active")
        enabled = item.get("enabled")
        if (
            not isinstance(name, str)
            or name not in OWNED_UNITS
            or name in names
            or not isinstance(active, str)
            or active not in ACTIVE_STATES
            or not isinstance(enabled, str)
            or enabled not in ENABLED_STATES
        ):
            raise StateError("systemd receipt unit entry is invalid")
        names.append(name)
    if tuple(names) != OWNED_UNITS:
        raise StateError("systemd receipt unit order is invalid")
    return record


def _validate_context(
    record: dict[str, object],
    app_dir: Path,
    current_before: str | None = None,
    previous_before: str | None = None,
) -> None:
    expected_app, current, previous, current_identity, previous_identity = _release_context(
        app_dir,
        record["current_before"] if current_before is None else current_before,
        record["previous_before"] if previous_before is None else previous_before,
    )
    if record.get("app_dir") != str(expected_app):
        raise StateError("systemd receipt application path changed")
    if current_before is not None and record.get("current_before") != current_before:
        raise StateError("systemd receipt current release mismatch")
    if previous_before is not None and record.get("previous_before") != previous_before:
        raise StateError("systemd receipt previous release mismatch")
    if current_identity != record.get("current_before_identity") or previous_identity != record.get(
        "previous_before_identity"
    ):
        raise StateError("systemd receipt release identity changed")


def _validate_active_overrides(
    record: dict[str, object], active_overrides: dict[str, str]
) -> None:
    actual = {item["name"]: item["active"] for item in _receipt_units(record)}
    for unit, expected in active_overrides.items():
        if actual.get(unit) != expected:
            raise StateError("systemd receipt transaction state changed")


def _read_transaction(path: Path, app_dir: Path) -> tuple[str, str, dict[str, str]]:
    _safe_receipt(path)
    try:
        raw = path.read_text(encoding="ascii")
        record = json.loads(raw, object_pairs_hook=_strict_object)
    except StateError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("release transaction is invalid") from exc
    if not isinstance(record, dict) or set(record) != TRANSACTION_KEYS:
        raise StateError("release transaction schema is invalid")
    if record.get("version") != 2 or record.get("operation") != "install":
        raise StateError("release transaction is not an install recovery")
    if record.get("app_dir") != str(app_dir):
        raise StateError("release transaction application path changed")
    current_before = record.get("current_before")
    previous_before = record.get("previous_before")
    if not isinstance(current_before, str) or not isinstance(previous_before, str):
        raise StateError("release transaction release paths are invalid")
    _release_context(app_dir, current_before, previous_before)
    if not _valid_identity(record.get("current_before_identity")) or not _valid_identity(
        record.get("previous_before_identity")
    ):
        raise StateError("release transaction release identities are invalid")
    _, _, _, current_identity, previous_identity = _release_context(
        app_dir, current_before, previous_before
    )
    if current_identity != record.get("current_before_identity") or previous_identity != record.get(
        "previous_before_identity"
    ):
        raise StateError("release transaction release identity changed")
    candidate = record.get("candidate_release")
    if not isinstance(candidate, str):
        raise StateError("release transaction candidate path is invalid")
    releases = app_dir / "releases"
    candidate_path = Path(candidate)
    if (
        not candidate_path.is_absolute()
        or candidate_path.parent != releases
        or SLUG_PATTERN.fullmatch(candidate_path.name) is None
    ):
        raise StateError("release transaction candidate escapes releases")
    candidate_identity = _identity(candidate_path, label="release transaction candidate")
    if candidate_identity != record.get("candidate_identity"):
        raise StateError("release transaction candidate identity changed")
    service_state = record.get("service_state_before")
    expected_service_names = {
        unit.removesuffix(".service") for unit in TRANSACTION_SERVICE_UNITS
    }
    expected_service_order = [unit.removesuffix(".service") for unit in TRANSACTION_SERVICE_UNITS]
    timer_active_before = record.get("timer_active_before")
    if (
        not isinstance(service_state, dict)
        or set(service_state) != expected_service_names
        or any(
            type(value) is not str or value not in ACTIVE_STATES
            for value in service_state.values()
        )
        or record.get("quiesced_services") != expected_service_order
        or type(timer_active_before) is not bool
    ):
        raise StateError("release transaction service state is invalid")
    typed_service_state = cast(dict[str, str], service_state)
    active_overrides = {
        **{
            unit: typed_service_state[unit.removesuffix(".service")]
            for unit in TRANSACTION_SERVICE_UNITS
        },
        TRANSACTION_TIMER_UNIT: "active" if timer_active_before else "inactive",
    }
    return current_before, previous_before, active_overrides


def _write_receipt(path: Path, record: dict[str, object]) -> None:
    parent = path.parent
    _identity(parent, label="systemd receipt directory")
    temporary = parent / f".{path.name}.{uuid4().hex}.tmp"
    raw = (
        json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    if len(raw) > RECEIPT_MAX_BYTES:
        raise StateError("systemd receipt is too large")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise StateError("systemd receipt write was incomplete")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    descriptor = os.open(
        parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def capture(
    path: Path,
    *,
    app_dir: Path,
    current_before: str,
    previous_before: str,
    active_overrides: dict[str, str] | None = None,
) -> None:
    if os.geteuid() != 0:
        raise StateError("systemd receipts require root")
    _release_context(app_dir, current_before, previous_before)
    if os.path.lexists(path):
        existing = _read_receipt(path)
        _validate_context(existing, app_dir, current_before, previous_before)
        if active_overrides is not None:
            _validate_active_overrides(existing, active_overrides)
        return
    current, previous = Path(current_before), Path(previous_before)
    current_identity = _identity(current, label="original current release")
    previous_identity = _identity(previous, label="original previous release")
    record: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "operation": "rollback",
        "app_dir": str(app_dir),
        "current_before": current_before,
        "previous_before": previous_before,
        "current_before_identity": current_identity,
        "previous_before_identity": previous_identity,
        "units": _unit_snapshot(active_overrides),
    }
    _write_receipt(path, record)
    _validate_context(_read_receipt(path), app_dir, current_before, previous_before)


def capture_transaction(path: Path, *, transaction: Path, app_dir: Path) -> None:
    if os.geteuid() != 0:
        raise StateError("systemd receipts require root")
    current_before, previous_before, active_overrides = _read_transaction(
        transaction, app_dir
    )
    capture(
        path,
        app_dir=app_dir,
        current_before=current_before,
        previous_before=previous_before,
        active_overrides=active_overrides,
    )


def validate(path: Path, *, app_dir: Path) -> None:
    record = _read_receipt(path)
    _validate_context(record, app_dir)


def _receipt_units(record: dict[str, object]) -> list[dict[str, str]]:
    return cast(list[dict[str, str]], record["units"])


def _apply_enabled(record: dict[str, object]) -> None:
    for item in _receipt_units(record):
        unit = item["name"]
        expected = item["enabled"]
        if expected == "static":
            # Static units have no enablement symlink and must be left alone.
            continue
        action = "enable" if expected == "enabled" else "disable"
        status, _ = _run_systemctl(action, unit)
        if status != 0:
            raise StateError("owned unit enablement restore failed")
    for item in _receipt_units(record):
        if _read_enabled(item["name"]) != item["enabled"]:
            raise StateError("owned unit enablement restore did not verify")


def _apply_active(record: dict[str, object]) -> None:
    for item in _receipt_units(record):
        unit = item["name"]
        action = "restart" if item["active"] == "active" else "stop"
        status, _ = _run_systemctl(action, unit)
        if status != 0:
            raise StateError("owned unit active-state restore failed")
    for item in _receipt_units(record):
        if _read_active(item["name"]) != item["active"]:
            raise StateError("owned unit active-state restore did not verify")


def restore(path: Path, *, app_dir: Path, active: bool) -> None:
    if os.geteuid() != 0:
        raise StateError("systemd receipts require root")
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    _apply_enabled(record)
    if active:
        _apply_active(record)


def verify(path: Path, *, app_dir: Path) -> None:
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    for item in _receipt_units(record):
        if _read_active(item["name"]) != item["active"] or _read_enabled(
            item["name"]
        ) != item["enabled"]:
            raise StateError("owned unit state does not match receipt")


def clear(path: Path, *, app_dir: Path) -> None:
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    path.unlink()
    descriptor = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_parser = commands.add_parser("capture")
    capture_parser.add_argument("--systemctl", default=SYSTEMCTL)
    capture_parser.add_argument("--state", required=True, type=Path)
    capture_parser.add_argument("--app-dir", required=True, type=Path)
    capture_parser.add_argument("--current-before", required=True)
    capture_parser.add_argument("--previous-before", required=True)
    transaction_capture_parser = commands.add_parser("capture-transaction")
    transaction_capture_parser.add_argument("--systemctl", default=SYSTEMCTL)
    transaction_capture_parser.add_argument("--state", required=True, type=Path)
    transaction_capture_parser.add_argument("--transaction", required=True, type=Path)
    transaction_capture_parser.add_argument("--app-dir", required=True, type=Path)
    for name in ("validate", "restore-enabled", "restore", "verify", "clear"):
        command = commands.add_parser(name)
        command.add_argument("--systemctl", default=SYSTEMCTL)
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--app-dir", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        global SYSTEMCTL_PATH
        if not isinstance(args.systemctl, str) or not args.systemctl.startswith("/"):
            raise StateError("systemctl path is invalid")
        SYSTEMCTL_PATH = args.systemctl
        if args.command == "capture":
            capture(
                args.state,
                app_dir=args.app_dir,
                current_before=args.current_before,
                previous_before=args.previous_before,
            )
        elif args.command == "capture-transaction":
            capture_transaction(
                args.state,
                transaction=args.transaction,
                app_dir=args.app_dir,
            )
        elif args.command == "validate":
            validate(args.state, app_dir=args.app_dir)
        elif args.command == "restore-enabled":
            restore(args.state, app_dir=args.app_dir, active=False)
        elif args.command == "restore":
            restore(args.state, app_dir=args.app_dir, active=True)
        elif args.command == "verify":
            verify(args.state, app_dir=args.app_dir)
        elif args.command == "clear":
            clear(args.state, app_dir=args.app_dir)
        else:
            raise StateError("unknown systemd state command")
        return 0
    except (StateError, OSError, ValueError):
        # Keep paths and systemctl output out of public logs.  Callers only
        # need a stable non-zero result and retain the receipt for recovery.
        print("RELEASE_SYSTEMD_STATE schema=1 status=failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
