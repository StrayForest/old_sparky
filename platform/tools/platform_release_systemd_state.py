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
import hashlib
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
OPERATION_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
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
        "operation_id",
        "app_dir",
        "current_before",
        "previous_before",
        "current_before_identity",
        "previous_before_identity",
        "helper_digests",
        "units",
    }
)
UNIT_KEYS: Final = frozenset(("name", "active", "enabled"))
TRANSACTION_KEYS: Final = frozenset(
    {
        "operation_id",
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
HELPER_RELATIVE_PATHS: Final = (
    "tools/platform_install_systemd_units.sh",
    "tools/platform_install_nginx.py",
    "tools/platform_deploy_smoke.py",
    "tools/platform_live_qa_runtime_install.py",
    "tools/platform_install_logging.sh",
    "tools/platform_prepare_service_user.sh",
    "tools/platform_render_service_envs.py",
    "tools/platform_deploy_smoke_impl.py",
    "tools/platform_safe_env_exec.py",
    "tools/platform_release_restore_runtime.sh",
    "tools/platform_release_systemd_state.py",
    "tools/platform_release_transaction.py",
    "tools/platform_release_lock.sh",
)


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
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
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


def _helper_digests(release: Path) -> dict[str, str]:
    _identity(release / "tools", label="release tools directory")
    result: dict[str, str] = {}
    for relative in HELPER_RELATIVE_PATHS:
        path = release / relative
        try:
            metadata = path.lstat()
        except OSError:
            raise StateError("release helper manifest is incomplete") from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not stat.S_IMODE(metadata.st_mode) & 0o111
            or metadata.st_size > 4 * 1024 * 1024
        ):
            raise StateError("release helper metadata is unsafe")
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise StateError("release helper is unavailable") from exc
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or opened.st_uid != 0
                or opened.st_gid != 0
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) & 0o022
                or not stat.S_IMODE(opened.st_mode) & 0o111
                or opened.st_size != metadata.st_size
            ):
                raise StateError("release helper changed during validation")
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            final = os.fstat(descriptor)
            if (
                final.st_dev != opened.st_dev
                or final.st_ino != opened.st_ino
                or final.st_uid != opened.st_uid
                or final.st_gid != opened.st_gid
                or final.st_nlink != opened.st_nlink
                or final.st_mode != opened.st_mode
                or final.st_size != opened.st_size
                or final.st_mtime_ns != opened.st_mtime_ns
                or final.st_ctime_ns != opened.st_ctime_ns
            ):
                raise StateError("release helper changed during validation")
            result[relative] = digest.hexdigest()
        except OSError as exc:
            raise StateError("release helper cannot be read") from exc
        finally:
            os.close(descriptor)
    return result


