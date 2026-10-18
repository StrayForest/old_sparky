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
SCRATCH_BACKEND_TMPFS_V1 = "tmpfs_v1"
SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1 = "disk_ext4_image_v1"
SCRATCH_BACKENDS = frozenset(
    {SCRATCH_BACKEND_TMPFS_V1, SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1}
)
SCRATCH_DISK_IMAGE_ROOT = pathlib.Path("/var/lib/oldsparky/backup-restore-images")
SCRATCH_SYS_BLOCK_ROOT = pathlib.Path("/sys/block")
SCRATCH_SYS_DEV_BLOCK_ROOT = pathlib.Path("/sys/dev/block")
SCRATCH_SYS_DEVICES_ROOT = pathlib.Path("/sys/devices")
SCRATCH_DISK_IMAGE_BYTES = 576 * 1024**2
SCRATCH_IMAGE_IDENTITY_PREDICATES = (
    "regular_file",
    "exact_size",
    "owner_mode_nlink",
    "path_inode_match",
    "parent_identity_stable",
    "same_filesystem",
    "fully_allocated",
)
SCRATCH_DISK_USABLE_MIN_BYTES = 512 * 1024**2
SCRATCH_DISK_LOOP_TEARDOWN_SECONDS = 5.0
SCRATCH_MEMORY_MAX_BYTES = 640 * 1024**2
SCRATCH_MEMORY_RESERVE_BYTES = 384 * 1024**2
SCRATCH_MEMORY_ADMISSION_BYTES = SCRATCH_MEMORY_MAX_BYTES + SCRATCH_MEMORY_RESERVE_BYTES
SCRATCH_SHARED_BUFFERS_MB = 16
PROTECTED_BACKUP_MANIFEST_MAX_BYTES = 64 * 1024
PROTECTED_BACKUP_MINIMUM_KEEP = 15
PROTECTED_BACKUP_DUMP_RE = re.compile(r"platformdb-[0-9]{8}T[0-9]{6}Z\.dump\Z")
SCRATCH_ADMISSION_RSS_MAX_BYTES = 1 << 40
SCRATCH_PROC_STATUS_MAX_BYTES = 16 * 1024
SCRATCH_RESOURCE_ATTRIBUTION_FILE_MAX_BYTES = 16 * 1024
SCRATCH_RESOURCE_ATTRIBUTION_MAX_BYTES = 4096
SCRATCH_MEMORY_STOP_MARGIN_BYTES = 32 * 1024**2
SCRATCH_TMPFS_STOP_BYTES = 64 * 1024**2
SCRATCH_RUNTIME_SECONDS = 15 * 60
SCRATCH_STARTUP_SECONDS = 30
SCRATCH_RUNTIME_SAMPLE_SECONDS = 0.25
SCRATCH_SYNTHETIC_ARCHIVE_MAX_BYTES = 48 * 1024**2
SCRATCH_SYNTHETIC_TMPFS_SAMPLE_SECONDS = 0.01
SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES = 16 * 1024
SCRATCH_SYNTHETIC_JOURNAL_MAX_LINES = 32
SCRATCH_SYNTHETIC_JOURNAL_QUERY_LINES = SCRATCH_SYNTHETIC_JOURNAL_MAX_LINES + 1
SCRATCH_SYNTHETIC_JOURNAL_TIMEOUT_SECONDS = 2.0
SCRATCH_EARLY_EXIT_DRAIN_SECONDS = 2.0
SCRATCH_EARLY_EXIT_DRAIN_BYTES = 1024 * 1024
SCRATCH_PG_PORT = 55432
SCRATCH_WORKER_MODE = "--_scratch-restore-worker"
SCRATCH_SYNTHETIC_RESTORE_DIAGNOSTIC_MODE = "--_scratch-synthetic-restore-diagnostic"
SCRATCH_START_TOKEN = b"OLDSPARKY_SCRATCH_RESTORE_START_V1\n"
SCRATCH_DONE_TOKEN = b"OLDSPARKY_SCRATCH_RESTORE_DONE_V1\n"
SCRATCH_APP_ROLE = "platform_user"
SCRATCH_PRIVATE_OUTPUT_MARKER = b"SCRATCH_UNIT_PRIVATE_OUTPUT_V1\n"
SCRATCH_FIRST_FAULT_MARKER = b"SCRATCH_FIRST_FAULT_V1\n"
SCRATCH_FIRST_FAULT_FRAME_MAX_BYTES = 1024
SCRATCH_PHASE_FRAME_MARKER = b"SCRATCH_PHASE_V1:"
SCRATCH_PHASE_FRAME_MAX_BYTES = 192
SCRATCH_PHASE_FRAME_MAX_COUNT = 12
SCRATCH_PHASE_OFFSET_MAX_MS = (SCRATCH_RUNTIME_SECONDS + SCRATCH_STARTUP_SECONDS + 30) * 1000
SCRATCH_ALLOCATION_FRAME_MARKER = b"SCRATCH_ALLOCATION_V1:"
SCRATCH_ALLOCATION_FRAME_MAX_BYTES = 4096
SCRATCH_ALLOCATION_ATTRIBUTION_MARKER = b"SCRATCH_ALLOCATION_ATTRIBUTION_V1:"
SCRATCH_ALLOCATION_ATTRIBUTION_MAX_BYTES = 8192
SCRATCH_ALLOCATION_MAX_SAMPLES = 6
SCRATCH_ALLOCATION_ENTRY_MAX = 8192
SCRATCH_ALLOCATION_DEPTH_MAX = 32
SCRATCH_ALLOCATION_SAMPLE_BUDGET_SECONDS = 0.1
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
SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES = 4 * 1024
SCRATCH_SYNTHETIC_RESULT_MAX_BYTES = 8 * 1024
SCRATCH_PRIVATE_RAW_BYTES = SCRATCH_PRIVATE_STDOUT_BYTES + SCRATCH_PRIVATE_STDERR_BYTES
SCRATCH_PRIVATE_ENVELOPE_BYTES = 48 * 1024
SCRATCH_AUTHORIZATION_PROBE_FRAME = (
    b'{"kind":"authorization_probe","schema":1,"status":"completed"}\n'
)
SCRATCH_SYNTHETIC_RESTORE_ARMS = (
    ("baseline", "replica", 10, False),
    ("minimal_no_senders", "minimal", 0, False),
    ("minimal_no_senders_single_transaction", "minimal", 0, True),
)
SCRATCH_SYNTHETIC_RESTORE_PHASES = ("restore_platform", "restore_public")
SCRATCH_SYNTHETIC_PAYLOAD_ROWS = 4096
SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW = 8192
SCRATCH_SYNTHETIC_PAYLOAD_BLOCKS = 255
SCRATCH_SYNTHETIC_PAYLOAD_SEED = "oldsparky-backup-restore-synthetic-v2"
SCRATCH_SYNTHETIC_PAYLOAD_BYTES = (
    SCRATCH_SYNTHETIC_PAYLOAD_ROWS * SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW
)
SCRATCH_SYNTHETIC_FIXTURE_ID = "platform_backup_restore_synthetic_bulk_v2"
SCRATCH_SYNTHETIC_ALEMBIC_REVISION = "synthetic_restore_revision_001"
SCRATCH_SYNTHETIC_TRGM_INDEX = "synthetic_restore_probe_search_trgm_idx"
SCRATCH_SYNTHETIC_MEMORY_SCOPE = "unit_cgroup_peak_since_phase_start_reset"
SCRATCH_SYNTHETIC_WAL_SCOPE = "cluster_pg_stat_wal_delta_after_restore_db_backend_drain"
SCRATCH_SYNTHETIC_ELAPSED_SCOPE = "monotonic_ns_around_pg_restore_command"
SCRATCH_SYNTHETIC_PHASE_ELAPSED_SCOPE = "monotonic_ns_precheck_through_postcheck"
SCRATCH_SYNTHETIC_TMPFS_SCOPE = "sampled_high_water_during_pg_restore_command"
SCRATCH_SYNTHETIC_DISK_SCOPE = "sampled_high_water_during_pg_restore_command"
SCRATCH_SYNTHETIC_WAL_BACKEND_DRAIN_SECONDS = 5.0
SCRATCH_SYNTHETIC_WAL_QUERY_TIMEOUT_SECONDS = 1.0
SCRATCH_SYNTHETIC_WAL_POLL_SECONDS = 0.05
_SCRATCH_INACCESSIBLE_PATHS = (
    "/opt/oldsparky/platform/shared",
    "/var/lib/postgresql",
    "/var/run/postgresql",
    "/run/postgresql",
    "/etc/postgresql",
    "/root",
)
_SCRATCH_SYNTHETIC_PATH_FIXTURE_PARENT_COUNTS = (4, 3, 2, 2, 1)
_SCRATCH_SYNTHETIC_PATH_FIXTURE_PREDICATES = (
    "not_directory",
    "symlink",
    "uid_mismatch",
    "gid_mismatch",
    "group_or_world_writable",
)
_SCRATCH_IDENTITY_LOAD_STATES = frozenset(
    {"loaded", "not-found", "masked", "error", "bad-setting", "merged", "stub", "other"}
)
_SCRATCH_IDENTITY_ACTIVE_STATES = frozenset(
    {"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance", "other"}
)
_SCRATCH_IDENTITY_COMPARISON_PROPERTIES = (
    "load_state",
    "active_state",
    "exec_main_pid",
    "invocation_id",
)
_SCRATCH_FIRST_FAULT_ORIGINS = frozenset({"controller", "worker"})
_SCRATCH_FIRST_FAULT_SITES = frozenset(
    {
        "controller_entry",
        "controller_archive_identity",
        "controller_unit_setup",
        "controller_synthetic_path_fixture",
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
_SCRATCH_DISK_MOUNT_FAILURE_REASONS = frozenset(
    {
        "mount_row_missing",
        "mount_metadata",
        "filesystem_source_root_options",
        "filesystem_not_ext4",
        "source_noncanonical",
        "device_major_minor_invalid",
        "sysfs_mount_unverified",
        "sysfs_device_unavailable",
        "sysfs_not_loop_device",
        "sysfs_device_number_mismatch",
        "mount_root_not_global",
        "mountpoint_mismatch",
        "mount_options_missing",
        "root_owner_mode",
        "root_uid_mismatch",
        "root_gid_mismatch",
        "root_mode_mismatch",
        "statvfs_unavailable",
        "statvfs_bound",
        "total_capacity_out_of_bounds",
        "free_below_stop",
    }
)
_SCRATCH_VERIFICATION_SITES = frozenset(
    {
        "none",
        "service_properties_shape",
        "service_resource_policy",
        "service_identity_state",
        "service_cgroup_identity",
        "service_pid_identity",
        "process_cgroup_read",
        "process_cgroup_parse",
        "process_cgroup_mismatch",
        "expected_executable_stat",
        "proc_exe_stat",
        "proc_exe_mismatch",
        "backend_mount_identity",
        "credential_visibility",
        "readonly_input_binds",
        "archive_exclusion",
        "controller_expected_executable_resolve",
        "controller_snapshot_properties",
        "controller_cgroup_snapshot",
        "controller_invocation_identity",
        "controller_image_mount_identity",
        "controller_process_start_time",
        "controller_cgroup_identity_compare",
        "controller_process_start_time_compare",
        "controller_second_snapshot_identity",
        "controller_start_deadline",
        "controller_process_state",
        "controller_completion_unit_state",
        "controller_completion_identity",
        "active_poll_host_guard",
        "active_poll_selector_wait",
        "active_poll_stream_read",
        "active_poll_stream_unregister",
        "active_poll_output_bound",
        "worker_root_path_identity",
        "worker_root_mode_identity",
        "worker_mount_identity",
        "worker_cgroup_identity",
        "worker_postgres_account_identity",
        "other",
    }
)
_SCRATCH_ACTIVE_POLL_VERIFICATION_SITES = frozenset(
    {
        "active_poll_host_guard",
        "active_poll_selector_wait",
        "active_poll_stream_read",
        "active_poll_stream_unregister",
        "active_poll_output_bound",
    }
)
_SCRATCH_WORKER_IDENTITY_VERIFICATION_SITES = frozenset(
    {
        "worker_root_path_identity",
        "worker_root_mode_identity",
        "worker_mount_identity",
        "worker_cgroup_identity",
        "worker_postgres_account_identity",
    }
)


def _scratch_fault_verification_binding_is_valid(
    origin: str, site: str, verification_site: str
) -> bool:
    if verification_site not in _SCRATCH_VERIFICATION_SITES - {"none"}:
        return False
    if origin == "controller" and site == "controller_active_poll":
        return verification_site in _SCRATCH_ACTIVE_POLL_VERIFICATION_SITES
    if origin == "controller" and site in {
        "controller_identity", "controller_completion_snapshot", "controller_final_snapshot",
    }:
        return (
            verification_site not in _SCRATCH_ACTIVE_POLL_VERIFICATION_SITES
            and verification_site not in _SCRATCH_WORKER_IDENTITY_VERIFICATION_SITES
        )
    if origin == "worker" and site in {
        "worker_root_identity", "worker_mount_snapshot", "worker_cgroup_snapshot",
    }:
        return verification_site not in _SCRATCH_ACTIVE_POLL_VERIFICATION_SITES
    return False


def valid_scratch_synthetic_path_fixture_rejection(value: Any) -> bool:
    """Validate the bounded target/component/predicate projection."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "target_index", "parent_component_index", "predicates"
    }:
        return False
    target_index = value.get("target_index")
    parent_component_index = value.get("parent_component_index")
    predicates = value.get("predicates")
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(target_index) is not int
        or not 0 <= target_index < len(_SCRATCH_SYNTHETIC_PATH_FIXTURE_PARENT_COUNTS)
        or type(parent_component_index) is not int
        or not 0 <= parent_component_index
        < _SCRATCH_SYNTHETIC_PATH_FIXTURE_PARENT_COUNTS[target_index]
        or type(predicates) is not list
        or not predicates
        or len(predicates) > len(_SCRATCH_SYNTHETIC_PATH_FIXTURE_PREDICATES)
        or any(
            type(predicate) is not str
            or predicate not in _SCRATCH_SYNTHETIC_PATH_FIXTURE_PREDICATES
            for predicate in predicates
        )
        or len(set(predicates)) != len(predicates)
        or tuple(predicates)
        != tuple(
            predicate
            for predicate in _SCRATCH_SYNTHETIC_PATH_FIXTURE_PREDICATES
            if predicate in predicates
        )
    ):
        return False
    return True


def valid_scratch_identity_snapshot_comparison(value: Any) -> bool:
    """Validate closed two-snapshot identity evidence without raw identifiers."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "first", "second", "equal"
    }:
        return False
    if type(value.get("schema")) is not int or value["schema"] != 1:
        return False
    states: list[dict[str, Any]] = []
    for name in ("first", "second"):
        snapshot = value.get(name)
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "load_state", "active_state", "exec_main_pid_present", "invocation_id_present"
        }:
            return False
        if (
            type(snapshot.get("load_state")) is not str
            or snapshot["load_state"] not in _SCRATCH_IDENTITY_LOAD_STATES
            or type(snapshot.get("active_state")) is not str
            or snapshot["active_state"] not in _SCRATCH_IDENTITY_ACTIVE_STATES
            or type(snapshot.get("exec_main_pid_present")) is not bool
            or type(snapshot.get("invocation_id_present")) is not bool
        ):
            return False
        states.append(snapshot)
    first, second = states
    if (
        first["load_state"] != "loaded"
        or first["active_state"] != "active"
        or not first["exec_main_pid_present"]
        or not first["invocation_id_present"]
    ):
        return False
    equal = value.get("equal")
    if not isinstance(equal, dict) or set(equal) != set(
        _SCRATCH_IDENTITY_COMPARISON_PROPERTIES
    ) or any(type(equal.get(key)) is not bool for key in _SCRATCH_IDENTITY_COMPARISON_PROPERTIES):
        return False
    if equal["load_state"] != (second["load_state"] == "loaded"):
        return False
    if equal["active_state"] != (second["active_state"] == "active"):
        return False
    if not second["exec_main_pid_present"] and equal["exec_main_pid"]:
        return False
    if not second["invocation_id_present"] and equal["invocation_id"]:
        return False
    return True


def _scratch_identity_snapshot_comparison(
    first_properties: dict[str, Any], second_properties: dict[str, Any]
) -> dict[str, Any] | None:
    """Project the exact preauthorization identity comparison to closed fields."""
    if not isinstance(first_properties, dict) or not isinstance(second_properties, dict):
        return None

    def state(value: Any, allowed: frozenset[str]) -> str:
        return value if type(value) is str and value in allowed - {"other"} else "other"

    first = {
        "load_state": state(first_properties.get("LoadState"), _SCRATCH_IDENTITY_LOAD_STATES),
        "active_state": state(first_properties.get("ActiveState"), _SCRATCH_IDENTITY_ACTIVE_STATES),
        "exec_main_pid_present": (
            type(first_properties.get("ExecMainPID")) is str
            and first_properties["ExecMainPID"].isdecimal()
            and first_properties["ExecMainPID"] not in {"", "0"}
        ),
        "invocation_id_present": (
            type(first_properties.get("InvocationID")) is str
            and re.fullmatch(r"[0-9a-f]{32}", first_properties["InvocationID"]) is not None
        ),
    }
    second = {
        "load_state": state(second_properties.get("LoadState"), _SCRATCH_IDENTITY_LOAD_STATES),
        "active_state": state(second_properties.get("ActiveState"), _SCRATCH_IDENTITY_ACTIVE_STATES),
        "exec_main_pid_present": (
            type(second_properties.get("ExecMainPID")) is str
            and second_properties["ExecMainPID"].isdecimal()
            and second_properties["ExecMainPID"] not in {"", "0"}
        ),
        "invocation_id_present": (
            type(second_properties.get("InvocationID")) is str
            and re.fullmatch(r"[0-9a-f]{32}", second_properties["InvocationID"]) is not None
        ),
    }
    comparison = {
        "schema": 1,
        "first": first,
        "second": second,
        "equal": {
            "load_state": first_properties.get("LoadState") == second_properties.get("LoadState"),
            "active_state": first_properties.get("ActiveState") == second_properties.get("ActiveState"),
            "exec_main_pid": first_properties.get("ExecMainPID") == second_properties.get("ExecMainPID"),
            "invocation_id": first_properties.get("InvocationID") == second_properties.get("InvocationID"),
        },
    }
    return comparison if valid_scratch_identity_snapshot_comparison(comparison) else None


def _scratch_second_snapshot_identity_failure(
    first_properties: dict[str, Any],
    second_properties: dict[str, Any],
    *,
    expected_pid: str,
    expected_invocation_id: str,
    synthetic_restore_diagnostic: bool,
) -> RuntimeError | None:
    """Apply the preauthorization identity guard and project synthetic failures only."""
    if (
        second_properties.get("LoadState") == "loaded"
        and second_properties.get("ActiveState") == "active"
        and second_properties.get("ExecMainPID") == expected_pid
        and second_properties.get("InvocationID") == expected_invocation_id
    ):
        return None
    failure = RuntimeError("Scratch restore unit identity changed before authorization.")
    if synthetic_restore_diagnostic:
        comparison = _scratch_identity_snapshot_comparison(
            first_properties, second_properties
        )
        if comparison is not None:
            setattr(
                failure,
                "scratch_synthetic_identity_snapshot_comparison",
                comparison,
            )
    return failure


