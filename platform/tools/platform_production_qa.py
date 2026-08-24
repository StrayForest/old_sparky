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
from sqlalchemy import delete, func, or_, select, text

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import (
    MediaCleanupRequired,
    purge_deleted_media_metadata,
)
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
    TournamentInviteAccess,
    TournamentMatch,
    TournamentParticipant,
    User,
    UserRole,
    UserSession,
)
from python_packages.platform_infra.security import hash_password, new_session_token, session_token_digest


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
PROCESS_CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
BROWSER_POLLING_PROFILE_NAME = "browser-polling-20x50"
BROWSER_POLLING_TOURNAMENT_PLAN = (
    ("registration_open", 10),
    ("ready_check_active", 5),
    ("bracket_active", 3),
    ("terminal", 2),
)
BROWSER_POLLING_USERS_PER_TOURNAMENT = 50
BROWSER_POLLING_DURATION_SECONDS = 300.0
BROWSER_POLLING_OPEN_STAGGER_SECONDS = 30.0
BROWSER_POLLING_FIXED_INTERVAL_MS = 10_000
BROWSER_POLLING_DEFAULT_INTERVAL_MS = 15_000
BROWSER_POLLING_JITTER_MS = 3_000
BROWSER_POLLING_READY_TEAMS = 2
BROWSER_POLLING_HOT_ROUTES = (
    "GET /tournaments/{slug}",
    "GET /tournaments/{slug}/workspace",
    "GET /tournaments/{slug}/bracket",
    "GET /tournaments/{slug}/deadlock/ready-check",
)
WRITE_BURST_PROFILE_NAME = "write-burst-v1"
WRITE_BURST_USERS_PER_TOURNAMENT = 50
WRITE_BURST_TOURNAMENT_COUNT = 20
WRITE_BURST_JOIN_SPREAD_SECONDS = (10.0, 30.0, 60.0)
WRITE_BURST_READY_SPREAD_SECONDS = (5.0, 10.0, 30.0)
WRITE_BURST_MULTI_SPREAD_SECONDS = 30.0
WRITE_BURST_MULTI_START_STAGGER_SECONDS = 1.0


def qa_display_name(marker: str, label: str) -> str:
    return f"{label[:7]}-{marker[-7:]}"[:15]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run targeted end-to-end production QA for Old Sparky Arena."
    )
    parser.add_argument("--origin", default="http://127.0.0.1")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--browser-gate-dir", type=Path, default=None)
    parser.add_argument("--browser-gate-timeout", type=float, default=120.0)
    parser.add_argument("--http-timeout", type=float, default=180.0)
    parser.add_argument(
        "--mode",
        choices=("targeted", "scale", "browser-polling", "write-burst"),
        default="targeted",
    )
    parser.add_argument("--scale-users", type=int, default=10_000)
    parser.add_argument("--scale-teams", type=int, default=128)
    parser.add_argument("--concurrency", type=int, default=80)
    parser.add_argument("--collect-performance", action="store_true")
    parser.add_argument("--system-sample-interval", type=float, default=1.0)
    parser.add_argument("--scale-site-mix-users", type=int, default=None)
    parser.add_argument("--scale-bracket-view-users", type=int, default=None)
    parser.add_argument("--browser-polling-profile", default=BROWSER_POLLING_PROFILE_NAME)
    parser.add_argument(
        "--browser-polling-duration",
        type=float,
        default=BROWSER_POLLING_DURATION_SECONDS,
    )
    parser.add_argument(
        "--browser-polling-users-per-tournament",
        type=int,
        default=BROWSER_POLLING_USERS_PER_TOURNAMENT,
    )
    parser.add_argument(
        "--browser-polling-open-stagger",
        type=float,
        default=BROWSER_POLLING_OPEN_STAGGER_SECONDS,
        help="Spread initial tab opens over this many seconds to avoid an artificial synchronized wave.",
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
    ) -> None:
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
            )
        )

    def summary(self, *, phases: set[str] | None = None) -> dict[str, Any]:
        samples = (
            self.samples
            if phases is None
            else [sample for sample in self.samples if sample.phase in phases]
        )
        if not samples:
            return {
                "requests": 0,
                "requests_per_second": 0,
                "errors": 0,
                "overall": metric_stats([]),
                "by_phase": {},
                "by_route": {},
            }
        elapsed_ms = [sample.elapsed_ms for sample in samples]
        started_at = min(sample.started_at for sample in samples)
        finished_at = max(sample.finished_at for sample in samples)
        wall_seconds = max(0.001, finished_at - started_at)
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
        return {
            "requests": len(samples),
            "requests_per_second": round(len(samples) / wall_seconds, 3),
            "wall_seconds": round(wall_seconds, 3),
            "errors": sum(1 for sample in samples if not sample.ok),
            "status_counts": dict(sorted(status_counts.items())),
            "overall": {
                **metric_stats(elapsed_ms),
                "response_bytes": byte_stats([sample.response_bytes for sample in samples]),
            },
            "by_phase": {
                phase: {
                    **metric_stats(values),
                    "response_bytes": byte_stats(by_phase_bytes[phase]),
                }
                for phase, values in sorted(by_phase.items())
            },
            "by_route": route_rows,
        }