def _safe_receipt(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise StateError("systemd receipt is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
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
    if (
        not isinstance(record.get("operation_id"), str)
        or OPERATION_ID_PATTERN.fullmatch(record["operation_id"]) is None
    ):
        raise StateError("systemd receipt operation identity is invalid")
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
    helper_digests = record.get("helper_digests")
    if not isinstance(helper_digests, dict):
        raise StateError("systemd receipt helper manifest is invalid")
    if set(helper_digests) not in (
        {"current_before"},
        {"current_before", "previous_before"},
    ) or any(
        not isinstance(manifest, dict)
        or set(manifest) != set(HELPER_RELATIVE_PATHS)
        or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for key, value in manifest.items()
        )
        for manifest in helper_digests.values()
    ):
        raise StateError("systemd receipt helper manifest is invalid")
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


def _read_transaction(
    path: Path, app_dir: Path
) -> tuple[
    str,
    str,
    str,
    str,
    dict[str, int],
    dict[str, int],
    dict[str, str],
]:
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
    if record.get("version") != 2 or record.get("operation") not in {"install", "rollback"}:
        raise StateError("release transaction is not a supported recovery")
    operation_id = record.get("operation_id")
    if not isinstance(operation_id, str) or OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise StateError("release transaction operation identity is invalid")
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
    if os.path.lexists(candidate_path):
        candidate_identity = _identity(candidate_path, label="release transaction candidate")
        if candidate_identity != record.get("candidate_identity"):
            raise StateError("release transaction candidate identity changed")
    elif record.get("phase") != "recovery-restored":
        raise StateError("release transaction candidate is unavailable")
    service_state = record.get("service_state_before")
    expected_service_names = {
        unit.removesuffix(".service") for unit in TRANSACTION_SERVICE_UNITS
    }
    expected_service_order = [unit.removesuffix(".service") for unit in TRANSACTION_SERVICE_UNITS]
    timer_active_before = record.get("timer_active_before")
    if service_state is None and record.get("quiesced_services") is None and timer_active_before is None:
        active_overrides: dict[str, str] = {}
    else:
        if (
            record.get("operation") != "install"
            or not isinstance(service_state, dict)
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
    assert current_identity is not None
    assert previous_identity is not None
    return (
        operation_id,
        cast(str, record["operation"]),
        current_before,
        previous_before,
        current_identity,
        previous_identity,
        active_overrides,
    )


def _validate_transaction_binding(
    record: dict[str, object],
    transaction: Path,
    app_dir: Path,
    helper_release: Path | None = None,
) -> None:
    (
        operation_id,
        operation,
        current_before,
        previous_before,
        current_identity,
        previous_identity,
        active_overrides,
    ) = _read_transaction(transaction, app_dir)
    _validate_context(record, app_dir, current_before, previous_before)
    if (
        record.get("operation_id") != operation_id
        or record.get("current_before_identity") != current_identity
        or record.get("previous_before_identity") != previous_identity
    ):
        raise StateError("systemd receipt transaction identity mismatch")
    helper_digests = record.get("helper_digests")
    if not isinstance(helper_digests, dict):
        raise StateError("systemd receipt helper manifest is missing")
    expected_releases = {
        "current_before": Path(current_before),
    }
    if operation == "rollback":
        expected_releases["previous_before"] = Path(previous_before)
    if set(helper_digests) != set(expected_releases):
        raise StateError("systemd receipt helper manifest is missing")
    if helper_release is None:
        helper_label = "previous_before" if operation == "rollback" else "current_before"
    elif helper_release == expected_releases["current_before"]:
        helper_label = "current_before"
    elif (
        "previous_before" in expected_releases
        and helper_release == expected_releases["previous_before"]
    ):
        helper_label = "previous_before"
    else:
        raise StateError("systemd receipt helper release mismatch")
    expected_manifests = {
        label: _helper_digests(path)
        for label, path in expected_releases.items()
    }
    if helper_digests != expected_manifests:
        raise StateError("systemd receipt helper manifest changed")
    if helper_digests[helper_label] != _helper_digests(expected_releases[helper_label]):
        raise StateError("systemd receipt helper release changed")
    _validate_active_overrides(record, active_overrides)


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
    operation_id: str,
    helper_digests: dict[str, object] | None = None,
    active_overrides: dict[str, str] | None = None,
) -> None:
    if os.geteuid() != 0:
        raise StateError("systemd receipts require root")
    if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise StateError("systemd receipt operation identity is invalid")
    current, previous = Path(current_before), Path(previous_before)
    if helper_digests is None:
        helper_digests = {
            "current_before": _helper_digests(current),
            "previous_before": _helper_digests(previous),
        }
    elif set(helper_digests) not in (
        {"current_before"},
        {"current_before", "previous_before"},
    ):
        raise StateError("systemd receipt helper manifest is invalid")
    _release_context(app_dir, current_before, previous_before)
    if os.path.lexists(path):
        existing = _read_receipt(path)
        _validate_context(existing, app_dir, current_before, previous_before)
        if existing.get("operation_id") != operation_id:
            raise StateError("systemd receipt operation identity changed")
        if existing.get("helper_digests") != helper_digests:
            raise StateError("systemd receipt helper manifest changed")
        if active_overrides is not None:
            _validate_active_overrides(existing, active_overrides)
        return
    current_identity = _identity(current, label="original current release")
    previous_identity = _identity(previous, label="original previous release")
    record: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "operation": "rollback",
        "operation_id": operation_id,
        "app_dir": str(app_dir),
        "current_before": current_before,
        "previous_before": previous_before,
        "current_before_identity": current_identity,
        "previous_before_identity": previous_identity,
        "helper_digests": helper_digests,
        "units": _unit_snapshot(active_overrides),
    }
    _write_receipt(path, record)
    _validate_context(_read_receipt(path), app_dir, current_before, previous_before)


