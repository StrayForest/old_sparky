#!/usr/bin/env python3
"""Durable, inode-checked install/rollback transaction recovery."""

from __future__ import annotations

import argparse
import ast
import base64
import ctypes
import errno
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
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
SYSTEMD_STATE_NAME = ".release-systemd-state.json"
QUIESCE_STATE_NAME = ".release-quiesce.json"
QUARANTINE_DIR_NAME = ".release-quarantine"
QUARANTINE_RECEIPT_SCHEMA = 1
QUARANTINE_PRUNE_INTENT_SCHEMA = 1
QUARANTINE_MAX_ENTRIES = 100_000
QUARANTINE_MAX_BYTES = 8 * 1024 * 1024 * 1024
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
PROTECTED_ACTIVATION_CANDIDATE_SHA = "79faf7fe9104da7afa67036d3894e19e41c582ad"
PROTECTED_ACTIVATION_CURRENT_SHA = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
PROTECTED_ACTIVATION_PREVIOUS_SHA = "f1154c7dba4f57bf52f7d7544e97d924c732be94"
SERVICE_UNITS = ("deadlock-api", "deadlock-worker", "deadlock-web")
LEGACY_READINESS_TIMEOUT_SECONDS = 30.0
LEGACY_READINESS_ATTEMPT_TIMEOUT_SECONDS = 3.0
LEGACY_READINESS_CURL_TIMEOUT_SECONDS = 2
LEGACY_READINESS_RETRY_INTERVAL_SECONDS = 1.0
SYSTEMD_CALL_TIMEOUT_SECONDS = 30.0
SYSTEMD_OPERATION_TIMEOUT_SECONDS = 120.0
WEB_CACHE_RELATIVE_PATH = Path(
    "apps/platform_web/.next/standalone/.next/cache"
)


def _protected_static_revision_graph(release_root: Path) -> dict[str, tuple[str, ...]]:
    """Read only literal Alembic metadata for the pinned protected recovery.

    Migration files belong to a release tree whose full payload proof may not
    have run yet. Parse metadata as data; never import or execute those files.
    """
    root = Path(release_root)
    versions = root / "alembic" / "versions"
    for directory in (root, root / "alembic", versions):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise TransactionError("protected migration directory is unsafe")

    result: dict[str, tuple[str, ...]] = {}
    with os.scandir(versions) as iterator:
        entries = list(iterator)
    for entry in entries:
        if entry.is_symlink():
            raise TransactionError("protected migration entry is a symlink")
        if entry.is_dir(follow_symlinks=False):
            if entry.name == "__pycache__":
                continue
            raise TransactionError("protected migration subdirectory is unsafe")
        if entry.name == "__init__.py":
            continue
        if not entry.name.endswith(".py"):
            raise TransactionError("protected migration entry is unexpected")
        info = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise TransactionError("protected migration file is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(entry.path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise TransactionError("protected migration file changed")
            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > 2 * 1024 * 1024:
                    raise TransactionError("protected migration file is too large")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise TransactionError("protected migration file changed while reading")
        finally:
            os.close(fd)
        try:
            tree = ast.parse(b"".join(chunks).decode("utf-8"), filename=entry.name)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise TransactionError("protected migration metadata is malformed") from exc

        metadata: dict[str, object] = {}
        for node in tree.body:
            names: list[str] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
                value = node.value
            for name in ("revision", "down_revision", "depends_on", "branch_labels"):
                if name not in names:
                    continue
                if name in metadata or value is None:
                    raise TransactionError("protected migration metadata is ambiguous")
                try:
                    metadata[name] = ast.literal_eval(value)
                except (ValueError, TypeError, SyntaxError) as exc:
                    raise TransactionError("protected migration metadata is not literal") from exc

        revision = metadata.get("revision")
        if set(metadata) != {"revision", "down_revision", "depends_on", "branch_labels"}:
            raise TransactionError("protected migration metadata is incomplete")
        if not isinstance(revision, str) or not revision or revision in result:
            raise TransactionError("protected migration revision is invalid")
        parents: list[str] = []
        for name in ("down_revision", "depends_on"):
            value = metadata.get(name)
            if value is None:
                continue
            values = (value,) if isinstance(value, str) else value
            if not isinstance(values, (tuple, list)) or not all(
                isinstance(parent, str) and parent for parent in values
            ):
                raise TransactionError("protected migration parent metadata is invalid")
            parents.extend(values)
        labels = metadata.get("branch_labels")
        if labels is not None and not (
            isinstance(labels, str)
            or isinstance(labels, (tuple, list))
            and all(isinstance(label, str) for label in labels)
        ):
            raise TransactionError("protected migration branch metadata is invalid")
        result[revision] = tuple(dict.fromkeys(parents))

    if not result or any(
        parent not in result for parents in result.values() for parent in parents
    ):
        raise TransactionError("protected migration graph is incomplete")
    return result
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
    "rollback-cache-purged",
    "rollback-runtime-pending",
    "rollback-runtime-applied",
    "filesystem-restored-runtime-pending",
    "filesystem-restored-services-pending",
    "recovery-restored",
    "legacy-services-restored",
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
        "rollback-cache-purged",
        "filesystem-restored-runtime-pending",
        "recovery-authorized",
    },
    "rollback-cache-purged": {
        "services-restarted",
        "rollback-runtime-applied",
        "recovery-authorized",
    },
    "rollback-runtime-applied": {"recovery-authorized"},
    "filesystem-restored-runtime-pending": {"recovery-restored"},
    "filesystem-restored-services-pending": {"recovery-restored"},
    "recovery-restored": set(),
    "legacy-services-restored": set(),
}
ROLLBACK_CACHE_PURGE_PROOF_PHASES = frozenset(
    {
        "rollback-cache-purged",
        "services-restarted",
        "smoke-passed",
        "rollback-runtime-applied",
    }
)
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
    "profile_access_cache_purge_proof",
}
RECORD_KEYS_WITHOUT_PROFILE_CACHE_PURGE_PROOF = RECORD_KEYS - {
    "profile_access_cache_purge_proof"
}
LEGACY_RECORD_KEYS = RECORD_KEYS - {
    "operation_id",
    "service_enabled_before",
    "timer_enabled_before",
    "systemd_state_before",
    "profile_access_cache_purge_proof",
}
RECORD_KEYS_WITHOUT_SYSTEMD_STATE = RECORD_KEYS - {"systemd_state_before"}
RECORD_KEYS_WITHOUT_SYSTEMD_AND_PROFILE_CACHE_PURGE_PROOF = (
    RECORD_KEYS - {"systemd_state_before", "profile_access_cache_purge_proof"}
)
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