class PollingMetricsRecorder:
    def __init__(self) -> None:
        self.counters: Counter[str] = Counter()
        self.by_route: dict[str, Counter[str]] = defaultdict(Counter)
        self.by_role: dict[str, Counter[str]] = defaultdict(Counter)
        self.by_tournament_status: dict[str, Counter[str]] = defaultdict(Counter)
        self.route_executed_by_hidden: Counter[str] = Counter()
        self.route_executed_by_terminal: Counter[str] = Counter()

    def mark(
        self,
        event: str,
        *,
        route: str | None = None,
        role: str | None = None,
        tournament_status: str | None = None,
        hidden: bool = False,
        terminal_known: bool = False,
    ) -> None:
        self.counters[event] += 1
        if route:
            self.by_route[route][event] += 1
            if event == "executed" and hidden:
                self.route_executed_by_hidden[route] += 1
            if event == "executed" and terminal_known:
                self.route_executed_by_terminal[route] += 1
        if role:
            self.by_role[role][event] += 1
        if tournament_status:
            self.by_tournament_status[tournament_status][event] += 1

    @staticmethod
    def _counter_row(counter: Counter[str]) -> dict[str, int]:
        return {
            "total_scheduled": int(counter.get("scheduled", 0)),
            "executed": int(counter.get("executed", 0)),
            "skipped_hidden": int(counter.get("skipped_hidden", 0)),
            "skipped_terminal": int(counter.get("skipped_terminal", 0)),
            "deduped": int(counter.get("deduped", 0)),
            "aborted": int(counter.get("aborted", 0)),
            "sse_reconnect": int(counter.get("sse_reconnect", 0)),
        }

    def summary(self) -> dict[str, Any]:
        return {
            **self._counter_row(self.counters),
            "by_route": {
                key: self._counter_row(counter)
                for key, counter in sorted(self.by_route.items())
            },
            "by_role": {
                key: self._counter_row(counter)
                for key, counter in sorted(self.by_role.items())
            },
            "by_tournament_status": {
                key: self._counter_row(counter)
                for key, counter in sorted(self.by_tournament_status.items())
            },
            "executed_while_hidden": dict(sorted(self.route_executed_by_hidden.items())),
            "executed_after_terminal": dict(sorted(self.route_executed_by_terminal.items())),
        }


def is_local_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    hostname = (parsed.hostname or "").lower()
    return hostname in {"", "127.0.0.1", "localhost", "::1"}


def polling_delay_seconds(raw_delay_ms: Any, *, tab_index: int, tick: int) -> float | None:
    if raw_delay_ms == 0:
        return None
    if isinstance(raw_delay_ms, (int, float)) and raw_delay_ms > 0:
        delay_ms = float(raw_delay_ms)
    else:
        delay_ms = float(BROWSER_POLLING_DEFAULT_INTERVAL_MS)
    jitter_ms = (tab_index * 997 + tick * 389) % BROWSER_POLLING_JITTER_MS
    return max(0.25, (delay_ms + jitter_ms) / 1000)


