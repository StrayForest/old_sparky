#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import pwd
import re
import selectors
import secrets
import shlex
import signal
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from typing import Any, BinaryIO, Callable


def _load_staged_disk_policy() -> Any:
    helper_path = pathlib.Path(__file__).resolve().with_name("platform_disk_policy.py")
    spec = importlib.util.spec_from_file_location(
        "_oldsparky_backup_restore_disk_policy", helper_path
    )
    if spec is None or spec.loader is None:
        raise ImportError("platform disk policy helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:
    from .platform_disk_policy import (
        DEFAULT_MIN_FREE_GIB,
        minimum_free_bytes,
        snapshot_for_path,
    )
except ImportError:  # Direct execution from the tools directory.
    _disk_policy = _load_staged_disk_policy()
    DEFAULT_MIN_FREE_GIB = _disk_policy.DEFAULT_MIN_FREE_GIB
    minimum_free_bytes = _disk_policy.minimum_free_bytes
    snapshot_for_path = _disk_policy.snapshot_for_path


DEFAULT_ENV_FILE = pathlib.Path("/opt/oldsparky/platform/shared/.env.platform")
DEFAULT_OUTPUT_DIR = pathlib.Path("/opt/oldsparky/platform/shared/backups")
LOCAL_DATABASE_HOSTS = {None, "", "127.0.0.1", "localhost", "::1"}
REQUIRED_PLATFORM_EXTENSIONS = ("pg_trgm",)
RESTORE_DISK_LEAD_BYTES = 256 * 1024**2
RESTORE_DISK_SAMPLE_SECONDS = 0.25
RESTORE_TERMINATE_TIMEOUT_SECONDS = 1.0
RESTORE_DISK_MINIMUM_BYTES = minimum_free_bytes(DEFAULT_MIN_FREE_GIB)
SCRATCH_TMPFS_BYTES = 512 * 1024**2
SCRATCH_MEMORY_MAX_BYTES = 640 * 1024**2
SCRATCH_MEMORY_RESERVE_BYTES = 384 * 1024**2
SCRATCH_MEMORY_ADMISSION_BYTES = SCRATCH_MEMORY_MAX_BYTES + SCRATCH_MEMORY_RESERVE_BYTES
SCRATCH_ADMISSION_RSS_MAX_BYTES = 1 << 40
SCRATCH_PROC_STATUS_MAX_BYTES = 16 * 1024
SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES = 16 * 1024
SCRATCH_RESOURCE_ATTRIBUTION_MAX_BYTES = 4096
SCRATCH_MEMORY_STOP_MARGIN_BYTES = 32 * 1024**2
SCRATCH_TMPFS_STOP_BYTES = 64 * 1024**2
SCRATCH_RUNTIME_SECONDS = 15 * 60
SCRATCH_STARTUP_SECONDS = 30
SCRATCH_RUNTIME_SAMPLE_SECONDS = 0.25
SCRATCH_EARLY_EXIT_DRAIN_SECONDS = 2.0
SCRATCH_EARLY_EXIT_DRAIN_BYTES = 1024 * 1024
SCRATCH_PG_PORT = 55432
SCRATCH_WORKER_MODE = "--_scratch-restore-worker"
SCRATCH_START_TOKEN = b"OLDSPARKY_SCRATCH_RESTORE_START_V1\n"
SCRATCH_DONE_TOKEN = b"OLDSPARKY_SCRATCH_RESTORE_DONE_V1\n"
SCRATCH_APP_ROLE = "platform_user"
SCRATCH_PRIVATE_OUTPUT_MARKER = b"SCRATCH_UNIT_PRIVATE_OUTPUT_V1\n"
SCRATCH_FIRST_FAULT_MARKER = b"SCRATCH_FIRST_FAULT_V1\n"
SCRATCH_PHASE_FRAME_MARKER = b"SCRATCH_PHASE_V1:"
SCRATCH_PHASE_FRAME_MAX_BYTES = 192
SCRATCH_PHASE_FRAME_MAX_COUNT = 12
SCRATCH_PHASE_OFFSET_MAX_MS = (SCRATCH_RUNTIME_SECONDS + SCRATCH_STARTUP_SECONDS + 30) * 1000
SCRATCH_PHASES = (
    "archive_verify",
    "initdb",
    "postgres_start",
    "restore_platform",
    "restore_public",
    "alembic_verify",
)
SCRATCH_PRIVATE_STDOUT_BYTES = 2 * 1024
SCRATCH_PRIVATE_STDERR_BYTES = 32 * 1024
SCRATCH_PRIVATE_RAW_BYTES = SCRATCH_PRIVATE_STDOUT_BYTES + SCRATCH_PRIVATE_STDERR_BYTES
SCRATCH_PRIVATE_ENVELOPE_BYTES = 48 * 1024
SCRATCH_AUTHORIZATION_PROBE_FRAME = (
    b'{"kind":"authorization_probe","schema":1,"status":"completed"}\n'
)

_SCRATCH_FIRST_FAULT_ORIGINS = frozenset({"controller", "worker"})
_SCRATCH_FIRST_FAULT_SITES = frozenset(
    {
        "controller_entry",
        "controller_archive_identity",
        "controller_unit_setup",
        "controller_host_guard",
        "controller_launch",
        "controller_unit_snapshot",
        "controller_identity",
        "controller_start_token",
        "controller_active_poll",
        "controller_worker_frame",
        "controller_completion_snapshot",
        "controller_done_token",
        "controller_final_snapshot",
        "controller_cleanup",
        "worker_entry",
        "worker_start_token",
        "worker_root_identity",
        "worker_mount_snapshot",
        "worker_cgroup_snapshot",
        "worker_host_reserve",
        "worker_archive_open",
        "worker_directory_setup",
        "worker_postgres_tools",
        "worker_initdb",
        "worker_after_initdb_guard",
        "worker_postgres_start",
        "worker_after_postgres_start_guard",
        "worker_role_setup",
        "worker_pg_restore_resource_guard",
        "worker_restore",
        "worker_after_restore_guard",
        "worker_postgres_stop",
        "worker_result_frame",
        "worker_done_token",
    }
)
_SCRATCH_FIRST_FAULT_KINDS = frozenset(
    {
        "permission",
        "os_error",
        "timeout",
        "subprocess",
        "json_decode",
        "unicode",
        "attribute",
        "index",
        "name",
        "type",
        "key",
        "value",
        "restore_guard_stop",
        "backup_interrupted",
        "scratch_token_delivery",
        "runtime",
        "other",
    }
)


def valid_scratch_first_fault(value: Any) -> bool:
    """Validate a closed first-fault projection without accepting raw details."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "origin", "site", "exception_kind", "errno"
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("origin")) is not str
        or value["origin"] not in _SCRATCH_FIRST_FAULT_ORIGINS
        or type(value.get("site")) is not str
        or value["site"] not in _SCRATCH_FIRST_FAULT_SITES
        or type(value.get("exception_kind")) is not str
        or value["exception_kind"] not in _SCRATCH_FIRST_FAULT_KINDS
        or (
            value.get("errno") is not None
            and (type(value["errno"]) is not int or not 1 <= value["errno"] <= 4095)
        )
    ):
        return False
    expected_origin = (
        "controller" if value["site"].startswith("controller_")
        else "worker" if value["site"].startswith("worker_")
        else None
    )
    if value["origin"] != expected_origin:
        return False
    if value["exception_kind"] in {"permission", "os_error"}:
        return value["errno"] is None or type(value["errno"]) is int
    return value["errno"] is None


def _scratch_exception_projection(error: BaseException) -> tuple[str, int | None]:
    """Map exception chains to fixed kinds; never return class or message text."""
    current: BaseException | None = error
    seen: set[int] = set()
    candidates: list[BaseException] = []
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        candidates.append(current)
        current = current.__cause__ or current.__context__

    def kind(item: BaseException) -> str:
        if isinstance(item, RestoreGuardStop):
            return "restore_guard_stop"
        if isinstance(item, BackupOperationInterrupted):
            return "backup_interrupted"
        if isinstance(item, _ScratchTokenDeliveryError):
            return "scratch_token_delivery"
        if isinstance(item, PermissionError):
            return "permission"
        if isinstance(item, OSError):
            return "os_error"
        if isinstance(item, subprocess.TimeoutExpired):
            return "timeout"
        if isinstance(item, subprocess.SubprocessError):
            return "subprocess"
        if isinstance(item, json.JSONDecodeError):
            return "json_decode"
        if isinstance(item, UnicodeError):
            return "unicode"
        if isinstance(item, AttributeError):
            return "attribute"
        if isinstance(item, IndexError):
            return "index"
        if isinstance(item, NameError):
            return "name"
        if isinstance(item, TypeError):
            return "type"
        if isinstance(item, KeyError):
            return "key"
        if isinstance(item, ValueError):
            return "value"
        if isinstance(item, RuntimeError):
            return "runtime"
        return "other"

    for candidate in candidates:
        selected = kind(candidate)
        if selected not in {"runtime", "other"}:
            errno = candidate.errno if isinstance(candidate, OSError) else None
            return selected, errno if type(errno) is int and 1 <= errno <= 4095 else None
    selected = kind(error)
    errno = error.errno if isinstance(error, OSError) else None
    return selected, errno if type(errno) is int and 1 <= errno <= 4095 else None


def _scratch_first_fault(error: BaseException, *, origin: str, site: str) -> dict[str, Any] | None:
    exception_kind, errno = _scratch_exception_projection(error)
    value = {
        "schema": 1,
        "origin": origin,
        "site": site,
        "exception_kind": exception_kind,
        "errno": errno if exception_kind in {"permission", "os_error"} else None,
    }
    return value if valid_scratch_first_fault(value) else None


def _retain_first_scratch_fault(error: BaseException, *, origin: str, site: str) -> None:
    if getattr(error, "scratch_first_fault", None) is not None:
        return
    diagnostic = _scratch_first_fault(error, origin=origin, site=site)
    if diagnostic is not None:
        setattr(error, "scratch_first_fault", diagnostic)


def _scratch_cleanup_failure(
    primary_error: BaseException | None,
    cleanup_errors: list[BaseException],
    *,
    authorization_probe: bool,
) -> BaseException | None:
    """Keep cleanup status separate and never replace an existing first fault."""
    if not cleanup_errors:
        if primary_error is not None and authorization_probe:
            setattr(primary_error, "scratch_cleanup_status", "confirmed")
        return None
    if primary_error is not None:
        if authorization_probe:
            setattr(primary_error, "scratch_cleanup_status", "failed")
        if hasattr(primary_error, "add_note"):
            primary_error.add_note("Scratch restore final cleanup also failed.")
        return None
    cleanup_error = RuntimeError(
        "Scratch restore unit or mount cleanup could not be confirmed."
    )
    if authorization_probe:
        setattr(cleanup_error, "scratch_authorization_probe", True)
        setattr(cleanup_error, "scratch_cleanup_status", "failed")
    _retain_first_scratch_fault(
        cleanup_error,
        origin="controller",
        site="controller_cleanup",
    )
    return cleanup_error


def _scratch_first_fault_frame(value: Any) -> bytes | None:
    if not valid_scratch_first_fault(value):
        return None
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    return SCRATCH_FIRST_FAULT_MARKER + payload + b"\n"


class RestoreGuardStop(RuntimeError):
    """A fixed, non-sensitive reason for stopping an owned restore child."""

    def __init__(
        self,
        reason: str,
        *,
        free_bytes: int | None = None,
        required_free_bytes: int | None = None,
        phase: str | None = None,
    ) -> None:
        if reason not in _RESTORE_GUARD_MESSAGES:
            raise ValueError("unknown restore guard reason")
        self.reason = reason
        self.free_bytes = free_bytes
        self.required_free_bytes = required_free_bytes
        self.phase = phase
        super().__init__(_RESTORE_GUARD_MESSAGES[reason])

    def private_backup_disk_diagnostic(self) -> dict[str, Any] | None:
        if self.phase not in _BACKUP_DISK_DIAGNOSTIC_PHASES:
            return None
        value = {
            "schema": 1,
            "phase": self.phase,
            "guard_reason": self.reason,
            "free_bytes": self.free_bytes,
            "required_free_bytes": self.required_free_bytes,
        }
        return value if valid_backup_disk_diagnostic(value) else None


class BackupOperationInterrupted(RuntimeError):
    """A termination signal converted for rollback-safe backup CLI cleanup."""

    def __init__(self, signum: int) -> None:
        label = {
            signal.SIGHUP: "SIGHUP",
            signal.SIGINT: "SIGINT",
            signal.SIGTERM: "SIGTERM",
        }.get(signum, "signal")
        super().__init__(f"Backup operation interrupted by {label}.")


@contextlib.contextmanager
def _termination_signal_scope():
    """Turn CLI termination into rollback-safe exceptions for one operation."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Backup operation signal cleanup requires the main thread.")
    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    }

    def interrupt_operation(signum: int, _frame: Any) -> None:
        # Ignore reentrant termination until rollback, unit stop/reap, and
        # inode-pinned cleanup finish. The owning context restores prior handlers.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise BackupOperationInterrupted(signum)

    for signum in previous_handlers:
        signal.signal(signum, interrupt_operation)
    try:
        yield
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


_RESTORE_GUARD_MESSAGES = {
    "disk_floor": "Restore drill stopped by the low-disk safety guard.",
    "disk_unavailable": "Restore drill stopped because disk space could not be verified.",
    "child_unstopped": "Backup or restore command could not be confirmed stopped.",
    "command_start_failed": "Backup or restore command could not be started.",
    "command_failed": "Backup or restore command exited unsuccessfully.",
}
_BACKUP_DISK_DIAGNOSTIC_PHASES = {
    "backup_admission",
    "pg_dump_pre_spawn",
    "pg_dump_post_start",
    "pg_dump_monitor",
    "pg_dump_post_exit",
}
_BACKUP_DISK_DIAGNOSTIC_REASONS = {
    "disk_floor",
    "disk_unavailable",
    "child_unstopped",
    "command_start_failed",
    "command_failed",
}
_RESTORE_DIAGNOSTIC_STAGES = {
    "pre_create_admission",
    "create_database",
    "extension_setup",
    "schema_setup",
    "restore_platform",
    "restore_public",
    "validate_table_count",
    "validate_connectivity",
    "validate_revision",
    "validate_extensions",
    "drop_database",
}
_RESTORE_DROP_OUTCOMES = {
    "not_required",
    "drop_failed",
    "database_present",
    "absence_unconfirmed",
    "confirmed_absent",
}


def valid_backup_disk_diagnostic(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "phase",
        "guard_reason",
        "free_bytes",
        "required_free_bytes",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("phase")) is not str
        or value["phase"] not in _BACKUP_DISK_DIAGNOSTIC_PHASES
        or type(value.get("guard_reason")) is not str
        or value["guard_reason"] not in _BACKUP_DISK_DIAGNOSTIC_REASONS
        or any(
            item is not None and (type(item) is not int or item < 0)
            for item in (value.get("free_bytes"), value.get("required_free_bytes"))
        )
    ):
        return False
    if value["guard_reason"] == "disk_floor":
        return (
            type(value["free_bytes"]) is int
            and type(value["required_free_bytes"]) is int
            and value["free_bytes"] < value["required_free_bytes"]
        )
    return True


def valid_scratch_admission_diagnostic(value: Any) -> bool:
    """Validate the closed numeric summary for a pre-unit scratch admission refusal."""
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "phase",
        "guard_reason",
        "available_bytes",
        "required_bytes",
        "helper_rss_bytes",
        "direct_parent_rss_bytes",
        "observed_pair_rss_bytes",
        "observed_process_count",
    }:
        return False
    available = value.get("available_bytes")
    required = value.get("required_bytes")
    helper_rss = value.get("helper_rss_bytes")
    parent_rss = value.get("direct_parent_rss_bytes")
    pair_rss = value.get("observed_pair_rss_bytes")
    count = value.get("observed_process_count")
    if not (
        type(value.get("schema")) is int
        and value["schema"] == 1
        and type(value.get("phase")) is str
        and value["phase"] == "pre_create_admission"
        and type(value.get("guard_reason")) is str
        and value["guard_reason"] == "memory_reserve"
        and type(available) is int
        and 0 <= available < SCRATCH_MEMORY_ADMISSION_BYTES
        and type(required) is int
        and required == SCRATCH_MEMORY_ADMISSION_BYTES
        and all(
            item is None or (type(item) is int and 0 <= item <= SCRATCH_ADMISSION_RSS_MAX_BYTES)
            for item in (helper_rss, parent_rss)
        )
        and type(count) is int
        and count == sum(item is not None for item in (helper_rss, parent_rss))
        and count in {0, 1, 2}
    ):
        return False
    if count == 2:
        return (
            type(pair_rss) is int
            and pair_rss == helper_rss + parent_rss
            and pair_rss <= 2 * SCRATCH_ADMISSION_RSS_MAX_BYTES
        )
    return pair_rss is None


_SCRATCH_HOST_GUARD_SITES = frozenset(
    {
        "parent_before_archive_open",
        "parent_before_authorization_probe",
        "parent_before_unit_launch",
        "parent_active_poll",
        "parent_after_worker_cleanup",
        "worker_after_start_before_archive_open",
        "worker_after_start_before_authorization_probe",
        "worker_after_initdb_before_postgres_start",
        "worker_after_postgres_start",
        "worker_pg_restore_resource_guard",
        "worker_after_restore_before_postgres_stop",
    }
)
SCRATCH_HOST_GUARD_FRAME_MARKER = b"OLDS_PG_SCRATCH_HOST_GUARD_V1:"
SCRATCH_HOST_GUARD_FRAME_MAX_BYTES = 512


def valid_scratch_host_guard_diagnostic(value: Any) -> bool:
    """Validate the closed private summary for an observed host-memory guard."""
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "guard_site",
        "guard_reason",
        "available_bytes",
        "required_bytes",
    }:
        return False
    available = value.get("available_bytes")
    return (
        type(value.get("schema")) is int
        and value["schema"] == 2
        and type(value.get("guard_site")) is str
        and value["guard_site"] in _SCRATCH_HOST_GUARD_SITES
        and type(value.get("guard_reason")) is str
        and value["guard_reason"] == "memory_reserve"
        and type(available) is int
        and 0 <= available < SCRATCH_MEMORY_ADMISSION_BYTES
        and type(value.get("required_bytes")) is int
        and value["required_bytes"] == SCRATCH_MEMORY_ADMISSION_BYTES
    )


_SCRATCH_RESOURCE_UNKNOWN_REASONS = frozenset(
    {"sample_unavailable", "identity_drift", "parse_invalid", "cap_exceeded", "internal_error"}
)
_SCRATCH_PHASE_UNKNOWN_REASONS = frozenset(
    {
        "capture_unavailable",
        "capture_prefix",
        "incomplete",
        "malformed",
        "non_phase_bytes",
        "truncated",
        "internal_error",
    }
)


