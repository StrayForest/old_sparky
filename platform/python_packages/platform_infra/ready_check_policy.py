from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import math
from typing import Literal


READY_CHECK_SSE_HARD_TARGET = 10_000
READY_CHECK_SSE_PRODUCTION_LIMIT = 3_000
READY_CHECK_SAFE_OPEN_RATE_PER_SECOND = 25
READY_CHECK_PREPARATION_SAFETY_SECONDS = 15
READY_CHECK_MAX_PREPARATION_SECONDS = 15 * 60
READY_CHECK_LATE_ADMISSION_PRIORITY = "late"
READY_CHECK_SCHEDULED_ADMISSION_PRIORITY = "scheduled"
READY_CHECK_POLLING_ADMISSION_PRIORITY = "polling"
ReadyCheckAdmissionMode = Literal["scheduled_sse", "late_sse", "polling"]


@dataclass(frozen=True, slots=True)
class ReadyCheckDemand:
    tournament_id: str
    starts_at: datetime
    eligible_count: int


@dataclass(frozen=True, slots=True)
class ReadyCheckPreparationPlan:
    preparation_starts_at: datetime
    expected_demand: int
    already_connected: int
    remaining_demand: int
    simultaneous_ready_checks: int
    safe_open_rate_per_second: int


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Ready Check schedule must include timezone information.")
    return value.astimezone(UTC)


def ready_check_preparation_plan(
    demands: list[ReadyCheckDemand] | tuple[ReadyCheckDemand, ...],
    *,
    already_connected: int = 0,
    safe_open_rate_per_second: int = READY_CHECK_SAFE_OPEN_RATE_PER_SECOND,
    safety_seconds: int = READY_CHECK_PREPARATION_SAFETY_SECONDS,
    max_preparation_seconds: int = READY_CHECK_MAX_PREPARATION_SECONDS,
) -> ReadyCheckPreparationPlan:
    if safe_open_rate_per_second < 1:
        raise ValueError("Safe SSE opening rate must be positive.")
    if safety_seconds < 0 or max_preparation_seconds < 0:
        raise ValueError("Preparation margins cannot be negative.")
    normalized = tuple(demand for demand in demands if demand.eligible_count > 0)
    if not normalized:
        raise ValueError("At least one Ready Check demand is required.")
    expected_demand = sum(max(0, int(demand.eligible_count)) for demand in normalized)
    already_connected = max(0, int(already_connected))
    remaining_demand = max(0, expected_demand - already_connected)
    simultaneous = len(normalized)
    opening_seconds = math.ceil(remaining_demand / safe_open_rate_per_second)
    contention_margin = max(0, simultaneous - 1) * 5
    preparation_seconds = min(
        max_preparation_seconds,
        opening_seconds + safety_seconds + contention_margin,
    )
    latest_start = min(_utc(demand.starts_at) for demand in normalized)
    return ReadyCheckPreparationPlan(
        preparation_starts_at=latest_start - timedelta(seconds=preparation_seconds),
        expected_demand=expected_demand,
        already_connected=already_connected,
        remaining_demand=remaining_demand,
        simultaneous_ready_checks=simultaneous,
        safe_open_rate_per_second=safe_open_rate_per_second,
    )


def ready_check_admission_open_at(
    plan: ReadyCheckPreparationPlan,
    *,
    now: datetime,
) -> tuple[datetime, str]:
    now_utc = _utc(now)
    preparation_start = _utc(plan.preparation_starts_at)
    if now_utc >= preparation_start:
        return now_utc, READY_CHECK_LATE_ADMISSION_PRIORITY
    return preparation_start, READY_CHECK_SCHEDULED_ADMISSION_PRIORITY


def ready_check_user_admission(
    plan: ReadyCheckPreparationPlan,
    *,
    demand: ReadyCheckDemand,
    user_id: str,
    sse_quota: int,
    now: datetime,
) -> tuple[datetime, str, ReadyCheckAdmissionMode]:
    """Return a deterministic, fair opening slot for one eligible user.

    Users inside the finite planning quota are spread across the preparation
    window. Users outside it remain polling-only until the Ready Check start.
    A late SSE admission is still useful when a participant reaches the page
    after their scheduled slot but before T; after T the initial tournament
    state is authoritative and the client stays polling-only. This is a
    client schedule, not a correctness guard: Redis remains the final global
    lease decision.
    """

    now_utc = _utc(now)
    starts_at = _utc(demand.starts_at)
    preparation_start = _utc(plan.preparation_starts_at)
    if now_utc >= starts_at:
        return starts_at, READY_CHECK_POLLING_ADMISSION_PRIORITY, "polling"

    eligible_count = max(0, int(demand.eligible_count))
    quota = min(max(0, int(sse_quota)), eligible_count)
    if quota == 0 or eligible_count == 0:
        return starts_at, READY_CHECK_POLLING_ADMISSION_PRIORITY, "polling"

    rank_digest = hashlib.sha256(
        f"{demand.tournament_id}:{user_id}:{int(starts_at.timestamp())}".encode("utf-8")
    ).digest()
    stable_rank = int.from_bytes(rank_digest[:8], "big") % eligible_count
    if stable_rank >= quota:
        return starts_at, READY_CHECK_POLLING_ADMISSION_PRIORITY, "polling"

    preparation_seconds = max(0.0, (starts_at - preparation_start).total_seconds())
    rank_fraction = int.from_bytes(rank_digest[8:16], "big") / 2**64
    offset_seconds = preparation_seconds * (stable_rank + rank_fraction) / quota
    scheduled_at = preparation_start + timedelta(seconds=offset_seconds)
    if now_utc >= scheduled_at:
        return now_utc, READY_CHECK_LATE_ADMISSION_PRIORITY, "late_sse"
    return scheduled_at, READY_CHECK_SCHEDULED_ADMISSION_PRIORITY, "scheduled_sse"


def proportional_ready_check_capacity(
    demands: list[ReadyCheckDemand] | tuple[ReadyCheckDemand, ...],
    *,
    capacity: int,
) -> dict[str, int]:
    """Allocate a finite global pool proportionally with deterministic rounding.

    The result is a planning quota, not a correctness guard. The Redis global
    lease remains the final admission guard and late arrivals are never put in
    a server-side queue.
    """

    if capacity < 0:
        raise ValueError("Ready Check capacity cannot be negative.")
    normalized = {
        demand.tournament_id: max(0, int(demand.eligible_count))
        for demand in demands
        if demand.eligible_count > 0
    }
    total_demand = sum(normalized.values())
    target = min(capacity, total_demand)
    if not normalized or target == 0:
        return {tournament_id: 0 for tournament_id in normalized}

    allocations: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for tournament_id, demand in normalized.items():
        exact = target * demand / total_demand
        allocation = math.floor(exact)
        allocations[tournament_id] = allocation
        remainders.append((exact - allocation, tournament_id))
    for _, tournament_id in sorted(remainders, key=lambda item: (-item[0], item[1]))[: target - sum(allocations.values())]:
        allocations[tournament_id] += 1
    return allocations
