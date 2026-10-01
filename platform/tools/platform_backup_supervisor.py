#!/usr/bin/env python3
"""Lock-aware supervisor for platform backup and off-site operations.

The backup directory is a shared trust boundary.  This module owns every
production mutation which can create, consume, rotate, or publish a backup.
The older backup helpers remain useful as library primitives and for
read-only diagnostics, but their command-line mutation paths enter here.

The lock order is deliberately represented as data as well as code::

    release -> retained-load -> source/build -> live-QA -> backup

An operation may acquire a suffix of that order (off-site work needs only the
backup lock), but no operation may acquire a predecessor after the backup
lock.  This makes the ownership boundary reviewable and prevents a later
consumer from re-acquiring an earlier lock while the final backup lock is
held.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import errno
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Iterator, Mapping, final
from uuid import uuid4
import weakref


BACKUP_LOCK_PATH = Path("/run/lock/oldsparky-platform-backup.lock")
# This is a Linux abstract-namespace socket name, deliberately not derived
# from the lock filename.  The socket is held for the complete operation and
# is released by the kernel when the owning process exits.  Keeping this
# boundary outside the filesystem closes the inode-replacement hole in a
# pathname-only flock scheme.
_BACKUP_SINGLETON_NAME = b"\0oldsparky-platform-backup-v1"
LOCK_ROOT = Path("/run/lock")
BACKUP_EVIDENCE_DIRNAME = "backup-evidence"
EVIDENCE_SCHEMA = 1
EVIDENCE_KIND = "platform_backup"
EVIDENCE_STATUSES = frozenset(
    {"started", "passed", "failed", "blocked", "cancelled", "unknown"}
)
LOCK_ORDER = ("release", "retained-load", "source/build", "live-QA", "backup")
LOCK_ORDER_NAMES = LOCK_ORDER
OPERATION_LOCK_MATRIX: Mapping[str, tuple[str, ...]] = {
    "maintenance": LOCK_ORDER,
    "local-backup": LOCK_ORDER,
    "local-backup-prune": LOCK_ORDER,
    "offsite": ("backup",),
    "backup-check": (),
    "production-restore": LOCK_ORDER,
}
STATUS_EXIT_CODES = {
    "passed": 0,
    "blocked": 75,
    "cancelled": 130,
    "failed": 1,
    "unknown": 1,
}
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ALEMBIC_HEAD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
SAFE_EVIDENCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
SAFE_ERROR_RE = re.compile(r"^[a-z0-9_.-]{1,80}$")


class BackupSupervisorError(RuntimeError):
    """A fail-closed backup supervisor refusal."""


class BackupLockError(BackupSupervisorError):
    """The canonical backup lock cannot be trusted or released safely."""


class BackupLockConflict(BackupLockError):
    """Another process owns the backup lock (non-blocking conflict)."""

    code = "backup_lock_conflict"
    status = "blocked"
    exit_code = 75


class BackupEvidenceError(BackupSupervisorError):
    """Durable evidence could not be validated or published."""


class ProductionRestoreDisabled(BackupSupervisorError):
    """Destructive production restore is intentionally unavailable."""

    code = "production_restore_disabled"
    status = "blocked"
    exit_code = 75


@dataclass(frozen=True, slots=True)
class LockIdentity:
    device: int
    inode: int
    owner: int
    group: int
    links: int
    mode: int


@dataclass(slots=True)
class BackupLockHandle:
    """A held descriptor and the inode identity it proved before ``flock``."""

    path: Path
    fd: int
    identity: LockIdentity
    _closed: bool = False

    def validate(self) -> None:
        if self._closed:
            raise BackupLockError("backup lock descriptor is closed")
        try:
            opened = os.fstat(self.fd)
            current = self.path.lstat()
        except OSError as exc:
            raise BackupLockError("backup lock pathname disappeared") from exc
        _validate_lock_stat(opened)
        _validate_lock_stat(current)
        if _identity(opened) != self.identity or _identity(current) != self.identity:
            raise BackupLockError("backup lock pathname was replaced while held")

    def close(self) -> None:
        if self._closed:
            return
        try:
            # A replacement is a correctness failure, not a reason to unlock a
            # different inode.  The descriptor remains the only object passed
            # to the kernel.
            self.validate()
        finally:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self._closed = True


def _identity(metadata: os.stat_result) -> LockIdentity:
    return LockIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner=metadata.st_uid,
        group=metadata.st_gid,
        links=metadata.st_nlink,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _validate_lock_stat(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise BackupLockError("backup lock file must be root-owned regular 0600")


def _validate_lock_root(root: Path) -> os.stat_result:
    try:
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise BackupLockError("backup lock root is unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        resolved != root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or (mode & 0o022 and not mode & stat.S_ISVTX)
    ):
        raise BackupLockError("backup lock root metadata is unsafe")
    return metadata


def _open_lock_parent(root: Path) -> tuple[int, os.stat_result]:
    expected = _validate_lock_root(root)
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise BackupLockError("backup lock root could not be opened") from exc
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise BackupLockError("backup lock root changed while opening")
    return descriptor, opened


def _lock_path_for(path: Path, *, allow_test_path: bool = False) -> tuple[Path, Path]:
    target = Path(path)
    if (not allow_test_path and target != BACKUP_LOCK_PATH) or (
        allow_test_path
        and (not target.is_absolute() or target.name != BACKUP_LOCK_PATH.name)
    ):
        raise BackupLockError("backup lock pathname is not canonical")
    return target, target.parent


def _bind_backup_singleton(name: bytes = _BACKUP_SINGLETON_NAME) -> socket.socket:
    """Bind the fixed kernel singleton before opening the filesystem lock.

    ``name`` is private-test dependency injection only.  Production callers
    use the module constant and have no path/socket-name selection API.
    """

    if not name.startswith(b"\0") or name != _BACKUP_SINGLETON_NAME:
        # The test backend below passes an explicitly generated private name;
        # production can never redirect this boundary accidentally.
        if not name.startswith(b"\0oldsparky-platform-backup-test-"):
            raise BackupLockError("backup singleton name is not canonical")
    try:
        singleton = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        singleton.bind(name)
        singleton.listen(1)
        return singleton
    except OSError as exc:
        try:
            singleton.close()
        except (UnboundLocalError, OSError):
            pass
        if exc.errno in {errno.EADDRINUSE, errno.EAGAIN}:
            raise BackupLockConflict(
                "another backup operation owns the kernel singleton"
            ) from exc
        raise BackupLockError("backup kernel singleton could not be acquired") from exc


@contextmanager
def _exclusive_backup_lock_backend(
    lock_path: Path,
    *,
    singleton_name: bytes,
) -> Iterator[BackupLockHandle]:
    """Acquire the root-owned canonical lock without waiting or trusting mtime.

    ``flock(2)`` is non-blocking by design.  A stale filename is harmless: the
    current inode is validated and a stale file with no kernel owner is simply
    reused.  A same-owner replacement is rejected by the descriptor/path
    identity checks before and after the operation.
    """

    lock_path, lock_root = _lock_path_for(
        lock_path, allow_test_path=singleton_name != _BACKUP_SINGLETON_NAME
    )
    singleton = _bind_backup_singleton(singleton_name)
    try:
        parent_fd, _ = _open_lock_parent(lock_root)
    except BaseException:
        singleton.close()
        raise
    descriptor: int | None = None
    handle: BackupLockHandle | None = None
    try:
        try:
            descriptor = os.open(
                lock_path.name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise BackupLockError("backup lock pathname is a symlink or link") from exc
            raise BackupLockError("backup lock file could not be opened") from exc
        opened = os.fstat(descriptor)
        try:
            current = os.stat(lock_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise BackupLockError("backup lock pathname could not be validated") from exc
        _validate_lock_stat(opened)
        _validate_lock_stat(current)
        if _identity(opened) != _identity(current):
            raise BackupLockError("backup lock pathname changed while opening")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BackupLockConflict(
                    "another backup operation holds the canonical lock"
                ) from exc
            raise BackupLockError("backup lock could not be acquired") from exc
        handle = BackupLockHandle(lock_path, descriptor, _identity(opened))
        handle.validate()
        yield handle
    finally:
        try:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        finally:
            try:
                os.close(parent_fd)
            finally:
                singleton.close()


@contextmanager
def exclusive_backup_lock() -> Iterator[BackupLockHandle]:
    """Acquire the fixed root-owned lock and fixed kernel singleton."""

    with _exclusive_backup_lock_backend(
        BACKUP_LOCK_PATH, singleton_name=_BACKUP_SINGLETON_NAME
    ) as handle:
        yield handle


@contextmanager
def _exclusive_backup_lock_for_test(
    path: Path,
    *,
    singleton_name: bytes | None = None,
) -> Iterator[BackupLockHandle]:
    """Private dependency-injected backend used only by lock unit tests."""

    name = singleton_name or (b"\0oldsparky-platform-backup-test-" + uuid4().hex.encode())
    with _exclusive_backup_lock_backend(Path(path), singleton_name=name) as handle:
        yield handle


def operation_lock_requirements(operation: str) -> tuple[str, ...]:
    try:
        return OPERATION_LOCK_MATRIX[operation]
    except KeyError as exc:
        raise ValueError(f"unknown backup operation: {operation}") from exc


def validate_lock_sequence(sequence: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate an observed lock acquisition sequence against the global order."""

    positions = {name: index for index, name in enumerate(LOCK_ORDER)}
    normalized = tuple(sequence)
    if any(name not in positions for name in normalized):
        raise BackupSupervisorError("unknown lock in backup operation sequence")
    if len(set(normalized)) != len(normalized):
        raise BackupSupervisorError("backup operation reacquired a lock")
    if tuple(sorted(normalized, key=positions.__getitem__)) != normalized:
        raise BackupSupervisorError("backup operation uses reverse lock order")
    return normalized