def capture_transaction(
    path: Path,
    *,
    transaction: Path,
    app_dir: Path,
    require_helper_manifest: bool = False,
    helper_release: Path | None = None,
) -> None:
    if os.geteuid() != 0:
        raise StateError("systemd receipts require root")
    (
        operation_id,
        operation,
        current_before,
        previous_before,
        _,
        _,
        active_overrides,
    ) = _read_transaction(
        transaction, app_dir
    )
    expected_releases = {"current_before": Path(current_before)}
    if operation == "rollback":
        expected_releases["previous_before"] = Path(previous_before)
    if helper_release is not None and helper_release not in expected_releases.values():
        raise StateError("systemd receipt helper release mismatch")
    if not require_helper_manifest:
        raise StateError("transaction helper manifest assertion is required")
    helper_digests = {
        label: _helper_digests(path)
        for label, path in expected_releases.items()
    }
    capture(
        path,
        app_dir=app_dir,
        current_before=current_before,
        previous_before=previous_before,
        operation_id=operation_id,
        helper_digests=helper_digests,
        active_overrides=active_overrides,
    )


def validate(
    path: Path,
    *,
    app_dir: Path,
    transaction: Path | None = None,
    helper_release: Path | None = None,
) -> None:
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    if transaction is not None:
        _validate_transaction_binding(record, transaction, app_dir, helper_release)


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


def restore(
    path: Path,
    *,
    app_dir: Path,
    active: bool,
    transaction: Path | None = None,
    helper_release: Path | None = None,
) -> None:
    if os.geteuid() != 0:
        raise StateError("systemd receipts require root")
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    if transaction is not None:
        _validate_transaction_binding(record, transaction, app_dir, helper_release)
    _apply_enabled(record)
    if active:
        _apply_active(record)


def verify(
    path: Path,
    *,
    app_dir: Path,
    transaction: Path | None = None,
    helper_release: Path | None = None,
) -> None:
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    if transaction is not None:
        _validate_transaction_binding(record, transaction, app_dir, helper_release)
    for item in _receipt_units(record):
        if _read_active(item["name"]) != item["active"] or _read_enabled(
            item["name"]
        ) != item["enabled"]:
            raise StateError("owned unit state does not match receipt")


def clear(
    path: Path,
    *,
    app_dir: Path,
    transaction: Path | None = None,
    helper_release: Path | None = None,
) -> None:
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    if transaction is not None:
        _validate_transaction_binding(record, transaction, app_dir, helper_release)
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
    capture_parser.add_argument("--operation-id", required=True)
    transaction_capture_parser = commands.add_parser("capture-transaction")
    transaction_capture_parser.add_argument("--systemctl", default=SYSTEMCTL)
    transaction_capture_parser.add_argument("--state", required=True, type=Path)
    transaction_capture_parser.add_argument("--transaction", required=True, type=Path)
    transaction_capture_parser.add_argument("--app-dir", required=True, type=Path)
    transaction_capture_parser.add_argument("--helper-release", type=Path)
    transaction_capture_parser.add_argument("--require-helper-manifest", action="store_true")
    for name in ("validate", "restore-enabled", "restore", "verify", "clear"):
        command = commands.add_parser(name)
        command.add_argument("--systemctl", default=SYSTEMCTL)
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--transaction", type=Path)
        command.add_argument("--helper-release", type=Path)
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
                operation_id=args.operation_id,
            )
        elif args.command == "capture-transaction":
            capture_transaction(
                args.state,
                transaction=args.transaction,
                app_dir=args.app_dir,
                require_helper_manifest=args.require_helper_manifest,
                helper_release=args.helper_release,
            )
        elif args.command == "validate":
            validate(
                args.state,
                app_dir=args.app_dir,
                transaction=args.transaction,
                helper_release=args.helper_release,
            )
        elif args.command == "restore-enabled":
            restore(
                args.state,
                app_dir=args.app_dir,
                active=False,
                transaction=args.transaction,
                helper_release=args.helper_release,
            )
        elif args.command == "restore":
            restore(
                args.state,
                app_dir=args.app_dir,
                active=True,
                transaction=args.transaction,
                helper_release=args.helper_release,
            )
        elif args.command == "verify":
            verify(
                args.state,
                app_dir=args.app_dir,
                transaction=args.transaction,
                helper_release=args.helper_release,
            )
        elif args.command == "clear":
            clear(
                args.state,
                app_dir=args.app_dir,
                transaction=args.transaction,
                helper_release=args.helper_release,
            )
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