def valid_scratch_phase_attribution(value: Any) -> bool:
    """Validate closed, private phase-boundary observations."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "status", "unknown_reason", "controller_guard_offset_ms",
        "controller_drain_offset_ms", "frames",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("status")) is not str
        or value["status"] not in {"complete", "unknown"}
        or (
            value.get("unknown_reason") is not None
            and (
                type(value["unknown_reason"]) is not str
                or value["unknown_reason"] not in _SCRATCH_PHASE_UNKNOWN_REASONS
            )
        )
    ):
        return False
    for key in ("controller_guard_offset_ms", "controller_drain_offset_ms"):
        offset = value.get(key)
        if offset is not None and (
            type(offset) is not int or not 0 <= offset <= SCRATCH_PHASE_OFFSET_MAX_MS
        ):
            return False
    if (value["controller_guard_offset_ms"] is None) != (
        value["controller_drain_offset_ms"] is None
    ):
        return False
    if (
        value["controller_guard_offset_ms"] is not None
        and value["controller_drain_offset_ms"] is not None
        and value["controller_drain_offset_ms"] < value["controller_guard_offset_ms"]
    ):
        return False
    frames = value.get("frames")
    if not isinstance(frames, list) or len(frames) > SCRATCH_PHASE_FRAME_MAX_COUNT:
        return False
    expected = [
        (phase, event)
        for phase in SCRATCH_PHASES
        for event in ("start", "end")
    ]
    previous_offset = -1
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict) or set(frame) != {
            "ordinal", "phase", "event", "worker_offset_ms",
        }:
            return False
        if (
            type(frame.get("ordinal")) is not int
            or frame["ordinal"] != index + 1
            or (frame.get("phase"), frame.get("event")) != expected[index]
            or type(frame.get("worker_offset_ms")) is not int
            or not 0 <= frame["worker_offset_ms"] <= SCRATCH_PHASE_OFFSET_MAX_MS
            or frame["worker_offset_ms"] < previous_offset
        ):
            return False
        previous_offset = frame["worker_offset_ms"]
    if value["status"] == "complete":
        return (
            len(frames) == SCRATCH_PHASE_FRAME_MAX_COUNT
            and value["unknown_reason"] is None
        )
    return value["unknown_reason"] in _SCRATCH_PHASE_UNKNOWN_REASONS


def _scratch_unknown_phase_attribution(
    reason: str,
    *,
    frames: list[dict[str, Any]] | None = None,
    controller_guard_offset_ms: int | None = None,
    controller_drain_offset_ms: int | None = None,
) -> dict[str, Any]:
    if reason not in _SCRATCH_PHASE_UNKNOWN_REASONS:
        reason = "internal_error"
    candidate = {
        "schema": 1,
        "status": "unknown",
        "unknown_reason": reason,
        "controller_guard_offset_ms": controller_guard_offset_ms,
        "controller_drain_offset_ms": controller_drain_offset_ms,
        "frames": frames if frames is not None else [],
    }
    return candidate if valid_scratch_phase_attribution(candidate) else {
        "schema": 1,
        "status": "unknown",
        "unknown_reason": "internal_error",
        "controller_guard_offset_ms": None,
        "controller_drain_offset_ms": None,
        "frames": [],
    }


def _scratch_phase_offset_ms(monotonic_ns: int | None, launch_monotonic_ns: int | None) -> int | None:
    if (
        type(monotonic_ns) is not int
        or type(launch_monotonic_ns) is not int
        or monotonic_ns < launch_monotonic_ns
    ):
        return None
    offset = (monotonic_ns - launch_monotonic_ns) // 1_000_000
    return offset if offset <= SCRATCH_PHASE_OFFSET_MAX_MS else None


def _scratch_emit_phase_boundary(args: argparse.Namespace, phase: str, event: str) -> None:
    """Emit one closed phase event to the existing private stderr pipe."""
    if (
        not getattr(args, "private_scratch_diagnostics", False)
        or getattr(args, "_scratch_authorization_probe", False)
    ):
        return
    ordinal = getattr(args, "_scratch_phase_ordinal", 0) + 1
    if (
        phase not in SCRATCH_PHASES
        or event not in {"start", "end"}
        or type(ordinal) is not int
        or ordinal > SCRATCH_PHASE_FRAME_MAX_COUNT
    ):
        setattr(args, "_scratch_phase_emit_failed", True)
        return
    frame = {
        "ordinal": ordinal,
        "phase": phase,
        "event": event,
        "worker_offset_ms": _scratch_phase_offset_ms(
            time.monotonic_ns(),
            getattr(args, "_scratch_phase_origin_monotonic_ns", None),
        ),
    }
    try:
        if frame["worker_offset_ms"] is None:
            setattr(args, "_scratch_phase_emit_failed", True)
            return
        encoded = (
            SCRATCH_PHASE_FRAME_MARKER
            + json.dumps(frame, separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
        )
        if len(encoded) > SCRATCH_PHASE_FRAME_MAX_BYTES:
            setattr(args, "_scratch_phase_emit_failed", True)
            return
        stream = getattr(sys.stderr, "buffer", None)
        if stream is None:
            setattr(args, "_scratch_phase_emit_failed", True)
            return
        stream.write(encoded)
        stream.flush()
        setattr(args, "_scratch_phase_ordinal", ordinal)
    except BaseException:
        # Optional phase evidence must never replace the restore/guard result.
        setattr(args, "_scratch_phase_emit_failed", True)


def _scratch_run_observed_phase(
    phase_observer: Callable[[str, str], None] | None,
    phase: str,
    operation: Callable[[], Any],
) -> Any:
    if phase_observer is not None:
        phase_observer(phase, "start")
    result = operation()
    if phase_observer is not None:
        phase_observer(phase, "end")
    return result


def _scratch_reject_duplicate_phase_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate phase frame key")
        value[key] = item
    return value


def _scratch_parse_phase_attribution(
    stderr: bytes,
    *,
    truncated: bool,
    complete: bool,
    launch_monotonic_ns: int | None,
    controller_guard_monotonic_ns: int | None = None,
    controller_drain_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    guard_offset = _scratch_phase_offset_ms(
        controller_guard_monotonic_ns, launch_monotonic_ns
    )
    drain_offset = _scratch_phase_offset_ms(
        controller_drain_monotonic_ns, launch_monotonic_ns
    )

    def unknown(reason: str, frames: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return _scratch_unknown_phase_attribution(
            reason,
            frames=frames,
            controller_guard_offset_ms=guard_offset,
            controller_drain_offset_ms=drain_offset,
        )

    if type(stderr) is not bytes or type(truncated) is not bool or type(complete) is not bool:
        return unknown("malformed")
    if truncated:
        return unknown("truncated")
    if not stderr:
        return unknown("capture_unavailable")
    if launch_monotonic_ns is None or type(launch_monotonic_ns) is not int:
        return unknown("capture_unavailable")
    lines = stderr.splitlines(keepends=True)
    if not lines or len(lines) > SCRATCH_PHASE_FRAME_MAX_COUNT:
        return unknown("malformed")
    expected = [
        (phase, event)
        for phase in SCRATCH_PHASES
        for event in ("start", "end")
    ]
    frames: list[dict[str, Any]] = []
    previous_offset = -1
    for index, line in enumerate(lines):
        if not line.endswith(b"\n") or not line.startswith(SCRATCH_PHASE_FRAME_MARKER):
            return unknown("non_phase_bytes", frames)
        payload = line[len(SCRATCH_PHASE_FRAME_MARKER):-1]
        if not payload or len(payload) > SCRATCH_PHASE_FRAME_MAX_BYTES:
            return unknown("malformed")
        try:
            decoded = json.loads(
                payload.decode("ascii"),
                object_pairs_hook=_scratch_reject_duplicate_phase_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return unknown("malformed", frames)
        except ValueError:
            return unknown("malformed", frames)
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"ordinal", "phase", "event", "worker_offset_ms"}
            or type(decoded.get("ordinal")) is not int
            or decoded["ordinal"] != index + 1
            or (decoded.get("phase"), decoded.get("event")) != expected[index]
            or type(decoded.get("worker_offset_ms")) is not int
            or not 0 <= decoded["worker_offset_ms"] <= SCRATCH_PHASE_OFFSET_MAX_MS
            or decoded["worker_offset_ms"] < previous_offset
        ):
            return unknown("malformed", frames)
        if json.dumps(decoded, separators=(",", ":"), sort_keys=True).encode("ascii") != payload:
            return unknown("malformed", frames)
        previous_offset = decoded["worker_offset_ms"]
        frames.append(
            {
                "ordinal": decoded["ordinal"],
                "phase": decoded["phase"],
                "event": decoded["event"],
                "worker_offset_ms": decoded["worker_offset_ms"],
            }
        )
    if complete and len(frames) == SCRATCH_PHASE_FRAME_MAX_COUNT:
        candidate = {
            "schema": 1,
            "status": "complete",
            "unknown_reason": None,
            "controller_guard_offset_ms": guard_offset,
            "controller_drain_offset_ms": drain_offset,
            "frames": frames,
        }
        return candidate if valid_scratch_phase_attribution(candidate) else unknown("malformed")
    return unknown("capture_prefix" if not complete else "incomplete", frames)


def _scratch_success_stderr_accepted(
    stderr_prefix: bytes,
    *,
    observed_bytes: int,
    stderr_eof: bool,
    private_diagnostics: bool,
    authorization_probe: bool,
    launch_monotonic_ns: int | None,
) -> bool:
    """Keep legacy empty-stderr success except for the opt-in restore phase stream."""
    if type(observed_bytes) is not int or observed_bytes < 0 or type(stderr_eof) is not bool:
        return False
    if observed_bytes == 0 and not stderr_prefix and stderr_eof:
        return True
    if authorization_probe or not private_diagnostics:
        return False
    phase = _scratch_parse_phase_attribution(
        stderr_prefix,
        truncated=observed_bytes > len(stderr_prefix),
        complete=stderr_eof,
        launch_monotonic_ns=launch_monotonic_ns,
    )
    return (
        observed_bytes > 0
        and valid_scratch_phase_attribution(phase)
        and phase.get("status") == "complete"
    )


def valid_scratch_resource_attribution(value: Any) -> bool:
    """Validate optional private role/cgroup observations without admitting work."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "status", "sample_offset_ms", "unknown_reason", "controller",
        "worker", "cgroup",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("status")) is not str
        or value["status"] not in {"complete", "unknown"}
        or (
            value.get("sample_offset_ms") is not None
            and (type(value["sample_offset_ms"]) is not int or value["sample_offset_ms"] < 0)
        )
        or (
            value.get("unknown_reason") is not None
            and (
                type(value["unknown_reason"]) is not str
                or value["unknown_reason"] not in _SCRATCH_RESOURCE_UNKNOWN_REASONS
            )
        )
    ):
        return False
    for role in ("controller", "worker"):
        observation = value.get(role)
        if (
            not isinstance(observation, dict)
            or set(observation) != {"identity_bound", "rss_bytes"}
            or type(observation.get("identity_bound")) is not bool
            or (
                observation.get("rss_bytes") is not None
                and (
                    type(observation["rss_bytes"]) is not int
                    or not 0 <= observation["rss_bytes"] <= SCRATCH_ADMISSION_RSS_MAX_BYTES
                )
            )
        ):
            return False
        if observation["rss_bytes"] is not None and not observation["identity_bound"]:
            return False
    cgroup = value.get("cgroup")
    if (
        not isinstance(cgroup, dict)
        or set(cgroup) != {
            "identity_bound", "memory_current_bytes", "anon_bytes", "file_bytes",
            "shmem_bytes", "events",
        }
        or type(cgroup.get("identity_bound")) is not bool
    ):
        return False
    for key in ("memory_current_bytes", "anon_bytes", "file_bytes", "shmem_bytes"):
        item = cgroup.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= (1 << 60)):
            return False
        if item is not None and not cgroup["identity_bound"]:
            return False
    events = cgroup.get("events")
    if not isinstance(events, dict) or set(events) != {"high", "max", "oom", "oom_kill"}:
        return False
    for item in events.values():
        if item is not None and (type(item) is not int or not 0 <= item <= (1 << 60)):
            return False
        if item is not None and not cgroup["identity_bound"]:
            return False
    complete = (
        value["status"] == "complete"
        and value["sample_offset_ms"] is not None
        and value["unknown_reason"] is None
        and value["controller"]["identity_bound"]
        and value["controller"]["rss_bytes"] is not None
        and value["worker"]["identity_bound"]
        and value["worker"]["rss_bytes"] is not None
        and cgroup["identity_bound"]
        and all(cgroup[key] is not None for key in ("memory_current_bytes", "anon_bytes", "file_bytes", "shmem_bytes"))
        and all(item is not None for item in events.values())
    )
    if value["status"] == "complete":
        return complete
    return value["unknown_reason"] in _SCRATCH_RESOURCE_UNKNOWN_REASONS


