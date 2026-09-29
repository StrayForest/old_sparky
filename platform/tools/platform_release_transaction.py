#!/usr/bin/env python3
"""Durable, inode-checked install/rollback transaction recovery."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import sys
import time
from typing import cast
from uuid import uuid4

try:
    from .platform_release_systemd_state import INITIAL_SYSTEMD_UNITS
except ImportError:  # The immutable recovery generation runs this file directly.
    # ``python -I`` deliberately removes the script directory from
    # ``sys.path``.  Load the sibling by its trusted, generation-local path
    # instead of weakening isolation by adding an arbitrary import path.
    _systemd_state_path = Path(__file__).resolve().with_name(
        "platform_release_systemd_state.py"
    )
    INITIAL_SYSTEMD_UNITS = runpy.run_path(str(_systemd_state_path))[
        "INITIAL_SYSTEMD_UNITS"
    ]


STATE_NAME = ".release-operation.json"
QUIESCE_STATE_NAME = ".release-quiesce.json"
STATE_VERSION = 2
# Version 1 is the narrow operation-less receipt written before the installer
# transaction exists.  It predates enabled-state capture and is retained only
# for exact compatibility; all newly-created receipts are version 2.
LEGACY_QUIESCE_STATE_VERSION = 1
QUIESCE_STATE_VERSION = 2
RENAME_EXCHANGE = 2
AT_FDCWD = -100
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SERVICE_UNITS = ("deadlock-api", "deadlock-worker", "deadlock-web")
SYSTEMD_CALL_TIMEOUT_SECONDS = 30.0
SYSTEMD_OPERATION_TIMEOUT_SECONDS = 120.0
PHASES = {
    "prepared",
    "venv-transitioned",
    "snapshot-placed",
    "current-switched",
    "previous-switched",
    "pointers-switched",
    "staged",
    "migration-pending",
    "migration-failed",
    "migration-applied",
    "activation-pending",
    "services-restarted",
    "nginx-pending",
    "nginx-applied",
    "smoke-passed",
    "systemd-activation-pending",
    "systemd-activated",
    "activation-committed",
    "recovery-authorized",
    "restart-pending",
    "rollback-runtime-pending",
    "rollback-runtime-applied",
    "filesystem-restored-runtime-pending",
    "filesystem-restored-services-pending",
    "recovery-restored",
}
PHASE_TRANSITIONS = {
    "prepared": {"venv-transitioned"},
    "venv-transitioned": {
        "snapshot-placed",
        "previous-switched",
        "pointers-switched",
        "staged",
        "current-switched",
    },
    "snapshot-placed": {"previous-switched", "pointers-switched", "staged"},
    "previous-switched": {"current-switched", "pointers-switched"},
    "current-switched": {"pointers-switched"},
    "pointers-switched": {
        "activation-pending",
        "restart-pending",
        "rollback-runtime-pending",
    },
    "staged": {"migration-pending"},
    "migration-pending": {"migration-failed", "migration-applied", "recovery-authorized"},
    "migration-failed": {"migration-pending", "migration-applied", "recovery-authorized"},
    "migration-applied": {
        "previous-switched",
        "current-switched",
        "activation-pending",
        "recovery-authorized",
    },
    "activation-pending": {"services-restarted", "recovery-authorized"},
    "services-restarted": {
        "nginx-pending",
        "nginx-applied",
        "smoke-passed",
        "recovery-authorized",
    },
    "nginx-pending": {"nginx-applied", "recovery-authorized"},
    "nginx-applied": {"smoke-passed", "recovery-authorized"},
    "smoke-passed": {
        "systemd-activation-pending",
        "activation-committed",
        "rollback-runtime-applied",
        "recovery-authorized",
    },
    "systemd-activation-pending": {
        "systemd-activated",
        "recovery-authorized",
    },
    "systemd-activated": {
        "systemd-activation-pending",
        "activation-committed",
        "recovery-authorized",
    },
    "activation-committed": {"systemd-activation-pending", "recovery-authorized"},
    "recovery-authorized": set(),
    "restart-pending": {
        "services-restarted",
        "rollback-runtime-applied",
        "recovery-authorized",
    },
    "rollback-runtime-pending": {
        "restart-pending",
        "rollback-runtime-applied",
        "filesystem-restored-runtime-pending",
        "recovery-authorized",
    },
    "rollback-runtime-applied": {"recovery-authorized"},
    "filesystem-restored-runtime-pending": {"recovery-restored"},
    "filesystem-restored-services-pending": {"recovery-restored"},
    "recovery-restored": set(),
}
MIGRATION_OUTCOME_UNCERTAIN_PHASES = {
    "migration-pending",
    "migration-failed",
    "migration-applied",
    "activation-pending",
    "services-restarted",
    "nginx-pending",
    "nginx-applied",
    "smoke-passed",
    "systemd-activation-pending",
    "systemd-activated",
    "activation-committed",
}
INITIAL_SYSTEMD_PHASES = frozenset(
    {
        "staged",
        "migration-pending",
        "migration-failed",
        "migration-applied",
        "activation-pending",
        "services-restarted",
        "nginx-pending",
        "nginx-applied",
        "smoke-passed",
        "systemd-activation-pending",
        "systemd-activated",
        "activation-committed",
        "recovery-authorized",
        "recovery-restored",
    }
)
INITIAL_RECOVERY_PHASES = frozenset(
    {
        "prepared",
        "venv-transitioned",
        "snapshot-placed",
        "current-switched",
        "previous-switched",
        "pointers-switched",
        *INITIAL_SYSTEMD_PHASES,
    }
)
RECOVERY_CONFIRMATION = "MIGRATION_NOT_REVERSED"
RECORD_KEYS = {
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
}
LEGACY_RECORD_KEYS = RECORD_KEYS - {
    "operation_id",
    "service_enabled_before",
    "timer_enabled_before",
    "systemd_state_before",
}
RECORD_KEYS_WITHOUT_SYSTEMD_STATE = RECORD_KEYS - {"systemd_state_before"}
QUIESCE_RECORD_KEYS = {
    "version",
    "operation",
    "phase",
    "app_dir",
    "current_before",
    "previous_before",
    "candidate_release",
    "shared_env_before",
    "current_before_identity",
    "previous_before_identity",
    "service_state_before",
    "service_enabled_before",
    "quiesced_services",
    "timer_active_before",
    "timer_enabled_before",
}
LEGACY_QUIESCE_RECORD_KEYS = QUIESCE_RECORD_KEYS - {
    "service_enabled_before",
    "timer_enabled_before",
}


class TransactionError(RuntimeError):
    """A release transaction cannot be proven safe."""


def _validate_service_snapshot(record: dict[str, object]) -> None:
    service_state = record.get("service_state_before")
    service_enabled = record.get("service_enabled_before")
    quiesced_services = record.get("quiesced_services")
    timer_active_before = record.get("timer_active_before")
    timer_enabled = record.get("timer_enabled_before")

    # Rollback transactions do not quiesce the application services. The
    # low-level installer may create an install receipt before the deploy
    # wrapper has captured its pre-migration state; the production migration
    # and abort callers reject that receipt rather than guessing a state.
    if (
        service_state is None
        and service_enabled is None
        and quiesced_services is None
        and timer_active_before is None
        and timer_enabled is None
    ):
        return
    if (
        record.get("phase") == "recovery-restored"
        and service_enabled is None
        and timer_enabled is None
    ):
        # The only retained compatibility window for pre-enabled-state v2
        # receipts is an already restored, read-only cleanup record.  A live
        # pending/current-only recovery must carry both dimensions.
        return
    if record.get("operation") != "install":
        raise TransactionError(
            "service state is unexpected for a rollback transaction"
        )
    if not isinstance(service_state, dict) or set(service_state) != set(SERVICE_UNITS):
        raise TransactionError("pre-migration service state is invalid")
    if any(
        type(value) is not str or value not in {"active", "inactive"}
        for value in service_state.values()
    ):
        raise TransactionError("pre-migration service state is invalid")
    if (
        not isinstance(service_enabled, dict)
        or set(service_enabled) != set(SERVICE_UNITS)
        or any(type(value) is not str or value not in {"enabled", "disabled"} for value in service_enabled.values())
    ):
        raise TransactionError("pre-migration enabled service state is invalid")
    if (
        not isinstance(quiesced_services, list)
        or quiesced_services != list(SERVICE_UNITS)
    ):
        raise TransactionError("quiesced service set is invalid")
    if type(timer_active_before) is not bool:
        raise TransactionError("pre-migration timer state is invalid")
    if timer_enabled not in {"enabled", "disabled"}:
        raise TransactionError("pre-migration enabled timer state is invalid")


def _validate_initial_systemd_snapshot(record: dict[str, object]) -> None:
    """Validate the clean-install systemd baseline bound to the transaction.

    The snapshot is deliberately narrow: it is only an authority for a
    first-install operation with no prior release, and every intended unit
    must have been inactive and disabled before the installer can mutate the
    unit files or enablement topology.  Missing snapshots remain valid for
    older receipts, but they cannot authorize initial activation/recovery.
    """

    snapshot = record.get("systemd_state_before")
    if snapshot is None:
        return
    if (
        record.get("operation") != "install"
        or record.get("current_before") is not None
        or record.get("previous_before") is not None
    ):
        raise TransactionError("initial systemd snapshot is unexpected")
    if not isinstance(snapshot, dict) or set(snapshot) != set(INITIAL_SYSTEMD_UNITS):
        raise TransactionError("initial systemd snapshot is incomplete")
    for unit in INITIAL_SYSTEMD_UNITS:
        state = snapshot.get(unit)
        if (
            not isinstance(state, dict)
            or set(state) != {"active", "enabled"}
            or state.get("active") != "inactive"
            or state.get("enabled") != "disabled"
        ):
            raise TransactionError("initial systemd snapshot is not inactive/disabled")


def _validate_legacy_quiesce_snapshot(record: dict[str, object]) -> None:
    """Validate the exact version-1 operation-less snapshot contract.

    Version 1 was written before enabled-state capture existed.  It is safe
    to use only for restoring active/inactive state; accepting any enabled
    fields here would make a hybrid receipt ambiguous and could turn an
    untrusted extra field into restore authority.
    """

    service_state = record.get("service_state_before")
    quiesced_services = record.get("quiesced_services")
    timer_active_before = record.get("timer_active_before")
    if not isinstance(service_state, dict) or set(service_state) != set(SERVICE_UNITS):
        raise TransactionError("pre-quiesce service state is invalid")
    if any(
        type(value) is not str or value not in {"active", "inactive"}
        for value in service_state.values()
    ):
        raise TransactionError("pre-quiesce service state is invalid")
    if quiesced_services != list(SERVICE_UNITS):
        raise TransactionError("pre-quiesce quiesced service set is invalid")
    if type(timer_active_before) is not bool:
        raise TransactionError("pre-quiesce timer state is invalid")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TransactionError("release operation record contains duplicate keys")
        result[key] = value
    return result


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _safe_directory(path: Path, *, label: str) -> os.stat_result:
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise TransactionError(f"{label} path is not canonical")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TransactionError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_nlink < 2
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or resolved != path
    ):
        raise TransactionError(f"{label} metadata is unsafe")
    return metadata


def _optional_safe_directory(path: Path, *, label: str) -> os.stat_result | None:
    if not _lexists(path):
        return None
    return _safe_directory(path, label=label)


def _safe_state_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TransactionError("release operation record is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > 64 * 1024
    ):
        raise TransactionError("release operation record metadata is unsafe")
    return metadata


def _optional_safe_private_file(path: Path, *, label: str) -> os.stat_result | None:
    if not _lexists(path):
        return None
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TransactionError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise TransactionError(f"{label} metadata is unsafe")
    return metadata


def _identity(metadata: os.stat_result) -> dict[str, int]:
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


def _matches(path: Path, identity: dict[str, int]) -> bool:
    metadata = _optional_safe_directory(path, label=f"transaction path {path.name}")
    return metadata is not None and _identity(metadata) == identity


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_fsync_record(
    path: Path,
    *,
    label: str,
    expected: bytes,
    maximum: int = 4096,
) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise TransactionError(f"{label} is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > maximum
        ):
            raise TransactionError(f"{label} metadata is unsafe")
        raw = os.read(descriptor, maximum + 1)
        if raw != expected:
            raise TransactionError(f"{label} is invalid")
        os.fsync(descriptor)
    except OSError as exc:
        raise TransactionError(f"{label} is unavailable") from exc
    finally:
        os.close(descriptor)


def _safe_file_sha256(path: Path, *, label: str) -> str:
    try:
        metadata = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise TransactionError(f"{label} is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != 0
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o444
            or opened.st_size > 1024 * 1024
            or _identity(opened) != _identity(metadata)
        ):
            raise TransactionError(f"{label} metadata is unsafe")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _fsync_install_rollback_metadata(
    candidate: Path,
    current_before: str,
    *,
    transition: str,
) -> None:
    rollback = candidate / ".rollback"
    _safe_directory(rollback, label="install rollback metadata directory")
    expected_file = rollback / "previous-release"
    if not current_before:
        if _lexists(expected_file):
            raise TransactionError("install rollback previous record is unexpected")
        _fsync_directory(rollback)
        _fsync_directory(candidate)
        return
    _read_fsync_record(
        expected_file,
        label="install rollback previous record",
        expected=f"{current_before}\n".encode(),
    )
    transition_value = "snapshot" if transition == "exchange" else "unchanged"
    _read_fsync_record(
        rollback / "venv-transition",
        label="install rollback transition record",
        expected=f"{transition_value}\n".encode(),
    )
    freeze_record = rollback / "shared-freeze.sha256"
    if transition == "none":
        freeze_digest = _safe_file_sha256(
            candidate / "requirements-platform.freeze.txt",
            label="candidate Python freeze",
        )
        _read_fsync_record(
            freeze_record,
            label="install rollback freeze record",
            expected=f"{freeze_digest}\n".encode(),
        )
    elif _lexists(freeze_record):
        raise TransactionError("install rollback freeze record is unexpected")
    _fsync_directory(rollback)
    _fsync_directory(candidate)


def _write_record(state: Path, record: dict[str, object], *, creating: bool) -> None:
    _safe_directory(state.parent, label="shared release directory")
    if creating and _lexists(state):
        raise TransactionError("a release operation is already pending")
    temporary = state.parent / f".{STATE_NAME}.{uuid4().hex}.tmp"
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
        raw = (
            json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("ascii")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise TransactionError("release operation record could not be written")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, state)
    _fsync_directory(state.parent)


def _release_target(
    path_value: object,
    releases: Path,
    *,
    label: str,
    must_exist: bool = True,
) -> Path | None:
    if path_value is None:
        return None
    if not isinstance(path_value, str):
        raise TransactionError(f"{label} is invalid")
    path = Path(path_value)
    if path.parent != releases or SLUG_PATTERN.fullmatch(path.name) is None:
        raise TransactionError(f"{label} escapes the releases directory")
    if must_exist or _lexists(path):
        _safe_directory(path, label=label)
    return path


def _validate_record(
    state: Path,
    record: dict[str, object],
) -> dict[str, object]:
    legacy_recovery = (
        set(record) == LEGACY_RECORD_KEYS
        and record.get("phase") == "recovery-restored"
    )
    if (
        set(record) not in (RECORD_KEYS, RECORD_KEYS_WITHOUT_SYSTEMD_STATE)
        and not legacy_recovery
    ) or record.get("version") != STATE_VERSION:
        raise TransactionError("release operation record schema is invalid")
    if "systemd_state_before" not in record:
        # Existing v2 receipts predate first-install systemd activation. Keep
        # them readable, but normalize the optional field in memory so every
        # subsequent durable write has one unambiguous schema.
        record = {**record, "systemd_state_before": None}
    if not legacy_recovery and (
        not isinstance(record.get("operation_id"), str)
        or OPERATION_ID_PATTERN.fullmatch(record["operation_id"]) is None
    ):
        raise TransactionError("release operation identity is invalid")
    if legacy_recovery:
        # Legacy receipts are read-only recovery inputs. They may be cleaned
        # up when no systemd receipt exists, but cannot authorize a new
        # systemd capture/restore because no durable operation correlation is
        # available. Never synthesize or persist an identity for them.
        record = {**record, "operation_id": None}
    operation = record.get("operation")
    phase = record.get("phase")
    transition = record.get("transition")
    if operation not in {"install", "rollback"}:
        raise TransactionError("release operation type is invalid")
    if phase not in PHASES:
        raise TransactionError("release operation phase is invalid")
    if transition not in {"exchange", "create", "none"}:
        raise TransactionError("release venv transition is invalid")
    if type(record.get("remove_env_on_recovery")) is not bool:
        raise TransactionError("release env recovery flag is invalid")
    _validate_service_snapshot(record)
    _validate_initial_systemd_snapshot(record)

    app = Path(str(record.get("app_dir")))
    _safe_directory(app, label="application directory")
    releases = app / "releases"
    shared = app / "shared"
    _safe_directory(releases, label="releases directory")
    _safe_directory(shared, label="shared directory")
    if state != shared / STATE_NAME:
        raise TransactionError("release operation record path is invalid")

    current_before = _release_target(
        record.get("current_before"), releases, label="original current release"
    )
    previous_before = _release_target(
        record.get("previous_before"), releases, label="original previous release"
    )
    candidate = _release_target(
        record.get("candidate_release"),
        releases,
        label="candidate release",
        must_exist=not (operation == "install" and phase == "recovery-restored"),
    )
    if candidate is None:
        raise TransactionError("candidate release is missing")
    if operation == "rollback" and current_before != candidate:
        raise TransactionError("rollback candidate does not match original current")
    if operation == "rollback" and previous_before is None:
        raise TransactionError("rollback target is missing")
    current_before_identity = record.get("current_before_identity")
    previous_before_identity = record.get("previous_before_identity")
    candidate_identity = record.get("candidate_identity")
    for path, identity, label in (
        (current_before, current_before_identity, "original current release"),
        (previous_before, previous_before_identity, "original previous release"),
    ):
        if path is None:
            if identity is not None:
                raise TransactionError(f"{label} identity is unexpected")
        elif not _valid_identity(identity) or not _matches(path, identity):
            raise TransactionError(f"{label} identity changed")
    if not _valid_identity(candidate_identity):
        raise TransactionError("candidate release identity is invalid")
    if _lexists(candidate):
        candidate_identity = cast(dict[str, int], candidate_identity)
        if not _matches(candidate, candidate_identity):
            raise TransactionError("candidate release identity changed")
    if operation == "install" and candidate in {current_before, previous_before}:
        raise TransactionError("install candidate is already active")

    shared_venv = Path(str(record.get("shared_venv")))
    peer = Path(str(record.get("peer")))
    snapshot = Path(str(record.get("snapshot")))
    expected_snapshot = candidate / ".rollback/shared-venv-before-install"
    if shared_venv != shared / "venv" or snapshot != expected_snapshot:
        raise TransactionError("release venv transaction paths are invalid")
    if operation == "install":
        expected_prefix = f".venv-install-{candidate.name}."
        if peer.parent != shared or not peer.name.startswith(expected_prefix):
            raise TransactionError("install venv peer path is invalid")
    elif peer != snapshot:
        raise TransactionError("rollback venv peer path is invalid")

    shared_before = record.get("shared_before")
    peer_before = record.get("peer_before")
    if transition == "exchange":
        if not _valid_identity(shared_before) or not _valid_identity(peer_before):
            raise TransactionError("release venv identities are invalid")
        shared_before = cast(dict[str, int], shared_before)
        peer_before = cast(dict[str, int], peer_before)
        if shared_before == peer_before:
            raise TransactionError("release venv identities are ambiguous")
    elif transition == "create":
        if (
            operation != "install"
            or shared_before is not None
            or not _valid_identity(peer_before)
        ):
            raise TransactionError("created venv identity is invalid")
    elif peer_before is not None or (
        shared_before is not None and not _valid_identity(shared_before)
    ):
        raise TransactionError("no-op venv identity is invalid")

    return {
        **record,
        "app": app,
        "releases": releases,
        "shared": shared,
        "current_before_path": current_before,
        "previous_before_path": previous_before,
        "candidate_path": candidate,
        "shared_venv_path": shared_venv,
        "peer_path": peer,
        "snapshot_path": snapshot,
    }


def _validate_quiesce_record(
    state: Path,
    record: dict[str, object],
) -> dict[str, object]:
    version = record.get("version")
    if version == QUIESCE_STATE_VERSION:
        expected_keys = QUIESCE_RECORD_KEYS
    elif version == LEGACY_QUIESCE_STATE_VERSION:
        expected_keys = LEGACY_QUIESCE_RECORD_KEYS
    else:
        expected_keys = set()
    if set(record) != expected_keys:
        raise TransactionError("pre-quiesce receipt schema is invalid")
    if record.get("operation") != "install":
        raise TransactionError("pre-quiesce receipt operation is invalid")
    if record.get("phase") != "quiesce-pending":
        raise TransactionError("pre-quiesce receipt phase is invalid")
    app_value = record.get("app_dir")
    if not isinstance(app_value, str):
        raise TransactionError("pre-quiesce receipt application path is invalid")
    app = Path(app_value)
    _safe_directory(app, label="application directory")
    releases = app / "releases"
    shared = app / "shared"
    _safe_directory(releases, label="releases directory")
    _safe_directory(shared, label="shared directory")
    if state not in {shared / QUIESCE_STATE_NAME, shared / STATE_NAME}:
        raise TransactionError("pre-quiesce receipt path is invalid")

    current_before = _release_target(
        record.get("current_before"),
        releases,
        label="original current release",
    )
    previous_before = _release_target(
        record.get("previous_before"),
        releases,
        label="original previous release",
    )
    candidate = _release_target(
        record.get("candidate_release"),
        releases,
        label="candidate release",
        must_exist=False,
    )
    if candidate is None:
        raise TransactionError("pre-quiesce candidate release is missing")
    if candidate in {current_before, previous_before}:
        raise TransactionError("pre-quiesce candidate is already active")
    shared_env = shared / ".env.platform"
    shared_env_before = record.get("shared_env_before")
    if shared_env_before is not None:
        if not _valid_identity(shared_env_before):
            raise TransactionError("pre-quiesce shared env identity is invalid")
        env_metadata = _optional_safe_private_file(
            shared_env, label="shared env file"
        )
        if env_metadata is None or _identity(env_metadata) != shared_env_before:
            raise TransactionError("pre-quiesce shared env identity changed")
    else:
        _optional_safe_private_file(shared_env, label="shared env file")
    for path, identity, label in (
        (
            current_before,
            record.get("current_before_identity"),
            "original current release",
        ),
        (
            previous_before,
            record.get("previous_before_identity"),
            "original previous release",
        ),
    ):
        if path is None:
            if identity is not None:
                raise TransactionError(f"{label} identity is unexpected")
        elif not _valid_identity(identity) or not _matches(path, identity):
            raise TransactionError(f"{label} identity changed")

    if version == LEGACY_QUIESCE_STATE_VERSION:
        _validate_legacy_quiesce_snapshot(record)
    else:
        # Reuse the exact install snapshot contract, but do not allow a
        # receipt that has no service state: this file is the recovery
        # authority before the low-level installer has created its transaction
        # record.
        snapshot_record = {
            "operation": "install",
            "service_state_before": record.get("service_state_before"),
            "service_enabled_before": record.get("service_enabled_before"),
            "quiesced_services": record.get("quiesced_services"),
            "timer_active_before": record.get("timer_active_before"),
            "timer_enabled_before": record.get("timer_enabled_before"),
        }
        _validate_service_snapshot(snapshot_record)
        if any(value is None for value in snapshot_record.values()):
            raise TransactionError("pre-quiesce receipt service state is incomplete")

    return {
        **record,
        "app": app,
        "releases": releases,
        "shared": shared,
        "candidate_path": candidate,
        "current_before_path": current_before,
        "previous_before_path": previous_before,
    }


def _load_record(state: Path) -> dict[str, object]:
    _safe_state_file(state)
    try:
        raw = state.read_text(encoding="ascii")
        parsed = json.loads(raw, object_pairs_hook=_strict_object)
    except TransactionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("release operation record is invalid") from exc
    if not isinstance(parsed, dict):
        raise TransactionError("release operation record schema is invalid")
    return _validate_record(state, parsed)


def _load_quiesce_record(state: Path) -> dict[str, object]:
    _safe_state_file(state)
    try:
        raw = state.read_text(encoding="ascii")
        parsed = json.loads(raw, object_pairs_hook=_strict_object)
    except TransactionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("pre-quiesce receipt is invalid") from exc
    if not isinstance(parsed, dict):
        raise TransactionError("pre-quiesce receipt schema is invalid")
    return _validate_quiesce_record(state, parsed)


def _record_for_write(record: dict[str, object]) -> dict[str, object]:
    return {key: record[key] for key in RECORD_KEYS}


def _quiesce_record_for_write(record: dict[str, object]) -> dict[str, object]:
    return {key: record[key] for key in QUIESCE_RECORD_KEYS}


def create_record(
    state: Path,
    *,
    operation: str,
    app_dir: Path,
    current_before: str,
    previous_before: str,
    candidate_release: Path,
    shared_venv: Path,
    peer: Path,
    snapshot: Path,
    transition: str,
    remove_env_on_recovery: bool,
) -> None:
    if os.geteuid() != 0:
        raise TransactionError("release transactions require root")
    shared_metadata = _optional_safe_directory(shared_venv, label="shared venv")
    peer_metadata = _optional_safe_directory(peer, label="venv transaction peer")
    if transition == "exchange" and (shared_metadata is None or peer_metadata is None):
        raise TransactionError("venv exchange inputs are missing")
    if transition == "create" and (
        shared_metadata is not None or peer_metadata is None
    ):
        raise TransactionError("created venv inputs are ambiguous")
    if operation == "install" and current_before and transition in {"exchange", "none"}:
        _fsync_install_rollback_metadata(
            candidate_release,
            current_before,
            transition=transition,
        )
    record: dict[str, object] = {
        "operation_id": uuid4().hex,
        "version": STATE_VERSION,
        "operation": operation,
        "phase": "prepared",
        "app_dir": str(app_dir),
        "current_before": current_before or None,
        "previous_before": previous_before or None,
        "candidate_release": str(candidate_release),
        "shared_venv": str(shared_venv),
        "peer": str(peer),
        "snapshot": str(snapshot),
        "transition": transition,
        "shared_before": _identity(shared_metadata)
        if shared_metadata is not None
        else None,
        "peer_before": (
            _identity(peer_metadata)
            if peer_metadata is not None and transition in {"exchange", "create"}
            else None
        ),
        "current_before_identity": (
            _identity(
                _safe_directory(Path(current_before), label="original current release")
            )
            if current_before
            else None
        ),
        "previous_before_identity": (
            _identity(
                _safe_directory(
                    Path(previous_before), label="original previous release"
                )
            )
            if previous_before
            else None
        ),
        "candidate_identity": _identity(
            _safe_directory(candidate_release, label="candidate release")
        ),
        "remove_env_on_recovery": remove_env_on_recovery,
        "service_state_before": None,
        "service_enabled_before": None,
        "quiesced_services": None,
        "timer_active_before": None,
        "timer_enabled_before": None,
        "systemd_state_before": None,
    }
    validated = _validate_record(state, record)
    _write_record(state, _record_for_write(validated), creating=True)


def set_phase(state: Path, *, expected: str, phase: str) -> None:
    record = _load_record(state)
    if record["phase"] != expected:
        raise TransactionError(
            f"release operation phase mismatch: expected {expected}, got {record['phase']}"
        )
    if phase not in PHASES:
        raise TransactionError("release operation phase is invalid")
    if phase not in PHASE_TRANSITIONS.get(expected, set()):
        raise TransactionError(
            f"release operation phase transition is invalid: {expected} -> {phase}"
        )
    record["phase"] = phase
    _write_record(state, _record_for_write(record), creating=False)


def _parse_service_snapshot(
    service_states: list[str], timer_active_before: str
) -> tuple[dict[str, str], bool]:
    parsed_states: dict[str, str] = {}
    for value in service_states:
        if "=" not in value:
            raise TransactionError("pre-migration service state entry is invalid")
        service, service_state = value.split("=", 1)
        if service in parsed_states or service not in SERVICE_UNITS:
            raise TransactionError("pre-migration service state entry is invalid")
        if service_state not in {"active", "inactive"}:
            raise TransactionError("pre-migration service state entry is invalid")
        parsed_states[service] = service_state
    if set(parsed_states) != set(SERVICE_UNITS):
        raise TransactionError("pre-migration service state is incomplete")
    if timer_active_before not in {"active", "inactive"}:
        raise TransactionError("pre-migration timer state is invalid")
    return parsed_states, timer_active_before == "active"


def _parse_enabled_snapshot(
    service_enabled: list[str], timer_enabled_before: str
) -> tuple[dict[str, str], str]:
    parsed: dict[str, str] = {}
    for value in service_enabled:
        if "=" not in value:
            raise TransactionError("pre-migration enabled service state entry is invalid")
        service, enabled_state = value.split("=", 1)
        if service in parsed or service not in SERVICE_UNITS or enabled_state not in {"enabled", "disabled"}:
            raise TransactionError("pre-migration enabled service state entry is invalid")
        parsed[service] = enabled_state
    if set(parsed) != set(SERVICE_UNITS):
        raise TransactionError("pre-migration enabled service state is incomplete")
    if timer_enabled_before not in {"enabled", "disabled"}:
        raise TransactionError("pre-migration enabled timer state is invalid")
    return parsed, timer_enabled_before


def record_services(
    state: Path,
    *,
    service_states: list[str],
    timer_active_before: str,
    service_enabled: list[str],
    timer_enabled_before: str,
) -> None:
    record = _load_record(state)
    if record["operation"] != "install" or record["phase"] != "staged":
        raise TransactionError(
            "pre-migration service state can only be recorded for a staged install"
        )
    parsed_states, timer_value = _parse_service_snapshot(
        service_states, timer_active_before
    )
    parsed_enabled, timer_enabled_value = _parse_enabled_snapshot(
        service_enabled, timer_enabled_before
    )

    existing_states = record["service_state_before"]
    existing_services = record["quiesced_services"]
    existing_timer = record["timer_active_before"]
    existing_enabled = record["service_enabled_before"]
    existing_timer_enabled = record["timer_enabled_before"]
    if existing_states is not None or existing_enabled is not None or existing_services is not None or existing_timer is not None or existing_timer_enabled is not None:
        if (
            existing_states != parsed_states
            or existing_enabled != parsed_enabled
            or existing_services != list(SERVICE_UNITS)
            or existing_timer != timer_value
            or existing_timer_enabled != timer_enabled_value
        ):
            raise TransactionError("pre-migration service state was already recorded")
        return

    record["service_state_before"] = parsed_states
    record["service_enabled_before"] = parsed_enabled
    record["quiesced_services"] = list(SERVICE_UNITS)
    record["timer_active_before"] = timer_value
    record["timer_enabled_before"] = timer_enabled_value
    _write_record(state, _record_for_write(record), creating=False)


def prepare_quiesce(
    state: Path,
    *,
    app_dir: Path,
    candidate_release: Path,
    service_states: list[str],
    timer_active_before: str,
    service_enabled: list[str],
    timer_enabled_before: str,
    candidate_may_exist: bool,
) -> None:
    """Persist the pre-stop runtime snapshot before staging can mutate files."""

    if os.geteuid() != 0:
        raise TransactionError("release transactions require root")
    _safe_directory(app_dir, label="application directory")
    app_path = app_dir
    releases = app_path / "releases"
    shared = app_path / "shared"
    _safe_directory(releases, label="releases directory")
    _safe_directory(shared, label="shared directory")
    if state not in {shared / QUIESCE_STATE_NAME, shared / STATE_NAME}:
        raise TransactionError("pre-quiesce receipt path is invalid")
    parsed_states, timer_value = _parse_service_snapshot(
        service_states, timer_active_before
    )
    parsed_enabled, timer_enabled_value = _parse_enabled_snapshot(
        service_enabled, timer_enabled_before
    )
    shared_env = app_path / "shared" / ".env.platform"
    shared_env_metadata = _optional_safe_private_file(
        shared_env, label="shared env file"
    )
    candidate = _release_target(
        str(candidate_release),
        releases,
        label="candidate release",
        must_exist=False,
    )
    if candidate is None or (not candidate_may_exist and _lexists(candidate)):
        raise TransactionError("pre-quiesce candidate release already exists")
    current = _read_pointer(app_path, "current")
    previous = _read_pointer(app_path, "previous")
    record: dict[str, object] = {
        "version": QUIESCE_STATE_VERSION,
        "operation": "install",
        "phase": "quiesce-pending",
        "app_dir": str(app_path),
        "current_before": str(current) if current is not None else None,
        "previous_before": str(previous) if previous is not None else None,
        "candidate_release": str(candidate),
        "shared_env_before": (
            _identity(shared_env_metadata) if shared_env_metadata is not None else None
        ),
        "current_before_identity": (
            _identity(_safe_directory(current, label="original current release"))
            if current is not None
            else None
        ),
        "previous_before_identity": (
            _identity(_safe_directory(previous, label="original previous release"))
            if previous is not None
            else None
        ),
        "service_state_before": parsed_states,
        "service_enabled_before": parsed_enabled,
        "quiesced_services": list(SERVICE_UNITS),
        "timer_active_before": timer_value,
        "timer_enabled_before": timer_enabled_value,
    }
    validated = _validate_quiesce_record(state, record)
    _write_record(state, _quiesce_record_for_write(validated), creating=True)


def promote_quiesce(
    state: Path,
    *,
    candidate_release: Path,
    shared_venv: Path,
    peer: Path,
    snapshot: Path,
    transition: str,
    remove_env_on_recovery: bool,
) -> None:
    """Promote the pre-stop receipt into the full installer transaction."""

    pre = _load_quiesce_record(state)
    # Promotion is the boundary at which the installer is allowed to mutate
    # the candidate/venv metadata. Recheck both release pointers while the
    # caller still owns the canonical release lock; a stale pre-quiesce receipt
    # must never be converted into an apparently valid staged transaction.
    verify_quiesce(state)
    app = cast(Path, pre["app"])
    shared = cast(Path, pre["shared"])
    if state != shared / STATE_NAME:
        raise TransactionError("pre-quiesce promotion requires the operation path")
    candidate_path = cast(Path, pre["candidate_path"])
    if candidate_release != candidate_path:
        raise TransactionError("staged candidate does not match pre-quiesce receipt")
    candidate_metadata = _safe_directory(candidate_release, label="candidate release")
    shared_metadata = _optional_safe_directory(shared_venv, label="shared venv")
    peer_metadata = _optional_safe_directory(peer, label="venv transaction peer")
    if transition == "exchange" and (shared_metadata is None or peer_metadata is None):
        raise TransactionError("venv exchange inputs are missing")
    if transition == "create" and (
        shared_metadata is not None or peer_metadata is None
    ):
        raise TransactionError("created venv inputs are ambiguous")
    current_before = cast(Path | None, pre["current_before_path"])
    if current_before is not None and transition in {"exchange", "none"}:
        _fsync_install_rollback_metadata(
            candidate_release,
            str(current_before),
            transition=transition,
        )
    record: dict[str, object] = {
        "operation_id": uuid4().hex,
        "version": STATE_VERSION,
        "operation": "install",
        "phase": "prepared",
        "app_dir": str(app),
        "current_before": str(current_before) if current_before is not None else None,
        "previous_before": (
            str(pre["previous_before_path"])
            if pre["previous_before_path"] is not None
            else None
        ),
        "candidate_release": str(candidate_release),
        "shared_venv": str(shared_venv),
        "peer": str(peer),
        "snapshot": str(snapshot),
        "transition": transition,
        "shared_before": (
            _identity(shared_metadata) if shared_metadata is not None else None
        ),
        "peer_before": (
            _identity(peer_metadata)
            if peer_metadata is not None and transition in {"exchange", "create"}
            else None
        ),
        "current_before_identity": pre["current_before_identity"],
        "previous_before_identity": pre["previous_before_identity"],
        "candidate_identity": _identity(candidate_metadata),
        "remove_env_on_recovery": remove_env_on_recovery,
        "service_state_before": pre["service_state_before"],
        "service_enabled_before": pre["service_enabled_before"],
        "quiesced_services": pre["quiesced_services"],
        "timer_active_before": pre["timer_active_before"],
        "timer_enabled_before": pre["timer_enabled_before"],
        "systemd_state_before": None,
    }
    validated = _validate_record(state, record)
    _write_record(state, _record_for_write(validated), creating=False)


def verify_quiesce(state: Path) -> None:
    record = _load_quiesce_record(state)
    app = cast(Path, record["app"])
    if (
        _read_pointer(app, "current") != record["current_before_path"]
        or _read_pointer(app, "previous") != record["previous_before_path"]
    ):
        raise TransactionError("pre-quiesce release pointers changed")


def validate_quiesce_noop(state: Path) -> None:
    """Validate the only systemd-free operationless first-install receipt.

    A pre-promotion receipt can exist before the installer has created its
    operation identity.  When neither pointer existed, the deploy wrapper
    writes a complete all-inactive/disabled compatibility snapshot; recovery
    must validate that exact shape and then clean up without querying or
    mutating systemd.  Active, enabled, partial, or hybrid snapshots are not
    a no-op and fail closed before cleanup.
    """

    record = _load_quiesce_record(state)
    if record["current_before"] is not None or record["previous_before"] is not None:
        raise TransactionError("pre-quiesce receipt is not first-install topology")
    service_state = cast(dict[str, str], record["service_state_before"])
    if any(value != "inactive" for value in service_state.values()):
        raise TransactionError("first-install quiesce snapshot is active")
    if record["timer_active_before"] is not False:
        raise TransactionError("first-install quiesce timer snapshot is active")
    if record["version"] == QUIESCE_STATE_VERSION:
        service_enabled = cast(dict[str, str], record["service_enabled_before"])
        if any(value != "disabled" for value in service_enabled.values()):
            raise TransactionError("first-install quiesce service is enabled")
        if record["timer_enabled_before"] != "disabled":
            raise TransactionError("first-install quiesce timer is enabled")


def clear_quiesce(state: Path) -> None:
    _load_quiesce_record(state)
    state.unlink()
    _fsync_directory(state.parent)


def validate_service_snapshot(state: Path, *, require: str) -> None:
    record = _load_record(state)
    service_state = record.get("service_state_before")
    service_enabled = record.get("service_enabled_before")
    quiesced_services = record.get("quiesced_services")
    timer_active_before = record.get("timer_active_before")
    timer_enabled = record.get("timer_enabled_before")
    present = (
        service_state is not None
        or service_enabled is not None
        or quiesced_services is not None
        or timer_active_before is not None
        or timer_enabled is not None
    )
    if require == "present":
        if not present:
            raise TransactionError("pre-migration service state is missing")
        _validate_service_snapshot(record)
    elif require == "absent":
        if present:
            raise TransactionError("pre-migration service state is unexpected")
    elif require == "optional":
        if present:
            _validate_service_snapshot(record)
            service_state = cast(dict[str, str], record["service_state_before"])
            service_enabled = cast(dict[str, str], record["service_enabled_before"])
            if (
                any(value != "inactive" for value in service_state.values())
                or record["timer_active_before"] is not False
                or any(value != "disabled" for value in service_enabled.values())
                or record["timer_enabled_before"] != "disabled"
            ):
                raise TransactionError(
                    "first-install service snapshot must be fully inactive and disabled"
                )
    else:
        raise TransactionError("service snapshot requirement is invalid")


def abort_quiesce(state: Path) -> None:
    record = _load_quiesce_record(state)
    verify_quiesce(state)
    candidate = cast(Path, record["candidate_path"])
    _validate_quiesce_candidate_for_abort(candidate)
    if _lexists(candidate):
        # Only an empty directory is authorized above.  Keep the final
        # removal non-recursive so a concurrent replacement/population turns
        # into a retained receipt rather than a recursive delete.
        try:
            candidate.rmdir()
        except OSError as exc:
            raise TransactionError("pre-quiesce candidate release changed") from exc
        _fsync_directory(candidate.parent)
    shared = cast(Path, record["shared"])
    if record["shared_env_before"] is None:
        shared_env = shared / ".env.platform"
        if _lexists(shared_env):
            _optional_safe_private_file(shared_env, label="shared env file")
            shared_env.unlink()
            _fsync_directory(shared)
    candidate_name = candidate.name
    for pattern in (
        f".venv-install-{candidate_name}.*",
        f".freeze-check-{candidate_name}.*",
    ):
        for path in sorted(shared.glob(pattern)):
            if path.is_symlink():
                raise TransactionError("pre-quiesce temporary path is a symlink")
            if path.is_dir():
                # The operation-less pre-quiesce receipt carries no inode or
                # content identity for a staging directory.  Never turn a
                # filename pattern into recursive deletion authority; retain
                # the receipt for an operator if a directory is present.
                raise TransactionError("pre-quiesce temporary directory is unbound")
            else:
                _optional_safe_private_file(path, label="pre-quiesce temporary file")
                path.unlink()
                _fsync_directory(shared)
    state.unlink()
    _fsync_directory(state.parent)


def _validate_quiesce_candidate_for_abort(candidate: Path) -> None:
    """Allow only an empty, receipt-owned candidate directory to be removed.

    The pre-promotion receipt predates candidate extraction and therefore has
    no candidate inode/content identity.  A populated path is consequently
    unbound data, not cleanup authority; retain it for operator review rather
    than recursively deleting it.  The same check is used before restoring
    services so every abort caller fails closed before its first systemd
    operation when the candidate has been replaced or is unsafe.
    """

    if not _lexists(candidate):
        return
    metadata = _safe_directory(candidate, label="pre-quiesce candidate release")
    releases = candidate.parent
    releases_metadata = _safe_directory(releases, label="releases directory")
    if metadata.st_dev != releases_metadata.st_dev:
        raise TransactionError("pre-quiesce candidate release is on another device")
    try:
        with os.scandir(candidate) as entries:
            if next(entries, None) is not None:
                raise TransactionError("pre-quiesce candidate release is occupied")
    except OSError as exc:
        raise TransactionError("pre-quiesce candidate release is unavailable") from exc


def _systemctl_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\n" in value
        or "\x00" in value
    ):
        raise TransactionError("systemctl path is invalid")
    path = Path(value)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise TransactionError("systemctl path is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o111
        or path.resolve(strict=True) != path
    ):
        raise TransactionError("systemctl path metadata is unsafe")
    return value


def _systemd_deadline_timeout(deadline: float | None) -> float:
    if deadline is None:
        return SYSTEMD_CALL_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TransactionError("systemd operation deadline exceeded")
    return min(SYSTEMD_CALL_TIMEOUT_SECONDS, remaining)


def _run_systemctl(
    systemctl: str, *arguments: str, deadline: float | None = None
) -> str:
    try:
        result = subprocess.run(
            [systemctl, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=_systemd_deadline_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransactionError("systemctl operation failed") from exc
    output = result.stdout.strip()
    if "\n" in output or "\r" in output:
        raise TransactionError("systemctl operation returned multiple states")
    if result.returncode != 0:
        raise TransactionError("systemctl operation failed")
    return output


def _read_systemctl_state(
    systemctl: str, unit: str, *, deadline: float | None = None
) -> str:
    try:
        result = subprocess.run(
            [systemctl, "is-active", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=_systemd_deadline_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransactionError("systemctl state query failed") from exc
    output = result.stdout.strip()
    if output not in {"active", "inactive"}:
        raise TransactionError("systemctl state query is invalid")
    if (output == "active" and result.returncode != 0) or (
        output == "inactive" and result.returncode != 3
    ):
        raise TransactionError("systemctl active state/status mismatch")
    return output


def _read_systemctl_enabled(
    systemctl: str, unit: str, *, deadline: float | None = None
) -> str:
    try:
        result = subprocess.run(
            [systemctl, "is-enabled", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=_systemd_deadline_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransactionError("systemctl enabled state query failed") from exc
    output = result.stdout.strip()
    if output not in {"enabled", "disabled"}:
        raise TransactionError("systemctl enabled state query is invalid")
    if (output == "enabled" and result.returncode != 0) or (
        output == "disabled" and result.returncode != 1
    ):
        raise TransactionError("systemctl enabled state/status mismatch")
    return output


def _initial_systemd_snapshot(
    systemctl: str, *, deadline: float | None = None
) -> dict[str, dict[str, str]]:
    return {
        unit: {
            "active": _read_systemctl_state(systemctl, unit, deadline=deadline),
            "enabled": _read_systemctl_enabled(systemctl, unit, deadline=deadline),
        }
        for unit in INITIAL_SYSTEMD_UNITS
    }


def _require_initial_systemd_record(
    record: dict[str, object],
    *,
    allowed_phases: set[str],
    require_snapshot: bool = True,
) -> dict[str, dict[str, str]]:
    if (
        record["operation"] != "install"
        or record["current_before_path"] is not None
        or record["previous_before_path"] is not None
        or record["phase"] not in allowed_phases
    ):
        raise TransactionError("transaction is not a clean first-install systemd operation")
    _validate_initial_systemd_snapshot(record)
    snapshot = record.get("systemd_state_before")
    if snapshot is None and not require_snapshot:
        return {}
    if not isinstance(snapshot, dict):
        raise TransactionError("clean first-install systemd snapshot is missing")
    return cast(dict[str, dict[str, str]], snapshot)


def _verify_initial_systemd_snapshot(
    systemctl: str,
    snapshot: dict[str, dict[str, str]],
    *,
    deadline: float | None = None,
) -> None:
    if deadline is None:
        deadline = time.monotonic() + SYSTEMD_OPERATION_TIMEOUT_SECONDS
    actual = _initial_systemd_snapshot(systemctl, deadline=deadline)
    if actual != snapshot:
        raise TransactionError("clean first-install systemd baseline changed")


def capture_initial_systemd(state: Path, *, systemctl: str) -> None:
    """Persist the inactive/disabled clean-install baseline atomically."""

    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    snapshot = _require_initial_systemd_record(
        record, allowed_phases={"staged"}, require_snapshot=False
    )
    deadline = time.monotonic() + SYSTEMD_OPERATION_TIMEOUT_SECONDS
    if record.get("systemd_state_before") is not None:
        _verify_initial_systemd_snapshot(systemctl, snapshot, deadline=deadline)
        return
    captured = _initial_systemd_snapshot(systemctl, deadline=deadline)
    probe = {**record, "systemd_state_before": captured}
    _validate_initial_systemd_snapshot(probe)
    record["systemd_state_before"] = captured
    _write_record(state, _record_for_write(record), creating=False)
    _verify_initial_systemd_snapshot(systemctl, captured, deadline=deadline)


def restore_initial_systemd(state: Path, *, systemctl: str) -> None:
    """Return clean-install units to their receipt-bound baseline.

    Each operation is idempotent. A failed stop/disable or verification leaves
    the transaction and candidate untouched so a later retry can finish the
    restore before filesystem recovery is attempted.
    """

    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    snapshot = _require_initial_systemd_record(
        record,
        allowed_phases={
            *INITIAL_SYSTEMD_PHASES,
        },
    )
    _verify_recovery_pointers(record)
    deadline = time.monotonic() + SYSTEMD_OPERATION_TIMEOUT_SECONDS
    for unit in INITIAL_SYSTEMD_UNITS:
        _run_systemctl(systemctl, "stop", unit, deadline=deadline)
        _run_systemctl(systemctl, "disable", unit, deadline=deadline)
    _verify_initial_systemd_snapshot(systemctl, snapshot, deadline=deadline)


def verify_initial_systemd(state: Path, *, systemctl: str) -> None:
    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    snapshot = _require_initial_systemd_record(
        record,
        allowed_phases={
            *INITIAL_SYSTEMD_PHASES,
        },
    )
    _verify_recovery_pointers(record)
    _verify_initial_systemd_snapshot(systemctl, snapshot)


def validate_initial_systemd(state: Path) -> None:
    record = _load_record(state)
    _require_initial_systemd_record(
        record,
        allowed_phases={
            *INITIAL_SYSTEMD_PHASES,
        },
    )
    _verify_recovery_pointers(record)


def verify_initial_systemd_activated(state: Path, *, systemctl: str) -> None:
    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    _require_initial_systemd_record(
        record,
        allowed_phases={"systemd-activated", "activation-committed"},
    )
    _verify_recovery_pointers(record)
    deadline = time.monotonic() + SYSTEMD_OPERATION_TIMEOUT_SECONDS
    actual = _initial_systemd_snapshot(systemctl, deadline=deadline)
    if any(
        state.get("active") != "active" or state.get("enabled") != "enabled"
        for state in actual.values()
    ):
        raise TransactionError("clean first-install systemd activation is incomplete")


def restore_quiesce(state: Path, *, systemctl: str) -> None:
    """Restore the exact pre-quiesce service snapshot before abort cleanup."""

    systemctl = _systemctl_path(systemctl)
    record = _load_quiesce_record(state)
    verify_quiesce(state)
    # The operation-less receipt is written before staging.  It carries the
    # candidate pathname, but deliberately no candidate inode/content
    # identity, so a pre-promotion recovery may only proceed while that path
    # is absent or is an empty, canonical root-owned release directory.
    # Treating populated data as recoverable would turn an unbound directory
    # into cleanup authority.
    candidate = cast(Path, record["candidate_path"])
    _validate_quiesce_candidate_for_abort(candidate)
    service_state = cast(dict[str, str], record["service_state_before"])
    version = cast(int, record["version"])
    if version == QUIESCE_STATE_VERSION:
        service_enabled = cast(dict[str, str], record["service_enabled_before"])
        for unit in SERVICE_UNITS:
            enabled_expected = service_enabled[unit]
            _run_systemctl(systemctl, "enable" if enabled_expected == "enabled" else "disable", unit)
            if _read_systemctl_enabled(systemctl, unit) != enabled_expected:
                raise TransactionError("quiesced service enabled state was not restored")
            expected = service_state[unit]
            _run_systemctl(systemctl, "restart" if expected == "active" else "stop", unit)
            if _read_systemctl_state(systemctl, unit) != expected:
                raise TransactionError("quiesced service state was not restored")
    else:
        # Legacy version-1 receipts intentionally lack enabled-state fields.
        # Restore only their exact active/inactive snapshot; never infer or
        # mutate enablement from an absent field.
        for unit in SERVICE_UNITS:
            expected = service_state[unit]
            _run_systemctl(systemctl, "restart" if expected == "active" else "stop", unit)
            if _read_systemctl_state(systemctl, unit) != expected:
                raise TransactionError("quiesced service state was not restored")
    timer_expected = "active" if record["timer_active_before"] else "inactive"
    if version == QUIESCE_STATE_VERSION:
        timer_enabled_expected = cast(str, record["timer_enabled_before"])
        _run_systemctl(
            systemctl,
            "enable" if timer_enabled_expected == "enabled" else "disable",
            "deadlock-cloudflare-ips.timer",
        )
        if _read_systemctl_enabled(systemctl, "deadlock-cloudflare-ips.timer") != timer_enabled_expected:
            raise TransactionError("quiesced timer enabled state was not restored")
    _run_systemctl(
        systemctl,
        "start" if timer_expected == "active" else "stop",
        "deadlock-cloudflare-ips.timer",
    )
    if _read_systemctl_state(systemctl, "deadlock-cloudflare-ips.timer") != timer_expected:
        raise TransactionError("quiesced timer state was not restored")


def authorize_recovery(state: Path, *, confirmation: str) -> None:
    if confirmation != RECOVERY_CONFIRMATION:
        raise TransactionError(
            "explicit recovery confirmation must be MIGRATION_NOT_REVERSED"
        )
    record = _load_record(state)
    if record["operation"] != "install":
        raise TransactionError("explicit migration recovery applies only to installs")
    phase = cast(str, record["phase"])
    if phase not in MIGRATION_OUTCOME_UNCERTAIN_PHASES:
        raise TransactionError(
            f"release operation phase does not require migration recovery authorization: {phase}"
        )
    record["phase"] = "recovery-authorized"
    _write_record(state, _record_for_write(record), creating=False)


def _rename_exchange(first: Path, second: Path) -> None:
    if first.stat().st_dev != second.stat().st_dev:
        raise TransactionError("release venv exchange crosses filesystems")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise TransactionError("renameat2 is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            AT_FDCWD,
            os.fsencode(first),
            AT_FDCWD,
            os.fsencode(second),
            RENAME_EXCHANGE,
        )
        != 0
    ):
        error = ctypes.get_errno()
        raise TransactionError(
            f"atomic venv exchange failed: {os.strerror(error)} "
            f"({errno.errorcode.get(error, error)})"
        )
    _fsync_directory(first.parent)
    if second.parent != first.parent:
        _fsync_directory(second.parent)


def exchange_recorded_venvs(state: Path) -> None:
    record = _load_record(state)
    if record["transition"] != "exchange" or record["phase"] != "prepared":
        raise TransactionError("release operation is not prepared for a venv exchange")
    shared = cast(Path, record["shared_venv_path"])
    peer = cast(Path, record["peer_path"])
    shared_before = cast(dict[str, int], record["shared_before"])
    peer_before = cast(dict[str, int], record["peer_before"])
    if not _matches(shared, shared_before) or not _matches(peer, peer_before):
        raise TransactionError("release venv inputs changed before exchange")
    _rename_exchange(shared, peer)


def rename_recorded_venv(state: Path, *, mode: str) -> None:
    record = _load_record(state)
    transition = record["transition"]
    shared = cast(Path, record["shared_venv_path"])
    peer = cast(Path, record["peer_path"])
    snapshot = cast(Path, record["snapshot_path"])
    peer_before = cast(dict[str, int], record["peer_before"])
    if mode == "activate-created":
        if transition != "create" or record["phase"] != "prepared":
            raise TransactionError(
                "release operation is not prepared for venv activation"
            )
        if _lexists(shared) or not _matches(peer, peer_before):
            raise TransactionError("created venv inputs changed before activation")
        os.rename(peer, shared)
        _fsync_directory(shared.parent)
        return
    if mode == "place-snapshot":
        if (
            record["operation"] != "install"
            or transition != "exchange"
            or record["phase"] != "venv-transitioned"
        ):
            raise TransactionError(
                "release operation is not ready to place its snapshot"
            )
        shared_before = cast(dict[str, int], record["shared_before"])
        if (
            not _matches(shared, peer_before)
            or not _matches(peer, shared_before)
            or _lexists(snapshot)
        ):
            raise TransactionError("release snapshot inputs are ambiguous")
        if peer.stat().st_dev != snapshot.parent.stat().st_dev:
            raise TransactionError("release snapshot crosses filesystems")
        os.rename(peer, snapshot)
        _fsync_directory(peer.parent)
        _fsync_directory(snapshot.parent)
        return
    raise TransactionError("release venv rename mode is invalid")


def _read_pointer(app: Path, name: str) -> Path | None:
    pointer = app / name
    if not _lexists(pointer):
        return None
    try:
        metadata = pointer.lstat()
        target = pointer.resolve(strict=True)
    except OSError as exc:
        raise TransactionError(f"release pointer is unavailable: {name}") from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise TransactionError(f"release pointer is not a symlink: {name}")
    return target


def _set_pointer(app: Path, name: str, target: Path | None) -> None:
    pointer = app / name
    if _lexists(pointer) and not pointer.is_symlink():
        raise TransactionError(f"release pointer is not replaceable: {name}")
    if target is None:
        if _lexists(pointer):
            pointer.unlink()
            _fsync_directory(app)
        return
    temporary = app / f".release-link-{name}-{uuid4().hex}"
    os.symlink(target, temporary)
    try:
        os.replace(temporary, pointer)
        _fsync_directory(app)
    finally:
        if _lexists(temporary):
            temporary.unlink()


def switch_pointer(state: Path, *, name: str, target_value: str) -> None:
    if name not in {"current", "previous"}:
        raise TransactionError("release pointer name is invalid")
    record = _load_record(state)
    app = cast(Path, record["app"])
    releases = cast(Path, record["releases"])
    target = (
        None
        if not target_value
        else _release_target(target_value, releases, label=f"new {name} release")
    )
    allowed = {
        record["current_before_path"],
        record["previous_before_path"],
        record["candidate_path"],
        None,
    }
    if target not in allowed:
        raise TransactionError("release pointer target is outside the transaction")
    _set_pointer(app, name, target)


def _unique_paths(*paths: Path) -> list[Path]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return result


def _identity_locations(
    paths: list[Path], expected: tuple[dict[str, int], ...]
) -> dict[tuple[int, int], Path]:
    locations: dict[tuple[int, int], Path] = {}
    expected_keys = {(item["dev"], item["ino"]) for item in expected}
    for path in paths:
        metadata = _optional_safe_directory(path, label=f"transaction path {path.name}")
        if metadata is None:
            continue
        key = (metadata.st_dev, metadata.st_ino)
        if key not in expected_keys or key in locations:
            raise TransactionError("release venv transaction state is ambiguous")
        locations[key] = path
    return locations


def _restore_pointers(record: dict[str, object]) -> None:
    app = cast(Path, record["app"])
    current = cast(Path | None, record["current_before_path"])
    previous = cast(Path | None, record["previous_before_path"])
    _set_pointer(app, "current", current)
    _set_pointer(app, "previous", previous)


def _verify_original_pointers(record: dict[str, object]) -> None:
    app = cast(Path, record["app"])
    if _read_pointer(app, "current") != record["current_before_path"]:
        raise TransactionError("current release pointer was not restored")
    if _read_pointer(app, "previous") != record["previous_before_path"]:
        raise TransactionError("previous release pointer was not restored")


def _verify_recovery_pointers(record: dict[str, object]) -> None:
    """Reject an unrelated or phase-impossible pointer pair before recovery."""

    app = cast(Path, record["app"])
    actual = (_read_pointer(app, "current"), _read_pointer(app, "previous"))
    original = (record["current_before_path"], record["previous_before_path"])
    candidate = cast(Path, record["candidate_path"])
    release_paths = [
        path for path in (
            cast(Path | None, record["current_before_path"]),
            cast(Path | None, record["previous_before_path"]),
            candidate,
        ) if path is not None and _lexists(path)
    ]
    devices = {_safe_directory(path, label=f"recovery release {path.name}").st_dev for path in release_paths}
    if len(devices) > 1:
        raise TransactionError("release recovery crosses filesystems")
    phase = cast(str, record["phase"])
    if record["operation"] == "install":
        current_before = cast(Path | None, record["current_before_path"])
        previous_before = cast(Path | None, record["previous_before_path"])
        desired_previous = current_before if current_before is not None else previous_before
        # The installer moves the old current pointer to ``previous`` before
        # switching ``current`` to the candidate.  During that narrow
        # previous-switched window the live pair is therefore
        # (current_before, current_before), not (candidate, ...).  Once the
        # current pointer has moved, every post-current phase is
        # (candidate, current_before).  Keeping these pairs phase-aware is
        # what lets a crash between the two pointer renames be recovered
        # without accepting an unrelated topology.
        phase_pairs = {
            "current-switched": ((candidate, desired_previous),),
            # The durable marker is written after the previous symlink and
            # before the current symlink.  A kill in that tiny window leaves
            # (current_before, current_before); a kill after the second
            # symlink but before the marker update leaves the post-promotion
            # pair.  Both are exact transaction-owned states, so permit both
            # and reject every other topology.
            "previous-switched": tuple(
                pair
                for pair in (
                    (current_before, current_before),
                    (candidate, desired_previous),
                )
                if pair[0] is not None or pair[1] is not None
            ),
            "pointers-switched": ((candidate, desired_previous),),
            "restart-pending": ((candidate, desired_previous),),
            "rollback-runtime-pending": ((candidate, desired_previous),),
            "rollback-runtime-applied": ((candidate, desired_previous),),
            "recovery-authorized": ((candidate, desired_previous),),
            "staged": ((candidate, desired_previous),),
            "migration-pending": ((candidate, desired_previous),),
            "migration-failed": ((candidate, desired_previous),),
            "migration-applied": ((candidate, desired_previous),),
            "activation-pending": ((candidate, desired_previous),),
            "services-restarted": ((candidate, desired_previous),),
            "nginx-pending": ((candidate, desired_previous),),
            "nginx-applied": ((candidate, desired_previous),),
            "smoke-passed": ((candidate, desired_previous),),
            "systemd-activation-pending": ((candidate, desired_previous),),
            "systemd-activated": ((candidate, desired_previous),),
            "activation-committed": ((candidate, desired_previous),),
            "recovery-restored": ((None, None),),
        }
        allowed = (original, *phase_pairs.get(phase, ()))
    else:
        rollback_current = cast(Path | None, record["previous_before_path"])
        rollback_previous = cast(Path | None, record["current_before_path"])
        phase_pairs = {
            "current-switched": ((rollback_current, rollback_current),),
            "previous-switched": ((rollback_current, rollback_previous),),
            "pointers-switched": ((rollback_current, rollback_previous),),
            "restart-pending": ((rollback_current, rollback_previous),),
            "rollback-runtime-pending": ((rollback_current, rollback_previous),),
            "rollback-runtime-applied": ((rollback_current, rollback_previous),),
            "services-restarted": ((rollback_current, rollback_previous),),
            "smoke-passed": ((rollback_current, rollback_previous),),
        }
        allowed = (original, *phase_pairs.get(phase, ()))
    if actual not in allowed:
        raise TransactionError("release pointers do not match this transaction")


def _restore_venv(record: dict[str, object]) -> None:
    transition = record["transition"]
    shared = cast(Path, record["shared_venv_path"])
    peer = cast(Path, record["peer_path"])
    snapshot = cast(Path, record["snapshot_path"])
    if transition == "none":
        shared_before = record["shared_before"]
        if shared_before is None:
            if _lexists(shared):
                raise TransactionError(
                    "shared venv appeared during a no-op transaction"
                )
        else:
            shared_before = cast(dict[str, int], shared_before)
            if not _matches(shared, shared_before):
                raise TransactionError("shared venv changed during a no-op transaction")
        return
    peer_before = cast(dict[str, int], record["peer_before"])
    if transition == "create":
        locations = _identity_locations(_unique_paths(shared, peer), (peer_before,))
        new_location = locations.get((peer_before["dev"], peer_before["ino"]))
        if new_location is None:
            if record["phase"] != "recovery-restored":
                raise TransactionError("created venv identity is missing")
            return
        if new_location == shared:
            if _lexists(peer):
                raise TransactionError("created venv recovery peer is occupied")
            os.rename(shared, peer)
            _fsync_directory(shared.parent)
        if _lexists(shared):
            raise TransactionError("shared venv was not restored to absence")
        return

    shared_before = cast(dict[str, int], record["shared_before"])
    paths = _unique_paths(shared, peer, snapshot)
    locations = _identity_locations(paths, (shared_before, peer_before))
    old_key = (shared_before["dev"], shared_before["ino"])
    new_key = (peer_before["dev"], peer_before["ino"])
    old_location = locations.get(old_key)
    new_location = locations.get(new_key)
    if old_location is None:
        raise TransactionError("original shared venv identity is missing")
    if old_location != shared:
        if new_location != shared:
            raise TransactionError("shared venv exchange state is ambiguous")
        _rename_exchange(shared, old_location)
    if not _matches(shared, shared_before):
        raise TransactionError("original shared venv was not restored")
    if record["operation"] == "rollback":
        if not _matches(peer, peer_before):
            raise TransactionError("rollback snapshot was not restored")


def _remove_tree(
    path: Path,
    *,
    expected_identity: dict[str, int] | None = None,
    allowed_symlink_roots: tuple[Path, ...] = (),
) -> None:
    metadata = _safe_directory(path, label=f"cleanup path {path.name}")
    if expected_identity is not None and _identity(metadata) != expected_identity:
        raise TransactionError("release cleanup identity changed")

    # Validate the complete tree before changing permissions or deleting
    # anything.  A receipt binds only the root inode; every descendant must
    # still be ordinary root-owned data on the same filesystem.  Symlinks,
    # hardlinks, special files and group/world-writable content are retained
    # with the receipt instead of becoming recursive cleanup authority.
    device = metadata.st_dev
    pending = [path]
    while pending:
        root = pending.pop()
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            raise TransactionError("release cleanup tree cannot be inspected") from exc
        for entry in entries:
            try:
                child_metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise TransactionError("release cleanup entry cannot be inspected") from exc
            mode = child_metadata.st_mode
            child = Path(entry.path)
            is_symlink = stat.S_ISLNK(mode)
            symlink_root_allowed = any(
                child == allowed_root or allowed_root in child.parents
                for allowed_root in allowed_symlink_roots
            )
            if (
                child_metadata.st_dev != device
                or child_metadata.st_uid != 0
                or child_metadata.st_gid != 0
                or (
                    not is_symlink
                    and stat.S_IMODE(mode) & 0o022
                )
                or (
                    is_symlink
                    and (
                        not symlink_root_allowed
                        or child_metadata.st_nlink != 1
                    )
                )
            ):
                raise TransactionError("release cleanup entry metadata is unsafe")
            if is_symlink:
                # A staged Python virtualenv normally contains interpreter
                # symlinks (bin/python*, lib64).  They are safe to unlink
                # after the exact receipt-bound venv root has been moved into
                # quarantine: shutil.rmtree never follows symlinks.  Keep the
                # default strict for release trees and arbitrary cleanup
                # callers; only exact receipt-bound venv roots opt into this
                # narrow compatibility path.
                continue
            if stat.S_ISDIR(mode):
                if child_metadata.st_nlink < 2:
                    raise TransactionError("release cleanup directory identity is invalid")
                pending.append(child)
            elif stat.S_ISREG(mode):
                if child_metadata.st_nlink != 1:
                    raise TransactionError("release cleanup hardlink is unsafe")
            else:
                raise TransactionError("release cleanup entry type is unsafe")
    # Move the fully checked tree into a private, same-parent quarantine before
    # deleting it.  The rename closes the pathname replacement window between
    # validation and recursive deletion: cleanup is now rooted at the inode
    # that was rechecked immediately before the atomic rename.  The parent is
    # root-owned and non-writable to group/other, so the generated name cannot
    # be planted by an untrusted process.
    parent = path.parent
    _safe_directory(parent, label="release cleanup parent")
    quarantine = parent / f".{path.name}.cleanup-{uuid4().hex}"
    if _lexists(quarantine):
        raise TransactionError("release cleanup quarantine already exists")
    latest = _safe_directory(path, label=f"cleanup path {path.name}")
    if expected_identity is not None and _identity(latest) != expected_identity:
        raise TransactionError("release cleanup identity changed")
    if latest.st_dev != metadata.st_dev or latest.st_ino != metadata.st_ino:
        raise TransactionError("release cleanup path changed during validation")
    try:
        os.rename(path, quarantine)
    except OSError as exc:
        raise TransactionError("release cleanup quarantine could not be created") from exc
    try:
        quarantined = _safe_directory(quarantine, label="release cleanup quarantine")
        if _identity(quarantined) != _identity(metadata):
            raise TransactionError("release cleanup quarantine identity changed")
        if expected_identity is not None and _identity(quarantined) != expected_identity:
            raise TransactionError("release cleanup quarantine identity changed")
        if quarantined.st_dev != device:
            raise TransactionError("release cleanup quarantine device changed")
        for root, directories, _files in os.walk(
            quarantine, topdown=False, followlinks=False
        ):
            for directory in directories:
                child = Path(root) / directory
                child_mode = child.lstat().st_mode
                if stat.S_ISLNK(child_mode):
                    # Symlinks are receipt-bound unlink-only entries.  chmod
                    # follows a directory symlink on this platform, so never
                    # apply the directory permission repair to one. Remove it
                    # explicitly because shutil.rmtree refuses symlink roots.
                    child.unlink()
                    continue
                os.chmod(child, stat.S_IMODE(child_mode) | 0o700)
            for filename in _files:
                child = Path(root) / filename
                if stat.S_ISLNK(child.lstat().st_mode):
                    # Allowed receipt-bound file links are unlink-only too;
                    # never follow or chmod them.
                    child.unlink()
            root_path = Path(root)
            if not stat.S_ISLNK(root_path.lstat().st_mode):
                os.chmod(root_path, stat.S_IMODE(root_path.lstat().st_mode) | 0o700)
        shutil.rmtree(quarantine)
    except Exception as exc:
        # Preserve the receipt and the quarantined tree on every validation or
        # deletion failure. Restore the original pathname only when it is still
        # absent; never overwrite a replacement supplied by another actor.
        if _lexists(quarantine) and not _lexists(path):
            try:
                os.rename(quarantine, path)
            except OSError:
                pass
        if isinstance(exc, TransactionError):
            raise
        raise TransactionError("release cleanup deletion failed") from exc
    _fsync_directory(path.parent)


def _mark_recovery_restored(
    state: Path, record: dict[str, object]
) -> dict[str, object]:
    record["phase"] = "recovery-restored"
    _write_record(state, _record_for_write(record), creating=False)
    return _load_record(state)


def _cleanup_recovered_install(
    state: Path, record: dict[str, object], *, remove_receipt: bool = True
) -> None:
    shared = cast(Path, record["shared"])
    peer = cast(Path, record["peer_path"])
    candidate = cast(Path, record["candidate_path"])
    peer_before = record["peer_before"]
    if _lexists(peer):
        if not isinstance(peer_before, dict):
            raise TransactionError("install cleanup peer identity is invalid")
        _remove_tree(
            peer,
            expected_identity=peer_before,
            allowed_symlink_roots=(peer,),
        )
    if _lexists(candidate):
        if (
            _read_pointer(record["app"], "current") == candidate
            or _read_pointer(record["app"], "previous") == candidate
        ):
            raise TransactionError("candidate release is still active during cleanup")
        candidate_identity = cast(dict[str, int], record["candidate_identity"])
        _remove_tree(
            candidate,
            expected_identity=candidate_identity,
            allowed_symlink_roots=(candidate / ".rollback/shared-venv-before-install",),
        )
    if record["remove_env_on_recovery"]:
        env_file = shared / ".env.platform"
        if _lexists(env_file):
            metadata = env_file.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_nlink != 1
            ):
                raise TransactionError("created shared env file is unsafe to remove")
            env_file.unlink()
            _fsync_directory(shared)
    # The installer creates this private check file before the venv
    # transaction is published. A SIGKILL after promotion bypasses the
    # installer's EXIT cleanup, so consume only the exact candidate-scoped
    # regular files here; an unexpected symlink or directory keeps the receipt
    # retained for operator recovery.
    for path in sorted(shared.glob(f".freeze-check-{candidate.name}.*")):
        if path.is_symlink() or path.is_dir():
            raise TransactionError("install freeze-check temporary path is unsafe")
        _optional_safe_private_file(path, label="install freeze-check temporary file")
        path.unlink()
        _fsync_directory(shared)
    if remove_receipt and _lexists(state):
        state.unlink()
        _fsync_directory(state.parent)


def restore_services(state: Path, *, systemctl: str) -> None:
    """Restore a current-only install's exact pre-quiesce service snapshot."""

    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    if (
        record["operation"] != "install"
        or record["phase"] != "filesystem-restored-services-pending"
        or record["current_before_path"] is None
        or record["previous_before_path"] is not None
    ):
        raise TransactionError("transaction is not ready for service restoration")
    validate_service_snapshot(state, require="present")
    _verify_original_pointers(record)
    _restore_venv(record)
    service_state = cast(dict[str, str], record["service_state_before"])
    service_enabled = cast(dict[str, str], record["service_enabled_before"])
    for unit in SERVICE_UNITS:
        enabled_expected = service_enabled[unit]
        _run_systemctl(systemctl, "enable" if enabled_expected == "enabled" else "disable", unit)
        if _read_systemctl_enabled(systemctl, unit) != enabled_expected:
            raise TransactionError("pre-migration service enabled state was not restored")
        expected = service_state[unit]
        _run_systemctl(systemctl, "restart" if expected == "active" else "stop", unit)
        if _read_systemctl_state(systemctl, unit) != expected:
            raise TransactionError("pre-migration service state was not restored")
    timer_expected = "active" if record["timer_active_before"] else "inactive"
    timer_enabled_expected = cast(str, record["timer_enabled_before"])
    _run_systemctl(
        systemctl,
        "enable" if timer_enabled_expected == "enabled" else "disable",
        "deadlock-cloudflare-ips.timer",
    )
    if _read_systemctl_enabled(systemctl, "deadlock-cloudflare-ips.timer") != timer_enabled_expected:
        raise TransactionError("pre-migration timer enabled state was not restored")
    _run_systemctl(
        systemctl,
        "start" if timer_expected == "active" else "stop",
        "deadlock-cloudflare-ips.timer",
    )
    if _read_systemctl_state(systemctl, "deadlock-cloudflare-ips.timer") != timer_expected:
        raise TransactionError("pre-migration timer state was not restored")


