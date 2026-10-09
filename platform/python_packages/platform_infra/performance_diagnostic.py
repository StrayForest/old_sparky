"""Default-off, process-bound CPU diagnostics for an approved read workload.

The only arming authority is a short-lived, root-owned plan written by the
trusted controller.  This module deliberately keeps the plan immutable and
uses process-local one-use state; workers never claim, unlink, or rewrite it.
"""

from __future__ import annotations

import cProfile
from dataclasses import dataclass
import grp
import json
import logging
import os
from pathlib import Path
import re
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
WORKLOAD = "authenticated_workspace_read_pair_v1"
OFF_WINDOW_MS = 20_000
IDLE_WINDOW_MS = 5_000
ON_WINDOW_MS = 20_000
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_RE = re.compile(r"^[0-9a-f]{32}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
INVOCATION_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f-]{36})$")
CPU_CGROUP_RE = re.compile(r"^0::(?P<path>/[A-Za-z0-9._/-]{0,512})$")
CPU_WINDOW_TOLERANCE_MS = 250


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
    if payload["workload"] != WORKLOAD:
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
    if (not off_start_ms < off_end_ms <= on_start_ms < on_end_ms
            or off_end_ms - off_start_ms != OFF_WINDOW_MS
            or on_start_ms - off_end_ms != IDLE_WINDOW_MS
            or on_end_ms - on_start_ms != ON_WINDOW_MS
            or expires_ms != on_end_ms
            or expires_ms - off_start_ms > MAX_CAPTURE_MS
            or on_end_ms - on_start_ms > MAX_CAPTURE_MS):
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
        workload=WORKLOAD,
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
    if key in _usage_windows_started:
        return
    _usage_windows_started.add(key)
    start_ms_target = plan.off_start_ms if phase == "off" else plan.on_start_ms
    end_ms_target = plan.off_end_ms if phase == "off" else plan.on_end_ms
    start_ms = time.time_ns() // 1_000_000
    start_lag_ms = start_ms - start_ms_target
    start_cpu_ns = time.process_time_ns()
    start_monotonic_ns = time.monotonic_ns()
    capacity = _effective_cpu_capacity()

    def finish() -> None:
        end_ms = time.time_ns() // 1_000_000
        end_lag_ms = end_ms - end_ms_target
        window_ms = max(0, (time.monotonic_ns() - start_monotonic_ns) // 1_000_000)
        cpu_ns = max(0, time.process_time_ns() - start_cpu_ns)
        timing_complete = bool(
            0 <= start_lag_ms <= CPU_WINDOW_TOLERANCE_MS
            and abs(end_lag_ms) <= CPU_WINDOW_TOLERANCE_MS
            and 20_000 - CPU_WINDOW_TOLERANCE_MS
            <= window_ms
            <= 20_000 + 2 * CPU_WINDOW_TOLERANCE_MS
        )
        logger.info(
            "cpu_diagnostic_usage service=api run_id=%s phase=%s window_ms=%s "
            "cpu_ns=%s cpu_capacity_cpus=%s start_lag_ms=%s end_lag_ms=%s "
            "timing_complete=%s",
            plan.run_id,
            phase,
            window_ms,
            cpu_ns,
            f"{capacity:.6f}" if capacity is not None else "unknown",
            start_lag_ms,
            end_lag_ms,
            str(timing_complete).lower(),
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

    # This module lives under <release>/platform/python_packages/...; the
    # immutable release receipt is one parent above the platform tree.
    release_file = Path(__file__).resolve().parents[3] / "RELEASE.json"
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
    if plan is None or plan.phase_at(current_ms) is None:
        return None
    return plan


def _read_process_plan_uncached(
    service: str,
    *,
    now_ms: int | None,
    pid: int | None,
    start_ticks: int | None,
    invocation_id: str | None,
    release_identity: tuple[str, str] | None,
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
        directory_fd = os.open(
            PLAN_DIRECTORY,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
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
    if plan.phase_at(current_ms) is None:
        return None
    if current_ms > plan.expires_at_ms or plan.expires_at_ms - plan.off_start_ms > MAX_CAPTURE_MS:
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


_active_profile: "ApiCpuDiagnostic | None" = None
_consumed_run_ids: set[str] = set()
_usage_windows_started: set[tuple[str, str]] = set()


# Only these repository call labels may leave the process. Everything else is
# reduced to one of the fixed runtime categories or `other`.
API_FUNCTION_ALLOWLIST = frozenset({
    "get_tournament_workspace",
    "get_tournament_workspace_by_slug",
    "workspace_conditional_preflight",
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
        if plan is None or trace_run_id != plan.run_id:
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
        _start_cpu_usage_window(plan, phase, loop)
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
        logger.info(
            "cpu_diagnostic_complete service=api run_id=%s timer=thread_cpu "
            "start_lag_ms=%s elapsed_ms=%s total_self_cpu_us=%s functions=%s",
            self.plan.run_id,
            self.start_lag_ms,
            elapsed_ms,
            total_self_us,
            categories[:6000],
        )
