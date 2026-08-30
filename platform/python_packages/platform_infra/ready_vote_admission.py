"""Process-local adaptive admission control for the Ready Vote hot path.

The controller deliberately lives before the route-owned database session.  It
protects the two-core API workers from accepting more Ready Vote work than the
event loop and database path can complete, while leaving authentication,
workflow authorization and PostgreSQL as the correctness authorities.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import logging
from pathlib import Path
from time import perf_counter

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.performance import (
    record_ready_vote_admission_completion,
    record_ready_vote_admission_start,
)

logger = logging.getLogger("platform.ready_vote_admission")

READY_VOTE_ADMISSION_NORMAL = "normal"
READY_VOTE_ADMISSION_WATCH = "watch"
READY_VOTE_ADMISSION_PRESSURE = "pressure"
READY_VOTE_ADMISSION_SEVERE = "severe"

CPU_NORMAL_THRESHOLD_PERCENT = 70.0
CPU_PRESSURE_THRESHOLD_PERCENT = 80.0
CPU_SEVERE_THRESHOLD_PERCENT = 90.0

# These ratios are intentionally conservative for the two-worker production
# host.  They are pressure signals, not a reason to queue low-load traffic:
# an idle controller does not wait, batch, or use Redis to coordinate votes.
INFLIGHT_WATCH_RATIO = 0.75
INFLIGHT_PRESSURE_RATIO = 0.90
INFLIGHT_SEVERE_RATIO = 1.0


@dataclass(frozen=True, slots=True)
class ReadyVoteAdmissionConfig:
    enabled: bool = True
    min_concurrency: int = 4
    initial_concurrency: int = 8
    max_concurrency: int = 16
    max_waiters: int = 0
    wait_timeout_ms: float = 0.0
    cpu_sample_interval_seconds: float = 0.5
    cpu_ewma_alpha: float = 0.25
    recovery_samples: int = 8
    control_interval_seconds: float = 0.5

    @classmethod
    def from_settings(cls) -> "ReadyVoteAdmissionConfig":
        settings = get_settings()
        return cls(
            enabled=settings.platform_ready_vote_admission_enabled,
            min_concurrency=settings.platform_ready_vote_admission_min_concurrency,
            initial_concurrency=settings.platform_ready_vote_admission_initial_concurrency,
            max_concurrency=settings.platform_ready_vote_admission_max_concurrency,
            max_waiters=settings.platform_ready_vote_admission_max_waiters,
            wait_timeout_ms=settings.platform_ready_vote_admission_wait_timeout_ms,
            cpu_sample_interval_seconds=(
                settings.platform_ready_vote_admission_cpu_sample_interval_seconds
            ),
            cpu_ewma_alpha=settings.platform_ready_vote_admission_cpu_ewma_alpha,
            recovery_samples=settings.platform_ready_vote_admission_recovery_samples,
            control_interval_seconds=(
                settings.platform_ready_vote_admission_control_interval_seconds
            ),
        )


@dataclass(frozen=True, slots=True)
class ReadyVoteAdmissionSnapshot:
    inflight: int
    limit: int
    waiters: int
    state: str
    cpu_pressure: float
    admitted_total: int
    shed_total: int
    limit_changes: int
    latency_ewma_ms: float
    pool_wait_ewma_ms: float
    admission_wait_ewma_ms: float
    cpu_monitor_sample_ms: float
    cpu_monitor_samples: int


@dataclass(slots=True)
class ReadyVoteAdmissionLease:
    controller: "ReadyVoteAdmissionController"
    started_at: float
    wait_ms: float
    snapshot: ReadyVoteAdmissionSnapshot
    released: bool = False

    async def release(
        self,
        *,
        service_ms: float,
        pool_wait_ms: float,
    ) -> None:
        if self.released:
            return
        self.released = True
        await self.controller.release(
            lease=self,
            service_ms=service_ms,
            pool_wait_ms=pool_wait_ms,
        )


def _read_cpu_totals() -> dict[str, tuple[int, int]]:
    """Read aggregate per-core CPU counters once per monitor tick."""

    try:
        lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}

    totals: dict[str, tuple[int, int]] = {}
    for line in lines:
        columns = line.split()
        if not columns or columns[0] == "cpu" or not columns[0].startswith("cpu"):
            continue
        name = columns[0]
        if not name[3:].isdigit():
            continue
        try:
            values = [int(value) for value in columns[1:]]
        except ValueError:
            continue
        if len(values) < 4:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        totals[name] = (sum(values), idle)
    return totals


def _cpu_percentages(
    previous: dict[str, tuple[int, int]] | None,
    current: dict[str, tuple[int, int]],
) -> dict[str, float]:
    if previous is None:
        return {}
    percentages: dict[str, float] = {}
    for core, (total, idle) in current.items():
        previous_total, previous_idle = previous.get(core, (total, idle))
        total_delta = total - previous_total
        idle_delta = idle - previous_idle
        if total_delta <= 0:
            continue
        percentages[core] = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))
    return percentages


class ReadyVoteAdmissionController:
    """Bounded, event-loop-local controller for one API worker process."""

    def __init__(self, config: ReadyVoteAdmissionConfig | None = None) -> None:
        self.config = config or ReadyVoteAdmissionConfig.from_settings()
        self._limit = self.config.initial_concurrency
        self._inflight = 0
        self._waiters = 0
        self._state = READY_VOTE_ADMISSION_NORMAL
        self._cpu_pressure: float | None = None
        self._latency_ewma_ms = 0.0
        self._pool_wait_ewma_ms = 0.0
        self._admission_wait_ewma_ms = 0.0
        self._recent_latencies_ms: deque[float] = deque(maxlen=64)
        self._admitted_total = 0
        self._shed_total = 0
        self._limit_changes = 0
        self._pressure_samples = 0
        self._severe_samples = 0
        self._healthy_samples = 0
        self._last_control_at = 0.0
        self._cpu_monitor_sample_ms = 0.0
        self._cpu_monitor_samples = 0
        self._state_lock = asyncio.Lock()
        self._capacity_event = asyncio.Event()
        self._capacity_event.set()
        self._monitor_task: asyncio.Task[None] | None = None
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._previous_cpu_totals: dict[str, tuple[int, int]] | None = None

    @property
    def monitor_task(self) -> asyncio.Task[None] | None:
        return self._monitor_task

    def start(self) -> None:
        self._ensure_event_loop()
        if not self.config.enabled or self._monitor_task is not None:
            return
        self._monitor_task = asyncio.create_task(
            self._monitor_cpu(),
            name="ready-vote-admission-cpu-monitor",
        )

    async def stop(self) -> None:
        task = self._monitor_task
        self._monitor_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def acquire(self) -> ReadyVoteAdmissionLease | None:
        """Admit immediately, use only a bounded waiter budget, or shed."""

        self._ensure_event_loop()
        if not self.config.enabled:
            now = perf_counter()
            async with self._state_lock:
                self._inflight += 1
                self._admitted_total += 1
                snapshot = self._snapshot_locked()
            record_ready_vote_admission_start(
                snapshot=snapshot,
                wait_ms=0.0,
                admitted=True,
            )
            return ReadyVoteAdmissionLease(self, now, 0.0, snapshot)

        self.start()
        started_at = perf_counter()
        deadline = started_at + max(0.0, self.config.wait_timeout_ms) / 1000

        while True:
            async with self._state_lock:
                if self._inflight < self._limit:
                    self._inflight += 1
                    self._admitted_total += 1
                    self._refresh_capacity_event_locked()
                    wait_ms = (perf_counter() - started_at) * 1000
                    snapshot = self._snapshot_locked()
                    record_ready_vote_admission_start(
                        snapshot=snapshot,
                        wait_ms=wait_ms,
                        admitted=True,
                    )
                    return ReadyVoteAdmissionLease(
                        self,
                        started_at,
                        wait_ms,
                        snapshot,
                    )

                wait_remaining = deadline - perf_counter()
                if self.config.max_waiters <= self._waiters or wait_remaining <= 0:
                    self._shed_total += 1
                    self._recompute_control_locked()
                    self._refresh_capacity_event_locked()
                    wait_ms = (perf_counter() - started_at) * 1000
                    snapshot = self._snapshot_locked()
                    record_ready_vote_admission_start(
                        snapshot=snapshot,
                        wait_ms=wait_ms,
                        admitted=False,
                    )
                    return None
                self._waiters += 1

            try:
                await asyncio.wait_for(
                    self._capacity_event.wait(),
                    timeout=max(0.0, wait_remaining),
                )
            except asyncio.TimeoutError:
                async with self._state_lock:
                    self._waiters = max(0, self._waiters - 1)
                    self._shed_total += 1
                    self._refresh_capacity_event_locked()
                    wait_ms = (perf_counter() - started_at) * 1000
                    snapshot = self._snapshot_locked()
                    record_ready_vote_admission_start(
                        snapshot=snapshot,
                        wait_ms=wait_ms,
                        admitted=False,
                    )
                    return None
            except asyncio.CancelledError:
                async with self._state_lock:
                    self._waiters = max(0, self._waiters - 1)
                    self._refresh_capacity_event_locked()
                raise
            else:
                async with self._state_lock:
                    self._waiters = max(0, self._waiters - 1)

    async def release(
        self,
        *,
        lease: ReadyVoteAdmissionLease,
        service_ms: float,
        pool_wait_ms: float,
    ) -> None:
        async with self._state_lock:
            self._inflight = max(0, self._inflight - 1)
            self._observe_locked(
                service_ms=max(0.0, service_ms),
                pool_wait_ms=max(0.0, pool_wait_ms),
                admission_wait_ms=lease.wait_ms,
            )
            self._refresh_capacity_event_locked()
            snapshot = self._snapshot_locked()
        record_ready_vote_admission_completion(
            snapshot=snapshot,
            pool_wait_ms=max(0.0, pool_wait_ms),
        )

    def snapshot(self) -> ReadyVoteAdmissionSnapshot:
        """Return a cheap diagnostic snapshot for tests and operators."""

        # All mutations run on the event loop. This read is intentionally
        # lock-free so it can be used while constructing a 503 response.
        return self._snapshot_locked()

    async def observe_cpu_percent(self, cpu_percent: float) -> None:
        """Inject a CPU sample; primarily useful for deterministic tests."""

        async with self._state_lock:
            self._update_cpu_locked(max(0.0, min(100.0, cpu_percent)))

    async def _monitor_cpu(self) -> None:
        interval = max(0.1, self.config.cpu_sample_interval_seconds)
        while True:
            sample_started = perf_counter()
            current = _read_cpu_totals()
            percentages = _cpu_percentages(self._previous_cpu_totals, current)
            self._previous_cpu_totals = current
            if percentages:
                cpu_percent = sum(percentages.values()) / len(percentages)
                async with self._state_lock:
                    self._cpu_monitor_sample_ms = (perf_counter() - sample_started) * 1000
                    self._cpu_monitor_samples += 1
                    self._update_cpu_locked(cpu_percent)
            await asyncio.sleep(interval)

    def _ensure_event_loop(self) -> None:
        """Rebind test/reload instances without crossing asyncio loop state."""

        current_loop = asyncio.get_running_loop()
        if self._bound_loop is current_loop:
            return
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = None
        self._state_lock = asyncio.Lock()
        self._capacity_event = asyncio.Event()
        self._capacity_event.set()
        self._bound_loop = current_loop
        self._previous_cpu_totals = None
        self._inflight = 0
        self._waiters = 0

    def _update_cpu_locked(self, cpu_percent: float) -> None:
        alpha = max(0.01, min(1.0, self.config.cpu_ewma_alpha))
        self._cpu_pressure = (
            cpu_percent
            if self._cpu_pressure is None
            else alpha * cpu_percent + (1 - alpha) * self._cpu_pressure
        )
        self._recompute_control_locked()

    def _observe_locked(
        self,
        *,
        service_ms: float,
        pool_wait_ms: float,
        admission_wait_ms: float,
    ) -> None:
        alpha = 0.2
        self._latency_ewma_ms = (
            service_ms
            if self._latency_ewma_ms == 0
            else alpha * service_ms + (1 - alpha) * self._latency_ewma_ms
        )
        self._pool_wait_ewma_ms = (
            pool_wait_ms
            if self._pool_wait_ewma_ms == 0
            else alpha * pool_wait_ms + (1 - alpha) * self._pool_wait_ewma_ms
        )
        self._admission_wait_ewma_ms = (
            admission_wait_ms
            if self._admission_wait_ewma_ms == 0
            else alpha * admission_wait_ms + (1 - alpha) * self._admission_wait_ewma_ms
        )
        self._recent_latencies_ms.append(service_ms)
        self._recompute_control_locked()

    def _recompute_control_locked(self) -> None:
        now = perf_counter()
        if now - self._last_control_at < self.config.control_interval_seconds:
            return
        cpu_pressure = self._cpu_pressure or 0.0
        inflight_ratio = self._inflight / max(1, self._limit)

        recent_p90 = 0.0
        if self._recent_latencies_ms:
            ordered = sorted(self._recent_latencies_ms)
            recent_p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        severe = (
            cpu_pressure >= CPU_SEVERE_THRESHOLD_PERCENT
            or inflight_ratio >= INFLIGHT_SEVERE_RATIO
            or self._latency_ewma_ms >= 800
            or recent_p90 >= 900
            or self._pool_wait_ewma_ms >= 50
            or self._admission_wait_ewma_ms >= 50
        )
        pressure = (
            cpu_pressure >= CPU_PRESSURE_THRESHOLD_PERCENT
            or inflight_ratio >= INFLIGHT_PRESSURE_RATIO
            or self._latency_ewma_ms >= 450
            or recent_p90 >= 600
            or self._pool_wait_ewma_ms >= 15
            or self._admission_wait_ewma_ms >= 25
        )
        watch = (
            cpu_pressure >= CPU_NORMAL_THRESHOLD_PERCENT
            or inflight_ratio >= INFLIGHT_WATCH_RATIO
            or self._latency_ewma_ms >= 300
            or recent_p90 >= 400
            or self._pool_wait_ewma_ms >= 5
            or self._admission_wait_ewma_ms >= 10
        )

        self._last_control_at = now
        if severe:
            self._severe_samples += 1
            self._pressure_samples += 1
            self._healthy_samples = 0
            next_state = READY_VOTE_ADMISSION_SEVERE
            delta = -2 if self._severe_samples >= 2 else -1
        elif pressure:
            self._severe_samples = 0
            self._pressure_samples += 1
            self._healthy_samples = 0
            next_state = READY_VOTE_ADMISSION_PRESSURE
            delta = -1 if self._pressure_samples >= 2 else 0
        elif watch:
            self._severe_samples = 0
            self._pressure_samples = 0
            self._healthy_samples = 0
            next_state = READY_VOTE_ADMISSION_WATCH
            delta = 0
        else:
            self._severe_samples = 0
            self._pressure_samples = 0
            self._healthy_samples += 1
            next_state = READY_VOTE_ADMISSION_NORMAL
            # Recovery is deliberately slow, but it must also happen after
            # pressure has drained; requiring a busy queue here would leave a
            # controller stuck at a reduced limit during a quiet period.
            delta = 1 if self._healthy_samples >= self.config.recovery_samples else 0

        old_state = self._state
        old_limit = self._limit
        self._state = next_state
        self._limit = max(
            self.config.min_concurrency,
            min(self.config.max_concurrency, self._limit + delta),
        )
        self._refresh_capacity_event_locked()
        if self._limit != old_limit:
            self._limit_changes += 1
            logger.info(
                "ready_vote_admission_limit_change old_limit=%s new_limit=%s "
                "state=%s cpu_pressure=%.2f latency_ewma_ms=%.2f "
                "pool_wait_ewma_ms=%.2f admission_wait_ewma_ms=%.2f",
                old_limit,
                self._limit,
                self._state,
                cpu_pressure,
                self._latency_ewma_ms,
                self._pool_wait_ewma_ms,
                self._admission_wait_ewma_ms,
            )
        elif self._state != old_state:
            logger.info(
                "ready_vote_admission_controller_state state=%s limit=%s "
                "cpu_pressure=%.2f latency_ewma_ms=%.2f "
                "pool_wait_ewma_ms=%.2f admission_wait_ewma_ms=%.2f",
                self._state,
                self._limit,
                cpu_pressure,
                self._latency_ewma_ms,
                self._pool_wait_ewma_ms,
                self._admission_wait_ewma_ms,
            )

    def _refresh_capacity_event_locked(self) -> None:
        if self._inflight < self._limit:
            self._capacity_event.set()
        else:
            self._capacity_event.clear()

    def _snapshot_locked(self) -> ReadyVoteAdmissionSnapshot:
        return ReadyVoteAdmissionSnapshot(
            inflight=self._inflight,
            limit=self._limit,
            waiters=self._waiters,
            state=self._state,
            cpu_pressure=round(self._cpu_pressure or 0.0, 3),
            admitted_total=self._admitted_total,
            shed_total=self._shed_total,
            limit_changes=self._limit_changes,
            latency_ewma_ms=round(self._latency_ewma_ms, 3),
            pool_wait_ewma_ms=round(self._pool_wait_ewma_ms, 3),
            admission_wait_ewma_ms=round(self._admission_wait_ewma_ms, 3),
            cpu_monitor_sample_ms=round(self._cpu_monitor_sample_ms, 3),
            cpu_monitor_samples=self._cpu_monitor_samples,
        )


_controller: ReadyVoteAdmissionController | None = None


def get_ready_vote_admission_controller() -> ReadyVoteAdmissionController:
    global _controller
    if _controller is None:
        _controller = ReadyVoteAdmissionController()
    return _controller


def start_ready_vote_admission_controller() -> None:
    get_ready_vote_admission_controller().start()


async def stop_ready_vote_admission_controller() -> None:
    controller = _controller
    if controller is not None:
        await controller.stop()
