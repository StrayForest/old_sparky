#!/usr/bin/env python3
"""Capture private bounded evidence for the owned maintenance unit.

The caller must hold the canonical platform release lock. Capture never
changes systemd or release state. Reset is a separate capability and accepts
only the exact immutable evidence digest emitted by capture.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any


UNIT = "deadlock-maintenance.service"
TIMER = "deadlock-maintenance.timer"
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
MAX_RECEIPT_BYTES = 64 * 1024
MAX_JOURNAL_BYTES = 128 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40,64}$")
UNIT_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "unknown"}
SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|cookie|api[_-]?key)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class EvidenceError(ValueError):
    """A closed, non-sensitive evidence operation failure."""


def _run(argv: list[str], *, maximum: int = MAX_JOURNAL_BYTES) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceError("fixed evidence command failed") from exc
    if len(result.stdout) > maximum:
        raise EvidenceError("fixed evidence command output exceeded its bound")
    return result


def _strict_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, EvidenceError) as exc:
        raise EvidenceError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is invalid")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceError("evidence JSON has duplicate keys")
        value[key] = item
    return value


def _read_receipt(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise EvidenceError("release receipt is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_size > MAX_RECEIPT_BYTES
    ):
        raise EvidenceError("release receipt metadata is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise EvidenceError("release receipt cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino or opened.st_nlink != 1:
            raise EvidenceError("release receipt changed while opening")
        chunks = bytearray()
        while len(chunks) <= MAX_RECEIPT_BYTES:
            chunk = os.read(descriptor, MAX_RECEIPT_BYTES + 1 - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        if after.st_dev != opened.st_dev or after.st_ino != opened.st_ino or after.st_size != opened.st_size or len(chunks) != opened.st_size:
            raise EvidenceError("release receipt changed while reading")
        raw = bytes(chunks)
    except OSError as exc:
        raise EvidenceError("release receipt cannot be read") from exc
    finally:
        os.close(descriptor)
    return _strict_json(raw, label="release receipt"), hashlib.sha256(raw).hexdigest()


def _pointer(app_dir: Path, name: str) -> str | None:
    path = app_dir / name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EvidenceError("release pointer is unavailable") from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise EvidenceError("release pointer is not a symlink")
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_nlink != 1:
        raise EvidenceError("release pointer metadata is unsafe")
    try:
        target = path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError("release pointer target is unavailable") from exc
    releases = (app_dir / "releases").resolve(strict=True)
    if target.parent != releases or not target.is_dir():
        raise EvidenceError("release pointer target is outside releases")
    return target.name


def _systemd_properties(unit: str) -> dict[str, str]:
    fields = (
        "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus",
        "MainPID", "ExecMainPID", "ControlGroup", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
        "StateChangeTimestamp", "UnitFileState",
    )
    result = _run(
        [SYSTEMCTL, "show", unit, "--no-pager", *(f"--property={field}" for field in fields)],
        maximum=16 * 1024,
    )
    if result.returncode != 0:
        raise EvidenceError("systemd properties could not be read")
    values: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "strict").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in fields or key in values or "\x00" in value:
            raise EvidenceError("systemd properties are malformed")
        values[key] = value[:512]
    if set(values) != set(fields):
        raise EvidenceError("systemd properties are incomplete")
    if values["ActiveState"] not in UNIT_STATES:
        values["ActiveState"] = "unknown"
    return values


def _queued_jobs() -> list[dict[str, str]]:
    result = _run([SYSTEMCTL, "list-jobs", "--no-pager", "--all"], maximum=32 * 1024)
    if result.returncode != 0:
        raise EvidenceError("systemd jobs could not be read")
    lines = result.stdout.decode("utf-8", "strict").splitlines()
    if not lines or lines[0].split() != ["JOB", "UNIT", "TYPE", "STATE"]:
        raise EvidenceError("systemd jobs output is malformed")
    jobs: list[dict[str, str]] = []
    listed_count: int | None = None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) == 3 and fields[1] in {"job", "jobs"} and fields[2] == "listed." and fields[0].isdecimal():
            if listed_count is not None:
                raise EvidenceError("systemd jobs summary is malformed")
            listed_count = int(fields[0])
            continue
        if len(fields) != 4 or not fields[0].isdecimal() or not re.fullmatch(r"[A-Za-z0-9_.@:-]{1,255}", fields[1]) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fields[2]) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fields[3]):
            raise EvidenceError("systemd job row is malformed")
        jobs.append({"unit": fields[1], "job_type": fields[2], "job_state": fields[3]})
    if listed_count is None or listed_count != len(jobs):
        raise EvidenceError("systemd jobs summary does not match rows")
    return jobs


def _validate_transaction_context(
    app_dir: Path, current: str | None, previous: str | None,
    operation: dict[str, Any] | None, systemd: dict[str, Any] | None,
) -> str:
    if operation is None or systemd is None:
        raise EvidenceError("release transaction receipts are required")
    operation_keys = {
        "operation_id", "version", "operation", "phase", "app_dir", "current_before",
        "previous_before", "candidate_release", "shared_venv", "peer", "snapshot",
        "transition", "shared_before", "peer_before", "current_before_identity",
        "previous_before_identity", "candidate_identity", "remove_env_on_recovery",
        "service_state_before", "service_enabled_before", "quiesced_services",
        "timer_active_before", "timer_enabled_before", "systemd_state_before",
        "profile_access_cache_purge_proof",
    }
    systemd_keys = {
        "schema", "operation", "operation_id", "app_dir", "current_before",
        "previous_before", "current_before_identity", "previous_before_identity",
        "helper_digests", "units",
    }
    identity_keys = {"slug", "dev", "ino", "source_sha"}
    slug = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
    def valid_identity(value: Any, expected_slug: str | None) -> bool:
        return (
            isinstance(value, dict) and set(value) == identity_keys
            and isinstance(value.get("slug"), str) and slug.fullmatch(value["slug"]) is not None
            and (expected_slug is None or value["slug"] == expected_slug)
            and type(value.get("dev")) is int and value["dev"] >= 0
            and type(value.get("ino")) is int and value["ino"] > 0
            and isinstance(value.get("source_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", value["source_sha"]) is not None
        )
    def release_name(value: Any, *, nullable: bool) -> str | None:
        if value is None and nullable:
            return None
        if not isinstance(value, str):
            raise EvidenceError("release transaction pointer is invalid")
        path = Path(value)
        if not path.is_absolute() or path.parent != app_dir / "releases" or slug.fullmatch(path.name) is None:
            raise EvidenceError("release transaction path is invalid")
        return path.name

    if (
        set(operation) != operation_keys
        or set(systemd) != systemd_keys
        or operation.get("version") != 2
        or operation.get("phase") != "recovery-restored"
        or operation.get("operation") not in {"install", "rollback"}
        or not isinstance(operation.get("operation_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", operation["operation_id"]) is None
        or operation.get("app_dir") != str(app_dir)
        or systemd.get("schema") != 1
        or systemd.get("operation") != operation.get("operation")
        or systemd.get("operation_id") != operation.get("operation_id")
        or systemd.get("app_dir") != str(app_dir)
    ):
        raise EvidenceError("release transaction receipt schema is not the expected restored state")
    current_before = release_name(operation.get("current_before"), nullable=True)
    previous_before = release_name(operation.get("previous_before"), nullable=True)
    candidate = release_name(operation.get("candidate_release"), nullable=False)
    if (
        current != current_before or previous != previous_before
        or systemd.get("current_before") != operation.get("current_before")
        or systemd.get("previous_before") != operation.get("previous_before")
        or systemd.get("current_before_identity") != operation.get("current_before_identity")
        or systemd.get("previous_before_identity") != operation.get("previous_before_identity")
        or (operation["operation"] == "rollback" and candidate != current_before)
        or (operation["operation"] == "install" and candidate in {current_before, previous_before})
        or not valid_identity(operation.get("candidate_identity"), candidate)
        or not (operation.get("current_before_identity") is None if current_before is None else valid_identity(operation.get("current_before_identity"), current_before))
        or not (operation.get("previous_before_identity") is None if previous_before is None else valid_identity(operation.get("previous_before_identity"), previous_before))
    ):
        raise EvidenceError("release transaction receipt does not match restored pointers")
    return operation["candidate_identity"]["source_sha"]


def _cgroup_members(control_group: str) -> list[int]:
    if control_group == "":
        return []
    if not control_group or control_group == "/" or ".." in Path(control_group).parts:
        raise EvidenceError("maintenance cgroup identity is invalid")
    root = Path("/sys/fs/cgroup")
    directory = root / control_group.lstrip("/")
    try:
        directory.resolve(strict=True).relative_to(root.resolve(strict=True))
        raw = (directory / "cgroup.procs").read_text(encoding="ascii")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise EvidenceError("maintenance cgroup state is unavailable") from exc
    if len(raw) > 16 * 1024:
        raise EvidenceError("maintenance cgroup state exceeded its bound")
    members: list[int] = []
    for item in raw.splitlines():
        if not item.isdecimal() or len(item) > 10:
            raise EvidenceError("maintenance cgroup state is malformed")
        members.append(int(item))
    if len(members) > 256:
        raise EvidenceError("maintenance cgroup member count exceeded its bound")
    return members


def _journal() -> list[dict[str, Any]]:
    result = _run(
        [JOURNALCTL, "--unit", UNIT, "--lines=100", "--output=json", "--no-pager", "--since=-24 hours"],
        maximum=MAX_JOURNAL_BYTES,
    )
    if result.returncode not in (0, 1):
        raise EvidenceError("maintenance journal could not be read")
    entries: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        row = _strict_json(line, label="maintenance journal entry")
        # Journal messages are retained only in the private evidence artifact.
        # Never write them to stdout, Actions logs, or a committed file.
        entry = {
            "realtime_timestamp": str(row.get("__REALTIME_TIMESTAMP", ""))[:24],
            "priority": str(row.get("PRIORITY", ""))[:2],
            "message": BEARER_PATTERN.sub("Bearer [REDACTED]", SECRET_PATTERN.sub(r"\1\2[REDACTED]", str(row.get("MESSAGE", ""))))[:4096],
        }
        entries.append(entry)
    if len(entries) > 100:
        raise EvidenceError("maintenance journal row count exceeded its bound")
    return entries


def _atomic_private_file(path: Path, raw: bytes) -> None:
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise EvidenceError("maintenance evidence exceeded its bound")
    if path.exists() or path.is_symlink():
        raise EvidenceError("maintenance evidence destination already exists")
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != 0
        or parent.st_gid != 0
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise EvidenceError("maintenance evidence directory is unsafe")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _durable_evidence_path(
    app_dir: Path, source_sha: str, capture_run_id: str, capture_run_attempt: str,
    *, create: bool,
) -> Path:
    if not SOURCE_SHA.fullmatch(source_sha):
        raise EvidenceError("maintenance evidence source identity is invalid")
    run_id_pattern = re.compile(r"^[1-9][0-9]{0,31}$")
    if not run_id_pattern.fullmatch(capture_run_id) or not run_id_pattern.fullmatch(capture_run_attempt):
        raise EvidenceError("maintenance capture run identity is invalid")
    app_dir = app_dir.resolve(strict=True)
    shared = app_dir / "shared"
    recovery = shared / ".release-recovery"
    for directory, expected_mode in ((shared, None), (recovery, 0o755)):
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise EvidenceError("maintenance evidence directory is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or (expected_mode is not None and stat.S_IMODE(metadata.st_mode) != expected_mode)
        ):
            raise EvidenceError("maintenance evidence directory is unsafe")
    directory = recovery / "maintenance-failure-evidence"
    if create and not os.path.lexists(directory):
        try:
            directory.mkdir(mode=0o700)
            os.chown(directory, 0, 0)
            os.chmod(directory, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise EvidenceError("maintenance evidence directory cannot be created") from exc
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise EvidenceError("maintenance evidence directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise EvidenceError("maintenance evidence directory is unsafe")
    return directory / f"{source_sha}-{capture_run_id}-{capture_run_attempt}.json"


def capture(
    *, app_dir: Path, source_sha: str, bundle_sha: str, output: Path,
    capture_run_id: str, capture_run_attempt: str,
    security_run_id: str, security_run_attempt: str,
) -> tuple[str, dict[str, Any]]:
    if os.geteuid() != 0:
        raise EvidenceError("maintenance evidence requires root")
    if not SOURCE_SHA.fullmatch(source_sha) or not HEX64.fullmatch(bundle_sha):
        raise EvidenceError("maintenance source identity is invalid")
    run_id_pattern = re.compile(r"^[1-9][0-9]{0,31}$")
    if any(not run_id_pattern.fullmatch(item) for item in (
        capture_run_id, capture_run_attempt, security_run_id, security_run_attempt,
    )):
        raise EvidenceError("maintenance workflow run identity is invalid")
    app_dir = app_dir.resolve(strict=True)
    current = _pointer(app_dir, "current")
    previous = _pointer(app_dir, "previous")
    operation, operation_sha = _read_receipt(app_dir / "shared/.release-operation.json")
    systemd, systemd_sha = _read_receipt(app_dir / "shared/.release-systemd-state.json")
    candidate_source_sha = _validate_transaction_context(app_dir, current, previous, operation, systemd)
    try:
        host_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise EvidenceError("production host identity is unavailable") from exc
    if not re.fullmatch(r"[0-9a-f]{32}", host_id):
        raise EvidenceError("production host identity is invalid")
    unit = _systemd_properties(UNIT)
    timer = _systemd_properties(TIMER)
    jobs = _queued_jobs()
    pids = _cgroup_members(unit["ControlGroup"])
    journal = _journal()
    if unit["ActiveState"] != "failed":
        raise EvidenceError("maintenance service is not in the observed failed state")
    payload = {
        "schema": 1,
        "kind": "maintenance_failure_evidence",
        "source_sha": source_sha,
        "bundle_sha256": bundle_sha,
        "capture_run_id": capture_run_id,
        "capture_run_attempt": capture_run_attempt,
        "security_run_id": security_run_id,
        "security_run_attempt": security_run_attempt,
        "host_id_sha256": hashlib.sha256(host_id.encode("ascii")).hexdigest(),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_dir": str(app_dir),
        "pointers": {"current": current, "previous": previous},
        "receipts": {
            "operation_sha256": operation_sha,
            "systemd_sha256": systemd_sha,
            "operation_id": operation.get("operation_id") if operation else None,
            "operation": operation.get("operation") if operation else None,
            "phase": operation.get("phase") if operation else None,
            "candidate_release": operation.get("candidate_release") if operation else None,
            "candidate_identity": operation.get("candidate_identity") if operation else None,
            "peer": operation.get("peer") if operation else None,
            "operation_source_sha": candidate_source_sha,
            "companion_present": True,
        },
        "unit": unit,
        "timer": timer,
        "queued_jobs": jobs,
        "cgroup_pids": pids,
        "journal": journal,
    }
    raw = (json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    expected_output = _durable_evidence_path(
        app_dir, source_sha, capture_run_id, capture_run_attempt, create=True,
    )
    if output != expected_output:
        raise EvidenceError("maintenance evidence destination is not source/run bound")
    _atomic_private_file(expected_output, raw)
    return hashlib.sha256(raw).hexdigest(), payload


def _validate_evidence(
    path: Path, expected_sha: str, *, source_sha: str, bundle_sha: str,
    capture_run_id: str, capture_run_attempt: str,
    security_run_id: str, security_run_attempt: str,
) -> dict[str, Any]:
    if not HEX64.fullmatch(expected_sha):
        raise EvidenceError("maintenance evidence digest is invalid")
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > MAX_EVIDENCE_BYTES
    ):
        raise EvidenceError("maintenance evidence metadata is unsafe")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise EvidenceError("maintenance evidence digest does not match")
    value = _strict_json(raw, label="maintenance evidence")
    expected_keys = {
        "schema", "kind", "source_sha", "bundle_sha256", "capture_run_id",
        "capture_run_attempt", "security_run_id", "security_run_attempt",
        "host_id_sha256", "captured_at_utc", "app_dir", "pointers", "receipts",
        "unit", "timer", "queued_jobs", "cgroup_pids", "journal",
    }
    run_id_pattern = re.compile(r"^[1-9][0-9]{0,31}$")
    try:
        host_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise EvidenceError("production host identity is unavailable") from exc
    if (
        set(value) != expected_keys
        or value.get("schema") != 1
        or value.get("kind") != "maintenance_failure_evidence"
        or value.get("source_sha") != source_sha
        or value.get("bundle_sha256") != bundle_sha
        or not isinstance(value.get("capture_run_id"), str)
        or not run_id_pattern.fullmatch(value["capture_run_id"])
        or not isinstance(value.get("capture_run_attempt"), str)
        or not run_id_pattern.fullmatch(value["capture_run_attempt"])
        or not isinstance(value.get("security_run_id"), str)
        or not run_id_pattern.fullmatch(value["security_run_id"])
        or not isinstance(value.get("security_run_attempt"), str)
        or not run_id_pattern.fullmatch(value["security_run_attempt"])
        or value.get("capture_run_id") != capture_run_id
        or value.get("capture_run_attempt") != capture_run_attempt
        or value.get("security_run_id") != security_run_id
        or value.get("security_run_attempt") != security_run_attempt
        or value.get("host_id_sha256") != hashlib.sha256(host_id.encode("ascii")).hexdigest()
        or value.get("app_dir") != "/opt/oldsparky/platform"
        or not isinstance(value.get("pointers"), dict)
        or set(value["pointers"]) != {"current", "previous"}
        or not isinstance(value.get("receipts"), dict)
        or set(value["receipts"]) != {
            "operation_sha256", "systemd_sha256", "operation_id", "operation", "phase",
            "candidate_release", "candidate_identity", "peer", "operation_source_sha",
            "companion_present",
        }
        or not isinstance(value.get("unit"), dict)
        or not isinstance(value.get("timer"), dict)
        or not isinstance(value.get("journal"), list)
        or set(value.get("unit", {})) != {
            "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus",
            "MainPID", "ExecMainPID", "ControlGroup", "ExecMainStartTimestamp",
            "ExecMainExitTimestamp", "StateChangeTimestamp", "UnitFileState",
        }
        or set(value.get("timer", {})) != {
            "ActiveState", "SubState", "Result", "ExecMainCode", "ExecMainStatus",
            "MainPID", "ExecMainPID", "ControlGroup", "ExecMainStartTimestamp",
            "ExecMainExitTimestamp", "StateChangeTimestamp", "UnitFileState",
        }
        or any(item is not None and (not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}", item) is None) for item in value.get("pointers", {}).values())
        or any(not HEX64.fullmatch(str(value.get("receipts", {}).get(key, ""))) for key in ("operation_sha256", "systemd_sha256"))
        or value.get("receipts", {}).get("companion_present") is not True
        or value.get("receipts", {}).get("operation") not in {"install", "rollback"}
        or value.get("receipts", {}).get("phase") != "recovery-restored"
        or not isinstance(value.get("receipts", {}).get("operation_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["receipts"]["operation_id"]) is None
        or not isinstance(value.get("receipts", {}).get("candidate_release"), str)
        or not isinstance(value.get("receipts", {}).get("candidate_identity"), dict)
        or value.get("receipts", {}).get("operation_source_sha") != value.get("receipts", {}).get("candidate_identity", {}).get("source_sha")
        or not isinstance(value.get("receipts", {}).get("peer"), str)
        or set(value["receipts"]["candidate_identity"]) != {"slug", "dev", "ino", "source_sha"}
        or value["receipts"]["candidate_identity"].get("slug") != Path(value["receipts"]["candidate_release"]).name
        or type(value["receipts"]["candidate_identity"].get("dev")) is not int
        or value["receipts"]["candidate_identity"].get("dev") < 0
        or type(value["receipts"]["candidate_identity"].get("ino")) is not int
        or value["receipts"]["candidate_identity"].get("ino") <= 0
        or not isinstance(value["receipts"]["operation_source_sha"], str)
        or re.fullmatch(r"[0-9a-f]{40}", value["receipts"]["operation_source_sha"]) is None
        or not isinstance(value["receipts"]["candidate_release"], str)
        or Path(value["receipts"]["candidate_release"]).parent != Path(value["app_dir"]) / "releases"
        or (value["receipts"]["operation"] == "rollback" and value["pointers"]["current"] != value["receipts"]["candidate_identity"]["slug"])
        or len(value["journal"]) > 100
        or any(not isinstance(row, dict) or set(row) != {"realtime_timestamp", "priority", "message"} or not all(isinstance(row[key], str) for key in row) or len(row["message"]) > 4096 for row in value["journal"])
        or value.get("unit", {}).get("ActiveState") != "failed"
        or not isinstance(value.get("queued_jobs"), list)
        or len(value["queued_jobs"]) > 256
        or any(not isinstance(row, dict) or set(row) != {"unit", "job_type", "job_state"} or any(not isinstance(row[key], str) or len(row[key]) > 255 for key in row) for row in value["queued_jobs"])
        or not isinstance(value.get("cgroup_pids"), list)
        or len(value["cgroup_pids"]) > 256
        or any(type(pid) is not int or pid < 0 for pid in value["cgroup_pids"])
    ):
        raise EvidenceError("maintenance evidence does not authorize reset")
    return value


def reset_failed(
    *, app_dir: Path, source_sha: str, bundle_sha: str, evidence: Path, evidence_sha: str,
    capture_run_id: str, capture_run_attempt: str,
    security_run_id: str, security_run_attempt: str,
) -> None:
    if os.geteuid() != 0:
        raise EvidenceError("maintenance reset requires root")
    expected_evidence = _durable_evidence_path(
        app_dir, source_sha, capture_run_id, capture_run_attempt, create=False,
    )
    if evidence != expected_evidence:
        raise EvidenceError("maintenance evidence path is not source/run bound")
    evidence_value = _validate_evidence(
        expected_evidence, evidence_sha, source_sha=source_sha, bundle_sha=bundle_sha,
        capture_run_id=capture_run_id, capture_run_attempt=capture_run_attempt,
        security_run_id=security_run_id, security_run_attempt=security_run_attempt,
    )
    app_dir = app_dir.resolve(strict=True)
    if _pointer(app_dir, "current") != evidence_value["pointers"].get("current") or _pointer(app_dir, "previous") != evidence_value["pointers"].get("previous"):
        raise EvidenceError("release pointers changed after evidence capture")
    current_operation, current_operation_sha = _read_receipt(app_dir / "shared/.release-operation.json")
    current_systemd, current_systemd_sha = _read_receipt(app_dir / "shared/.release-systemd-state.json")
    receipts = evidence_value["receipts"]
    if current_operation_sha != receipts.get("operation_sha256") or current_systemd_sha != receipts.get("systemd_sha256"):
        raise EvidenceError("release receipts changed after evidence capture")
    if (current_operation is None) != (current_systemd is None):
        raise EvidenceError("release transaction receipt pair changed")
    if current_operation and current_systemd and current_operation.get("operation_id") != current_systemd.get("operation_id"):
        raise EvidenceError("release transaction receipt identity changed")
    unit = _systemd_properties(UNIT)
    timer = _systemd_properties(TIMER)
    jobs = _queued_jobs()
    pids = _cgroup_members(unit["ControlGroup"])
    if (
        unit["ActiveState"] != "failed"
        or unit["MainPID"] != "0"
        or unit != evidence_value["unit"]
        or timer["ActiveState"] != "inactive"
        or timer["UnitFileState"] not in {"disabled", "masked"}
        or timer != evidence_value["timer"]
        or jobs
        or pids
    ):
        raise EvidenceError("maintenance reset preconditions changed")
    result = _run([SYSTEMCTL, "reset-failed", UNIT], maximum=4096)
    if result.returncode != 0:
        raise EvidenceError("fixed maintenance reset failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private maintenance failure evidence and guarded reset")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("capture_maintenance_failure", "reset_maintenance_failed"):
        command = commands.add_parser(action)
        command.add_argument("--app-dir", type=Path, required=True)
        command.add_argument("--source-sha", required=True)
        command.add_argument("--bundle-sha", required=True)
        if action == "capture_maintenance_failure":
            command.add_argument("--capture-run-id", required=True)
            command.add_argument("--capture-run-attempt", required=True)
            command.add_argument("--security-run-id", required=True)
            command.add_argument("--security-run-attempt", required=True)
        else:
            command.add_argument("--evidence-sha256", required=True)
            command.add_argument("--capture-run-id", required=True)
            command.add_argument("--capture-run-attempt", required=True)
            command.add_argument("--security-run-id", required=True)
            command.add_argument("--security-run-attempt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.action == "capture_maintenance_failure":
            output = _durable_evidence_path(
                args.app_dir, args.source_sha, args.capture_run_id,
                args.capture_run_attempt, create=True,
            )
            digest, _ = capture(
                app_dir=args.app_dir, source_sha=args.source_sha,
                bundle_sha=args.bundle_sha, output=output,
                capture_run_id=args.capture_run_id,
                capture_run_attempt=args.capture_run_attempt,
                security_run_id=args.security_run_id,
                security_run_attempt=args.security_run_attempt,
            )
            print("MAINTENANCE_EVIDENCE schema=1 status=captured private=true")
            return 0
        reset_failed(
            app_dir=args.app_dir, source_sha=args.source_sha, bundle_sha=args.bundle_sha,
            evidence=_durable_evidence_path(
                args.app_dir, args.source_sha, args.capture_run_id,
                args.capture_run_attempt, create=False,
            ), evidence_sha=args.evidence_sha256,
            capture_run_id=args.capture_run_id,
            capture_run_attempt=args.capture_run_attempt,
            security_run_id=args.security_run_id,
            security_run_attempt=args.security_run_attempt,
        )
        print("MAINTENANCE_RESET schema=1 status=complete unit=deadlock-maintenance.service")
        return 0
    except (EvidenceError, OSError, UnicodeError) as exc:
        print(f"MAINTENANCE_EVIDENCE schema=1 status=failed error_class={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
