#!/usr/bin/env python3
"""Value-only contract shared by the storage diagnostics projector and fallback.

This module intentionally has no repository imports.  The workflow packages it
with the projector so a prepare failure can still use the same closed schema
when the runner has to fall back to its tiny inline writer.
"""

from __future__ import annotations

import math
import re
import argparse
import json
import os
import stat


SCHEMA = 1
MAX_REPORTED_BYTES = 1_000_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")

SAFE_FAILURE_PHASES = frozenset(
    {
        "capture",
        "precondition",
        "transport",
        "remote",
        "report",
        "filesystem",
        "journal",
        "service",
        "category",
        "retention",
        "cleanup",
    }
)
SAFE_FAILURE_REASONS = frozenset(
    {
        "active_release_id_invalid",
        "active_release_manifest_missing",
        "active_sha_mismatch",
        "category_usage_producer",
        "current_release_missing",
        "disk_usage_producer",
        "inode_usage_producer",
        "journal_usage_producer",
        "maintenance_tool_missing",
        "privilege",
        "python_runtime_missing",
        "remote_collection_failed",
        "remote_report_missing",
        "remote_timeout",
        "report_schema_incomplete",
        "report_truncated",
        "retention_producer",
        "retention_summary",
        "sanitizer_exception",
        "sanitizer_unavailable",
        "service_state_producer",
        "ssh_cleanup_failed",
        "ssh_transport",
        "stderr_capture_missing",
        "stderr_capture_mismatch",
        "ulimit_unavailable",
        "remote_retention_timeout",
    }
)
SAFE_FAILURE_ACTIONS = frozenset(
    {
        "inspect_current_release_manifest",
        "inspect_deployed_storage_tool",
        "inspect_current_release_pointer",
        "inspect_production_runner_identity",
        "inspect_release_identity",
        "inspect_remote_collection",
        "inspect_remote_diagnostics_command",
        "inspect_remote_diagnostics_contract",
        "inspect_remote_output_volume",
        "inspect_remote_transport_timeout",
        "inspect_runner_evidence_projector",
        "inspect_runner_ssh_cleanup",
        "inspect_service_state_producer",
        "inspect_ssh_transport_and_host_key",
        "inspect_category_usage_producer",
        "inspect_disk_usage_producer",
        "inspect_inode_usage_producer",
        "inspect_journal_usage_producer",
        "inspect_retention_dry_run",
        "inspect_retention_summary",
        "inspect_remote_retention_timeout",
        "inspect_runner_limits",
        "recalculate_expected_sha_from_release",
    }
)

EXPECTED_SECTIONS = (
    "lock",
    "filesystem",
    "journal",
    "services",
    "categories",
    "retention",
)

