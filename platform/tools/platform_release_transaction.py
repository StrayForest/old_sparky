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
import shutil
import stat
import sys
from typing import cast
from uuid import uuid4


STATE_NAME = ".release-operation.json"
STATE_VERSION = 1
RENAME_EXCHANGE = 2
AT_FDCWD = -100
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
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
    "nginx-applied",
    "smoke-passed",
    "activation-committed",
    "recovery-authorized",
    "restart-pending",
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
    "previous-switched": {"pointers-switched"},
    "current-switched": {"pointers-switched"},
    "pointers-switched": {"restart-pending"},
    "staged": {"migration-pending"},
    "migration-pending": {"migration-failed", "migration-applied", "recovery-authorized"},
    "migration-failed": {"migration-pending", "migration-applied", "recovery-authorized"},
    "migration-applied": {"activation-pending", "recovery-authorized"},
    "activation-pending": {"services-restarted", "recovery-authorized"},
    "services-restarted": {"nginx-applied", "recovery-authorized"},
    "nginx-applied": {"smoke-passed", "recovery-authorized"},
    "smoke-passed": {"activation-committed", "recovery-authorized"},
    "activation-committed": {"recovery-authorized"},
    "recovery-authorized": set(),
    "restart-pending": set(),
    "recovery-restored": set(),
}
MIGRATION_OUTCOME_UNCERTAIN_PHASES = {
    "migration-pending",
    "migration-failed",
    "migration-applied",
    "activation-pending",
    "services-restarted",
    "nginx-applied",
    "smoke-passed",
    "activation-committed",
}
RECOVERY_CONFIRMATION = "MIGRATION_NOT_REVERSED"
RECORD_KEYS = {
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
}


class TransactionError(RuntimeError):
    """A release transaction cannot be proven safe."""


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
    if set(record) != RECORD_KEYS or record.get("version") != STATE_VERSION:
        raise TransactionError("release operation record schema is invalid")
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


def _record_for_write(record: dict[str, object]) -> dict[str, object]:
    return {key: record[key] for key in RECORD_KEYS}


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
    path: Path, *, expected_identity: dict[str, int] | None = None
) -> None:
    metadata = _safe_directory(path, label=f"cleanup path {path.name}")
    if expected_identity is not None and _identity(metadata) != expected_identity:
        raise TransactionError("release cleanup identity changed")
    for root, directories, _files in os.walk(path, topdown=False, followlinks=False):
        for directory in directories:
            child = Path(root) / directory
            if not child.is_symlink():
                os.chmod(child, stat.S_IMODE(child.lstat().st_mode) | 0o700)
        os.chmod(root, stat.S_IMODE(Path(root).lstat().st_mode) | 0o700)
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _mark_recovery_restored(
    state: Path, record: dict[str, object]
) -> dict[str, object]:
    record["phase"] = "recovery-restored"
    _write_record(state, _record_for_write(record), creating=False)
    return _load_record(state)


def _cleanup_recovered_install(state: Path, record: dict[str, object]) -> None:
    shared = cast(Path, record["shared"])
    peer = cast(Path, record["peer_path"])
    candidate = cast(Path, record["candidate_path"])
    peer_before = record["peer_before"]
    if _lexists(peer):
        if not isinstance(peer_before, dict):
            raise TransactionError("install cleanup peer identity is invalid")
        _remove_tree(peer, expected_identity=peer_before)
    if _lexists(candidate):
        if (
            _read_pointer(record["app"], "current") == candidate
            or _read_pointer(record["app"], "previous") == candidate
        ):
            raise TransactionError("candidate release is still active during cleanup")
        candidate_identity = cast(dict[str, int], record["candidate_identity"])
        _remove_tree(candidate, expected_identity=candidate_identity)
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
    if _lexists(state):
        state.unlink()
        _fsync_directory(state.parent)


def recover(state: Path) -> None:
    record = _load_record(state)
    if (
        record["operation"] == "install"
        and record["phase"] in MIGRATION_OUTCOME_UNCERTAIN_PHASES
    ):
        raise TransactionError(
            "migration outcome is not safely reversible; retain the state and "
            "resume the deployment or make an explicit operator rollback decision"
        )
    if record["phase"] == "restart-pending":
        raise TransactionError(
            "rollback filesystem state is complete but its service restart is pending"
        )
    if record["phase"] != "recovery-restored":
        _restore_pointers(record)
        _restore_venv(record)
        _verify_original_pointers(record)
        record = _mark_recovery_restored(state, record)
    else:
        _verify_original_pointers(record)
        _restore_venv(record)
    if record["operation"] == "install":
        _cleanup_recovered_install(state, record)
    else:
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


def complete(state: Path) -> None:
    record = _load_record(state)
    if record["phase"] not in {
        "pointers-switched",
        "restart-pending",
        "activation-committed",
    }:
        raise TransactionError("release operation pointers are not durably switched")
    _validate_success(record)
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
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--state", required=True, type=Path)
    authorize_parser = commands.add_parser("authorize-recovery")
    authorize_parser.add_argument("--state", required=True, type=Path)
    authorize_parser.add_argument("--confirm", required=True)
    complete_parser = commands.add_parser("complete")
    complete_parser.add_argument("--state", required=True, type=Path)
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
        elif args.command == "exchange":
            exchange_recorded_venvs(args.state)
        elif args.command == "rename":
            rename_recorded_venv(args.state, mode=args.mode)
        elif args.command == "switch-pointer":
            switch_pointer(args.state, name=args.name, target_value=args.target)
        elif args.command == "recover":
            recover(args.state)
        elif args.command == "authorize-recovery":
            authorize_recovery(args.state, confirmation=args.confirm)
        elif args.command == "complete":
            complete(args.state)
        else:
            record = _load_record(args.state)
            if record["phase"] == "restart-pending":
                _validate_success(record)
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
                        },
                        sort_keys=True,
                    )
                )
            else:
                print(f"{record['operation']} {record['phase']}")
        return 0
    except TransactionError as exc:
        print(f"Release transaction refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
