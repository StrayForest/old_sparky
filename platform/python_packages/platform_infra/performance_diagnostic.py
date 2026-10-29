"""Default-off, process-bound CPU diagnostics for an approved read workload.

The only arming authority is a short-lived, root-owned plan written by the
trusted controller.  This module deliberately keeps the plan immutable and
uses process-local one-use state; workers never claim, unlink, or rewrite it.
"""

from __future__ import annotations

import cProfile
from dataclasses import dataclass
import grp
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import stat
import time
from typing import Any


logger = logging.getLogger("platform.performance.diagnostic")
PLAN_DIRECTORY = Path("/run/oldsparky-platform")
PLAN_PATHS = {
    "api": PLAN_DIRECTORY / "performance-diagnostic-plan.api.json",
    "web": PLAN_DIRECTORY / "performance-diagnostic-plan.web.json",
}
MAX_PLAN_BYTES = 4096
MAX_CAPTURE_MS = 60_000
MAX_PLAN_WINDOW_MS = 160_000
JOURNAL_SOCKET_PATH = "/run/systemd/journal/socket"
MAX_CPU_DIAGNOSTIC_DATAGRAM_BYTES = 8192
WORKLOAD = "authenticated_workspace_read_pair_v1"
WORKLOAD_SSR_CAPACITY = "authenticated_workspace_read_capacity_v1"
WORKLOAD_HTML_SSR_CAPACITY = "authenticated_html_ssr_capacity_v1"
WORKLOAD_HTML_SSR_CAPACITY_120S = "authenticated_html_ssr_capacity_120s_v1"
WORKLOAD_HTML_SSR_CPU_PROFILE_20S = "authenticated_html_ssr_cpu_profile_20s_v1"
SUPPORTED_WORKLOADS = frozenset({
    WORKLOAD,
    WORKLOAD_SSR_CAPACITY,
    WORKLOAD_HTML_SSR_CAPACITY,
    WORKLOAD_HTML_SSR_CAPACITY_120S,
    WORKLOAD_HTML_SSR_CPU_PROFILE_20S,
})
PROFILE_WORKLOADS = frozenset({WORKLOAD, WORKLOAD_HTML_SSR_CPU_PROFILE_20S})
CPU_USAGE_PREARM_WORKLOADS = frozenset({
    WORKLOAD_HTML_SSR_CAPACITY,
    WORKLOAD_HTML_SSR_CAPACITY_120S,
})
HTML_REQUEST_PERF_WORKLOADS = frozenset({
    WORKLOAD_HTML_SSR_CAPACITY_120S,
    WORKLOAD_HTML_SSR_CPU_PROFILE_20S,
})
HTML_REQUEST_PERF_SAMPLE_SCHEME = "sha256-runid-requestid-1of8-v1"
OFF_WINDOW_MS = 20_000
IDLE_WINDOW_MS = 5_000
ON_WINDOW_MS = 20_000
ON_WINDOW_MS_BY_WORKLOAD = {workload: ON_WINDOW_MS for workload in SUPPORTED_WORKLOADS}
ON_WINDOW_MS_BY_WORKLOAD[WORKLOAD_HTML_SSR_CAPACITY_120S] = 120_000
PLAN_EXPIRY_GRACE_MS = {workload: 0 for workload in SUPPORTED_WORKLOADS}
PLAN_EXPIRY_GRACE_MS[WORKLOAD_HTML_SSR_CAPACITY_120S] = 15_000
PLAN_EXPIRY_GRACE_MS[WORKLOAD_HTML_SSR_CPU_PROFILE_20S] = 15_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^[0-9a-f]{32}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
INVOCATION_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f-]{36})$")
CPU_CGROUP_RE = re.compile(r"^0::(?P<path>/[A-Za-z0-9._/-]{0,512})$")
CPU_WINDOW_TOLERANCE_MS = 250
API_CPU_PREARM_LEAD_MS = 52_000


def _warn_cpu_diagnostic_journal_failure(reason: str) -> None:
    if reason not in {"message_rejected", "transport_failed"}:
        reason = "transport_failed"
    logging.getLogger("platform.performance.transport").warning(
        "API CPU diagnostic journal delivery failed reason=%s", reason,
    )