SAFE_CATEGORIES = (
    "root",
    "tmp",
    "var_tmp",
    "platform_runtime",
    "logs",
)
SAFE_DU_CATEGORIES = (
    "source_release_artifacts",
    "browser_test_artifacts",
    "preprod_screenshots",
    "live_qa_runtime",
    "backups",
)
SAFE_SERVICES = ("deadlock-api", "deadlock-worker", "deadlock-web")
SERVICE_PROPERTY_KEYS = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "NRestarts",
    "MemoryCurrent",
    "MemoryPeak",
    "MemoryMax",
    "TasksCurrent",
    "TasksMax",
    "CPUUsageNSec",
)
SERVICE_ENUMS = {
    "ActiveState": frozenset(
        {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
    ),
    "SubState": frozenset(
        {"running", "dead", "exited", "failed", "auto-restart", "start", "stop"}
    ),
    "Result": frozenset(
        {
            "success", "exit-code", "signal", "core-dump", "timeout", "watchdog",
            "start-limit-hit", "resources", "protocol", "dependency", "assert", "condition",
        }
    ),
    "ExecMainCode": frozenset({"exited", "killed", "dumped", "dead", "invalid"}),
}
SERVICE_INFINITY_KEYS = frozenset({"MemoryMax", "TasksMax"})

FAILURE_KEYS = frozenset(
    {
        "schema",
        "kind",
        "status",
        "raw_output_included",
        "expected_sha",
        "remote_exit_code",
        "remote_stderr_bytes",
        "stderr_truncated",
        "report_present",
        "report_truncated",
        "phase",
        "reason",
        "action",
        "active_release_id",
        "active_source_sha",
        "sections",
    }
)
SUCCESS_KEYS = frozenset(
    {
        "schema",
        "kind",
        "status",
        "raw_output_included",
        "expected_sha",
        "remote_exit_code",
        "remote_stderr_bytes",
        "stderr_truncated",
        "report_present",
        "report_truncated",
        "active_release_id",
        "active_source_sha",
        "sections",
    }
)


def empty_sections() -> dict[str, object]:
    return {
        "lock": None,
        "filesystem": {},
        "journal": None,
        "services": {},
        "categories": {},
        "retention": None,
    }


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _safe_sha(value: object) -> bool:
    return value == "unavailable" or (
        isinstance(value, str) and SHA_RE.fullmatch(value) is not None
    )


def _safe_provenance(value: object, *, sha: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if sha:
        return _safe_sha(value)
    return value == "unavailable" or RELEASE_ID_RE.fullmatch(value) is not None


def _closed_values(value: object) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _closed_values(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_closed_values(item) for item in value)
    if isinstance(value, bool):
        return True
    return _finite_number(value)


def _nonnegative(value: object, *, maximum: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number) and number >= 0 and (
        maximum is None or number <= maximum
    )


def _exact_numeric_summary(value: object, keys: set[str], *, maximum: float | None = None) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == keys
        and all(_nonnegative(value.get(key), maximum=maximum) for key in keys)
    )


def _validate_service(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema", "kind", "status", "service", "error_class", "properties"
    }:
        return False
    if value.get("schema") != SCHEMA or value.get("kind") != "service_state" or value.get("status") != "ok":
        return False
    if value.get("service") not in SAFE_SERVICES or value.get("error_class") not in {
        "none", "timeout", "crash", "unknown", "failure"
    }:
        return False
    properties = value.get("properties")
    if not isinstance(properties, dict) or set(properties) != set(SERVICE_PROPERTY_KEYS):
        return False
    for key in ("ActiveState", "SubState", "Result", "ExecMainCode"):
        if properties.get(key) not in SERVICE_ENUMS[key]:
            return False
    for key in set(SERVICE_PROPERTY_KEYS) - set(SERVICE_ENUMS):
        field = properties.get(key)
        if key in SERVICE_INFINITY_KEYS and field == "infinity":
            continue
        if isinstance(field, bool) or not isinstance(field, int) or field < 0:
            return False
    return True


def _validate_retention(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema", "kind", "status", "ok", "mode", "categories", "transient",
        "transient_reclaimable_bytes", "duration_seconds", "limits", "disk_before",
        "disk_after", "backup",
    }:
        return False
    if (
        value.get("schema") != SCHEMA
        or value.get("kind") != "storage_retention"
        or value.get("status") != "ok"
        or value.get("ok") is not True
        or value.get("mode") != "dry-run"
    ):
        return False
    categories = value.get("categories")
    category_keys = {"protected_count", "retained_count", "deleted_count", "reclaimable_bytes"}
    if not isinstance(categories, dict) or set(categories) != {
        "production_releases", "source_release_artifacts", "live_qa_runtime"
    }:
        return False
    if not all(_exact_numeric_summary(item, category_keys, maximum=10**18) for item in categories.values()):
        return False
    transient = value.get("transient")
    transient_keys = {"failed_builds", "browser_test_artifacts", "preprod_screenshots"}
    item_keys = {"count", "reclaimable_bytes"}
    if not isinstance(transient, dict) or set(transient) != transient_keys:
        return False
    if not all(_exact_numeric_summary(item, item_keys, maximum=10**18) for item in transient.values()):
        return False
    transient_bytes = value.get("transient_reclaimable_bytes")
    if not isinstance(transient_bytes, dict) or set(transient_bytes) != transient_keys:
        return False
    if not all(_nonnegative(item, maximum=10**18) for item in transient_bytes.values()):
        return False
    if not _nonnegative(value.get("duration_seconds"), maximum=10**9):
        return False
    for key in ("disk_before", "disk_after"):
        if not _exact_numeric_summary(value.get(key), {"free_bytes", "used_percent"}, maximum=10**18):
            return False
        if value[key]["used_percent"] > 100:
            return False
    limits = value.get("limits")
    if not _exact_numeric_summary(limits, {"minimum_free_bytes", "maximum_used_percent"}, maximum=10**18):
        return False
    if limits["maximum_used_percent"] > 100:
        return False
    backup = value.get("backup")
    backup_keys = {
        "status", "restore_verified", "alembic_revision_verified", "checksum_present",
        "size_bytes", "duration_seconds", "restored_table_count", "removed_count",
    }
    if not isinstance(backup, dict) or set(backup) != backup_keys or backup.get("status") not in {"completed", "skipped"}:
        return False
    if backup.get("status") == "completed":
        if not all(backup.get(key) is True for key in ("restore_verified", "alembic_revision_verified", "checksum_present")):
            return False
        if not all(_nonnegative(backup.get(key), maximum=10**18) for key in ("size_bytes", "duration_seconds", "restored_table_count", "removed_count")):
            return False
    else:
        if any(backup.get(key) is not None for key in ("restore_verified", "alembic_revision_verified", "size_bytes", "duration_seconds", "restored_table_count", "removed_count")):
            return False
        if backup.get("checksum_present") is not False:
            return False
    return True


def validate_sections(sections: object) -> bool:
    """Validate every projected section, not just the top-level names."""

    if not isinstance(sections, dict) or set(sections) != set(EXPECTED_SECTIONS):
        return False
    lock = sections.get("lock")
    if not isinstance(lock, dict) or set(lock) != {"schema", "kind", "status", "state", "lock_held", "holder_count"}:
        return False
    if (
        lock.get("schema") != SCHEMA or lock.get("kind") != "lock_state" or lock.get("status") != "ok"
        or lock.get("state") not in {"held", "unlocked"}
        or not isinstance(lock.get("lock_held"), bool)
        or not isinstance(lock.get("holder_count"), int)
        or isinstance(lock.get("holder_count"), bool)
        or lock.get("holder_count") not in {0, 1}
    ):
        return False
    journal = sections.get("journal")
    if not isinstance(journal, dict) or set(journal) != {"schema", "kind", "status", "size_bytes"}:
        return False
    if journal.get("schema") != SCHEMA or journal.get("kind") != "journal_usage" or journal.get("status") != "ok" or not _nonnegative(journal.get("size_bytes"), maximum=10**18):
        return False
    filesystem = sections.get("filesystem")
    if not isinstance(filesystem, dict) or set(filesystem) != set(SAFE_CATEGORIES):
        return False
    disk_keys = {"schema", "kind", "status", "category", "total_bytes", "used_bytes", "free_bytes", "used_percent"}
    inode_keys = {"schema", "kind", "status", "category", "used_inodes", "free_inodes", "used_percent"}
    for category, entry in filesystem.items():
        if not isinstance(entry, dict) or set(entry) != disk_keys | {"inode"}:
            return False
        if entry.get("schema") != SCHEMA or entry.get("kind") != "disk_usage" or entry.get("status") != "ok" or entry.get("category") != category:
            return False
        if not all(_nonnegative(entry.get(key), maximum=10**18) for key in ("total_bytes", "used_bytes", "free_bytes")) or not _nonnegative(entry.get("used_percent"), maximum=100):
            return False
        inode = entry.get("inode")
        if not isinstance(inode, dict) or set(inode) != inode_keys or inode.get("schema") != SCHEMA or inode.get("kind") != "inode_usage" or inode.get("status") != "ok" or inode.get("category") != category:
            return False
        if not all(_nonnegative(inode.get(key), maximum=10**18) for key in ("used_inodes", "free_inodes")) or not _nonnegative(inode.get("used_percent"), maximum=100):
            return False
    categories = sections.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(SAFE_DU_CATEGORIES):
        return False
    category_keys = {"schema", "kind", "status", "category", "size_bytes"}
    for category, entry in categories.items():
        if not isinstance(entry, dict) or set(entry) != category_keys or entry.get("schema") != SCHEMA or entry.get("kind") != "category_size" or entry.get("status") != "ok" or entry.get("category") != category or not _nonnegative(entry.get("size_bytes"), maximum=10**18):
            return False
    services = sections.get("services")
    if not isinstance(services, dict) or set(services) != set(SAFE_SERVICES) or not all(_validate_service(item) for item in services.values()):
        return False
    return _validate_retention(sections.get("retention"))


def validate_artifact(payload: object) -> bool:
    """Validate the public artifact shape without reading any raw producer data."""

    if not isinstance(payload, dict) or not _closed_values(payload):
        return False
    status = payload.get("status")
    expected_keys = SUCCESS_KEYS if status == "passed" else FAILURE_KEYS if status == "failed" else frozenset()
    if not expected_keys or set(payload) != set(expected_keys):
        return False
    if payload.get("schema") != SCHEMA or payload.get("raw_output_included") is not False:
        return False
    if status == "passed":
        if payload.get("kind") != "platform_storage_diagnostics":
            return False
        if (
            payload.get("remote_exit_code") != 0
            or payload.get("report_present") is not True
            or payload.get("stderr_truncated") is not False
            or payload.get("report_truncated") is not False
        ):
            return False
    else:
        if payload.get("kind") != "platform_storage_diagnostics_failure":
            return False
        if payload.get("phase") not in SAFE_FAILURE_PHASES:
            return False
        if payload.get("reason") not in SAFE_FAILURE_REASONS:
            return False
        if payload.get("action") not in SAFE_FAILURE_ACTIONS:
            return False
    if not _safe_sha(payload.get("expected_sha")):
        return False
    for key in ("remote_exit_code", "remote_stderr_bytes"):
        value = payload.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            return False
        if value is not None and value < 0:
            return False
        if key == "remote_exit_code" and value is not None and value > 255:
            return False
        if key == "remote_stderr_bytes" and value is not None and value > MAX_REPORTED_BYTES:
            return False
    if not all(isinstance(payload.get(key), bool) for key in ("stderr_truncated", "report_present", "report_truncated")):
        return False
    if not _safe_provenance(payload.get("active_release_id")):
        return False
    if not _safe_provenance(payload.get("active_source_sha"), sha=True):
        return False
    sections = payload.get("sections")
    if status == "failed":
        return sections == empty_sections()
    return validate_sections(sections)


def normalize_failure_values(
    *,
    expected_sha: object,
    remote_exit_code: object,
    remote_stderr_bytes: object,
    report_present: object,
    report_truncated: bool = False,
    stderr_truncated: bool = False,
    phase: object = "capture",
    reason: object = "remote_collection_failed",
    action: object = "inspect_remote_collection",
) -> dict[str, object]:
    """Build the one allowed fallback payload for runner-side emergencies."""

    def integer(value: object, maximum: int) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            result = int(value) if value not in (None, "") else None
        except (TypeError, ValueError, OverflowError):
            return None
        return result if result is not None and 0 <= result <= maximum else None

    safe_expected = expected_sha if isinstance(expected_sha, str) and SHA_RE.fullmatch(expected_sha) else "unavailable"
    safe_phase = (
        phase
        if isinstance(phase, str) and phase in SAFE_FAILURE_PHASES
        else "capture"
    )
    safe_reason = (
        reason
        if isinstance(reason, str) and reason in SAFE_FAILURE_REASONS
        else "remote_collection_failed"
    )
    safe_action = (
        action
        if isinstance(action, str) and action in SAFE_FAILURE_ACTIONS
        else "inspect_remote_collection"
    )
    payload = {
        "schema": SCHEMA,
        "kind": "platform_storage_diagnostics_failure",
        "status": "failed",
        "raw_output_included": False,
        "expected_sha": safe_expected,
        "remote_exit_code": integer(remote_exit_code, 255),
        "remote_stderr_bytes": integer(remote_stderr_bytes, MAX_REPORTED_BYTES),
        "stderr_truncated": bool(stderr_truncated),
        "report_present": report_present is True or report_present == "true",
        "report_truncated": bool(report_truncated),
        "phase": safe_phase,
        "reason": safe_reason,
        "action": safe_action,
        "active_release_id": "unavailable",
        "active_source_sha": "unavailable",
        "sections": empty_sections(),
    }
    if not validate_artifact(payload):
        raise ValueError("invalid failure contract")
    return payload


def _validate_path(path: str) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("artifact metadata")
        chunks = bytearray()
        while len(chunks) <= 512 * 1024:
            chunk = os.read(descriptor, min(65536, 512 * 1024 + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
    finally:
        os.close(descriptor)
    if len(chunks) > 512 * 1024:
        raise ValueError("artifact bound")
    payload = json.loads(bytes(chunks).decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    if not validate_artifact(payload):
        raise ValueError("artifact contract")
    print(payload["status"])
    return 0


def _write_failure(path: str, args: argparse.Namespace) -> int:
    payload = normalize_failure_values(
        expected_sha=args.expected_sha,
        remote_exit_code=args.remote_exit_code,
        remote_stderr_bytes=args.remote_stderr_bytes,
        report_present=args.report_present,
        stderr_truncated=args.stderr_truncated,
        report_truncated=args.report_truncated,
        phase=args.phase,
        reason=args.reason,
        action=args.action,
    )
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    if len(encoded) > 512 * 1024:
        raise ValueError("artifact bound")
    parent = os.path.dirname(path)
    parent_info = os.lstat(parent)
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError("artifact parent")
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise ValueError("artifact symlink")
    if existing is not None and stat.S_ISREG(existing.st_mode):
        os.unlink(path)
    temporary = os.path.join(parent, "." + os.path.basename(path) + ".contract.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("artifact metadata")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if not validate_artifact(json.loads(encoded)):
            raise ValueError("artifact contract")
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate storage diagnostics artifact.")
    parser.add_argument("--validate", metavar="PATH")
    parser.add_argument("--write-failure", metavar="PATH")
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--remote-exit-code", default="")
    parser.add_argument("--remote-stderr-bytes", default="")
    parser.add_argument("--report-present", default="false")
    parser.add_argument("--stderr-truncated", action="store_true")
    parser.add_argument("--report-truncated", action="store_true")
    parser.add_argument("--phase", default="capture")
    parser.add_argument("--reason", default="remote_collection_failed")
    parser.add_argument("--action", default="inspect_remote_collection")
    args = parser.parse_args(argv)
    try:
        if args.validate:
            return _validate_path(args.validate)
        if args.write_failure:
            return _write_failure(args.write_failure, args)
        parser.error("--validate or --write-failure is required")
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