def fixed_polling_expectation(
    *,
    duration_seconds: float,
    tabs: list[dict[str, Any]],
) -> dict[str, Any]:
    ticks = max(1, int(duration_seconds // (BROWSER_POLLING_FIXED_INTERVAL_MS / 1000)))
    by_route: Counter[str] = Counter()
    by_role: Counter[str] = Counter()
    by_tournament_status: Counter[str] = Counter()
    for tab in tabs:
        route = str(tab.get("route_label") or tab["route"])
        by_route[route] += ticks
        by_role[str(tab["role"])] += ticks
        by_tournament_status[str(tab["tournament_status"])] += ticks
    total = ticks * len(tabs)
    return {
        "interval_ms": BROWSER_POLLING_FIXED_INTERVAL_MS,
        "ticks_per_tab": ticks,
        "total_expected_gets": total,
        "by_route": dict(sorted(by_route.items())),
        "by_role": dict(sorted(by_role.items())),
        "by_tournament_status": dict(sorted(by_tournament_status.items())),
    }


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


def hot_route_counts(http_summary: dict[str, Any]) -> dict[str, int]:
    by_route = http_summary.get("by_route", {}) if isinstance(http_summary, dict) else {}
    if not isinstance(by_route, dict):
        return {}
    return {
        route: int(metrics.get("count") or 0)
        for route, metrics in by_route.items()
        if route in BROWSER_POLLING_HOT_ROUTES and isinstance(metrics, dict)
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

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

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
                "api_connections": read_tcp_connection_counts(8010),
                "postgres_connections": read_tcp_connection_counts(5432),
                "gunicorn": gunicorn_counts(processes),
                "postgres_cpu_percent": round(postgres_cpu_percent, 2),
                "processes": process_metrics,
                "postgres_waits": await sample_postgres_waits(),
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
        "sql_ms": float,
        "sql_count": int,
        "max_sql_ms": float,
        "compute_ms": float,
        "compute_blocks": int,
        "response_bytes": int,
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
    rows = [
        row
        for row in (parse_request_perf_line(line) for line in lines)
        if row is not None
    ]
    if not rows:
        return {"logged_requests": 0}
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

    totals = [float(row["total_ms"]) for row in rows if isinstance(row.get("total_ms"), (int, float))]
    sql_counts = [float(row["sql_count"]) for row in rows if isinstance(row.get("sql_count"), (int, float))]
    sql_times = [float(row["sql_ms"]) for row in rows if isinstance(row.get("sql_ms"), (int, float))]
    compute_times = [float(row["compute_ms"]) for row in rows if isinstance(row.get("compute_ms"), (int, float))]
    max_sql_times = [float(row["max_sql_ms"]) for row in rows if isinstance(row.get("max_sql_ms"), (int, float))]
    response_bytes = [float(row["response_bytes"]) for row in rows if isinstance(row.get("response_bytes"), (int, float))]
    non_sql_times = [
        max(
            0.0,
            float(row.get("total_ms") or 0)
            - float(row.get("sql_ms") or 0)
            - float(row.get("compute_ms") or 0),
        )
        for row in rows
    ]

    def summarize_route_rows(row_values: list[dict[str, Any]]) -> dict[str, Any]:
        non_sql_times = [
            max(
                0.0,
                float(row.get("total_ms") or 0)
                - float(row.get("sql_ms") or 0)
                - float(row.get("compute_ms") or 0),
            )
            for row in row_values
        ]
        return {
            "requests": len(row_values),
            "total": row_metric_stats("total_ms", row_values),
            "avg_sql_queries_per_request": round(
                sum(float(row["sql_count"]) for row in row_values if isinstance(row.get("sql_count"), (int, float)))
                / max(1, sum(1 for row in row_values if isinstance(row.get("sql_count"), (int, float)))),
                3,
            ),
            "avg_db_time_ms": round(
                sum(float(row["sql_ms"]) for row in row_values if isinstance(row.get("sql_ms"), (int, float)))
                / max(1, sum(1 for row in row_values if isinstance(row.get("sql_ms"), (int, float)))),
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
        }

    return {
        "logged_requests": len(rows),
        "overall": metric_stats(totals),
        "avg_sql_queries_per_request": round(sum(sql_counts) / len(sql_counts), 3) if sql_counts else None,
        "avg_db_time_ms": round(sum(sql_times) / len(sql_times), 3) if sql_times else None,
        "avg_compute_time_ms": round(sum(compute_times) / len(compute_times), 3) if compute_times else None,
        "non_sql_time": metric_stats(non_sql_times),
        "max_sql_time_ms": round(max(max_sql_times), 3) if max_sql_times else None,
        "response_bytes": byte_stats([int(value) for value in response_bytes]),
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


def collect_api_journal_lines(since: str, until: str) -> list[str]:
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                "deadlock-api",
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


class ProductionQa:
    def __init__(
        self,
        *,
        origin: str,
        report_path: Path,
        keep_data: bool,
        browser_gate_dir: Path | None,
        browser_gate_timeout: float,
        http_timeout: float,
        mode: str = "targeted",
        scale_users: int = 10_000,
        scale_teams: int = 128,
        concurrency: int = 80,
        collect_performance: bool = False,
        system_sample_interval: float = 1.0,
        scale_site_mix_users: int | None = None,
        scale_bracket_view_users: int | None = None,
        scale_final_view_profile: str = "current",
        tournament_visibility: str = "public",
        profile_journey: bool = False,
        browser_polling_profile: str = BROWSER_POLLING_PROFILE_NAME,
        browser_polling_duration: float = BROWSER_POLLING_DURATION_SECONDS,
        browser_polling_users_per_tournament: int = BROWSER_POLLING_USERS_PER_TOURNAMENT,
        browser_polling_open_stagger: float = BROWSER_POLLING_OPEN_STAGGER_SECONDS,
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
        prefix = "preprod" if mode in {"scale", "browser-polling", "write-burst"} else "qa"
        self.marker = f"{prefix}{timestamp}{secrets.token_hex(2)}"
        self.origin = origin.rstrip("/")
        self.api_origin = f"{self.origin}/api/v1"
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
        self.browser_gate_dir = browser_gate_dir
        self.browser_gate_timeout = browser_gate_timeout
        self.http_timeout = max(1.0, http_timeout)
        self.mode = mode
        self.browser_polling_profile = browser_polling_profile or BROWSER_POLLING_PROFILE_NAME
        self.browser_polling_duration = max(1.0, browser_polling_duration)
        self.browser_polling_users_per_tournament = max(
            10,
            browser_polling_users_per_tournament,
        )
        self.browser_polling_open_stagger = max(0.0, browser_polling_open_stagger)
        self.write_burst_profile = write_burst_profile
        self.write_burst_users_per_tournament = max(14, write_burst_users_per_tournament)
        self.write_burst_time_scale = max(0.01, write_burst_time_scale)
        browser_polling_users = (
            sum(count for _, count in BROWSER_POLLING_TOURNAMENT_PLAN)
            * self.browser_polling_users_per_tournament
        )
        write_burst_users = (
            WRITE_BURST_TOURNAMENT_COUNT * self.write_burst_users_per_tournament
            if write_burst_profile in {"all", "multi-staggered"}
            else self.write_burst_users_per_tournament
        )
        if mode == "browser-polling":
            self.scale_users = browser_polling_users
        elif mode == "write-burst":
            self.scale_users = write_burst_users
        else:
            self.scale_users = max(14, scale_users)
        self.scale_teams = max(2, min(128, scale_teams))
        self.concurrency = max(1, concurrency)
        self.collect_performance = collect_performance
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
        self.password = secrets.token_urlsafe(18)
        self.clients: list[httpx.AsyncClient] = []
        self.users_by_id: dict[str, dict[str, Any]] = {}
        self.user_ids: list[str] = []
        self.session_tokens_by_user_id: dict[str, str] = {}
        self.rostered_session_token_digest: str | None = None
        self.control_participant_session_token_digest: str | None = None
        self.tournament_id: str | None = None
        self.tournament_slug: str | None = None
        self.tournament_ids: list[str] = []
        self.tournament_slugs: list[str] = []
        self.polling_metrics = PollingMetricsRecorder()
        self.scenarios: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {
            "marker": self.marker,
            "started_at": datetime.now(UTC).isoformat(),
            "origin": self.origin,
            "mode": mode,
            "report_path": str(report_path),
            "requested_users": self.scale_users if mode in {"scale", "browser-polling", "write-burst"} else None,
            "http_timeout_seconds": self.http_timeout,
            "profile_journey": self.profile_journey,
            "scenarios": self.scenarios,
            "user_ids": self.user_ids,
            "tournament_ids": [],
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
            "polling": {},
            "write_burst": {},
            "retained_participant": None,
            "rostered_participant": None,
            "control_participant": None,
        }

    def scenario(self, name: str, ok: bool, detail: Any = None) -> None:
        self.scenarios.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
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
        polling_report = self.report.get("polling")
        if self.mode == "browser-polling" and isinstance(polling_report, dict):
            polling_report["http_route_counts"] = hot_route_counts(http_summary)
        write_burst_report = self.report.get("write_burst")
        if self.mode == "write-burst" and isinstance(write_burst_report, dict):
            server_by_phase = server_request_summary.get("by_qa_phase", {})
            if isinstance(server_by_phase, dict):
                write_burst_report["server_by_phase"] = server_by_phase
                for profile in write_burst_report.get("profiles") or []:
                    if not isinstance(profile, dict):
                        continue
                    phase = str(profile.get("phase") or "")
                    profile["server"] = server_by_phase.get(phase)
        self.report["performance"] = {
            "http_client": http_summary,
            "system": system_summary,
            "server_request_perf_logs": server_request_summary,
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
                "site_mix_users": self.scale_site_mix_users,
                "bracket_view_users": self.scale_bracket_view_users,
                "final_view_profile": self.scale_final_view_profile,
                "browser_polling_profile": self.browser_polling_profile,
                "browser_polling_duration": self.browser_polling_duration,
                "browser_polling_users_per_tournament": self.browser_polling_users_per_tournament,
                "browser_polling_open_stagger": self.browser_polling_open_stagger,
                "write_burst_profile": self.write_burst_profile,
                "write_burst_users_per_tournament": self.write_burst_users_per_tournament,
                "write_burst_time_scale": self.write_burst_time_scale,
                "load_generator_local": is_local_origin(self.origin),
            },
        }

    async def new_client(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=self.api_origin,
            follow_redirects=True,
            timeout=httpx.Timeout(self.http_timeout),
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
        try:
            headers = (
                {"X-Platform-QA-Phase": self.current_phase}
                if self.collect_performance
                else None
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
                )
        if response.status_code != expected:
            raise QaFailure(
                f"{method} {path}: expected {expected}, got {response.status_code}: "
                f"{response.text[:1000]}"
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
        expected: int,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        token = self.session_tokens_by_user_id[str(user["id"])]
        started_at = time.monotonic()
        status_code = 0
        ok = False
        response_bytes = 0
        try:
            response = await client.request(
                method,
                path,
                headers={
                    "Cookie": f"{self.session_cookie_name}={token}",
                    **(
                        {"X-Platform-QA-Phase": self.current_phase}
                        if self.collect_performance
                        else {}
                    ),
                },
                json=json_payload,
            )
            status_code = response.status_code
            ok = response.status_code == expected
            response_bytes = len(response.content or b"")
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
                )
        if response.status_code != expected:
            raise QaFailure(
                f"{method} {path} as {user['label']}: expected {expected}, got "
                f"{response.status_code}: {response.text[:1000]}"
            )
        if not response.content:
            return None
        return response.json()

    async def wait_for_auto_assignment_run_as(
        self,
        client: httpx.AsyncClient,
        user: dict[str, Any],
        *,
        previous_run_id: str | None = None,
        expected_teams: int | None = None,
    ) -> dict[str, Any]:
        job = await self.request_as(
            client,
            user,
            "POST",
            f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment/run-async",
            expected=202,
        )
        return await self._poll_auto_assignment_run_as(
            client,
            user,
            previous_run_id=previous_run_id,
            expected_teams=expected_teams,
            task_id=str(job["task_id"]),
        )

    async def wait_for_auto_assignment_run(
        self,
        client: httpx.AsyncClient,
        *,
        previous_run_id: str | None = None,
        expected_teams: int | None = None,
    ) -> dict[str, Any]:
        job = await self.request(
            client,
            "POST",
            f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment/run-async",
            expected=202,
        )
        return await self._poll_auto_assignment_run(
            client,
            previous_run_id=previous_run_id,
            expected_teams=expected_teams,
            task_id=str(job["task_id"]),
        )

    async def _poll_auto_assignment_run_as(
        self,
        client: httpx.AsyncClient,
        user: dict[str, Any],
        *,
        previous_run_id: str | None,
        expected_teams: int | None,
        task_id: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(300.0, self.http_timeout * 12)
        last_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            state = await self.request_as(
                client,
                user,
                "GET",
                f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment",
                expected=200,
            )
            last_state = state
            latest_run = state.get("latest_run")
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
        previous_run_id: str | None,
        expected_teams: int | None,
        task_id: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(300.0, self.http_timeout * 12)
        last_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            state = await self.request(
                client,
                "GET",
                f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment",
                expected=200,
            )
            last_state = state
            latest_run = state.get("latest_run")
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

    async def record_preprod_run(self, **updates: Any) -> None:
        async with session_factory()() as db_session:
            run = await db_session.scalar(select(PreprodTestRun).where(PreprodTestRun.marker == self.marker))
            if run is None:
                run = PreprodTestRun(
                    marker=self.marker,
                    status=str(updates.pop("status", "running")),
                    origin=self.origin,
                    requested_users=(
                        self.scale_users
                        if self.mode in {"scale", "browser-polling", "write-burst"}
                        else len(self.user_ids)
                    ),
                    report_path=str(self.report_path),
                    started_at=datetime.now(UTC),
                    report=dict(self.report),
                )
                db_session.add(run)
            for key, value in updates.items():
                if hasattr(run, key):
                    setattr(run, key, value)
            run.report = dict(self.report)
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

    async def bounded_each(self, items: list[dict[str, Any]], task) -> None:
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_one(item: dict[str, Any]) -> None:
            async with semaphore:
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
                await self.record_preprod_run(created_users=len(users))

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

    async def grant_browser_polling_permissions(
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

    async def create_browser_polling_tournament(
        self,
        api_client: httpx.AsyncClient,
        *,
        organizer: dict[str, Any],
        category: str,
        index: int,
    ) -> dict[str, Any]:
        name = f"BP {index:02d} {category.replace('_', '-')}"[:25]
        created = await self.request_as(
            api_client,
            organizer,
            "POST",
            "/tournaments",
            expected=201,
            json_payload={
                "name": name,
                "description": f"Browser polling profile {self.marker} {category}.",
                "visibility": "public",
                "format_slug": "solo",
                "allowed_ranks": VALID_RANKS,
                "max_participants": self.browser_polling_users_per_tournament,
                "match_format": "bo3",
                "final_format": "bo5",
                "teams_count": BROWSER_POLLING_READY_TEAMS,
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
        return {**created, "category": category}

    async def join_browser_polling_participants(
        self,
        api_client: httpx.AsyncClient,
        *,
        tournament: dict[str, Any],
        participants: list[dict[str, Any]],
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

        await self.bounded_each(participants, join_user)

    async def setup_browser_polling_tournament_state(
        self,
        api_client: httpx.AsyncClient,
        *,
        tournament: dict[str, Any],
        organizer: dict[str, Any],
        participants: list[dict[str, Any]],
    ) -> None:
        category = str(tournament["category"])
        slug = str(tournament["slug"])
        if participants and category != "terminal":
            await self.join_browser_polling_participants(
                api_client,
                tournament=tournament,
                participants=participants,
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

        await self.bounded_each(participants, vote_yes)
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
            json_payload={"teams_count": BROWSER_POLLING_READY_TEAMS},
        )
        previous_slug = self.tournament_slug
        self.tournament_slug = slug
        try:
            final_run = await self.wait_for_auto_assignment_run_as(
                api_client,
                organizer,
                expected_teams=BROWSER_POLLING_READY_TEAMS,
            )
        finally:
            self.tournament_slug = previous_slug
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

    def build_browser_polling_tabs(
        self,
        *,
        tournaments: list[dict[str, Any]],
        user_chunks: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        tabs: list[dict[str, Any]] = []
        for tournament_index, (tournament, users) in enumerate(zip(tournaments, user_chunks)):
            slug = str(tournament["slug"])
            category = str(tournament["category"])
            organizer_count = max(1, round(len(users) * 0.05))
            participant_count = max(1, round(len(users) * 0.35))
            for user_index, user in enumerate(users):
                if user_index == 0:
                    role = "organizer"
                elif user_index < organizer_count:
                    role = "admin"
                elif user_index < organizer_count + participant_count:
                    role = "participant"
                else:
                    role = "viewer"
                hidden_after_open = (len(tabs) % 10) in {0, 1, 2}
                route = f"/tournaments/{slug}"
                route_label = "GET /tournaments/{slug}"
                if category == "ready_check_active" and role == "participant":
                    route = f"/tournaments/{slug}/deadlock/ready-check"
                    route_label = "GET /tournaments/{slug}/deadlock/ready-check"
                elif category == "bracket_active":
                    if role in {"organizer", "admin"} or user_index % 2 == 0:
                        route = f"/tournaments/{slug}/bracket?teams_view=summary"
                        route_label = "GET /tournaments/{slug}/bracket"
                    else:
                        route = (
                            f"/tournaments/{slug}/workspace"
                            "?participants_limit=0&participants_offset=0"
                            "&workspace_view=bracket_summary"
                        )
                        route_label = "GET /tournaments/{slug}/workspace"
                elif category != "terminal":
                    route = (
                        f"/tournaments/{slug}/workspace"
                        "?participants_limit=0&participants_offset=0"
                        "&workspace_view=detail"
                    )
                    route_label = "GET /tournaments/{slug}/workspace"
                tabs.append(
                    {
                        "client_id": f"tab-{tournament_index:02d}-{user_index:02d}",
                        "tab_index": len(tabs),
                        "user": user,
                        "role": role,
                        "tournament_status": category,
                        "slug": slug,
                        "route": route,
                        "route_label": route_label,
                        "hidden_after_open": hidden_after_open,
                        "hidden": False,
                        "terminal_known": False,
                        "abort_once": (len(tabs) % 37) == 0,
                        "open_stagger_seconds": (
                            0.0
                            if self.browser_polling_open_stagger <= 0
                            else round(
                                (
                                    ((len(tabs) * 37) % 1000) / 999
                                )
                                * self.browser_polling_open_stagger,
                                4,
                            )
                        ),
                    }
                )
        return tabs

    async def execute_browser_polling_request(
        self,
        api_client: httpx.AsyncClient,
        tab: dict[str, Any],
        inflight: set[str],
    ) -> dict[str, Any] | None:
        route_label = str(tab["route_label"])
        inflight_key = f"{tab['client_id']}:{route_label}"
        if inflight_key in inflight:
            self.polling_metrics.mark(
                "deduped",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
                hidden=bool(tab.get("hidden")),
                terminal_known=bool(tab.get("terminal_known")),
            )
            return None
        if tab.get("abort_next"):
            tab["abort_next"] = False
            self.polling_metrics.mark(
                "aborted",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
                hidden=bool(tab.get("hidden")),
                terminal_known=bool(tab.get("terminal_known")),
            )
            return None
        inflight.add(inflight_key)
        await asyncio.sleep(0)
        try:
            self.polling_metrics.mark(
                "executed",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
                hidden=bool(tab.get("hidden")),
                terminal_known=bool(tab.get("terminal_known")),
            )
            return await self.request_as(
                api_client,
                tab["user"],
                "GET",
                str(tab["route"]),
                expected=200,
            )
        finally:
            inflight.discard(inflight_key)

    @staticmethod
    def extract_next_poll_delay_ms(payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        raw = payload.get("next_poll_after_ms")
        if isinstance(raw, int):
            return raw
        for key in ("tournament", "bracket", "ready_check"):
            nested = payload.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("next_poll_after_ms"), int):
                return int(nested["next_poll_after_ms"])
        return None

    @staticmethod
    def payload_is_terminal(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        status_value = payload.get("status")
        if status_value is None and isinstance(payload.get("tournament"), dict):
            status_value = payload["tournament"].get("status")
        return status_value in {"completed", "cancelled"}

    async def run_browser_polling_tab(
        self,
        api_client: httpx.AsyncClient,
        tab: dict[str, Any],
        *,
        profile_duration: float,
        inflight: set[str],
    ) -> None:
        open_stagger_seconds = float(tab.get("open_stagger_seconds") or 0.0)
        if open_stagger_seconds > 0:
            await asyncio.sleep(open_stagger_seconds)
        deadline = time.monotonic() + profile_duration
        route_label = str(tab["route_label"])
        self.polling_metrics.mark(
            "scheduled",
            route=route_label,
            role=str(tab["role"]),
            tournament_status=str(tab["tournament_status"]),
        )
        if route_label == "GET /tournaments/{slug}/bracket":
            first = asyncio.create_task(
                self.execute_browser_polling_request(api_client, tab, inflight)
            )
            await asyncio.sleep(0)
            self.polling_metrics.mark(
                "scheduled",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
            )
            await self.execute_browser_polling_request(api_client, tab, inflight)
            payload = await first
        else:
            payload = await self.execute_browser_polling_request(api_client, tab, inflight)
        raw_delay_ms = self.extract_next_poll_delay_ms(payload)
        if raw_delay_ms == 0 or self.payload_is_terminal(payload):
            tab["terminal_known"] = True
        if tab.get("hidden_after_open"):
            tab["hidden"] = True
            self.polling_metrics.mark(
                "skipped_hidden",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
                hidden=True,
                terminal_known=bool(tab.get("terminal_known")),
            )
            return
        if tab.get("terminal_known"):
            self.polling_metrics.mark(
                "skipped_terminal",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
                terminal_known=True,
            )
            return

        tick = 0
        duplicate_probe_done = False
        while time.monotonic() < deadline:
            delay_seconds = polling_delay_seconds(
                raw_delay_ms,
                tab_index=int(tab["tab_index"]),
                tick=tick,
            )
            if delay_seconds is None or time.monotonic() + delay_seconds > deadline:
                break
            await asyncio.sleep(delay_seconds)
            tick += 1
            self.polling_metrics.mark(
                "scheduled",
                route=route_label,
                role=str(tab["role"]),
                tournament_status=str(tab["tournament_status"]),
                hidden=bool(tab.get("hidden")),
                terminal_known=bool(tab.get("terminal_known")),
            )
            if tab.get("hidden"):
                self.polling_metrics.mark(
                    "skipped_hidden",
                    route=route_label,
                    role=str(tab["role"]),
                    tournament_status=str(tab["tournament_status"]),
                    hidden=True,
                    terminal_known=bool(tab.get("terminal_known")),
                )
                break
            if tab.get("terminal_known"):
                self.polling_metrics.mark(
                    "skipped_terminal",
                    route=route_label,
                    role=str(tab["role"]),
                    tournament_status=str(tab["tournament_status"]),
                    terminal_known=True,
                )
                break
            if tab.get("abort_once"):
                tab["abort_once"] = False
                tab["abort_next"] = True
                await self.execute_browser_polling_request(api_client, tab, inflight)
                continue
            if route_label == "GET /tournaments/{slug}/bracket" and not duplicate_probe_done:
                duplicate_probe_done = True
                first = asyncio.create_task(
                    self.execute_browser_polling_request(api_client, tab, inflight)
                )
                await asyncio.sleep(0)
                await self.execute_browser_polling_request(api_client, tab, inflight)
                payload = await first
            else:
                payload = await self.execute_browser_polling_request(api_client, tab, inflight)
            raw_delay_ms = self.extract_next_poll_delay_ms(payload)
            if raw_delay_ms == 0 or self.payload_is_terminal(payload):
                tab["terminal_known"] = True

    async def run_browser_polling_profile(self) -> dict[str, Any]:
        api_client = await self.new_client()
        started = time.monotonic()
        try:
            await self.record_preprod_run(status="running", requested_users=self.scale_users)
            await self.start_performance_collection()
            with self.phase("browser_polling_seed_users"):
                users = await self.bulk_register_scale_users()
            self.scenario(
                "browser_polling_users_created",
                len(users) == self.scale_users,
                {"users": len(users)},
            )

            tournament_count = sum(count for _, count in BROWSER_POLLING_TOURNAMENT_PLAN)
            chunk_size = self.browser_polling_users_per_tournament
            user_chunks = [
                users[index * chunk_size : (index + 1) * chunk_size]
                for index in range(tournament_count)
            ]
            organizers = [chunk[0] for chunk in user_chunks if chunk]
            admins = [chunk[1] for chunk in user_chunks if len(chunk) > 1]
            await self.grant_browser_polling_permissions(organizers, admins)

            tournament_categories = [
                category
                for category, count in BROWSER_POLLING_TOURNAMENT_PLAN
                for _ in range(count)
            ]
            tournaments: list[dict[str, Any]] = []
            with self.phase("browser_polling_tournament_setup"):
                for index, (category, chunk) in enumerate(zip(tournament_categories, user_chunks), start=1):
                    tournament = await self.create_browser_polling_tournament(
                        api_client,
                        organizer=chunk[0],
                        category=category,
                        index=index,
                    )
                    tournaments.append(tournament)
                await self.record_preprod_run(tournaments_created=len(tournaments))
            self.scenario(
                "browser_polling_tournaments_created",
                len(tournaments) == tournament_count,
                {
                    "tournaments": len(tournaments),
                    "plan": dict(BROWSER_POLLING_TOURNAMENT_PLAN),
                },
            )

            active_participants = 0
            with self.phase("browser_polling_state_setup"):
                for tournament, chunk in zip(tournaments, user_chunks):
                    organizer_count = max(1, round(len(chunk) * 0.05))
                    participant_count = max(1, round(len(chunk) * 0.35))
                    participants = chunk[organizer_count : organizer_count + participant_count]
                    if tournament["category"] != "terminal":
                        active_participants += len(participants)
                    await self.setup_browser_polling_tournament_state(
                        api_client,
                        tournament=tournament,
                        organizer=chunk[0],
                        participants=participants,
                    )
            await self.record_preprod_run(active_participants=active_participants)

            tabs = self.build_browser_polling_tabs(
                tournaments=tournaments,
                user_chunks=user_chunks,
            )
            fixed_expected = fixed_polling_expectation(
                duration_seconds=self.browser_polling_duration,
                tabs=tabs,
            )
            self.report["polling"] = {
                "profile": self.browser_polling_profile,
                "duration_seconds": self.browser_polling_duration,
                "tournament_plan": dict(BROWSER_POLLING_TOURNAMENT_PLAN),
                "users_per_tournament": self.browser_polling_users_per_tournament,
                "open_stagger_seconds": self.browser_polling_open_stagger,
                "tabs_planned": len(tabs),
                "visible_tabs": sum(1 for tab in tabs if not tab["hidden_after_open"]),
                "hidden_tabs": sum(1 for tab in tabs if tab["hidden_after_open"]),
                "load_generator_local": is_local_origin(self.origin),
                "fixed_polling_expectation": fixed_expected,
            }
            self.scenario(
                "browser_polling_tabs_planned",
                len(tabs) == self.scale_users,
                {
                    "tabs": len(tabs),
                    "visible": self.report["polling"]["visible_tabs"],
                    "hidden": self.report["polling"]["hidden_tabs"],
                },
            )

            with self.phase("browser_polling_run"):
                inflight: set[str] = set()
                await asyncio.gather(
                    *(
                        self.run_browser_polling_tab(
                            api_client,
                            tab,
                            profile_duration=self.browser_polling_duration,
                            inflight=inflight,
                        )
                        for tab in tabs
                    )
                )

            polling_summary = self.polling_metrics.summary()
            self.report["polling"].update(polling_summary)
            self.scenario(
                "browser_polling_hidden_tabs_do_not_poll",
                not polling_summary["executed_while_hidden"],
                polling_summary["executed_while_hidden"],
            )
            self.scenario(
                "browser_polling_terminal_tabs_do_not_poll",
                not polling_summary["executed_after_terminal"],
                polling_summary["executed_after_terminal"],
            )
            self.scenario(
                "browser_polling_bracket_deduped",
                polling_summary["deduped"] > 0,
                {"deduped": polling_summary["deduped"]},
            )
            long_enough_for_polling_tick = (
                self.browser_polling_duration
                >= (BROWSER_POLLING_FIXED_INTERVAL_MS / 1000)
            )
            self.scenario(
                "browser_polling_abort_counter_populated",
                True if not long_enough_for_polling_tick else polling_summary["aborted"] > 0,
                {
                    "aborted": polling_summary["aborted"],
                    "skipped_short_duration": not long_enough_for_polling_tick,
                },
            )
            self.scenario(
                "browser_polling_repeated_gets_below_fixed_polling",
                True
                if not long_enough_for_polling_tick
                else polling_summary["executed"] < fixed_expected["total_expected_gets"],
                {
                    "executed": polling_summary["executed"],
                    "fixed_expected_gets": fixed_expected["total_expected_gets"],
                    "skipped_short_duration": not long_enough_for_polling_tick,
                },
            )
            self.report["duration_seconds"] = round(time.monotonic() - started, 4)
            self.scenario("browser_polling_profile_complete", all(item["ok"] for item in self.scenarios))
            await self.record_preprod_run(
                status="passed",
                created_users=len(users),
                tournaments_created=len(tournaments),
                active_participants=active_participants,
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
                        "name": "browser_polling_cleanup",
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
                "name": f"WB {index:02d} {label.replace('_', '-')}"[:25],
                "description": f"Write burst profile {self.marker} {label}.",
                "visibility": "public",
                "format_slug": "solo",
                "allowed_ranks": VALID_RANKS,
                "max_participants": self.write_burst_users_per_tournament,
                "match_format": "bo3",
                "final_format": "bo5",
                "teams_count": BROWSER_POLLING_READY_TEAMS,
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
    ) -> dict[str, Any]:
        tournament = await self.create_write_burst_tournament(
            api_client,
            organizer=organizer,
            label=label,
            index=index,
        )
        await self.join_browser_polling_participants(
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
                await self.setup_browser_polling_tournament_state(
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
            await self.grant_browser_polling_permissions(list(organizers_by_id.values()), [])

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
        include_ready_check: bool,
        include_auto_assignment: bool,
        include_bracket: bool,
        bracket_user_limit: int | None = None,
    ) -> None:
        if not users:
            return
        api_client = await self.new_client()
        bracket_user_limit = len(users) if bracket_user_limit is None else bracket_user_limit

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
            await self.request_as(
                api_client,
                user,
                "GET",
                f"/tournaments/{self.tournament_slug}/workspace?participants_limit=0&participants_offset=0&workspace_view=detail",
                expected=200,
            )
            if include_ready_check:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/deadlock/ready-check",
                    expected=200,
                )
            if include_auto_assignment:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/deadlock/auto-assignment",
                    expected=200,
                )
            if include_bracket:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/bracket?teams_view=summary",
                    expected=200,
                )

        await self.bounded_each(users[: self.scale_site_mix_users], browse)
        if include_bracket and bracket_user_limit > self.scale_site_mix_users:
            remaining = users[self.scale_site_mix_users : bracket_user_limit]
            async def view_bracket(user: dict[str, Any]) -> None:
                await self.request_as(
                    api_client,
                    user,
                    "GET",
                    f"/tournaments/{self.tournament_slug}/bracket?teams_view=summary",
                    expected=200,
                )

            await self.bounded_each(remaining, view_bracket)

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

    async def wait_for_browser_gate(
        self,
        *,
        organizer: dict[str, Any],
        watcher: dict[str, Any],
        bracket: dict[str, Any],
    ) -> None:
        if self.browser_gate_dir is None:
            return

        self.browser_gate_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.browser_gate_dir / f"state-{self.marker}.json"
        result_path = self.browser_gate_dir / f"result-{self.marker}.json"
        state_path.write_text(
            json.dumps(
                {
                    "marker": self.marker,
                    "origin": self.origin,
                    "slug": self.tournament_slug,
                    "organizer_email": organizer["email"],
                    "watcher_email": watcher["email"],
                    "password": self.password,
                    "initial_revision": bracket["revision"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        try:
            deadline = time.monotonic() + self.browser_gate_timeout
            while not result_path.exists():
                if time.monotonic() >= deadline:
                    raise QaFailure(
                        f"two_browser_realtime: timed out waiting for {result_path}"
                    )
                await asyncio.sleep(0.25)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.scenario(
                "two_browser_realtime_under_two_seconds",
                bool(result.get("ok"))
                and float(result.get("pointer_seconds", 999)) <= 2
                and float(result.get("keyboard_seconds", 999)) <= 2,
                result,
            )
        finally:
            state_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)

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

    async def wait_for_bracket_event(
        self,
        *,
        watcher: httpx.AsyncClient,
        trigger,
        expected_after_revision: int,
    ) -> tuple[Any, float]:
        connected = asyncio.Event()
        event_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async def listen() -> None:
            timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
            async with watcher.stream(
                "GET",
                f"/tournaments/{self.tournament_slug}/bracket/events",
                timeout=timeout,
            ) as response:
                if response.status_code != 200:
                    raise QaFailure(
                        f"SSE returned {response.status_code}: {await response.aread()}"
                    )
                current_event = ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        current_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        connected.set()
                        if current_event != "bracket":
                            continue
                        payload = json.loads(line.split(":", 1)[1].strip())
                        if int(payload.get("revision") or 0) > expected_after_revision:
                            if not event_future.done():
                                event_future.set_result(payload)
                            return

        listener = asyncio.create_task(listen())
        try:
            await asyncio.wait_for(connected.wait(), timeout=2)
            started = time.monotonic()
            mutation_result = await trigger()
            await asyncio.wait_for(event_future, timeout=2)
            return mutation_result, time.monotonic() - started
        finally:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)

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
                        include_ready_check=False,
                        include_auto_assignment=False,
                        include_bracket=False,
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
                    self.scenario(
                        "control_participant_assigned",
                        assigned_team is not None,
                        control_report,
                    )

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
            await self.wait_for_browser_gate(
                organizer=organizer,
                watcher=active_players[1],
                bracket=bracket,
            )

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
            async def opening_report_trigger() -> dict[str, Any]:
                return await play_match(quarterfinals[0], 2, 0)

            _, realtime_seconds = await self.wait_for_bracket_event(
                watcher=active_players[1]["client"],
                trigger=opening_report_trigger,
                expected_after_revision=bracket["revision"],
            )
            self.scenario(
                "edge_sse_realtime_under_two_seconds",
                realtime_seconds <= 2,
                {"seconds": round(realtime_seconds, 4)},
            )
            self.scenario("simplified_score_report_without_match_start", True)
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
                    select(TournamentInviteAccess.id).where(
                        TournamentInviteAccess.tournament_id.in_(tournament_ids)
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

            if subject_ids or self.user_ids:
                await db_session.execute(
                    delete(AuditLog).where(
                        or_(
                            AuditLog.subject_id.in_(list(subject_ids)),
                            AuditLog.actor_user_id.in_(self.user_ids),
                        )
                    )
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
    polling = report.get("polling") if isinstance(report.get("polling"), dict) else {}
    fixed_expected = (
        polling.get("fixed_polling_expectation")
        if isinstance(polling, dict)
        and isinstance(polling.get("fixed_polling_expectation"), dict)
        else {}
    )
    write_burst = report.get("write_burst") if isinstance(report.get("write_burst"), dict) else {}
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
        "teams": len(report.get("strength_ranking") or []),
        "matches": len(report.get("match_path") or []) or report.get("matches_count"),
        "duration_seconds": report.get("duration_seconds"),
        "assignment_seconds": report.get("assignment_seconds"),
        "fatal_error": report.get("fatal_error"),
        "polling": (
            {
                "profile": polling.get("profile"),
                "tabs_planned": polling.get("tabs_planned"),
                "total_scheduled": polling.get("total_scheduled"),
                "executed": polling.get("executed"),
                "skipped_hidden": polling.get("skipped_hidden"),
                "skipped_terminal": polling.get("skipped_terminal"),
                "deduped": polling.get("deduped"),
                "aborted": polling.get("aborted"),
                "fixed_expected_gets": fixed_expected.get("total_expected_gets"),
                "load_generator_local": polling.get("load_generator_local"),
            }
            if polling
            else None
        ),
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
        live_env = Path("/opt/oldsparky/platform/shared/.env.platform")
        env_file = live_env if live_env.exists() else PLATFORM_ROOT / ".env.platform"
    load_env_file(env_file)

    qa = ProductionQa(
        origin=args.origin,
        report_path=args.report_path,
        keep_data=args.keep_data,
        browser_gate_dir=args.browser_gate_dir,
        browser_gate_timeout=args.browser_gate_timeout,
        http_timeout=args.http_timeout,
        mode=args.mode,
        scale_users=args.scale_users,
        scale_teams=args.scale_teams,
        concurrency=args.concurrency,
        collect_performance=args.collect_performance,
        system_sample_interval=args.system_sample_interval,
        scale_site_mix_users=args.scale_site_mix_users,
        scale_bracket_view_users=args.scale_bracket_view_users,
        scale_final_view_profile=args.scale_final_view_profile,
        tournament_visibility=args.tournament_visibility,
        profile_journey=args.profile_journey,
        browser_polling_profile=args.browser_polling_profile,
        browser_polling_duration=args.browser_polling_duration,
        browser_polling_users_per_tournament=args.browser_polling_users_per_tournament,
        browser_polling_open_stagger=args.browser_polling_open_stagger,
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
        if args.mode == "scale":
            report = await qa.run_scale()
        elif args.mode == "browser-polling":
            report = await qa.run_browser_polling_profile()
        elif args.mode == "write-burst":
            report = await qa.run_write_burst_profile()
        else:
            report = await qa.run()
    except Exception as exc:
        qa.report["fatal_error"] = str(exc)
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
