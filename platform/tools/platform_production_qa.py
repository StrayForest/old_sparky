#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from redis.asyncio import Redis, from_url
from sqlalchemy import delete, func, select, text

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.csrf import UNSAFE_METHODS, generate_csrf_token
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import (
    MediaCleanupRequired,
    purge_deleted_media_metadata,
)
from python_packages.platform_infra.performance import WORKSPACE_PERF_KEYS
from python_packages.platform_infra.models import (
    AuditLog,
    DeadlockDreamSlot,
    DeadlockProfile,
    PasswordCredential,
    PlayerProfile,
    PreprodTestRun,
    Role,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainEntry,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentInvite,
    TournamentMatch,
    TournamentParticipant,
    TournamentTeam,
    User,
    UserRole,
    UserSession,
)
from python_packages.platform_infra.security import hash_password, new_session_token, session_token_digest
from apps.platform_api.app.services.tournament_read_models import (
    delete_tournament_read_models,
)
from apps.platform_api.app.services.tournament_participant_capacity import (
    ensure_participant_slot_claimed,
)


VALID_RANKS = list(RANKS[:-1])
ROLE_PATTERNS = [
    ["Carry", "Semi-Carry"],
    ["Semi-Carry", "Support"],
    ["Support", "Semi-Support"],
    ["Carry", "Support"],
]
HERO_PATTERNS = [
    ["Abrams", "Kelvin", "Seven"],
    ["Ivy", "Mina", "Apollo"],
    ["Bebop", "Dynamo", "Haze"],
    ["Infernus", "Warden", "Wraith"],
]
DEFAULT_REPORT_PATH = Path("/tmp/platform-production-qa-report.json")
UUID_RE = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?=/|$)"
)
NUMERIC_PATH_RE = re.compile(r"/\d+(?=/|$)")
REQUEST_PERF_RE = re.compile(r"\brequest_perf\b(?P<body>.*)$")
SSR_PERF_RE = re.compile(r"\bssr_perf\b(?P<body>.*)$")
SSR_EVENT_LOOP_RE = re.compile(r"\bssr_event_loop\b(?P<body>.*)$")
NGINX_ACCESS_LOG_PATH = Path("/var/log/nginx/platform-access.log")
READY_VOTE_PERF_KEYS = (
    "ready_vote_auth_ms",
    "ready_vote_checkout_count",
    "ready_vote_checkout_ms",
    "ready_vote_admission_inflight",
    "ready_vote_admission_limit",
    "ready_vote_admission_wait_ms",
    "ready_vote_admitted_total",
    "ready_vote_shed_total",
    "ready_vote_controller_limit_changes",
    "ready_vote_cpu_pressure",
    "ready_vote_pool_wait_ms",
    "ready_vote_cpu_monitor_sample_ms",
    "ready_vote_cpu_monitor_samples",
    "ready_vote_preflight_ms",
    "ready_vote_upsert_ms",
    "ready_vote_commit_ms",
    "ready_vote_response_ms",
)
PROCESS_CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
# Progress checkpoints are written while a retained fixture is being built.
# Do not serialize the complete 10k-user identity inventory on every batch;
# the final report still contains the exact inventory used by cleanup.
PREPROD_PROGRESS_ID_SAMPLE_SIZE = 4
READY_TEST_TEAMS = 2
WRITE_BURST_PROFILE_NAME = "write-burst-v1"
WRITE_BURST_USERS_PER_TOURNAMENT = 50
WRITE_BURST_TOURNAMENT_COUNT = 20
WRITE_BURST_JOIN_SPREAD_SECONDS = (10.0, 30.0, 60.0)
WRITE_BURST_READY_SPREAD_SECONDS = (5.0, 10.0, 30.0)
WRITE_BURST_MULTI_SPREAD_SECONDS = 30.0
WRITE_BURST_MULTI_START_STAGGER_SECONDS = 1.0
RESPONSE_DIAGNOSTIC_BODY_LIMIT = 4096
RESPONSE_DIAGNOSTIC_SAMPLE_LIMIT = 25
SENSITIVE_RESPONSE_HEADER_RE = re.compile(
    r"(?:authorization|cookie|set-cookie|token|secret|password|credential|signature)",
    re.IGNORECASE,
)


def qa_display_name(marker: str, label: str) -> str:
    return f"{label[:7]}-{marker[-7:]}"[:15]


def write_burst_tournament_name(marker: str, index: int, label: str) -> str:
    """Return a marker-qualified public name within the public length limit."""

    return f"WB{index:02d}-{marker[-10:]}-{label.replace('_', '-')[:5]}"[:25]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run targeted end-to-end production QA for Old Sparky Arena."
    )
    parser.add_argument("--origin", default="http://127.0.0.1")
    parser.add_argument(
        "--request-origin",
        default=None,
        help="Origin header for the API request contour; defaults to --origin.",
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--http-timeout", type=float, default=180.0)
    parser.add_argument(
        "--mode",
        choices=("targeted", "scale", "read-mix", "write-burst", "tournament-lifecycle"),
        default="targeted",
    )
    parser.add_argument("--scale-users", type=int, default=10_000)
    parser.add_argument("--scale-teams", type=int, default=128)
    parser.add_argument("--concurrency", type=int, default=80)
    parser.add_argument(
        "--http-max-connections",
        type=int,
        default=None,
        help="Explicit httpx connection-pool ceiling; required for high-concurrency virtual-user runs.",
    )
    parser.add_argument("--collect-performance", action="store_true")
    parser.add_argument("--system-sample-interval", type=float, default=1.0)
    parser.add_argument("--scale-site-mix-users", type=int, default=None)
    parser.add_argument("--scale-bracket-view-users", type=int, default=None)
    parser.add_argument(
        "--lifecycle-tournament-count",
        type=int,
        default=20,
        help="Tournament count for --mode tournament-lifecycle.",
    )
    parser.add_argument(
        "--lifecycle-users-per-tournament",
        type=int,
        default=500,
        help="Users per tournament for --mode tournament-lifecycle.",
    )
    parser.add_argument(
        "--lifecycle-teams-count",
        type=int,
        default=64,
        help="Requested Deadlock team count for --mode tournament-lifecycle.",
    )
    parser.add_argument(
        "--write-burst-profile",
        choices=("all", "single-join", "single-ready", "multi-staggered"),
        default="all",
    )
    parser.add_argument(
        "--write-burst-users-per-tournament",
        type=int,
        default=WRITE_BURST_USERS_PER_TOURNAMENT,
    )
    parser.add_argument(
        "--write-burst-time-scale",
        type=float,
        default=1.0,
        help="Scale burst spreads for a short tooling smoke; use 1.0 for retained measurements.",
    )
    parser.add_argument(
        "--scale-final-view-profile",
        choices=("current", "legacy"),
        default="current",
        help=(
            "current reads the aggregated workspace snapshot used by the current "
            "frontend flow; legacy also reads /bracket for worst-case comparison."
        ),
    )
    parser.add_argument(
        "--tournament-visibility",
        choices=("public", "invite_only"),
        default="public",
        help="Visibility for a retained scale tournament.",
    )
    parser.add_argument(
        "--profile-journey",
        action="store_true",
        help=(
            "Exercise ordinary profile, Deadlock profile and captain-profile "
            "writes, changes and persisted reads for every scale user."
        ),
    )
    parser.add_argument(
        "--tournament-name",
        default=None,
        help="Optional public tournament name for a retained scale QA run.",
    )
    parser.add_argument(
        "--retained-participant-email",
        default=None,
        help=(
            "Existing account to register after teams and bracket are locked. "
            "Allowed only for a retained scale run and never changes that account."
        ),
    )
    parser.add_argument(
        "--retained-participant-state",
        choices=("registered", "ready-unassigned"),
        default="registered",
        help=(
            "State for --retained-participant-email after the scale bracket is locked. "
            "ready-unassigned also records a ready vote and an explicit assignment leftover."
        ),
    )
    parser.add_argument(
        "--rostered-participant-email",
        default=None,
        help=(
            "Existing account to include before ready-check and auto-assignment. "
            "Allowed only for a retained scale run; its profile is read but never changed."
        ),
    )
    parser.add_argument(
        "--control-participant-email",
        default=None,
        help=(
            "Existing account to join through the participant API in a retained "
            "scale run; its profile is read but never changed."
        ),
    )
    parser.add_argument(
        "--control-participant-state",
        choices=("registered", "ready", "assigned"),
        default=None,
        help=(
            "Final control-account journey: registered only, ready-check voter, "
            "or eligible for team assignment."
        ),
    )
    parser.add_argument("--keep-data", action="store_true")
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


class QaFailure(RuntimeError):
    pass


def response_diagnostics(
    response: httpx.Response,
    *,
    method: str,
    path: str,
) -> dict[str, Any]:
    """Return bounded, secret-safe evidence for an unexpected HTTP response."""

    headers: dict[str, str] = {}
    for name, value in response.headers.items():
        headers[name.lower()] = (
            "<redacted>"
            if SENSITIVE_RESPONSE_HEADER_RE.search(name)
            else value[:500]
        )
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "method": method,
        "path": path.split("?", 1)[0],
        "status": response.status_code,
        "headers": headers,
        "body": response.content[:RESPONSE_DIAGNOSTIC_BODY_LIMIT].decode(
            "utf-8",
            errors="replace",
        ),
    }


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percent / 100)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def metric_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(values),
        "avg_ms": round(sum(values) / len(values), 3),
        "p50_ms": round(percentile(values, 50) or 0, 3),
        "p95_ms": round(percentile(values, 95) or 0, 3),
        "p99_ms": round(percentile(values, 99) or 0, 3),
        "max_ms": round(max(values), 3),
    }


def byte_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "avg_bytes": None,
            "p50_bytes": None,
            "p95_bytes": None,
            "p99_bytes": None,
            "max_bytes": None,
            "total_bytes": 0,
        }
    float_values = [float(value) for value in values]
    return {
        "count": len(values),
        "avg_bytes": round(sum(values) / len(values), 3),
        "p50_bytes": int(round(percentile(float_values, 50) or 0)),
        "p95_bytes": int(round(percentile(float_values, 95) or 0)),
        "p99_bytes": int(round(percentile(float_values, 99) or 0)),
        "max_bytes": max(values),
        "total_bytes": sum(values),
    }


def normalize_path(path: str, *, tournament_slug: str | None) -> str:
    normalized = path.split("?", 1)[0]
    if tournament_slug:
        normalized = normalized.replace(f"/{tournament_slug}", "/{slug}")
    normalized = UUID_RE.sub("/{uuid}", normalized)
    normalized = NUMERIC_PATH_RE.sub("/{id}", normalized)
    return normalized


def normalize_path_for_slugs(
    path: str,
    *,
    tournament_slug: str | None,
    tournament_slugs: list[str] | tuple[str, ...] = (),
) -> str:
    normalized = path.split("?", 1)[0]
    for slug in tournament_slugs:
        normalized = normalized.replace(f"/{slug}", "/{slug}")
    if tournament_slug:
        normalized = normalized.replace(f"/{tournament_slug}", "/{slug}")
    normalized = UUID_RE.sub("/{uuid}", normalized)
    normalized = NUMERIC_PATH_RE.sub("/{id}", normalized)
    return normalized


@dataclass(slots=True)
class HttpSample:
    phase: str
    method: str
    path: str
    status_code: int
    elapsed_ms: float
    ok: bool
    started_at: float
    finished_at: float
    response_bytes: int
    read_model_events: tuple[str, ...] = ()


class HttpMetricsRecorder:
    def __init__(self) -> None:
        self.samples: list[HttpSample] = []

    def record(
        self,
        *,
        phase: str,
        method: str,
        path: str,
        status_code: int,
        elapsed_seconds: float,
        ok: bool,
        started_at: float,
        finished_at: float,
        response_bytes: int,
        read_model_events: str | None = None,
    ) -> None:
        parsed_read_model_events = tuple(
            item.strip()
            for item in str(read_model_events or "").split(",")
            if item.strip()
        )
        self.samples.append(
            HttpSample(
                phase=phase,
                method=method,
                path=path,
                status_code=status_code,
                elapsed_ms=elapsed_seconds * 1000,
                ok=ok,
                started_at=started_at,
                finished_at=finished_at,
                response_bytes=response_bytes,
                read_model_events=parsed_read_model_events,
            )
        )

    @staticmethod
    def _read_model_summary(samples: list[HttpSample]) -> dict[str, Any]:
        outcomes: Counter[str] = Counter()
        models: Counter[str] = Counter()
        for sample in samples:
            for event in sample.read_model_events:
                model, separator, outcome = event.partition(":")
                if not separator:
                    continue
                models[model] += 1
                outcomes[outcome] += 1
        return {
            "events": sum(outcomes.values()),
            "by_outcome": dict(sorted(outcomes.items())),
            "by_model": dict(sorted(models.items())),
        }

    def summary(self, *, phases: set[str] | None = None) -> dict[str, Any]:
        samples = (
            self.samples
            if phases is None
            else [sample for sample in self.samples if sample.phase in phases]
        )
        if not samples:
            return {
                "scope": "full_population",
                "requests": 0,
                "successes": 0,
                "requests_per_second": 0,
                "throughput_per_second": 0,
                "goodput_per_second": 0,
                "errors": 0,
                "overall": metric_stats([]),
                "redis_read_models": self._read_model_summary([]),
                "by_phase": {},
                "by_route": {},
            }
        elapsed_ms = [sample.elapsed_ms for sample in samples]
        started_at = min(sample.started_at for sample in samples)
        finished_at = max(sample.finished_at for sample in samples)
        wall_seconds = max(0.001, finished_at - started_at)
        successes = sum(1 for sample in samples if sample.ok)
        by_phase: dict[str, list[float]] = defaultdict(list)
        by_route: dict[str, list[float]] = defaultdict(list)
        by_phase_bytes: dict[str, list[int]] = defaultdict(list)
        by_route_bytes: dict[str, list[int]] = defaultdict(list)
        status_counts: Counter[str] = Counter()
        for sample in samples:
            by_phase[sample.phase].append(sample.elapsed_ms)
            route_key = f"{sample.method} {sample.path}"
            by_route[route_key].append(sample.elapsed_ms)
            by_phase_bytes[sample.phase].append(sample.response_bytes)
            by_route_bytes[route_key].append(sample.response_bytes)
            status_counts[str(sample.status_code)] += 1
        route_rows = {
            route: {
                **metric_stats(values),
                "response_bytes": byte_stats(by_route_bytes[route]),
            }
            for route, values in sorted(
                by_route.items(),
                key=lambda item: (len(item[1]), max(item[1])),
                reverse=True,
            )
        }
        phase_rows: dict[str, dict[str, Any]] = {}
        for phase, values in sorted(by_phase.items()):
            phase_samples = [sample for sample in samples if sample.phase == phase]
            phase_started_at = min(sample.started_at for sample in phase_samples)
            phase_finished_at = max(sample.finished_at for sample in phase_samples)
            phase_wall_seconds = max(0.001, phase_finished_at - phase_started_at)
            phase_successes = sum(1 for sample in phase_samples if sample.ok)
            phase_rows[phase] = {
                **metric_stats(values),
                "successes": phase_successes,
                "errors": len(phase_samples) - phase_successes,
                "requests_per_second": round(len(phase_samples) / phase_wall_seconds, 3),
                "throughput_per_second": round(len(phase_samples) / phase_wall_seconds, 3),
                "goodput_per_second": round(phase_successes / phase_wall_seconds, 3),
                "wall_seconds": round(phase_wall_seconds, 3),
                "response_bytes": byte_stats(by_phase_bytes[phase]),
                "redis_read_models": self._read_model_summary(phase_samples),
            }
        return {
            "scope": "full_population",
            "requests": len(samples),
            "successes": successes,
            "requests_per_second": round(len(samples) / wall_seconds, 3),
            "throughput_per_second": round(len(samples) / wall_seconds, 3),
            "goodput_per_second": round(successes / wall_seconds, 3),
            "wall_seconds": round(wall_seconds, 3),
            "errors": sum(1 for sample in samples if not sample.ok),
            "status_counts": dict(sorted(status_counts.items())),
            "overall": {
                **metric_stats(elapsed_ms),
                "response_bytes": byte_stats([sample.response_bytes for sample in samples]),
            },
            "redis_read_models": self._read_model_summary(samples),
            "by_phase": phase_rows,
            "by_route": route_rows,
        }


def is_local_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower()
    return hostname in {"", "127.0.0.1", "localhost", "::1"}


def burst_offsets(*, count: int, spread_seconds: float) -> list[float]:
    if count <= 0:
        return []
    if count == 1 or spread_seconds <= 0:
        return [0.0] * count
    step = spread_seconds / count
    return [round(index * step, 6) for index in range(count)]


def is_compact_mutation_response(payload: Any, *, max_fields: int) -> bool:
    if not isinstance(payload, dict) or len(payload) > max_fields:
        return False
    return not any(isinstance(value, (list, dict)) for value in payload.values())


