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
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import sys
import time
from typing import Final, cast
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION: Final = 1
SYSTEMCTL: Final = "/usr/bin/systemctl"
SYSTEMCTL_PATH = SYSTEMCTL
JOURNALCTL_PATH: Final = "/usr/bin/journalctl"
RECEIPT_MAX_BYTES: Final = 64 * 1024
PRIVATE_EVIDENCE_MAX_BYTES: Final = 16 * 1024
PRIVATE_JOURNAL_MAX_BYTES: Final = 64 * 1024
PENDING_GUARD_MIN_NEXT_TIMER_SECONDS: Final = 900
PENDING_GUARD_CURRENT_SOURCE_SHA: Final = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
PENDING_GUARD_PREVIOUS_SOURCE_SHA: Final = "f1154c7dba4f57bf52f7d7544e97d924c732be94"
CLOUDFLARE_UNIT: Final = "deadlock-cloudflare-ips.service"
CLOUDFLARE_TIMER: Final = "deadlock-cloudflare-ips.timer"
CLOUDFLARE_FRAGMENT: Final = "/etc/systemd/system/deadlock-cloudflare-ips.service"
CLOUDFLARE_EXEC_START_FIELDS: Final = (
    "path", "argv[]", "ignore_errors", "start_time", "stop_time", "pid", "code", "status",
)
CLOUDFLARE_SERVICE_PROPERTIES: Final = (
    "Id", "FragmentPath", "DropInPaths", "NeedDaemonReload", "Type", "RemainAfterExit",
    "KillMode", "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus",
    "MainPID", "ControlPID", "ControlGroup", "TasksCurrent", "UnitFileState", "InvocationID",
    "ExecMainStartTimestamp", "ExecStart",
)
CLOUDFLARE_TIMER_PROPERTIES: Final = (
    "Id", "FragmentPath", "DropInPaths", "NeedDaemonReload", "ActiveState", "SubState",
    "Unit", "UnitFileState", "LastTriggerUSec", "NextElapseUSecRealtime",
)
HEALTH_MONITOR_UNIT: Final = "deadlock-health-monitor.service"
HEALTH_MONITOR_TIMER: Final = "deadlock-health-monitor.timer"
HEALTH_MONITOR_FRAGMENT: Final = "/etc/systemd/system/deadlock-health-monitor.service"
HEALTH_MONITOR_PROPERTIES: Final = (
    "Id", "FragmentPath", "DropInPaths", "NeedDaemonReload", "LoadState",
    "UnitFileState", "Type", "ActiveState", "SubState", "Result", "ExecMainCode",
    "ExecMainStatus", "MainPID", "ControlPID", "InvocationID",
    "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic", "ExecStart",
)
HEALTH_MONITOR_TIMER_PROPERTIES: Final = (
    "Id", "FragmentPath", "DropInPaths", "NeedDaemonReload", "Unit",
    "ActiveState", "SubState", "UnitFileState",
    "LastTriggerUSecMonotonic", "RandomizedDelayUSec", "AccuracyUSec",
    "Persistent", "TimersMonotonic",
)
HEALTH_MONITOR_WAIT_SECONDS: Final = 450
HEALTH_MONITOR_POLL_SECONDS: Final = 3
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
INITIAL_SYSTEMD_UNITS: Final = (
    "deadlock-api.service",
    "deadlock-worker.service",
    "deadlock-web.service",
    "deadlock-maintenance.timer",
    "deadlock-logrotate.timer",
    "deadlock-cloudflare-ips.timer",
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
PROFILE_ACCESS_CACHE_PURGE_PROOF_KEYS: Final = frozenset(
    {
        "schema",
        "operation_id",
        "operation",
        "source_role",
        "source_identity",
        "venv_identity",
        "scope",
    }
)
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
        "service_enabled_before",
        "quiesced_services",
        "timer_active_before",
        "timer_enabled_before",
        "systemd_state_before",
        "profile_access_cache_purge_proof",
    }
)
TRANSACTION_KEYS_WITHOUT_SYSTEMD_STATE: Final = TRANSACTION_KEYS - {
    "systemd_state_before"
}
TRANSACTION_KEYS_WITHOUT_PROFILE_CACHE_PURGE_PROOF: Final = TRANSACTION_KEYS - {
    "profile_access_cache_purge_proof"
}
TRANSACTION_KEYS_WITHOUT_SYSTEMD_AND_PROFILE_CACHE_PURGE_PROOF: Final = (
    TRANSACTION_KEYS_WITHOUT_SYSTEMD_STATE - {"profile_access_cache_purge_proof"}
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


def _validate_systemctl_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value or "\n" in value:
        raise StateError("systemctl path is invalid")
    path = Path(value)
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StateError("systemctl path is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o111
        or resolved != path
    ):
        raise StateError("systemctl path metadata is unsafe")
    return value


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
        value == "inactive" and status != 3
    ):
        raise StateError("owned unit active state/status mismatch")
    return value


def _read_enabled(unit: str) -> str:
    status, value = _run_systemctl("is-enabled", unit)
    if value not in ENABLED_STATES:
        raise StateError("owned unit enabled state is unsupported")
    # Keep the systemctl contract closed.  ``disabled`` is the one normal
    # state whose query returns rc=1; static units are successful rc=0.  Do
    # not accept arbitrary non-zero statuses as a disabled/static result.
    if value in {"enabled", "static"} and status != 0:
        raise StateError("owned unit enabled state/status mismatch")
    if value == "disabled" and status != 1:
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


