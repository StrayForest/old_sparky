"""Independent, literal Celery worker ownership contract for platform tests.

This module intentionally does not import the application worker.  The
expected task, queue and beat ownership below is a reviewed contract, not a
set derived from ``apps.platform_worker.worker``.  Changes to the worker
registry must update this manifest and its contract tests together.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkerRouteContract:
    """One explicit task route owned by the platform worker."""

    queue: str
    priority: int


@dataclass(frozen=True, slots=True)
class WorkerTaskContract:
    """The stable contract for one application task."""

    name: str
    effective_queue: str
    effective_priority: int
    ignore_result: bool
    route: WorkerRouteContract | None = None
    soft_time_limit: int | None = None
    time_limit: int | None = None


@dataclass(frozen=True, slots=True)
class BeatEntryContract:
    """One periodic task and its bounded expiry."""

    name: str
    task: str
    schedule_seconds: float
    expires_seconds: float


# These names are deliberately repeated instead of imported from the worker.
# They are the queues that the production worker must consume, and the
# integration test uses the same safe queues in the isolated Redis DB13.
HIGH_PRIORITY_QUEUE = "deadlock-platform-high"
DEFAULT_PRIORITY_QUEUE = "deadlock-platform-default"
LOW_PRIORITY_QUEUE = "deadlock-platform-low"
REQUIRED_WORKER_QUEUES = (
    HIGH_PRIORITY_QUEUE,
    DEFAULT_PRIORITY_QUEUE,
    LOW_PRIORITY_QUEUE,
)

# The application owns exactly these nine platform tasks at this checkpoint.
# Celery's built-in ``celery.*`` tasks are outside this application-owned set.
EXPECTED_TASKS = (
    WorkerTaskContract(
        name="platform.ping",
        effective_queue=DEFAULT_PRIORITY_QUEUE,
        effective_priority=5,
        ignore_result=False,
    ),
    WorkerTaskContract(
        name="platform.deadlock_automation_tick",
        effective_queue=HIGH_PRIORITY_QUEUE,
        effective_priority=9,
        ignore_result=True,
        route=WorkerRouteContract(HIGH_PRIORITY_QUEUE, 9),
    ),
    WorkerTaskContract(
        name="platform.deadlock_auto_assignment_run",
        effective_queue=DEFAULT_PRIORITY_QUEUE,
        effective_priority=6,
        ignore_result=False,
        route=WorkerRouteContract(DEFAULT_PRIORITY_QUEUE, 6),
    ),
    WorkerTaskContract(
        name="platform.player_commitment_reconciliation",
        effective_queue=LOW_PRIORITY_QUEUE,
        effective_priority=2,
        ignore_result=True,
        route=WorkerRouteContract(LOW_PRIORITY_QUEUE, 2),
    ),
    WorkerTaskContract(
        name="platform.home_content_refresh",
        effective_queue=LOW_PRIORITY_QUEUE,
        effective_priority=1,
        ignore_result=True,
        route=WorkerRouteContract(LOW_PRIORITY_QUEUE, 1),
    ),
    WorkerTaskContract(
        name="platform.patch_translation",
        effective_queue=LOW_PRIORITY_QUEUE,
        effective_priority=1,
        ignore_result=True,
        route=WorkerRouteContract(LOW_PRIORITY_QUEUE, 1),
        soft_time_limit=180,
        time_limit=210,
    ),
    WorkerTaskContract(
        name="platform.auth_lifecycle_cleanup",
        effective_queue=LOW_PRIORITY_QUEUE,
        effective_priority=1,
        ignore_result=True,
        route=WorkerRouteContract(LOW_PRIORITY_QUEUE, 1),
    ),
    WorkerTaskContract(
        name="platform.media_reconciliation",
        effective_queue=LOW_PRIORITY_QUEUE,
        effective_priority=1,
        ignore_result=True,
        route=WorkerRouteContract(LOW_PRIORITY_QUEUE, 1),
    ),
    WorkerTaskContract(
        name="platform.media_process_asset",
        effective_queue=LOW_PRIORITY_QUEUE,
        effective_priority=1,
        ignore_result=True,
        route=WorkerRouteContract(LOW_PRIORITY_QUEUE, 1),
        soft_time_limit=90,
        time_limit=120,
    ),
)

EXPECTED_BEAT_ENTRIES = (
    BeatEntryContract(
        name="deadlock-automation-tick",
        task="platform.deadlock_automation_tick",
        schedule_seconds=60.0,
        expires_seconds=60.0,
    ),
    BeatEntryContract(
        name="player-commitment-reconciliation",
        task="platform.player_commitment_reconciliation",
        schedule_seconds=900.0,
        expires_seconds=900.0,
    ),
    BeatEntryContract(
        name="home-content-refresh",
        task="platform.home_content_refresh",
        schedule_seconds=1800.0,
        expires_seconds=1800.0,
    ),
    BeatEntryContract(
        name="auth-lifecycle-cleanup",
        task="platform.auth_lifecycle_cleanup",
        schedule_seconds=3600.0,
        expires_seconds=3600.0,
    ),
    BeatEntryContract(
        name="media-reconciliation",
        task="platform.media_reconciliation",
        schedule_seconds=60.0,
        expires_seconds=60.0,
    ),
)

# The current cadence bounds are intentionally conservative.  They keep a
# missed/stale periodic message from remaining executable for more than one
# cadence while allowing the hourly cleanup entry.
MIN_BEAT_SECONDS = 1.0
MAX_BEAT_SECONDS = 24 * 60 * 60.0

# The launcher is checked as a source contract so a future CLI change cannot
# silently leave a queue unconsumed.  The integration test separately starts
# a one-shot worker without beat or a schedule file.
WORKER_LAUNCHER_QUEUE_ARGUMENT = ",".join(REQUIRED_WORKER_QUEUES)


__all__ = [
    "BeatEntryContract",
    "DEFAULT_PRIORITY_QUEUE",
    "EXPECTED_BEAT_ENTRIES",
    "EXPECTED_TASKS",
    "HIGH_PRIORITY_QUEUE",
    "LOW_PRIORITY_QUEUE",
    "MAX_BEAT_SECONDS",
    "MIN_BEAT_SECONDS",
    "REQUIRED_WORKER_QUEUES",
    "WORKER_LAUNCHER_QUEUE_ARGUMENT",
    "WorkerRouteContract",
    "WorkerTaskContract",
]