def follow_up_read_counts(
    samples: list[HttpSample],
    *,
    phase_prefix: str = "",
    phase_token: str | None = None,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for sample in samples:
        if not sample.phase.startswith(phase_prefix):
            continue
        if phase_token is not None and phase_token not in sample.phase:
            continue
        if sample.method != "GET":
            continue
        counts[f"{sample.method} {sample.path}"] += 1
    return dict(sorted(counts.items()))


def evaluate_write_burst_profiles(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    thresholds = {
        "p95_ms": 250.0,
        "p99_ms": 500.0,
        "avg_cpu_per_core_percent": 70.0,
        "max_lock_waiters": 0,
        "max_lock_waiting_query_ms": 0.0,
    }
    failures: list[dict[str, Any]] = []
    for profile in profiles:
        http = profile.get("http") if isinstance(profile, dict) else None
        overall = http.get("overall") if isinstance(http, dict) else None
        system = profile.get("system") if isinstance(profile, dict) else None
        if not isinstance(overall, dict):
            failures.append({"profile": profile.get("name"), "reason": "missing_http_metrics"})
            continue
        p95_ms = float(overall.get("p95_ms") or 0)
        p99_ms = float(overall.get("p99_ms") or 0)
        if p95_ms > thresholds["p95_ms"] or p99_ms > thresholds["p99_ms"]:
            failures.append(
                {
                    "profile": profile.get("name"),
                    "reason": "latency_budget",
                    "p95_ms": p95_ms,
                    "p99_ms": p99_ms,
                }
            )
        if not isinstance(system, dict) or int(system.get("samples") or 0) == 0:
            continue
        core_rows = system.get("cpu_per_core") or {}
        max_avg_cpu = max(
            [float(row.get("avg_percent") or 0) for row in core_rows.values() if isinstance(row, dict)]
            or [0.0]
        )
        waits = system.get("postgres_waits") or {}
        max_lock_waiters = int(waits.get("max_lock_waiters") or 0)
        max_lock_waiting_ms = float(waits.get("max_lock_waiting_query_ms") or 0)
        if max_avg_cpu > thresholds["avg_cpu_per_core_percent"]:
            failures.append(
                {
                    "profile": profile.get("name"),
                    "reason": "cpu_budget",
                    "max_avg_cpu_per_core_percent": max_avg_cpu,
                }
            )
        if (
            max_lock_waiters > thresholds["max_lock_waiters"]
            or max_lock_waiting_ms > thresholds["max_lock_waiting_query_ms"]
        ):
            failures.append(
                {
                    "profile": profile.get("name"),
                    "reason": "lock_wait",
                    "max_lock_waiters": max_lock_waiters,
                    "max_lock_waiting_query_ms": max_lock_waiting_ms,
                }
            )
    return {
        "healthy": not failures,
        "thresholds": thresholds,
        "failures": failures,
    }


def read_cpu_totals() -> dict[str, tuple[int, int]]:
    totals: dict[str, tuple[int, int]] = {}
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if not line.startswith("cpu"):
            continue
        columns = line.split()
        name = columns[0]
        if name == "cpu" or not name[3:].isdigit():
            continue
        values = [int(value) for value in columns[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        totals[name] = (total, idle)
    return totals


def cpu_percentages(
    previous: dict[str, tuple[int, int]] | None,
    current: dict[str, tuple[int, int]],
) -> dict[str, float]:
    if previous is None:
        return {}
    percentages: dict[str, float] = {}
    for core, (total, idle) in current.items():
        prev_total, prev_idle = previous.get(core, (total, idle))
        total_delta = total - prev_total
        idle_delta = idle - prev_idle
        if total_delta <= 0:
            continue
        percentages[core] = round((1 - idle_delta / total_delta) * 100, 2)
    return percentages


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, raw_value = line.split(":", 1)
        values[key] = int(raw_value.strip().split()[0]) * 1024
    return values


def read_tcp_connection_counts(port: int) -> dict[str, int]:
    port_hex = f"{port:04X}"
    counts: Counter[str] = Counter()
    for proc_path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not proc_path.exists():
            continue
        for line in proc_path.read_text(encoding="utf-8").splitlines()[1:]:
            columns = line.split()
            if len(columns) < 4:
                continue
            local_address = columns[1]
            local_port = local_address.rsplit(":", 1)[-1]
            if local_port.upper() != port_hex:
                continue
            state = columns[3]
            counts["total"] += 1
            if state == "01":
                counts["established"] += 1
    return {"total": counts["total"], "established": counts["established"]}


def read_process_io(proc_path: Path) -> dict[str, int]:
    values = {"read_bytes": 0, "write_bytes": 0}
    io_path = proc_path / "io"
    if not io_path.exists():
        return values
    with suppress(OSError, ValueError):
        for line in io_path.read_text(encoding="utf-8").splitlines():
            key, _, raw_value = line.partition(":")
            if key in values:
                values[key] = int(raw_value.strip())
    return values


def read_process_rss_bytes(proc_path: Path) -> int:
    status_path = proc_path / "status"
    if not status_path.exists():
        return 0
    with suppress(OSError, ValueError):
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                columns = line.split()
                if len(columns) >= 2:
                    return int(columns[1]) * 1024
    return 0


def parse_process_stat(raw_stat: str) -> tuple[int, int, int]:
    comm_end = raw_stat.rfind(")")
    if comm_end < 0:
        raise ValueError("invalid /proc stat row")
    columns = raw_stat[comm_end + 2:].split()
    if len(columns) < 13:
        raise ValueError("short /proc stat row")
    # After pid + comm, columns start at state (field 3).
    ppid = int(columns[1])
    utime = int(columns[11])
    stime = int(columns[12])
    return ppid, utime, stime


def iter_processes() -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    for child in proc_root.iterdir():
        if not child.name.isdigit():
            continue
        with suppress(OSError, ValueError):
            ppid, utime, stime = parse_process_stat(
                (child / "stat").read_text(encoding="utf-8")
            )
            comm = (child / "comm").read_text(encoding="utf-8").strip()
            cmdline = (child / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8",
                errors="replace",
            )
            io_values = read_process_io(child)
            processes.append(
                {
                    "pid": int(child.name),
                    "ppid": ppid,
                    "comm": comm,
                    "cmdline": cmdline,
                    "utime": utime,
                    "stime": stime,
                    "rss_bytes": read_process_rss_bytes(child),
                    "read_bytes": io_values["read_bytes"],
                    "write_bytes": io_values["write_bytes"],
                }
            )
    return processes


PROCESS_LABELS = (
    "deadlock-api",
    "deadlock-web",
    "deadlock-worker",
    "postgresql",
    "redis-server",
    "nginx",
    "load-generator",
)


def process_label(process: dict[str, Any]) -> str | None:
    cmdline = str(process.get("cmdline") or "").lower()
    comm = str(process.get("comm") or "").lower()
    if "apps.platform_api.app.main:app" in cmdline and "gunicorn" in cmdline:
        return "deadlock-api"
    if "apps.platform_worker.worker:celery_app" in cmdline or (
        "celery" in cmdline and "platform_worker" in cmdline
    ):
        return "deadlock-worker"
    if comm.startswith("next-server") or "next-server" in cmdline or (
        ("node" in comm or "/node" in cmdline)
        and (
            "/platform/apps/platform_web" in cmdline
            or "/platform/current/apps/platform_web" in cmdline
            or "/oldsparky/platform/current/apps/platform_web" in cmdline
        )
    ):
        return "deadlock-web"
    if is_postgres_process(process):
        return "postgresql"
    if comm == "redis-server" or "redis-server" in cmdline:
        return "redis-server"
    if comm == "nginx" or cmdline.startswith("nginx:"):
        return "nginx"
    if "platform_production_qa.py" in cmdline:
        return "load-generator"
    return None


def process_group_snapshot(processes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {
        label: {
            "process_count": 0,
            "cpu_ticks": 0,
            "rss_bytes": 0,
            "read_bytes": 0,
            "write_bytes": 0,
        }
        for label in PROCESS_LABELS
    }
    for process in processes:
        label = process_label(process)
        if label is None:
            continue
        group = groups[label]
        group["process_count"] += 1
        group["cpu_ticks"] += int(process["utime"]) + int(process["stime"])
        group["rss_bytes"] += int(process.get("rss_bytes") or 0)
        group["read_bytes"] += int(process.get("read_bytes") or 0)
        group["write_bytes"] += int(process.get("write_bytes") or 0)
    return groups


def process_cpu_total(processes: list[dict[str, Any]], matcher) -> int:
    return sum(
        int(process["utime"]) + int(process["stime"])
        for process in processes
        if matcher(process)
    )


def is_postgres_process(process: dict[str, Any]) -> bool:
    cmdline = str(process["cmdline"]).lower()
    comm = str(process["comm"]).lower()
    return "postgres" in comm or "postgres" in cmdline


def gunicorn_counts(processes: list[dict[str, Any]]) -> dict[str, int]:
    gunicorn_processes = [
        process
        for process in processes
        if "gunicorn" in str(process["cmdline"])
        and "apps.platform_api.app.main:app" in str(process["cmdline"])
    ]
    gunicorn_pids = {int(process["pid"]) for process in gunicorn_processes}
    workers = sum(
        1
        for process in gunicorn_processes
        if int(process.get("ppid") or 0) in gunicorn_pids
    )
    master = len(gunicorn_processes) - workers
    return {"master": master, "workers": workers}


def summarize_api_worker_lifecycle(lines: list[str]) -> dict[str, int]:
    boot_events = 0
    exit_events = 0
    restart_events = 0
    for line in lines:
        if "Booting worker with pid" in line:
            boot_events += 1
        if "Worker exiting" in line:
            exit_events += 1
        if "worker" in line.lower() and any(
            token in line.lower()
            for token in ("restarting", "timeout", "killed", "sent sig")
        ):
            restart_events += 1
    return {
        "boot_events": boot_events,
        "exit_events": exit_events,
        "restart_events": restart_events,
    }


async def sample_postgres_waits() -> dict[str, Any]:
    try:
        async with session_factory()() as db_session:
            row = (
                await db_session.execute(
                    text(
                        """
                        SELECT
                            (
                                SELECT count(*)::integer
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND wait_event_type = 'Lock'
                            ) AS lock_waiters,
                            (
                                SELECT count(*)::integer
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND state = 'active'
                                  AND wait_event_type IS NOT NULL
                            ) AS waiting_backends,
                            (
                                SELECT count(*)::integer
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND state = 'active'
                            ) AS active_backends,
                            (
                                SELECT COALESCE(
                                    max(extract(epoch FROM now() - query_start) * 1000),
                                    0
                                )::double precision
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND state = 'active'
                                  AND wait_event_type IS NOT NULL
                                  AND query_start IS NOT NULL
                            ) AS max_waiting_query_ms,
                            (
                                SELECT COALESCE(
                                    max(extract(epoch FROM now() - query_start) * 1000),
                                    0
                                )::double precision
                                FROM pg_stat_activity
                                WHERE datname = current_database()
                                  AND wait_event_type = 'Lock'
                                  AND query_start IS NOT NULL
                            ) AS max_lock_waiting_query_ms,
                            (
                                SELECT count(*)::integer
                                FROM pg_locks AS locks
                                JOIN pg_database AS db ON db.oid = locks.database
                                WHERE db.datname = current_database()
                                  AND NOT locks.granted
                            ) AS ungranted_locks
                            ,(
                                SELECT COALESCE(
                                    json_agg(
                                        json_build_object(
                                            'state', activity.state,
                                            'application_name', coalesce(activity.application_name, ''),
                                            'wait_event_type', activity.wait_event_type,
                                            'wait_event', activity.wait_event,
                                            'query_age_ms', round(
                                                extract(epoch FROM now() - activity.query_start) * 1000,
                                                3
                                            ),
                                            'query', left(
                                                regexp_replace(activity.query, E'\\s+', ' ', 'g'),
                                                240
                                            )
                                        )
                                        ORDER BY activity.query_start
                                    ),
                                    '[]'::json
                                )
                                FROM (
                                    SELECT state, application_name, wait_event_type, wait_event, query_start, query
                                    FROM pg_stat_activity
                                    WHERE datname = current_database()
                                      AND pid <> pg_backend_pid()
                                      AND state = 'active'
                                      AND query_start IS NOT NULL
                                    ORDER BY query_start
                                    LIMIT 8
                                ) AS activity
                            ) AS active_query_samples,
                            (
                                SELECT COALESCE(
                                    json_agg(
                                        json_build_object(
                                            'application_name', activity.application_name,
                                            'current', activity.connection_count
                                        )
                                        ORDER BY activity.application_name
                                    ),
                                    '[]'::json
                                )
                                FROM (
                                    SELECT coalesce(application_name, '') AS application_name,
                                           count(*)::integer AS connection_count
                                    FROM pg_stat_activity
                                    WHERE datname = current_database()
                                    GROUP BY coalesce(application_name, '')
                                ) AS activity
                            ) AS connection_ownership
                        """
                    )
                )
            ).mappings().one()
            return {
                "lock_waiters": int(row["lock_waiters"] or 0),
                "waiting_backends": int(row["waiting_backends"] or 0),
                "active_backends": int(row["active_backends"] or 0),
                "max_waiting_query_ms": round(float(row["max_waiting_query_ms"] or 0), 3),
                "max_lock_waiting_query_ms": round(
                    float(row["max_lock_waiting_query_ms"] or 0),
                    3,
                ),
                "ungranted_locks": int(row["ungranted_locks"] or 0),
                "active_query_samples": row["active_query_samples"] or [],
                "connection_ownership": row["connection_ownership"] or [],
            }
    except Exception as exc:
        return {"error": type(exc).__name__}


class SystemSampler:
    def __init__(self, *, interval_seconds: float) -> None:
        self.interval_seconds = max(0.25, interval_seconds)
        self.samples: list[dict[str, Any]] = []
        self._task: asyncio.Task[None] | None = None
        self._previous_cpu: dict[str, tuple[int, int]] | None = None
        self._previous_postgres_ticks: int | None = None
        self._previous_process_groups: dict[str, dict[str, Any]] | None = None
        self._previous_monotonic: float | None = None
        self._api_port = int(get_settings().platform_api_port)
        self._celery_redis: Redis = from_url(
            get_settings().platform_celery_broker_url,
            decode_responses=True,
        )

    async def celery_backlog(self) -> dict[str, int] | dict[str, str]:
        try:
            queue_names = (
                "deadlock-platform-high",
                "deadlock-platform-default",
                "deadlock-platform-low",
            )
            lengths = await asyncio.gather(
                *(self._celery_redis.llen(queue) for queue in queue_names)
            )
            return {
                queue: int(length or 0)
                for queue, length in zip(queue_names, lengths, strict=True)
            }
        except Exception as exc:
            return {"error": type(exc).__name__}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            await self._celery_redis.aclose()
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        await self._celery_redis.aclose()

    async def _run(self) -> None:
        while True:
            await self.sample()
            await asyncio.sleep(self.interval_seconds)

    async def sample(self) -> None:
        now = time.monotonic()
        cpu_totals = read_cpu_totals()
        per_core_cpu = cpu_percentages(self._previous_cpu, cpu_totals)
        self._previous_cpu = cpu_totals

        meminfo = read_meminfo()
        memory_total = meminfo.get("MemTotal", 0)
        memory_available = meminfo.get("MemAvailable", 0)
        swap_total = meminfo.get("SwapTotal", 0)
        swap_free = meminfo.get("SwapFree", 0)
        load_columns = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        processes = iter_processes()
        postgres_ticks = process_cpu_total(processes, is_postgres_process)
        process_groups = process_group_snapshot(processes)
        postgres_cpu_percent = 0.0
        process_metrics: dict[str, dict[str, Any]] = {}
        if self._previous_postgres_ticks is not None and self._previous_monotonic is not None:
            elapsed = max(0.001, now - self._previous_monotonic)
            postgres_cpu_percent = (
                (postgres_ticks - self._previous_postgres_ticks)
                / PROCESS_CLK_TCK
                / elapsed
                * 100
            )
            postgres_cpu_percent = max(0.0, postgres_cpu_percent)
            for label, current in process_groups.items():
                previous = (self._previous_process_groups or {}).get(label, {})
                cpu_percent = (
                    (int(current["cpu_ticks"]) - int(previous.get("cpu_ticks") or 0))
                    / PROCESS_CLK_TCK
                    / elapsed
                    * 100
                )
                read_bps = (
                    (int(current["read_bytes"]) - int(previous.get("read_bytes") or 0))
                    / elapsed
                )
                write_bps = (
                    (int(current["write_bytes"]) - int(previous.get("write_bytes") or 0))
                    / elapsed
                )
                process_metrics[label] = {
                    "process_count": int(current["process_count"]),
                    "cpu_percent": round(max(0.0, cpu_percent), 2),
                    "rss_bytes": int(current["rss_bytes"]),
                    "read_bytes_per_second": round(max(0.0, read_bps), 2),
                    "write_bytes_per_second": round(max(0.0, write_bps), 2),
                }
        else:
            for label, current in process_groups.items():
                process_metrics[label] = {
                    "process_count": int(current["process_count"]),
                    "cpu_percent": 0.0,
                    "rss_bytes": int(current["rss_bytes"]),
                    "read_bytes_per_second": 0.0,
                    "write_bytes_per_second": 0.0,
                }
        self._previous_postgres_ticks = postgres_ticks
        self._previous_process_groups = process_groups
        self._previous_monotonic = now

        self.samples.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "cpu_per_core_percent": per_core_cpu,
                "memory_used_bytes": max(0, memory_total - memory_available),
                "memory_total_bytes": memory_total,
                "swap_used_bytes": max(0, swap_total - swap_free),
                "swap_total_bytes": swap_total,
                "load_average": {
                    "1m": float(load_columns[0]),
                    "5m": float(load_columns[1]),
                    "15m": float(load_columns[2]),
                },
                "nginx_connections": read_tcp_connection_counts(80),
                "api_connections": read_tcp_connection_counts(self._api_port),
                "postgres_connections": read_tcp_connection_counts(5432),
                # Keep Redis socket pressure beside the existing counters so a
                # load run can distinguish cache/queue pressure from
                # PostgreSQL pool pressure without issuing extra Redis
                # commands per sample.
                "redis_connections": read_tcp_connection_counts(6379),
                "gunicorn": gunicorn_counts(processes),
                "postgres_cpu_percent": round(postgres_cpu_percent, 2),
                "processes": process_metrics,
                "postgres_waits": await sample_postgres_waits(),
                "celery_backlog": await self.celery_backlog(),
            }
        )

    def summary(self, *, start: int = 0, end: int | None = None) -> dict[str, Any]:
        samples = self.samples[start:end]
        if not samples:
            return {"samples": 0}
        cpu_by_core: dict[str, list[float]] = defaultdict(list)
        for sample in samples:
            for core, value in sample["cpu_per_core_percent"].items():
                cpu_by_core[core].append(float(value))
        memory_used = [float(sample["memory_used_bytes"]) for sample in samples]
        swap_used = [float(sample["swap_used_bytes"]) for sample in samples]
        load_1m = [float(sample["load_average"]["1m"]) for sample in samples]
        nginx_established = [
            int(sample["nginx_connections"]["established"]) for sample in samples
        ]
        postgres_connections = [
            int(sample["postgres_connections"]["established"]) for sample in samples
        ]
        redis_connections = [
            int(sample["redis_connections"]["established"]) for sample in samples
        ]
        postgres_cpu = [
            float(sample["postgres_cpu_percent"]) for sample in samples[1:]
        ]
        process_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        process_samples = samples[1:] if len(samples) > 1 else samples
        for sample in process_samples:
            sample_processes = sample.get("processes", {})
            if not isinstance(sample_processes, dict):
                continue
            for label, row in sample_processes.items():
                if isinstance(row, dict):
                    process_rows[str(label)].append(row)
        postgres_wait_rows = [
            sample.get("postgres_waits", {})
            for sample in samples
            if isinstance(sample.get("postgres_waits"), dict)
            and "error" not in sample.get("postgres_waits", {})
        ]
        postgres_lock_waiters = [
            int(row.get("lock_waiters") or 0)
            for row in postgres_wait_rows
        ]
        postgres_waiting_backends = [
            int(row.get("waiting_backends") or 0)
            for row in postgres_wait_rows
        ]
        postgres_ungranted_locks = [
            int(row.get("ungranted_locks") or 0)
            for row in postgres_wait_rows
        ]
        postgres_waiting_query_ms = [
            float(row.get("max_waiting_query_ms") or 0)
            for row in postgres_wait_rows
        ]
        postgres_lock_waiting_query_ms = [
            float(row.get("max_lock_waiting_query_ms") or 0)
            for row in postgres_wait_rows
        ]
        active_query_samples: list[dict[str, Any]] = []
        for row in postgres_wait_rows:
            active_rows = row.get("active_query_samples")
            if not isinstance(active_rows, list):
                continue
            active_query_samples.extend(
                sample for sample in active_rows if isinstance(sample, dict)
            )
        active_query_samples.sort(
            key=lambda sample: float(sample.get("query_age_ms") or 0),
            reverse=True,
        )
        ownership_values: dict[str, list[int]] = defaultdict(list)
        for row in postgres_wait_rows:
            ownership = row.get("connection_ownership")
            if not isinstance(ownership, list):
                continue
            for entry in ownership:
                if not isinstance(entry, dict):
                    continue
                application_name = str(entry.get("application_name") or "unknown")
                try:
                    ownership_values[application_name].append(
                        max(0, int(entry.get("current") or 0))
                    )
                except (TypeError, ValueError):
                    continue
        backlog_rows = [
            sample.get("celery_backlog", {})
            for sample in samples
            if isinstance(sample.get("celery_backlog"), dict)
            and "error" not in sample.get("celery_backlog", {})
        ]
        backlog_by_queue = {
            queue: {
                "max": max(int(row.get(queue) or 0) for row in backlog_rows),
                "last": int(backlog_rows[-1].get(queue) or 0),
            }
            for queue in (
                "deadlock-platform-high",
                "deadlock-platform-default",
                "deadlock-platform-low",
            )
            if backlog_rows
        }
        worker_counts = [int(sample["gunicorn"]["workers"]) for sample in samples]
        return {
            "samples": len(samples),
            "interval_seconds": self.interval_seconds,
            "cpu_per_core": {
                core: {
                    "avg_percent": round(sum(values) / len(values), 2),
                    "max_percent": round(max(values), 2),
                }
                for core, values in sorted(cpu_by_core.items())
                if values
            },
            "memory": {
                "avg_used_mb": round(sum(memory_used) / len(memory_used) / 1024 / 1024, 2),
                "max_used_mb": round(max(memory_used) / 1024 / 1024, 2),
                "total_mb": round(
                    float(samples[-1]["memory_total_bytes"]) / 1024 / 1024,
                    2,
                ),
            },
            "swap": {
                "avg_used_mb": round(sum(swap_used) / len(swap_used) / 1024 / 1024, 2),
                "max_used_mb": round(max(swap_used) / 1024 / 1024, 2),
                "total_mb": round(
                    float(samples[-1]["swap_total_bytes"]) / 1024 / 1024,
                    2,
                ),
            },
            "load_average_1m": {
                "avg": round(sum(load_1m) / len(load_1m), 2),
                "max": round(max(load_1m), 2),
            },
            "nginx_established_connections": {
                "avg": round(sum(nginx_established) / len(nginx_established), 2),
                "max": max(nginx_established),
            },
            "postgres_established_connections": {
                "avg": round(sum(postgres_connections) / len(postgres_connections), 2),
                "max": max(postgres_connections),
            },
            "redis_established_connections": {
                "avg": round(sum(redis_connections) / len(redis_connections), 2),
                "max": max(redis_connections),
            },
            "gunicorn_workers": {
                "min": min(worker_counts),
                "max": max(worker_counts),
                "last": worker_counts[-1],
            },
            "postgres_cpu_percent": {
                "avg": round(sum(postgres_cpu) / len(postgres_cpu), 2) if postgres_cpu else 0,
                "max": round(max(postgres_cpu), 2) if postgres_cpu else 0,
            },
            "processes": {
                label: {
                    "samples": len(rows),
                    "process_count_last": int(rows[-1].get("process_count") or 0),
                    "process_count_max": max(int(row.get("process_count") or 0) for row in rows),
                    "avg_cpu_percent": round(
                        sum(float(row.get("cpu_percent") or 0) for row in rows) / len(rows),
                        2,
                    ),
                    "max_cpu_percent": round(
                        max(float(row.get("cpu_percent") or 0) for row in rows),
                        2,
                    ),
                    "avg_rss_mb": round(
                        sum(float(row.get("rss_bytes") or 0) for row in rows)
                        / len(rows)
                        / 1024
                        / 1024,
                        2,
                    ),
                    "max_rss_mb": round(
                        max(float(row.get("rss_bytes") or 0) for row in rows)
                        / 1024
                        / 1024,
                        2,
                    ),
                    "avg_read_mb_per_second": round(
                        sum(float(row.get("read_bytes_per_second") or 0) for row in rows)
                        / len(rows)
                        / 1024
                        / 1024,
                        3,
                    ),
                    "avg_write_mb_per_second": round(
                        sum(float(row.get("write_bytes_per_second") or 0) for row in rows)
                        / len(rows)
                        / 1024
                        / 1024,
                        3,
                    ),
                    "max_read_mb_per_second": round(
                        max(float(row.get("read_bytes_per_second") or 0) for row in rows)
                        / 1024
                        / 1024,
                        3,
                    ),
                    "max_write_mb_per_second": round(
                        max(float(row.get("write_bytes_per_second") or 0) for row in rows)
                        / 1024
                        / 1024,
                        3,
                    ),
                }
                for label, rows in sorted(process_rows.items())
                if rows
            },
            "postgres_waits": {
                "samples": len(postgres_wait_rows),
                "max_lock_waiters": max(postgres_lock_waiters or [0]),
                "max_waiting_backends": max(postgres_waiting_backends or [0]),
                "max_ungranted_locks": max(postgres_ungranted_locks or [0]),
                "max_waiting_query_ms": round(max(postgres_waiting_query_ms or [0]), 3),
                "max_lock_waiting_query_ms": round(
                    max(postgres_lock_waiting_query_ms or [0]),
                    3,
                ),
                "active_query_samples": active_query_samples[:16],
            },
            "postgres_connection_ownership": {
                application_name: {
                    "samples": len(values),
                    "avg": round(sum(values) / len(values), 2),
                    "max": max(values),
                    "last": values[-1],
                }
                for application_name, values in sorted(ownership_values.items())
                if values
            },
            "celery_backlog": {
                "samples": len(backlog_rows),
                "by_queue": backlog_by_queue,
            },
        }


def parse_request_perf_line(line: str) -> dict[str, Any] | None:
    match = REQUEST_PERF_RE.search(line)
    if match is None:
        return None
    values: dict[str, Any] = {}
    for token in match.group("body").strip().split():
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        values[key] = raw_value
    numeric_keys = {
        "status": int,
        "total_ms": float,
        "request_ms": float,
        "sql_ms": float,
        "db_sql_ms": float,
        "sql_count": int,
        "max_sql_ms": float,
        "compute_ms": float,
        "compute_blocks": int,
        "response_bytes": int,
        "pool_wait_ms": float,
        "pool_checkout_wait_ms": float,
        "pool_connection_hold_ms": float,
        "pool_connection_hold_count": int,
        "authenticated_read_admission_wait_ms": float,
        "authenticated_read_admission_limit": int,
        "authenticated_read_admission_inflight": int,
        "authenticated_read_admission_shed": int,
        "redis_read_model_get_ms": float,
        "redis_read_model_build_ms": float,
        "redis_read_model_set_ms": float,
        "redis_read_model_payload_bytes": int,
        "ready_vote_checkout_count": int,
        "ready_vote_admission_inflight": int,
        "ready_vote_admission_limit": int,
        "ready_vote_admitted_total": int,
        "ready_vote_shed_total": int,
        "ready_vote_controller_limit_changes": int,
        "ready_vote_cpu_monitor_samples": int,
        **{
            key: float
            for key in READY_VOTE_PERF_KEYS
            if key not in {
                "ready_vote_checkout_count",
                "ready_vote_admission_inflight",
                "ready_vote_admission_limit",
                "ready_vote_admitted_total",
                "ready_vote_shed_total",
                "ready_vote_controller_limit_changes",
                "ready_vote_cpu_monitor_samples",
            }
        },
        **{key: float for key in WORKSPACE_PERF_KEYS},
    }
    for key, caster in numeric_keys.items():
        if key not in values:
            continue
        with suppress(ValueError):
            values[key] = caster(values[key])
    return values


def summarize_request_perf_logs(
    lines: list[str],
    *,
    tournament_slug: str | None,
    tournament_slugs: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    # ``request_perf`` is intentionally a bounded diagnostic stream: fast
    # successful Ready Vote requests are normally suppressed by the API
    # logger. Keep this scope in the parser output so it cannot be mistaken
    # for the full-population HTTP client measurements.
    diagnostic_scope = {
        "kind": "diagnostic_sample",
        "population": "journal_lines_selected_by_request_perf_logging_policy",
        "full_population_source": "http_client",
    }
    rows = [
        row
        for row in (parse_request_perf_line(line) for line in lines)
        if row is not None
    ]
    if not rows:
        return {"logged_requests": 0, "scope": diagnostic_scope}
    for row in rows:
        # Keep the original short keys for retained reports, while making the
        # read-path diagnostic names explicit for new and old journal lines.
        if not isinstance(row.get("request_ms"), (int, float)):
            row["request_ms"] = row.get("total_ms")
        if not isinstance(row.get("db_sql_ms"), (int, float)):
            row["db_sql_ms"] = row.get("sql_ms")
        if not isinstance(row.get("pool_checkout_wait_ms"), (int, float)):
            row["pool_checkout_wait_ms"] = row.get("pool_wait_ms")
    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_method_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_qa_phase: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        route = str(row.get("route") or row.get("path") or "")
        normalized_route = normalize_path_for_slugs(
            route,
            tournament_slug=tournament_slug,
            tournament_slugs=tournament_slugs,
        )
        by_route[normalized_route].append(row)
        by_method_route[f"{str(row.get('method') or '').upper()} {normalized_route}".strip()].append(row)
        qa_phase = str(row.get("qa_phase") or "")
        if qa_phase and qa_phase != "-":
            by_qa_phase[qa_phase].append(row)

    def row_metric_stats(key: str, row_values: list[dict[str, Any]]) -> dict[str, Any]:
        values = [float(row[key]) for row in row_values if isinstance(row.get(key), (int, float))]
        return metric_stats(values)

    def controller_state_counts(row_values: list[dict[str, Any]]) -> dict[str, int]:
        return dict(
            sorted(
                Counter(
                    str(row["ready_vote_controller_state"])
                    for row in row_values
                    if isinstance(row.get("ready_vote_controller_state"), str)
                ).items()
            )
        )

    def read_model_summary(row_values: list[dict[str, Any]]) -> dict[str, Any]:
        models: Counter[str] = Counter()
        outcomes: Counter[str] = Counter()
        for row in row_values:
            for model in str(row.get("redis_read_model_models") or "").split("|"):
                if model and model != "-":
                    models[model] += 1
            for outcome in str(row.get("redis_read_model_outcomes") or "").split("|"):
                if outcome and outcome != "-":
                    outcomes[outcome] += 1
        payload_values = [
            int(row["redis_read_model_payload_bytes"])
            for row in row_values
            if isinstance(row.get("redis_read_model_payload_bytes"), (int, float))
        ]
        return {
            "events": sum(outcomes.values()),
            "by_model": dict(sorted(models.items())),
            "by_outcome": dict(sorted(outcomes.items())),
            "get_ms": row_metric_stats("redis_read_model_get_ms", row_values),
            "build_ms": row_metric_stats("redis_read_model_build_ms", row_values),
            "set_ms": row_metric_stats("redis_read_model_set_ms", row_values),
            "payload_bytes": byte_stats(payload_values),
        }

    totals = [float(row["request_ms"]) for row in rows if isinstance(row.get("request_ms"), (int, float))]
    sql_counts = [float(row["sql_count"]) for row in rows if isinstance(row.get("sql_count"), (int, float))]
    sql_times = [float(row["db_sql_ms"]) for row in rows if isinstance(row.get("db_sql_ms"), (int, float))]
    compute_times = [float(row["compute_ms"]) for row in rows if isinstance(row.get("compute_ms"), (int, float))]
    max_sql_times = [float(row["max_sql_ms"]) for row in rows if isinstance(row.get("max_sql_ms"), (int, float))]
    response_bytes = [float(row["response_bytes"]) for row in rows if isinstance(row.get("response_bytes"), (int, float))]
    pool_wait_times = [float(row["pool_checkout_wait_ms"]) for row in rows if isinstance(row.get("pool_checkout_wait_ms"), (int, float))]
    pool_hold_times = [float(row["pool_connection_hold_ms"]) for row in rows if isinstance(row.get("pool_connection_hold_ms"), (int, float))]
    admission_wait_times = [float(row["authenticated_read_admission_wait_ms"]) for row in rows if isinstance(row.get("authenticated_read_admission_wait_ms"), (int, float))]
    non_sql_times = [
        max(
            0.0,
            float(row.get("request_ms") or 0)
            - float(row.get("db_sql_ms") or 0)
            - float(row.get("compute_ms") or 0),
        )
        for row in rows
    ]

    def summarize_route_rows(row_values: list[dict[str, Any]]) -> dict[str, Any]:
        non_sql_times = [
            max(
                0.0,
                float(row.get("request_ms", row.get("total_ms")) or 0)
                - float(row.get("db_sql_ms", row.get("sql_ms")) or 0)
                - float(row.get("compute_ms") or 0),
            )
            for row in row_values
        ]
        ready_vote_spans = {
            key: row_metric_stats(key, row_values)
            for key in READY_VOTE_PERF_KEYS
            if any(isinstance(row.get(key), (int, float)) for row in row_values)
        }
        workspace_stages = {
            key: row_metric_stats(key, row_values)
            for key in WORKSPACE_PERF_KEYS
            if any(isinstance(row.get(key), (int, float)) for row in row_values)
        }
        return {
            "requests": len(row_values),
            "total": row_metric_stats("request_ms", row_values),
            "request": row_metric_stats("request_ms", row_values),
            "avg_sql_queries_per_request": round(
                sum(float(row["sql_count"]) for row in row_values if isinstance(row.get("sql_count"), (int, float)))
                / max(1, sum(1 for row in row_values if isinstance(row.get("sql_count"), (int, float)))),
                3,
            ),
            "avg_db_time_ms": round(
                sum(float(row["db_sql_ms"]) for row in row_values if isinstance(row.get("db_sql_ms"), (int, float)))
                / max(1, sum(1 for row in row_values if isinstance(row.get("db_sql_ms"), (int, float)))),
                3,
            ),
            "avg_compute_time_ms": round(
                sum(float(row["compute_ms"]) for row in row_values if isinstance(row.get("compute_ms"), (int, float)))
                / max(1, sum(1 for row in row_values if isinstance(row.get("compute_ms"), (int, float)))),
                3,
            ),
            "non_sql_time": metric_stats(non_sql_times),
            "max_sql_time_ms": round(
                max(
                    [float(row["max_sql_ms"]) for row in row_values if isinstance(row.get("max_sql_ms"), (int, float))]
                    or [0]
                ),
                3,
            ),
            "response_bytes": byte_stats(
                [int(row["response_bytes"]) for row in row_values if isinstance(row.get("response_bytes"), (int, float))]
            ),
            "pool_checkout_wait_ms": row_metric_stats("pool_checkout_wait_ms", row_values),
            "pool_connection_hold_ms": row_metric_stats("pool_connection_hold_ms", row_values),
            "authenticated_read_admission_wait_ms": row_metric_stats(
                "authenticated_read_admission_wait_ms", row_values
            ),
            "ready_vote": ready_vote_spans,
            "ready_vote_controller_state_counts": controller_state_counts(row_values),
            "redis_read_models": read_model_summary(row_values),
            "workspace": workspace_stages,
        }

    return {
        "logged_requests": len(rows),
        "scope": diagnostic_scope,
        "overall": metric_stats(totals),
        "request_ms": metric_stats(totals),
        "avg_sql_queries_per_request": round(sum(sql_counts) / len(sql_counts), 3) if sql_counts else None,
        "avg_db_time_ms": round(sum(sql_times) / len(sql_times), 3) if sql_times else None,
        "db_sql_ms": metric_stats(sql_times),
        "avg_compute_time_ms": round(sum(compute_times) / len(compute_times), 3) if compute_times else None,
        "non_sql_time": metric_stats(non_sql_times),
        "max_sql_time_ms": round(max(max_sql_times), 3) if max_sql_times else None,
        "response_bytes": byte_stats([int(value) for value in response_bytes]),
        "pool_checkout_wait_ms": metric_stats(pool_wait_times),
        "pool_connection_hold_ms": metric_stats(pool_hold_times),
        "authenticated_read_admission_wait_ms": metric_stats(admission_wait_times),
        "ready_vote": {
            key: row_metric_stats(key, rows)
            for key in READY_VOTE_PERF_KEYS
            if any(isinstance(row.get(key), (int, float)) for row in rows)
        },
        "ready_vote_controller_state_counts": controller_state_counts(rows),
        "redis_read_models": read_model_summary(rows),
        "workspace": {
            key: row_metric_stats(key, rows)
            for key in WORKSPACE_PERF_KEYS
            if any(isinstance(row.get(key), (int, float)) for row in rows)
        },
        "by_route": {
            route: summarize_route_rows(row_values)
            for route, row_values in sorted(
                by_route.items(),
                key=lambda item: len(item[1]),
                reverse=True,
            )
        },
        "by_method_route": {
            route: summarize_route_rows(row_values)
            for route, row_values in sorted(
                by_method_route.items(),
                key=lambda item: len(item[1]),
                reverse=True,
            )
        },
        "by_qa_phase": {
            phase: summarize_route_rows(row_values)
            for phase, row_values in sorted(by_qa_phase.items())
        },
    }


def _attach_server_diagnostic_sample(
    write_burst_report: dict[str, Any],
    server_by_phase: Any,
) -> None:
    """Attach sampled request_perf data with an explicit nested scope."""

    if not isinstance(server_by_phase, dict):
        return
    # The HTTP client summary is the complete QA population; request_perf is
    # a deliberately sampled journal stream and must not look like a second
    # full set of measurements when consumed in isolation.
    write_burst_report["server_by_phase"] = {
        "scope": "diagnostic_sample",
        "by_phase": server_by_phase,
    }
    for profile in write_burst_report.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        phase = str(profile.get("phase") or "")
        profile["server"] = {
            "scope": "diagnostic_sample",
            "phase": phase,
            "summary": server_by_phase.get(phase),
        }


def top_metric_rows(
    rows: dict[str, dict[str, Any]],
    *,
    metric: str = "p95_ms",
    limit: int = 8,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for name, stats in rows.items():
        value = stats.get(metric)
        if isinstance(value, (int, float)):
            ranked.append((float(value), name, stats))
    ranked.sort(reverse=True)
    return [
        {
            "name": name,
            **stats,
        }
        for _, name, stats in ranked[:limit]
    ]


def top_response_byte_rows(
    rows: dict[str, dict[str, Any]],
    *,
    metric: str = "avg_bytes",
    limit: int = 8,
) -> list[dict[str, Any]]:
    ranked: list[tuple[float, str, dict[str, Any]]] = []
    for name, stats in rows.items():
        response_bytes = stats.get("response_bytes")
        if not isinstance(response_bytes, dict):
            continue
        value = response_bytes.get(metric)
        if isinstance(value, (int, float)):
            ranked.append((float(value), name, stats))
    ranked.sort(reverse=True)
    return [
        {
            "name": name,
            **stats,
        }
        for _, name, stats in ranked[:limit]
    ]


def summarize_bottleneck_evidence(
    *,
    http_summary: dict[str, Any],
    server_summary: dict[str, Any],
    system_summary: dict[str, Any],
) -> dict[str, Any]:
    server_routes = server_summary.get("by_route") if isinstance(server_summary, dict) else {}
    if not isinstance(server_routes, dict):
        server_routes = {}

    server_route_totals = {
        route: metrics.get("total", {})
        for route, metrics in server_routes.items()
        if isinstance(metrics, dict) and isinstance(metrics.get("total"), dict)
    }
    server_sql_hotspots = []
    for route, metrics in server_routes.items():
        if not isinstance(metrics, dict):
            continue
        total = metrics.get("total")
        if not isinstance(total, dict):
            total = {}
        server_sql_hotspots.append({
            "route": route,
            "requests": metrics.get("requests"),
            "p95_ms": total.get("p95_ms"),
            "avg_sql_queries_per_request": metrics.get("avg_sql_queries_per_request"),
            "avg_db_time_ms": metrics.get("avg_db_time_ms"),
            "max_sql_time_ms": metrics.get("max_sql_time_ms"),
        })
    server_sql_hotspots.sort(
        key=lambda row: (
            float(row["avg_db_time_ms"] or 0),
            float(row["avg_sql_queries_per_request"] or 0),
            float(row["p95_ms"] or 0),
        ),
        reverse=True,
    )

    cpu_rows = (
        system_summary.get("cpu_per_core", {})
        if isinstance(system_summary, dict)
        else {}
    )
    cpu_sustained_saturation = False
    cpu_peak_saturation = False
    if isinstance(cpu_rows, dict):
        cpu_sustained_saturation = any(
            isinstance(row, dict)
            and float(row.get("avg_percent") or 0) >= 85
            for row in cpu_rows.values()
        )
        cpu_peak_saturation = any(
            isinstance(row, dict)
            and float(row.get("max_percent") or 0) >= 95
            for row in cpu_rows.values()
        )
    load_average = (
        system_summary.get("load_average_1m", {})
        if isinstance(system_summary, dict)
        else {}
    )
    load_high = isinstance(load_average, dict) and float(load_average.get("max") or 0) >= 4
    postgres_connections = (
        system_summary.get("postgres_established_connections", {})
        if isinstance(system_summary, dict)
        else {}
    )
    postgres_connection_peak_high = (
        isinstance(postgres_connections, dict)
        and int(postgres_connections.get("max") or 0) >= 30
    )
    postgres_waits = (
        system_summary.get("postgres_waits", {})
        if isinstance(system_summary, dict)
        else {}
    )
    postgres_lock_wait_observed = (
        isinstance(postgres_waits, dict)
        and (
            int(postgres_waits.get("max_lock_waiters") or 0) > 0
            or int(postgres_waits.get("max_ungranted_locks") or 0) > 0
        )
    )
    postgres_lock_contention = (
        isinstance(postgres_waits, dict)
        and (
            int(postgres_waits.get("max_ungranted_locks") or 0) > 0
            or float(postgres_waits.get("max_lock_waiting_query_ms") or 0) >= 1000
        )
    )
    postgres_backend_waits = (
        isinstance(postgres_waits, dict)
        and int(postgres_waits.get("max_waiting_backends") or 0) > 0
        and float(postgres_waits.get("max_waiting_query_ms") or 0) >= 1000
    )
    process_rows = (
        system_summary.get("processes", {})
        if isinstance(system_summary, dict)
        else {}
    )
    top_processes_by_cpu: list[dict[str, Any]] = []
    if isinstance(process_rows, dict):
        for label, row in process_rows.items():
            if not isinstance(row, dict):
                continue
            top_processes_by_cpu.append({
                "name": label,
                "avg_cpu_percent": row.get("avg_cpu_percent"),
                "max_cpu_percent": row.get("max_cpu_percent"),
                "avg_rss_mb": row.get("avg_rss_mb"),
                "max_rss_mb": row.get("max_rss_mb"),
                "process_count_max": row.get("process_count_max"),
            })
    top_processes_by_cpu.sort(
        key=lambda row: float(row.get("avg_cpu_percent") or 0),
        reverse=True,
    )
    dominant_process = top_processes_by_cpu[0]["name"] if top_processes_by_cpu else None

    likely_classes: list[str] = []
    if cpu_sustained_saturation or load_high:
        likely_classes.append("cpu_or_python_serialization_saturation")
        if dominant_process:
            likely_classes.append(f"dominant_process:{dominant_process}")
    if postgres_connection_peak_high and postgres_backend_waits:
        likely_classes.append("postgres_pool_pressure")
    if postgres_lock_contention:
        likely_classes.append("postgres_lock_or_wait_contention")
    elif postgres_backend_waits:
        likely_classes.append("postgres_backend_waits")
    if server_sql_hotspots and float(server_sql_hotspots[0].get("avg_db_time_ms") or 0) >= 250:
        likely_classes.append("db_time_hotspot")
    if not likely_classes:
        likely_classes.append("no_single_dominant_bottleneck_detected")

    return {
        "top_client_phases_by_p95": top_metric_rows(
            http_summary.get("by_phase", {}) if isinstance(http_summary, dict) else {},
            limit=8,
        ),
        "top_client_routes_by_p95": top_metric_rows(
            http_summary.get("by_route", {}) if isinstance(http_summary, dict) else {},
            limit=10,
        ),
        "top_client_routes_by_avg_response_bytes": top_response_byte_rows(
            http_summary.get("by_route", {}) if isinstance(http_summary, dict) else {},
            limit=10,
        ),
        "top_server_routes_by_p95": top_metric_rows(server_route_totals, limit=10),
        "top_processes_by_avg_cpu": top_processes_by_cpu[:8],
        "server_sql_hotspots": server_sql_hotspots[:10],
        "resource_flags": {
            "cpu_sustained_saturation": cpu_sustained_saturation,
            "cpu_peak_saturation": cpu_peak_saturation,
            "load_average_high": load_high,
            "postgres_connection_peak_high": postgres_connection_peak_high,
            "postgres_lock_wait_observed": postgres_lock_wait_observed,
            "postgres_lock_contention": postgres_lock_contention,
            "postgres_backend_waits": postgres_backend_waits,
        },
        "likely_bottleneck_classes": likely_classes,
    }


def _collect_journal_lines(unit: str, since: str, until: str) -> list[str]:
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                since,
                "--until",
                until,
                "--no-pager",
                "-o",
                "cat",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def collect_api_journal_lines(since: str, until: str) -> list[str]:
    return _collect_journal_lines("deadlock-api", since, until)


def collect_web_journal_lines(since: str, until: str) -> list[str]:
    return _collect_journal_lines("deadlock-web", since, until)


def parse_ssr_perf_line(line: str) -> dict[str, Any] | None:
    match = SSR_PERF_RE.search(line)
    if match is None:
        return None
    values: dict[str, Any] = {}
    for token in match.group("body").strip().split():
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        values[key] = raw_value
    for key in ("duration_ms",):
        if key in values:
            with suppress(ValueError):
                values[key] = float(values[key])
    if not values.get("request_id") or not values.get("stage"):
        return None
    return values


def parse_ssr_event_loop_line(line: str) -> dict[str, Any] | None:
    match = SSR_EVENT_LOOP_RE.search(line)
    if match is None:
        return None
    values: dict[str, Any] = {}
    for token in match.group("body").strip().split():
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        with suppress(ValueError):
            values[key] = float(raw_value)
    if not isinstance(values.get("p95_ms"), (int, float)):
        return None
    return values


def _nginx_record_timestamp(raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str):
        return None
    try:
        value = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _nginx_seconds(raw_value: object) -> float | None:
    if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        return float(raw_value) * 1000
    if not isinstance(raw_value, str) or raw_value in {"", "-"}:
        return None
    values: list[float] = []
    for item in raw_value.split(","):
        with suppress(ValueError):
            values.append(float(item.strip()) * 1000)
    return sum(values) if values else None


def collect_nginx_access_records(
    since: datetime,
    until: datetime,
    *,
    log_path: Path = NGINX_ACCESS_LOG_PATH,
) -> list[dict[str, Any]]:
    """Read only JSON access records in the observer window.

    The raw records stay inside the observer process. The report contains
    aggregate timings and never serializes the request URI or request ID.
    Rotated, uncompressed siblings are included so a long benchmark crossing
    the size rotation boundary does not silently lose its first requests.
    """

    paths = [log_path]
    paths.extend(
        sorted(
            path
            for path in log_path.parent.glob(f"{log_path.name}.*")
            if path.is_file() and not path.name.endswith(".gz")
        )
    )
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                timestamp = _nginx_record_timestamp(record.get("time"))
                if timestamp is None or timestamp < since or timestamp > until:
                    continue
                records.append(record)
    return records


def _nginx_html_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for record in records:
        if str(record.get("method") or "").upper() != "GET":
            continue
        try:
            status = int(record.get("status") or 0)
        except (TypeError, ValueError):
            continue
        if status != 200:
            continue
        path = str(record.get("uri") or "").split("?", 1)[0]
        if not re.fullmatch(r"/tournaments/[^/]+", path):
            continue
        selected.append(record)
    return selected


def summarize_ssr_observability(
    web_journal_lines: list[str],
    nginx_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join sampled Next stage logs with Nginx timings without leaking IDs."""

    stage_rows = [
        row
        for row in (parse_ssr_perf_line(line) for line in web_journal_lines)
        if row is not None
    ]
    event_loop_rows = [
        row
        for row in (parse_ssr_event_loop_line(line) for line in web_journal_lines)
        if row is not None
    ]
    by_stage: dict[str, list[float]] = defaultdict(list)
    by_request: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in stage_rows:
        duration = row.get("duration_ms")
        if not isinstance(duration, (int, float)):
            continue
        stage = str(row["stage"])
        request_id = str(row["request_id"])
        by_stage[stage].append(float(duration))
        by_request[request_id][stage].append(float(duration))

    html_records = _nginx_html_records(nginx_records)
    correlated_rows: list[dict[str, Any]] = []
    for record in html_records:
        request_id = str(record.get("request_id") or "")
        stages = by_request.get(request_id)
        if not stages:
            continue
        row: dict[str, Any] = {
            "request_ms": _nginx_seconds(record.get("request_time")),
            "upstream_ms": _nginx_seconds(record.get("upstream_time")),
        }
        for stage, durations in stages.items():
            row[stage] = sum(durations)
        data_ready = row.get("tournament_detail_data_ready")
        upstream_ms = row.get("upstream_ms")
        if isinstance(data_ready, (int, float)) and isinstance(upstream_ms, (int, float)):
            row["unattributed_upstream_after_data_ms"] = max(0.0, upstream_ms - data_ready)
        correlated_rows.append(row)

    def metric_for_rows(key: str) -> dict[str, Any]:
        return metric_stats(
            [float(row[key]) for row in correlated_rows if isinstance(row.get(key), (int, float))]
        )

    stage_presence = {
        stage: sum(1 for row in correlated_rows if stage in row)
        for stage in sorted(by_stage)
    }
    return {
        "scope": {
            "kind": "diagnostic_sample",
            "stage_population": "sampled_ssr_requests",
            "nginx_population": "all_authenticated_tournament_html_200_records_in_window",
        },
        "event_loop": {
            "samples": len(event_loop_rows),
            "p95_ms": metric_stats(
                [float(row["p95_ms"]) for row in event_loop_rows]
            ),
            "max_ms": metric_stats(
                [float(row["max_ms"]) for row in event_loop_rows if isinstance(row.get("max_ms"), (int, float))]
            ),
        },
        "ssr_stages": {
            "logged_stages": len(stage_rows),
            "sampled_requests": len(by_request),
            "by_stage": {
                stage: metric_stats(values)
                for stage, values in sorted(by_stage.items())
            },
        },
        "nginx_html": {
            "requests": len(html_records),
            "request_time_ms": metric_stats(
                [value for record in html_records if (value := _nginx_seconds(record.get("request_time"))) is not None]
            ),
            "upstream_time_ms": metric_stats(
                [value for record in html_records if (value := _nginx_seconds(record.get("upstream_time"))) is not None]
            ),
        },
        "correlated_html": {
            "requests": len(correlated_rows),
            "request_time_ms": metric_for_rows("request_ms"),
            "upstream_time_ms": metric_for_rows("upstream_ms"),
            "unattributed_upstream_after_data_ms": metric_for_rows(
                "unattributed_upstream_after_data_ms"
            ),
            "stage_presence": stage_presence,
            "stage_ms": {
                stage: metric_for_rows(stage)
                for stage in sorted(by_stage)
            },
        },
    }


class ProductionQa:
    def __init__(
        self,
        *,
        origin: str,
        request_origin: str | None = None,
        report_path: Path,
        keep_data: bool,
        http_timeout: float,
        mode: str = "targeted",
        scale_users: int = 10_000,
        scale_teams: int = 128,
        concurrency: int = 80,
        http_max_connections: int | None = None,
        collect_performance: bool = False,
        system_sample_interval: float = 1.0,
        scale_site_mix_users: int | None = None,
        scale_bracket_view_users: int | None = None,
        lifecycle_tournament_count: int = 20,
        lifecycle_users_per_tournament: int = 500,
        lifecycle_teams_count: int = 64,
        scale_final_view_profile: str = "current",
        tournament_visibility: str = "public",
        profile_journey: bool = False,
        write_burst_profile: str = "all",
        write_burst_users_per_tournament: int = WRITE_BURST_USERS_PER_TOURNAMENT,
        write_burst_time_scale: float = 1.0,
        tournament_name: str | None = None,
        retained_participant_email: str | None = None,
        retained_participant_state: str = "registered",
        rostered_participant_email: str | None = None,
        control_participant_email: str | None = None,
        control_participant_state: str | None = None,
    ) -> None:
        timestamp = datetime.now(UTC).strftime("%y%m%d%H%M%S")
        prefix = "preprod" if mode in {"scale", "read-mix", "write-burst", "tournament-lifecycle"} else "qa"
        self.marker = f"{prefix}{timestamp}{secrets.token_hex(2)}"
        self.origin = origin.rstrip("/")
        self.api_origin = f"{self.origin}/api/v1"
        self.request_origin = (request_origin or self.origin).rstrip("/")
        self.report_path = report_path
        self.keep_data = keep_data
        requested_tournament_name = (tournament_name or "").strip()
        if len(requested_tournament_name) > 25:
            raise ValueError("QA tournament name must not exceed 25 characters.")
        self.tournament_name = requested_tournament_name or f"PRE {self.marker}"[:25]
        if tournament_visibility not in {"public", "invite_only"}:
            raise ValueError("tournament_visibility must be public or invite_only")
        self.tournament_visibility = tournament_visibility
        self.profile_journey = bool(profile_journey)
        self.retained_participant_email = (retained_participant_email or "").strip().lower() or None
        self.retained_participant_state = (
            "ready-unassigned" if retained_participant_state == "ready-unassigned" else "registered"
        )
        if self.retained_participant_email and (mode != "scale" or not keep_data):
            raise ValueError(
                "--retained-participant-email requires --mode scale and --keep-data."
            )
        self.rostered_participant_email = (rostered_participant_email or "").strip().lower() or None
        if self.rostered_participant_email and (mode != "scale" or not keep_data):
            raise ValueError(
                "--rostered-participant-email requires --mode scale and --keep-data."
            )
        self.control_participant_email = (control_participant_email or "").strip().lower() or None
        if self.control_participant_email and (mode != "scale" or not keep_data):
            raise ValueError(
                "--control-participant-email requires --mode scale and --keep-data."
            )
        if self.control_participant_email and control_participant_state not in {
            "registered",
            "ready",
            "assigned",
        }:
            raise ValueError(
                "--control-participant-state is required with --control-participant-email."
            )
        if control_participant_state and not self.control_participant_email:
            raise ValueError(
                "--control-participant-email is required with --control-participant-state."
            )
        self.control_participant_state = control_participant_state
        self.http_timeout = max(1.0, http_timeout)
        self.mode = mode
        self.lifecycle_tournament_count = max(1, lifecycle_tournament_count)
        self.lifecycle_users_per_tournament = max(14, lifecycle_users_per_tournament)
        lifecycle_team_choices = (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192)
        requested_lifecycle_teams = max(2, lifecycle_teams_count)
        if requested_lifecycle_teams > 8192:
            raise ValueError("lifecycle team count cannot exceed 8192")
        self.lifecycle_teams_count = next(
            count for count in lifecycle_team_choices if count >= requested_lifecycle_teams
        )
        if mode == "tournament-lifecycle":
            lifecycle_total_users = (
                self.lifecycle_tournament_count * self.lifecycle_users_per_tournament
            )
            if lifecycle_total_users > 20_000:
                raise ValueError("tournament-lifecycle fixture cannot exceed 20,000 users")
            if self.lifecycle_users_per_tournament < self.lifecycle_teams_count * 7:
                raise ValueError(
                    "tournament-lifecycle requires at least 7 users per requested team"
                )
            if self.lifecycle_teams_count > 8192:
                raise ValueError("lifecycle team count cannot exceed 8192")
        self.write_burst_profile = write_burst_profile
        self.write_burst_users_per_tournament = max(14, write_burst_users_per_tournament)
        self.write_burst_time_scale = max(0.01, write_burst_time_scale)
        write_burst_users = (
            WRITE_BURST_TOURNAMENT_COUNT * self.write_burst_users_per_tournament
            if write_burst_profile in {"all", "multi-staggered"}
            else self.write_burst_users_per_tournament
        )
        if mode == "write-burst":
            self.scale_users = write_burst_users
        elif mode == "tournament-lifecycle":
            self.scale_users = self.lifecycle_tournament_count * self.lifecycle_users_per_tournament
        else:
            self.scale_users = max(14, scale_users)
        self.scale_teams = max(2, min(128, scale_teams))
        self.concurrency = max(1, concurrency)
        requested_http_connections = (
            max(100, self.concurrency)
            if http_max_connections is None
            else http_max_connections
        )
        self.http_max_connections = max(1, min(10_000, requested_http_connections))
        self.collect_performance = collect_performance or mode == "tournament-lifecycle"
        self.scale_site_mix_users = (
            self.scale_users
            if scale_site_mix_users is None
            else max(0, min(self.scale_users, scale_site_mix_users))
        )
        self.scale_bracket_view_users = (
            self.scale_users
            if scale_bracket_view_users is None
            else max(0, min(self.scale_users, scale_bracket_view_users))
        )
        self.scale_final_view_profile = (
            "legacy" if scale_final_view_profile == "legacy" else "current"
        )
        self.http_metrics = HttpMetricsRecorder()
        self.system_sampler = SystemSampler(interval_seconds=system_sample_interval)
        self.current_phase = "setup"
        self.journal_since: str | None = None
        self.journal_until: str | None = None
        self.session_cookie_name = get_settings().platform_session_cookie_name
        self.csrf_cookie_name = f"{self.session_cookie_name}_csrf"
        self.password = secrets.token_urlsafe(18)
        self.clients: list[httpx.AsyncClient] = []
        self.users_by_id: dict[str, dict[str, Any]] = {}
        self.user_ids: list[str] = []
        self.session_tokens_by_user_id: dict[str, str] = {}
        self.csrf_tokens_by_user_id: dict[str, str] = {}
        self.csrf_tokens_preseeded = False
        self.rostered_session_token_digest: str | None = None
        self.control_participant_session_token_digest: str | None = None
        self.tournament_id: str | None = None
        self.tournament_slug: str | None = None
        self.tournament_ids: list[str] = []
        self.tournament_slugs: list[str] = []
        self.scenarios: list[dict[str, Any]] = []
        self.lifecycle_phase_metrics: dict[str, dict[str, Any]] = {}
        self.lifecycle_timings: dict[str, Any] = {}
        self.report: dict[str, Any] = {
            "marker": self.marker,
            "started_at": datetime.now(UTC).isoformat(),
            "origin": self.origin,
            "request_origin": self.request_origin,
            "mode": mode,
            "report_path": str(report_path),
            "requested_users": self.scale_users if mode in {"scale", "read-mix", "write-burst"} else (
                self.lifecycle_tournament_count * self.lifecycle_users_per_tournament
                if mode == "tournament-lifecycle"
                else None
            ),
            "http_timeout_seconds": self.http_timeout,
            "profile_journey": self.profile_journey,
            "scenarios": self.scenarios,
            "user_ids": self.user_ids,
            "tournament_ids": [],
            "tournament_slugs": [],
            "tournament_visibility": self.tournament_visibility,
            "invite_code": None,
            "teams": [],
            "strength_ranking": [],
            "initial_pairings": [],
            "match_path": [],
            "preference_metrics": {},
            "preference_metrics_by_team": [],
            "cleanup": {},
            "performance": {},
            "write_burst": {},
            "retained_participant": None,
            "rostered_participant": None,
            "control_participant": None,
            "http_failure_diagnostics": [],
            "tournament_lifecycle": {
                "tournament_count": self.lifecycle_tournament_count,
                "users_per_tournament": self.lifecycle_users_per_tournament,
                "total_users": self.lifecycle_tournament_count * self.lifecycle_users_per_tournament,
                "teams_count": self.lifecycle_teams_count,
                "phases": self.lifecycle_phase_metrics,
                "timings": self.lifecycle_timings,
            },
        }

    def scenario(
        self,
        name: str,
        ok: bool,
        detail: Any = None,
        *,
        fatal: bool = True,
    ) -> None:
        self.scenarios.append({"name": name, "ok": ok, "detail": detail})
        if not ok and fatal:
            raise QaFailure(f"{name}: {detail}")

    @contextmanager
    def phase(self, name: str):
        previous = self.current_phase
        self.current_phase = name
        try:
            yield
        finally:
            self.current_phase = previous

    def metric_path(self, path: str) -> str:
        return normalize_path_for_slugs(
            path,
            tournament_slug=self.tournament_slug,
            tournament_slugs=self.tournament_slugs,
        )

    async def run_lifecycle_phase(self, name: str, operation):
        """Run one lifecycle user step and retain its existing harness metrics."""

        system_sample_start = len(self.system_sampler.samples)
        started_at = time.monotonic()
        with self.phase(name):
            result = await operation()
        finished_at = time.monotonic()
        system_sample_end = len(self.system_sampler.samples)
        if self.collect_performance and system_sample_end == system_sample_start:
            # Short mutation waves can finish between sampler ticks. Capture a
            # boundary sample using the same SystemSampler used by all other
            # QA modes so every lifecycle phase has resource evidence.
            await self.system_sampler.sample()
            system_sample_end = len(self.system_sampler.samples)
        http = self.http_metrics.summary(phases={name})
        system = self.system_sampler.summary(
            start=system_sample_start,
            end=system_sample_end,
        )
        phase_report = {
            "phase_duration_seconds": round(finished_at - started_at, 4),
            "http": http,
            "system": system,
        }
        self.lifecycle_phase_metrics[name] = phase_report
        return result

    async def start_performance_collection(self) -> None:
        if not self.collect_performance:
            return
        self.journal_since = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        await self.system_sampler.start()

    async def stop_performance_collection(self) -> None:
        if not self.collect_performance:
            return
        self.journal_until = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        await self.system_sampler.stop()
        journal_lines: list[str] = []
        if self.journal_since and self.journal_until:
            journal_lines = collect_api_journal_lines(self.journal_since, self.journal_until)
        http_summary = self.http_metrics.summary()
        system_summary = self.system_sampler.summary()
        server_request_summary = summarize_request_perf_logs(
            journal_lines,
            tournament_slug=self.tournament_slug,
            tournament_slugs=self.tournament_slugs,
        )
        write_burst_report = self.report.get("write_burst")
        if self.mode == "write-burst" and isinstance(write_burst_report, dict):
            _attach_server_diagnostic_sample(
                write_burst_report,
                server_request_summary.get("by_qa_phase", {}),
            )
        lifecycle_phases = self.lifecycle_phase_metrics
        server_by_phase = server_request_summary.get("by_qa_phase", {})
        if isinstance(lifecycle_phases, dict) and isinstance(server_by_phase, dict):
            for phase_name, phase_report in lifecycle_phases.items():
                phase_report["server_request_perf"] = server_by_phase.get(phase_name, {
                    "scope": "diagnostic_sample",
                    "requests": 0,
                })
                system = phase_report.get("system") or {}
                processes = system.get("processes") if isinstance(system, dict) else {}
                api_process = processes.get("deadlock-api", {}) if isinstance(processes, dict) else {}
                worker_process = processes.get("deadlock-worker", {}) if isinstance(processes, dict) else {}
                waits = system.get("postgres_waits", {}) if isinstance(system, dict) else {}
                phase_report["resources"] = {
                    "api_cpu": api_process,
                    "worker_cpu": worker_process,
                    "postgres_cpu": system.get("postgres_cpu_percent", {}),
                    "memory": system.get("memory", {}),
                    "db_connections": system.get("postgres_established_connections", {}),
                    "db_pool_wait": (phase_report["server_request_perf"] or {}).get(
                        "pool_checkout_wait_ms", {}
                    ),
                    "postgres_waits": waits,
                    "celery_backlog": system.get("celery_backlog", {}),
                }
        self.report["performance"] = {
            "http_client": http_summary,
            "system": system_summary,
            "server_request_perf_logs": server_request_summary,
            "measurement_scope": {
                "http_client": "full_population",
                "server_request_perf_logs": "diagnostic_sample",
                "note": (
                    "http_client covers every request issued by the QA phase; "
                    "request_perf contains only journal lines selected by the "
                    "API diagnostic logging policy."
                ),
            },
            "bottleneck_summary": summarize_bottleneck_evidence(
                http_summary=http_summary,
                server_summary=server_request_summary,
                system_summary=system_summary,
            ),
            "api_worker_lifecycle": summarize_api_worker_lifecycle(journal_lines),
            "journal_window": {
                "since": self.journal_since,
                "until": self.journal_until,
                "lines": len(journal_lines),
            },
            "shape": {
                "scale_users": self.scale_users,
                "scale_teams": self.scale_teams,
                "concurrency": self.concurrency,
                "http_max_connections": self.http_max_connections,
                "site_mix_users": self.scale_site_mix_users,
                "bracket_view_users": self.scale_bracket_view_users,
                "final_view_profile": self.scale_final_view_profile,
                "write_burst_profile": self.write_burst_profile,
                "write_burst_users_per_tournament": self.write_burst_users_per_tournament,
                "write_burst_time_scale": self.write_burst_time_scale,
                "load_generator_local": is_local_origin(self.origin),
            },
            "lifecycle_phases": lifecycle_phases,
        }

    async def new_client(self, origin: str | None = None) -> httpx.AsyncClient:
        client_origin = (origin or self.origin).rstrip("/")
        client = httpx.AsyncClient(
            base_url=f"{client_origin}/api/v1",
            follow_redirects=True,
            timeout=httpx.Timeout(self.http_timeout),
            limits=httpx.Limits(
                max_connections=self.http_max_connections,
                max_keepalive_connections=self.http_max_connections,
            ),
        )
        self.clients.append(client)
        return client

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        expected: int,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        started_at = time.monotonic()
        status_code = 0
        ok = False
        response_bytes = 0
        read_model_events: str | None = None
        try:
            headers = (
                {
                    "Origin": self.request_origin,
                    "X-Platform-QA-Phase": self.current_phase,
                }
                if self.collect_performance
                else {"Origin": self.request_origin}
            )
            response = await client.request(
                method,
                path,
                headers=headers,
                json=json_payload,
            )
            status_code = response.status_code
            ok = response.status_code == expected
            response_bytes = len(response.content or b"")
            read_model_events = response.headers.get("x-platform-read-model")
        finally:
            if self.collect_performance:
                finished_at = time.monotonic()
                self.http_metrics.record(
                    phase=self.current_phase,
                    method=method,
                    path=self.metric_path(path),
                    status_code=status_code,
                    elapsed_seconds=finished_at - started_at,
                    ok=ok,
                    started_at=started_at,
                    finished_at=finished_at,
                    response_bytes=response_bytes,
                    read_model_events=read_model_events,
                )
        if response.status_code != expected:
            diagnostics = response_diagnostics(response, method=method, path=path)
            failure_samples = self.report.setdefault("http_failure_diagnostics", [])
            if len(failure_samples) < RESPONSE_DIAGNOSTIC_SAMPLE_LIMIT:
                failure_samples.append(diagnostics)
            raise QaFailure(
                f"{method} {path}: expected {expected}, got {response.status_code}: "
                f"{response.text[:1000]} diagnostics="
                f"{json.dumps(diagnostics, ensure_ascii=False, separators=(',', ':'))}"
            )
        if not response.content:
            return None
        return response.json()

    async def request_as(
        self,
        client: httpx.AsyncClient,
        user: dict[str, Any],
        method: str,
        path: str,
        *,
        expected: int | tuple[int, ...],
        json_payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        return_response_meta: bool = False,
        record_ok_statuses: set[int] | None = None,
    ) -> Any:
        token = self.session_tokens_by_user_id[str(user["id"])]
        csrf_token = None
        if method.upper() in UNSAFE_METHODS:
            csrf_token = await self.csrf_token_for_user(client, user)
        request_headers = {
            "Origin": self.request_origin,
            "Cookie": "; ".join(
                filter(
                    None,
                    (
                        f"{self.session_cookie_name}={token}",
                        f"{self.csrf_cookie_name}={csrf_token}" if csrf_token else None,
                    ),
                )
            ),
            **({"X-CSRF-Token": csrf_token} if csrf_token else {}),
            **(
                {"X-Platform-QA-Phase": self.current_phase}
                if self.collect_performance
                else {}
            ),
        }
        if extra_headers:
            request_headers.update(extra_headers)
        retryable_get = self.mode == "tournament-lifecycle" and method.upper() == "GET"
        # The production API intentionally sheds lifecycle reads on DB pool
        # timeout with Retry-After: 1. Keep enough bounded budget for a full-
        # population read wave without replaying mutations: a mutation can
        # commit before a transient response is returned.
        retry_limit = 8 if retryable_get else 0
        response: httpx.Response | None = None
        for request_attempt in range(retry_limit + 1):
            started_at = time.monotonic()
            status_code = 0
            ok = False
            response_bytes = 0
            read_model_events: str | None = None
            retry_busy = False
            try:
                response = await client.request(
                    method,
                    path,
                    headers=request_headers,
                    json=json_payload,
                )
                status_code = response.status_code
                expected_statuses = (
                    {expected}
                    if isinstance(expected, int)
                    else set(expected)
                )
                ok = (
                    response.status_code in expected_statuses
                    if record_ok_statuses is None
                    else response.status_code in record_ok_statuses
                )
                response_bytes = len(response.content or b"")
                read_model_events = response.headers.get("x-platform-read-model")
                retry_busy = (
                    retryable_get
                    and response.status_code == 503
                    and request_attempt < retry_limit
                )
            except httpx.TransportError:
                if request_attempt >= retry_limit:
                    raise
                retry_busy = True
            finally:
                if self.collect_performance:
                    finished_at = time.monotonic()
                    self.http_metrics.record(
                        phase=self.current_phase,
                        method=method,
                        path=self.metric_path(path),
                        status_code=status_code,
                        elapsed_seconds=finished_at - started_at,
                        ok=ok,
                        started_at=started_at,
                        finished_at=finished_at,
                        response_bytes=response_bytes,
                        read_model_events=read_model_events,
                    )
            if retry_busy:
                retry_after_seconds = 0.25 * (request_attempt + 1)
                if response is not None:
                    retry_after_header = response.headers.get("retry-after")
                    if retry_after_header:
                        with suppress(ValueError):
                            retry_after_seconds = max(
                                retry_after_seconds,
                                float(retry_after_header),
                            )
                await asyncio.sleep(min(5.0, retry_after_seconds))
                continue
            break
        if response is None:
            raise QaFailure(f"{method} {path}: request returned no response")
        expected_statuses = (
            {expected}
            if isinstance(expected, int)
            else set(expected)
        )
        if response.status_code not in expected_statuses:
            diagnostics = response_diagnostics(response, method=method, path=path)
            failure_samples = self.report.setdefault("http_failure_diagnostics", [])
            if len(failure_samples) < RESPONSE_DIAGNOSTIC_SAMPLE_LIMIT:
                failure_samples.append(diagnostics)
            raise QaFailure(
                f"{method} {path} as {user['label']}: expected {sorted(expected_statuses)}, got "
                f"{response.status_code}: {response.text[:1000]} diagnostics="
                f"{json.dumps(diagnostics, ensure_ascii=False, separators=(',', ':'))}"
            )
        payload = response.json() if response.content else None
        if return_response_meta:
            return payload, response.status_code, dict(response.headers)
        return payload

    async def csrf_token_for_user(
        self,
        client: httpx.AsyncClient,
        user: dict[str, Any],
    ) -> str:
        user_id = str(user["id"])
        cached = self.csrf_tokens_by_user_id.get(user_id)
        if cached:
            return cached

        session_token = self.session_tokens_by_user_id[user_id]
        started_at = time.monotonic()
        status_code = 0
        response_bytes = 0
        read_model_events: str | None = None
        try:
            response = await client.get(
                "/auth/csrf",
                headers={
                    "Origin": self.request_origin,
                    "Cookie": f"{self.session_cookie_name}={session_token}",
                },
            )
            status_code = response.status_code
            response_bytes = len(response.content or b"")
            read_model_events = response.headers.get("x-platform-read-model")
        finally:
            if self.collect_performance:
                self.http_metrics.record(
                    phase=self.current_phase,
                    method="GET",
                    path=self.metric_path("/auth/csrf"),
                    status_code=status_code,
                    elapsed_seconds=time.monotonic() - started_at,
                    ok=status_code == 200,
                    started_at=started_at,
                    finished_at=time.monotonic(),
                    response_bytes=response_bytes,
                    read_model_events=read_model_events,
                )
        if response.status_code != 200:
            diagnostics = response_diagnostics(
                response,
                method="GET",
                path="/auth/csrf",
            )
            failure_samples = self.report.setdefault("http_failure_diagnostics", [])
            if len(failure_samples) < RESPONSE_DIAGNOSTIC_SAMPLE_LIMIT:
                failure_samples.append(diagnostics)
            raise QaFailure(
                f"GET /auth/csrf as {user['label']}: expected 200, got "
                f"{response.status_code}: {response.text[:1000]} diagnostics="
                f"{json.dumps(diagnostics, ensure_ascii=False, separators=(',', ':'))}"
            )
        try:
            csrf_token = str(response.json()["csrf_token"])
        except (KeyError, TypeError, ValueError):
            raise QaFailure(f"GET /auth/csrf as {user['label']}: token missing") from None
        if len(csrf_token) < 32:
            raise QaFailure(f"GET /auth/csrf as {user['label']}: token invalid")
        self.csrf_tokens_by_user_id[user_id] = csrf_token
        return csrf_token

    def preseed_csrf_tokens(self, users: list[dict[str, Any]]) -> None:
        """Model tokens issued during login without measuring /auth/csrf."""

        settings = get_settings()
        for user in users:
            user_id = str(user["id"])
            self.csrf_tokens_by_user_id[user_id] = generate_csrf_token(
                self.session_tokens_by_user_id[user_id],
                settings,
            )
        self.csrf_tokens_preseeded = True

    async def wait_for_auto_assignment_run_as(
        self,
        client: httpx.AsyncClient,
        user: dict[str, Any],
        *,
        tournament_slug: str | None = None,
        previous_run_id: str | None = None,
        expected_teams: int | None = None,
    ) -> dict[str, Any]:
        slug = tournament_slug or self.tournament_slug
        if not slug:
            raise QaFailure("Cannot wait for auto-assignment without a tournament slug.")
        job = await self.request_as(
            client,
            user,
            "POST",
            f"/tournaments/{slug}/deadlock/auto-assignment/run-async",
            expected=202,
        )
        return await self._poll_auto_assignment_run_as(
            client,
            user,
            tournament_slug=slug,
            previous_run_id=previous_run_id,
            expected_teams=expected_teams,
            task_id=str(job["task_id"]),
        )

    async def wait_for_auto_assignment_run(
        self,
        client: httpx.AsyncClient,
        *,
        tournament_slug: str | None = None,
        previous_run_id: str | None = None,
        expected_teams: int | None = None,
    ) -> dict[str, Any]:
        slug = tournament_slug or self.tournament_slug
        if not slug:
            raise QaFailure("Cannot wait for auto-assignment without a tournament slug.")
        job = await self.request(
            client,
            "POST",
            f"/tournaments/{slug}/deadlock/auto-assignment/run-async",
            expected=202,
        )
        return await self._poll_auto_assignment_run(
            client,
            tournament_slug=slug,
            previous_run_id=previous_run_id,
            expected_teams=expected_teams,
            task_id=str(job["task_id"]),
        )

    async def _poll_auto_assignment_run_as(
        self,
        client: httpx.AsyncClient,
        user: dict[str, Any],
        *,
        tournament_slug: str,
        previous_run_id: str | None,
        expected_teams: int | None,
        task_id: str,
    ) -> dict[str, Any]:
        wait_seconds = max(300.0, self.http_timeout * 12)
        deadline = time.monotonic() + wait_seconds
        last_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            state = await self.request_as(
                client,
                user,
                "GET",
                f"/tournaments/{tournament_slug}/deadlock/auto-assignment",
                expected=200,
            )
            last_state = state
            latest_run = state.get("latest_run")
            if isinstance(latest_run, dict) and latest_run.get("status") in {
                "failed",
                "cancelled",
            }:
                raise QaFailure(
                    "Auto-assignment worker reported a terminal failure "
                    f"task_id={task_id} state={latest_run}"
                )
            if self._auto_assignment_run_matches(
                latest_run,
                previous_run_id=previous_run_id,
                expected_teams=expected_teams,
            ):
                latest_run["queued_task_id"] = task_id
                return latest_run
            await asyncio.sleep(1.0)
        raise QaFailure(
            "Timed out waiting for async auto-assignment run "
            f"task_id={task_id} previous_run_id={previous_run_id} last_state={last_state}"
        )

    async def _poll_auto_assignment_run(
        self,
        client: httpx.AsyncClient,
        *,
        tournament_slug: str,
        previous_run_id: str | None,
        expected_teams: int | None,
        task_id: str,
    ) -> dict[str, Any]:
        wait_seconds = max(300.0, self.http_timeout * 12)
        deadline = time.monotonic() + wait_seconds
        last_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            state = await self.request(
                client,
                "GET",
                f"/tournaments/{tournament_slug}/deadlock/auto-assignment",
                expected=200,
            )
            last_state = state
            latest_run = state.get("latest_run")
            if isinstance(latest_run, dict) and latest_run.get("status") in {
                "failed",
                "cancelled",
            }:
                raise QaFailure(
                    "Auto-assignment worker reported a terminal failure "
                    f"task_id={task_id} state={latest_run}"
                )
            if self._auto_assignment_run_matches(
                latest_run,
                previous_run_id=previous_run_id,
                expected_teams=expected_teams,
            ):
                latest_run["queued_task_id"] = task_id
                return latest_run
            await asyncio.sleep(1.0)
        raise QaFailure(
            "Timed out waiting for async auto-assignment run "
            f"task_id={task_id} previous_run_id={previous_run_id} last_state={last_state}"
        )

    @staticmethod
    def _auto_assignment_run_matches(
        run: dict[str, Any] | None,
        *,
        previous_run_id: str | None,
        expected_teams: int | None,
    ) -> bool:
        if not run or run.get("status") != "generated":
            return False
        if previous_run_id is not None and str(run.get("id")) == previous_run_id:
            return False
        if expected_teams is not None and len(run.get("teams") or []) != expected_teams:
            return False
        return True

    def _preprod_report_snapshot(self, *, progress: bool) -> dict[str, Any]:
        snapshot = dict(self.report)
        if not progress:
            return snapshot

        for key in ("user_ids", "tournament_ids", "tournament_slugs"):
            value = snapshot.get(key)
            if not isinstance(value, list) or len(value) <= PREPROD_PROGRESS_ID_SAMPLE_SIZE * 2:
                continue
            snapshot[key] = {
                "count": len(value),
                "first": value[:PREPROD_PROGRESS_ID_SAMPLE_SIZE],
                "last": value[-PREPROD_PROGRESS_ID_SAMPLE_SIZE:],
                "complete_inventory_in_final_report": True,
            }
        snapshot["fixture_progress"] = {
            "marker": self.marker,
            "synthetic_user_count": len(self.user_ids),
            "exact_identity_report_deferred_until_phase_completion": True,
        }
        return snapshot

    async def record_preprod_run(self, *, progress: bool = False, **updates: Any) -> None:
        async with session_factory()() as db_session:
            run = await db_session.scalar(select(PreprodTestRun).where(PreprodTestRun.marker == self.marker))
            if run is None:
                run = PreprodTestRun(
                    marker=self.marker,
                    status=str(updates.pop("status", "running")),
                    origin=self.origin,
                    requested_users=(
                        self.scale_users
                        if self.mode in {"scale", "write-burst", "tournament-lifecycle"}
                        else len(self.user_ids)
                    ),
                    report_path=str(self.report_path),
                    started_at=datetime.now(UTC),
                    report=self._preprod_report_snapshot(progress=progress),
                )
                db_session.add(run)
            for key, value in updates.items():
                if hasattr(run, key):
                    setattr(run, key, value)
            run.report = self._preprod_report_snapshot(progress=progress)
            await db_session.commit()

    async def add_retained_participant(self) -> dict[str, Any] | None:
        if self.retained_participant_email is None:
            return None
        if self.tournament_id is None:
            raise QaFailure("retained_participant: tournament is not initialized")

        async with session_factory()() as db_session:
            user = await db_session.scalar(
                select(User).where(func.lower(User.email) == self.retained_participant_email)
            )
            if user is None:
                raise QaFailure(
                    f"retained_participant: account {self.retained_participant_email!r} was not found"
                )
            user_id = user.id
            participant = await db_session.scalar(
                select(TournamentParticipant).where(
                    TournamentParticipant.tournament_id == self.tournament_id,
                    TournamentParticipant.user_id == user_id,
                )
            )
            created = participant is None
            if participant is None:
                participant = TournamentParticipant(
                    tournament_id=self.tournament_id,
                    user_id=user_id,
                    entry_type="solo",
                    status="registered",
                )
                db_session.add(participant)
            else:
                participant.entry_type = "solo"
                participant.status = "registered"
            await db_session.flush()
            await ensure_participant_slot_claimed(
                db_session,
                tournament_id=self.tournament_id,
                max_participants=(
                    await db_session.scalar(
                        select(Tournament.max_participants).where(
                            Tournament.id == self.tournament_id
                        )
                    )
                ),
                participant_id=participant.id,
            )

            ready_round_id: int | None = None
            if self.retained_participant_state == "ready-unassigned":
                ready_round = await db_session.scalar(
                    select(TournamentDeadlockReadyRound)
                    .where(TournamentDeadlockReadyRound.tournament_id == self.tournament_id)
                    .order_by(
                        TournamentDeadlockReadyRound.created_at.desc(),
                        TournamentDeadlockReadyRound.id.desc(),
                    )
                    .limit(1)
                )
                assignment_run = await db_session.scalar(
                    select(TournamentDeadlockAssignmentRun)
                    .where(
                        TournamentDeadlockAssignmentRun.tournament_id == self.tournament_id,
                        TournamentDeadlockAssignmentRun.status == "locked",
                    )
                    .order_by(
                        TournamentDeadlockAssignmentRun.created_at.desc(),
                        TournamentDeadlockAssignmentRun.id.desc(),
                    )
                    .limit(1)
                )
                if ready_round is None or assignment_run is None:
                    raise QaFailure(
                        "retained_participant: ready round and locked assignment are required"
                    )
                ready_round_id = ready_round.id
                eligible_user_ids = [str(item) for item in ready_round.eligible_user_ids or []]
                if user_id not in eligible_user_ids:
                    ready_round.eligible_user_ids = [*eligible_user_ids, user_id]
                db_session.add(
                    TournamentDeadlockReadyVote(
                        round_id=ready_round.id,
                        user_id=user_id,
                        choice="yes",
                        responded_at=datetime.now(UTC),
                    )
                )
                candidate_pool_user_ids = [
                    str(item) for item in assignment_run.candidate_pool_user_ids or []
                ]
                if user_id not in candidate_pool_user_ids:
                    assignment_run.candidate_pool_user_ids = [
                        *candidate_pool_user_ids,
                        user_id,
                    ]
                leftover_user_ids = [str(item) for item in assignment_run.leftover_user_ids or []]
                if user_id not in leftover_user_ids:
                    assignment_run.leftover_user_ids = [*leftover_user_ids, user_id]
            db_session.add(
                AuditLog(
                    actor_user_id=None,
                    action=f"preprod.retained_participant.{self.retained_participant_state}",
                    subject_type="tournament",
                    subject_id=self.tournament_id,
                    payload={
                        "marker": self.marker,
                        "participant_user_id": user_id,
                        "created": created,
                        "state": self.retained_participant_state,
                        "ready_round_id": ready_round_id,
                    },
                )
            )
            await db_session.commit()

        result = {
            "user_id": user_id,
            "email": self.retained_participant_email,
            "status": "registered",
            "ready_choice": "yes" if self.retained_participant_state == "ready-unassigned" else None,
            "state": self.retained_participant_state,
            "assigned_to_team": False,
        }
        self.report["retained_participant"] = result
        self.scenario("retained_participant_registered", True, result)
        return result

    async def add_rostered_participant(self) -> dict[str, Any] | None:
        if self.rostered_participant_email is None:
            return None
        if self.tournament_id is None:
            raise QaFailure("rostered_participant: tournament is not initialized")

        async with session_factory()() as db_session:
            row = (
                await db_session.execute(
                    select(User, PlayerProfile, DeadlockProfile)
                    .join(PlayerProfile, PlayerProfile.user_id == User.id)
                    .join(DeadlockProfile, DeadlockProfile.user_id == User.id)
                    .where(func.lower(User.email) == self.rostered_participant_email)
                )
            ).one_or_none()
            if row is None:
                raise QaFailure(
                    "rostered_participant: account and complete Deadlock profile are required"
                )
            user, profile, deadlock_profile = row
            participant = await db_session.scalar(
                select(TournamentParticipant).where(
                    TournamentParticipant.tournament_id == self.tournament_id,
                    TournamentParticipant.user_id == user.id,
                )
            )
            if participant is None:
                participant = TournamentParticipant(
                    tournament_id=self.tournament_id,
                    user_id=user.id,
                    entry_type="solo",
                    status="registered",
                )
                db_session.add(participant)
            else:
                participant.entry_type = "solo"
                participant.status = "registered"
            await db_session.flush()
            await ensure_participant_slot_claimed(
                db_session,
                tournament_id=self.tournament_id,
                max_participants=(
                    await db_session.scalar(
                        select(Tournament.max_participants).where(
                            Tournament.id == self.tournament_id
                        )
                    )
                ),
                participant_id=participant.id,
            )

            token = new_session_token()
            token_digest = session_token_digest(token)
            db_session.add(
                UserSession(
                    user_id=user.id,
                    token_digest=token_digest,
                    ip_address="127.0.0.1",
                    user_agent=f"platform-production-qa-rostered:{self.marker}",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            db_session.add(
                AuditLog(
                    actor_user_id=None,
                    action="preprod.rostered_participant.add",
                    subject_type="tournament",
                    subject_id=self.tournament_id,
                    payload={
                        "marker": self.marker,
                        "participant_user_id": user.id,
                    },
                )
            )
            await db_session.commit()

        self.session_tokens_by_user_id[user.id] = token
        self.rostered_session_token_digest = token_digest
        result = {
            "id": user.id,
            "label": "rostered-participant",
            "email": user.email,
            "display_name": profile.display_name,
            "rank": deadlock_profile.rank,
            "subrank": deadlock_profile.subrank,
            "roles": list(deadlock_profile.roles or []),
            "heroes": list(deadlock_profile.pool or []),
        }
        self.report["rostered_participant"] = {
            "user_id": user.id,
            "email": user.email,
            "display_name": profile.display_name,
            "assigned_to_team": False,
        }
        self.scenario("rostered_participant_registered", True, self.report["rostered_participant"])
        return result

    async def remove_rostered_participant_session(self) -> None:
        if self.rostered_session_token_digest is None:
            return
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(UserSession).where(UserSession.token_digest == self.rostered_session_token_digest)
            )
            await db_session.commit()
        self.rostered_session_token_digest = None

    async def add_control_participant_session(self) -> dict[str, Any] | None:
        if self.control_participant_email is None:
            return None
        async with session_factory()() as db_session:
            row = (
                await db_session.execute(
                    select(User, PlayerProfile, DeadlockProfile)
                    .join(PlayerProfile, PlayerProfile.user_id == User.id)
                    .join(DeadlockProfile, DeadlockProfile.user_id == User.id)
                    .where(func.lower(User.email) == self.control_participant_email)
                )
            ).one_or_none()
            if row is None:
                raise QaFailure(
                    "control_participant: account and complete Deadlock profile are required"
                )
            user, profile, deadlock_profile = row
            token = new_session_token()
            token_digest = session_token_digest(token)
            db_session.add(
                UserSession(
                    user_id=user.id,
                    token_digest=token_digest,
                    ip_address="127.0.0.1",
                    user_agent=f"platform-production-qa-control:{self.marker}",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db_session.commit()

        self.session_tokens_by_user_id[user.id] = token
        self.control_participant_session_token_digest = token_digest
        return {
            "id": user.id,
            "label": "control-participant",
            "email": user.email,
            "display_name": profile.display_name,
            "rank": deadlock_profile.rank,
            "subrank": deadlock_profile.subrank,
            "roles": list(deadlock_profile.roles or []),
            "heroes": list(deadlock_profile.pool or []),
        }

    async def remove_control_participant_session(self) -> None:
        if self.control_participant_session_token_digest is None:
            return
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(UserSession).where(
                    UserSession.token_digest == self.control_participant_session_token_digest
                )
            )
            await db_session.commit()
        self.control_participant_session_token_digest = None

    async def bounded_each(
        self,
        items: list[dict[str, Any]],
        task,
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        request_semaphore = semaphore or asyncio.Semaphore(self.concurrency)

        async def run_one(item: dict[str, Any]) -> None:
            async with request_semaphore:
                await task(item)

        await asyncio.gather(*(run_one(item) for item in items))

    async def bounded_each_at_rate(
        self,
        items: list[dict[str, Any]],
        task,
        *,
        rate_per_second: float,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        """Start bounded requests at a fixed rate instead of in one burst."""

        if rate_per_second <= 0:
            await self.bounded_each(items, task, semaphore=semaphore)
            return
        request_semaphore = semaphore or asyncio.Semaphore(self.concurrency)
        launch_lock = asyncio.Lock()
        next_launch_at = time.monotonic()
        interval = 1.0 / rate_per_second

        async def run_one(item: dict[str, Any]) -> None:
            nonlocal next_launch_at
            async with launch_lock:
                now = time.monotonic()
                launch_at = max(now, next_launch_at)
                next_launch_at = launch_at + interval
            delay = launch_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            async with request_semaphore:
                await task(item)

        await asyncio.gather(*(run_one(item) for item in items))

    async def bulk_register_scale_users(self) -> list[dict[str, Any]]:
        settings = get_settings()
        now = datetime.now(UTC)
        expires_at = now + timedelta(days=settings.platform_session_ttl_days)
        password_hash = hash_password(self.password)
        users: list[dict[str, Any]] = []
        async with session_factory()() as db_session:
            roles = list(
                (
                    await db_session.scalars(
                        select(Role).where(Role.slug.in_(("authenticated_user", "player")))
                    )
                ).all()
            )
            if {role.slug for role in roles} != {"authenticated_user", "player"}:
                raise QaFailure("bulk_register_scale_users: required roles are missing")

            for batch_start in range(0, self.scale_users, 500):
                batch_end = min(self.scale_users, batch_start + 500)
                batch_users: list[User] = []
                for index in range(batch_start, batch_end):
                    label = "organizer" if index == 0 else f"p{index:05d}"
                    user = User(
                        email=f"{self.marker}-{label}@example.com",
                        display_name=qa_display_name(self.marker, label),
                        status="active",
                        email_verified_at=now,
                        public_tournament_credits=5 if index == 0 else 0,
                        private_tournament_credits=0,
                    )
                    db_session.add(user)
                    batch_users.append(user)
                await db_session.flush()

                for index, user in enumerate(batch_users, start=batch_start):
                    label = "organizer" if index == 0 else f"p{index:05d}"
                    rank = VALID_RANKS[index % len(VALID_RANKS)]
                    roles_for_user = ROLE_PATTERNS[index % len(ROLE_PATTERNS)]
                    heroes = HERO_PATTERNS[index % len(HERO_PATTERNS)]
                    token = new_session_token()
                    self.session_tokens_by_user_id[user.id] = token
                    self.user_ids.append(user.id)
                    users.append(
                        {
                            "id": user.id,
                            "label": label,
                            "profile_index": index,
                            "email": user.email,
                            "display_name": user.display_name,
                            "rank": rank,
                            "subrank": 6 - (index % 6),
                            "roles": roles_for_user,
                            "heroes": heroes,
                        }
                    )
                    db_session.add(
                        PasswordCredential(
                            user_id=user.id,
                            password_hash=password_hash,
                            password_version="argon2id",
                        )
                    )
                    db_session.add(
                        PlayerProfile(
                            user_id=user.id,
                            display_name=user.display_name,
                            captain_team_name=f"T{index + 1:05d}"[:15],
                        )
                    )
                    db_session.add(
                        DeadlockProfile(
                            user_id=user.id,
                            rank=rank,
                            subrank=6 - (index % 6),
                            playtime="1501-2000",
                            roles=list(roles_for_user),
                            pool=list(heroes),
                            captain_priority="yes" if index < self.scale_teams * 2 else "neutral",
                        )
                    )
                    db_session.add(
                        UserSession(
                            user_id=user.id,
                            token_digest=session_token_digest(token),
                            ip_address="127.0.0.1",
                            user_agent="platform-production-qa-scale",
                            expires_at=expires_at,
                        )
                    )
                    for role in roles:
                        db_session.add(UserRole(user_id=user.id, role_id=role.id))
                await db_session.commit()
                self.report["created_users"] = len(users)
                await self.record_preprod_run(progress=True, created_users=len(users))

            # The fixture is inserted directly for deterministic scale setup.
            # Refresh planner statistics before the first authenticated API
            # request so a large synthetic session set cannot be measured with
            # stale estimates from the previous cleanup state.
            await db_session.execute(
                text(
                    "ANALYZE platform.users, platform.sessions, platform.user_roles"
                )
            )
            await db_session.commit()
            self.report["fixture_statistics"] = {
                "analyzed": [
                    "platform.users",
                    "platform.sessions",
                    "platform.user_roles",
                ]
            }

        self.users_by_id.update({user["id"]: user for user in users})
        return users

    def _profile_write_payloads(
        self,
        user: dict[str, Any],
        *,
        changed: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        index = int(user["profile_index"])
        pattern_offset = 1 if changed else 0
        roles = ROLE_PATTERNS[(index + pattern_offset) % len(ROLE_PATTERNS)]
        heroes = HERO_PATTERNS[(index + pattern_offset) % len(HERO_PATTERNS)]
        captain_priority = "yes" if index < self.scale_teams * 2 else "neutral"
        if changed and index >= self.scale_teams * 2:
            captain_priority = "no"
        display_name = qa_display_name(
            self.marker,
            f"u{index:05d}" if not changed else f"v{index:05d}",
        )
        suffix = "b" if changed else "a"
        general = {
            "display_name": display_name,
            "handle": f"{self.marker}-{index:05d}-{suffix}",
            "bio": f"Load profile {self.marker} revision {suffix}.",
            "region": "NA" if changed else "EU",
            "discord_account": f"qa-{self.marker}-{index:05d}-{suffix}",
            "captain_team_name": f"C{index:05d}{suffix}"[:15],
        }
        deadlock = {
            "rank": VALID_RANKS[(index + pattern_offset) % len(VALID_RANKS)],
            "subrank": ((index + pattern_offset) % 6) + 1,
            "playtime": "2001-3000" if changed else "1501-2000",
            "roles": list(roles),
            "pool": list(heroes),
            "captain_priority": captain_priority,
        }
        captain = {
            "captain_team_name": f"C{index:05d}{suffix}"[:15],
            "slots": [
                {
                    "slot_number": slot_number,
                    "allowed_roles": [
                        ROLE_PATTERNS[(index + pattern_offset + slot_number - 1) % len(ROLE_PATTERNS)][0]
                    ],
                    "desired_heroes": [
                        HERO_PATTERNS[(index + pattern_offset + slot_number - 1) % len(HERO_PATTERNS)][0]
                    ],
                }
                for slot_number in range(1, 7)
            ],
        }
        return general, deadlock, captain

    async def run_profile_journey(
        self,
        api_client: httpx.AsyncClient,
        users: list[dict[str, Any]],
    ) -> None:
        if not users:
            return

        async def write_general(user: dict[str, Any], *, changed: bool) -> None:
            general, _, _ = self._profile_write_payloads(user, changed=changed)
            response = await self.request_as(
                api_client,
                user,
                "PUT",
                "/profiles/me",
                expected=200,
                json_payload=general,
            )
            if response.get("display_name") != general["display_name"]:
                raise QaFailure(f"profile_general_persisted: {user['label']}")

        async def write_deadlock(user: dict[str, Any], *, changed: bool) -> None:
            _, deadlock, _ = self._profile_write_payloads(user, changed=changed)
            response = await self.request_as(
                api_client,
                user,
                "PUT",
                "/profiles/me/deadlock",
                expected=200,
                json_payload=deadlock,
            )
            if response.get("rank") != deadlock["rank"]:
                raise QaFailure(f"profile_deadlock_persisted: {user['label']}")

        async def write_captain(user: dict[str, Any], *, changed: bool) -> None:
            _, _, captain = self._profile_write_payloads(user, changed=changed)
            response = await self.request_as(
                api_client,
                user,
                "PUT",
                "/profiles/me/captain",
                expected=200,
                json_payload=captain,
            )
            if response.get("captain_team_name") != captain["captain_team_name"]:
                raise QaFailure(f"profile_captain_persisted: {user['label']}")
            if len(response.get("dream_slots") or []) != len(captain["slots"]):
                raise QaFailure(f"profile_captain_slots_persisted: {user['label']}")

        async def verify_workspace(user: dict[str, Any], *, changed: bool) -> None:
            general, deadlock, captain = self._profile_write_payloads(user, changed=changed)
            workspace = await self.request_as(
                api_client,
                user,
                "GET",
                "/profiles/me/workspace",
                expected=200,
            )
            profile = workspace.get("profile") or {}
            deadlock_profile = workspace.get("deadlock_profile") or {}
            dream_slots = workspace.get("dream_slots") or []
            if (
                profile.get("display_name") != general["display_name"]
                or profile.get("captain_team_name") != captain["captain_team_name"]
                or deadlock_profile.get("rank") != deadlock["rank"]
                or len(dream_slots) != len(captain["slots"])
            ):
                raise QaFailure(f"profile_workspace_persisted: {user['label']}")

        with self.phase("profile_general_initial"):
            await self.bounded_each(users, lambda user: write_general(user, changed=False))
        with self.phase("profile_deadlock_initial"):
            await self.bounded_each(users, lambda user: write_deadlock(user, changed=False))
        with self.phase("profile_captain_initial"):
            await self.bounded_each(users, lambda user: write_captain(user, changed=False))
        with self.phase("profile_workspace_initial_read"):
            await self.bounded_each(users, lambda user: verify_workspace(user, changed=False))
        with self.phase("profile_general_changed"):
            await self.bounded_each(users, lambda user: write_general(user, changed=True))
        with self.phase("profile_deadlock_changed"):
            await self.bounded_each(users, lambda user: write_deadlock(user, changed=True))
        with self.phase("profile_captain_changed"):
            await self.bounded_each(users, lambda user: write_captain(user, changed=True))
        with self.phase("profile_workspace_changed_read"):
            await self.bounded_each(users, lambda user: verify_workspace(user, changed=True))

        self.report["profile_journey"] = {
            "users": len(users),
            "writes_per_user": 6,
            "verification_reads_per_user": 2,
            "phases": [
                "profile_general_initial",
                "profile_deadlock_initial",
                "profile_captain_initial",
                "profile_workspace_initial_read",
                "profile_general_changed",
                "profile_deadlock_changed",
                "profile_captain_changed",
                "profile_workspace_changed_read",
            ],
        }
        self.scenario(
            "scale_profile_journey_persisted",
            True,
            {
                "users": len(users),
                "writes": len(users) * 6,
                "verification_reads": len(users) * 2,
            },
        )

    async def configure_scale_captain_preferences(self, captain_ids: list[str]) -> None:
        slot_roles = ["Carry", "Semi-Carry", "Support", "Semi-Support", "Carry", "Support"]
        slot_heroes = ["Abrams", "Kelvin", "Seven", "Ivy", "Mina", "Apollo"]
        async with session_factory()() as db_session:
            await db_session.execute(delete(DeadlockDreamSlot).where(DeadlockDreamSlot.user_id.in_(captain_ids)))
            for captain_index, captain_id in enumerate(captain_ids):
                for slot_number in range(1, 7):
                    db_session.add(
                        DeadlockDreamSlot(
                            user_id=captain_id,
                            slot_number=slot_number,
                            allowed_roles=[slot_roles[(slot_number + captain_index - 1) % len(slot_roles)]],
                            desired_heroes=[
                                slot_heroes[(slot_number + captain_index - 1) % len(slot_heroes)],
                                slot_heroes[(slot_number + captain_index) % len(slot_heroes)],
                                slot_heroes[(slot_number + captain_index + 1) % len(slot_heroes)],
                                slot_heroes[(slot_number + captain_index + 2) % len(slot_heroes)],
                                slot_heroes[(slot_number + captain_index + 3) % len(slot_heroes)],
                            ],
                        )
                    )
            await db_session.commit()

    async def grant_tournament_permissions(
        self,
        organizers: list[dict[str, Any]],
        admins: list[dict[str, Any]],
    ) -> None:
        if not organizers and not admins:
            return
        organizer_ids = [str(user["id"]) for user in organizers]
        admin_ids = [str(user["id"]) for user in admins]
        async with session_factory()() as db_session:
            if organizer_ids:
                rows = (
                    await db_session.scalars(
                        select(User).where(User.id.in_(organizer_ids))
                    )
                ).all()
                for row in rows:
                    row.public_tournament_credits = max(
                        int(row.public_tournament_credits or 0),
                        2,
                    )
            if admin_ids:
                admin_role = await db_session.scalar(
                    select(Role).where(Role.slug.in_(("admin", "superadmin")))
                )
                if admin_role is not None:
                    existing = set(
                        (
                            await db_session.execute(
                                select(UserRole.user_id, UserRole.role_id).where(
                                    UserRole.user_id.in_(admin_ids),
                                    UserRole.role_id == admin_role.id,
                                )
                            )
                        ).all()
                    )
                    for user_id in admin_ids:
                        key = (user_id, admin_role.id)
                        if key not in existing:
                            db_session.add(
                                UserRole(user_id=user_id, role_id=admin_role.id)
                            )
            await db_session.commit()

    async def join_tournament_participants(
        self,
        api_client: httpx.AsyncClient,
        *,
        tournament: dict[str, Any],
        participants: list[dict[str, Any]],
        request_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        slug = str(tournament["slug"])

        async def join_user(user: dict[str, Any]) -> None:
            await self.request_as(
                api_client,
                user,
                "POST",
                f"/tournaments/{slug}/join",
                expected=201,
                json_payload={"entry_type": "solo"},
            )

        await self.bounded_each(participants, join_user, semaphore=request_semaphore)

    async def setup_tournament_state(
        self,
        api_client: httpx.AsyncClient,
        *,
        tournament: dict[str, Any],
        organizer: dict[str, Any],
        participants: list[dict[str, Any]],
        request_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        category = str(tournament["category"])
        slug = str(tournament["slug"])
        if participants and category != "terminal":
            await self.join_tournament_participants(
                api_client,
                tournament=tournament,
                participants=participants,
                request_semaphore=request_semaphore,
            )
        if category == "registration_open":
            return
        if category == "terminal":
            await self.request_as(
                api_client,
                organizer,
                "PATCH",
                f"/tournaments/{slug}/status",
                expected=200,
                json_payload={"status": "cancelled"},
            )
            return

        await self.request_as(
            api_client,
            organizer,
            "PATCH",
            f"/tournaments/{slug}/status",
            expected=200,
            json_payload={"status": "registration_closed"},
        )
        await self.request_as(
            api_client,
            organizer,
            "POST",
            f"/tournaments/{slug}/deadlock/ready-check/start",
            expected=201,
        )
        if category == "ready_check_active":
            return

        async def vote_yes(user: dict[str, Any]) -> None:
            await self.request_as(
                api_client,
                user,
                "POST",
                f"/tournaments/{slug}/deadlock/ready-check/vote",
                expected=200,
                json_payload={"choice": "yes"},
            )

        await self.bounded_each(participants, vote_yes, semaphore=request_semaphore)
        await self.request_as(
            api_client,
            organizer,
            "POST",
            f"/tournaments/{slug}/deadlock/ready-check/close",
            expected=200,
        )
        await self.request_as(
            api_client,
            organizer,
            "POST",
            f"/tournaments/{slug}/deadlock/captain-round/start",
            expected=201,
            json_payload={"teams_count": READY_TEST_TEAMS},
        )
        final_run = await self.wait_for_auto_assignment_run_as(
            api_client,
            organizer,
            tournament_slug=slug,
            expected_teams=READY_TEST_TEAMS,
        )
        run_id = str(final_run["id"])
        await self.request_as(
            api_client,
            organizer,
            "POST",
            f"/tournaments/{slug}/deadlock/auto-assignment/{run_id}/publish",
            expected=200,
        )
        await self.request_as(
            api_client,
            organizer,
            "POST",
            f"/tournaments/{slug}/deadlock/auto-assignment/{run_id}/lock",
            expected=200,
        )
        await self.request_as(
            api_client,
            organizer,
            "POST",
            f"/tournaments/{slug}/matches/seed-opening-round",
            expected=201,
        )
        await self.request_as(
            api_client,
            organizer,
            "PATCH",
            f"/tournaments/{slug}/status",
            expected=200,
            json_payload={"status": "in_progress"},
        )

    async def create_write_burst_tournament(
        self,
        api_client: httpx.AsyncClient,
        *,
        organizer: dict[str, Any],
        label: str,
        index: int,
    ) -> dict[str, Any]:
        created = await self.request_as(
            api_client,
            organizer,
            "POST",
            "/tournaments",
            expected=201,
            json_payload={
                "name": write_burst_tournament_name(self.marker, index, label),
                "description": f"Write burst profile {self.marker} {label}.",
                "visibility": "public",
                "format_slug": "solo",
                "allowed_ranks": VALID_RANKS,
                "max_participants": self.write_burst_users_per_tournament,
                "match_format": "bo3",
                "final_format": "bo5",
                "teams_count": READY_TEST_TEAMS,
            },
        )
        tournament_id = str(created["id"])
        tournament_slug = str(created["slug"])
        self.tournament_id = self.tournament_id or tournament_id
        self.tournament_slug = self.tournament_slug or tournament_slug
        self.tournament_ids.append(tournament_id)
        self.tournament_slugs.append(tournament_slug)
        self.report["tournament_ids"] = list(self.tournament_ids)
        self.report["tournament_slugs"] = list(self.tournament_slugs)
        await self.request_as(
            api_client,
            organizer,
            "PATCH",
            f"/tournaments/{tournament_slug}/status",
            expected=200,
            json_payload={"status": "registration_open"},
        )
        return {**created, "category": label, "organizer": organizer}

    async def run_spread_requests(
        self,
        items: list[dict[str, Any]],
        *,
        spread_seconds: float,
        task,
    ) -> list[Any]:
        offsets = burst_offsets(count=len(items), spread_seconds=spread_seconds)
        started_at = time.monotonic()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_one(index: int, item: dict[str, Any]) -> Any:
            await asyncio.sleep(max(0.0, started_at + offsets[index] - time.monotonic()))
            async with semaphore:
                return await task(item)

        return list(await asyncio.gather(*(run_one(index, item) for index, item in enumerate(items))))

    def write_burst_profile_summary(
        self,
        *,
        name: str,
        phase: str,
        spread_seconds: float,
        system_sample_start: int,
        system_sample_end: int,
        follow_up_phase_prefix: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "phase": phase,
            "spread_seconds": spread_seconds,
            "http": self.http_metrics.summary(phases={phase}),
            "system": self.system_sampler.summary(
                start=system_sample_start,
                end=system_sample_end,
            ),
            "follow_up_reads": follow_up_read_counts(
                self.http_metrics.samples,
                phase_prefix=follow_up_phase_prefix,
            ),
            **(detail or {}),
        }

    async def run_single_join_bursts(
        self,
        api_client: httpx.AsyncClient,
        *,
        users: list[dict[str, Any]],
        tournament_index_start: int,
    ) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        participants = users[: self.write_burst_users_per_tournament]
        for variant_index, raw_spread in enumerate(WRITE_BURST_JOIN_SPREAD_SECONDS):
            spread = raw_spread * self.write_burst_time_scale
            organizer = users[variant_index]
            with self.phase("write_join_setup"):
                tournament = await self.create_write_burst_tournament(
                    api_client,
                    organizer=organizer,
                    label=f"join_{int(raw_spread)}s",
                    index=tournament_index_start + variant_index,
                )
            slug = str(tournament["slug"])
            phase = f"write_join_burst_{int(raw_spread)}s"
            system_start = len(self.system_sampler.samples)

            async def join_user(user: dict[str, Any]) -> Any:
                return await self.request_as(
                    api_client,
                    user,
                    "POST",
                    f"/tournaments/{slug}/join",
                    expected=201,
                    json_payload={"entry_type": "solo"},
                )

            with self.phase(phase):
                payloads = await self.run_spread_requests(
                    participants,
                    spread_seconds=spread,
                    task=join_user,
                )
            system_end = len(self.system_sampler.samples)
            compact = all(is_compact_mutation_response(payload, max_fields=12) for payload in payloads)
            local_patch_correct = all(
                isinstance(payload, dict) and str(payload.get("user_id")) == str(user["id"])
                for user, payload in zip(participants, payloads)
            )
            with self.phase(f"{phase}_duplicate"):
                await self.request_as(
                    api_client,
                    participants[0],
                    "POST",
                    f"/tournaments/{slug}/join",
                    expected=409,
                    json_payload={"entry_type": "solo"},
                )
            with self.phase(f"{phase}_followup"):
                summary = await self.request_as(
                    api_client,
                    organizer,
                    "GET",
                    f"/tournaments/{slug}",
                    expected=200,
                )
            state_correct = int(summary.get("participant_count") or 0) == len(participants)
            self.scenario(
                f"{phase}_contract_and_state",
                compact and local_patch_correct and state_correct,
                {
                    "compact": compact,
                    "local_patch_correct": local_patch_correct,
                    "participant_count": summary.get("participant_count"),
                },
            )
            profiles.append(
                self.write_burst_profile_summary(
                    name=f"single-tournament-join-{int(raw_spread)}s",
                    phase=phase,
                    spread_seconds=spread,
                    system_sample_start=system_start,
                    system_sample_end=system_end,
                    follow_up_phase_prefix=f"{phase}_followup",
                    detail={
                        "mutations": len(payloads),
                        "compact_response": compact,
                        "duplicate_join_status": 409,
                        "state_correct": state_correct,
                    },
                )
            )
        return profiles

    async def prepare_ready_burst_tournament(
        self,
        api_client: httpx.AsyncClient,
        *,
        organizer: dict[str, Any],
        participants: list[dict[str, Any]],
        label: str,
        index: int,
        start_ready_check: bool = True,
    ) -> dict[str, Any]:
        tournament = await self.create_write_burst_tournament(
            api_client,
            organizer=organizer,
            label=label,
            index=index,
        )
        await self.join_tournament_participants(
            api_client,
            tournament=tournament,
            participants=participants,
        )
        slug = str(tournament["slug"])
        await self.request_as(
            api_client,
            organizer,
            "PATCH",
            f"/tournaments/{slug}/status",
            expected=200,
            json_payload={"status": "registration_closed"},
        )
        if start_ready_check:
            await self.request_as(
                api_client,
                organizer,
                "POST",
                f"/tournaments/{slug}/deadlock/ready-check/start",
                expected=201,
            )
        return tournament

    async def run_single_ready_bursts(
        self,
        api_client: httpx.AsyncClient,
        *,
        users: list[dict[str, Any]],
        tournament_index_start: int,
    ) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        participants = users[: self.write_burst_users_per_tournament]
        for variant_index, raw_spread in enumerate(WRITE_BURST_READY_SPREAD_SECONDS):
            spread = raw_spread * self.write_burst_time_scale
            organizer = users[variant_index + 3]
            with self.phase("write_ready_setup"):
                tournament = await self.prepare_ready_burst_tournament(
                    api_client,
                    organizer=organizer,
                    participants=participants,
                    label=f"ready_{int(raw_spread)}s",
                    index=tournament_index_start + variant_index,
                )
            slug = str(tournament["slug"])
            phase = f"write_ready_burst_{int(raw_spread)}s"
            system_start = len(self.system_sampler.samples)

            async def vote_yes(user: dict[str, Any]) -> Any:
                return await self.request_as(
                    api_client,
                    user,
                    "POST",
                    f"/tournaments/{slug}/deadlock/ready-check/vote",
                    expected=200,
                    json_payload={"choice": "yes"},
                )

            with self.phase(phase):
                payloads = await self.run_spread_requests(
                    participants,
                    spread_seconds=spread,
                    task=vote_yes,
                )
            system_end = len(self.system_sampler.samples)
            compact = all(is_compact_mutation_response(payload, max_fields=7) for payload in payloads)
            with self.phase(f"{phase}_duplicate"):
                duplicate = await vote_yes(participants[0])
            with self.phase(f"{phase}_followup"):
                state = await self.request_as(
                    api_client,
                    organizer,
                    "GET",
                    f"/tournaments/{slug}/deadlock/ready-check",
                    expected=200,
                )
            active_round = state.get("active_round") if isinstance(state, dict) else None
            state_correct = (
                isinstance(active_round, dict)
                and int(active_round.get("ready_count") or 0) == len(participants)
            )
            duplicate_correct = isinstance(duplicate, dict) and duplicate.get("changed") is False
            self.scenario(
                f"{phase}_contract_and_state",
                compact and state_correct and duplicate_correct,
                {
                    "compact": compact,
                    "ready_count": active_round.get("ready_count") if isinstance(active_round, dict) else None,
                    "duplicate_changed": duplicate.get("changed") if isinstance(duplicate, dict) else None,
                },
            )
            profiles.append(
                self.write_burst_profile_summary(
                    name=f"single-tournament-ready-{int(raw_spread)}s",
                    phase=phase,
                    spread_seconds=spread,
                    system_sample_start=system_start,
                    system_sample_end=system_end,
                    follow_up_phase_prefix=f"{phase}_followup",
                    detail={
                        "mutations": len(payloads),
                        "compact_response": compact,
                        "duplicate_vote_unchanged": duplicate_correct,
                        "state_correct": state_correct,
                    },
                )
            )
        return profiles

    async def run_multi_tournament_write_burst(
        self,
        api_client: httpx.AsyncClient,
        *,
        users: list[dict[str, Any]],
        tournament_index_start: int,
    ) -> dict[str, Any]:
        categories = ["join_active"] * 10 + ["ready_check_active"] * 5 + ["bracket_active"] * 3 + ["terminal"] * 2
        chunk_size = self.write_burst_users_per_tournament
        chunks = [users[index * chunk_size : (index + 1) * chunk_size] for index in range(len(categories))]
        tournaments: list[dict[str, Any]] = []
        bracket_snapshots: dict[str, dict[str, Any]] = {}
        with self.phase("write_multi_setup"):
            for index, (category, chunk) in enumerate(zip(categories, chunks)):
                tournament = await self.create_write_burst_tournament(
                    api_client,
                    organizer=chunk[0],
                    label=category,
                    index=tournament_index_start + index,
                )
                tournaments.append(tournament)
                if category == "join_active":
                    continue
                await self.setup_tournament_state(
                    api_client,
                    tournament=tournament,
                    organizer=chunk[0],
                    participants=chunk,
                )
                if category == "bracket_active":
                    bracket_snapshots[str(tournament["slug"])] = await self.request_as(
                        api_client,
                        chunk[0],
                        "GET",
                        f"/tournaments/{tournament['slug']}/bracket?teams_view=summary",
                        expected=200,
                    )

        phase = "write_multi_staggered"
        scaled_spread = WRITE_BURST_MULTI_SPREAD_SECONDS * self.write_burst_time_scale
        scaled_stagger = WRITE_BURST_MULTI_START_STAGGER_SECONDS * self.write_burst_time_scale
        actions: list[dict[str, Any]] = []
        for tournament_index, (tournament, chunk) in enumerate(zip(tournaments, chunks)):
            category = str(tournament["category"])
            base_delay = tournament_index * scaled_stagger
            if category in {"join_active", "ready_check_active"}:
                for user, offset in zip(
                    chunk,
                    burst_offsets(count=len(chunk), spread_seconds=scaled_spread),
                ):
                    actions.append(
                        {
                            "delay": base_delay + offset,
                            "kind": "join" if category == "join_active" else "ready",
                            "tournament": tournament,
                            "user": user,
                        }
                    )
            elif category == "bracket_active":
                actions.append(
                    {
                        "delay": base_delay,
                        "kind": "match_report",
                        "tournament": tournament,
                        "user": chunk[0],
                        "bracket": bracket_snapshots[str(tournament["slug"])],
                    }
                )

        started_at = time.monotonic()
        semaphore = asyncio.Semaphore(self.concurrency)
        system_start = len(self.system_sampler.samples)

        async def execute_action(action: dict[str, Any]) -> Any:
            await asyncio.sleep(max(0.0, started_at + float(action["delay"]) - time.monotonic()))
            tournament = action["tournament"]
            slug = str(tournament["slug"])
            async with semaphore:
                if action["kind"] == "join":
                    return await self.request_as(
                        api_client,
                        action["user"],
                        "POST",
                        f"/tournaments/{slug}/join",
                        expected=201,
                        json_payload={"entry_type": "solo"},
                    )
                if action["kind"] == "ready":
                    return await self.request_as(
                        api_client,
                        action["user"],
                        "POST",
                        f"/tournaments/{slug}/deadlock/ready-check/vote",
                        expected=200,
                        json_payload={"choice": "yes"},
                    )
                bracket = action["bracket"]
                match = list(bracket.get("matches") or [])[0]
                return await self.request_as(
                    api_client,
                    action["user"],
                    "POST",
                    f"/tournaments/{slug}/matches/{match['id']}/report",
                    expected=200,
                    json_payload={
                        "home_score": 3,
                        "away_score": 1,
                        "expected_revision": bracket["revision"],
                    },
                )

        with self.phase(phase):
            results = list(await asyncio.gather(*(execute_action(action) for action in actions)))
        system_end = len(self.system_sampler.samples)

        correct_states = 0
        with self.phase(f"{phase}_followup"):
            for tournament, chunk in zip(tournaments, chunks):
                category = str(tournament["category"])
                slug = str(tournament["slug"])
                if category == "join_active":
                    summary = await self.request_as(api_client, chunk[0], "GET", f"/tournaments/{slug}", expected=200)
                    correct_states += int(summary.get("participant_count") or 0) == len(chunk)
                elif category == "ready_check_active":
                    state = await self.request_as(
                        api_client,
                        chunk[0],
                        "GET",
                        f"/tournaments/{slug}/deadlock/ready-check",
                        expected=200,
                    )
                    active_round = state.get("active_round") if isinstance(state, dict) else None
                    correct_states += (
                        isinstance(active_round, dict)
                        and int(active_round.get("ready_count") or 0) == len(chunk)
                    )
                elif category == "bracket_active":
                    bracket = await self.request_as(
                        api_client,
                        chunk[0],
                        "GET",
                        f"/tournaments/{slug}/bracket?teams_view=summary",
                        expected=200,
                    )
                    correct_states += any(match.get("winner_team_id") for match in bracket.get("matches") or [])
        expected_states = 18
        self.scenario(
            "write_multi_staggered_state_correct",
            correct_states == expected_states,
            {"correct": correct_states, "expected": expected_states},
        )
        return self.write_burst_profile_summary(
            name="multi-tournament-write-staggered",
            phase=phase,
            spread_seconds=scaled_spread,
            system_sample_start=system_start,
            system_sample_end=system_end,
            follow_up_phase_prefix=f"{phase}_followup",
            detail={
                "tournament_plan": dict(Counter(categories)),
                "mutations": len(results),
                "join_mutations": sum(action["kind"] == "join" for action in actions),
                "ready_mutations": sum(action["kind"] == "ready" for action in actions),
                "match_report_mutations": sum(action["kind"] == "match_report" for action in actions),
                "state_checks_correct": correct_states,
            },
        )

    async def run_write_burst_profile(self) -> dict[str, Any]:
        api_client = await self.new_client()
        started = time.monotonic()
        profiles: list[dict[str, Any]] = []
        try:
            await self.record_preprod_run(status="running", requested_users=self.scale_users)
            await self.start_performance_collection()
            with self.phase("write_burst_seed_users"):
                users = await self.bulk_register_scale_users()
            self.preseed_csrf_tokens(users)
            self.scenario("write_burst_users_created", len(users) == self.scale_users, {"users": len(users)})
            organizer_users = list(users[:6])
            if self.write_burst_profile in {"all", "multi-staggered"}:
                organizer_users.extend(
                    users[index * self.write_burst_users_per_tournament]
                    for index in range(WRITE_BURST_TOURNAMENT_COUNT)
                )
            organizers_by_id = {
                str(user["id"]): user
                for user in organizer_users
            }
            await self.grant_tournament_permissions(list(organizers_by_id.values()), [])

            tournament_index = 1
            if self.write_burst_profile in {"all", "single-join"}:
                profiles.extend(
                    await self.run_single_join_bursts(
                        api_client,
                        users=users,
                        tournament_index_start=tournament_index,
                    )
                )
                tournament_index += len(WRITE_BURST_JOIN_SPREAD_SECONDS)
            if self.write_burst_profile in {"all", "single-ready"}:
                profiles.extend(
                    await self.run_single_ready_bursts(
                        api_client,
                        users=users,
                        tournament_index_start=tournament_index,
                    )
                )
                tournament_index += len(WRITE_BURST_READY_SPREAD_SECONDS)
            if self.write_burst_profile in {"all", "multi-staggered"}:
                profiles.append(
                    await self.run_multi_tournament_write_burst(
                        api_client,
                        users=users,
                        tournament_index_start=tournament_index,
                    )
                )

            acceptance = evaluate_write_burst_profiles(profiles)
            acceptance["enforced"] = self.write_burst_time_scale >= 1.0
            self.report["write_burst"] = {
                "profile": WRITE_BURST_PROFILE_NAME,
                "selection": self.write_burst_profile,
                "users_per_tournament": self.write_burst_users_per_tournament,
                "time_scale": self.write_burst_time_scale,
                "profiles": profiles,
                "load_generator_local": is_local_origin(self.origin),
                "csrf_preseeded": self.csrf_tokens_preseeded,
                "acceptance": acceptance,
                "follow_up_reads": follow_up_read_counts(
                    self.http_metrics.samples,
                    phase_prefix="write_",
                    phase_token="_followup",
                ),
            }
            self.scenario(
                "write_burst_http_errors_zero",
                self.http_metrics.summary()["errors"] == 0,
                {"errors": self.http_metrics.summary()["errors"]},
            )
            self.scenario(
                "write_burst_target_budget",
                acceptance["healthy"] or not acceptance["enforced"],
                acceptance,
            )
            self.report["duration_seconds"] = round(time.monotonic() - started, 4)
            self.scenario("write_burst_profile_complete", all(item["ok"] for item in self.scenarios))
            await self.record_preprod_run(
                status="passed",
                created_users=len(users),
                tournaments_created=len(self.tournament_ids),
                finished_at=datetime.now(UTC),
            )
        except Exception:
            await self.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
            raise
        finally:
            await self.stop_performance_collection()
            for client in self.clients:
                await client.aclose()
            if not self.keep_data:
                cleanup = await self.cleanup_targeted()
                self.report["cleanup"] = cleanup
                self.scenarios.append(
                    {
                        "name": "write_burst_cleanup",
                        "ok": cleanup.get("ok", False),
                        "detail": cleanup,
                    }
                )
                await self.record_preprod_run(
                    status="cleaned" if cleanup.get("ok") else "failed",
                    cleanup_state=cleanup,
                    finished_at=datetime.now(UTC),
                )
            else:
                self.report["cleanup"] = {"ok": False, "kept": True}
            self.report["duration_seconds"] = round(time.monotonic() - started, 4)
            self.report["finished_at"] = datetime.now(UTC).isoformat()
            self.report["passed"] = all(item["ok"] for item in self.scenarios)
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            await self.record_preprod_run(report_path=str(self.report_path))
        return self.report

    async def run_scale_site_mix(
        self,
        users: list[dict[str, Any]],
        *,
        include_auto_assignment: bool,
        include_bracket: bool,
        manual_refresh: bool = False,
        bracket_user_limit: int | None = None,
    ) -> None:
        if not users:
            return
        api_client = await self.new_client()
        bracket_user_limit = len(users) if bracket_user_limit is None else bracket_user_limit
        workspace_etags: dict[str, str] = {}
        bracket_etags: dict[str, str] = {}

        async def browse(user: dict[str, Any]) -> None:
            await self.request_as(api_client, user, "GET", "/auth/session", expected=200)
            await self.request_as(api_client, user, "GET", "/users/me", expected=200)
            await self.request_as(api_client, user, "GET", "/profiles/me", expected=200)
            await self.request_as(api_client, user, "GET", "/profiles/me/deadlock", expected=200)
            await self.request_as(
                api_client,
                user,
                "GET",
                "/profiles/me/deadlock/dream-slots",
                expected=200,
            )
            await self.request_as(
                api_client,
                user,
                "GET",
                "/tournaments?limit=9&offset=0&open_registration=true",
                expected=200,
            )
            await self.request_as(
                api_client,
                user,
                "GET",
                f"/tournaments/{self.tournament_slug}",
                expected=200,
            )
            _, _, workspace_headers = await self.request_as(
                api_client,
                user,
                "GET",
                f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=detail&include_current_user=false",
                expected=200,
                return_response_meta=True,
            )
            etag = workspace_headers.get("etag")
            if etag:
                workspace_etags[str(user["id"])] = etag
            if include_auto_assignment:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment",
                    expected=200,
                )
            if include_bracket:
                _, _, bracket_headers = await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=bracket&include_current_user=false",
                    expected=200,
                    return_response_meta=True,
                )
                bracket_etag = bracket_headers.get("etag")
                if bracket_etag:
                    bracket_etags[str(user["id"])] = bracket_etag

        await self.bounded_each(users[: self.scale_site_mix_users], browse)
        if manual_refresh and workspace_etags:
            async def refresh_workspace(user: dict[str, Any]) -> None:
                etag = workspace_etags.get(str(user["id"]))
                if not etag:
                    return
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=detail&include_current_user=false",
                    expected=(200, 304),
                    extra_headers={"If-None-Match": etag},
                )

            with self.phase("manual_workspace_refresh"):
                await self.bounded_each(users[: self.scale_site_mix_users], refresh_workspace)
        if include_bracket and bracket_user_limit > self.scale_site_mix_users:
            remaining = users[self.scale_site_mix_users : bracket_user_limit]
            async def view_bracket(user: dict[str, Any]) -> None:
                _, _, bracket_headers = await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=bracket&include_current_user=false",
                    expected=200,
                    return_response_meta=True,
                )
                bracket_etag = bracket_headers.get("etag")
                if bracket_etag:
                    bracket_etags[str(user["id"])] = bracket_etag

            await self.bounded_each(remaining, view_bracket)
        if manual_refresh and bracket_etags:
            async def refresh_bracket(user: dict[str, Any]) -> None:
                etag = bracket_etags.get(str(user["id"]))
                if not etag:
                    return
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=bracket&include_current_user=false",
                    expected=(200, 304),
                    extra_headers={"If-None-Match": etag},
                )

            bracket_users = [
                user
                for user in users[:bracket_user_limit]
                if str(user["id"]) in bracket_etags
            ]
            with self.phase("manual_bracket_refresh"):
                await self.bounded_each(bracket_users, refresh_bracket)

    async def join_scale_participant(
        self,
        api_client: httpx.AsyncClient,
        user: dict[str, Any],
        *,
        invite_code: str | None,
        organizer_id: str,
    ) -> None:
        if invite_code is not None and str(user["id"]) != organizer_id:
            await self.request_as(
                api_client,
                user,
                "POST",
                "/tournaments/invites/claim",
                expected=201,
                json_payload={
                    "code": invite_code,
                    "entry_type": "solo",
                    "team_name": None,
                },
            )
        await self.request_as(
            api_client,
            user,
            "POST",
            f"/tournaments/{self.tournament_slug}/join",
            expected=201,
            json_payload={"entry_type": "solo"},
        )

    async def register_user(
        self,
        *,
        label: str,
        rank: str,
        index: int,
    ) -> dict[str, Any]:
        client = await self.new_client()
        email = f"{self.marker}-{label}@example.com"
        display_name = qa_display_name(self.marker, label)
        registered = await self.request(
            client,
            "POST",
            "/auth/register",
            expected=201,
            json_payload={
                "email": email,
                "password": self.password,
                "display_name": display_name,
            },
        )
        roles = ROLE_PATTERNS[index % len(ROLE_PATTERNS)]
        heroes = HERO_PATTERNS[index % len(HERO_PATTERNS)]
        await self.request(
            client,
            "PUT",
            "/profiles/me/deadlock",
            expected=200,
            json_payload={
                "rank": rank,
                "subrank": 6 - (index % 6),
                "playtime": "1501-2000",
                "roles": roles,
                "pool": heroes,
                "captain_priority": "neutral",
            },
        )
        user_id = str(registered["user"]["id"])
        user = {
            "id": user_id,
            "label": label,
            "email": email,
            "display_name": display_name,
            "rank": rank,
            "subrank": 6 - (index % 6),
            "roles": roles,
            "heroes": heroes,
            "client": client,
        }
        self.users_by_id[user_id] = user
        self.user_ids.append(user_id)
        return user

    async def claim_and_join(
        self,
        user: dict[str, Any],
        *,
        invite_code: str,
        expected_join: int,
    ) -> Any:
        if user["id"] != self.user_ids[0]:
            await self.request(
                user["client"],
                "POST",
                "/tournaments/invites/claim",
                expected=201,
                json_payload={
                    "code": invite_code,
                    "entry_type": "solo",
                    "team_name": None,
                },
            )
        response = await user["client"].post(
            f"/tournaments/{self.tournament_slug}/join",
            json={"entry_type": "solo"},
        )
        if response.status_code != expected_join:
            raise QaFailure(
                f"join {user['label']}: expected {expected_join}, got "
                f"{response.status_code}: {response.text}"
            )
        return response.json()

    async def configure_captain_preferences(self, captain_ids: list[str]) -> None:
        slot_roles = ["Carry", "Semi-Carry", "Support", "Semi-Support", "Carry", "Support"]
        slot_heroes = ["Abrams", "Kelvin", "Seven", "Ivy", "Mina", "Apollo"]
        for captain_index, captain_id in enumerate(captain_ids):
            user = self.users_by_id[captain_id]
            slots = [
                {
                    "slot_number": slot_number,
                    "allowed_roles": [slot_roles[(slot_number + captain_index - 1) % len(slot_roles)]],
                    "desired_heroes": [slot_heroes[(slot_number + captain_index - 1) % len(slot_heroes)]],
                }
                for slot_number in range(1, 7)
            ]
            await self.request(
                user["client"],
                "PUT",
                "/profiles/me/deadlock/dream-slots",
                expected=200,
                json_payload={"slots": slots},
            )

    async def run_scale(self) -> dict[str, Any]:
        api_client = await self.new_client()
        try:
            await self.record_preprod_run(status="running", requested_users=self.scale_users)
            started = time.monotonic()
            await self.start_performance_collection()
            with self.phase("bulk_seed_users"):
                users = await self.bulk_register_scale_users()
            organizer = users[0]
            self.scenario(
                "scale_users_and_profiles_created",
                len(users) == self.scale_users,
                {"users": len(users)},
            )
            if self.profile_journey:
                await self.run_profile_journey(api_client, users)

            with self.phase("tournament_setup"):
                created = await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    "/tournaments",
                    expected=201,
                    json_payload={
                        "name": self.tournament_name,
                        "description": f"Large preprod QA tournament {self.marker}.",
                        "visibility": self.tournament_visibility,
                        "format_slug": "solo",
                        "allowed_ranks": VALID_RANKS,
                        "max_participants": self.scale_users + 64,
                        "match_format": "bo3",
                        "final_format": "bo5",
                        "teams_count": self.scale_teams,
                    },
                )
            self.tournament_id = str(created["id"])
            self.tournament_slug = str(created["slug"])
            self.report["tournament_id"] = self.tournament_id
            self.report["scale_tournament_id"] = self.tournament_id
            self.report["tournament_ids"] = [self.tournament_id]
            self.report["tournament_slug"] = self.tournament_slug
            await self.record_preprod_run(tournaments_created=1)
            with self.phase("tournament_setup"):
                await self.request_as(
                    api_client,
                    organizer,
                    "PATCH",
                    f"/tournaments/{self.tournament_slug}/status",
                    expected=200,
                    json_payload={"status": "registration_open"},
                )
            invite_code: str | None = None
            if self.tournament_visibility == "invite_only":
                with self.phase("invite_setup"):
                    invite = await self.request_as(
                        api_client,
                        organizer,
                        "POST",
                        f"/tournaments/{self.tournament_slug}/invites",
                        expected=201,
                        json_payload={
                            "note": self.marker,
                            "max_uses": self.scale_users + 64,
                            "expires_at": None,
                        },
                    )
                invite_code = str(invite["code"])
                self.report["invite_code"] = invite_code
                self.scenario(
                    "scale_invite_only_code_created",
                    bool(invite_code),
                    {"visibility": self.tournament_visibility},
                )

            if self.scale_site_mix_users > 0 and self.tournament_visibility == "public":
                site_mix_started = time.monotonic()
                with self.phase("public_site_mix"):
                    await self.run_scale_site_mix(
                        users,
                        include_auto_assignment=False,
                        include_bracket=(
                            self.mode == "read-mix" and self.scale_bracket_view_users > 0
                        ),
                        manual_refresh=self.mode == "read-mix",
                        bracket_user_limit=self.scale_bracket_view_users,
                    )
                self.report["public_site_mix_seconds"] = round(
                    time.monotonic() - site_mix_started,
                    4,
                )
                self.scenario(
                    "scale_public_site_mix_complete",
                    True,
                    {
                        "users": self.scale_site_mix_users,
                        "seconds": self.report["public_site_mix_seconds"],
                    },
                )
                if self.mode == "read-mix":
                    self.report["read_mix"] = {
                        "users": self.scale_site_mix_users,
                        "manual_workspace_refresh": True,
                        "manual_bracket_refresh": self.scale_bracket_view_users > 0,
                        "bracket_view_users": self.scale_bracket_view_users,
                        "network_traffic_before_transition": "none",
                        "bracket_refresh": "manual_only",
                    }
                    self.report["duration_seconds"] = round(time.monotonic() - started, 4)
                    self.scenario(
                        "read_mix_complete",
                        True,
                        {
                            "users": self.scale_site_mix_users,
                            "manual_workspace_refresh": True,
                            "seconds": self.report["duration_seconds"],
                        },
                    )
                    await self.record_preprod_run(
                        status="passed",
                        created_users=len(users),
                        tournaments_created=1,
                        active_participants=0,
                        teams_count=0,
                        matches_count=0,
                        finished_at=datetime.now(UTC),
                    )
                    return self.report
            elif self.scale_site_mix_users > 0:
                self.report["public_site_mix_skipped"] = {
                    "users": self.scale_site_mix_users,
                    "reason": "invite_only participants must claim the invite before reads",
                }

            join_participants = users
            control_participant = await self.add_control_participant_session()

            async def join_user(user: dict[str, Any]) -> None:
                await self.join_scale_participant(
                    api_client,
                    user,
                    invite_code=invite_code,
                    organizer_id=str(organizer["id"]),
                )

            join_started = time.monotonic()
            with self.phase("join_tournament"):
                await self.bounded_each(join_participants, join_user)
                if control_participant is not None:
                    await self.join_scale_participant(
                        api_client,
                        control_participant,
                        invite_code=invite_code,
                        organizer_id=str(organizer["id"]),
                    )
                rostered_participant = await self.add_rostered_participant()
            workflow_users = users + (
                [rostered_participant] if rostered_participant is not None else []
            )
            if control_participant is not None and self.control_participant_state in {
                "ready",
                "assigned",
            }:
                workflow_users.append(control_participant)
            if control_participant is not None:
                self.report["control_participant"] = {
                    "user_id": control_participant["id"],
                    "email": control_participant["email"],
                    "state": self.control_participant_state,
                    "joined_via_api": True,
                    "ready_choice": self.control_participant_state in {"ready", "assigned"},
                    "assigned_to_team": False,
                }
            join_seconds = time.monotonic() - join_started
            self.report["join_seconds"] = round(join_seconds, 4)
            self.scenario(
                "scale_joined_all_players",
                True,
                {
                    "organizer_joined_via_api": True,
                    "joined_via_api": len(join_participants),
                    "eligible_expected": len(workflow_users),
                    "seconds": round(join_seconds, 4),
                },
            )
            await self.record_preprod_run(active_participants=len(workflow_users))

            with self.phase("ready_setup"):
                await self.request_as(
                    api_client,
                    organizer,
                    "PATCH",
                    f"/tournaments/{self.tournament_slug}/status",
                    expected=200,
                    json_payload={"status": "registration_closed"},
                )
                ready = await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/ready-check/start",
                    expected=201,
                )
            self.scenario(
                "scale_ready_check_eligible",
                ready["eligible_participant_count"] >= len(workflow_users),
                {
                    "eligible": ready["eligible_participant_count"],
                    "workflow_users": len(workflow_users),
                    "external_participants": ready["eligible_participant_count"] - len(workflow_users),
                },
            )

            async def view_ready_state(user: dict[str, Any]) -> None:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/deadlock/ready-check",
                    expected=200,
                )

            ready_view_started = time.monotonic()
            with self.phase("ready_state_views"):
                await self.bounded_each(workflow_users, view_ready_state)
            self.report["ready_state_view_seconds"] = round(
                time.monotonic() - ready_view_started,
                4,
            )
            self.scenario(
                "scale_ready_state_views_complete",
                True,
                {
                    "users": len(workflow_users),
                    "seconds": self.report["ready_state_view_seconds"],
                },
            )

            async def vote_yes(user: dict[str, Any]) -> None:
                await self.request_as(
                    api_client,
                    user,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/ready-check/vote",
                    expected=200,
                    json_payload={"choice": "yes"},
                )

            vote_started = time.monotonic()
            with self.phase("ready_votes"):
                await self.bounded_each(workflow_users, vote_yes)
            vote_seconds = time.monotonic() - vote_started
            self.report["ready_vote_seconds"] = round(vote_seconds, 4)
            with self.phase("ready_close"):
                closed_ready = await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/ready-check/close",
                    expected=200,
                )
            self.scenario(
                "scale_ready_check_all_confirmed",
                closed_ready["ready_count"] == len(workflow_users),
                {
                    "ready": closed_ready["ready_count"],
                    "workflow_users": len(workflow_users),
                    "eligible": ready["eligible_participant_count"],
                    "external_participants_removed": ready["eligible_participant_count"] - closed_ready["ready_count"],
                    "seconds": round(vote_seconds, 4),
                },
            )

            with self.phase("captain_round"):
                captain_round = await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/captain-round/start",
                    expected=201,
                    json_payload={"teams_count": self.scale_teams},
                )
            self.scenario(
                "scale_captain_round_auto_finalized",
                captain_round["status"] == "finalized"
                and captain_round["assigned_count"] == self.scale_teams,
                {
                    "status": captain_round["status"],
                    "assigned": captain_round["assigned_count"],
                    "candidates": captain_round["candidate_count"],
                },
            )

            captain_ids = [
                str(row["user_id"])
                for row in captain_round["entries"]
                if row["state"] == "assigned"
            ]
            self.scenario(
                "scale_captains_selected",
                len(captain_ids) == self.scale_teams,
                {
                    "captains": len(captain_ids),
                },
            )

            protected_user_ids = {
                str(user["id"])
                for user in (rostered_participant, control_participant)
                if user is not None
            }
            await self.configure_scale_captain_preferences(
                [user_id for user_id in captain_ids if user_id not in protected_user_ids]
            )
            assignment_started = time.monotonic()
            with self.phase("auto_assignment_run"):
                final_run = await self.wait_for_auto_assignment_run_as(
                    api_client,
                    organizer,
                    expected_teams=self.scale_teams,
                )
            assignment_seconds = time.monotonic() - assignment_started
            self.report["assignment_seconds"] = round(assignment_seconds, 4)
            self.report["assignment_task_id"] = final_run.get("queued_task_id")
            self.report["preference_metrics"] = final_run["preference_metrics"]
            self.report["optimization_summary"] = final_run["optimization_summary"]
            self.scenario(
                "scale_assignment_generated",
                len(final_run["teams"]) == self.scale_teams,
                {
                    "teams": len(final_run["teams"]),
                    "candidate_pool": len(final_run["candidate_pool_user_ids"]),
                    "leftovers": len(final_run["leftover_user_ids"]),
                    "seconds": round(assignment_seconds, 4),
                },
            )
            run_id = str(final_run["id"])
            with self.phase("auto_assignment_publish_lock"):
                await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment/{run_id}/publish",
                    expected=200,
                )
                locked = await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment/{run_id}/lock",
                    expected=200,
                )
            self.scenario("scale_assignment_publish_lock", locked["status"] == "locked")
            if rostered_participant is not None:
                rostered_user_id = str(rostered_participant["id"])
                assigned_team = next(
                    (
                        team
                        for team in final_run["teams"]
                        if str(team["captain"]["user_id"]) == rostered_user_id
                        or any(
                            str(slot["assigned_player"]["user_id"]) == rostered_user_id
                            for slot in team.get("starter_slots") or []
                        )
                        or (
                            team.get("reserve_slot") is not None
                            and str(team["reserve_slot"]["assigned_player"]["user_id"]) == rostered_user_id
                        )
                    ),
                    None,
                )
                rostered_report = self.report["rostered_participant"]
                rostered_report.update({
                    "assigned_to_team": assigned_team is not None,
                    "team_id": assigned_team.get("team_id") if assigned_team else None,
                    "team_name": assigned_team.get("team_name") if assigned_team else None,
                })
                self.scenario(
                    "rostered_participant_assigned",
                    assigned_team is not None,
                    rostered_report,
                )
            if control_participant is not None:
                control_user_id = str(control_participant["id"])
                assigned_team = next(
                    (
                        team
                        for team in final_run["teams"]
                        if str(team["captain"]["user_id"]) == control_user_id
                        or any(
                            str(slot["assigned_player"]["user_id"]) == control_user_id
                            for slot in team.get("starter_slots") or []
                        )
                        or (
                            team.get("reserve_slot") is not None
                            and str(team["reserve_slot"]["assigned_player"]["user_id"])
                            == control_user_id
                        )
                    ),
                    None,
                )
                control_report = self.report["control_participant"]
                control_report.update({
                    "assigned_to_team": assigned_team is not None,
                    "team_id": assigned_team.get("team_id") if assigned_team else None,
                    "team_name": assigned_team.get("team_name") if assigned_team else None,
                })
                if self.control_participant_state == "assigned":
                    # The control account is an observer for the retained
                    # assignment scenario.  The allocator may legitimately
                    # leave one eligible candidate out when there are more
                    # candidates than team slots, so a specific control user
                    # must not turn a valid assignment run into a harness
                    # failure.
                    control_report["assignment_requested"] = True

            with self.phase("bracket_seed"):
                await self.request_as(
                    api_client,
                    organizer,
                    "POST",
                    f"/tournaments/{self.tournament_slug}/matches/seed-opening-round",
                    expected=201,
                )
                bracket = await self.request_as(
                    api_client,
                    organizer,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/bracket",
                    expected=200,
                )
            self.report["teams"] = [summarize_team(team) for team in final_run["teams"][: min(32, len(final_run["teams"]))]]
            self.report["strength_ranking"] = [
                {
                    "rank": team["seed"],
                    "team_id": team["id"],
                    "team_name": team["name"],
                    "starter_strength": team["starter_strength"],
                    "starter_average_strength": team["starter_average_strength"],
                }
                for team in sorted(bracket["teams"], key=lambda item: int(item["seed"] or 999))
            ]
            self.report["preference_metrics_by_team"] = [
                summarize_team_preferences(team) for team in final_run["teams"][: min(32, len(final_run["teams"]))]
            ]
            self.report["teams_count"] = len(bracket["teams"])
            self.report["matches_count"] = len(bracket["matches"])
            if rostered_participant is not None:
                rostered_team_id = self.report["rostered_participant"].get("team_id")
                rostered_team = next(
                    (team for team in bracket["teams"] if team.get("id") == rostered_team_id),
                    None,
                )
                self.report["rostered_participant"]["team_name"] = (
                    rostered_team.get("name") if rostered_team else None
                )
            self.report["final_view_profile"] = self.scale_final_view_profile
            self.scenario(
                "scale_bracket_seeded",
                len(bracket["teams"]) == self.scale_teams
                and len(bracket["matches"]) == self.scale_teams - 1,
                {"teams": len(bracket["teams"]), "matches": len(bracket["matches"])},
            )

            with self.phase("retained_participant"):
                retained_participant = await self.add_retained_participant()
            active_participants = len(workflow_users) + (1 if retained_participant is not None else 0)

            with self.phase("final_assignment_state_sample"):
                assignment_state = await self.request_as(
                    api_client,
                    organizer,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment",
                    expected=200,
                )
            self.scenario(
                "scale_assignment_state_sample_loaded",
                bool(assignment_state.get("published_run") or assignment_state.get("latest_run")),
                {
                    "latest": assignment_state.get("latest_run", {}).get("status")
                    if isinstance(assignment_state.get("latest_run"), dict)
                    else None,
                    "published": assignment_state.get("published_run", {}).get("status")
                    if isinstance(assignment_state.get("published_run"), dict)
                    else None,
                },
            )

            async def view_final_workspace_legacy(user: dict[str, Any]) -> None:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/workspace?participants_limit=25&participants_offset=0&workspace_view=bracket",
                    expected=200,
                )

                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/bracket",
                    expected=200,
                )

            async def view_final_bracket_shell(user: dict[str, Any]) -> None:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=bracket_summary",
                    expected=200,
                )

            async def view_final_bracket_full(user: dict[str, Any]) -> None:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/bracket?teams_view=summary",
                    expected=200,
                )

            final_view_users = users[: self.scale_bracket_view_users]
            if final_view_users:
                final_views_started = time.monotonic()
                if self.scale_final_view_profile == "legacy":
                    with self.phase("final_assignment_bracket_views"):
                        await self.bounded_each(final_view_users, view_final_workspace_legacy)
                else:
                    bracket_shell_started = time.monotonic()
                    with self.phase("final_assignment_bracket_shell_views"):
                        await self.bounded_each(final_view_users, view_final_bracket_shell)
                    self.report["final_assignment_bracket_shell_view_seconds"] = round(
                        time.monotonic() - bracket_shell_started,
                        4,
                    )
                    bracket_full_started = time.monotonic()
                    with self.phase("final_assignment_bracket_full_views"):
                        await self.bounded_each(final_view_users, view_final_bracket_full)
                    self.report["final_assignment_bracket_full_view_seconds"] = round(
                        time.monotonic() - bracket_full_started,
                        4,
                    )
                self.report["final_assignment_bracket_view_seconds"] = round(
                    time.monotonic() - final_views_started,
                    4,
                )
                self.scenario(
                    "scale_final_assignment_bracket_views_complete",
                    True,
                    {
                        "users": len(final_view_users),
                        "seconds": self.report["final_assignment_bracket_view_seconds"],
                        "profile": self.scale_final_view_profile,
                    },
                )

            self.report["duration_seconds"] = round(time.monotonic() - started, 4)
            self.scenario("scale_workflow_complete", all(item["ok"] for item in self.scenarios))
            await self.record_preprod_run(
                status="passed",
                created_users=len(users),
                tournaments_created=1,
                active_participants=active_participants,
                teams_count=len(bracket["teams"]),
                matches_count=len(bracket["matches"]),
                finished_at=datetime.now(UTC),
            )
        except Exception:
            await self.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
            raise
        finally:
            await self.stop_performance_collection()
            with suppress(Exception):
                await self.remove_rostered_participant_session()
            with suppress(Exception):
                await self.remove_control_participant_session()
            for client in self.clients:
                await client.aclose()
            if not self.keep_data:
                cleanup = await self.cleanup_targeted()
                self.report["cleanup"] = cleanup
                self.scenarios.append(
                    {
                        "name": "targeted_cleanup",
                        "ok": cleanup.get("ok", False),
                        "detail": cleanup,
                    }
                )
                await self.record_preprod_run(
                    status="cleaned" if cleanup.get("ok") else "failed",
                    cleanup_state=cleanup,
                    finished_at=datetime.now(UTC),
                )
            else:
                self.report["cleanup"] = {"ok": False, "kept": True}
            self.report["finished_at"] = datetime.now(UTC).isoformat()
            self.report["passed"] = all(item["ok"] for item in self.scenarios)
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            await self.record_preprod_run(report_path=str(self.report_path))
        return self.report

    async def run_tournament_lifecycle(self) -> dict[str, Any]:
        """Exercise the complete multi-tournament Deadlock lifecycle.

        This mode deliberately uses the same API requests as the current web
        flow. Fixture users and sessions are seeded through the existing QA
        setup helper; every measured workflow action remains an HTTP request
        against the real platform API.
        """

        if self.mode != "tournament-lifecycle":
            raise QaFailure("run_tournament_lifecycle requires tournament-lifecycle mode")

        async def bounded_map(items: list[dict[str, Any]], task) -> list[Any]:
            semaphore = asyncio.Semaphore(self.concurrency)

            async def run_one(item: dict[str, Any]) -> Any:
                async with semaphore:
                    return await task(item)

            return list(await asyncio.gather(*(run_one(item) for item in items)))

        async def create_tournament(
            api_client: httpx.AsyncClient,
            organizer: dict[str, Any],
            index: int,
        ) -> dict[str, Any]:
            created = await self.request_as(
                api_client,
                organizer,
                "POST",
                "/tournaments",
                expected=201,
                json_payload={
                    "name": f"LC{index + 1:02d}-{self.marker[-12:]}"[:25],
                    "description": f"Tournament lifecycle QA {self.marker} {index + 1}.",
                    "visibility": "public",
                    "format_slug": "solo",
                    "allowed_ranks": VALID_RANKS,
                    "max_participants": self.lifecycle_users_per_tournament,
                    "match_format": "bo3",
                    "final_format": "bo5",
                    "teams_count": self.lifecycle_teams_count,
                },
            )
            slug = str(created["slug"])
            await self.request_as(
                api_client,
                organizer,
                "PATCH",
                f"/tournaments/{slug}/status",
                expected=200,
                json_payload={"status": "registration_open"},
            )
            tournament_id = str(created["id"])
            self.tournament_ids.append(tournament_id)
            self.tournament_slugs.append(slug)
            if self.tournament_id is None:
                self.tournament_id = tournament_id
                self.tournament_slug = slug
            self.report["tournament_ids"] = list(dict.fromkeys(self.tournament_ids))
            self.report["tournament_slugs"] = list(dict.fromkeys(self.tournament_slugs))
            return {
                **created,
                "slug": slug,
                "organizer": organizer,
                "index": index,
            }

        async def read_detail(
            item: dict[str, Any],
            *,
            expected_team_count: int | None = None,
            expected_status: str | None = None,
            return_payload: bool = False,
        ) -> dict[str, Any] | None:
            tournament = item["tournament"]
            user = item["user"]
            payload = await self.request_as(
                api_client,
                user,
                "GET",
                f"/tournaments/{tournament['slug']}/workspace?participants_limit=0"
                "&participants_offset=0&workspace_view=detail&include_current_user=false",
                expected=200,
            )
            if payload.get("participants") != []:
                raise QaFailure(
                    "tournament lifecycle detail workspace unexpectedly returned participant rows"
                )
            if payload.get("participants_limit") != 0:
                raise QaFailure("tournament lifecycle detail workspace changed participants_limit")
            if int(payload.get("participants_total") or 0) != self.lifecycle_users_per_tournament:
                raise QaFailure("tournament lifecycle detail workspace participant total mismatch")
            if payload.get("current_user") is not None:
                raise QaFailure("tournament lifecycle detail workspace returned current_user")
            tournament_payload = payload.get("tournament") or {}
            if expected_status is not None and tournament_payload.get("status") != expected_status:
                raise QaFailure(
                    f"tournament lifecycle detail status mismatch: expected {expected_status}"
                )
            bracket = payload.get("bracket") or {}
            teams = bracket.get("teams") or []
            if expected_team_count is not None and len(teams) != expected_team_count:
                raise QaFailure(
                    "tournament lifecycle detail normalized team state mismatch: "
                    f"expected {expected_team_count}, got {len(teams)}"
                )
            if expected_team_count:
                member_count = sum(len(team.get("members") or []) for team in teams)
                expected_member_count = expected_team_count * 7
                if member_count != expected_member_count:
                    raise QaFailure(
                        "tournament lifecycle detail normalized team member count mismatch: "
                        f"expected {expected_member_count}, got {member_count}"
                    )
            return payload if return_payload else None

        async def mass_detail_reads(
            items: list[dict[str, Any]],
            *,
            expected_team_count: int | None = None,
            expected_status: str | None = None,
        ) -> dict[str, dict[str, Any]]:
            first_user_by_slug = {
                str(slug): users_for_tournament[0]
                for slug, users_for_tournament in tournament_users.items()
            }

            async def read_one(item: dict[str, Any]) -> dict[str, Any] | None:
                is_first = item["user"]["id"] == first_user_by_slug[
                    str(item["tournament"]["slug"])
                ].get("id")
                return await read_detail(
                    item,
                    expected_team_count=expected_team_count,
                    expected_status=expected_status,
                    return_payload=is_first,
                )

            payloads = await bounded_map(items, read_one)
            return {
                str(item["tournament"]["slug"]): payload
                for item, payload in zip(items, payloads)
                if payload is not None
            }

        async def profile_read(item: dict[str, Any], target_user_id: str) -> None:
            payload = await self.request_as(
                api_client,
                item["user"],
                "GET",
                f"/tournaments/{item['tournament']['slug']}/profiles/{target_user_id}",
                expected=200,
            )
            profile = payload.get("profile") if isinstance(payload, dict) else None
            if not isinstance(profile, dict) or str(profile.get("user_id")) != target_user_id:
                raise QaFailure("tournament-scoped profile response did not match requested user")

        async def mass_profile_reads(
            items: list[dict[str, Any]],
            target_key: str,
        ) -> None:
            await bounded_map(
                items,
                lambda item: profile_read(item, str(item[target_key])),
            )

        async def mass_bracket_workspace_reads(
            items: list[dict[str, Any]],
            etags: dict[tuple[str, str], str],
        ) -> dict[str, dict[str, Any]]:
            first_user_by_slug = {
                str(slug): users_for_tournament[0]
                for slug, users_for_tournament in tournament_users.items()
            }

            async def read_one(item: dict[str, Any]) -> dict[str, Any] | None:
                payload, status_code, headers = await self.request_as(
                    api_client,
                    item["user"],
                    "GET",
                    f"/tournaments/{item['tournament']['slug']}/workspace?participants_limit=0"
                    "&participants_offset=0&workspace_view=bracket&include_current_user=false",
                    expected=200,
                    return_response_meta=True,
                )
                if status_code != 200 or not isinstance(payload, dict):
                    raise QaFailure("initial bracket workspace read did not return 200 JSON")
                bracket = payload.get("bracket") or {}
                if len(bracket.get("teams") or []) != self.lifecycle_teams_count:
                    raise QaFailure("initial bracket workspace team count mismatch")
                if len(bracket.get("matches") or []) != self.lifecycle_teams_count - 1:
                    raise QaFailure("initial bracket workspace match count mismatch")
                etag = headers.get("etag") or headers.get("ETag")
                if not etag:
                    raise QaFailure("initial bracket workspace response did not include ETag")
                etags[(str(item["tournament"]["slug"]), str(item["user"]["id"]))] = etag
                if item["user"]["id"] == first_user_by_slug[
                    str(item["tournament"]["slug"])
                ].get("id"):
                    return payload
                return None

            payloads = await bounded_map(items, read_one)
            return {
                str(item["tournament"]["slug"]): payload
                for item, payload in zip(items, payloads)
                if payload is not None
            }

        async def mass_bracket_workspace_304(
            items: list[dict[str, Any]],
            etags: dict[tuple[str, str], str],
        ) -> None:
            async def read_one(item: dict[str, Any]) -> None:
                key = (str(item["tournament"]["slug"]), str(item["user"]["id"]))
                etag = etags.get(key)
                if not etag:
                    raise QaFailure("missing ETag for conditional initial bracket workspace read")
                payload, status_code, _ = await self.request_as(
                    api_client,
                    item["user"],
                    "GET",
                    f"/tournaments/{item['tournament']['slug']}/workspace?participants_limit=0"
                    "&participants_offset=0&workspace_view=bracket&include_current_user=false",
                    expected=304,
                    extra_headers={"If-None-Match": etag},
                    return_response_meta=True,
                )
                if status_code != 304 or payload is not None:
                    raise QaFailure("conditional initial bracket workspace read did not return 304")

            await bounded_map(items, read_one)

        async def mass_summary_reads(
            items: list[dict[str, Any]],
            etags: dict[tuple[str, str], str],
        ) -> None:
            async def read_one(item: dict[str, Any]) -> None:
                payload, status_code, headers = await self.request_as(
                    api_client,
                    item["user"],
                    "GET",
                    f"/tournaments/{item['tournament']['slug']}/bracket?teams_view=summary",
                    expected=200,
                    return_response_meta=True,
                )
                if status_code != 200 or not isinstance(payload, dict):
                    raise QaFailure("bracket summary refresh did not return 200 JSON")
                etag = headers.get("etag") or headers.get("ETag")
                if not etag:
                    raise QaFailure("bracket summary refresh response did not include ETag")
                etags[(str(item["tournament"]["slug"]), str(item["user"]["id"]))] = etag

            await bounded_map(items, read_one)

        async def mass_summary_304(
            items: list[dict[str, Any]],
            etags: dict[tuple[str, str], str],
        ) -> None:
            async def read_one(item: dict[str, Any]) -> None:
                key = (str(item["tournament"]["slug"]), str(item["user"]["id"]))
                etag = etags.get(key)
                if not etag:
                    raise QaFailure("missing ETag for conditional bracket summary refresh")
                payload, status_code, _ = await self.request_as(
                    api_client,
                    item["user"],
                    "GET",
                    f"/tournaments/{item['tournament']['slug']}/bracket?teams_view=summary",
                    expected=304,
                    extra_headers={"If-None-Match": etag},
                    return_response_meta=True,
                )
                if status_code != 304 or payload is not None:
                    raise QaFailure("conditional bracket summary refresh did not return 304")

            await bounded_map(items, read_one)

        async def queue_and_wait_assignment(tournament: dict[str, Any]) -> dict[str, Any]:
            organizer = tournament["organizer"]
            slug = str(tournament["slug"])
            previous_run_id = None
            queue_started_at = time.monotonic()
            queue_started_utc = datetime.now(UTC)
            job = await self.request_as(
                api_client,
                organizer,
                "POST",
                f"/tournaments/{slug}/deadlock/auto-assignment/run-async",
                expected=202,
            )
            queued_at = time.monotonic()
            queued_at_utc = datetime.now(UTC)
            final_run = await self._poll_auto_assignment_run_as(
                api_client,
                organizer,
                tournament_slug=slug,
                previous_run_id=previous_run_id,
                expected_teams=self.lifecycle_teams_count,
                task_id=str(job["task_id"]),
            )
            finished_at = time.monotonic()
            created_at_raw = final_run.get("created_at")
            queue_delay_seconds: float | None = None
            if isinstance(created_at_raw, str):
                with suppress(ValueError):
                    created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
                    queue_delay_seconds = max(
                        0.0,
                        (created_at - queued_at_utc).total_seconds(),
                    )
            return {
                "tournament": tournament,
                "run": final_run,
                "task_id": str(job["task_id"]),
                "queue_started_utc": queue_started_utc.isoformat(),
                "queue_delay_reference_utc": queued_at_utc.isoformat(),
                "queue_delay_seconds": queue_delay_seconds,
                "duration_seconds": round(finished_at - queue_started_at, 4),
                "worker_observed_seconds": round(finished_at - queued_at, 4),
            }

        async def team_materialization_timings(
            run_ids_by_slug: dict[str, str],
        ) -> list[float]:
            if not run_ids_by_slug:
                return []
            async with session_factory()() as db_session:
                rows = (
                    await db_session.execute(
                        select(
                            TournamentDeadlockAssignmentRun.id,
                            TournamentDeadlockAssignmentRun.created_at,
                            func.min(TournamentTeam.created_at),
                            func.max(TournamentTeam.created_at),
                        )
                        .join(
                            TournamentTeam,
                            TournamentTeam.source_assignment_run_id
                            == TournamentDeadlockAssignmentRun.id,
                        )
                        .where(
                            TournamentDeadlockAssignmentRun.id.in_(list(run_ids_by_slug.values()))
                        )
                        .group_by(
                            TournamentDeadlockAssignmentRun.id,
                            TournamentDeadlockAssignmentRun.created_at,
                        )
                    )
                ).all()
            durations: list[float] = []
            for _, run_created_at, first_team_created_at, last_team_created_at in rows:
                if run_created_at is None or last_team_created_at is None:
                    continue
                durations.append(
                    max(
                        0.0,
                        (last_team_created_at - run_created_at).total_seconds(),
                    )
                )
            return durations

        started = time.monotonic()
        api_client = await self.new_client()
        try:
            await self.record_preprod_run(status="running", requested_users=self.scale_users)
            await self.start_performance_collection()
            users = await self.run_lifecycle_phase(
                "lifecycle_fixture_seed",
                self.bulk_register_scale_users,
            )
            self.preseed_csrf_tokens(users)
            organizer_count = self.lifecycle_tournament_count
            organizers = users[:organizer_count]
            self.scenario(
                "lifecycle_fixture_shape",
                len(users) == self.lifecycle_tournament_count * self.lifecycle_users_per_tournament,
                {
                    "users": len(users),
                    "tournaments": self.lifecycle_tournament_count,
                    "users_per_tournament": self.lifecycle_users_per_tournament,
                },
            )

            tournament_users: dict[str, list[dict[str, Any]]] = {}
            await self.run_lifecycle_phase(
                "lifecycle_setup_permissions",
                lambda: self.grant_tournament_permissions(organizers, []),
            )
            tournaments = await self.run_lifecycle_phase(
                "lifecycle_tournament_setup",
                lambda: bounded_map(
                    [
                        {"organizer": organizer, "index": index}
                        for index, organizer in enumerate(organizers)
                    ],
                    lambda item: create_tournament(
                        api_client,
                        item["organizer"],
                        int(item["index"]),
                    ),
                ),
            )
            self.tournament_ids = list(dict.fromkeys(self.tournament_ids))
            self.tournament_slugs = list(dict.fromkeys(self.tournament_slugs))
            self.tournament_id = self.tournament_id or str(tournaments[0]["id"])
            self.tournament_slug = self.tournament_slug or str(tournaments[0]["slug"])
            self.report["tournament_ids"] = list(self.tournament_ids)
            self.report["tournament_slugs"] = list(self.tournament_slugs)
            for index, tournament in enumerate(tournaments):
                start_index = index * self.lifecycle_users_per_tournament
                tournament_users[str(tournament["slug"])] = users[
                    start_index : start_index + self.lifecycle_users_per_tournament
                ]
            self.report["created_users"] = len(users)
            await self.record_preprod_run(
                created_users=len(users),
                tournaments_created=len(tournaments),
            )

            all_items = [
                {"tournament": tournament, "user": user}
                for tournament in tournaments
                for user in tournament_users[str(tournament["slug"])]
            ]

            await self.run_lifecycle_phase(
                "mass_join",
                lambda: bounded_map(
                    all_items,
                    lambda item: self.request_as(
                        api_client,
                        item["user"],
                        "POST",
                        f"/tournaments/{item['tournament']['slug']}/join",
                        expected=201,
                        json_payload={"entry_type": "solo"},
                    ),
                ),
            )
            self.scenario(
                "lifecycle_mass_join_complete",
                True,
                {"requests": len(all_items)},
            )

            await self.run_lifecycle_phase(
                "mass_tournament_detail_workspace_reads",
                lambda: mass_detail_reads(all_items),
            )

            await self.run_lifecycle_phase(
                "registration_close",
                lambda: bounded_map(
                    tournaments,
                    lambda tournament: self.request_as(
                        api_client,
                        tournament["organizer"],
                        "PATCH",
                        f"/tournaments/{tournament['slug']}/status",
                        expected=200,
                        json_payload={"status": "registration_closed"},
                    ),
                ),
            )

            async def start_ready_check(tournament: dict[str, Any]) -> Any:
                return await self.request_as(
                    api_client,
                    tournament["organizer"],
                    "POST",
                    f"/tournaments/{tournament['slug']}/deadlock/ready-check/start",
                    expected=201,
                )

            ready_starts = await self.run_lifecycle_phase(
                "ready_check_start",
                lambda: bounded_map(tournaments, start_ready_check),
            )
            if any(
                int(payload.get("eligible_participant_count") or 0)
                != self.lifecycle_users_per_tournament
                for payload in ready_starts
            ):
                raise QaFailure("ready-check start did not expose all tournament participants")

            await self.run_lifecycle_phase(
                "mass_ready_check_state_reads",
                lambda: bounded_map(
                    all_items,
                    lambda item: self.request_as(
                        api_client,
                        item["user"],
                        "GET",
                        f"/tournaments/{item['tournament']['slug']}/deadlock/ready-check",
                        expected=200,
                    ),
                ),
            )

            async def ready_vote_with_bounded_retry(item: dict[str, Any]) -> Any:
                for attempt in range(9):
                    payload, status_code, headers = await self.request_as(
                        api_client,
                        item["user"],
                        "POST",
                        f"/tournaments/{item['tournament']['slug']}/deadlock/ready-check/vote",
                        expected=(200, 503),
                        json_payload={"choice": "yes"},
                        return_response_meta=True,
                        record_ok_statuses={200},
                    )
                    if status_code == 200:
                        return payload
                    if attempt == 8:
                        raise QaFailure(
                            "ready vote remained overloaded after the bounded lifecycle retry budget"
                        )
                    retry_after_ms = (
                        int(payload.get("retry_after_ms") or 250)
                        if isinstance(payload, dict)
                        else 250
                    )
                    retry_after_header = headers.get("retry-after") or headers.get("Retry-After")
                    if retry_after_header:
                        with suppress(ValueError):
                            retry_after_ms = max(
                                retry_after_ms,
                                int(float(retry_after_header) * 1000),
                            )
                    await asyncio.sleep(min(2.0, max(0.15, retry_after_ms / 1000)))
                raise AssertionError("unreachable")

            async def mass_ready_votes() -> None:
                # Start at the reviewed per-worker admission contour. The
                # controller may raise its limit while the wave is healthy,
                # but a single isolated QA worker must not begin above its
                # initial limit and shed every first attempt. The full user
                # population is still issued and transient 503 attempts stay
                # visible in the phase metrics.
                vote_semaphore = asyncio.Semaphore(min(self.concurrency, 8))

                async def run_one(item: dict[str, Any]) -> None:
                    async with vote_semaphore:
                        await ready_vote_with_bounded_retry(item)

                await asyncio.gather(*(run_one(item) for item in all_items))

            await self.run_lifecycle_phase("mass_ready_votes", mass_ready_votes)

            async def close_ready_check(tournament: dict[str, Any]) -> Any:
                return await self.request_as(
                    api_client,
                    tournament["organizer"],
                    "POST",
                    f"/tournaments/{tournament['slug']}/deadlock/ready-check/close",
                    expected=200,
                )

            closed_ready = await self.run_lifecycle_phase(
                "ready_check_close",
                lambda: bounded_map(tournaments, close_ready_check),
            )
            if any(
                int(payload.get("ready_count") or 0) != self.lifecycle_users_per_tournament
                for payload in closed_ready
            ):
                raise QaFailure("ready-check close did not retain all ready voters")

            async def start_captain_round(tournament: dict[str, Any]) -> Any:
                return await self.request_as(
                    api_client,
                    tournament["organizer"],
                    "POST",
                    f"/tournaments/{tournament['slug']}/deadlock/captain-round/start",
                    expected=201,
                    json_payload={"teams_count": self.lifecycle_teams_count},
                )

            captain_rounds = await self.run_lifecycle_phase(
                "captain_round_start",
                lambda: bounded_map(tournaments, start_captain_round),
            )
            if any(
                payload.get("status") != "finalized"
                or int(payload.get("assigned_count") or 0) != self.lifecycle_teams_count
                for payload in captain_rounds
            ):
                raise QaFailure("captain-round did not finalize the requested team count")

            assignment_results = await self.run_lifecycle_phase(
                "auto_assignment_wait",
                lambda: bounded_map(tournaments, queue_and_wait_assignment),
            )
            run_by_slug = {
                str(result["tournament"]["slug"]): result["run"]
                for result in assignment_results
            }
            run_id_by_slug = {
                slug: str(run["id"])
                for slug, run in run_by_slug.items()
            }
            queue_delays = [
                float(result["queue_delay_seconds"])
                for result in assignment_results
                if result.get("queue_delay_seconds") is not None
            ]
            assignment_durations = [float(result["duration_seconds"]) for result in assignment_results]
            self.lifecycle_timings["auto_assignment_duration"] = {
                "per_tournament_seconds": assignment_durations,
                "statistics_ms": metric_stats([value * 1000 for value in assignment_durations]),
            }
            self.lifecycle_timings["assignment_queue_delay"] = {
                "definition": "worker run.created_at minus enqueue request completion; includes queue/start delay",
                "per_tournament_seconds": queue_delays,
                "statistics_ms": metric_stats([value * 1000 for value in queue_delays]),
            }
            if any(len(result["run"].get("teams") or []) != self.lifecycle_teams_count for result in assignment_results):
                raise QaFailure("auto-assignment did not generate the requested number of teams")

            async def publish_assignment(tournament: dict[str, Any]) -> dict[str, Any]:
                slug = str(tournament["slug"])
                run_id = run_id_by_slug[slug]
                started_at = time.monotonic()
                payload = await self.request_as(
                    api_client,
                    tournament["organizer"],
                    "POST",
                    f"/tournaments/{slug}/deadlock/auto-assignment/{run_id}/publish",
                    expected=200,
                )
                return {"payload": payload, "duration_seconds": time.monotonic() - started_at}

            published = await self.run_lifecycle_phase(
                "assignment_publish",
                lambda: bounded_map(tournaments, publish_assignment),
            )
            publish_durations = [float(item["duration_seconds"]) for item in published]
            self.lifecycle_timings["assignment_publish_duration"] = {
                "per_tournament_seconds": publish_durations,
                "statistics_ms": metric_stats([value * 1000 for value in publish_durations]),
            }
            if any(item["payload"].get("status") != "published" for item in published):
                raise QaFailure("assignment publish did not return published state")

            async def lock_assignment(tournament: dict[str, Any]) -> dict[str, Any]:
                slug = str(tournament["slug"])
                run_id = run_id_by_slug[slug]
                started_at = time.monotonic()
                payload = await self.request_as(
                    api_client,
                    tournament["organizer"],
                    "POST",
                    f"/tournaments/{slug}/deadlock/auto-assignment/{run_id}/lock",
                    expected=200,
                )
                return {"payload": payload, "duration_seconds": time.monotonic() - started_at}

            locked = await self.run_lifecycle_phase(
                "assignment_lock",
                lambda: bounded_map(tournaments, lock_assignment),
            )
            lock_durations = [float(item["duration_seconds"]) for item in locked]
            self.lifecycle_timings["assignment_lock_duration"] = {
                "per_tournament_seconds": lock_durations,
                "statistics_ms": metric_stats([value * 1000 for value in lock_durations]),
            }
            if any(item["payload"].get("status") != "locked" for item in locked):
                raise QaFailure("assignment lock did not return locked state")

            materialization_durations = await team_materialization_timings(run_id_by_slug)
            self.lifecycle_timings["team_materialization_duration"] = {
                "definition": "max materialized team created_at minus assignment run created_at",
                "per_tournament_seconds": materialization_durations,
                "statistics_ms": metric_stats([value * 1000 for value in materialization_durations]),
            }

            detail_payloads = await self.run_lifecycle_phase(
                "post_assignment_detail_read",
                lambda: mass_detail_reads(
                    all_items,
                    expected_team_count=self.lifecycle_teams_count,
                ),
            )
            self.lifecycle_timings["post_assignment_detail_read"] = {
                "phase_duration_seconds": self.lifecycle_phase_metrics["post_assignment_detail_read"][
                    "phase_duration_seconds"
                ],
            }
            if len(detail_payloads) != self.lifecycle_tournament_count:
                raise QaFailure("post-assignment detail reads did not cover every tournament")

            profile_targets: list[dict[str, Any]] = []
            for tournament in tournaments:
                slug = str(tournament["slug"])
                teams = list((detail_payloads[slug].get("bracket") or {}).get("teams") or [])
                if len(teams) != self.lifecycle_teams_count:
                    raise QaFailure("normalized detail teams are missing before profile reads")
                members_by_team = [
                    [str(member["user_id"]) for member in team.get("members") or []]
                    for team in teams
                ]
                member_team_index = {
                    user_id: index
                    for index, member_ids in enumerate(members_by_team)
                    for user_id in member_ids
                }
                materialized_ids = [user_id for member_ids in members_by_team for user_id in member_ids]
                if not materialized_ids:
                    raise QaFailure("normalized detail teams contain no materialized members")
                users_for_tournament = tournament_users[slug]
                for user in users_for_tournament:
                    user_id = str(user["id"])
                    team_index = member_team_index.get(user_id)
                    if team_index is not None:
                        own_team = members_by_team[team_index]
                        teammate_id = next(member_id for member_id in own_team if member_id != user_id)
                        opponent_team = members_by_team[(team_index + 1) % len(members_by_team)]
                    else:
                        teammate_id = members_by_team[0][0]
                        opponent_team = members_by_team[1]
                    profile_targets.append(
                        {
                            "tournament": tournament,
                            "user": user,
                            "teammate_id": teammate_id,
                            "opponent_id": opponent_team[0],
                        }
                    )

            self.lifecycle_timings["tournament_profile_read"] = {
                "teammate_phase": "teammate_profile_reads",
                "opponent_phase": "opponent_profile_reads",
            }
            await self.run_lifecycle_phase(
                "teammate_profile_reads",
                lambda: mass_profile_reads(profile_targets, "teammate_id"),
            )
            await self.run_lifecycle_phase(
                "opponent_profile_reads",
                lambda: mass_profile_reads(profile_targets, "opponent_id"),
            )

            async def seed_opening_round(tournament: dict[str, Any]) -> dict[str, Any]:
                started_at = time.monotonic()
                payload = await self.request_as(
                    api_client,
                    tournament["organizer"],
                    "POST",
                    f"/tournaments/{tournament['slug']}/matches/seed-opening-round",
                    expected=201,
                )
                return {"matches": payload, "duration_seconds": time.monotonic() - started_at}

            seeded = await self.run_lifecycle_phase(
                "bracket_seed",
                lambda: bounded_map(tournaments, seed_opening_round),
            )
            seed_durations = [float(item["duration_seconds"]) for item in seeded]
            self.lifecycle_timings["bracket_seed_duration"] = {
                "per_tournament_seconds": seed_durations,
                "statistics_ms": metric_stats([value * 1000 for value in seed_durations]),
            }
            if any(len(item["matches"]) != self.lifecycle_teams_count // 2 for item in seeded):
                raise QaFailure("opening-round seed did not create the expected number of matches")

            initial_bracket_payloads: dict[str, dict[str, Any]] = {}
            initial_bracket_etags: dict[tuple[str, str], str] = {}
            initial_bracket_payloads = await self.run_lifecycle_phase(
                "initial_bracket_workspace_read_200",
                lambda: mass_bracket_workspace_reads(all_items, initial_bracket_etags),
            )
            await self.run_lifecycle_phase(
                "initial_bracket_workspace_read_304",
                lambda: mass_bracket_workspace_304(all_items, initial_bracket_etags),
            )
            self.lifecycle_timings["initial_bracket_workspace_read"] = {
                "phase_duration_seconds": self.lifecycle_phase_metrics[
                    "initial_bracket_workspace_read_200"
                ]["phase_duration_seconds"],
                "conditional_304_phase_duration_seconds": self.lifecycle_phase_metrics[
                    "initial_bracket_workspace_read_304"
                ]["phase_duration_seconds"],
            }

            await self.run_lifecycle_phase(
                "tournament_start",
                lambda: bounded_map(
                    tournaments,
                    lambda tournament: self.request_as(
                        api_client,
                        tournament["organizer"],
                        "PATCH",
                        f"/tournaments/{tournament['slug']}/status",
                        expected=200,
                        json_payload={"status": "in_progress"},
                    ),
                ),
            )

            current_matches_by_slug: dict[str, list[dict[str, Any]]] = {}
            current_revision_by_slug: dict[str, int] = {}
            for tournament in tournaments:
                slug = str(tournament["slug"])
                bracket = initial_bracket_payloads[slug].get("bracket") or {}
                current_matches_by_slug[slug] = sorted(
                    [
                        match
                        for match in bracket.get("matches") or []
                        if int(match.get("round_number") or 0) == 1
                    ],
                    key=lambda match: int(match.get("match_order") or 0),
                )
                current_revision_by_slug[slug] = int(bracket.get("revision") or 0)

            summary_etags: dict[tuple[str, str], str] = {}
            round_phase_durations: list[float] = []
            match_phase_durations: list[float] = []
            completed = False
            round_number = 1
            total_rounds = self.lifecycle_teams_count.bit_length() - 1
            while not completed:
                report_started = time.monotonic()
                phase_name = f"match_report_round_{round_number}"

                async def report_match(item: dict[str, Any]) -> Any:
                    tournament = item["tournament"]
                    slug = str(tournament["slug"])
                    match = item["match"]
                    is_final = round_number == total_rounds
                    home_score, away_score = (3, 0) if is_final else (2, 0)
                    return await self.request_as(
                        api_client,
                        tournament["organizer"],
                        "POST",
                        f"/tournaments/{slug}/matches/{match['id']}/report",
                        expected=200,
                        json_payload={
                            "home_score": home_score,
                            "away_score": away_score,
                            "expected_revision": current_revision_by_slug[slug],
                        },
                    )

                async def report_round() -> None:
                    max_matches = max(
                        len(current_matches_by_slug[str(tournament["slug"])])
                        for tournament in tournaments
                    )
                    for wave_index in range(max_matches):
                        wave = [
                            {
                                "tournament": tournament,
                                "match": current_matches_by_slug[str(tournament["slug"])][wave_index],
                            }
                            for tournament in tournaments
                            if wave_index < len(current_matches_by_slug[str(tournament["slug"])])
                        ]
                        await bounded_map(wave, report_match)
                        for item in wave:
                            slug = str(item["tournament"]["slug"])
                            current_revision_by_slug[slug] += 1

                await self.run_lifecycle_phase(phase_name, report_round)
                match_phase_durations.append(time.monotonic() - report_started)
                if round_number < total_rounds:
                    async def seed_next_round(tournament: dict[str, Any]) -> dict[str, Any]:
                        started_at = time.monotonic()
                        payload = await self.request_as(
                            api_client,
                            tournament["organizer"],
                            "POST",
                            f"/tournaments/{tournament['slug']}/matches/seed-next-round",
                            expected=201,
                        )
                        return {"matches": payload, "duration_seconds": time.monotonic() - started_at}

                    next_round = await self.run_lifecycle_phase(
                        f"bracket_seed_next_round_{round_number}",
                        lambda: bounded_map(tournaments, seed_next_round),
                    )
                    for tournament, seeded_next in zip(tournaments, next_round):
                        slug = str(tournament["slug"])
                        current_matches_by_slug[slug] = sorted(
                            list(seeded_next["matches"] or []),
                            key=lambda match: int(match.get("match_order") or 0),
                        )
                        # seed-opening-round creates the full graph in the current
                        # backend flow. seed-next-round returns the existing next
                        # round in that case and does not advance bracket_revision.

                refresh_started = time.monotonic()
                await self.run_lifecycle_phase(
                    f"bracket_summary_refresh_round_{round_number}_200",
                    lambda: mass_summary_reads(all_items, summary_etags),
                )
                await self.run_lifecycle_phase(
                    f"bracket_summary_refresh_round_{round_number}_304",
                    lambda: mass_summary_304(all_items, summary_etags),
                )
                round_phase_durations.append(time.monotonic() - refresh_started)

                profile_phase_name = f"mixed_bracket_profile_reads_round_{round_number}"
                await self.run_lifecycle_phase(
                    profile_phase_name,
                    lambda: bounded_map(
                        profile_targets,
                        lambda item: profile_read(
                            item,
                            str(
                                item["teammate_id"]
                                if int(item["user"]["profile_index"]) % 2 == 0
                                else item["opponent_id"]
                            ),
                        ),
                    ),
                )
                completed = round_number == total_rounds
                round_number += 1

            self.lifecycle_timings["match_report"] = {
                "round_phase_durations_seconds": match_phase_durations,
                "total_seconds": round(sum(match_phase_durations), 4),
                "statistics_ms": metric_stats([value * 1000 for value in match_phase_durations]),
            }
            self.lifecycle_timings["bracket_summary_refresh"] = {
                "round_phase_durations_seconds": round_phase_durations,
                "total_seconds": round(sum(round_phase_durations), 4),
                "conditional_status": 304,
            }

            async def completed_state_storm() -> None:
                await asyncio.gather(
                    mass_detail_reads(
                        all_items,
                        expected_team_count=self.lifecycle_teams_count,
                        expected_status="completed",
                    ),
                    mass_summary_reads(all_items, summary_etags),
                )
                await mass_summary_304(all_items, summary_etags)
                await mass_profile_reads(profile_targets, "opponent_id")

            await self.run_lifecycle_phase(
                "completed_state_read_storm",
                completed_state_storm,
            )

            async with session_factory()() as db_session:
                completed_count = int(
                    await db_session.scalar(
                        select(func.count())
                        .select_from(Tournament)
                        .where(
                            Tournament.id.in_(self.tournament_ids),
                            Tournament.status == "completed",
                        )
                    )
                    or 0
                )
                match_count = int(
                    await db_session.scalar(
                        select(func.count())
                        .select_from(TournamentMatch)
                        .where(TournamentMatch.tournament_id.in_(self.tournament_ids))
                    )
                    or 0
                )
            expected_match_count = self.lifecycle_tournament_count * (self.lifecycle_teams_count - 1)
            self.report["matches_count"] = match_count
            self.report["teams_count"] = self.lifecycle_tournament_count * self.lifecycle_teams_count
            self.scenario(
                "tournament_lifecycle_completed",
                completed_count == self.lifecycle_tournament_count
                and match_count == expected_match_count,
                {
                    "completed_tournaments": completed_count,
                    "expected_tournaments": self.lifecycle_tournament_count,
                    "matches": match_count,
                    "expected_matches": expected_match_count,
                },
            )
            self.report["duration_seconds"] = round(time.monotonic() - started, 4)
            await self.record_preprod_run(
                status="passed",
                created_users=len(users),
                tournaments_created=len(tournaments),
                active_participants=0,
                teams_count=self.lifecycle_tournament_count * self.lifecycle_teams_count,
                matches_count=match_count,
                finished_at=datetime.now(UTC),
            )
        except Exception:
            await self.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
            raise
        finally:
            await self.stop_performance_collection()
            for client in self.clients:
                await client.aclose()
            if not self.keep_data:
                cleanup = await self.cleanup_targeted()
                self.report["cleanup"] = cleanup
                self.scenarios.append(
                    {
                        "name": "tournament_lifecycle_cleanup",
                        "ok": cleanup.get("ok", False),
                        "detail": cleanup,
                    }
                )
                await self.record_preprod_run(
                    status="cleaned" if cleanup.get("ok") else "failed",
                    cleanup_state=cleanup,
                    finished_at=datetime.now(UTC),
                )
            else:
                self.report["cleanup"] = {"ok": False, "kept": True}
            self.report["finished_at"] = datetime.now(UTC).isoformat()
            self.report["passed"] = all(item["ok"] for item in self.scenarios)
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            await self.record_preprod_run(report_path=str(self.report_path))
        return self.report

    async def run(self) -> dict[str, Any]:
        organizer: dict[str, Any] | None = None
        started = time.monotonic()
        try:
            await self.start_performance_collection()
            organizer = await self.register_user(
                label="organizer",
                rank="Eternus",
                index=0,
            )
            valid_players = [organizer]
            for index in range(1, 57):
                valid_players.append(
                    await self.register_user(
                        label=f"p{index:02d}",
                        rank=VALID_RANKS[index % len(VALID_RANKS)],
                        index=index,
                    )
                )
            rank_rejected = await self.register_user(
                label="rank-reject",
                rank="Initiate",
                index=57,
            )
            capacity_rejected = await self.register_user(
                label="capacity-reject",
                rank="Phantom",
                index=58,
            )
            self.scenario("auth_and_profiles", len(self.user_ids) == 59, {"users": len(self.user_ids)})

            created = await self.request(
                organizer["client"],
                "POST",
                "/tournaments",
                expected=201,
                json_payload={
                    "name": f"QA {self.marker}"[:25],
                    "description": f"Targeted production QA tournament {self.marker}.",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "allowed_ranks": VALID_RANKS,
                    "max_participants": 57,
                    "match_format": "bo3",
                    "final_format": "bo5",
                    "teams_count": 8,
                },
            )
            self.tournament_id = str(created["id"])
            self.tournament_slug = str(created["slug"])
            await self.request(
                organizer["client"],
                "PATCH",
                f"/tournaments/{self.tournament_slug}/status",
                expected=200,
                json_payload={"status": "registration_open"},
            )
            invite = await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/invites",
                expected=201,
                json_payload={
                    "note": self.marker,
                    "max_uses": 60,
                    "expires_at": None,
                },
            )
            invite_code = str(invite["code"])
            self.scenario("invite_only_tournament_created", True, self.tournament_slug)

            rank_response = await rank_rejected["client"].post(
                "/tournaments/invites/claim",
                json={"code": invite_code, "entry_type": "solo", "team_name": None},
            )
            self.scenario("rank_invite_claim", rank_response.status_code == 201, rank_response.text)
            rank_join = await rank_rejected["client"].post(
                f"/tournaments/{self.tournament_slug}/join",
                json={"entry_type": "solo"},
            )
            self.scenario(
                "rank_rejection",
                rank_join.status_code == 409
                and "outside" in str(rank_join.json().get("detail", "")).lower(),
                {"status": rank_join.status_code, "body": rank_join.json()},
            )

            for user in valid_players:
                await self.claim_and_join(user, invite_code=invite_code, expected_join=201)

            capacity_claim = await capacity_rejected["client"].post(
                "/tournaments/invites/claim",
                json={"code": invite_code, "entry_type": "solo", "team_name": None},
            )
            self.scenario(
                "capacity_invite_claim",
                capacity_claim.status_code == 201,
                capacity_claim.text,
            )
            capacity_join = await capacity_rejected["client"].post(
                f"/tournaments/{self.tournament_slug}/join",
                json={"entry_type": "solo"},
            )
            self.scenario(
                "capacity_rejection",
                capacity_join.status_code == 409
                and "limit" in str(capacity_join.json().get("detail", "")).lower(),
                {"status": capacity_join.status_code, "body": capacity_join.json()},
            )

            participants = await self.request(
                organizer["client"],
                "GET",
                f"/tournaments/{self.tournament_slug}/participants",
                expected=200,
            )
            self.scenario("registered_participants_57", len(participants) == 57, len(participants))
            participant_by_user = {str(row["user_id"]): row for row in participants}
            disqualified_user = valid_players[-1]
            await self.request(
                organizer["client"],
                "PATCH",
                (
                    f"/tournaments/{self.tournament_slug}/participants/"
                    f"{participant_by_user[disqualified_user['id']]['id']}/moderation"
                ),
                expected=200,
                json_payload={
                    "status": "disqualified",
                    "moderation_note": self.marker,
                },
            )
            active_players = valid_players[:-1]
            self.scenario("moderation_leaves_56_active", len(active_players) == 56)

            await self.request(
                organizer["client"],
                "PATCH",
                f"/tournaments/{self.tournament_slug}/status",
                expected=200,
                json_payload={"status": "registration_closed"},
            )
            ready = await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/deadlock/ready-check/start",
                expected=201,
            )
            self.scenario(
                "ready_check_56_eligible",
                ready["eligible_participant_count"] == 56,
                ready["eligible_participant_count"],
            )
            for user in active_players:
                await self.request(
                    user["client"],
                    "POST",
                    f"/tournaments/{self.tournament_slug}/deadlock/ready-check/vote",
                    expected=200,
                    json_payload={"choice": "yes"},
                )
            closed_ready = await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/deadlock/ready-check/close",
                expected=200,
            )
            self.scenario("ready_check_all_confirmed", closed_ready["ready_count"] == 56)

            captain_round = await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/deadlock/captain-round/start",
                expected=201,
                json_payload={"teams_count": 8},
            )
            self.scenario(
                "captain_round_auto_assigns_8",
                captain_round["status"] == "finalized"
                and captain_round["assigned_count"] == 8,
                captain_round,
            )
            captain_ids = [
                str(row["user_id"])
                for row in captain_round["entries"]
                if row["state"] == "assigned"
            ]
            self.scenario(
                "captain_auto_assign_finalize",
                len(captain_ids) == 8,
                {
                    "captains": captain_ids,
                },
            )

            await self.configure_captain_preferences(captain_ids)
            first_run = await self.wait_for_auto_assignment_run(
                organizer["client"],
                expected_teams=8,
            )
            self.scenario("assignment_first_run", len(first_run["teams"]) == 8)

            changed_captain = self.users_by_id[captain_ids[0]]
            await self.request(
                changed_captain["client"],
                "PUT",
                "/profiles/me/deadlock/dream-slots",
                expected=200,
                json_payload={
                    "slots": [
                        {
                            "slot_number": slot_number,
                            "allowed_roles": [["Carry"], ["Semi-Carry"], ["Support"], ["Semi-Support"]][
                                (slot_number - 1) % 4
                            ],
                            "desired_heroes": [["Seven"], ["Kelvin"], ["Abrams"]][
                                (slot_number - 1) % 3
                            ],
                        }
                        for slot_number in range(1, 7)
                    ]
                },
            )
            stale_state = await self.request(
                organizer["client"],
                "GET",
                f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment",
                expected=200,
            )
            self.scenario(
                "assignment_stale_detected",
                stale_state["latest_run"]["is_stale"]
                and "dream_slots_changed" in stale_state["latest_run"]["stale_reasons"],
                stale_state["latest_run"]["stale_reasons"],
            )
            final_run = await self.wait_for_auto_assignment_run(
                organizer["client"],
                previous_run_id=str(first_run["id"]),
                expected_teams=8,
            )
            run_id = str(final_run["id"])
            await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment/{run_id}/publish",
                expected=200,
            )
            locked = await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment/{run_id}/lock",
                expected=200,
            )
            self.scenario("assignment_publish_lock", locked["status"] == "locked")

            await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/matches/seed-opening-round",
                expected=201,
            )
            bracket = await self.request(
                organizer["client"],
                "GET",
                f"/tournaments/{self.tournament_slug}/bracket",
                expected=200,
            )
            self.scenario(
                "full_strength_seeded_graph",
                len(bracket["matches"]) == 7
                and len(bracket["teams"]) == 8
                and bracket["revision"] == 1,
                {
                    "matches": len(bracket["matches"]),
                    "teams": len(bracket["teams"]),
                    "revision": bracket["revision"],
                },
            )
            opening_matches = sorted(
                [
                    match
                    for match in bracket["matches"]
                    if match["round_number"] == 1
                ],
                key=lambda match: match["match_order"],
            )
            self.report["initial_pairings"] = [
                {
                    "match": match["match_order"],
                    "home_team_id": match["team_a_id"],
                    "away_team_id": match["team_b_id"],
                }
                for match in opening_matches
            ]
            async def refresh_bracket() -> dict[str, Any]:
                return await self.request(
                    organizer["client"],
                    "GET",
                    f"/tournaments/{self.tournament_slug}/bracket",
                    expected=200,
                )

            async def play_match(match: dict[str, Any], home: int, away: int) -> dict[str, Any]:
                current = await refresh_bracket()
                reported = await self.request(
                    organizer["client"],
                    "POST",
                    f"/tournaments/{self.tournament_slug}/matches/{match['id']}/report",
                    expected=200,
                    json_payload={
                        "home_score": home,
                        "away_score": away,
                        "expected_revision": current["revision"],
                    },
                )
                self.report["match_path"].append(
                    {
                        "round": match["round_number"],
                        "match": match["match_order"],
                        "home_team_id": match["team_a_id"],
                        "away_team_id": match["team_b_id"],
                        "score": f"{home}-{away}",
                        "winner_team_id": reported["winner_team_id"],
                    }
                )
                return reported

            bracket = await refresh_bracket()
            quarterfinals = sorted(
                [match for match in bracket["matches"] if match["round_number"] == 1],
                key=lambda match: match["match_order"],
            )
            await play_match(quarterfinals[0], 2, 0)
            self.scenario("manual_bracket_refresh_after_mutation", True)
            bracket = await refresh_bracket()
            await self.request(
                organizer["client"],
                "PATCH",
                f"/tournaments/{self.tournament_slug}/matches/{quarterfinals[0]['id']}/status",
                expected=200,
                json_payload={
                    "status": "scheduled",
                    "expected_revision": bracket["revision"],
                },
            )
            bracket = await refresh_bracket()
            recovered = await self.request(
                organizer["client"],
                "POST",
                f"/tournaments/{self.tournament_slug}/matches/{quarterfinals[0]['id']}/report",
                expected=200,
                json_payload={
                    "home_score": 0,
                    "away_score": 2,
                    "expected_revision": bracket["revision"],
                },
            )
            self.report["match_path"][-1] = {
                "round": 1,
                "match": 1,
                "home_team_id": quarterfinals[0]["team_a_id"],
                "away_team_id": quarterfinals[0]["team_b_id"],
                "score": "0-2",
                "winner_team_id": recovered["winner_team_id"],
                "recovered": True,
            }
            self.scenario(
                "result_recovery_before_dependent_start",
                recovered["winner_team_id"] == quarterfinals[0]["team_b_id"],
            )
            for match in quarterfinals[1:]:
                await play_match(match, 2, 1)

            bracket = await refresh_bracket()
            semifinals = sorted(
                [match for match in bracket["matches"] if match["round_number"] == 2],
                key=lambda match: match["match_order"],
            )
            for match in semifinals:
                await play_match(match, 2, 0)

            bracket = await refresh_bracket()
            final_match = next(
                match for match in bracket["matches"] if match["round_number"] == 3
            )
            invalid_final = await organizer["client"].post(
                f"/tournaments/{self.tournament_slug}/matches/{final_match['id']}/report",
                json={
                    "home_score": 2,
                    "away_score": 0,
                    "expected_revision": bracket["revision"],
                },
            )
            self.scenario(
                "strict_bo5_final_validation",
                invalid_final.status_code == 422,
                {"status": invalid_final.status_code, "body": invalid_final.json()},
            )
            await play_match(final_match, 3, 2)
            await self.request(
                organizer["client"],
                "PATCH",
                f"/tournaments/{self.tournament_slug}/status",
                expected=200,
                json_payload={"status": "in_progress"},
            )
            completed = await self.request(
                organizer["client"],
                "PATCH",
                f"/tournaments/{self.tournament_slug}/status",
                expected=200,
                json_payload={"status": "completed"},
            )
            self.scenario("tournament_completed_after_final", completed["status"] == "completed")
            frozen = await organizer["client"].patch(
                f"/tournaments/{self.tournament_slug}/matches/{final_match['id']}/status",
                json={"status": "scheduled"},
            )
            self.scenario("terminal_bracket_freeze", frozen.status_code == 409, frozen.text)

            final_bracket = await refresh_bracket()
            teams_by_id = {str(team["team_id"]): team for team in final_run["teams"]}
            ranking = sorted(
                final_bracket["teams"],
                key=lambda team: int(team["seed"] or 999),
            )
            self.report["strength_ranking"] = [
                {
                    "rank": team["seed"],
                    "team_id": team["id"],
                    "starter_strength": team["starter_strength"],
                    "starter_average_strength": team["starter_average_strength"],
                }
                for team in ranking
            ]
            self.report["teams"] = [
                summarize_team(teams_by_id[str(team["id"])])
                for team in ranking
            ]
            self.report["preference_metrics"] = final_run["preference_metrics"]
            self.report["preference_metrics_by_team"] = [
                summarize_team_preferences(teams_by_id[str(team["id"])])
                for team in ranking
            ]
            self.scenario(
                "qa_workflow_complete",
                all(item["ok"] for item in self.scenarios),
            )
        finally:
            await self.stop_performance_collection()
            for client in self.clients:
                await client.aclose()
            if not self.keep_data:
                cleanup = await self.cleanup_targeted()
                self.report["cleanup"] = cleanup
                self.scenarios.append(
                    {
                        "name": "targeted_cleanup",
                        "ok": cleanup.get("ok", False),
                        "detail": cleanup,
                    }
                )
            else:
                self.report["cleanup"] = {"ok": False, "kept": True}
            self.report["duration_seconds"] = round(time.monotonic() - started, 4)
            self.report["finished_at"] = datetime.now(UTC).isoformat()
            self.report["passed"] = all(item["ok"] for item in self.scenarios)
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return self.report

    async def cleanup_targeted(self) -> dict[str, Any]:
        settings = get_settings()
        if settings.platform_db_schema != "platform":
            return {"ok": False, "error": "unexpected schema"}
        if "platformdb" not in settings.platform_database_url:
            return {"ok": False, "error": "unexpected database"}
        tournament_ids = list(dict.fromkeys(
            [value for value in self.tournament_ids if value]
            + ([self.tournament_id] if self.tournament_id else [])
        ))
        if not tournament_ids and not self.user_ids:
            return {"ok": True, "tournaments": 0, "users": 0}

        async with session_factory()() as db_session:
            try:
                await purge_deleted_media_metadata(
                    db_session,
                    owner_user_ids=self.user_ids,
                    tournament_ids=tournament_ids,
                )
            except MediaCleanupRequired as exc:
                await db_session.rollback()
                return {
                    "ok": False,
                    "error": "media_cleanup_required",
                    "statuses": exc.status_counts,
                }
            subject_ids: set[str] = set(self.user_ids)
            if tournament_ids:
                subject_ids.update(tournament_ids)
                model_queries = (
                    select(TournamentInvite.id).where(
                        TournamentInvite.tournament_id.in_(tournament_ids)
                    ),
                    select(TournamentParticipant.id).where(
                        TournamentParticipant.tournament_id.in_(tournament_ids)
                    ),
                    select(TournamentDeadlockReadyRound.id).where(
                        TournamentDeadlockReadyRound.tournament_id.in_(tournament_ids)
                    ),
                    select(TournamentDeadlockCaptainRound.id).where(
                        TournamentDeadlockCaptainRound.tournament_id.in_(tournament_ids)
                    ),
                    select(TournamentDeadlockAssignmentRun.id).where(
                        TournamentDeadlockAssignmentRun.tournament_id.in_(tournament_ids)
                    ),
                    select(TournamentMatch.id).where(
                        TournamentMatch.tournament_id.in_(tournament_ids)
                    ),
                )
                for stmt in model_queries:
                    subject_ids.update(str(value) for value in (await db_session.scalars(stmt)).all())
                captain_round_ids = [
                    int(value)
                    for value in (
                        await db_session.scalars(
                            select(TournamentDeadlockCaptainRound.id).where(
                                TournamentDeadlockCaptainRound.tournament_id.in_(
                                    tournament_ids
                                )
                            )
                        )
                    ).all()
                ]
                if captain_round_ids:
                    subject_ids.update(
                        str(value)
                        for value in (
                            await db_session.scalars(
                                select(TournamentDeadlockCaptainEntry.id).where(
                                    TournamentDeadlockCaptainEntry.round_id.in_(
                                        captain_round_ids
                                    )
                                )
                            )
                        ).all()
                    )

            # Keep each asyncpg statement below its 32,767 bind-parameter
            # limit. Separate deletes preserve the old OR semantics; a log
            # matching both predicates is deleted by the first statement and
            # contributes zero rows to the second one.
            for start in range(0, len(self.user_ids), 10_000):
                user_chunk = self.user_ids[start : start + 10_000]
                if user_chunk:
                    await db_session.execute(
                        delete(AuditLog).where(AuditLog.actor_user_id.in_(user_chunk))
                    )
            subject_values = list(subject_ids)
            for start in range(0, len(subject_values), 10_000):
                subject_chunk = subject_values[start : start + 10_000]
                if subject_chunk:
                    await db_session.execute(
                        delete(AuditLog).where(AuditLog.subject_id.in_(subject_chunk))
                    )
            deleted_tournaments = 0
            if tournament_ids:
                result = await db_session.execute(
                    delete(Tournament).where(Tournament.id.in_(tournament_ids))
                )
                deleted_tournaments = int(result.rowcount or 0)
            deleted_users = 0
            if self.user_ids:
                result = await db_session.execute(
                    delete(User).where(User.id.in_(self.user_ids))
                )
                deleted_users = int(result.rowcount or 0)
            await db_session.commit()

            read_models_cleanup_error: str | None = None
            for tournament_id in tournament_ids:
                try:
                    await delete_tournament_read_models(
                        tournament_id,
                        ("teams", "workspace_detail", "bracket_summary", "bracket_full"),
                    )
                except Exception as exc:
                    # Redis is a disposable projection. Keep cleanup's
                    # authoritative DB result successful if Redis is down.
                    read_models_cleanup_error = type(exc).__name__
                    break

            remaining_tournaments = list(
                (
                    await db_session.scalars(
                        select(Tournament.id).where(Tournament.id.in_(tournament_ids))
                    )
                ).all()
            ) if tournament_ids else []
            remaining_users = list(
                (
                    await db_session.scalars(
                        select(User.id).where(User.id.in_(self.user_ids))
                    )
                ).all()
            )
            return {
                "ok": not remaining_tournaments and not remaining_users,
                "tournaments": deleted_tournaments,
                "users": deleted_users,
                "remaining_tournaments": remaining_tournaments,
                "remaining_users": remaining_users,
                "read_models_cleanup_error": read_models_cleanup_error,
            }


def summarize_team(team: dict[str, Any]) -> dict[str, Any]:
    captain = team["captain"]
    starters = [
        {
            "slot": slot["slot_number"],
            "user_id": slot["assigned_player"]["user_id"],
            "username": slot["assigned_player"]["username"],
            "rank": slot["assigned_player"]["rank"],
            "subrank": slot["assigned_player"]["subrank"],
            "strength": slot["assigned_player"]["strength"],
            "assigned_role": slot["assigned_role"],
        }
        for slot in team["starter_slots"]
    ]
    reserve_slot = team.get("reserve_slot")
    reserve = (
        {
            "slot": reserve_slot["slot_number"],
            "user_id": reserve_slot["assigned_player"]["user_id"],
            "username": reserve_slot["assigned_player"]["username"],
            "rank": reserve_slot["assigned_player"]["rank"],
            "subrank": reserve_slot["assigned_player"]["subrank"],
            "strength": reserve_slot["assigned_player"]["strength"],
            "assigned_role": reserve_slot["assigned_role"],
        }
        if reserve_slot is not None
        else None
    )
    return {
        "team_id": team["team_id"],
        "starter_strength": team["starter_strength"],
        "starter_average_strength": team["starter_average_strength"],
        "captain": {
            "user_id": captain["user_id"],
            "username": captain["username"],
            "rank": captain["rank"],
            "subrank": captain["subrank"],
            "strength": captain["strength"],
            "assigned_role": captain["assigned_role"],
        },
        "starters": starters,
        "reserve": reserve,
    }


def summarize_team_preferences(team: dict[str, Any]) -> dict[str, Any]:
    starter_slots = list(team["starter_slots"])
    configured = [
        slot
        for slot in starter_slots
        if slot["desired_heroes"] or len(slot["allowed_roles"]) < 4
    ]
    role_matches = sum(1 for slot in starter_slots if slot["role_match"])
    hero_slots = [slot for slot in starter_slots if slot["desired_heroes"]]
    hero_hits = sum(1 for slot in hero_slots if slot["desired_match_count"] > 0)
    fully_honored = sum(
        1
        for slot in configured
        if slot["role_match"]
        and (not slot["desired_heroes"] or slot["desired_match_count"] > 0)
    )
    return {
        "team_id": team["team_id"],
        "configured_starter_slots": len(configured),
        "role_matches": role_matches,
        "hero_preference_slots": len(hero_slots),
        "hero_preference_hits": hero_hits,
        "fully_honored": fully_honored,
        "fully_honored_percent": round(
            fully_honored / len(configured) * 100 if configured else 0,
            2,
        ),
    }


def cli_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    scenarios = list(report.get("scenarios") or [])
    write_burst = report.get("write_burst") if isinstance(report.get("write_burst"), dict) else {}
    performance = report.get("performance") if isinstance(report.get("performance"), dict) else {}
    return {
        "marker": report.get("marker"),
        "mode": report.get("mode"),
        "passed": report.get("passed"),
        "report_path": str(report.get("report_path") or ""),
        "tournament_slug": report.get("tournament_slug"),
        "created_users": report.get("created_users"),
        "requested_users": report.get("requested_users"),
        "tournament_visibility": report.get("tournament_visibility"),
        "profile_journey": report.get("profile_journey"),
        "read_mix": report.get("read_mix"),
        "teams": len(report.get("strength_ranking") or []),
        "matches": len(report.get("match_path") or []) or report.get("matches_count"),
        "duration_seconds": report.get("duration_seconds"),
        "assignment_seconds": report.get("assignment_seconds"),
        "performance_scope": performance.get("measurement_scope"),
        "fatal_error": report.get("fatal_error"),
        "write_burst": (
            {
                "profile": write_burst.get("profile"),
                "selection": write_burst.get("selection"),
                "profiles": [
                    {
                        "name": row.get("name"),
                        "mutations": row.get("mutations"),
                        "p95_ms": (row.get("http") or {}).get("overall", {}).get("p95_ms"),
                        "p99_ms": (row.get("http") or {}).get("overall", {}).get("p99_ms"),
                    }
                    for row in write_burst.get("profiles") or []
                    if isinstance(row, dict)
                ],
                "load_generator_local": write_burst.get("load_generator_local"),
            }
            if write_burst
            else None
        ),
        "rostered_participant": report.get("rostered_participant"),
        "retained_participant": report.get("retained_participant"),
        "control_participant": report.get("control_participant"),
        "scenarios": [
            {
                "name": scenario.get("name"),
                "ok": scenario.get("ok"),
                "detail": scenario.get("detail"),
            }
            for scenario in scenarios
        ],
    }


async def async_main() -> int:
    args = parse_args()
    env_file = args.env_file
    if env_file is None:
        configured_env = os.environ.get("PLATFORM_ENV_FILE", "").strip()
        env_file = Path(configured_env) if configured_env else PLATFORM_ROOT / ".env.platform"
    load_env_file(env_file)

    qa = ProductionQa(
        origin=args.origin,
        request_origin=args.request_origin,
        report_path=args.report_path,
        keep_data=args.keep_data,
        http_timeout=args.http_timeout,
        mode=args.mode,
        scale_users=args.scale_users,
        scale_teams=args.scale_teams,
        concurrency=args.concurrency,
        http_max_connections=args.http_max_connections,
        collect_performance=args.collect_performance,
        system_sample_interval=args.system_sample_interval,
        scale_site_mix_users=args.scale_site_mix_users,
        scale_bracket_view_users=args.scale_bracket_view_users,
        lifecycle_tournament_count=args.lifecycle_tournament_count,
        lifecycle_users_per_tournament=args.lifecycle_users_per_tournament,
        lifecycle_teams_count=args.lifecycle_teams_count,
        scale_final_view_profile=args.scale_final_view_profile,
        tournament_visibility=args.tournament_visibility,
        profile_journey=args.profile_journey,
        write_burst_profile=args.write_burst_profile,
        write_burst_users_per_tournament=args.write_burst_users_per_tournament,
        write_burst_time_scale=args.write_burst_time_scale,
        tournament_name=args.tournament_name,
        retained_participant_email=args.retained_participant_email,
        retained_participant_state=args.retained_participant_state,
        rostered_participant_email=args.rostered_participant_email,
        control_participant_email=args.control_participant_email,
        control_participant_state=args.control_participant_state,
    )
    try:
        if args.mode in {"scale", "read-mix"}:
            report = await qa.run_scale()
        elif args.mode == "write-burst":
            report = await qa.run_write_burst_profile()
        elif args.mode == "tournament-lifecycle":
            report = await qa.run_tournament_lifecycle()
        else:
            report = await qa.run()
    except Exception as exc:
        qa.report["fatal_error"] = f"{type(exc).__name__}: {exc!r}"
        qa.report["finished_at"] = datetime.now(UTC).isoformat()
        qa.report["passed"] = False
        qa.report["report_path"] = str(args.report_path)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(qa.report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            await qa.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
        except Exception:
            pass
        print(json.dumps(cli_report_summary(qa.report), ensure_ascii=False))
        return 1
    finally:
        await dispose_engine()

    report["report_path"] = str(args.report_path)
    print(json.dumps(cli_report_summary(report), ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
