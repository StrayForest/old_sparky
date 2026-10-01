"""Explicit, test-only Celery probe tasks for liveness route evidence.

The production worker registry remains owned by
``worker_registry_manifest.py``.  This module adds three non-domain task
variants only to the ephemeral CI application so route resolution can be
tested without invoking a mutating production task.
"""

from __future__ import annotations

import os
from typing import Mapping

from celery import Celery
from kombu import Exchange, Queue

from apps.platform_worker.worker import celery_app as _platform_celery_app


PROBE_TASK_NAMES = {
    "high": "platform.ci_liveness_probe_high",
    "default": "platform.ci_liveness_probe_default",
    "low": "platform.ci_liveness_probe_low",
}
PROBE_ROUTE_PRIORITIES = {"high": 9, "default": 6, "low": 1}
PROBE_QUEUE_ENV_NAMES = {
    "high": "PLATFORM_CI_LIVENESS_QUEUE_HIGH",
    "default": "PLATFORM_CI_LIVENESS_QUEUE_DEFAULT",
    "low": "PLATFORM_CI_LIVENESS_QUEUE_LOW",
}
PROBE_RUN_ENV_NAME = "PLATFORM_CI_LIVENESS_RUN_ID"


def configure_probe_application(
    run_id: str,
    queue_names: Mapping[str, str],
) -> Celery:
    """Add safe probe tasks and route them through the run's physical queues."""

    if not run_id or set(queue_names) != set(PROBE_TASK_NAMES):
        raise ValueError("liveness probe configuration must contain all three semantics")
    if any(not queue_names[semantic] for semantic in PROBE_TASK_NAMES):
        raise ValueError("liveness probe queue names must be non-empty")

    exchange = Exchange(f"platform-ci-{run_id}", type="direct", durable=False)
    task_routes = dict(_platform_celery_app.conf.task_routes or {})
    task_queues = []
    for semantic in ("high", "default", "low"):
        queue_name = queue_names[semantic]
        task_routes[PROBE_TASK_NAMES[semantic]] = {
            "queue": queue_name,
            "priority": PROBE_ROUTE_PRIORITIES[semantic],
        }
        task_queues.append(
            Queue(
                queue_name,
                exchange=exchange,
                routing_key=queue_name,
                durable=False,
            )
        )

    _platform_celery_app.conf.update(
        task_always_eager=False,
        task_routes=task_routes,
        task_queues=tuple(task_queues),
        broker_transport_options={
            **dict(_platform_celery_app.conf.broker_transport_options or {}),
            # Kombu applies this prefix to queue, priority, binding, unacked
            # and pidbox keys.  It makes cleanup an exact per-run allowlist.
            "global_keyprefix": f"platform-ci-{run_id}-",
        },
    )

    for semantic, task_name in PROBE_TASK_NAMES.items():
        if task_name in _platform_celery_app.tasks:
            continue

        def _probe() -> str:
            return "pong"

        _platform_celery_app.task(name=task_name, ignore_result=False)(_probe)
    return _platform_celery_app


def _configure_from_environment() -> Celery:
    run_id = os.environ.get(PROBE_RUN_ENV_NAME)
    queue_names = {
        semantic: os.environ.get(environment_name, "")
        for semantic, environment_name in PROBE_QUEUE_ENV_NAMES.items()
    }
    if not run_id or not all(queue_names.values()):
        raise RuntimeError(
            "the liveness probe worker requires its explicit CI run and queue environment"
        )
    return configure_probe_application(run_id, queue_names)


if os.environ.get(PROBE_RUN_ENV_NAME):
    celery_app = _configure_from_environment()
else:
    # Parent-side tests call configure_probe_application after generating the
    # run ID.  Keeping this import-safe avoids inventing a queue or namespace.
    celery_app = _platform_celery_app


__all__ = [
    "PROBE_QUEUE_ENV_NAMES",
    "PROBE_ROUTE_PRIORITIES",
    "PROBE_RUN_ENV_NAME",
    "PROBE_TASK_NAMES",
    "celery_app",
    "configure_probe_application",
]