def valid_scratch_first_fault(value: Any) -> bool:
    """Validate a closed first-fault projection without accepting raw details."""
    base_fields = {"schema", "origin", "site", "exception_kind", "errno"}
    if not isinstance(value, dict) or set(value) not in (
        base_fields,
        base_fields | {"verification_site"},
        base_fields | {"synthetic_path_fixture_rejection"},
        base_fields | {"synthetic_identity_snapshot_comparison"},
        base_fields | {"synthetic_identity_snapshot_comparison", "verification_site"},
        base_fields | {"disk_mount_failure_reason"},
        base_fields | {"disk_mount_failure_reason", "disk_mount_predicates"},
        base_fields | {"verification_site", "disk_mount_failure_reason"},
        base_fields | {"verification_site", "disk_mount_failure_reason", "disk_mount_predicates"},
    ):
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
    path_rejection = value.get("synthetic_path_fixture_rejection")
    identity_comparison = value.get("synthetic_identity_snapshot_comparison")
    if "synthetic_path_fixture_rejection" in value and (
        value["origin"] != "controller"
        or value["site"] != "controller_synthetic_path_fixture"
        or value["exception_kind"] != "runtime"
        or value["errno"] is not None
        or not valid_scratch_synthetic_path_fixture_rejection(path_rejection)
    ):
        return False
    if "synthetic_identity_snapshot_comparison" in value and (
        "synthetic_path_fixture_rejection" in value
        or value["origin"] != "controller"
        or value["site"] not in {
            "controller_identity", "controller_completion_snapshot", "controller_final_snapshot"
        }
        or value["exception_kind"] != "runtime"
        or value["errno"] is not None
        or not valid_scratch_identity_snapshot_comparison(identity_comparison)
    ):
        return False
    if "verification_site" in value and (
        type(value["verification_site"]) is not str
        or not _scratch_fault_verification_binding_is_valid(
            value["origin"], value["site"], value["verification_site"]
        )
    ):
        return False
    if "disk_mount_failure_reason" in value and (
        value["origin"] != "worker"
        or value["site"] != "worker_mount_snapshot"
        or value["exception_kind"] not in {"runtime", "os_error"}
        or (value["exception_kind"] == "runtime" and value["errno"] is not None)
        or (value["exception_kind"] == "os_error" and value["errno"] is None)
        or type(value["disk_mount_failure_reason"]) is not str
        or value["disk_mount_failure_reason"] not in _SCRATCH_DISK_MOUNT_FAILURE_REASONS
    ):
        return False
    if "disk_mount_predicates" in value:
        reason = value["disk_mount_failure_reason"]
        predicates = value["disk_mount_predicates"]
        mount_keys = {
            "fs_ext4", "source_canonical", "device_major_minor_valid",
            "sysfs_mount_verified", "sysfs_device_resolved", "sysfs_loop_device",
            "sysfs_device_number_matches", "sysfs_backing_file_present", "root_expected",
            "target_expected", "option_rw", "option_nosuid", "option_nodev",
            "option_noexec",
        }
        root_keys = {"root_uid", "root_gid", "root_mode_0700"}
        capacity_keys = {"total_capacity_bounded", "free_capacity_above_stop"}
        if not isinstance(predicates, dict) or any(type(item) is not bool for item in predicates.values()):
            return False
        if set(predicates) == mount_keys:
            failed = {
                "filesystem_not_ext4": not predicates["fs_ext4"],
                "source_noncanonical": not predicates["source_canonical"],
                "device_major_minor_invalid": not predicates["device_major_minor_valid"],
                "sysfs_mount_unverified": not predicates["sysfs_mount_verified"],
                "sysfs_device_unavailable": not predicates["sysfs_device_resolved"],
                "sysfs_not_loop_device": not predicates["sysfs_loop_device"] or not predicates["sysfs_backing_file_present"],
                "sysfs_device_number_mismatch": not predicates["sysfs_device_number_matches"],
                "mount_root_not_global": not predicates["root_expected"],
                "mountpoint_mismatch": not predicates["target_expected"],
                "mount_options_missing": not all(
                    predicates[key] for key in ("option_rw", "option_nosuid", "option_nodev", "option_noexec")
                ),
            }
            if reason == "filesystem_source_root_options":
                return any(not predicates[key] for key in (
                    "fs_ext4", "source_canonical", "root_expected",
                    "target_expected", "option_rw", "option_nosuid", "option_nodev", "option_noexec",
                ))
            return failed.get(reason) is True
        if set(predicates) == root_keys:
            failed = {
                "root_owner_mode": not all(predicates.values()),
                "root_uid_mismatch": not predicates["root_uid"],
                "root_gid_mismatch": not predicates["root_gid"],
                "root_mode_mismatch": not predicates["root_mode_0700"],
            }
            return failed.get(reason) is True
        if set(predicates) == capacity_keys:
            failed = {
                "statvfs_bound": not all(predicates.values()),
                "total_capacity_out_of_bounds": not predicates["total_capacity_bounded"],
                "free_below_stop": not predicates["free_capacity_above_stop"],
            }
            return failed.get(reason) is True
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
    path_rejection = getattr(error, "scratch_synthetic_path_fixture_rejection", None)
    identity_comparison = getattr(error, "scratch_synthetic_identity_snapshot_comparison", None)
    if path_rejection is not None:
        if identity_comparison is not None or not valid_scratch_synthetic_path_fixture_rejection(path_rejection):
            return None
        exception_kind, errno = "runtime", None
    else:
        exception_kind, errno = _scratch_exception_projection(error)
    value = {
        "schema": 1,
        "origin": origin,
        "site": site,
        "exception_kind": exception_kind,
        "errno": errno if exception_kind in {"permission", "os_error"} else None,
    }
    verification_site = getattr(error, "scratch_verification_site", None)
    if verification_site is not None:
        if (
            type(verification_site) is not str
            or not _scratch_fault_verification_binding_is_valid(origin, site, verification_site)
        ):
            return None
        value["verification_site"] = verification_site
    if path_rejection is not None:
        value["synthetic_path_fixture_rejection"] = path_rejection
    if identity_comparison is not None:
        if not valid_scratch_identity_snapshot_comparison(identity_comparison):
            return None
        exception_kind, errno = "runtime", None
        value["exception_kind"] = exception_kind
        value["errno"] = errno
        value["synthetic_identity_snapshot_comparison"] = identity_comparison
    disk_mount_reason = getattr(error, "scratch_disk_mount_failure_reason", None)
    if disk_mount_reason is not None:
        if disk_mount_reason not in _SCRATCH_DISK_MOUNT_FAILURE_REASONS:
            return None
        value["disk_mount_failure_reason"] = disk_mount_reason
    disk_mount_predicates = getattr(error, "scratch_disk_mount_predicates", None)
    if disk_mount_predicates is not None:
        if disk_mount_reason is None:
            return None
        value["disk_mount_predicates"] = disk_mount_predicates
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
    service_stopped: bool | None = None,
    mount_absent: bool | None = None,
    mountpoint_removed: bool | None = None,
    loop_released: bool | None = None,
    image_created_by_invocation: bool | None = None,
    image_removed: bool | None = None,
    retention_receipt_written: bool | None = None,
    protected_paths_cleaned: bool | None = None,
) -> BaseException | None:
    """Keep cleanup status separate and never replace an existing first fault."""
    observed_cleanup = (service_stopped, mount_absent, mountpoint_removed)
    if image_created_by_invocation is True or image_removed is not None:
        observed_cleanup += (image_removed,)
    if protected_paths_cleaned is not None:
        observed_cleanup += (protected_paths_cleaned,)
    if all(type(value) is bool for value in observed_cleanup):
        cleanup_status = (
            "confirmed"
            if all(observed_cleanup) and not cleanup_errors
            else "failed"
        )
    elif cleanup_errors:
        cleanup_status = "failed"
    else:
        cleanup_status = "unknown"
    cleanup_diagnostic = {
        "schema": 1,
        "service_stopped": service_stopped,
        "mount_absent": mount_absent,
        "mountpoint_removed": mountpoint_removed,
        "loop_released": loop_released,
        "image_created_by_invocation": image_created_by_invocation,
        "image_unlinked": image_removed,
        "retention_receipt_written": retention_receipt_written,
        "protected_paths_cleaned": protected_paths_cleaned,
        "cleanup_status": cleanup_status,
    }
    if not valid_scratch_cleanup_diagnostic(cleanup_diagnostic):
        cleanup_diagnostic = None
    if not cleanup_errors:
        if primary_error is not None:
            setattr(primary_error, "scratch_cleanup_status", cleanup_status)
            if cleanup_diagnostic is not None:
                setattr(primary_error, "scratch_cleanup_diagnostic", cleanup_diagnostic)
        return None
    if primary_error is not None:
        setattr(primary_error, "scratch_cleanup_status", cleanup_status)
        if cleanup_diagnostic is not None:
            setattr(primary_error, "scratch_cleanup_diagnostic", cleanup_diagnostic)
        if hasattr(primary_error, "add_note"):
            primary_error.add_note("Scratch restore final cleanup also failed.")
        return None
    cleanup_error = RuntimeError(
        "Scratch restore unit or mount cleanup could not be confirmed."
    )
    setattr(cleanup_error, "scratch_cleanup_status", cleanup_status)
    if cleanup_diagnostic is not None:
        setattr(cleanup_error, "scratch_cleanup_diagnostic", cleanup_diagnostic)
    if authorization_probe:
        setattr(cleanup_error, "scratch_authorization_probe", True)
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
    if len(payload) > SCRATCH_FIRST_FAULT_FRAME_MAX_BYTES:
        return None
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
        "parent_before_synthetic_restore_diagnostic",
        "parent_before_unit_launch",
        "parent_active_poll",
        "parent_after_worker_cleanup",
        "worker_after_start_before_archive_open",
        "worker_after_start_before_authorization_probe",
        "worker_after_start_before_synthetic_diagnostic",
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
_SCRATCH_ALLOCATION_PHASES = frozenset(
    {
        "initdb_end",
        "postgres_start_end",
        "restore_platform_start",
        "restore_platform_end",
    }
)
_SCRATCH_ALLOCATION_OPTIONAL_PHASES = frozenset({"worker_failure", "parent_last_guard"})
_SCRATCH_ALLOCATION_UNKNOWN_REASONS = frozenset(
    {
        "sample_unavailable", "identity_drift", "mount_identity", "read_error",
        "unsupported_entry", "entry_cap", "depth_cap", "deadline", "malformed",
        "incomplete", "capture_unavailable", "capture_prefix", "internal_error",
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
    if not lines or len(lines) > SCRATCH_PHASE_FRAME_MAX_COUNT + SCRATCH_ALLOCATION_MAX_SAMPLES:
        return unknown("malformed")
    phase_lines: list[bytes] = []
    for line in lines:
        if line.startswith(SCRATCH_ALLOCATION_FRAME_MARKER):
            # Allocation frames are optional observations. Their parser below
            # can mark them unknown without rejecting the restore's phase stream.
            if len(line) > len(SCRATCH_ALLOCATION_FRAME_MARKER) + SCRATCH_ALLOCATION_FRAME_MAX_BYTES + 1:
                continue
            continue
        phase_lines.append(line)
    if not phase_lines or len(phase_lines) > SCRATCH_PHASE_FRAME_MAX_COUNT:
        return unknown("malformed")
    expected = [
        (phase, event)
        for phase in SCRATCH_PHASES
        for event in ("start", "end")
    ]
    frames: list[dict[str, Any]] = []
    previous_offset = -1
    for index, line in enumerate(phase_lines):
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


def valid_scratch_allocation_sample(value: Any) -> bool:
    """Validate one closed aggregate-only restore allocation observation."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "phase", "status", "unknown_reason", "sample_unix_ns",
        "sample_offset_ms", "guard_available_bytes", "guard_sample_lag_ms",
        "mount", "host", "tree",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("phase")) is not str
        or value["phase"] not in _SCRATCH_ALLOCATION_PHASES | _SCRATCH_ALLOCATION_OPTIONAL_PHASES
        or type(value.get("status")) is not str
        or value["status"] not in {"complete", "unknown"}
        or type(value.get("sample_unix_ns")) is not int
        or not 0 <= value["sample_unix_ns"] <= (1 << 63) - 1
    ):
        return False
    reason = value.get("unknown_reason")
    if reason is not None and (type(reason) is not str or reason not in _SCRATCH_ALLOCATION_UNKNOWN_REASONS):
        return False
    for key, upper in (
        ("sample_offset_ms", SCRATCH_PHASE_OFFSET_MAX_MS),
        ("guard_available_bytes", SCRATCH_MEMORY_ADMISSION_BYTES - 1),
        ("guard_sample_lag_ms", 60_000),
    ):
        item = value.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= upper):
            return False
    mount = value.get("mount")
    if not isinstance(mount, dict) or set(mount) != {
        "dev", "ino", "mount_id", "total_bytes", "used_bytes", "free_bytes",
    }:
        return False
    for key, upper in (
        ("dev", (1 << 63) - 1), ("ino", (1 << 63) - 1),
        ("mount_id", (1 << 31) - 1),
        ("total_bytes", 1 << 60), ("used_bytes", 1 << 60), ("free_bytes", 1 << 60),
    ):
        item = mount.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= upper):
            return False
    if (
        mount.get("used_bytes") is not None
        and mount.get("free_bytes") is not None
        and mount["used_bytes"] + mount["free_bytes"] != mount.get("total_bytes")
    ):
        return False
    host = value.get("host")
    if not isinstance(host, dict) or set(host) != {
        "mem_available_bytes", "rss_bytes", "memory_current_bytes", "anon_bytes",
        "file_bytes", "shmem_bytes",
    }:
        return False
    for item in host.values():
        if item is not None and (type(item) is not int or not 0 <= item <= (1 << 60)):
            return False
    tree = value.get("tree")
    if not isinstance(tree, dict) or set(tree) != {"data", "base", "pg_wal"}:
        return False
    for counters in tree.values():
        if counters is not None and (
            not isinstance(counters, list)
            or len(counters) != 4
            or any(type(item) is not int or not 0 <= item <= (1 << 60) for item in counters[:2])
            or any(type(item) is not int or not 0 <= item <= SCRATCH_ALLOCATION_ENTRY_MAX for item in counters[2:])
        ):
            return False
    if value["status"] == "complete":
        return (
            reason is None
            and all(type(mount.get(key)) is int for key in mount)
            and all(type(host.get(key)) is int for key in host)
            and all(isinstance(tree.get(key), list) for key in ("data", "base", "pg_wal"))
        )
    return type(reason) is str


def valid_scratch_allocation_attribution(value: Any) -> bool:
    """Validate the bounded private restore sample series."""
    if not isinstance(value, dict) or set(value) != {
        "schema", "status", "unknown_reason", "archive_size_bytes", "archive_sha256", "samples",
    }:
        return False
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or type(value.get("status")) is not str
        or value["status"] not in {"complete", "partial", "unknown"}
        or type(value.get("archive_size_bytes")) is not int
        or not 0 < value["archive_size_bytes"] <= (1 << 60)
        or type(value.get("archive_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"]) is None
    ):
        return False
    reason = value.get("unknown_reason")
    if reason is not None and (type(reason) is not str or reason not in _SCRATCH_ALLOCATION_UNKNOWN_REASONS):
        return False
    samples = value.get("samples")
    if not isinstance(samples, list) or len(samples) > SCRATCH_ALLOCATION_MAX_SAMPLES:
        return False
    phases: list[str] = []
    phase_order = {
        "initdb_end": 0,
        "postgres_start_end": 1,
        "restore_platform_start": 2,
        "restore_platform_end": 3,
        "worker_failure": 4,
        "parent_last_guard": 5,
    }
    previous_phase = -1
    for sample in samples:
        if (
            not valid_scratch_allocation_sample(sample)
            or sample["phase"] in phases
            or phase_order[sample["phase"]] <= previous_phase
        ):
            return False
        phases.append(sample["phase"])
        previous_phase = phase_order[sample["phase"]]
    all_complete = all(sample["status"] == "complete" for sample in samples)
    if value["status"] == "complete":
        valid = (
            all_complete
            and reason is None
            and set(phases) == set(_SCRATCH_ALLOCATION_PHASES)
        )
    elif value["status"] == "partial":
        valid = bool(samples) and reason is not None
    else:
        valid = not samples and reason is not None
    if not valid:
        return False
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        return False
    return len(encoded) <= SCRATCH_ALLOCATION_ATTRIBUTION_MAX_BYTES


def valid_scratch_cleanup_status(value: Any) -> bool:
    """Validate the closed, private cleanup outcome enum."""
    return type(value) is str and value in {"confirmed", "failed", "unknown"}


def valid_scratch_cleanup_diagnostic(value: Any) -> bool:
    """Validate the post-finalizer observations without treating unknown as success."""
    keys = {
        "schema",
        "service_stopped",
        "mount_absent",
        "mountpoint_removed",
        "loop_released",
        "image_created_by_invocation",
        "image_unlinked",
        "retention_receipt_written",
        "protected_paths_cleaned",
        "cleanup_status",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or type(value.get("schema")) is not int
        or value["schema"] != 1
    ):
        return False
    for field in keys - {"schema", "cleanup_status"}:
        if value[field] is not None and type(value[field]) is not bool:
            return False
    if not valid_scratch_cleanup_status(value["cleanup_status"]):
        return False
    if value["image_created_by_invocation"] is False and any(
        value[field] is not None
        for field in (
            "loop_released",
            "image_unlinked",
            "retention_receipt_written",
        )
    ):
        return False
    if value["image_created_by_invocation"] is True and (
        value["image_unlinked"] is True
        and value["loop_released"] is not True
    ):
        return False
    if value["image_created_by_invocation"] is True and (
        value["image_unlinked"] is False
        and value["retention_receipt_written"] is None
    ):
        return False
    if value["image_created_by_invocation"] is True and (
        value["image_unlinked"] is True
        and value["retention_receipt_written"] is not None
    ):
        return False
    if value["image_created_by_invocation"] is None and any(
        value[field] is not None
        for field in ("loop_released", "image_unlinked", "retention_receipt_written")
    ):
        return False
    if value["image_unlinked"] is True and (
        value["service_stopped"] is not True
        or value["mount_absent"] is not True
        or value["mountpoint_removed"] is not True
        or value["loop_released"] is not True
        or value["retention_receipt_written"] is not None
    ):
        return False
    if value["image_unlinked"] is False and (
        value["service_stopped"] is not True
        or value["mount_absent"] is not True
        or value["mountpoint_removed"] is not True
        or value["loop_released"] is not True
        or value["retention_receipt_written"] is None
    ):
        return False
    if value["loop_released"] is False and value["image_unlinked"] is not None:
        return False
    if value["retention_receipt_written"] is True and value["image_unlinked"] is True:
        return False
    if value["cleanup_status"] == "confirmed" and any(
        value[field] is not True
        for field in (
            "service_stopped",
            "mount_absent",
            "mountpoint_removed",
            "protected_paths_cleaned",
        )
    ):
        return False
    if value["cleanup_status"] == "confirmed" and value[
        "image_created_by_invocation"
    ] is True and (
        value["loop_released"] is not True or value["image_unlinked"] is not True
    ):
        return False
    return True


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
        "scratch_cleanup_status",
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
        or (
            "scratch_cleanup_status" in report
            and not valid_scratch_cleanup_status(report["scratch_cleanup_status"])
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
    if not frame_bytes or len(frame_bytes) > SCRATCH_FIRST_FAULT_FRAME_MAX_BYTES:
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
            "scratch_cleanup_status",
        }
    ):
        return None
    value = report.get("scratch_first_fault")
    if not valid_scratch_first_fault(value) or frame != value:
        return None
    if (
        "scratch_cleanup_status" in report
        and not valid_scratch_cleanup_status(report["scratch_cleanup_status"])
    ):
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
_SCRATCH_UNIT_EXEC_MAIN_CODES = {"exited", "killed", "dumped", "unknown", "none"}
_SCRATCH_JOURNAL_STATUSES = {"complete", "bounded", "unavailable", "malformed"}
_SCRATCH_JOURNAL_SYNC_STATUSES = {"synced", "timeout", "unavailable"}
_SCRATCH_JOURNAL_REASONS = {
    "namespace_setup", "exec_failure", "unit_start_failure", "none", "unknown"
}
_SCRATCH_JOURNAL_NAMESPACE_KINDS = {
    "mount", "user", "network", "ipc", "uts", "cgroup", "time", "none", "unknown"
}
_SCRATCH_JOURNAL_ERRNOS = {
    "EACCES", "EPERM", "EINVAL", "ENOENT", "EBUSY", "ENOSPC", "ENOMEM", "EIO",
    "none", "unknown",
}
_SCRATCH_JOURNAL_TARGETS = {
    "scratch_tmpfs", "source_script", "source_policy", "archive", "shared_platform",
    "postgres_data", "postgres_socket", "postgres_config", "root_home", "none", "unknown",
}
_SCRATCH_GENERATED_UNIT_RE = re.compile(
    r"oldsparky-backup-restore-[0-9a-f]{24}\.service\Z"
)


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
    if schema == 3:
        expected_keys = base_keys | {"verification_site"}
    if schema == 4:
        expected_keys = base_keys | {"verification_site", "worker_frame_status"}
    if schema == 5:
        expected_keys = base_keys | {"verification_site", "worker_frame_status", "terminal_snapshot"}
    phases = {
        "unit_exited_before_confinement",
        "confinement_verification_failed",
        "unit_exited_after_confinement",
    }
    if schema in {2, 3, 4, 5}:
        phases.add("authorization_failed")
    if schema == 3 and "terminal_snapshot" in value:
        expected_keys.add("terminal_snapshot")
    if schema not in {1, 2, 3, 4, 5} or set(value) != expected_keys:
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
            schema in {3, 4, 5}
            and (
                type(value.get("verification_site")) is not str
                or value.get("verification_site") not in (
                    _SCRATCH_VERIFICATION_SITES
                    if schema in {4, 5}
                    else _SCRATCH_VERIFICATION_SITES - {"none"}
                )
            )
        )
        or (
            schema == 3
            and value.get("verification_site") in _SCRATCH_ACTIVE_POLL_VERIFICATION_SITES
        )
        or (
            schema in {4, 5}
            and (
                type(value.get("worker_frame_status")) is not str
                or value.get("worker_frame_status") not in {
                    "not_started", "missing", "valid", "invalid", "worker_failure_report"
                }
            )
        )
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
    if schema in {4, 5}:
        frame_status = value["worker_frame_status"]
        terminal = value.get("terminal_snapshot")
        if frame_status == "not_started" and terminal is not None and terminal["start_token_sent"]:
            return False
        if frame_status == "missing" and (
            value["stdout_bytes"] != 0
            or (terminal is not None and not terminal["start_token_sent"])
        ):
            return False
        if frame_status in {"valid", "invalid"} and value["stdout_bytes"] == 0:
            return False
        if frame_status == "worker_failure_report" and value["stderr_bytes"] == 0:
            return False
    if schema in {2, 3, 5} and "terminal_snapshot" in value and not valid_scratch_terminal_snapshot(
        value.get("terminal_snapshot")
    ):
        return False
    return True


_SCRATCH_UNIT_LOAD_STATES = {"loaded", "not-found", "other"}
_SCRATCH_UNIT_ACTIVE_STATES = {"active", "activating", "deactivating", "inactive", "failed", "reloading", "other"}


def valid_scratch_terminal_snapshot(value: Any) -> bool:
    """Validate the bounded lifecycle-only snapshot captured before unit stop."""
    keys = {
        "load_state", "active_state", "sub_state", "result", "exec_main_status",
        "exec_main_code",
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
        and type(value.get("exec_main_code")) is str
        and value["exec_main_code"] in _SCRATCH_UNIT_EXEC_MAIN_CODES
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
    exec_code_raw = properties.get("ExecMainCode")
    exec_code = {
        "1": "exited",
        "2": "killed",
        "3": "dumped",
        "0": "none",
    }.get(exec_code_raw, "unknown")
    value = {
        "load_state": properties.get("LoadState") if properties.get("LoadState") in _SCRATCH_UNIT_LOAD_STATES else "other",
        "active_state": properties.get("ActiveState") if properties.get("ActiveState") in _SCRATCH_UNIT_ACTIVE_STATES else "other",
        "sub_state": properties.get("SubState") if properties.get("SubState") in _SCRATCH_UNIT_SUBSTATES else "other",
        "result": properties.get("Result") if properties.get("Result") in _SCRATCH_UNIT_RESULTS else "other",
        "exec_main_status": exec_status,
        "exec_main_code": exec_code,
        "start_token_sent": start_token_sent,
        "completion_token_sent": completion_token_sent,
    }
    return value if valid_scratch_terminal_snapshot(value) else None


def valid_scratch_unit_journal_diagnostic(value: Any) -> bool:
    keys = {
        "schema", "status", "sync_status", "bytes_read", "complete", "records", "reason",
        "namespace_kind", "errno", "target",
    }
    return (
        isinstance(value, dict)
        and set(value) == keys
        and type(value.get("schema")) is int
        and value["schema"] == 1
        and type(value.get("status")) is str
        and value["status"] in _SCRATCH_JOURNAL_STATUSES
        and type(value.get("sync_status")) is str
        and value["sync_status"] in _SCRATCH_JOURNAL_SYNC_STATUSES
        and type(value.get("bytes_read")) is int
        and 0 <= value["bytes_read"] <= SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES
        and type(value.get("complete")) is bool
        and (value["status"] == "complete") == value["complete"]
        and type(value.get("records")) is int
        and 0 <= value["records"] <= SCRATCH_SYNTHETIC_JOURNAL_MAX_LINES
        and type(value.get("reason")) is str
        and value["reason"] in _SCRATCH_JOURNAL_REASONS
        and type(value.get("namespace_kind")) is str
        and value["namespace_kind"] in _SCRATCH_JOURNAL_NAMESPACE_KINDS
        and type(value.get("errno")) is str
        and value["errno"] in _SCRATCH_JOURNAL_ERRNOS
        and type(value.get("target")) is str
        and value["target"] in _SCRATCH_JOURNAL_TARGETS
        and (
            value["reason"] != "none"
            or (
                value["namespace_kind"] == "none"
                and value["errno"] == "none"
                and value["target"] == "none"
            )
        )
    )


def _scratch_empty_journal_diagnostic(
    status: str,
    *,
    sync_status: str = "unavailable",
    bytes_read: int = 0,
    complete: bool = False,
    records: int = 0,
) -> dict[str, Any]:
    diagnostic = {
        "schema": 1,
        "status": status,
        "sync_status": sync_status,
        "bytes_read": bytes_read,
        "complete": complete,
        "records": records,
        "reason": "unknown",
        "namespace_kind": "unknown",
        "errno": "unknown",
        "target": "unknown",
    }
    return diagnostic if valid_scratch_unit_journal_diagnostic(diagnostic) else {
        "schema": 1,
        "status": "unavailable",
        "sync_status": "unavailable",
        "bytes_read": 0,
        "complete": False,
        "records": 0,
        "reason": "unknown",
        "namespace_kind": "unknown",
        "errno": "unknown",
        "target": "unknown",
    }


def _scratch_journal_target(message: str, mountpoint: pathlib.Path) -> str:
    paths = (
        (str(mountpoint / "platform_backup_restore_drill.py"), "source_script"),
        (str(mountpoint / "platform_disk_policy.py"), "source_policy"),
        (str(mountpoint / "archive.dump"), "archive"),
        (str(mountpoint), "scratch_tmpfs"),
        ("/opt/oldsparky/platform/shared", "shared_platform"),
        ("/var/lib/postgresql", "postgres_data"),
        ("/var/run/postgresql", "postgres_socket"),
        ("/run/postgresql", "postgres_socket"),
        ("/etc/postgresql", "postgres_config"),
        ("/root", "root_home"),
    )
    path_characters = r"A-Za-z0-9._/-"
    hits = [
        (path, target)
        for path, target in paths
        if re.search(
            rf"(?<![{path_characters}]){re.escape(path)}(?![{path_characters}])",
            message,
        )
    ]
    if not hits:
        return "unknown"
    specific_hits = [
        (path, target)
        for path, target in hits
        if not any(
            other_path.startswith(path.rstrip("/") + "/")
            for other_path, _ in hits
            if other_path != path
        )
    ]
    return specific_hits[0][1] if len(specific_hits) == 1 else "unknown"


def _scratch_journal_message_projection(
    message: str,
    *,
    unit_name: str,
    mountpoint: pathlib.Path,
    terminal_snapshot: dict[str, Any] | None,
) -> tuple[str, str, str, str] | None:
    unit_prefix = f"{unit_name}: "
    if message.startswith(unit_prefix):
        message = message[len(unit_prefix):]
    lowered = message.casefold()
    namespace_start = re.match(
        r"^failed to set up (mount|user|network|ipc|uts|cgroup|time) namespacing"
        r"(?=\s*:|\s+and\s+failed to set up |$)",
        lowered,
    )
    if namespace_start:
        namespace_matches = re.findall(
            r"\bfailed to set up (mount|user|network|ipc|uts|cgroup|time) namespacing"
            r"(?=\s*:|\s+and\s+failed to set up |$)",
            lowered,
        )
        namespace_kind = namespace_matches[0] if len(set(namespace_matches)) == 1 else "unknown"
        reason = "namespace_setup"
    elif re.match(r"^failed at step namespace(?:\s|$)", lowered):
        namespace_kind = "unknown"
        reason = "namespace_setup"
    elif re.match(r"^failed at step exec(?:\s|$)", lowered):
        namespace_kind = "none"
        reason = "exec_failure"
    elif (
        re.fullmatch(
            r"main process exited, code=exited, status=226/namespace",
            lowered,
        )
        and isinstance(terminal_snapshot, dict)
        and terminal_snapshot.get("exec_main_code") == "exited"
        and terminal_snapshot.get("exec_main_status") == 226
    ):
        namespace_kind = "unknown"
        reason = "namespace_setup"
    elif re.match(r"^failed (?:to start|with result)\b", lowered):
        namespace_kind = "none"
        reason = "unit_start_failure"
    else:
        return None

    errno_hits = [
        name
        for phrase, name in (
            ("permission denied", "EACCES"),
            ("operation not permitted", "EPERM"),
            ("invalid argument", "EINVAL"),
            ("no such file or directory", "ENOENT"),
            ("device or resource busy", "EBUSY"),
            ("no space left on device", "ENOSPC"),
            ("cannot allocate memory", "ENOMEM"),
            ("input/output error", "EIO"),
        )
        if phrase in lowered
    ]
    errno_name = errno_hits[0] if len(set(errno_hits)) == 1 else "unknown"
    target = _scratch_journal_target(message, mountpoint)
    return reason, namespace_kind, errno_name, target


def _scratch_journal_capture_status(
    raw: bytes, *, eof: bool, returncode: int | None
) -> tuple[str, bool]:
    if type(raw) is not bytes or len(raw) > SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES:
        return "bounded", False
    if not eof or returncode is None:
        return "bounded", False
    if returncode != 0:
        return "unavailable", False
    line_count = sum(bool(line) for line in raw.splitlines())
    if line_count >= SCRATCH_SYNTHETIC_JOURNAL_QUERY_LINES:
        return "bounded", False
    return "complete", True


def _scratch_parse_unit_journal(
    raw: bytes,
    *,
    unit_name: str,
    mountpoint: pathlib.Path,
    complete: bool,
    status: str,
    sync_status: str = "synced",
    terminal_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        _SCRATCH_GENERATED_UNIT_RE.fullmatch(unit_name) is None
        or type(raw) is not bytes
        or len(raw) > SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES
        or status not in _SCRATCH_JOURNAL_STATUSES
        or sync_status not in _SCRATCH_JOURNAL_SYNC_STATUSES
        or type(complete) is not bool
    ):
        return _scratch_empty_journal_diagnostic("malformed", bytes_read=0)
    if status != "complete" or not complete:
        return _scratch_empty_journal_diagnostic(
            "bounded" if status == "bounded" else "unavailable",
            sync_status=sync_status,
            bytes_read=len(raw),
        )

    exact_records = 0
    projections: set[tuple[str, str, str, str]] = set()
    try:
        for line in raw.splitlines(keepends=True):
            if not line:
                continue
            if not line.endswith(b"\n"):
                raise ValueError("incomplete journal record")
            record = json.loads(line.decode("utf-8"))
            if not isinstance(record, dict):
                raise ValueError("journal record shape")
            unit_fields = [
                record[key]
                for key in ("_SYSTEMD_UNIT", "UNIT", "OBJECT_SYSTEMD_UNIT")
                if key in record
            ]
            if not unit_fields or any(value != unit_name for value in unit_fields):
                continue
            message = record.get("MESSAGE")
            if not isinstance(message, str):
                continue
            exact_records += 1
            if exact_records > SCRATCH_SYNTHETIC_JOURNAL_MAX_LINES:
                raise ValueError("journal record count")
            projection = _scratch_journal_message_projection(
                message,
                unit_name=unit_name,
                mountpoint=mountpoint,
                terminal_snapshot=terminal_snapshot,
            )
            if projection is not None:
                projections.add(projection)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return _scratch_empty_journal_diagnostic(
            "malformed",
            sync_status=sync_status,
            bytes_read=len(raw),
            records=min(exact_records, SCRATCH_SYNTHETIC_JOURNAL_MAX_LINES),
        )

    if sync_status != "synced":
        return _scratch_empty_journal_diagnostic(
            "complete",
            sync_status=sync_status,
            bytes_read=len(raw),
            complete=True,
            records=exact_records,
        )
    if not projections:
        return _scratch_empty_journal_diagnostic(
            "complete",
            sync_status=sync_status,
            bytes_read=len(raw),
            complete=True,
            records=exact_records,
        )
    specific_projections = {
        item for item in projections if item[0] != "unit_start_failure"
    }
    if len(specific_projections) == 1:
        reason, namespace_kind, errno_name, target = next(iter(specific_projections))
    elif not specific_projections and len(projections) == 1:
        reason, namespace_kind, errno_name, target = next(iter(projections))
    else:
        reason = namespace_kind = errno_name = target = "unknown"
    diagnostic = {
        "schema": 1,
        "status": "complete",
        "sync_status": sync_status,
        "bytes_read": len(raw),
        "complete": True,
        "records": exact_records,
        "reason": reason,
        "namespace_kind": namespace_kind,
        "errno": errno_name,
        "target": target,
    }
    return diagnostic if valid_scratch_unit_journal_diagnostic(diagnostic) else (
        _scratch_empty_journal_diagnostic(
            "malformed",
            sync_status=sync_status,
            bytes_read=len(raw),
            records=exact_records,
        )
    )


def _scratch_journal_sync_status() -> str:
    try:
        completed = subprocess.run(
            ["journalctl", "--sync"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "SYSTEMD_COLORS": "0",
                "SYSTEMD_PAGER": "cat",
            },
            cwd="/",
            close_fds=True,
            timeout=SCRATCH_SYNTHETIC_JOURNAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return "synced" if completed.returncode == 0 else "unavailable"


def _scratch_unit_journal_diagnostic(
    unit_name: str,
    *,
    mountpoint: pathlib.Path,
    terminal_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Capture only bounded, exact-unit journal facts; never retain raw messages."""
    if _SCRATCH_GENERATED_UNIT_RE.fullmatch(unit_name) is None:
        return _scratch_empty_journal_diagnostic("malformed")
    sync_status = _scratch_journal_sync_status()
    command = [
        "journalctl", "--no-pager", "--quiet", "--output=json",
        f"--lines={SCRATCH_SYNTHETIC_JOURNAL_QUERY_LINES}", f"--unit={unit_name}",
    ]
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SYSTEMD_COLORS": "0",
        "SYSTEMD_PAGER": "cat",
    }
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    raw = bytearray()
    complete = False
    status = "unavailable"
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd="/",
            close_fds=True,
            bufsize=0,
        )
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + SCRATCH_SYNTHETIC_JOURNAL_TIMEOUT_SECONDS
        eof = False
        bounded = False
        while not eof and time.monotonic() < deadline:
            events = selector.select(min(0.1, max(0.0, deadline - time.monotonic())))
            if not events:
                if process.poll() is not None:
                    # Drain through the pipe's EOF after journalctl exits.
                    continue
                continue
            try:
                chunk = os.read(
                    process.stdout.fileno(),
                    min(4096, SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES + 1 - len(raw)),
                )
            except BlockingIOError:
                continue
            if not chunk:
                eof = True
                break
            raw.extend(chunk)
            if len(raw) > SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES:
                del raw[SCRATCH_SYNTHETIC_JOURNAL_MAX_BYTES:]
                bounded = True
                break
        if eof:
            return_code = process.wait(timeout=0.5)
            status, complete = _scratch_journal_capture_status(
                bytes(raw), eof=True, returncode=return_code
            )
        elif bounded:
            status = "bounded"
        else:
            status = "bounded"
    except (OSError, subprocess.SubprocessError, ValueError):
        status = "unavailable"
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
        if selector is not None:
            try:
                selector.close()
            except Exception:
                pass
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
    return _scratch_parse_unit_journal(
        bytes(raw),
        unit_name=unit_name,
        mountpoint=mountpoint,
        complete=complete,
        status=status,
        sync_status=sync_status,
        terminal_snapshot=terminal_snapshot,
    )


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
    verification_site: str = "none",
    worker_frame_status: str | None = None,
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
    schema = (
        5 if worker_frame_status is not None and valid_scratch_terminal_snapshot(terminal_snapshot)
        else 4 if worker_frame_status is not None
        else 3 if verification_site != "none"
        else 2 if valid_scratch_terminal_snapshot(terminal_snapshot)
        else 1
    )
    diagnostic = {
        "schema": schema,
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
    if schema == 3:
        diagnostic["verification_site"] = verification_site
    if schema in {4, 5}:
        diagnostic["verification_site"] = verification_site
        diagnostic["worker_frame_status"] = worker_frame_status
    if valid_scratch_terminal_snapshot(terminal_snapshot):
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


def _scratch_verification_site(error: BaseException) -> str:
    """Return only an allowlisted controller verification operation name."""
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(8):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        site = getattr(current, "scratch_verification_site", None)
        if type(site) is str and site in _SCRATCH_VERIFICATION_SITES - {"none"}:
            return site
        current = current.__cause__ or current.__context__
    return "none"


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
    parser.add_argument(
        "--protected-backup-manifest",
        type=pathlib.Path,
        default=None,
        help="Private root-owned schema-1 list of exact backup dump basenames to preserve.",
    )
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
    parser.add_argument("--_scratch-backend", choices=sorted(SCRATCH_BACKENDS), default=SCRATCH_BACKEND_TMPFS_V1, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-dev", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-ino", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-size", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-archive-sha256", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-timestamp-slug", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--_scratch-authorization-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        SCRATCH_SYNTHETIC_RESTORE_DIAGNOSTIC_MODE,
        dest="_scratch_synthetic_restore_diagnostic",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    if args.private_scratch_diagnostics and not args.as_json:
        parser.error("--private-scratch-diagnostics requires --json")
    if (
        args._scratch_backend != SCRATCH_BACKEND_TMPFS_V1
        and not args._scratch_restore_worker
        and not args._scratch_authorization_probe
        and not args._scratch_synthetic_restore_diagnostic
    ):
        parser.error("A non-default scratch backend is restricted to a fixed diagnostic or worker.")
    if args.protected_backup_manifest is not None and (
        args.check_latest
        or args.verify_latest_existing
        or args.verify_dump is not None
    ):
        parser.error("--protected-backup-manifest is only valid while creating a backup")
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


def _scratch_mountinfo_escape(value: str) -> str:
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _scratch_mountinfo_for(path: pathlib.Path) -> tuple[str, str, str, str, str, str]:
    """Return the deepest mount containing a fixed local path."""
    target = str(path.resolve(strict=True))
    try:
        raw = pathlib.Path("/proc/self/mountinfo").read_text(encoding="ascii")
    except OSError as exc:
        error = RuntimeError("Scratch backing filesystem identity is unavailable.")
        setattr(error, "scratch_disk_mount_failure_reason", "mount_metadata")
        raise error from exc
    if len(raw) > 1024 * 1024:
        error = RuntimeError("Scratch backing filesystem metadata exceeded its bound.")
        setattr(error, "scratch_disk_mount_failure_reason", "mount_metadata")
        raise error
    found: tuple[str, str, str, str] | None = None
    for line in raw.splitlines():
        left, separator, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if not separator or len(left_fields) < 6 or len(right_fields) < 3:
            continue
        mountpoint = _scratch_mountinfo_escape(left_fields[4])
        if target != mountpoint and not (
            target.startswith("/")
            if mountpoint == "/"
            else target.startswith(mountpoint.rstrip("/") + "/")
        ):
            continue
        candidate = (
            mountpoint,
            left_fields[2],
            right_fields[0],
            _scratch_mountinfo_escape(right_fields[1]),
            _scratch_mountinfo_escape(left_fields[3]),
            left_fields[5],
        )
        if found is None or len(candidate[0]) > len(found[0]):
            found = candidate
    if found is None:
        error = RuntimeError("Scratch backing filesystem mount is not present.")
        setattr(error, "scratch_disk_mount_failure_reason", "mount_row_missing")
        raise error
    return found


def _scratch_validate_image_directory(path: pathlib.Path) -> os.stat_result:
    if path != SCRATCH_DISK_IMAGE_ROOT:
        raise RuntimeError("Scratch disk image directory path is outside its fixed owner location.")
    try:
        info = path.lstat()
    except OSError as exc:
        raise RuntimeError("Scratch disk image directory identity is unavailable.") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RuntimeError("Scratch disk image directory identity is unsafe.")
    return info


def valid_scratch_image_identity_diagnostic(value: Any) -> bool:
    """Validate the closed projection for a failed synthetic image identity check."""
    expected_keys = {
        "schema",
        "predicates",
        "failed_predicate",
        "observed_size",
        "expected_size",
        "allocated_bytes",
        "observed_mode",
        "expected_mode",
        "observed_nlink",
        "expected_nlink",
    }
    if (
        type(value) is not dict
        or set(value) != expected_keys
        or type(value.get("schema")) is not int
        or value["schema"] != 1
    ):
        return False
    predicates = value.get("predicates")
    if (
        type(predicates) is not dict
        or set(predicates) != set(SCRATCH_IMAGE_IDENTITY_PREDICATES)
        or any(type(result) is not bool for result in predicates.values())
    ):
        return False
    first_failed = next(
        (name for name in SCRATCH_IMAGE_IDENTITY_PREDICATES if not predicates[name]), None
    )
    if (
        first_failed is None
        or type(value.get("failed_predicate")) is not str
        or value["failed_predicate"] != first_failed
    ):
        return False
    exact_int_ranges = {
        "observed_size": (0, 2**63 - 1),
        "expected_size": (1, 2**63 - 1),
        "allocated_bytes": (0, 2**63 - 1),
        "observed_mode": (0, 0o7777),
        "expected_mode": (0, 0o7777),
        "observed_nlink": (0, 2**31 - 1),
        "expected_nlink": (1, 2**31 - 1),
    }
    for name, (minimum, maximum) in exact_int_ranges.items():
        observed = value.get(name)
        if type(observed) is not int or not minimum <= observed <= maximum:
            return False
    predicates = value["predicates"]
    if predicates["exact_size"] is not (
        value["observed_size"] == value["expected_size"]
    ):
        return False
    if predicates["fully_allocated"] is not (
        value["allocated_bytes"] >= value["observed_size"]
    ):
        return False
    if predicates["owner_mode_nlink"] and (
        value["observed_mode"] != value["expected_mode"]
        or value["observed_nlink"] != value["expected_nlink"]
    ):
        return False
    return (
        value["expected_size"] == SCRATCH_DISK_IMAGE_BYTES
        and value["expected_mode"] == 0o600
        and value["expected_nlink"] == 1
    )


def _scratch_image_path_identity(path: pathlib.Path) -> tuple[int, int, int, int, int, int, int]:
    """Validate the exact root-owned, fixed-size, fully allocated image inode."""
    parent_before = _scratch_validate_image_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("Scratch disk image identity is unavailable.") from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        parent_after = _scratch_validate_image_directory(path.parent)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_uid,
            opened.st_gid,
            stat.S_IMODE(opened.st_mode),
            opened.st_nlink,
        )
        predicates = {
            "regular_file": stat.S_ISREG(opened.st_mode),
            "exact_size": identity[2] == SCRATCH_DISK_IMAGE_BYTES,
            "owner_mode_nlink": identity[3:] == (0, 0, 0o600, 1),
            "path_inode_match": (opened.st_dev, opened.st_ino, opened.st_size)
            == (current.st_dev, current.st_ino, current.st_size),
            "parent_identity_stable": (parent_before.st_dev, parent_before.st_ino)
            == (parent_after.st_dev, parent_after.st_ino),
            "same_filesystem": opened.st_dev == parent_after.st_dev,
            "fully_allocated": opened.st_blocks * 512 >= opened.st_size,
        }
        if not all(predicates.values()):
            diagnostic = {
                "schema": 1,
                "predicates": predicates,
                "failed_predicate": next(
                    name for name in SCRATCH_IMAGE_IDENTITY_PREDICATES if not predicates[name]
                ),
                "observed_size": opened.st_size,
                "expected_size": SCRATCH_DISK_IMAGE_BYTES,
                "allocated_bytes": opened.st_blocks * 512,
                "observed_mode": stat.S_IMODE(opened.st_mode),
                "expected_mode": 0o600,
                "observed_nlink": opened.st_nlink,
                "expected_nlink": 1,
            }
            error = RuntimeError("Scratch disk image inode failed its fixed allocation contract.")
            setattr(error, "scratch_image_identity_diagnostic", diagnostic)
            raise error
        return identity
    finally:
        os.close(descriptor)


def _scratch_fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _scratch_disk_image_required_free_bytes(total_bytes: int) -> int:
    """Keep the existing chain floor and lead after reserving the full image."""
    return (
        _required_disk_floor(total_bytes)
        + RESTORE_DISK_LEAD_BYTES
        + SCRATCH_DISK_IMAGE_BYTES
    )


def _scratch_prepare_disk_image(token: str) -> tuple[pathlib.Path, tuple[int, int, int, int, int, int, int]]:
    """Create a fixed-size ext4 image after reserving its full disk allocation."""
    if re.fullmatch(r"[0-9a-f]{24}", token) is None:
        raise ValueError("Scratch image token is invalid.")
    if os.geteuid() != 0:
        raise RuntimeError("Scratch disk image creation requires the root maintenance owner.")
    parent = SCRATCH_DISK_IMAGE_ROOT
    parent_parent = parent.parent
    parent_parent_info = parent_parent.lstat()
    if (
        not stat.S_ISDIR(parent_parent_info.st_mode)
        or parent_parent_info.st_uid != 0
        or parent_parent_info.st_gid != 0
        or stat.S_IMODE(parent_parent_info.st_mode) & 0o022
    ):
        raise RuntimeError("Scratch image parent directory identity is unsafe.")
    mountpoint, device, fs_type, source, _mount_root, _mount_options = _scratch_mountinfo_for(parent_parent)
    if (
        fs_type != "ext4"
        or not source.startswith("/dev/")
        or os.stat(parent_parent).st_dev != os.stat("/").st_dev
    ):
        raise RuntimeError("Scratch image backing storage is not the verified disk-backed ext4 filesystem.")
    del mountpoint, device
    snapshot = snapshot_for_path(parent_parent)
    if not snapshot.valid:
        raise RestoreGuardStop("disk_unavailable", phase="scratch_image_admission")
    required = _scratch_disk_image_required_free_bytes(snapshot.total_bytes)
    if snapshot.free_bytes < required:
        raise RestoreGuardStop(
            "disk_floor",
            free_bytes=snapshot.free_bytes,
            required_free_bytes=required,
            phase="scratch_image_admission",
        )
    try:
        parent.mkdir(mode=0o700)
    except FileExistsError:
        pass
    parent_info = parent.lstat()
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != 0
        or parent_info.st_gid != 0
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or parent_info.st_dev != parent_parent_info.st_dev
    ):
        raise RuntimeError("Scratch image directory identity is unsafe.")
    path = parent / f"restore-{token}.ext4"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created_identity: tuple[int, int] | None = None
    image_created_by_invocation = False
    image_unlinked: bool | None = None
    retention_receipt_written: bool | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        image_created_by_invocation = True
        info = os.fstat(descriptor)
        created_identity = (info.st_dev, info.st_ino)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or info.st_nlink != 1:
            raise RuntimeError("Scratch image file creation returned an unsafe inode.")
        os.fchmod(descriptor, 0o600)
        if not hasattr(os, "posix_fallocate"):
            raise RuntimeError("Scratch image filesystem cannot guarantee full preallocation.")
        os.posix_fallocate(descriptor, 0, SCRATCH_DISK_IMAGE_BYTES)
        os.fsync(descriptor)
        allocated = os.fstat(descriptor)
        if allocated.st_size != SCRATCH_DISK_IMAGE_BYTES or allocated.st_blocks * 512 < allocated.st_size:
            raise RuntimeError("Scratch image allocation is sparse or incomplete.")
        mkfs_path = pathlib.Path("/usr/sbin/mkfs.ext4")
        try:
            mkfs_info = mkfs_path.lstat()
            mkfs_target = mkfs_path.resolve(strict=True)
            mkfs_target_info = mkfs_target.stat()
        except OSError as exc:
            raise RuntimeError("Scratch image formatter is unavailable.") from exc
        if (
            not (stat.S_ISREG(mkfs_info.st_mode) or stat.S_ISLNK(mkfs_info.st_mode))
            or mkfs_info.st_uid != 0
            or mkfs_info.st_gid != 0
            or (
                stat.S_ISREG(mkfs_info.st_mode)
                and stat.S_IMODE(mkfs_info.st_mode) & 0o022
            )
            or mkfs_target not in {mkfs_path, pathlib.Path("/usr/sbin/mke2fs")}
            or not stat.S_ISREG(mkfs_target_info.st_mode)
            or mkfs_target_info.st_uid != 0
            or mkfs_target_info.st_gid != 0
            or stat.S_IMODE(mkfs_target_info.st_mode) & 0o022
            or not stat.S_IMODE(mkfs_target_info.st_mode) & 0o111
        ):
            raise RuntimeError("Scratch image formatter identity is unsafe.")
        subprocess.run(
            [
                str(mkfs_path), "-F", "-q", "-m", "0", "-E",
                "nodiscard,lazy_itable_init=0", str(path),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        # e2fsprogs may use hole-punch fallback for zero-range on regular
        # files even with discard disabled. Reassert allocation on the exact
        # held inode before the unchanged full-allocation identity check.
        os.posix_fallocate(descriptor, 0, SCRATCH_DISK_IMAGE_BYTES)
        os.fsync(descriptor)
        _scratch_fsync_directory(parent)
        identity = _scratch_image_path_identity(path)
        after = snapshot_for_path(parent)
        after_required = _required_disk_floor(after.total_bytes) + RESTORE_DISK_LEAD_BYTES
        if not after.valid or after.free_bytes < after_required:
            raise RestoreGuardStop(
                "disk_floor",
                free_bytes=after.free_bytes,
                required_free_bytes=after_required,
                phase="scratch_image_postallocation",
            )
        return path, identity
    except BaseException as setup_error:
        # This image has not been offered to systemd yet. Remove only the inode
        # this call created; if identity changed, preserve it for investigation.
        try:
            path_info = path.lstat() if image_created_by_invocation else None
            descriptor_identity_matches = False
            if descriptor is not None and created_identity is not None:
                current = os.fstat(descriptor)
                assert path_info is not None
                descriptor_identity_matches = (
                    (current.st_dev, current.st_ino) == created_identity == (
                    path_info.st_dev, path_info.st_ino
                    )
                )
            if descriptor_identity_matches:
                try:
                    path.unlink()
                    _scratch_fsync_directory(parent)
                    image_unlinked = True
                except BaseException:
                    image_unlinked = False
                    try:
                        retained = path.lstat()
                        if (retained.st_dev, retained.st_ino) == created_identity:
                            retained_identity = (
                                retained.st_dev,
                                retained.st_ino,
                                retained.st_size,
                                retained.st_uid,
                                retained.st_gid,
                                stat.S_IMODE(retained.st_mode),
                                retained.st_nlink,
                            )
                            _scratch_write_image_cleanup_receipt(
                                path,
                                retained_identity,
                                unit_name=f"oldsparky-backup-restore-{token}.service",
                                reason="cleanup_observer_failed",
                            )
                            retention_receipt_written = True
                    except FileNotFoundError:
                        image_unlinked = True
                    except BaseException:
                        retention_receipt_written = False
            elif image_created_by_invocation and path_info is not None and (
                    stat.S_ISREG(path_info.st_mode)
                    and path_info.st_uid == 0
                    and path_info.st_gid == 0
                    and path_info.st_nlink == 1
            ):
                retained_identity = (
                    path_info.st_dev,
                    path_info.st_ino,
                    path_info.st_size,
                    path_info.st_uid,
                    path_info.st_gid,
                    stat.S_IMODE(path_info.st_mode),
                    path_info.st_nlink,
                )
                _scratch_write_image_cleanup_receipt(
                    path,
                    retained_identity,
                    unit_name=f"oldsparky-backup-restore-{token}.service",
                    reason="image_identity_changed",
                )
                retention_receipt_written = True
        except BaseException:
            # Preserve the first setup fault. The exact pathname and inode are
            # left untouched whenever ownership cannot be proven.
            pass
        setattr(
            setup_error,
            "scratch_image_created_by_invocation",
            image_created_by_invocation,
        )
        setattr(setup_error, "scratch_image_unlinked", image_unlinked)
        setattr(
            setup_error,
            "scratch_loop_released",
            True if image_created_by_invocation else None,
        )
        setattr(setup_error, "scratch_retention_receipt_written", retention_receipt_written)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _scratch_image_loop_matches(
    image_path: pathlib.Path,
    image_identity: tuple[int, int, int, int, int, int, int],
) -> list[tuple[str, str]]:
    """Find systemd-managed loop devices backed by this exact image inode."""
    matches: list[tuple[str, str]] = []
    loop_root = SCRATCH_SYS_BLOCK_ROOT
    loop_root_info = loop_root.lstat()
    mountpoint, _device, fs_type, _source, mount_root, _options = _scratch_mountinfo_for(loop_root)
    if (
        not stat.S_ISDIR(loop_root_info.st_mode)
        or loop_root_info.st_uid != 0
        or fs_type != "sysfs"
        or mount_root != "/"
        or not mountpoint.startswith("/sys")
    ):
        raise RuntimeError("Scratch loop inventory is not backed by the verified host sysfs mount.")
    loop_directories: list[pathlib.Path] = []
    try:
        with os.scandir(loop_root) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    raise RuntimeError("Scratch sysfs block inventory exceeded its fixed bound.")
                if re.fullmatch(r"loop[0-9]{1,3}", entry.name):
                    loop_directories.append(loop_root / entry.name)
    except OSError as exc:
        raise RuntimeError("Scratch sysfs block inventory could not be read.") from exc
    if len(loop_directories) > 64:
        raise RuntimeError("Scratch loop inventory exceeded its fixed bound.")
    for loop_directory in sorted(loop_directories):
        backing_file = loop_directory / "loop" / "backing_file"
        try:
            raw_path = backing_file.read_bytes()
            if len(raw_path) > 4096:
                raise RuntimeError("Scratch loop backing path exceeded its bound.")
            value = raw_path.decode("utf-8", errors="strict").strip()
            if not value:
                continue
            backing_path = pathlib.Path(_scratch_mountinfo_escape(value))
            if not backing_path.is_absolute() or str(backing_path) != str(image_path):
                continue
            info = backing_path.lstat()
            identity = _scratch_image_path_identity(backing_path)
            if (
                (info.st_dev, info.st_ino, info.st_size)
                != (image_identity[0], image_identity[1], image_identity[2])
                or identity[:3] != image_identity[:3]
            ):
                raise RuntimeError("Scratch loop backing image identity changed.")
            loop_name = loop_directory.name
            dev_path = loop_directory / "dev"
            raw_dev = dev_path.read_text(encoding="ascii").strip()
            if re.fullmatch(r"[0-9]+:[0-9]+", raw_dev) is None:
                raise RuntimeError("Scratch loop device identity is invalid.")
            matches.append((loop_name, raw_dev))
        except FileNotFoundError:
            # Loop devices may disappear between enumeration and read; the
            # caller rechecks until the bounded teardown deadline.
            continue
        except OSError as exc:
            raise RuntimeError("Scratch loop association could not be verified.") from exc
    return matches


def _scratch_verify_process_image_mount(
    *,
    pid: str,
    mountpoint: pathlib.Path,
    image_path: pathlib.Path,
    image_identity: tuple[int, int, int, int, int, int, int],
) -> str:
    _scratch_image_path_identity(image_path)
    matches = _scratch_image_loop_matches(image_path, image_identity)
    if len(matches) != 1:
        raise RuntimeError("Scratch image loop association is not unique for the active unit.")
    try:
        raw = pathlib.Path(f"/proc/{pid}/mountinfo").read_text(encoding="ascii")
    except OSError as exc:
        raise RuntimeError("Scratch unit mount namespace is unavailable.") from exc
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Scratch unit mount namespace metadata exceeded its bound.")
    expected_mount: tuple[str, str, str, str] | None = None
    for line in raw.splitlines():
        left, separator, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if not separator or len(left_fields) < 6 or len(right_fields) < 3:
            continue
        target = _scratch_mountinfo_escape(left_fields[4])
        if target == str(mountpoint):
            expected_mount = (
                left_fields[2],
                _scratch_mountinfo_escape(left_fields[3]),
                right_fields[0],
                left_fields[5],
            )
            break
    if (
        expected_mount is None
        or expected_mount[0] != matches[0][1]
        or expected_mount[1] != "/"
        or expected_mount[2] != "ext4"
        or not {"rw", "nosuid", "nodev", "noexec"}.issubset(
            set(expected_mount[3].split(","))
        )
    ):
        raise RuntimeError("Scratch unit mount does not match its exact loop-backed ext4 image.")
    if _scratch_mountpoint_is_mounted(mountpoint):
        raise RuntimeError("Scratch disk image mount unexpectedly appeared in the host namespace.")
    return matches[0][1]


def _scratch_wait_for_image_loop_release(
    image_path: pathlib.Path,
    image_identity: tuple[int, int, int, int, int, int, int],
) -> bool:
    deadline = time.monotonic() + SCRATCH_DISK_LOOP_TEARDOWN_SECONDS
    while True:
        if not _scratch_image_loop_matches(image_path, image_identity):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _scratch_unlink_image_exact(
    image_path: pathlib.Path,
    image_identity: tuple[int, int, int, int, int, int, int],
) -> None:
    current = _scratch_image_path_identity(image_path)
    if current != image_identity:
        raise RuntimeError("Scratch image changed before its owned-inode cleanup.")
    if _scratch_image_loop_matches(image_path, image_identity):
        raise RuntimeError("Scratch image still has a systemd loop association.")
    image_path.unlink()
    _scratch_fsync_directory(image_path.parent)


def _scratch_write_image_cleanup_receipt(
    image_path: pathlib.Path,
    image_identity: tuple[int, int, int, int, int, int, int],
    *,
    unit_name: str,
    reason: str,
) -> None:
    allowed = {
        "service_not_stopped",
        "host_mount_present",
        "mountpoint_not_removed",
        "loop_association_persists",
        "image_identity_changed",
        "cleanup_observer_failed",
    }
    if reason not in allowed or image_path.parent != SCRATCH_DISK_IMAGE_ROOT:
        raise ValueError("Scratch image cleanup receipt inputs are invalid.")
    receipt = {
        "schema": 1,
        "unit_name": unit_name,
        "image_name": image_path.name,
        "image_dev": image_identity[0],
        "image_ino": image_identity[1],
        "image_size_bytes": image_identity[2],
        "cleanup_status": "retained",
        "reason": reason,
    }
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(payload) > 1024:
        raise RuntimeError("Scratch image cleanup receipt exceeded its bound.")
    path = SCRATCH_DISK_IMAGE_ROOT / f"cleanup-{image_path.stem}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        if os.write(descriptor, payload) != len(payload):
            raise RuntimeError("Scratch image cleanup receipt was not written completely.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _scratch_fsync_directory(path.parent)


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


def prune_backups(
    output_dir: pathlib.Path,
    *,
    keep: int,
    protected_dump_files: frozenset[str] = frozenset(),
    verified_only: bool = False,
    expected_verified_order: tuple[dict[str, Any], ...] | None = None,
) -> list[str]:
    if keep < 1:
        raise ValueError("--keep must be at least 1.")
    if expected_verified_order is not None:
        if not verified_only:
            raise ValueError("Expected verified order requires verified-only pruning.")
        current_order = _verified_backup_candidate_order(output_dir)
        if current_order != expected_verified_order:
            raise RuntimeError("Verified backup inventory changed before retention.")
        removed: list[str] = []
        for expected in expected_verified_order[:-keep]:
            dump_name = expected["dump_file"]
            if dump_name in protected_dump_files:
                continue
            current = _backup_retention_pair_row(output_dir, dump_name)
            if current != expected:
                raise RuntimeError("Verified backup pair changed before retention.")
            dump_path = output_dir / dump_name
            metadata_path = dump_path.with_suffix(".json")
            sidecar_stat, sidecar_digest, _sidecar_bytes = _inventory_file(metadata_path)
            if (
                _backup_retention_stat_identity(sidecar_stat) != expected["sidecar_identity"]
                or sidecar_digest != expected["sidecar_content_sha256"]
            ):
                raise RuntimeError("Verified backup sidecar changed before retention.")
            dump_path.unlink()
            removed.append(str(dump_path))
            metadata_path.unlink()
            removed.append(str(metadata_path))
        return removed
    dumps: list[pathlib.Path] = []
    for path in output_dir.glob("platformdb-*.dump"):
        if verified_only:
            metadata_path = path.with_suffix(".json")
            try:
                _metadata_stat, _digest, raw = _inventory_file(metadata_path)
                metadata = json.loads((raw or b"").decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError):
                continue
            if (
                not isinstance(metadata, dict)
                or metadata.get("dump_file") != path.name
                or metadata.get("restore_verified") is not True
            ):
                continue
        dumps.append(path)
    dumps.sort(key=lambda path: path.stat().st_mtime)
    removed: list[str] = []
    for dump_path in dumps[:-keep]:
        if dump_path.name in protected_dump_files:
            continue
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
    protected_dump_files: frozenset[str] = frozenset(),
    expected_candidate_order: tuple[dict[str, Any], ...] | None = None,
) -> list[str]:
    if expected_candidate_order is not None:
        current_order = _unverified_backup_candidate_order(
            output_dir,
            preserve_metadata=preserve_metadata,
            protected_dump_files=protected_dump_files,
        )
        if current_order != expected_candidate_order:
            raise RuntimeError("Unverified backup inventory changed before retention.")
        removed: list[str] = []
        for expected in expected_candidate_order:
            current = _unverified_backup_candidate_row(
                output_dir,
                expected["pair"]["dump_file"],
                preserve_metadata=preserve_metadata,
                protected_dump_files=protected_dump_files,
            )
            if current != expected:
                raise RuntimeError("Unverified backup pair changed before retention.")
            dump_path = output_dir / expected["pair"]["dump_file"]
            metadata_path = dump_path.with_suffix(".json")
            current_sidecar_stat, current_sidecar_digest, _raw = _inventory_file(metadata_path)
            if (
                _backup_retention_stat_identity(current_sidecar_stat)
                != expected["pair"]["sidecar_identity"]
                or current_sidecar_digest != expected["pair"]["sidecar_content_sha256"]
            ):
                raise RuntimeError("Unverified backup sidecar changed before retention.")
            dump_path.unlink()
            metadata_path.unlink()
            removed.extend((str(metadata_path), str(dump_path)))
        return removed

    candidates: list[
        tuple[pathlib.Path, pathlib.Path, tuple[int, ...], tuple[int, ...], str]
    ] = []

    def stat_identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_gid,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    for metadata_path in output_dir.glob("platformdb-*.json"):
        if metadata_path == preserve_metadata:
            continue
        if f"{metadata_path.stem}.dump" in protected_dump_files:
            continue
        try:
            metadata_stat, metadata_digest, raw = _inventory_file(metadata_path)
            metadata = json.loads((raw or b"").decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if metadata.get("restore_verified"):
            continue
        dump_file = metadata.get("dump_file")
        dump_name = metadata_path.with_suffix(".dump").name
        if (
            PROTECTED_BACKUP_DUMP_RE.fullmatch(dump_name) is None
            or dump_file != dump_name
        ):
            raise RuntimeError("Refusing unverified backup metadata with a mismatched dump name.")
        dump_path = metadata_path.with_suffix(".dump")
        dump_stat = _stable_backup_stat(dump_path)
        candidates.append(
            (
                dump_path,
                metadata_path,
                stat_identity(dump_stat),
                stat_identity(metadata_stat),
                metadata_digest,
            )
        )

    removed: list[str] = []
    for dump_path, metadata_path, dump_identity, metadata_identity, metadata_digest in candidates:
        # Recheck the exact paired names immediately before unlinking.  The
        # metadata value is never used as a filesystem path.
        if stat_identity(_stable_backup_stat(dump_path)) != dump_identity:
            raise RuntimeError("Unverified backup archive changed before prune.")
        current_metadata, current_digest, _raw = _inventory_file(metadata_path)
        if (
            stat_identity(current_metadata) != metadata_identity
            or current_digest != metadata_digest
        ):
            raise RuntimeError("Unverified backup sidecar changed before prune.")
        dump_path.unlink()
        metadata_path.unlink()
        removed.extend((str(metadata_path), str(dump_path)))
    return removed


def _retention_canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _retention_domain_digest(domain: str, value: Any) -> str:
    if domain not in {
        "protected-backup-manifest",
        "protected-backup-pairs",
        "verified-backup-candidates",
        "verified-backup-selection",
        "backup-retention-removals",
    }:
        raise ValueError("Unknown protected retention digest domain.")
    return hashlib.sha256(
        b"oldsparky." + domain.encode("ascii") + b".v1\0" + _retention_canonical_bytes(value)
    ).hexdigest()


def _backup_retention_stat_identity(info: os.stat_result) -> list[int]:
    return [
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


def _protected_backup_manifest_snapshot(path: pathlib.Path) -> dict[str, Any]:
    """Read and bind the private manifest without following or trusting its path."""

    try:
        path_stat = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RuntimeError("Protected backup manifest is missing or unsafe.") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > PROTECTED_BACKUP_MANIFEST_MAX_BYTES
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise RuntimeError("Protected backup manifest has unsafe metadata.")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, min(8192, PROTECTED_BACKUP_MANIFEST_MAX_BYTES + 1 - total))
            if not block:
                break
            total += len(block)
            if total > PROTECTED_BACKUP_MANIFEST_MAX_BYTES:
                raise RuntimeError("Protected backup manifest exceeds its size limit.")
            chunks.append(block)
        after = os.fstat(descriptor)
        current_path = path.lstat()
        identity_fields = (
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
        if (
            total != before.st_size
            or any(getattr(before, field) != getattr(after, field) for field in identity_fields)
            or any(getattr(after, field) != getattr(current_path, field) for field in identity_fields)
        ):
            raise RuntimeError("Protected backup manifest changed while being read.")
    finally:
        os.close(descriptor)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    raw = b"".join(chunks)
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Protected backup manifest is not valid schema 1 JSON.") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "protected_dump_files"}
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or not isinstance(document.get("protected_dump_files"), list)
        or not document["protected_dump_files"]
    ):
        raise RuntimeError("Protected backup manifest does not match schema 1.")
    names = document["protected_dump_files"]
    if any(
        not isinstance(name, str) or PROTECTED_BACKUP_DUMP_RE.fullmatch(name) is None
        for name in names
    ) or len(set(names)) != len(names):
        raise RuntimeError("Protected backup manifest contains invalid or duplicate names.")
    snapshot: dict[str, Any] = {
        "identity": _backup_retention_stat_identity(after),
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "protected_dump_files": sorted(names),
    }
    snapshot["digest"] = _retention_domain_digest(
        "protected-backup-manifest", snapshot
    )
    return snapshot


def _load_protected_backup_manifest(path: pathlib.Path) -> frozenset[str]:
    """Compatibility wrapper returning the validated manifest names."""

    snapshot = _protected_backup_manifest_snapshot(path)
    return frozenset(snapshot["protected_dump_files"])


def _stable_backup_stat(path: pathlib.Path) -> os.stat_result:
    try:
        path_stat = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RuntimeError("Protected backup pair is missing or unsafe.") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise RuntimeError("Protected backup pair has unsafe file metadata.")
        after = os.fstat(descriptor)
        current_path = path.lstat()
        identity_fields = (
            "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
            "st_size", "st_mtime_ns", "st_ctime_ns",
        )
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields) or any(
            getattr(after, field) != getattr(current_path, field) for field in identity_fields
        ):
            raise RuntimeError("Protected backup pair changed while being checked.")
        return after
    finally:
        os.close(descriptor)


def _backup_retention_pair_row(
    output_dir: pathlib.Path, dump_name: str
) -> dict[str, Any]:
    """Read a pair's metadata identities and sidecar claims, never its dump body."""

    if PROTECTED_BACKUP_DUMP_RE.fullmatch(dump_name) is None:
        raise RuntimeError("Backup pair name is not canonical.")
    dump_path = output_dir / dump_name
    metadata_path = dump_path.with_suffix(".json")
    dump_stat = _stable_backup_stat(dump_path)
    metadata_stat, sidecar_digest, sidecar_bytes = _inventory_file(metadata_path)
    try:
        metadata = json.loads((sidecar_bytes or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Backup sidecar is invalid.") from exc
    claimed_size = metadata.get("size_bytes") if isinstance(metadata, dict) else None
    claimed_sha256 = metadata.get("sha256") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("dump_file") != dump_name
        or type(claimed_size) is not int
        or claimed_size <= 0
        or not isinstance(claimed_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", claimed_sha256) is None
    ):
        raise RuntimeError("Backup pair is incomplete or mismatched.")
    if claimed_size != dump_stat.st_size:
        raise RuntimeError("Backup pair is incomplete or mismatched: size claim does not match.")
    verified = metadata.get("restore_verified")
    restore_verified = "true" if verified is True else "false" if verified is False else "unknown"
    return {
        "dump_file": dump_name,
        "dump_identity": _backup_retention_stat_identity(dump_stat),
        "sidecar_identity": _backup_retention_stat_identity(metadata_stat),
        "sidecar_content_sha256": sidecar_digest,
        "claimed_size_bytes": claimed_size,
        "claimed_sha256": claimed_sha256,
        "restore_verified": restore_verified,
    }


def _backup_restore_status_class(metadata: dict[str, Any]) -> str:
    if "restore_verified" not in metadata:
        return "missing"
    value = metadata["restore_verified"]
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if type(value) in {int, float} and value == 0:
        return "zero"
    if isinstance(value, str) and value == "":
        return "empty_string"
    if isinstance(value, list) and not value:
        return "empty_array"
    if isinstance(value, dict) and not value:
        return "empty_object"
    return "truthy_or_unsupported"


def _unverified_backup_candidate_row(
    output_dir: pathlib.Path,
    dump_name: str,
    *,
    preserve_metadata: pathlib.Path,
    protected_dump_files: frozenset[str],
) -> dict[str, Any] | None:
    if (
        PROTECTED_BACKUP_DUMP_RE.fullmatch(dump_name) is None
        or dump_name in protected_dump_files
    ):
        raise RuntimeError("Unverified backup candidate name is not eligible.")
    dump_path = output_dir / dump_name
    metadata_path = dump_path.with_suffix(".json")
    if metadata_path == preserve_metadata:
        raise RuntimeError("New backup metadata cannot be an unverified prune candidate.")
    metadata_stat, metadata_digest, raw = _inventory_file(metadata_path)
    try:
        metadata = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Unverified backup sidecar changed before retention.") from exc
    if not isinstance(metadata, dict) or metadata.get("restore_verified"):
        return None
    if metadata.get("dump_file") != dump_name:
        raise RuntimeError("Refusing unverified backup metadata with a mismatched dump name.")
    status_class = _backup_restore_status_class(metadata)
    if status_class not in {
        "false",
        "missing",
        "null",
        "zero",
        "empty_string",
        "empty_array",
        "empty_object",
    }:
        raise RuntimeError("Unverified backup status is not eligible for retention removal.")
    dump_stat = _stable_backup_stat(dump_path)
    pair = _backup_retention_pair_row(output_dir, dump_name)
    if (
        _backup_retention_stat_identity(metadata_stat) != pair["sidecar_identity"]
        or metadata_digest != pair["sidecar_content_sha256"]
        or _backup_retention_stat_identity(dump_stat) != pair["dump_identity"]
        or pair["restore_verified"] == "true"
    ):
        raise RuntimeError("Unverified backup pair changed while being checked.")
    return {"pair": pair, "restore_status_class": status_class}


def _unverified_backup_candidate_order(
    output_dir: pathlib.Path,
    *,
    preserve_metadata: pathlib.Path,
    protected_dump_files: frozenset[str],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for metadata_path in output_dir.glob("platformdb-*.json"):
        if metadata_path == preserve_metadata:
            continue
        dump_name = metadata_path.with_suffix(".dump").name
        if dump_name in protected_dump_files:
            continue
        try:
            _metadata_stat, _metadata_digest, raw = _inventory_file(metadata_path)
            metadata = json.loads((raw or b"").decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RuntimeError):
            continue
        if not isinstance(metadata, dict) or metadata.get("restore_verified"):
            continue
        if (
            PROTECTED_BACKUP_DUMP_RE.fullmatch(dump_name) is None
            or metadata.get("dump_file") != dump_name
        ):
            raise RuntimeError("Refusing unverified backup metadata with a mismatched dump name.")
        row = _unverified_backup_candidate_row(
            output_dir,
            dump_name,
            preserve_metadata=preserve_metadata,
            protected_dump_files=protected_dump_files,
        )
        if row is None:
            raise RuntimeError("Unverified backup status changed while being checked.")
        rows.append(row)
    return tuple(sorted(rows, key=lambda row: row["pair"]["dump_file"]))


def _protected_backup_pair_rows(
    output_dir: pathlib.Path, dump_names: frozenset[str]
) -> list[dict[str, Any]]:
    return [
        _backup_retention_pair_row(output_dir, dump_name)
        for dump_name in sorted(dump_names)
    ]


def _verified_backup_candidate_order(
    output_dir: pathlib.Path,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for dump_path in output_dir.glob("platformdb-*.dump"):
        if PROTECTED_BACKUP_DUMP_RE.fullmatch(dump_path.name) is None:
            continue
        try:
            row = _backup_retention_pair_row(output_dir, dump_path.name)
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if row["restore_verified"] == "true":
            rows.append(row)
    return tuple(sorted(rows, key=lambda row: (row["dump_identity"][7], row["dump_file"])))


def _removed_backup_pair_rows(
    output_dir: pathlib.Path,
    removed_paths: list[str],
    protected_dump_files: frozenset[str],
    *,
    unverified_candidate_order: tuple[dict[str, Any], ...],
    verified_candidate_order: tuple[dict[str, Any], ...],
    selected_order: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(set(removed_paths)) != len(removed_paths):
        raise RuntimeError("Retention reported a duplicate removed backup entry.")
    dump_names: set[str] = set()
    sidecar_names: set[str] = set()
    for raw_path in removed_paths:
        path = pathlib.Path(raw_path)
        if path.parent != output_dir:
            raise RuntimeError("Retention removed a path outside the configured backup directory.")
        if PROTECTED_BACKUP_DUMP_RE.fullmatch(path.name):
            dump_names.add(path.name)
        elif re.fullmatch(r"platformdb-[0-9]{8}T[0-9]{6}Z\.json", path.name):
            sidecar_names.add(path.name)
        else:
            raise RuntimeError("Retention removed a noncanonical backup entry.")
    if dump_names != {name[:-5] + ".dump" for name in sidecar_names}:
        raise RuntimeError("Retention did not remove complete backup pairs.")
    if len(removed_paths) != 2 * len(dump_names):
        raise RuntimeError("Retention reported an incomplete backup-pair removal set.")
    if dump_names & protected_dump_files:
        raise RuntimeError("Retention removed a named protected backup pair.")
    verified_by_name = {row["dump_file"]: row for row in verified_candidate_order}
    selected_names = {row["dump_file"] for row in selected_order}
    retained_count = len(selected_order)
    expected_verified_removals = {
        row["dump_file"]
        for row in verified_candidate_order[:-retained_count]
        if row["dump_file"] not in protected_dump_files
    }
    unverified_by_name = {
        candidate["pair"]["dump_file"]: candidate
        for candidate in unverified_candidate_order
    }
    if len(unverified_by_name) != len(unverified_candidate_order):
        raise RuntimeError("Unverified retention inventory has duplicate pairs.")
    if set(unverified_by_name) & set(verified_by_name):
        raise RuntimeError("Backup pair was classified as both verified and unverified.")
    expected_dump_names = expected_verified_removals | set(unverified_by_name)
    if dump_names != expected_dump_names:
        raise RuntimeError("Retention removal set did not match eligible pre-delete pairs.")
    rows: list[dict[str, Any]] = []
    for name in sorted(dump_names):
        if name in unverified_by_name:
            candidate = unverified_by_name[name]
            pair = candidate["pair"]
            status_class = candidate["restore_status_class"]
            reason = "unverified"
            if pair["restore_verified"] == "true" or status_class == "true":
                raise RuntimeError("Verified backup entered the unverified removal set.")
        else:
            pair = verified_by_name.get(name)
            if pair is None or name in selected_names:
                raise RuntimeError("Verified backup removal is outside the retention prefix.")
            status_class = "true"
            reason = "verified-retention"
        rows.append(
            {
                "dump_file": name,
                "sidecar_file": name[:-5] + ".json",
                "reason": reason,
                "restore_status_class": status_class,
                "pair": pair,
            }
        )
    return rows


def valid_protected_retention_proof(
    value: Any, *, requested_keep: int, new_dump_file: str
) -> bool:
    """Validate the private, source-bound evidence for configured rotation."""

    proof_keys = {
        "schema_version",
        "status",
        "policy",
        "rotation_mode",
        "requested_keep",
        "effective_verified_keep",
        "manifest",
        "protected_pairs",
        "verified_selection",
        "removed_pair_count",
        "protected_removed_count",
        "removed_pairs",
        "removed_pairs_digest",
    }
    if not isinstance(value, dict) or set(value) != proof_keys:
        return False
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("status") != "pass"
        or value.get("policy") != "protected-plus-latest-verified-v1"
        or value.get("rotation_mode") != "rotate-existing"
        or type(value.get("requested_keep")) is not int
        or value["requested_keep"] != requested_keep
        or requested_keep < 1
        or type(value.get("effective_verified_keep")) is not int
        or value["effective_verified_keep"] != max(requested_keep, PROTECTED_BACKUP_MINIMUM_KEEP)
        or type(value.get("removed_pair_count")) is not int
        or value["removed_pair_count"] < 0
        or type(value.get("protected_removed_count")) is not int
        or value["protected_removed_count"] != 0
    ):
        return False

    def valid_digest(digest: Any) -> bool:
        return isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None

    def valid_identity(identity: Any) -> bool:
        return (
            isinstance(identity, list)
            and len(identity) == 9
            and all(type(field) is int and field >= 0 for field in identity)
        )

    def valid_manifest_snapshot(snapshot: Any) -> bool:
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "identity", "content_sha256", "protected_dump_files", "digest"
        }:
            return False
        names = snapshot.get("protected_dump_files")
        if (
            not valid_identity(snapshot.get("identity"))
            or not valid_digest(snapshot.get("content_sha256"))
            or not isinstance(names, list)
            or not names
            or any(
                not isinstance(name, str)
                or PROTECTED_BACKUP_DUMP_RE.fullmatch(name) is None
                for name in names
            )
            or names != sorted(set(names))
            or not valid_digest(snapshot.get("digest"))
        ):
            return False
        body = {key: snapshot[key] for key in ("identity", "content_sha256", "protected_dump_files")}
        return snapshot["digest"] == _retention_domain_digest("protected-backup-manifest", body)

    def valid_pair_row(row: Any, *, expected_verified: str | None = None) -> bool:
        if not isinstance(row, dict) or set(row) != {
            "dump_file",
            "dump_identity",
            "sidecar_identity",
            "sidecar_content_sha256",
            "claimed_size_bytes",
            "claimed_sha256",
            "restore_verified",
        }:
            return False
        if (
            not isinstance(row.get("dump_file"), str)
            or PROTECTED_BACKUP_DUMP_RE.fullmatch(row["dump_file"]) is None
            or not valid_identity(row.get("dump_identity"))
            or not valid_identity(row.get("sidecar_identity"))
            or not valid_digest(row.get("sidecar_content_sha256"))
            or type(row.get("claimed_size_bytes")) is not int
            or row["claimed_size_bytes"] <= 0
            or row["claimed_size_bytes"] != row["dump_identity"][6]
            or not valid_digest(row.get("claimed_sha256"))
            or not isinstance(row.get("restore_verified"), str)
            or row["restore_verified"] not in {"true", "false", "unknown"}
        ):
            return False
        return expected_verified is None or row["restore_verified"] == expected_verified

    manifest = value.get("manifest")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"before", "after", "unchanged"}
        or type(manifest.get("unchanged")) is not bool
        or manifest["unchanged"] is not True
        or not valid_manifest_snapshot(manifest.get("before"))
        or not valid_manifest_snapshot(manifest.get("after"))
        or manifest["before"] != manifest["after"]
    ):
        return False

    protected = value.get("protected_pairs")
    if (
        not isinstance(protected, dict)
        or set(protected) != {
            "count", "before", "after", "before_digest", "after_digest", "unchanged", "removed_count"
        }
    ):
        return False
    protected_before = protected.get("before")
    protected_after = protected.get("after")
    if (
        type(protected.get("count")) is not int
        or protected["count"] <= 0
        or not isinstance(protected_before, list)
        or not isinstance(protected_after, list)
        or any(not valid_pair_row(row) for row in protected_before)
        or any(not valid_pair_row(row) for row in protected_after)
        or [row["dump_file"] for row in protected_before]
        != sorted({row["dump_file"] for row in protected_before})
        or protected_before != protected_after
        or protected["count"] != len(protected_before)
        or [row["dump_file"] for row in protected_before]
        != manifest["before"]["protected_dump_files"]
        or type(protected.get("unchanged")) is not bool
        or protected["unchanged"] is not True
        or type(protected.get("removed_count")) is not int
        or protected["removed_count"] != 0
        or not valid_digest(protected.get("before_digest"))
        or not valid_digest(protected.get("after_digest"))
        or protected["before_digest"]
        != _retention_domain_digest("protected-backup-pairs", protected_before)
        or protected["after_digest"]
        != _retention_domain_digest("protected-backup-pairs", protected_after)
        or protected["before_digest"] != protected["after_digest"]
    ):
        return False

    selection = value.get("verified_selection")
    if (
        not isinstance(selection, dict)
        or set(selection) != {
            "available_count",
            "retained_count",
            "candidate_order",
            "selected_order",
            "retained_after",
            "candidate_digest",
            "selected_before_digest",
            "selected_after_digest",
        }
    ):
        return False
    candidates = selection.get("candidate_order")
    selected = selection.get("selected_order")
    retained_after = selection.get("retained_after")
    effective_keep = value["effective_verified_keep"]
    if (
        type(selection.get("available_count")) is not int
        or not isinstance(candidates, list)
        or selection["available_count"] != len(candidates)
        or any(not valid_pair_row(row, expected_verified="true") for row in candidates)
        or len({row["dump_file"] for row in candidates}) != len(candidates)
    ):
        return False
    expected_order = sorted(candidates, key=lambda row: (row["dump_identity"][7], row["dump_file"]))
    if candidates != expected_order:
        return False
    expected_retained_count = min(effective_keep, len(candidates))
    expected_selected = candidates[-expected_retained_count:] if expected_retained_count else []
    if (
        type(selection.get("retained_count")) is not int
        or selection["retained_count"] != expected_retained_count
        or not isinstance(selected, list)
        or selected != expected_selected
        or not isinstance(retained_after, list)
        or retained_after != expected_selected
        or not valid_digest(selection.get("candidate_digest"))
        or not valid_digest(selection.get("selected_before_digest"))
        or not valid_digest(selection.get("selected_after_digest"))
        or selection["candidate_digest"]
        != _retention_domain_digest("verified-backup-candidates", candidates)
        or selection["selected_before_digest"]
        != _retention_domain_digest("verified-backup-selection", selected)
        or selection["selected_after_digest"]
        != _retention_domain_digest("verified-backup-selection", retained_after)
        or selection["selected_before_digest"] != selection["selected_after_digest"]
        or new_dump_file not in {row["dump_file"] for row in selected}
    ):
        return False

    removed_pairs = value.get("removed_pairs")
    if (
        not isinstance(removed_pairs, list)
        or type(value.get("removed_pair_count")) is not int
        or value["removed_pair_count"] != len(removed_pairs)
        or any(
            not isinstance(row, dict)
            or set(row)
            != {
                "dump_file",
                "sidecar_file",
                "reason",
                "restore_status_class",
                "pair",
            }
            or not isinstance(row.get("dump_file"), str)
            or PROTECTED_BACKUP_DUMP_RE.fullmatch(row["dump_file"]) is None
            or row.get("sidecar_file") != row["dump_file"][:-5] + ".json"
            or not valid_pair_row(row.get("pair"))
            or row["pair"]["dump_file"] != row["dump_file"]
            or (
                row.get("reason") == "unverified"
                and (
                    row.get("restore_status_class")
                    not in (
                        "false",
                        "missing",
                        "null",
                        "zero",
                        "empty_string",
                        "empty_array",
                        "empty_object",
                    )
                    or row["pair"]["restore_verified"] == "true"
                    or (
                        row["restore_status_class"] == "false"
                        and row["pair"]["restore_verified"] != "false"
                    )
                    or (
                        row["restore_status_class"] != "false"
                        and row["pair"]["restore_verified"] != "unknown"
                    )
                )
            )
            or (
                row.get("reason") == "verified-retention"
                and (
                    row.get("restore_status_class") != "true"
                    or not valid_pair_row(row["pair"], expected_verified="true")
                )
            )
            or row.get("reason") not in ("unverified", "verified-retention")
            for row in removed_pairs
        )
        or [row["dump_file"] for row in removed_pairs]
        != sorted({row["dump_file"] for row in removed_pairs})
        or {row["dump_file"] for row in removed_pairs}
        & set(manifest["before"]["protected_dump_files"])
        or not valid_digest(value.get("removed_pairs_digest"))
        or value["removed_pairs_digest"]
        != _retention_domain_digest("backup-retention-removals", removed_pairs)
    ):
        return False
    verified_by_name = {row["dump_file"]: row for row in candidates}
    expected_verified_removed = {
        row["dump_file"]
        for row in candidates[:-expected_retained_count]
        if row["dump_file"] not in set(manifest["before"]["protected_dump_files"])
    }
    actual_verified_removed = {
        row["dump_file"]
        for row in removed_pairs
        if row["reason"] == "verified-retention"
    }
    if actual_verified_removed != expected_verified_removed:
        return False
    for row in removed_pairs:
        if row["reason"] == "verified-retention" and row["pair"] != verified_by_name.get(
            row["dump_file"]
        ):
            return False
    return True


def _assert_protected_retention_inputs_unchanged(
    manifest_path: pathlib.Path,
    before_manifest: dict[str, Any],
    output_dir: pathlib.Path,
    protected_dump_files: frozenset[str],
    before_pairs: list[dict[str, Any]],
) -> None:
    current_manifest = _protected_backup_manifest_snapshot(manifest_path)
    if current_manifest != before_manifest:
        raise RuntimeError("Protected backup manifest changed during restore verification.")
    current_pairs = _protected_backup_pair_rows(output_dir, protected_dump_files)
    if current_pairs != before_pairs:
        raise RuntimeError("Protected backup pair changed during restore verification.")


def _build_protected_retention_proof(
    *,
    manifest_path: pathlib.Path,
    manifest_before: dict[str, Any],
    output_dir: pathlib.Path,
    protected_dump_files: frozenset[str],
    protected_pairs_before: list[dict[str, Any]],
    unverified_candidate_order: tuple[dict[str, Any], ...],
    verified_candidate_order: tuple[dict[str, Any], ...],
    selected_order: list[dict[str, Any]],
    requested_keep: int,
    effective_keep: int,
    new_dump_file: str,
    removed_paths: list[str],
) -> dict[str, Any]:
    manifest_after = _protected_backup_manifest_snapshot(manifest_path)
    if manifest_after != manifest_before:
        raise RuntimeError("Protected backup manifest changed during retention.")
    protected_pairs_after = _protected_backup_pair_rows(output_dir, protected_dump_files)
    if protected_pairs_after != protected_pairs_before:
        raise RuntimeError("Protected backup pair changed during retention.")

    verified_after_order = _verified_backup_candidate_order(output_dir)
    after_by_name = {row["dump_file"]: row for row in verified_after_order}
    selected_names = {row["dump_file"] for row in selected_order}
    old_protected_verified_names = {
        row["dump_file"]
        for row in verified_candidate_order
        if row["dump_file"] in protected_dump_files and row["dump_file"] not in selected_names
    }
    expected_after_names = selected_names | old_protected_verified_names
    if set(after_by_name) != expected_after_names:
        raise RuntimeError("Verified backup retention did not match the protected latest selection.")
    retained_after = [after_by_name[row["dump_file"]] for row in selected_order]
    if retained_after != selected_order:
        raise RuntimeError("Selected verified backup pair changed during retention.")
    if new_dump_file not in selected_names:
        raise RuntimeError("New verified backup is outside the retained latest selection.")

    remaining_unverified = _unverified_backup_candidate_order(
        output_dir,
        preserve_metadata=output_dir / f"{new_dump_file[:-5]}.json",
        protected_dump_files=protected_dump_files,
    )
    if remaining_unverified:
        raise RuntimeError("Unverified backup retention left eligible pairs behind.")

    removed_pairs = _removed_backup_pair_rows(
        output_dir,
        removed_paths,
        protected_dump_files,
        unverified_candidate_order=unverified_candidate_order,
        verified_candidate_order=verified_candidate_order,
        selected_order=selected_order,
    )
    protected_names = set(manifest_before["protected_dump_files"])
    protected_removed_count = sum(
        1 for row in removed_pairs if row["dump_file"] in protected_names
    )
    if protected_removed_count:
        raise RuntimeError("Retention removed a named protected backup pair.")

    protected_before_digest = _retention_domain_digest(
        "protected-backup-pairs", protected_pairs_before
    )
    protected_after_digest = _retention_domain_digest(
        "protected-backup-pairs", protected_pairs_after
    )
    selected_before_digest = _retention_domain_digest(
        "verified-backup-selection", selected_order
    )
    selected_after_digest = _retention_domain_digest(
        "verified-backup-selection", retained_after
    )
    proof = {
        "schema_version": 1,
        "status": "pass",
        "policy": "protected-plus-latest-verified-v1",
        "rotation_mode": "rotate-existing",
        "requested_keep": requested_keep,
        "effective_verified_keep": effective_keep,
        "manifest": {
            "before": manifest_before,
            "after": manifest_after,
            "unchanged": True,
        },
        "protected_pairs": {
            "count": len(protected_pairs_before),
            "before": protected_pairs_before,
            "after": protected_pairs_after,
            "before_digest": protected_before_digest,
            "after_digest": protected_after_digest,
            "unchanged": True,
            "removed_count": protected_removed_count,
        },
        "verified_selection": {
            "available_count": len(verified_candidate_order),
            "retained_count": len(selected_order),
            "candidate_order": list(verified_candidate_order),
            "selected_order": selected_order,
            "retained_after": retained_after,
            "candidate_digest": _retention_domain_digest(
                "verified-backup-candidates", list(verified_candidate_order)
            ),
            "selected_before_digest": selected_before_digest,
            "selected_after_digest": selected_after_digest,
        },
        "removed_pair_count": len(removed_pairs),
        "protected_removed_count": protected_removed_count,
        "removed_pairs": removed_pairs,
        "removed_pairs_digest": _retention_domain_digest(
            "backup-retention-removals", removed_pairs
        ),
    }
    if not valid_protected_retention_proof(
        proof, requested_keep=requested_keep, new_dump_file=new_dump_file
    ):
        raise RuntimeError("Protected backup retention proof is invalid.")
    return proof


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


def _scratch_synthetic_expected_rows() -> list[list[Any]]:
    """Build fixed row digests without retaining the bulk payload in memory."""
    expected: list[list[Any]] = []
    seed = SCRATCH_SYNTHETIC_PAYLOAD_SEED
    for row_id in range(1, SCRATCH_SYNTHETIC_PAYLOAD_ROWS + 1):
        digest = hashlib.md5(usedforsecurity=False)
        digest.update(f"{row_id:08d}".encode("ascii"))
        for block in range(1, SCRATCH_SYNTHETIC_PAYLOAD_BLOCKS + 1):
            block_digest = hashlib.md5(
                f"{seed}:{row_id}:{block}".encode("ascii"),
                usedforsecurity=False,
            ).hexdigest()
            digest.update(block_digest.encode("ascii"))
        tail = hashlib.md5(
            f"{seed}:tail:{row_id}".encode("ascii"), usedforsecurity=False
        ).hexdigest()[:24]
        digest.update(tail.encode("ascii"))
        expected.append([row_id, f"row-{row_id:08d}", digest.hexdigest()])
    return expected


def _scratch_synthetic_restore_payload_state(
    target: DatabaseTarget,
    *,
    expected_rows: list[list[Any]],
    psql_binary: str = "psql",
) -> dict[str, Any]:
    """Check exact synthetic bulk bytes and row digests at one phase boundary."""
    presence = run_command(
        [
            psql_binary,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            *connection_args(target),
            "--command",
            "SELECT to_regclass('platform.synthetic_restore_probe') IS NOT NULL;",
        ],
        target=target,
        capture_output=True,
    )
    if presence.stdout.strip() == "f":
        return {"table_present": False}
    if presence.stdout.strip() != "t":
        raise RuntimeError("Synthetic restore payload presence check was invalid.")
    result = run_command(
        [
            psql_binary,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            *connection_args(target),
            "--command",
            """
            SELECT jsonb_build_object(
                'table_present', true,
                'rows', (
                    SELECT COALESCE(
                        jsonb_agg(jsonb_build_array(id, label, md5(search_text)) ORDER BY id),
                        '[]'::jsonb
                    ) FROM platform.synthetic_restore_probe
                ),
                'row_count', count(*),
                'payload_bytes', COALESCE(sum(octet_length(search_text)), 0),
                'min_payload_bytes', min(octet_length(search_text)),
                'max_payload_bytes', max(octet_length(search_text)),
                'unique_prefix_count', count(DISTINCT left(search_text, 8)),
                'prefixes_match_ids', COALESCE(
                    bool_and(left(search_text, 8) = lpad(id::text, 8, '0')), false
                )
            )::text
            FROM platform.synthetic_restore_probe;
            """,
        ],
        target=target,
        capture_output=True,
    )
    try:
        actual = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Synthetic restore payload snapshot was invalid.") from exc
    expected = {
        "table_present": True,
        "rows": expected_rows,
        "row_count": SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
        "payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
        "min_payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW,
        "max_payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW,
        "unique_prefix_count": SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
        "prefixes_match_ids": True,
    }
    if actual != expected:
        raise RuntimeError("Synthetic restore phase changed the exact bulk fixture payload.")
    return actual


def _scratch_synthetic_restore_snapshot(
    target: DatabaseTarget,
    *,
    expected_rows: list[list[Any]] | None = None,
    psql_binary: str = "psql",
) -> dict[str, Any]:
    """Assert the fixed bulk fixture, index path, and Alembic marker."""
    result = run_command(
        [
            psql_binary,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            *connection_args(target),
            "--command",
            """
            SELECT jsonb_build_object(
                'tables', (
                    SELECT COALESCE(jsonb_agg(table_name ORDER BY table_name), '[]'::jsonb)
                    FROM information_schema.tables
                    WHERE table_schema = 'platform' AND table_type = 'BASE TABLE'
                ),
                'columns', (
                    SELECT COALESCE(
                        jsonb_agg(
                            jsonb_build_array(column_name, data_type, is_nullable)
                            ORDER BY ordinal_position
                        ),
                        '[]'::jsonb
                    )
                    FROM information_schema.columns
                    WHERE table_schema = 'platform'
                      AND table_name = 'synthetic_restore_probe'
                ),
                'primary_key_columns', (
                    SELECT COALESCE(jsonb_agg(attribute.attname ORDER BY key_column.ordinality), '[]'::jsonb)
                    FROM pg_constraint AS constraint_row
                    JOIN LATERAL unnest(constraint_row.conkey)
                        WITH ORDINALITY AS key_column(attnum, ordinality) ON TRUE
                    JOIN pg_attribute AS attribute
                      ON attribute.attrelid = constraint_row.conrelid
                     AND attribute.attnum = key_column.attnum
                    WHERE constraint_row.conrelid = 'platform.synthetic_restore_probe'::regclass
                      AND constraint_row.contype = 'p'
                ),
                'rows', (
                    SELECT COALESCE(
                        jsonb_agg(jsonb_build_array(id, label, md5(search_text)) ORDER BY id),
                        '[]'::jsonb
                    )
                    FROM platform.synthetic_restore_probe
                ),
                'row_count', (
                    SELECT count(*) FROM platform.synthetic_restore_probe
                ),
                'payload_bytes', (
                    SELECT COALESCE(sum(octet_length(search_text)), 0)
                    FROM platform.synthetic_restore_probe
                ),
                'min_payload_bytes', (
                    SELECT min(octet_length(search_text))
                    FROM platform.synthetic_restore_probe
                ),
                'max_payload_bytes', (
                    SELECT max(octet_length(search_text))
                    FROM platform.synthetic_restore_probe
                ),
                'unique_prefix_count', (
                    SELECT count(DISTINCT left(search_text, 8))
                    FROM platform.synthetic_restore_probe
                ),
                'prefixes_match_ids', (
                    SELECT COALESCE(bool_and(left(search_text, 8) = lpad(id::text, 8, '0')), false)
                    FROM platform.synthetic_restore_probe
                ),
                'like_match_ids', (
                    SELECT COALESCE(jsonb_agg(id ORDER BY id), '[]'::jsonb)
                    FROM platform.synthetic_restore_probe
                    WHERE search_text LIKE '00000001%'
                ),
                'alembic_versions', (
                    SELECT COALESCE(jsonb_agg(version_num ORDER BY version_num), '[]'::jsonb)
                    FROM public.alembic_version
                ),
                'pg_trgm_installed', EXISTS (
                    SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'
                ),
                'pg_trgm_functional',
                    similarity('restore fixture alpha', 'restore fixture alpha') = 1.0,
                'gin_trgm_index_valid', EXISTS (
                    SELECT 1
                    FROM pg_index AS index_row
                    JOIN pg_class AS index_class ON index_class.oid = index_row.indexrelid
                    JOIN pg_am AS access_method ON access_method.oid = index_class.relam
                    JOIN pg_opclass AS operator_class
                      ON operator_class.oid = index_row.indclass[0]
                    WHERE index_row.indrelid = 'platform.synthetic_restore_probe'::regclass
                      AND index_class.relname = 'synthetic_restore_probe_search_trgm_idx'
                      AND index_class.relnamespace = 'platform'::regnamespace
                      AND index_row.indisvalid
                      AND index_row.indisready
                      AND index_row.indnatts = 1
                      AND index_row.indnkeyatts = 1
                      AND access_method.amname = 'gin'
                      AND operator_class.opcname = 'gin_trgm_ops'
                      AND operator_class.opcnamespace = 'public'::regnamespace
                      AND pg_get_indexdef(index_row.indexrelid, 1, true) = 'search_text'
                )
            )::text;
            """,
        ],
        target=target,
        capture_output=True,
    )
    try:
        actual = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Synthetic restore returned an invalid logical snapshot.") from exc
    actual_plan_result = run_command(
        [
            psql_binary,
            "--no-psqlrc",
            "--quiet",
            "--tuples-only",
            "--no-align",
            *connection_args(target),
            "--command",
            "SET enable_seqscan = off; EXPLAIN (FORMAT JSON) "
            "SELECT id FROM platform.synthetic_restore_probe "
            "WHERE search_text LIKE '00000001%';",
        ],
        target=target,
        capture_output=True,
    )
    try:
        plan_rows = json.loads(actual_plan_result.stdout.strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Synthetic restore returned an invalid index plan.") from exc

    def has_expected_index_scan(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        if (
            node.get("Node Type") == "Bitmap Index Scan"
            and node.get("Index Name") == SCRATCH_SYNTHETIC_TRGM_INDEX
        ):
            return True
        return any(
            has_expected_index_scan(child)
            for child in node.get("Plans", [])
            if isinstance(child, dict)
        )

    if (
        not isinstance(plan_rows, list)
        or len(plan_rows) != 1
        or not isinstance(plan_rows[0], dict)
        or not has_expected_index_scan(plan_rows[0].get("Plan"))
    ):
        raise RuntimeError("Synthetic pg_trgm lookup did not use its fixed GIN index.")
    expected = {
        "tables": ["synthetic_restore_probe"],
        "columns": [
            ["id", "integer", "NO"],
            ["label", "text", "NO"],
            ["search_text", "text", "NO"],
        ],
        "primary_key_columns": ["id"],
        "rows": expected_rows if expected_rows is not None else _scratch_synthetic_expected_rows(),
        "row_count": SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
        "payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
        "min_payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW,
        "max_payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW,
        "unique_prefix_count": SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
        "prefixes_match_ids": True,
        "like_match_ids": [1],
        "alembic_versions": [SCRATCH_SYNTHETIC_ALEMBIC_REVISION],
        "pg_trgm_installed": True,
        "pg_trgm_functional": True,
        "gin_trgm_index_valid": True,
    }
    if actual != expected:
        raise RuntimeError("Synthetic PostgreSQL restore did not reproduce its exact fixture.")
    canonical = json.dumps(actual, separators=(",", ":"), sort_keys=True).encode("ascii")
    return {"logical_sha256": hashlib.sha256(canonical).hexdigest()}


def perform_restore_drill(
    dump_path: pathlib.Path,
    *,
    app_target: DatabaseTarget,
    admin_target: DatabaseTarget | None,
    timestamp_slug: str,
    archive_stdin: BinaryIO | None = None,
    restore_as_postgres: bool = False,
    restore_no_tablespaces: bool = False,
    restore_single_transaction: bool = False,
    psql_binary: str = "psql",
    pg_restore_binary: str = "pg_restore",
    synthetic_validation_result: dict[str, Any] | None = None,
    synthetic_expected_rows: list[list[Any]] | None = None,
    synthetic_phase_validator: Callable[[str, str], None] | None = None,
    resource_guard: Callable[[], None] | None = None,
    capture_private_output: bool = False,
    phase_observer: Callable[[str, str], None] | None = None,
) -> int:
    if type(restore_single_transaction) is not bool:
        raise ValueError("Restore transaction mode must be a boolean.")
    if synthetic_validation_result is not None and (
        type(synthetic_validation_result) is not dict or synthetic_validation_result
    ):
        raise ValueError("Synthetic restore validation result must be an empty dictionary.")
    if synthetic_validation_result is None and (
        synthetic_expected_rows is not None or synthetic_phase_validator is not None
    ):
        raise ValueError("Synthetic restore phase checks require synthetic validation mode.")
    if synthetic_validation_result is not None and (
        not isinstance(synthetic_expected_rows, list)
        or len(synthetic_expected_rows) != SCRATCH_SYNTHETIC_PAYLOAD_ROWS
        or not callable(synthetic_phase_validator)
    ):
        raise ValueError("Synthetic restore phase checks are incomplete.")
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
                    psql_binary,
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
                psql_binary,
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
            phase_name = (
                "restore_platform"
                if selector == ("--schema=platform",)
                else "restore_public"
            )
            failure_stage = phase_name
            if synthetic_phase_validator is not None:
                synthetic_phase_validator(phase_name, "before")
            restore_command = [
                pg_restore_binary,
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                *selector,
                *connection_args(restore_target),
            ]
            if restore_no_tablespaces:
                restore_command.append("--no-tablespaces")
            if restore_single_transaction:
                restore_command.append("--single-transaction")
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
            if synthetic_phase_validator is not None:
                synthetic_phase_validator(phase_name, "after")
        failure_stage = "validate_table_count"
        table_count_result = run_command(
            [
                psql_binary,
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
                psql_binary,
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
                    psql_binary,
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
                psql_binary,
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
        if synthetic_validation_result is not None:
            synthetic_validation_result.update(
                _scratch_synthetic_restore_snapshot(
                    restore_target,
                    expected_rows=synthetic_expected_rows,
                    psql_binary=psql_binary,
                )
            )
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
                    psql_binary=psql_binary,
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
    psql_binary: str = "psql",
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
            psql_binary,
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
        psql_binary,
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


def _scratch_mount_snapshot(
    path: pathlib.Path,
    *,
    backend: str = SCRATCH_BACKEND_TMPFS_V1,
    expected_owner: tuple[int, int] = (0, 0),
) -> dict[str, int | str]:
    if backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        return _scratch_disk_mount_snapshot(path, expected_owner=expected_owner)
    if backend != SCRATCH_BACKEND_TMPFS_V1:
        raise RuntimeError("Scratch restore backend is not supported.")
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


def _scratch_prepare_disk_root_for_postgres(
    root: pathlib.Path, *, postgres_uid: int, postgres_gid: int
) -> None:
    """Validate the private image root, then grant only the postgres owner access."""
    before = root.lstat()
    before_identity = (before.st_dev, before.st_ino)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or stat.S_IMODE(before.st_mode) != 0o700
        or type(postgres_uid) is not int
        or type(postgres_gid) is not int
        or postgres_uid <= 0
        or postgres_gid < 0
    ):
        raise RuntimeError("Scratch ext4 root did not meet its root:root mode 0700 pre-PG contract.")
    try:
        os.chown(root, postgres_uid, postgres_gid, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("Scratch ext4 root could not be assigned to the exact postgres identity.") from exc
    after = root.lstat()
    if (
        not stat.S_ISDIR(after.st_mode)
        or (after.st_dev, after.st_ino) != before_identity
        or after.st_uid != postgres_uid
        or after.st_gid != postgres_gid
        or stat.S_IMODE(after.st_mode) != 0o700
    ):
        raise RuntimeError("Scratch ext4 root identity changed during the postgres ownership transition.")


def _scratch_sysfs_loop_device_predicates(
    device: str,
) -> tuple[str | None, dict[str, bool]]:
    """Resolve one mountinfo major:minor through sysfs to a loop device.

    The caller's controller separately binds that same major:minor, worker PID,
    invocation, and mountpoint to the exact admitted image inode before it
    sends the start token and throughout the worker lifetime.  The mountinfo
    SOURCE field is filesystem-specific and is not a device identity.
    """
    predicates = {
        "device_major_minor_valid": bool(
            re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", device)
        ),
        "sysfs_mount_verified": False,
        "sysfs_device_resolved": False,
        "sysfs_loop_device": False,
        "sysfs_device_number_matches": False,
        "sysfs_backing_file_present": False,
    }
    if not predicates["device_major_minor_valid"]:
        return "device_major_minor_invalid", predicates

    try:
        sysfs_root = SCRATCH_SYS_DEV_BLOCK_ROOT
        root_info = sysfs_root.lstat()
        mountpoint, _mount_device, fs_type, _source, mount_root, _options = (
            _scratch_mountinfo_for(sysfs_root)
        )
    except (OSError, RuntimeError):
        return "sysfs_mount_unverified", predicates
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or fs_type != "sysfs"
        or mountpoint != "/sys"
        or mount_root != "/"
    ):
        return "sysfs_mount_unverified", predicates
    predicates["sysfs_mount_verified"] = True

    link = sysfs_root / device
    try:
        link_info = link.lstat()
        devices_root = SCRATCH_SYS_DEVICES_ROOT.resolve(strict=True)
        resolved = link.resolve(strict=True)
        resolved_info = resolved.stat()
    except (OSError, RuntimeError):
        return "sysfs_device_unavailable", predicates
    if (
        not stat.S_ISLNK(link_info.st_mode)
        or not resolved.is_relative_to(devices_root)
        or resolved.parent.name != "block"
        or re.fullmatch(r"loop[0-9]{1,3}", resolved.name) is None
        or not stat.S_ISDIR(resolved_info.st_mode)
    ):
        predicates["sysfs_device_resolved"] = True
        return "sysfs_not_loop_device", predicates
    predicates["sysfs_device_resolved"] = True
    predicates["sysfs_loop_device"] = True

    try:
        dev_path = resolved / "dev"
        dev_info = dev_path.lstat()
        dev_raw = dev_path.read_bytes()
        if (
            not stat.S_ISREG(dev_info.st_mode)
            or len(dev_raw) > 64
        ):
            return "sysfs_device_number_mismatch", predicates
        observed_device = dev_raw.decode("ascii", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return "sysfs_device_number_mismatch", predicates
    if re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", observed_device) is None:
        return "sysfs_device_number_mismatch", predicates
    predicates["sysfs_device_number_matches"] = observed_device == device
    if not predicates["sysfs_device_number_matches"]:
        return "sysfs_device_number_mismatch", predicates

    try:
        backing_path = resolved / "loop" / "backing_file"
        backing_info = backing_path.lstat()
        backing_raw = backing_path.read_bytes()
        if (
            not stat.S_ISREG(backing_info.st_mode)
            or not backing_raw
            or len(backing_raw) > 4096
            or b"\x00" in backing_raw
            or not backing_raw.startswith(b"/")
        ):
            return "sysfs_not_loop_device", predicates
    except OSError:
        return "sysfs_not_loop_device", predicates
    predicates["sysfs_backing_file_present"] = True
    return None, predicates


def _scratch_disk_mount_snapshot(
    path: pathlib.Path,
    *,
    expected_owner: tuple[int, int] = (0, 0),
) -> dict[str, int | str]:
    def fail(
        reason: str,
        message: str,
        cause: BaseException | None = None,
        predicates: dict[str, bool] | None = None,
    ) -> None:
        error = RuntimeError(message)
        setattr(error, "scratch_disk_mount_failure_reason", reason)
        if predicates is not None:
            setattr(error, "scratch_disk_mount_predicates", predicates)
        if cause is not None:
            raise error from cause
        raise error

    try:
        mountpoint, device, fs_type, source, mount_root, mount_options = _scratch_mountinfo_for(path)
        mount_stat = path.lstat()
    except (OSError, RuntimeError) as exc:
        reason = getattr(exc, "scratch_disk_mount_failure_reason", "mount_metadata")
        if type(reason) is not str or reason not in _SCRATCH_DISK_MOUNT_FAILURE_REASONS:
            reason = "mount_metadata"
        fail(reason, "Scratch mount metadata could not be verified.", exc)
    options = set(mount_options.split(","))
    device_reason, device_predicates = _scratch_sysfs_loop_device_predicates(device)
    mount_predicates = {
        "fs_ext4": fs_type == "ext4",
        "source_canonical": source == source.strip(),
        **device_predicates,
        "root_expected": mount_root == "/",
        "target_expected": mountpoint == str(path),
        "option_rw": "rw" in options,
        "option_nosuid": "nosuid" in options,
        "option_nodev": "nodev" in options,
        "option_noexec": "noexec" in options,
    }
    mount_failures = (
        ("filesystem_not_ext4", "fs_ext4"),
        ("source_noncanonical", "source_canonical"),
        ("mount_root_not_global", "root_expected"),
        ("mountpoint_mismatch", "target_expected"),
    )
    for reason, predicate in mount_failures:
        if not mount_predicates[predicate]:
            fail(
                reason,
                "Isolated restore filesystem is not the pinned private ext4 image mount.",
                predicates=mount_predicates,
            )
    if device_reason is not None:
        fail(
            device_reason,
            "Isolated restore filesystem device is not a verified loop device.",
            predicates=mount_predicates,
        )
    if not all(mount_predicates[key] for key in (
        "option_rw", "option_nosuid", "option_nodev", "option_noexec"
    )):
        fail(
            "mount_options_missing",
            "Isolated restore filesystem is not the pinned private ext4 image mount.",
            predicates=mount_predicates,
        )
    root_predicates = {
        "root_uid": mount_stat.st_uid == expected_owner[0],
        "root_gid": mount_stat.st_gid == expected_owner[1],
        "root_mode_0700": stat.S_IMODE(mount_stat.st_mode) == 0o700,
    }
    if not all(root_predicates.values()):
        reason = (
            "root_uid_mismatch" if not root_predicates["root_uid"]
            else "root_gid_mismatch" if not root_predicates["root_gid"]
            else "root_mode_mismatch"
        )
        fail(reason, "Isolated restore filesystem root identity is unsafe.", predicates=root_predicates)
    try:
        values = os.statvfs(path)
    except OSError as exc:
        fail("statvfs_unavailable", "Isolated ext4 filesystem capacity could not be verified.", exc)
    total = values.f_blocks * values.f_frsize
    free = values.f_bavail * values.f_frsize
    capacity_predicates = {
        "total_capacity_bounded": SCRATCH_DISK_USABLE_MIN_BYTES <= total <= SCRATCH_DISK_IMAGE_BYTES,
        "free_capacity_above_stop": free > SCRATCH_TMPFS_STOP_BYTES,
    }
    if not all(capacity_predicates.values()):
        reason = (
            "total_capacity_out_of_bounds" if not capacity_predicates["total_capacity_bounded"]
            else "free_below_stop"
        )
        fail(
            reason,
            "Isolated ext4 scratch filesystem is below its bounded usable capacity.",
            predicates=capacity_predicates,
        )
    return {
        "total_bytes": total,
        "free_bytes": free,
        "filesystem": "ext4",
        "device": device,
    }


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
    """Return (pid, ppid, VmRSS bytes) for a bounded, stable process sample."""
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


def _scratch_proc_status_observation(
    pid: int, *, require_helper_executable: bool = True
) -> tuple[int, int, int] | None:
    """Read stable status using the established parser, optionally without exe lookup."""
    if require_helper_executable:
        return _scratch_proc_status_sample(pid)
    if type(pid) is not int or pid <= 1:
        return None
    status_path = pathlib.Path(f"/proc/{pid}/status")
    try:
        with status_path.open("rb") as status_file:
            raw_before = status_file.read(SCRATCH_PROC_STATUS_MAX_BYTES + 1)
        if len(raw_before) > SCRATCH_PROC_STATUS_MAX_BYTES:
            return None
        parsed_before = _parse_scratch_proc_status(raw_before)
        if parsed_before is None or parsed_before[0] != pid:
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


def _scratch_private_cgroup_values(
    pid_text: str, control_group: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one identity-joined v2 cgroup snapshot for existing private diagnostics."""
    if (
        not re.fullmatch(r"[1-9][0-9]{0,9}", pid_text)
        or not re.fullmatch(
            r"/system\.slice/oldsparky-backup-restore-[0-9a-f]{24}\.service",
            control_group,
        )
    ):
        return None, "identity_drift"
    membership_before = _scratch_private_proc_file(pid_text, "cgroup")
    if membership_before is None:
        return None, "sample_unavailable"
    try:
        if not any(
            row.split(":", 2) == ["0", "", control_group]
            for row in membership_before.decode("ascii").splitlines()
        ):
            return None, "identity_drift"
    except UnicodeError:
        return None, "parse_invalid"

    root_fd = system_slice_fd = cgroup_fd = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        root_fd = os.open("/sys/fs/cgroup", flags)
        root_before = os.fstat(root_fd)
        system_slice_fd = os.open("system.slice", flags, dir_fd=root_fd)
        system_slice_before = os.fstat(system_slice_fd)
        leaf_name = control_group.removeprefix("/system.slice/")
        if "/" in leaf_name:
            return None, "identity_drift"
        cgroup_fd = os.open(leaf_name, flags, dir_fd=system_slice_fd)
        cgroup_before = os.fstat(cgroup_fd)
        current = _scratch_private_counter(
            _scratch_private_cgroup_file(cgroup_fd, "memory.current")
        )
        stat_values = _scratch_private_key_values(
            _scratch_private_cgroup_file(cgroup_fd, "memory.stat"),
            {"anon", "file", "shmem"},
        )
        event_values = _scratch_private_key_values(
            _scratch_private_cgroup_file(cgroup_fd, "memory.events"),
            {"high", "max", "oom", "oom_kill"},
        )
        if current is None or stat_values is None or event_values is None:
            return None, "parse_invalid"
        cgroup_after = os.fstat(cgroup_fd)
        system_slice_after = os.fstat(system_slice_fd)
        root_after = os.fstat(root_fd)
        system_slice_named = os.stat(
            "system.slice", dir_fd=root_fd, follow_symlinks=False
        )
        cgroup_named = os.stat(
            leaf_name, dir_fd=system_slice_fd, follow_symlinks=False
        )
        membership_after = _scratch_private_proc_file(pid_text, "cgroup")
        if membership_after is None:
            return None, "sample_unavailable"
        try:
            still_member = any(
                row.split(":", 2) == ["0", "", control_group]
                for row in membership_after.decode("ascii").splitlines()
            )
        except UnicodeError:
            return None, "parse_invalid"
        if (
            not still_member
            or (root_before.st_dev, root_before.st_ino)
            != (root_after.st_dev, root_after.st_ino)
            or (system_slice_before.st_dev, system_slice_before.st_ino)
            != (system_slice_after.st_dev, system_slice_after.st_ino)
            or (system_slice_before.st_dev, system_slice_before.st_ino)
            != (system_slice_named.st_dev, system_slice_named.st_ino)
            or (cgroup_before.st_dev, cgroup_before.st_ino)
            != (cgroup_after.st_dev, cgroup_after.st_ino)
            or (cgroup_before.st_dev, cgroup_before.st_ino)
            != (cgroup_named.st_dev, cgroup_named.st_ino)
        ):
            return None, "identity_drift"
        return {
            "memory_current_bytes": current,
            "anon_bytes": stat_values["anon"],
            "file_bytes": stat_values["file"],
            "shmem_bytes": stat_values["shmem"],
            "events": event_values,
        }, None
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return None, "sample_unavailable"
    finally:
        for descriptor in (cgroup_fd, system_slice_fd, root_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


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
        if (
            not controller_bound
            or worker_start_before != worker_start_time
            or worker_start_after != worker_start_time
            or worker_sample is None
            or worker_sample[0] != int(worker_pid_text)
        ):
            return unknown("identity_drift")
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return unknown("sample_unavailable")

    cgroup_values, cgroup_reason = _scratch_private_cgroup_values(
        worker_pid_text, control_group
    )
    if cgroup_values is None:
        return unknown(cgroup_reason or "sample_unavailable")
    try:
        if (
            _scratch_process_start_time(worker_pid_text) != worker_start_time
            or _scratch_process_start_time(str(controller_pid)) != controller_start_before
            or os.getpid() != controller_pid
        ):
            return unknown("identity_drift")
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return unknown("sample_unavailable")

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
            "memory_current_bytes": cgroup_values["memory_current_bytes"],
            "anon_bytes": cgroup_values["anon_bytes"],
            "file_bytes": cgroup_values["file_bytes"],
            "shmem_bytes": cgroup_values["shmem_bytes"],
            "events": cgroup_values["events"],
        },
    }
    if not valid_scratch_resource_attribution(result):
        return unknown("internal_error")
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > SCRATCH_RESOURCE_ATTRIBUTION_MAX_BYTES:
        return unknown("cap_exceeded")
    return result


class _ScratchAllocationReadError(RuntimeError):
    def __init__(
        self, reason: str, *, partial_tree: dict[str, list[int] | None] | None = None
    ) -> None:
        self.reason = reason if reason in _SCRATCH_ALLOCATION_UNKNOWN_REASONS else "internal_error"
        self.partial_tree = partial_tree
        super().__init__(self.reason)


def _scratch_allocation_read_fd(descriptor: int, cap: int) -> bytes:
    chunks = bytearray()
    while len(chunks) <= cap:
        block = os.read(descriptor, min(8192, cap + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
    if len(chunks) > cap:
        raise _ScratchAllocationReadError("entry_cap")
    return bytes(chunks)


def _scratch_allocation_mount_row(raw: bytes, target: str) -> tuple[int, int, int] | None:
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeError:
        return None
    found: list[tuple[int, int, int]] = []
    for line in lines:
        left, separator, right = line.partition(" - ")
        fields = left.split()
        post = right.split()
        if not separator or len(fields) < 6 or len(post) < 3:
            continue
        mountpoint = re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            fields[4],
        )
        if mountpoint != target:
            continue
        device = fields[2].split(":", 1)
        if (
            len(device) != 2
            or not device[0].isdecimal()
            or not device[1].isdecimal()
            or fields[3] != "/"
            or post[0] != "tmpfs"
            or "rw" not in fields[5].split(",")
            or not fields[0].isdecimal()
        ):
            return None
        found.append((int(fields[0]), int(device[0]), int(device[1])))
    return found[0] if len(found) == 1 else None


def _scratch_allocation_tree(root_fd: int, root_dev: int, deadline: float) -> dict[str, list[int]]:
    """Aggregate PGDATA, base and pg_wal with a best-effort metadata-only walk.

    The unique-node bound includes the PGDATA root itself. File sizes and
    directory membership are observations over the scan interval; ordinary
    PostgreSQL writes do not count as identity drift. Deadline checks happen
    between filesystem calls and cannot interrupt a blocking kernel call.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    data_fd = os.open("data", flags, dir_fd=root_fd)
    open_fds = {data_fd}
    counters = {
        name: [0, 0, 0, 0]
        for name in ("data", "base", "pg_wal")
    }
    seen = {"base": False, "pg_wal": False}
    visited_nodes = 1

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid,
            info.st_gid, stat.S_IMODE(info.st_mode),
        )

    def add_file(buckets: frozenset[str], info: os.stat_result) -> None:
        for bucket in buckets:
            row = counters[bucket]
            row[0] += info.st_size
            row[1] += info.st_blocks * 512
            row[2] += 1

    try:
        root_info = os.fstat(data_fd)
        root_named = os.stat("data", dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_dev != root_dev
            or identity(root_info) != identity(root_named)
        ):
            raise _ScratchAllocationReadError("identity_drift")
        counters["data"][3] = 1
        if visited_nodes > SCRATCH_ALLOCATION_ENTRY_MAX:
            raise _ScratchAllocationReadError("entry_cap")

        def walk(directory_fd: int, depth: int, buckets: frozenset[str]) -> None:
            nonlocal visited_nodes
            if time.monotonic() >= deadline:
                raise _ScratchAllocationReadError("deadline")
            before = os.fstat(directory_fd)
            try:
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline:
                            raise _ScratchAllocationReadError("deadline")
                        if visited_nodes >= SCRATCH_ALLOCATION_ENTRY_MAX:
                            raise _ScratchAllocationReadError("entry_cap")
                        visited_nodes += 1
                        name = entry.name
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if info.st_dev != root_dev:
                            raise _ScratchAllocationReadError("mount_identity")
                        if stat.S_ISREG(info.st_mode):
                            file_fd = os.open(
                                name,
                                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=directory_fd,
                            )
                            try:
                                opened_info = os.fstat(file_fd)
                                named_info = os.stat(
                                    name, dir_fd=directory_fd, follow_symlinks=False
                                )
                                if (
                                    identity(info) != identity(opened_info)
                                    or identity(info) != identity(named_info)
                                ):
                                    raise _ScratchAllocationReadError("identity_drift")
                                add_file(buckets, opened_info)
                            finally:
                                os.close(file_fd)
                            continue
                        if not stat.S_ISDIR(info.st_mode):
                            raise _ScratchAllocationReadError("unsupported_entry")
                        child_depth = depth + 1
                        if child_depth > SCRATCH_ALLOCATION_DEPTH_MAX:
                            raise _ScratchAllocationReadError("depth_cap")
                        child_fd = os.open(name, flags, dir_fd=directory_fd)
                        open_fds.add(child_fd)
                        try:
                            child_info = os.fstat(child_fd)
                            named_info = os.stat(
                                name, dir_fd=directory_fd, follow_symlinks=False
                            )
                            if (
                                identity(info) != identity(child_info)
                                or identity(info) != identity(named_info)
                            ):
                                raise _ScratchAllocationReadError("identity_drift")
                            child_buckets = buckets
                            if depth == 0 and name in {"base", "pg_wal"}:
                                child_buckets = frozenset((*buckets, name))
                                seen[name] = True
                            for bucket in child_buckets:
                                counters[bucket][3] += 1
                            walk(child_fd, child_depth, child_buckets)
                        finally:
                            os.close(child_fd)
                            open_fds.discard(child_fd)
                after = os.fstat(directory_fd)
                named_after = os.stat(
                    ".", dir_fd=directory_fd, follow_symlinks=False
                )
                if identity(before) != identity(after) or identity(after) != identity(named_after):
                    raise _ScratchAllocationReadError("identity_drift")
            except _ScratchAllocationReadError:
                raise
            except (OSError, ValueError, UnicodeError):
                raise _ScratchAllocationReadError("read_error") from None

        walk(data_fd, 0, frozenset({"data"}))
        if not all(seen.values()):
            raise _ScratchAllocationReadError("read_error")
        if any(
            item > (1 << 60)
            for row in counters.values()
            for item in row[:2]
        ):
            raise _ScratchAllocationReadError("entry_cap")
        return counters
    except _ScratchAllocationReadError as exc:
        if exc.partial_tree is None:
            exc.partial_tree = {
                name: (
                    list(counters[name])
                    if name == "data" or seen[name]
                    else None
                )
                for name in counters
            }
        raise
    except (OSError, ValueError, UnicodeError):
        raise _ScratchAllocationReadError("read_error") from None
    finally:
        for descriptor in open_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _scratch_allocation_host_values(
    pid_text: str, control_group: str
) -> tuple[dict[str, int | None], bool]:
    values: dict[str, int | None] = {
        "mem_available_bytes": None,
        "rss_bytes": None,
        "memory_current_bytes": None,
        "anon_bytes": None,
        "file_bytes": None,
        "shmem_bytes": None,
    }
    try:
        start_before = _scratch_process_start_time(pid_text)
        namespace_before = os.stat(f"/proc/{pid_text}/ns/mnt")
        parsed = _scratch_proc_status_observation(
            int(pid_text), require_helper_executable=False
        )
        cgroup, _cgroup_reason = _scratch_private_cgroup_values(
            pid_text, control_group
        )
        values["mem_available_bytes"] = _scratch_mem_available_bytes()
        if parsed is not None and parsed[0] == int(pid_text):
            values["rss_bytes"] = parsed[2]
        if cgroup is not None:
            values["memory_current_bytes"] = cgroup["memory_current_bytes"]
            values["anon_bytes"] = cgroup["anon_bytes"]
            values["file_bytes"] = cgroup["file_bytes"]
            values["shmem_bytes"] = cgroup["shmem_bytes"]
        namespace_after = os.stat(f"/proc/{pid_text}/ns/mnt")
        start_after = _scratch_process_start_time(pid_text)
        membership_after = _scratch_private_proc_file(pid_text, "cgroup")
        if membership_after is None or not any(
            row.split(":", 2) == ["0", "", control_group]
            for row in membership_after.decode("ascii").splitlines()
        ):
            return values, False
        stable = (
            start_before == start_after
            and (namespace_before.st_dev, namespace_before.st_ino)
            == (namespace_after.st_dev, namespace_after.st_ino)
            and parsed is not None
            and cgroup is not None
        )
        return values, stable
    except (OSError, ValueError, UnicodeError, RuntimeError, _ScratchAllocationReadError):
        return values, False


def _scratch_allocation_empty_tree() -> dict[str, list[int] | None]:
    return {name: None for name in ("data", "base", "pg_wal")}


def _scratch_allocation_sample_from_root_fd(
    root_fd: int,
    *,
    mount_id: int,
    phase: str,
    pid_text: str,
    control_group: str,
    launch_monotonic_ns: int | None,
    guard_available_bytes: int | None = None,
    guard_monotonic_ns: int | None = None,
    operation_deadline: float | None = None,
    host_observation: dict[str, int | None] | None = None,
    host_identity_bound: bool | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + SCRATCH_ALLOCATION_SAMPLE_BUDGET_SECONDS
    if operation_deadline is not None:
        deadline = min(deadline, operation_deadline)
    sampled_unix_ns = time.time_ns()
    offset_ms = None
    if type(launch_monotonic_ns) is int and launch_monotonic_ns <= time.monotonic_ns():
        offset_ms = (time.monotonic_ns() - launch_monotonic_ns) // 1_000_000
    guard_lag_ms = None
    if type(guard_monotonic_ns) is int:
        guard_lag_ms = max(0, (time.monotonic_ns() - guard_monotonic_ns) // 1_000_000)
    root_before = os.fstat(root_fd)
    mount: dict[str, int | None] = {
        "dev": root_before.st_dev,
        "ino": root_before.st_ino,
        "mount_id": mount_id,
        "total_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
    }
    if host_observation is None:
        host, host_stable = _scratch_allocation_host_values(pid_text, control_group)
    else:
        host = dict(host_observation)
        host_stable = host_identity_bound is True
    tree = _scratch_allocation_empty_tree()
    reason: str | None = None
    try:
        statvfs = os.fstatvfs(root_fd)
        total = statvfs.f_blocks * statvfs.f_frsize
        free = statvfs.f_bavail * statvfs.f_frsize
        if total <= 0 or not 0 <= free <= total:
            raise _ScratchAllocationReadError("mount_identity")
        mount.update({"total_bytes": total, "used_bytes": total - free, "free_bytes": free})
        if time.monotonic() >= deadline:
            raise _ScratchAllocationReadError("deadline")
        try:
            tree = _scratch_allocation_tree(root_fd, root_before.st_dev, deadline)
        except _ScratchAllocationReadError as exc:
            tree = exc.partial_tree or _scratch_allocation_empty_tree()
            raise
        if time.monotonic() >= deadline:
            raise _ScratchAllocationReadError("deadline")
        root_after = os.fstat(root_fd)
        def root_identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid,
                info.st_gid, stat.S_IMODE(info.st_mode),
            )
        if root_identity(root_before) != root_identity(root_after):
            raise _ScratchAllocationReadError("identity_drift")
    except _ScratchAllocationReadError as exc:
        reason = exc.reason
        if exc.partial_tree is not None:
            tree = exc.partial_tree
    except (OSError, ValueError, OverflowError):
        reason = "read_error"
        tree = _scratch_allocation_empty_tree()
    if not host_stable and reason is None:
        reason = "identity_drift"
    if any(value is None for value in host.values()) and reason is None:
        reason = "sample_unavailable"
    sample = {
        "schema": 1,
        "phase": phase,
        "status": "complete" if reason is None else "unknown",
        "unknown_reason": reason,
        "sample_unix_ns": sampled_unix_ns,
        "sample_offset_ms": offset_ms,
        "guard_available_bytes": guard_available_bytes,
        "guard_sample_lag_ms": guard_lag_ms,
        "mount": mount,
        "host": host,
        "tree": tree,
    }
    if not valid_scratch_allocation_sample(sample):
        sample.update(
            status="unknown",
            unknown_reason="internal_error",
            tree=_scratch_allocation_empty_tree(),
        )
    return sample


def _scratch_allocation_proc_bytes(pid_text: str, name: str, cap: int) -> bytes:
    if (
        not re.fullmatch(r"[1-9][0-9]{0,9}", pid_text)
        or name not in {"mountinfo"}
        or type(cap) is not int
        or not 1 <= cap <= 4 * 1024 * 1024
    ):
        raise _ScratchAllocationReadError("identity_drift")
    descriptor = os.open(
        f"/proc/{pid_text}/{name}", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    )
    try:
        return _scratch_allocation_read_fd(descriptor, cap)
    finally:
        os.close(descriptor)


def _scratch_allocation_parent_last_guard(
    *,
    mountpoint: pathlib.Path,
    worker_identity: tuple[str, str, str] | None,
    control_group: str | None,
    launch_monotonic_ns: int | None,
    guard_diagnostic: dict[str, Any],
    resource_attribution: dict[str, Any] | None,
    operation_deadline: float,
    guard_monotonic_ns: int,
) -> dict[str, Any]:
    """Best-effort root-view sample pinned to the active worker, never a guard."""
    now = time.monotonic()
    default_host: dict[str, int | None] = {
        "mem_available_bytes": guard_diagnostic.get("available_bytes"),
        "rss_bytes": None,
        "memory_current_bytes": None,
        "anon_bytes": None,
        "file_bytes": None,
        "shmem_bytes": None,
    }
    resource_bound = (
        isinstance(resource_attribution, dict)
        and valid_scratch_resource_attribution(resource_attribution)
        and resource_attribution.get("status") == "complete"
    )
    if resource_bound:
        default_host["rss_bytes"] = resource_attribution["worker"]["rss_bytes"]
        for key in ("memory_current_bytes", "anon_bytes", "file_bytes", "shmem_bytes"):
            default_host[key] = resource_attribution["cgroup"][key]

    def unknown(reason: str) -> dict[str, Any]:
        now_ns = time.monotonic_ns()
        return {
            "schema": 1,
            "phase": "parent_last_guard",
            "status": "unknown",
            "unknown_reason": reason,
            "sample_unix_ns": time.time_ns(),
            "sample_offset_ms": (
                (now_ns - launch_monotonic_ns) // 1_000_000
                if type(launch_monotonic_ns) is int and launch_monotonic_ns <= now_ns
                else None
            ),
            "guard_available_bytes": guard_diagnostic.get("available_bytes"),
            "guard_sample_lag_ms": max(0, (now_ns - guard_monotonic_ns) // 1_000_000),
            "mount": {key: None for key in ("dev", "ino", "mount_id", "total_bytes", "used_bytes", "free_bytes")},
            "host": default_host,
            "tree": _scratch_allocation_empty_tree(),
        }

    if now >= operation_deadline:
        return unknown("deadline")
    if (
        not isinstance(worker_identity, tuple)
        or len(worker_identity) != 3
        or control_group is None
        or guard_diagnostic.get("guard_site") != "parent_active_poll"
        or re.fullmatch(r"/run/oldsparky-backup-restore-[0-9a-f]{24}", str(mountpoint)) is None
    ):
        return unknown("identity_drift")
    pid_text, invocation_id, expected_start = worker_identity
    if (
        not re.fullmatch(r"[1-9][0-9]{0,9}", pid_text)
        or not re.fullmatch(r"[0-9a-f]{32}", invocation_id)
        or not re.fullmatch(r"[0-9]+", expected_start)
        or not re.fullmatch(
            r"/system\.slice/oldsparky-backup-restore-[0-9a-f]{24}\.service",
            control_group,
        )
    ):
        return unknown("identity_drift")

    proc_root_fd = root_fd = -1
    try:
        start_before = _scratch_process_start_time(pid_text)
        status_before = _scratch_proc_status_observation(
            int(pid_text), require_helper_executable=False
        )
        namespace_before = os.stat(f"/proc/{pid_text}/ns/mnt")
        membership_before = _scratch_private_proc_file(pid_text, "cgroup")
        if (
            start_before != expected_start
            or status_before is None
            or status_before[0] != int(pid_text)
            or membership_before is None
            or not any(
                row.split(":", 2) == ["0", "", control_group]
                for row in membership_before.decode("ascii").splitlines()
            )
        ):
            return unknown("identity_drift")
        proc_root_path = pathlib.Path(f"/proc/{pid_text}/root")
        root_path_before = proc_root_path.stat()
        proc_root_fd = os.open(
            proc_root_path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        proc_root_before = os.fstat(proc_root_fd)
        def root_identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid,
                info.st_gid, stat.S_IMODE(info.st_mode),
            )
        if root_identity(root_path_before) != root_identity(proc_root_before):
            return unknown("identity_drift")
        if time.monotonic() >= operation_deadline:
            return unknown("deadline")
        relative = str(mountpoint).lstrip("/")
        target_path_before = os.stat(relative, dir_fd=proc_root_fd, follow_symlinks=False)
        root_fd = os.open(
            relative,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=proc_root_fd,
        )
        target_opened = os.fstat(root_fd)
        if root_identity(target_path_before) != root_identity(target_opened):
            return unknown("identity_drift")
        if time.monotonic() >= operation_deadline:
            return unknown("deadline")
        mountinfo = _scratch_allocation_proc_bytes(pid_text, "mountinfo", 4 * 1024 * 1024)
        row = _scratch_allocation_mount_row(mountinfo, str(mountpoint))
        if row is None or (os.major(target_opened.st_dev), os.minor(target_opened.st_dev)) != row[1:]:
            return unknown("mount_identity")
        if time.monotonic() >= operation_deadline:
            return unknown("deadline")

        host_bound = resource_bound and all(value is not None for value in default_host.values())
        sample = _scratch_allocation_sample_from_root_fd(
            root_fd,
            mount_id=row[0],
            phase="parent_last_guard",
            pid_text=pid_text,
            control_group=control_group,
            launch_monotonic_ns=launch_monotonic_ns,
            guard_available_bytes=guard_diagnostic.get("available_bytes"),
            guard_monotonic_ns=guard_monotonic_ns,
            operation_deadline=operation_deadline,
            host_observation=default_host,
            host_identity_bound=host_bound,
        )
        try:
            mountinfo_after = _scratch_allocation_proc_bytes(
                pid_text, "mountinfo", 4 * 1024 * 1024
            )
            row_after = _scratch_allocation_mount_row(
                mountinfo_after, str(mountpoint)
            )
        except _ScratchAllocationReadError as exc:
            sample.update(status="unknown", unknown_reason=exc.reason)
            row_after = None
        except (OSError, ValueError, UnicodeError, RuntimeError):
            sample.update(status="unknown", unknown_reason="read_error")
            row_after = None
        if row_after != row:
            sample.update(status="unknown", unknown_reason="mount_identity")
        target_path_after = os.stat(relative, dir_fd=proc_root_fd, follow_symlinks=False)
        target_opened_after = os.fstat(root_fd)
        root_path_after = proc_root_path.stat()
        proc_root_after = os.fstat(proc_root_fd)
        namespace_after = os.stat(f"/proc/{pid_text}/ns/mnt")
        membership_after = _scratch_private_proc_file(pid_text, "cgroup")
        start_after = _scratch_process_start_time(pid_text)
        status_after = _scratch_proc_status_observation(
            int(pid_text), require_helper_executable=False
        )
        if (
            root_identity(target_path_before) != root_identity(target_path_after)
            or root_identity(target_opened) != root_identity(target_opened_after)
            or root_identity(root_path_before) != root_identity(root_path_after)
            or root_identity(proc_root_before) != root_identity(proc_root_after)
            or (namespace_before.st_dev, namespace_before.st_ino)
            != (namespace_after.st_dev, namespace_after.st_ino)
            or start_after != expected_start
            or status_after is None
            or status_after[0] != int(pid_text)
            or status_after[:2] != status_before[:2]
            or membership_after is None
            or not any(
                row_text.split(":", 2) == ["0", "", control_group]
                for row_text in membership_after.decode("ascii").splitlines()
            )
        ):
            sample.update(status="unknown", unknown_reason="identity_drift")
        return sample
    except _ScratchAllocationReadError as exc:
        return unknown(exc.reason)
    except (OSError, ValueError, UnicodeError, RuntimeError, OverflowError):
        return unknown("sample_unavailable")
    finally:
        for descriptor in (root_fd, proc_root_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _scratch_allocation_worker_sample(
    args: argparse.Namespace,
    root: pathlib.Path,
    phase: str,
    *,
    guard_available_bytes: int | None = None,
) -> dict[str, Any]:
    started_ns = time.monotonic_ns()
    unknown = {
        "schema": 1, "phase": phase, "status": "unknown", "unknown_reason": "sample_unavailable",
        "sample_unix_ns": time.time_ns(), "sample_offset_ms": None,
        "guard_available_bytes": guard_available_bytes, "guard_sample_lag_ms": None,
        "mount": {key: None for key in ("dev", "ino", "mount_id", "total_bytes", "used_bytes", "free_bytes")},
        "host": {key: None for key in ("mem_available_bytes", "rss_bytes", "memory_current_bytes", "anon_bytes", "file_bytes", "shmem_bytes")},
        "tree": _scratch_allocation_empty_tree(),
    }
    fd = -1
    try:
        root_stat = os.stat(root, follow_symlinks=False)
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        opened = os.fstat(fd)
        def stable_identity(info: os.stat_result) -> tuple[int, ...]:
            return (
                info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_uid,
                info.st_gid, stat.S_IMODE(info.st_mode),
            )
        if stable_identity(root_stat) != stable_identity(opened):
            raise _ScratchAllocationReadError("identity_drift")
        mountinfo_fd = os.open("/proc/self/mountinfo", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            mountinfo = _scratch_allocation_read_fd(mountinfo_fd, 4 * 1024 * 1024)
        finally:
            os.close(mountinfo_fd)
        row = _scratch_allocation_mount_row(mountinfo, str(root))
        if row is None or (os.major(opened.st_dev), os.minor(opened.st_dev)) != row[1:]:
            raise _ScratchAllocationReadError("mount_identity")
        control_group_raw = _scratch_private_proc_file(str(os.getpid()), "cgroup")
        if control_group_raw is None:
            raise _ScratchAllocationReadError("sample_unavailable")
        rows = [item.split(":", 2) for item in control_group_raw.decode("ascii").splitlines()]
        current = next((item[2] for item in rows if len(item) == 3 and item[:2] == ["0", ""]), None)
        if current is None:
            raise _ScratchAllocationReadError("sample_unavailable")
        sample = _scratch_allocation_sample_from_root_fd(
            fd,
            mount_id=row[0],
            phase=phase,
            pid_text=str(os.getpid()),
            control_group=current,
            launch_monotonic_ns=getattr(args, "_scratch_phase_origin_monotonic_ns", None),
            guard_available_bytes=guard_available_bytes,
            guard_monotonic_ns=started_ns if guard_available_bytes is not None else None,
        )
        try:
            mountinfo_after_fd = os.open(
                "/proc/self/mountinfo", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                mountinfo_after = _scratch_allocation_read_fd(
                    mountinfo_after_fd, 4 * 1024 * 1024
                )
            finally:
                os.close(mountinfo_after_fd)
            row_after = _scratch_allocation_mount_row(mountinfo_after, str(root))
        except _ScratchAllocationReadError as exc:
            sample.update(status="unknown", unknown_reason=exc.reason)
            row_after = None
        except (OSError, ValueError, UnicodeError, RuntimeError):
            sample.update(status="unknown", unknown_reason="read_error")
            row_after = None
        if row_after != row:
            sample.update(status="unknown", unknown_reason="mount_identity")
        named_after = os.stat(root, follow_symlinks=False)
        opened_after = os.fstat(fd)
        if stable_identity(root_stat) != stable_identity(named_after) or (
            stable_identity(opened) != stable_identity(opened_after)
        ):
            sample.update(status="unknown", unknown_reason="identity_drift", tree=_scratch_allocation_empty_tree())
        return sample
    except _ScratchAllocationReadError as exc:
        unknown["unknown_reason"] = exc.reason
    except (OSError, ValueError, UnicodeError, RuntimeError):
        unknown["unknown_reason"] = "sample_unavailable"
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    return unknown


def _scratch_emit_allocation_sample(
    args: argparse.Namespace,
    root: pathlib.Path,
    phase: str,
    *,
    guard_available_bytes: int | None = None,
) -> None:
    if (
        not getattr(args, "private_scratch_diagnostics", False)
        or getattr(args, "_scratch_authorization_probe", False)
        or phase not in _SCRATCH_ALLOCATION_PHASES | {"worker_failure"}
    ):
        return
    try:
        sample = _scratch_allocation_worker_sample(
            args, root, phase, guard_available_bytes=guard_available_bytes
        )
        if not valid_scratch_allocation_sample(sample):
            return
        payload = json.dumps(sample, separators=(",", ":"), sort_keys=True).encode("ascii")
        if len(payload) > SCRATCH_ALLOCATION_FRAME_MAX_BYTES:
            return
        stream = getattr(sys.stderr, "buffer", None)
        if stream is None:
            return
        stream.write(SCRATCH_ALLOCATION_FRAME_MARKER + payload + b"\n")
        stream.flush()
    except BaseException:
        # Optional attribution never alters restore or resource-guard behavior.
        return


def _scratch_unknown_allocation_attribution(
    archive_size_bytes: int, archive_sha256: str, reason: str
) -> dict[str, Any]:
    if reason not in _SCRATCH_ALLOCATION_UNKNOWN_REASONS:
        reason = "internal_error"
    candidate = {
        "schema": 1,
        "status": "unknown",
        "unknown_reason": reason,
        "archive_size_bytes": archive_size_bytes,
        "archive_sha256": archive_sha256,
        "samples": [],
    }
    if not valid_scratch_allocation_attribution(candidate):
        candidate.update(
            archive_size_bytes=1,
            archive_sha256="0" * 64,
            unknown_reason="internal_error",
        )
    return candidate


def _scratch_parse_allocation_attribution(
    stderr: bytes,
    *,
    truncated: bool,
    archive_size_bytes: int,
    archive_sha256: str,
    parent_sample: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect only bounded canonical worker frames and one parent observation."""
    if (
        type(stderr) is not bytes
        or type(truncated) is not bool
        or type(archive_size_bytes) is not int
        or archive_size_bytes <= 0
        or type(archive_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
    ):
        return _scratch_unknown_allocation_attribution(
            max(1, archive_size_bytes if type(archive_size_bytes) is int else 1),
            archive_sha256 if type(archive_sha256) is str and re.fullmatch(r"[0-9a-f]{64}", archive_sha256) else "0" * 64,
            "malformed",
        )
    samples: list[dict[str, Any]] = []
    parse_reason = "capture_prefix" if truncated else None
    expected_order = {
        "initdb_end": 0,
        "postgres_start_end": 1,
        "restore_platform_start": 2,
        "restore_platform_end": 3,
        "worker_failure": 4,
    }
    previous_phase = -1
    seen: set[str] = set()
    for line in stderr.splitlines(keepends=True):
        if not line.startswith(SCRATCH_ALLOCATION_FRAME_MARKER):
            continue
        if not line.endswith(b"\n"):
            parse_reason = parse_reason or "capture_prefix"
            continue
        payload = line[len(SCRATCH_ALLOCATION_FRAME_MARKER):-1]
        if not payload or len(payload) > SCRATCH_ALLOCATION_FRAME_MAX_BYTES:
            parse_reason = parse_reason or "malformed"
            continue
        try:
            decoded = json.loads(
                payload.decode("ascii"),
                object_pairs_hook=_scratch_reject_duplicate_phase_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            parse_reason = parse_reason or "malformed"
            continue
        if (
            not isinstance(decoded, dict)
            or not valid_scratch_allocation_sample(decoded)
            or decoded["phase"] not in expected_order
            or decoded["phase"] in seen
            or expected_order[decoded["phase"]] <= previous_phase
            or json.dumps(decoded, separators=(",", ":"), sort_keys=True).encode("ascii") != payload
            or len(samples) >= SCRATCH_ALLOCATION_MAX_SAMPLES
        ):
            parse_reason = parse_reason or "malformed"
            continue
        previous_phase = expected_order[decoded["phase"]]
        seen.add(decoded["phase"])
        samples.append(decoded)
    if parent_sample is not None:
        if (
            valid_scratch_allocation_sample(parent_sample)
            and parent_sample.get("phase") == "parent_last_guard"
            and len(samples) < SCRATCH_ALLOCATION_MAX_SAMPLES
        ):
            samples.append(parent_sample)
        else:
            parse_reason = parse_reason or "sample_unavailable"
    samples_by_phase = [sample["phase"] for sample in samples]
    all_lifecycle_phases = set(samples_by_phase) == set(_SCRATCH_ALLOCATION_PHASES)
    all_complete = all(sample["status"] == "complete" for sample in samples)
    if not samples:
        return _scratch_unknown_allocation_attribution(
            archive_size_bytes, archive_sha256, parse_reason or "capture_unavailable"
        )
    if (
        all_lifecycle_phases
        and all_complete
        and parse_reason is None
        and "worker_failure" not in samples_by_phase
        and "parent_last_guard" not in samples_by_phase
    ):
        status = "complete"
        reason = None
    else:
        status = "partial"
        reason = parse_reason
        if reason is None:
            reason = next(
                (sample["unknown_reason"] for sample in samples if sample["unknown_reason"] is not None),
                "incomplete",
            )
    candidate = {
        "schema": 1,
        "status": status,
        "unknown_reason": reason,
        "archive_size_bytes": archive_size_bytes,
        "archive_sha256": archive_sha256,
        "samples": samples,
    }
    if not valid_scratch_allocation_attribution(candidate):
        return _scratch_unknown_allocation_attribution(
            archive_size_bytes, archive_sha256, "internal_error"
        )
    return candidate


def _scratch_allocation_attribution_frame(value: Any) -> bytes | None:
    if not valid_scratch_allocation_attribution(value):
        return None
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    frame = SCRATCH_ALLOCATION_ATTRIBUTION_MARKER + payload + b"\n"
    return frame if len(frame) <= SCRATCH_ALLOCATION_ATTRIBUTION_MAX_BYTES + len(SCRATCH_ALLOCATION_ATTRIBUTION_MARKER) + 1 else None


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
    scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1,
    expected_owner: tuple[int, int] | None = None,
) -> None:
    require_restore_headroom(pathlib.Path("/"))
    _raise_scratch_host_memory_reserve(
        guard_site,
        message="Scratch restore stopped by the host-memory reserve guard.",
    )
    _scratch_cgroup_snapshot()
    if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        if (
            not isinstance(expected_owner, tuple)
            or len(expected_owner) != 2
            or type(expected_owner[0]) is not int
            or type(expected_owner[1]) is not int
            or expected_owner[0] <= 0
            or expected_owner[1] < 0
        ):
            raise RuntimeError("Scratch disk resource guard requires the exact postgres owner.")
        _scratch_mount_snapshot(
            root,
            backend=scratch_backend,
            expected_owner=expected_owner,
        )
    else:
        if scratch_backend != SCRATCH_BACKEND_TMPFS_V1 or expected_owner is not None:
            raise RuntimeError("Scratch restore backend or owner is not supported.")
        _scratch_mount_snapshot(root, backend=scratch_backend)


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


def _scratch_cleanup_synthetic_cluster(
    paths: tuple[pathlib.Path, ...], *, root_device: int
) -> None:
    pending: list[tuple[pathlib.Path, os.stat_result]] = []
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_dev != root_device:
            raise RuntimeError("Synthetic restore cluster path escaped its private tmpfs.")
        if stat.S_ISDIR(info.st_mode):
            try:
                (path / "postmaster.pid").lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("Synthetic restore cluster must be reaped before cleanup.")
        pending.append((path, info))
    for path, info in pending:
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path)
        elif stat.S_ISREG(info.st_mode):
            path.unlink()
        else:
            raise RuntimeError("Synthetic restore cluster contains an unsupported path type.")


def _scratch_synthetic_restore_metric_phase(phase: str) -> bool:
    """Return whether a fixed restore phase contributes diagnostic metrics."""
    if phase == "alembic_verify":
        return False
    if phase in SCRATCH_SYNTHETIC_RESTORE_PHASES:
        return True
    raise RuntimeError("Synthetic restore reported an unknown phase.")


def _scratch_synthetic_restore_diagnostic_worker(
    args: argparse.Namespace, root: pathlib.Path
) -> dict[str, Any]:
    """Build a real PGDMP fixture and measure three fixed restore arms in-unit."""
    if any(
        getattr(args, name, None) is not None
        for name in (
            "_scratch_archive", "_scratch_archive_dev", "_scratch_archive_ino",
            "_scratch_archive_size", "_scratch_archive_sha256",
        )
    ):
        raise RuntimeError("Synthetic restore diagnostic cannot receive an external archive.")
    scratch_backend = getattr(args, "_scratch_backend", SCRATCH_BACKEND_TMPFS_V1)
    if scratch_backend not in SCRATCH_BACKENDS:
        raise RuntimeError("Synthetic restore diagnostic backend is unsupported.")
    postgres_user = pwd.getpwnam("postgres")
    scratch_expected_owner = (postgres_user.pw_uid, postgres_user.pw_gid)
    if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        _scratch_disk_mount_snapshot(root)
        _scratch_prepare_disk_root_for_postgres(
            root, postgres_uid=postgres_user.pw_uid, postgres_gid=postgres_user.pw_gid
        )
        filesystem_capacity_bytes = int(
            _scratch_disk_mount_snapshot(
                root, expected_owner=(postgres_user.pw_uid, postgres_user.pw_gid)
            )["total_bytes"]
        )
    else:
        os.chown(root, postgres_user.pw_uid, postgres_user.pw_gid)
        filesystem_capacity_bytes = SCRATCH_TMPFS_BYTES
    require_commands("pg_config", "runuser")
    pg_config_path = shutil.which("pg_config")
    if pg_config_path is None:
        raise RuntimeError("Synthetic restore diagnostic is missing pg_config.")
    pg_config = pathlib.Path(pg_config_path)
    pg_config_version = run_command(
        [str(pg_config), "--version"], capture_output=True
    ).stdout
    if re.search(r"\b16\.\d+\b", pg_config_version) is None:
        raise RuntimeError("Synthetic restore diagnostic requires PostgreSQL 16 pg_config.")
    bindir = pathlib.Path(
        run_command([str(pg_config), "--bindir"], capture_output=True).stdout.strip()
    )
    sharedir = pathlib.Path(
        run_command([str(pg_config), "--sharedir"], capture_output=True).stdout.strip()
    )
    postgres = bindir / "postgres"
    initdb = bindir / "initdb"
    pgctl = bindir / "pg_ctl"
    pg_dump = bindir / "pg_dump"
    pg_restore = bindir / "pg_restore"
    psql = bindir / "psql"
    createdb = bindir / "createdb"
    dropdb = bindir / "dropdb"
    for binary in (
        postgres, initdb, pgctl, pg_dump, pg_restore, psql, createdb, dropdb
    ):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("Synthetic restore diagnostic is missing a PostgreSQL 16 binary.")
    version = run_command([str(postgres), "--version"], capture_output=True).stdout
    version_match = re.search(r"\b16\.(\d+)\b", version)
    if version_match is None:
        raise RuntimeError("Synthetic restore diagnostic requires PostgreSQL 16 binaries.")
    for binary in (initdb, pgctl, pg_dump, pg_restore, psql, createdb, dropdb):
        binary_version = run_command([str(binary), "--version"], capture_output=True).stdout
        if re.search(r"\b16\.\d+\b", binary_version) is None:
            raise RuntimeError("Synthetic restore diagnostic requires PostgreSQL 16 binaries.")
    postgres_version = f"16.{version_match.group(1)}"
    if not (sharedir / "extension" / "pg_trgm.control").is_file():
        raise RuntimeError("Synthetic restore diagnostic requires the pg_trgm extension files.")
    # Resolve generic PostgreSQL helpers (createdb/dropdb) from this validated
    # installation for this worker process only. Restore clients below also
    # receive explicit paths so runuser cannot select a different pg_restore.
    os.environ["PATH"] = os.pathsep.join(
        part for part in (str(bindir), os.environ.get("PATH", "")) if part
    )

    root_device = os.stat(root).st_dev
    archive_path = root / "archive.dump"
    archive: BinaryIO | None = None
    peak_fd: int | None = None
    archive_initial: tuple[int, int, int, int, int] | None = None
    expected_rows = _scratch_synthetic_expected_rows()
    source_data = root / "source-data"
    source_socket = root / "source-socket"
    source_log = root / "source-postgres.log"
    target_data = root / "data"
    target_socket = root / "socket"
    target_log = root / "postgres.log"

    def as_postgres(command: list[str], *, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        return run_command(
            ["runuser", "-u", "postgres", "--", *command],
            capture_output=capture_output,
        )

    def prepare_cluster(data: pathlib.Path, socket_dir: pathlib.Path) -> None:
        _scratch_prepare_postgres_directory(
            data, uid=postgres_user.pw_uid, gid=postgres_user.pw_gid
        )
        _scratch_prepare_postgres_directory(
            socket_dir, uid=postgres_user.pw_uid, gid=postgres_user.pw_gid
        )
        _scratch_resource_guard(
            root,
            guard_site="worker_synthetic_before_initdb",
            scratch_backend=scratch_backend,
            expected_owner=(
                scratch_expected_owner
                if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                else None
            ),
        )
        as_postgres(_scratch_initdb_command(str(initdb), data), capture_output=True)

    def start_cluster(
        data: pathlib.Path,
        socket_dir: pathlib.Path,
        log: pathlib.Path,
        options: list[str],
    ) -> None:
        _scratch_resource_guard(
            root,
            guard_site="worker_synthetic_before_postgres_start",
            scratch_backend=scratch_backend,
            expected_owner=(
                scratch_expected_owner
                if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                else None
            ),
        )
        as_postgres(
            [
                str(pgctl), "--pgdata", str(data), "--log", str(log),
                "--options", shlex.join(options), "--wait", "--timeout=30", "start",
            ],
            capture_output=True,
        )
        _scratch_resource_guard(
            root,
            guard_site="worker_synthetic_after_postgres_start",
            scratch_backend=scratch_backend,
            expected_owner=(
                scratch_expected_owner
                if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                else None
            ),
        )

    def stop_cluster(data: pathlib.Path) -> None:
        pid_file = data / "postmaster.pid"
        if pid_file.exists():
            as_postgres(
                [str(pgctl), "--pgdata", str(data), "--mode=fast", "--wait", "--timeout=20", "stop"],
                capture_output=True,
            )
        if pid_file.exists():
            raise RuntimeError("Synthetic restore PostgreSQL process was not reaped.")

    def remove_cluster(data: pathlib.Path, socket_dir: pathlib.Path, log: pathlib.Path) -> None:
        _scratch_cleanup_synthetic_cluster(
            (data, socket_dir, log), root_device=root_device
        )

    try:
        # Fixture creation itself runs in this same private unit, after the
        # controller has verified systemd, mount, cgroup, and host reserve.
        prepare_cluster(source_data, source_socket)
        source_admin = DatabaseTarget(
            host=str(source_socket), port=SCRATCH_PG_PORT,
            username="postgres", password=None, database="postgres",
        )
        source_started = False
        try:
            start_cluster(
                source_data,
                source_socket,
                source_log,
                _scratch_postgres_start_options(source_socket),
            )
            source_started = True
            source_version_result = run_command(
                [
                    str(psql), "--no-psqlrc", "--tuples-only", "--no-align",
                    *connection_args(source_admin), "--command",
                    "SELECT current_setting('server_version');",
                ],
                target=source_admin,
                capture_output=True,
            )
            source_version_match = re.match(
                r"^16\.\d+\b", source_version_result.stdout.strip()
            )
            if source_version_match is None or source_version_match.group(0) != postgres_version:
                raise RuntimeError("Synthetic source PostgreSQL server version differs from its PG16 binaries.")
            run_command(
                [str(psql), "--no-psqlrc", *connection_args(source_admin),
                 "--command", "CREATE DATABASE scratch_restore_fixture;"],
                target=source_admin,
                capture_output=True,
            )
            fixture_target = source_admin.with_database("scratch_restore_fixture")
            fixture_sql = f"""
                CREATE EXTENSION pg_trgm WITH SCHEMA public;
                CREATE SCHEMA platform;
                CREATE TABLE platform.synthetic_restore_probe (
                    id integer PRIMARY KEY,
                    label text NOT NULL,
                    search_text text NOT NULL
                );
                INSERT INTO platform.synthetic_restore_probe (id, label, search_text)
                SELECT
                    row_id,
                    'row-' || lpad(row_id::text, 8, '0'),
                    lpad(row_id::text, 8, '0')
                    || string_agg(
                        md5('{SCRATCH_SYNTHETIC_PAYLOAD_SEED}:' || row_id::text || ':' || block_id::text),
                        '' ORDER BY block_id
                    )
                    || left(md5('{SCRATCH_SYNTHETIC_PAYLOAD_SEED}:tail:' || row_id::text), 24)
                FROM generate_series(1, {SCRATCH_SYNTHETIC_PAYLOAD_ROWS}) AS rows(row_id)
                CROSS JOIN generate_series(1, {SCRATCH_SYNTHETIC_PAYLOAD_BLOCKS}) AS blocks(block_id)
                GROUP BY row_id
                ORDER BY row_id;
                CREATE INDEX {SCRATCH_SYNTHETIC_TRGM_INDEX}
                    ON platform.synthetic_restore_probe USING gin (search_text gin_trgm_ops);
                CREATE TABLE public.alembic_version (
                    version_num varchar(32) NOT NULL PRIMARY KEY
                );
                INSERT INTO public.alembic_version (version_num)
                    VALUES ('synthetic_restore_revision_001');
            """
            run_command(
                [str(psql), "--no-psqlrc", "--set=ON_ERROR_STOP=1",
                 *connection_args(fixture_target), "--command", fixture_sql],
                target=fixture_target,
                capture_output=True,
            )
            _scratch_synthetic_restore_payload_state(
                fixture_target,
                expected_rows=expected_rows,
                psql_binary=str(psql),
            )
            if archive_path.exists():
                raise RuntimeError("Synthetic restore archive path already exists.")
            as_postgres(
                [
                    str(pg_dump), "--format=custom", "--no-owner", "--no-acl",
                    "--schema=platform", "--schema=public",
                    *connection_args(fixture_target), "--file", str(archive_path),
                ],
                capture_output=True,
            )
            archive_info = archive_path.lstat()
            if (
                not stat.S_ISREG(archive_info.st_mode)
                or archive_info.st_dev != root_device
                or archive_info.st_uid != postgres_user.pw_uid
                or archive_info.st_size <= 5
                or archive_info.st_size > SCRATCH_SYNTHETIC_ARCHIVE_MAX_BYTES
            ):
                raise RuntimeError("Synthetic PostgreSQL custom archive is outside its fixed bound.")
            os.chown(archive_path, 0, 0)
            os.chmod(archive_path, 0o600)
            archive_info = archive_path.lstat()
            if archive_info.st_nlink != 1 or stat.S_IMODE(archive_info.st_mode) != 0o600:
                raise RuntimeError("Synthetic PostgreSQL custom archive identity is unsafe.")
            args._scratch_archive = str(archive_path)
            args._scratch_archive_dev = archive_info.st_dev
            args._scratch_archive_ino = archive_info.st_ino
            args._scratch_archive_size = archive_info.st_size
            args._scratch_archive_sha256 = sha256_file(archive_path)
            archive = _open_verified_scratch_archive(args)
            archive_initial = (
                archive_info.st_dev,
                archive_info.st_ino,
                archive_info.st_size,
                archive_info.st_mtime_ns,
                archive_info.st_ctime_ns,
            )
        finally:
            if source_started or (source_data / "postmaster.pid").exists():
                stop_cluster(source_data)
            remove_cluster(source_data, source_socket, source_log)

        peak_fd = _scratch_open_memory_peak_fd(args)
        arm_results: list[dict[str, Any]] = []
        for arm_index, arm in enumerate(SCRATCH_SYNTHETIC_RESTORE_ARMS, start=1):
            name, wal_level, max_wal_senders, single_transaction = arm
            prepare_cluster(target_data, target_socket)
            target_admin = DatabaseTarget(
                host=str(target_socket), port=SCRATCH_PG_PORT,
                username="postgres", password=None, database="postgres",
            )
            target_started = False
            sampler: _ScratchFilesystemPeakSampler | None = None
            metric_state: dict[str, dict[str, int]] = {}
            validation: dict[str, Any] = {}
            arm_slug = f"{args._scratch_timestamp_slug}_arm{arm_index}"
            arm_restore_database = (
                f"platform_restore_drill_{arm_slug.lower()}_{os.getpid()}"
            )
            arm_restore_target = DatabaseTarget(
                host=str(target_socket),
                port=SCRATCH_PG_PORT,
                username=SCRATCH_APP_ROLE,
                password=None,
                database=arm_restore_database,
            )
            try:
                start_cluster(
                    target_data,
                    target_socket,
                    target_log,
                    _scratch_postgres_start_options_for_diagnostic_arm(target_socket, arm),
                )
                target_started = True
                _scratch_create_application_role(target_admin, psql_binary=str(psql))
                setting_result = run_command(
                    [
                        str(psql), "--no-psqlrc", "--tuples-only", "--no-align",
                        *connection_args(target_admin), "--command",
                        "SELECT current_setting('server_version') || '|' || current_setting('wal_level') || '|' || current_setting('max_wal_senders');",
                    ],
                    target=target_admin,
                    capture_output=True,
                )
                setting_fields = setting_result.stdout.strip().split("|", 2)
                target_version_match = (
                    re.match(r"^16\.\d+\b", setting_fields[0])
                    if len(setting_fields) == 3
                    else None
                )
                if (
                    target_version_match is None
                    or target_version_match.group(0) != postgres_version
                    or setting_fields[1:] != [wal_level, str(max_wal_senders)]
                ):
                    raise RuntimeError("Synthetic restore PostgreSQL arm settings were not applied.")

                def phase_observer(phase: str, event: str) -> None:
                    nonlocal sampler
                    if not _scratch_synthetic_restore_metric_phase(phase):
                        return
                    if event == "start":
                        state = metric_state.get(phase)
                        if (
                            state is None
                            or set(state) != {"phase_started_ns"}
                            or sampler is not None
                        ):
                            raise RuntimeError("Synthetic restore phase metrics started more than once.")
                        state["wal_before_bytes"] = _scratch_pg_stat_wal_bytes(
                            target_admin, psql_binary=str(psql)
                        )
                        state["memory_start_bytes"] = _scratch_read_or_reset_memory_peak(
                            peak_fd, reset=True
                        )
                        sampler = _ScratchFilesystemPeakSampler(
                            root,
                            backend=scratch_backend,
                            postgres_owner=(postgres_user.pw_uid, postgres_user.pw_gid),
                        )
                        sampler.start()
                        state["restore_command_started_ns"] = time.monotonic_ns()
                    elif event == "end":
                        state = metric_state.get(phase)
                        if (
                            state is None
                            or "restore_command_started_ns" not in state
                            or "restore_command_ended_ns" in state
                            or sampler is None
                        ):
                            raise RuntimeError("Synthetic restore phase metrics ended out of order.")
                        state["restore_command_ended_ns"] = time.monotonic_ns()
                        state["filesystem_peak_sampled_bytes"] = sampler.stop()
                        if sampler.capacity_bytes != filesystem_capacity_bytes:
                            raise RuntimeError("Synthetic restore filesystem capacity changed during the run.")
                        state["filesystem_capacity_bytes"] = filesystem_capacity_bytes
                        sampler = None
                        state["memory_peak_bytes"] = _scratch_read_or_reset_memory_peak(peak_fd)
                        _scratch_wait_for_restore_database_backends_to_exit(
                            target_admin,
                            arm_restore_target.database,
                            psql_binary=str(psql),
                        )
                        state["wal_after_bytes"] = _scratch_pg_stat_wal_bytes(
                            target_admin, psql_binary=str(psql)
                        )
                    else:
                        raise RuntimeError("Synthetic restore reported an unknown phase event.")

                def validate_restore_phase(phase: str, boundary: str) -> None:
                    if phase not in SCRATCH_SYNTHETIC_RESTORE_PHASES or boundary not in ("before", "after"):
                        raise RuntimeError("Synthetic restore payload check reported an unknown boundary.")
                    if boundary == "before":
                        if phase in metric_state or sampler is not None:
                            raise RuntimeError("Synthetic restore phase validation began out of order.")
                        metric_state[phase] = {"phase_started_ns": time.monotonic_ns()}
                    state = _scratch_synthetic_restore_payload_state(
                        arm_restore_target,
                        expected_rows=expected_rows,
                        psql_binary=str(psql),
                    )
                    if phase == "restore_platform" and boundary == "before":
                        if state != {"table_present": False}:
                            raise RuntimeError(
                                "Synthetic restore platform target was not empty before restore."
                            )
                    elif state.get("table_present") is not True:
                        raise RuntimeError("Synthetic restore phase did not retain the bulk table.")
                    if boundary == "after":
                        phase_state = metric_state.get(phase)
                        if (
                            phase_state is None
                            or "restore_command_ended_ns" not in phase_state
                            or "phase_ended_ns" in phase_state
                        ):
                            raise RuntimeError("Synthetic restore phase validation ended out of order.")
                        phase_state["phase_ended_ns"] = time.monotonic_ns()

                def resource_guard() -> None:
                    _scratch_resource_guard(
                        root,
                        guard_site="worker_synthetic_restore_resource_guard",
                        scratch_backend=scratch_backend,
                        expected_owner=(
                            scratch_expected_owner
                            if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                            else None
                        ),
                    )

                table_count = perform_restore_drill(
                    archive_path,
                    app_target=arm_restore_target,
                    admin_target=target_admin,
                    timestamp_slug=arm_slug,
                    archive_stdin=archive,
                    restore_as_postgres=True,
                    restore_no_tablespaces=True,
                    restore_single_transaction=single_transaction,
                    psql_binary=str(psql),
                    pg_restore_binary=str(pg_restore),
                    synthetic_validation_result=validation,
                    synthetic_expected_rows=expected_rows,
                    synthetic_phase_validator=validate_restore_phase,
                    resource_guard=resource_guard,
                    phase_observer=phase_observer,
                )
                if (
                    len(validation) != 1
                    or not isinstance(validation.get("logical_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", validation["logical_sha256"]) is None
                    or list(metric_state) != list(SCRATCH_SYNTHETIC_RESTORE_PHASES)
                    or any(
                        not {
                            "wal_before_bytes", "wal_after_bytes", "phase_started_ns",
                            "phase_ended_ns", "restore_command_started_ns",
                            "restore_command_ended_ns", "filesystem_peak_sampled_bytes",
                            "filesystem_capacity_bytes",
                            "memory_start_bytes", "memory_peak_bytes",
                        }.issubset(metric_state[phase])
                        for phase in SCRATCH_SYNTHETIC_RESTORE_PHASES
                    )
                ):
                    raise RuntimeError("Synthetic restore arm did not produce complete validation metrics.")
                phase_results: list[dict[str, Any]] = []
                for phase in SCRATCH_SYNTHETIC_RESTORE_PHASES:
                    state = metric_state[phase]
                    wal_delta_bytes = state["wal_after_bytes"] - state["wal_before_bytes"]
                    command_elapsed_ns = (
                        state["restore_command_ended_ns"]
                        - state["restore_command_started_ns"]
                    )
                    phase_elapsed_ns = state["phase_ended_ns"] - state["phase_started_ns"]
                    command_elapsed_us = (command_elapsed_ns + 999) // 1000
                    phase_elapsed_us = (phase_elapsed_ns + 999) // 1000
                    if (
                        wal_delta_bytes < 0
                        or command_elapsed_ns <= 0
                        or phase_elapsed_ns < command_elapsed_ns
                        or command_elapsed_us > SCRATCH_RUNTIME_SECONDS * 1_000_000
                        or phase_elapsed_us > SCRATCH_RUNTIME_SECONDS * 1_000_000
                        or state["memory_peak_bytes"] < state["memory_start_bytes"]
                    ):
                        raise RuntimeError("Synthetic restore phase metrics are outside their fixed bound.")
                    phase_result = {
                            "phase": phase,
                            "wal_before_bytes": state["wal_before_bytes"],
                            "wal_after_bytes": state["wal_after_bytes"],
                            "wal_delta_bytes": wal_delta_bytes,
                            "restore_command_elapsed_us": command_elapsed_us,
                            "phase_elapsed_us": phase_elapsed_us,
                            "memory_start_bytes": state["memory_start_bytes"],
                            "memory_peak_bytes": state["memory_peak_bytes"],
                            "memory_scope": SCRATCH_SYNTHETIC_MEMORY_SCOPE,
                            "wal_scope": SCRATCH_SYNTHETIC_WAL_SCOPE,
                            "elapsed_scope": SCRATCH_SYNTHETIC_ELAPSED_SCOPE,
                            "phase_elapsed_scope": SCRATCH_SYNTHETIC_PHASE_ELAPSED_SCOPE,
                        }
                    if scratch_backend == SCRATCH_BACKEND_TMPFS_V1:
                        phase_result.update(
                            {
                                "tmpfs_peak_sampled_bytes": state["filesystem_peak_sampled_bytes"],
                                "tmpfs_sample_period_ms": int(SCRATCH_SYNTHETIC_TMPFS_SAMPLE_SECONDS * 1000),
                                "tmpfs_scope": SCRATCH_SYNTHETIC_TMPFS_SCOPE,
                            }
                        )
                    else:
                        phase_result.update(
                            {
                                "disk_peak_sampled_bytes": state["filesystem_peak_sampled_bytes"],
                                "disk_capacity_bytes": state["filesystem_capacity_bytes"],
                                "disk_sample_period_ms": int(SCRATCH_SYNTHETIC_TMPFS_SAMPLE_SECONDS * 1000),
                                "disk_scope": SCRATCH_SYNTHETIC_DISK_SCOPE,
                            }
                        )
                    phase_results.append(phase_result)
                arm_results.append(
                    {
                        "name": name,
                        "wal_level": wal_level,
                        "max_wal_senders": max_wal_senders,
                        "single_transaction": single_transaction,
                        "restored_table_count": table_count,
                        "fixture_row_count": SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
                        "fixture_payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
                        "logical_sha256": validation["logical_sha256"],
                        "phases": phase_results,
                    }
                )
            finally:
                if sampler is not None:
                    sampler.stop()
                if target_started or (target_data / "postmaster.pid").exists():
                    stop_cluster(target_data)
                remove_cluster(target_data, target_socket, target_log)

        if archive is None or archive_initial is None:
            raise RuntimeError("Synthetic restore archive was not retained for all arms.")
        final_archive_info = os.fstat(archive.fileno())
        current_archive_info = archive_path.lstat()
        final_identity = (
            final_archive_info.st_dev,
            final_archive_info.st_ino,
            final_archive_info.st_size,
            final_archive_info.st_mtime_ns,
            final_archive_info.st_ctime_ns,
        )
        if final_identity != archive_initial or (
            current_archive_info.st_dev,
            current_archive_info.st_ino,
        ) != archive_initial[:2]:
            raise RuntimeError("Synthetic restore archive changed during its diagnostic arms.")
        report = {
            "kind": "synthetic_restore_diagnostic",
            "schema": 3,
            "status": "completed",
            "scratch_backend": scratch_backend,
            "scratch_filesystem_capacity_bytes": filesystem_capacity_bytes,
            "postgres_version": postgres_version,
            "source_fixture": SCRATCH_SYNTHETIC_FIXTURE_ID,
            "fixture_row_count": SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
            "fixture_payload_bytes": SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
            "archive_format": "pg_dump_custom",
            "archive_size_bytes": archive_initial[2],
            "archive_sha256": args._scratch_archive_sha256,
            "arms": arm_results,
        }
        if not valid_scratch_synthetic_restore_diagnostic(report):
            raise RuntimeError("Synthetic restore diagnostic did not complete all fixed arms.")
        return report
    finally:
        if archive is not None:
            archive.close()
        if peak_fd is not None:
            os.close(peak_fd)
        _scratch_cleanup_synthetic_cluster(
            (source_data, source_socket, source_log, target_data, target_socket, target_log, archive_path),
            root_device=root_device,
        )


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
    scratch_backend = getattr(args, "_scratch_backend", SCRATCH_BACKEND_TMPFS_V1)
    args._scratch_fault_site = "worker_root_identity"
    args._scratch_verification_site = "worker_root_path_identity"
    if (
        not re.fullmatch(r"/run/oldsparky-backup-restore-[0-9a-f]{24}", str(root))
        or not root.is_dir()
        or scratch_backend not in SCRATCH_BACKENDS
    ):
        raise RuntimeError("Scratch restore root is invalid.")
    if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        args._scratch_verification_site = "worker_root_mode_identity"
        if os.geteuid() != 0:
            raise RuntimeError("Scratch disk-image worker is not root before filesystem setup.")
        os.chmod(root, 0o700)
        root_info = root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != 0
            or root_info.st_gid != 0
            or stat.S_IMODE(root_info.st_mode) != 0o700
        ):
            raise RuntimeError("Scratch ext4 root could not be secured root:root mode 0700.")
    args._scratch_fault_site = "worker_mount_snapshot"
    args._scratch_verification_site = "worker_mount_identity"
    if scratch_backend == SCRATCH_BACKEND_TMPFS_V1:
        _scratch_mount_snapshot(root)
    else:
        _scratch_mount_snapshot(root, backend=scratch_backend)
    args._scratch_fault_site = "worker_cgroup_snapshot"
    args._scratch_verification_site = "worker_cgroup_identity"
    _scratch_cgroup_snapshot()
    args._scratch_fault_site = "worker_host_reserve"
    if getattr(args, "_scratch_authorization_probe", False):
        _raise_scratch_host_memory_reserve(
            "worker_after_start_before_authorization_probe",
            message="Scratch restore admission memory reserve is unavailable.",
        )
    else:
        _raise_scratch_host_memory_reserve(
            "worker_after_start_before_synthetic_diagnostic"
            if getattr(args, "_scratch_synthetic_restore_diagnostic", False)
            else "worker_after_start_before_archive_open",
            message="Scratch restore admission memory reserve is unavailable.",
        )
    if os.geteuid() != 0:
        raise RuntimeError("Scratch restore supervisor is not root.")
    postgres = None
    if (
        scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
        and not getattr(args, "_scratch_authorization_probe", False)
    ):
        args._scratch_fault_site = "worker_postgres_tools"
        args._scratch_verification_site = "worker_postgres_account_identity"
        postgres = pwd.getpwnam("postgres")
    scratch_expected_owner = (
        (postgres.pw_uid, postgres.pw_gid) if postgres is not None else None
    )
    if getattr(args, "_scratch_synthetic_restore_diagnostic", False):
        args._scratch_fault_site = "worker_entry"
        if getattr(args, "_scratch_authorization_probe", False) or any(
            getattr(args, name, None) is not None
            for name in (
                "_scratch_archive", "_scratch_archive_dev", "_scratch_archive_ino",
                "_scratch_archive_size", "_scratch_archive_sha256",
            )
        ):
            raise RuntimeError("Synthetic restore diagnostic must not receive archive inputs.")
        report = _scratch_synthetic_restore_diagnostic_worker(args, root)
        result_stream = getattr(sys.stdout, "buffer", None)
        if result_stream is None:
            raise RuntimeError("Synthetic restore diagnostic result channel is unavailable.")
        result_stream.write(
            (json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
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
            raise RuntimeError("Synthetic restore diagnostic completion was not acknowledged.") from exc
        if completion != SCRATCH_DONE_TOKEN:
            raise RuntimeError("Synthetic restore diagnostic completion was not acknowledged.")
        return 0
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

    if postgres is None:
        postgres = pwd.getpwnam("postgres")
    args._scratch_fault_site = "worker_directory_setup"
    if scratch_backend == SCRATCH_BACKEND_TMPFS_V1:
        os.chown(root, postgres.pw_uid, postgres.pw_gid)
    else:
        args._scratch_fault_site = "worker_disk_root_postgres_transition"
        _scratch_prepare_disk_root_for_postgres(
            root,
            postgres_uid=postgres.pw_uid,
            postgres_gid=postgres.pw_gid,
        )
        args._scratch_fault_site = "worker_directory_setup"
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
        if phase == "restore_platform" and event == "start":
            _scratch_emit_allocation_sample(args, root, "restore_platform_start")
        if phase == "restore_platform" and event == "end":
            _scratch_emit_allocation_sample(args, root, "restore_platform_end")

    _scratch_run_observed_phase(
        phase_observer,
        "initdb",
        lambda: as_postgres(
            _scratch_initdb_command(initdb, pgdata),
            capture_output=True,
        ),
    )
    started = False
    primary_error: BaseException | None = None
    table_count: int | None = None
    try:
        args._scratch_fault_site = "worker_after_initdb_guard"
        _scratch_resource_guard(
            root,
            guard_site="worker_after_initdb_before_postgres_start",
            scratch_backend=scratch_backend,
            expected_owner=scratch_expected_owner,
        )
        _scratch_emit_allocation_sample(args, root, "initdb_end")
        server_options = shlex.join(_scratch_postgres_start_options(socket_dir))
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
        _scratch_resource_guard(
            root,
            guard_site="worker_after_postgres_start",
            scratch_backend=scratch_backend,
            expected_owner=scratch_expected_owner,
        )
        _scratch_emit_allocation_sample(args, root, "postgres_start_end")
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
            _scratch_resource_guard(
                root,
                guard_site="worker_pg_restore_resource_guard",
                scratch_backend=scratch_backend,
                expected_owner=scratch_expected_owner,
            )

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
            root,
            guard_site="worker_after_restore_before_postgres_stop",
            scratch_backend=scratch_backend,
            expected_owner=scratch_expected_owner,
        )
    except BaseException as exc:
        if getattr(args, "private_scratch_diagnostics", False):
            worker_identity_sites = {
                "worker_root_identity": {"worker_root_path_identity", "worker_root_mode_identity"},
                "worker_mount_snapshot": {"worker_mount_identity"},
                "worker_cgroup_snapshot": {"worker_cgroup_identity"},
                "worker_postgres_tools": {"worker_postgres_account_identity"},
            }
            verification_site = getattr(args, "_scratch_verification_site", None)
            if verification_site in worker_identity_sites.get(
                getattr(args, "_scratch_fault_site", "worker_entry"), set()
            ):
                setattr(exc, "scratch_verification_site", verification_site)
            _retain_first_scratch_fault(
                exc,
                origin="worker",
                site=getattr(args, "_scratch_fault_site", "worker_entry"),
            )
            _scratch_emit_allocation_sample(args, root, "worker_failure")
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


def _scratch_create_application_role(
    admin_target: DatabaseTarget, *, psql_binary: str = "psql"
) -> None:
    run_command(
        [
            psql_binary,
            "--no-psqlrc",
            *connection_args(admin_target),
            "--command",
            f"CREATE ROLE {SCRATCH_APP_ROLE} LOGIN;",
        ],
        target=admin_target,
        capture_output=True,
    )


def _scratch_initdb_command(initdb: str, pgdata: pathlib.Path) -> list[str]:
    """Apply the same bounded shared-buffer setting during bootstrap and runtime."""
    return [
        initdb,
        "--pgdata",
        str(pgdata),
        "--username=postgres",
        "--no-locale",
        "--encoding=UTF8",
        "--set",
        f"shared_buffers={SCRATCH_SHARED_BUFFERS_MB}MB",
        "--auth-local=trust",
        "--auth-host=reject",
    ]


def _scratch_postgres_start_options(socket_dir: pathlib.Path) -> list[str]:
    """Return isolated restore settings passed through pg_ctl.

    The WAL limits are scratch-cluster checkpoint/recycling thresholds, not
    hard tmpfs limits. Durability settings remain enabled.
    """
    return [
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
        f"shared_buffers={SCRATCH_SHARED_BUFFERS_MB}MB",
        "-c",
        "work_mem=4MB",
        "-c",
        "maintenance_work_mem=32MB",
        "-c",
        "max_connections=10",
        "-c",
        "max_wal_size=64MB",
        "-c",
        "min_wal_size=32MB",
    ]


def _scratch_postgres_start_options_for_diagnostic_arm(
    socket_dir: pathlib.Path, arm: tuple[str, str, int, bool]
) -> list[str]:
    """Apply only the fixed diagnostic WAL variants to a disposable cluster."""
    if arm not in SCRATCH_SYNTHETIC_RESTORE_ARMS:
        raise ValueError("Synthetic restore diagnostic arm is not recognized.")
    options = _scratch_postgres_start_options(socket_dir)
    name, wal_level, max_wal_senders, _single_transaction = arm
    if name != "baseline":
        if (wal_level, max_wal_senders) != ("minimal", 0):
            raise ValueError("Synthetic restore diagnostic WAL settings are invalid.")
        options.extend(("-c", "wal_level=minimal", "-c", "max_wal_senders=0"))
    elif (wal_level, max_wal_senders) != ("replica", 10):
        raise ValueError("Synthetic baseline WAL settings are invalid.")
    return options


def _scratch_open_memory_peak_fd(args: argparse.Namespace) -> int:
    """Open the verified unit's cgroup-v2 peak counter for per-arm sampling."""
    root_name = pathlib.Path(args._scratch_root).name
    match = re.fullmatch(r"oldsparky-backup-restore-([0-9a-f]{24})", root_name)
    if match is None:
        raise RuntimeError("Synthetic restore diagnostic unit identity is invalid.")
    expected_group = f"/system.slice/oldsparky-backup-restore-{match.group(1)}.service"
    control_group = None
    for row in pathlib.Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            control_group = fields[2]
            break
    if control_group != expected_group:
        raise RuntimeError("Synthetic restore diagnostic is outside its verified unit cgroup.")
    cgroup_root = pathlib.Path("/sys/fs/cgroup").resolve(strict=True)
    cgroup_directory = (cgroup_root / control_group.lstrip("/")).resolve(strict=True)
    cgroup_directory.relative_to(cgroup_root)
    descriptor = os.open(
        cgroup_directory / "memory.peak",
        os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("Synthetic restore cgroup peak counter is not a regular interface file.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _scratch_read_or_reset_memory_peak(descriptor: int, *, reset: bool = False) -> int:
    if reset:
        os.lseek(descriptor, 0, os.SEEK_SET)
        written = os.write(descriptor, b"reset\n")
        if written != 6:
            raise RuntimeError("Synthetic restore cgroup peak counter could not be reset.")
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, 64)
    value = raw.decode("ascii").strip()
    if not value.isdecimal():
        raise RuntimeError("Synthetic restore cgroup peak counter is invalid.")
    peak = int(value)
    if not 0 < peak <= SCRATCH_MEMORY_MAX_BYTES:
        raise RuntimeError("Synthetic restore cgroup peak exceeded the fixed memory bound.")
    return peak


def _scratch_tmpfs_used_bytes(root: pathlib.Path) -> int:
    statvfs = os.statvfs(root)
    total = statvfs.f_blocks * statvfs.f_frsize
    free = statvfs.f_bavail * statvfs.f_frsize
    used = total - free
    if total != SCRATCH_TMPFS_BYTES or not 0 <= used <= total:
        raise RuntimeError("Synthetic restore tmpfs measurement is outside its fixed bound.")
    return used


class _ScratchFilesystemPeakSampler:
    """Capture the bounded backend-specific filesystem high-water mark."""

    def __init__(
        self,
        root: pathlib.Path,
        *,
        backend: str = SCRATCH_BACKEND_TMPFS_V1,
        postgres_owner: tuple[int, int] | None = None,
    ) -> None:
        if type(backend) is not str or backend not in SCRATCH_BACKENDS:
            raise ValueError("Scratch filesystem sampler backend is unsupported.")
        if backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1 and postgres_owner is None:
            raise ValueError("Disk filesystem sampler requires the exact postgres owner.")
        self.root = root
        self.backend = backend
        self.postgres_owner = postgres_owner
        self.stop_event = threading.Event()
        self.peak_bytes = 0
        self.capacity_bytes: int | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="scratch-filesystem-sampler",
            daemon=True,
        )

    def _sample(self) -> None:
        if self.backend == SCRATCH_BACKEND_TMPFS_V1:
            capacity = SCRATCH_TMPFS_BYTES
            used = _scratch_tmpfs_used_bytes(self.root)
        else:
            assert self.postgres_owner is not None
            snapshot = _scratch_disk_mount_snapshot(
                self.root, expected_owner=self.postgres_owner
            )
            capacity = int(snapshot["total_bytes"])
            used = capacity - int(snapshot["free_bytes"])
            if used < 0 or used > capacity - SCRATCH_TMPFS_STOP_BYTES:
                raise RuntimeError("Synthetic restore ext4 measurement is outside its stop bound.")
        if self.capacity_bytes is not None and capacity != self.capacity_bytes:
            raise RuntimeError("Synthetic restore filesystem capacity changed during sampling.")
        self.capacity_bytes = capacity
        self.peak_bytes = max(self.peak_bytes, used)

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                self._sample()
                self.stop_event.wait(SCRATCH_SYNTHETIC_TMPFS_SAMPLE_SECONDS)
        except BaseException as exc:
            self.error = exc
            self.stop_event.set()

    def start(self) -> None:
        self._sample()
        self.thread.start()

    def stop(self) -> int:
        self.stop_event.set()
        self.thread.join(timeout=2.0)
        if self.thread.is_alive():
            raise RuntimeError("Synthetic restore tmpfs sampler could not be reaped.")
        if self.error is not None:
            raise RuntimeError("Synthetic restore tmpfs sampler failed.") from self.error
        self._sample()
        return self.peak_bytes


def _scratch_pg_stat_wal_bytes(
    target: DatabaseTarget, *, psql_binary: str = "psql"
) -> int:
    result = run_command(
        [
            psql_binary, "--no-psqlrc", "--tuples-only", "--no-align",
            *connection_args(target), "--command", "SELECT wal_bytes FROM pg_stat_wal;",
        ],
        target=target,
        capture_output=True,
    )
    raw = result.stdout.strip()
    if not raw.isdecimal():
        raise RuntimeError("Synthetic restore WAL counter is invalid.")
    value = int(raw)
    if value < 0 or value > SCRATCH_TMPFS_BYTES:
        raise RuntimeError("Synthetic restore WAL counter exceeded the fixed fixture bound.")
    return value


def _scratch_wait_for_restore_database_backends_to_exit(
    control_target: DatabaseTarget,
    restore_database: str,
    *,
    psql_binary: str = "psql",
) -> None:
    """Wait until the exact restore DB has flushed and removed its backends."""
    if (
        control_target.database == restore_database
        or re.fullmatch(
            r"platform_restore_drill_\d{8}t\d{6}z_arm[1-3]_\d+",
            restore_database,
        )
        is None
    ):
        raise RuntimeError("Synthetic restore backend observer target is invalid.")

    deadline = time.monotonic() + SCRATCH_SYNTHETIC_WAL_BACKEND_DRAIN_SECONDS
    # The fullmatch above limits this generated name to a safe SQL literal.
    # psql -c accepts server SQL and does not perform psql variable interpolation.
    query = (
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname = '{restore_database}';"
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Synthetic restore database backends did not exit before the WAL read.")
        command = [
            psql_binary,
            "--no-psqlrc",
            "--quiet",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            *connection_args(control_target),
            "--command",
            query,
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
                env=command_env(control_target),
                timeout=min(SCRATCH_SYNTHETIC_WAL_QUERY_TIMEOUT_SECONDS, remaining),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Synthetic restore backend-exit observer failed.") from exc
        raw = result.stdout.strip()
        if not raw.isdecimal():
            raise RuntimeError("Synthetic restore backend-exit observer returned an invalid count.")
        backend_count = int(raw)
        if backend_count == 0:
            return
        if backend_count > 13:
            raise RuntimeError("Synthetic restore backend-exit observer exceeded its fixed bound.")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(SCRATCH_SYNTHETIC_WAL_POLL_SECONDS, remaining))


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
    synthetic_restore_diagnostic: bool = False,
    scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1,
    scratch_image_path: pathlib.Path | None = None,
) -> list[str]:
    if scratch_backend not in SCRATCH_BACKENDS:
        raise ValueError("Scratch restore backend is not supported.")
    if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        if (
            scratch_image_path is None
            or scratch_image_path.parent != SCRATCH_DISK_IMAGE_ROOT
        ):
            raise ValueError("Disk-image scratch backend requires one exact image and a supported protocol.")
        if authorization_probe and any(
            value is not None for value in (archive_path, archive_stat, archive_sha256)
        ):
            raise ValueError("Disk-image authorization probe cannot bind archive inputs.")
    elif scratch_image_path is not None:
        raise ValueError("Tmpfs scratch backend cannot bind a disk image.")
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
        *([] if synthetic_restore_diagnostic else ["--collect"]),
        f"--unit={unit_name}",
        "--property=Type=exec",
        *(
            [f"--property=MountImages={scratch_image_path}:{mountpoint}:rw,nosuid,nodev,noexec"]
            if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
            else [f"--property=TemporaryFileSystem={mountpoint}:rw,size={SCRATCH_TMPFS_BYTES},mode=0700"]
        ),
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
        *(
            f"--property=InaccessiblePaths={path}"
            for path in _SCRATCH_INACCESSIBLE_PATHS
        ),
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
        "--_scratch-backend",
        scratch_backend,
        "--_scratch-timestamp-slug",
        timestamp_slug,
    ]
    if synthetic_restore_diagnostic:
        command.insert(command.index("--property=Type=exec"), "--property=CollectMode=inactive")
    if authorization_probe and synthetic_restore_diagnostic:
        raise ValueError("Scratch authorization probe and synthetic diagnostic are mutually exclusive.")
    if authorization_probe:
        if archive_path is not None or archive_stat is not None or archive_sha256 is not None:
            raise ValueError("Authorization probe cannot bind archive inputs.")
        command.append("--_scratch-authorization-probe")
    elif synthetic_restore_diagnostic:
        if archive_path is not None or archive_stat is not None or archive_sha256 is not None:
            raise ValueError("Synthetic restore diagnostic cannot bind archive inputs.")
        command.append(SCRATCH_SYNTHETIC_RESTORE_DIAGNOSTIC_MODE)
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
    *,
    stderr_tail_buffer: bytearray | None = None,
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
                if stderr_tail_buffer is not None:
                    _scratch_append_bounded_tail(
                        stderr_tail_buffer, data, SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES
                    )
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


def _scratch_append_bounded_tail(buffer: bytearray, data: bytes, cap: int) -> None:
    if type(buffer) is not bytearray or type(data) is not bytes or type(cap) is not int or cap <= 0:
        raise ValueError("Scratch diagnostic tail buffer is invalid.")
    if len(data) >= cap:
        buffer[:] = data[-cap:]
        return
    overflow = len(buffer) + len(data) - cap
    if overflow > 0:
        del buffer[:overflow]
    buffer.extend(data)


def _scratch_drain_phase_prefix_now(
    process: subprocess.Popen[bytes],
    stderr_buffer: bytearray,
    stderr_observed: int,
    *,
    stderr_tail_buffer: bytearray | None = None,
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
        if stderr_tail_buffer is not None:
            _scratch_append_bounded_tail(
                stderr_tail_buffer, block, SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES
            )
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
        "ExecMainCode",
        "ExecMainStatus",
        "CollectMode",
        "Result",
        "TemporaryFileSystem",
        "MountImages",
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


def valid_scratch_synthetic_restore_diagnostic(value: Any) -> bool:
    """Validate the diagnostic-only result; it is never a backup receipt."""
    if not isinstance(value, dict):
        return False
    schema = value.get("schema")
    if schema == 2:
        expected_top_keys = {
            "kind", "schema", "status", "postgres_version", "source_fixture",
            "fixture_row_count", "fixture_payload_bytes", "archive_format",
            "archive_size_bytes", "archive_sha256", "arms",
        }
        backend = SCRATCH_BACKEND_TMPFS_V1
        filesystem_capacity = SCRATCH_TMPFS_BYTES
    elif schema == 3:
        expected_top_keys = {
            "kind", "schema", "status", "scratch_backend",
            "scratch_filesystem_capacity_bytes", "postgres_version", "source_fixture",
            "fixture_row_count", "fixture_payload_bytes", "archive_format",
            "archive_size_bytes", "archive_sha256", "arms",
        }
        backend = value.get("scratch_backend")
        filesystem_capacity = value.get("scratch_filesystem_capacity_bytes")
        if (
            type(backend) is not str
            or backend not in SCRATCH_BACKENDS
            or type(filesystem_capacity) is not int
            or (
                backend == SCRATCH_BACKEND_TMPFS_V1
                and filesystem_capacity != SCRATCH_TMPFS_BYTES
            )
            or (
                backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                and not SCRATCH_DISK_USABLE_MIN_BYTES
                <= filesystem_capacity
                <= SCRATCH_DISK_IMAGE_BYTES
            )
        ):
            return False
    else:
        return False
    if set(value) != expected_top_keys:
        return False
    archive_sha256 = value.get("archive_sha256")
    arms = value.get("arms")
    if (
        value.get("kind") != "synthetic_restore_diagnostic"
        or type(value.get("schema")) is not int
        or value["schema"] not in (2, 3)
        or value.get("status") != "completed"
        or type(value.get("postgres_version")) is not str
        or re.fullmatch(r"16\.\d+", value["postgres_version"]) is None
        or value.get("source_fixture") != SCRATCH_SYNTHETIC_FIXTURE_ID
        or type(value.get("fixture_row_count")) is not int
        or value["fixture_row_count"] != SCRATCH_SYNTHETIC_PAYLOAD_ROWS
        or type(value.get("fixture_payload_bytes")) is not int
        or value["fixture_payload_bytes"] != SCRATCH_SYNTHETIC_PAYLOAD_BYTES
        or value.get("archive_format") != "pg_dump_custom"
        or type(value.get("archive_size_bytes")) is not int
        or not 5 < value["archive_size_bytes"] <= SCRATCH_SYNTHETIC_ARCHIVE_MAX_BYTES
        or type(archive_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None
        or not isinstance(arms, list)
        or len(arms) != len(SCRATCH_SYNTHETIC_RESTORE_ARMS)
    ):
        return False
    expected_names = [arm[0] for arm in SCRATCH_SYNTHETIC_RESTORE_ARMS]
    arm_keys = {
        "name", "wal_level", "max_wal_senders", "single_transaction",
        "restored_table_count", "fixture_row_count", "fixture_payload_bytes",
        "logical_sha256", "phases",
    }
    for index, arm in enumerate(arms):
        expected = SCRATCH_SYNTHETIC_RESTORE_ARMS[index]
        if (
            not isinstance(arm, dict)
            or set(arm) != arm_keys
            or arm.get("name") != expected_names[index]
            or arm.get("wal_level") != expected[1]
            or type(arm.get("max_wal_senders")) is not int
            or arm["max_wal_senders"] != expected[2]
            or type(arm.get("single_transaction")) is not bool
            or arm["single_transaction"] is not expected[3]
            or type(arm.get("restored_table_count")) is not int
            or arm["restored_table_count"] <= 0
            or type(arm.get("fixture_row_count")) is not int
            or arm["fixture_row_count"] != SCRATCH_SYNTHETIC_PAYLOAD_ROWS
            or type(arm.get("fixture_payload_bytes")) is not int
            or arm["fixture_payload_bytes"] != SCRATCH_SYNTHETIC_PAYLOAD_BYTES
            or type(arm.get("logical_sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", arm["logical_sha256"]) is None
            or not isinstance(arm.get("phases"), list)
            or len(arm["phases"]) != len(SCRATCH_SYNTHETIC_RESTORE_PHASES)
        ):
            return False
        for phase_index, phase in enumerate(arm["phases"]):
            common_phase_keys = {
                "phase", "wal_before_bytes", "wal_after_bytes", "wal_delta_bytes",
                "restore_command_elapsed_us", "phase_elapsed_us",
                "memory_start_bytes", "memory_peak_bytes",
                "memory_scope", "wal_scope", "elapsed_scope", "phase_elapsed_scope",
            }
            if schema == 2 or backend == SCRATCH_BACKEND_TMPFS_V1:
                phase_keys = common_phase_keys | {
                    "tmpfs_peak_sampled_bytes", "tmpfs_sample_period_ms", "tmpfs_scope"
                }
                peak_key = "tmpfs_peak_sampled_bytes"
                period_key = "tmpfs_sample_period_ms"
                scope_key = "tmpfs_scope"
                capacity = SCRATCH_TMPFS_BYTES
                expected_scope = SCRATCH_SYNTHETIC_TMPFS_SCOPE
            else:
                phase_keys = common_phase_keys | {
                    "disk_peak_sampled_bytes", "disk_capacity_bytes",
                    "disk_sample_period_ms", "disk_scope",
                }
                peak_key = "disk_peak_sampled_bytes"
                period_key = "disk_sample_period_ms"
                scope_key = "disk_scope"
                capacity = filesystem_capacity
                expected_scope = SCRATCH_SYNTHETIC_DISK_SCOPE
            if not isinstance(phase, dict) or set(phase) != phase_keys:
                return False
            integer_fields = (
                "wal_before_bytes", "wal_after_bytes", "wal_delta_bytes",
                "restore_command_elapsed_us", "phase_elapsed_us",
                "memory_start_bytes", "memory_peak_bytes", peak_key, period_key,
            )
            if schema == 3 and backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
                integer_fields += ("disk_capacity_bytes",)
            if (
                phase.get("phase") != SCRATCH_SYNTHETIC_RESTORE_PHASES[phase_index]
                or any(type(phase.get(field)) is not int for field in integer_fields)
                or not 0 <= phase["wal_before_bytes"] <= 2**63 - 1
                or not 0 <= phase["wal_after_bytes"] <= 2**63 - 1
                or phase["wal_delta_bytes"]
                != phase["wal_after_bytes"] - phase["wal_before_bytes"]
                or not 0 <= phase["wal_delta_bytes"] <= SCRATCH_TMPFS_BYTES
                or not 0 < phase["restore_command_elapsed_us"]
                <= SCRATCH_RUNTIME_SECONDS * 1_000_000
                or not phase["restore_command_elapsed_us"] <= phase["phase_elapsed_us"]
                <= SCRATCH_RUNTIME_SECONDS * 1_000_000
                or not 0 < phase["memory_start_bytes"] <= SCRATCH_MEMORY_MAX_BYTES
                or not phase["memory_start_bytes"] <= phase["memory_peak_bytes"] <= SCRATCH_MEMORY_MAX_BYTES
                or not 0 <= phase[peak_key] <= (
                    capacity - SCRATCH_TMPFS_STOP_BYTES
                    if backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                    else capacity
                )
                or phase[period_key]
                != int(SCRATCH_SYNTHETIC_TMPFS_SAMPLE_SECONDS * 1000)
                or (
                    schema == 3
                    and backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                    and phase["disk_capacity_bytes"] != filesystem_capacity
                )
                or phase.get("memory_scope") != SCRATCH_SYNTHETIC_MEMORY_SCOPE
                or phase.get("wal_scope") != SCRATCH_SYNTHETIC_WAL_SCOPE
                or phase.get("elapsed_scope") != SCRATCH_SYNTHETIC_ELAPSED_SCOPE
                or phase.get("phase_elapsed_scope") != SCRATCH_SYNTHETIC_PHASE_ELAPSED_SCOPE
                or phase.get(scope_key) != expected_scope
            ):
                return False
    logical_digests = {arm["logical_sha256"] for arm in arms}
    return len(logical_digests) == 1


def _scratch_synthetic_worker_candidate(stdout: bytes) -> dict[str, Any] | None:
    """Accept only one closed diagnostic frame with all three fixed arms."""
    if (
        not stdout
        or len(stdout) > SCRATCH_SYNTHETIC_RESULT_MAX_BYTES
        or not stdout.endswith(b"\n")
    ):
        return None
    payload = stdout[:-1]
    if not payload or b"\n" in payload or b"\r" in payload:
        raise RuntimeError("Scratch synthetic diagnostic result framing is invalid.")
    try:
        decoded = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Scratch synthetic diagnostic returned invalid JSON.") from exc
    if not valid_scratch_synthetic_restore_diagnostic(decoded):
        raise RuntimeError("Scratch synthetic diagnostic result failed validation.")
    return decoded


def _scratch_synthetic_worker_failure_candidate(
    stdout: bytes,
    stderr: bytes,
    *,
    stderr_eof: bool,
) -> tuple[str, dict[str, Any] | None]:
    """Recognize only the child's fixed, canonical failure report/frame pair.

    The report is emitted before its stderr frame. A valid report with an
    incomplete frame stays pending so the success parser never sees it and no
    completion token can be sent prematurely.
    """
    if (
        not stdout
        or len(stdout) > SCRATCH_SYNTHETIC_RESULT_MAX_BYTES
        or not stdout.endswith(b"\n")
    ):
        return "not_failure", None
    try:
        decoded = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "not_failure", None
    if not isinstance(decoded, dict) or set(decoded) != {
        "ok", "error", "scratch_first_fault"
    }:
        return "not_failure", None
    fault = decoded.get("scratch_first_fault")
    if (
        decoded.get("ok") is not False
        or decoded.get("error") != "Synthetic restore worker failed."
        or not valid_scratch_first_fault(fault)
        or fault.get("origin") != "worker"
        or stdout
        != json.dumps(decoded, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    ):
        return "invalid", None
    expected_frame = _scratch_first_fault_frame(fault)
    if expected_frame is None:
        return "invalid", None
    if stderr == expected_frame:
        bound = _parse_bound_scratch_first_fault(
            stdout,
            stderr,
            stdout_truncated=False,
            stderr_truncated=False,
        )
        return ("valid", fault) if bound == fault else ("invalid", None)
    if not stderr_eof and expected_frame.startswith(stderr):
        return "pending", None
    return "invalid", None


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
    synthetic_restore_diagnostic: bool = False,
    scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1,
    scratch_image_path: pathlib.Path | None = None,
) -> str:
    operation = ["service_properties_shape"]
    try:
        return _scratch_expected_properties_impl(
            snapshot,
            unit_name=unit_name,
            mountpoint=mountpoint,
            python_binary=python_binary,
            authorization_probe=authorization_probe,
            synthetic_restore_diagnostic=synthetic_restore_diagnostic,
            scratch_backend=scratch_backend,
            scratch_image_path=scratch_image_path,
            operation=operation,
        )
    except BaseException as exc:
        if _scratch_verification_site(exc) == "none":
            setattr(exc, "scratch_verification_site", operation[0])
        raise


def _scratch_expected_properties_impl(
    snapshot: dict[str, Any],
    *,
    unit_name: str,
    mountpoint: pathlib.Path,
    python_binary: pathlib.Path,
    authorization_probe: bool,
    synthetic_restore_diagnostic: bool,
    scratch_backend: str,
    scratch_image_path: pathlib.Path | None,
    operation: list[str],
) -> str:
    def fail(site: str, message: str, cause: BaseException | None = None) -> None:
        error = RuntimeError(message)
        setattr(error, "scratch_verification_site", site)
        if cause is not None:
            raise error from cause
        raise error

    if authorization_probe and synthetic_restore_diagnostic:
        raise ValueError("Scratch authorization probe and synthetic diagnostic are mutually exclusive.")
    operation[0] = "service_properties_shape"
    properties = snapshot.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Scratch restore service properties are malformed.")
    operation[0] = "service_resource_policy"
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
    operation[0] = "service_identity_state"
    if properties.get("ReadWritePaths") not in {None, ""}:
        raise RuntimeError("Scratch restore service has an unexpected writable-path override.")
    if properties.get("LoadState") != "loaded" or properties.get("ActiveState") != "active":
        raise RuntimeError("Scratch restore service is not active while it is being checked.")
    if synthetic_restore_diagnostic and properties.get("CollectMode") != "inactive":
        raise RuntimeError("Synthetic restore unit does not retain failed state for diagnosis.")
    operation[0] = "service_cgroup_identity"
    control_group = properties.get("ControlGroup", "")
    if not control_group.startswith("/system.slice/") or ".." in pathlib.PurePosixPath(control_group).parts:
        raise RuntimeError("Scratch restore service cgroup is invalid.")
    if control_group != f"/system.slice/{unit_name}":
        raise RuntimeError("Scratch restore service cgroup does not match its unique unit.")
    operation[0] = "service_pid_identity"
    pid_text = properties.get("ExecMainPID", "0")
    if pid_text in {"", "0"} or not pid_text.isdecimal():
        raise RuntimeError("Scratch restore worker process identity is unavailable.")
    try:
        operation[0] = "process_cgroup_read"
        process_cgroup = None
        for row in pathlib.Path(f"/proc/{pid_text}/cgroup").read_text(encoding="ascii").splitlines():
            operation[0] = "process_cgroup_parse"
            fields = row.split(":", 2)
            if len(fields) == 3 and fields[0] == "0":
                process_cgroup = fields[2]
                break
    except OSError as exc:
        fail("process_cgroup_read", "Scratch restore worker process identity disappeared during verification.", exc)
    if process_cgroup != control_group:
        fail("process_cgroup_mismatch", "Scratch restore worker is outside its reviewed systemd cgroup.")
    try:
        operation[0] = "expected_executable_stat"
        expected_python = python_binary.stat()
    except OSError as exc:
        fail("expected_executable_stat", "Scratch restore interpreter identity disappeared during verification.", exc)
    try:
        operation[0] = "proc_exe_stat"
        running_python = pathlib.Path(f"/proc/{pid_text}/exe").stat()
    except OSError as exc:
        fail("proc_exe_stat", "Scratch restore interpreter identity disappeared during verification.", exc)
    operation[0] = "proc_exe_mismatch"
    if (running_python.st_dev, running_python.st_ino) != (
        expected_python.st_dev,
        expected_python.st_ino,
    ):
        fail("proc_exe_mismatch", "Scratch restore interpreter does not match the pinned host executable.")
    if scratch_backend == SCRATCH_BACKEND_TMPFS_V1:
        operation[0] = "backend_mount_identity"
        tmpfs = properties.get("TemporaryFileSystem", "")
        if str(mountpoint) not in tmpfs or properties.get("MountImages", ""):
            raise RuntimeError("Scratch restore tmpfs request is not present in the active unit.")
    elif scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        operation[0] = "backend_mount_identity"
        mount_images = properties.get("MountImages", "")
        if (
            scratch_image_path is None
            or f"{scratch_image_path}:{mountpoint}:" not in mount_images
            or properties.get("TemporaryFileSystem", "")
        ):
            raise RuntimeError("Scratch restore disk image request is not present in the active unit.")
    else:
        operation[0] = "backend_mount_identity"
        raise RuntimeError("Scratch restore backend is not supported.")
    operation[0] = "credential_visibility"
    inaccessible = properties.get("InaccessiblePaths", "")
    for required in _SCRATCH_INACCESSIBLE_PATHS:
        if required not in inaccessible:
            raise RuntimeError("Scratch restore service can see an out-of-scope credential or cluster path.")
    bind_paths = properties.get("BindReadOnlyPaths", "")
    required_binds = [
        str(mountpoint / "platform_backup_restore_drill.py"),
        str(mountpoint / "platform_disk_policy.py"),
    ]
    if not authorization_probe and not synthetic_restore_diagnostic:
        required_binds.insert(0, str(mountpoint / "archive.dump"))
    operation[0] = "readonly_input_binds"
    for required in required_binds:
        if required not in bind_paths:
            raise RuntimeError("Scratch restore service is missing a required read-only input bind.")
    if authorization_probe and str(mountpoint / "archive.dump") in bind_paths:
        operation[0] = "archive_exclusion"
        raise RuntimeError("Scratch authorization probe has an unexpected archive bind.")
    if synthetic_restore_diagnostic and str(mountpoint / "archive.dump") in bind_paths:
        operation[0] = "archive_exclusion"
        raise RuntimeError("Synthetic restore diagnostic has an unexpected archive bind.")
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


def _scratch_synthetic_protected_path_targets(
    *, scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1
) -> tuple[pathlib.Path, ...]:
    if scratch_backend not in SCRATCH_BACKENDS:
        raise RuntimeError("Synthetic protected path fixture backend is unsupported.")
    targets: list[pathlib.Path] = []
    var_run = pathlib.Path("/var/run")
    try:
        if not var_run.is_symlink() or var_run.resolve(strict=True) != pathlib.Path("/run"):
            raise RuntimeError("Synthetic protected path fixture found an unsupported /var/run alias.")
    except OSError as exc:
        raise RuntimeError("Synthetic protected path fixture could not verify /var/run.") from exc
    for value in _SCRATCH_INACCESSIBLE_PATHS:
        target = pathlib.Path(value)
        if value == "/var/run/postgresql" and var_run.is_symlink():
            target = pathlib.Path("/run/postgresql")
        if not target.is_absolute() or ".." in target.parts:
            raise RuntimeError("Synthetic protected path fixture contains an invalid fixed target.")
        if target not in targets:
            targets.append(target)
    if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
        for target in (SCRATCH_DISK_IMAGE_ROOT.parent, SCRATCH_DISK_IMAGE_ROOT):
            if target not in targets:
                targets.append(target)
    return tuple(targets)


def _scratch_validate_synthetic_disk_image_fixture_dirs(
    *, paths: tuple[pathlib.Path, ...] | None = None
) -> None:
    """Pin the fixed disk-image parent chain before creating an image or unit."""
    if paths is None:
        paths = (
            pathlib.Path("/var"),
            pathlib.Path("/var/lib"),
            SCRATCH_DISK_IMAGE_ROOT.parent,
            SCRATCH_DISK_IMAGE_ROOT,
        )
    if len(paths) != 4 or any(not path.is_absolute() for path in paths):
        raise RuntimeError("Synthetic disk image fixture parent chain is invalid.")
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise RuntimeError("Synthetic disk image fixture cannot pin its parent chain.")
    for index, path in enumerate(paths):
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            os.set_inheritable(descriptor, False)
            fd_info = os.fstat(descriptor)
            path_info = path.lstat()
            expected_mode = 0o700 if index >= 2 else None
            invalid = (
                not stat.S_ISDIR(fd_info.st_mode)
                or stat.S_ISLNK(fd_info.st_mode)
                or not stat.S_ISDIR(path_info.st_mode)
                or stat.S_ISLNK(path_info.st_mode)
                or (fd_info.st_dev, fd_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)
                or fd_info.st_uid != 0
                or fd_info.st_gid != 0
                or path_info.st_uid != 0
                or path_info.st_gid != 0
                or (
                    expected_mode is not None
                    and (
                        stat.S_IMODE(fd_info.st_mode) != expected_mode
                        or stat.S_IMODE(path_info.st_mode) != expected_mode
                    )
                )
                or (
                    expected_mode is None
                    and (
                        stat.S_IMODE(fd_info.st_mode) & 0o022
                        or stat.S_IMODE(path_info.st_mode) & 0o022
                    )
                )
            )
            if invalid:
                raise RuntimeError(
                    "Synthetic disk image fixture parent identity or mode is unsafe."
                )
        except OSError as exc:
            raise RuntimeError(
                "Synthetic disk image fixture parent could not be pinned."
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _scratch_create_missing_protected_path_directories(
    targets: tuple[pathlib.Path, ...], *, owner_uid: int, owner_gid: int
) -> tuple[tuple[str, int, int], ...]:
    """Create only absent fixed target directories and retain exact identities."""
    created: list[tuple[str, int, int]] = []
    try:
        for target_index, target in enumerate(targets):
            if not target.is_absolute() or ".." in target.parts:
                raise RuntimeError("Synthetic protected path fixture target is invalid.")
            current = pathlib.Path("/")
            for part in target.parts[1:]:
                current = current / part
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    info = None
                if info is None:
                    try:
                        parent_info = current.parent.lstat()
                    except OSError as exc:
                        raise RuntimeError(
                            "Synthetic protected path fixture parent could not be verified."
                        ) from exc
                    rejected_predicates: list[str] = []
                    if not stat.S_ISDIR(parent_info.st_mode):
                        rejected_predicates.append("not_directory")
                    if stat.S_ISLNK(parent_info.st_mode):
                        rejected_predicates.append("symlink")
                    if parent_info.st_uid != owner_uid:
                        rejected_predicates.append("uid_mismatch")
                    if parent_info.st_gid != owner_gid:
                        rejected_predicates.append("gid_mismatch")
                    if stat.S_IMODE(parent_info.st_mode) & 0o022:
                        rejected_predicates.append("group_or_world_writable")
                    if rejected_predicates:
                        rejection = {
                            "schema": 1,
                            "target_index": target_index,
                            "parent_component_index": len(current.parent.parts) - 1,
                            "predicates": rejected_predicates,
                        }
                        failure = RuntimeError(
                            "Synthetic protected path fixture parent is not safely owned."
                        )
                        if valid_scratch_synthetic_path_fixture_rejection(rejection):
                            setattr(
                                failure,
                                "scratch_synthetic_path_fixture_rejection",
                                rejection,
                            )
                        raise failure
                    try:
                        os.mkdir(current, 0o700)
                        # Keep an unpinned ledger entry until lstat confirms the
                        # inode. If that read fails, cleanup must not claim success
                        # or remove a path whose identity is ambiguous.
                        created.append((str(current), -1, -1))
                        try:
                            info = current.lstat()
                        except OSError as exc:
                            raise RuntimeError(
                                "Synthetic protected path fixture directory could not be created."
                            ) from exc
                    except OSError as exc:
                        raise RuntimeError(
                            "Synthetic protected path fixture directory could not be created."
                        ) from exc
                    created[-1] = (str(current), info.st_dev, info.st_ino)
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or stat.S_ISLNK(info.st_mode)
                        or info.st_uid != owner_uid
                        or info.st_gid != owner_gid
                        or stat.S_IMODE(info.st_mode) != 0o700
                    ):
                        raise RuntimeError(
                            "Synthetic protected path fixture directory identity is unsafe."
                        )
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise RuntimeError(
                        "Synthetic protected path fixture target is not a real directory."
                    )
    except BaseException as exc:
        cleanup_confirmed = True
        try:
            _scratch_remove_created_protected_path_directories(
                tuple(created), owner_uid=owner_uid, owner_gid=owner_gid
            )
        except BaseException:
            cleanup_confirmed = False
        setattr(exc, "scratch_synthetic_path_fixture_created", tuple(created))
        setattr(exc, "scratch_synthetic_path_fixture_cleanup_confirmed", cleanup_confirmed)
        raise
    return tuple(created)


def _scratch_remove_created_protected_path_directories(
    created: tuple[tuple[str, int, int], ...], *, owner_uid: int, owner_gid: int
) -> None:
    """Remove only empty fixture directories whose pinned identities still match."""
    cleanup_failed = False
    for path_text, device, inode in reversed(created):
        path = pathlib.Path(path_text)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            cleanup_failed = True
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (info.st_dev, info.st_ino) != (device, inode)
            or info.st_uid != owner_uid
            or info.st_gid != owner_gid
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            cleanup_failed = True
            continue
        try:
            if any(path.iterdir()):
                cleanup_failed = True
                continue
            path.rmdir()
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            cleanup_failed = True
        except OSError:
            cleanup_failed = True
    if cleanup_failed:
        raise RuntimeError("Synthetic protected path fixture cleanup could not be confirmed.")


def _scratch_tighten_opt_parent_for_synthetic_path_fixture() -> tuple[int, int, int, int, int, int, int] | None:
    """Temporarily remove write bits from the pinned /opt only for this fixture."""
    required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, flag) for flag in required_flags):
        raise RuntimeError("Synthetic protected path fixture cannot safely pin /opt.")
    descriptor: int | None = None
    adjustment: tuple[int, int, int, int, int, int, int] | None = None
    chmod_attempted = False
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open("/opt", flags)
        os.set_inheritable(descriptor, False)
        descriptor_info = os.fstat(descriptor)
        path_info = pathlib.Path("/opt").lstat()
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or stat.S_ISLNK(descriptor_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or descriptor_info.st_uid != 0
            or descriptor_info.st_gid != 0
            or path_info.st_uid != 0
            or path_info.st_gid != 0
            or (descriptor_info.st_dev, descriptor_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise RuntimeError("Synthetic protected path fixture /opt identity is unsafe.")
        original_mode = stat.S_IMODE(descriptor_info.st_mode)
        if not original_mode & 0o022:
            os.close(descriptor)
            return None
        tightened_mode = original_mode & ~0o022
        adjustment = (
            descriptor,
            descriptor_info.st_dev,
            descriptor_info.st_ino,
            descriptor_info.st_uid,
            descriptor_info.st_gid,
            original_mode,
            tightened_mode,
        )
        # Publish the lease on any later error so the caller's unconditional
        # cleanup can restore /opt after the synthetic fixture is removed.
        chmod_attempted = True
        os.fchmod(descriptor, tightened_mode)
        tightened_fd_info = os.fstat(descriptor)
        tightened_path_info = pathlib.Path("/opt").lstat()
        if (
            (tightened_fd_info.st_dev, tightened_fd_info.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
            or (tightened_path_info.st_dev, tightened_path_info.st_ino)
            != (descriptor_info.st_dev, descriptor_info.st_ino)
            or tightened_fd_info.st_uid != 0
            or tightened_fd_info.st_gid != 0
            or tightened_path_info.st_uid != 0
            or tightened_path_info.st_gid != 0
            or not stat.S_ISDIR(tightened_fd_info.st_mode)
            or stat.S_ISLNK(tightened_fd_info.st_mode)
            or not stat.S_ISDIR(tightened_path_info.st_mode)
            or stat.S_ISLNK(tightened_path_info.st_mode)
            or stat.S_IMODE(tightened_fd_info.st_mode) != tightened_mode
            or stat.S_IMODE(tightened_path_info.st_mode) != tightened_mode
        ):
            raise RuntimeError("Synthetic protected path fixture /opt mode change is unsafe.")
        return adjustment
    except BaseException as exc:
        if descriptor is not None:
            if chmod_attempted and adjustment is not None:
                setattr(exc, "scratch_synthetic_opt_parent_adjustment", adjustment)
            else:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def _scratch_finish_opt_parent_adjustment(
    adjustment: tuple[int, int, int, int, int, int, int], *, restore: bool
) -> None:
    """Restore the exact pinned /opt mode after fixture cleanup, then close it."""
    descriptor, device, inode, owner_uid, owner_gid, original_mode, tightened_mode = adjustment
    if (
        type(descriptor) is not int
        or descriptor < 0
        or any(type(value) is not int or value < 0 for value in (device, inode, owner_uid, owner_gid))
        or not 0 <= original_mode <= 0o7777
        or not 0 <= tightened_mode <= 0o7777
        or tightened_mode != original_mode & ~0o022
        or type(restore) is not bool
    ):
        raise RuntimeError("Synthetic protected path fixture /opt cleanup identity is invalid.")
    cleanup_error: BaseException | None = None
    try:
        descriptor_info = os.fstat(descriptor)
        path_info = pathlib.Path("/opt").lstat()
        if (
            not stat.S_ISDIR(descriptor_info.st_mode)
            or stat.S_ISLNK(descriptor_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or (descriptor_info.st_dev, descriptor_info.st_ino) != (device, inode)
            or (path_info.st_dev, path_info.st_ino) != (device, inode)
            or descriptor_info.st_uid != owner_uid
            or descriptor_info.st_gid != owner_gid
            or path_info.st_uid != owner_uid
            or path_info.st_gid != owner_gid
        ):
            raise RuntimeError("Synthetic protected path fixture /opt identity changed during cleanup.")
        descriptor_mode = stat.S_IMODE(descriptor_info.st_mode)
        path_mode = stat.S_IMODE(path_info.st_mode)
        if descriptor_mode != path_mode or descriptor_mode not in {tightened_mode, original_mode}:
            raise RuntimeError("Synthetic protected path fixture /opt mode changed during cleanup.")
        if restore:
            if descriptor_mode == tightened_mode and tightened_mode != original_mode:
                os.fchmod(descriptor, original_mode)
            restored_fd_info = os.fstat(descriptor)
            restored_path_info = pathlib.Path("/opt").lstat()
            if (
                (restored_fd_info.st_dev, restored_fd_info.st_ino) != (device, inode)
                or (restored_path_info.st_dev, restored_path_info.st_ino) != (device, inode)
                or restored_fd_info.st_uid != owner_uid
                or restored_fd_info.st_gid != owner_gid
                or restored_path_info.st_uid != owner_uid
                or restored_path_info.st_gid != owner_gid
                or not stat.S_ISDIR(restored_fd_info.st_mode)
                or stat.S_ISLNK(restored_fd_info.st_mode)
                or not stat.S_ISDIR(restored_path_info.st_mode)
                or stat.S_ISLNK(restored_path_info.st_mode)
                or stat.S_IMODE(restored_fd_info.st_mode) != original_mode
                or stat.S_IMODE(restored_path_info.st_mode) != original_mode
            ):
                raise RuntimeError("Synthetic protected path fixture /opt mode restore could not be confirmed.")
        elif descriptor_mode != tightened_mode:
            raise RuntimeError("Synthetic protected path fixture /opt remains unexpectedly writable.")
    except BaseException as exc:
        cleanup_error = exc
    try:
        os.close(descriptor)
    except BaseException as exc:
        if cleanup_error is None:
            cleanup_error = exc
    if cleanup_error is not None:
        raise RuntimeError("Synthetic protected path fixture /opt cleanup could not be confirmed.") from cleanup_error


def _scratch_prepare_synthetic_protected_paths(
    *, scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1
) -> tuple[
    tuple[tuple[str, int, int], ...], tuple[int, int, int, int, int, int, int] | None
]:
    """Prepare the fixed namespace targets only on the ephemeral hosted runner."""
    if (
        os.geteuid() != 0
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_OS") != "Linux"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise RuntimeError("Synthetic protected path fixture requires the root GitHub-hosted Linux runner.")
    targets = _scratch_synthetic_protected_path_targets(
        scratch_backend=scratch_backend
    )
    adjustment: tuple[int, int, int, int, int, int, int] | None = None
    created: tuple[tuple[str, int, int], ...] = ()
    try:
        # R6 proved that the hosted runner's fixed /opt parent can be
        # group/world writable while the first protected target is absent.
        # Pin and tighten only that parent before creating the missing subtree.
        first_opt_child = pathlib.Path("/opt/oldsparky")
        try:
            first_opt_child.lstat()
            first_opt_child_missing = False
        except FileNotFoundError:
            first_opt_child_missing = True
        if first_opt_child_missing and targets[0].parts[:2] == ("/", "opt"):
            adjustment = _scratch_tighten_opt_parent_for_synthetic_path_fixture()
        created = _scratch_create_missing_protected_path_directories(
            targets, owner_uid=0, owner_gid=0
        )
        if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
            _scratch_validate_synthetic_disk_image_fixture_dirs()
        return created, adjustment
    except BaseException as exc:
        if adjustment is None:
            pending = getattr(exc, "scratch_synthetic_opt_parent_adjustment", None)
            if isinstance(pending, tuple):
                adjustment = pending
        # The directory creator owns its partial ledger if it fails mid-walk.
        # Preserve that exact evidence so the outer caller can retry cleanup or
        # fail closed; only publish this layer's ledger when none was attached.
        if not hasattr(exc, "scratch_synthetic_path_fixture_created"):
            setattr(exc, "scratch_synthetic_path_fixture_created", created)
        if not hasattr(exc, "scratch_synthetic_path_fixture_cleanup_confirmed"):
            setattr(exc, "scratch_synthetic_path_fixture_cleanup_confirmed", False)
        if adjustment is not None:
            setattr(exc, "scratch_synthetic_opt_parent_adjustment", adjustment)
        raise


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


def _scratch_reset_failed_unit(unit_name: str) -> None:
    if _SCRATCH_GENERATED_UNIT_RE.fullmatch(unit_name) is None:
        raise RuntimeError("Scratch restore cleanup unit identity is invalid.")
    completed = subprocess.run(
        ["systemctl", "reset-failed", unit_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "SYSTEMD_PAGER": "cat"},
        timeout=3,
    )
    if completed.returncode != 0:
        raise RuntimeError("Scratch restore failed unit state could not be reset.")


def _scratch_wait_unit_not_found(
    unit_name: str, *, timeout_seconds: float = 2.0
) -> dict[str, Any]:
    if _SCRATCH_GENERATED_UNIT_RE.fullmatch(unit_name) is None:
        raise RuntimeError("Scratch restore cleanup unit identity is invalid.")
    deadline = time.monotonic() + timeout_seconds
    while True:
        snapshot = _scratch_service_snapshot(unit_name)
        if snapshot.get("load_state") == "not-found":
            return snapshot
        if time.monotonic() >= deadline:
            raise RuntimeError("Scratch restore transient unit was not unloaded after reset.")
        time.sleep(0.05)


def _scratch_unload_synthetic_unit(
    unit_name: str, snapshot: dict[str, Any]
) -> dict[str, Any]:
    if _SCRATCH_GENERATED_UNIT_RE.fullmatch(unit_name) is None:
        raise RuntimeError("Scratch restore cleanup unit identity is invalid.")
    if not isinstance(snapshot, dict) or type(snapshot.get("load_state")) is not str:
        raise RuntimeError("Scratch restore cleanup unit state is invalid.")
    if snapshot["load_state"] != "not-found":
        _scratch_reset_failed_unit(unit_name)
    return _scratch_wait_unit_not_found(unit_name)


def _run_isolated_restore_drill(
    dump_path: pathlib.Path | None,
    *,
    timestamp_slug: str,
    private_scratch_diagnostics: bool = False,
    authorization_probe: bool = False,
    synthetic_restore_diagnostic: bool = False,
    scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1,
) -> int | dict[str, Any]:
    """Run an existing-archive drill or one of the fixed no-input protocols."""
    fault_site = "controller_entry"
    try:
        if os.geteuid() != 0:
            raise RuntimeError("Isolated backup verification requires the root maintenance owner.")
        if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", timestamp_slug) is None:
            raise ValueError("Scratch restore timestamp identity is invalid.")
        if authorization_probe and synthetic_restore_diagnostic:
            raise ValueError("Scratch authorization probe and synthetic diagnostic are mutually exclusive.")
        if scratch_backend not in SCRATCH_BACKENDS:
            raise ValueError("Scratch restore backend is not supported.")
        if synthetic_restore_diagnostic and (dump_path is not None or private_scratch_diagnostics):
            raise ValueError("Synthetic restore diagnostic must not receive an archive or private archive diagnostics.")
        fault_site = "controller_entry"
        if authorization_probe:
            if dump_path is not None:
                raise ValueError("No-archive scratch protocol must not receive an archive path.")
            require_commands("systemd-run", "systemctl")
        elif synthetic_restore_diagnostic:
            if dump_path is not None:
                raise ValueError("No-archive scratch protocol must not receive an archive path.")
            require_commands("systemd-run", "systemctl", "pg_config", "runuser")
        else:
            if dump_path is None:
                raise ValueError("Scratch restore requires an archive path.")
            require_commands("systemd-run", "systemctl", "pg_config", "runuser")
        fault_site = "controller_host_guard"
        if authorization_probe:
            _scratch_host_guard("parent_before_authorization_probe")
        elif synthetic_restore_diagnostic:
            _scratch_host_guard("parent_before_synthetic_restore_diagnostic")
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
    if not authorization_probe and not synthetic_restore_diagnostic:
        assert dump_path is not None
        fault_site = "controller_archive_identity"
        archive_path = dump_path.absolute()
    try:
        if not authorization_probe and not synthetic_restore_diagnostic:
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
        image_path = (
            SCRATCH_DISK_IMAGE_ROOT / f"restore-{token}.ext4"
            if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
            else None
        )
        image_identity: tuple[int, int, int, int, int, int, int] | None = None
        image_created_by_invocation: bool | None = (
            False
            if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
            else None
        )
        mountpoint_identity: tuple[int, int] | None = None
        mountpoint_created = False
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
            synthetic_restore_diagnostic=synthetic_restore_diagnostic,
            scratch_backend=scratch_backend,
            scratch_image_path=image_path,
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
        stdout_limit = (
            SCRATCH_SYNTHETIC_RESULT_MAX_BYTES
            if synthetic_restore_diagnostic
            else 4096
        )
        stderr_buffer = bytearray()
        stderr_tail_buffer = (
            bytearray() if synthetic_restore_diagnostic else None
        )
        stdout_observed = 0
        stderr_size = 0
        early_exit_snapshot: dict[str, Any] | None = None
        early_exit_phase = "unit_exited_before_confinement"
        verification_error = "none"
        verification_errno: int | None = None
        verification_site = "none"
        verification_operation: str | None = None
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
        synthetic_protected_path_dirs: tuple[tuple[str, int, int], ...] = ()
        synthetic_opt_parent_adjustment: tuple[int, int, int, int, int, int, int] | None = None
        blocked_signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
        setup_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
        try:
            if synthetic_restore_diagnostic:
                fault_site = "controller_synthetic_path_fixture"
                try:
                    (
                        synthetic_protected_path_dirs,
                        synthetic_opt_parent_adjustment,
                    ) = _scratch_prepare_synthetic_protected_paths(
                        scratch_backend=scratch_backend
                    )
                except BaseException as fixture_error:
                    created = getattr(
                        fixture_error,
                        "scratch_synthetic_path_fixture_created",
                        (),
                    )
                    if isinstance(created, tuple):
                        synthetic_protected_path_dirs = created
                    adjustment = getattr(
                        fixture_error,
                        "scratch_synthetic_opt_parent_adjustment",
                        None,
                    )
                    if isinstance(adjustment, tuple):
                        synthetic_opt_parent_adjustment = adjustment
                    raise
            os.mkdir(mountpoint, 0o700)
            mountpoint_created = True
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
            if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
                assert image_path is not None
                try:
                    image_path, image_identity = _scratch_prepare_disk_image(token)
                    image_created_by_invocation = True
                except BaseException as image_error:
                    image_created_by_invocation = getattr(
                        image_error,
                        "scratch_image_created_by_invocation",
                        image_created_by_invocation,
                    )
                    raise
        except BaseException as setup_error:
            fixture_cleanup_failed = False
            setup_cleanup_errors: list[BaseException] = []
            image_unlinked: bool | None = getattr(
                setup_error, "scratch_image_unlinked", None
            )
            retention_receipt_written: bool | None = getattr(
                setup_error, "scratch_retention_receipt_written", None
            )
            if image_path is not None and image_identity is not None:
                try:
                    current_image = _scratch_image_path_identity(image_path)
                    if current_image == image_identity:
                        image_path.unlink()
                        _scratch_fsync_directory(image_path.parent)
                        image_created_by_invocation = True
                        image_unlinked = True
                        retention_receipt_written = None
                    else:
                        image_unlinked = False
                except BaseException:
                    # Before systemd receives the source, uncertainty retains
                    # the exact root-owned inode rather than risking a wrong delete.
                    image_unlinked = None
                    setup_cleanup_errors.append(RuntimeError("Scratch image cleanup was not confirmed."))
            mountpoint_removed = not mountpoint_created
            if mountpoint_created:
                try:
                    current_mountpoint = mountpoint.lstat()
                    if (
                        mountpoint_identity is not None
                        and (current_mountpoint.st_dev, current_mountpoint.st_ino)
                        == mountpoint_identity
                        and stat.S_ISDIR(current_mountpoint.st_mode)
                        and current_mountpoint.st_uid == 0
                        and current_mountpoint.st_gid == 0
                        and stat.S_IMODE(current_mountpoint.st_mode) == 0o700
                        and not any(mountpoint.iterdir())
                    ):
                        mountpoint.rmdir()
                        mountpoint_removed = True
                    else:
                        mountpoint_removed = False
                except FileNotFoundError:
                    mountpoint_removed = True
                except BaseException:
                    mountpoint_removed = None
                    setup_cleanup_errors.append(
                        RuntimeError("Scratch mountpoint cleanup was not confirmed.")
                    )
            if synthetic_restore_diagnostic:
                try:
                    _scratch_remove_created_protected_path_directories(
                        synthetic_protected_path_dirs,
                        owner_uid=0,
                        owner_gid=0,
                    )
                    synthetic_protected_path_dirs = ()
                except BaseException:
                    fixture_cleanup_failed = True
                if synthetic_opt_parent_adjustment is not None:
                    try:
                        _scratch_finish_opt_parent_adjustment(
                            synthetic_opt_parent_adjustment,
                            restore=not fixture_cleanup_failed,
                        )
                        synthetic_opt_parent_adjustment = None
                    except BaseException:
                        fixture_cleanup_failed = True
            loop_released = (
                True
                if image_created_by_invocation is True and not launch_started
                else getattr(setup_error, "scratch_loop_released", None)
            )
            try:
                _scratch_cleanup_failure(
                    setup_error,
                    setup_cleanup_errors,
                    authorization_probe=authorization_probe,
                    service_stopped=not launch_started,
                    mount_absent=True if not launch_started else None,
                    mountpoint_removed=mountpoint_removed,
                    loop_released=loop_released,
                    image_created_by_invocation=image_created_by_invocation,
                    image_removed=image_unlinked,
                    retention_receipt_written=retention_receipt_written,
                    protected_paths_cleaned=not fixture_cleanup_failed,
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, setup_signal_mask)
            if fixture_cleanup_failed:
                cleanup_error = RuntimeError(
                    "Synthetic protected path fixture setup failed and cleanup could not be confirmed."
                )
                for name in (
                    "scratch_cleanup_status",
                    "scratch_cleanup_diagnostic",
                    "scratch_first_fault",
                    "scratch_image_identity_diagnostic",
                    "scratch_unit_diagnostic",
                    "scratch_unit_journal_diagnostic",
                    "scratch_unit_private_output",
                    "scratch_synthetic_stderr_tail",
                    "scratch_synthetic_stderr_tail_complete",
                ):
                    if hasattr(setup_error, name):
                        setattr(cleanup_error, name, getattr(setup_error, name))
                raise cleanup_error from setup_error
            raise
        try:
            # A pending TERM/INT is delivered only after the exact host
            # mountpoint and, for the synthetic lane, path-fixture lease are
            # ready for the normal cleanup path.
            if not synthetic_restore_diagnostic:
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
                verification_operation = "active_poll_host_guard"
                try:
                    _scratch_host_guard("parent_active_poll")
                except BaseException as guard_error:
                    if _scratch_verification_site(guard_error) == "none":
                        setattr(guard_error, "scratch_verification_site", verification_operation)
                    guard_diagnostic = getattr(
                        guard_error, "scratch_host_guard_diagnostic", None
                    )
                    if (
                        private_scratch_diagnostics
                        and valid_scratch_host_guard_diagnostic(guard_diagnostic)
                        and guard_diagnostic.get("guard_site") == "parent_active_poll"
                    ):
                        # Preserve the reserve refusal before any optional sample.
                        _retain_first_scratch_fault(
                            guard_error,
                            origin="controller",
                            site="controller_active_poll",
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
                                    process,
                                    stderr_buffer,
                                    stderr_size,
                                    stderr_tail_buffer=stderr_tail_buffer,
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
                        resource_attribution: dict[str, Any] | None = None
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
                                resource_attribution = attribution
                                setattr(guard_error, "scratch_resource_attribution", attribution)
                        except BaseException:
                            # Optional evidence must never replace the reserve guard failure.
                            pass
                        try:
                            assert phase_guard_monotonic_ns is not None
                            assert archive_before is not None and archive_sha256 is not None
                            guard_deadline = min(
                                deadline,
                                phase_guard_monotonic_ns / 1_000_000_000
                                + SCRATCH_ALLOCATION_SAMPLE_BUDGET_SECONDS,
                            )
                            allocation_sample = _scratch_allocation_parent_last_guard(
                                mountpoint=mountpoint,
                                worker_identity=active_identity,
                                control_group=active_control_group,
                                launch_monotonic_ns=launch_monotonic_ns,
                                guard_diagnostic=guard_diagnostic,
                                resource_attribution=resource_attribution,
                                operation_deadline=guard_deadline,
                                guard_monotonic_ns=phase_guard_monotonic_ns,
                            )
                            if valid_scratch_allocation_sample(allocation_sample):
                                setattr(
                                    guard_error,
                                    "scratch_parent_last_guard_sample",
                                    allocation_sample,
                                )
                        except BaseException:
                            # The monotonic budget is checked between syscalls; it
                            # cannot interrupt a blocking kernel call. Sampling
                            # remains best effort and cannot replace the guard.
                            pass
                    raise
                verification_operation = None
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
                    verification_operation = "controller_snapshot_properties"
                    try:
                        fault_site = "controller_identity"
                        verification_operation = "controller_expected_executable_resolve"
                        expected_python_binary = pathlib.Path(
                            getattr(sys, "_base_executable", sys.executable)
                        ).resolve(strict=True)
                        verification_operation = "controller_snapshot_properties"
                        verified_control_group = _scratch_expected_properties(
                            snapshot,
                            unit_name=unit_name,
                            mountpoint=mountpoint,
                            python_binary=expected_python_binary,
                            authorization_probe=authorization_probe,
                            synthetic_restore_diagnostic=synthetic_restore_diagnostic,
                            scratch_backend=scratch_backend,
                            scratch_image_path=image_path,
                        )
                        verification_operation = "controller_cgroup_snapshot"
                        _scratch_control_group_snapshot(verified_control_group)
                        verification_operation = "controller_invocation_identity"
                        invocation_id = properties.get("InvocationID", "")
                        if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
                            raise RuntimeError("Scratch restore unit invocation identity is unavailable.")
                        if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
                            verification_operation = "controller_image_mount_identity"
                            assert image_path is not None and image_identity is not None
                            _scratch_verify_process_image_mount(
                                pid=pid_text,
                                mountpoint=mountpoint,
                                image_path=image_path,
                                image_identity=image_identity,
                            )
                        verification_operation = "controller_process_start_time"
                        process_start_time = _scratch_process_start_time(pid_text)
                        observed_identity = (pid_text, invocation_id, process_start_time)
                        verification_operation = "controller_process_state"
                        if active_identity is not None and observed_identity != active_identity:
                            raise RuntimeError("Scratch restore worker identity changed during verification.")
                        if active_identity is None:
                            verification_operation = "controller_start_deadline"
                            if time.monotonic() >= startup_deadline or process.poll() is not None:
                                raise RuntimeError("Scratch restore worker exited before authorization.")
                            fault_site = "controller_identity"
                            verification_operation = "controller_second_snapshot_identity"
                            second_snapshot = _scratch_service_snapshot(unit_name)
                            second_properties = second_snapshot.get("properties", {})
                            identity_failure = _scratch_second_snapshot_identity_failure(
                                properties,
                                second_properties,
                                expected_pid=pid_text,
                                expected_invocation_id=invocation_id,
                                synthetic_restore_diagnostic=synthetic_restore_diagnostic,
                            )
                            if identity_failure is not None:
                                raise identity_failure
                            verification_operation = "controller_snapshot_properties"
                            second_python_binary = pathlib.Path(
                                getattr(sys, "_base_executable", sys.executable)
                            ).resolve(strict=True)
                            second_control_group = _scratch_expected_properties(
                                second_snapshot,
                                unit_name=unit_name,
                                mountpoint=mountpoint,
                                python_binary=second_python_binary,
                                authorization_probe=authorization_probe,
                                synthetic_restore_diagnostic=synthetic_restore_diagnostic,
                                scratch_backend=scratch_backend,
                                scratch_image_path=image_path,
                            )
                            verification_operation = "controller_cgroup_snapshot"
                            _scratch_control_group_snapshot(second_control_group)
                            if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
                                verification_operation = "controller_image_mount_identity"
                                assert image_path is not None and image_identity is not None
                                _scratch_verify_process_image_mount(
                                    pid=pid_text,
                                    mountpoint=mountpoint,
                                    image_path=image_path,
                                    image_identity=image_identity,
                            )
                            verification_operation = "controller_process_start_time"
                            second_process_start_time = _scratch_process_start_time(pid_text)
                            verification_operation = "controller_cgroup_identity_compare"
                            if second_control_group != verified_control_group:
                                raise RuntimeError("Scratch restore worker identity changed before authorization.")
                            verification_operation = "controller_process_start_time_compare"
                            if second_process_start_time != process_start_time:
                                raise RuntimeError("Scratch restore worker identity changed before authorization.")
                            verification_operation = "controller_start_deadline"
                            if time.monotonic() >= startup_deadline:
                                raise RuntimeError("Scratch restore worker identity changed before authorization.")
                            verification_operation = "controller_process_state"
                            if process.poll() is not None:
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
                        if _scratch_verification_site(verification_exception) == "none":
                            setattr(
                                verification_exception,
                                "scratch_verification_site",
                                verification_operation,
                            )
                        if identity_checks_passed and not completion_token_sent:
                            early_exit_phase = "authorization_failed"
                        elif active_control_group is not None:
                            early_exit_phase = "unit_exited_after_confinement"
                        else:
                            early_exit_phase = "confinement_verification_failed"
                        verification_error, verification_errno = (
                            _scratch_verification_error_details(verification_exception)
                        )
                        verification_site = _scratch_verification_site(verification_exception)
                        raise
                    early_exit_snapshot = None
                    verification_operation = None
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
                # Identity verification is complete for this iteration. Any
                # later selector/read failure belongs to the bounded active
                # poll, not to the prior controller identity check.
                fault_site = "controller_active_poll"
                verification_operation = None
                last_tick = now
                verification_operation = "active_poll_selector_wait"
                ready_events = selector.select(timeout=SCRATCH_RUNTIME_SAMPLE_SECONDS)
                verification_operation = None
                for key, _ in ready_events:
                    verification_operation = "active_poll_stream_read"
                    try:
                        data = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        verification_operation = None
                        continue
                    verification_operation = None
                    if not data:
                        verification_operation = "active_poll_stream_unregister"
                        selector.unregister(key.fileobj)
                        verification_operation = None
                        if key.data == "stderr":
                            stderr_eof = True
                    elif key.data == "stdout":
                        verification_operation = "active_poll_output_bound"
                        stdout_observed += len(data)
                        stdout_buffer.extend(data)
                        if len(stdout_buffer) > stdout_limit:
                            raise RuntimeError("Scratch restore worker output exceeded its closed bound.")
                        verification_operation = None
                    else:
                        verification_operation = "active_poll_output_bound"
                        stderr_size += len(data)
                        if stderr_tail_buffer is not None:
                            _scratch_append_bounded_tail(
                                stderr_tail_buffer,
                                data,
                                SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES,
                            )
                        if len(stderr_buffer) < SCRATCH_PRIVATE_STDERR_BYTES:
                            stderr_buffer.extend(data[: SCRATCH_PRIVATE_STDERR_BYTES - len(stderr_buffer)])
                        if stderr_size > 65536:
                            raise RuntimeError("Scratch restore worker error output exceeded its closed bound.")
                        verification_operation = None
                if start_token_sent and not completion_token_sent and stdout_buffer:
                    fault_site = "controller_worker_frame"
                    if synthetic_restore_diagnostic:
                        failure_state, worker_failure = (
                            _scratch_synthetic_worker_failure_candidate(
                                bytes(stdout_buffer),
                                bytes(stderr_buffer),
                                stderr_eof=stderr_eof,
                            )
                        )
                        if failure_state == "pending":
                            continue
                        if failure_state == "valid":
                            failure_error = RuntimeError(
                                "Synthetic restore worker reported a bounded failure."
                            )
                            assert worker_failure is not None
                            setattr(failure_error, "scratch_first_fault", worker_failure)
                            raise failure_error
                        if failure_state == "invalid":
                            raise RuntimeError(
                                "Scratch synthetic worker failure frame did not match its report."
                            )
                    candidate = (
                        _scratch_authorization_probe_candidate(bytes(stdout_buffer))
                        if authorization_probe
                        else _scratch_synthetic_worker_candidate(bytes(stdout_buffer))
                        if synthetic_restore_diagnostic
                        else _scratch_worker_candidate(bytes(stdout_buffer))
                    )
                    if candidate is not None:
                        verification_operation = "controller_completion_identity"
                        if active_identity is None or active_control_group is None:
                            raise RuntimeError("Scratch restore worker identity was not retained through completion.")
                        fault_site = "controller_completion_snapshot"
                        verification_operation = "controller_completion_identity"
                        final_snapshot = _scratch_service_snapshot(unit_name)
                        final_properties = final_snapshot.get("properties", {})
                        final_pid = final_properties.get("ExecMainPID", "0")
                        final_invocation = final_properties.get("InvocationID", "")
                        verification_operation = "controller_completion_unit_state"
                        if final_properties.get("LoadState") != "loaded" or final_properties.get("ActiveState") != "active":
                            raise RuntimeError("Scratch restore worker identity changed before completion acknowledgement.")
                        verification_operation = "controller_completion_identity"
                        if (final_pid, final_invocation) != active_identity[:2]:
                            raise RuntimeError("Scratch restore worker identity changed before completion acknowledgement.")
                        verification_operation = "controller_start_deadline"
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Scratch restore worker identity changed before completion acknowledgement.")
                        final_control_group = _scratch_expected_properties(
                            final_snapshot,
                            unit_name=unit_name,
                            mountpoint=mountpoint,
                            python_binary=pathlib.Path(
                                getattr(sys, "_base_executable", sys.executable)
                            ).resolve(strict=True),
                            authorization_probe=authorization_probe,
                            synthetic_restore_diagnostic=synthetic_restore_diagnostic,
                            scratch_backend=scratch_backend,
                            scratch_image_path=image_path,
                        )
                        verification_operation = "controller_cgroup_snapshot"
                        _scratch_control_group_snapshot(final_control_group)
                        if scratch_backend == SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1:
                            verification_operation = "controller_image_mount_identity"
                            assert image_path is not None and image_identity is not None
                            _scratch_verify_process_image_mount(
                                pid=final_pid,
                                mountpoint=mountpoint,
                                image_path=image_path,
                                image_identity=image_identity,
                            )
                        verification_operation = "controller_process_start_time"
                        final_process_start_time = _scratch_process_start_time(final_pid)
                        verification_operation = "controller_cgroup_identity_compare"
                        if final_control_group != active_control_group:
                            raise RuntimeError("Scratch restore worker identity changed before completion acknowledgement.")
                        verification_operation = "controller_process_start_time_compare"
                        if final_process_start_time != active_identity[2]:
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
                            if len(stdout_buffer) > stdout_limit:
                                raise RuntimeError("Scratch restore worker output exceeded its closed bound.")
                        else:
                            stderr_size += len(data)
                            if stderr_tail_buffer is not None:
                                _scratch_append_bounded_tail(
                                    stderr_tail_buffer,
                                    data,
                                    SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES,
                                )
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
                    verification_site = _scratch_verification_site(snapshot_error)
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
            elif synthetic_restore_diagnostic:
                decoded = _scratch_synthetic_worker_candidate(bytes(stdout_buffer))
                if decoded is None:
                    early_exit_snapshot = _scratch_service_snapshot(unit_name)
                    early_exit_phase = "unit_exited_after_confinement"
                    raise RuntimeError("Scratch synthetic restore diagnostic result failed validation.")
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
            if verification_operation is not None:
                if _scratch_verification_site(operation_error) == "none":
                    setattr(
                        operation_error,
                        "scratch_verification_site",
                        verification_operation,
                    )
                verification_error, verification_errno = (
                    _scratch_verification_error_details(operation_error)
                )
                verification_site = _scratch_verification_site(operation_error)
            if synthetic_restore_diagnostic:
                created = getattr(
                    operation_error,
                    "scratch_synthetic_path_fixture_created",
                    None,
                )
                if isinstance(created, tuple):
                    synthetic_protected_path_dirs = created
                adjustment = getattr(
                    operation_error,
                    "scratch_synthetic_opt_parent_adjustment",
                    None,
                )
                if isinstance(adjustment, tuple):
                    synthetic_opt_parent_adjustment = adjustment
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
            if synthetic_restore_diagnostic and launch_started:
                try:
                    if early_exit_snapshot is None:
                        early_exit_snapshot = _scratch_service_snapshot(unit_name)
                except BaseException:
                    pass
                try:
                    journal_diagnostic = _scratch_unit_journal_diagnostic(
                        unit_name,
                        mountpoint=mountpoint,
                        terminal_snapshot=terminal_snapshot,
                    )
                    if valid_scratch_unit_journal_diagnostic(journal_diagnostic):
                        setattr(
                            operation_error,
                            "scratch_unit_journal_diagnostic",
                            journal_diagnostic,
                        )
                except BaseException:
                    setattr(
                        operation_error,
                        "scratch_unit_journal_diagnostic",
                        _scratch_empty_journal_diagnostic("unavailable"),
                    )
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
                            stderr_tail_buffer=stderr_tail_buffer,
                        )
                    )
                    if synthetic_restore_diagnostic:
                        setattr(
                            operation_error,
                            "scratch_synthetic_stderr_tail",
                            bytes(stderr_tail_buffer or b""),
                        )
                        setattr(
                            operation_error,
                            "scratch_synthetic_stderr_tail_complete",
                            drain_complete["stderr"],
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
                    if not start_token_sent:
                        worker_frame_status = "not_started"
                    elif candidate_output is not None:
                        worker_frame_status = "valid"
                    elif first_fault is not None and first_fault.get("origin") == "worker":
                        worker_frame_status = "worker_failure_report"
                    elif not stdout_buffer:
                        worker_frame_status = "missing"
                    else:
                        worker_frame_status = "invalid"
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
                        verification_site=verification_site,
                        worker_frame_status=worker_frame_status,
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
                    if (
                        private_scratch_diagnostics
                        and archive_before is not None
                        and archive_sha256 is not None
                    ):
                        try:
                            attribution = _scratch_parse_allocation_attribution(
                                stderr_prefix,
                                truncated=(
                                    stderr_size > len(stderr_prefix)
                                    or not drain_complete["stderr"]
                                ),
                                archive_size_bytes=archive_before.st_size,
                                archive_sha256=archive_sha256,
                                parent_sample=getattr(
                                    operation_error,
                                    "scratch_parent_last_guard_sample",
                                    None,
                                ),
                            )
                        except BaseException:
                            attribution = _scratch_unknown_allocation_attribution(
                                archive_before.st_size,
                                archive_sha256,
                                "internal_error",
                            )
                        if valid_scratch_allocation_attribution(attribution):
                            setattr(
                                operation_error,
                                "scratch_allocation_attribution",
                                attribution,
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
            service_stopped: bool | None = True if not launch_started else None
            if launch_started:
                try:
                    final_snapshot = _scratch_service_snapshot(unit_name)
                    final_props = final_snapshot["properties"]
                    if final_props.get("ActiveState") not in {"inactive", "failed"}:
                        _scratch_stop_unit(unit_name)
                        final_snapshot = _scratch_service_snapshot(unit_name)
                        final_props = final_snapshot["properties"]
                    if synthetic_restore_diagnostic:
                        final_snapshot = _scratch_unload_synthetic_unit(
                            unit_name, final_snapshot
                        )
                        final_props = final_snapshot["properties"]
                        service_stopped = final_props.get("LoadState") == "not-found"
                    else:
                        service_stopped = (
                            final_props.get("LoadState") == "not-found"
                            or (
                                final_props.get("ActiveState") in {"inactive", "failed"}
                                and final_props.get("ExecMainPID", "0") in {"", "0"}
                                and final_props.get("ControlGroup", "") == ""
                            )
                        )
                except BaseException as cleanup_error:
                    service_stopped = None
                    final_cleanup_errors.append(cleanup_error)
            try:
                mount_absent = not _scratch_mountpoint_is_mounted(mountpoint)
            except BaseException as cleanup_error:
                mount_absent = None
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
                mountpoint_removed = None
                final_cleanup_errors.append(cleanup_error)
            loop_released: bool | None = None
            image_removed: bool | None = None
            retention_receipt_written: bool | None = None
            if image_path is not None and image_identity is not None:
                image_created_by_invocation = True
                retain_reason = "cleanup_observer_failed"
                try:
                    if not service_stopped:
                        retain_reason = "service_not_stopped"
                    elif not mount_absent:
                        retain_reason = "host_mount_present"
                    elif not mountpoint_removed:
                        retain_reason = "mountpoint_not_removed"
                    elif not _scratch_wait_for_image_loop_release(image_path, image_identity):
                        loop_released = False
                        retain_reason = "loop_association_persists"
                    else:
                        loop_released = True
                        image_removed = False
                        _scratch_unlink_image_exact(image_path, image_identity)
                        image_removed = True
                except BaseException as cleanup_error:
                    current_identity: tuple[int, int, int, int, int, int, int] | None
                    try:
                        current_identity = _scratch_image_path_identity(image_path)
                    except BaseException:
                        current_identity = None
                    if current_identity is not None and current_identity != image_identity:
                        retain_reason = "image_identity_changed"
                    final_cleanup_errors.append(cleanup_error)
                if not image_removed:
                    retention_receipt_written = False
                    try:
                        _scratch_write_image_cleanup_receipt(
                            image_path,
                            image_identity,
                            unit_name=unit_name,
                            reason=retain_reason,
                        )
                        retention_receipt_written = True
                    except BaseException as cleanup_error:
                        final_cleanup_errors.append(cleanup_error)
                    final_cleanup_errors.append(
                        RuntimeError("Scratch disk image was retained because teardown was not proven.")
                    )
            protected_paths_cleaned = not synthetic_restore_diagnostic
            if synthetic_restore_diagnostic:
                fixture_teardown_safe = (
                    service_stopped
                    and mount_absent
                    and mountpoint_removed
                    and (
                        scratch_backend != SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                        or image_removed is True
                    )
                )
                if fixture_teardown_safe:
                    try:
                        _scratch_remove_created_protected_path_directories(
                            synthetic_protected_path_dirs,
                            owner_uid=0,
                            owner_gid=0,
                        )
                        protected_paths_cleaned = True
                    except BaseException as cleanup_error:
                        protected_paths_cleaned = False
                        final_cleanup_errors.append(cleanup_error)
                else:
                    protected_paths_cleaned = False
                    final_cleanup_errors.append(
                        RuntimeError(
                            "Synthetic protected paths were retained because disk teardown was not confirmed."
                        )
                    )
                if synthetic_opt_parent_adjustment is not None:
                    try:
                        if not service_stopped or not protected_paths_cleaned:
                            _scratch_finish_opt_parent_adjustment(
                                synthetic_opt_parent_adjustment,
                                restore=False,
                            )
                            raise RuntimeError(
                                "Synthetic protected path fixture /opt cannot be restored before cleanup is confirmed."
                            )
                        _scratch_finish_opt_parent_adjustment(
                            synthetic_opt_parent_adjustment,
                            restore=True,
                        )
                    except BaseException as cleanup_error:
                        protected_paths_cleaned = False
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
            if (
                not service_stopped
                or not mount_absent
                or not mountpoint_removed
                or image_removed is False
                or not protected_paths_cleaned
            ):
                final_cleanup_errors.append(
                    RuntimeError("Scratch restore unit, mount, or fixture cleanup could not be confirmed.")
                )
            cleanup_failure = _scratch_cleanup_failure(
                sys.exc_info()[1],
                final_cleanup_errors,
                authorization_probe=authorization_probe,
                service_stopped=service_stopped,
                mount_absent=mount_absent,
                mountpoint_removed=mountpoint_removed,
                loop_released=loop_released,
                image_created_by_invocation=image_created_by_invocation,
                image_removed=image_removed,
                retention_receipt_written=retention_receipt_written,
                protected_paths_cleaned=protected_paths_cleaned,
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
        if synthetic_restore_diagnostic:
            if not valid_scratch_synthetic_restore_diagnostic(worker_result):
                raise RuntimeError("Scratch synthetic restore diagnostic returned an invalid result.")
            fault_site = "controller_host_guard"
            _scratch_host_guard("parent_after_worker_cleanup")
            return worker_result
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
        try:
            if archive_descriptor is not None:
                os.close(archive_descriptor)
        finally:
            if synthetic_restore_diagnostic:
                # Keep termination signals blocked through the complete unit,
                # image, mount, and fixture cleanup above, then restore the
                # exact caller mask even when cleanup or archive close failed.
                signal.pthread_sigmask(signal.SIG_SETMASK, setup_signal_mask)


def run_synthetic_restore_diagnostic(
    *, scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1
) -> dict[str, Any]:
    """Run the fixed three-arm synthetic diagnostic through the verified unit."""
    timestamp_slug = utc_now().strftime("%Y%m%dT%H%M%SZ")
    result = _run_isolated_restore_drill(
        None,
        timestamp_slug=timestamp_slug,
        synthetic_restore_diagnostic=True,
        scratch_backend=scratch_backend,
    )
    if not valid_scratch_synthetic_restore_diagnostic(result):
        raise RuntimeError("Synthetic restore diagnostic did not return a complete report.")
    return result


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
    scratch_backend = getattr(args, "scratch_backend", SCRATCH_BACKEND_TMPFS_V1)
    if type(scratch_backend) is not str or scratch_backend not in SCRATCH_BACKENDS:
        raise ValueError("unsupported backup scratch backend")
    env_file = pathlib.Path(args.env_file)
    output_dir = pathlib.Path(args.output_dir)
    protected_manifest = getattr(args, "protected_backup_manifest", None)
    protected_manifest_path = (
        pathlib.Path(protected_manifest) if protected_manifest is not None else None
    )
    protected_manifest_before = (
        _protected_backup_manifest_snapshot(protected_manifest_path)
        if protected_manifest_path is not None
        else None
    )
    protected_dump_files = (
        frozenset(protected_manifest_before["protected_dump_files"])
        if protected_manifest_before is not None
        else frozenset()
    )
    protected_pairs_before: list[dict[str, Any]] = []
    if protected_dump_files:
        if type(args.keep) is not int or args.keep < 1:
            raise ValueError("--keep must be at least 1.")
        if not output_dir.is_dir():
            raise RuntimeError("Protected backup inventory is unavailable.")
        protected_pairs_before = _protected_backup_pair_rows(
            output_dir, protected_dump_files
        )
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
                scratch_backend=scratch_backend,
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
        protected_retention_proof: dict[str, Any] | None = None
        if restore_verified and rotate_existing:
            if protected_dump_files:
                if protected_manifest_path is None or protected_manifest_before is None:
                    raise RuntimeError("Protected backup manifest snapshot is unavailable.")
                effective_keep = max(args.keep, PROTECTED_BACKUP_MINIMUM_KEEP)
                _assert_protected_retention_inputs_unchanged(
                    protected_manifest_path,
                    protected_manifest_before,
                    output_dir,
                    protected_dump_files,
                    protected_pairs_before,
                )
                verified_candidate_order = _verified_backup_candidate_order(output_dir)
                selected_order = list(verified_candidate_order[-effective_keep:])
                if dump_path.name not in {row["dump_file"] for row in selected_order}:
                    raise RuntimeError(
                        "New verified backup is outside the retained latest selection."
                    )
                unverified_candidate_order = _unverified_backup_candidate_order(
                    output_dir,
                    preserve_metadata=metadata_path,
                    protected_dump_files=protected_dump_files,
                )
                removed.extend(
                    prune_unverified_backups(
                        output_dir,
                        preserve_metadata=metadata_path,
                        protected_dump_files=protected_dump_files,
                        expected_candidate_order=unverified_candidate_order,
                    )
                )
                _assert_protected_retention_inputs_unchanged(
                    protected_manifest_path,
                    protected_manifest_before,
                    output_dir,
                    protected_dump_files,
                    protected_pairs_before,
                )
                if _verified_backup_candidate_order(output_dir) != verified_candidate_order:
                    raise RuntimeError("Verified backup inventory changed before retention.")
                removed.extend(
                    prune_backups(
                        output_dir,
                        keep=effective_keep,
                        protected_dump_files=protected_dump_files,
                        verified_only=True,
                        expected_verified_order=verified_candidate_order,
                    )
                )
                protected_retention_proof = _build_protected_retention_proof(
                    manifest_path=protected_manifest_path,
                    manifest_before=protected_manifest_before,
                    output_dir=output_dir,
                    protected_dump_files=protected_dump_files,
                    protected_pairs_before=protected_pairs_before,
                    unverified_candidate_order=unverified_candidate_order,
                    verified_candidate_order=verified_candidate_order,
                    selected_order=selected_order,
                    requested_keep=args.keep,
                    effective_keep=effective_keep,
                    new_dump_file=dump_path.name,
                    removed_paths=removed,
                )
            else:
                removed.extend(
                    prune_unverified_backups(
                        output_dir, preserve_metadata=metadata_path
                    )
                )
                removed.extend(prune_backups(output_dir, keep=args.keep))
        elif (
            protected_dump_files
            and protected_manifest_path is not None
            and protected_manifest_before is not None
        ):
            # Non-rotating preserve mode keeps every old pair, but reports no
            # rotation proof because it has not selected or pruned a suffix.
            _assert_protected_retention_inputs_unchanged(
                protected_manifest_path,
                protected_manifest_before,
                output_dir,
                protected_dump_files,
                protected_pairs_before,
            )
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
        if protected_retention_proof is not None:
            result["protected_retention_proof"] = protected_retention_proof
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


def run_scratch_authorization_probe(
    *, scratch_backend: str = SCRATCH_BACKEND_TMPFS_V1
) -> dict[str, Any]:
    """Exercise only the staged helper's pre-archive authorization protocol."""
    _run_isolated_restore_drill(
        None,
        timestamp_slug=utc_now().strftime("%Y%m%dT%H%M%SZ"),
        private_scratch_diagnostics=True,
        authorization_probe=True,
        scratch_backend=scratch_backend,
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
    allocation_attribution = getattr(exc, "scratch_allocation_attribution", None)
    if (
        private_scratch_diagnostics
        and valid_scratch_allocation_attribution(allocation_attribution)
    ):
        report["scratch_allocation_attribution"] = allocation_attribution
    cleanup_status = getattr(exc, "scratch_cleanup_status", None)
    if private_scratch_diagnostics and valid_scratch_cleanup_status(cleanup_status):
        report["scratch_cleanup_status"] = cleanup_status
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
    allocation_frame = _scratch_allocation_attribution_frame(
        getattr(exc, "scratch_allocation_attribution", None)
    )
    if allocation_frame is not None:
        frames.append(allocation_frame)
    return b"".join(frames).decode("ascii") if frames else None


def main() -> int:
    args = parse_args()
    try:
        if getattr(args, "_scratch_restore_worker", False):
            required_worker_values = (
                args._scratch_root,
                args._scratch_timestamp_slug,
            )
            if not (
                getattr(args, "_scratch_authorization_probe", False)
                or getattr(args, "_scratch_synthetic_restore_diagnostic", False)
            ):
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
                verification_site = getattr(args, "_scratch_verification_site", None)
                site_pairs = {
                    "worker_root_identity": {"worker_root_path_identity", "worker_root_mode_identity"},
                    "worker_mount_snapshot": {"worker_mount_identity"},
                    "worker_cgroup_snapshot": {"worker_cgroup_identity"},
                    "worker_postgres_tools": {"worker_postgres_account_identity"},
                }
                if verification_site in site_pairs.get(
                    getattr(args, "_scratch_fault_site", "worker_entry"), set()
                ):
                    setattr(exc, "scratch_verification_site", verification_site)
                _retain_first_scratch_fault(
                    exc,
                    origin="worker",
                    site=getattr(args, "_scratch_fault_site", "worker_entry"),
                )
                raise
        if getattr(args, "_scratch_authorization_probe", False):
            if any(
                (
                    args.check_latest,
                    args.verify_latest_existing,
                    args.dump_only,
                    args.verify_dump is not None,
                    args.protected_backup_manifest is not None,
                )
            ):
                raise ValueError("Scratch authorization probe cannot be combined with backup modes.")
            result = run_scratch_authorization_probe(
                scratch_backend=args._scratch_backend
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if getattr(args, "_scratch_synthetic_restore_diagnostic", False):
            if (
                not args.as_json
                or args.private_scratch_diagnostics
                or args.check_latest
                or args.verify_latest_existing
                or args.dump_only
                or args.verify_dump is not None
                or args.protected_backup_manifest is not None
                or args.admin_database_url is not None
                or args.output_dir != str(DEFAULT_OUTPUT_DIR)
                or args.env_file != str(DEFAULT_ENV_FILE)
                or args.keep != 14
                or args.rotate_existing is not True
                or args.max_age_hours != 24.0
                or getattr(args, "_scratch_authorization_probe", False)
                or getattr(args, "_scratch_restore_worker", False)
                or any(
                    getattr(args, name, None) is not None
                    for name in (
                        "_scratch_root", "_scratch_archive", "_scratch_archive_dev",
                        "_scratch_archive_ino", "_scratch_archive_size",
                        "_scratch_archive_sha256", "_scratch_timestamp_slug",
                    )
                )
            ):
                raise ValueError("Synthetic restore diagnostic accepts only its typed JSON mode.")
            result = run_synthetic_restore_diagnostic(
                scratch_backend=args._scratch_backend
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
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
        synthetic_worker_failure = (
            getattr(args, "_scratch_restore_worker", False)
            and getattr(args, "_scratch_synthetic_restore_diagnostic", False)
        )
        first_fault = getattr(exc, "scratch_first_fault", None)
        first_fault_frame = _scratch_first_fault_frame(first_fault)
        if (
            synthetic_worker_failure
            and valid_scratch_first_fault(first_fault)
            and first_fault_frame is not None
        ):
            # This fixed internal worker protocol may expose only its closed
            # first-fault frame. Keep the exception text and every optional
            # private diagnostic out of the parent-visible channels.
            report = {
                "ok": False,
                "error": "Synthetic restore worker failed.",
                "scratch_first_fault": first_fault,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            sys.stderr.write(first_fault_frame.decode("ascii"))
            sys.stderr.flush()
            return 1
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
