#!/usr/bin/env python3
"""Prepare or expire a paired, root-controlled CPU diagnostic plan.

The helper never signals a service, changes configuration, starts workload,
or restarts a unit.  It publishes a short-lived API/web plan only after both
live service identities and the active release are bound to the request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
from typing import Any
import uuid


PLATFORM_ROOT = Path("/opt/oldsparky/platform")
PLAN_DIRECTORY = Path("/run/oldsparky-platform")
PLAN_NAMES = {
    "api": "performance-diagnostic-plan.api.json",
    "web": "performance-diagnostic-plan.web.json",
}
MAX_PLAN_BYTES = 4096
MAX_REQUEST_BYTES = 4096
MAX_TOTAL_WINDOW_MS = 60_000
MAX_JOURNAL_BYTES = 1_048_576
MAX_JOURNAL_LINE_BYTES = 16_384
MAX_JOURNAL_ROWS = 128
JOURNAL_TIMEOUT_SECONDS = 3
MAX_SAMPLES_PROFILE = 1200
MIN_PLAN_LEAD_MS = 45_000
MAX_PLAN_LEAD_MS = 60_000
WORKLOAD = "authenticated_workspace_read_pair_v1"
OFF_WINDOW_MS = 20_000
IDLE_WINDOW_MS = 5_000
ON_WINDOW_MS = 20_000
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
        if (set(payload) != {"schema", "run_id"}
                or type(payload.get("schema")) is not int or payload.get("schema") != 1
                or not isinstance(payload.get("run_id"), str)
                or RUN_RE.fullmatch(payload["run_id"]) is None):
            raise PlanError("cleanup request fields are invalid")
        return argparse.Namespace(command="cleanup", run_id=payload["run_id"])
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
    # Values are inspected in-memory only for two fixed identity keys; the raw
    # environment and all unrelated entries are never printed or persisted.
    fd = os.open(f"/proc/{pid}/environ", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = bytearray()
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
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator and key in wanted:
            selected[key.decode("ascii")] = value
    raw[:] = b"\0" * len(raw)
    return selected


def _service_targets(service: str, fields: dict[str, str]) -> list[dict[str, object]]:
    unit = f"deadlock-{service}.service"
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
        or args.on_end_ms - args.on_start_ms != ON_WINDOW_MS
    ):
        raise PlanError("diagnostic windows do not match the fixed workload")
    if args.on_end_ms - args.off_start_ms > MAX_TOTAL_WINDOW_MS:
        raise PlanError("diagnostic window exceeds 60-second cap")
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
    if args.workload != WORKLOAD:
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
            "workload": WORKLOAD,
            "off_start_ms": args.off_start_ms,
            "off_end_ms": args.off_end_ms,
            "on_start_ms": args.on_start_ms,
            "on_end_ms": args.on_end_ms,
            "expires_at_ms": args.on_end_ms,
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
API_PROFILE_CATEGORIES = frozenset({
    "other", "serialization_validation", "orm_result", "db_driver",
    "async_event_loop", "crypto", "repo.get_tournament_workspace",
    "repo.get_tournament_workspace_by_slug", "repo.workspace_conditional_preflight",
    "repo.get_current_user", "repo.get_current_user_optional",
    "repo.get_server_request_correlation_headers", "repo.run_with_ssr_trace",
})
WEB_PROFILE_CATEGORIES = frozenset({
    "other", "web_framework", "serialization_validation", "async_event_loop",
    "http_client", "crypto", "repo.workspace_api_fetch", "repo.workspace_page",
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


def _usage_summary(
    plans: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate exact target/phase journal events; expose aggregates only."""

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
    invalid_event = False
    invalid_profile = False
    expected_units = {"api": "deadlock-api.service", "web": "deadlock-web.service"}
    expected_cgroups = {service: f"/system.slice/{unit}" for service, unit in expected_units.items()}
    run_id = str(plans["api"]["run_id"])
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
        if not is_usage and not is_profile:
            continue
        usage_match = USAGE_MESSAGE_RE.fullmatch(message) if is_usage else None
        profile_api_match = PROFILE_API_MESSAGE_RE.fullmatch(message) if is_profile else None
        profile_web_match = PROFILE_WEB_MESSAGE_RE.fullmatch(message) if is_profile else None
        if is_usage and usage_match is None:
            invalid_event = True
            continue
        if is_profile and profile_api_match is None and profile_web_match is None:
            invalid_profile = True
            continue
        if usage_match is not None:
            service, event_run, phase = usage_match.group(1), usage_match.group(2), usage_match.group(3)
        elif profile_api_match is not None:
            service, event_run, phase = "api", profile_api_match.group(1), "on"
        else:
            assert profile_web_match is not None
            service, event_run, phase = "web", profile_web_match.group(1), "on"
        if event_run != run_id:
            continue
        try:
            pid = int(record.get("_PID", ""))
            timestamp_us = int(record.get("__REALTIME_TIMESTAMP", ""))
            invocation = str(record.get("_SYSTEMD_INVOCATION_ID", "")).lower()
            unit = str(record.get("_SYSTEMD_UNIT", ""))
            cgroup = str(record.get("_SYSTEMD_CGROUP", ""))
            start_ticks, expected_invocation = targets[service][pid]
            min_timestamp = (
                int(plans[service]["on_start_ms"]) * 1000 - 1_000_000
                if is_profile else int(plans[service]["off_start_ms"]) * 1000 - 1_000_000
            )
            if (timestamp_us < min_timestamp
                    or timestamp_us > int(plans[service]["on_end_ms"]) * 1000 + 5_000_000
                    or invocation != expected_invocation or unit != expected_units[service]
                    or cgroup != expected_cgroups[service]
                    or invocation != str(plans[service]["targets"][0]["invocation_id"]).lower()
                    or _process_start_ticks(pid) != start_ticks):
                if is_profile:
                    invalid_profile = True
                else:
                    invalid_event = True
                continue
            if usage_match is not None:
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
                end_lag_ms = start_lag_ms + elapsed_ms - ON_WINDOW_MS
                if (categories is None or total_cpu_us < 0 or total_cpu_us > 60_000_000
                        or not 0 <= start_lag_ms <= 250
                        or not 19_750 <= elapsed_ms <= 20_500
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
            if is_profile:
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
                    and 19_750 <= item["window_ms"] <= 20_500
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
            len(selected) == expected_count
            and event_count == expected_count
            and observed_count == expected_count
            and duplicate_count == 0
            and all(item["timer"] == ("thread_cpu" if service == "api" else "v8_cpu")
                    for item in flat_profiles)
            and all(0 <= item["start_lag_ms"] <= 250 for item in flat_profiles)
            and all(19_750 <= item["elapsed_ms"] <= 20_500 for item in flat_profiles)
            and all(-250 <= item["end_lag_ms"] <= 250 for item in flat_profiles)
        )
        if event_count < expected_count and profile_reason == "none":
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
    return {
        "usage_status": "complete" if all_complete else "incomplete",
        "usage_reason": reason,
        "usage_rows": rows,
        "profile_status": "complete" if profile_complete else "incomplete",
        "profile_reason": profile_reason,
        "profile_rows": profile_rows,
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
                    or payload.get("workload") != WORKLOAD
                    or payload.get("expires_at_ms") != payload.get("on_end_ms")
                    or any(type(payload.get(key)) is not int for key in (
                        "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms", "expires_at_ms"
                    ))):
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
                            "total_cpu_us": None,
                            "sample_count": None,
                            "categories": [],
                        }
                        for service, expected in (("api", 2), ("web", 1))
                    ],
                }
        else:
            usage = {
                "usage_status": "unavailable",
                "usage_reason": "missing",
                "usage_rows": [],
                "profile_status": "unavailable",
                "profile_reason": "missing",
                "profile_rows": [],
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
    return {
        "status": "expired_plans_removed",
        "service_count": len(removed),
        **usage,
    }


def _parser() -> argparse.ArgumentParser:
    class ClosedArgumentParser(argparse.ArgumentParser):
        def error(self, _message: str) -> None:
            raise PlanError("command shape is invalid")

    parser = ClosedArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare-stdin")
    subparsers.add_parser("cleanup-stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        request = _read_request(args.command)
        result = prepare(request) if request.command == "prepare" else cleanup(request)
    except Exception as exc:
        known_class = type(exc).__name__
        safe_class = (
            known_class
            if known_class in {"PlanError", "OSError", "ValueError", "KeyError"}
            else "unexpected"
        )
        print(f"CPU_DIAGNOSTIC_PLAN status=refused class={safe_class}", file=sys.stderr)
        return 2
    print(
        "CPU_DIAGNOSTIC_PLAN "
        + json.dumps(result, sort_keys=True, separators=(",", ":"))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