def recover(
    state: Path,
    *,
    retain: bool = False,
    runtime_pending: bool = False,
    service_pending: bool = False,
) -> None:
    record = _load_record(state)
    if runtime_pending and service_pending:
        raise TransactionError("recovery subphases are mutually exclusive")
    if runtime_pending and (
        not retain or record["operation"] != "rollback"
    ):
        raise TransactionError(
            "runtime-pending recovery requires a retained rollback transaction"
        )
    if service_pending and (
        not retain
        or record["operation"] != "install"
        or record["current_before_path"] is None
        or record["previous_before_path"] is not None
    ):
        raise TransactionError(
            "service-pending recovery requires a retained current-only install"
        )
    if service_pending:
        validate_service_snapshot(state, require="present")
    if (
        record["operation"] == "install"
        and record["phase"] in MIGRATION_OUTCOME_UNCERTAIN_PHASES
    ):
        raise TransactionError(
            "migration outcome is not safely reversible; retain the state and "
            "resume the deployment or make an explicit operator rollback decision"
        )
    if (
        record["phase"] == "restart-pending"
        and not (retain and record["operation"] == "rollback")
    ):
        raise TransactionError(
            "rollback filesystem state is complete but its service restart is pending"
        )
    _verify_recovery_pointers(record)
    if record["phase"] != "recovery-restored":
        _restore_pointers(record)
        _restore_venv(record)
        _verify_original_pointers(record)
        if runtime_pending:
            record["phase"] = "filesystem-restored-runtime-pending"
            _write_record(state, _record_for_write(record), creating=False)
            record = _load_record(state)
        elif service_pending:
            record["phase"] = "filesystem-restored-services-pending"
            _write_record(state, _record_for_write(record), creating=False)
            record = _load_record(state)
        else:
            record = _mark_recovery_restored(state, record)
    else:
        _verify_original_pointers(record)
        _restore_venv(record)
    if retain:
        return
    if record["operation"] == "install":
        _cleanup_recovered_install(state, record)
    else:
        state.unlink()
        _fsync_directory(state.parent)