def parse_scratch_host_guard_frame(
    stderr: bytes, *, truncated: bool
) -> dict[str, Any] | None:
    """Read exactly one bounded worker frame; malformed or incomplete is unknown."""
    if type(stderr) is not bytes or type(truncated) is not bool or truncated:
        return None
    marker = SCRATCH_HOST_GUARD_FRAME_MARKER
    if stderr.count(marker) != 1:
        return None
    marker_offset = stderr.find(marker)
    line_end = stderr.find(b"\n", marker_offset)
    if line_end < 0:
        return None
    frame = stderr[marker_offset + len(marker) : line_end]
    if not frame or len(frame) > SCRATCH_HOST_GUARD_FRAME_MAX_BYTES:
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate frame field")
            result[key] = value
        return result

    try:
        candidate = json.loads(frame.decode("ascii"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return candidate if valid_scratch_host_guard_diagnostic(candidate) else None


def parse_scratch_host_guard_failure_report(
    stdout: bytes, *, truncated: bool
) -> dict[str, Any] | None:
    """Read only the helper's complete, closed stdout failure report."""
    if (
        type(stdout) is not bytes
        or type(truncated) is not bool
        or truncated
        or not stdout.endswith(b"\n")
        or len(stdout) > SCRATCH_PRIVATE_STDOUT_BYTES
    ):
        return None

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report field")
            result[key] = value
        return result

    try:
        report = json.loads(
            stdout.decode("utf-8"), object_pairs_hook=unique_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    allowed_keys = {
        "ok",
        "error",
        "restore_diagnostic",
        "backup_disk_diagnostic",
        "scratch_unit_diagnostic",
        "scratch_admission_diagnostic",
        "scratch_host_guard_diagnostic",
    }
    if (
        not isinstance(report, dict)
        or not set(report).issubset(allowed_keys)
        or type(report.get("ok")) is not bool
        or report["ok"] is not False
        or type(report.get("error")) is not str
        or not valid_scratch_host_guard_diagnostic(
            report.get("scratch_host_guard_diagnostic")
        )
    ):
        return None
    # Match the sole report writer in main(); this also rejects prefix/suffix
    # data, multiple objects, and otherwise valid but noncanonical framing.
    canonical = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if stdout != canonical:
        return None
    return report["scratch_host_guard_diagnostic"]


def _write_scratch_worker_private_output(stdout: bytes, stderr: bytes) -> None:
    """Keep child output off the helper-owned JSON stdout channel."""
    error_stream = getattr(sys.stderr, "buffer", None)
    if error_stream is None:
        return
    if stdout:
        error_stream.write(stdout)
    if stderr:
        error_stream.write(stderr)
    error_stream.flush()


def _parse_bound_scratch_host_guard_diagnostic(
    stdout: bytes,
    stderr: bytes,
    *,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> dict[str, Any] | None:
    """Promote only the same closed observation in both worker channels."""
    report = parse_scratch_host_guard_failure_report(
        stdout, truncated=stdout_truncated
    )
    frame = parse_scratch_host_guard_frame(stderr, truncated=stderr_truncated)
    return report if report is not None and report == frame else None


def _parse_bound_scratch_first_fault(
    stdout: bytes,
    stderr: bytes,
    *,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> dict[str, Any] | None:
    """Promote a worker first-fault only when its closed report and frame agree."""
    if stdout_truncated or stderr_truncated or not stdout or not stderr:
        return None
    marker_count = stderr.count(SCRATCH_FIRST_FAULT_MARKER)
    if marker_count != 1:
        return None
    marker_start = stderr.index(SCRATCH_FIRST_FAULT_MARKER) + len(SCRATCH_FIRST_FAULT_MARKER)
    marker_end = stderr.find(b"\n", marker_start)
    if marker_end < 0:
        return None
    frame_bytes = stderr[marker_start:marker_end]
    if not frame_bytes or len(frame_bytes) > 512:
        return None
    try:
        frame = json.loads(frame_bytes.decode("ascii"))
        report = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(report, dict)
        or report.get("ok") is not False
        or not isinstance(report.get("error"), str)
        or set(report) - {
            "ok", "error", "restore_diagnostic", "backup_disk_diagnostic",
            "scratch_unit_diagnostic", "scratch_admission_diagnostic",
            "scratch_host_guard_diagnostic", "scratch_first_fault",
            "scratch_phase_attribution",
        }
    ):
        return None
    value = report.get("scratch_first_fault")
    if not valid_scratch_first_fault(value) or frame != value:
        return None
    canonical_report = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if stdout != canonical_report:
        return None
    return value


def _scratch_host_guard_frame(value: Any) -> bytes | None:
    if not valid_scratch_host_guard_diagnostic(value):
        return None
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(payload) > SCRATCH_HOST_GUARD_FRAME_MAX_BYTES:
        return None
    return SCRATCH_HOST_GUARD_FRAME_MARKER + payload + b"\n"


_SCRATCH_UNIT_SUBSTATES = {
    "dead", "failed", "exited", "start", "start-pre", "start-post",
    "running", "stop", "stop-sigterm", "stop-sigkill", "final-sigterm",
    "final-sigkill", "auto-restart", "condition", "assert", "other",
}
_SCRATCH_UNIT_RESULTS = {
    "success", "resources", "timeout", "exit-code", "signal", "core-dump",
    "watchdog", "start-limit-hit", "protocol", "exec-condition", "condition",
    "assert", "oom-kill", "other",
}


def valid_scratch_unit_diagnostic(value: Any) -> bool:
    """Validate the closed, non-sensitive summary of an early scratch-unit exit."""
    base_keys = {
        "schema", "phase", "active_state", "sub_state", "result",
        "exec_main_status", "launcher_exit_status", "stdout_bytes",
        "stdout_sha256", "stdout_truncated", "stderr_bytes", "stderr_sha256",
        "stderr_truncated", "verification_error", "verification_errno",
    }
    if not isinstance(value, dict) or type(value.get("schema")) is not int:
        return False
    schema = value["schema"]
    expected_keys = base_keys if schema == 1 else base_keys | {"terminal_snapshot"}
    phases = {
        "unit_exited_before_confinement",
        "confinement_verification_failed",
        "unit_exited_after_confinement",
    }
    if schema == 2:
        phases.add("authorization_failed")
    if schema not in {1, 2} or set(value) != expected_keys:
        return False
    if (
        type(value.get("schema")) is not int
        or type(value.get("phase")) is not str
        or value.get("phase") not in phases
        or type(value.get("active_state")) is not str
        or value.get("active_state") not in {"failed", "inactive", "active"}
        or type(value.get("sub_state")) is not str
        or value.get("sub_state") not in _SCRATCH_UNIT_SUBSTATES
        or type(value.get("result")) is not str
        or value.get("result") not in _SCRATCH_UNIT_RESULTS
        or (
            value.get("exec_main_status") is not None
            and (type(value["exec_main_status"]) is not int or not 0 <= value["exec_main_status"] <= 255)
        )
        or (
            value.get("launcher_exit_status") is not None
            and (type(value["launcher_exit_status"]) is not int or not -255 <= value["launcher_exit_status"] <= 255)
        )
        or type(value.get("stdout_bytes")) is not int
        or not 0 <= value["stdout_bytes"] <= SCRATCH_PRIVATE_STDOUT_BYTES
        or type(value.get("stderr_bytes")) is not int
        or not 0 <= value["stderr_bytes"] <= SCRATCH_PRIVATE_STDERR_BYTES
        or type(value.get("stdout_truncated")) is not bool
        or type(value.get("stderr_truncated")) is not bool
        or type(value.get("stdout_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["stdout_sha256"]) is None
        or type(value.get("stderr_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["stderr_sha256"]) is None
        or type(value.get("verification_error")) is not str
        or value.get("verification_error") not in {"none", "os_error", "policy_error", "other"}
        or (
            value.get("verification_errno") is not None
            and (
                type(value["verification_errno"]) is not int
                or not 1 <= value["verification_errno"] <= 4095
            )
        )
    ):
        return False
    if value["phase"] in {"confinement_verification_failed", "authorization_failed"}:
        if value["active_state"] != "active" or value["verification_error"] == "none":
            return False
    elif value["phase"] == "unit_exited_after_confinement":
        if (
            value["active_state"] not in {"failed", "inactive"}
            or value["verification_error"] != "none"
            or value["verification_errno"] is not None
        ):
            return False
    elif (
        value["active_state"] not in {"failed", "inactive"}
        or value["verification_error"] != "none"
        or value["verification_errno"] is not None
    ):
        return False
    if value["verification_error"] != "os_error" and value["verification_errno"] is not None:
        return False
    if schema == 2 and not valid_scratch_terminal_snapshot(value.get("terminal_snapshot")):
        return False
    return True


_SCRATCH_UNIT_LOAD_STATES = {"loaded", "not-found", "other"}
_SCRATCH_UNIT_ACTIVE_STATES = {"active", "activating", "deactivating", "inactive", "failed", "reloading", "other"}


def valid_scratch_terminal_snapshot(value: Any) -> bool:
    """Validate the bounded lifecycle-only snapshot captured before unit stop."""
    keys = {
        "load_state", "active_state", "sub_state", "result", "exec_main_status",
        "start_token_sent", "completion_token_sent",
    }
    return (
        isinstance(value, dict)
        and set(value) == keys
        and type(value.get("load_state")) is str
        and value["load_state"] in _SCRATCH_UNIT_LOAD_STATES
        and type(value.get("active_state")) is str
        and value["active_state"] in _SCRATCH_UNIT_ACTIVE_STATES
        and type(value.get("sub_state")) is str
        and value["sub_state"] in _SCRATCH_UNIT_SUBSTATES
        and type(value.get("result")) is str
        and value["result"] in _SCRATCH_UNIT_RESULTS
        and (
            value.get("exec_main_status") is None
            or (type(value["exec_main_status"]) is int and 0 <= value["exec_main_status"] <= 255)
        )
        and type(value.get("start_token_sent")) is bool
        and type(value.get("completion_token_sent")) is bool
    )


def _scratch_terminal_snapshot(
    snapshot: dict[str, Any], *, start_token_sent: bool, completion_token_sent: bool
) -> dict[str, Any] | None:
    properties = snapshot.get("properties")
    if not isinstance(properties, dict):
        return None
    exec_status_raw = properties.get("ExecMainStatus")
    exec_status = (
        int(exec_status_raw)
        if isinstance(exec_status_raw, str) and re.fullmatch(r"[0-9]{1,3}", exec_status_raw)
        else None
    )
    value = {
        "load_state": properties.get("LoadState") if properties.get("LoadState") in _SCRATCH_UNIT_LOAD_STATES else "other",
        "active_state": properties.get("ActiveState") if properties.get("ActiveState") in _SCRATCH_UNIT_ACTIVE_STATES else "other",
        "sub_state": properties.get("SubState") if properties.get("SubState") in _SCRATCH_UNIT_SUBSTATES else "other",
        "result": properties.get("Result") if properties.get("Result") in _SCRATCH_UNIT_RESULTS else "other",
        "exec_main_status": exec_status,
        "start_token_sent": start_token_sent,
        "completion_token_sent": completion_token_sent,
    }
    return value if valid_scratch_terminal_snapshot(value) else None


def _scratch_private_output_envelope(stdout: bytes, stderr: bytes) -> bytes:
    """Frame bounded raw launcher output for the existing private stderr capture."""
    if (
        len(stdout) > SCRATCH_PRIVATE_STDOUT_BYTES
        or len(stderr) > SCRATCH_PRIVATE_STDERR_BYTES
        or len(stdout) + len(stderr) > SCRATCH_PRIVATE_RAW_BYTES
    ):
        raise ValueError("scratch private output exceeded its closed envelope bound")
    payload = struct.pack(">H", len(stdout)) + stdout + stderr
    envelope = SCRATCH_PRIVATE_OUTPUT_MARKER + base64.b64encode(payload) + b"\n"
    if len(envelope) > SCRATCH_PRIVATE_ENVELOPE_BYTES:
        raise ValueError("scratch private output envelope exceeded its closed bound")
    return envelope


def _scratch_unit_diagnostic(
    snapshot: dict[str, Any],
    *,
    launcher_exit_status: int | None,
    stdout_prefix: bytes,
    stdout_observed: int,
    stdout_complete: bool,
    stderr_prefix: bytes,
    stderr_observed: int,
    stderr_complete: bool,
    phase: str = "unit_exited_before_confinement",
    verification_error: str = "none",
    verification_errno: int | None = None,
    terminal_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    properties = snapshot.get("properties", {})
    if not isinstance(properties, dict):
        return None
    exec_status_raw = properties.get("ExecMainStatus")
    exec_status = (
        int(exec_status_raw)
        if isinstance(exec_status_raw, str) and re.fullmatch(r"[0-9]{1,3}", exec_status_raw)
        else None
    )
    stdout_prefix = stdout_prefix[:SCRATCH_PRIVATE_STDOUT_BYTES]
    stderr_prefix = stderr_prefix[:SCRATCH_PRIVATE_STDERR_BYTES]
    diagnostic = {
        "schema": 2 if valid_scratch_terminal_snapshot(terminal_snapshot) else 1,
        "phase": phase,
        "active_state": properties.get("ActiveState"),
        "sub_state": properties.get("SubState"),
        "result": properties.get("Result"),
        "exec_main_status": exec_status,
        "launcher_exit_status": launcher_exit_status,
        "stdout_bytes": len(stdout_prefix),
        "stdout_sha256": hashlib.sha256(stdout_prefix).hexdigest(),
        "stdout_truncated": stdout_observed > len(stdout_prefix) or not stdout_complete,
        "stderr_bytes": len(stderr_prefix),
        "stderr_sha256": hashlib.sha256(stderr_prefix).hexdigest(),
        "stderr_truncated": stderr_observed > len(stderr_prefix) or not stderr_complete,
        "verification_error": verification_error,
        "verification_errno": verification_errno,
    }
    if diagnostic["schema"] == 2:
        diagnostic["terminal_snapshot"] = terminal_snapshot
    return diagnostic if valid_scratch_unit_diagnostic(diagnostic) else None


def _scratch_verification_error_details(error: BaseException) -> tuple[str, int | None]:
    """Return closed diagnostic fields without serializing exception text."""
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, OSError):
            code = current.errno
            safe_code = code if type(code) is int and 1 <= code <= 4095 else None
            return "os_error", safe_code
        current = current.__cause__ or current.__context__
    if isinstance(error, RuntimeError):
        return "policy_error", None
    return "other", None


def _valid_restore_diagnostic(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "restore_stage",
        "guard_reason",
        "free_bytes",
        "required_free_bytes",
        "temporary_database_created",
        "drop_outcome",
        "temporary_database_absent",
        "archive_sha256",
        "archive_size_bytes",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("restore_stage")) is not str
        or value["restore_stage"] not in _RESTORE_DIAGNOSTIC_STAGES
        or type(value.get("guard_reason")) is not str
        or value["guard_reason"] not in {"none", *_RESTORE_GUARD_MESSAGES.keys()}
        or type(value.get("drop_outcome")) is not str
        or value["drop_outcome"] not in _RESTORE_DROP_OUTCOMES
        or (
            value.get("temporary_database_created") is not None
            and type(value.get("temporary_database_created")) is not bool
        )
        or (
            value.get("temporary_database_absent") is not None
            and type(value.get("temporary_database_absent")) is not bool
        )
    ):
        return False
    if not all(
        value.get(key) is None or (type(value[key]) is int and value[key] >= 0)
        for key in ("free_bytes", "required_free_bytes")
    ):
        return False
    if (
        type(value.get("archive_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"]) is None
        or type(value.get("archive_size_bytes")) is not int
        or value["archive_size_bytes"] < 0
    ):
        return False
    if value["guard_reason"] == "disk_floor":
        if (
            type(value.get("free_bytes")) is not int
            or type(value.get("required_free_bytes")) is not int
            or value["free_bytes"] >= value["required_free_bytes"]
        ):
            return False
    if value["temporary_database_created"] is False or value["temporary_database_created"] is None:
        return (
            (value["temporary_database_created"] is False or value["restore_stage"] == "create_database")
            and
            value["drop_outcome"] == "not_required"
            and value["temporary_database_absent"] is None
        )
    if value["drop_outcome"] == "not_required":
        return False
    if value["drop_outcome"] == "confirmed_absent":
        return value["temporary_database_absent"] is True
    if value["drop_outcome"] == "database_present":
        return value["temporary_database_absent"] is False
    if value["drop_outcome"] in {"drop_failed", "absence_unconfirmed"}:
        return value["temporary_database_absent"] is None
    return False


@dataclasses.dataclass(frozen=True)
class DatabaseTarget:
    host: str | None
    port: int
    username: str
    password: str | None
    database: str

    def with_database(self, database: str) -> "DatabaseTarget":
        return dataclasses.replace(self, database=database)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create an atomic custom-format backup of platformdb's platform schema "
            "and verify it in a bounded isolated PostgreSQL cluster."
        )
    )
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--keep", type=int, default=14)
    rotation = parser.add_mutually_exclusive_group()
    rotation.add_argument(
        "--preserve-existing",
        dest="rotate_existing",
        action="store_false",
        help="Keep every pre-existing archive and sidecar; do not rotate backups.",
    )
    rotation.add_argument(
        "--rotate-existing",
        dest="rotate_existing",
        action="store_true",
        help="Apply the requested retention policy to older verified backups.",
    )
    # Keep the low-level CLI's historical rotation behavior. Backup-only
    # maintenance opts into preservation explicitly through its own caller.
    parser.set_defaults(rotate_existing=True)
    parser.add_argument(
        "--admin-database-url",
        default=None,
        help="Accepted for CLI compatibility; isolated restore verification does not use this URL.",
    )
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="Create and validate the archive without performing the restore drill.",
    )
    parser.add_argument(
        "--check-latest",
        action="store_true",
        help="Only verify the newest retained backup metadata and checksum.",
    )
    parser.add_argument(
        "--verify-latest-existing",
        action="store_true",
        help=(
            "Restore and verify only the newest retained archive, then mark its existing "
            "metadata verified without creating or rotating a backup."
        ),
    )
    parser.add_argument(
        "--verify-dump",
        default=None,
        help="Restore an existing custom-format platform backup in an isolated cluster.",
    )
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--private-scratch-diagnostics",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(SCRATCH_WORKER_MODE, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-dev", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-ino", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-size", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-sha256", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-timestamp-slug", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-authorization-probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.private_scratch_diagnostics and not args.as_json:
        parser.error("--private-scratch-diagnostics requires --json")
    return args


def load_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def parse_database_url(database_url: str, *, require_platformdb: bool = True) -> DatabaseTarget:
    normalized = database_url
    for scheme in ("postgresql+asyncpg://", "postgresql+psycopg://"):
        if normalized.startswith(scheme):
            normalized = "postgresql://" + normalized[len(scheme) :]
            break
    parsed = urllib.parse.urlsplit(normalized)
    database = urllib.parse.unquote(parsed.path.lstrip("/"))
    username = urllib.parse.unquote(parsed.username or "")
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("PLATFORM_DATABASE_URL must use a PostgreSQL scheme.")
    if not username or not database:
        raise ValueError("PLATFORM_DATABASE_URL must include a username and database name.")
    if require_platformdb and database != "platformdb":
        raise ValueError(
            f"Refusing to back up database {database!r}; expected the isolated platformdb database."
        )
    return DatabaseTarget(
        host=parsed.hostname,
        port=parsed.port or 5432,
        username=username,
        password=urllib.parse.unquote(parsed.password) if parsed.password else None,
        database=database,
    )


def connection_args(target: DatabaseTarget, *, include_database: bool = True) -> list[str]:
    args: list[str] = []
    if target.host:
        args.extend(["--host", target.host])
    args.extend(["--port", str(target.port), "--username", target.username])
    if include_database:
        args.extend(["--dbname", target.database])
    return args


def command_env(target: DatabaseTarget) -> dict[str, str]:
    env = dict(os.environ)
    if target.password:
        env["PGPASSWORD"] = target.password
    else:
        env.pop("PGPASSWORD", None)
    return env


def run_command(
    command: list[str],
    *,
    target: DatabaseTarget | None = None,
    capture_output: bool = False,
    stdin_handle: BinaryIO | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=capture_output,
        stdin=stdin_handle,
        env=command_env(target) if target is not None else None,
    )


def _required_disk_floor(total_bytes: int, *, phase: str | None = None) -> int:
    if type(total_bytes) is not int or total_bytes <= 0:
        raise RestoreGuardStop("disk_unavailable", phase=phase)
    fifteen_percent_ceil = (total_bytes * 15 + 99) // 100
    return max(RESTORE_DISK_MINIMUM_BYTES, fifteen_percent_ceil)


def require_restore_headroom(
    *paths: pathlib.Path, phase: str | None = None
) -> None:
    """Fail closed unless every affected filesystem has policy floor plus lead."""

    checked_paths = paths or (pathlib.Path("/"),)
    try:
        snapshots = [snapshot_for_path(path) for path in checked_paths]
    except OSError as exc:
        raise RestoreGuardStop("disk_unavailable", phase=phase) from exc
    for snapshot in snapshots:
        if not snapshot.valid:
            raise RestoreGuardStop("disk_unavailable", phase=phase)
        floor = _required_disk_floor(snapshot.total_bytes, phase=phase)
        required_free_bytes = floor + RESTORE_DISK_LEAD_BYTES
        if snapshot.free_bytes < required_free_bytes:
            raise RestoreGuardStop(
                "disk_floor",
                free_bytes=snapshot.free_bytes,
                required_free_bytes=required_free_bytes,
                phase=phase,
            )


def _stop_owned_child(
    process: subprocess.Popen[bytes], *, phase: str | None = None
) -> None:
    """Stop and reap only the child process started by this call."""

    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=RESTORE_TERMINATE_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        # Do not let finally/dropdb race a child that is still writing. SIGKILL
        # is followed by a wait on this exact Popen until it has been reaped.
        process.wait()
    except OSError as exc:
        raise RestoreGuardStop("child_unstopped", phase=phase) from exc


def run_disk_guarded_command(
    command: list[str],
    *,
    target: DatabaseTarget,
    stage: str,
    disk_paths: tuple[pathlib.Path, ...] = (pathlib.Path("/"),),
    stdin_handle: BinaryIO | None = None,
    resource_guard: Callable[[], None] | None = None,
    capture_private_output: bool = False,
) -> None:
    """Run one owned dump/restore child under bounded disk monitoring.

    Child output remains discarded by default. The isolated restore worker may
    opt into bounded diagnostic prefixes; both pipes are drained concurrently
    so a verbose failing command cannot block before its status is observed.
    """

    if stage not in {"pg_dump", "pg_restore"}:
        raise ValueError("unknown disk-guarded command stage")
    phase = f"{stage}_pre_spawn"
    backup_phase = phase if stage == "pg_dump" else None
    require_restore_headroom(*disk_paths, phase=backup_phase)
    if resource_guard is not None:
        resource_guard()
    stdout_prefix = bytearray()
    stderr_prefix = bytearray()
    stdout_observed = 0
    stderr_observed = 0
    output_selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            command,
            stdin=stdin_handle if stdin_handle is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_private_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_private_output else subprocess.DEVNULL,
            env=command_env(target),
            close_fds=True,
            bufsize=0,
        )
    except OSError as exc:
        raise RestoreGuardStop("command_start_failed", phase=backup_phase) from exc

    if capture_private_output:
        assert process.stdout is not None and process.stderr is not None
        output_selector = selectors.DefaultSelector()
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            output_selector.register(stream, selectors.EVENT_READ, name)

    def drain_child_output(*, timeout: float) -> None:
        nonlocal stdout_observed, stderr_observed
        if output_selector is None:
            return
        for key, _ in output_selector.select(timeout=timeout):
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                output_selector.unregister(key.fileobj)
                continue
            if key.data == "stdout":
                stdout_observed += len(chunk)
                if len(stdout_prefix) < SCRATCH_PRIVATE_STDOUT_BYTES:
                    stdout_prefix.extend(chunk[: SCRATCH_PRIVATE_STDOUT_BYTES - len(stdout_prefix)])
            else:
                stderr_observed += len(chunk)
                if len(stderr_prefix) < SCRATCH_PRIVATE_STDERR_BYTES:
                    stderr_prefix.extend(chunk[: SCRATCH_PRIVATE_STDERR_BYTES - len(stderr_prefix)])

    def retain_failure_output(error: BaseException) -> None:
        if capture_private_output:
            setattr(
                error,
                "scratch_worker_private_output",
                (
                    bytes(stdout_prefix),
                    bytes(stderr_prefix),
                    stdout_observed > len(stdout_prefix),
                    stderr_observed > len(stderr_prefix),
                ),
            )

    try:
        # Always take a post-start sample, including when pg_restore is short.
        phase = f"{stage}_post_start"
        backup_phase = phase if stage == "pg_dump" else None
        require_restore_headroom(*disk_paths, phase=backup_phase)
        if resource_guard is not None:
            resource_guard()
        while process.poll() is None:
            if output_selector is None:
                time.sleep(RESTORE_DISK_SAMPLE_SECONDS)
            else:
                drain_child_output(timeout=RESTORE_DISK_SAMPLE_SECONDS)
            if process.poll() is not None:
                break
            phase = f"{stage}_monitor"
            backup_phase = phase if stage == "pg_dump" else None
            require_restore_headroom(*disk_paths, phase=backup_phase)
            if resource_guard is not None:
                resource_guard()
        return_code = process.wait()
        if output_selector is not None:
            # A child may write its final bytes in the interval between poll
            # and exit. Drain through EOF after wait, with no unbounded storage.
            while output_selector.get_map():
                drain_child_output(timeout=0.05)
        # Catch a floor crossing between the final sample and child exit.
        phase = f"{stage}_post_exit"
        backup_phase = phase if stage == "pg_dump" else None
        require_restore_headroom(*disk_paths, phase=backup_phase)
        if resource_guard is not None:
            resource_guard()
    except BaseException as exc:
        if process.poll() is None:
            try:
                _stop_owned_child(process, phase=backup_phase)
            except BaseException as cleanup_error:
                # Keep the operation failure primary, but retain a closed
                # cleanup classification so callers can fail closed without
                # losing the bounded child output captured below.
                cleanup_reason = getattr(cleanup_error, "reason", None)
                if type(cleanup_reason) is not str or cleanup_reason not in {
                    "child_unstopped",
                }:
                    cleanup_reason = type(cleanup_error).__name__
                setattr(exc, "scratch_worker_cleanup_error", cleanup_reason)
                if hasattr(exc, "add_note"):
                    exc.add_note(f"Child cleanup also failed: {cleanup_reason}")
        if output_selector is not None:
            try:
                while output_selector.get_map():
                    drain_child_output(timeout=0.05)
            except BaseException:
                pass
        retain_failure_output(exc)
        raise
    finally:
        if output_selector is not None:
            output_selector.close()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    if return_code != 0:
        failure = RestoreGuardStop("command_failed", phase=backup_phase)
        retain_failure_output(failure)
        raise failure


def run_restore_command(
    command: list[str],
    *,
    target: DatabaseTarget,
    stdin_handle: BinaryIO | None = None,
    resource_guard: Callable[[], None] | None = None,
    capture_private_output: bool = False,
) -> None:
    run_disk_guarded_command(
        command,
        target=target,
        stage="pg_restore",
        stdin_handle=stdin_handle,
        resource_guard=resource_guard,
        capture_private_output=capture_private_output,
    )


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_utc_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def newest_metadata(output_dir: pathlib.Path) -> pathlib.Path:
    candidates = sorted(output_dir.glob("platformdb-*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise RuntimeError(f"No retained platform backup metadata found in {output_dir}.")
    return candidates[-1]


def check_latest_backup(output_dir: pathlib.Path, *, max_age_hours: float) -> dict[str, Any]:
    if max_age_hours <= 0:
        raise ValueError("--max-age-hours must be positive.")
    metadata_path = newest_metadata(output_dir)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not metadata.get("restore_verified"):
        raise RuntimeError(f"Latest platform backup was not restore-verified: {metadata_path}.")
    if int(metadata.get("format_version") or 1) >= 2 and not metadata.get(
        "alembic_revision_verified"
    ):
        raise RuntimeError(f"Latest platform backup did not verify Alembic state: {metadata_path}.")
    completed_at = parse_utc_timestamp(str(metadata["completed_at_utc"]))
    age_hours = (utc_now() - completed_at).total_seconds() / 3600
    if age_hours > max_age_hours:
        raise RuntimeError(
            f"Latest restore-verified platform backup is {age_hours:.2f} hours old; "
            f"maximum is {max_age_hours:.2f}."
        )
    dump_path = output_dir / str(metadata["dump_file"])
    if not dump_path.is_file():
        raise RuntimeError(f"Backup archive referenced by metadata is missing: {dump_path}.")
    actual_sha256 = sha256_file(dump_path)
    if actual_sha256 != metadata.get("sha256"):
        raise RuntimeError(f"Backup archive checksum does not match metadata: {dump_path}.")
    return {
        "ok": True,
        "metadata_file": str(metadata_path),
        "dump_file": str(dump_path),
        "age_hours": round(age_hours, 3),
        "format_version": int(metadata.get("format_version") or 1),
        "restore_verified": True,
        "alembic_revision_verified": bool(
            metadata.get("alembic_revision_verified")
        ),
        "restored_table_count": metadata.get("restored_table_count"),
        "sha256": actual_sha256,
    }


def _stable_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _backup_directory_identity(path: pathlib.Path) -> tuple[int, int, int, int, int, int]:
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise RuntimeError("Backup output directory has unsafe metadata.")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
    )


def _read_private_json(
    path: pathlib.Path,
    *,
    expected_gid: int | None = None,
    max_bytes: int = 1_048_576,
) -> tuple[bytes, dict[str, Any], tuple[int, ...]]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or (expected_gid is not None and before.st_gid != expected_gid)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise RuntimeError("Latest backup metadata has unsafe file metadata.")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        identity = _stable_file_identity(before)
        if len(raw) > max_bytes or _stable_file_identity(after) != identity:
            raise RuntimeError("Latest backup metadata changed while being read.")
    finally:
        os.close(fd)

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate metadata key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Latest backup metadata is malformed.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Latest backup metadata must be a JSON object.")
    return raw, value, identity


def _verify_backup_age(metadata: dict[str, Any], *, max_age_hours: float) -> tuple[dt.datetime, float]:
    try:
        timestamp = metadata["completed_at_utc"]
        if not isinstance(timestamp, str):
            raise ValueError("timestamp type")
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("naive timestamp")
        completed_at = parsed.astimezone(dt.UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Latest backup creation timestamp is invalid.") from exc
    now = utc_now()
    age_hours = (now - completed_at).total_seconds() / 3600
    if age_hours < 0 or not math.isfinite(age_hours) or age_hours > max_age_hours:
        raise RuntimeError("Latest backup is outside the permitted creation-age window.")
    return completed_at, age_hours


def _latest_pair_for_verification(
    output_dir: pathlib.Path,
    *,
    max_age_hours: float,
) -> tuple[
    pathlib.Path,
    pathlib.Path,
    bytes,
    dict[str, Any],
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, int, int, int, int, int],
    str,
    float,
]:
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        raise ValueError("--max-age-hours must be positive.")
    directory_identity = _backup_directory_identity(output_dir)
    metadata_path = newest_metadata(output_dir)
    metadata_raw, metadata, metadata_identity = _read_private_json(
        metadata_path, expected_gid=directory_identity[3]
    )
    dump_name = metadata.get("dump_file")
    if (
        not isinstance(dump_name, str)
        or pathlib.PurePath(dump_name).name != dump_name
        or not dump_name.startswith("platformdb-")
        or not dump_name.endswith(".dump")
        or metadata_path.name != pathlib.Path(dump_name).with_suffix(".json").name
    ):
        raise RuntimeError("Latest backup metadata has an invalid archive binding.")
    _, age_hours = _verify_backup_age(metadata, max_age_hours=max_age_hours)
    dump_path = output_dir / dump_name
    dump_stat = dump_path.lstat()
    if (
        not stat.S_ISREG(dump_stat.st_mode)
        or dump_stat.st_uid != os.geteuid()
        or dump_stat.st_gid != directory_identity[3]
        or stat.S_IMODE(dump_stat.st_mode) != 0o600
        or dump_stat.st_nlink != 1
        or dump_stat.st_size <= 0
    ):
        raise RuntimeError("Latest backup archive has unsafe file metadata.")
    dump_identity = _stable_file_identity(dump_stat)
    actual_sha256 = sha256_file(dump_path)
    version = metadata.get("format_version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
        raise RuntimeError("Latest backup metadata format is unsupported.")
    if metadata.get("sha256") != actual_sha256:
        raise RuntimeError("Latest backup archive checksum does not match metadata.")
    size_bytes = metadata.get("size_bytes")
    if size_bytes is not None and (
        isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes != dump_stat.st_size
    ):
        raise RuntimeError("Latest backup archive size does not match metadata.")
    if metadata.get("database", "platformdb") != "platformdb":
        raise RuntimeError("Latest backup metadata does not identify platformdb.")
    schemas = metadata.get("schemas", ["platform", "public"])
    if not isinstance(schemas, list) or schemas != ["platform", "public"]:
        raise RuntimeError("Latest backup metadata has an unsupported schema set.")
    extensions = metadata.get("required_extensions", list(REQUIRED_PLATFORM_EXTENSIONS))
    if not isinstance(extensions, list) or extensions != list(REQUIRED_PLATFORM_EXTENSIONS):
        raise RuntimeError("Latest backup metadata has an unsupported extension set.")
    for flag in ("restore_verified", "alembic_revision_verified"):
        if flag in metadata and not isinstance(metadata[flag], bool):
            raise RuntimeError("Latest backup metadata has an invalid verification flag.")
    if "restore_error" in metadata and metadata["restore_error"] is not None and not isinstance(
        metadata["restore_error"], str
    ):
        raise RuntimeError("Latest backup metadata has an invalid restore diagnostic.")
    if newest_metadata(output_dir) != metadata_path:
        raise RuntimeError("Latest backup selection changed during verification.")
    return (
        metadata_path,
        dump_path,
        metadata_raw,
        metadata,
        metadata_identity,
        dump_identity,
        directory_identity,
        actual_sha256,
        age_hours,
    )


def _publish_verified_metadata(
    output_dir: pathlib.Path,
    metadata_path: pathlib.Path,
    *,
    original_raw: bytes,
    original_identity: tuple[int, ...],
    directory_identity: tuple[int, int, int, int, int, int],
    updated: dict[str, Any],
    max_age_hours: float,
) -> None:
    if _backup_directory_identity(output_dir) != directory_identity:
        raise RuntimeError("Backup output directory changed before metadata staging.")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{metadata_path.name}.", suffix=".verify.tmp", dir=output_dir
    )
    temporary_path: pathlib.Path | None = pathlib.Path(temporary_name)
    temp_identity: tuple[int, int] | None = None
    published_identity: tuple[int, int] | None = None
    rollback_path: pathlib.Path | None = None
    rollback_identity: tuple[int, int] | None = None
    try:
        expected_bytes = (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        initial = os.fstat(fd)
        temp_identity = (initial.st_dev, initial.st_ino)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            written = handle.write(expected_bytes)
            if written != len(expected_bytes):
                raise RuntimeError("Verified backup metadata staging write was incomplete.")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600, follow_symlinks=False)
        if _backup_directory_identity(output_dir) != directory_identity:
            raise RuntimeError("Backup output directory changed during metadata staging.")
        staged = temporary_path.lstat()
        if (
            (staged.st_dev, staged.st_ino) != temp_identity
            or not stat.S_ISREG(staged.st_mode)
            or staged.st_uid != os.geteuid()
            or staged.st_gid != directory_identity[3]
            or stat.S_IMODE(staged.st_mode) != 0o600
            or staged.st_nlink != 1
            or staged.st_size != len(expected_bytes)
        ):
            raise RuntimeError("Verified backup metadata staging identity changed.")
        verify_fd = os.open(
            temporary_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            verify_before = os.fstat(verify_fd)
            staged_bytes = b""
            while len(staged_bytes) <= len(expected_bytes):
                chunk = os.read(verify_fd, min(65536, len(expected_bytes) + 1 - len(staged_bytes)))
                if not chunk:
                    break
                staged_bytes += chunk
            verify_after = os.fstat(verify_fd)
            if (
                _stable_file_identity(verify_before) != _stable_file_identity(verify_after)
                or staged_bytes != expected_bytes
            ):
                raise RuntimeError("Verified backup metadata staging bytes changed.")
        finally:
            os.close(verify_fd)
        current_raw, _, current_identity = _read_private_json(
            metadata_path, expected_gid=directory_identity[3]
        )
        if current_raw != original_raw or current_identity != original_identity:
            raise RuntimeError("Latest backup metadata changed before verification commit.")
        if _backup_directory_identity(output_dir) != directory_identity:
            raise RuntimeError("Backup output directory changed before verification commit.")
        if newest_metadata(output_dir) != metadata_path:
            raise RuntimeError("Latest backup selection changed before verification commit.")
        _verify_backup_age(updated, max_age_hours=max_age_hours)
        os.replace(temporary_path, metadata_path)
        temporary_path = None
        published_identity = temp_identity
        published_stat = metadata_path.lstat()
        published_identity = (published_stat.st_dev, published_stat.st_ino)
        if (
            not stat.S_ISREG(published_stat.st_mode)
            or published_stat.st_uid != os.geteuid()
            or published_stat.st_gid != directory_identity[3]
            or stat.S_IMODE(published_stat.st_mode) != 0o600
            or published_stat.st_nlink != 1
            or published_stat.st_size != len(expected_bytes)
        ):
            raise RuntimeError("Verified backup metadata publication metadata changed.")
        directory_fd = os.open(
            output_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if published_identity is not None:
            try:
                current = metadata_path.lstat()
                if (current.st_dev, current.st_ino) == published_identity and stat.S_ISREG(
                    current.st_mode
                ):
                    rollback_fd, rollback_name = tempfile.mkstemp(
                        prefix=f".{metadata_path.name}.", suffix=".rollback.tmp", dir=output_dir
                    )
                    rollback_path = pathlib.Path(rollback_name)
                    rollback_stat = os.fstat(rollback_fd)
                    rollback_identity = (rollback_stat.st_dev, rollback_stat.st_ino)
                    with os.fdopen(rollback_fd, "wb", closefd=True) as handle:
                        handle.write(original_raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(rollback_path, 0o600, follow_symlinks=False)
                    staged_rollback = rollback_path.lstat()
                    if (
                        (staged_rollback.st_dev, staged_rollback.st_ino) != rollback_identity
                        or not stat.S_ISREG(staged_rollback.st_mode)
                        or staged_rollback.st_uid != os.geteuid()
                        or staged_rollback.st_gid != directory_identity[3]
                        or staged_rollback.st_nlink != 1
                        or stat.S_IMODE(staged_rollback.st_mode) != 0o600
                    ):
                        raise RuntimeError("Backup metadata rollback staging identity changed.")
                    current = metadata_path.lstat()
                    if (current.st_dev, current.st_ino) != published_identity:
                        raise RuntimeError("Backup metadata changed before rollback.")
                    os.replace(rollback_path, metadata_path)
                    rollback_path = None
                    rollback_directory_fd = os.open(
                        output_dir,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        os.fsync(rollback_directory_fd)
                    finally:
                        os.close(rollback_directory_fd)
            except Exception as rollback_error:
                raise RuntimeError(
                    "Backup metadata commit failed and exact rollback could not be confirmed."
                ) from rollback_error
        raise
    finally:
        if temp_identity is not None and temporary_path is not None:
            try:
                current = temporary_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == temp_identity and stat.S_ISREG(current.st_mode):
                    temporary_path.unlink()
        if rollback_path is not None and rollback_identity is not None:
            try:
                current = rollback_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) == rollback_identity and stat.S_ISREG(
                    current.st_mode
                ):
                    rollback_path.unlink()


def verify_latest_existing_backup(
    output_dir: pathlib.Path,
    env_file: pathlib.Path,
    max_age_hours: float = 24.0,
    *,
    admin_database_url: str | None = None,
    private_scratch_diagnostics: bool = False,
) -> dict[str, Any]:
    """Re-verify only the newest existing backup and update its sidecar on success.

    Callers are responsible for the canonical host lock. The archive bytes and original
    creation timestamp remain untouched; any failed restore leaves both retained files
    byte-for-byte unchanged.
    """
    (
        metadata_path,
        dump_path,
        original_raw,
        metadata,
        metadata_identity,
        dump_identity,
        directory_identity,
        archive_sha256,
        _,
    ) = _latest_pair_for_verification(output_dir, max_age_hours=max_age_hours)
    require_commands("systemd-run", "systemctl")
    try:
        table_count = _run_isolated_restore_drill(
            dump_path,
            timestamp_slug=utc_now().strftime("%Y%m%dT%H%M%SZ"),
            private_scratch_diagnostics=private_scratch_diagnostics,
        )
    except Exception as exc:
        diagnostic = getattr(exc, "restore_diagnostic", None)
        if isinstance(diagnostic, dict):
            diagnostic = {
                **diagnostic,
                "archive_sha256": archive_sha256,
                "archive_size_bytes": dump_identity[7],
            }
            if _valid_restore_diagnostic(diagnostic):
                setattr(exc, "restore_diagnostic", diagnostic)
        raise

    # A restore can cross the source-age deadline; check the original creation time again.
    _, age_hours = _verify_backup_age(metadata, max_age_hours=max_age_hours)
    latest = _latest_pair_for_verification(output_dir, max_age_hours=max_age_hours)
    if (
        latest[0] != metadata_path
        or latest[2] != original_raw
        or latest[4] != metadata_identity
        or latest[6] != directory_identity
    ):
        raise RuntimeError("Latest backup metadata changed during restore verification.")
    dump_after = dump_path.lstat()
    if (
        _stable_file_identity(dump_after) != dump_identity
        or sha256_file(dump_path) != archive_sha256
    ):
        raise RuntimeError("Latest backup archive changed during restore verification.")
    updated = dict(metadata)
    updated["restore_verified"] = True
    updated["alembic_revision_verified"] = True
    updated["restored_table_count"] = table_count
    _, age_hours = _verify_backup_age(metadata, max_age_hours=max_age_hours)
    verified_at = utc_now()
    updated["restore_verified_at_utc"] = verified_at.isoformat().replace("+00:00", "Z")
    _publish_verified_metadata(
        output_dir,
        metadata_path,
        original_raw=original_raw,
        original_identity=metadata_identity,
        directory_identity=directory_identity,
        updated=updated,
        max_age_hours=max_age_hours,
    )
    return {
        "ok": True,
        "metadata_file": str(metadata_path),
        "dump_file": str(dump_path),
        "age_hours": round(age_hours, 3),
        "restore_verified": True,
        "alembic_revision_verified": True,
        "restored_table_count": table_count,
        "sha256": archive_sha256,
        "restore_verified_at_utc": updated["restore_verified_at_utc"],
    }


def local_postgres_admin_command(action: str, target: DatabaseTarget, database: str) -> list[str]:
    if action == "create":
        command = ["createdb", "--owner", target.username, database]
    elif action == "drop":
        command = ["dropdb", "--if-exists", database]
    else:  # pragma: no cover - internal programming error
        raise ValueError(f"Unsupported database admin action: {action}")
    return ["runuser", "-u", "postgres", "--", *command]


def remote_admin_command(
    action: str,
    admin_target: DatabaseTarget,
    app_target: DatabaseTarget,
    database: str,
) -> list[str]:
    base = connection_args(admin_target, include_database=False)
    if action == "create":
        return ["createdb", *base, "--owner", app_target.username, database]
    if action == "drop":
        return ["dropdb", *base, "--if-exists", database]
    raise ValueError(f"Unsupported database admin action: {action}")


def prune_backups(output_dir: pathlib.Path, *, keep: int) -> list[str]:
    if keep < 1:
        raise ValueError("--keep must be at least 1.")
    dumps = sorted(output_dir.glob("platformdb-*.dump"), key=lambda path: path.stat().st_mtime)
    removed: list[str] = []
    for dump_path in dumps[:-keep]:
        metadata_path = dump_path.with_suffix(".json")
        dump_path.unlink()
        removed.append(str(dump_path))
        if metadata_path.exists():
            metadata_path.unlink()
            removed.append(str(metadata_path))
    return removed


def prune_unverified_backups(
    output_dir: pathlib.Path,
    *,
    preserve_metadata: pathlib.Path,
) -> list[str]:
    removed: list[str] = []
    for metadata_path in output_dir.glob("platformdb-*.json"):
        if metadata_path == preserve_metadata:
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.get("restore_verified"):
            continue
        dump_file = metadata.get("dump_file")
        if isinstance(dump_file, str):
            dump_path = output_dir / dump_file
            if dump_path.exists():
                dump_path.unlink()
                removed.append(str(dump_path))
        metadata_path.unlink()
        removed.append(str(metadata_path))
    return removed


def _inventory_file(path: pathlib.Path) -> tuple[os.stat_result, str, bytes | None]:
    """Hash one closed regular file without following links or accepting races."""

    try:
        path_stat = path.lstat()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Refusing an unsafe or changing existing backup entry.") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise RuntimeError("Refusing unsafe existing backup entry.")
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if path.suffix == ".json" else None
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if chunks is not None:
                if total > 1024 * 1024:
                    raise RuntimeError("Refusing oversized existing backup metadata.")
                chunks.append(chunk)
        after = os.fstat(descriptor)
        current_path = path.lstat()
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields) or any(
            getattr(after, field) != getattr(current_path, field) for field in stable_fields
        ) or total != before.st_size:
            raise RuntimeError("Existing backup entry changed while being inventoried.")
        return after, digest.hexdigest(), b"".join(chunks) if chunks is not None else None
    finally:
        os.close(descriptor)


def backup_inventory(
    output_dir: pathlib.Path,
) -> tuple[tuple[str, int, int, int, int, int, int, str, int, int, int], ...]:
    entries: list[tuple[str, int, int, int, int, int, int, str, int, int, int]] = []
    for path in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if not path.name.startswith("platformdb-") or path.suffix not in {".dump", ".json"}:
            continue
        metadata, content_sha256, _ = _inventory_file(path)
        entries.append(
            (
                path.name,
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                content_sha256,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_ctime_ns,
            )
        )
    return tuple(entries)


def validate_complete_backup_pairs(
    output_dir: pathlib.Path,
    entries: tuple[tuple[str, int, int, int, int, int, int, str, int, int, int], ...],
) -> None:
    """Require exact dump/sidecar pairs whose recorded bytes match on disk."""

    by_name = {entry[0]: entry for entry in entries}
    if len(by_name) != len(entries):
        raise RuntimeError("Refusing duplicate existing backup entry names.")
    if not by_name:
        return
    dump_names = {name for name in by_name if name.endswith(".dump")}
    sidecar_names = {name for name in by_name if name.endswith(".json")}
    dump_stems = {name[:-5] for name in dump_names}
    sidecar_stems = {name[:-5] for name in sidecar_names}
    if not dump_names or dump_stems != sidecar_stems:
        raise RuntimeError("Refusing incomplete existing backup pairs.")
    for dump_name in sorted(dump_names):
        sidecar_name = f"{dump_name[:-5]}.json"
        try:
            sidecar_stat, sidecar_digest, sidecar_bytes = _inventory_file(output_dir / sidecar_name)
            metadata = json.loads((sidecar_bytes or b"").decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError("Refusing invalid existing backup metadata.") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("dump_file") != dump_name
            or type(metadata.get("size_bytes")) is not int
            or metadata.get("size_bytes") != by_name[dump_name][5]
            or not isinstance(metadata.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is None
            or metadata["sha256"] != by_name[dump_name][7]
            or sidecar_digest != by_name[sidecar_name][7]
            or (sidecar_stat.st_dev, sidecar_stat.st_ino) != (by_name[sidecar_name][1], by_name[sidecar_name][2])
        ):
            raise RuntimeError("Refusing mismatched existing backup pair.")


def backup_inventory_digest(
    entries: tuple[tuple[str, int, int, int, int, int, int, str, int, int, int], ...],
) -> str:
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _publish_noclobber(temporary_path: pathlib.Path, target_path: pathlib.Path) -> os.stat_result:
    temporary_identity = temporary_path.lstat()
    try:
        os.link(temporary_path, target_path, follow_symlinks=False)
    except FileExistsError as exc:
        raise RuntimeError("Backup destination already exists; refusing to replace it.") from exc
    try:
        published = target_path.lstat()
        temporary = temporary_path.lstat()
        if (published.st_dev, published.st_ino) != (temporary.st_dev, temporary.st_ino):
            raise RuntimeError("Backup publication identity changed.")
    except Exception:
        _unlink_if_same_inode(
            target_path,
            (temporary_identity.st_dev, temporary_identity.st_ino),
        )
        raise
    temporary_path.unlink()
    return published


def _fsync_backup_dump(
    path: pathlib.Path,
    *,
    expected_identity: tuple[int, int],
    expected_uid: int,
) -> None:
    """Durably flush one exact private dump inode before restore or publish."""
    if type(expected_uid) is not int or expected_uid < 0:
        raise RuntimeError("Temporary backup archive owner is invalid.")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        path_before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or (before.st_dev, before.st_ino) != expected_identity
            or (path_before.st_dev, path_before.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError("Temporary backup archive identity is unsafe before fsync.")

        # Do not catch this error: a failed flush must stop restore and publish.
        os.fsync(descriptor)

        after = os.fstat(descriptor)
        path_after = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_uid != expected_uid
            or stat.S_IMODE(after.st_mode) != 0o600
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != expected_identity
            or (path_after.st_dev, path_after.st_ino)
            != (after.st_dev, after.st_ino)
            or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        ):
            raise RuntimeError("Temporary backup archive changed while being fsynced.")
    finally:
        os.close(descriptor)


def _unlink_if_same_inode(path: pathlib.Path, identity: tuple[int, int] | None) -> None:
    if path is None or identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == identity and stat.S_ISREG(current.st_mode):
        path.unlink()


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_published_backup_pair(
    dump_path: pathlib.Path,
    dump_identity: tuple[int, int] | None,
    metadata_path: pathlib.Path,
    metadata_identity: tuple[int, int] | None,
) -> None:
    """Remove only this operation's exact published inodes after a failed create."""
    removed = False
    failures: list[str] = []
    for path, identity in ((metadata_path, metadata_identity), (dump_path, dump_identity)):
        if identity is None:
            continue
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            failures.append(path.name)
            continue
        try:
            path.unlink()
            removed = True
        except OSError:
            failures.append(path.name)
    if removed:
        _fsync_directory(dump_path.parent)
    if failures:
        raise RuntimeError("Could not roll back the exact newly published backup pair.")


def perform_restore_drill(
    dump_path: pathlib.Path,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    timestamp_slug: str,
    archive_stdin: BinaryIO | None = None,
    restore_as_postgres: bool = False,
    restore_no_tablespaces: bool = False,
    resource_guard: Callable[[], None] | None = None,
    capture_private_output: bool = False,
    phase_observer: Callable[[str, str], None] | None = None,
) -> int:
    drill_database = f"platform_restore_drill_{timestamp_slug.lower()}_{os.getpid()}"
    use_local_admin = (
        admin_target is None
        and os.geteuid() == 0
        and app_target.host in LOCAL_DATABASE_HOSTS
        and shutil.which("runuser") is not None
    )
    if use_local_admin:
        create_command = local_postgres_admin_command("create", app_target, drill_database)
        drop_command = local_postgres_admin_command("drop", app_target, drill_database)
        admin_command_target = None
    else:
        effective_admin = admin_target or app_target.with_database("postgres")
        create_command = remote_admin_command("create", effective_admin, app_target, drill_database)
        drop_command = remote_admin_command("drop", effective_admin, app_target, drill_database)
        admin_command_target = effective_admin

    created: bool | None = False
    failure_stage = "pre_create_admission"
    failure: BaseException | None = None
    failure_traceback = None
    failure_reason = "none"
    failure_free_bytes: int | None = None
    failure_required_free_bytes: int | None = None
    drop_outcome = "not_required"
    database_absent: bool | None = None
    table_count: int | None = None
    try:
        require_restore_headroom()
        failure_stage = "create_database"
        # A failed connection can leave the server-side create outcome uncertain.
        created = None
        run_command(create_command, target=admin_command_target)
        created = True
        restore_target = app_target.with_database(drill_database)
        failure_stage = "extension_setup"
        for extension in REQUIRED_PLATFORM_EXTENSIONS:
            run_command(
                [
                    "psql",
                    "--no-psqlrc",
                    *connection_args(restore_target),
                    "--command",
                    f"CREATE EXTENSION IF NOT EXISTS {extension} WITH SCHEMA public;",
                ],
                target=restore_target,
                capture_output=True,
            )
        failure_stage = "schema_setup"
        run_command(
            [
                "psql",
                "--no-psqlrc",
                *connection_args(restore_target),
                "--command",
                "CREATE SCHEMA platform AUTHORIZATION CURRENT_USER;",
            ],
            target=restore_target,
            capture_output=True,
        )
        failure_stage = "restore_platform"
        for selector in (
            ("--schema=platform",),
            ("--schema=public",),
        ):
            if selector == ("--schema=public",):
                failure_stage = "restore_public"
            restore_command = [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                *selector,
                *connection_args(restore_target),
            ]
            if restore_no_tablespaces:
                restore_command.append("--no-tablespaces")
            if archive_stdin is None:
                restore_command.append(str(dump_path))
            else:
                archive_stdin.seek(0)
            if restore_as_postgres:
                restore_command = ["runuser", "-u", "postgres", "--", *restore_command]
            _scratch_run_observed_phase(
                phase_observer,
                "restore_platform" if selector == ("--schema=platform",) else "restore_public",
                lambda: run_restore_command(
                    restore_command,
                    target=restore_target,
                    stdin_handle=archive_stdin,
                    resource_guard=resource_guard,
                    capture_private_output=capture_private_output,
                ),
            )
        failure_stage = "validate_table_count"
        table_count_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'platform';",
            ],
            target=restore_target,
            capture_output=True,
        )
        table_count = int(table_count_result.stdout.strip())
        if table_count <= 0:
            raise RuntimeError("Restore drill produced no tables in the platform schema.")
        failure_stage = "validate_connectivity"
        connectivity_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT 1;",
            ],
            target=restore_target,
            capture_output=True,
        )
        if connectivity_result.stdout.strip() != "1":
            raise RuntimeError("Restore drill connectivity verification failed.")
        failure_stage = "validate_revision"
        def verify_alembic_revision() -> list[str]:
            revision_result = run_command(
                [
                    "psql",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    *connection_args(restore_target),
                    "--command",
                    "SELECT version_num FROM public.alembic_version;",
                ],
                target=restore_target,
                capture_output=True,
            )
            revisions = [
                line.strip() for line in revision_result.stdout.splitlines() if line.strip()
            ]
            if len(revisions) != 1:
                raise RuntimeError(
                    "Restore drill did not recover exactly one Alembic revision."
                )
            return revisions

        _scratch_run_observed_phase(
            phase_observer,
            "alembic_verify",
            verify_alembic_revision,
        )
        failure_stage = "validate_extensions"
        extension_count_result = run_command(
            [
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                *connection_args(restore_target),
                "--command",
                "SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm';",
            ],
            target=restore_target,
            capture_output=True,
        )
        if int(extension_count_result.stdout.strip()) != len(REQUIRED_PLATFORM_EXTENSIONS):
            raise RuntimeError("Restore drill is missing a required platform PostgreSQL extension.")
    except BaseException as exc:
        failure = exc
        failure_traceback = exc.__traceback__
        if isinstance(exc, RestoreGuardStop):
            failure_reason = exc.reason
            failure_free_bytes = exc.free_bytes
            failure_required_free_bytes = exc.required_free_bytes
    if created is True:
        primary_failure_stage = failure_stage if failure is not None else None
        failure_stage = "drop_database"
        try:
            run_command(drop_command, target=admin_command_target)
        except BaseException as exc:
            drop_outcome = "drop_failed"
            if failure is None:
                failure = exc
                failure_traceback = exc.__traceback__
                failure_reason = exc.reason if isinstance(exc, RestoreGuardStop) else "none"
                failure_stage = "drop_database"
        else:
            try:
                absence_command, absence_target = _database_absence_command(
                    drill_database,
                    app_target=app_target,
                    admin_target=admin_command_target,
                    use_local_admin=use_local_admin,
                )
                absence_result = run_command(
                    absence_command,
                    target=absence_target,
                    capture_output=True,
                )
                absence_value = absence_result.stdout.strip()
                if absence_value == "0":
                    database_absent = True
                    drop_outcome = "confirmed_absent"
                elif absence_value == "1":
                    database_absent = False
                    drop_outcome = "database_present"
                    if failure is None:
                        failure = RuntimeError("Restore drill database remained after drop.")
                        failure_traceback = failure.__traceback__
                        failure_stage = "drop_database"
                else:
                    drop_outcome = "absence_unconfirmed"
                    if failure is None:
                        failure = RuntimeError("Restore drill database absence was not confirmed.")
                        failure_traceback = failure.__traceback__
                        failure_stage = "drop_database"
            except BaseException as exc:
                drop_outcome = "absence_unconfirmed"
                if failure is None:
                    failure = exc
                    failure_traceback = exc.__traceback__
                    failure_stage = "drop_database"
        if failure is not None and primary_failure_stage is not None:
            # Preserve the primary failure stage and reason when cleanup also fails.
            failure_stage = primary_failure_stage
    if failure is not None:
        diagnostic = {
            "schema": 1,
            "restore_stage": failure_stage,
            "guard_reason": failure_reason,
            "free_bytes": failure_free_bytes,
            "required_free_bytes": failure_required_free_bytes,
            "temporary_database_created": created,
            "drop_outcome": drop_outcome,
            "temporary_database_absent": database_absent,
        }
        try:
            setattr(failure, "restore_diagnostic", diagnostic)
        except Exception:
            pass
        raise failure.with_traceback(failure_traceback)
    if table_count is None:
        raise RuntimeError("Restore drill did not produce a table count.")
    return table_count


def _database_absence_command(
    database: str,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    use_local_admin: bool,
) -> tuple[list[str], DatabaseTarget | None]:
    if not re.fullmatch(r"platform_restore_drill_[a-z0-9_]{1,80}", database):
        raise ValueError("restore drill database identity is invalid")
    sql = f"SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_database WHERE datname = '{database}') THEN 1 ELSE 0 END;"
    if use_local_admin:
        command = [
            "runuser",
            "-u",
            "postgres",
            "--",
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--dbname",
            "postgres",
            "--command",
            sql,
        ]
        return command, None
    effective_admin = admin_target or app_target.with_database("postgres")
    command = [
        "psql",
        "--no-psqlrc",
        "--tuples-only",
        "--no-align",
        *connection_args(effective_admin),
        "--command",
        sql,
    ]
    return command, effective_admin


def require_commands(*commands: str) -> None:
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        raise RuntimeError(f"Missing required PostgreSQL command(s): {', '.join(missing)}")


def _scratch_mount_snapshot(path: pathlib.Path) -> dict[str, int]:
    target = str(path)
    mount_row: tuple[list[str], list[str]] | None = None
    for line in pathlib.Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        left_fields = left.split()
        if len(left_fields) < 6 or left_fields[4].replace("\\040", " ") != target:
            continue
        mount_row = (left_fields, right.split())
        break
    if mount_row is None:
        raise RuntimeError("Isolated restore filesystem is not mounted.")
    left_fields, right_fields = mount_row
    if len(right_fields) < 3 or right_fields[0] != "tmpfs" or left_fields[3] != "/":
        raise RuntimeError("Isolated restore filesystem is not a private tmpfs mount.")
    mount_stat = path.stat()
    if (
        mount_stat.st_uid not in {0, pwd.getpwnam("postgres").pw_uid}
        or stat.S_IMODE(mount_stat.st_mode) != 0o700
        or mount_stat.st_dev == pathlib.Path("/run").stat().st_dev
    ):
        raise RuntimeError("Isolated restore filesystem mount properties are unsafe.")
    values = os.statvfs(path)
    total = values.f_blocks * values.f_frsize
    free = values.f_bavail * values.f_frsize
    if total != SCRATCH_TMPFS_BYTES or free <= SCRATCH_TMPFS_STOP_BYTES:
        raise RuntimeError("Isolated restore filesystem is below its bounded capacity.")
    return {"total_bytes": total, "free_bytes": free}


def _scratch_cgroup_snapshot() -> dict[str, int]:
    cgroup_path: str | None = None
    for row in pathlib.Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            cgroup_path = fields[2]
            break
    if cgroup_path is None or ".." in pathlib.PurePosixPath(cgroup_path).parts:
        raise RuntimeError("Scratch restore cgroup could not be identified.")
    root = pathlib.Path("/sys/fs/cgroup").resolve(strict=True)
    directory = (root / cgroup_path.lstrip("/")).resolve(strict=True)
    directory.relative_to(root)

    def read_integer(name: str) -> int:
        value = (directory / name).read_text(encoding="ascii").strip()
        if not value.isdecimal():
            raise RuntimeError("Scratch restore cgroup limit is unavailable.")
        return int(value)

    memory_max = read_integer("memory.max")
    memory_current = read_integer("memory.current")
    swap_max = read_integer("memory.swap.max")
    swap_current = read_integer("memory.swap.current")
    oom_group = read_integer("memory.oom.group")
    if (
        memory_max != SCRATCH_MEMORY_MAX_BYTES
        or swap_max != 0
        or swap_current != 0
        or oom_group != 1
        or memory_current >= memory_max - SCRATCH_MEMORY_STOP_MARGIN_BYTES
    ):
        raise RuntimeError("Scratch restore memory guard stopped the operation.")
    return {
        "memory_max_bytes": memory_max,
        "memory_current_bytes": memory_current,
        "swap_max_bytes": swap_max,
        "swap_current_bytes": swap_current,
        "oom_group": oom_group,
    }


def _scratch_mem_available_bytes() -> int:
    for line in pathlib.Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) == 3 and fields[1].isdecimal() and fields[2] == "kB":
                return int(fields[1]) * 1024
    raise RuntimeError("Host memory availability could not be verified.")


def _scratch_prepare_postgres_directory(
    path: pathlib.Path, *, uid: int, gid: int
) -> None:
    """Create and fix mode before handing the directory to PostgreSQL."""
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    os.chown(path, uid, gid)


def _scratch_proc_status_sample(pid: int) -> tuple[int, int, int] | None:
    """Return (pid, ppid, VmRSS bytes) for a bounded, stable root process sample."""
    if type(pid) is not int or pid <= 1:
        return None
    status_path = pathlib.Path(f"/proc/{pid}/status")
    exe_path = pathlib.Path(f"/proc/{pid}/exe")
    try:
        with status_path.open("rb") as status_file:
            raw_before = status_file.read(SCRATCH_PROC_STATUS_MAX_BYTES + 1)
        if len(raw_before) > SCRATCH_PROC_STATUS_MAX_BYTES:
            return None
        parsed_before = _parse_scratch_proc_status(raw_before)
        if parsed_before is None or parsed_before[0] != pid:
            return None
        expected_exe = pathlib.Path("/proc/self/exe").stat()
        sampled_exe = exe_path.stat()
        if (sampled_exe.st_dev, sampled_exe.st_ino) != (expected_exe.st_dev, expected_exe.st_ino):
            return None
        with status_path.open("rb") as status_file:
            raw_after = status_file.read(SCRATCH_PROC_STATUS_MAX_BYTES + 1)
        if len(raw_after) > SCRATCH_PROC_STATUS_MAX_BYTES:
            return None
        parsed_after = _parse_scratch_proc_status(raw_after)
        if parsed_after is None or parsed_before[:2] != parsed_after[:2]:
            return None
        return parsed_before
    except (OSError, ValueError, UnicodeError):
        return None


def _scratch_private_cgroup_file(directory_fd: int, name: str) -> bytes | None:
    if name not in {"memory.current", "memory.stat", "memory.events"}:
        return None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        try:
            result = bytearray()
            while len(result) <= SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES:
                block = os.read(
                    descriptor,
                    min(4096, SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES + 1 - len(result)),
                )
                if not block:
                    break
                result.extend(block)
            if len(result) > SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES:
                return None
            return bytes(result)
        finally:
            os.close(descriptor)
    except OSError:
        return None


def _scratch_private_counter(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeError:
        return None
    if not value.isdecimal():
        return None
    number = int(value)
    return number if number <= (1 << 60) else None


def _scratch_private_proc_file(pid_text: str, name: str) -> bytes | None:
    if (
        not re.fullmatch(r"[1-9][0-9]{0,9}", pid_text)
        or name not in {"cgroup"}
    ):
        return None
    try:
        descriptor = os.open(
            f"/proc/{pid_text}/{name}",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            result = bytearray()
            while len(result) <= SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES:
                block = os.read(
                    descriptor,
                    min(4096, SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES + 1 - len(result)),
                )
                if not block:
                    break
                result.extend(block)
            if len(result) > SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES:
                return None
            return bytes(result)
        finally:
            os.close(descriptor)
    except OSError:
        return None


def _scratch_private_key_values(raw: bytes | None, required: set[str]) -> dict[str, int] | None:
    if raw is None:
        return None
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError:
        return None
    values: dict[str, int] = {}
    for line in lines:
        fields = line.split()
        if not fields or fields[0] not in required:
            continue
        if len(fields) != 2 or fields[0] in values or not fields[1].isdecimal():
            return None
        number = int(fields[1])
        if number > (1 << 60):
            return None
        values[fields[0]] = number
    return values if set(values) == required else None


def _scratch_unknown_resource_attribution(
    reason: str, *, sample_offset_ms: int | None = None
) -> dict[str, Any]:
    if reason not in _SCRATCH_RESOURCE_UNKNOWN_REASONS:
        reason = "internal_error"
    return {
        "schema": 1,
        "status": "unknown",
        "sample_offset_ms": sample_offset_ms,
        "unknown_reason": reason,
        "controller": {"identity_bound": False, "rss_bytes": None},
        "worker": {"identity_bound": False, "rss_bytes": None},
        "cgroup": {
            "identity_bound": False,
            "memory_current_bytes": None,
            "anon_bytes": None,
            "file_bytes": None,
            "shmem_bytes": None,
            "events": {"high": None, "max": None, "oom": None, "oom_kill": None},
        },
    }


def _scratch_private_resource_attribution(
    *,
    snapshot: dict[str, Any] | None,
    worker_identity: tuple[str, str, str] | None,
    control_group: str | None,
    launch_monotonic_ns: int | None,
) -> dict[str, Any]:
    """Capture bounded role/cgroup observations without affecting guard policy."""
    started_ns = time.monotonic_ns()

    def unknown(reason: str) -> dict[str, Any]:
        offset = None
        if type(launch_monotonic_ns) is int and launch_monotonic_ns <= started_ns:
            offset = (started_ns - launch_monotonic_ns) // 1_000_000
        return _scratch_unknown_resource_attribution(reason, sample_offset_ms=offset)

    if (
        not isinstance(snapshot, dict)
        or worker_identity is None
        or control_group is None
        or type(launch_monotonic_ns) is not int
        or launch_monotonic_ns > started_ns
    ):
        return unknown("sample_unavailable")
    properties = snapshot.get("properties")
    worker_pid_text, invocation_id, worker_start_time = worker_identity
    if (
        not isinstance(properties, dict)
        or properties.get("ActiveState") != "active"
        or properties.get("ExecMainPID") != worker_pid_text
        or properties.get("InvocationID") != invocation_id
        or properties.get("ControlGroup") != control_group
        or not re.fullmatch(r"[1-9][0-9]{0,9}", worker_pid_text)
        or not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
        or not re.fullmatch(r"[0-9]+", worker_start_time)
        or not control_group.startswith("/system.slice/oldsparky-backup-restore-")
        or not control_group.endswith(".service")
        or ".." in pathlib.PurePosixPath(control_group).parts
    ):
        return unknown("identity_drift")

    controller_pid = os.getpid()
    try:
        controller_start_before = _scratch_process_start_time(str(controller_pid))
        controller_sample = _scratch_proc_status_sample(controller_pid)
        controller_start_after = _scratch_process_start_time(str(controller_pid))
        controller_bound = (
            os.getpid() == controller_pid
            and controller_start_before == controller_start_after
            and controller_sample is not None
            and controller_sample[0] == controller_pid
        )
        worker_start_before = _scratch_process_start_time(worker_pid_text)
        worker_sample = _scratch_proc_status_sample(int(worker_pid_text))
        worker_start_after = _scratch_process_start_time(worker_pid_text)
        worker_cgroup_before_raw = _scratch_private_proc_file(worker_pid_text, "cgroup")
        if worker_cgroup_before_raw is None:
            return unknown("sample_unavailable")
        worker_cgroup_before = worker_cgroup_before_raw.decode("ascii")
        worker_membership_before = any(
            row.split(":", 2) == ["0", "", control_group]
            for row in worker_cgroup_before.splitlines()
        )
        if (
            not controller_bound
            or worker_start_before != worker_start_time
            or worker_start_after != worker_start_time
            or worker_sample is None
            or worker_sample[0] != int(worker_pid_text)
            or not worker_membership_before
        ):
            return unknown("identity_drift")
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return unknown("sample_unavailable")

    root_fd = system_slice_fd = cgroup_fd = -1
    try:
        root_fd = os.open(
            "/sys/fs/cgroup",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        root_before = os.fstat(root_fd)
        system_slice_fd = os.open(
            "system.slice",
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        system_slice_before = os.fstat(system_slice_fd)
        leaf_name = control_group.removeprefix("/system.slice/")
        if "/" in leaf_name or not re.fullmatch(
            r"oldsparky-backup-restore-[0-9a-f]{24}\.service", leaf_name
        ):
            return unknown("identity_drift")
        cgroup_fd = os.open(
            leaf_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=system_slice_fd,
        )
        cgroup_before = os.fstat(cgroup_fd)
        current = _scratch_private_counter(_scratch_private_cgroup_file(cgroup_fd, "memory.current"))
        stat_values = _scratch_private_key_values(
            _scratch_private_cgroup_file(cgroup_fd, "memory.stat"),
            {"anon", "file", "shmem"},
        )
        event_values = _scratch_private_key_values(
            _scratch_private_cgroup_file(cgroup_fd, "memory.events"),
            {"high", "max", "oom", "oom_kill"},
        )
        if current is None or stat_values is None or event_values is None:
            return unknown("parse_invalid")
        cgroup_after = os.fstat(cgroup_fd)
        system_slice_after = os.fstat(system_slice_fd)
        root_after = os.fstat(root_fd)
        system_slice_named = os.stat("system.slice", dir_fd=root_fd, follow_symlinks=False)
        cgroup_named = os.stat(leaf_name, dir_fd=system_slice_fd, follow_symlinks=False)
        worker_cgroup_after_raw = _scratch_private_proc_file(worker_pid_text, "cgroup")
        if worker_cgroup_after_raw is None:
            return unknown("sample_unavailable")
        worker_cgroup_after = worker_cgroup_after_raw.decode("ascii")
        worker_membership_after = any(
            row.split(":", 2) == ["0", "", control_group]
            for row in worker_cgroup_after.splitlines()
        )
        if (
            _scratch_process_start_time(worker_pid_text) != worker_start_time
            or not worker_membership_after
            or _scratch_process_start_time(str(controller_pid)) != controller_start_before
            or os.getpid() != controller_pid
            or (root_before.st_dev, root_before.st_ino) != (root_after.st_dev, root_after.st_ino)
            or (system_slice_before.st_dev, system_slice_before.st_ino)
            != (system_slice_after.st_dev, system_slice_after.st_ino)
            or (system_slice_before.st_dev, system_slice_before.st_ino)
            != (system_slice_named.st_dev, system_slice_named.st_ino)
            or (cgroup_before.st_dev, cgroup_before.st_ino)
            != (cgroup_after.st_dev, cgroup_after.st_ino)
            or (cgroup_before.st_dev, cgroup_before.st_ino)
            != (cgroup_named.st_dev, cgroup_named.st_ino)
        ):
            return unknown("identity_drift")
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return unknown("sample_unavailable")
    finally:
        for descriptor in (cgroup_fd, system_slice_fd, root_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    finished_ns = time.monotonic_ns()
    offset_ms = (finished_ns - launch_monotonic_ns) // 1_000_000
    result = {
        "schema": 1,
        "status": "complete",
        "sample_offset_ms": offset_ms,
        "unknown_reason": None,
        "controller": {"identity_bound": True, "rss_bytes": controller_sample[2]},
        "worker": {"identity_bound": True, "rss_bytes": worker_sample[2]},
        "cgroup": {
            "identity_bound": True,
            "memory_current_bytes": current,
            "anon_bytes": stat_values["anon"],
            "file_bytes": stat_values["file"],
            "shmem_bytes": stat_values["shmem"],
            "events": {
                "high": event_values["high"],
                "max": event_values["max"],
                "oom": event_values["oom"],
                "oom_kill": event_values["oom_kill"],
            },
        },
    }
    if not valid_scratch_resource_attribution(result):
        return unknown("internal_error")
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > SCRATCH_RESOURCE_ATTRIBUTION_MAX_BYTES:
        return unknown("cap_exceeded")
    return result


def _parse_scratch_proc_status(raw: bytes) -> tuple[int, int, int] | None:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError:
        return None
    fields: dict[str, list[str]] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in {"Pid", "PPid", "Uid", "VmRSS"}:
            if key in fields:
                return None
            fields[key] = value.split()
    if set(fields) != {"Pid", "PPid", "Uid", "VmRSS"}:
        return None
    pid_values, ppid_values = fields["Pid"], fields["PPid"]
    uid_values, rss_values = fields["Uid"], fields["VmRSS"]
    if (
        len(pid_values) != 1
        or len(ppid_values) != 1
        or len(uid_values) != 4
        or len(rss_values) != 2
        or any(not item.isdecimal() for item in (*pid_values, *ppid_values, *uid_values, rss_values[0]))
        or rss_values[1] != "kB"
        or any(item != "0" for item in uid_values)
    ):
        return None
    rss_bytes = int(rss_values[0]) * 1024
    if rss_bytes > SCRATCH_ADMISSION_RSS_MAX_BYTES:
        return None
    return int(pid_values[0]), int(ppid_values[0]), rss_bytes


def _scratch_admission_rss_snapshot() -> dict[str, int | None]:
    """Best-effort helper/parent RSS telemetry; never used for admission decisions."""
    result: dict[str, int | None] = {
        "helper_rss_bytes": None,
        "direct_parent_rss_bytes": None,
        "observed_pair_rss_bytes": None,
        "observed_process_count": 0,
    }
    try:
        helper_pid = os.getpid()
        parent_pid = os.getppid()
        helper = _scratch_proc_status_sample(helper_pid)
        parent = _scratch_proc_status_sample(parent_pid)
        if helper is not None and helper[1] == parent_pid and os.getpid() == helper_pid:
            result["helper_rss_bytes"] = helper[2]
        if parent is not None and os.getppid() == parent_pid:
            result["direct_parent_rss_bytes"] = parent[2]
        count = sum(
            result[key] is not None
            for key in ("helper_rss_bytes", "direct_parent_rss_bytes")
        )
        result["observed_process_count"] = count
        if count == 2:
            result["observed_pair_rss_bytes"] = (
                result["helper_rss_bytes"] + result["direct_parent_rss_bytes"]
            )
    except Exception:
        # Telemetry must not replace the original resource-admission failure.
        pass
    return result


def _raise_scratch_host_memory_reserve(
    guard_site: str,
    *,
    message: str,
    retain_legacy_admission: bool = False,
) -> None:
    available_bytes = _scratch_mem_available_bytes()
    if available_bytes >= SCRATCH_MEMORY_ADMISSION_BYTES:
        return
    error = RuntimeError(message)
    diagnostic = {
        "schema": 2,
        "guard_site": guard_site,
        "guard_reason": "memory_reserve",
        "available_bytes": available_bytes,
        "required_bytes": SCRATCH_MEMORY_ADMISSION_BYTES,
    }
    if valid_scratch_host_guard_diagnostic(diagnostic):
        error.scratch_host_guard_diagnostic = diagnostic
    if retain_legacy_admission:
        try:
            rss_snapshot = _scratch_admission_rss_snapshot()
        except Exception:
            rss_snapshot = {
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            }
        legacy = {
            "schema": 1,
            "phase": "pre_create_admission",
            "guard_reason": "memory_reserve",
            "available_bytes": available_bytes,
            "required_bytes": SCRATCH_MEMORY_ADMISSION_BYTES,
            **rss_snapshot,
        }
        if valid_scratch_admission_diagnostic(legacy):
            error.scratch_admission_diagnostic = legacy
    raise error


def _scratch_resource_guard(
    root: pathlib.Path,
    *,
    guard_site: str = "worker_pg_restore_resource_guard",
) -> None:
    require_restore_headroom(pathlib.Path("/"))
    _raise_scratch_host_memory_reserve(
        guard_site,
        message="Scratch restore stopped by the host-memory reserve guard.",
    )
    _scratch_cgroup_snapshot()
    _scratch_mount_snapshot(root)


def _open_verified_scratch_archive(args: argparse.Namespace) -> BinaryIO:
    archive_path = pathlib.Path(args._scratch_archive)
    expected_sha256 = args._scratch_archive_sha256
    expected = (
        args._scratch_archive_dev,
        args._scratch_archive_ino,
        args._scratch_archive_size,
    )
    if (
        any(type(value) is not int or value < 0 for value in expected)
        or not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RuntimeError("Scratch restore archive identity is invalid.")
    descriptor = os.open(
        archive_path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino, before.st_size) != expected
        ):
            raise RuntimeError("Scratch restore archive identity changed.")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or total != before.st_size
            or digest.hexdigest() != expected_sha256
        ):
            raise RuntimeError("Scratch restore archive content changed.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        header = os.read(descriptor, 5)
        if header != b"PGDMP":
            raise RuntimeError("Scratch restore archive format is invalid.")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise


def _scratch_read_worker_token(
    stream: Any,
    expected: bytes,
    *,
    require_eof: bool,
    deadline: float,
) -> bytes:
    """Read one framed worker token without buffering bytes from the next phase."""
    failure_message = (
        "Scratch restore completion was not acknowledged."
        if require_eof
        else "Scratch restore authorization was not received."
    )
    try:
        raw_fd = stream.fileno()
    except (AttributeError, OSError):
        # In-memory streams are used by contract tests; production stdin is a pipe.
        try:
            value = stream.buffer.read(len(expected) + 1)
            if value == expected and require_eof:
                value += stream.buffer.read(1)
        except (AttributeError, OSError) as exc:
            raise RuntimeError(failure_message) from exc
        if value != expected or time.monotonic() >= deadline:
            raise RuntimeError(failure_message)
        return expected

    was_blocking: bool | None = None
    selector: selectors.BaseSelector | None = None
    try:
        was_blocking = os.get_blocking(raw_fd)
        os.set_blocking(raw_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(raw_fd, selectors.EVENT_READ)
        received = bytearray()
        while len(received) < len(expected):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise RuntimeError(failure_message)
            try:
                chunk = os.read(
                    raw_fd,
                    min(len(expected) - len(received), len(expected) + 1 - len(received)),
                )
            except BlockingIOError:
                continue
            if not chunk:
                raise RuntimeError(failure_message)
            received.extend(chunk)
            if not expected.startswith(received):
                raise RuntimeError(failure_message)

        # Before DONE, EOF is invalid because the parent must keep the pipe open
        # until the candidate result has been checked. After DONE, EOF is the
        # framing terminator because the parent closes stdin with that token.
        if time.monotonic() >= deadline:
            raise RuntimeError(failure_message)
        if require_eof:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RuntimeError("Scratch restore completion was not acknowledged.")
                try:
                    probe = os.read(raw_fd, 1)
                except BlockingIOError:
                    continue
                if probe:
                    raise RuntimeError("Scratch restore completion was not acknowledged.")
                break
        elif selector.select(0):
            try:
                probe = os.read(raw_fd, 1)
            except BlockingIOError:
                probe = None
            if probe is not None:
                raise RuntimeError(failure_message)
        if time.monotonic() >= deadline:
            raise RuntimeError(failure_message)
        return bytes(received)
    except OSError as exc:
        raise RuntimeError(failure_message) from exc
    finally:
        if selector is not None:
            selector.close()
        if was_blocking is not None:
            try:
                os.set_blocking(raw_fd, was_blocking)
            except OSError:
                pass


def _scratch_worker_main(args: argparse.Namespace) -> int:
    worker_deadline = time.monotonic() + SCRATCH_RUNTIME_SECONDS
    startup_deadline = min(
        worker_deadline,
        time.monotonic() + SCRATCH_STARTUP_SECONDS,
    )
    try:
        args._scratch_fault_site = "worker_start_token"
        authorization = _scratch_read_worker_token(
            sys.stdin,
            SCRATCH_START_TOKEN,
            require_eof=False,
            deadline=startup_deadline,
        )
    except (OSError, AttributeError) as exc:
        raise RuntimeError("Scratch restore authorization was not received.") from exc
    if authorization != SCRATCH_START_TOKEN:
        raise RuntimeError("Scratch restore authorization was not received.")
    for name in tuple(os.environ):
        if name.startswith("PG"):
            os.environ.pop(name, None)
    root = pathlib.Path(args._scratch_root)
    args._scratch_fault_site = "worker_root_identity"
    if (
        not re.fullmatch(r"/run/oldsparky-backup-restore-[0-9a-f]{24}", str(root))
        or not root.is_dir()
    ):
        raise RuntimeError("Scratch restore root is invalid.")
    args._scratch_fault_site = "worker_mount_snapshot"
    _scratch_mount_snapshot(root)
    args._scratch_fault_site = "worker_cgroup_snapshot"
    _scratch_cgroup_snapshot()
    args._scratch_fault_site = "worker_host_reserve"
    if getattr(args, "_scratch_authorization_probe", False):
        _raise_scratch_host_memory_reserve(
            "worker_after_start_before_authorization_probe",
            message="Scratch restore admission memory reserve is unavailable.",
        )
    else:
        _raise_scratch_host_memory_reserve(
            "worker_after_start_before_archive_open",
            message="Scratch restore admission memory reserve is unavailable.",
        )
    if os.geteuid() != 0:
        raise RuntimeError("Scratch restore supervisor is not root.")
    if getattr(args, "_scratch_authorization_probe", False):
        if any(
            getattr(args, name, None) is not None
            for name in (
                "_scratch_archive", "_scratch_archive_dev", "_scratch_archive_ino",
                "_scratch_archive_size", "_scratch_archive_sha256",
            )
        ):
            raise RuntimeError("Authorization probe must not receive archive identity.")
        args._scratch_fault_site = "worker_result_frame"
        result_stream = getattr(sys.stdout, "buffer", None)
        if result_stream is None:
            raise RuntimeError("Scratch authorization probe result channel is unavailable.")
        result_stream.write(SCRATCH_AUTHORIZATION_PROBE_FRAME)
        result_stream.flush()
        try:
            args._scratch_fault_site = "worker_done_token"
            completion = _scratch_read_worker_token(
                sys.stdin,
                SCRATCH_DONE_TOKEN,
                require_eof=True,
                deadline=worker_deadline,
            )
        except (OSError, AttributeError) as exc:
            raise RuntimeError("Scratch authorization probe completion was not acknowledged.") from exc
        if completion != SCRATCH_DONE_TOKEN:
            raise RuntimeError("Scratch authorization probe completion was not acknowledged.")
        return 0
    args._scratch_phase_origin_monotonic_ns = time.monotonic_ns()
    args._scratch_fault_site = "worker_archive_open"
    def verify_archive() -> BinaryIO:
        opened_archive = _open_verified_scratch_archive(args)
        archive_info = os.fstat(opened_archive.fileno())
        if (archive_info.st_dev, archive_info.st_ino) != (
            args._scratch_archive_dev, args._scratch_archive_ino
        ):
            opened_archive.close()
            raise RuntimeError("Scratch restore archive bind identity does not match.")
        return opened_archive

    archive = _scratch_run_observed_phase(
        lambda phase, event: _scratch_emit_phase_boundary(args, phase, event),
        "archive_verify",
        verify_archive,
    )

    postgres = pwd.getpwnam("postgres")
    args._scratch_fault_site = "worker_directory_setup"
    os.chown(root, postgres.pw_uid, postgres.pw_gid)
    pgdata = root / "data"
    socket_dir = root / "socket"
    pglog = root / "postgres.log"
    for path in (pgdata, socket_dir):
        _scratch_prepare_postgres_directory(
            path, uid=postgres.pw_uid, gid=postgres.pw_gid
        )

    def as_postgres(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        return run_command(
            ["runuser", "-u", "postgres", "--", *command],
            capture_output=capture_output,
        )

    args._scratch_fault_site = "worker_postgres_tools"
    bindir_result = run_command(["pg_config", "--bindir"], capture_output=True)
    bindir = pathlib.Path(bindir_result.stdout.strip())
    version_result = run_command([str(bindir / "postgres"), "--version"], capture_output=True)
    if re.search(r"\b16(?:\.|\s|$)", version_result.stdout) is None:
        archive.close()
        raise RuntimeError("Scratch restore requires PostgreSQL 16 binaries.")
    require_commands("runuser")
    pgctl = str(bindir / "pg_ctl")
    initdb = str(bindir / "initdb")
    args._scratch_fault_site = "worker_initdb"

    def phase_observer(phase: str, event: str) -> None:
        _scratch_emit_phase_boundary(args, phase, event)

    _scratch_run_observed_phase(
        phase_observer,
        "initdb",
        lambda: as_postgres(
            [
                initdb,
                "--pgdata",
                str(pgdata),
                "--username=postgres",
                "--no-locale",
                "--encoding=UTF8",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
            capture_output=True,
        ),
    )
    started = False
    primary_error: BaseException | None = None
    table_count: int | None = None
    try:
        args._scratch_fault_site = "worker_after_initdb_guard"
        _scratch_resource_guard(
            root, guard_site="worker_after_initdb_before_postgres_start"
        )
        server_options = shlex.join(
            [
                "-c",
                "listen_addresses=",
                "-c",
                f"unix_socket_directories={socket_dir}",
                "-c",
                f"port={SCRATCH_PG_PORT}",
                "-c",
                "fsync=on",
                "-c",
                "full_page_writes=on",
                "-c",
                "synchronous_commit=on",
                "-c",
                "dynamic_shared_memory_type=mmap",
                "-c",
                "shared_buffers=64MB",
                "-c",
                "work_mem=4MB",
                "-c",
                "maintenance_work_mem=32MB",
                "-c",
                "max_connections=10",
            ]
        )
        args._scratch_fault_site = "worker_postgres_start"
        _scratch_run_observed_phase(
            phase_observer,
            "postgres_start",
            lambda: as_postgres(
                [pgctl, "--pgdata", str(pgdata), "--log", str(pglog), "--options", server_options,
                 "--wait", "--timeout=30", "start"],
                capture_output=True,
            ),
        )
        started = True
        args._scratch_fault_site = "worker_after_postgres_start_guard"
        _scratch_resource_guard(root, guard_site="worker_after_postgres_start")
        args._scratch_fault_site = "worker_role_setup"
        admin_target = DatabaseTarget(
            host=str(socket_dir),
            port=SCRATCH_PG_PORT,
            username="postgres",
            password=None,
            database="postgres",
        )
        _scratch_create_application_role(admin_target)
        scratch_target = DatabaseTarget(
            host=str(socket_dir),
            port=SCRATCH_PG_PORT,
            username=SCRATCH_APP_ROLE,
            password=None,
            database="postgres",
        )
        args._scratch_fault_site = "worker_restore"

        def pg_restore_resource_guard() -> None:
            args._scratch_fault_site = "worker_pg_restore_resource_guard"
            _scratch_resource_guard(root, guard_site="worker_pg_restore_resource_guard")

        table_count = perform_restore_drill(
            root / "archive.dump",
            app_target=scratch_target,
            admin_target=admin_target,
            timestamp_slug=args._scratch_timestamp_slug,
            archive_stdin=archive,
            restore_as_postgres=True,
            restore_no_tablespaces=True,
            resource_guard=pg_restore_resource_guard,
            capture_private_output=getattr(args, "private_scratch_diagnostics", False),
            phase_observer=phase_observer,
        )
        args._scratch_fault_site = "worker_after_restore_guard"
        _scratch_resource_guard(
            root, guard_site="worker_after_restore_before_postgres_stop"
        )
    except BaseException as exc:
        if getattr(args, "private_scratch_diagnostics", False):
            _retain_first_scratch_fault(
                exc,
                origin="worker",
                site=getattr(args, "_scratch_fault_site", "worker_entry"),
            )
        primary_error = exc
        if getattr(args, "private_scratch_diagnostics", False) and not hasattr(
            exc, "scratch_worker_private_output"
        ):
            def bounded_output(value: Any, limit: int) -> bytes:
                if isinstance(value, bytes):
                    raw = value
                elif isinstance(value, str):
                    raw = value.encode("utf-8", errors="replace")
                else:
                    return b""
                return raw[:limit]

            output = bounded_output(
                getattr(exc, "stdout", getattr(exc, "output", None)),
                SCRATCH_PRIVATE_STDOUT_BYTES,
            )
            error_output = bounded_output(
                getattr(exc, "stderr", None), SCRATCH_PRIVATE_STDERR_BYTES
            )
            if output or error_output:
                raw_output = getattr(exc, "stdout", getattr(exc, "output", None))
                raw_error_output = getattr(exc, "stderr", None)
                output_size = len(raw_output) if isinstance(raw_output, (bytes, str)) else 0
                error_output_size = (
                    len(raw_error_output)
                    if isinstance(raw_error_output, (bytes, str))
                    else 0
                )
                setattr(
                    exc,
                    "scratch_worker_private_output",
                    (
                        output,
                        error_output,
                        output_size > len(output),
                        error_output_size > len(error_output),
                    ),
                )
    finally:
        if started or (pgdata / "postmaster.pid").exists():
            args._scratch_fault_site = "worker_postgres_stop"
            try:
                as_postgres(
                    [pgctl, "--pgdata", str(pgdata), "--mode=fast", "--wait", "--timeout=20", "stop"],
                    capture_output=True,
                )
            except BaseException as stop_error:
                if primary_error is None:
                    primary_error = stop_error
        archive.close()
    if primary_error is not None:
        if getattr(args, "private_scratch_diagnostics", False):
            private_output = getattr(primary_error, "scratch_worker_private_output", None)
            if (
                isinstance(private_output, tuple)
                and len(private_output) == 4
                and type(private_output[0]) is bytes
                and type(private_output[1]) is bytes
                and type(private_output[2]) is bool
                and type(private_output[3]) is bool
            ):
                output = private_output[0]
                error_output = private_output[1]
                if private_output[2]:
                    output += b"\0"
                if private_output[3]:
                    error_output += b"\0"
                _write_scratch_worker_private_output(output, error_output)
        raise primary_error
    if table_count is None or (pgdata / "postmaster.pid").exists():
        raise RuntimeError("Scratch restore server teardown was not confirmed.")
    result_stream = getattr(sys.stdout, "buffer", None)
    args._scratch_fault_site = "worker_result_frame"
    if result_stream is None:
        raise RuntimeError("Scratch restore result channel is unavailable.")
    result_stream.write(
        (json.dumps({"ok": True, "restored_table_count": table_count}, separators=(",", ":")) + "\n").encode("ascii")
    )
    result_stream.flush()
    try:
        args._scratch_fault_site = "worker_done_token"
        completion = _scratch_read_worker_token(
            sys.stdin,
            SCRATCH_DONE_TOKEN,
            require_eof=True,
            deadline=worker_deadline,
        )
    except (OSError, AttributeError) as exc:
        raise RuntimeError("Scratch restore completion was not acknowledged.") from exc
    if completion != SCRATCH_DONE_TOKEN:
        raise RuntimeError("Scratch restore completion was not acknowledged.")
    return 0


def _scratch_create_application_role(admin_target: DatabaseTarget) -> None:
    run_command(
        [
            "psql",
            "--no-psqlrc",
            *connection_args(admin_target),
            "--command",
            f"CREATE ROLE {SCRATCH_APP_ROLE} LOGIN;",
        ],
        target=admin_target,
        capture_output=True,
    )


def _scratch_unit_command(
    *,
    mountpoint: pathlib.Path,
    archive_path: pathlib.Path | None,
    archive_stat: os.stat_result | None,
    archive_sha256: str | None,
    timestamp_slug: str,
    unit_name: str,
    private_scratch_diagnostics: bool = False,
    authorization_probe: bool = False,
) -> list[str]:
    source_script = pathlib.Path(__file__).resolve(strict=True)
    source_policy = source_script.with_name("platform_disk_policy.py").resolve(strict=True)
    python_binary = pathlib.Path(getattr(sys, "_base_executable", sys.executable)).resolve(strict=True)
    archive_target = mountpoint / "archive.dump"
    worker_target = mountpoint / "platform_backup_restore_drill.py"
    policy_target = mountpoint / "platform_disk_policy.py"
    command = [
        "systemd-run",
        "--system",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit_name}",
        "--property=Type=exec",
        f"--property=TemporaryFileSystem={mountpoint}:rw,size={SCRATCH_TMPFS_BYTES},mode=0700",
        f"--property=BindReadOnlyPaths={source_script}:{worker_target}",
        f"--property=BindReadOnlyPaths={source_policy}:{policy_target}",
        f"--property=MemoryMax={SCRATCH_MEMORY_MAX_BYTES}",
        "--property=MemorySwapMax=0",
        "--property=RuntimeMaxSec=900s",
        "--property=TimeoutStopSec=5s",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=kill",
        "--property=PrivateNetwork=yes",
        "--property=PrivateDevices=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=SystemCallArchitectures=native",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=tmpfs",
        "--property=NoNewPrivileges=yes",
        "--property=CapabilityBoundingSet=CAP_CHOWN CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE",
        "--property=InaccessiblePaths=/opt/oldsparky/platform/shared",
        "--property=InaccessiblePaths=/var/lib/postgresql",
        "--property=InaccessiblePaths=/var/run/postgresql",
        "--property=InaccessiblePaths=/run/postgresql",
        "--property=InaccessiblePaths=/etc/postgresql",
        "--property=InaccessiblePaths=/root",
        "--setenv=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--setenv=LANG=C.UTF-8",
        "--setenv=LC_ALL=C.UTF-8",
        "--setenv=SYSTEMD_COLORS=0",
        "--setenv=SYSTEMD_PAGER=cat",
        "--",
        # systemd resolves ExecStart in the manager's host namespace before
        # entering the unit's private mounts. The bounded tmpfs does not yet
        # contain its later read-only binds at that point, so ExecStart must
        # name the already-resolved host interpreter. ProtectSystem=strict
        # keeps that interpreter read-only inside the unit.
        str(python_binary),
        "-I",
        "-B",
        str(worker_target),
        SCRATCH_WORKER_MODE,
        "--_scratch-root",
        str(mountpoint),
        "--_scratch-timestamp-slug",
        timestamp_slug,
    ]
    if authorization_probe:
        if archive_path is not None or archive_stat is not None or archive_sha256 is not None:
            raise ValueError("Authorization probe cannot bind archive inputs.")
        command.append("--_scratch-authorization-probe")
    else:
        if archive_path is None or archive_stat is None or archive_sha256 is None:
            raise ValueError("Scratch restore requires a complete archive identity.")
        command.insert(
            command.index(f"--property=BindReadOnlyPaths={source_script}:{worker_target}"),
            f"--property=BindReadOnlyPaths={archive_path}:{archive_target}",
        )
        command.extend(
            (
                "--_scratch-archive", str(archive_target),
                "--_scratch-archive-dev", str(archive_stat.st_dev),
                "--_scratch-archive-ino", str(archive_stat.st_ino),
                "--_scratch-archive-size", str(archive_stat.st_size),
                "--_scratch-archive-sha256", archive_sha256,
            )
        )
    if private_scratch_diagnostics:
        command.extend(("--json", "--private-scratch-diagnostics"))
    if any("ReadWritePaths=" in part for part in command):
        raise RuntimeError("Scratch unit must not override its private tmpfs mount.")
    return command


def _scratch_drain_early_exit_output(
    selector: selectors.BaseSelector,
    process: subprocess.Popen[bytes],
    stdout_buffer: bytearray,
    stderr_buffer: bytearray,
    stdout_observed: int,
    stderr_observed: int,
) -> tuple[dict[str, bool], int, int]:
    """Drain stopped-launcher pipes to EOF, bounded by time and bytes.

    A single nonblocking read can return ``EAGAIN`` while the writer is still
    making progress. Keep selecting until both writers close, or explicitly
    mark the corresponding prefix incomplete when a hard bound is reached.
    """
    streams = {
        "stdout": (process.stdout, stdout_buffer, SCRATCH_PRIVATE_STDOUT_BYTES),
        "stderr": (process.stderr, stderr_buffer, SCRATCH_PRIVATE_STDERR_BYTES),
    }
    complete: dict[str, bool] = {}
    drained = {name: 0 for name in streams}
    pending: set[str] = set()
    for name, (stream, _prefix, _cap) in streams.items():
        if stream is None:
            complete[name] = True
            continue
        try:
            selector.get_key(stream)
        except KeyError:
            # The normal monitor already observed EOF and unregistered it.
            complete[name] = True
            continue
        complete[name] = False
        pending.add(name)

    deadline = time.monotonic() + SCRATCH_EARLY_EXIT_DRAIN_SECONDS
    while pending:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        for key, _ in selector.select(timeout=min(0.05, remaining)):
            name = key.data
            if name not in pending:
                continue
            stream, prefix, cap = streams[name]
            assert stream is not None
            byte_budget = SCRATCH_EARLY_EXIT_DRAIN_BYTES + 1 - drained[name]
            if byte_budget <= 0:
                selector.unregister(stream)
                pending.remove(name)
                continue
            try:
                data = os.read(stream.fileno(), min(65536, byte_budget))
            except BlockingIOError:
                continue
            except OSError:
                selector.unregister(stream)
                pending.remove(name)
                continue
            if not data:
                selector.unregister(stream)
                complete[name] = True
                pending.remove(name)
                continue
            drained[name] += len(data)
            if name == "stdout":
                stdout_observed += len(data)
            else:
                stderr_observed += len(data)
            if len(prefix) < cap:
                prefix.extend(data[: cap - len(prefix)])
            if drained[name] > SCRATCH_EARLY_EXIT_DRAIN_BYTES:
                selector.unregister(stream)
                pending.remove(name)

    for name in pending:
        stream = streams[name][0]
        if stream is not None:
            selector.unregister(stream)
    return complete, stdout_observed, stderr_observed


def _scratch_drain_phase_prefix_now(
    process: subprocess.Popen[bytes],
    stderr_buffer: bytearray,
    stderr_observed: int,
) -> tuple[int, bool]:
    """Take one immediate, nonblocking, bounded stderr prefix before stopping."""
    stream = process.stderr
    if stream is None:
        return stderr_observed, False
    drained = 0
    eof = False
    while drained <= SCRATCH_PRIVATE_STDERR_BYTES:
        remaining_total = 65536 - stderr_observed
        budget = min(SCRATCH_PRIVATE_STDERR_BYTES + 1 - drained, remaining_total)
        if budget <= 0:
            break
        try:
            block = os.read(stream.fileno(), min(4096, budget))
        except BlockingIOError:
            break
        except OSError:
            break
        if not block:
            eof = True
            break
        drained += len(block)
        stderr_observed += len(block)
        if len(stderr_buffer) < SCRATCH_PRIVATE_STDERR_BYTES:
            stderr_buffer.extend(block[: SCRATCH_PRIVATE_STDERR_BYTES - len(stderr_buffer)])
        if drained > SCRATCH_PRIVATE_STDERR_BYTES:
            break
    return stderr_observed, eof


def _scratch_control_group_snapshot(control_group: str) -> dict[str, int]:
    if not control_group.startswith("/system.slice/") or ".." in pathlib.PurePosixPath(control_group).parts:
        raise RuntimeError("Scratch restore service cgroup is outside system.slice.")
    root = pathlib.Path("/sys/fs/cgroup").resolve(strict=True)
    directory = (root / control_group.lstrip("/")).resolve(strict=True)
    directory.relative_to(root)

    def read(name: str) -> str:
        return (directory / name).read_text(encoding="ascii").strip()

    values = {
        "memory_max_bytes": int(read("memory.max")),
        "memory_current_bytes": int(read("memory.current")),
        "memory_swap_max_bytes": int(read("memory.swap.max")),
        "memory_swap_current_bytes": int(read("memory.swap.current")),
        "memory_oom_group": int(read("memory.oom.group")),
    }
    events = {}
    for row in read("memory.events").splitlines():
        fields = row.split()
        if len(fields) == 2 and fields[0] in {"oom", "oom_kill", "high", "max"} and fields[1].isdecimal():
            events[fields[0]] = int(fields[1])
    values["oom_kill_events"] = events.get("oom_kill", 0)
    if (
        values["memory_max_bytes"] != SCRATCH_MEMORY_MAX_BYTES
        or values["memory_swap_max_bytes"] != 0
        or values["memory_swap_current_bytes"] != 0
        or values["memory_oom_group"] != 1
        or values["memory_current_bytes"] >= SCRATCH_MEMORY_MAX_BYTES - SCRATCH_MEMORY_STOP_MARGIN_BYTES
        or values["oom_kill_events"] != 0
    ):
        raise RuntimeError("Scratch restore service exceeded its effective memory bounds.")
    return values


def _scratch_service_snapshot(unit_name: str) -> dict[str, Any]:
    keys = (
        "LoadState",
        "ActiveState",
        "SubState",
        "ControlGroup",
        "ExecMainPID",
        "InvocationID",
        "ExecMainStatus",
        "Result",
        "TemporaryFileSystem",
        "BindReadOnlyPaths",
        "InaccessiblePaths",
        "RestrictAddressFamilies",
        "MemoryMax",
        "MemorySwapMax",
        "OOMPolicy",
        "PrivateNetwork",
        "ProtectSystem",
        "ProtectHome",
        "NoNewPrivileges",
        "ReadWritePaths",
    )
    completed = subprocess.run(
        ["systemctl", "show", "--no-pager", *[f"--property={key}" for key in keys], unit_name],
        check=False,
        text=True,
        capture_output=True,
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_PAGER": "cat",
        },
        timeout=2,
    )
    if completed.returncode != 0 or completed.stderr:
        raise RuntimeError("Scratch restore service properties could not be verified.")
    properties = dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )
    return {"properties": properties, "load_state": properties.get("LoadState")}


def _scratch_process_start_time(pid_text: str) -> str:
    """Read the kernel start-time field to distinguish PID reuse during authorization."""
    if not pid_text.isdecimal() or len(pid_text) > 10:
        raise RuntimeError("Scratch restore worker process identity is unavailable.")
    with pathlib.Path(f"/proc/{pid_text}/stat").open("rb") as source:
        data = source.read(8193)
    if len(data) > 8192:
        raise RuntimeError("Scratch restore worker process identity is malformed.")
    closing = data.rfind(b")")
    if closing < 0:
        raise RuntimeError("Scratch restore worker process identity is malformed.")
    fields = data[closing + 1 :].split()
    # The tail begins at stat field 3 (state); starttime is field 22.
    # /proc/<pid>/stat is opened in binary mode, so validate its ASCII decimal
    # field as bytes before decoding it.  bytes.isdigit() accepts only 0-9.
    if len(fields) <= 19 or not fields[19].isdigit():
        raise RuntimeError("Scratch restore worker process identity is malformed.")
    return fields[19].decode("ascii")


class _ScratchTokenDeliveryError(RuntimeError):
    """A failed token write with a truthful complete-pipe-write marker."""

    def __init__(self, message: str, *, complete: bool) -> None:
        super().__init__(message)
        self.complete = complete


def _scratch_send_worker_token(
    process: subprocess.Popen[bytes], *, token: bytes, deadline: float, close_after: bool
) -> None:
    """Write one fixed protocol token through a bounded nonblocking pipe."""
    stream = process.stdin
    if stream is None:
        raise _ScratchTokenDeliveryError(
            "Scratch restore authorization channel is unavailable.", complete=False
        )
    fd = stream.fileno()
    os.set_blocking(fd, False)
    remaining = memoryview(token)
    write_error: BaseException | None = None
    try:
        with selectors.DefaultSelector() as authorization_selector:
            authorization_selector.register(stream, selectors.EVENT_WRITE)
            while remaining:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise _ScratchTokenDeliveryError(
                        "Scratch restore authorization did not complete before its startup deadline.",
                        complete=False,
                    )
                ready = authorization_selector.select(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                if not ready:
                    continue
                try:
                    written = os.write(fd, remaining)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise _ScratchTokenDeliveryError(
                        "Scratch restore authorization channel closed before acknowledgement.",
                        complete=False,
                    )
                remaining = remaining[written:]
                if not remaining:
                    completed_at = time.monotonic()
                    if completed_at >= deadline:
                        raise _ScratchTokenDeliveryError(
                            "Scratch restore authorization completed after its startup deadline.",
                            complete=True,
                        )
    except BaseException as exc:
        write_error = exc
        if isinstance(exc, _ScratchTokenDeliveryError):
            raise
        raise _ScratchTokenDeliveryError(
            "Scratch restore authorization channel failed before a complete write.",
            complete=not remaining,
        ) from exc
    finally:
        if close_after and not stream.closed:
            try:
                stream.close()
            except OSError:
                if write_error is None:
                    raise _ScratchTokenDeliveryError(
                        "Scratch restore authorization channel could not be closed.",
                        complete=not remaining,
                    )


def _scratch_worker_candidate(stdout: bytes) -> dict[str, Any] | None:
    """Parse only one complete, bounded worker result frame before DONE."""
    if not stdout or len(stdout) > 4096:
        return None
    if not stdout.endswith(b"\n"):
        return None
    payload = stdout[:-1]
    if not payload or b"\n" in payload or b"\r" in payload:
        raise RuntimeError("Scratch restore worker result framing is invalid.")
    try:
        decoded = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Scratch restore worker returned an invalid result candidate.") from exc
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"ok", "restored_table_count"}
        or decoded.get("ok") is not True
        or type(decoded.get("restored_table_count")) is not int
        or decoded["restored_table_count"] <= 0
    ):
        raise RuntimeError("Scratch restore worker result candidate failed validation.")
    return decoded


def _scratch_authorization_probe_candidate(stdout: bytes) -> dict[str, Any] | None:
    """Accept only the probe-only result frame, never a restore receipt."""
    if stdout != SCRATCH_AUTHORIZATION_PROBE_FRAME:
        return None
    try:
        candidate = json.loads(stdout.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if candidate != {
        "kind": "authorization_probe",
        "schema": 1,
        "status": "completed",
    }:
        return None
    return candidate


def _scratch_expected_properties(
    snapshot: dict[str, Any],
    *,
    unit_name: str,
    mountpoint: pathlib.Path,
    python_binary: pathlib.Path,
    authorization_probe: bool = False,
) -> str:
    properties = snapshot.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Scratch restore service properties are malformed.")
    expected = {
        "MemoryMax": str(SCRATCH_MEMORY_MAX_BYTES),
        "MemorySwapMax": "0",
        "OOMPolicy": "kill",
        "PrivateNetwork": "yes",
        "RestrictAddressFamilies": "AF_UNIX",
        "ProtectSystem": "strict",
        "ProtectHome": "tmpfs",
        "NoNewPrivileges": "yes",
    }
    if any(properties.get(key, "").lower() != value.lower() for key, value in expected.items()):
        raise RuntimeError("Scratch restore service properties do not match the closed policy.")
    if properties.get("ReadWritePaths") not in {None, ""}:
        raise RuntimeError("Scratch restore service has an unexpected writable-path override.")
    if properties.get("LoadState") != "loaded" or properties.get("ActiveState") != "active":
        raise RuntimeError("Scratch restore service is not active while it is being checked.")
    control_group = properties.get("ControlGroup", "")
    if not control_group.startswith("/system.slice/") or ".." in pathlib.PurePosixPath(control_group).parts:
        raise RuntimeError("Scratch restore service cgroup is invalid.")
    if control_group != f"/system.slice/{unit_name}":
        raise RuntimeError("Scratch restore service cgroup does not match its unique unit.")
    pid_text = properties.get("ExecMainPID", "0")
    if pid_text in {"", "0"} or not pid_text.isdecimal():
        raise RuntimeError("Scratch restore worker process identity is unavailable.")
    try:
        process_cgroup = None
        for row in pathlib.Path(f"/proc/{pid_text}/cgroup").read_text(encoding="ascii").splitlines():
            fields = row.split(":", 2)
            if len(fields) == 3 and fields[0] == "0":
                process_cgroup = fields[2]
                break
    except OSError as exc:
        raise RuntimeError("Scratch restore worker process identity disappeared during verification.") from exc
    if process_cgroup != control_group:
        raise RuntimeError("Scratch restore worker is outside its reviewed systemd cgroup.")
    try:
        expected_python = python_binary.stat()
        running_python = pathlib.Path(f"/proc/{pid_text}/exe").stat()
    except OSError as exc:
        raise RuntimeError("Scratch restore interpreter identity disappeared during verification.") from exc
    if (running_python.st_dev, running_python.st_ino) != (
        expected_python.st_dev,
        expected_python.st_ino,
    ):
        raise RuntimeError("Scratch restore interpreter does not match the pinned host executable.")
    tmpfs = properties.get("TemporaryFileSystem", "")
    if str(mountpoint) not in tmpfs:
        raise RuntimeError("Scratch restore tmpfs request is not present in the active unit.")
    inaccessible = properties.get("InaccessiblePaths", "")
    for required in (
        "/opt/oldsparky/platform/shared",
        "/var/lib/postgresql",
        "/var/run/postgresql",
        "/run/postgresql",
        "/etc/postgresql",
        "/root",
    ):
        if required not in inaccessible:
            raise RuntimeError("Scratch restore service can see an out-of-scope credential or cluster path.")
    bind_paths = properties.get("BindReadOnlyPaths", "")
    required_binds = [
        str(mountpoint / "platform_backup_restore_drill.py"),
        str(mountpoint / "platform_disk_policy.py"),
    ]
    if not authorization_probe:
        required_binds.insert(0, str(mountpoint / "archive.dump"))
    for required in required_binds:
        if required not in bind_paths:
            raise RuntimeError("Scratch restore service is missing a required read-only input bind.")
    if authorization_probe and str(mountpoint / "archive.dump") in bind_paths:
        raise RuntimeError("Scratch authorization probe has an unexpected archive bind.")
    return control_group


def _scratch_host_guard(guard_site: str = "parent_before_archive_open") -> None:
    require_restore_headroom(pathlib.Path("/"))
    _raise_scratch_host_memory_reserve(
        guard_site,
        message="Scratch restore admission memory reserve is unavailable.",
        retain_legacy_admission=guard_site == "parent_before_archive_open",
    )


def _scratch_mountpoint_is_mounted(path: pathlib.Path) -> bool:
    target = str(path)
    for line in pathlib.Path("/proc/self/mountinfo").read_text(encoding="ascii").splitlines():
        left, separator, _ = line.partition(" - ")
        fields = left.split()
        if separator and len(fields) >= 5 and fields[4].replace("\\040", " ") == target:
            return True
    return False


def _scratch_stop_unit(unit_name: str) -> None:
    try:
        subprocess.run(
            ["systemctl", "stop", unit_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "SYSTEMD_PAGER": "cat"},
            timeout=6,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Scratch restore service could not be confirmed stopped.") from exc


def _run_isolated_restore_drill(
    dump_path: pathlib.Path | None,
    *,
    timestamp_slug: str,
    private_scratch_diagnostics: bool = False,
    authorization_probe: bool = False,
) -> int:
    """Run a restore drill or the distinct no-archive authorization protocol."""
    fault_site = "controller_entry"
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Isolated backup verification requires the root maintenance owner.")
        if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", timestamp_slug) is None:
            raise ValueError("Scratch restore timestamp identity is invalid.")
        fault_site = "controller_entry"
        if authorization_probe:
            if dump_path is not None:
                raise ValueError("Authorization probe must not receive an archive path.")
            require_commands("systemd-run", "systemctl")
        else:
            if dump_path is None:
                raise ValueError("Scratch restore requires an archive path.")
            require_commands("systemd-run", "systemctl", "pg_config", "runuser")
        fault_site = "controller_host_guard"
        if authorization_probe:
            _scratch_host_guard("parent_before_authorization_probe")
        else:
            _scratch_host_guard("parent_before_archive_open")
    except BaseException as exc:
        if authorization_probe:
            setattr(exc, "scratch_authorization_probe", True)
        _retain_first_scratch_fault(exc, origin="controller", site=fault_site)
        raise
    archive_path: pathlib.Path | None = None
    archive_descriptor: int | None = None
    archive_before: os.stat_result | None = None
    archive_sha256: str | None = None
    if not authorization_probe:
        assert dump_path is not None
        fault_site = "controller_archive_identity"
        archive_path = dump_path.absolute()
    try:
        if not authorization_probe:
            assert archive_path is not None
            archive_descriptor = os.open(
                archive_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            archive_before = os.fstat(archive_descriptor)
            path_before = archive_path.lstat()
            if (
                not stat.S_ISREG(archive_before.st_mode)
                or archive_before.st_uid != 0
                or stat.S_IMODE(archive_before.st_mode) != 0o600
                or archive_before.st_nlink != 1
                or (archive_before.st_dev, archive_before.st_ino)
                != (path_before.st_dev, path_before.st_ino)
                or archive_before.st_size <= 5
            ):
                raise RuntimeError("Scratch restore archive identity is unsafe.")
            archive_hash = hashlib.sha256()
            total_bytes = 0
            while True:
                chunk = os.read(archive_descriptor, 1024 * 1024)
                if not chunk:
                    break
                archive_hash.update(chunk)
                total_bytes += len(chunk)
            archive_after = os.fstat(archive_descriptor)
            if (
                total_bytes != archive_before.st_size
                or os.pread(archive_descriptor, 5, 0) != b"PGDMP"
                or (archive_before.st_dev, archive_before.st_ino, archive_before.st_size,
                    archive_before.st_mtime_ns, archive_before.st_ctime_ns)
                != (archive_after.st_dev, archive_after.st_ino, archive_after.st_size,
                    archive_after.st_mtime_ns, archive_after.st_ctime_ns)
            ):
                raise RuntimeError("Scratch restore archive content or identity is invalid.")
            archive_sha256 = archive_hash.hexdigest()
            os.lseek(archive_descriptor, 0, os.SEEK_SET)
        host_run_stat = pathlib.Path("/run").stat()
        if not stat.S_ISDIR(host_run_stat.st_mode) or host_run_stat.st_uid != 0:
            raise RuntimeError("Scratch restore mount parent is not the trusted root-owned /run directory.")
        token = secrets.token_hex(12)
        mountpoint = pathlib.Path(f"/run/oldsparky-backup-restore-{token}")
        unit_name = f"oldsparky-backup-restore-{token}.service"
        fault_site = "controller_unit_setup"
        command = _scratch_unit_command(
            mountpoint=mountpoint,
            archive_path=archive_path,
            archive_stat=archive_before,
            archive_sha256=archive_sha256,
            timestamp_slug=timestamp_slug,
            unit_name=unit_name,
            private_scratch_diagnostics=private_scratch_diagnostics,
            authorization_probe=authorization_probe,
        )
        environment = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_PAGER": "cat",
        }
        process: subprocess.Popen[bytes] | None = None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_observed = 0
        stderr_size = 0
        early_exit_snapshot: dict[str, Any] | None = None
        early_exit_phase = "unit_exited_before_confinement"
        verification_error = "none"
        verification_errno: int | None = None
        active_control_group: str | None = None
        active_snapshot: dict[str, Any] | None = None
        active_identity: tuple[str, str, str] | None = None
        start_token_sent = False
        completion_token_sent = False
        candidate_output: bytes | None = None
        identity_checks_passed = False
        terminal_snapshot: dict[str, Any] | None = None
        selector: selectors.BaseSelector | None = None
        stderr_eof = False
        launch_started = False
        launch_monotonic_ns: int | None = None
        last_tick = time.monotonic()
        deadline = last_tick + SCRATCH_RUNTIME_SECONDS + 30
        worker_result: dict[str, Any] | None = None
        phase_prefix: bytes | None = None
        phase_prefix_observed: int | None = None
        phase_guard_monotonic_ns: int | None = None
        phase_drain_monotonic_ns: int | None = None
        blocked_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
        setup_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
        try:
            os.mkdir(mountpoint, 0o700)
            mountpoint_stat = mountpoint.lstat()
            mountpoint_identity = (mountpoint_stat.st_dev, mountpoint_stat.st_ino)
            if (
                not stat.S_ISDIR(mountpoint_stat.st_mode)
                or mountpoint_stat.st_uid != 0
                or mountpoint_stat.st_gid != 0
                or stat.S_IMODE(mountpoint_stat.st_mode) != 0o700
                or _scratch_mountpoint_is_mounted(mountpoint)
            ):
                mountpoint.rmdir()
                raise RuntimeError("Scratch restore mountpoint identity is unsafe.")
        except BaseException:
            signal.pthread_sigmask(signal.SIG_SETMASK, setup_signal_mask)
            raise
        try:
            # A pending TERM/INT is delivered only after the exact host
            # mountpoint identity is ready for the normal cleanup path.
            signal.pthread_sigmask(signal.SIG_SETMASK, setup_signal_mask)
            fault_site = "controller_host_guard"
            _scratch_host_guard("parent_before_unit_launch")
            # Do not let a termination signal land after the client process
            # starts but before its handle and unit-cleanup state are recorded.
            previous_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
            try:
                # Treat the unit as potentially started once its launcher is
                # invoked, even if Popen itself raises after systemd accepted
                # the transient unit request.
                launch_started = True
                fault_site = "controller_launch"
                startup_deadline = time.monotonic() + SCRATCH_STARTUP_SECONDS
                launch_monotonic_ns = time.monotonic_ns()
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    cwd="/",
                    close_fds=True,
                    bufsize=0,
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_signal_mask)
            selector = selectors.DefaultSelector()
            assert process.stdout is not None and process.stderr is not None
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while process.poll() is None and not completion_token_sent:
                now = time.monotonic()
                if now >= deadline or now - last_tick > SCRATCH_RUNTIME_SAMPLE_SECONDS * 8:
                    raise RuntimeError("Scratch restore monitor exceeded its bounded runtime or sample interval.")
                fault_site = "controller_active_poll"
                try:
                    _scratch_host_guard("parent_active_poll")
                except BaseException as guard_error:
                    guard_diagnostic = getattr(
                        guard_error, "scratch_host_guard_diagnostic", None
                    )
                    if (
                        private_scratch_diagnostics
                        and valid_scratch_host_guard_diagnostic(guard_diagnostic)
                        and guard_diagnostic.get("guard_site") == "parent_active_poll"
                    ):
                        # Freeze only bytes already available at this stop boundary.
                        # This read is nonblocking and completes before _scratch_stop_unit.
                        phase_guard_monotonic_ns = time.monotonic_ns()
                        try:
                            if process is not None:
                                stderr_size, _phase_prefix_eof = _scratch_drain_phase_prefix_now(
                                    process, stderr_buffer, stderr_size
                                )
                                phase_prefix = bytes(stderr_buffer)
                                phase_prefix_observed = stderr_size
                        except BaseException:
                            phase_prefix = None
                            phase_prefix_observed = None
                        phase_drain_monotonic_ns = time.monotonic_ns()
                    if (
                        private_scratch_diagnostics
                        and valid_scratch_host_guard_diagnostic(guard_diagnostic)
                        and guard_diagnostic.get("guard_site") == "parent_active_poll"
                    ):
                        try:
                            attribution = _scratch_private_resource_attribution(
                                snapshot=active_snapshot,
                                worker_identity=active_identity,
                                control_group=active_control_group,
                                launch_monotonic_ns=launch_monotonic_ns,
                            )
                        except BaseException:
                            attribution = _scratch_unknown_resource_attribution("internal_error")
                        try:
                            if valid_scratch_resource_attribution(attribution):
                                setattr(guard_error, "scratch_resource_attribution", attribution)
                        except BaseException:
                            # Optional evidence must never replace the reserve guard failure.
                            pass
                    raise
                fault_site = "controller_unit_snapshot"
                snapshot = _scratch_service_snapshot(unit_name)
                properties = snapshot.get("properties", {})
                active_state = properties.get("ActiveState")
                load_state = properties.get("LoadState")
                result_state = properties.get("Result")
                pid_text = properties.get("ExecMainPID", "0")
                control_group = properties.get("ControlGroup", "")
                if active_state == "failed" or result_state not in {None, "", "success"}:
                    early_exit_snapshot = snapshot
                    if active_control_group is not None:
                        early_exit_phase = "unit_exited_after_confinement"
                        raise RuntimeError(
                            "Scratch restore service failed after its confinement was verified."
                        )
                    raise RuntimeError(
                        "Scratch restore service failed before its confinement was verified."
                    )
                if active_state == "active" and pid_text not in {"", "0"} and control_group:
                    # Preserve the exact live snapshot before any identity or
                    # cgroup verification can raise. A disappearing /proc PID
                    # is still a failed verification, but its bounded worker
                    # output must reach the existing private diagnostic path.
                    early_exit_snapshot = snapshot
                    try:
                        fault_site = "controller_identity"
                        verified_control_group = _scratch_expected_properties(
                            snapshot,
                            unit_name=unit_name,
                            mountpoint=mountpoint,
                            python_binary=pathlib.Path(
                                getattr(sys, "_base_executable", sys.executable)
                            ).resolve(strict=True),
                            authorization_probe=authorization_probe,
                        )
                        _scratch_control_group_snapshot(verified_control_group)
                        invocation_id = properties.get("InvocationID", "")
                        if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
                            raise RuntimeError("Scratch restore unit invocation identity is unavailable.")
                        process_start_time = _scratch_process_start_time(pid_text)
                        observed_identity = (pid_text, invocation_id, process_start_time)
                        if active_identity is not None and observed_identity != active_identity:
                            raise RuntimeError("Scratch restore worker identity changed during verification.")
                        if active_identity is None:
                            if time.monotonic() >= startup_deadline or process.poll() is not None:
                                raise RuntimeError("Scratch restore worker exited before authorization.")
                            fault_site = "controller_identity"
                            second_snapshot = _scratch_service_snapshot(unit_name)
                            second_properties = second_snapshot.get("properties", {})
                            if (
                                second_properties.get("LoadState") != "loaded"
                                or second_properties.get("ActiveState") != "active"
                                or second_properties.get("ExecMainPID") != pid_text
                                or second_properties.get("InvocationID") != invocation_id
                            ):
                                raise RuntimeError("Scratch restore unit identity changed before authorization.")
                            second_control_group = _scratch_expected_properties(
                                second_snapshot,
                                unit_name=unit_name,
                                mountpoint=mountpoint,
                                python_binary=pathlib.Path(
                                    getattr(sys, "_base_executable", sys.executable)
                                ).resolve(strict=True),
                                authorization_probe=authorization_probe,
                            )
                            _scratch_control_group_snapshot(second_control_group)
                            if (
                                second_control_group != verified_control_group
                                or _scratch_process_start_time(pid_text) != process_start_time
                                or time.monotonic() >= startup_deadline
                                or process.poll() is not None
                            ):
                                raise RuntimeError("Scratch restore worker identity changed before authorization.")
                            identity_checks_passed = True
                            active_identity = observed_identity
                            active_control_group = verified_control_group
                            active_snapshot = second_snapshot
                            try:
                                fault_site = "controller_start_token"
                                _scratch_send_worker_token(
                                    process,
                                    token=SCRATCH_START_TOKEN,
                                    deadline=startup_deadline,
                                    close_after=False,
                                )
                            except _ScratchTokenDeliveryError as delivery_error:
                                start_token_sent = delivery_error.complete
                                raise
                            start_token_sent = True
                        else:
                            identity_checks_passed = True
                    except BaseException as verification_exception:
                        if identity_checks_passed and not completion_token_sent:
                            early_exit_phase = "authorization_failed"
                        elif active_control_group is not None:
                            early_exit_phase = "unit_exited_after_confinement"
                        else:
                            early_exit_phase = "confinement_verification_failed"
                        verification_error, verification_errno = (
                            _scratch_verification_error_details(verification_exception)
                        )
                        raise
                    early_exit_snapshot = None
                elif (
                    active_state in {"inactive", "activating", "active"}
                    and load_state in {"loaded", "not-found"}
                    and (
                        active_state == "activating"
                        or (pid_text in {"", "0"} and not control_group)
                    )
                ):
                    # systemd can briefly report the not-yet-created transient
                    # unit as inactive after systemd-run has been launched. The
                    # launcher is still making the request; poll through this
                    # bounded startup window rather than treating absence as a
                    # terminal unit failure.
                    if process.poll() is not None or now >= startup_deadline:
                        early_exit_snapshot = snapshot
                        raise RuntimeError(
                            "Scratch restore service did not become active with verified confinement before its startup deadline."
                        )
                else:
                    early_exit_snapshot = snapshot
                    raise RuntimeError("Scratch restore service state cannot prove confinement startup.")
                last_tick = now
                for key, _ in selector.select(timeout=SCRATCH_RUNTIME_SAMPLE_SECONDS):
                    try:
                        data = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        if key.data == "stderr":
                            stderr_eof = True
                    elif key.data == "stdout":
                        stdout_observed += len(data)
                        stdout_buffer.extend(data)
                        if len(stdout_buffer) > 4096:
                            raise RuntimeError("Scratch restore worker output exceeded its closed bound.")
                    else:
                        stderr_size += len(data)
                        if len(stderr_buffer) < SCRATCH_PRIVATE_STDERR_BYTES:
                            stderr_buffer.extend(data[: SCRATCH_PRIVATE_STDERR_BYTES - len(stderr_buffer)])
                        if stderr_size > 65536:
                            raise RuntimeError("Scratch restore worker error output exceeded its closed bound.")
                if start_token_sent and not completion_token_sent and stdout_buffer:
                    fault_site = "controller_worker_frame"
                    candidate = (
                        _scratch_authorization_probe_candidate(bytes(stdout_buffer))
                        if authorization_probe
                        else _scratch_worker_candidate(bytes(stdout_buffer))
                    )
                    if candidate is not None:
                        if active_identity is None or active_control_group is None:
                            raise RuntimeError("Scratch restore worker identity was not retained through completion.")
                        fault_site = "controller_completion_snapshot"
                        final_snapshot = _scratch_service_snapshot(unit_name)
                        final_properties = final_snapshot.get("properties", {})
                        final_pid = final_properties.get("ExecMainPID", "0")
                        final_invocation = final_properties.get("InvocationID", "")
                        if (
                            final_properties.get("LoadState") != "loaded"
                            or final_properties.get("ActiveState") != "active"
                            or (final_pid, final_invocation) != active_identity[:2]
                            or time.monotonic() >= deadline
                        ):
                            raise RuntimeError("Scratch restore worker identity changed before completion acknowledgement.")
                        final_control_group = _scratch_expected_properties(
                            final_snapshot,
                            unit_name=unit_name,
                            mountpoint=mountpoint,
                            python_binary=pathlib.Path(
                                getattr(sys, "_base_executable", sys.executable)
                            ).resolve(strict=True),
                            authorization_probe=authorization_probe,
                        )
                        _scratch_control_group_snapshot(final_control_group)
                        if (
                            final_control_group != active_control_group
                            or _scratch_process_start_time(final_pid) != active_identity[2]
                        ):
                            raise RuntimeError("Scratch restore worker identity changed before completion acknowledgement.")
                        candidate_output = bytes(stdout_buffer)
                        worker_result = candidate
                        early_exit_snapshot = final_snapshot
                        early_exit_phase = "authorization_failed"
                        verification_error = "other"
                        try:
                            fault_site = "controller_done_token"
                            _scratch_send_worker_token(
                                process,
                                token=SCRATCH_DONE_TOKEN,
                                deadline=deadline,
                                close_after=True,
                            )
                        except _ScratchTokenDeliveryError as delivery_error:
                            completion_token_sent = delivery_error.complete
                            raise
                        completion_token_sent = True
                        # DONE releases the already-verified worker to exit;
                        # do not race that intentional exit with another /proc probe.
                        break
            if active_control_group is None:
                # A very short-lived launcher may finish before the monitor
                # observes an active unit. Never accept its worker result
                # without having validated the live service confinement.
                early_exit_snapshot = _scratch_service_snapshot(unit_name)
                raise RuntimeError(
                    "Scratch restore service exited before its confinement was verified."
                )
            if not completion_token_sent or candidate_output is None or bytes(stdout_buffer) != candidate_output:
                early_exit_snapshot = _scratch_service_snapshot(unit_name)
                early_exit_phase = "unit_exited_after_confinement"
                raise RuntimeError("Scratch restore worker completion was not fully authorized and verified.")
            fault_site = "controller_final_snapshot"
            return_code = process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream:
                    while True:
                        try:
                            data = os.read(stream.fileno(), 65536)
                        except BlockingIOError:
                            break
                        if not data:
                            if stream is process.stderr:
                                stderr_eof = True
                            break
                        if stream is process.stdout:
                            stdout_observed += len(data)
                            stdout_buffer.extend(data)
                            if len(stdout_buffer) > 4096:
                                raise RuntimeError("Scratch restore worker output exceeded its closed bound.")
                        else:
                            stderr_size += len(data)
                            if len(stderr_buffer) < SCRATCH_PRIVATE_STDERR_BYTES:
                                stderr_buffer.extend(data[: SCRATCH_PRIVATE_STDERR_BYTES - len(stderr_buffer)])
                        if stderr_size > 65536:
                            raise RuntimeError("Scratch restore worker error output exceeded its closed bound.")
            stderr_accepted = _scratch_success_stderr_accepted(
                bytes(stderr_buffer),
                observed_bytes=stderr_size,
                stderr_eof=stderr_eof,
                private_diagnostics=private_scratch_diagnostics,
                authorization_probe=authorization_probe,
                launch_monotonic_ns=launch_monotonic_ns,
            )
            if return_code != 0 or not stderr_accepted:
                try:
                    final_snapshot = _scratch_service_snapshot(unit_name)
                except BaseException as snapshot_error:
                    if active_snapshot is not None:
                        early_exit_snapshot = active_snapshot
                        early_exit_phase = "confinement_verification_failed"
                    verification_error, verification_errno = (
                        _scratch_verification_error_details(snapshot_error)
                    )
                    raise RuntimeError(
                        "Scratch restore final service state could not be verified."
                    ) from snapshot_error
                early_exit_snapshot = final_snapshot
                early_exit_phase = "unit_exited_after_confinement"
                raise RuntimeError("Isolated PostgreSQL backup restore did not complete successfully.")
            if authorization_probe:
                decoded = _scratch_authorization_probe_candidate(bytes(stdout_buffer))
                if decoded is None:
                    early_exit_snapshot = _scratch_service_snapshot(unit_name)
                    early_exit_phase = "unit_exited_after_confinement"
                    raise RuntimeError("Scratch authorization probe result failed validation.")
            else:
                try:
                    decoded = json.loads(stdout_buffer.decode("ascii"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    early_exit_snapshot = _scratch_service_snapshot(unit_name)
                    early_exit_phase = "unit_exited_after_confinement"
                    raise RuntimeError("Isolated PostgreSQL backup restore returned an invalid result.") from exc
                if (
                    not isinstance(decoded, dict)
                    or set(decoded) != {"ok", "restored_table_count"}
                    or decoded.get("ok") is not True
                    or type(decoded.get("restored_table_count")) is not int
                    or decoded["restored_table_count"] <= 0
                ):
                    early_exit_snapshot = _scratch_service_snapshot(unit_name)
                    early_exit_phase = "unit_exited_after_confinement"
                    raise RuntimeError("Isolated PostgreSQL backup restore result failed validation.")
            if decoded != worker_result:
                early_exit_snapshot = _scratch_service_snapshot(unit_name)
                early_exit_phase = "unit_exited_after_confinement"
                raise RuntimeError("Scratch restore worker result changed after completion authorization.")
            worker_result = decoded
        except BaseException as operation_error:
            # Record the first controller-side failure before any teardown can
            # raise or replace it. A separately validated worker frame, if
            # present, is more precise about the original child failure.
            controller_first_fault = _scratch_first_fault(
                operation_error, origin="controller", site=fault_site
            )
            if start_token_sent and not completion_token_sent and active_snapshot is not None:
                # The unit is still held at the worker barrier. Preserve the
                # live proof and classify this as an authorization-stage failure.
                early_exit_snapshot = active_snapshot
                early_exit_phase = "authorization_failed"
                if verification_error == "none":
                    verification_error = "other"
            # Cleanup must not be interrupted by a second TERM/INT. The unit
            # itself also remains bounded by its systemd RuntimeMaxSec.
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            cleanup_errors: list[BaseException] = []
            if launch_started:
                try:
                    terminal_snapshot = _scratch_terminal_snapshot(
                        _scratch_service_snapshot(unit_name),
                        start_token_sent=start_token_sent,
                        completion_token_sent=completion_token_sent,
                    )
                except BaseException:
                    terminal_snapshot = None
            authorization_stream = getattr(process, "stdin", None) if process is not None else None
            if authorization_stream is not None and not authorization_stream.closed:
                try:
                    authorization_stream.close()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            try:
                if launch_started:
                    _scratch_stop_unit(unit_name)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            finally:
                # A failed systemd stop must not skip reaping the exact
                # launcher process. The final unit-state check below still
                # fails closed if stopping the service was not confirmed.
                if process is not None:
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                            process.wait(timeout=5)
                        except BaseException as cleanup_error:
                            cleanup_errors.append(cleanup_error)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
            if early_exit_snapshot is not None:
                fault_site = "controller_cleanup"
                try:
                    assert process is not None and selector is not None
                    drain_complete, stdout_observed, stderr_size = (
                        _scratch_drain_early_exit_output(
                            selector,
                            process,
                            stdout_buffer,
                            stderr_buffer,
                            stdout_observed,
                            stderr_size,
                        )
                    )
                    stdout_prefix = bytes(stdout_buffer[:SCRATCH_PRIVATE_STDOUT_BYTES])
                    stderr_prefix = bytes(stderr_buffer[:SCRATCH_PRIVATE_STDERR_BYTES])
                    if private_scratch_diagnostics:
                        bound_diagnostic = _parse_bound_scratch_host_guard_diagnostic(
                            stdout_prefix,
                            stderr_prefix,
                            stdout_truncated=(
                                stdout_observed > len(stdout_prefix)
                                or not drain_complete["stdout"]
                            ),
                            stderr_truncated=(
                                stderr_size > len(stderr_prefix)
                                or not drain_complete["stderr"]
                            ),
                        )
                        if bound_diagnostic is not None:
                            setattr(
                                operation_error,
                                "scratch_host_guard_diagnostic",
                                bound_diagnostic,
                            )
                    first_fault = _parse_bound_scratch_first_fault(
                        stdout_prefix,
                        stderr_prefix,
                        stdout_truncated=(
                            stdout_observed > len(stdout_prefix)
                            or not drain_complete["stdout"]
                        ),
                        stderr_truncated=(
                            stderr_size > len(stderr_prefix)
                            or not drain_complete["stderr"]
                        ),
                    )
                    if first_fault is not None:
                        setattr(operation_error, "scratch_first_fault", first_fault)
                    diagnostic = _scratch_unit_diagnostic(
                        early_exit_snapshot,
                        launcher_exit_status=process.returncode if process is not None else None,
                        stdout_prefix=stdout_prefix,
                        stdout_observed=stdout_observed,
                        stdout_complete=drain_complete["stdout"],
                        stderr_prefix=stderr_prefix,
                        stderr_observed=stderr_size,
                        stderr_complete=drain_complete["stderr"],
                        phase=early_exit_phase,
                        verification_error=verification_error,
                        verification_errno=verification_errno,
                        terminal_snapshot=terminal_snapshot,
                    )
                    if diagnostic is not None:
                        setattr(operation_error, "scratch_unit_diagnostic", diagnostic)
                    # The transient unit may already have been collected when its
                    # final properties are queried. In that case the closed
                    # systemd summary is intentionally omitted, but bounded worker
                    # output still remains useful private failure evidence.
                    if diagnostic is not None or private_scratch_diagnostics:
                        setattr(
                            operation_error,
                            "scratch_unit_private_output",
                            (stdout_prefix, stderr_prefix),
                        )
                except BaseException:
                    # Failure-only projection is secondary evidence. A broken
                    # drain/parser must not replace the first operation error;
                    # the controller projection captured above remains usable.
                    pass
            if private_scratch_diagnostics and phase_guard_monotonic_ns is not None:
                try:
                    phase_observation = _scratch_parse_phase_attribution(
                        phase_prefix if phase_prefix is not None else b"",
                        truncated=(
                            phase_prefix is not None
                            and phase_prefix_observed is not None
                            and phase_prefix_observed > len(phase_prefix)
                        ),
                        complete=False,
                        launch_monotonic_ns=launch_monotonic_ns,
                        controller_guard_monotonic_ns=phase_guard_monotonic_ns,
                        controller_drain_monotonic_ns=phase_drain_monotonic_ns,
                    )
                except BaseException:
                    phase_observation = _scratch_unknown_phase_attribution(
                        "internal_error",
                        controller_guard_offset_ms=_scratch_phase_offset_ms(
                            phase_guard_monotonic_ns, launch_monotonic_ns
                        ),
                        controller_drain_offset_ms=_scratch_phase_offset_ms(
                            phase_drain_monotonic_ns, launch_monotonic_ns
                        ),
                    )
                if valid_scratch_phase_attribution(phase_observation):
                    setattr(operation_error, "scratch_phase_attribution", phase_observation)
            for cleanup_error in cleanup_errors:
                if hasattr(operation_error, "add_note"):
                    operation_error.add_note(
                        f"Scratch restore cleanup also failed: {type(cleanup_error).__name__}."
                    )
            if getattr(operation_error, "scratch_first_fault", None) is None:
                if controller_first_fault is not None:
                    setattr(operation_error, "scratch_first_fault", controller_first_fault)
            raise
        finally:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            final_cleanup_errors: list[BaseException] = []
            service_stopped = not launch_started
            if launch_started:
                try:
                    final_snapshot = _scratch_service_snapshot(unit_name)
                    final_props = final_snapshot["properties"]
                    if final_props.get("ActiveState") not in {"inactive", "failed"}:
                        _scratch_stop_unit(unit_name)
                        final_snapshot = _scratch_service_snapshot(unit_name)
                        final_props = final_snapshot["properties"]
                    service_stopped = (
                        final_props.get("LoadState") == "not-found"
                        or (
                            final_props.get("ActiveState") in {"inactive", "failed"}
                            and final_props.get("ExecMainPID", "0") in {"", "0"}
                            and final_props.get("ControlGroup", "") == ""
                        )
                    )
                except BaseException as cleanup_error:
                    final_cleanup_errors.append(cleanup_error)
            try:
                mount_absent = not _scratch_mountpoint_is_mounted(mountpoint)
            except BaseException as cleanup_error:
                mount_absent = False
                final_cleanup_errors.append(cleanup_error)
            try:
                current_mountpoint = mountpoint.lstat()
                mountpoint_stable = (
                    (current_mountpoint.st_dev, current_mountpoint.st_ino) == mountpoint_identity
                    and stat.S_ISDIR(current_mountpoint.st_mode)
                    and current_mountpoint.st_uid == 0
                    and stat.S_IMODE(current_mountpoint.st_mode) == 0o700
                    and not any(mountpoint.iterdir())
                )
                if service_stopped and mount_absent and mountpoint_stable:
                    mountpoint.rmdir()
                    mountpoint_removed = True
                else:
                    mountpoint_removed = False
            except FileNotFoundError:
                mountpoint_removed = False
            except BaseException as cleanup_error:
                mountpoint_removed = False
                final_cleanup_errors.append(cleanup_error)
            if selector is not None:
                try:
                    selector.close()
                except BaseException as cleanup_error:
                    final_cleanup_errors.append(cleanup_error)
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except BaseException as cleanup_error:
                            final_cleanup_errors.append(cleanup_error)
            if not service_stopped or not mount_absent or not mountpoint_removed:
                final_cleanup_errors.append(
                    RuntimeError("Scratch restore unit or mount cleanup could not be confirmed.")
                )
            cleanup_failure = _scratch_cleanup_failure(
                sys.exc_info()[1],
                final_cleanup_errors,
                authorization_probe=authorization_probe,
            )
            if cleanup_failure is not None:
                raise cleanup_failure from final_cleanup_errors[0]
        if worker_result is None:
            raise RuntimeError("Scratch restore completed without a verified worker result.")
        if authorization_probe:
            if worker_result != {
                "kind": "authorization_probe",
                "schema": 1,
                "status": "completed",
            }:
                raise RuntimeError("Scratch authorization probe returned an invalid result.")
            fault_site = "controller_host_guard"
            _scratch_host_guard("parent_after_worker_cleanup")
            return 0
        assert archive_descriptor is not None and archive_path is not None and archive_before is not None
        archive_after = os.fstat(archive_descriptor)
        current_path = archive_path.lstat()
        if (
            (archive_after.st_dev, archive_after.st_ino, archive_after.st_size,
             archive_after.st_mtime_ns, archive_after.st_ctime_ns)
            != (archive_before.st_dev, archive_before.st_ino, archive_before.st_size,
                archive_before.st_mtime_ns, archive_before.st_ctime_ns)
            or (current_path.st_dev, current_path.st_ino) != (archive_before.st_dev, archive_before.st_ino)
        ):
            raise RuntimeError("Scratch restore archive changed while it was being verified.")
        fault_site = "controller_host_guard"
        _scratch_host_guard("parent_after_worker_cleanup")
        return worker_result["restored_table_count"]
    except BaseException as exc:
        if authorization_probe:
            setattr(exc, "scratch_authorization_probe", True)
        _retain_first_scratch_fault(exc, origin="controller", site=fault_site)
        raise
    finally:
        if archive_descriptor is not None:
            os.close(archive_descriptor)


def _validate_custom_archive_header(path: pathlib.Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 5 or os.read(descriptor, 5) != b"PGDMP":
            raise RuntimeError("Backup archive is not a valid custom-format PostgreSQL dump.")
    finally:
        os.close(descriptor)


def create_backup(args: argparse.Namespace) -> dict[str, Any]:
    env_file = pathlib.Path(args.env_file)
    output_dir = pathlib.Path(args.output_dir)
    file_env = load_env(env_file)
    merged_env = {**file_env, **os.environ}
    database_url = merged_env.get("PLATFORM_DATABASE_URL")
    if not database_url:
        raise RuntimeError(f"PLATFORM_DATABASE_URL is missing from environment and {env_file}.")

    app_target = parse_database_url(database_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    rotate_existing = bool(getattr(args, "rotate_existing", True))
    preserve_existing = not rotate_existing
    initial_inventory = backup_inventory(output_dir) if preserve_existing else ()
    if preserve_existing and initial_inventory:
        validate_complete_backup_pairs(output_dir, initial_inventory)
    initial_inventory_digest = backup_inventory_digest(initial_inventory)
    timestamp = utc_now()
    timestamp_slug = timestamp.strftime("%Y%m%dT%H%M%SZ")
    dump_path = output_dir / f"platformdb-{timestamp_slug}.dump"
    metadata_path = dump_path.with_suffix(".json")
    if os.path.lexists(dump_path) or os.path.lexists(metadata_path):
        raise RuntimeError("Backup destination already exists; refusing to replace it.")

    # Check before creating the temporary output file; the child monitor then
    # guards the actual archive write against the same filesystem policy.
    require_restore_headroom(
        pathlib.Path("/"), output_dir, phase="backup_admission"
    )

    require_commands("pg_dump")
    if not args.dump_only:
        require_commands("systemd-run", "systemctl")

    dump_fd, dump_tmp_name = tempfile.mkstemp(
        prefix=f".{dump_path.name}.", suffix=".tmp", dir=output_dir
    )
    temporary_dump_path = pathlib.Path(dump_tmp_name)
    os.close(dump_fd)
    temporary_dump_identity = temporary_dump_path.lstat()
    temporary_metadata_path: pathlib.Path | None = None
    temporary_metadata_identity: tuple[int, int] | None = None
    published_dump_identity: tuple[int, int] | None = None
    published_metadata_identity: tuple[int, int] | None = None
    published_directory_synced = False
    started_at = utc_now()
    restore_verified = False
    restored_table_count: int | None = None

    try:
        run_disk_guarded_command(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--schema=platform",
                "--schema=public",
                *connection_args(app_target),
                "--file",
                str(temporary_dump_path),
            ],
            target=app_target,
            stage="pg_dump",
            disk_paths=(pathlib.Path("/"), output_dir),
        )
        if not temporary_dump_path.is_file() or temporary_dump_path.stat().st_size <= 0:
            raise RuntimeError("pg_dump did not produce a non-empty archive.")
        temporary_dump_stat = temporary_dump_path.lstat()
        if (temporary_dump_stat.st_dev, temporary_dump_stat.st_ino) != (
            temporary_dump_identity.st_dev,
            temporary_dump_identity.st_ino,
        ):
            raise RuntimeError("Temporary backup archive identity changed.")
        _fsync_backup_dump(
            temporary_dump_path,
            expected_identity=(temporary_dump_identity.st_dev, temporary_dump_identity.st_ino),
            expected_uid=temporary_dump_identity.st_uid,
        )
        if not args.dump_only:
            restored_table_count = _run_isolated_restore_drill(
                temporary_dump_path,
                timestamp_slug=timestamp_slug,
                private_scratch_diagnostics=getattr(args, "private_scratch_diagnostics", False),
            )
            restore_verified = True
        else:
            _validate_custom_archive_header(temporary_dump_path)

        completed_at = utc_now()
        metadata: dict[str, Any] = {
            "format_version": 2,
            "database": app_target.database,
            "schemas": ["platform", "public"],
            "required_extensions": list(REQUIRED_PLATFORM_EXTENSIONS),
            "dump_file": dump_path.name,
            "size_bytes": temporary_dump_path.stat().st_size,
            "sha256": sha256_file(temporary_dump_path),
            "started_at_utc": started_at.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed_at.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round((completed_at - started_at).total_seconds(), 3),
            "restore_verified": restore_verified,
            "alembic_revision_verified": restore_verified,
            "restored_table_count": restored_table_count,
            "restore_error": None,
        }
        if preserve_existing:
            before_publish_inventory = backup_inventory(output_dir)
            validate_complete_backup_pairs(output_dir, before_publish_inventory)
            before_publish_digest = backup_inventory_digest(before_publish_inventory)
            if before_publish_inventory != initial_inventory:
                raise RuntimeError("Pre-existing backup inventory changed before publication.")
            metadata.update(
                {
                    "preexisting_archive_count": sum(
                        1 for entry in initial_inventory if entry[0].endswith(".dump")
                    ),
                    "preexisting_sidecar_count": sum(
                        1 for entry in initial_inventory if entry[0].endswith(".json")
                    ),
                    "preexisting_archive_inventory_sha256": initial_inventory_digest,
                    "postexisting_archive_inventory_sha256": before_publish_digest,
                    "preexisting_archives_preserved": True,
                }
            )
        metadata_fd, metadata_tmp_name = tempfile.mkstemp(
            prefix=f".{metadata_path.name}.", suffix=".tmp", dir=output_dir
        )
        temporary_metadata_path = pathlib.Path(metadata_tmp_name)
        temporary_metadata_identity_stat = temporary_metadata_path.lstat()
        temporary_metadata_identity = (
            temporary_metadata_identity_stat.st_dev,
            temporary_metadata_identity_stat.st_ino,
        )
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(metadata, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_dump_path, 0o600)
        os.chmod(temporary_metadata_path, 0o600)
        metadata_stat = temporary_metadata_path.lstat()
        if (metadata_stat.st_dev, metadata_stat.st_ino) != temporary_metadata_identity:
            raise RuntimeError("Temporary backup metadata identity changed.")
        published_dump = _publish_noclobber(temporary_dump_path, dump_path)
        published_dump_identity = (published_dump.st_dev, published_dump.st_ino)
        published_metadata = _publish_noclobber(temporary_metadata_path, metadata_path)
        published_metadata_identity = (published_metadata.st_dev, published_metadata.st_ino)
        temporary_metadata_path = None
        _fsync_directory(output_dir)
        published_directory_synced = True

        removed: list[str] = []
        if restore_verified and rotate_existing:
            removed.extend(prune_unverified_backups(output_dir, preserve_metadata=metadata_path))
            removed.extend(prune_backups(output_dir, keep=args.keep))
        postexisting_archive_count: int | None = None
        if preserve_existing:
            post_publish_inventory = backup_inventory(output_dir)
            validate_complete_backup_pairs(output_dir, post_publish_inventory)
            postexisting_archive_count = sum(
                1 for entry in post_publish_inventory if entry[0].endswith(".dump")
            )
            expected_archive_count = sum(
                1 for entry in initial_inventory if entry[0].endswith(".dump")
            ) + 1
            if postexisting_archive_count != expected_archive_count:
                raise RuntimeError("Preserve mode did not retain exactly one new complete backup pair.")
        result = {
            "ok": True,
            **metadata,
            "rotation_mode": "rotate-existing" if rotate_existing else "preserve-existing",
            "metadata_file": str(metadata_path),
            "removed": removed,
            "postexisting_archive_count": postexisting_archive_count,
        }
        return result
    finally:
        active_exception = sys.exception()
        _unlink_if_same_inode(
            temporary_dump_path,
            (temporary_dump_identity.st_dev, temporary_dump_identity.st_ino),
        )
        _unlink_if_same_inode(temporary_metadata_path, temporary_metadata_identity)
        if active_exception is not None and (
            preserve_existing
            or (published_dump_identity is not None and not published_directory_synced)
        ):
            try:
                _rollback_published_backup_pair(
                    dump_path,
                    published_dump_identity,
                    metadata_path,
                    published_metadata_identity,
                )
            except Exception as rollback_error:
                setattr(
                    active_exception,
                    "_backup_preservation_integrity",
                    "new_pair_rollback_failed",
                )
                active_exception.add_note(str(rollback_error))
        if preserve_existing:
            try:
                current_inventory = backup_inventory(output_dir)
                validate_complete_backup_pairs(output_dir, current_inventory)
                owned = {dump_path.name: published_dump_identity}
                owned[metadata_path.name] = published_metadata_identity
                current_old_inventory = tuple(
                    entry
                    for entry in current_inventory
                    if owned.get(entry[0]) != (entry[1], entry[2])
                )
                if current_old_inventory != initial_inventory:
                    raise RuntimeError("Pre-existing backup inventory changed during preserve mode.")
            except Exception as integrity_error:
                if active_exception is None:
                    try:
                        _rollback_published_backup_pair(
                            dump_path,
                            published_dump_identity,
                            metadata_path,
                            published_metadata_identity,
                        )
                    except Exception as rollback_error:
                        setattr(
                            integrity_error,
                            "_backup_preservation_integrity",
                            "new_pair_rollback_failed",
                        )
                        integrity_error.add_note(str(rollback_error))
                    setattr(
                        integrity_error,
                        "_backup_preservation_integrity",
                        getattr(
                            integrity_error,
                            "_backup_preservation_integrity",
                            "preexisting_inventory_changed",
                        ),
                    )
                    raise
                setattr(
                    active_exception,
                    "_backup_preservation_integrity",
                    getattr(
                        active_exception,
                        "_backup_preservation_integrity",
                        "preexisting_inventory_changed",
                    ),
                )


def print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print("[OK] Platform database backup is valid")
    print(f"[OK] Archive: {result['dump_file']}")
    print(f"[OK] SHA256: {result['sha256']}")
    print(f"[OK] Restore verified: {result['restore_verified']}")
    if result.get("restored_table_count") is not None:
        print(f"[OK] Restored platform tables: {result['restored_table_count']}")
    if result.get("age_hours") is not None:
        print(f"[OK] Backup age: {result['age_hours']} hours")


def verify_existing_dump(args: argparse.Namespace) -> dict[str, Any]:
    dump_path = pathlib.Path(args.verify_dump).resolve()
    if not dump_path.is_file() or dump_path.suffix != ".dump":
        raise RuntimeError("--verify-dump must reference an existing .dump archive.")
    require_commands("systemd-run", "systemctl")
    table_count = _run_isolated_restore_drill(
        dump_path,
        timestamp_slug=utc_now().strftime("%Y%m%dT%H%M%SZ"),
        private_scratch_diagnostics=getattr(args, "private_scratch_diagnostics", False),
    )
    return {
        "ok": True,
        "dump_file": str(dump_path),
        "sha256": sha256_file(dump_path),
        "restore_verified": True,
        "alembic_revision_verified": True,
        "restored_table_count": table_count,
    }


def run_scratch_authorization_probe() -> dict[str, Any]:
    """Exercise only the staged helper's pre-archive authorization protocol."""
    _run_isolated_restore_drill(
        None,
        timestamp_slug=utc_now().strftime("%Y%m%dT%H%M%SZ"),
        private_scratch_diagnostics=True,
        authorization_probe=True,
    )
    return {"schema": 1, "status": "completed"}


def failure_report_for_exception(
    exc: Exception, *, private_scratch_diagnostics: bool
) -> dict[str, Any]:
    """Build the closed failure report shared by CLI and in-process callers."""
    error_message = str(exc)
    integrity_status = getattr(exc, "_backup_preservation_integrity", None)
    if integrity_status in {
        "preexisting_inventory_changed",
        "new_pair_rollback_failed",
    }:
        error_message += f" [backup_integrity={integrity_status}]"
    report: dict[str, Any] = {"ok": False, "error": error_message}
    diagnostic = getattr(exc, "restore_diagnostic", None)
    if _valid_restore_diagnostic(diagnostic):
        report["restore_diagnostic"] = diagnostic
    backup_disk_diagnostic = (
        exc.private_backup_disk_diagnostic()
        if isinstance(exc, RestoreGuardStop)
        else None
    )
    if valid_backup_disk_diagnostic(backup_disk_diagnostic):
        report["backup_disk_diagnostic"] = backup_disk_diagnostic
    scratch_diagnostic = getattr(exc, "scratch_unit_diagnostic", None)
    if private_scratch_diagnostics and valid_scratch_unit_diagnostic(scratch_diagnostic):
        report["scratch_unit_diagnostic"] = scratch_diagnostic
    admission_diagnostic = getattr(exc, "scratch_admission_diagnostic", None)
    if private_scratch_diagnostics and valid_scratch_admission_diagnostic(admission_diagnostic):
        report["scratch_admission_diagnostic"] = admission_diagnostic
    host_guard_diagnostic = getattr(exc, "scratch_host_guard_diagnostic", None)
    if private_scratch_diagnostics and valid_scratch_host_guard_diagnostic(host_guard_diagnostic):
        report["scratch_host_guard_diagnostic"] = host_guard_diagnostic
    first_fault = getattr(exc, "scratch_first_fault", None)
    if private_scratch_diagnostics and valid_scratch_first_fault(first_fault):
        report["scratch_first_fault"] = first_fault
    resource_attribution = getattr(exc, "scratch_resource_attribution", None)
    if (
        private_scratch_diagnostics
        and valid_scratch_resource_attribution(resource_attribution)
    ):
        report["scratch_resource_attribution"] = resource_attribution
    phase_attribution = getattr(exc, "scratch_phase_attribution", None)
    if private_scratch_diagnostics and valid_scratch_phase_attribution(phase_attribution):
        report["scratch_phase_attribution"] = phase_attribution
    return report


def private_failure_frame_for_exception(
    exc: Exception, *, enabled: bool
) -> str | None:
    """Return the existing bounded private frame, never raw text by default."""
    if not enabled:
        return None
    private_output = (
        None
        if getattr(exc, "scratch_authorization_probe", False)
        else getattr(exc, "scratch_unit_private_output", None)
    )
    frames: list[bytes] = []
    if (
        isinstance(private_output, tuple)
        and len(private_output) == 2
        and all(type(part) is bytes for part in private_output)
    ):
        frames.append(_scratch_private_output_envelope(*private_output))
    host_guard_frame = _scratch_host_guard_frame(
        getattr(exc, "scratch_host_guard_diagnostic", None)
    )
    if host_guard_frame is not None:
        frames.append(host_guard_frame)
    first_fault_frame = _scratch_first_fault_frame(
        getattr(exc, "scratch_first_fault", None)
    )
    if first_fault_frame is not None:
        frames.append(first_fault_frame)
    return b"".join(frames).decode("ascii") if frames else None


def main() -> int:
    args = parse_args()
    try:
        if getattr(args, "_scratch_restore_worker", False):
            required_worker_values = (
                args._scratch_root,
                args._scratch_timestamp_slug,
            )
            if not getattr(args, "_scratch_authorization_probe", False):
                required_worker_values += (
                    args._scratch_archive,
                    args._scratch_archive_dev,
                    args._scratch_archive_ino,
                    args._scratch_archive_size,
                    args._scratch_archive_sha256,
                )
            if any(value is None for value in required_worker_values):
                raise ValueError("Scratch restore worker input is incomplete.")
            try:
                return _scratch_worker_main(args)
            except BaseException as exc:
                _retain_first_scratch_fault(
                    exc,
                    origin="worker",
                    site=getattr(args, "_scratch_fault_site", "worker_entry"),
                )
                raise
        selected_modes = (
            int(args.check_latest)
            + int(getattr(args, "verify_latest_existing", False))
            + int(args.dump_only)
            + int(args.verify_dump is not None)
        )
        if selected_modes > 1:
            raise ValueError(
                "--dump-only, --check-latest, --verify-latest-existing, and --verify-dump "
                "are mutually exclusive."
            )
        if args.check_latest:
            result = check_latest_backup(pathlib.Path(args.output_dir), max_age_hours=args.max_age_hours)
        else:
            with _termination_signal_scope():
                if args.verify_dump is not None:
                    result = verify_existing_dump(args)
                elif getattr(args, "verify_latest_existing", False):
                    result = verify_latest_existing_backup(
                        pathlib.Path(args.output_dir),
                        pathlib.Path(args.env_file),
                        max_age_hours=args.max_age_hours,
                        admin_database_url=args.admin_database_url,
                        private_scratch_diagnostics=getattr(args, "private_scratch_diagnostics", False),
                    )
                else:
                    result = create_backup(args)
        print_result(result, as_json=args.as_json)
        return 0
    except Exception as exc:
        private_scratch_diagnostics = getattr(args, "private_scratch_diagnostics", False)
        report = failure_report_for_exception(
            exc, private_scratch_diagnostics=private_scratch_diagnostics
        )
        error_message = report["error"]
        if args.as_json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            private_frame = private_failure_frame_for_exception(
                exc, enabled=private_scratch_diagnostics
            )
            if private_frame is not None:
                sys.stderr.write(private_frame)
        else:
            print(f"[FAIL] {error_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