def _validate_initial_systemd_snapshot(record: dict[str, object]) -> None:
    """Reject malformed clean-install systemd authority before side effects."""

    snapshot = record.get("systemd_state_before")
    if snapshot is None:
        return
    if (
        record.get("operation") != "install"
        or record.get("current_before") is not None
        or record.get("previous_before") is not None
    ):
        raise StateError("initial systemd snapshot is unexpected")
    if not isinstance(snapshot, dict) or set(snapshot) != set(INITIAL_SYSTEMD_UNITS):
        raise StateError("initial systemd snapshot is incomplete")
    for unit in INITIAL_SYSTEMD_UNITS:
        state = snapshot.get(unit)
        if (
            not isinstance(state, dict)
            or set(state) != {"active", "enabled"}
            or state.get("active") != "inactive"
            or state.get("enabled") != "disabled"
        ):
            raise StateError("initial systemd snapshot is invalid")


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
    if not isinstance(record, dict) or set(record) not in (
        TRANSACTION_KEYS,
        TRANSACTION_KEYS_WITHOUT_SYSTEMD_STATE,
        TRANSACTION_KEYS_WITHOUT_PROFILE_CACHE_PURGE_PROOF,
        TRANSACTION_KEYS_WITHOUT_SYSTEMD_AND_PROFILE_CACHE_PURGE_PROOF,
    ):
        raise StateError("release transaction schema is invalid")
    _validate_initial_systemd_snapshot(record)
    if record.get("version") != 2 or record.get("operation") not in {"install", "rollback"}:
        raise StateError("release transaction is not a supported recovery")
    operation_id = record.get("operation_id")
    if not isinstance(operation_id, str) or OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
        raise StateError("release transaction operation identity is invalid")
    purge_proof = record.get("profile_access_cache_purge_proof")
    if purge_proof is not None:
        if (
            not isinstance(purge_proof, dict)
            or set(purge_proof) != PROFILE_ACCESS_CACHE_PURGE_PROOF_KEYS
            or type(purge_proof.get("schema")) is not int
            or purge_proof.get("schema") != 1
            or purge_proof.get("operation_id") != operation_id
            or purge_proof.get("operation") != record.get("operation")
            or purge_proof.get("source_role")
            != ("current-before" if record.get("operation") == "rollback" else "candidate")
            or not _valid_identity(purge_proof.get("source_identity"))
            or not _valid_identity(purge_proof.get("venv_identity"))
            or purge_proof.get("source_identity")
            != (
                record.get("current_before_identity")
                if record.get("operation") == "rollback"
                else record.get("candidate_identity")
            )
            or purge_proof.get("venv_identity")
            != (
                record.get("shared_before")
                if record.get("operation") == "rollback"
                or record.get("transition") not in {"exchange", "create"}
                else record.get("peer_before")
            )
            or purge_proof.get("scope") != "tournament-profile-access-v1"
        ):
            raise StateError("release transaction cache-purge proof is invalid")
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
    service_enabled = record.get("service_enabled_before")
    timer_enabled_before = record.get("timer_enabled_before")
    if service_state is None and service_enabled is None and record.get("quiesced_services") is None and timer_active_before is None and timer_enabled_before is None:
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
            or not isinstance(service_enabled, dict)
            or set(service_enabled) != expected_service_names
            or any(type(value) is not str or value not in {"enabled", "disabled"} for value in service_enabled.values())
            or timer_enabled_before not in {"enabled", "disabled"}
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
    if operation == "install" and not active_overrides:
        # The low-level transaction schema permits an install receipt to be
        # created before the deploy wrapper records its service snapshot.
        # Recovery must never turn that nullable representation into a live
        # systemd observation: doing so would create an authoritative receipt
        # from state that was not captured before quiesce.
        raise StateError("install transaction service snapshot is incomplete")
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