def complete_recovery(state: Path, *, retain_receipt: bool = False) -> None:
    record = _load_record(state)
    if record["phase"] != "recovery-restored":
        raise TransactionError("release operation recovery is not durably restored")
    _verify_original_pointers(record)
    _restore_venv(record)
    if record["operation"] == "install":
        _cleanup_recovered_install(
            state,
            record,
            remove_receipt=not retain_receipt,
        )
    else:
        if not retain_receipt:
            state.unlink()
            _fsync_directory(state.parent)


def _validate_success(record: dict[str, object]) -> None:
    app = cast(Path, record["app"])
    candidate = cast(Path, record["candidate_path"])
    current_before = record["current_before_path"]
    previous_before = record["previous_before_path"]
    shared = cast(Path, record["shared_venv_path"])
    peer = cast(Path, record["peer_path"])
    snapshot = cast(Path, record["snapshot_path"])
    if record["operation"] == "install":
        desired_previous = (
            current_before if current_before is not None else previous_before
        )
        if (
            _read_pointer(app, "current") != candidate
            or _read_pointer(app, "previous") != desired_previous
        ):
            raise TransactionError("installed release pointers are incomplete")
        if record["transition"] in {"exchange", "create"}:
            peer_before = cast(dict[str, int], record["peer_before"])
            if not _matches(shared, peer_before):
                raise TransactionError("installed shared venv identity is incorrect")
        else:
            shared_before = record["shared_before"]
            if shared_before is None:
                if _lexists(shared):
                    raise TransactionError(
                        "shared venv appeared during dependency skip"
                    )
            else:
                shared_before = cast(dict[str, int], shared_before)
                if not _matches(shared, shared_before):
                    raise TransactionError("shared venv changed during dependency skip")
        if record["transition"] == "exchange":
            shared_before = cast(dict[str, int], record["shared_before"])
            if not _matches(snapshot, shared_before):
                raise TransactionError(
                    "installed rollback snapshot identity is incorrect"
                )
        if _lexists(peer):
            raise TransactionError("install venv peer was not consumed")
    else:
        if (
            _read_pointer(app, "current") != previous_before
            or _read_pointer(app, "previous") != current_before
        ):
            raise TransactionError("rollback release pointers are incomplete")
        if record["transition"] == "exchange":
            shared_before = cast(dict[str, int], record["shared_before"])
            peer_before = cast(dict[str, int], record["peer_before"])
            if not _matches(shared, peer_before) or not _matches(peer, shared_before):
                raise TransactionError("rolled-back venv identities are incorrect")
        elif record["shared_before"] is not None:
            shared_before = cast(dict[str, int], record["shared_before"])
            if not _matches(shared, shared_before):
                raise TransactionError(
                    "shared venv changed during pointer-only rollback"
                )
        elif _lexists(shared):
            raise TransactionError("shared venv appeared during pointer-only rollback")