def _emit_cpu_diagnostic_message(message: str) -> bool:
    """Send one bounded API diagnostic marker with the worker's journal PID."""

    if (type(message) is not str
            or not message.startswith((
                "cpu_diagnostic_usage service=api ",
                "cpu_diagnostic_complete service=api ",
            ))):
        _warn_cpu_diagnostic_journal_failure("message_rejected")
        return False
    if len(message) > MAX_CPU_DIAGNOSTIC_DATAGRAM_BYTES:
        _warn_cpu_diagnostic_journal_failure("message_rejected")
        return False
    if any(not 0x20 <= ord(character) <= 0x7E for character in message):
        _warn_cpu_diagnostic_journal_failure("message_rejected")
        return False
    try:
        message.encode("ascii")
    except UnicodeEncodeError:
        _warn_cpu_diagnostic_journal_failure("message_rejected")
        return False
    wrapped_message = json.dumps(
        {"message": message}, ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    payload = (
        b"MESSAGE=" + wrapped_message + b"\n"
        b"PRIORITY=6\n"
        b"SYSLOG_IDENTIFIER=old-sparky-platform-api\n"
    )
    if len(payload) > MAX_CPU_DIAGNOSTIC_DATAGRAM_BYTES:
        _warn_cpu_diagnostic_journal_failure("message_rejected")
        return False

    sender: socket.socket | None = None
    try:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        sent = sender.sendto(payload, JOURNAL_SOCKET_PATH)
        if sent != len(payload):
            _warn_cpu_diagnostic_journal_failure("transport_failed")
            return False
        return True
    except OSError:
        _warn_cpu_diagnostic_journal_failure("transport_failed")
        return False
    finally:
        if sender is not None:
            try:
                sender.close()
            except OSError:
                pass


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class ProcessTarget:
    service: str
    pid: int
    start_ticks: int
    invocation_id: str


@dataclass(frozen=True, slots=True)
class DiagnosticPlan:
    run_id: str
    source_sha: str
    release_slug: str
    workload: str
    off_start_ms: int
    off_end_ms: int
    on_start_ms: int
    on_end_ms: int
    expires_at_ms: int
    targets: tuple[ProcessTarget, ...]

    def phase_at(self, timestamp_ms: int) -> str | None:
        if self.off_start_ms <= timestamp_ms < self.off_end_ms:
            return "off"
        if self.on_start_ms <= timestamp_ms < self.on_end_ms:
            return "on"
        return None


def _plan_expiry_ms(workload: str, on_end_ms: int) -> int:
    return on_end_ms + PLAN_EXPIRY_GRACE_MS[workload]


def parse_plan_payload(raw: bytes, *, expected_service: str) -> DiagnosticPlan:
    """Validate the exact closed plan schema and canonical JSON encoding."""

    if expected_service not in PLAN_PATHS or not raw or len(raw) > MAX_PLAN_BYTES:
        raise ValueError("invalid plan service or size")
    try:
        payload = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey) as exc:
        raise ValueError("invalid plan JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "run_id",
        "source_sha",
        "release_slug",
        "workload",
        "off_start_ms",
        "off_end_ms",
        "on_start_ms",
        "on_end_ms",
        "expires_at_ms",
        "targets",
    }:
        raise ValueError("invalid plan fields")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if raw != canonical:
        raise ValueError("noncanonical plan")
    if payload["schema"] != 1 or isinstance(payload["schema"], bool):
        raise ValueError("invalid plan schema")
    run_id = payload["run_id"]
    source_sha = payload["source_sha"]
    release_slug = payload["release_slug"]
    if not isinstance(run_id, str) or RUN_RE.fullmatch(run_id) is None:
        raise ValueError("invalid run id")
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("invalid source identity")
    if not isinstance(release_slug, str) or SLUG_RE.fullmatch(release_slug) is None:
        raise ValueError("invalid release identity")
    if (not isinstance(payload["workload"], str)
            or payload["workload"] not in SUPPORTED_WORKLOADS):
        raise ValueError("invalid workload")

    times: list[int] = []
    for name in (
        "off_start_ms", "off_end_ms", "on_start_ms", "on_end_ms", "expires_at_ms"
    ):
        value = payload[name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("invalid window")
        times.append(value)
    off_start_ms, off_end_ms, on_start_ms, on_end_ms, expires_ms = times
    workload = payload["workload"]
    if (not off_start_ms < off_end_ms <= on_start_ms < on_end_ms
            or off_end_ms - off_start_ms != OFF_WINDOW_MS
            or on_start_ms - off_end_ms != IDLE_WINDOW_MS
            or on_end_ms - on_start_ms != ON_WINDOW_MS_BY_WORKLOAD[workload]
            or expires_ms != _plan_expiry_ms(workload, on_end_ms)
            or expires_ms - off_start_ms > MAX_PLAN_WINDOW_MS
            or (workload in PROFILE_WORKLOADS and on_end_ms - on_start_ms > MAX_CAPTURE_MS)):
        raise ValueError("window exceeds capture bound")

    raw_targets = payload["targets"]
    if not isinstance(raw_targets, list) or not raw_targets or len(raw_targets) > 32:
        raise ValueError("invalid target list")
    targets: list[ProcessTarget] = []
    seen: set[tuple[str, int, int, str]] = set()
    for item in raw_targets:
        if not isinstance(item, dict) or set(item) != {
            "service", "pid", "start_ticks", "invocation_id"
        }:
            raise ValueError("invalid target fields")
        service, pid, start_ticks, invocation_id = (
            item["service"], item["pid"], item["start_ticks"], item["invocation_id"]
        )
        if service != expected_service:
            raise ValueError("cross-service target")
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                or not isinstance(start_ticks, int) or isinstance(start_ticks, bool)
                or start_ticks <= 0):
            raise ValueError("invalid process identity")
        if not isinstance(invocation_id, str) or INVOCATION_RE.fullmatch(invocation_id) is None:
            raise ValueError("invalid invocation identity")
        key = (service, pid, start_ticks, invocation_id)
        if key in seen:
            raise ValueError("duplicate target")
        seen.add(key)
        targets.append(ProcessTarget(*key))
    return DiagnosticPlan(
        run_id=run_id,
        source_sha=source_sha,
        release_slug=release_slug,
        workload=payload["workload"],
        off_start_ms=off_start_ms,
        off_end_ms=off_end_ms,
        on_start_ms=on_start_ms,
        on_end_ms=on_end_ms,
        expires_at_ms=expires_ms,
        targets=tuple(targets),
    )


