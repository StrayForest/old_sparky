#!/usr/bin/env python3
"""Verify exact source-bound live-QA artifacts before maintenance timer activation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))

import platform_backup_restore_drill as backup_restore_drill
from platform_release_retention import REQUIRED_PROTECTED_RELEASES

from platform_workflow_provenance import (
    ProvenanceError,
    validate_workflow_run,
)


REPOSITORY = "StrayForest/old_sparky"
API_ORIGIN = "https://api.github.com"
LIVE_LAUNCH_PATH = ".github/workflows/platform-live-launch.yml"
LIVE_LAUNCH_NAME = "Platform live launch sanity"
LIVE_USER_QA_PATH = ".github/workflows/platform-live-user-qa.yml"
LIVE_USER_QA_NAME = "Platform live user QA"
MAX_API_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_UNPACKED_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 128 * 1024
MAX_MAINTENANCE_REPORT_BYTES = 256 * 1024
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,31}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
BACKUP_DUMP_RE = re.compile(r"platformdb-[0-9]{8}T[0-9]{6}Z\.dump\Z")
MAINTENANCE_REPORT_RE = re.compile(r"platform-maintenance-[0-9]{8}T[0-9]{6}Z\.json\Z")
PROTECTED_BACKUP_MANIFEST_PATH = Path(
    "/opt/oldsparky/platform/shared/backup-retention/protected-dump-files.json"
)
MAINTENANCE_REPORT_DIRECTORY = Path("/opt/oldsparky/platform/shared/maintenance")
PROTECTED_BACKUP_KEEP = 15
UNIT_PROPERTY_KEYS = frozenset(
    {
        "FragmentPath",
        "DropInPaths",
        "NeedDaemonReload",
        "ActiveState",
        "SubState",
        "MainPID",
        "ExecStart",
        "CPUQuotaPerSecUSec",
        "MemoryMax",
        "TimeoutStartUSec",
    }
)
EXEC_START_FIELDS = (
    "path",
    "argv[]",
    "ignore_errors",
    "start_time",
    "stop_time",
    "pid",
    "code",
    "status",
)
EXPECTED_MAINTENANCE_ARGV = (
    "/opt/oldsparky/platform/shared/venv/bin/python",
    "/opt/oldsparky/platform/current/tools/platform_storage_maintenance.py",
    "--apply",
    "--scratch-backend",
    "disk_ext4_image_v1",
    "--protected-backup-manifest",
    "/opt/oldsparky/platform/shared/backup-retention/protected-dump-files.json",
    "--backup-keep",
    "15",
    *(
        argument
        for slug in REQUIRED_PROTECTED_RELEASES
        for argument in ("--protected-release", slug)
    ),
    "--release-keep",
    "5",
    "--test-artifact-max-age-days",
    "7",
    "--screenshot-max-age-days",
    "30",
    "--failed-build-max-age-days",
    "1",
    "--minimum-free-gib",
    "5",
    "--maximum-used-percent",
    "85",
)


def maintenance_preview_argv() -> tuple[str, ...]:
    """Derive the read-only retention CLI from the reviewed service argv."""
    argv = list(EXPECTED_MAINTENANCE_ARGV)
    if argv.count("--apply") != 1 or "--json" in argv:
        _fail()
    argv.remove("--apply")
    argv.insert(2, "--json")
    return tuple(argv)


def run_validated_maintenance_preview(
    source_path: str, installed_path: str, properties: str
) -> int:
    """Run only the read-only preview derived from an exactly bound unit."""
    validate_loaded_maintenance_unit(source_path, installed_path, properties)
    completed = subprocess.run(
        maintenance_preview_argv(),
        stdin=subprocess.DEVNULL,
        stdout=sys.stdout.buffer,
        stderr=subprocess.DEVNULL,
        timeout=180,
        check=False,
    )
    return completed.returncode
MAX_UNIT_BYTES = 64 * 1024

LAUNCH_LOGICAL_FIELDS = (
    "logical_total",
    "logical_pass",
    "logical_fail",
    "logical_expected_fail",
    "logical_flaky",
    "logical_skip",
    "logical_interrupted",
)
LAUNCH_ATTEMPT_FIELDS = (
    "attempt_total",
    "attempt_pass",
    "attempt_fail",
    "attempt_skip",
    "attempt_interrupted",
    "attempt_timedout",
)
USER_COUNT_FIELDS = (
    "logical_total",
    "logical_pass",
    "logical_fail",
    "logical_expected_fail",
    "logical_flaky",
    "logical_skip",
    "logical_interrupted",
    "attempt_total",
    "attempt_pass",
    "attempt_fail",
    "attempt_skip",
    "attempt_interrupted",
    "attempt_timedout",
)
LAUNCH_REPORT_KEYS = frozenset(
    {
        "schema", "source_git_sha", "app_target_sha", "source_binding_sha256",
        "status", "success", "failure_reason", "process_exit_code", "stage",
        "check_id", "child_exit_code", "source_sha", "line_count",
        "retained_line_count", "truncated", "test_count", "failed_test_count",
        "flaky_test_count", "skipped_test_count", "interrupted_test_count",
        "logical_counts", "attempt_counts", "tests", "class_counts",
        "route_class_counts", "status_counts", "error_class_counts",
        "child_diagnostic",
    }
)
USER_REPORT_KEYS = frozenset(
    {
        "schema", "source_git_sha", "app_target_sha", "source_binding_sha256",
        "status", "success", "process_exit_code", "line_count",
        "retained_line_count", "truncated", "coverage_status", "test_count",
        "logical_total", "logical_skip_count", "logical_expected_fail_count",
        "failed_test_count", "browser_counts", "error_class_counts",
        "child_diagnostic", "failure_origin",
    }
)
USER_BROWSER_COUNT_KEYS = frozenset(
    {
        "run_status", *USER_COUNT_FIELDS, "source_sha", "app_sha", "marker_sha256"
    }
)


class EvidenceError(ValueError):
    """A bounded source-bound QA proof is missing or invalid."""


def _fail() -> None:
    raise EvidenceError("source-bound live QA evidence is invalid")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _strict_json(raw: bytes) -> dict[str, Any]:
    if not raw or len(raw) > MAX_API_BYTES:
        _fail()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, EvidenceError, RecursionError):
        _fail()
    if not isinstance(value, dict):
        _fail()
    return value


def protected_backup_manifest_snapshot_digest() -> str:
    """Return only the digest of the exact private manifest snapshot."""

    snapshot = backup_restore_drill._protected_backup_manifest_snapshot(
        PROTECTED_BACKUP_MANIFEST_PATH
    )
    digest = snapshot.get("digest") if isinstance(snapshot, dict) else None
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        _fail()
    return digest


def _read_private_maintenance_report(path_value: str) -> dict[str, Any]:
    if (
        not isinstance(path_value, str)
        or "\x00" in path_value
        or len(path_value) > 512
    ):
        _fail()
    path = Path(path_value)
    if (
        path.parent != MAINTENANCE_REPORT_DIRECTORY
        or MAINTENANCE_REPORT_RE.fullmatch(path.name) is None
    ):
        _fail()
    try:
        parent = path.parent.lstat()
        resolved_parent = path.parent.resolve(strict=True)
        named = path.lstat()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        _fail()
    try:
        if (
            resolved_parent != path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != 0
            or parent.st_gid != 0
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            _fail()
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_gid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > MAX_MAINTENANCE_REPORT_BYTES
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            _fail()
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(
                descriptor,
                min(8192, MAX_MAINTENANCE_REPORT_BYTES + 1 - total),
            )
            if not block:
                break
            total += len(block)
            if total > MAX_MAINTENANCE_REPORT_BYTES:
                _fail()
            chunks.append(block)
        after = os.fstat(descriptor)
        current = path.lstat()
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
            or any(getattr(after, field) != getattr(current, field) for field in identity_fields)
        ):
            _fail()
    finally:
        os.close(descriptor)
    return _strict_json(b"".join(chunks))


def validate_protected_retention_report(
    report: dict[str, Any], expected_manifest_digest: str
) -> None:
    """Bind private rotation evidence to the pre-service manifest snapshot."""

    if (
        not isinstance(report, dict)
        or report.get("mode") != "apply"
        or type(report.get("ok")) is not bool
        or not isinstance(expected_manifest_digest, str)
        or DIGEST_RE.fullmatch(expected_manifest_digest) is None
    ):
        _fail()
    backup = report.get("backup")
    if not isinstance(backup, dict):
        _fail()
    archive_name = backup.get("archive_name")
    archive_sha256 = backup.get("archive_sha256")
    archive_size = backup.get("size_bytes")
    proof = backup.get("protected_retention_proof")
    if (
        backup.get("status") != "completed"
        or backup.get("created") is not True
        or backup.get("restore_verified") is not True
        or backup.get("alembic_revision_verified") is not True
        or backup.get("checksum_present") is not True
        or backup.get("rotation_mode") != "rotate-existing"
        or type(backup.get("format_version")) is not int
        or backup["format_version"] != 2
        or not isinstance(archive_name, str)
        or BACKUP_DUMP_RE.fullmatch(archive_name) is None
        or not isinstance(archive_sha256, str)
        or DIGEST_RE.fullmatch(archive_sha256) is None
        or type(archive_size) is not int
        or archive_size <= 0
        or not backup_restore_drill.valid_protected_retention_proof(
            proof,
            requested_keep=PROTECTED_BACKUP_KEEP,
            new_dump_file=archive_name,
        )
    ):
        _fail()
    manifest = proof.get("manifest")
    selection = proof.get("verified_selection")
    removed_pair_count = proof.get("removed_pair_count")
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("before"), dict)
        or manifest["before"].get("digest") != expected_manifest_digest
        or not isinstance(selection, dict)
        or not isinstance(selection.get("selected_order"), list)
        or type(removed_pair_count) is not int
        or type(backup.get("removed_count")) is not int
        or backup["removed_count"] != 2 * removed_pair_count
    ):
        _fail()
    matching_archive = [
        row
        for row in selection["selected_order"]
        if isinstance(row, dict) and row.get("dump_file") == archive_name
    ]
    if (
        len(matching_archive) != 1
        or matching_archive[0].get("claimed_sha256") != archive_sha256
        or matching_archive[0].get("claimed_size_bytes") != archive_size
    ):
        _fail()


def _positive_id(value: object) -> int:
    if type(value) is not int or value <= 0 or value > 2**63 - 1:
        _fail()
    return value


def _safe_unit_bytes(path_value: str, *, installed: bool) -> bytes:
    if not isinstance(path_value, str) or not path_value.startswith("/") or "\x00" in path_value:
        _fail()
    path = Path(path_value)
    expected_installed = Path("/etc/systemd/system/deadlock-maintenance.service")
    if installed and path != expected_installed:
        _fail()
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        raw = path.read_bytes()
        after = path.lstat()
    except OSError:
        _fail()
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
        resolved != path
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != 0
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size > MAX_UNIT_BYTES
        or len(raw) != before.st_size
        or any(getattr(before, field) != getattr(after, field) for field in identity_fields)
    ):
        _fail()
    return raw


def _parse_systemd_exec_start(value: object) -> dict[str, str]:
    if not isinstance(value, str) or not value.startswith("{ ") or not value.endswith(" }"):
        _fail()
    fields: dict[str, str] = {}
    for item in value[2:-2].split(" ; "):
        key, separator, field_value = item.partition("=")
        if (
            not separator
            or key not in EXEC_START_FIELDS
            or key in fields
            or (not field_value and key not in {"start_time", "stop_time"})
        ):
            _fail()
        fields[key] = field_value
    if tuple(fields) != EXEC_START_FIELDS:
        _fail()
    never_run_time = {"", "[n/a]"}
    no_run = (
        fields["pid"] == "0"
        and fields["code"] == "(null)"
        and fields["status"] == "0/0"
        and fields["start_time"] in never_run_time
        and fields["start_time"] == fields["stop_time"]
        and fields["stop_time"] in never_run_time
    )
    pid_match = re.fullmatch(r"[1-9][0-9]{0,9}", fields["pid"])
    completed = (
        pid_match is not None
        and int(fields["pid"], 10) <= 2**31 - 1
        and fields["code"] in {"exited", "killed", "dumped"}
        and re.fullmatch(r"[0-9]{1,3}(?:/[A-Za-z0-9_-]{1,32})?", fields["status"])
        is not None
        and fields["status"] != "0/0"
        and fields["start_time"] not in never_run_time
        and fields["stop_time"] not in never_run_time
    )
    if (
        fields["ignore_errors"] != "no"
        or not (no_run or completed)
        or any(
            len(fields[name]) > 128
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in fields[name])
            for name in ("start_time", "stop_time")
        )
    ):
        _fail()
    return fields


def validate_loaded_maintenance_unit(
    source_path: str, installed_path: str, systemctl_output: str
) -> None:
    """Fail closed unless the loaded maintenance service matches this release."""

    source_bytes = _safe_unit_bytes(source_path, installed=False)
    installed_bytes = _safe_unit_bytes(installed_path, installed=True)
    _validate_maintenance_unit_bytes(
        source_bytes, installed_bytes, installed_path, systemctl_output
    )


def _validate_maintenance_unit_bytes(
    source_bytes: bytes,
    installed_bytes: bytes,
    installed_path: str,
    systemctl_output: str,
) -> None:
    if source_bytes != installed_bytes or not isinstance(systemctl_output, str):
        _fail()
    if len(systemctl_output) > MAX_UNIT_BYTES or not systemctl_output.endswith("\n"):
        _fail()
    properties: dict[str, str] = {}
    for line in systemctl_output.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in UNIT_PROPERTY_KEYS or key in properties:
            _fail()
        properties[key] = value
    if set(properties) != set(UNIT_PROPERTY_KEYS):
        _fail()
    if (
        properties["FragmentPath"] != installed_path
        or properties["DropInPaths"] != ""
        or properties["NeedDaemonReload"] != "no"
        or properties["CPUQuotaPerSecUSec"] != "500ms"
        or properties["MemoryMax"] != "536870912"
        or properties["TimeoutStartUSec"] != "30min"
        or (properties["ActiveState"], properties["SubState"])
        not in {("inactive", "dead"), ("failed", "failed")}
        or properties["MainPID"] != "0"
    ):
        _fail()
    exec_fields = _parse_systemd_exec_start(properties["ExecStart"])
    try:
        text = source_bytes.decode("ascii")
    except UnicodeError:
        _fail()
    sections: set[str] = set()
    current_section = ""
    directive_values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            if current_section in sections:
                _fail()
            sections.add(current_section)
            continue
        if current_section != "Service" or "=" not in line:
            continue
        key, item = line.split("=", 1)
        directive_values.setdefault(key, []).append(item)
        if key.startswith("Exec") and key != "ExecStart":
            _fail()
    for name, expected in (
        ("CPUQuota", "50%"),
        ("MemoryMax", "512M"),
        ("TimeoutStartSec", "30min"),
    ):
        values = directive_values.get(name)
        if values != [expected]:
            _fail()
    exec_values = directive_values.get("ExecStart")
    if exec_values is None or len(exec_values) != 1:
        _fail()
    try:
        source_argv = tuple(shlex.split(exec_values[0], posix=True))
        loaded_argv = tuple(exec_fields["argv[]"].split())
    except ValueError:
        _fail()
    if (
        source_argv != EXPECTED_MAINTENANCE_ARGV
        or loaded_argv != source_argv
        or exec_fields["path"] != source_argv[0]
    ):
        _fail()


def _input_id(value: str) -> int:
    if not isinstance(value, str) or RUN_ID_RE.fullmatch(value) is None:
        _fail()
    number = int(value, 10)
    if number > 2**63 - 1:
        _fail()
    return number


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        _fail()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail()
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _fail()
    return parsed.astimezone(timezone.utc)


def _closed_ints(value: object, fields: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail()
    result: dict[str, int] = {}
    for field in fields:
        item = value.get(field)
        if type(item) is not int or not 0 <= item <= 100_000:
            _fail()
        result[field] = item
    return result


def _check_source_binding(
    report: dict[str, Any], expected_runner_sha: str, expected_app_target_sha: str
) -> None:
    """Bind report claims to the exact runner and deployed app target.

    For runner/target mismatches, the exact successful QA workflow run has
    already authenticated its verified-noop handoff before it emits this
    report; require that workflow's canonical binding digest to be carried
    through alongside the explicitly expected app target.
    """
    app_sha = report.get("app_target_sha")
    binding = report.get("source_binding_sha256")
    if (
        not isinstance(expected_runner_sha, str)
        or SHA_RE.fullmatch(expected_runner_sha) is None
        or not isinstance(expected_app_target_sha, str)
        or SHA_RE.fullmatch(expected_app_target_sha) is None
        or not isinstance(app_sha, str)
        or SHA_RE.fullmatch(app_sha) is None
        or app_sha != expected_app_target_sha
        or (expected_app_target_sha == expected_runner_sha and binding is not None)
        or (
            expected_app_target_sha != expected_runner_sha
            and (not isinstance(binding, str) or DIGEST_RE.fullmatch(binding) is None)
        )
    ):
        _fail()


def _check_success_child_diagnostic(value: object) -> None:
    if value is None:
        return
    expected_keys = {"kind", "stdout_bytes", "stderr_bytes", "truncated", "child_exit_code"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        _fail()
    if (
        value.get("kind") != "none"
        or type(value.get("stdout_bytes")) is not int
        or not 0 <= value["stdout_bytes"] <= 2**63 - 1
        or type(value.get("stderr_bytes")) is not int
        or not 0 <= value["stderr_bytes"] <= 2**63 - 1
        or value.get("truncated") is not False
        or value.get("child_exit_code") != 0
        or type(value.get("child_exit_code")) is not int
    ):
        _fail()


def validate_live_launch_report(
    report: dict[str, Any], expected_runner_sha: str, expected_app_target_sha: str
) -> None:
    if (
        set(report) != LAUNCH_REPORT_KEYS
        or type(report.get("schema")) is not int
        or report.get("schema") != 1
        or report.get("source_git_sha") != expected_runner_sha
        or report.get("source_sha") != expected_runner_sha
        or report.get("status") != "passed"
        or report.get("success") is not True
        or report.get("failure_reason") != "none"
        or report.get("process_exit_code") != 0
        or type(report.get("process_exit_code")) is not int
        or report.get("stage") != "complete"
        or report.get("check_id") != "none"
        or report.get("child_exit_code") != 0
        or type(report.get("child_exit_code")) is not int
        or report.get("truncated") is not False
        or report.get("tests") != []
    ):
        _fail()
    for field in (
        "line_count", "retained_line_count", "test_count", "failed_test_count",
        "flaky_test_count", "skipped_test_count", "interrupted_test_count",
    ):
        if type(report.get(field)) is not int or report[field] < 0:
            _fail()
    _check_source_binding(report, expected_runner_sha, expected_app_target_sha)
    _check_success_child_diagnostic(report.get("child_diagnostic"))
    logical = _closed_ints(report.get("logical_counts"), LAUNCH_LOGICAL_FIELDS)
    attempts = _closed_ints(report.get("attempt_counts"), LAUNCH_ATTEMPT_FIELDS)
    total = logical["logical_total"]
    skipped = logical["logical_skip"]
    if (
        total != 54
        or not 6 <= skipped <= 10
        or logical["logical_pass"] != total - skipped
        or any(
            logical[name] != 0
            for name in (
                "logical_fail",
                "logical_expected_fail",
                "logical_flaky",
                "logical_interrupted",
            )
        )
        or attempts["attempt_total"] != total
        or attempts["attempt_pass"] != logical["logical_pass"]
        or attempts["attempt_skip"] != skipped
        or any(attempts[name] != 0 for name in ("attempt_fail", "attempt_interrupted", "attempt_timedout"))
        or report.get("test_count") != total - skipped
        or report.get("failed_test_count") != 0
        or report.get("flaky_test_count") != 0
        or report.get("skipped_test_count") != skipped
        or report.get("interrupted_test_count") != 0
        or _closed_ints(report.get("class_counts"), LAUNCH_LOGICAL_FIELDS) != logical
        or report.get("route_class_counts") != {}
        or report.get("error_class_counts") != {"none": 1}
        or report.get("line_count") != report.get("retained_line_count")
        or type(report.get("line_count")) is not int
        or report.get("line_count") not in {2, 3}
    ):
        _fail()
    status_counts = _closed_ints(
        report.get("status_counts"),
        ("passed", "failed", "expected_failed", "flaky", "skipped", "interrupted"),
    )
    if status_counts != {
        "passed": total - skipped,
        "failed": 0,
        "expected_failed": 0,
        "flaky": 0,
        "skipped": skipped,
        "interrupted": 0,
    }:
        _fail()
def validate_live_user_qa_report(
    report: dict[str, Any], expected_runner_sha: str, expected_app_target_sha: str
) -> None:
    if (
        set(report) != USER_REPORT_KEYS
        or type(report.get("schema")) is not int
        or report.get("schema") != 1
        or report.get("source_git_sha") != expected_runner_sha
        or report.get("status") != "passed"
        or report.get("failure_origin") is not None
        or report.get("success") is not True
        or report.get("process_exit_code") != 0
        or type(report.get("process_exit_code")) is not int
        or report.get("truncated") is not False
        or report.get("coverage_status") != "complete"
        or report.get("test_count") != 1
        or type(report.get("test_count")) is not int
        or report.get("logical_total") != 1
        or report.get("logical_skip_count") != 0
        or report.get("logical_expected_fail_count") != 0
        or report.get("failed_test_count") != 0
        or report.get("error_class_counts") != {}
        or not isinstance(report.get("child_diagnostic"), dict)
        or report.get("line_count") != report.get("retained_line_count")
        or type(report.get("line_count")) is not int
        or report.get("line_count") < 2
    ):
        _fail()
    for field in (
        "line_count", "retained_line_count", "test_count", "logical_total",
        "logical_skip_count", "logical_expected_fail_count", "failed_test_count",
    ):
        if type(report.get(field)) is not int or report[field] < 0:
            _fail()
    _check_source_binding(report, expected_runner_sha, expected_app_target_sha)
    _check_success_child_diagnostic(report.get("child_diagnostic"))
    counts = report.get("browser_counts")
    if not isinstance(counts, dict) or set(counts) != USER_BROWSER_COUNT_KEYS:
        _fail()
    if (
        counts.get("run_status") != "passed"
        or counts.get("source_sha") != expected_runner_sha
        or counts.get("app_sha") != report.get("app_target_sha")
        or not isinstance(counts.get("marker_sha256"), str)
        or DIGEST_RE.fullmatch(counts["marker_sha256"]) is None
    ):
        _fail()
    for field in USER_COUNT_FIELDS:
        item = counts.get(field)
        if type(item) is not int or not 0 <= item <= 100_000:
            _fail()
    if (
        counts["logical_total"] != 1
        or counts["logical_pass"] != 1
        or any(counts[name] != 0 for name in USER_COUNT_FIELDS[2:7])
        or counts["attempt_total"] != 1
        or counts["attempt_pass"] != 1
        or any(counts[name] != 0 for name in USER_COUNT_FIELDS[9:])
    ):
        _fail()


def _parse_report_member(archive_bytes: bytes, expected_member: str) -> dict[str, Any]:
    if not archive_bytes or len(archive_bytes) > MAX_ARCHIVE_BYTES:
        _fail()
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            entries = archive.infolist()
            if len(entries) != 1:
                _fail()
            names: set[str] = set()
            selected: zipfile.ZipInfo | None = None
            unpacked = 0
            for entry in entries:
                name = entry.filename
                mode = (entry.external_attr >> 16) & 0o170000
                if (
                    not isinstance(name, str)
                    or not name
                    or name.startswith("/")
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or name in names
                    or entry.flag_bits & 1
                    or entry.is_dir()
                    or mode not in {0, stat.S_IFREG}
                    or entry.file_size < 0
                    or entry.compress_size < 0
                    or entry.file_size > MAX_ARCHIVE_UNPACKED_BYTES
                    or entry.compress_size > MAX_ARCHIVE_BYTES
                ):
                    _fail()
                names.add(name)
                unpacked += entry.file_size
                if unpacked > MAX_ARCHIVE_UNPACKED_BYTES:
                    _fail()
                if name == expected_member:
                    if selected is not None or entry.file_size > MAX_REPORT_BYTES:
                        _fail()
                    selected = entry
            if selected is None:
                _fail()
            with archive.open(selected, "r") as member:
                report_bytes = member.read(MAX_REPORT_BYTES + 1)
            if len(report_bytes) != selected.file_size or len(report_bytes) > MAX_REPORT_BYTES:
                _fail()
    except (OSError, EOFError, ValueError, RuntimeError, RecursionError, zlib.error, zipfile.BadZipFile):
        _fail()
    if not report_bytes.endswith(b"\n") or report_bytes.endswith(b"\n\n"):
        _fail()
    payload = _strict_json(report_bytes[:-1])
    try:
        canonical = json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        _fail()
    if canonical != report_bytes:
        _fail()
    return payload


def _validate_artifact_binding(
    *,
    workflow: dict[str, Any],
    run: dict[str, Any],
    listing: dict[str, Any],
    metadata: dict[str, Any],
    archive_bytes: bytes,
    run_id_text: str,
    attempt_text: str,
    expected_runner_sha: str,
    expected_app_target_sha: str,
    workflow_path: str,
    workflow_name: str,
    artifact_prefix: str,
    member_name: str,
    report_validator: Any,
    now: datetime | None = None,
) -> None:
    run_id = _input_id(run_id_text)
    attempt = _input_id(attempt_text)
    if (
        not isinstance(metadata, dict)
        or not isinstance(expected_runner_sha, str)
        or SHA_RE.fullmatch(expected_runner_sha) is None
        or not isinstance(expected_app_target_sha, str)
        or SHA_RE.fullmatch(expected_app_target_sha) is None
    ):
        _fail()
    expected_name = f"{artifact_prefix}-{run_id_text}-{attempt_text}"
    try:
        validate_workflow_run(
            workflow,
            run,
            expected_run_id=run_id,
            expected_attempt=attempt,
            expected_target_sha=expected_runner_sha,
            expected_event="workflow_dispatch",
            expected_branch="dev",
            expected_path=workflow_path,
            expected_name=workflow_name,
        )
    except (ProvenanceError, TypeError, ValueError, KeyError):
        _fail()
    if not isinstance(listing, dict):
        _fail()
    rows = listing.get("artifacts")
    if (
        not isinstance(rows, list)
        or type(listing.get("total_count")) is not int
        or listing["total_count"] != len(rows)
        or len(rows) > 100
        or any(not isinstance(row, dict) for row in rows)
    ):
        _fail()
    named = [row for row in rows if row.get("name") == expected_name]
    if len(named) != 1:
        _fail()
    listed = named[0]
    artifact_id = _positive_id(listed.get("id"))
    size = metadata.get("size_in_bytes") if isinstance(metadata, dict) else None
    digest = metadata.get("digest") if isinstance(metadata, dict) else None
    if (
        type(metadata.get("id")) is not int
        or metadata["id"] != artifact_id
        or metadata.get("name") != expected_name
        or metadata.get("expired") is not False
        or type(size) is not int
        or not 0 < size <= MAX_ARCHIVE_BYTES
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        _fail()
    for field, expected in (
        ("size_in_bytes", size),
        ("digest", digest),
        ("expired", False),
    ):
        if field in listed and listed.get(field) != expected:
            _fail()
    artifact_run = metadata.get("workflow_run")
    if (
        not isinstance(artifact_run, dict)
        or type(artifact_run.get("id")) is not int
        or artifact_run["id"] != run_id
        or artifact_run.get("head_sha") != expected_runner_sha
        or artifact_run.get("head_branch") != "dev"
        or ("run_attempt" in artifact_run and
            (type(artifact_run.get("run_attempt")) is not int or artifact_run["run_attempt"] != attempt))
    ):
        _fail()
    started_at = _utc(run.get("run_started_at"))
    completed_at = _utc(run.get("updated_at"))
    artifact_created = _utc(metadata.get("created_at"))
    artifact_expires = _utc(metadata.get("expires_at"))
    reference_time = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    if (
        completed_at < started_at
        or not started_at <= artifact_created <= completed_at
        or artifact_expires <= max(reference_time, artifact_created)
    ):
        _fail()
    if len(archive_bytes) != size or hashlib.sha256(archive_bytes).hexdigest() != digest.removeprefix("sha256:"):
        _fail()
    report = _parse_report_member(archive_bytes, member_name)
    report_validator(report, expected_runner_sha, expected_app_target_sha)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> None:
        return None


class _SafeArtifactRedirect(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirects = 0

    def redirect_request(self, request: Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> Request | None:
        self.redirects += 1
        if self.redirects != 1 or not _is_approved_artifact_url(new_url):
            return None
        return Request(new_url, headers={"Accept": "application/octet-stream"})


def _read_response(opener: Any, request: Request, maximum: int, *, exact_url: str | None = None) -> bytes:
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                _fail()
            final_url = response.geturl()
            parsed = urlsplit(final_url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or (exact_url is not None and final_url != exact_url)
                or (
                    exact_url is None
                    and final_url != request.full_url
                    and not _is_approved_artifact_url(final_url)
                )
            ):
                _fail()
            body = response.read(maximum + 1)
    except (OSError, URLError, HTTPError, TimeoutError):
        _fail()
    if not body or len(body) > maximum:
        _fail()
    return body


def _is_approved_artifact_url(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname.lower() if parsed.hostname is not None else None
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and hostname
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and "#" not in value
        and not hostname.endswith(".")
        and (
            hostname.endswith(".blob.core.windows.net")
            or hostname.endswith(".actions.githubusercontent.com")
        )
    )


def _api_json(opener: Any, url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers=headers)
    return _strict_json(_read_response(opener, request, MAX_API_BYTES, exact_url=url))


def _verify_one_qa(
    *,
    token: str,
    runner_sha: str,
    app_target_sha: str,
    run_id: str,
    attempt: str,
    workflow_path: str,
    workflow_name: str,
    artifact_prefix: str,
    member_name: str,
    report_validator: Any,
) -> None:
    run_id_value = _input_id(run_id)
    attempt_value = _input_id(attempt)
    if (
        not token
        or "\n" in token
        or "\r" in token
        or SHA_RE.fullmatch(runner_sha) is None
        or SHA_RE.fullmatch(app_target_sha) is None
    ):
        _fail()
    api_opener = build_opener(_NoRedirect())
    artifact_opener = build_opener(_SafeArtifactRedirect())
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "OldSparky-maintenance-qa-evidence",
    }
    base = f"{API_ORIGIN}/repos/{REPOSITORY}"
    workflow = _api_json(api_opener, f"{base}/actions/workflows/{workflow_path.split('/')[-1]}", headers)
    if workflow.get("path") != workflow_path or workflow.get("name") != workflow_name or workflow.get("state") != "active":
        _fail()
    run = _api_json(api_opener, f"{base}/actions/runs/{run_id_value}", headers)
    if run.get("run_attempt") != attempt_value:
        _fail()
    artifact_listing = _api_json(
        api_opener,
        f"{base}/actions/runs/{run_id_value}/artifacts?per_page=100&page=1",
        headers,
    )
    artifact_name = f"{artifact_prefix}-{run_id_value}-{attempt_value}"
    rows = artifact_listing.get("artifacts")
    if not isinstance(rows, list):
        _fail()
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == artifact_name]
    if len(matches) != 1:
        _fail()
    artifact_id = _positive_id(matches[0].get("id"))
    metadata = _api_json(api_opener, f"{base}/actions/artifacts/{artifact_id}", headers)
    archive_url = f"{API_ORIGIN}/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip"
    archive_bytes = _read_response(
        artifact_opener,
        Request(archive_url, headers=headers),
        MAX_ARCHIVE_BYTES,
    )
    _validate_artifact_binding(
        workflow=workflow,
        run=run,
        listing=artifact_listing,
        metadata=metadata,
        archive_bytes=archive_bytes,
        run_id_text=run_id,
        attempt_text=attempt,
        expected_runner_sha=runner_sha,
        expected_app_target_sha=app_target_sha,
        workflow_path=workflow_path,
        workflow_name=workflow_name,
        artifact_prefix=artifact_prefix,
        member_name=member_name,
        report_validator=report_validator,
    )


def verify_qa_evidence(
    *,
    runner_sha: str,
    app_target_sha: str,
    live_launch_run_id: str,
    live_launch_attempt: str,
    live_user_qa_run_id: str,
    live_user_qa_attempt: str,
    token: str,
) -> None:
    if live_user_qa_attempt != "1":
        _fail()
    _verify_one_qa(
        token=token,
        runner_sha=runner_sha,
        app_target_sha=app_target_sha,
        run_id=live_launch_run_id,
        attempt=live_launch_attempt,
        workflow_path=LIVE_LAUNCH_PATH,
        workflow_name=LIVE_LAUNCH_NAME,
        artifact_prefix="platform-live-launch-sanitized",
        member_name="live-launch-sanitized.json",
        report_validator=validate_live_launch_report,
    )
    _verify_one_qa(
        token=token,
        runner_sha=runner_sha,
        app_target_sha=app_target_sha,
        run_id=live_user_qa_run_id,
        attempt=live_user_qa_attempt,
        workflow_path=LIVE_USER_QA_PATH,
        workflow_name=LIVE_USER_QA_NAME,
        artifact_prefix="platform-live-user-qa",
        member_name="live-user-qa-report.json",
        report_validator=validate_live_user_qa_report,
    )


def main(argv: list[str] | None = None) -> int:
    actual_argv = sys.argv[1:] if argv is None else argv
    if actual_argv[:1] == ["snapshot-protected-backup-manifest"]:
        snapshot_parser = argparse.ArgumentParser(description=__doc__)
        snapshot_parser.add_argument("mode")
        snapshot_parser.parse_args(actual_argv)
        try:
            print(protected_backup_manifest_snapshot_digest())
        except Exception:
            print("MAINTENANCE_RETENTION_MANIFEST schema=1 status=unavailable", file=sys.stderr)
            return 1
        return 0
    if actual_argv[:1] == ["validate-protected-retention-report"]:
        retention_parser = argparse.ArgumentParser(description=__doc__)
        retention_parser.add_argument("mode")
        retention_parser.add_argument("report_path")
        retention_parser.add_argument("manifest_digest")
        args = retention_parser.parse_args(actual_argv)
        try:
            report = _read_private_maintenance_report(args.report_path)
            validate_protected_retention_report(report, args.manifest_digest)
        except Exception:
            print("MAINTENANCE_RETENTION_PROOF schema=1 status=unavailable", file=sys.stderr)
            return 1
        print("MAINTENANCE_RETENTION_PROOF schema=1 status=verified")
        return 0
    if actual_argv[:1] == ["validate-maintenance-unit"]:
        unit_parser = argparse.ArgumentParser(description=__doc__)
        unit_parser.add_argument("mode")
        unit_parser.add_argument("source_path")
        unit_parser.add_argument("installed_path")
        args = unit_parser.parse_args(actual_argv)
        try:
            properties = sys.stdin.read(MAX_UNIT_BYTES + 1)
            validate_loaded_maintenance_unit(
                args.source_path, args.installed_path, properties
            )
        except Exception:
            print("MAINTENANCE_UNIT_CONTRACT schema=1 status=unavailable", file=sys.stderr)
            return 1
        return 0
    if actual_argv[:1] == ["run-retention-preview"]:
        preview_parser = argparse.ArgumentParser(description=__doc__)
        preview_parser.add_argument("mode")
        preview_parser.add_argument("source_path")
        preview_parser.add_argument("installed_path")
        args = preview_parser.parse_args(actual_argv)
        try:
            properties = sys.stdin.read(MAX_UNIT_BYTES + 1)
            if len(properties.encode("utf-8")) > MAX_UNIT_BYTES:
                _fail()
            return run_validated_maintenance_preview(
                args.source_path, args.installed_path, properties
            )
        except Exception:
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-sha", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--live-launch-run-id", required=True)
    parser.add_argument("--live-launch-attempt", required=True)
    parser.add_argument("--live-user-qa-run-id", required=True)
    parser.add_argument("--live-user-qa-attempt", required=True)
    args = parser.parse_args(actual_argv)
    try:
        verify_qa_evidence(
            runner_sha=args.runner_sha,
            app_target_sha=args.target_sha,
            live_launch_run_id=args.live_launch_run_id,
            live_launch_attempt=args.live_launch_attempt,
            live_user_qa_run_id=args.live_user_qa_run_id,
            live_user_qa_attempt=args.live_user_qa_attempt,
            token=os.environ.get("GH_TOKEN", ""),
        )
    except Exception:
        print("MAINTENANCE_QA_EVIDENCE schema=1 status=unavailable", file=sys.stderr)
        return 1
    print("MAINTENANCE_QA_EVIDENCE schema=1 status=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