def complete(state: Path, *, retain_receipt: bool = False) -> None:
    record = _load_record(state)
    if record["phase"] not in {
        "pointers-switched",
        "restart-pending",
        "activation-committed",
        "rollback-runtime-applied",
    }:
        raise TransactionError("release operation pointers are not durably switched")
    _validate_success(record)
    if not retain_receipt:
        state.unlink()
        _fsync_directory(state.parent)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage durable platform release transactions."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--state", required=True, type=Path)
    create.add_argument("--operation", required=True, choices=("install", "rollback"))
    create.add_argument("--app-dir", required=True, type=Path)
    create.add_argument("--current-before", default="")
    create.add_argument("--previous-before", default="")
    create.add_argument("--candidate-release", required=True, type=Path)
    create.add_argument("--shared-venv", required=True, type=Path)
    create.add_argument("--peer", required=True, type=Path)
    create.add_argument("--snapshot", required=True, type=Path)
    create.add_argument(
        "--transition", required=True, choices=("exchange", "create", "none")
    )
    create.add_argument("--remove-env-on-recovery", action="store_true")

    phase = commands.add_parser("phase")
    phase.add_argument("--state", required=True, type=Path)
    phase.add_argument("--expected", required=True, choices=tuple(sorted(PHASES)))
    phase.add_argument("--phase", required=True, choices=tuple(sorted(PHASES)))

    record_services_parser = commands.add_parser("record-services")
    record_services_parser.add_argument("--state", required=True, type=Path)
    record_services_parser.add_argument(
        "--service-state", required=True, action="append"
    )
    record_services_parser.add_argument(
        "--timer-active-before", required=True, choices=("active", "inactive")
    )
    record_services_parser.add_argument(
        "--service-enabled", required=True, action="append"
    )
    record_services_parser.add_argument(
        "--timer-enabled-before", required=True, choices=("enabled", "disabled")
    )

    prepare_quiesce_parser = commands.add_parser("prepare-quiesce")
    prepare_quiesce_parser.add_argument("--state", required=True, type=Path)
    prepare_quiesce_parser.add_argument("--app-dir", required=True, type=Path)
    prepare_quiesce_parser.add_argument(
        "--candidate-release", required=True, type=Path
    )
    prepare_quiesce_parser.add_argument(
        "--candidate-may-exist", action="store_true"
    )
    prepare_quiesce_parser.add_argument(
        "--service-state", required=True, action="append"
    )
    prepare_quiesce_parser.add_argument(
        "--timer-active-before", required=True, choices=("active", "inactive")
    )
    prepare_quiesce_parser.add_argument(
        "--service-enabled", required=True, action="append"
    )
    prepare_quiesce_parser.add_argument(
        "--timer-enabled-before", required=True, choices=("enabled", "disabled")
    )
    promote_quiesce_parser = commands.add_parser("promote-quiesce")
    promote_quiesce_parser.add_argument("--state", required=True, type=Path)
    promote_quiesce_parser.add_argument(
        "--candidate-release", required=True, type=Path
    )
    promote_quiesce_parser.add_argument("--shared-venv", required=True, type=Path)
    promote_quiesce_parser.add_argument("--peer", required=True, type=Path)
    promote_quiesce_parser.add_argument("--snapshot", required=True, type=Path)
    promote_quiesce_parser.add_argument(
        "--transition", required=True, choices=("exchange", "create", "none")
    )
    promote_quiesce_parser.add_argument(
        "--remove-env-on-recovery", action="store_true"
    )
    verify_quiesce_parser = commands.add_parser("verify-quiesce")
    verify_quiesce_parser.add_argument("--state", required=True, type=Path)
    validate_quiesce_noop_parser = commands.add_parser("validate-quiesce-noop")
    validate_quiesce_noop_parser.add_argument("--state", required=True, type=Path)
    clear_quiesce_parser = commands.add_parser("clear-quiesce")
    clear_quiesce_parser.add_argument("--state", required=True, type=Path)
    abort_quiesce_parser = commands.add_parser("abort-quiesce")
    abort_quiesce_parser.add_argument("--state", required=True, type=Path)
    restore_quiesce_parser = commands.add_parser("restore-quiesce")
    restore_quiesce_parser.add_argument("--state", required=True, type=Path)
    restore_quiesce_parser.add_argument("--systemctl", required=True)
    validate_service_snapshot_parser = commands.add_parser("validate-service-snapshot")
    validate_service_snapshot_parser.add_argument("--state", required=True, type=Path)
    validate_service_snapshot_parser.add_argument(
        "--require", required=True, choices=("present", "absent", "optional")
    )
    restore_services_parser = commands.add_parser("restore-services")
    restore_services_parser.add_argument("--state", required=True, type=Path)
    restore_services_parser.add_argument("--systemctl", required=True)
    for name in (
        "capture-initial-systemd",
        "restore-initial-systemd",
        "verify-initial-systemd",
        "validate-initial-systemd",
        "verify-initial-systemd-activated",
    ):
        initial_systemd_parser = commands.add_parser(name)
        initial_systemd_parser.add_argument("--state", required=True, type=Path)
        initial_systemd_parser.add_argument("--systemctl", required=True)
    status_quiesce_parser = commands.add_parser("status-quiesce")
    status_quiesce_parser.add_argument("--state", required=True, type=Path)
    status_quiesce_parser.add_argument("--json", action="store_true", dest="as_json")

    exchange = commands.add_parser("exchange")
    exchange.add_argument("--state", required=True, type=Path)
    rename = commands.add_parser("rename")
    rename.add_argument("--state", required=True, type=Path)
    rename.add_argument(
        "--mode", required=True, choices=("activate-created", "place-snapshot")
    )
    pointer = commands.add_parser("switch-pointer")
    pointer.add_argument("--state", required=True, type=Path)
    pointer.add_argument("--name", required=True, choices=("current", "previous"))
    pointer.add_argument("--target", default="")
    validate_recovery_pointers_parser = commands.add_parser(
        "validate-recovery-pointers"
    )
    validate_recovery_pointers_parser.add_argument("--state", required=True, type=Path)
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--state", required=True, type=Path)
    recover_parser.add_argument("--retain", action="store_true")
    recover_parser.add_argument("--runtime-pending", action="store_true")
    recover_parser.add_argument("--service-pending", action="store_true")
    authorize_parser = commands.add_parser("authorize-recovery")
    authorize_parser.add_argument("--state", required=True, type=Path)
    authorize_parser.add_argument("--confirm", required=True)
    verify_original_parser = commands.add_parser("verify-original")
    verify_original_parser.add_argument("--state", required=True, type=Path)
    complete_parser = commands.add_parser("complete")
    complete_parser.add_argument("--state", required=True, type=Path)
    complete_parser.add_argument("--retain-receipt", action="store_true")
    complete_recovery_parser = commands.add_parser("complete-recovery")
    complete_recovery_parser.add_argument("--state", required=True, type=Path)
    complete_recovery_parser.add_argument("--retain-receipt", action="store_true")
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--state", required=True, type=Path)
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if os.geteuid() != 0:
            raise TransactionError("release transactions require root")
        if args.command == "create":
            create_record(
                args.state,
                operation=args.operation,
                app_dir=args.app_dir,
                current_before=args.current_before,
                previous_before=args.previous_before,
                candidate_release=args.candidate_release,
                shared_venv=args.shared_venv,
                peer=args.peer,
                snapshot=args.snapshot,
                transition=args.transition,
                remove_env_on_recovery=args.remove_env_on_recovery,
            )
        elif args.command == "phase":
            set_phase(args.state, expected=args.expected, phase=args.phase)
        elif args.command == "record-services":
            record_services(
                args.state,
                service_states=args.service_state,
                timer_active_before=args.timer_active_before,
                service_enabled=args.service_enabled,
                timer_enabled_before=args.timer_enabled_before,
            )
        elif args.command == "prepare-quiesce":
            prepare_quiesce(
                args.state,
                app_dir=args.app_dir,
                candidate_release=args.candidate_release,
                service_states=args.service_state,
                timer_active_before=args.timer_active_before,
                service_enabled=args.service_enabled,
                timer_enabled_before=args.timer_enabled_before,
                candidate_may_exist=args.candidate_may_exist,
            )
        elif args.command == "promote-quiesce":
            promote_quiesce(
                args.state,
                candidate_release=args.candidate_release,
                shared_venv=args.shared_venv,
                peer=args.peer,
                snapshot=args.snapshot,
                transition=args.transition,
                remove_env_on_recovery=args.remove_env_on_recovery,
            )
        elif args.command == "verify-quiesce":
            verify_quiesce(args.state)
        elif args.command == "validate-quiesce-noop":
            validate_quiesce_noop(args.state)
        elif args.command == "clear-quiesce":
            clear_quiesce(args.state)
        elif args.command == "abort-quiesce":
            abort_quiesce(args.state)
        elif args.command == "restore-quiesce":
            restore_quiesce(args.state, systemctl=args.systemctl)
        elif args.command == "validate-service-snapshot":
            validate_service_snapshot(args.state, require=args.require)
        elif args.command == "restore-services":
            restore_services(args.state, systemctl=args.systemctl)
        elif args.command == "capture-initial-systemd":
            capture_initial_systemd(args.state, systemctl=args.systemctl)
        elif args.command == "restore-initial-systemd":
            restore_initial_systemd(args.state, systemctl=args.systemctl)
        elif args.command == "verify-initial-systemd":
            verify_initial_systemd(args.state, systemctl=args.systemctl)
        elif args.command == "validate-initial-systemd":
            validate_initial_systemd(args.state)
        elif args.command == "verify-initial-systemd-activated":
            verify_initial_systemd_activated(args.state, systemctl=args.systemctl)
        elif args.command == "status-quiesce":
            record = _load_quiesce_record(args.state)
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "operation": record["operation"],
                            "phase": record["phase"],
                            "app_dir": record["app_dir"],
                            "current_before": record["current_before"],
                            "previous_before": record["previous_before"],
                            "candidate_release": record["candidate_release"],
                            "service_state_before": record["service_state_before"],
                            "service_enabled_before": record.get("service_enabled_before"),
                            "quiesced_services": record["quiesced_services"],
                            "timer_active_before": record["timer_active_before"],
                            "timer_enabled_before": record.get("timer_enabled_before"),
                        },
                        sort_keys=True,
                    )
                )
            else:
                print(f"{record['operation']} {record['phase']}")
        elif args.command == "exchange":
            exchange_recorded_venvs(args.state)
        elif args.command == "rename":
            rename_recorded_venv(args.state, mode=args.mode)
        elif args.command == "switch-pointer":
            switch_pointer(args.state, name=args.name, target_value=args.target)
        elif args.command == "validate-recovery-pointers":
            record = _load_record(args.state)
            _verify_recovery_pointers(record)
        elif args.command == "recover":
            recover(
                args.state,
                retain=args.retain,
                runtime_pending=args.runtime_pending,
                service_pending=args.service_pending,
            )
        elif args.command == "authorize-recovery":
            authorize_recovery(args.state, confirmation=args.confirm)
        elif args.command == "verify-original":
            record = _load_record(args.state)
            _verify_original_pointers(record)
        elif args.command == "complete":
            complete(args.state, retain_receipt=args.retain_receipt)
        elif args.command == "complete-recovery":
            complete_recovery(args.state, retain_receipt=args.retain_receipt)
        elif args.command == "status":
            try:
                record = _load_record(args.state)
            except TransactionError as operation_error:
                try:
                    quiesce_record = _load_quiesce_record(args.state)
                except TransactionError:
                    raise operation_error
                if args.as_json:
                    print(
                        json.dumps(
                            {
                                "operation": quiesce_record["operation"],
                                "phase": quiesce_record["phase"],
                                "app_dir": quiesce_record["app_dir"],
                                "current_before": quiesce_record["current_before"],
                                "previous_before": quiesce_record["previous_before"],
                                "candidate_release": quiesce_record[
                                    "candidate_release"
                                ],
                                "service_state_before": quiesce_record[
                                    "service_state_before"
                                ],
                                "service_enabled_before": quiesce_record.get(
                                    "service_enabled_before"
                                ),
                                "quiesced_services": quiesce_record[
                                    "quiesced_services"
                                ],
                                "timer_active_before": quiesce_record[
                                    "timer_active_before"
                                ],
                                "timer_enabled_before": quiesce_record.get(
                                    "timer_enabled_before"
                                ),
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    print(f"{quiesce_record['operation']} {quiesce_record['phase']}")
                return 0
            if record["phase"] == "restart-pending":
                _validate_success(record)
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "operation": record["operation"],
                            "operation_id": record["operation_id"],
                            "phase": record["phase"],
                            "app_dir": record["app_dir"],
                            "current_before": record["current_before"],
                            "previous_before": record["previous_before"],
                            "candidate_release": record["candidate_release"],
                            "service_state_before": record["service_state_before"],
                            "service_enabled_before": record["service_enabled_before"],
                            "quiesced_services": record["quiesced_services"],
                            "timer_active_before": record["timer_active_before"],
                            "timer_enabled_before": record["timer_enabled_before"],
                        },
                        sort_keys=True,
                    )
                )
            else:
                print(f"{record['operation']} {record['phase']}")
        else:
            raise TransactionError("unknown release transaction command")
        return 0
    except TransactionError as exc:
        # Error details can contain private release/shared paths from the
        # durable receipt. Callers project only this fixed class; the full
        # exception remains available to root-only diagnostics, never to a
        # public workflow log.
        migration_uncertain = str(exc).startswith(
            "migration outcome is not safely reversible"
        )
        if migration_uncertain:
            print(
                "RELEASE_TRANSACTION schema=1 status=failed class=transaction "
                "error_class=migration_uncertain "
                "detail=migration outcome is not safely reversible",
                file=sys.stderr,
            )
        else:
            print(
                "RELEASE_TRANSACTION schema=1 status=failed class=transaction",
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
