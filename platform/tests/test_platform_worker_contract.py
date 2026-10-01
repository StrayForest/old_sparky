from __future__ import annotations

import math
from pathlib import Path
import re
import unittest

from apps.platform_worker import worker
from tests.worker_registry_manifest import (
    EXPECTED_BEAT_ENTRIES,
    EXPECTED_TASKS,
    MAX_BEAT_SECONDS,
    MIN_BEAT_SECONDS,
    REQUIRED_WORKER_QUEUES,
    WORKER_LAUNCHER_QUEUE_ARGUMENT,
)
from tools.platform_test_runner import (
    TestResourceConfigurationError,
    validate_test_celery_resource_configuration,
)


PLATFORM_ROOT = Path(__file__).resolve().parents[1]


class PlatformWorkerRegistryContractTests(unittest.TestCase):
    """Compare the live Celery registry with the independent ownership manifest."""

    def test_exact_application_task_registry_is_registered(self) -> None:
        expected_names = {task.name for task in EXPECTED_TASKS}
        registered_names = {
            name for name in worker.celery_app.tasks if name.startswith("platform.")
        }

        self.assertEqual(registered_names, expected_names)
        self.assertEqual(len(registered_names), 9)

    def test_routes_are_exhaustive_and_use_known_queues(self) -> None:
        expected_routes = {
            task.name: task.route
            for task in EXPECTED_TASKS
            if task.route is not None
        }
        routes = worker.celery_app.conf.task_routes

        self.assertEqual(set(routes), set(expected_routes))
        known_tasks = {task.name for task in EXPECTED_TASKS}
        known_queues = set(REQUIRED_WORKER_QUEUES)
        for task_name, route in routes.items():
            self.assertIn(task_name, known_tasks)
            self.assertIsInstance(route, dict)
            self.assertEqual(set(route), {"queue", "priority"})
            self.assertIn(route["queue"], known_queues)
            self.assertIsInstance(route["priority"], int)
            self.assertNotIsInstance(route["priority"], bool)
            expected = expected_routes[task_name]
            assert expected is not None
            self.assertEqual(route["queue"], expected.queue)
            self.assertEqual(route["priority"], expected.priority)

    def test_each_task_has_the_manifest_effective_contract(self) -> None:
        self.assertEqual(
            worker.celery_app.conf.task_default_queue,
            "deadlock-platform-default",
        )
        self.assertEqual(worker.celery_app.conf.task_default_priority, 5)

        routes = worker.celery_app.conf.task_routes
        for expected in EXPECTED_TASKS:
            task = worker.celery_app.tasks[expected.name]
            self.assertEqual(task.name, expected.name)
            self.assertEqual(task.ignore_result, expected.ignore_result)
            self.assertEqual(task.soft_time_limit, expected.soft_time_limit)
            self.assertEqual(task.time_limit, expected.time_limit)
            if expected.route is None:
                self.assertNotIn(expected.name, routes)
                self.assertEqual(
                    worker.celery_app.conf.task_default_queue,
                    expected.effective_queue,
                )
                self.assertEqual(
                    worker.celery_app.conf.task_default_priority,
                    expected.effective_priority,
                )
            else:
                self.assertEqual(routes[expected.name]["queue"], expected.effective_queue)
                self.assertEqual(routes[expected.name]["priority"], expected.effective_priority)

    def test_worker_queues_and_launcher_are_exact(self) -> None:
        queues = tuple(queue.name for queue in worker.celery_app.conf.task_queues)
        self.assertEqual(queues, REQUIRED_WORKER_QUEUES)
        self.assertEqual(len(set(queues)), 3)
        for queue in worker.celery_app.conf.task_queues:
            self.assertEqual(queue.routing_key, queue.name)

        launcher = (PLATFORM_ROOT / "tools/platform_run_worker.sh").read_text(
            encoding="utf-8"
        )
        queue_arguments = re.findall(r"--queues\s+([^\s\\]+)", launcher)
        self.assertEqual(queue_arguments, [WORKER_LAUNCHER_QUEUE_ARGUMENT])
        self.assertEqual(launcher.count("--queues"), 1)

    def test_beat_schedule_is_registered_bounded_and_sane(self) -> None:
        expected_entries = {entry.name: entry for entry in EXPECTED_BEAT_ENTRIES}
        schedule = worker.celery_app.conf.beat_schedule

        self.assertEqual(set(schedule), set(expected_entries))
        registered_names = {task.name for task in EXPECTED_TASKS}
        for name, expected in expected_entries.items():
            entry = schedule[name]
            self.assertEqual(set(entry), {"task", "schedule", "options"})
            self.assertIn(entry["task"], registered_names)
            self.assertEqual(entry["task"], expected.task)
            self.assertEqual(set(entry["options"]), {"expires"})

            cadence = entry["schedule"]
            expires = entry["options"]["expires"]
            for value in (cadence, expires):
                self.assertIsInstance(value, (int, float))
                self.assertNotIsInstance(value, bool)
                self.assertTrue(math.isfinite(float(value)))
                self.assertGreaterEqual(float(value), MIN_BEAT_SECONDS)
                self.assertLessEqual(float(value), MAX_BEAT_SECONDS)
            self.assertLessEqual(float(expires), float(cadence))
            self.assertEqual(float(cadence), expected.schedule_seconds)
            self.assertEqual(float(expires), expected.expires_seconds)

    def test_priority_and_ack_contract_remains_bounded(self) -> None:
        self.assertEqual(worker.celery_app.conf.task_queue_max_priority, 10)
        self.assertEqual(worker.celery_app.conf.worker_prefetch_multiplier, 1)
        self.assertTrue(worker.celery_app.conf.task_acks_late)
        self.assertTrue(worker.celery_app.conf.task_reject_on_worker_lost)


class PlatformWorkerTestResourceContractTests(unittest.TestCase):
    _VALID_SETTINGS = {
        "platform_environment": "test",
        "platform_db_schema": "platform",
        "platform_database_url": (
            "postgresql+asyncpg://platform_user:platform_password@127.0.0.1:5432/"
            "platformdb_test"
        ),
        "platform_redis_url": "redis://127.0.0.1:6379/15",
        "platform_celery_broker_url": "redis://127.0.0.1:6379/13",
        "platform_celery_result_backend": "redis://127.0.0.1:6379/14",
    }

    def test_test_runtime_uses_distinct_app_broker_and_result_namespaces(self) -> None:
        configuration = validate_test_celery_resource_configuration(self._VALID_SETTINGS)

        self.assertEqual(configuration.app_redis_database, "15")
        self.assertEqual(configuration.broker_database, "13")
        self.assertEqual(configuration.result_backend_database, "14")
        self.assertEqual(
            {
                configuration.app_redis_database,
                configuration.broker_database,
                configuration.result_backend_database,
            },
            {"13", "14", "15"},
        )

    def test_test_runtime_rejects_non_loopback_celery_targets(self) -> None:
        unsafe = dict(self._VALID_SETTINGS)
        unsafe["platform_celery_broker_url"] = "redis://redis.example.test:6379/13"

        with self.assertRaises(TestResourceConfigurationError):
            validate_test_celery_resource_configuration(unsafe)

    def test_test_runtime_rejects_wrong_celery_database_namespace(self) -> None:
        unsafe = dict(self._VALID_SETTINGS)
        unsafe["platform_celery_result_backend"] = "redis://127.0.0.1:6379/15"

        with self.assertRaises(TestResourceConfigurationError):
            validate_test_celery_resource_configuration(unsafe)


if __name__ == "__main__":
    unittest.main()