def _read_current_start_ticks() -> int | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            "/proc/self/stat",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        raw = os.read(descriptor, 4096).decode("ascii")
        close = raw.rfind(")")
        columns = raw[close + 2 :].split()
        if close < 0 or len(columns) <= 19:
            return None
        value = int(columns[19])
    except (OSError, UnicodeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return value if value > 0 else None


def _effective_cpu_capacity() -> float | None:
    """Return this service cgroup's bounded CPU capacity in core units."""

    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = os.cpu_count() or 0
    if not 1 <= affinity <= 4096:
        return None
    try:
        cgroup_lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    relative: str | None = None
    for line in cgroup_lines:
        match = CPU_CGROUP_RE.fullmatch(line)
        if match is not None:
            relative = match.group("path").lstrip("/")
            break
    if relative is None:
        return None
    components = relative.split("/") if relative else []
    if any(part in {"", ".", ".."} for part in components):
        return None
    cgroup_root = Path("/sys/fs/cgroup")
    cgroup_dir = cgroup_root.joinpath(*components) if components else cgroup_root
    if cgroup_dir != cgroup_root and cgroup_root not in cgroup_dir.parents:
        return None
    capacity = float(affinity)
    chain = [cgroup_dir, *cgroup_dir.parents]
    if cgroup_root not in chain:
        return None
    chain = chain[: chain.index(cgroup_root) + 1]
    for directory in chain:
        try:
            cpu_max = (directory / "cpu.max").read_text(encoding="ascii")
        except FileNotFoundError:
            # A missing controller file means this level has no CPU quota.
            continue
        except (OSError, UnicodeError):
            return None
        if len(cpu_max) > 128:
            return None
        fields = cpu_max.split()
        if len(fields) != 2 or not fields[1].isdecimal() or int(fields[1]) <= 0:
            return None
        if fields[0] == "max":
            continue
        if not fields[0].isdecimal() or int(fields[0]) <= 0:
            return None
        quota_capacity = int(fields[0]) / int(fields[1])
        if not 0 < quota_capacity <= 4096:
            return None
        capacity = min(capacity, quota_capacity)
    return capacity


def _start_cpu_usage_window(plan: DiagnosticPlan, phase: str, loop: Any) -> None:
    """Measure process CPU over one exact phase from its first bound request."""

    key = (plan.run_id, phase)
    if key in _usage_windows_started or key in _usage_windows_scheduled:
        return
    _usage_windows_started.add(key)
    start_ms_target = plan.off_start_ms if phase == "off" else plan.on_start_ms
    end_ms_target = plan.off_end_ms if phase == "off" else plan.on_end_ms
    expected_window_ms = end_ms_target - start_ms_target
    start_ms = time.time_ns() // 1_000_000
    start_lag_ms = start_ms - start_ms_target
    start_cpu_ns = time.process_time_ns()
    start_monotonic_ns = time.monotonic_ns()
    capacity = _effective_cpu_capacity()

    def finish() -> None:
        end_ms = time.time_ns() // 1_000_000
        end_lag_ms = end_ms - end_ms_target
        fresh_plan = _read_process_plan_uncached(
            "api",
            now_ms=end_ms,
            phase_at_ms=end_ms_target - 1,
            expiry_tolerance_ms=CPU_WINDOW_TOLERANCE_MS,
            pid=None,
            start_ticks=None,
            invocation_id=None,
            release_identity=None,
        )
        if (fresh_plan != plan
                or end_ms > end_ms_target + CPU_WINDOW_TOLERANCE_MS
                or end_ms > plan.expires_at_ms + CPU_WINDOW_TOLERANCE_MS):
            return
        window_ms = max(0, (time.monotonic_ns() - start_monotonic_ns) // 1_000_000)
        cpu_ns = max(0, time.process_time_ns() - start_cpu_ns)
        timing_complete = bool(
            0 <= start_lag_ms <= CPU_WINDOW_TOLERANCE_MS
            and abs(end_lag_ms) <= CPU_WINDOW_TOLERANCE_MS
            and expected_window_ms - CPU_WINDOW_TOLERANCE_MS
            <= window_ms
            <= expected_window_ms + 2 * CPU_WINDOW_TOLERANCE_MS
        )
        _emit_cpu_diagnostic_message(
            "cpu_diagnostic_usage service=api run_id=%s phase=%s window_ms=%s "
            "cpu_ns=%s cpu_capacity_cpus=%s start_lag_ms=%s end_lag_ms=%s "
            "timing_complete=%s" % (
                plan.run_id,
                phase,
                window_ms,
                cpu_ns,
                f"{capacity:.6f}" if capacity is not None else "unknown",
                start_lag_ms,
                end_lag_ms,
                str(timing_complete).lower(),
            )
        )

    delay_seconds = max(0.0, (end_ms_target - start_ms) / 1000)
    loop.call_later(delay_seconds, finish)


def _header_from_scope(scope: dict[str, Any], name: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() == name:
            value = raw_value.decode("ascii", errors="ignore").strip()
            return value[:80] if value else None
    return None


def _read_release_identity() -> tuple[str, str] | None:
    """Read source/release identity from this process' immutable release root."""

    # Release staging strips the repository's `platform/` prefix, so this
    # module lives under <release>/python_packages/....
    release_file = Path(__file__).resolve().parents[2] / "RELEASE.json"
    try:
        descriptor = os.open(
            release_file,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1:
                return None
            if before.st_size <= 0 or before.st_size > 16_384:
                return None
            raw = os.read(descriptor, 16_385)
            after = os.fstat(descriptor)
            if (len(raw) != before.st_size or before.st_dev != after.st_dev
                    or before.st_ino != after.st_ino or before.st_size != after.st_size):
                return None
        finally:
            os.close(descriptor)
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source_sha = payload.get("source_git_commit")
    slug = payload.get("release_slug")
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        return None
    if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None:
        return None
    return source_sha, slug


_plan_cache: dict[str, tuple[float, DiagnosticPlan | None]] = {}
PLAN_CACHE_SECONDS = 0.1


def read_process_plan(
    service: str,
    *,
    now_ms: int | None = None,
    allow_html_request_drain: bool = False,
    pid: int | None = None,
    start_ticks: int | None = None,
    invocation_id: str | None = None,
    release_identity: tuple[str, str] | None = None,
) -> DiagnosticPlan | None:
    """Read and bind this process' fixed service-scoped root plan.

    Any missing, malformed, stale, mismatched, replaced, or insecure input is
    a no-op. The caller must still enforce process-local one-use.
    """

    if service not in PLAN_PATHS:
        return None
    cache_now = time.monotonic()
    cached = _plan_cache.get(service)
    if cached is not None and cache_now - cached[0] < PLAN_CACHE_SECONDS:
        plan = cached[1]
    else:
        plan = _read_process_plan_uncached(
            service,
            now_ms=now_ms,
            pid=pid,
            start_ticks=start_ticks,
            invocation_id=invocation_id,
            release_identity=release_identity,
        )
        _plan_cache[service] = (cache_now, plan)
    current_ms = int(time.time_ns() // 1_000_000) if now_ms is None else now_ms
    if plan is None:
        return None
    if plan.phase_at(current_ms) is None:
        if (not allow_html_request_drain
                or plan.workload not in HTML_REQUEST_PERF_WORKLOADS
                or current_ms > plan.expires_at_ms):
            return None
        # Revalidate the same root-owned process-bound plan while evaluating
        # its fixed ON phase. The request's trusted parent-start timestamp is
        # checked by the caller; this only permits bounded plan lookup during
        # the typed plan's post-ON logging grace.
        drained_plan = _read_process_plan_uncached(
            service, now_ms=current_ms, pid=pid, start_ticks=start_ticks,
            invocation_id=invocation_id, release_identity=release_identity,
            phase_at_ms=plan.on_start_ms,
        )
        if drained_plan != plan:
            return None
    return plan


def authorize_html_request_perf_capture(
    scope: dict[str, Any],
    *,
    plan: DiagnosticPlan | None = None,
    now_ms: int | None = None,
) -> tuple[DiagnosticPlan | None, str | None, bool]:
    """Authorize bounded request_perf capture for one validated HTML call.

    The request headers only select a correlation candidate. Capture is enabled
    only when the root-owned, service-bound plan independently authorizes the
    matching run and exact ON phase.
    """

    if scope.get("type") != "http" or scope.get("method") != "GET":
        return None, None, False
    path = scope.get("path")
    if not isinstance(path, str):
        return None, None, False
    is_auth_bootstrap = path == "/api/v1/auth/bootstrap"
    pieces = path.strip("/").split("/")
    is_workspace = (
        len(pieces) == 5
        and pieces[0] == "api"
        and pieces[1] == "v1"
        and pieces[2] == "tournaments"
        and bool(SLUG_RE.fullmatch(pieces[3]))
        and pieces[4] == "workspace"
    )
    if not (is_auth_bootstrap or is_workspace):
        return None, None, False

    trace_run_id = _header_from_scope(scope, b"x-platform-ssr-trace")
    ssr_request_id = _header_from_scope(scope, b"x-platform-ssr-request-id")
    request_id = _header_from_scope(scope, b"x-request-id")
    if (trace_run_id is None or RUN_RE.fullmatch(trace_run_id) is None
            or ssr_request_id is None or RUN_RE.fullmatch(ssr_request_id) is None
            or request_id != ssr_request_id):
        return None, None, False

    if hashlib.sha256(f"{trace_run_id}:{ssr_request_id}".encode("ascii")).digest()[0] & 7:
        return None, None, False

    current_ms = int(time.time_ns() // 1_000_000) if now_ms is None else now_ms
    parent_start_raw = _header_from_scope(scope, b"x-platform-ssr-request-start-ms")
    if (parent_start_raw is None
            or re.fullmatch(r"(?:0|[1-9][0-9]{0,12})", parent_start_raw) is None):
        return None, None, False
    parent_start_ms = int(parent_start_raw)
    authorized_plan = plan
    if authorized_plan is None or authorized_plan.run_id != trace_run_id:
        authorized_plan = read_process_plan(
            "api", now_ms=current_ms, allow_html_request_drain=True,
        )
    if (authorized_plan is None
            or authorized_plan.run_id != trace_run_id
            or authorized_plan.workload not in HTML_REQUEST_PERF_WORKLOADS
            or not authorized_plan.on_start_ms <= parent_start_ms < authorized_plan.on_end_ms
            or not (
                authorized_plan.phase_at(current_ms) == "on"
                or authorized_plan.on_end_ms <= current_ms <= authorized_plan.expires_at_ms
            )
            or current_ms > authorized_plan.expires_at_ms):
        return None, None, False
    return authorized_plan, "on", True


def _read_process_plan_uncached(
    service: str,
    *,
    now_ms: int | None,
    pid: int | None,
    start_ticks: int | None,
    invocation_id: str | None,
    release_identity: tuple[str, str] | None,
    allow_prearm: bool = False,
    phase_at_ms: int | None = None,
    expiry_tolerance_ms: int = 0,
) -> DiagnosticPlan | None:
    if service not in PLAN_PATHS:
        return None
    path = PLAN_PATHS[service]
    try:
        expected_gid = grp.getgrnam(f"oldsparky-{service}").gr_gid
    except KeyError:
        return None
    directory_fd = file_fd = None
    try:
        path_only_flag = getattr(os, "O_PATH", None)
        if path_only_flag is None:
            return None
        directory_fd = os.open(
            PLAN_DIRECTORY,
            path_only_flag | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        directory_stat = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != 0
                or directory_stat.st_gid != 0
                or stat.S_IMODE(directory_stat.st_mode) != 0o711):
            return None
        file_fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0
                or before.st_gid != expected_gid or stat.S_IMODE(before.st_mode) != 0o440
                or before.st_nlink != 1
                or before.st_size <= 0 or before.st_size > MAX_PLAN_BYTES):
            return None
        raw = os.read(file_fd, MAX_PLAN_BYTES + 1)
        after = os.fstat(file_fd)
        path_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (len(raw) != before.st_size or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino or before.st_size != after.st_size
                or path_after.st_dev != before.st_dev or path_after.st_ino != before.st_ino):
            return None
    except OSError:
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)
    try:
        plan = parse_plan_payload(raw, expected_service=service)
    except ValueError:
        return None
    current_ms = int(time.time_ns() // 1_000_000) if now_ms is None else now_ms
    phase_ms = current_ms if phase_at_ms is None else phase_at_ms
    phase = plan.phase_at(phase_ms)
    if allow_prearm:
        lead_ms = plan.off_start_ms - current_ms
        if (plan.workload not in CPU_USAGE_PREARM_WORKLOADS or phase is not None
                or not 0 < lead_ms <= API_CPU_PREARM_LEAD_MS):
            return None
    elif phase is None:
        return None
    # The only nonzero expiry allowance is the finalization of an already
    # started window at its exact end edge; starts and prearms stay strict.
    if (type(expiry_tolerance_ms) is not int or expiry_tolerance_ms < 0
            or (expiry_tolerance_ms and phase_at_ms is None)
            or current_ms > plan.expires_at_ms + expiry_tolerance_ms
            or plan.expires_at_ms - plan.off_start_ms > MAX_PLAN_WINDOW_MS):
        return None
    expected_pid = os.getpid() if pid is None else pid
    expected_ticks = _read_current_start_ticks() if start_ticks is None else start_ticks
    expected_invocation = os.environ.get("INVOCATION_ID") if invocation_id is None else invocation_id
    if expected_ticks is None or not expected_invocation:
        return None
    if os.environ.get("PLATFORM_RUNTIME_SERVICE") != service:
        return None
    if not any(
        target.pid == expected_pid
        and target.start_ticks == expected_ticks
        and target.invocation_id == expected_invocation
        for target in plan.targets
    ):
        return None
    current_release = _read_release_identity() if release_identity is None else release_identity
    if current_release != (plan.source_sha, plan.release_slug):
        return None
    return plan


def _read_process_plan_for_cpu_prearm(*, now_ms: int | None = None) -> DiagnosticPlan | None:
    """Uncached, process-bound future plan read for HTML CPU edge scheduling."""

    current_ms = int(time.time_ns() // 1_000_000) if now_ms is None else now_ms
    return _read_process_plan_uncached(
        "api", now_ms=current_ms, pid=None, start_ticks=None,
        invocation_id=None, release_identity=None, allow_prearm=True,
    )


def _same_cpu_plan(left: DiagnosticPlan | None, right: DiagnosticPlan) -> bool:
    return left == right


def _schedule_cpu_usage_window(
    plan: DiagnosticPlan, phase: str, loop: Any, *, now_ms: int | None = None,
) -> None:
    """Schedule one exact usage window after a root-plan-authorized prearm."""

    key = (plan.run_id, phase)
    if key in _usage_windows_started or key in _usage_windows_scheduled:
        return
    start_ms = plan.off_start_ms if phase == "off" else plan.on_start_ms
    current_ms = int(time.time_ns() // 1_000_000) if now_ms is None else now_ms
    _usage_windows_scheduled.add(key)

    def start_at_edge() -> None:
        actual_ms = time.time_ns() // 1_000_000
        if actual_ms < start_ms:
            fresh_plan = _read_process_plan_uncached(
                "api", now_ms=actual_ms, phase_at_ms=start_ms,
                pid=None, start_ticks=None,
                invocation_id=None, release_identity=None,
            )
            if _same_cpu_plan(fresh_plan, plan):
                loop.call_later((start_ms - actual_ms) / 1000, start_at_edge)
                return
        fresh_plan = _read_process_plan_uncached(
            "api", now_ms=actual_ms, pid=None, start_ticks=None,
            invocation_id=None, release_identity=None,
        )
        if (not _same_cpu_plan(fresh_plan, plan)
                or fresh_plan.phase_at(actual_ms) != phase
                or actual_ms < start_ms
                or actual_ms > fresh_plan.expires_at_ms):
            _usage_windows_scheduled.discard(key)
            return
        _usage_windows_scheduled.discard(key)
        _start_cpu_usage_window(fresh_plan, phase, loop)

    loop.call_later(max(0.0, (start_ms - current_ms) / 1000), start_at_edge)


def _prearm_cpu_usage_windows(plan: DiagnosticPlan, loop: Any, *, now_ms: int | None = None) -> None:
    """Arm OFF and ON process CPU windows without sampling before their edges."""

    current_ms = int(time.time_ns() // 1_000_000) if now_ms is None else now_ms
    lead_ms = plan.off_start_ms - current_ms
    if (plan.workload not in CPU_USAGE_PREARM_WORKLOADS
            or not 0 < lead_ms <= API_CPU_PREARM_LEAD_MS):
        return
    _schedule_cpu_usage_window(plan, "off", loop, now_ms=current_ms)
    _schedule_cpu_usage_window(plan, "on", loop, now_ms=current_ms)


_active_profile: "ApiCpuDiagnostic | None" = None
_consumed_run_ids: set[str] = set()
_usage_windows_started: set[tuple[str, str]] = set()
_usage_windows_scheduled: set[tuple[str, str]] = set()


# Only these repository call labels may leave the process. Everything else is
# reduced to one of the fixed runtime categories or `other`.
API_FUNCTION_ALLOWLIST = frozenset({
    "get_auth_bootstrap",
    "build_auth_bootstrap",
    "get_tournament_workspace",
    "get_tournament_workspace_by_slug",
    "workspace_conditional_preflight",
    "ensure_workspace_private_membership",
    "ensure_tournament_workspace_visible",
    "build_tournament_workspace_detail_bracket_response",
    "build_tournament_workspace_bracket_summary_response",
    "_workspace_response_etag",
    "_serialized_model_response",
    "get_current_user",
    "get_current_user_optional",
    "get_server_request_correlation_headers",
    "run_with_ssr_trace",
})
RUNTIME_PREFIX_CATEGORIES = (
    ("/sqlalchemy/", "orm_result"),
    ("/asyncpg/", "db_driver"),
    ("/psycopg/", "db_driver"),
    ("/asyncio/", "async_event_loop"),
    ("/uvloop/", "async_event_loop"),
    ("/fastapi/", "serialization_validation"),
    ("/pydantic/", "serialization_validation"),
    ("/cryptography/", "crypto"),
    ("/bcrypt/", "crypto"),
)


def _profile_summary(profile: cProfile.Profile) -> tuple[list[tuple[str, int, int]], int]:
    import pstats

    stats = pstats.Stats(profile).stats
    totals: dict[str, list[int]] = {}
    all_self_us = 0
    platform_root = str(Path(__file__).resolve().parents[2])
    for (filename, _line, function), values in stats.items():
        self_seconds = max(0.0, float(values[2]))
        self_us = int(self_seconds * 1_000_000)
        all_self_us += self_us
        normalized = filename.replace("\\", "/")
        label = "other"
        if normalized.startswith(platform_root + "/"):
            relative = normalized[len(platform_root) + 1 :]
            if function in API_FUNCTION_ALLOWLIST and relative.startswith((
                "apps/platform_api/", "python_packages/platform_infra/"
            )):
                label = f"repo.{function}"
        else:
            for prefix, category in RUNTIME_PREFIX_CATEGORIES:
                if prefix in normalized:
                    label = category
                    break
        entry = totals.setdefault(label, [0, 0])
        entry[0] += self_us
        entry[1] += int(values[1])
    ordered = sorted(totals.items(), key=lambda item: (-item[1][0], item[0]))[:10]
    return [(name, values[0], values[1]) for name, values in ordered], all_self_us


class ApiCpuDiagnostic:
    """One process-local cProfile capture using the event-loop thread CPU clock."""

    def __init__(self, plan: DiagnosticPlan) -> None:
        self.plan = plan
        self.profile: cProfile.Profile | None = None
        self.loop: Any = None
        self.stop_handle: Any = None
        self.started_monotonic: float | None = None
        self.start_lag_ms = 0
        self.stopped = False

    @classmethod
    def activate_for_scope(
        cls,
        scope: dict[str, Any],
    ) -> tuple[DiagnosticPlan | None, str | None]:
        global _active_profile
        if scope.get("method") != "GET":
            return None, None
        path = str(scope.get("path") or "")
        pieces = path.strip("/").split("/")
        if (len(pieces) != 5 or pieces[0] != "api" or pieces[1] != "v1"
                or pieces[2] != "tournaments" or not pieces[3]
                or pieces[4] != "workspace"):
            return None, None
        trace_run_id = _header_from_scope(scope, b"x-platform-ssr-trace")
        if trace_run_id is None or RUN_RE.fullmatch(trace_run_id) is None:
            return None, None
        plan = read_process_plan("api")
        if plan is None:
            prearm_plan = _read_process_plan_for_cpu_prearm()
            if prearm_plan is None or trace_run_id != prearm_plan.run_id:
                return None, None
            try:
                import asyncio

                loop = asyncio.get_running_loop()
            except RuntimeError:
                return None, None
            _prearm_cpu_usage_windows(prearm_plan, loop)
            # Prearming records no request phase or profiler context; only
            # callbacks at the immutable plan edges emit usage samples.
            return None, None
        if trace_run_id != plan.run_id:
            return None, None
        now_ms = int(time.time_ns() // 1_000_000)
        phase = plan.phase_at(now_ms)
        if phase is None:
            return None, None
        try:
            import asyncio

            loop = asyncio.get_running_loop()
        except RuntimeError:
            return plan, phase
        if phase == "off" and plan.workload in CPU_USAGE_PREARM_WORKLOADS:
            _schedule_cpu_usage_window(plan, "on", loop, now_ms=now_ms)
        _start_cpu_usage_window(plan, phase, loop)
        # HTML capacity diagnostics need the same run/phase correlation and
        # low-cost CPU usage window, but never enable the expensive cProfile
        # capture reserved for the dedicated CPU-pair workload.
        if plan.workload not in PROFILE_WORKLOADS:
            return plan, phase
        if phase == "off" or plan.run_id in _consumed_run_ids or _active_profile is not None:
            return plan, phase
        remaining_seconds = max(0.0, (plan.on_end_ms - now_ms) / 1000)
        if remaining_seconds <= 0:
            return plan, phase
        diagnostic = cls(plan)
        diagnostic.profile = cProfile.Profile(timer=time.thread_time_ns, timeunit=1e-9)
        diagnostic.loop = loop
        diagnostic.started_monotonic = time.monotonic()
        diagnostic.start_lag_ms = max(0, now_ms - plan.on_start_ms)
        _consumed_run_ids.add(plan.run_id)
        _active_profile = diagnostic
        diagnostic.profile.enable()
        diagnostic.stop_handle = loop.call_later(remaining_seconds, diagnostic.stop)
        logger.info("cpu_diagnostic_started service=api run_id=%s", plan.run_id)
        return plan, phase

    def stop(self) -> None:
        global _active_profile
        if self.stopped:
            return
        self.stopped = True
        if self.profile is not None:
            self.profile.disable()
        if _active_profile is self:
            _active_profile = None
        if self.profile is None:
            return
        rows, total_self_us = _profile_summary(self.profile)
        elapsed_ms = 0
        if self.started_monotonic is not None:
            elapsed_ms = min(MAX_CAPTURE_MS, int((time.monotonic() - self.started_monotonic) * 1000))
        categories = ",".join(f"{name}:{cpu_us}:{calls}" for name, cpu_us, calls in rows)
        _emit_cpu_diagnostic_message(
            "cpu_diagnostic_complete service=api run_id=%s timer=thread_cpu "
            "start_lag_ms=%s elapsed_ms=%s total_self_cpu_us=%s functions=%s" % (
                self.plan.run_id,
                self.start_lag_ms,
                elapsed_ms,
                total_self_us,
                categories[:6000],
            )
        )
