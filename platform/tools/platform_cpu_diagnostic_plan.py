#!/usr/bin/env python3
"""Prepare or expire a paired, root-controlled CPU diagnostic plan.

The helper never signals a service, changes configuration, starts workload,
or restarts a unit.  It publishes a short-lived API/web plan only after both
live service identities and the active release are bound to the request.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import pwd
import grp
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
import uuid


PLATFORM_ROOT = Path("/opt/oldsparky/platform")
PLAN_DIRECTORY = Path("/run/oldsparky-platform")
PLAN_NAMES = {
    "api": "performance-diagnostic-plan.api.json",
    "web": "performance-diagnostic-plan.web.json",
}
MAX_PLAN_BYTES = 4096
MAX_REQUEST_BYTES = 64 * 1024
MAX_TOTAL_WINDOW_MS = 145_000
PLAN_EXPIRY_GRACE_MS = 15_000
MAX_JOURNAL_BYTES = 1_048_576
MAX_JOURNAL_LINE_BYTES = 16_384
MAX_JOURNAL_ROWS = 640
JOURNAL_TIMEOUT_SECONDS = 3
CAPACITY_JOURNAL_TIMEOUT_SECONDS = 12
CAPACITY_JOURNAL_BYTES = 8 * 1024 * 1024
CAPACITY_JOURNAL_LINE_BYTES = 64 * 1024
CAPACITY_JOURNAL_ROWS = 50_000
CAPACITY_NGINX_BYTES = 32 * 1024 * 1024
CAPACITY_NGINX_ROWS = 50_000
CAPACITY_NGINX_FILES = 8
CAPACITY_REQUEST_SAMPLE_LIMIT = 1_000
CAPACITY_CAPTURE_DRAIN_MS = 15_000
NGINX_ACCESS_LOG = Path("/var/log/nginx/platform-access.log")
MAX_SAMPLES_PROFILE = 1200
MEMORY_SAMPLE_COUNT = 5
SSR_CAPACITY_MEMORY_MAX_BYTES = 1_073_741_824
SSR_CAPACITY_ABORT_FLOOR_BYTES = 768 * 1024 * 1024
SSR_CAPACITY_WATCH_INTERVAL_SECONDS = 0.25
MIN_PLAN_LEAD_MS = 45_000
MAX_PLAN_LEAD_MS = 60_000
WORKLOAD = "authenticated_workspace_read_pair_v1"
CAPACITY_WORKLOAD = "authenticated_workspace_read_capacity_v1"
HTML_CAPACITY_WORKLOAD = "authenticated_html_ssr_capacity_v1"
HTML_CAPACITY_120S_WORKLOAD = "authenticated_html_ssr_capacity_120s_v1"
HTML_CPU_PROFILE_20S_WORKLOAD = "authenticated_html_ssr_cpu_profile_20s_v1"
SUPPORTED_WORKLOADS = frozenset({
    WORKLOAD,
    CAPACITY_WORKLOAD,
    HTML_CAPACITY_WORKLOAD,
    HTML_CAPACITY_120S_WORKLOAD,
    HTML_CPU_PROFILE_20S_WORKLOAD,
})
MEMORY_WATCH_WORKLOADS = frozenset({
    CAPACITY_WORKLOAD,
    HTML_CAPACITY_WORKLOAD,
    HTML_CAPACITY_120S_WORKLOAD,
    HTML_CPU_PROFILE_20S_WORKLOAD,
})
OFF_WINDOW_MS = 20_000
IDLE_WINDOW_MS = 5_000
ON_WINDOW_MS = 20_000
ON_WINDOW_MS_BY_WORKLOAD = {
    workload: ON_WINDOW_MS for workload in SUPPORTED_WORKLOADS
}
ON_WINDOW_MS_BY_WORKLOAD[HTML_CAPACITY_120S_WORKLOAD] = 120_000
PLAN_EXPIRY_GRACE_MS_BY_WORKLOAD = {
    workload: 0 for workload in SUPPORTED_WORKLOADS
}
PLAN_EXPIRY_GRACE_MS_BY_WORKLOAD[HTML_CAPACITY_120S_WORKLOAD] = PLAN_EXPIRY_GRACE_MS
PLAN_EXPIRY_GRACE_MS_BY_WORKLOAD[HTML_CPU_PROFILE_20S_WORKLOAD] = PLAN_EXPIRY_GRACE_MS
RUN_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
INVOCATION_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f-]{36})$", re.IGNORECASE)


class PlanError(RuntimeError):
    """A closed plan refusal; messages contain no process or secret data."""


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii") + b"\n"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanError("request has duplicate fields")
        result[key] = value
    return result


def _read_request(command: str, stream: Any = None) -> argparse.Namespace:
    source = stream if stream is not None else sys.stdin.buffer
    try:
        raw = source.read(MAX_REQUEST_BYTES + 1)
    except OSError as exc:
        raise PlanError("request input is unavailable") from exc
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise PlanError("request input is invalid")
    try:
        payload = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, PlanError) as exc:
        raise PlanError("request input is invalid") from exc
    if not isinstance(payload, dict):
        raise PlanError("request input is noncanonical")
    try:
        canonical = _canonical_json(payload)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PlanError("request input is noncanonical") from exc
    if raw != canonical:
        raise PlanError("request input is noncanonical")
    if command == "prepare-stdin":
        expected = {
            "schema", "run_id", "source_sha", "workload",
            "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms",
        }
        if (set(payload) != expected or type(payload.get("schema")) is not int
                or payload.get("schema") != 1):
            raise PlanError("prepare request fields are invalid")
        return argparse.Namespace(command="prepare", **{
            key: payload[key] for key in expected - {"schema"}
        })
    if command == "cleanup-stdin":
        base_fields = {"schema", "run_id"}
        correlation_fields = {"client_request_ids", "client_cohort_counts"}
        if (set(payload) not in (base_fields, base_fields | correlation_fields)
                or type(payload.get("schema")) is not int or payload.get("schema") != 1
                or not isinstance(payload.get("run_id"), str)
                or RUN_RE.fullmatch(payload["run_id"]) is None):
            raise PlanError("cleanup request fields are invalid")
        request_ids = payload.get("client_request_ids")
        if "client_request_ids" in payload and (
            not isinstance(request_ids, list) or len(request_ids) > 1_000
            or any(not isinstance(value, str) or RUN_RE.fullmatch(value) is None
                   for value in request_ids)
            or len(set(request_ids)) != len(request_ids)
            or request_ids != sorted(request_ids)
        ):
            raise PlanError("cleanup request correlation IDs are invalid")
        cohort_counts = payload.get("client_cohort_counts")
        if "client_request_ids" in payload:
            expected_counts = {
                "submitted_html_count", "valid_response_id_count",
                "missing_response_id_count", "duplicate_response_id_count",
                "malformed_response_id_count", "eligible_response_id_count",
                "retained_sample_count", "sample_scheme", "sample_cap_truncated",
            }
            if (not isinstance(cohort_counts, dict) or set(cohort_counts) != expected_counts
                    or any(type(cohort_counts[key]) is not int or cohort_counts[key] < 0
                           or cohort_counts[key] > 100_000 for key in expected_counts - {
                               "sample_scheme", "sample_cap_truncated",
                           })
                    or cohort_counts.get("sample_scheme") != "sha256-runid-requestid-1of8-v1"
                    or type(cohort_counts.get("sample_cap_truncated")) is not bool
                    or sum(cohort_counts[key] for key in {
                        "valid_response_id_count", "missing_response_id_count",
                        "duplicate_response_id_count", "malformed_response_id_count",
                    })
                    != cohort_counts["submitted_html_count"]
                    or cohort_counts["eligible_response_id_count"] > cohort_counts["valid_response_id_count"]
                    or cohort_counts["retained_sample_count"] != len(request_ids)
                    or len(request_ids) != min(
                        cohort_counts["eligible_response_id_count"], CAPACITY_REQUEST_SAMPLE_LIMIT,
                    )
                    or cohort_counts["sample_cap_truncated"] != (
                        cohort_counts["eligible_response_id_count"] > CAPACITY_REQUEST_SAMPLE_LIMIT
                    )
                    or any(
                        hashlib.sha256(f"{payload['run_id']}:{item}".encode("ascii")).digest()[0] & 7
                        for item in request_ids
                    )):
                raise PlanError("cleanup request cohort counts are invalid")
        elif "client_cohort_counts" in payload:
            raise PlanError("cleanup request cohort counts are unbound")
        return argparse.Namespace(
            command="cleanup", run_id=payload["run_id"],
            client_request_ids=request_ids,
            client_cohort_counts=cohort_counts,
        )
    if command == "memory-watch-stdin":
        if (set(payload) != {"schema", "run_id", "source_sha", "workload"}
                or type(payload.get("schema")) is not int or payload.get("schema") != 1
                or not isinstance(payload.get("workload"), str)
                or payload.get("workload") not in MEMORY_WATCH_WORKLOADS
                or not isinstance(payload.get("run_id"), str)
                or RUN_RE.fullmatch(payload["run_id"]) is None
                or not isinstance(payload.get("source_sha"), str)
                or SHA_RE.fullmatch(payload["source_sha"]) is None):
            raise PlanError("memory watch request fields are invalid")
        return argparse.Namespace(command="memory-watch", **{
            key: payload[key] for key in ("run_id", "source_sha", "workload")
        })
    raise PlanError("unsupported request operation")


def _read_bounded(path: Path, maximum: int) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > maximum):
            raise PlanError("unsafe bounded input")
        chunks = bytearray()
        while len(chunks) <= maximum:
            part = os.read(fd, min(4096, maximum + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        after = os.fstat(fd)
        path_after = os.stat(path, follow_symlinks=False)
        if (len(chunks) != before.st_size or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino or before.st_size != after.st_size
                or path_after.st_dev != before.st_dev or path_after.st_ino != before.st_ino):
            raise PlanError("bounded input changed")
        return bytes(chunks), before
    finally:
        os.close(fd)


def _read_bounded_at(
    directory_fd: int, name: str, maximum: int
) -> tuple[bytes, os.stat_result]:
    """Read a private basename through the already-validated directory fd."""

    if not name or "/" in name or name in {".", ".."}:
        raise PlanError("bounded basename is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != 0
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o440
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            raise PlanError("bounded input metadata is unsafe")
        chunks = bytearray()
        while len(chunks) <= maximum:
            part = os.read(fd, min(4096, maximum + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(chunks) != before.st_size
            or len(chunks) > maximum
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise PlanError("bounded input changed during read")
        return bytes(chunks), before
    finally:
        os.close(fd)


def _release_identity() -> tuple[str, str]:
    release_root = PLATFORM_ROOT / "current"
    if not release_root.is_symlink():
        raise PlanError("current release is unavailable")
    resolved_root = release_root.resolve(strict=True)
    if resolved_root.parent != PLATFORM_ROOT / "releases":
        raise PlanError("current release path is outside the release store")
    raw, _metadata = _read_bounded(resolved_root / "RELEASE.json", 16_384)
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError("release identity is unreadable") from exc
    source_sha = payload.get("source_git_commit") if isinstance(payload, dict) else None
    slug = payload.get("release_slug") if isinstance(payload, dict) else None
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        raise PlanError("release source identity is invalid")
    if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None or slug != resolved_root.name:
        raise PlanError("release slug identity is invalid")
    return source_sha, slug


def _systemd_unit(service: str, *, timeout_seconds: float = 5) -> dict[str, str]:
    unit = f"deadlock-{service}.service"
    try:
        completed = subprocess.run(
            [
                "/usr/bin/systemctl", "show", "--no-pager",
                "--property=ActiveState", "--property=InvocationID",
                "--property=MainPID", "--property=ControlGroup", unit,
            ],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
            text=True,
            encoding="ascii",
            errors="strict",
            env={"LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise PlanError("service identity query failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 4096 or completed.stderr:
        raise PlanError("service identity query failed")
    fields: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise PlanError("service identity fields are invalid")
        fields[key] = value
    if set(fields) != {"ActiveState", "InvocationID", "MainPID", "ControlGroup"}:
        raise PlanError("service identity fields are incomplete")
    expected_group = f"/system.slice/{unit}"
    if fields["ActiveState"] != "active" or fields["ControlGroup"] != expected_group:
        raise PlanError("service is not the expected active unit")
    if INVOCATION_RE.fullmatch(fields["InvocationID"]) is None:
        raise PlanError("service invocation identity is invalid")
    if not fields["MainPID"].isdigit() or int(fields["MainPID"]) <= 1:
        raise PlanError("service main process identity is invalid")
    return fields


def _systemd_memory_max() -> int:
    try:
        completed = subprocess.run(
            ["/usr/bin/systemctl", "show", "--no-pager", "--property=MemoryMax", "deadlock-web.service"],
            check=False, capture_output=True, timeout=3, text=True,
            encoding="ascii", errors="strict",
            env={"LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise PlanError("capacity memory limit query failed") from exc
    value = completed.stdout.strip()
    if (completed.returncode != 0 or completed.stderr or len(completed.stdout) > 128
            or not re.fullmatch(r"MemoryMax=[1-9][0-9]{0,15}", value)):
        raise PlanError("capacity memory limit query failed")
    return int(value.partition("=")[2])


def _process_start_ticks(pid: int) -> int:
    descriptor = os.open(
        f"/proc/{pid}/stat",
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        raw = os.read(descriptor, 4096).decode("ascii")
    finally:
        os.close(descriptor)
    close = raw.rfind(")")
    fields = raw[close + 2 :].split()
    if close < 0 or len(fields) <= 19:
        raise PlanError("process identity is unavailable")
    try:
        value = int(fields[19])
    except ValueError as exc:
        raise PlanError("process identity is invalid") from exc
    if value <= 0:
        raise PlanError("process identity is invalid")
    return value


def _read_process_environ(pid: int, *, maximum: int = 131_072) -> dict[str, bytes]:
    # Values are inspected in-memory only for two fixed identity keys. Parse
    # offsets in the mutable buffer so unrelated bytearray entries are never
    # copied into temporary objects before the buffer is wiped.
    fd = os.open(f"/proc/{pid}/environ", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    raw = bytearray()
    try:
        try:
            while len(raw) <= maximum:
                chunk = os.read(fd, min(8192, maximum + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            os.close(fd)
        if len(raw) > maximum:
            raise PlanError("service process environment exceeds bound")
        wanted = {b"INVOCATION_ID", b"PLATFORM_RUNTIME_SERVICE"}
        selected: dict[str, bytes] = {}
        start = 0
        while start < len(raw):
            end = raw.find(b"\0", start)
            if end < 0:
                end = len(raw)
            separator = raw.find(b"=", start, end)
            if separator >= 0:
                key_bytes = bytes(raw[start:separator])
                if key_bytes in wanted:
                    name = key_bytes.decode("ascii")
                    if name in selected:
                        raise PlanError("service process identity marker is duplicated")
                    selected[name] = bytes(raw[separator + 1:end])
            start = end + 1
        return selected
    finally:
        raw[:] = b"\0" * len(raw)


def _service_targets(service: str, fields: dict[str, str]) -> list[dict[str, object]]:
    cgroup = fields["ControlGroup"]
    cgroup_file = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cgroup.procs"
    try:
        raw_pids = cgroup_file.read_text(encoding="ascii")
    except OSError as exc:
        raise PlanError("service process group is unavailable") from exc
    if len(raw_pids) > 8192:
        raise PlanError("service process group exceeds bound")
    pid_values = raw_pids.splitlines()
    if not 1 <= len(pid_values) <= 32 or any(not value.isdecimal() for value in pid_values):
        raise PlanError("service process group membership is invalid")
    service_uid = pwd.getpwnam(f"oldsparky-{service}").pw_uid
    invocation_id = fields["InvocationID"].lower()
    main_pid = int(fields["MainPID"])
    targets: list[dict[str, object]] = []
    for raw_pid in pid_values:
        pid = int(raw_pid)
        proc_root = Path(f"/proc/{pid}")
        try:
            process_stat_before = proc_root.stat()
            start_ticks_before = _process_start_ticks(pid)
            executable = os.path.basename(os.readlink(proc_root / "exe"))
            environment = _read_process_environ(pid)
            process_invocation = environment.get("INVOCATION_ID", b"").decode("ascii")
            runtime_service = environment.get("PLATFORM_RUNTIME_SERVICE", b"").decode("ascii")
            start_ticks_after = _process_start_ticks(pid)
            process_stat_after = proc_root.stat()
        except (OSError, UnicodeError, PlanError) as exc:
            raise PlanError("service process identity is incomplete") from exc
        if (process_stat_before.st_uid != service_uid
                or process_stat_after.st_uid != service_uid
                or (process_stat_before.st_dev, process_stat_before.st_ino)
                != (process_stat_after.st_dev, process_stat_after.st_ino)
                or start_ticks_before != start_ticks_after
                or process_invocation.lower() != invocation_id):
            raise PlanError("service process ownership or invocation mismatch")
        if runtime_service != service:
            raise PlanError("service process role mismatch")
        executable_matches = (
            executable.startswith("python") if service == "api" else executable == "node"
        )
        if not executable_matches:
            # The API master may be a Gunicorn Python process; all Python
            # processes in the exact unit cgroup are eligible API workers.
            raise PlanError("unexpected executable in service cgroup")
        if service == "api" and pid == main_pid:
            # The standard production unit uses Gunicorn. Its master does not
            # execute request handlers, so only its worker children profile.
            continue
        targets.append({
            "service": service,
            "pid": pid,
            "start_ticks": start_ticks_after,
            "invocation_id": invocation_id,
        })
    if not targets or len(targets) > 16:
        raise PlanError("service worker target set is empty or oversized")
    return sorted(targets, key=lambda row: int(row["pid"]))


def _validate_windows(args: argparse.Namespace, *, now_ms: int) -> None:
    values = (
        args.off_start_ms,
        args.off_end_ms,
        args.on_start_ms,
        args.on_end_ms,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        raise PlanError("diagnostic windows are invalid")
    if not args.off_start_ms < args.off_end_ms <= args.on_start_ms < args.on_end_ms:
        raise PlanError("diagnostic windows are unordered")
    if (
        args.off_end_ms - args.off_start_ms != OFF_WINDOW_MS
        or args.on_start_ms - args.off_end_ms != IDLE_WINDOW_MS
        or args.on_end_ms - args.on_start_ms
        != ON_WINDOW_MS_BY_WORKLOAD.get(args.workload)
    ):
        raise PlanError("diagnostic windows do not match the fixed workload")
    if args.on_end_ms - args.off_start_ms > MAX_TOTAL_WINDOW_MS:
        raise PlanError("diagnostic window exceeds workload cap")
    lead_ms = args.off_start_ms - now_ms
    if not MIN_PLAN_LEAD_MS <= lead_ms <= MAX_PLAN_LEAD_MS:
        raise PlanError("diagnostic plan lead time is outside the fixed preparation window")


def _validate_plan_directory(*, create: bool) -> int:
    if create:
        created = False
        try:
            PLAN_DIRECTORY.mkdir(mode=0o711)
            created = True
        except FileExistsError:
            pass
        if created:
            os.chown(PLAN_DIRECTORY, 0, 0)
            os.chmod(PLAN_DIRECTORY, 0o711)
    metadata = os.lstat(PLAN_DIRECTORY)
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
            or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o711):
        raise PlanError("plan directory metadata is unsafe")
    return os.open(
        PLAN_DIRECTORY,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )


def _publish_plan(
    directory_fd: int, name: str, payload: dict[str, Any], group_name: str
) -> tuple[str, int, int]:
    raw = _canonical_json(payload)
    if len(raw) > MAX_PLAN_BYTES:
        raise PlanError("plan exceeds byte limit")
    gid = grp.getgrnam(group_name).gr_gid
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    temporary_metadata = os.fstat(fd)
    try:
        os.fchown(fd, 0, gid)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PlanError("plan write failed")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o440)
        os.close(fd)
        fd = -1
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
        final_fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            final = os.fstat(final_fd)
            final_raw = os.read(final_fd, MAX_PLAN_BYTES + 1)
            final_after = os.fstat(final_fd)
        finally:
            os.close(final_fd)
        final_path = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (not stat.S_ISREG(final.st_mode) or final.st_uid != 0 or final.st_gid != gid
                or stat.S_IMODE(final.st_mode) != 0o440 or final.st_nlink != 1
                or (final.st_dev, final.st_ino) != (final_after.st_dev, final_after.st_ino)
                or len(final_raw) != len(raw) or final_raw != raw
                or (final_path.st_dev, final_path.st_ino) != (final.st_dev, final.st_ino)):
            raise PlanError("published plan failed exact post-write validation")
    except BaseException:
        try:
            temporary_current = os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
            if (temporary_current.st_dev, temporary_current.st_ino) == (
                temporary_metadata.st_dev, temporary_metadata.st_ino
            ):
                os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        try:
            final_current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            final_fd = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                final_raw = os.read(final_fd, MAX_PLAN_BYTES + 1)
                final_stat = os.fstat(final_fd)
            finally:
                os.close(final_fd)
            if ((final_current.st_dev, final_current.st_ino) == (final_stat.st_dev, final_stat.st_ino)
                    and hashlib.sha256(final_raw).hexdigest() == hashlib.sha256(raw).hexdigest()):
                os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        if fd >= 0:
            os.close(fd)
        raise
    if fd >= 0:
        os.close(fd)
    digest = hashlib.sha256(raw).hexdigest()
    return digest, final.st_dev, final.st_ino


def prepare(args: argparse.Namespace) -> dict[str, object]:
    if os.geteuid() != 0:
        raise PlanError("root identity required")
    if RUN_RE.fullmatch(args.run_id) is None or SHA_RE.fullmatch(args.source_sha) is None:
        raise PlanError("run or source identity is invalid")
    if args.workload not in SUPPORTED_WORKLOADS:
        raise PlanError("unsupported diagnostic workload")
    now_ms = time.time_ns() // 1_000_000
    _validate_windows(args, now_ms=now_ms)
    source_sha, release_slug = _release_identity()
    if source_sha != args.source_sha:
        raise PlanError("current release source does not match the requested source")
    api_unit = _systemd_unit("api")
    web_unit = _systemd_unit("web")
    api_targets = _service_targets("api", api_unit)
    web_targets = _service_targets("web", web_unit)
    # These are the production unit shapes: two Gunicorn request workers and
    # one Next server. Refuse a changed topology rather than silently profiling
    # only a subset of the service.
    if len(api_targets) != 2 or len(web_targets) != 1:
        raise PlanError("service worker topology does not match the reviewed profile")
    directory_fd = _validate_plan_directory(create=True)
    created: list[tuple[str, int, int, str]] = []
    try:
        for name in PLAN_NAMES.values():
            try:
                os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise PlanError("an existing diagnostic plan must expire first")
        # Re-sample source and service/process identities immediately before the
        # pair is written. A restart or worker replacement during preparation
        # invalidates the candidate without publishing either plan.
        if _release_identity() != (source_sha, release_slug):
            raise PlanError("active release changed during plan preparation")
        if _systemd_unit("api") != api_unit or _systemd_unit("web") != web_unit:
            raise PlanError("service invocation changed during plan preparation")
        if _service_targets("api", api_unit) != api_targets or _service_targets("web", web_unit) != web_targets:
            raise PlanError("service process set changed during plan preparation")
        common: dict[str, Any] = {
            "schema": 1,
            "run_id": args.run_id,
            "source_sha": source_sha,
            "release_slug": release_slug,
            "workload": args.workload,
            "off_start_ms": args.off_start_ms,
            "off_end_ms": args.off_end_ms,
            "on_start_ms": args.on_start_ms,
            "on_end_ms": args.on_end_ms,
            "expires_at_ms": _plan_expiry_ms(args.on_end_ms, args.workload),
        }
        for service, targets in (("api", api_targets), ("web", web_targets)):
            payload = {**common, "targets": targets}
            name = PLAN_NAMES[service]
            digest, device, inode = _publish_plan(
                directory_fd, name, payload, f"oldsparky-{service}"
            )
            # _publish_plan returns the identity it validated before return, so
            # rollback ownership is recorded without a second path lookup gap.
            created.append((name, device, inode, digest))
        # Publication is useful only if the exact service cohort still exists
        # and both windows retain their full preparation lead time. A restart
        # during the two-file write must turn the pair into a closed refusal.
        _validate_windows(args, now_ms=time.time_ns() // 1_000_000)
        if (_release_identity() != (source_sha, release_slug)
                or _systemd_unit("api") != api_unit
                or _systemd_unit("web") != web_unit
                or _service_targets("api", api_unit) != api_targets
                or _service_targets("web", web_unit) != web_targets):
            raise PlanError("service identities changed after plan publication")
    except BaseException:
        for name, device, inode, digest in reversed(created):
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                raw = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                try:
                    content = os.read(raw, MAX_PLAN_BYTES + 1)
                finally:
                    os.close(raw)
                if (current.st_dev == device and current.st_ino == inode
                        and hashlib.sha256(content).hexdigest() == digest):
                    os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
        raise
    finally:
        os.close(directory_fd)
    return {
        "status": "prepared",
        "api_target_count": len(api_targets),
        "web_target_count": len(web_targets),
        "release_slug": release_slug,
    }


def _journal_command(plan: dict[str, Any], service: str) -> list[str]:
    start_ms = int(plan["off_start_ms"])
    end_ms = int(plan["on_end_ms"])
    if (service not in PLAN_NAMES or end_ms <= start_ms
            or end_ms - start_ms > MAX_TOTAL_WINDOW_MS):
        raise PlanError("diagnostic journal window is invalid")
    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PlanError("diagnostic journal targets are invalid")
    invocation = targets[0].get("invocation_id") if isinstance(targets[0], dict) else None
    if not isinstance(invocation, str) or INVOCATION_RE.fullmatch(invocation) is None:
        raise PlanError("diagnostic journal invocation is invalid")
    since = f"@{max(0, start_ms - 1000) / 1000:.3f}"
    until = f"@{(end_ms + 5000) / 1000:.3f}"
    unit = f"deadlock-{service}.service"
    return [
        "/usr/bin/journalctl", "--no-pager", "--quiet", "--output=json",
        "--output-fields=MESSAGE,_PID,_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,_SYSTEMD_CGROUP,__REALTIME_TIMESTAMP",
        "--since", since, "--until", until,
        f"--unit={unit}", "--grep=cpu_diagnostic_", "--case-sensitive=yes",
        f"_SYSTEMD_INVOCATION_ID={invocation.lower()}",
    ]


def _plan_on_window_ms(workload: str) -> int | None:
    return ON_WINDOW_MS_BY_WORKLOAD.get(workload)


def _plan_expiry_ms(on_end_ms: int, workload: str) -> int:
    return on_end_ms + PLAN_EXPIRY_GRACE_MS_BY_WORKLOAD[workload]


def _terminate_journal(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _journal_rows(plans: dict[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Read per-service exact-Invocation selections; retain rows only in memory."""

    all_rows: list[dict[str, Any]] = []
    total_bytes = 0
    for service in ("api", "web"):
        plan = plans[service]
        try:
            process = subprocess.Popen(
                _journal_command(plan, service),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env={"LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "TZ": "UTC"},
            )
        except OSError:
            return "journal_failed", all_rows
        assert process.stdout is not None
        fd = process.stdout.fileno()
        selector = selectors.DefaultSelector()
        pending = bytearray()
        deadline = time.monotonic() + JOURNAL_TIMEOUT_SECONDS
        eof = False
        status = "complete"
        try:
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
            while process.poll() is None or not eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timeout"
                    _terminate_journal(process)
                    break
                for event, _mask in selector.select(min(remaining, 0.1)):
                    try:
                        chunk = os.read(event.fd, 32_768)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(event.fd)
                        eof = True
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_JOURNAL_BYTES:
                        status = "byte_cap"
                        _terminate_journal(process)
                        eof = True
                        break
                    pending.extend(chunk)
                    while True:
                        newline = pending.find(b"\n")
                        if newline < 0:
                            if len(pending) > MAX_JOURNAL_LINE_BYTES:
                                status = "line_cap"
                                _terminate_journal(process)
                                eof = True
                            break
                        if newline > MAX_JOURNAL_LINE_BYTES:
                            status = "line_cap"
                            _terminate_journal(process)
                            eof = True
                            break
                        line = bytes(pending[:newline])
                        del pending[: newline + 1]
                        if not line:
                            status = "invalid_event"
                            continue
                        try:
                            record = json.loads(
                                line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
                            )
                        except (UnicodeError, json.JSONDecodeError, PlanError):
                            status = "invalid_event"
                            continue
                        if not isinstance(record, dict):
                            status = "invalid_event"
                            continue
                        # Raw journal values are consumed only for this transient
                        # exact-identity match and never copied to the result.
                        all_rows.append(record)
                        if len(all_rows) > MAX_JOURNAL_ROWS:
                            status = "line_cap"
                            _terminate_journal(process)
                            eof = True
                            break
                    if status in {"byte_cap", "line_cap"}:
                        break
                if status in {"byte_cap", "line_cap"}:
                    break
            if status == "complete" and pending:
                status = "invalid_event"
            try:
                child_status = process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                status = "timeout"
                _terminate_journal(process)
                child_status = process.poll()
            if child_status != 0 and status == "complete":
                status = "journal_failed"
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                _terminate_journal(process)
            pending[:] = b"\0" * len(pending)
        if status != "complete":
            return status, all_rows
    return "complete", all_rows


def _journal_epoch(milliseconds: int) -> str:
    return f"@{milliseconds / 1000:.3f}"


def _bounded_capacity_journal_rows(
    service: str,
    plan: dict[str, Any],
    *,
    since_ms: int,
    until_ms: int,
    markers: tuple[str, ...],
) -> tuple[list[dict[str, Any]], bool]:
    """Read a bounded exact-invocation journal slice for SSR/API joins."""

    targets = plan.get("targets")
    invocation = targets[0].get("invocation_id") if isinstance(targets, list) and targets and isinstance(targets[0], dict) else None
    if (service not in PLAN_NAMES or not isinstance(invocation, str)
            or INVOCATION_RE.fullmatch(invocation) is None
            or type(since_ms) is not int or type(until_ms) is not int
            or not 0 <= since_ms < until_ms
            or until_ms - since_ms > MAX_TOTAL_WINDOW_MS + CAPACITY_CAPTURE_DRAIN_MS + 5_000
            or not markers or any(not re.fullmatch(r"[a-z_]+", marker) for marker in markers)):
        return [], True
    marker_expression = "|".join(markers)
    command = [
        "/usr/bin/journalctl", "--no-pager", "--quiet", "--output=json",
        "--output-fields=MESSAGE,_PID,_SYSTEMD_UNIT,_SYSTEMD_INVOCATION_ID,_SYSTEMD_CGROUP,__REALTIME_TIMESTAMP",
        "--since", _journal_epoch(since_ms), "--until", _journal_epoch(until_ms),
        f"--unit=deadlock-{service}.service", f"--grep={marker_expression}",
        "--case-sensitive=yes", f"_SYSTEMD_INVOCATION_ID={invocation.lower()}",
    ]
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
            env={"LANG": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "TZ": "UTC"},
        )
    except OSError:
        return [], True
    assert process.stdout is not None
    fd = process.stdout.fileno()
    selector = selectors.DefaultSelector()
    pending = bytearray()
    rows: list[dict[str, Any]] = []
    bytes_read = 0
    deadline = time.monotonic() + CAPACITY_JOURNAL_TIMEOUT_SECONDS
    truncated = False
    eof = False
    try:
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ)
        while process.poll() is None or not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                truncated = True
                break
            for event, _mask in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(event.fd, 32_768)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(event.fd)
                    eof = True
                    break
                bytes_read += len(chunk)
                if bytes_read > CAPACITY_JOURNAL_BYTES:
                    truncated = True
                    break
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        if len(pending) > CAPACITY_JOURNAL_LINE_BYTES:
                            truncated = True
                        break
                    if newline > CAPACITY_JOURNAL_LINE_BYTES:
                        truncated = True
                        del pending[: newline + 1]
                        continue
                    line = bytes(pending[:newline])
                    del pending[: newline + 1]
                    try:
                        row = json.loads(line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
                    except (UnicodeError, json.JSONDecodeError, PlanError):
                        truncated = True
                        continue
                    if not isinstance(row, dict):
                        truncated = True
                        continue
                    rows.append(row)
                    if len(rows) > CAPACITY_JOURNAL_ROWS:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        if process.poll() is None:
            _terminate_journal(process)
        try:
            child_status = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            _terminate_journal(process)
            child_status = process.poll()
        if child_status != 0 or pending:
            truncated = True
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            _terminate_journal(process)
        pending[:] = b"\0" * len(pending)
    return rows[:CAPACITY_JOURNAL_ROWS], truncated


def _iter_file_lines_reverse(
    handle: Any, *, maximum_bytes: int, scan_state: dict[str, Any],
):
    handle.seek(0, os.SEEK_END)
    position = handle.tell()
    read_bytes = 0
    remainder = b""
    while position > 0 and read_bytes < maximum_bytes:
        take = min(64 * 1024, position, maximum_bytes - read_bytes)
        position -= take
        handle.seek(position)
        chunk = handle.read(take)
        read_bytes += len(chunk)
        scan_state["bytes"] = read_bytes
        pieces = (chunk + remainder).split(b"\n")
        remainder = pieces[0]
        for line in reversed(pieces[1:]):
            if line:
                yield line
    if position == 0 and remainder:
        yield remainder
    scan_state["truncated"] = position > 0


def _bounded_nginx_rows(
    request_ids: set[str], *, since_ms: int, until_ms: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Read only a bounded reverse tail of Nginx JSON access logs."""

    if not request_ids:
        return [], False
    paths = [NGINX_ACCESS_LOG]
    try:
        rotated = [
            path for path in NGINX_ACCESS_LOG.parent.glob(f"{NGINX_ACCESS_LOG.name}.*")
            if re.fullmatch(re.escape(NGINX_ACCESS_LOG.name) + r"\.[1-9][0-9]*", path.name)
        ]
        rotated.sort(key=lambda path: int(path.name.rsplit(".", 1)[1]))
    except OSError:
        return [], True
    paths.extend(rotated[:CAPACITY_NGINX_FILES - 1])
    truncated = len(rotated) >= CAPACITY_NGINX_FILES
    rows: list[dict[str, Any]] = []
    bytes_seen = 0
    start = datetime.fromtimestamp(since_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(until_ms / 1000, tz=UTC)
    stop = False
    for path in paths:
        if stop or bytes_seen >= CAPACITY_NGINX_BYTES:
            truncated = True
            break
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb", closefd=True) as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode):
                    truncated = True
                    continue
                line_count = 0
                scan_state: dict[str, Any] = {"bytes": 0, "truncated": False}
                for item in _iter_file_lines_reverse(
                    handle, maximum_bytes=CAPACITY_NGINX_BYTES - bytes_seen,
                    scan_state=scan_state,
                ):
                    line_count += 1
                    if line_count > CAPACITY_NGINX_ROWS:
                        truncated = True
                        stop = True
                        break
                    try:
                        record = json.loads(item.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    timestamp = _capacity_correlation_nginx_timestamp(record.get("time"))
                    if timestamp is None:
                        continue
                    if timestamp < start:
                        stop = True
                        break
                    if timestamp > end or record.get("request_id") not in request_ids:
                        continue
                    rows.append(record)
                    if len(rows) > CAPACITY_NGINX_ROWS:
                        truncated = True
                        stop = True
                        break
                bytes_seen += int(scan_state.get("bytes", 0))
                if scan_state.get("truncated") is True:
                    truncated = True
                after = os.fstat(handle.fileno())
                current = os.stat(path, follow_symlinks=False)
                if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                    truncated = True
        except OSError:
            truncated = True
    return rows[:CAPACITY_NGINX_ROWS], truncated


def _event_timestamp(record: dict[str, Any]) -> str | None:
    raw = record.get("__REALTIME_TIMESTAMP")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value < 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1_000_000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _message_text(record: dict[str, Any]) -> str | None:
    message = record.get("MESSAGE")
    if not isinstance(message, str):
        return None
    if message.startswith("{"):
        try:
            wrapped = json.loads(message)
        except (json.JSONDecodeError, UnicodeError):
            return None
        if isinstance(wrapped, dict):
            message = wrapped.get("message")
    return message if isinstance(message, str) else None


def _record_ssr_stream_coverage(
    message: str, *, run_id: str, coverage: dict[str, int],
) -> bool:
    """Consume one bounded plan coverage marker; reject unknown shapes visibly."""

    capture_name = "ssr_stream_capture_coverage"
    capture_prefix = capture_name + " "
    if message.startswith(capture_name):
        fields: dict[str, str] = {}
        tokens = message[len(capture_prefix):].split() if message.startswith(capture_prefix) else ()
        for token in tokens:
            if token.count("=") != 1:
                fields = {}
                break
            key, value = token.split("=", 1)
            if key in fields:
                fields = {}
                break
            fields[key] = value
        expected = {
            "scheme", "run_id", "eligible", "retained", "lost", "rows",
            "lost_rows", "status",
        }
        if set(fields) != expected or fields.get("run_id") != run_id:
            coverage["capture_malformed_records"] += 1
            return True
        if fields.get("scheme") != "sha256-runid-requestid-1of8-v1":
            coverage["capture_malformed_records"] += 1
            return True
        try:
            values = {
                key: int(fields[key])
                for key in ("eligible", "retained", "lost", "rows", "lost_rows")
            }
        except ValueError:
            coverage["capture_malformed_records"] += 1
            return True
        if (any(value < 0 or value > 1_000_000 for value in values.values())
                or values["rows"] > 6_000
                or values["eligible"] != values["retained"] + values["lost"]
                or fields["status"] not in {"complete", "truncated"}
                or (fields["status"] == "complete" and (
                    values["lost"] != 0 or values["lost_rows"] != 0
                ))
                or (fields["status"] == "truncated" and (
                    values["lost"] == 0 and values["lost_rows"] == 0
                ))):
            coverage["capture_malformed_records"] += 1
            return True
        coverage["capture_marker_records"] += 1
        for source_key, target_key in (
            ("eligible", "capture_eligible_requests"),
            ("retained", "capture_retained_requests"),
            ("lost", "capture_lost_requests"),
            ("rows", "capture_rows"),
            ("lost_rows", "capture_lost_rows"),
        ):
            coverage[target_key] += values[source_key]
        if fields["status"] == "truncated":
            coverage["capture_truncated_records"] += 1
        return True

    coverage_name = "ssr_stream_coverage"
    prefix = coverage_name + " "
    if not message.startswith(coverage_name):
        return False
    fields: dict[str, str] = {}
    tokens = message[len(prefix):].split() if message.startswith(prefix) else ()
    for token in tokens:
        if token.count("=") != 1:
            fields = {}
            break
        key, value = token.split("=", 1)
        if key in fields:
            fields = {}
            break
        fields[key] = value
    if fields.get("run_id") != run_id or fields.get("status") != "truncated":
        coverage["malformed_records"] += 1
        return True
    if set(fields) == {
        "run_id", "selector_overflow", "selector_overflow_unattributed", "status",
    }:
        try:
            overflow = int(fields["selector_overflow"])
            unattributed = int(fields["selector_overflow_unattributed"])
        except (KeyError, ValueError):
            coverage["malformed_records"] += 1
            return True
        if not (0 <= overflow <= 1_000_000 and 0 <= unattributed <= 1_000_000):
            coverage["malformed_records"] += 1
            return True
        coverage["records"] += 1
        coverage["selector_overflow"] += overflow
        coverage["selector_overflow_unattributed"] += unattributed
        coverage["truncated_records"] += 1
        return True
    if set(fields) == {"run_id", "rows", "status"}:
        try:
            capped_rows = int(fields["rows"])
        except (KeyError, ValueError):
            coverage["malformed_records"] += 1
            return True
        if not 1 <= capped_rows <= 1_000_000:
            coverage["malformed_records"] += 1
            return True
        coverage["records"] += 1
        coverage["row_cap_records"] += 1
        coverage["row_cap_rows"] += capped_rows
        coverage["truncated_records"] += 1
        return True
    coverage["malformed_records"] += 1
    return True


def _capacity_request_telemetry(
    plans: dict[str, dict[str, Any]],
    *,
    selected_request_ids: list[str],
    cohort_counts: dict[str, int],
) -> dict[str, Any]:
    """Join a bounded client-ID sample to phase-scoped Nginx/SSR/API events."""

    if set(plans) != {"api", "web"}:
        raise PlanError("capacity correlation plans are incomplete")
    api_plan, web_plan = plans["api"], plans["web"]
    run_id = web_plan.get("run_id")
    workload = web_plan.get("workload")
    if (not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id)
            or workload not in {HTML_CAPACITY_120S_WORKLOAD, HTML_CPU_PROFILE_20S_WORKLOAD}
            or api_plan.get("run_id") != run_id
            or api_plan.get("workload") != workload
            or api_plan.get("on_start_ms") != web_plan.get("on_start_ms")
            or api_plan.get("on_end_ms") != web_plan.get("on_end_ms")
            or not isinstance(selected_request_ids, list)
            or len(selected_request_ids) > CAPACITY_REQUEST_SAMPLE_LIMIT
            or any(not isinstance(value, str) or not RUN_RE.fullmatch(value)
                   for value in selected_request_ids)
            or len(set(selected_request_ids)) != len(selected_request_ids)
            or selected_request_ids != sorted(selected_request_ids)):
        raise PlanError("capacity correlation identity is invalid")
    counts_fields = {
        "submitted_html_count", "valid_response_id_count",
        "missing_response_id_count", "duplicate_response_id_count",
        "malformed_response_id_count",
        "eligible_response_id_count", "retained_sample_count",
        "sample_scheme", "sample_cap_truncated",
    }
    if (set(cohort_counts) != counts_fields
            or any(type(cohort_counts[key]) is not int or cohort_counts[key] < 0
                   or cohort_counts[key] > 100_000 for key in counts_fields - {
                       "sample_scheme", "sample_cap_truncated",
                   })
            or cohort_counts.get("sample_scheme") != "sha256-runid-requestid-1of8-v1"
            or type(cohort_counts.get("sample_cap_truncated")) is not bool
            or sum(cohort_counts[key] for key in {
                "valid_response_id_count", "missing_response_id_count",
                "duplicate_response_id_count", "malformed_response_id_count",
            })
            != cohort_counts["submitted_html_count"]
            or cohort_counts["eligible_response_id_count"] > cohort_counts["valid_response_id_count"]
            or cohort_counts["retained_sample_count"] != len(selected_request_ids)
            or len(selected_request_ids) != min(
                cohort_counts["eligible_response_id_count"], CAPACITY_REQUEST_SAMPLE_LIMIT,
            )
            or cohort_counts["sample_cap_truncated"] != (
                cohort_counts["eligible_response_id_count"] > CAPACITY_REQUEST_SAMPLE_LIMIT
            )):
        raise PlanError("capacity correlation cohort is invalid")
    start_ms = web_plan["on_start_ms"]
    end_ms = web_plan["on_end_ms"]
    capture_end_ms = min(
        web_plan["expires_at_ms"],
        end_ms + CAPACITY_CAPTURE_DRAIN_MS,
    )
    start_utc = datetime.fromtimestamp(start_ms / 1000, tz=UTC).isoformat()
    end_utc = datetime.fromtimestamp(capture_end_ms / 1000, tz=UTC).isoformat()
    selected_set = set(selected_request_ids)
    api_raw, api_truncated = _bounded_capacity_journal_rows(
        "api", api_plan, since_ms=max(0, start_ms - 1_000),
        until_ms=capture_end_ms,
        markers=("request_perf", "ssr_api_request_perf_coverage"),
    )
    web_raw, web_truncated = _bounded_capacity_journal_rows(
        "web", web_plan, since_ms=max(0, start_ms - 1_000),
        until_ms=capture_end_ms, markers=(
            "ssr_perf", "ssr_stream", "ssr_stream_coverage",
            "ssr_stream_capture_coverage",
        ),
    )
    nginx_raw, nginx_truncated = _bounded_nginx_rows(
        selected_set, since_ms=max(0, start_ms - 1_000), until_ms=capture_end_ms,
    )
    def context(event_at_utc: str, request_id: str) -> dict[str, str]:
        return {
            "run_id": run_id, "phase": "on", "window_start_utc": start_utc,
            "window_end_utc": end_utc, "event_at_utc": event_at_utc,
            "request_id": request_id,
        }

    api_rows: list[dict[str, Any]] = []
    api_capture_truncated = False
    for raw in api_raw:
        message = _message_text(raw)
        event_at = _event_timestamp(raw)
        if message is None or event_at is None:
            continue
        if message.startswith("ssr_api_request_perf_coverage"):
            # This marker is emitted only when the fixed per-process cap is
            # reached. Any unknown form also invalidates source completeness.
            api_capture_truncated = True
            continue
        parsed = _capacity_correlation_parse_request_perf(message)
        if not isinstance(parsed, dict) or parsed.get("request_id") not in selected_set:
            continue
        request_id = parsed["request_id"]
        route = _capacity_correlation_safe_route_class(
            parsed.get("path") or parsed.get("route") or ""
        )
        row: dict[str, Any] = {
            **context(event_at, request_id),
            "route_class": route if route in {"auth_bootstrap", "tournament_workspace"} else "other",
        }
        for field in (
            "total_ms", "sql_ms", "max_sql_ms", "pool_checkout_wait_ms",
            "pool_connection_hold_ms", "compute_ms", "sql_count",
        ):
            value = parsed.get(field)
            if type(value) in (int, float) and not isinstance(value, bool):
                row[field] = value
        api_rows.append(row)

    ssr_rows: list[dict[str, Any]] = []
    stream_rows: list[dict[str, Any]] = []
    stream_coverage = {
        "records": 0, "selector_overflow": 0,
        "selector_overflow_unattributed": 0, "row_cap_records": 0,
        "row_cap_rows": 0, "truncated_records": 0,
        "malformed_records": 0, "capture_marker_records": 0,
        "capture_eligible_requests": 0, "capture_retained_requests": 0,
        "capture_lost_requests": 0, "capture_rows": 0,
        "capture_lost_rows": 0, "capture_truncated_records": 0,
        "capture_malformed_records": 0,
    }
    for raw in web_raw:
        message = _message_text(raw)
        event_at = _event_timestamp(raw)
        if message is None or event_at is None:
            continue
        if _record_ssr_stream_coverage(message, run_id=run_id, coverage=stream_coverage):
            continue
        parsed = _capacity_correlation_parse_ssr_perf(message)
        if parsed is not None:
            if parsed.get("diagnostic_run_id") != run_id or parsed.get("request_id") not in selected_set:
                continue
            request_id = parsed["request_id"]
            stage = _capacity_correlation_safe_stage(parsed.get("stage"))
            duration = parsed.get("duration_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                ssr_rows.append({
                    **context(event_at, request_id),
                    "stage": stage, "duration_ms": duration,
                })
            continue
        parsed_stream = _capacity_correlation_parse_ssr_stream(message)
        if parsed_stream is not None and parsed_stream.get("request_id") in selected_set:
            request_id = parsed_stream["request_id"]
            stage = _capacity_correlation_safe_stage(parsed_stream.get("stage"))
            elapsed = parsed_stream.get("elapsed_ms")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
                stream_rows.append({
                    **context(event_at, request_id),
                    "stage": stage, "elapsed_ms": elapsed,
                })

    nginx_rows: list[dict[str, Any]] = []
    for raw in nginx_raw:
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or request_id not in selected_set:
            continue
        timestamp = _capacity_correlation_nginx_timestamp(raw.get("time"))
        if timestamp is None:
            continue
        request_seconds = raw.get("request_time")
        try:
            request_ms = float(request_seconds) * 1000
        except (TypeError, ValueError, OverflowError):
            request_ms = None
        nginx_rows.append({
            **context(timestamp.isoformat(), request_id),
            "request_time_ms": request_ms,
            "cf_ray": raw.get("cf_ray"),
        })

    rows_truncated = {
        "nginx": nginx_truncated,
        "ssr": web_truncated,
        "stream": web_truncated,
        "api": api_truncated or api_capture_truncated,
    }
    result = summarize_ssr_request_correlation(
        selected_request_ids=selected_request_ids,
        submitted_html_count=cohort_counts["submitted_html_count"],
        valid_response_id_count=cohort_counts["valid_response_id_count"],
        missing_response_id_count=cohort_counts["missing_response_id_count"],
        duplicate_response_id_count=cohort_counts["duplicate_response_id_count"],
        malformed_response_id_count=cohort_counts["malformed_response_id_count"],
        selection_scheme=cohort_counts["sample_scheme"],
        eligible_response_id_count=cohort_counts["eligible_response_id_count"],
        run_id=run_id, phase="on",
        window_start_utc=start_utc, window_end_utc=end_utc,
        nginx_rows=nginx_rows, ssr_rows=ssr_rows, stream_rows=stream_rows,
        api_rows=api_rows,
        rows_truncated=rows_truncated,
        max_rows=CAPACITY_JOURNAL_ROWS,
        sample_limit=CAPACITY_REQUEST_SAMPLE_LIMIT,
        cf_ray_fallback_proven=False,
    )
    result["ssr_stream_coverage"] = {
        **stream_coverage,
        "capture_status": (
            "incomplete" if stream_coverage["capture_malformed_records"]
            else "missing" if stream_coverage["capture_marker_records"] == 0
            else "incomplete" if (
                stream_coverage["capture_marker_records"] != 1
                or stream_coverage["capture_truncated_records"]
                or stream_coverage["capture_malformed_records"]
            ) else "complete"
        ),
        "status": (
            "incomplete"
            if (stream_coverage["truncated_records"]
                or stream_coverage["malformed_records"]
                or stream_coverage["capture_marker_records"] != 1
                or stream_coverage["capture_truncated_records"]
                or stream_coverage["capture_malformed_records"])
            else "none"
        ),
    }
    if (stream_coverage["truncated_records"] or stream_coverage["malformed_records"]
            or stream_coverage["capture_marker_records"] != 1
            or stream_coverage["capture_truncated_records"]
            or stream_coverage["capture_malformed_records"]):
        result["coverage"]["complete"] = False
        result["coverage"]["full_cohort"] = False
    return result


USAGE_MESSAGE_RE = re.compile(
    r"^cpu_diagnostic_usage service=(api|web) run_id=([0-9a-f]{32}) "
    r"phase=(off|on) window_ms=([0-9]{1,8}) cpu_ns=([0-9]{1,20}) "
    r"cpu_capacity_cpus=(?:unknown|[0-9]{1,4}\.[0-9]{6}) "
    r"start_lag_ms=(-?[0-9]{1,8}) end_lag_ms=(-?[0-9]{1,8}) "
    r"timing_complete=(true|false)$"
)
PROFILE_API_MESSAGE_RE = re.compile(
    r"^cpu_diagnostic_complete service=api run_id=([0-9a-f]{32}) "
    r"timer=thread_cpu start_lag_ms=([0-9]{1,8}) elapsed_ms=([0-9]{1,8}) "
    r"total_self_cpu_us=([0-9]{1,12}) functions=(.*)$"
)
PROFILE_WEB_MESSAGE_RE = re.compile(
    r"^cpu_diagnostic_complete service=web run_id=([0-9a-f]{32}) "
    r"timer=v8_cpu start_lag_ms=([0-9]{1,8}) elapsed_ms=([0-9]{1,8}) "
    r"sample_interval_us=100000 sample_count=([0-9]{1,8}) categories=(.*)$"
)
NODE_MEMORY_FIELDS = frozenset({
    "service", "run_id", "phase", "sample_count", "status",
    "rss_min_bytes", "rss_max_bytes", "rss_mean_bytes",
    "heap_total_min_bytes", "heap_total_max_bytes", "heap_total_mean_bytes",
    "heap_used_min_bytes", "heap_used_max_bytes", "heap_used_mean_bytes",
    "heap_limit_min_bytes", "heap_limit_max_bytes", "heap_limit_mean_bytes",
    "external_min_bytes", "external_max_bytes", "external_mean_bytes",
    "array_buffers_min_bytes", "array_buffers_max_bytes", "array_buffers_mean_bytes",
    "sampled_window_start_elapsed_ms", "sampled_window_end_elapsed_ms", "sampled_window_duration_ms",
    "cgroup_sample_count", "cgroup_memory_current_start_bytes",
    "cgroup_memory_current_end_bytes", "cgroup_memory_current_sampled_max_bytes",
    "cgroup_memory_peak_start_bytes", "cgroup_memory_peak_end_bytes",
    "cgroup_memory_peak_sampled_max_bytes", "cgroup_memory_status", "cgroup_memory_reason",
    "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
    "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
    "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
    "gc_count", "gc_duration_ms", "gc_status", "gc_major_count", "gc_minor_count",
    "gc_incremental_count", "gc_weakcb_count", "gc_other_count", "gc_overflow",
})
NODE_MEMORY_INTEGER_FIELDS = NODE_MEMORY_FIELDS - {
    "service", "run_id", "phase", "status", "gc_duration_ms", "gc_status", "gc_overflow",
    "cgroup_memory_status", "cgroup_memory_reason",
}
API_PROFILE_CATEGORIES = frozenset({
    "other", "serialization_validation", "orm_result", "db_driver",
    "async_event_loop", "crypto", "repo.get_auth_bootstrap", "repo.build_auth_bootstrap",
    "repo.get_tournament_workspace",
    "repo.get_tournament_workspace_by_slug", "repo.workspace_conditional_preflight",
    "repo.ensure_workspace_private_membership", "repo.ensure_tournament_workspace_visible",
    "repo.build_tournament_workspace_detail_bracket_response",
    "repo.build_tournament_workspace_bracket_summary_response",
    "repo._workspace_response_etag", "repo._serialized_model_response",
    "repo.get_current_user", "repo.get_current_user_optional",
    "repo.get_server_request_correlation_headers", "repo.run_with_ssr_trace",
})
WEB_PROFILE_CATEGORIES = frozenset({
    "other", "web_framework", "serialization_validation", "async_event_loop",
    "http_client", "crypto", "repo.workspace_api_fetch", "repo.workspace_page",
    "react_server_render", "repo.ssr_route_module", "compiled_server_chunk",
})


def _parse_profile_categories(
    raw: str,
    *,
    service: str,
) -> list[dict[str, int | str]] | None:
    if len(raw) > 6000:
        return None
    allowed = API_PROFILE_CATEGORIES if service == "api" else WEB_PROFILE_CATEGORIES
    if not raw:
        return [] if service == "api" else None
    parsed: list[dict[str, int | str]] = []
    seen: set[str] = set()
    for token in raw.split(","):
        parts = token.split(":")
        if len(parts) != 3 or parts[0] not in allowed or parts[0] in seen:
            return None
        if not parts[1].isdecimal() or not parts[2].isdecimal():
            return None
        cpu_us, observations = int(parts[1]), int(parts[2])
        if cpu_us < 0 or cpu_us > 60_000_000 or observations < 0 or observations > 10_000_000:
            return None
        seen.add(parts[0])
        parsed.append({"category": parts[0], "cpu_us": cpu_us, "observations": observations})
    return parsed


def _parse_node_memory_event(message: str) -> dict[str, Any] | None:
    """Project one bounded Node/cgroup aggregate from its closed log schema."""
    if not message.startswith("cpu_diagnostic_memory ") or len(message) > 4096:
        return None
    values: dict[str, str] = {}
    for token in message.split()[1:]:
        if token.count("=") != 1:
            return None
        key, value = token.split("=", 1)
        if key not in NODE_MEMORY_FIELDS or key in values or not value:
            return None
        values[key] = value
    if set(values) != NODE_MEMORY_FIELDS:
        return None
    if (values["service"] != "web" or RUN_RE.fullmatch(values["run_id"]) is None
            or values["phase"] not in {"off", "on"}
            or values["status"] not in {"complete", "incomplete"}
            or values["gc_status"] not in {"complete", "incomplete", "unavailable"}
            or values["cgroup_memory_status"] not in {"complete", "unavailable", "invalid"}
            or values["cgroup_memory_reason"] not in {"none", "hierarchy_unavailable", "file_unavailable", "invalid_snapshot"}
            or values["gc_overflow"] not in {"true", "false"}
            or not re.fullmatch(r"[0-9]{1,5}\.[0-9]{3}", values["gc_duration_ms"])):
        return None
    result: dict[str, Any] = {"service": "web", "run_id": values["run_id"], "phase": values["phase"]}
    for key in NODE_MEMORY_INTEGER_FIELDS:
        raw = values[key]
        if raw == "unknown" and key.startswith(("cgroup_", "memory_events_", "sampled_window_")):
            result[key] = None
            continue
        if not raw.isdecimal() or len(raw) > 16:
            return None
        result[key] = int(raw)
    for key in ("sample_count", "cgroup_sample_count"):
        if result[key] > 5:
            return None
    if result["sample_count"] > 0 and values["status"] == "complete" and result["sample_count"] != 5:
        return None
    if result["sampled_window_duration_ms"] is not None and result["sampled_window_duration_ms"] > 20_000:
        return None
    if values["cgroup_memory_status"] == "complete" and values["cgroup_memory_reason"] != "none":
        return None
    if values["cgroup_memory_status"] == "invalid" and values["cgroup_memory_reason"] != "invalid_snapshot":
        return None
    if values["cgroup_memory_status"] == "unavailable" and values["cgroup_memory_reason"] == "none":
        return None
    try:
        duration = float(values["gc_duration_ms"])
    except ValueError:
        return None
    if not 0 <= duration <= 20_000 or result["gc_count"] > 10_000:
        return None
    result.update({
        "status": values["status"],
        "gc_status": values["gc_status"],
        "gc_overflow": values["gc_overflow"] == "true",
        "cgroup_memory_status": values["cgroup_memory_status"],
        "cgroup_memory_reason": values["cgroup_memory_reason"],
        "gc_duration_ms": duration,
    })
    return result


def _usage_summary(
    plans: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate exact target/phase journal events; expose aggregates only."""

    workload = plans.get("api", {}).get("workload")
    if (not isinstance(workload, str) or workload not in SUPPORTED_WORKLOADS
            or plans.get("web", {}).get("workload") != workload):
        workload = None

    def empty_rows() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for service in ("api", "web"):
            target_count = 2 if service == "api" else 1
            for phase in ("off", "on"):
                rows.append({
                    "service": service,
                    "phase": phase,
                    "expected_targets": target_count,
                    "observed_targets": 0,
                    "event_count": 0,
                    "cpu_ns": None,
                    "window_ms_min": None,
                    "window_ms_max": None,
                    "start_lag_ms_min": None,
                    "start_lag_ms_max": None,
                    "end_lag_ms_min": None,
                    "end_lag_ms_max": None,
                    "duplicate_count": 0,
                    "timing_complete": False,
                })
        return rows

    def empty_profile_rows() -> list[dict[str, Any]]:
        return [
            {
                "service": service,
                "expected_targets": expected,
                "observed_targets": 0,
                "event_count": 0,
                "timer": "thread_cpu" if service == "api" else "v8_cpu",
                "observation_unit": "calls" if service == "api" else "samples",
                "start_lag_ms_min": None,
                "start_lag_ms_max": None,
                "elapsed_ms_min": None,
                "elapsed_ms_max": None,
                "end_lag_ms_min": None,
                "end_lag_ms_max": None,
                "total_cpu_us": None,
                "sample_count": None,
                "categories": [],
            }
            for service, expected in (("api", 2), ("web", 1))
        ]

    def incomplete(reason: str) -> dict[str, Any]:
        return {
            "workload": workload,
            "usage_status": "incomplete",
            "usage_reason": reason,
            "usage_rows": empty_rows(),
            "profile_status": "incomplete",
            "profile_reason": (
                "identity_changed" if reason == "identity_changed"
                else "invalid_profile" if reason == "invalid_event"
                else reason
            ),
            "profile_rows": empty_profile_rows(),
            "memory_status": "incomplete",
            "memory_reason": "incomplete",
            "cgroup_memory_status": "unknown",
            "cgroup_memory_reason": "hierarchy_unavailable",
            "memory_rows": [],
        }

    targets: dict[str, dict[int, tuple[int, str]]] = {}
    try:
        release_identity = _release_identity()
        if release_identity != (
            str(plans["api"].get("source_sha")),
            str(plans["api"].get("release_slug")),
        ):
            return incomplete("identity_changed")
        for service in ("api", "web"):
            payload = plans[service]
            if (payload.get("run_id") != plans["api"].get("run_id")
                    or payload.get("source_sha") != plans["api"].get("source_sha")
                    or payload.get("release_slug") != plans["api"].get("release_slug")
                    or payload.get("workload") != plans["api"].get("workload")
                    or payload.get("off_start_ms") != plans["api"].get("off_start_ms")
                    or payload.get("off_end_ms") != plans["api"].get("off_end_ms")
                    or payload.get("on_start_ms") != plans["api"].get("on_start_ms")
                    or payload.get("on_end_ms") != plans["api"].get("on_end_ms")):
                return incomplete("identity_changed")
            targets[service] = {
                int(item["pid"]): (int(item["start_ticks"]), str(item["invocation_id"]))
                for item in payload["targets"]
            }
            expected_count = 2 if service == "api" else 1
            if len(targets[service]) != expected_count or len(payload["targets"]) != expected_count:
                return incomplete("identity_changed")
            unit = _systemd_unit(service, timeout_seconds=3)
            actual = _service_targets(service, unit)
            expected = sorted(
                (service, pid, ticks, invocation)
                for pid, (ticks, invocation) in targets[service].items()
            )
            observed = sorted(
                (str(item["service"]), int(item["pid"]), int(item["start_ticks"]),
                 str(item["invocation_id"]))
                for item in actual
            )
            if unit["InvocationID"].lower() not in {
                invocation for _ticks, invocation in targets[service].values()
            } or expected != observed:
                return incomplete("identity_changed")
    except (OSError, KeyError, TypeError, ValueError, PlanError):
        return incomplete("identity_changed")

    capture_status, records = _journal_rows(plans)
    events: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    profiles: dict[tuple[str, int], list[dict[str, Any]]] = {}
    memory_samples: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    invalid_event = False
    invalid_profile = False
    invalid_memory = False
    expected_units = {"api": "deadlock-api.service", "web": "deadlock-web.service"}
    expected_cgroups = {service: f"/system.slice/{unit}" for service, unit in expected_units.items()}
    run_id = str(plans["api"]["run_id"])
    workload = str(plans["api"].get("workload"))
    for record in records:
        message: Any = record.get("MESSAGE")
        if not isinstance(message, str):
            continue
        # API logging wraps `message` in its JSON formatter; web console output
        # is already plain. Decode exactly one API formatter layer if present.
        if message.startswith("{"):
            try:
                wrapped = json.loads(message)
            except (json.JSONDecodeError, UnicodeError):
                wrapped = None
            if isinstance(wrapped, dict):
                message = wrapped.get("message")
        if not isinstance(message, str):
            continue
        is_usage = message.startswith("cpu_diagnostic_usage")
        is_profile = message.startswith("cpu_diagnostic_complete")
        is_memory = message.startswith("cpu_diagnostic_memory")
        if not is_usage and not is_profile and not is_memory:
            continue
        usage_match = USAGE_MESSAGE_RE.fullmatch(message) if is_usage else None
        profile_api_match = PROFILE_API_MESSAGE_RE.fullmatch(message) if is_profile else None
        profile_web_match = PROFILE_WEB_MESSAGE_RE.fullmatch(message) if is_profile else None
        memory_match = _parse_node_memory_event(message) if is_memory else None
        if memory_match is not None and memory_match["run_id"] != run_id:
            continue
        if is_usage and usage_match is None:
            invalid_event = True
            continue
        if is_profile and profile_api_match is None and profile_web_match is None:
            invalid_profile = True
            continue
        if is_memory and memory_match is None:
            invalid_memory = True
            continue
        if usage_match is not None:
            service, event_run, phase = usage_match.group(1), usage_match.group(2), usage_match.group(3)
        elif profile_api_match is not None:
            service, event_run, phase = "api", profile_api_match.group(1), "on"
        elif profile_web_match is not None:
            assert profile_web_match is not None
            service, event_run, phase = "web", profile_web_match.group(1), "on"
        else:
            assert memory_match is not None
            service, event_run, phase = "web", run_id, str(memory_match["phase"])
        if event_run != run_id:
            continue
        try:
            pid = int(record.get("_PID", ""))
            timestamp_us = int(record.get("__REALTIME_TIMESTAMP", ""))
            invocation = str(record.get("_SYSTEMD_INVOCATION_ID", "")).lower()
            unit = str(record.get("_SYSTEMD_UNIT", ""))
            cgroup = str(record.get("_SYSTEMD_CGROUP", ""))
            start_ticks, expected_invocation = targets[service][pid]
            min_timestamp_key = "on_start_ms" if is_profile or (is_memory and phase == "on") else "off_start_ms"
            max_timestamp_key = "on_end_ms" if is_profile or is_usage else ("on_end_ms" if phase == "on" else "off_end_ms")
            min_timestamp = int(plans[service][min_timestamp_key]) * 1000 - 1_000_000
            max_timestamp = int(plans[service][max_timestamp_key]) * 1000 + 5_000_000
            if (timestamp_us < min_timestamp
                    or timestamp_us > max_timestamp
                    or invocation != expected_invocation or unit != expected_units[service]
                    or cgroup != expected_cgroups[service]
                    or invocation != str(plans[service]["targets"][0]["invocation_id"]).lower()
                    or _process_start_ticks(pid) != start_ticks):
                if is_memory:
                    invalid_memory = True
                elif is_profile:
                    invalid_profile = True
                else:
                    invalid_event = True
                continue
            if memory_match is not None:
                memory_samples.setdefault((service, pid, phase), []).append(memory_match)
            elif usage_match is not None:
                event = {
                    "window_ms": int(usage_match.group(4)),
                    "cpu_ns": int(usage_match.group(5)),
                    "start_lag_ms": int(usage_match.group(6)),
                    "end_lag_ms": int(usage_match.group(7)),
                    "timing_complete": usage_match.group(8) == "true",
                }
                if (event["window_ms"] <= 0 or event["window_ms"] > MAX_TOTAL_WINDOW_MS
                        or event["cpu_ns"] > event["window_ms"] * 1_000_000 * 4096):
                    invalid_event = True
                    continue
                events.setdefault((service, pid, phase), []).append(event)
            else:
                if profile_api_match is not None:
                    start_lag_ms = int(profile_api_match.group(2))
                    elapsed_ms = int(profile_api_match.group(3))
                    total_cpu_us = int(profile_api_match.group(4))
                    categories = _parse_profile_categories(profile_api_match.group(5), service="api")
                    sample_count = None
                    timer = "thread_cpu"
                else:
                    assert profile_web_match is not None
                    start_lag_ms = int(profile_web_match.group(2))
                    elapsed_ms = int(profile_web_match.group(3))
                    sample_count = int(profile_web_match.group(4))
                    categories = _parse_profile_categories(profile_web_match.group(5), service="web")
                    total_cpu_us = (
                        sum(int(item["cpu_us"]) for item in categories)
                        if categories is not None else 0
                    )
                    timer = "v8_cpu"
                profile_window_ms = _plan_on_window_ms(workload)
                end_lag_ms = start_lag_ms + elapsed_ms - profile_window_ms
                if (categories is None or total_cpu_us < 0 or total_cpu_us > 60_000_000
                        or not 0 <= start_lag_ms <= 250
                        or profile_window_ms is None
                        or not profile_window_ms - 250 <= elapsed_ms <= profile_window_ms + 500
                        or not -250 <= end_lag_ms <= 250
                        or total_cpu_us > (elapsed_ms + 250) * 1000
                        or (timer == "v8_cpu" and (sample_count is None or sample_count <= 0
                                                   or sample_count > MAX_SAMPLES_PROFILE))):
                    invalid_profile = True
                    continue
                if timer == "v8_cpu" and (
                    sum(int(item["observations"]) for item in categories) != sample_count
                    or sum(int(item["cpu_us"]) for item in categories) != total_cpu_us
                ):
                    invalid_profile = True
                    continue
                if timer == "thread_cpu" and (
                    sum(int(item["cpu_us"]) for item in categories) != total_cpu_us
                ):
                    invalid_profile = True
                    continue
                profiles.setdefault((service, pid), []).append({
                    "start_lag_ms": start_lag_ms,
                    "elapsed_ms": elapsed_ms,
                    "end_lag_ms": end_lag_ms,
                    "total_cpu_us": total_cpu_us,
                    "sample_count": sample_count,
                    "timer": timer,
                    "categories": categories,
                })
        except (KeyError, OSError, ValueError):
            if is_memory:
                invalid_memory = True
            elif is_profile:
                invalid_profile = True
            else:
                invalid_event = True

    reason = "none"
    if capture_status != "complete":
        reason = capture_status
    elif invalid_event:
        reason = "invalid_event"
    rows: list[dict[str, Any]] = []
    all_complete = reason == "none"
    for service in ("api", "web"):
        expected_count = len(targets[service])
        for phase in ("off", "on"):
            expected_window_ms = (
                OFF_WINDOW_MS if phase == "off" else _plan_on_window_ms(workload)
            )
            selected = [
                rows_for_target
                for (event_service, _pid, event_phase), rows_for_target in events.items()
                if event_service == service and event_phase == phase
            ]
            event_count = sum(len(values) for values in selected)
            observed_count = sum(1 for values in selected if len(values) == 1)
            duplicate_count = sum(max(0, len(values) - 1) for values in selected)
            flat = [item for values in selected for item in values]
            row_complete = (
                len(selected) == expected_count
                and event_count == expected_count
                and observed_count == expected_count
                and duplicate_count == 0
                and all(item["timing_complete"] for item in flat)
                and all(
                    0 <= item["start_lag_ms"] <= 250
                    and abs(item["end_lag_ms"]) <= 250
                    and expected_window_ms is not None
                    and expected_window_ms - 250
                    <= item["window_ms"]
                    <= expected_window_ms + 500
                    for item in flat
                )
            )
            if event_count < expected_count and reason == "none":
                reason = "missing"
            elif duplicate_count and reason == "none":
                reason = "duplicate"
            elif flat and not all(item["timing_complete"] for item in flat) and reason == "none":
                reason = "timing_incomplete"
            elif flat and not row_complete and reason == "none":
                reason = "invalid_event"
            if not row_complete:
                all_complete = False
            rows.append({
                "service": service,
                "phase": phase,
                "expected_targets": expected_count,
                "observed_targets": observed_count,
                "event_count": event_count,
                "cpu_ns": sum(item["cpu_ns"] for item in flat) if row_complete else None,
                "window_ms_min": min((item["window_ms"] for item in flat), default=None),
                "window_ms_max": max((item["window_ms"] for item in flat), default=None),
                "start_lag_ms_min": min((item["start_lag_ms"] for item in flat), default=None),
                "start_lag_ms_max": max((item["start_lag_ms"] for item in flat), default=None),
                "end_lag_ms_min": min((item["end_lag_ms"] for item in flat), default=None),
                "end_lag_ms_max": max((item["end_lag_ms"] for item in flat), default=None),
                "duplicate_count": duplicate_count,
                "timing_complete": row_complete,
            })
    if not all_complete and reason == "none":
        reason = "invalid_event"

    profile_reason = "none"
    if capture_status != "complete":
        profile_reason = "invalid_profile" if capture_status == "invalid_event" else capture_status
    elif invalid_profile:
        profile_reason = "invalid_profile"
    profile_rows: list[dict[str, Any]] = []
    profile_complete = profile_reason == "none"
    for service in ("api", "web"):
        expected_count = len(targets[service])
        expected_profile_count = expected_count if workload in {
            WORKLOAD, HTML_CPU_PROFILE_20S_WORKLOAD,
        } else 0
        selected = [
            rows_for_target
            for (event_service, _pid), rows_for_target in profiles.items()
            if event_service == service
        ]
        event_count = sum(len(values) for values in selected)
        observed_count = sum(1 for values in selected if len(values) == 1)
        duplicate_count = sum(max(0, len(values) - 1) for values in selected)
        flat_profiles = [item for values in selected for item in values]
        row_complete = (
            len(selected) == expected_profile_count
            and event_count == expected_profile_count
            and observed_count == expected_profile_count
            and duplicate_count == 0
            and all(item["timer"] == ("thread_cpu" if service == "api" else "v8_cpu")
                    for item in flat_profiles)
            and all(0 <= item["start_lag_ms"] <= 250 for item in flat_profiles)
            and all(
                int(_plan_on_window_ms(workload) or 0) - 250
                <= item["elapsed_ms"]
                <= int(_plan_on_window_ms(workload) or 0) + 500
                for item in flat_profiles
            )
            and all(-250 <= item["end_lag_ms"] <= 250 for item in flat_profiles)
        )
        if event_count < expected_profile_count and profile_reason == "none":
            profile_reason = "missing"
        elif duplicate_count and profile_reason == "none":
            profile_reason = "duplicate"
        elif not row_complete:
            profile_complete = False
        if not row_complete:
            profile_complete = False
        category_totals: dict[str, list[int]] = {}
        for item in flat_profiles:
            for category in item["categories"]:
                entry = category_totals.setdefault(str(category["category"]), [0, 0])
                entry[0] += int(category["cpu_us"])
                entry[1] += int(category["observations"])
        category_rows = [
            {"category": name, "cpu_us": values[0], "observations": values[1]}
            for name, values in sorted(category_totals.items())
        ]
        sample_counts = [item["sample_count"] for item in flat_profiles]
        profile_rows.append({
            "service": service,
            "expected_targets": expected_count,
            "observed_targets": observed_count,
            "event_count": event_count,
            "timer": "thread_cpu" if service == "api" else "v8_cpu",
            "observation_unit": "calls" if service == "api" else "samples",
            "start_lag_ms_min": min((item["start_lag_ms"] for item in flat_profiles), default=None),
            "start_lag_ms_max": max((item["start_lag_ms"] for item in flat_profiles), default=None),
            "elapsed_ms_min": min((item["elapsed_ms"] for item in flat_profiles), default=None),
            "elapsed_ms_max": max((item["elapsed_ms"] for item in flat_profiles), default=None),
            "end_lag_ms_min": min((item["end_lag_ms"] for item in flat_profiles), default=None),
            "end_lag_ms_max": max((item["end_lag_ms"] for item in flat_profiles), default=None),
            "total_cpu_us": (
                sum(int(item["total_cpu_us"]) for item in flat_profiles)
                if row_complete else None
            ),
            "sample_count": (
                sum(int(value) for value in sample_counts if value is not None)
                if row_complete and service == "web" else None
            ),
            "categories": category_rows,
        })
    if not profile_complete and profile_reason == "none":
        profile_reason = "invalid_profile"
    memory_rows: list[dict[str, Any]] = []
    memory_complete = capture_status == "complete" and not invalid_memory
    cgroup_memory_complete = memory_complete
    for phase in ("off", "on"):
        selected = [
            rows_for_target
            for (event_service, _pid, event_phase), rows_for_target in memory_samples.items()
            if event_service == "web" and event_phase == phase
        ]
        flat_memory = [item for rows_for_target in selected for item in rows_for_target]
        duplicate_count = sum(max(0, len(values) - 1) for values in selected)
        row_complete = (
            len(selected) == 1 and len(flat_memory) == 1 and duplicate_count == 0
            and flat_memory[0].get("status") == "complete"
            and flat_memory[0].get("sample_count") == MEMORY_SAMPLE_COUNT
            and flat_memory[0].get("gc_status") == "complete"
            and flat_memory[0].get("gc_overflow") is False
        ) if flat_memory else False
        cgroup_complete = (
            row_complete and flat_memory[0].get("cgroup_sample_count") == MEMORY_SAMPLE_COUNT
            and flat_memory[0].get("cgroup_memory_status") == "complete"
            and flat_memory[0].get("cgroup_memory_reason") == "none"
            and all(flat_memory[0].get(name) is not None for name in (
                "cgroup_memory_current_start_bytes", "cgroup_memory_current_end_bytes",
                "cgroup_memory_current_sampled_max_bytes", "cgroup_memory_peak_start_bytes",
                "cgroup_memory_peak_end_bytes", "cgroup_memory_peak_sampled_max_bytes",
                "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
                "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
                "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
            ))
        )
        cgroup_invalid = any(item.get("cgroup_memory_status") == "invalid" for item in flat_memory)
        cgroup_claim_invalid = any(
            item.get("cgroup_memory_status") == "complete"
            and (
                item.get("cgroup_sample_count") != MEMORY_SAMPLE_COUNT
                or any(item.get(name) is None for name in (
                    "cgroup_memory_current_start_bytes", "cgroup_memory_current_end_bytes",
                    "cgroup_memory_current_sampled_max_bytes", "cgroup_memory_peak_start_bytes",
                    "cgroup_memory_peak_end_bytes", "cgroup_memory_peak_sampled_max_bytes",
                    "memory_events_low_sampled_delta", "memory_events_high_sampled_delta",
                    "memory_events_max_sampled_delta", "memory_events_oom_sampled_delta",
                    "memory_events_oom_kill_sampled_delta", "memory_events_oom_group_kill_sampled_delta",
                ))
            ) for item in flat_memory
        )
        cgroup_invalid = cgroup_invalid or cgroup_claim_invalid
        if not row_complete:
            memory_complete = False
        if not cgroup_complete and not cgroup_invalid:
            cgroup_memory_complete = False
        row: dict[str, Any] = {
            "service": "web", "run_id": plans["web"]["run_id"],
            "phase": phase, "expected_targets": 1,
            "observed_targets": 1 if selected else 0,
            "event_count": sum(len(values) for values in selected),
            "duplicate_count": duplicate_count,
            "status": "complete" if row_complete else "incomplete",
            "gc_status": "unavailable",
            "gc_overflow": False,
            "cgroup_memory_status": "complete" if cgroup_complete else "invalid" if cgroup_invalid else "unknown",
            "cgroup_memory_reason": (
                "invalid_snapshot" if cgroup_invalid else
                flat_memory[0].get("cgroup_memory_reason", "hierarchy_unavailable") if flat_memory else
                "hierarchy_unavailable"
            ),
        }
        if flat_memory:
            for key, value in flat_memory[0].items():
                if key not in {"service", "phase", "status", "gc_status", "gc_overflow", "cgroup_memory_status"}:
                    row[key] = value
            row["gc_status"] = flat_memory[0]["gc_status"]
            row["gc_overflow"] = flat_memory[0]["gc_overflow"]
        for key in NODE_MEMORY_FIELDS - {
            "service", "run_id", "phase", "status", "gc_status", "gc_overflow",
            "cgroup_memory_status", "cgroup_memory_reason",
        }:
            row.setdefault(key, None)
        memory_rows.append(row)
    if invalid_memory:
        memory_complete = False
        cgroup_memory_complete = False
    cgroup_memory_invalid = invalid_memory or any(
        row.get("cgroup_memory_status") == "invalid" for row in memory_rows
    )
    cgroup_memory_reason = (
        "invalid_snapshot" if cgroup_memory_invalid
        else next((str(row["cgroup_memory_reason"]) for row in memory_rows
                   if row.get("cgroup_memory_reason") in {"file_unavailable", "hierarchy_unavailable"}),
                  "none" if cgroup_memory_complete else "hierarchy_unavailable")
    )
    return {
        "workload": workload,
        "usage_status": "complete" if all_complete else "incomplete",
        "usage_reason": reason,
        "usage_rows": rows,
        "profile_status": "complete" if profile_complete else "incomplete",
        "profile_reason": profile_reason,
        "profile_rows": profile_rows,
        "memory_status": "complete" if memory_complete else "incomplete",
        "memory_reason": "none" if memory_complete else "incomplete",
        "cgroup_memory_status": "invalid" if cgroup_memory_invalid else "complete" if cgroup_memory_complete else "unknown",
        "cgroup_memory_reason": cgroup_memory_reason,
        "memory_rows": memory_rows,
    }


def cleanup(args: argparse.Namespace) -> dict[str, object]:
    if os.geteuid() != 0 or RUN_RE.fullmatch(args.run_id) is None:
        raise PlanError("cleanup identity is invalid")
    directory_fd = _validate_plan_directory(create=False)
    removed: list[str] = []
    plans: dict[str, dict[str, Any]] = {}
    identities: dict[str, tuple[int, int, str]] = {}
    try:
        for service, name in PLAN_NAMES.items():
            try:
                raw, metadata = _read_bounded_at(directory_fd, name, MAX_PLAN_BYTES)
            except FileNotFoundError:
                continue
            try:
                payload = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PlanError("existing plan cannot be verified") from exc
            expected_keys = {
                "schema", "run_id", "source_sha", "release_slug", "workload",
                "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms",
                "expires_at_ms", "targets",
            }
            if (not isinstance(payload, dict) or set(payload) != expected_keys
                    or raw != _canonical_json(payload)
                    or payload.get("run_id") != args.run_id
                    or payload.get("schema") != 1
                    or not isinstance(payload.get("workload"), str)
                    or payload.get("workload") not in SUPPORTED_WORKLOADS
                    or any(type(payload.get(key)) is not int for key in (
                        "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms", "expires_at_ms"
                    ))
                    or payload.get("expires_at_ms")
                    != _plan_expiry_ms(payload["on_end_ms"], payload["workload"])):
                raise PlanError("existing plan identity does not match cleanup request")
            if (not RUN_RE.fullmatch(payload.get("run_id", ""))
                    or not SHA_RE.fullmatch(payload.get("source_sha", ""))
                    or not SLUG_RE.fullmatch(payload.get("release_slug", ""))):
                raise PlanError("existing plan identity is malformed")
            targets = payload.get("targets")
            if (not isinstance(targets, list) or not targets
                    or any(not isinstance(target, dict)
                           or set(target) != {"service", "pid", "start_ticks", "invocation_id"}
                           or target.get("service") != service
                           or type(target.get("pid")) is not int or target["pid"] <= 0
                           or type(target.get("start_ticks")) is not int or target["start_ticks"] <= 0
                           or not isinstance(target.get("invocation_id"), str)
                           or not INVOCATION_RE.fullmatch(target["invocation_id"])
                           for target in targets)):
                raise PlanError("existing plan target set is malformed")
            now_ms = time.time_ns() // 1_000_000
            if payload["expires_at_ms"] >= now_ms:
                raise PlanError("diagnostic plan has not expired")
            gid = grp.getgrnam(f"oldsparky-{service}").gr_gid
            if (metadata.st_gid != gid or stat.S_IMODE(metadata.st_mode) != 0o440):
                raise PlanError("existing plan metadata does not match service")
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if current.st_dev != metadata.st_dev or current.st_ino != metadata.st_ino:
                raise PlanError("existing plan changed during cleanup")
            plans[service] = payload
            identities[service] = (
                metadata.st_dev,
                metadata.st_ino,
                hashlib.sha256(raw).hexdigest(),
            )

        usage: dict[str, Any]
        if set(plans) == {"api", "web"}:
            try:
                usage = _usage_summary(plans)
            except Exception:
                # Capture is supplementary. Never leave a live plan behind
                # because bounded journal summarization failed.
                usage = {
                    "workload": plans["api"].get("workload"),
                    "usage_status": "incomplete",
                    "usage_reason": "invalid_event",
                    "usage_rows": [
                        {
                            "service": service,
                            "phase": phase,
                            "expected_targets": 2 if service == "api" else 1,
                            "observed_targets": 0,
                            "event_count": 0,
                            "cpu_ns": None,
                            "window_ms_min": None,
                            "window_ms_max": None,
                            "start_lag_ms_min": None,
                            "start_lag_ms_max": None,
                            "end_lag_ms_min": None,
                            "end_lag_ms_max": None,
                            "duplicate_count": 0,
                            "timing_complete": False,
                        }
                        for service in ("api", "web")
                        for phase in ("off", "on")
                    ],
                    "profile_status": "incomplete",
                    "profile_reason": "invalid_profile",
                    "profile_rows": [
                        {
                            "service": service,
                            "expected_targets": expected,
                            "observed_targets": 0,
                            "event_count": 0,
                            "timer": "thread_cpu" if service == "api" else "v8_cpu",
                            "observation_unit": "calls" if service == "api" else "samples",
                            "start_lag_ms_min": None,
                            "start_lag_ms_max": None,
                            "elapsed_ms_min": None,
                            "elapsed_ms_max": None,
                            "end_lag_ms_min": None,
                            "end_lag_ms_max": None,
                            "total_cpu_us": None,
                            "sample_count": None,
                            "categories": [],
                        }
                        for service, expected in (("api", 2), ("web", 1))
                    ],
                    "memory_status": "incomplete",
                    "memory_reason": "incomplete",
                    "cgroup_memory_status": "unknown",
                    "cgroup_memory_reason": "hierarchy_unavailable",
                    "memory_rows": [],
                }
        else:
            usage = {
                "workload": (
                    next(iter(plans.values())).get("workload") if plans else None
                ),
                "usage_status": "unavailable",
                "usage_reason": "missing",
                "usage_rows": [],
                "profile_status": "unavailable",
                "profile_reason": "missing",
                "profile_rows": [],
                "memory_status": "unavailable",
                "memory_reason": "unavailable",
                "cgroup_memory_status": "unknown",
                "cgroup_memory_reason": "hierarchy_unavailable",
                "memory_rows": [],
            }

        request_telemetry: dict[str, Any] | None = None
        if getattr(args, "client_request_ids", None) is not None:
            try:
                if not isinstance(args.client_cohort_counts, dict):
                    raise PlanError("request cohort is unavailable")
                summary = _capacity_request_telemetry(
                    plans,
                    selected_request_ids=args.client_request_ids,
                    cohort_counts=args.client_cohort_counts,
                )
                request_telemetry = {
                    "status": (
                        "complete" if summary.get("coverage", {}).get("complete") is True
                        else "incomplete"
                    ),
                    "summary": summary,
                }
            except Exception:
                request_telemetry = {
                    "status": "incomplete",
                    "summary": None,
                }

        # Plan removal remains an exact, separately verified operation even
        # when the diagnostic journal was incomplete or unavailable.
        for service, name in PLAN_NAMES.items():
            identity = identities.get(service)
            if identity is None:
                continue
            device, inode, expected_digest = identity
            raw, metadata = _read_bounded_at(directory_fd, name, MAX_PLAN_BYTES)
            if ((metadata.st_dev, metadata.st_ino) != (device, inode)
                    or hashlib.sha256(raw).hexdigest() != expected_digest):
                raise PlanError("existing plan changed during cleanup")
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (device, inode):
                raise PlanError("existing plan changed during cleanup")
            os.unlink(name, dir_fd=directory_fd)
            removed.append(service)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    result = {
        "status": "expired_plans_removed",
        "service_count": len(removed),
        **usage,
    }
    if request_telemetry is not None:
        result["request_telemetry"] = request_telemetry
    return result


def _read_cgroup_pseudo(path: Path, *, maximum: int) -> str:
    """Read one fixed, derived cgroup pseudo-file without trusting st_size."""

    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise PlanError("cgroup file type is invalid")
        raw = bytearray()
        while len(raw) <= maximum:
            block = os.read(fd, min(1024, maximum + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (not raw or len(raw) > maximum
                or (before.st_dev, before.st_ino, before.st_mode)
                != (after.st_dev, after.st_ino, after.st_mode)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
            raise PlanError("cgroup file changed")
        return bytes(raw).decode("ascii").strip()
    finally:
        os.close(fd)


def _capacity_plan_identity(
    run_id: str, source_sha: str
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    if RUN_RE.fullmatch(run_id) is None or SHA_RE.fullmatch(source_sha) is None:
        raise PlanError("capacity watch identity is invalid")
    if _release_identity()[0] != source_sha:
        raise PlanError("capacity watch release source changed")
    directory_fd = _validate_plan_directory(create=False)
    try:
        raw, _metadata = _read_bounded_at(directory_fd, PLAN_NAMES["web"], MAX_PLAN_BYTES)
    finally:
        os.close(directory_fd)
    try:
        plan = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError, PlanError) as exc:
        raise PlanError("capacity watch plan is invalid") from exc
    if (not isinstance(plan, dict) or raw != _canonical_json(plan)
            or plan.get("schema") != 1
            or plan.get("run_id") != run_id or plan.get("source_sha") != source_sha
            or not isinstance(plan.get("workload"), str)
            or plan.get("workload") not in MEMORY_WATCH_WORKLOADS
            or any(type(plan.get(key)) is not int for key in (
                "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms", "expires_at_ms"
            ))
            or plan.get("expires_at_ms")
            != _plan_expiry_ms(plan["on_end_ms"], plan["workload"])):
        raise PlanError("capacity watch plan binding failed")
    if (plan["off_start_ms"] >= plan["off_end_ms"]
            or plan["off_end_ms"] >= plan["on_start_ms"]
            or plan["on_start_ms"] >= plan["on_end_ms"]
            or plan["off_end_ms"] - plan["off_start_ms"] != OFF_WINDOW_MS
            or plan["on_start_ms"] - plan["off_end_ms"] != IDLE_WINDOW_MS
            or plan["on_end_ms"] - plan["on_start_ms"]
            != ON_WINDOW_MS_BY_WORKLOAD.get(str(plan["workload"]))
            or plan["expires_at_ms"]
            != _plan_expiry_ms(plan["on_end_ms"], str(plan["workload"]))):
        raise PlanError("capacity watch plan timing is invalid")
    web_unit = _systemd_unit("web")
    targets = _service_targets("web", web_unit)
    if len(targets) != 1 or targets[0]["pid"] != int(web_unit["MainPID"]):
        raise PlanError("capacity watch web process topology changed")
    return web_unit, targets[0], plan


def watch_capacity_memory(args: argparse.Namespace):
    """Stream bounded read-only memory observations for the fixed web unit."""

    if os.geteuid() != 0:
        raise PlanError("root identity required")
    web_unit, target, plan = _capacity_plan_identity(args.run_id, args.source_sha)
    if getattr(args, "workload", None) != plan.get("workload"):
        raise PlanError("capacity watch workload binding changed")
    control_group = web_unit["ControlGroup"]
    if control_group != "/system.slice/deadlock-web.service":
        raise PlanError("capacity watch control group changed")
    cgroup_dir = Path("/sys/fs/cgroup/system.slice/deadlock-web.service")
    if _systemd_memory_max() != SSR_CAPACITY_MEMORY_MAX_BYTES:
        raise PlanError("capacity watch declared memory limit changed")
    off_start_ms = plan["off_start_ms"]
    on_end_ms = plan["on_end_ms"]
    if type(off_start_ms) is not int or type(on_end_ms) is not int:
        raise PlanError("capacity watch plan timing is invalid")
    initial_events: dict[str, int] | None = None
    sequence = 0
    abort_reason: str | None = None
    while True:
        now_ms = time.time_ns() // 1_000_000
        reason = "none"
        decision = "continue"
        identity_status = "complete"
        memory_current = memory_peak = memory_max = None
        deltas: dict[str, int] | None = None
        current_unit: dict[str, str] | None = None
        try:
            current_unit = _systemd_unit("web", timeout_seconds=2)
            current_targets = _service_targets("web", current_unit)
            memory_current = int(_read_cgroup_pseudo(cgroup_dir / "memory.current", maximum=64))
            memory_peak = int(_read_cgroup_pseudo(cgroup_dir / "memory.peak", maximum=64))
            memory_max = _read_cgroup_pseudo(cgroup_dir / "memory.max", maximum=64)
            if (memory_max != str(SSR_CAPACITY_MEMORY_MAX_BYTES)
                    or _systemd_memory_max() != SSR_CAPACITY_MEMORY_MAX_BYTES
                    or current_unit != web_unit or len(current_targets) != 1
                    or current_targets[0] != target):
                raise PlanError("capacity watch service identity changed")
            events_text = _read_cgroup_pseudo(cgroup_dir / "memory.events", maximum=512)
            events: dict[str, int] = {}
            for line in events_text.splitlines():
                fields = line.split()
                if len(fields) != 2 or fields[0] not in {
                    "low", "high", "max", "oom", "oom_kill", "oom_group_kill",
                } or fields[0] in events or not fields[1].isdecimal():
                    raise PlanError("capacity watch event schema changed")
                events[fields[0]] = int(fields[1])
            if set(events) != {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}:
                raise PlanError("capacity watch event schema incomplete")
            if initial_events is None:
                initial_events = events.copy()
            deltas = {key: events[key] - initial_events[key] for key in events}
            if any(value < 0 for value in deltas.values()):
                raise PlanError("capacity watch event counters reset")
            reason = "none"
            if memory_current >= SSR_CAPACITY_ABORT_FLOOR_BYTES:
                reason = "memory_floor"
            elif any(deltas[key] > 0 for key in ("high", "max", "oom", "oom_kill", "oom_group_kill")):
                reason = "memory_event"
            if reason != "none":
                abort_reason = reason
        except (OSError, ValueError, TypeError, KeyError, PlanError):
            memory_current = memory_peak = None
            memory_max = None
            deltas = None
            reason = "identity_changed" if current_unit is not None else "collection_error"
            abort_reason = reason
            identity_status = "invalid"
        if abort_reason is not None:
            decision = "abort"
            reason = abort_reason
        elif now_ms >= on_end_ms:
            decision = "abort" if now_ms > on_end_ms + 1_000 else "complete"
            reason = "collection_error" if decision == "abort" else "none"
        phase = (
            "off" if now_ms < plan["off_end_ms"]
            else "idle" if now_ms < plan["on_start_ms"]
            else "on" if now_ms < on_end_ms
            else "complete" if decision == "complete"
            else "abort"
        )
        sequence += 1
        yield {
            "schema": 1,
            "run_id": args.run_id,
            "source_sha": args.source_sha,
            "workload": plan["workload"],
            "sequence": sequence,
            "elapsed_ms": max(0, now_ms - off_start_ms),
            "phase": phase,
            "sampled_at_ms": now_ms,
            "plan_off_start_ms": off_start_ms,
            "plan_on_start_ms": plan["on_start_ms"],
            "plan_on_end_ms": on_end_ms,
            "decision": decision,
            "reason": reason,
            "memory_current_bytes": memory_current,
            "memory_peak_bytes": memory_peak,
            "memory_max_bytes": int(memory_max) if isinstance(memory_max, str) and memory_max.isdecimal() else None,
            "memory_events_delta": deltas,
            "target_pid": int(target["pid"]),
            "target_start_ticks": int(target["start_ticks"]),
            "target_invocation_id": str(target["invocation_id"]),
            "identity_status": identity_status,
        }
        if decision in {"abort", "complete"}:
            return
        # Keep the guard observable during plan lead time too; the parent
        # requires a bounded heartbeat and must be able to stop the workload.
        next_boundary_ms = min(
            on_end_ms,
            now_ms + round(SSR_CAPACITY_WATCH_INTERVAL_SECONDS * 1000),
        )
        time.sleep(max(0.0, (next_boundary_ms - now_ms) / 1000))


def _parser() -> argparse.ArgumentParser:
    class ClosedArgumentParser(argparse.ArgumentParser):
        def error(self, _message: str) -> None:
            raise PlanError("command shape is invalid")

    parser = ClosedArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare-stdin")
    subparsers.add_parser("cleanup-stdin")
    subparsers.add_parser("memory-watch-stdin")
    return parser


# Bounded HTML-to-server correlation used by the sealed diagnostic plan.
_CAPACITY_CORR_REQUEST_PERF_RE = re.compile(r"\brequest_perf\b(?P<body>.*)$")
_CAPACITY_CORR_SSR_PERF_RE = re.compile(r"\bssr_perf\b(?P<body>.*)$")
_CAPACITY_CORR_SSR_STREAM_RE = re.compile(r"\bssr_stream\b(?P<body>.*)$")
_CAPACITY_CORR_SAFE_STAGES = frozenset(
    {
        "http_request_start", "auth_bootstrap", "auth_bootstrap_fetch",
        "auth_http_fetch", "auth_body_parse", "auth_projection", "auth_session",
        "tournament_detail", "tournament_detail_data_ready", "tournament_workspace",
        "internal_read_admission_wait", "workspace_http_fetch", "workspace_body_parse",
        "workspace_projection", "proxy_start", "proxy_to_root_layout_start",
        "request_to_proxy", "request_start", "root_layout_start", "root_layout",
        "ready_check", "ready_vote", "assignment", "page_component",
        "response_stream_start", "first_body_write_attempt", "response_finish",
        "response_close", "response_error", "response", "other",
    }
)


def _capacity_correlation_parse_fields(
    line: str, expression: re.Pattern[str],
) -> dict[str, str] | None:
    match = expression.search(line)
    if match is None:
        return None
    values: dict[str, str] = {}
    for token in match.group("body").strip().split():
        if "=" in token:
            key, raw_value = token.split("=", 1)
            values[key] = raw_value
    return values


def _capacity_correlation_parse_request_perf(line: str) -> dict[str, Any] | None:
    values: dict[str, Any] | None = _capacity_correlation_parse_fields(
        line, _CAPACITY_CORR_REQUEST_PERF_RE,
    )
    if values is None:
        return None
    for key, caster in (
        ("total_ms", float), ("sql_ms", float), ("max_sql_ms", float),
        ("pool_checkout_wait_ms", float), ("pool_connection_hold_ms", float),
        ("compute_ms", float), ("sql_count", int),
    ):
        if key in values:
            try:
                values[key] = caster(values[key])
            except ValueError:
                pass
    return values


def _capacity_correlation_parse_ssr_perf(line: str) -> dict[str, Any] | None:
    values: dict[str, Any] | None = _capacity_correlation_parse_fields(
        line, _CAPACITY_CORR_SSR_PERF_RE,
    )
    if values is None:
        return None
    for key in ("start_ms", "end_ms", "duration_ms"):
        if key not in values:
            continue
        try:
            values[key] = float(values[key])
        except ValueError:
            pass
    if any(
        isinstance(values.get(key), float) and not math.isfinite(values[key])
        for key in ("start_ms", "end_ms", "duration_ms")
    ):
        return None
    if not values.get("request_id") or not values.get("stage"):
        return None
    return values


def _capacity_correlation_parse_ssr_stream(line: str) -> dict[str, Any] | None:
    values: dict[str, Any] | None = _capacity_correlation_parse_fields(
        line, _CAPACITY_CORR_SSR_STREAM_RE,
    )
    if values is None:
        return None
    for key in (
        "elapsed_ms", "status", "active_requests", "writable_finished",
        "write_count", "body_bytes", "response_error",
    ):
        if key not in values:
            continue
        try:
            values[key] = float(values[key]) if key == "elapsed_ms" else int(values[key])
        except ValueError:
            pass
    if (
        isinstance(values.get("elapsed_ms"), float)
        and not math.isfinite(values["elapsed_ms"])
    ):
        return None
    if (
        not values.get("request_id") or not values.get("stage")
        or not isinstance(values.get("elapsed_ms"), (int, float))
    ):
        return None
    return values


def _capacity_correlation_safe_route_class(path: object) -> str:
    if not isinstance(path, str):
        return "other"
    candidate = path.split("?", 1)[0].split("#", 1)[0]
    if "://" in candidate:
        try:
            candidate = urlsplit(candidate).path
        except ValueError:
            return "other"
    candidate = candidate.strip().lstrip("/")
    if candidate.startswith("api/v1/"):
        candidate = candidate[7:]
    elif candidate == "api/v1":
        candidate = ""
    if candidate == "auth/bootstrap":
        return "auth_bootstrap"
    if re.fullmatch(r"tournaments/[^/]+/workspace", candidate):
        return "tournament_workspace"
    return "other"


def _capacity_correlation_safe_stage(value: object) -> str:
    stage = str(value or "").strip().lower().replace("-", "_")
    return stage if stage in _CAPACITY_CORR_SAFE_STAGES else "other"


def _capacity_correlation_nginx_timestamp(raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str):
        return None
    try:
        value = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


_SSR_CORR_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SSR_CORR_SAFE_CONTEXT_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_SSR_CORR_CF_RAY_RE = re.compile(r"^[A-Za-z0-9.-]{1,128}$")
_SSR_CORR_DEFAULT_MAX_ROWS = 200_000
_SSR_CORR_DEFAULT_SAMPLE_LIMIT = 1_000
_SSR_CORR_MAX_METRIC_VALUE = 1_000_000_000.0
_SSR_CORR_SELECTION_SCHEME = "sha256-runid-requestid-1of8-v1"
_SSR_CORR_KNOWN_SSR_STAGES = frozenset(
    {
        "auth_bootstrap_fetch",
        "tournament_workspace",
        "tournament_detail_data_ready",
        "http_request_start",
        "root_layout",
        "page_component",
        "page_render",
        "proxy_start",
        "proxy_to_root_layout_start",
        "request_to_proxy",
    }
)
_SSR_CORR_KNOWN_STREAM_MILESTONES = frozenset(
    {
        "request_start",
        "response_stream_start",
        "first_body_write_attempt",
        "response_finish",
        "response_close",
        "response_error",
    }
)
_SSR_CORR_KNOWN_API_ROUTES = frozenset({"auth_bootstrap", "tournament_workspace"})


def _ssr_corr_finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > _SSR_CORR_MAX_METRIC_VALUE:
        return None
    return number


def _ssr_corr_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _ssr_corr_metric(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    ordered = sorted(values)

    def percentile(percent: float) -> float:
        rank = (len(ordered) - 1) * percent / 100
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "avg_ms": round(sum(ordered) / len(ordered), 3),
        "p50_ms": round(percentile(50), 3),
        "p95_ms": round(percentile(95), 3),
        "p99_ms": round(percentile(99), 3),
        "max_ms": round(ordered[-1], 3),
    }


def _ssr_corr_scalar_metric(values: list[float]) -> dict[str, float | int | None]:
    """Summarize dimensionless SQL-count values without millisecond labels."""

    if not values:
        return {
            "count": 0,
            "avg": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    ordered = sorted(values)

    def percentile(percent: float) -> float:
        rank = (len(ordered) - 1) * percent / 100
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "avg": round(sum(ordered) / len(ordered), 3),
        "p50": round(percentile(50), 3),
        "p95": round(percentile(95), 3),
        "p99": round(percentile(99), 3),
        "max": round(ordered[-1], 3),
    }


def _ssr_corr_bounded_rows(
    rows: Iterable[Mapping[str, Any]], limit: int
) -> tuple[list[Mapping[str, Any]], bool]:
    selected = list(itertools.islice(iter(rows), limit + 1))
    truncated = len(selected) > limit
    return selected[:limit], truncated


def _ssr_corr_context_valid(
    row: Mapping[str, Any],
    *,
    run_id: str,
    phase: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[bool, str]:
    if row.get("run_id") != run_id or row.get("phase") != phase:
        return False, "context_mismatch"
    if row.get("window_start_utc") != window_start.isoformat() or row.get(
        "window_end_utc"
    ) != window_end.isoformat():
        # Accept the caller's equivalent Z spelling only after parsing it.
        row_start = _ssr_corr_timestamp(row.get("window_start_utc"))
        row_end = _ssr_corr_timestamp(row.get("window_end_utc"))
        if row_start != window_start or row_end != window_end:
            return False, "context_mismatch"
    event_time = _ssr_corr_timestamp(row.get("event_at_utc"))
    if event_time is None:
        return False, "missing_or_invalid_timestamp"
    if event_time < window_start or event_time > window_end:
        return False, "outside_window"
    return True, "ok"


def _ssr_corr_safe_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def ssr_request_response_id_selection_digest(run_id: str, request_id: str) -> str | None:
    """Return the full SHA-256 rank for an eligible-format response ID."""

    if (
        not isinstance(run_id, str)
        or not isinstance(request_id, str)
        or not _SSR_CORR_REQUEST_ID_RE.fullmatch(request_id)
    ):
        return None
    try:
        raw = f"{run_id}:{request_id}".encode("ascii")
    except UnicodeEncodeError:
        return None
    return hashlib.sha256(raw).hexdigest()


def is_ssr_response_id_eligible(run_id: str, request_id: str) -> bool:
    """Apply the shared deterministic 1-in-8 diagnostic sampling predicate."""

    digest = ssr_request_response_id_selection_digest(run_id, request_id)
    return digest is not None and (int(digest[:2], 16) & 7) == 0


def summarize_ssr_request_correlation(
    *,
    selected_request_ids: Iterable[object],
    submitted_html_count: int,
    valid_response_id_count: int,
    eligible_response_id_count: int,
    selection_scheme: str,
    missing_response_id_count: int | None = None,
    duplicate_response_id_count: int = 0,
    malformed_response_id_count: int = 0,
    run_id: str,
    phase: str,
    window_start_utc: str,
    window_end_utc: str,
    nginx_rows: Iterable[Mapping[str, Any]],
    ssr_rows: Iterable[Mapping[str, Any]],
    stream_rows: Iterable[Mapping[str, Any]],
    api_rows: Iterable[Mapping[str, Any]],
    rows_truncated: bool | Mapping[str, bool] = False,
    max_rows: int = _SSR_CORR_DEFAULT_MAX_ROWS,
    sample_limit: int = _SSR_CORR_DEFAULT_SAMPLE_LIMIT,
    cf_ray_fallback_proven: bool = False,
) -> dict[str, Any]:
    """Join sampled HTML requests to Nginx, SSR, and all matching API calls.

    Each source row must carry ``run_id``, ``phase``, ``window_start_utc``,
    ``window_end_utc`` and an aware ``event_at_utc``. Request IDs are strict
    lowercase 32-hex tokens. CF-Ray fallback is disabled unless its exact
    equality contract has been independently established by the caller.

    The ``selected_request_ids`` iterable is expected to be the caller's
    deterministic lowest-rank sample across all eligible singleton response
    IDs, regardless of outcome or latency. The helper validates each selected
    ID against the shared 1-in-8 predicate, but cannot independently prove the
    producer selected the globally lowest ranks when the eligible population
    exceeds ``sample_limit``. Rows are bounded again here even if collection
    already applied a limit. Timing rows outside the capture window or without
    a valid timestamp are excluded from joins and reduce coverage. Stream
    offsets are separate from SSR stage durations; a first-body-write attempt
    is not evidence that bytes reached the client. The caller supplies
    ``valid_response_id_count`` as the number of IDs belonging to exactly one
    client request; every request involved in a duplicate-ID group belongs in
    ``duplicate_response_id_count`` and is excluded from the selected sample.
    """

    if not isinstance(run_id, str) or not _SSR_CORR_SAFE_CONTEXT_RE.fullmatch(run_id):
        raise ValueError("run_id is invalid")
    if not isinstance(phase, str) or not _SSR_CORR_SAFE_CONTEXT_RE.fullmatch(phase):
        raise ValueError("phase is invalid")
    if selection_scheme != _SSR_CORR_SELECTION_SCHEME:
        raise ValueError("selection_scheme is unsupported")
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows < 1:
        raise ValueError("max_rows must be positive")
    if not isinstance(sample_limit, int) or isinstance(sample_limit, bool) or sample_limit < 1:
        raise ValueError("sample_limit must be positive")
    submitted = _ssr_corr_safe_count(submitted_html_count)
    valid_ids = _ssr_corr_safe_count(valid_response_id_count)
    eligible_ids = _ssr_corr_safe_count(eligible_response_id_count)
    duplicate_ids = _ssr_corr_safe_count(duplicate_response_id_count)
    malformed_ids = _ssr_corr_safe_count(malformed_response_id_count)
    if (
        submitted is None
        or valid_ids is None
        or eligible_ids is None
        or duplicate_ids is None
        or malformed_ids is None
        or eligible_ids > valid_ids
        or valid_ids + duplicate_ids + malformed_ids > submitted
    ):
        raise ValueError("cohort counts are invalid")
    missing_ids = (
        submitted - valid_ids - duplicate_ids - malformed_ids
        if missing_response_id_count is None
        else _ssr_corr_safe_count(missing_response_id_count)
    )
    if (
        missing_ids is None
        or valid_ids + missing_ids + duplicate_ids + malformed_ids != submitted
    ):
        raise ValueError("cohort response-ID counts must cover every submitted request")
    window_start = _ssr_corr_timestamp(window_start_utc)
    window_end = _ssr_corr_timestamp(window_end_utc)
    if window_start is None or window_end is None or window_end < window_start:
        raise ValueError("capture window is invalid")

    raw_sample, selected_truncated = _ssr_corr_bounded_rows(selected_request_ids, sample_limit + 1)
    selected_truncated = selected_truncated or len(raw_sample) > sample_limit
    raw_sample = raw_sample[:sample_limit]
    selected_ids: set[str] = set()
    malformed_sample_ids = 0
    ineligible_sample_ids = 0
    duplicate_sample_ids = 0
    for value in raw_sample:
        if not isinstance(value, str) or not _SSR_CORR_REQUEST_ID_RE.fullmatch(value):
            malformed_sample_ids += 1
            continue
        if not is_ssr_response_id_eligible(run_id, value):
            ineligible_sample_ids += 1
            continue
        if value in selected_ids:
            duplicate_sample_ids += 1
            continue
        selected_ids.add(value)

    expected_sample_count = min(eligible_ids, sample_limit)
    sample_complete = (
        not selected_truncated
        and len(selected_ids) == expected_sample_count
        and malformed_sample_ids == 0
        and ineligible_sample_ids == 0
        and duplicate_sample_ids == 0
    )

    sources = {
        "nginx": nginx_rows,
        "ssr": ssr_rows,
        "stream": stream_rows,
        "api": api_rows,
    }
    bounded: dict[str, list[Mapping[str, Any]]] = {}
    source_truncated: dict[str, bool] = {}
    source_rejections: dict[str, Counter[str]] = {}
    for name, rows in sources.items():
        bounded[name], local_truncated = _ssr_corr_bounded_rows(rows, max_rows)
        declared = (
            bool(rows_truncated.get(name, False))
            if isinstance(rows_truncated, Mapping)
            else bool(rows_truncated)
        )
        source_truncated[name] = local_truncated or declared
        source_rejections[name] = Counter()

    valid_rows: dict[str, list[Mapping[str, Any]]] = {name: [] for name in sources}
    for name, rows in bounded.items():
        for row in rows:
            valid, reason = _ssr_corr_context_valid(
                row,
                run_id=run_id,
                phase=phase,
                window_start=window_start,
                window_end=window_end,
            )
            if not valid:
                source_rejections[name][reason] += 1
                continue
            request_id = row.get("request_id")
            if not isinstance(request_id, str) or not _SSR_CORR_REQUEST_ID_RE.fullmatch(request_id):
                source_rejections[name]["malformed_request_id"] += 1
                continue
            valid_rows[name].append(row)

    nginx_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows["nginx"]:
        nginx_by_id[str(row["request_id"])].append(row)
    duplicate_nginx_ids = sum(1 for rows in nginx_by_id.values() if len(rows) > 1)

    ssr_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows["ssr"]:
        ssr_by_id[str(row["request_id"])].append(row)

    stream_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows["stream"]:
        stream_by_id[str(row["request_id"])].append(row)

    api_by_id: dict[str, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    api_by_cf_ray: dict[str, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(valid_rows["api"]):
        api_by_id[str(row["request_id"])].append((index, row))
        cf_ray = row.get("cf_ray")
        if isinstance(cf_ray, str) and _SSR_CORR_CF_RAY_RE.fullmatch(cf_ray):
            api_by_cf_ray[cf_ray].append((index, row))

    matched_nginx_ids: set[str] = set()
    matched_ssr_ids: set[str] = set()
    matched_stream_ids: set[str] = set()
    matched_api_indexes: set[int] = set()
    matched_api_parent_ids: set[str] = set()
    matched_api_associations = 0
    api_join_by_request_id = 0
    api_join_by_cf_ray = 0
    nginx_times: list[float] = []
    ssr_by_stage: dict[str, list[float]] = defaultdict(list)
    stream_by_milestone: dict[str, list[float]] = defaultdict(list)
    stream_milestone_missing: Counter[str] = Counter()
    stream_milestone_unknown = 0
    api_by_route: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    api_route_row_count: Counter[str] = Counter()
    api_metric_missing: dict[str, Counter[str]] = defaultdict(Counter)
    for request_id in selected_ids:
        parents = nginx_by_id.get(request_id, [])
        if len(parents) != 1:
            continue
        matched_nginx_ids.add(request_id)
        parent = parents[0]
        request_time = _ssr_corr_finite_nonnegative(parent.get("request_time_ms"))
        if request_time is not None:
            nginx_times.append(request_time)

        ssr_events = ssr_by_id.get(request_id, [])
        stream_events = stream_by_id.get(request_id, [])
        if stream_events:
            matched_stream_ids.add(request_id)
        if ssr_events or stream_events:
            matched_ssr_ids.add(request_id)
        for event in stream_events:
            milestone = event.get("stage")
            if not isinstance(milestone, str) or milestone not in _SSR_CORR_KNOWN_STREAM_MILESTONES:
                stream_milestone_unknown += 1
                continue
            offset = _ssr_corr_finite_nonnegative(event.get("elapsed_ms"))
            if offset is None:
                stream_milestone_missing[milestone] += 1
            else:
                stream_by_milestone[milestone].append(offset)
        if ssr_events:
            for event in ssr_events:
                stage = event.get("stage")
                duration = _ssr_corr_finite_nonnegative(event.get("duration_ms"))
                if isinstance(stage, str) and stage in _SSR_CORR_KNOWN_SSR_STAGES and duration is not None:
                    ssr_by_stage[stage].append(duration)

        # Require the client→SSR edge before accepting the SSR→API edge.
        if not ssr_events and not stream_events:
            continue

        api_events = api_by_id.get(request_id, [])
        if api_events:
            api_join_by_request_id += len(api_events)
        elif cf_ray_fallback_proven:
            cf_ray = parent.get("cf_ray")
            if isinstance(cf_ray, str) and _SSR_CORR_CF_RAY_RE.fullmatch(cf_ray):
                api_events = api_by_cf_ray.get(cf_ray, [])
                if api_events:
                    api_join_by_cf_ray += len(api_events)
        if api_events:
            matched_api_parent_ids.add(request_id)
            matched_api_associations += len(api_events)
        for api_index, event in api_events:
            matched_api_indexes.add(api_index)
            route = event.get("route_class")
            route_key = route if isinstance(route, str) and route in _SSR_CORR_KNOWN_API_ROUTES else "other"
            api_route_row_count[route_key] += 1
            for field in (
                "total_ms",
                "sql_ms",
                "max_sql_ms",
                "pool_checkout_wait_ms",
                "pool_connection_hold_ms",
                "compute_ms",
                "sql_count",
            ):
                value = _ssr_corr_finite_nonnegative(event.get(field))
                if (
                    field == "sql_count"
                    and value is not None
                    and not value.is_integer()
                ):
                    value = None
                if value is None:
                    api_metric_missing[route_key][field] += 1
                else:
                    api_by_route[route_key][field].append(value)

    source_input_rows = {name: len(rows) for name, rows in bounded.items()}
    all_sources_complete = not any(source_truncated.values())
    all_context_rows_valid = all(not counts for counts in source_rejections.values())
    full_sample_coverage = (
        sample_complete
        and all_sources_complete
        and all_context_rows_valid
        and len(matched_nginx_ids) == len(selected_ids)
        and len(matched_ssr_ids) == len(selected_ids)
        and len(matched_stream_ids) == len(selected_ids)
        and len(matched_api_parent_ids) == len(selected_ids)
        and not stream_milestone_missing
        and stream_milestone_unknown == 0
    )
    valid_id_coverage = (100.0 * valid_ids / submitted) if submitted else 0.0

    return {
        "schema_version": 1,
        "phase": phase,
        "cohort": {
            "submitted_html_requests": submitted,
            "valid_response_ids": valid_ids,
            "eligible_response_ids": eligible_ids,
            "missing_response_ids": missing_ids,
            "duplicate_response_ids": duplicate_ids,
            "malformed_response_ids": malformed_ids,
            "valid_response_id_coverage_percent": round(valid_id_coverage, 3),
            "selected_sample_limit": sample_limit,
            "selection_scheme": selection_scheme,
            "selected_sample_expected": expected_sample_count,
            "selected_sample_received": len(raw_sample),
            "selected_sample_valid": len(selected_ids),
            "selected_sample_malformed_ids": malformed_sample_ids,
            "selected_sample_ineligible_ids": ineligible_sample_ids,
            "selected_sample_duplicate_ids": duplicate_sample_ids,
            "selected_sample_complete": sample_complete,
        },
        "join": {
            "matched_nginx_parents": len(matched_nginx_ids),
            "matched_ssr_parents": len(matched_ssr_ids),
            "matched_stream_parents": len(matched_stream_ids),
            "matched_api_rows": matched_api_associations,
            "matched_api_by_request_id": api_join_by_request_id,
            "matched_api_by_cf_ray": api_join_by_cf_ray,
            "unmatched_selected_to_nginx": len(selected_ids - matched_nginx_ids),
            "unmatched_selected_to_ssr": len(matched_nginx_ids - matched_ssr_ids),
            "unmatched_selected_to_stream": len(matched_nginx_ids - matched_stream_ids),
            "unmatched_selected_to_api": len(matched_nginx_ids - matched_api_parent_ids),
            "orphan_ssr_rows": sum(
                1 for row in valid_rows["ssr"] if str(row["request_id"]) not in matched_nginx_ids
            ),
            "orphan_stream_rows": sum(
                1 for row in valid_rows["stream"] if str(row["request_id"]) not in matched_nginx_ids
            ),
            "orphan_api_rows": len(valid_rows["api"]) - len(matched_api_indexes),
            "duplicate_nginx_request_ids": duplicate_nginx_ids,
        },
        "timing_ms": {
            "nginx_request": _ssr_corr_metric(nginx_times),
            "ssr_stage": {
                stage: _ssr_corr_metric(values)
                for stage, values in sorted(ssr_by_stage.items())
            },
            "ssr_stream_milestone_elapsed_ms": {
                milestone: _ssr_corr_metric(stream_by_milestone.get(milestone, []))
                for milestone in sorted(_SSR_CORR_KNOWN_STREAM_MILESTONES)
            },
            "stream_milestone_missing_or_invalid": dict(
                sorted(stream_milestone_missing.items())
            ),
            "stream_milestone_unknown_rows": stream_milestone_unknown,
            "api_request_by_route": {
                route: {
                    field: (
                        _ssr_corr_scalar_metric(values)
                        if field == "sql_count"
                        else _ssr_corr_metric(values)
                    )
                    for field in (
                        "total_ms",
                        "sql_ms",
                        "max_sql_ms",
                        "pool_checkout_wait_ms",
                        "pool_connection_hold_ms",
                        "compute_ms",
                        "sql_count",
                    )
                    for values in (metrics.get(field, []),)
                }
                for route in sorted(set(api_route_row_count) | set(api_by_route))
                for metrics in (api_by_route.get(route, {}),)
            },
            "api_request_rows_by_route": dict(sorted(api_route_row_count.items())),
            "api_metric_missing_or_invalid_by_route": {
                route: dict(sorted(fields.items()))
                for route, fields in sorted(api_metric_missing.items())
            },
        },
        "coverage": {
            "complete": full_sample_coverage,
            "scope": "selected_sample_only",
            "selection_rank_proof": (
                "all_eligible_ids_selected"
                if eligible_ids <= sample_limit
                else "producer_asserted_not_verifiable_from_selected_ids"
            ),
            "full_cohort": (
                full_sample_coverage
                and len(selected_ids) == submitted
                and valid_ids == submitted
            ),
            "source_rows_truncated": source_truncated,
            "source_input_rows": source_input_rows,
            "source_rejections": {
                name: dict(sorted(counts.items()))
                for name, counts in source_rejections.items()
            },
            "cf_ray_fallback_enabled": bool(cf_ray_fallback_proven),
        },
    }

def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        request = _read_request(args.command)
        if request.command == "memory-watch":
            for sample in watch_capacity_memory(request):
                print(
                    "CAPACITY_MEMORY_GUARD "
                    + json.dumps(sample, sort_keys=True, separators=(",", ":")),
                    flush=True,
                )
            return 0
        result = prepare(request) if request.command == "prepare" else cleanup(request)
    except Exception as exc:
        known_class = type(exc).__name__
        safe_class = (
            known_class
            if known_class in {"PlanError", "OSError", "ValueError", "KeyError"}
            else "unexpected"
        )
        # This single closed token is the cross-boundary refusal protocol.
        # Never include the exception text or any request/process identity.
        print(f"CPU_DIAGNOSTIC_PLAN status=refused class={safe_class}")
        return 2
    print(
        "CPU_DIAGNOSTIC_PLAN "
        + json.dumps(result, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