def _read_private_state_bytes(path: Path, *, label: str) -> bytes:
    metadata = _safe_state_file(path)
    if metadata.st_gid != 0:
        raise TransactionError(f"{label} group ownership is unsafe")
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(metadata)
            or opened.st_uid != 0
            or opened.st_gid != 0
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size != metadata.st_size
        ):
            raise TransactionError(f"{label} changed before reading")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 8192):
            total += len(chunk)
            if total > 64 * 1024:
                raise TransactionError(f"{label} is too large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            _identity(after) != _identity(opened)
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            raise TransactionError(f"{label} changed while reading")
        raw = b"".join(chunks)
        if len(raw) != opened.st_size:
            raise TransactionError(f"{label} length changed while reading")
        return raw
    except OSError as exc:
        raise TransactionError(f"{label} cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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


def _quarantine_tree_snapshot(root: Path) -> tuple[str, int, int, int]:
    """Hash a bounded, no-follow release tree without treating it as trusted."""
    root_meta = _safe_directory(root, label="candidate release")
    if root_meta.st_gid != 0:
        raise TransactionError("candidate release group ownership is unsafe")
    root_device = root_meta.st_dev
    rows: list[tuple[bytes, bytes]] = [
        (
            b"",
            json.dumps(
                ["", "directory", stat.S_IMODE(root_meta.st_mode), root_meta.st_uid, root_meta.st_gid],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii"),
        )
    ]
    pending = [root]
    file_count = directory_count = total_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise TransactionError("candidate release tree cannot be enumerated") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise TransactionError("candidate release tree changed") from exc
            if info.st_dev != root_device:
                raise TransactionError("candidate release tree crosses filesystems")
            if info.st_uid != 0 or info.st_gid != 0:
                raise TransactionError("candidate release tree ownership is unsafe")
            if stat.S_ISDIR(info.st_mode):
                directory_count += 1
                if directory_count + file_count > QUARANTINE_MAX_ENTRIES:
                    raise TransactionError("candidate release tree exceeds entry limit")
                pending.append(path)
                descriptor = json.dumps(
                    [relative, "directory", stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("ascii")
                rows.append((os.fsencode(relative), descriptor))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise TransactionError("candidate release tree contains a special file")
            if info.st_nlink != 1:
                raise TransactionError("candidate release tree contains a hard link")
            file_count += 1
            total_bytes += info.st_size
            if file_count + directory_count > QUARANTINE_MAX_ENTRIES or total_bytes > QUARANTINE_MAX_BYTES:
                raise TransactionError("candidate release tree exceeds evidence limits")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor_fd = -1
            try:
                descriptor_fd = os.open(path, flags)
                before = os.fstat(descriptor_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or _identity(before) != _identity(info)
                    or before.st_dev != root_device
                    or before.st_nlink != 1
                    or before.st_uid != 0
                    or before.st_gid != 0
                ):
                    raise TransactionError("candidate release file changed")
                digest = hashlib.sha256()
                while chunk := os.read(descriptor_fd, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor_fd)
                if _identity(after) != _identity(before) or after.st_size != before.st_size:
                    raise TransactionError("candidate release file changed during hashing")
            except OSError as exc:
                raise TransactionError("candidate release file cannot be read safely") from exc
            finally:
                if descriptor_fd >= 0:
                    os.close(descriptor_fd)
            descriptor = json.dumps(
                [relative, "file", stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid, info.st_size, digest.hexdigest()],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            rows.append((os.fsencode(relative), descriptor))
    rows.sort(key=lambda row: row[0])
    tree_digest = hashlib.sha256()
    for _, row in rows:
        tree_digest.update(row)
        tree_digest.update(b"\n")
    return tree_digest.hexdigest(), file_count, directory_count, total_bytes


def _quarantine_assert_no_mounts(
    root: Path,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> None:
    """Reject mountpoints at or below a retained tree, including same-device binds."""
    descriptor = -1
    try:
        descriptor = os.open(
            mountinfo_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        chunks: list[bytes] = []
        total = 0
        maximum = 16 * 1024 * 1024
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise TransactionError("quarantined candidate mount table exceeds its bound")
            chunks.append(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise TransactionError("quarantined candidate mount table is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    lines = raw.splitlines()
    if not lines:
        raise TransactionError("quarantined candidate mount table is empty")
    try:
        root_bytes = os.path.normpath(os.fsencode(root.resolve(strict=True)))
    except OSError as exc:
        raise TransactionError("quarantined candidate path is unavailable") from exc
    try:
        for line in lines:
            fields = line.split(b" ")
            if len(fields) < 6:
                raise TransactionError("quarantined candidate mount table is malformed")
            mountpoint = re.sub(
                rb"\\([0-7]{3})",
                lambda match: bytes((int(match.group(1), 8),)),
                fields[4],
            )
            mountpoint = os.path.normpath(mountpoint)
            if os.path.commonpath((root_bytes, mountpoint)) == root_bytes:
                raise TransactionError("quarantined candidate contains a mountpoint")
    except ValueError as exc:
        raise TransactionError("quarantined candidate mount table is malformed") from exc


def _quarantine_root(shared: Path, releases: Path) -> Path:
    root = shared / QUARANTINE_DIR_NAME
    if not _lexists(root):
        try:
            root.mkdir(mode=0o700)
            os.chown(root, 0, 0)
            os.chmod(root, 0o700)
            _fsync_directory(shared)
        except OSError as exc:
            raise TransactionError("recovery quarantine directory cannot be created") from exc
    try:
        info = root.lstat()
    except OSError as exc:
        raise TransactionError("recovery quarantine directory is unavailable") from exc
    releases_info = _safe_directory(releases, label="releases directory")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_nlink < 2
        or info.st_dev != releases_info.st_dev
    ):
        raise TransactionError("recovery quarantine directory metadata is unsafe")
    return root


def _rename_noreplace(source: Path, destination: Path) -> None:
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise TransactionError("candidate quarantine crosses filesystems")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise TransactionError("atomic no-replace rename is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(AT_FDCWD, os.fsencode(source), AT_FDCWD, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise TransactionError(f"candidate quarantine rename failed: {errno.errorcode.get(error, error)}")
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


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
    accepted_record_keys = {
        frozenset(RECORD_KEYS),
        frozenset(RECORD_KEYS_WITHOUT_SYSTEMD_STATE),
        frozenset(RECORD_KEYS_WITHOUT_PROFILE_CACHE_PURGE_PROOF),
        frozenset(RECORD_KEYS_WITHOUT_SYSTEMD_AND_PROFILE_CACHE_PURGE_PROOF),
    }
    if (
        frozenset(record) not in accepted_record_keys and not legacy_recovery
    ) or record.get("version") != STATE_VERSION:
        raise TransactionError("release operation record schema is invalid")
    if "systemd_state_before" not in record:
        # Existing v2 receipts predate first-install systemd activation. Keep
        # them readable, but normalize the optional field in memory so every
        # subsequent durable write has one unambiguous schema.
        record = {**record, "systemd_state_before": None}
    if "profile_access_cache_purge_proof" not in record:
        # Receipts written before cache-purge proof was introduced remain
        # readable for rollback, but cannot authorize cache-sensitive
        # completion or service activation.
        record = {**record, "profile_access_cache_purge_proof": None}
    if record["profile_access_cache_purge_proof"] is not None:
        _validate_profile_access_cache_purge_proof_shape(
            record["profile_access_cache_purge_proof"]
        )
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
    if phase == "rollback-cache-purged" and operation != "rollback":
        raise TransactionError("legacy cache purge phase is rollback-only")
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
        must_exist=not (
            operation == "install"
            and phase in {"recovery-restored", "legacy-services-restored"}
        ),
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

    validated = {
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
    if phase == "legacy-services-restored":
        # This phase is writable only through mark_legacy_services_restored,
        # after service and readiness checks. Revalidate its full authority
        # whenever loaded before allowing receipt-bound cleanup.
        if (
            operation != "install"
            or current_before is None
            or validated.get("systemd_state_before") is not None
            or not isinstance(validated.get("operation_id"), str)
            or OPERATION_ID_PATTERN.fullmatch(cast(str, validated["operation_id"])) is None
            or _lexists(shared / SYSTEMD_STATE_NAME)
        ):
            raise TransactionError("legacy services phase authority is invalid")
        _verify_original_pointers(validated)
        _verify_restored_venv(validated)
        legacy_release = cast(Path, current_before)
        if _lexists(legacy_release / "tools/platform_live_qa_runtime_install.py") or _lexists(
            legacy_release / "liveqa-runtime"
        ):
            raise TransactionError("legacy services phase has managed LiveQA inputs")
        _require_profile_access_cache_purge_proof(validated)
    if validated["profile_access_cache_purge_proof"] is not None:
        _require_profile_access_cache_purge_proof(validated)
    return validated


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
        "profile_access_cache_purge_proof": None,
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
    if (
        record["operation"] == "rollback"
        and phase in ROLLBACK_CACHE_PURGE_PROOF_PHASES
    ):
        _require_profile_access_cache_purge_proof(record)
    if (
        record["operation"] == "rollback"
        and expected in ROLLBACK_CACHE_PURGE_PROOF_PHASES
    ):
        _require_profile_access_cache_purge_proof(record)
    if (
        record["operation"] == "install"
        and phase == "legacy-services-restored"
    ):
        _require_profile_access_cache_purge_proof(record)
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
            "legacy-services-restored": ((None, None),),
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
            "rollback-cache-purged": ((rollback_current, rollback_previous),),
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
            if record["phase"] not in {"recovery-restored", "legacy-services-restored"}:
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
    service_owned_cache: tuple[Path, int, int] | None = None,
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
    service_cache_root: Path | None = None
    service_cache_uid = 0
    service_cache_gid = 0
    if service_owned_cache is not None:
        service_cache_root, service_cache_uid, service_cache_gid = service_owned_cache
        if (
            service_cache_root != path / WEB_CACHE_RELATIVE_PATH
            or service_cache_uid <= 0
            or service_cache_gid <= 0
        ):
            raise TransactionError("release cleanup service cache binding is invalid")
        if _lexists(service_cache_root):
            try:
                service_cache_metadata = service_cache_root.lstat()
            except OSError as exc:
                raise TransactionError("release cleanup service cache is unavailable") from exc
            if (
                not stat.S_ISDIR(service_cache_metadata.st_mode)
                or stat.S_ISLNK(service_cache_metadata.st_mode)
                or service_cache_metadata.st_dev != device
                or service_cache_metadata.st_uid != service_cache_uid
                or service_cache_metadata.st_gid != service_cache_gid
            ):
                raise TransactionError("release cleanup service cache metadata is unsafe")
    # Walk from open directory descriptors. A service-owned cache is only
    # eligible after its service cgroup has been stopped and the restored
    # legacy services have passed readiness; dirfd-relative no-follow stats
    # also ensure cleanup never follows a cache entry if the tree changes.
    try:
        for root, directories, files, directory_fd in os.fwalk(
            path, topdown=True, follow_symlinks=False
        ):
            root_path = Path(root)
            for name in (*directories, *files):
                try:
                    child_metadata = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                except OSError as exc:
                    raise TransactionError(
                        "release cleanup entry cannot be inspected"
                    ) from exc
                mode = child_metadata.st_mode
                child = root_path / name
                is_symlink = stat.S_ISLNK(mode)
                service_cache_entry = (
                    service_cache_root is not None
                    and (child == service_cache_root or service_cache_root in child.parents)
                )
                symlink_root_allowed = any(
                    child == allowed_root or allowed_root in child.parents
                    for allowed_root in allowed_symlink_roots
                )
                if service_cache_entry:
                    owner_is_unsafe = (
                        child_metadata.st_uid != service_cache_uid
                        or child_metadata.st_gid != service_cache_gid
                    )
                else:
                    owner_is_unsafe = (
                        child_metadata.st_uid != 0 or child_metadata.st_gid != 0
                    )
                if (
                    child_metadata.st_dev != device
                    or owner_is_unsafe
                    or (
                        not is_symlink
                        and stat.S_IMODE(mode) & 0o022
                    )
                    or (
                        is_symlink
                        and (
                            service_cache_entry
                            or not symlink_root_allowed
                            or child_metadata.st_nlink != 1
                        )
                    )
                ):
                    raise TransactionError("release cleanup entry metadata is unsafe")
                if is_symlink:
                    continue
                if stat.S_ISDIR(mode):
                    if child_metadata.st_nlink < 2:
                        raise TransactionError(
                            "release cleanup directory identity is invalid"
                        )
                elif stat.S_ISREG(mode):
                    if child_metadata.st_nlink != 1:
                        raise TransactionError("release cleanup hardlink is unsafe")
                else:
                    raise TransactionError("release cleanup entry type is unsafe")
    except OSError as exc:
        raise TransactionError("release cleanup tree cannot be inspected") from exc
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
        quarantined_service_cache = quarantine / WEB_CACHE_RELATIVE_PATH
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
            in_service_cache = (
                root_path == quarantined_service_cache
                or quarantined_service_cache in root_path.parents
            )
            if not in_service_cache and not stat.S_ISLNK(root_path.lstat().st_mode):
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


def _web_cache_service_identity() -> tuple[int, int]:
    try:
        service_account = pwd.getpwnam("oldsparky-web")
        service_group = grp.getgrnam("oldsparky-web")
        user_count = sum(
            entry.pw_uid == service_account.pw_uid for entry in pwd.getpwall()
        )
        group_count = sum(
            entry.gr_gid == service_group.gr_gid for entry in grp.getgrall()
        )
    except KeyError as exc:
        raise TransactionError("web cache service identity is unavailable") from exc
    if (
        service_account.pw_uid <= 0
        or service_group.gr_gid <= 0
        or service_account.pw_gid != service_group.gr_gid
        or user_count != 1
        or group_count != 1
    ):
        raise TransactionError("web cache service identity is unsafe")
    return service_account.pw_uid, service_group.gr_gid


def _require_web_unit_control_group(systemctl: str) -> None:
    kill_mode = _run_systemctl(
        systemctl,
        "show",
        "--property=KillMode",
        "--value",
        "deadlock-web.service",
    )
    if kill_mode != "control-group":
        raise TransactionError("web service process containment is unsafe")


def _mark_recovery_restored(
    state: Path, record: dict[str, object]
) -> dict[str, object]:
    record["phase"] = "recovery-restored"
    _write_record(state, _record_for_write(record), creating=False)
    return _load_record(state)


def _cleanup_recovered_install(
    state: Path,
    record: dict[str, object],
    *,
    remove_receipt: bool = True,
    allow_service_owned_cache: bool = False,
) -> None:
    shared = cast(Path, record["shared"])
    peer = cast(Path, record["peer_path"])
    candidate = cast(Path, record["candidate_path"])
    peer_before = record["peer_before"]
    candidate_cache = candidate / WEB_CACHE_RELATIVE_PATH
    if _lexists(candidate_cache) and (
        not allow_service_owned_cache
        or record.get("phase") != "legacy-services-restored"
        or record.get("operation") != "install"
    ):
        raise TransactionError(
            "service-owned candidate cache requires verified legacy restoration"
        )
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
        service_owned_cache = None
        if _lexists(candidate_cache):
            service_owned_cache = (
                candidate_cache,
                *_web_cache_service_identity(),
            )
        _remove_tree(
            candidate,
            expected_identity=candidate_identity,
            allowed_symlink_roots=(candidate / ".rollback/shared-venv-before-install",),
            service_owned_cache=service_owned_cache,
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


QUARANTINE_RECEIPT_KEYS = {
    "schema",
    "action",
    "phase",
    "failed_deploy_run_id",
    "failed_deploy_run_attempt",
    "failed_deploy_source_sha",
    "recovery_run_id",
    "recovery_run_attempt",
    "recovery_bundle_sha",
    "pending_sha256",
    "candidate_slug",
    "candidate_dev",
    "candidate_ino",
    "candidate_tree_sha256",
    "candidate_file_count",
    "candidate_directory_count",
    "candidate_bytes",
    "current_before",
    "previous_before",
    "service_state_before",
    "service_enabled_before",
    "timer_active_before",
    "timer_enabled_before",
    "quarantine_slug",
    "quarantine_dev",
    "quarantine_ino",
}
QUARANTINE_PHASES = frozenset(
    {"prepared", "quarantined", "services_restored", "receipt_consumed"}
)
QUARANTINE_PRUNE_INTENT_KEYS = {
    "schema",
    "action",
    "phase",
    "failed_deploy_run_id",
    "failed_deploy_run_attempt",
    "failed_deploy_source_sha",
    "candidate_slug",
    "original_receipt_sha256",
    "original_recovery_run_id",
    "original_recovery_run_attempt",
    "original_recovery_bundle_sha",
    "cleanup_run_id",
    "cleanup_run_attempt",
    "cleanup_source_sha",
    "cleanup_bundle_sha",
    "candidate_dev",
    "candidate_ino",
    "quarantine_dev",
    "quarantine_ino",
    "candidate_tree_sha256",
    "candidate_file_count",
    "candidate_directory_count",
    "candidate_bytes",
    "current_before",
    "previous_before",
    "stage_name",
}
QUARANTINE_PRUNE_PHASES = frozenset({"planned", "deleting", "deleted"})


def _quarantine_values(
    *,
    failed_deploy_run_id: str,
    failed_deploy_run_attempt: str,
    failed_deploy_source_sha: str,
    recovery_run_id: str,
    recovery_run_attempt: str,
    bundle_sha: str,
) -> dict[str, str]:
    for value, label in (
        (failed_deploy_run_id, "failed deployment run id"),
        (failed_deploy_run_attempt, "failed deployment attempt"),
        (recovery_run_id, "recovery run id"),
        (recovery_run_attempt, "recovery run attempt"),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
            raise TransactionError(f"{label} is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", failed_deploy_source_sha) is None:
        raise TransactionError("failed deployment source SHA is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", bundle_sha) is None:
        raise TransactionError("recovery bundle SHA is invalid")
    return {
        "failed_deploy_run_id": failed_deploy_run_id,
        "failed_deploy_run_attempt": failed_deploy_run_attempt,
        "failed_deploy_source_sha": failed_deploy_source_sha,
        "recovery_run_id": recovery_run_id,
        "recovery_run_attempt": recovery_run_attempt,
        "recovery_bundle_sha": bundle_sha,
    }


def _quarantine_receipt_path(root: Path, candidate_slug: str) -> Path:
    return root / f"{candidate_slug}.json"


def _read_quarantine_receipt(
    path: Path, *, raw: bytes | None = None
) -> dict[str, object]:
    info = _safe_state_file(path)
    if info.st_size > 64 * 1024:
        raise TransactionError("quarantine receipt is too large")
    try:
        payload = (
            _read_private_state_bytes(path, label="quarantine receipt")
            if raw is None
            else raw
        )
        if len(payload) != info.st_size:
            raise TransactionError("quarantine receipt changed while reading")
        value = json.loads(payload.decode("ascii"), object_pairs_hook=_strict_object)
    except TransactionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("quarantine receipt is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != QUARANTINE_RECEIPT_KEYS
        or type(value.get("schema")) is not int
        or value.get("schema") != QUARANTINE_RECEIPT_SCHEMA
        or type(value.get("action")) is not str
        or value.get("action") != "quarantine_populated_quiesce"
        or type(value.get("phase")) is not str
        or value.get("phase") not in QUARANTINE_PHASES
    ):
        raise TransactionError("quarantine receipt schema is invalid")
    string_fields = (
        "failed_deploy_run_id",
        "failed_deploy_run_attempt",
        "failed_deploy_source_sha",
        "recovery_run_id",
        "recovery_run_attempt",
        "recovery_bundle_sha",
        "pending_sha256",
        "candidate_slug",
        "candidate_tree_sha256",
        "quarantine_slug",
    )
    if any(type(value.get(key)) is not str for key in string_fields):
        raise TransactionError("quarantine receipt value types are invalid")
    try:
        _quarantine_values(
            failed_deploy_run_id=cast(str, value["failed_deploy_run_id"]),
            failed_deploy_run_attempt=cast(str, value["failed_deploy_run_attempt"]),
            failed_deploy_source_sha=cast(str, value["failed_deploy_source_sha"]),
            recovery_run_id=cast(str, value["recovery_run_id"]),
            recovery_run_attempt=cast(str, value["recovery_run_attempt"]),
            bundle_sha=cast(str, value["recovery_bundle_sha"]),
        )
    except TransactionError as exc:
        raise TransactionError("quarantine receipt binding is invalid") from exc
    for key in ("pending_sha256", "candidate_tree_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", cast(str, value[key])) is None:
            raise TransactionError("quarantine receipt digest is invalid")
    if (
        value["candidate_slug"]
        != f"gha-{value['failed_deploy_run_id']}-{value['failed_deploy_run_attempt']}-{cast(str, value['failed_deploy_source_sha'])[:12]}"
        or value["quarantine_slug"] != value["candidate_slug"]
    ):
        raise TransactionError("quarantine receipt slug is invalid")
    for key in (
        "candidate_dev",
        "candidate_ino",
        "candidate_file_count",
        "candidate_directory_count",
        "candidate_bytes",
        "quarantine_dev",
        "quarantine_ino",
    ):
        if type(value.get(key)) is not int or value[key] < 0:
            raise TransactionError("quarantine receipt numeric values are invalid")
    if value["candidate_ino"] <= 0 or value["quarantine_ino"] <= 0:
        raise TransactionError("quarantine receipt inode is invalid")
    service_state = value.get("service_state_before")
    service_enabled = value.get("service_enabled_before")
    if (
        not isinstance(service_state, dict)
        or set(service_state) != set(SERVICE_UNITS)
        or any(type(item) is not str or item not in {"active", "inactive"} for item in service_state.values())
        or not isinstance(service_enabled, dict)
        or set(service_enabled) != set(SERVICE_UNITS)
        or any(type(item) is not str or item not in {"enabled", "disabled"} for item in service_enabled.values())
        or type(value.get("timer_active_before")) is not bool
        or type(value.get("timer_enabled_before")) is not str
        or value["timer_enabled_before"] not in {"enabled", "disabled"}
    ):
        raise TransactionError("quarantine service snapshot is invalid")
    for key in ("current_before", "previous_before"):
        identity = value.get(key)
        if identity is not None and (
            not isinstance(identity, dict)
            or set(identity) != {"slug", "dev", "ino", "source_sha"}
            or type(identity.get("slug")) is not str
            or type(identity.get("dev")) is not int
            or type(identity.get("ino")) is not int
            or type(identity.get("source_sha")) is not str
            or re.fullmatch(r"[0-9a-f]{40}", identity["source_sha"]) is None
            or identity["dev"] < 0
            or identity["ino"] <= 0
        ):
            raise TransactionError("quarantine release pointer identity is invalid")
    return value


def _write_quarantine_receipt(path: Path, receipt: dict[str, object], *, creating: bool) -> None:
    if set(receipt) != QUARANTINE_RECEIPT_KEYS:
        raise TransactionError("quarantine receipt fields are invalid")
    root = path.parent
    _safe_directory(root, label="recovery quarantine directory")
    if creating and _lexists(path):
        raise TransactionError("quarantine receipt already exists")
    temporary = root / f".{path.name}.{uuid4().hex}.tmp"
    raw = (json.dumps(receipt, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
        os.fsync(descriptor)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    if creating:
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
    else:
        prior = _read_quarantine_receipt(path)
        compare = dict(receipt)
        compare["phase"] = prior["phase"]
        compare["recovery_run_attempt"] = prior["recovery_run_attempt"]
        allowed = {
            "prepared": "quarantined",
            "quarantined": "services_restored",
            "services_restored": "receipt_consumed",
        }
        if (
            prior != compare
            or int(cast(str, receipt["recovery_run_attempt"]))
            < int(cast(str, prior["recovery_run_attempt"]))
            or (
                receipt["phase"] != prior["phase"]
                and allowed.get(cast(str, prior["phase"])) != receipt["phase"]
            )
        ):
            raise TransactionError("quarantine receipt binding or phase changed")
        os.replace(temporary, path)
    temporary.unlink(missing_ok=True)
    _fsync_directory(root)


def _quarantine_release_identity(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    meta = _safe_directory(path, label="protected release")
    release = path / "RELEASE.json"
    descriptor = -1
    try:
        release_meta = release.lstat()
        if (
            stat.S_ISLNK(release_meta.st_mode)
            or not stat.S_ISREG(release_meta.st_mode)
            or release_meta.st_uid != 0
            or release_meta.st_nlink != 1
            or release_meta.st_size > 64 * 1024
        ):
            raise TransactionError("protected release metadata is unsafe")
        descriptor = os.open(
            release,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity(opened) != _identity(release_meta)
            or opened.st_uid != 0
            or opened.st_nlink != 1
            or opened.st_size != release_meta.st_size
        ):
            raise TransactionError("protected release metadata changed")
        chunks = []
        while chunk := os.read(descriptor, 8192):
            chunks.append(chunk)
            if sum(map(len, chunks)) > 64 * 1024:
                raise TransactionError("protected release metadata is too large")
        after = os.fstat(descriptor)
        if _identity(after) != _identity(opened) or after.st_size != opened.st_size:
            raise TransactionError("protected release metadata changed")
        raw = b"".join(chunks)
        payload = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except TransactionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("protected release metadata is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    source = payload.get("source_git_commit") if isinstance(payload, dict) else None
    if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        raise TransactionError("protected release source identity is invalid")
    return {"slug": path.name, "dev": meta.st_dev, "ino": meta.st_ino, "source_sha": source}


def _assert_quarantine_pointers(
    app: Path, receipt: dict[str, object]
) -> None:
    actual = {
        "current_before": _quarantine_release_identity(_read_pointer(app, "current")),
        "previous_before": _quarantine_release_identity(_read_pointer(app, "previous")),
    }
    if actual != {key: receipt[key] for key in actual}:
        raise TransactionError("release pointers changed during quarantine recovery")


def _accept_quarantine_recovery_attempt(
    path: Path,
    receipt: dict[str, object],
    binding: dict[str, str],
) -> dict[str, object]:
    for key, value in binding.items():
        if key != "recovery_run_attempt" and receipt.get(key) != value:
            raise TransactionError("quarantine recovery binding changed")
    if receipt.get("recovery_run_id") != binding["recovery_run_id"]:
        raise TransactionError("quarantine recovery run changed")
    incoming = int(binding["recovery_run_attempt"])
    prior = int(cast(str, receipt["recovery_run_attempt"]))
    if incoming < prior:
        raise TransactionError("quarantine recovery attempt moved backwards")
    if incoming > prior:
        receipt["recovery_run_attempt"] = binding["recovery_run_attempt"]
        _write_quarantine_receipt(path, receipt, creating=False)
    return receipt


def _assert_quarantine_tree(root: Path, receipt: dict[str, object]) -> None:
    candidate = root / cast(str, receipt["quarantine_slug"])
    metadata = _safe_directory(candidate, label="quarantined candidate")
    tree_sha, file_count, directory_count, byte_count = _quarantine_tree_snapshot(candidate)
    if (
        metadata.st_dev != receipt["quarantine_dev"]
        or metadata.st_ino != receipt["quarantine_ino"]
        or tree_sha != receipt["candidate_tree_sha256"]
        or file_count != receipt["candidate_file_count"]
        or directory_count != receipt["candidate_directory_count"]
        or byte_count != receipt["candidate_bytes"]
    ):
        raise TransactionError("quarantined candidate evidence changed")


def _quarantine_prune_intent_path(root: Path, candidate_slug: str) -> Path:
    return root / f"{candidate_slug}.prune.json"


def _quarantine_prune_stage_name(candidate_slug: str, cleanup_run_id: str) -> str:
    return f".{candidate_slug}.prune-{cleanup_run_id}"


def _read_quarantine_prune_intent(path: Path) -> dict[str, object]:
    raw = _read_private_state_bytes(path, label="quarantine prune intent")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TransactionError("quarantine prune intent is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != QUARANTINE_PRUNE_INTENT_KEYS
        or type(value.get("schema")) is not int
        or value.get("schema") != QUARANTINE_PRUNE_INTENT_SCHEMA
        or type(value.get("action")) is not str
        or value.get("action") != "prune_quarantined_candidate"
        or type(value.get("phase")) is not str
        or value.get("phase") not in QUARANTINE_PRUNE_PHASES
    ):
        raise TransactionError("quarantine prune intent schema is invalid")
    try:
        _quarantine_values(
            failed_deploy_run_id=cast(str, value["failed_deploy_run_id"]),
            failed_deploy_run_attempt=cast(str, value["failed_deploy_run_attempt"]),
            failed_deploy_source_sha=cast(str, value["failed_deploy_source_sha"]),
            recovery_run_id=cast(str, value["original_recovery_run_id"]),
            recovery_run_attempt=cast(str, value["original_recovery_run_attempt"]),
            bundle_sha=cast(str, value["original_recovery_bundle_sha"]),
        )
    except (KeyError, TransactionError) as exc:
        raise TransactionError("quarantine prune intent F binding is invalid") from exc
    for key in ("cleanup_run_id", "cleanup_run_attempt"):
        if (
            type(value.get(key)) is not str
            or re.fullmatch(r"[1-9][0-9]{0,31}", cast(str, value[key])) is None
        ):
            raise TransactionError("quarantine prune intent U run binding is invalid")
    for key in ("cleanup_source_sha",):
        if type(value.get(key)) is not str or re.fullmatch(r"[0-9a-f]{40}", cast(str, value[key])) is None:
            raise TransactionError("quarantine prune intent U source is invalid")
    for key in (
        "original_receipt_sha256",
        "cleanup_bundle_sha",
        "candidate_tree_sha256",
    ):
        if type(value.get(key)) is not str or re.fullmatch(r"[0-9a-f]{64}", cast(str, value[key])) is None:
            raise TransactionError("quarantine prune intent digest is invalid")
    expected_slug = (
        f"gha-{value['failed_deploy_run_id']}-{value['failed_deploy_run_attempt']}-"
        f"{cast(str, value['failed_deploy_source_sha'])[:12]}"
    )
    if (
        value.get("candidate_slug") != expected_slug
        or value.get("stage_name")
        != _quarantine_prune_stage_name(expected_slug, cast(str, value["cleanup_run_id"]))
    ):
        raise TransactionError("quarantine prune intent candidate path is invalid")
    for key in (
        "candidate_dev",
        "candidate_ino",
        "quarantine_dev",
        "quarantine_ino",
        "candidate_file_count",
        "candidate_directory_count",
        "candidate_bytes",
    ):
        if type(value.get(key)) is not int or value[key] < 0:
            raise TransactionError("quarantine prune intent tree identity is invalid")
    if value["candidate_ino"] <= 0 or value["quarantine_ino"] <= 0:
        raise TransactionError("quarantine prune intent inode is invalid")
    for key in ("current_before", "previous_before"):
        identity = value.get(key)
        if identity is not None and (
            not isinstance(identity, dict)
            or set(identity) != {"slug", "dev", "ino", "source_sha"}
            or type(identity.get("slug")) is not str
            or type(identity.get("dev")) is not int
            or type(identity.get("ino")) is not int
            or identity["dev"] < 0
            or identity["ino"] <= 0
            or type(identity.get("source_sha")) is not str
            or re.fullmatch(r"[0-9a-f]{40}", identity["source_sha"]) is None
        ):
            raise TransactionError("quarantine prune intent pointer binding is invalid")
    return value


def _write_quarantine_prune_intent(
    path: Path, intent: dict[str, object], *, creating: bool
) -> None:
    if set(intent) != QUARANTINE_PRUNE_INTENT_KEYS:
        raise TransactionError("quarantine prune intent fields are invalid")
    root = path.parent
    _safe_directory(root, label="recovery quarantine directory")
    if creating and _lexists(path):
        raise TransactionError("quarantine prune intent already exists")
    temporary = root / f".{path.name}.{uuid4().hex}.tmp"
    raw = (
        json.dumps(intent, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
        os.fsync(descriptor)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    try:
        if creating:
            os.link(temporary, path, follow_symlinks=False)
        else:
            prior = _read_quarantine_prune_intent(path)
            compare = dict(intent)
            compare["phase"] = prior["phase"]
            compare["cleanup_run_attempt"] = prior["cleanup_run_attempt"]
            allowed = {"planned": "deleting", "deleting": "deleted"}
            if (
                prior != compare
                or int(cast(str, intent["cleanup_run_attempt"]))
                < int(cast(str, prior["cleanup_run_attempt"]))
                or (
                    intent["phase"] != prior["phase"]
                    and allowed.get(cast(str, prior["phase"])) != intent["phase"]
                )
            ):
                raise TransactionError("quarantine prune intent binding or phase changed")
            os.replace(temporary, path)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def _quarantine_prune_identity_matches(
    path: Path, intent: dict[str, object]
) -> os.stat_result:
    metadata = _safe_directory(path, label="quarantine prune stage")
    if (
        metadata.st_dev != intent["quarantine_dev"]
        or metadata.st_ino != intent["quarantine_ino"]
    ):
        raise TransactionError("quarantine prune stage identity changed")
    return metadata


def _quarantine_prune_snapshot_matches(
    path: Path, intent: dict[str, object]
) -> None:
    metadata = _quarantine_prune_identity_matches(path, intent)
    _quarantine_assert_no_mounts(path)
    tree = _quarantine_tree_snapshot(path)
    if (
        metadata.st_dev != intent["candidate_dev"]
        or metadata.st_ino != intent["candidate_ino"]
        or tree
        != (
            intent["candidate_tree_sha256"],
            intent["candidate_file_count"],
            intent["candidate_directory_count"],
            intent["candidate_bytes"],
        )
    ):
        raise TransactionError("quarantine prune tree differs from consumed receipt")


def _quarantine_prune_validate_remaining_tree(
    path: Path, intent: dict[str, object]
) -> None:
    """Validate a possibly partially deleted, intent-bound staging inode."""
    _quarantine_prune_identity_matches(path, intent)
    _quarantine_assert_no_mounts(path)
    _tree_sha, file_count, directory_count, total_bytes = _quarantine_tree_snapshot(path)
    if (
        file_count > intent["candidate_file_count"]
        or directory_count > intent["candidate_directory_count"]
        or total_bytes > intent["candidate_bytes"]
    ):
        raise TransactionError("quarantine prune retry tree exceeds its bound intent")


def _quarantine_prune_delete_stage(
    root: Path, stage: Path, intent: dict[str, object]
) -> None:
    """Delete only the bound staged inode, with no-follow dirfd traversal."""
    _quarantine_prune_validate_remaining_tree(stage, intent)
    parent_fd = -1
    stage_fd = -1
    try:
        root_metadata = _safe_directory(root, label="recovery quarantine directory")
        parent_fd = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened_root = os.fstat(parent_fd)
        if _identity(opened_root) != _identity(root_metadata):
            raise TransactionError("recovery quarantine directory changed")
        staged_metadata = os.stat(stage.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(staged_metadata.st_mode)
            or staged_metadata.st_dev != intent["quarantine_dev"]
            or staged_metadata.st_ino != intent["quarantine_ino"]
        ):
            raise TransactionError("quarantine prune stage identity changed")
        stage_fd = os.open(
            stage.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened_stage = os.fstat(stage_fd)
        if (
            not stat.S_ISDIR(opened_stage.st_mode)
            or opened_stage.st_dev != intent["quarantine_dev"]
            or opened_stage.st_ino != intent["quarantine_ino"]
            or opened_stage.st_uid != 0
            or opened_stage.st_gid != 0
        ):
            raise TransactionError("quarantine prune stage changed before traversal")
        for walk_root, directories, files, directory_fd in os.fwalk(
            ".",
            topdown=False,
            follow_symlinks=False,
            dir_fd=stage_fd,
        ):
            current = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(current.st_mode)
                or current.st_dev != intent["quarantine_dev"]
                or current.st_uid != 0
                or current.st_gid != 0
                or (
                    walk_root == "."
                    and current.st_ino != intent["quarantine_ino"]
                )
            ):
                raise TransactionError("quarantine prune directory changed")
            os.fchmod(directory_fd, stat.S_IMODE(current.st_mode) | 0o700)
            for filename in files:
                try:
                    entry = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISREG(entry.st_mode)
                        or entry.st_dev != intent["quarantine_dev"]
                        or entry.st_uid != 0
                        or entry.st_gid != 0
                        or entry.st_nlink != 1
                    ):
                        raise TransactionError("quarantine prune file is unsafe")
                    file_fd = os.open(
                        filename,
                        os.O_RDONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        opened = os.fstat(file_fd)
                        if (
                            not stat.S_ISREG(opened.st_mode)
                            or _identity(opened) != _identity(entry)
                            or opened.st_dev != intent["quarantine_dev"]
                            or opened.st_uid != 0
                            or opened.st_gid != 0
                            or opened.st_nlink != 1
                        ):
                            raise TransactionError("quarantine prune file changed")
                    finally:
                        os.close(file_fd)
                    os.unlink(filename, dir_fd=directory_fd)
                except OSError as exc:
                    raise TransactionError("quarantine prune file could not be removed") from exc
            for dirname in directories:
                try:
                    entry = os.stat(dirname, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(entry.st_mode)
                        or entry.st_dev != intent["quarantine_dev"]
                        or entry.st_uid != 0
                        or entry.st_gid != 0
                        or entry.st_nlink < 2
                    ):
                        raise TransactionError("quarantine prune directory entry is unsafe")
                    os.rmdir(dirname, dir_fd=directory_fd)
                except OSError as exc:
                    raise TransactionError("quarantine prune directory could not be removed") from exc
            os.fsync(directory_fd)
        current_root = os.stat(stage.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_root.st_mode)
            or current_root.st_ino != intent["quarantine_ino"]
            or current_root.st_dev != intent["quarantine_dev"]
        ):
            raise TransactionError("quarantine prune root changed before removal")
        os.rmdir(stage.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        os.close(stage_fd)
        stage_fd = -1
    except TransactionError:
        raise
    except OSError as exc:
        raise TransactionError("quarantine prune staged tree could not be removed") from exc
    finally:
        if stage_fd >= 0:
            os.close(stage_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def prune_quarantined_candidate(
    state: Path,
    *,
    failed_deploy_run_id: str,
    failed_deploy_run_attempt: str,
    failed_deploy_source_sha: str,
    cleanup_run_id: str,
    cleanup_run_attempt: str,
    cleanup_source_sha: str,
    bundle_sha: str,
) -> dict[str, object]:
    """Remove one receipt-consumed candidate using a separate U-bound intent."""
    if (
        not isinstance(failed_deploy_run_id, str)
        or re.fullmatch(r"[1-9][0-9]{0,31}", failed_deploy_run_id) is None
        or not isinstance(failed_deploy_run_attempt, str)
        or re.fullmatch(r"[1-9][0-9]{0,31}", failed_deploy_run_attempt) is None
        or not isinstance(failed_deploy_source_sha, str)
        or re.fullmatch(r"[0-9a-f]{40}", failed_deploy_source_sha) is None
    ):
        raise TransactionError("failed deployment F binding is invalid")
    original_binding = {
        "failed_deploy_run_id": failed_deploy_run_id,
        "failed_deploy_run_attempt": failed_deploy_run_attempt,
        "failed_deploy_source_sha": failed_deploy_source_sha,
    }
    for value, label in (
        (cleanup_run_id, "cleanup run id"),
        (cleanup_run_attempt, "cleanup run attempt"),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
            raise TransactionError(f"{label} is invalid")
    if re.fullmatch(r"[0-9a-f]{40}", cleanup_source_sha) is None:
        raise TransactionError("cleanup source SHA is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", bundle_sha) is None:
        raise TransactionError("cleanup bundle SHA is invalid")

    shared = state.parent
    app = shared.parent
    releases = app / "releases"
    if state != shared / STATE_NAME:
        raise TransactionError("quarantine prune state path is invalid")
    if any(
        _lexists(shared / name)
        for name in (
            STATE_NAME,
            QUIESCE_STATE_NAME,
            SYSTEMD_STATE_NAME,
            ".migration-pending.json",
            ".migration-state.json",
        )
    ):
        raise TransactionError("quarantine prune has a pending release sidecar")
    _safe_directory(app, label="platform application directory")
    _safe_directory(releases, label="platform releases directory")
    root = _quarantine_root(shared, releases)
    candidate_slug = (
        f"gha-{failed_deploy_run_id}-{failed_deploy_run_attempt}-"
        f"{failed_deploy_source_sha[:12]}"
    )
    receipt_path = _quarantine_receipt_path(root, candidate_slug)
    receipt_raw = _read_private_state_bytes(receipt_path, label="quarantine receipt")
    receipt = _read_quarantine_receipt(receipt_path, raw=receipt_raw)
    if (
        receipt["phase"] != "receipt_consumed"
        or receipt["failed_deploy_run_id"] != original_binding["failed_deploy_run_id"]
        or receipt["failed_deploy_run_attempt"] != original_binding["failed_deploy_run_attempt"]
        or receipt["failed_deploy_source_sha"] != original_binding["failed_deploy_source_sha"]
        or receipt["candidate_slug"] != candidate_slug
        or receipt["quarantine_slug"] != candidate_slug
    ):
        raise TransactionError("quarantine receipt is not an exact consumed F receipt")
    current_identity = _quarantine_release_identity(_read_pointer(app, "current"))
    previous_identity = _quarantine_release_identity(_read_pointer(app, "previous"))
    if (
        current_identity != receipt["current_before"]
        or previous_identity != receipt["previous_before"]
    ):
        raise TransactionError("release pointers changed since quarantine receipt")
    for identity in (current_identity, previous_identity):
        if identity is not None:
            protected_release = releases / cast(str, identity["slug"])
            try:
                resolved_release = protected_release.resolve(strict=True)
            except OSError as exc:
                raise TransactionError("quarantine prune protected pointer is unavailable") from exc
            if resolved_release.parent != releases or resolved_release != protected_release:
                raise TransactionError("quarantine prune protected pointer is outside releases")

    original_receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
    intent_path = _quarantine_prune_intent_path(root, candidate_slug)
    candidate = root / candidate_slug
    stage_name = _quarantine_prune_stage_name(candidate_slug, cleanup_run_id)
    stage = root / stage_name
    if _lexists(intent_path):
        intent = _read_quarantine_prune_intent(intent_path)
        for key, expected in {
            "failed_deploy_run_id": failed_deploy_run_id,
            "failed_deploy_run_attempt": failed_deploy_run_attempt,
            "failed_deploy_source_sha": failed_deploy_source_sha,
            "candidate_slug": candidate_slug,
            "original_receipt_sha256": original_receipt_sha,
            "original_recovery_run_id": receipt["recovery_run_id"],
            "original_recovery_run_attempt": receipt["recovery_run_attempt"],
            "original_recovery_bundle_sha": receipt["recovery_bundle_sha"],
            "cleanup_run_id": cleanup_run_id,
            "cleanup_source_sha": cleanup_source_sha,
            "cleanup_bundle_sha": bundle_sha,
            "candidate_dev": receipt["candidate_dev"],
            "candidate_ino": receipt["candidate_ino"],
            "quarantine_dev": receipt["quarantine_dev"],
            "quarantine_ino": receipt["quarantine_ino"],
            "candidate_tree_sha256": receipt["candidate_tree_sha256"],
            "candidate_file_count": receipt["candidate_file_count"],
            "candidate_directory_count": receipt["candidate_directory_count"],
            "candidate_bytes": receipt["candidate_bytes"],
            "current_before": current_identity,
            "previous_before": previous_identity,
            "stage_name": stage_name,
        }.items():
            if intent.get(key) != expected:
                raise TransactionError("quarantine prune U/F intent binding changed")
        if int(cleanup_run_attempt) < int(cast(str, intent["cleanup_run_attempt"])):
            raise TransactionError("quarantine prune run attempt moved backwards")
        if cleanup_run_attempt != intent["cleanup_run_attempt"] and intent["phase"] != "deleted":
            intent["cleanup_run_attempt"] = cleanup_run_attempt
            _write_quarantine_prune_intent(intent_path, intent, creating=False)
    else:
        if _lexists(candidate) is False or _lexists(stage):
            raise TransactionError("quarantine prune candidate state is ambiguous")
        _quarantine_assert_no_mounts(candidate)
        candidate_meta = _safe_directory(candidate, label="quarantined candidate")
        tree = _quarantine_tree_snapshot(candidate)
        expected_tree = (
            receipt["candidate_tree_sha256"],
            receipt["candidate_file_count"],
            receipt["candidate_directory_count"],
            receipt["candidate_bytes"],
        )
        if (
            candidate_meta.st_dev != receipt["quarantine_dev"]
            or candidate_meta.st_ino != receipt["quarantine_ino"]
            or candidate_meta.st_dev != receipt["candidate_dev"]
            or candidate_meta.st_ino != receipt["candidate_ino"]
            or tree != expected_tree
        ):
            raise TransactionError("quarantine prune candidate differs from consumed receipt")
        intent = {
            "schema": QUARANTINE_PRUNE_INTENT_SCHEMA,
            "action": "prune_quarantined_candidate",
            "phase": "planned",
            "failed_deploy_run_id": failed_deploy_run_id,
            "failed_deploy_run_attempt": failed_deploy_run_attempt,
            "failed_deploy_source_sha": failed_deploy_source_sha,
            "candidate_slug": candidate_slug,
            "original_receipt_sha256": original_receipt_sha,
            "original_recovery_run_id": receipt["recovery_run_id"],
            "original_recovery_run_attempt": receipt["recovery_run_attempt"],
            "original_recovery_bundle_sha": receipt["recovery_bundle_sha"],
            "cleanup_run_id": cleanup_run_id,
            "cleanup_run_attempt": cleanup_run_attempt,
            "cleanup_source_sha": cleanup_source_sha,
            "cleanup_bundle_sha": bundle_sha,
            "candidate_dev": candidate_meta.st_dev,
            "candidate_ino": candidate_meta.st_ino,
            "quarantine_dev": receipt["quarantine_dev"],
            "quarantine_ino": receipt["quarantine_ino"],
            "candidate_tree_sha256": tree[0],
            "candidate_file_count": tree[1],
            "candidate_directory_count": tree[2],
            "candidate_bytes": tree[3],
            "current_before": current_identity,
            "previous_before": previous_identity,
            "stage_name": stage_name,
        }
        _write_quarantine_prune_intent(intent_path, intent, creating=True)

    if intent["phase"] == "deleted":
        if _lexists(candidate) or _lexists(stage):
            raise TransactionError("deleted quarantine prune target reappeared")
        if hashlib.sha256(_read_private_state_bytes(receipt_path, label="quarantine receipt")).hexdigest() != original_receipt_sha:
            raise TransactionError("original quarantine receipt changed after pruning")
        _assert_quarantine_pointers(app, receipt)
        return intent

    if intent["phase"] == "planned":
        if _lexists(candidate) and not _lexists(stage):
            _quarantine_prune_snapshot_matches(candidate, intent)
            _rename_noreplace(candidate, stage)
            _fsync_directory(root)
        elif _lexists(stage) and not _lexists(candidate):
            _quarantine_prune_snapshot_matches(stage, intent)
        else:
            raise TransactionError("planned quarantine prune staging state is ambiguous")
        _quarantine_prune_snapshot_matches(stage, intent)
        if (
            hashlib.sha256(
                _read_private_state_bytes(receipt_path, label="quarantine receipt")
            ).hexdigest()
            != original_receipt_sha
        ):
            raise TransactionError("original quarantine receipt changed before pruning")
        _assert_quarantine_pointers(app, receipt)
        intent["phase"] = "deleting"
        _write_quarantine_prune_intent(intent_path, intent, creating=False)

    if intent["phase"] != "deleting":
        raise TransactionError("quarantine prune intent phase is invalid")
    if _lexists(candidate):
        raise TransactionError("quarantine prune candidate path reappeared")
    if (
        hashlib.sha256(
            _read_private_state_bytes(receipt_path, label="quarantine receipt")
        ).hexdigest()
        != original_receipt_sha
    ):
        raise TransactionError("original quarantine receipt changed before deletion")
    _assert_quarantine_pointers(app, receipt)
    if _lexists(stage):
        _quarantine_prune_validate_remaining_tree(stage, intent)
        _quarantine_prune_delete_stage(root, stage, intent)
    if _lexists(candidate) or _lexists(stage):
        raise TransactionError("quarantine prune target remains after deletion")
    if hashlib.sha256(_read_private_state_bytes(receipt_path, label="quarantine receipt")).hexdigest() != original_receipt_sha:
        raise TransactionError("original quarantine receipt changed during pruning")
    _assert_quarantine_pointers(app, receipt)
    intent["phase"] = "deleted"
    _write_quarantine_prune_intent(intent_path, intent, creating=False)
    return intent


def quarantine_populated_quiesce(
    state: Path,
    *,
    failed_deploy_run_id: str,
    failed_deploy_run_attempt: str,
    failed_deploy_source_sha: str,
    recovery_run_id: str,
    recovery_run_attempt: str,
    bundle_sha: str,
) -> dict[str, object]:
    """Preserve, but never execute, one exact populated pre-promotion candidate."""
    binding = _quarantine_values(
        failed_deploy_run_id=failed_deploy_run_id,
        failed_deploy_run_attempt=failed_deploy_run_attempt,
        failed_deploy_source_sha=failed_deploy_source_sha,
        recovery_run_id=recovery_run_id,
        recovery_run_attempt=recovery_run_attempt,
        bundle_sha=bundle_sha,
    )
    qrecord = _load_quiesce_record(state)
    app = cast(Path, qrecord["app"])
    shared = cast(Path, qrecord["shared"])
    releases = cast(Path, qrecord["releases"])
    if state != shared / STATE_NAME or qrecord.get("phase") != "quiesce-pending":
        raise TransactionError("quarantine requires an operationless install quiesce receipt")
    if qrecord.get("operation_id") is not None or qrecord.get("systemd_state_before") is not None:
        raise TransactionError("quarantine receipt is not operationless")
    if any(
        _lexists(shared / name)
        for name in (
            QUIESCE_STATE_NAME,
            SYSTEMD_STATE_NAME,
            ".migration-pending.json",
            ".migration-state.json",
        )
    ):
        raise TransactionError("quarantine transaction has an ambiguous sidecar")
    candidate = cast(Path, qrecord["candidate_path"])
    expected_slug = (
        f"gha-{failed_deploy_run_id}-{failed_deploy_run_attempt}-"
        f"{failed_deploy_source_sha[:12]}"
    )
    if candidate.name != expected_slug:
        raise TransactionError("candidate slug does not match failed deployment")
    pending_info = _safe_state_file(state)
    pending_raw = state.read_bytes()
    if len(pending_raw) != pending_info.st_size:
        raise TransactionError("pending receipt changed during quarantine")
    pending_sha = hashlib.sha256(pending_raw).hexdigest()
    current_tuple = _quarantine_release_identity(cast(Path | None, qrecord["current_before_path"]))
    previous_tuple = _quarantine_release_identity(cast(Path | None, qrecord["previous_before_path"]))
    _assert_quarantine_pointers(
        app,
        {
            "current_before": current_tuple,
            "previous_before": previous_tuple,
        },
    )
    quarantine_path = shared / QUARANTINE_DIR_NAME
    if not _lexists(candidate):
        # A retry after the atomic rename must already have its durable
        # quarantine root. Do not create state while validating an absent or
        # mismatched candidate.
        root = _quarantine_root(shared, releases)
        receipt_path = _quarantine_receipt_path(root, expected_slug)
        destination = root / expected_slug
    else:
        root = quarantine_path
        receipt_path = _quarantine_receipt_path(root, expected_slug)
        destination = root / expected_slug
    if not _lexists(candidate) and _lexists(receipt_path):
        receipt = _read_quarantine_receipt(receipt_path)
        receipt = _accept_quarantine_recovery_attempt(receipt_path, receipt, binding)
        if (
            receipt.get("pending_sha256") != pending_sha
            or receipt.get("candidate_slug") != expected_slug
            or receipt.get("current_before") != current_tuple
            or receipt.get("previous_before") != previous_tuple
            or _lexists(candidate)
            or not _lexists(destination)
        ):
            raise TransactionError("existing quarantine intent is not resumable")
        _assert_quarantine_tree(root, receipt)
        _assert_quarantine_pointers(app, receipt)
        if receipt["phase"] == "prepared":
            receipt["phase"] = "quarantined"
            _write_quarantine_receipt(receipt_path, receipt, creating=False)
        elif receipt["phase"] not in {"quarantined", "services_restored"}:
            raise TransactionError("existing quarantine phase is not resumable")
        return receipt
    candidate_meta = _safe_directory(candidate, label="candidate release")
    candidate_tuple = _quarantine_release_identity(candidate)
    if candidate_tuple is None or candidate_tuple["source_sha"] != failed_deploy_source_sha:
        raise TransactionError("candidate source does not match failed deployment")
    tree_sha, file_count, directory_count, byte_count = _quarantine_tree_snapshot(candidate)
    # Validate the populated candidate completely before creating the
    # quarantine root; malformed candidates must not leave filesystem state.
    root = _quarantine_root(shared, releases)
    receipt_path = _quarantine_receipt_path(root, expected_slug)
    destination = root / expected_slug
    qslug = expected_slug
    expected = {
        "schema": QUARANTINE_RECEIPT_SCHEMA,
        "action": "quarantine_populated_quiesce",
        "phase": "prepared",
        **binding,
        "pending_sha256": pending_sha,
        "candidate_slug": expected_slug,
        "candidate_dev": candidate_meta.st_dev,
        "candidate_ino": candidate_meta.st_ino,
        "candidate_tree_sha256": tree_sha,
        "candidate_file_count": file_count,
        "candidate_directory_count": directory_count,
        "candidate_bytes": byte_count,
        "current_before": current_tuple,
        "previous_before": previous_tuple,
        "service_state_before": qrecord["service_state_before"],
        "service_enabled_before": qrecord["service_enabled_before"],
        "timer_active_before": qrecord["timer_active_before"],
        "timer_enabled_before": qrecord["timer_enabled_before"],
        "quarantine_slug": qslug,
        "quarantine_dev": root.stat().st_dev,
        "quarantine_ino": candidate_meta.st_ino,
    }
    if _lexists(receipt_path):
        receipt = _read_quarantine_receipt(receipt_path)
        receipt = _accept_quarantine_recovery_attempt(receipt_path, receipt, binding)
        compare = dict(expected)
        compare["phase"] = receipt["phase"]
        if receipt != compare:
            raise TransactionError("existing quarantine intent does not match exact recovery")
    else:
        if _lexists(destination):
            raise TransactionError("quarantine destination already exists without intent")
        _write_quarantine_receipt(receipt_path, expected, creating=True)
        receipt = expected
    if receipt["phase"] == "prepared":
        if _lexists(candidate) and not _lexists(destination):
            current_meta = _safe_directory(candidate, label="candidate release")
            current_tree = _quarantine_tree_snapshot(candidate)
            if _identity(current_meta) != {"dev": receipt["candidate_dev"], "ino": receipt["candidate_ino"]} or current_tree != (tree_sha, file_count, directory_count, byte_count):
                raise TransactionError("candidate changed after quarantine intent")
            _rename_noreplace(candidate, destination)
        elif _lexists(candidate) or not _lexists(destination):
            raise TransactionError("candidate quarantine recovery state is ambiguous")
        _assert_quarantine_tree(root, receipt)
        moved = _safe_directory(destination, label="quarantined candidate")
        receipt["quarantine_dev"] = moved.st_dev
        receipt["quarantine_ino"] = moved.st_ino
        receipt["phase"] = "quarantined"
        _write_quarantine_receipt(receipt_path, receipt, creating=False)
    else:
        _assert_quarantine_tree(root, receipt)
    _assert_quarantine_pointers(app, receipt)
    return receipt


def mark_quarantine_services_restored(
    state: Path,
    *,
    failed_deploy_run_id: str,
    failed_deploy_run_attempt: str,
    failed_deploy_source_sha: str,
    recovery_run_id: str,
    recovery_run_attempt: str,
    bundle_sha: str,
) -> dict[str, object]:
    binding = _quarantine_values(
        failed_deploy_run_id=failed_deploy_run_id,
        failed_deploy_run_attempt=failed_deploy_run_attempt,
        failed_deploy_source_sha=failed_deploy_source_sha,
        recovery_run_id=recovery_run_id,
        recovery_run_attempt=recovery_run_attempt,
        bundle_sha=bundle_sha,
    )
    qrecord = _load_quiesce_record(state)
    root = _quarantine_root(cast(Path, qrecord["shared"]), cast(Path, qrecord["releases"]))
    slug = f"gha-{failed_deploy_run_id}-{failed_deploy_run_attempt}-{failed_deploy_source_sha[:12]}"
    receipt_path = _quarantine_receipt_path(root, slug)
    receipt = _read_quarantine_receipt(receipt_path)
    receipt = _accept_quarantine_recovery_attempt(receipt_path, receipt, binding)
    if receipt.get("candidate_slug") != slug:
        raise TransactionError("quarantine recovery binding changed")
    if receipt["phase"] not in {"quarantined", "services_restored"}:
        raise TransactionError("quarantine is not ready for service restoration")
    _assert_quarantine_tree(root, receipt)
    _assert_quarantine_pointers(cast(Path, qrecord["app"]), receipt)
    if receipt["phase"] == "quarantined":
        receipt["phase"] = "services_restored"
        _write_quarantine_receipt(receipt_path, receipt, creating=False)
    return receipt


def inspect_quarantined_quiesce(
    state: Path,
    *,
    failed_deploy_run_id: str,
    failed_deploy_run_attempt: str,
    failed_deploy_source_sha: str,
    recovery_run_id: str,
    recovery_run_attempt: str,
    bundle_sha: str,
) -> dict[str, object]:
    binding = _quarantine_values(
        failed_deploy_run_id=failed_deploy_run_id,
        failed_deploy_run_attempt=failed_deploy_run_attempt,
        failed_deploy_source_sha=failed_deploy_source_sha,
        recovery_run_id=recovery_run_id,
        recovery_run_attempt=recovery_run_attempt,
        bundle_sha=bundle_sha,
    )
    shared = state.parent
    root = _quarantine_root(shared, shared.parent / "releases")
    slug = f"gha-{failed_deploy_run_id}-{failed_deploy_run_attempt}-{failed_deploy_source_sha[:12]}"
    receipt_path = _quarantine_receipt_path(root, slug)
    receipt = _read_quarantine_receipt(receipt_path)
    receipt = _accept_quarantine_recovery_attempt(receipt_path, receipt, binding)
    _assert_quarantine_tree(root, receipt)
    _assert_quarantine_pointers(shared.parent, receipt)
    return {
        "phase": receipt["phase"],
        "service_state_before": receipt["service_state_before"],
        "service_enabled_before": receipt["service_enabled_before"],
        "timer_active_before": receipt["timer_active_before"],
        "timer_enabled_before": receipt["timer_enabled_before"],
        "api": cast(dict[str, str], receipt["service_state_before"])["deadlock-api"],
        "web": cast(dict[str, str], receipt["service_state_before"])["deadlock-web"],
    }


def consume_quarantined_quiesce(
    state: Path,
    *,
    failed_deploy_run_id: str,
    failed_deploy_run_attempt: str,
    failed_deploy_source_sha: str,
    recovery_run_id: str,
    recovery_run_attempt: str,
    bundle_sha: str,
) -> dict[str, object]:
    binding = _quarantine_values(
        failed_deploy_run_id=failed_deploy_run_id,
        failed_deploy_run_attempt=failed_deploy_run_attempt,
        failed_deploy_source_sha=failed_deploy_source_sha,
        recovery_run_id=recovery_run_id,
        recovery_run_attempt=recovery_run_attempt,
        bundle_sha=bundle_sha,
    )
    shared = state.parent
    root = _quarantine_root(shared, shared.parent / "releases")
    slug = f"gha-{failed_deploy_run_id}-{failed_deploy_run_attempt}-{failed_deploy_source_sha[:12]}"
    receipt_path = _quarantine_receipt_path(root, slug)
    receipt = _read_quarantine_receipt(receipt_path)
    receipt = _accept_quarantine_recovery_attempt(receipt_path, receipt, binding)
    if receipt.get("candidate_slug") != slug:
        raise TransactionError("quarantine recovery binding changed")
    if receipt["phase"] == "receipt_consumed":
        if _lexists(state):
            raise TransactionError("consumed quarantine still has a pending receipt")
        _assert_quarantine_tree(root, receipt)
        _assert_quarantine_pointers(shared.parent, receipt)
        return receipt
    if receipt["phase"] != "services_restored":
        raise TransactionError("quarantine recovery services are not restored")
    _assert_quarantine_tree(root, receipt)
    if _lexists(state):
        qrecord = _load_quiesce_record(state)
        if qrecord.get("phase") != "quiesce-pending":
            raise TransactionError("pending receipt changed before quarantine completion")
        raw = state.read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["pending_sha256"]:
            raise TransactionError("pending receipt digest changed before quarantine completion")
        _assert_quarantine_pointers(cast(Path, qrecord["app"]), receipt)
        abort_quiesce(state)
    else:
        _assert_quarantine_pointers(shared.parent, receipt)
    if _lexists(state):
        raise TransactionError("pending receipt remains after quarantine completion")
    receipt["phase"] = "receipt_consumed"
    _write_quarantine_receipt(receipt_path, receipt, creating=False)
    return receipt


def _cache_purge_runtime_paths(record: dict[str, object]) -> tuple[Path, Path]:
    """Resolve the transaction-bound source and venv locations for a purge."""
    transition = cast(str, record["transition"])
    if record["operation"] == "rollback":
        # Rollback activates the older ``candidate_path``.  The purge code
        # must instead run from the current release being rolled back, which
        # remains in ``current_before_path`` and whose venv is retained by the
        # transaction snapshot after an exchange.
        source_release = cast(Path | None, record["current_before_path"])
        if source_release is None:
            raise TransactionError("rollback cache purge source is unavailable")
        runtime_identity = record["shared_before"]
    elif record["operation"] == "install":
        source_release = cast(Path, record["candidate_path"])
        if transition in {"exchange", "create"}:
            runtime_identity = record["peer_before"]
        else:
            runtime_identity = record["shared_before"]
    else:
        raise TransactionError("cache purge operation is invalid")
    _safe_directory(source_release, label="cache purge source release")
    if not _valid_identity(runtime_identity):
        raise TransactionError("cache purge candidate venv identity is invalid")
    expected_identity = cast(dict[str, int], runtime_identity)
    locations = _unique_paths(
        cast(Path, record["shared_venv_path"]),
        cast(Path, record["peer_path"]),
        cast(Path, record["snapshot_path"]),
    )
    matches = [path for path in locations if _matches(path, expected_identity)]
    if len(matches) != 1:
        raise TransactionError("cache purge candidate venv location is ambiguous")
    return source_release, matches[0]


def _cache_purge_runtime(record: dict[str, object]) -> tuple[Path, Path]:
    """Resolve the source/venv pair and verify its fixed purge interpreter."""

    source_release, venv = _cache_purge_runtime_paths(record)
    python = venv / "bin/python"
    try:
        python_metadata = python.lstat()
        resolved_python = python.resolve(strict=True)
        resolved_metadata = resolved_python.lstat()
    except OSError as exc:
        raise TransactionError("cache purge candidate interpreter is unavailable") from exc
    if (
        not (stat.S_ISLNK(python_metadata.st_mode) or stat.S_ISREG(python_metadata.st_mode))
        or not stat.S_ISREG(resolved_metadata.st_mode)
        or resolved_metadata.st_uid != 0
        or stat.S_IMODE(resolved_metadata.st_mode) & 0o022
        or not os.access(resolved_python, os.X_OK)
        or resolved_python != Path("/usr/bin/python3.12")
    ):
        raise TransactionError("cache purge candidate interpreter is unsafe")
    return source_release, python


PROFILE_ACCESS_CACHE_PURGE_PROOF_KEYS = {
    "schema",
    "operation_id",
    "operation",
    "source_role",
    "source_identity",
    "venv_identity",
    "scope",
}


def _validate_profile_access_cache_purge_proof_shape(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != PROFILE_ACCESS_CACHE_PURGE_PROOF_KEYS
        or type(value.get("schema")) is not int
        or value.get("schema") != 1
        or not isinstance(value.get("operation_id"), str)
        or OPERATION_ID_PATTERN.fullmatch(cast(str, value["operation_id"])) is None
        or not isinstance(value.get("operation"), str)
        or value.get("operation") not in {"install", "rollback"}
        or not isinstance(value.get("source_role"), str)
        or value.get("source_role") not in {"candidate", "current-before"}
        or not _valid_identity(value.get("source_identity"))
        or not _valid_identity(value.get("venv_identity"))
        or not isinstance(value.get("scope"), str)
        or value.get("scope") != "tournament-profile-access-v1"
    ):
        raise TransactionError("profile cache purge proof is invalid")


def _profile_access_cache_purge_proof_binding(
    record: dict[str, object],
) -> dict[str, object]:
    if not isinstance(record.get("operation_id"), str):
        raise TransactionError("profile cache purge operation identity is unavailable")
    source_release, venv = _cache_purge_runtime_paths(record)
    if record["operation"] == "rollback":
        source_role = "current-before"
        source_identity = record["current_before_identity"]
        venv_identity = record["shared_before"]
    elif record["operation"] == "install":
        source_role = "candidate"
        source_identity = record["candidate_identity"]
        venv_identity = (
            record["peer_before"]
            if record["transition"] in {"exchange", "create"}
            else record["shared_before"]
        )
    else:
        raise TransactionError("profile cache purge operation is invalid")
    if not _valid_identity(source_identity) or not _valid_identity(venv_identity):
        raise TransactionError("profile cache purge identity binding is invalid")
    source_identity = cast(dict[str, int], source_identity)
    venv_identity = cast(dict[str, int], venv_identity)
    source_metadata = _safe_directory(source_release, label="cache purge source release")
    venv_metadata = _safe_directory(venv, label="cache purge candidate venv")
    if (
        _identity(source_metadata) != source_identity
        or _identity(venv_metadata) != venv_identity
    ):
        raise TransactionError("profile cache purge identity binding changed")
    return {
        "schema": 1,
        "operation_id": record["operation_id"],
        "operation": record["operation"],
        "source_role": source_role,
        "source_identity": source_identity,
        "venv_identity": venv_identity,
        "scope": "tournament-profile-access-v1",
    }


def _require_profile_access_cache_purge_proof(record: dict[str, object]) -> None:
    proof = record.get("profile_access_cache_purge_proof")
    _validate_profile_access_cache_purge_proof_shape(proof)
    if proof != _profile_access_cache_purge_proof_binding(record):
        raise TransactionError("profile cache purge proof does not match transaction")


def purge_legacy_profile_access_cache(state: Path) -> None:
    """Purge only source-owned v1 profile ACL keys before old code restarts."""

    record = _load_record(state)
    phase = cast(str, record["phase"])
    if record["operation"] == "rollback":
        if phase != "rollback-runtime-pending":
            raise TransactionError("rollback cache purge is not at its restart boundary")
    elif record["operation"] == "install":
        if phase not in {
            "migration-pending",
            "migration-failed",
            "migration-applied",
            "filesystem-restored-services-pending",
            "recovery-restored",
        }:
            raise TransactionError("install cache purge is not at its restart boundary")
        if record["current_before_path"] is None:
            raise TransactionError("first install has no legacy release to restart")
    else:
        raise TransactionError("cache purge operation is invalid")

    source_release, python = _cache_purge_runtime(record)
    shared = cast(Path, record["shared"])
    env_file = shared / ".env.platform"
    _optional_safe_private_file(env_file, label="cache purge shared environment")
    safe_env_tool = source_release / "tools/platform_safe_env_exec.py"
    try:
        safe_tool_metadata = safe_env_tool.lstat()
    except OSError as exc:
        raise TransactionError("cache purge environment helper is unavailable") from exc
    if (
        not stat.S_ISREG(safe_tool_metadata.st_mode)
        or safe_tool_metadata.st_uid != 0
        or safe_tool_metadata.st_nlink != 1
        or stat.S_IMODE(safe_tool_metadata.st_mode) & 0o022
        or safe_tool_metadata.st_size > 1024 * 1024
    ):
        raise TransactionError("cache purge environment helper is unsafe")
    try:
        exported = subprocess.run(
            [
                "/usr/bin/python3.12",
                "-I",
                str(safe_env_tool),
                "export-b64",
                "--path",
                str(env_file),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/root"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransactionError("cache purge environment export failed") from exc
    if exported.returncode != 0 or len(exported.stdout) > 1024 * 1024:
        raise TransactionError("cache purge environment export failed")
    redis_url: str | None = None
    seen: set[str] = set()
    try:
        for line in exported.stdout.decode("ascii").splitlines():
            key, separator, encoded = line.partition("\t")
            if not separator or key in seen:
                raise ValueError("invalid safe environment export")
            seen.add(key)
            if key == "PLATFORM_REDIS_URL":
                value = base64.b64decode(encoded, validate=True).decode("utf-8")
                if not value or "\x00" in value or "\n" in value or "\r" in value:
                    raise ValueError("invalid Redis setting")
                redis_url = value
    except (UnicodeError, ValueError) as exc:
        raise TransactionError("cache purge environment export is invalid") from exc
    if redis_url is None:
        raise TransactionError("cache purge Redis setting is unavailable")

    child_env = {
        "HOME": "/root",
        "PATH": "/usr/bin:/bin",
        "PLATFORM_REDIS_URL": redis_url,
        "PLATFORM_SHARED_DIR": str(shared),
        "PLATFORM_ENVIRONMENT": "production",
    }
    child = (
        "import asyncio, pathlib, sys; "
        "root = pathlib.Path(sys.argv[1]).resolve(strict=True); "
        "sys.path[:0] = [str(root / 'apps/platform_api'), str(root / 'python_packages'), str(root)]; "
        "from app.services.tournament_profile_access import purge_legacy_profile_access_cache; "
        "result = asyncio.run(purge_legacy_profile_access_cache()); "
        "raise SystemExit(0 if type(result) is int and result >= 0 else 1)"
    )
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", child, str(source_release)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
            close_fds=True,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TransactionError("legacy profile cache purge failed") from exc
    if result.returncode != 0:
        raise TransactionError("legacy profile cache purge failed")
    purge_proof = _profile_access_cache_purge_proof_binding(record)
    latest = _load_record(state)
    if (
        latest["operation_id"] != record["operation_id"]
        or latest["operation"] != record["operation"]
        or latest["phase"] != record["phase"]
        or _profile_access_cache_purge_proof_binding(latest) != purge_proof
        or (
            latest["profile_access_cache_purge_proof"] is not None
            and latest["profile_access_cache_purge_proof"] != purge_proof
        )
    ):
        raise TransactionError("profile cache purge transaction changed")
    if latest["profile_access_cache_purge_proof"] is None:
        latest["profile_access_cache_purge_proof"] = purge_proof
        _write_record(state, _record_for_write(latest), creating=False)


def _validate_protected_activation_recovery(
    state: Path, record: dict[str, object], *, phase: str
) -> None:
    """Accept only the bound 79/9ce/f115 install recovery topology."""
    if phase not in {
        "activation-pending",
        "recovery-authorized",
        "filesystem-restored-services-pending",
    }:
        raise TransactionError("protected activation recovery phase is unsupported")
    candidate = cast(Path, record["candidate_path"])
    current = cast(Path, record["current_before_path"])
    previous = cast(Path, record["previous_before_path"])
    shared = cast(Path, record["shared"])
    current_identity = _quarantine_release_identity(current)
    previous_identity = _quarantine_release_identity(previous)
    candidate_identity = _quarantine_release_identity(candidate)
    if (
        record["operation"] != "install"
        or record["phase"] != phase
        or record["current_before_identity"] is None
        or record["previous_before_identity"] is None
        or record["candidate_identity"] is None
        or record["systemd_state_before"] is not None
        or _lexists(shared / SYSTEMD_STATE_NAME)
        or current_identity is None
        or current_identity["source_sha"] != PROTECTED_ACTIVATION_CURRENT_SHA
        or {"dev": current_identity["dev"], "ino": current_identity["ino"]}
        != record["current_before_identity"]
        or previous_identity is None
        or previous_identity["source_sha"] != PROTECTED_ACTIVATION_PREVIOUS_SHA
        or {"dev": previous_identity["dev"], "ino": previous_identity["ino"]}
        != record["previous_before_identity"]
        or candidate_identity is None
        or candidate_identity["source_sha"] != PROTECTED_ACTIVATION_CANDIDATE_SHA
        or {"dev": candidate_identity["dev"], "ino": candidate_identity["ino"]}
        != record["candidate_identity"]
        or record["timer_active_before"] is not True
        or record["timer_enabled_before"] != "enabled"
    ):
        raise TransactionError("protected activation recovery topology is not exact")
    validate_service_snapshot(state, require="present")


def restore_protected_activation_core_services(state: Path, *, systemctl: str) -> None:
    """Restore only API/worker/web from the bound protected snapshot."""
    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    _validate_protected_activation_recovery(
        state, record, phase="filesystem-restored-services-pending"
    )
    _verify_original_pointers(record)
    _restore_venv(record)
    service_state = cast(dict[str, str], record["service_state_before"])
    service_enabled = cast(dict[str, str], record["service_enabled_before"])
    for unit in SERVICE_UNITS:
        expected_enabled = service_enabled[unit]
        current_enabled = _read_systemctl_enabled(systemctl, unit)
        if current_enabled != expected_enabled:
            _run_systemctl(
                systemctl,
                "enable" if expected_enabled == "enabled" else "disable",
                unit,
            )
        if _read_systemctl_enabled(systemctl, unit) != expected_enabled:
            raise TransactionError("pre-migration service enabled state was not restored")
        expected = service_state[unit]
        current = _read_systemctl_state(systemctl, unit)
        if current not in {"active", "inactive"}:
            raise TransactionError("pre-migration service state is unsupported")
        if current != expected:
            _run_systemctl(systemctl, "start" if expected == "active" else "stop", unit)
        if _read_systemctl_state(systemctl, unit) != expected:
            raise TransactionError("pre-migration service state was not restored")


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
    purge_legacy_profile_access_cache(state)
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


def _validate_legacy_liveqa_recovery(
    state: Path, record: dict[str, object]
) -> Path:
    """Authorize only the pre-managed-LiveQA release recovery shape.

    This compatibility path is limited to an operation-correlated install
    receipt whose original active release predates managed LiveQA entirely.
    Both inputs must be absent; a partial or present payload remains an error.
    """

    if (
        record["operation"] != "install"
        or record["phase"] != "recovery-restored"
        or not isinstance(record.get("operation_id"), str)
        or OPERATION_ID_PATTERN.fullmatch(cast(str, record["operation_id"])) is None
        or record["current_before_path"] is None
        or record["systemd_state_before"] is not None
        or _lexists(cast(Path, record["shared"]) / SYSTEMD_STATE_NAME)
    ):
        raise TransactionError("transaction is not a legacy LiveQA recovery")
    validate_service_snapshot(state, require="present")
    _verify_original_pointers(record)
    _verify_restored_venv(record)
    release = cast(Path, record["current_before_path"])
    installer = release / "tools" / "platform_live_qa_runtime_install.py"
    runtime_tree = release / "liveqa-runtime"
    if _lexists(installer) or _lexists(runtime_tree):
        raise TransactionError("original release has partial or managed LiveQA payload")
    return release


def _verify_restored_venv(record: dict[str, object]) -> None:
    """Read-only validation that the transaction already restored its venv."""

    transition = record["transition"]
    shared = cast(Path, record["shared_venv_path"])
    peer = cast(Path, record["peer_path"])
    snapshot = cast(Path, record["snapshot_path"])
    if transition == "none":
        shared_before = record["shared_before"]
        if shared_before is None:
            if _lexists(shared):
                raise TransactionError("shared venv changed during legacy recovery")
        elif not _matches(shared, cast(dict[str, int], shared_before)):
            raise TransactionError("original shared venv was not restored")
        return
    if transition == "create":
        peer_before = cast(dict[str, int], record["peer_before"])
        peer_restored = _matches(peer, peer_before)
        cleanup_removed_peer = (
            record["phase"] == "legacy-services-restored" and not _lexists(peer)
        )
        if _lexists(shared) or not (peer_restored or cleanup_removed_peer):
            raise TransactionError("created venv recovery state is incomplete")
        return
    if transition != "exchange":
        raise TransactionError("legacy recovery venv transition is invalid")
    shared_before = cast(dict[str, int], record["shared_before"])
    peer_before = cast(dict[str, int], record["peer_before"])
    if not _matches(shared, shared_before):
        raise TransactionError("original shared venv was not restored")
    locations = _identity_locations(
        (shared, peer, snapshot), (shared_before, peer_before)
    )
    original_location = locations.get((shared_before["dev"], shared_before["ino"]))
    new_location = locations.get((peer_before["dev"], peer_before["ino"]))
    if original_location != shared:
        raise TransactionError("original shared venv recovery state is ambiguous")
    cleanup_removed_peer = record["phase"] == "legacy-services-restored" and new_location is None
    if new_location not in {peer, snapshot} and not cleanup_removed_peer:
        raise TransactionError("replacement shared venv recovery state is incomplete")


def validate_legacy_liveqa_recovery(
    state: Path, *, app_dir: Path, release: Path
) -> None:
    """Read-only preflight for the narrowly scoped legacy restore mode."""

    record = _load_record(state)
    if record["app"] != app_dir:
        raise TransactionError("transaction application identity changed")
    if _validate_legacy_liveqa_recovery(state, record) != release:
        raise TransactionError("legacy recovery release does not match transaction")


def restore_legacy_services(state: Path, *, systemctl: str) -> None:
    """Restore the transaction snapshot after legacy unit files are prepared."""

    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    _validate_legacy_liveqa_recovery(state, record)
    _require_profile_access_cache_purge_proof(record)
    service_state = cast(dict[str, str], record["service_state_before"])
    service_enabled = cast(dict[str, str], record["service_enabled_before"])
    for unit in SERVICE_UNITS:
        enabled_expected = service_enabled[unit]
        _run_systemctl(systemctl, "enable" if enabled_expected == "enabled" else "disable", unit)
        if _read_systemctl_enabled(systemctl, unit) != enabled_expected:
            raise TransactionError("legacy service enabled state was not restored")
        expected = service_state[unit]
        _run_systemctl(systemctl, "restart" if expected == "active" else "stop", unit)
        if _read_systemctl_state(systemctl, unit) != expected:
            raise TransactionError("legacy service state was not restored")
    timer_expected = "active" if record["timer_active_before"] else "inactive"
    timer_enabled_expected = cast(str, record["timer_enabled_before"])
    _run_systemctl(
        systemctl,
        "enable" if timer_enabled_expected == "enabled" else "disable",
        "deadlock-cloudflare-ips.timer",
    )
    if _read_systemctl_enabled(systemctl, "deadlock-cloudflare-ips.timer") != timer_enabled_expected:
        raise TransactionError("legacy timer enabled state was not restored")
    _run_systemctl(
        systemctl,
        "start" if timer_expected == "active" else "stop",
        "deadlock-cloudflare-ips.timer",
    )
    if _read_systemctl_state(systemctl, "deadlock-cloudflare-ips.timer") != timer_expected:
        raise TransactionError("legacy timer state was not restored")


def mark_legacy_services_restored(state: Path, *, systemctl: str) -> None:
    """Persist the readiness boundary before receipt-bound cleanup begins."""

    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    _validate_legacy_liveqa_recovery(state, record)
    _require_profile_access_cache_purge_proof(record)
    service_state = cast(dict[str, str], record["service_state_before"])
    service_enabled = cast(dict[str, str], record["service_enabled_before"])
    for unit in SERVICE_UNITS:
        if _read_systemctl_enabled(systemctl, unit) != service_enabled[unit]:
            raise TransactionError("legacy service enabled state is not restored")
        if _read_systemctl_state(systemctl, unit) != service_state[unit]:
            raise TransactionError("legacy service state is not restored")
    timer_expected = "active" if record["timer_active_before"] else "inactive"
    if _read_systemctl_enabled(systemctl, "deadlock-cloudflare-ips.timer") != record["timer_enabled_before"]:
        raise TransactionError("legacy timer enabled state is not restored")
    if _read_systemctl_state(systemctl, "deadlock-cloudflare-ips.timer") != timer_expected:
        raise TransactionError("legacy timer state is not restored")
    _verify_legacy_readiness(record)
    if (
        record["operation"] == "install"
        and _lexists(cast(Path, record["candidate_path"]) / WEB_CACHE_RELATIVE_PATH)
    ):
        _require_web_unit_control_group(systemctl)
    record["phase"] = "legacy-services-restored"
    _write_record(state, _record_for_write(record), creating=False)


def verify_legacy_services(state: Path, *, systemctl: str) -> None:
    """Read-only retry guard for a durably marked legacy recovery."""

    systemctl = _systemctl_path(systemctl)
    record = _load_record(state)
    if record["phase"] != "legacy-services-restored":
        raise TransactionError("legacy services recovery marker is missing")
    service_state = cast(dict[str, str], record["service_state_before"])
    service_enabled = cast(dict[str, str], record["service_enabled_before"])
    for unit in SERVICE_UNITS:
        if _read_systemctl_enabled(systemctl, unit) != service_enabled[unit]:
            raise TransactionError("legacy service enabled state changed")
        if _read_systemctl_state(systemctl, unit) != service_state[unit]:
            raise TransactionError("legacy service state changed")
    timer_expected = "active" if record["timer_active_before"] else "inactive"
    if _read_systemctl_enabled(systemctl, "deadlock-cloudflare-ips.timer") != record["timer_enabled_before"]:
        raise TransactionError("legacy timer enabled state changed")
    if _read_systemctl_state(systemctl, "deadlock-cloudflare-ips.timer") != timer_expected:
        raise TransactionError("legacy timer state changed")
    _verify_legacy_readiness(record)
    if (
        record["operation"] == "install"
        and _lexists(cast(Path, record["candidate_path"]) / WEB_CACHE_RELATIVE_PATH)
    ):
        _require_web_unit_control_group(systemctl)


def _verify_legacy_readiness(record: dict[str, object]) -> None:
    service_state = cast(dict[str, str], record["service_state_before"])
    deadline = time.monotonic() + LEGACY_READINESS_TIMEOUT_SECONDS
    checks = (
        (
            "deadlock-api",
            [
                "/usr/bin/curl", "--fail", "--silent", "--show-error",
                "--max-time", str(LEGACY_READINESS_CURL_TIMEOUT_SECONDS),
                "http://127.0.0.1:8010/api/v1/health/ready",
            ],
        ),
        (
            "deadlock-web",
            [
                "/usr/bin/curl", "--fail", "--silent", "--show-error",
                "--max-time", str(LEGACY_READINESS_CURL_TIMEOUT_SECONDS),
                "http://127.0.0.1:3000/",
            ],
        ),
    )
    for unit, command in checks:
        if service_state[unit] != "active":
            continue
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransactionError("legacy recovery readiness check failed")
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=min(LEGACY_READINESS_ATTEMPT_TIMEOUT_SECONDS, remaining),
                )
            except subprocess.TimeoutExpired:
                result = None
            except OSError as exc:
                raise TransactionError("legacy recovery readiness check failed") from exc
            if result is not None and result.returncode == 0:
                if time.monotonic() >= deadline:
                    raise TransactionError("legacy recovery readiness check failed")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransactionError("legacy recovery readiness check failed")
            time.sleep(min(LEGACY_READINESS_RETRY_INTERVAL_SECONDS, remaining))


def recover(
    state: Path,
    *,
    retain: bool = False,
    runtime_pending: bool = False,
    service_pending: bool = False,
) -> None:
    record = _load_record(state)
    if record["phase"] == "legacy-services-restored":
        raise TransactionError(
            "legacy recovery phase can only resume through complete-recovery"
        )
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
    ):
        raise TransactionError(
            "service-pending recovery requires a retained install"
        )
    if service_pending and record["previous_before_path"] is not None:
        protected_phase = cast(str, record["phase"])
        if protected_phase not in {"activation-pending", "recovery-authorized"}:
            raise TransactionError(
                "protected service-pending recovery phase is not authorized"
            )
        _validate_protected_activation_recovery(
            state, record, phase=protected_phase
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
        record["phase"] in {"restart-pending", "rollback-cache-purged"}
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


def complete_recovery(
    state: Path, *, retain_receipt: bool = False, systemctl: str | None = None
) -> None:
    record = _load_record(state)
    if record["phase"] not in {"recovery-restored", "legacy-services-restored"}:
        raise TransactionError("release operation recovery is not durably restored")
    if record["phase"] == "legacy-services-restored":
        if systemctl is None:
            raise TransactionError("legacy recovery cleanup requires fresh readiness proof")
        verify_legacy_services(state, systemctl=systemctl)
    _verify_original_pointers(record)
    _restore_venv(record)
    if record["operation"] == "install":
        _cleanup_recovered_install(
            state,
            record,
            remove_receipt=not retain_receipt,
            allow_service_owned_cache=(record["phase"] == "legacy-services-restored"),
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
    if record["operation"] == "rollback":
        if record["phase"] == "restart-pending":
            raise TransactionError(
                "legacy rollback restart-pending receipt has no cache purge proof"
            )
        if record["phase"] in ROLLBACK_CACHE_PURGE_PROOF_PHASES:
            _require_profile_access_cache_purge_proof(record)
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
    if record["operation"] == "rollback":
        if record["phase"] == "restart-pending":
            raise TransactionError(
                "legacy rollback restart-pending receipt has no cache purge proof"
            )
        _require_profile_access_cache_purge_proof(record)
    if record["phase"] not in {
        "pointers-switched",
        "restart-pending",
        "rollback-cache-purged",
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
    for command_name in (
        "quarantine-populated-quiesce",
        "mark-quarantine-services-restored",
        "inspect-quarantined-quiesce",
        "consume-quarantined-quiesce",
    ):
        quarantine_parser = commands.add_parser(command_name)
        quarantine_parser.add_argument("--state", required=True, type=Path)
        quarantine_parser.add_argument("--failed-deploy-run-id", required=True)
        quarantine_parser.add_argument("--failed-deploy-run-attempt", required=True)
        quarantine_parser.add_argument("--failed-deploy-source-sha", required=True)
        quarantine_parser.add_argument("--recovery-run-id", required=True)
        quarantine_parser.add_argument("--recovery-run-attempt", required=True)
        quarantine_parser.add_argument("--bundle-sha", required=True)
    prune_quarantine_parser = commands.add_parser("prune-quarantined-candidate")
    prune_quarantine_parser.add_argument("--state", required=True, type=Path)
    prune_quarantine_parser.add_argument("--failed-deploy-run-id", required=True)
    prune_quarantine_parser.add_argument("--failed-deploy-run-attempt", required=True)
    prune_quarantine_parser.add_argument("--failed-deploy-source-sha", required=True)
    prune_quarantine_parser.add_argument("--cleanup-run-id", required=True)
    prune_quarantine_parser.add_argument("--cleanup-run-attempt", required=True)
    prune_quarantine_parser.add_argument("--cleanup-source-sha", required=True)
    prune_quarantine_parser.add_argument("--bundle-sha", required=True)
    validate_service_snapshot_parser = commands.add_parser("validate-service-snapshot")
    validate_service_snapshot_parser.add_argument("--state", required=True, type=Path)
    validate_service_snapshot_parser.add_argument(
        "--require", required=True, choices=("present", "absent", "optional")
    )
    restore_services_parser = commands.add_parser("restore-services")
    restore_services_parser.add_argument("--state", required=True, type=Path)
    restore_services_parser.add_argument("--systemctl", required=True)
    protected_core_parser = commands.add_parser(
        "restore-protected-activation-core-services"
    )
    protected_core_parser.add_argument("--state", required=True, type=Path)
    protected_core_parser.add_argument("--systemctl", required=True)
    protected_validate_parser = commands.add_parser(
        "validate-protected-activation-recovery"
    )
    protected_validate_parser.add_argument("--state", required=True, type=Path)
    protected_validate_parser.add_argument(
        "--phase", required=True,
        choices=("activation-pending", "filesystem-restored-services-pending"),
    )
    purge_legacy_profile_cache_parser = commands.add_parser(
        "purge-legacy-profile-access-cache"
    )
    purge_legacy_profile_cache_parser.add_argument("--state", required=True, type=Path)
    validate_legacy_liveqa_parser = commands.add_parser(
        "validate-legacy-liveqa-recovery"
    )
    validate_legacy_liveqa_parser.add_argument("--state", required=True, type=Path)
    validate_legacy_liveqa_parser.add_argument("--app-dir", required=True, type=Path)
    validate_legacy_liveqa_parser.add_argument("--release", required=True, type=Path)
    restore_legacy_services_parser = commands.add_parser("restore-legacy-services")
    restore_legacy_services_parser.add_argument("--state", required=True, type=Path)
    restore_legacy_services_parser.add_argument("--systemctl", required=True)
    mark_legacy_services_parser = commands.add_parser("mark-legacy-services-restored")
    mark_legacy_services_parser.add_argument("--state", required=True, type=Path)
    mark_legacy_services_parser.add_argument("--systemctl", required=True)
    verify_legacy_services_parser = commands.add_parser("verify-legacy-services")
    verify_legacy_services_parser.add_argument("--state", required=True, type=Path)
    verify_legacy_services_parser.add_argument("--systemctl", required=True)
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
    complete_recovery_parser.add_argument("--systemctl")
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
        elif args.command in {
            "quarantine-populated-quiesce",
            "mark-quarantine-services-restored",
            "inspect-quarantined-quiesce",
            "consume-quarantined-quiesce",
        }:
            function = {
                "quarantine-populated-quiesce": quarantine_populated_quiesce,
                "mark-quarantine-services-restored": mark_quarantine_services_restored,
                "inspect-quarantined-quiesce": inspect_quarantined_quiesce,
                "consume-quarantined-quiesce": consume_quarantined_quiesce,
            }[args.command]
            receipt = function(
                args.state,
                failed_deploy_run_id=args.failed_deploy_run_id,
                failed_deploy_run_attempt=args.failed_deploy_run_attempt,
                failed_deploy_source_sha=args.failed_deploy_source_sha,
                recovery_run_id=args.recovery_run_id,
                recovery_run_attempt=args.recovery_run_attempt,
                bundle_sha=args.bundle_sha,
            )
            projection: dict[str, object] = {
                "schema": QUARANTINE_RECEIPT_SCHEMA,
                "action": "quarantine_populated_quiesce",
                "phase": receipt["phase"],
            }
            if args.command == "inspect-quarantined-quiesce":
                projection.update(
                    {
                        key: receipt[key]
                        for key in (
                            "service_state_before",
                            "service_enabled_before",
                            "timer_active_before",
                            "timer_enabled_before",
                            "api",
                            "web",
                        )
                    }
                )
            else:
                projection["candidate_tree_sha256"] = receipt["candidate_tree_sha256"]
            print(
                json.dumps(
                    projection,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.command == "prune-quarantined-candidate":
            intent = prune_quarantined_candidate(
                args.state,
                failed_deploy_run_id=args.failed_deploy_run_id,
                failed_deploy_run_attempt=args.failed_deploy_run_attempt,
                failed_deploy_source_sha=args.failed_deploy_source_sha,
                cleanup_run_id=args.cleanup_run_id,
                cleanup_run_attempt=args.cleanup_run_attempt,
                cleanup_source_sha=args.cleanup_source_sha,
                bundle_sha=args.bundle_sha,
            )
            print(
                json.dumps(
                    {
                        "schema": intent["schema"],
                        "action": intent["action"],
                        "phase": intent["phase"],
                        "candidate_slug": intent["candidate_slug"],
                        "original_receipt_sha256": intent["original_receipt_sha256"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.command == "validate-service-snapshot":
            validate_service_snapshot(args.state, require=args.require)
        elif args.command == "restore-services":
            restore_services(args.state, systemctl=args.systemctl)
        elif args.command == "restore-protected-activation-core-services":
            restore_protected_activation_core_services(
                args.state, systemctl=args.systemctl
            )
        elif args.command == "validate-protected-activation-recovery":
            record = _load_record(args.state)
            _validate_protected_activation_recovery(
                args.state, record, phase=args.phase
            )
        elif args.command == "purge-legacy-profile-access-cache":
            purge_legacy_profile_access_cache(args.state)
        elif args.command == "validate-legacy-liveqa-recovery":
            validate_legacy_liveqa_recovery(
                args.state, app_dir=args.app_dir, release=args.release
            )
        elif args.command == "restore-legacy-services":
            restore_legacy_services(args.state, systemctl=args.systemctl)
        elif args.command == "mark-legacy-services-restored":
            mark_legacy_services_restored(args.state, systemctl=args.systemctl)
        elif args.command == "verify-legacy-services":
            verify_legacy_services(args.state, systemctl=args.systemctl)
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
            complete_recovery(
                args.state,
                retain_receipt=args.retain_receipt,
                systemctl=args.systemctl,
            )
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
            if record["operation"] == "rollback" and record["phase"] in {
                "restart-pending",
                *ROLLBACK_CACHE_PURGE_PROOF_PHASES,
            }:
                if record["phase"] == "restart-pending":
                    raise TransactionError(
                        "legacy rollback restart-pending receipt has no cache purge proof"
                    )
                _require_profile_access_cache_purge_proof(record)
            if record["phase"] in {"restart-pending", "rollback-cache-purged"}:
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
