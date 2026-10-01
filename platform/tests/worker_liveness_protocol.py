"""Non-Celery wire constants shared by the parent watchdog and child probe."""

from __future__ import annotations


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
PROBE_BROKER_ENV_NAME = "PLATFORM_CI_LIVENESS_BROKER_URL"
PROBE_RESULT_ENV_NAME = "PLATFORM_CI_LIVENESS_RESULT_BACKEND"
PROBE_DEADLINE_ENV_NAME = "PLATFORM_CI_LIVENESS_DEADLINE"
PROBE_SUPERVISOR_FLAG = "--supervise"


__all__ = [
    "PROBE_BROKER_ENV_NAME",
    "PROBE_DEADLINE_ENV_NAME",
    "PROBE_QUEUE_ENV_NAMES",
    "PROBE_RESULT_ENV_NAME",
    "PROBE_ROUTE_PRIORITIES",
    "PROBE_RUN_ENV_NAME",
    "PROBE_SUPERVISOR_FLAG",
    "PROBE_TASK_NAMES",
]