def _bounded_command(argv: list[str], *, maximum: int, timeout: int = 30) -> tuple[int, str]:
    """Run one fixed evidence command without buffering unbounded output."""
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "Europe/Helsinki"},
        )
        assert process.stdout is not None
        descriptor = process.stdout.fileno()
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                raise StateError("bounded systemd evidence command timed out")
            for key, _ in events:
                try:
                    chunk = os.read(key.fd, 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                if len(output) + len(chunk) > maximum:
                    raise StateError("bounded systemd evidence command exceeded limit")
                output.extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise StateError("bounded systemd evidence command timed out") from exc
        try:
            decoded = output.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise StateError("bounded systemd evidence output is malformed") from exc
        return returncode, decoded
    except OSError as exc:
        raise StateError("bounded systemd evidence command failed") from exc
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()


def _parse_show_fields(output: str, expected: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(output, str) or len(output.encode("ascii", errors="strict")) > 32 * 1024:
        raise StateError("systemd properties are malformed")
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or "\x00" in value:
            raise StateError("systemd properties are malformed")
        if key == "TimersMonotonic" and key in fields:
            fields[key] += "\n" + value
        elif key in fields:
            raise StateError("systemd properties are malformed")
        else:
            fields[key] = value
    if set(fields) != set(expected):
        raise StateError("systemd properties are incomplete")
    return fields


def _fixed_show(unit: str, properties: tuple[str, ...]) -> dict[str, str]:
    status, output = _bounded_command(
        [SYSTEMCTL_PATH, "show", unit, "--no-pager", "--property=" + ",".join(properties)],
        maximum=32 * 1024,
    )
    if status != 0:
        raise StateError("fixed systemd unit properties are unavailable")
    return _parse_show_fields(output, properties)


def _fixed_jobs() -> list[tuple[str, str, str, str]]:
    status, output = _bounded_command(
        [SYSTEMCTL_PATH, "list-jobs", "--no-pager", "--all"], maximum=32 * 1024,
    )
    if status != 0:
        raise StateError("systemd jobs are unavailable")
    lines = output.splitlines()
    if lines == ["No jobs running."]:
        return []
    if len(lines) < 2 or lines[0].split() != ["JOB", "UNIT", "TYPE", "STATE"]:
        raise StateError("systemd jobs are malformed")
    jobs: list[tuple[str, str, str, str]] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) != 4 or not fields[0].isdigit():
            raise StateError("systemd jobs are malformed")
        jobs.append((fields[0], fields[1], fields[2], fields[3]))
    return jobs


def _systemd_timespan_usec(value: str) -> int:
    factors = {
        "us": Decimal(1), "ms": Decimal(1000),
        "s": Decimal(1_000_000), "min": Decimal(60_000_000),
        "h": Decimal(3_600_000_000), "d": Decimal(86_400_000_000),
        "w": Decimal(604_800_000_000), "month": Decimal(2_629_800_000_000),
        "y": Decimal(31_557_600_000_000),
    }
    token = re.compile(r"([0-9]+(?:\.[0-9]+)?)(month|y|min|ms|us|w|d|h|s)")
    position = 0
    total = Decimal(0)
    count = 0
    previous_factor: Decimal | None = None
    try:
        while position < len(value):
            while position < len(value) and value[position] == " ":
                position += 1
            match = token.match(value, position)
            if match is None:
                raise StateError("systemd monotonic timestamp is malformed")
            factor = factors[match.group(2)]
            amount = Decimal(match.group(1))
            if (
                amount < 0
                or (previous_factor is not None and factor >= previous_factor)
                or (
                    "." in match.group(1)
                    and (
                        match.group(2) != "s"
                        or len(match.group(1).partition(".")[2]) > 6
                        or value[match.end():].strip()
                    )
                )
            ):
                raise StateError("systemd monotonic timestamp is malformed")
            total += amount * factor
            previous_factor = factor
            position = match.end()
            count += 1
    except InvalidOperation as exc:
        raise StateError("systemd monotonic timestamp is malformed") from exc
    if count == 0 or not total.is_finite() or total < 0:
        raise StateError("systemd monotonic timestamp is malformed")
    return int(total)


def _health_timer_schedule_is_fixed(value: str) -> bool:
    entry = re.compile(
        r"\{\s*(OnBootUSec|OnUnitActiveUSec)=([0-9]+(?:\.[0-9]+)?(?:month|y|min|ms|us|w|d|h|s))"
        r"\s*;\s*next_elapse=([^{}\n]{1,128})\s*\}"
    )
    schedules: dict[str, str] = {}
    remainder = value
    for match in entry.finditer(value):
        key, interval, next_elapse = match.groups()
        if key in schedules or not next_elapse.strip():
            return False
        schedules[key] = interval
        remainder = remainder.replace(match.group(0), "", 1)
    return (
        not remainder.strip()
        and schedules == {"OnBootUSec": "5min", "OnUnitActiveUSec": "5min"}
    )


def _health_monitor_identity() -> tuple[dict[str, str], dict[str, str]]:
    service = _fixed_show(HEALTH_MONITOR_UNIT, HEALTH_MONITOR_PROPERTIES)
    timer = _fixed_show(HEALTH_MONITOR_TIMER, HEALTH_MONITOR_TIMER_PROPERTIES)
    expected_argv = (
        "/opt/oldsparky/platform/shared/venv/bin/python",
        "/opt/oldsparky/platform/current/tools/platform_health_monitor.py",
        "--disk-min-free-gib", "5", "--disk-max-used-percent", "90",
    )
    exec_value = service["ExecStart"]
    if not exec_value.startswith("{ ") or not exec_value.endswith(" }"):
        raise StateError("health-monitor ExecStart is malformed")
    fields: dict[str, str] = {}
    for item in exec_value[2:-2].split(" ; "):
        key, separator, field_value = item.partition("=")
        if not separator or key not in {"path", "argv[]", "ignore_errors", "start_time", "stop_time", "pid", "code", "status"} or key in fields:
            raise StateError("health-monitor ExecStart is malformed")
        fields[key] = field_value
    try:
        actual_argv = tuple(shlex.split(fields["argv[]"], posix=True))
    except (KeyError, ValueError) as exc:
        raise StateError("health-monitor ExecStart is malformed") from exc
    if (
        service["Id"] != HEALTH_MONITOR_UNIT
        or service["FragmentPath"] != HEALTH_MONITOR_FRAGMENT
        or service["DropInPaths"] != ""
        or service["NeedDaemonReload"] != "no"
        or service["LoadState"] != "loaded"
        or service["UnitFileState"] != "static"
        or service["Type"] != "oneshot"
        or set(fields) != {"path", "argv[]", "ignore_errors", "start_time", "stop_time", "pid", "code", "status"}
        or fields["path"] != expected_argv[0]
        or actual_argv != expected_argv
        or fields["ignore_errors"] != "no"
        or timer["Id"] != HEALTH_MONITOR_TIMER
        or timer["FragmentPath"] != "/etc/systemd/system/deadlock-health-monitor.timer"
        or timer["DropInPaths"] != ""
        or timer["NeedDaemonReload"] != "no"
        or timer["Unit"] != HEALTH_MONITOR_UNIT
        or timer["UnitFileState"] != "enabled"
        or not _health_timer_schedule_is_fixed(timer["TimersMonotonic"])
        or timer["RandomizedDelayUSec"] != "30s"
        or timer["AccuracyUSec"] != "15s"
        or timer["Persistent"] != "yes"
    ):
        raise StateError("health-monitor unit identity is unsupported")
    return service, timer


def _health_monitor_baseline() -> dict[str, object]:
    if _fixed_jobs():
        raise StateError("systemd jobs prevent protected recovery")
    service, timer = _health_monitor_identity()
    if (
        service["ActiveState"] not in {"failed", "inactive"}
        or service["InvocationID"] == ""
        or not service["ExecMainStartTimestampMonotonic"].isdigit()
        or not service["ExecMainExitTimestampMonotonic"].isdigit()
        or timer["ActiveState"] != "active"
        or timer["SubState"] != "waiting"
        or (
            service["ActiveState"] == "inactive"
            and (
                service["Result"] != "success"
                or service["ExecMainCode"] != "1"
                or service["ExecMainStatus"] != "0"
                or service["MainPID"] != "0"
                or service["ControlPID"] != "0"
            )
        )
    ):
        raise StateError("health-monitor baseline is unsupported")
    # Every unrelated owned unit must be in the ordinary supported state set.
    for unit in OWNED_UNITS:
        if unit == HEALTH_MONITOR_UNIT:
            continue
        _read_active(unit)
        _read_enabled(unit)
    return {
        "schema": 1,
        "invocation_id": service["InvocationID"],
        "last_trigger_usec_monotonic": _systemd_timespan_usec(
            timer["LastTriggerUSecMonotonic"]
        ),
    }


def _wait_health_monitor_success(baseline_json: str, *, timeout: int) -> None:
    try:
        baseline = json.loads(baseline_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StateError("health-monitor baseline is malformed") from exc
    if (
        not isinstance(baseline, dict)
        or set(baseline) != {"schema", "invocation_id", "last_trigger_usec_monotonic"}
        or baseline.get("schema") != 1
        or not isinstance(baseline.get("invocation_id"), str)
        or not baseline["invocation_id"]
        or type(baseline.get("last_trigger_usec_monotonic")) is not int
        or baseline["last_trigger_usec_monotonic"] < 0
        or not 1 <= timeout <= 600
    ):
        raise StateError("health-monitor baseline is malformed")
    restored_at = int(time.clock_gettime(time.CLOCK_MONOTONIC) * 1_000_000)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if _fixed_jobs():
                raise StateError("systemd jobs prevent health-monitor proof")
            service, timer = _health_monitor_identity()
            start = int(service["ExecMainStartTimestampMonotonic"])
            finish = int(service["ExecMainExitTimestampMonotonic"])
            trigger = _systemd_timespan_usec(timer["LastTriggerUSecMonotonic"])
            if (
                service["InvocationID"] != baseline["invocation_id"]
                and start > restored_at
                and finish >= start
                and trigger > max(restored_at, baseline["last_trigger_usec_monotonic"])
                and service["Result"] == "success"
                and service["ExecMainCode"] == "1"
                and service["ExecMainStatus"] == "0"
                and service["MainPID"] == "0"
                and service["ControlPID"] == "0"
                and service["ActiveState"] == "inactive"
                and timer["ActiveState"] == "active"
                and timer["SubState"] == "waiting"
            ):
                return
        except (StateError, ValueError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StateError("fresh health-monitor success was not observed")
        time.sleep(min(HEALTH_MONITOR_POLL_SECONDS, remaining))


def _systemd_timestamp(value: str) -> datetime:
    match = re.fullmatch(
        r"[A-Z][a-z]{2} ([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}:[0-9]{2}:[0-9]{2}) ([A-Z]{3,5})",
        value,
    )
    if match is None:
        raise StateError("systemd timestamp is malformed")
    try:
        zone = ZoneInfo("Europe/Helsinki")
    except ZoneInfoNotFoundError as exc:
        raise StateError("systemd timestamp zone is unavailable") from exc
    try:
        naive = datetime.strptime(match.group(1) + " " + match.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise StateError("systemd timestamp is malformed") from exc
    parsed = naive.replace(tzinfo=zone)
    if parsed.tzname() != match.group(3) or parsed.strftime("%a") != value[:3]:
        raise StateError("systemd timestamp zone mismatch")
    return parsed.astimezone(timezone.utc)


def _cloudflare_source_sha(release: Path) -> str:
    raw = _read_cloudflare_source_file(release / "RELEASE.json", maximum=256 * 1024, label="release identity metadata")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("release identity metadata is malformed") from exc
    source_sha = value.get("source_git_commit") if isinstance(value, dict) else None
    if not isinstance(source_sha, str) or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise StateError("release source identity is invalid")
    return source_sha


def _cloudflare_exec_start(value: str) -> tuple[str, ...]:
    if not value.startswith("{ ") or not value.endswith(" }"):
        raise StateError("Cloudflare unit ExecStart is malformed")
    fields: dict[str, str] = {}
    for item in value[2:-2].split(" ; "):
        key, separator, field_value = item.partition("=")
        if not separator or key not in CLOUDFLARE_EXEC_START_FIELDS or key in fields:
            raise StateError("Cloudflare unit ExecStart is malformed")
        fields[key] = field_value
    if tuple(fields) != CLOUDFLARE_EXEC_START_FIELDS or fields["ignore_errors"] != "no":
        raise StateError("Cloudflare unit ExecStart is malformed")
    if (
        fields["start_time"] != "[n/a]"
        or fields["stop_time"] != "[n/a]"
        or fields["pid"] != "0"
        or fields["code"] != "(null)"
        or fields["status"] != "0/0"
    ):
        raise StateError("Cloudflare unit ExecStart metadata changed")
    try:
        argv = tuple(shlex.split(fields["argv[]"], posix=True))
    except ValueError as exc:
        raise StateError("Cloudflare unit ExecStart is malformed") from exc
    expected = (
        "/bin/bash", "/opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh",
        "--app-dir", "/opt/oldsparky/platform", "--",
        "/opt/oldsparky/platform/shared/venv/bin/python",
        "/opt/oldsparky/platform/current/tools/platform_update_cloudflare_ips.py",
        "--apply", "--reload",
    )
    if fields["path"] != expected[0] or argv != expected:
        raise StateError("Cloudflare unit command identity changed")
    return argv


def _source_cloudflare_exec_start(release: Path) -> tuple[tuple[str, ...], str]:
    path = release / "deploy/systemd/deadlock-cloudflare-ips.service"
    raw = _read_cloudflare_source_file(path, maximum=64 * 1024, label="Cloudflare source unit")
    try:
        lines = raw.decode("ascii", errors="strict").splitlines()
    except UnicodeError as exc:
        raise StateError("Cloudflare source unit is malformed") from exc
    service = False
    exec_values: list[str] = []
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            service = line == "[Service]"
        elif service and line.startswith("ExecStart="):
            exec_values.append(line.partition("=")[2])
    if len(exec_values) != 1:
        raise StateError("Cloudflare source unit command is ambiguous")
    try:
        argv = tuple(shlex.split(exec_values[0], posix=True))
    except ValueError as exc:
        raise StateError("Cloudflare source unit command is malformed") from exc
    expected = (
        "/bin/bash", "/opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh",
        "--app-dir", "/opt/oldsparky/platform", "--",
        "/opt/oldsparky/platform/shared/venv/bin/python",
        "/opt/oldsparky/platform/current/tools/platform_update_cloudflare_ips.py",
        "--apply", "--reload",
    )
    if argv != expected:
        raise StateError("Cloudflare source unit command identity changed")
    return argv, hashlib.sha256(raw).hexdigest()


def _read_cloudflare_source_file(path: Path, *, maximum: int, label: str) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or before.st_size > maximum
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise StateError(f"{label} is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_uid != before.st_uid
            or opened.st_gid != before.st_gid
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != before.st_size
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise StateError(f"{label} changed while opening")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(8192, maximum + 1 - total)):
            total += len(chunk)
            if total > maximum:
                raise StateError(f"{label} exceeds its size limit")
            chunks.append(chunk)
        final = os.fstat(descriptor)
        after = path.lstat()
        if (
            total != opened.st_size
            or final.st_dev != opened.st_dev
            or final.st_ino != opened.st_ino
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
            or final.st_ctime_ns != opened.st_ctime_ns
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
        ):
            raise StateError(f"{label} changed while reading")
        return b"".join(chunks)
    except OSError as exc:
        raise StateError(f"{label} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _invocation_guard_record(invocation_id: str) -> tuple[datetime, bytes] | None:
    if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None or set(invocation_id) == {"0"}:
        return None
    try:
        status, output = _bounded_command(
            [
                JOURNALCTL_PATH, "--no-pager", "--quiet", "--output=json", "--lines=50",
                "--unit=" + CLOUDFLARE_UNIT, "_SYSTEMD_INVOCATION_ID=" + invocation_id,
            ],
            maximum=PRIVATE_JOURNAL_MAX_BYTES,
            timeout=15,
        )
    except StateError:
        return None
    if status != 0 or not output.endswith("\n"):
        return None
    matches: list[tuple[datetime, bytes]] = []
    for line in output.splitlines():
        if len(line) > 8192:
            return None
        try:
            entry = json.loads(line, object_pairs_hook=_strict_object)
        except (json.JSONDecodeError, StateError):
            return None
        if not isinstance(entry, dict):
            return None
        if entry.get("_SYSTEMD_INVOCATION_ID") != invocation_id:
            return None
        message = entry.get("MESSAGE")
        expected = re.fullmatch(
            r"RELEASE_GUARD schema=1 status=failed class=pending_operation release_slug=unavailable source_sha=unavailable",
            message if isinstance(message, str) else "",
        )
        if expected is not None:
            timestamp = entry.get("__REALTIME_TIMESTAMP")
            if not isinstance(timestamp, str) or re.fullmatch(r"[0-9]{1,20}", timestamp) is None:
                return None
            try:
                parsed = datetime.fromtimestamp(int(timestamp) / 1_000_000, timezone.utc)
                canonical = json.dumps(
                    entry,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            except (OverflowError, OSError, TypeError, ValueError):
                return None
            matches.append((parsed, hashlib.sha256(canonical).digest()))
    if len(matches) != 1:
        return None
    return matches[0]


def _fixed_pair_jobs_empty() -> bool:
    jobs = _fixed_jobs()
    return not any(job[1] in {CLOUDFLARE_UNIT, CLOUDFLARE_TIMER} for job in jobs)


def _private_evidence_directory(shared: Path) -> Path:
    _identity(shared, label="shared directory")
    directory = shared / ".release-cloudflare-pending-reset-evidence"
    created = False
    try:
        os.mkdir(directory, 0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise StateError("Cloudflare reset evidence directory is unavailable") from exc
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise StateError("Cloudflare reset evidence directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or directory.resolve(strict=True) != directory
    ):
        raise StateError("Cloudflare reset evidence directory is unsafe")
    if created:
        shared_fd = os.open(
            shared,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(shared_fd)
        finally:
            os.close(shared_fd)
    return directory


def _write_cloudflare_evidence(shared: Path, key: str, record: dict[str, object]) -> Path:
    directory = _private_evidence_directory(shared)
    path = directory / f"{key}.json"
    raw = (json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    if len(raw) > PRIVATE_EVIDENCE_MAX_BYTES:
        raise StateError("Cloudflare reset evidence is too large")
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptor = -1
    try:
        directory_metadata = os.fstat(directory_fd)
        path_metadata = directory.lstat()
        if (
            directory_metadata.st_dev != path_metadata.st_dev
            or directory_metadata.st_ino != path_metadata.st_ino
            or directory_metadata.st_uid != 0
            or directory_metadata.st_gid != 0
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise StateError("Cloudflare reset evidence directory changed")
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(descriptor)
        if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise StateError("Cloudflare reset evidence metadata is unsafe")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(descriptor)
        os.fsync(directory_fd)
    except OSError as exc:
        raise StateError("Cloudflare reset evidence already exists or cannot be created") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)
    return path


def _revalidate_cloudflare_binding(
    *,
    path: Path,
    transaction: Path,
    app_dir: Path,
    helper_release: Path,
    transaction_bytes: bytes,
    receipt_bytes: bytes,
    current: Path,
    previous: Path,
    invocation_id: str,
    expected_unit: dict[str, str],
    expected_timer: dict[str, str],
    expected_journal: tuple[datetime, bytes],
) -> None:
    """Recheck all durable bindings immediately before the one reset action."""
    if transaction.read_bytes() != transaction_bytes or path.read_bytes() != receipt_bytes:
        raise StateError("Cloudflare recovery receipt changed before reset")
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    _validate_transaction_binding(record, transaction, app_dir, helper_release)
    tx = json.loads(transaction_bytes.decode("ascii"), object_pairs_hook=_strict_object)
    if (
        tx.get("operation") != "install"
        or tx.get("phase") != "recovery-restored"
        or Path(cast(str, tx.get("current_before"))) != current
        or Path(cast(str, tx.get("previous_before"))) != previous
        or (app_dir / "current").resolve(strict=True) != current
        or (app_dir / "previous").resolve(strict=True) != previous
        or _cloudflare_source_sha(current) != PENDING_GUARD_CURRENT_SOURCE_SHA
        or _cloudflare_source_sha(previous) != PENDING_GUARD_PREVIOUS_SOURCE_SHA
    ):
        raise StateError("Cloudflare recovery binding changed before reset")
    unit = _fixed_show(CLOUDFLARE_UNIT, CLOUDFLARE_SERVICE_PROPERTIES)
    if unit != expected_unit or unit["InvocationID"] != invocation_id:
        raise StateError("Cloudflare invocation changed before reset")
    journal = _invocation_guard_record(invocation_id)
    if journal != expected_journal:
        raise StateError("Cloudflare guard evidence changed before reset")
    timer = _fixed_show(CLOUDFLARE_TIMER, CLOUDFLARE_TIMER_PROPERTIES)
    timer_status, timer_enabled = _run_systemctl("is-enabled", CLOUDFLARE_TIMER)
    next_elapse = _systemd_timestamp(timer["NextElapseUSecRealtime"])
    if (
        timer != expected_timer
        or timer_status != 0
        or timer_enabled != "enabled"
        or not _fixed_pair_jobs_empty()
        or (next_elapse - datetime.now(timezone.utc)).total_seconds()
        <= PENDING_GUARD_MIN_NEXT_TIMER_SECONDS
    ):
        raise StateError("Cloudflare timer binding changed before reset")


def _revalidate_cloudflare_timer_before_stop(expected_timer: dict[str, str]) -> None:
    timer = _fixed_show(CLOUDFLARE_TIMER, CLOUDFLARE_TIMER_PROPERTIES)
    timer_status, timer_enabled = _run_systemctl("is-enabled", CLOUDFLARE_TIMER)
    next_elapse = _systemd_timestamp(timer["NextElapseUSecRealtime"])
    if (
        timer != expected_timer
        or timer_status != 0
        or timer_enabled != "enabled"
        or not _fixed_pair_jobs_empty()
        or (next_elapse - datetime.now(timezone.utc)).total_seconds()
        <= PENDING_GUARD_MIN_NEXT_TIMER_SECONDS
    ):
        raise StateError("Cloudflare timer horizon changed before stop")


def reset_cloudflare_pending_guard(
    path: Path,
    *,
    transaction: Path,
    app_dir: Path,
    helper_release: Path,
    attempt_nonce: str,
    expected_event_id: str,
    apply_reset: bool = True,
) -> dict[str, object] | None:
    """Clear only the stale failed result proven to be the pending-operation guard."""
    if os.geteuid() != 0:
        raise StateError("Cloudflare pending-guard reset requires root")
    if re.fullmatch(r"[0-9a-f]{64}", attempt_nonce) is None:
        raise StateError("Cloudflare reset attempt nonce is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", expected_event_id) is None:
        raise StateError("Cloudflare reset expected event identity is invalid")
    record = _read_receipt(path)
    _validate_context(record, app_dir)
    _validate_transaction_binding(record, transaction, app_dir, helper_release)
    raw_transaction = transaction.read_bytes()
    raw_receipt = path.read_bytes()
    tx = json.loads(raw_transaction.decode("ascii"), object_pairs_hook=_strict_object)
    if (
        tx.get("operation") != "install"
        or tx.get("phase") != "recovery-restored"
        or record.get("operation") != "rollback"
    ):
        raise StateError("Cloudflare pending-guard transaction phase is unsupported")
    current = Path(cast(str, tx["current_before"]))
    previous = Path(cast(str, tx["previous_before"]))
    if (
        current.resolve(strict=True) != current
        or previous.resolve(strict=True) != previous
        or (app_dir / "current").resolve(strict=True) != current
        or (app_dir / "previous").resolve(strict=True) != previous
        or _cloudflare_source_sha(current) != PENDING_GUARD_CURRENT_SOURCE_SHA
        or _cloudflare_source_sha(previous) != PENDING_GUARD_PREVIOUS_SOURCE_SHA
    ):
        raise StateError("Cloudflare pending-guard release pair changed")
    units = _receipt_units(record)
    unit_receipt = next((item for item in units if item["name"] == CLOUDFLARE_UNIT), None)
    timer_receipt = next((item for item in units if item["name"] == CLOUDFLARE_TIMER), None)
    if unit_receipt != {"name": CLOUDFLARE_UNIT, "active": "inactive", "enabled": "static"} or timer_receipt != {
        "name": CLOUDFLARE_TIMER, "active": "active", "enabled": "enabled",
    }:
        raise StateError("Cloudflare receipt state is not the fixed recovery target")

    try:
        unit = _fixed_show(CLOUDFLARE_UNIT, CLOUDFLARE_SERVICE_PROPERTIES)
    except StateError:
        # This optional incident-specific cleanup must never block the
        # canonical receipt restore when the observation itself is unavailable.
        return {"outcome": "skip"}
    try:
        loaded_argv = _cloudflare_exec_start(unit["ExecStart"])
        source_argv, source_unit_sha = _source_cloudflare_exec_start(current)
    except StateError:
        return {"outcome": "skip"}
    if (
        unit["Id"] != CLOUDFLARE_UNIT
        or unit["FragmentPath"] != CLOUDFLARE_FRAGMENT
        or unit["DropInPaths"] != ""
        or unit["NeedDaemonReload"] != "no"
        or unit["Type"] != "oneshot"
        or unit["RemainAfterExit"] != "no"
        or unit["KillMode"] != "control-group"
        or unit["UnitFileState"] != "static"
        or unit["MainPID"] != "0"
        or unit["ControlPID"] != "0"
        or unit["ControlGroup"] != ""
        or unit["TasksCurrent"] != "[not set]"
        or loaded_argv != source_argv
    ):
        return {"outcome": "skip"}
    try:
        unit_active_status, unit_active = _run_systemctl("is-active", CLOUDFLARE_UNIT)
        unit_enabled_status, unit_enabled = _run_systemctl("is-enabled", CLOUDFLARE_UNIT)
    except StateError:
        return {"outcome": "skip"}
    if unit_enabled_status != 0 or unit_enabled != "static":
        return {"outcome": "skip"}
    if (
        unit["ActiveState"] != "failed"
        or unit["SubState"] != "failed"
        or unit["Result"] != "exit-code"
        or unit["ExecMainCode"] != "1"
        or unit["ExecMainStatus"] != "75"
        or unit_active != "failed"
        or unit_active_status != 3
    ):
        return {"outcome": "skip"}
    if (
        unit["SubState"] != "failed"
        or unit["Result"] != "exit-code"
        or unit["ExecMainCode"] != "1"
        or unit["ExecMainStatus"] != "75"
        or unit["MainPID"] != "0"
        or unit["ControlPID"] != "0"
    ):
        raise StateError("Cloudflare oneshot failure state is not the fixed target")
    invocation_id = unit["InvocationID"]
    journal = _invocation_guard_record(invocation_id)
    if journal is None:
        return {"outcome": "skip"}
    journal_time, journal_digest = journal
    try:
        start_time = _systemd_timestamp(unit["ExecMainStartTimestamp"])
    except StateError:
        return {"outcome": "skip"}
    if abs((journal_time - start_time).total_seconds()) > 120:
        return {"outcome": "skip"}

    timer = _fixed_show(CLOUDFLARE_TIMER, CLOUDFLARE_TIMER_PROPERTIES)
    timer_enabled_status, timer_enabled = _run_systemctl("is-enabled", CLOUDFLARE_TIMER)
    if (
        timer["Id"] != CLOUDFLARE_TIMER
        or timer["FragmentPath"] != "/etc/systemd/system/deadlock-cloudflare-ips.timer"
        or timer["DropInPaths"] != ""
        or timer["NeedDaemonReload"] != "no"
        or timer["ActiveState"] != "active"
        or timer["SubState"] != "waiting"
        or timer["Unit"] != CLOUDFLARE_UNIT
        or timer["UnitFileState"] != "enabled"
        or timer_enabled_status != 0
        or timer_enabled != "enabled"
        or not _fixed_pair_jobs_empty()
    ):
        raise StateError("Cloudflare timer is not safely pending")

    last_trigger = _systemd_timestamp(timer["LastTriggerUSec"])
    next_elapse = _systemd_timestamp(timer["NextElapseUSecRealtime"])
    now = datetime.now(timezone.utc)
    if (
        abs((last_trigger - start_time).total_seconds()) > 120
        or abs((last_trigger - journal_time).total_seconds()) > 120
        or (next_elapse - now).total_seconds() <= PENDING_GUARD_MIN_NEXT_TIMER_SECONDS
        or next_elapse <= last_trigger
    ):
        raise StateError("Cloudflare timer horizon does not protect the recovery window")

    operation_id = cast(str, tx["operation_id"])
    transaction_sha = hashlib.sha256(raw_transaction).hexdigest()
    receipt_sha = hashlib.sha256(raw_receipt).hexdigest()
    key = hashlib.sha256(
        f"{operation_id}:{transaction_sha}:{receipt_sha}:{invocation_id}".encode("ascii")
    ).hexdigest()
    # The wrapper snapshots this event key before invoking us.  If the
    # invocation or bound files changed between that snapshot and this fresh
    # preflight, leave the incident to canonical recovery without authorizing
    # an action or a compensation record for a different event.
    if key != expected_event_id:
        return {"outcome": "skip"}
    source_sha = _cloudflare_source_sha(current)
    evidence: dict[str, object] = {
        "schema": 1,
        "kind": "cloudflare_pending_guard_reset_evidence",
        "observed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "source_sha": source_sha,
        "previous_source_sha": _cloudflare_source_sha(previous),
        "executing_helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "target_helper_manifest": _helper_digests(current),
        "current_before": str(current),
        "previous_before": str(previous),
        "current_before_identity": tx["current_before_identity"],
        "previous_before_identity": tx["previous_before_identity"],
        "operation": tx["operation"],
        "operation_id": operation_id,
        "phase": tx["phase"],
        "transaction_sha256": transaction_sha,
        "systemd_receipt_sha256": receipt_sha,
        "attempt_nonce": attempt_nonce,
        "service": {
            "fragment_path": unit["FragmentPath"],
            "exec_start": list(loaded_argv),
            "source_unit_sha256": source_unit_sha,
            "invocation_id": invocation_id,
            "active_state": unit["ActiveState"],
            "sub_state": unit["SubState"],
            "result": unit["Result"],
            "exec_main_code": 1,
            "exec_main_status": 75,
            "main_pid": 0,
            "control_pid": 0,
            "control_group": "",
            "tasks_current": "[not set]",
            "exec_main_start_timestamp": unit["ExecMainStartTimestamp"],
        },
        "guard_journal": {
            "timestamp_utc": journal_time.isoformat().replace("+00:00", "Z"),
            "record_sha256": journal_digest.hex(),
        },
        "timer": {
            "active_state": timer["ActiveState"],
            "sub_state": timer["SubState"],
            "unit": timer["Unit"],
            "unit_file_state": timer["UnitFileState"],
            "last_trigger": timer["LastTriggerUSec"],
            "next_elapse": timer["NextElapseUSecRealtime"],
            "jobs_for_pair": 0,
        },
        "planned_action": "reset-failed deadlock-cloudflare-ips.service",
    }
    _revalidate_cloudflare_binding(
        path=path,
        transaction=transaction,
        app_dir=app_dir,
        helper_release=helper_release,
        transaction_bytes=raw_transaction,
        receipt_bytes=raw_receipt,
        current=current,
        previous=previous,
        invocation_id=invocation_id,
        expected_unit=unit,
        expected_timer=timer,
        expected_journal=(journal_time, journal_digest),
    )
    if not apply_reset:
        evidence["outcome"] = "preflight"
        return evidence

    timer_stop_attempted = False
    try:
        _revalidate_cloudflare_timer_before_stop(timer)
        # Bind the attempt durably only after all ordinary guards pass.  The
        # next operation is the one fixed timer stop, so the EXIT trap can
        # compensate only an attempt that crossed this point.
        evidence_path = _write_cloudflare_evidence(app_dir / "shared", key, evidence)
        timer_stop_attempted = True
        status, _ = _run_systemctl("stop", CLOUDFLARE_TIMER)
        if status != 0:
            raise StateError("Cloudflare timer stop failed")
        stopped_timer = _fixed_show(CLOUDFLARE_TIMER, CLOUDFLARE_TIMER_PROPERTIES)
        active_status, active_value = _run_systemctl("is-active", CLOUDFLARE_TIMER)
        if (
            active_status != 3
            or active_value != "inactive"
            or stopped_timer["ActiveState"] != "inactive"
            or stopped_timer["Unit"] != CLOUDFLARE_UNIT
            or stopped_timer["UnitFileState"] != "enabled"
            or not _fixed_pair_jobs_empty()
        ):
            raise StateError("Cloudflare timer did not quiesce safely")
        if transaction.read_bytes() != raw_transaction or path.read_bytes() != raw_receipt:
            raise StateError("Cloudflare recovery receipt changed before reset")
        bound_record = _read_receipt(path)
        _validate_context(bound_record, app_dir)
        _validate_transaction_binding(bound_record, transaction, app_dir, helper_release)
        if (
            (app_dir / "current").resolve(strict=True) != current
            or (app_dir / "previous").resolve(strict=True) != previous
            or _cloudflare_source_sha(current) != PENDING_GUARD_CURRENT_SOURCE_SHA
            or _cloudflare_source_sha(previous) != PENDING_GUARD_PREVIOUS_SOURCE_SHA
            or _invocation_guard_record(invocation_id) != (journal_time, journal_digest)
        ):
            raise StateError("Cloudflare recovery binding changed before reset")
        current_unit = _fixed_show(CLOUDFLARE_UNIT, CLOUDFLARE_SERVICE_PROPERTIES)
        if (
            current_unit != unit
            or current_unit["InvocationID"] != invocation_id
            or _source_cloudflare_exec_start(current)[0] != source_argv
            or _cloudflare_source_sha(current) != PENDING_GUARD_CURRENT_SOURCE_SHA
            or not _fixed_pair_jobs_empty()
        ):
            raise StateError("Cloudflare invocation changed before reset")
        status, _ = _run_systemctl("reset-failed", CLOUDFLARE_UNIT)
        if status != 0:
            raise StateError("Cloudflare pending-guard reset failed")
        reset_unit = _fixed_show(CLOUDFLARE_UNIT, CLOUDFLARE_SERVICE_PROPERTIES)
        active_status, active_value = _run_systemctl("is-active", CLOUDFLARE_UNIT)
        enabled_status, enabled_value = _run_systemctl("is-enabled", CLOUDFLARE_UNIT)
        if (
            active_status != 3
            or active_value != "inactive"
            or enabled_status != 0
            or enabled_value != "static"
            or reset_unit["ActiveState"] != "inactive"
            or reset_unit["SubState"] != "dead"
            or reset_unit["UnitFileState"] != "static"
            or reset_unit["MainPID"] != "0"
            or reset_unit["ControlPID"] != "0"
            or reset_unit["ControlGroup"] != ""
            or reset_unit["TasksCurrent"] != "[not set]"
            or not _fixed_pair_jobs_empty()
        ):
            raise StateError("Cloudflare reset did not verify inactive/static")
    except BaseException as exc:
        if timer_stop_attempted:
            active_status, active_value = _run_systemctl("is-active", CLOUDFLARE_TIMER)
            if active_status != 0 or active_value != "active":
                restore_status, _ = _run_systemctl("start", CLOUDFLARE_TIMER)
                verify_status, verify_value = _run_systemctl("is-active", CLOUDFLARE_TIMER)
                enabled_status, enabled_value = _run_systemctl("is-enabled", CLOUDFLARE_TIMER)
                if restore_status != 0 or verify_status != 0 or verify_value != "active" or enabled_status != 0 or enabled_value != "enabled":
                    raise StateError("Cloudflare timer restoration failed after reset refusal") from exc
        raise
    if not evidence_path.is_file():
        raise StateError("Cloudflare reset evidence disappeared")
    return {"outcome": "performed", "evidence_id": key}


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
    cloudflare_reset = commands.add_parser("reset-cloudflare-pending-guard")
    cloudflare_reset.add_argument("--systemctl", default=SYSTEMCTL)
    cloudflare_reset.add_argument("--state", required=True, type=Path)
    cloudflare_reset.add_argument("--transaction", required=True, type=Path)
    cloudflare_reset.add_argument("--helper-release", required=True, type=Path)
    cloudflare_reset.add_argument("--app-dir", required=True, type=Path)
    cloudflare_reset.add_argument("--attempt-nonce", required=True)
    cloudflare_reset.add_argument("--expected-event-id", required=True)
    health_baseline = commands.add_parser("health-monitor-baseline")
    health_baseline.add_argument("--systemctl", default=SYSTEMCTL)
    health_wait = commands.add_parser("wait-health-monitor-success")
    health_wait.add_argument("--systemctl", default=SYSTEMCTL)
    health_wait.add_argument("--baseline", required=True)
    health_wait.add_argument("--timeout", type=int, default=HEALTH_MONITOR_WAIT_SECONDS)
    return parser


def _emit_cloudflare_reset_private_marker() -> None:
    if (
        os.environ.get("PLATFORM_RUNTIME_PRIVATE_CAPTURE") != "1"
        or os.environ.get("PLATFORM_RUNTIME_PRIVATE_ROUTE") != "recover_pending"
    ):
        return
    output = b"fixed cloudflare pending-guard reset refused\n"
    print(
        "RUNTIME_PRIVATE_CONTEXT "
        + json.dumps(
            {
                "schema": 1,
                "route": "recover_pending",
                "component": "systemd_state_restore",
                "guard_id": None,
                "returncode": 2,
                "stream": "combined",
                "output_b64": base64.b64encode(output).decode("ascii"),
                "total_bytes": len(output),
                "truncated": False,
                "retained": "tail",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )


def main() -> int:
    args = _parser().parse_args()
    try:
        global SYSTEMCTL_PATH
        SYSTEMCTL_PATH = _validate_systemctl_path(args.systemctl)
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
        elif args.command == "reset-cloudflare-pending-guard":
            result = reset_cloudflare_pending_guard(
                args.state,
                transaction=args.transaction,
                app_dir=args.app_dir,
                helper_release=args.helper_release,
                attempt_nonce=args.attempt_nonce,
                expected_event_id=args.expected_event_id,
            )
            outcome = result.get("outcome") if isinstance(result, dict) else None
            evidence_id = result.get("evidence_id") if isinstance(result, dict) else None
            if outcome == "performed":
                if not isinstance(evidence_id, str) or re.fullmatch(r"[0-9a-f]{64}", evidence_id) is None:
                    raise StateError("Cloudflare reset evidence identity is invalid")
                print(f"RELEASE_CLOUDFLARE_PENDING_RESET schema=1 status=performed evidence_id={evidence_id}")
            elif outcome == "skip":
                print("RELEASE_CLOUDFLARE_PENDING_RESET schema=1 status=skip")
            else:
                raise StateError("Cloudflare reset outcome is invalid")
        elif args.command == "health-monitor-baseline":
            print(json.dumps(_health_monitor_baseline(), sort_keys=True, separators=(",", ":")))
        elif args.command == "wait-health-monitor-success":
            _wait_health_monitor_success(args.baseline, timeout=args.timeout)
        else:
            raise StateError("unknown systemd state command")
        return 0
    except (StateError, OSError, ValueError):
        # Keep paths and systemctl output out of public logs.  Callers only
        # need a stable non-zero result and retain the receipt for recovery.
        if args.command == "reset-cloudflare-pending-guard":
            _emit_cloudflare_reset_private_marker()
        print("RELEASE_SYSTEMD_STATE schema=1 status=failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