def assert_operation_lock_order(operation: str, sequence: tuple[str, ...] | list[str]) -> None:
    required = operation_lock_requirements(operation)
    observed = validate_lock_sequence(sequence)
    if observed != required:
        raise BackupSupervisorError(
            f"operation {operation} requires lock sequence {required!r}, got {observed!r}"
        )


def _build_authority_broker():
    """Build the private authority boundary and its guarded consumers.

    The registry and its mutators deliberately never leave this closure.  A
    caller can receive an already-issued authority only as the argument to a
    callback executed by one of the fixed supervisor scopes below.  This is a
    stronger boundary than a module-private ``_issue_*`` helper: reflection
    over module globals cannot obtain a raw issuer or registry.
    """

    def identity_registry():
        records: dict[int, tuple[weakref.ReferenceType[Any], object]] = {}

        def register(instance: object, record: object) -> None:
            key = id(instance)
            current = records.get(key)
            if current is not None and current[0]() is not None:
                raise BackupSupervisorError("authority identity is already issued")

            def discard(reference: weakref.ReferenceType[Any]) -> None:
                current_record = records.get(key)
                if current_record is not None and current_record[0] is reference:
                    records.pop(key, None)

            try:
                reference = weakref.ref(instance, discard)
            except TypeError as exc:
                raise BackupSupervisorError("authority identity cannot be registered") from exc
            records[key] = (reference, record)

        def lookup(instance: object) -> object | None:
            current = records.get(id(instance))
            if current is None or current[0]() is not instance:
                return None
            return current[1]

        return register, lookup

    register_capability, lookup_capability = identity_registry()
    register_head, lookup_head = identity_registry()

    def capability_record(instance: object) -> object | None:
        return lookup_capability(instance)

    def head_record(instance: object) -> object | None:
        return lookup_head(instance)

    def make_capability(
        operation: str,
        scope_state: dict[str, bool] | None = None,
    ) -> _MutationCapability:
        if operation not in {"maintenance", "offsite"}:
            raise BackupSupervisorError(f"unknown backup mutation operation: {operation}")
        instance = object.__new__(_MutationCapability)
        register_capability(instance, (operation, scope_state))
        return instance

    def make_head(
        value: str,
        source_root: Path,
        scope_state: dict[str, bool] | None = None,
    ) -> _TrustedAlembicHead:
        if not isinstance(value, str) or ALEMBIC_HEAD_RE.fullmatch(value) is None:
            raise BackupSupervisorError("trusted Alembic head is invalid")
        if not isinstance(source_root, Path) or not source_root.is_absolute():
            raise BackupSupervisorError("trusted Alembic source root is invalid")
        instance = object.__new__(_TrustedAlembicHead)
        register_head(instance, (value, source_root, scope_state))
        return instance

    def contains_authority(value: object, seen: set[int] | None = None) -> bool:
        """Prevent a scoped callback from smuggling authority out in a result."""

        if type(value) in {_MutationCapability, _TrustedAlembicHead}:
            return True
        if seen is None:
            seen = set()
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        if isinstance(value, Mapping):
            return any(
                contains_authority(key, seen) or contains_authority(item, seen)
                for key, item in value.items()
            )
        if isinstance(value, (tuple, list, set, frozenset)):
            return any(contains_authority(item, seen) for item in value)
        return False

    def public_result(value: Any) -> Any:
        if contains_authority(value):
            raise BackupSupervisorError("supervisor authority cannot escape its operation scope")
        return value

    def validate_scope_lock(lock: object) -> None:
        validator = getattr(lock, "validate", None)
        if not callable(validator):
            raise BackupSupervisorError("backup operation requires a held backup lock")
        try:
            validator()
        except BackupSupervisorError:
            raise
        except Exception as exc:
            raise BackupSupervisorError("held backup lock is invalid") from exc

    def load_restore_module() -> Any:
        try:
            return importlib.import_module("tools.platform_backup_restore_drill")
        except ImportError:
            # The installed production entrypoint is executed by absolute
            # path; in that mode its sibling directory is on sys.path.
            return importlib.import_module("platform_backup_restore_drill")

    def resolve_trusted_head(
        source_root: Path,
        *,
        restore: Any | None = None,
        scope_state: dict[str, bool] | None = None,
    ) -> _TrustedAlembicHead:
        """Derive and pin the exact head from the trusted source graph."""

        root = Path(source_root)
        if not root.is_absolute():
            raise BackupSupervisorError("trusted Alembic source root must be absolute")
        try:
            resolved_root = root.resolve(strict=False)
        except OSError as exc:
            raise BackupSupervisorError("trusted Alembic source root is invalid") from exc
        if restore is None:
            restore = load_restore_module()
        try:
            value = restore.expected_alembic_head(root)
        except Exception as exc:
            raise BackupSupervisorError(
                "trusted deployed Alembic source graph is unavailable"
            ) from exc
        return make_head(value, resolved_root, scope_state)

    def trusted_head_for_source(source_root: Path) -> _TrustedAlembicHead:
        """Resolve a trusted head using the deployed restore graph only."""

        return resolve_trusted_head(source_root, restore=load_restore_module())

    def local_backup_scope(
        args: argparse.Namespace,
        *,
        app_dir: Path,
        lock: BackupLockHandle,
        source_root: Path | None = None,
        _restore_module: Any | None = None,
        callback: Any,
    ) -> Any:
        """Issue fixed local-backup authorities only inside a held lock scope."""

        validate_scope_lock(lock)
        scope_state = {"active": True}
        try:
            restore = _restore_module if _restore_module is not None else load_restore_module()
            trusted_source = Path(source_root or (Path(app_dir) / "current"))
            trusted_head = None
            if not bool(getattr(args, "dump_only", False)):
                trusted_head = resolve_trusted_head(
                    trusted_source,
                    restore=restore,
                    scope_state=scope_state,
                )
            capability = make_capability("maintenance", scope_state)
            return public_result(callback(capability, trusted_head, restore))
        finally:
            scope_state["active"] = False

    def offsite_scope(
        args: argparse.Namespace,
        *,
        app_dir: Path,
        lock: BackupLockHandle,
        _restore_module: Any | None = None,
        callback: Any,
    ) -> Any:
        """Issue the fixed off-site authority after graph validation."""

        validate_scope_lock(lock)
        scope_state = {"active": True}
        try:
            restore = _restore_module if _restore_module is not None else load_restore_module()
            trusted_head = resolve_trusted_head(
                Path(app_dir) / "current",
                restore=restore,
                scope_state=scope_state,
            )
            capability = make_capability("offsite", scope_state)
            return public_result(callback(capability, trusted_head, restore))
        finally:
            scope_state["active"] = False

    def maintenance_scope(*, lock: BackupLockHandle, callback: Any) -> Any:
        """Issue the fixed maintenance authority under the held backup lock."""

        validate_scope_lock(lock)
        scope_state = {"active": True}
        try:
            return public_result(callback(make_capability("maintenance", scope_state)))
        finally:
            scope_state["active"] = False

    return (
        capability_record,
        head_record,
        trusted_head_for_source,
        local_backup_scope,
        offsite_scope,
        maintenance_scope,
    )


@final
class _MutationCapability:
    """Immutable, supervisor-created authority for one mutation contour."""

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: object, **_kwargs: object) -> _MutationCapability:
        raise TypeError("backup mutation capability is factory-only")

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("backup mutation capability is final")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("backup mutation capability is immutable")

    @property
    def operation(self) -> str:
        record = _authority_capability_record(self)
        if (
            not isinstance(record, tuple)
            or len(record) != 2
            or record[0] not in {"maintenance", "offsite"}
            or (
                record[1] is not None
                and (
                    not isinstance(record[1], dict)
                    or record[1].get("active") is not True
                )
            )
        ):
            raise BackupSupervisorError("backup mutation capability is invalid")
        return record[0]

    def prove(self, operation: str) -> None:
        if type(self) is not _MutationCapability:
            raise BackupSupervisorError("backup mutation capability is invalid")
        if operation not in {"maintenance", "offsite"}:
            raise BackupSupervisorError("backup mutation capability is invalid")
        record = _authority_capability_record(self)
        if (
            not isinstance(record, tuple)
            or len(record) != 2
            or record[0] != operation
            or (
                record[1] is not None
                and (
                    not isinstance(record[1], dict)
                    or record[1].get("active") is not True
                )
            )
        ):
            raise BackupSupervisorError("backup mutation capability is invalid")

    def __copy__(self) -> None:
        raise TypeError("backup mutation capability cannot be copied")

    def __deepcopy__(self, _memo: dict[int, object]) -> None:
        raise TypeError("backup mutation capability cannot be copied")

    def __reduce__(self) -> None:
        raise TypeError("backup mutation capability cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> None:
        raise TypeError("backup mutation capability cannot be serialized")


def require_mutation_capability(value: object, operation: str) -> _MutationCapability:
    if type(value) is not _MutationCapability:
        raise BackupSupervisorError(
            "backup mutation requires an in-process supervisor capability"
        )
    value.prove(operation)
    return value


@final
class _TrustedAlembicHead:
    """An exact migration head resolved once by the supervisor boundary.

    The provenance record lives in the private authority broker, not on the
    object.  The source root travels with the value, allowing the guarded
    hermetic test contour to use an isolated trusted graph without changing
    the production ``app_dir/current`` resolution rule.  Only the broker's
    trusted-graph resolver creates instances after reading that graph.
    """

    __slots__ = ("__weakref__",)

    def __new__(cls, *_args: object, **_kwargs: object) -> _TrustedAlembicHead:
        raise TypeError("trusted Alembic head is factory-only")

    def __init_subclass__(cls, **_: object) -> None:
        raise TypeError("trusted Alembic head is final")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise TypeError("trusted Alembic head is immutable")

    @property
    def value(self) -> str:
        record = _authority_head_record(self)
        if (
            not isinstance(record, tuple)
            or len(record) != 3
            or (
                record[2] is not None
                and (
                    not isinstance(record[2], dict)
                    or record[2].get("active") is not True
                )
            )
        ):
            raise BackupSupervisorError("trusted Alembic head is invalid")
        return record[0]

    @property
    def source_root(self) -> Path:
        record = _authority_head_record(self)
        if (
            not isinstance(record, tuple)
            or len(record) != 3
            or (
                record[2] is not None
                and (
                    not isinstance(record[2], dict)
                    or record[2].get("active") is not True
                )
            )
        ):
            raise BackupSupervisorError("trusted Alembic head is invalid")
        return record[1]

    def __copy__(self) -> None:
        raise TypeError("trusted Alembic head cannot be copied")

    def __deepcopy__(self, _memo: dict[int, object]) -> None:
        raise TypeError("trusted Alembic head cannot be copied")

    def __reduce__(self) -> None:
        raise TypeError("trusted Alembic head cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> None:
        raise TypeError("trusted Alembic head cannot be serialized")


def require_trusted_alembic_head(
    value: object,
    *,
    source_root: Path | None = None,
    expected: str | None = None,
) -> _TrustedAlembicHead:
    """Validate the supervisor-only head passed to a mutating primitive."""

    if type(value) is not _TrustedAlembicHead:
        raise BackupSupervisorError(
            "backup restore requires a supervisor-resolved trusted Alembic head"
        )
    record = _authority_head_record(value)
    if not isinstance(record, tuple) or len(record) != 3:
        raise BackupSupervisorError(
            "backup restore requires a supervisor-resolved trusted Alembic head"
        )
    head_value, head_source_root, scope_state = record
    if scope_state is not None and (
        not isinstance(scope_state, dict) or scope_state.get("active") is not True
    ):
        raise BackupSupervisorError("trusted Alembic head is no longer active")
    if not isinstance(head_value, str) or ALEMBIC_HEAD_RE.fullmatch(head_value) is None:
        raise BackupSupervisorError("trusted Alembic head is invalid")
    if not isinstance(head_source_root, Path) or not head_source_root.is_absolute():
        raise BackupSupervisorError("trusted Alembic source root is invalid")
    if source_root is not None:
        try:
            resolved_root = Path(source_root).resolve(strict=False)
        except OSError as exc:
            raise BackupSupervisorError("trusted Alembic source root is invalid") from exc
        if resolved_root != head_source_root:
            raise BackupSupervisorError("trusted Alembic source root changed")
    if expected is not None and expected != head_value:
        raise BackupSupervisorError(
            "requested Alembic head does not match the trusted deployed source graph"
        )
    return value


(
    _authority_capability_record,
    _authority_head_record,
    _trusted_head_for_source,
    _run_local_backup_scope,
    _run_offsite_scope,
    _run_maintenance_scope,
) = _build_authority_broker()
del _build_authority_broker


@dataclass(frozen=True, slots=True)
class BackupPairSnapshot:
    dump_name: str
    manifest_name: str
    dump_identity: tuple[int, int, int, int, int]
    manifest_identity: tuple[int, int, int, int, int]
    dump_sha256: str
    manifest_sha256: str
    size_bytes: int
    run_id: str


def _pair_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _validate_pair_stat(metadata: os.stat_result, *, label: str) -> None:
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise BackupSupervisorError(f"{label} metadata is unsafe")


def _read_held_fd(descriptor: int, *, label: str) -> tuple[bytes, os.stat_result]:
    try:
        before = os.fstat(descriptor)
        _validate_pair_stat(before, label=label)
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise BackupSupervisorError(f"{label} could not be read") from exc
    if _pair_identity(after) != _pair_identity(before):
        raise BackupSupervisorError(f"{label} changed while it was read")
    return b"".join(chunks), after


def _hash_held_fd(descriptor: int, *, label: str) -> tuple[str, os.stat_result]:
    try:
        before = os.fstat(descriptor)
        _validate_pair_stat(before, label=label)
        digest = hashlib.sha256()
        offset = 0
        while offset < before.st_size:
            chunk = os.pread(
                descriptor, min(1024 * 1024, before.st_size - offset), offset
            )
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise BackupSupervisorError(f"{label} could not be hashed") from exc
    if offset != before.st_size or _pair_identity(after) != _pair_identity(before):
        raise BackupSupervisorError(f"{label} changed while it was hashed")
    return digest.hexdigest(), after


@dataclass(slots=True)
class HeldBackupPair:
    """An exact pair held by descriptors for the complete consumer transaction."""

    dump_path: Path
    manifest_path: Path
    dump_fd: int
    manifest_fd: int
    dump_identity: tuple[int, int, int, int, int]
    manifest_identity: tuple[int, int, int, int, int]
    dump_sha256: str
    manifest_sha256: str
    manifest_bytes: bytes
    run_id: str
    _closed: bool = False

    @property
    def snapshot(self) -> BackupPairSnapshot:
        return BackupPairSnapshot(
            dump_name=self.dump_path.name,
            manifest_name=self.manifest_path.name,
            dump_identity=self.dump_identity,
            manifest_identity=self.manifest_identity,
            dump_sha256=self.dump_sha256,
            manifest_sha256=self.manifest_sha256,
            size_bytes=self.dump_identity[3],
            run_id=self.run_id,
        )

    def _validate_one(
        self,
        path: Path,
        descriptor: int,
        identity: tuple[int, int, int, int, int],
        digest: str,
        *,
        label: str,
    ) -> None:
        try:
            descriptor_stat = os.fstat(descriptor)
            path_stat = path.lstat()
        except OSError as exc:
            raise BackupSupervisorError(f"{label} identity was lost") from exc
        _validate_pair_stat(descriptor_stat, label=label)
        _validate_pair_stat(path_stat, label=label)
        if _pair_identity(descriptor_stat) != identity or _pair_identity(path_stat) != identity:
            raise BackupSupervisorError(f"{label} was replaced while held")
        current_digest, after = _hash_held_fd(descriptor, label=label)
        if current_digest != digest or _pair_identity(after) != identity:
            raise BackupSupervisorError(f"{label} bytes changed while held")
        try:
            final_path_stat = path.lstat()
        except OSError as exc:
            raise BackupSupervisorError(f"{label} disappeared while held") from exc
        _validate_pair_stat(final_path_stat, label=label)
        if _pair_identity(final_path_stat) != identity:
            raise BackupSupervisorError(f"{label} pathname changed while held")

    def validate(self) -> None:
        if self._closed:
            raise BackupSupervisorError("backup pair descriptors are closed")
        self._validate_one(
            self.dump_path,
            self.dump_fd,
            self.dump_identity,
            self.dump_sha256,
            label="backup dump",
        )
        self._validate_one(
            self.manifest_path,
            self.manifest_fd,
            self.manifest_identity,
            self.manifest_sha256,
            label="backup manifest",
        )

    def close(self) -> None:
        if self._closed:
            return
        failure: BaseException | None = None
        try:
            self.validate()
        except BaseException as exc:
            failure = exc
        for descriptor in (self.dump_fd, self.manifest_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._closed = True
        if failure is not None:
            raise failure


@contextmanager
def held_backup_pair(
    dump_path: Path,
    manifest_path: Path | None = None,
) -> Iterator[HeldBackupPair]:
    """Open and pin both pair members with ``O_NOFOLLOW`` until exit."""

    dump_path = Path(dump_path)
    manifest_path = dump_path.with_suffix(".json") if manifest_path is None else Path(manifest_path)
    if dump_path.parent.resolve(strict=True) != manifest_path.parent.resolve(strict=True):
        raise BackupSupervisorError("backup dump and manifest are not siblings")
    if manifest_path.with_suffix(".dump").name != dump_path.name:
        raise BackupSupervisorError("backup dump and manifest names do not match")
    descriptors: list[int] = []
    pair: HeldBackupPair | None = None
    try:
        opened: list[tuple[Path, str, int, os.stat_result, bytes | None]] = []
        for path, label, read_bytes in (
            (dump_path, "backup dump", False),
            (manifest_path, "backup manifest", True),
        ):
            try:
                path_stat = path.lstat()
                _validate_pair_stat(path_stat, label=label)
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                descriptors.append(descriptor)
                opened_stat = os.fstat(descriptor)
                _validate_pair_stat(opened_stat, label=label)
                if _pair_identity(opened_stat) != _pair_identity(path_stat):
                    raise BackupSupervisorError(f"{label} changed while opening")
                raw: bytes | None = None
                if read_bytes:
                    raw, opened_stat = _read_held_fd(descriptor, label=label)
                opened.append((path, label, descriptor, opened_stat, raw))
            except OSError as exc:
                raise BackupSupervisorError(f"{label} could not be opened") from exc
        dump, manifest = opened
        dump_sha, dump_stat = _hash_held_fd(dump[2], label="backup dump")
        manifest_sha, manifest_stat = _hash_held_fd(manifest[2], label="backup manifest")
        manifest_module = _manifest_module()
        try:
            parsed = manifest_module.parse_manifest_bytes(
                manifest[4] or b"", expected_dump_file=dump_path.name
            )
        except Exception as exc:
            raise BackupSupervisorError("backup manifest is invalid") from exc
        if (
            not parsed.restore_verified
            or not parsed.alembic_revision_verified
            or parsed.sha256 != dump_sha
            or parsed.size_bytes != dump_stat.st_size
        ):
            raise BackupSupervisorError("backup dump/manifest pair is not restore-verified")
        run_id = str(parsed.run_id)
        if not RUN_ID_RE.fullmatch(run_id):
            raise BackupSupervisorError("backup manifest run identity is invalid")
        pair = HeldBackupPair(
            dump_path=dump_path,
            manifest_path=manifest_path,
            dump_fd=dump[2],
            manifest_fd=manifest[2],
            dump_identity=_pair_identity(dump_stat),
            manifest_identity=_pair_identity(manifest_stat),
            dump_sha256=dump_sha,
            manifest_sha256=manifest_sha,
            manifest_bytes=manifest[4] or b"",
            run_id=run_id,
        )
        pair.validate()
        yield pair
    finally:
        if pair is not None:
            active_exception = sys.exc_info()[1]
            try:
                pair.close()
            except BackupSupervisorError:
                if active_exception is None:
                    raise
        else:
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _private_file_snapshot(path: Path, *, label: str) -> tuple[os.stat_result, str]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupSupervisorError(f"{label} is unavailable") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise BackupSupervisorError(f"{label} metadata is unsafe")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise BackupSupervisorError(f"{label} could not be opened") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
        ):
            raise BackupSupervisorError(f"{label} changed while opening")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise BackupSupervisorError(f"{label} changed while hashing")
    except OSError as exc:
        raise BackupSupervisorError(f"{label} could not be read") from exc
    finally:
        os.close(descriptor)
    return metadata, digest.hexdigest()


def _manifest_module() -> Any:
    try:
        return importlib.import_module("tools.platform_backup_manifest")
    except ImportError:
        return importlib.import_module("platform_backup_manifest")


def snapshot_backup_pair(
    dump_path: Path,
    manifest_path: Path | None = None,
) -> BackupPairSnapshot:
    """Read and hash an exact dump/manifest pair with identity pinning."""

    with held_backup_pair(dump_path, manifest_path) as pair:
        return pair.snapshot


def assert_backup_pair_unchanged(
    before: BackupPairSnapshot,
    dump_path: Path,
    manifest_path: Path | None = None,
) -> BackupPairSnapshot:
    after = snapshot_backup_pair(dump_path, manifest_path)
    if after != before:
        raise BackupSupervisorError("backup dump/manifest pair changed during consumer work")
    return after


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _public_value(value: object, *, pattern: re.Pattern[str], default: str = "unknown") -> str:
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    return default


def _safe_public_name(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    name = Path(value).name
    return name if SAFE_NAME_RE.fullmatch(name) else "unknown"


def _empty_evidence(
    *, operation: str, operation_id: str, status: str, started_at: str
) -> dict[str, Any]:
    return {
        "schema": EVIDENCE_SCHEMA,
        "kind": EVIDENCE_KIND,
        "operation": operation,
        "operation_id": operation_id,
        "status": status,
        "started_at_utc": started_at,
        "completed_at_utc": None,
        "source": {
            "dump_file": "unknown",
            "sha256": "unknown",
            "size_bytes": None,
        },
        "manifest": {"file": "unknown", "sha256": "unknown", "run_id": "unknown"},
        "release": {"source_sha": "unknown"},
        "alembic": {"revision": "unknown", "verified": False},
        "locks": {
            "required": list(OPERATION_LOCK_MATRIX.get(operation, ())),
            "acquired": list(OPERATION_LOCK_MATRIX.get(operation, ())),
            "order": list(LOCK_ORDER),
        },
        "remote_transport": {
            "attempted": False,
            "uploaded": False,
            "head_verified": False,
            "object": "unknown",
        },
        "recovery": {
            "restore_drill": "unknown",
            "production_restore": "disabled",
        },
        "error_class": None,
    }


EVIDENCE_KEYS = frozenset(_empty_evidence(operation="x", operation_id="0" * 32, status="started", started_at="x"))
EVIDENCE_SECTION_KEYS = {
    "source": frozenset({"dump_file", "sha256", "size_bytes"}),
    "manifest": frozenset({"file", "sha256", "run_id"}),
    "release": frozenset({"source_sha"}),
    "alembic": frozenset({"revision", "verified"}),
    "locks": frozenset({"required", "acquired", "order"}),
    "remote_transport": frozenset(
        {"attempted", "uploaded", "head_verified", "object"}
    ),
    "recovery": frozenset({"restore_drill", "production_restore"}),
}
REVISION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_public_text(
    value: object,
    *,
    pattern: re.Pattern[str],
    label: str,
    allow_unknown: bool = True,
) -> None:
    if allow_unknown and value == "unknown":
        return
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BackupEvidenceError(f"backup evidence {label} is invalid")


def _validate_section_keys(
    payload: Mapping[str, Any], section: str
) -> Mapping[str, Any]:
    value = payload.get(section)
    expected = EVIDENCE_SECTION_KEYS[section]
    if not isinstance(value, dict) or set(value) != expected:
        raise BackupEvidenceError(f"backup evidence {section} schema is not closed")
    return value


def validate_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != EVIDENCE_KEYS:
        raise BackupEvidenceError("backup evidence schema keys are not closed")
    if payload.get("schema") != EVIDENCE_SCHEMA or payload.get("kind") != EVIDENCE_KIND:
        raise BackupEvidenceError("backup evidence schema version is invalid")
    if payload.get("operation") not in OPERATION_LOCK_MATRIX:
        raise BackupEvidenceError("backup evidence operation is invalid")
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise BackupEvidenceError("backup evidence operation identity is invalid")
    if payload.get("status") not in EVIDENCE_STATUSES:
        raise BackupEvidenceError("backup evidence status is invalid")
    if not isinstance(payload.get("started_at_utc"), str) or len(payload["started_at_utc"]) > 64:
        raise BackupEvidenceError("backup evidence start timestamp is invalid")
    completed_at = payload.get("completed_at_utc")
    if completed_at is not None and (
        not isinstance(completed_at, str) or len(completed_at) > 64
    ):
        raise BackupEvidenceError("backup evidence completion timestamp is invalid")
    source = _validate_section_keys(payload, "source")
    _validate_public_text(
        source["dump_file"], pattern=SAFE_EVIDENCE_NAME_RE, label="dump name"
    )
    _validate_public_text(source["sha256"], pattern=SHA256_RE, label="dump digest")
    if source["size_bytes"] is not None and (
        type(source["size_bytes"]) is not int
        or source["size_bytes"] < 0
        or source["size_bytes"] > (2**63 - 1)
    ):
        raise BackupEvidenceError("backup evidence dump size is invalid")
    manifest = _validate_section_keys(payload, "manifest")
    _validate_public_text(
        manifest["file"], pattern=SAFE_EVIDENCE_NAME_RE, label="manifest name"
    )
    _validate_public_text(manifest["sha256"], pattern=SHA256_RE, label="manifest digest")
    _validate_public_text(manifest["run_id"], pattern=RUN_ID_RE, label="manifest run id")
    release = _validate_section_keys(payload, "release")
    _validate_public_text(release["source_sha"], pattern=SHA_RE, label="release SHA")
    alembic = _validate_section_keys(payload, "alembic")
    _validate_public_text(alembic["revision"], pattern=REVISION_RE, label="Alembic revision")
    if type(alembic["verified"]) is not bool:
        raise BackupEvidenceError("backup evidence Alembic state is invalid")
    if alembic["verified"] and alembic["revision"] == "unknown":
        raise BackupEvidenceError("verified backup evidence must bind an Alembic head")
    locks = _validate_section_keys(payload, "locks")
    for key in ("required", "acquired", "order"):
        if not isinstance(locks[key], list) or any(
            type(name) is not str or name not in LOCK_ORDER for name in locks[key]
        ):
            raise BackupEvidenceError("backup evidence lock list is invalid")
        if len(set(locks[key])) != len(locks[key]):
            raise BackupEvidenceError("backup evidence lock list has duplicates")
        validate_lock_sequence(locks[key])
    if locks["order"] != list(LOCK_ORDER):
        raise BackupEvidenceError("backup evidence lock order is invalid")
    expected_locks = list(operation_lock_requirements(payload["operation"]))
    if locks["required"] != expected_locks or locks["acquired"] != expected_locks:
        raise BackupEvidenceError("backup evidence lock ownership is invalid")
    remote = _validate_section_keys(payload, "remote_transport")
    for key in ("attempted", "uploaded", "head_verified"):
        if type(remote[key]) is not bool:
            raise BackupEvidenceError("backup evidence remote state is invalid")
    _validate_public_text(
        remote["object"], pattern=SAFE_EVIDENCE_NAME_RE, label="remote object"
    )
    recovery = _validate_section_keys(payload, "recovery")
    if recovery["restore_drill"] not in EVIDENCE_STATUSES - {"started"}:
        raise BackupEvidenceError("backup evidence restore state is invalid")
    if recovery["production_restore"] != "disabled":
        raise BackupEvidenceError("backup evidence production restore state is invalid")
    error_class = payload.get("error_class")
    if error_class is not None and (
        not isinstance(error_class, str) or SAFE_ERROR_RE.fullmatch(error_class) is None
    ):
        raise BackupEvidenceError("backup evidence error class is invalid")
    return dict(payload)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_evidence_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BackupEvidenceError("backup evidence directory metadata is unsafe")
    return path


def _write_private_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise BackupEvidenceError("backup evidence write failed")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_replace_private(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    replaced = False
    try:
        _write_private_new(temporary, payload)
        os.replace(temporary, path)
        replaced = True
        _sync_directory(path.parent)
    except BaseException:
        # If the directory fsync failed after rename, a green final record is
        # not trustworthy.  Remove this publication; the caller retains an
        # in-progress unknown record as the durable non-green state.
        if replaced:
            try:
                path.unlink()
            except OSError:
                pass
            try:
                _sync_directory(path.parent)
            except OSError:
                pass
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _publish_unknown_inprogress(path: Path, payload: Mapping[str, Any]) -> None:
    """Best-effort unknown receipt update that never removes the target."""

    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    try:
        _write_private_new(temporary, encoded)
        os.replace(temporary, path)
        try:
            _sync_directory(path.parent)
        except OSError:
            # The target is intentionally retained.  Readers already treat
            # any in-progress record as unknown even if this sync is lost.
            pass
    except (OSError, BackupEvidenceError):
        # Keep the pre-existing in-progress record if the replacement could
        # not be completed.  It is still interpreted as unknown by readers.
        pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def evidence_dir(app_dir: Path) -> Path:
    return _ensure_evidence_dir(Path(app_dir) / "shared" / BACKUP_EVIDENCE_DIRNAME)


def _evidence_basename(operation_id: str, *, inprogress: bool) -> str:
    return f"platform-backup-{operation_id}.json" + (".inprogress" if inprogress else "")


def _load_evidence(path: Path) -> dict[str, Any]:
    if path.name.endswith(".json") and not path.name.endswith(".json.inprogress"):
        inprogress = tuple(path.parent.glob("platform-backup-*.json.inprogress"))
        if inprogress:
            raise BackupEvidenceError("final backup evidence is shadowed by in-progress work")
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise BackupEvidenceError("backup evidence file metadata is unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return validate_evidence(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BackupEvidenceError("backup evidence is invalid") from exc


def recover_inprogress_evidence(app_dir: Path) -> tuple[Path, ...]:
    """Convert interrupted mutation records to terminal ``unknown`` records."""

    directory = evidence_dir(app_dir)
    recovered: list[Path] = []
    for path in sorted(directory.glob("platform-backup-*.json.inprogress")):
        try:
            payload = _load_evidence(path)
        except BackupEvidenceError:
            # Preserve an invalid record as non-green evidence.  Do not delete
            # forensic state and do not invent a successful result.
            continue
        payload["status"] = "unknown"
        payload["completed_at_utc"] = _utc_now()
        payload["error_class"] = "interrupted_evidence"
        final = path.with_name(path.name.removesuffix(".inprogress"))
        _publish_unknown_inprogress(path, payload)
        _atomic_replace_private(final, (json.dumps(payload, sort_keys=True) + "\n").encode())
        try:
            path.unlink()
            _sync_directory(directory)
        except OSError:
            pass
        recovered.append(final)
    return tuple(recovered)


def read_latest_evidence(app_dir: Path) -> dict[str, Any]:
    """Read the newest private evidence, treating any in-progress record as unknown."""

    directory = evidence_dir(app_dir)
    inprogress = sorted(directory.glob("platform-backup-*.json.inprogress"))
    if inprogress:
        try:
            payload = _load_evidence(inprogress[-1])
        except BackupEvidenceError:
            payload = _empty_evidence(
                operation="maintenance",
                operation_id="0" * 32,
                status="unknown",
                started_at="unknown",
            )
        payload["status"] = "unknown"
        payload["error_class"] = "interrupted_evidence"
        return payload
    finals = sorted(directory.glob("platform-backup-*.json"))
    if not finals:
        payload = _empty_evidence(
            operation="maintenance",
            operation_id="0" * 32,
            status="unknown",
            started_at="unknown",
        )
        payload["error_class"] = "missing_evidence"
        return payload
    return _load_evidence(finals[-1])


@dataclass(slots=True)
class EvidenceSession:
    app_dir: Path
    operation: str
    operation_id: str
    started_at: str
    path: Path
    payload: dict[str, Any]
    terminal_status: str | None = None
    terminal_error_class: str | None = None

    @classmethod
    def start(cls, app_dir: Path, operation: str, *, locks: tuple[str, ...]) -> "EvidenceSession":
        if operation not in OPERATION_LOCK_MATRIX:
            raise ValueError("unknown backup evidence operation")
        expected_locks = operation_lock_requirements(operation)
        if tuple(locks) != expected_locks:
            raise BackupEvidenceError("backup evidence lock ownership is invalid")
        recover_inprogress_evidence(app_dir)
        directory = evidence_dir(app_dir)
        operation_id = uuid4().hex
        started = _utc_now()
        path = directory / _evidence_basename(operation_id, inprogress=True)
        payload = _empty_evidence(
            operation=operation,
            operation_id=operation_id,
            status="started",
            started_at=started,
        )
        payload["locks"] = {
            "required": list(locks),
            "acquired": list(locks),
            "order": list(LOCK_ORDER),
        }
        _write_private_new(path, (json.dumps(payload, sort_keys=True) + "\n").encode())
        _sync_directory(directory)
        return cls(app_dir, operation, operation_id, started, path, payload)

    def update_pair(self, pair: BackupPairSnapshot) -> None:
        self.payload["source"] = {
            "dump_file": _safe_public_name(pair.dump_name),
            "sha256": _public_value(pair.dump_sha256, pattern=SHA256_RE),
            "size_bytes": pair.size_bytes,
        }
        self.payload["manifest"] = {
            "file": _safe_public_name(pair.manifest_name),
            "sha256": _public_value(pair.manifest_sha256, pattern=SHA256_RE),
            "run_id": _public_value(pair.run_id, pattern=RUN_ID_RE),
        }

    def update_release(self, source_sha: object) -> None:
        self.payload["release"] = {
            "source_sha": _public_value(source_sha, pattern=SHA_RE)
        }

    def update_remote(self, **values: object) -> None:
        section = self.payload["remote_transport"]
        for key in ("attempted", "uploaded", "head_verified"):
            if key in values:
                section[key] = bool(values[key])
        if "object" in values:
            section["object"] = _safe_public_name(values["object"])

    def mark_terminal(self, status: str, *, error_class: str | None = None) -> None:
        if status not in EVIDENCE_STATUSES - {"started"}:
            raise BackupEvidenceError("invalid requested evidence status")
        self.terminal_status = status
        self.terminal_error_class = error_class

    def finish(self, status: str, *, error_class: str | None = None) -> Path:
        if status not in EVIDENCE_STATUSES - {"started"}:
            raise BackupEvidenceError("cannot publish a non-terminal evidence status")
        if error_class is not None and not SAFE_ERROR_RE.fullmatch(error_class):
            error_class = "internal_failure"
        self.payload["status"] = status
        self.payload["completed_at_utc"] = _utc_now()
        self.payload["error_class"] = error_class
        validate_evidence(self.payload)
        directory = self.path.parent
        final = directory / _evidence_basename(self.operation_id, inprogress=False)
        try:
            _atomic_replace_private(
                final, (json.dumps(self.payload, sort_keys=True) + "\n").encode()
            )
        except BaseException as exc:
            unknown = dict(self.payload)
            unknown["status"] = "unknown"
            unknown["completed_at_utc"] = _utc_now()
            unknown["error_class"] = "evidence_publish_failed"
            _publish_unknown_inprogress(self.path, unknown)
            raise BackupEvidenceError("backup evidence final publication failed") from exc
        try:
            self.path.unlink()
            _sync_directory(directory)
        except OSError as exc:
            try:
                final.unlink()
            except OSError:
                pass
            try:
                _sync_directory(directory)
            except OSError:
                pass
            unknown = dict(self.payload)
            unknown["status"] = "unknown"
            unknown["completed_at_utc"] = _utc_now()
            unknown["error_class"] = "evidence_cleanup_failed"
            _publish_unknown_inprogress(self.path, unknown)
            raise BackupEvidenceError("backup evidence in-progress cleanup failed") from exc
        return final


@contextmanager
def evidence_session(app_dir: Path, operation: str, *, locks: tuple[str, ...]) -> Iterator[EvidenceSession]:
    session = EvidenceSession.start(app_dir, operation, locks=locks)
    previous_handlers: dict[int, Any] = {}
    if threading_is_main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[number] = signal.getsignal(number)
            signal.signal(number, _cancel_signal_handler)
    try:
        yield session
    except KeyboardInterrupt:
        session.finish("cancelled", error_class="cancelled")
        raise
    except BackupLockConflict:
        session.finish("blocked", error_class="backup_lock_conflict")
        raise
    except BaseException as exc:
        # Never serialize exception text: it may contain paths, credentials,
        # SQL, or child-process stderr.  The type is intentionally coarse.
        error_class = "cancelled" if isinstance(exc, (SystemExit,)) else "operation_failed"
        session.finish("failed", error_class=error_class)
        raise
    else:
        session.finish(
            session.terminal_status or "passed",
            error_class=session.terminal_error_class,
        )
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)


def threading_is_main_thread() -> bool:
    # Import lazily so the supervisor remains usable in isolated tool
    # interpreters without adding another global dependency during import.
    import threading

    return threading.current_thread() is threading.main_thread()


def _cancel_signal_handler(signum: int, _frame: Any) -> None:
    del signum
    raise KeyboardInterrupt


@contextmanager
def _machine_live_qa_lock() -> Iterator[int | None]:
    """Acquire the live-QA machine lock without calling its release wrapper."""

    try:
        module = importlib.import_module("tools.platform_live_qa_guard")
    except ImportError:
        try:
            module = importlib.import_module("platform_live_qa_guard")
        except ImportError:
            yield None
            return
    opener = getattr(module, "_open_machine_lock", None)
    if opener is None:
        yield None
        return
    descriptor = opener()
    try:
        yield descriptor
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def ordered_backup_lock_scope(
    app_dir: Path,
    *,
    source_release_dir: Path | None = None,
    include_predecessors: bool = True,
) -> Iterator[tuple[BackupLockHandle, int | None]]:
    """Yield held locks in the exact global order.

    Mutation authority is issued by the operation-specific supervisor scope
    while this context is held; it is intentionally not returned with the
    lock tuple.
    """

    if not include_predecessors:
        with exclusive_backup_lock() as backup_lock:
            yield backup_lock, None
        return

    try:
        retention = importlib.import_module("tools.platform_release_retention")
    except ImportError:
        retention = importlib.import_module("platform_release_retention")
    try:
        maintenance = importlib.import_module("tools.platform_storage_maintenance")
    except ImportError:
        maintenance = importlib.import_module("platform_storage_maintenance")
    source = source_release_dir
    if source is None:
        source = Path("/opt/oldsparky/platform/dist/releases")
    source = Path(source)
    if not source.exists() or not source.is_dir() or source.is_symlink():
        raise BackupSupervisorError("canonical source release contour is unavailable")
    with retention.release_operation_lock(Path(app_dir)):
        with retention.exclusive_retained_load_lock():
            source_scope = maintenance.source_release_lock(source)
            with source_scope:
                # The live-QA lock must precede backup, including when no
                # runtime cache is selected.  The backup lock is always last.
                with _machine_live_qa_lock() as live_qa_lock_fd:
                    with exclusive_backup_lock() as backup_lock:
                        yield backup_lock, live_qa_lock_fd


def _source_sha_from_release(app_dir: Path) -> str:
    try:
        current = Path(app_dir) / "current"
        release_json = current.resolve(strict=True) / "RELEASE.json"
        payload = json.loads(release_json.read_text(encoding="utf-8"))
        value = payload.get("source_git_commit") if isinstance(payload, dict) else None
        return value if isinstance(value, str) and SHA_RE.fullmatch(value) else "unknown"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown"


def _restore_args(
    app_dir: Path,
    *,
    keep: int,
    env_file: Path | None = None,
    output_dir: Path | None = None,
    admin_database_url: str | None = None,
) -> argparse.Namespace:
    shared = Path(app_dir) / "shared"
    return SimpleNamespace(
        env_file=str(env_file or (shared / ".env.platform")),
        output_dir=str(output_dir or (shared / "backups")),
        keep=keep,
        admin_database_url=admin_database_url,
        dump_only=False,
    )


def run_local_backup(
    app_dir: Path,
    *,
    keep: int = 14,
    max_age_hours: float = 24.0,
    env_file: Path | None = None,
    output_dir: Path | None = None,
    admin_database_url: str | None = None,
    trusted_alembic_head: object,
    source_root: Path | None = None,
    capability: object,
    lock: BackupLockHandle,
    evidence: EvidenceSession,
) -> dict[str, Any]:
    """Create, verify, and prune one local backup under the final lock."""

    lock.validate()
    require_mutation_capability(capability, "maintenance")
    try:
        restore = importlib.import_module("tools.platform_backup_restore_drill")
    except ImportError:
        # The installed production entrypoint is executed by absolute path;
        # in that mode its sibling directory, rather than the repository
        # package root, is on sys.path.
        restore = importlib.import_module("platform_backup_restore_drill")
    trusted_source = Path(source_root or (Path(app_dir) / "current"))
    trusted_alembic_head = require_trusted_alembic_head(
        trusted_alembic_head,
        source_root=trusted_source,
    )
    restore_args = _restore_args(
        app_dir,
        keep=keep,
        env_file=env_file,
        output_dir=output_dir,
        admin_database_url=admin_database_url,
    )
    # The supervisor, not the low-level producer, owns rotation.  This path
    # passes the in-process capability and explicitly disables nested pruning.
    created = restore.create_backup(
        restore_args,
        prune=False,
        capability=capability,
        trusted_alembic_head=trusted_alembic_head,
    )
    output_dir = Path(restore_args.output_dir)
    dump = output_dir / str(created["dump_file"])
    manifest = dump.with_suffix(".json")
    pair = snapshot_backup_pair(dump, manifest)
    evidence.update_pair(pair)
    evidence.payload["recovery"] = {
        "restore_drill": "passed" if created.get("restore_verified") else "failed",
        "production_restore": "disabled",
    }
    evidence.payload["alembic"] = {
        "revision": (
            created.get("alembic_revision")
            if created.get("alembic_revision_verified")
            else "unknown"
        ),
        "verified": bool(created.get("alembic_revision_verified")),
    }
    # Freshness is proven by the exact manifest and archive pair while the
    # backup lock is held; no second check-latest subprocess can race it.
    completed = created.get("completed_at_utc")
    if not isinstance(completed, str):
        raise BackupSupervisorError("backup completion timestamp is unavailable")
    manifest_module = _manifest_module()
    manifest_file = manifest_module.read_manifest_file(
        manifest,
        expected_owner=os.geteuid(),
        expected_group=os.getegid(),
        expected_dump_file=dump.name,
    )
    age_hours = (datetime.now(UTC) - manifest_file.manifest.completed_at_utc).total_seconds() / 3600
    if age_hours < -(5 / 60) or age_hours > max_age_hours:
        raise BackupSupervisorError("restore-verified backup freshness window failed")
    # Re-check immediately before and after every consumer-like prune phase.
    assert_backup_pair_unchanged(pair, dump, manifest)
    removed: list[str] = []
    if created.get("restore_verified") is not True:
        raise BackupSupervisorError("backup restore drill did not pass")
    removed.extend(
        restore.prune_unverified_backups(
            output_dir, preserve_metadata=manifest, capability=capability
        )
    )
    assert_backup_pair_unchanged(pair, dump, manifest)
    removed.extend(restore.prune_backups(output_dir, keep=keep, capability=capability))
    # The selected pair must remain intact even if pruning saw a malformed
    # unrelated archive.  This also proves no writer replaced it during prune.
    assert_backup_pair_unchanged(pair, dump, manifest)
    lock.validate()
    return {
        "status": "completed",
        "size_bytes": pair.size_bytes,
        "duration_seconds": created.get("duration_seconds"),
        "restore_verified": True,
        "alembic_revision_verified": True,
        "checksum_present": True,
        "restored_table_count": created.get("restored_table_count"),
        "age_hours": round(age_hours, 3),
        "removed_count": len(removed),
        "dump_file": pair.dump_name,
        "manifest_file": pair.manifest_name,
        "sha256": pair.dump_sha256,
    }


def run_offsite(
    args: argparse.Namespace,
    *,
    app_dir: Path,
    trusted_alembic_head: object,
    capability: object,
    lock: BackupLockHandle,
    evidence: EvidenceSession,
    client: Any | None = None,
) -> dict[str, Any]:
    """Select/encrypt/upload/head-verify one exact pair under backup lock."""

    lock.validate()
    require_mutation_capability(capability, "offsite")
    try:
        offsite = importlib.import_module("tools.platform_backup_offsite")
    except ImportError:
        offsite = importlib.import_module("platform_backup_offsite")
    backup = offsite.select_verified_backup(
        Path(args.backup_dir), getattr(args, "dump", None),
        max_age_hours=float(args.max_age_hours), apply=bool(args.apply),
    )
    work = Path(tempfile.mkdtemp(prefix="oldsparky-offsite-"))
    work.chmod(0o700)
    encrypted: Any | None = None
    try:
        with held_backup_pair(backup.dump_path, backup.metadata_path) as held:
            pair = held.snapshot
            # Selection happened by pathname.  Refuse a selected object whose
            # exact held identity/content no longer matches that selection,
            # even if an attacker replaced both files with valid-looking data.
            if (
                backup.dump_path != held.dump_path
                or backup.metadata_path != held.manifest_path
                or backup.plaintext_sha256 != pair.dump_sha256
                or backup.metadata_sha256 != pair.manifest_sha256
                or backup.size_bytes != pair.size_bytes
                or (
                    backup.dump_identity is not None
                    and backup.dump_identity != pair.dump_identity
                )
                or (
                    backup.metadata_identity is not None
                    and backup.metadata_identity != pair.manifest_identity
                )
            ):
                raise BackupSupervisorError("selected backup pair changed before pinning")
            held.validate()
            evidence.update_pair(pair)
            trusted_head = require_trusted_alembic_head(
                trusted_alembic_head,
                source_root=Path(app_dir) / "current",
            )
            evidence.payload["alembic"] = {
                "revision": trusted_head.value,
                "verified": True,
            }
            config = offsite.load_config(
                args.env_file, args.platform_env_file, apply=bool(args.apply)
            )
            encrypted = offsite.encrypt_backup_from_fd(
                config,
                backup,
                work,
                source_fd=held.dump_fd,
                validate_source=held.validate,
                apply=bool(args.apply),
            )
            held.validate()
            key = offsite.object_key(config, backup)
            result: dict[str, Any] = {
                "ok": True,
                "mode": "apply" if args.apply else "dry-run",
                "source_dump": pair.dump_name,
                "source_sha256": pair.dump_sha256,
                "cipher_sha256": encrypted.sha256,
                "cipher_size_bytes": encrypted.size_bytes,
                "bucket": config.bucket_name,
                "object_key": key,
                "uploaded": False,
                "verified": False,
                "remote_operations": 0,
                "retention_actions": 0,
            }
            if not args.apply:
                evidence.update_remote(attempted=False, object=key)
                held.validate()
                lock.validate()
                return result
            storage_client = (
                client
                if client is not None
                else offsite.build_storage_client(config, timeout=float(args.timeout))
            )
            evidence.update_remote(attempted=True, object=key)
            held.validate()
            storage_client.head_bucket(Bucket=config.bucket_name)
            held.validate()
            uploaded, remote = offsite.upload_and_verify(
                storage_client,
                config=config,
                backup=backup,
                encrypted=encrypted,
                key=key,
                encrypted_fd=encrypted.fd,
            )
            held.validate()
            evidence.update_remote(
                attempted=True,
                uploaded=uploaded,
                head_verified=True,
                object=key,
            )
            result.update(
                {
                    "cipher_sha256": remote["cipher_sha256"],
                    "uploaded": uploaded,
                    "verified": True,
                    "remote_operations": 4 if uploaded else 2,
                }
            )
            lock.validate()
            return result
    finally:
        if encrypted is not None and getattr(encrypted, "fd", None) is not None:
            try:
                os.close(encrypted.fd)
            except OSError:
                pass
        # The temporary ciphertext is deliberately private and never becomes
        # evidence.  Keep cleanup bounded to the supervisor-owned directory.
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def run_production_restore(*_args: object, **_kwargs: object) -> None:
    raise ProductionRestoreDisabled(
        "destructive production restore is disabled; use the documented operator recovery gate"
    )


def _operation_error_status(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, BackupLockConflict):
        return "blocked", "backup_lock_conflict"
    if isinstance(exc, ProductionRestoreDisabled):
        return "blocked", "production_restore_disabled"
    if isinstance(exc, KeyboardInterrupt):
        return "cancelled", "cancelled"
    return "failed", "operation_failed"


def run_offsite_entrypoint(args: argparse.Namespace, *, app_dir: Path) -> dict[str, Any]:
    requirements = operation_lock_requirements("offsite")
    # Start evidence before attempting the non-blocking lock so a contention
    # outcome is durable as ``blocked`` rather than disappearing before the
    # evidence boundary exists.
    with evidence_session(app_dir, "offsite", locks=requirements) as evidence:
        with exclusive_backup_lock() as lock:
            return _run_offsite_scope(
                args,
                app_dir=app_dir,
                lock=lock,
                callback=lambda capability, trusted_head, _restore: run_offsite(
                    args,
                    app_dir=app_dir,
                    trusted_alembic_head=trusted_head,
                    capability=capability,
                    lock=lock,
                    evidence=evidence,
                ),
            )


def run_backup_entrypoint(
    args: argparse.Namespace,
    *,
    app_dir: Path,
    source_release_dir: Path | None = None,
) -> dict[str, Any]:
    requirements = operation_lock_requirements("local-backup")
    with evidence_session(app_dir, "local-backup", locks=requirements) as evidence:
        with ordered_backup_lock_scope(
            app_dir, source_release_dir=source_release_dir, include_predecessors=True
        ) as (lock, _live_qa_lock_fd):
            evidence.update_release(_source_sha_from_release(app_dir))
            return _run_local_backup_scope(
                args,
                app_dir=app_dir,
                lock=lock,
                callback=lambda capability, trusted_head, _restore: run_local_backup(
                    app_dir,
                    keep=int(getattr(args, "keep", 14)),
                    max_age_hours=float(getattr(args, "max_age_hours", 24.0)),
                    env_file=getattr(args, "env_file", None),
                    output_dir=getattr(args, "output_dir", None),
                    admin_database_url=getattr(args, "admin_database_url", None),
                    trusted_alembic_head=trusted_head,
                    capability=capability,
                    lock=lock,
                    evidence=evidence,
                ),
            )


def run_maintenance_entrypoint(args: argparse.Namespace) -> dict[str, Any]:
    """Route the production maintenance unit through this supervisor."""

    app_dir = Path(args.app_dir).resolve(strict=True)
    requirements = operation_lock_requirements("maintenance")
    with evidence_session(app_dir, "maintenance", locks=requirements) as evidence:
        with ordered_backup_lock_scope(
            app_dir,
            source_release_dir=getattr(args, "source_release_dir", None),
            include_predecessors=True,
        ) as (lock, live_qa_lock_fd):
            evidence.update_release(_source_sha_from_release(app_dir))
            try:
                storage = importlib.import_module("tools.platform_storage_maintenance")
            except ImportError:
                storage = importlib.import_module("platform_storage_maintenance")
            report = _run_maintenance_scope(
                lock=lock,
                callback=lambda capability: storage.run_maintenance(
                    args,
                    _supervisor_capability=capability,
                    _backup_lock=lock,
                    _live_qa_lock_fd=live_qa_lock_fd,
                    _locks_held=True,
                    _evidence=evidence,
                ),
            )
            if report.get("ok") is False:
                evidence.mark_terminal("failed", error_class="storage_health_failed")
            write_report = getattr(storage, "write_report", None)
            if callable(write_report):
                write_report(
                    app_dir / "shared" / "maintenance",
                    report,
                    keep=int(getattr(args, "report_keep", 30)),
                )
            return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Old Sparky backup operation supervisor")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    maintenance = subparsers.add_parser("maintenance")
    maintenance.add_argument("--app-dir", type=Path, default=Path("/opt/oldsparky/platform"))
    maintenance.add_argument("--source-release-dir", type=Path, default=Path("/opt/oldsparky/platform/dist/releases"))
    maintenance.add_argument("--web-artifact-dir", type=Path, default=Path("/root/old_sparky/platform/apps/platform_web"))
    maintenance.add_argument("--backup-keep", type=int, default=14)
    maintenance.add_argument("--backup-max-age-hours", type=float, default=24.0)
    maintenance.add_argument("--release-keep", type=int, default=5)
    maintenance.add_argument("--test-artifact-max-age-days", type=int, default=7)
    maintenance.add_argument("--screenshot-max-age-days", type=int, default=30)
    maintenance.add_argument("--failed-build-max-age-days", type=int, default=1)
    maintenance.add_argument("--report-keep", type=int, default=30)
    maintenance.add_argument("--live-qa-runtime-keep", type=int, default=1)
    maintenance.add_argument("--minimum-free-gib", type=float, default=5.0)
    maintenance.add_argument("--maximum-used-percent", type=float, default=85.0)
    maintenance.add_argument("--skip-backup", action="store_true")
    maintenance.add_argument("--backup-only", action="store_true")
    maintenance.add_argument("--apply", action="store_true")
    maintenance.add_argument("--json", action="store_true", dest="as_json")
    backup = subparsers.add_parser("backup")
    backup.add_argument("--app-dir", type=Path, default=Path("/opt/oldsparky/platform"))
    backup.add_argument("--source-release-dir", type=Path, default=Path("/opt/oldsparky/platform/dist/releases"))
    backup.add_argument("--backup-keep", "--keep", dest="keep", type=int, default=14)
    backup.add_argument(
        "--backup-max-age-hours", "--max-age-hours", dest="max_age_hours", type=float, default=24.0
    )
    backup.add_argument("--env-file", type=Path)
    backup.add_argument("--output-dir", type=Path)
    backup.add_argument("--admin-database-url")
    backup.add_argument("--check-latest", action="store_true")
    backup.add_argument("--json", action="store_true", dest="as_json")
    offsite = subparsers.add_parser("offsite")
    offsite.add_argument("--app-dir", type=Path, default=Path("/opt/oldsparky/platform"))
    offsite.add_argument("--apply", action="store_true")
    offsite.add_argument("--env-file", type=Path, default=Path("/opt/oldsparky/platform/shared/.env.backup"))
    offsite.add_argument("--platform-env-file", type=Path, default=Path("/opt/oldsparky/platform/shared/.env.platform"))
    offsite.add_argument("--backup-dir", type=Path, default=Path("/opt/oldsparky/platform/shared/backups"))
    offsite.add_argument("--dump", type=Path)
    offsite.add_argument("--max-age-hours", type=float, default=30.0)
    offsite.add_argument("--timeout", type=float, default=20.0)
    offsite.add_argument("--json", action="store_true", dest="as_json")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--production", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.operation == "offsite":
            if args.apply:
                result = run_offsite_entrypoint(args, app_dir=args.app_dir)
            else:
                try:
                    offsite = importlib.import_module("tools.platform_backup_offsite")
                except ImportError:
                    offsite = importlib.import_module("platform_backup_offsite")
                # Read-only diagnostics keep the legacy dry-run path and do
                # not create a mutation evidence record or acquire the final
                # lock.  Apply mode above is the sole production owner.
                result = offsite.execute(args)
        elif args.operation == "backup":
            if args.check_latest:
                try:
                    restore = importlib.import_module("tools.platform_backup_restore_drill")
                except ImportError:
                    restore = importlib.import_module("platform_backup_restore_drill")
                output_dir = args.output_dir or (args.app_dir / "shared" / "backups")
                result = restore.check_latest_backup(
                    output_dir, max_age_hours=float(args.max_age_hours)
                )
            else:
                result = run_backup_entrypoint(
                    args,
                    app_dir=args.app_dir,
                    source_release_dir=args.source_release_dir,
                )
        elif args.operation == "maintenance":
            if args.apply:
                result = run_maintenance_entrypoint(args)
            else:
                try:
                    storage = importlib.import_module("tools.platform_storage_maintenance")
                except ImportError:
                    storage = importlib.import_module("platform_storage_maintenance")
                result = storage.run_maintenance(args)
        else:
            run_production_restore()
            result = {"ok": False}
        if getattr(args, "as_json", False):
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print("[OK] Platform backup supervisor completed")
        return 0 if result.get("ok", True) is not False else 1
    except BaseException as exc:
        status, error_class = _operation_error_status(exc)
        payload = {"ok": False, "status": status, "error_class": error_class}
        if getattr(args, "as_json", False):
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"[FAIL] Platform backup supervisor ({error_class})", file=os.sys.stderr)
        return STATUS_EXIT_CODES.get(status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
