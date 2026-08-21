from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_worker import worker


class PlatformWorkerRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        worker._close_worker_loop()

    def test_worker_reuses_one_event_loop_across_ticks(self) -> None:
        seen_loops: list[asyncio.AbstractEventLoop] = []

        async def capture_loop(value: int) -> int:
            seen_loops.append(asyncio.get_running_loop())
            return value

        self.assertEqual(worker._run_on_worker_loop(capture_loop(1)), 1)
        self.assertEqual(worker._run_on_worker_loop(capture_loop(2)), 2)

        self.assertEqual(len(seen_loops), 2)
        self.assertIs(seen_loops[0], seen_loops[1])

    def test_automation_task_does_not_store_unused_results(self) -> None:
        self.assertTrue(worker.deadlock_automation_tick.ignore_result)

    def test_automation_beat_entry_expires_at_the_next_cadence(self) -> None:
        self.assertEqual(
            worker.celery_app.conf.beat_schedule["deadlock-automation-tick"],
            {
                "task": "platform.deadlock_automation_tick",
                "schedule": 60.0,
                "options": {
                    "expires": 60.0,
                },
            },
        )
        self.assertEqual(
            worker.AUTOMATION_TICK_EXPIRES_SECONDS,
            worker.AUTOMATION_TICK_INTERVAL_SECONDS,
        )
        self.assertEqual(
            worker.deadlock_automation_tick.name,
            "platform.deadlock_automation_tick",
        )

    def test_commitment_reconciliation_has_bounded_singleton_schedule(self) -> None:
        self.assertEqual(
            worker.celery_app.conf.beat_schedule["player-commitment-reconciliation"],
            {
                "task": "platform.player_commitment_reconciliation",
                "schedule": 900.0,
                "options": {"expires": 900.0},
            },
        )
        self.assertTrue(worker.player_commitment_reconciliation.ignore_result)
        self.assertEqual(
            worker.player_commitment_reconciliation.name,
            "platform.player_commitment_reconciliation",
        )

    def test_home_content_refresh_is_periodic_and_does_not_store_results(self) -> None:
        self.assertEqual(
            worker.celery_app.conf.beat_schedule["home-content-refresh"],
            {
                "task": "platform.home_content_refresh",
                "schedule": 1800.0,
                "options": {"expires": 1800.0},
            },
        )
        self.assertTrue(worker.home_content_refresh.ignore_result)
        self.assertEqual(
            worker.home_content_refresh.name,
            "platform.home_content_refresh",
        )

    def test_auth_lifecycle_cleanup_is_hourly_and_does_not_store_results(self) -> None:
        self.assertEqual(
            worker.celery_app.conf.beat_schedule["auth-lifecycle-cleanup"],
            {
                "task": "platform.auth_lifecycle_cleanup",
                "schedule": 3600.0,
                "options": {"expires": 3600.0},
            },
        )
        self.assertTrue(worker.auth_lifecycle_cleanup.ignore_result)
        self.assertEqual(
            worker.auth_lifecycle_cleanup.name,
            "platform.auth_lifecycle_cleanup",
        )


class PlatformWorkerAutomationLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_runs_and_releases_owned_lock(self) -> None:
        client = Mock()
        client.set = AsyncMock(return_value=True)
        client.eval = AsyncMock(return_value=1)
        client.aclose = AsyncMock()
        expected = {
            "scanned": 1,
            "deferred": 0,
            "registration_opened": 0,
            "registration_closed": 0,
            "ready_started": 0,
            "ready_closed": 0,
            "captain_started": 0,
            "captain_offers_expired": 0,
            "captain_finalized": 0,
            "assignment_generated": 0,
            "errors": 0,
        }

        with (
            patch.object(worker, "redis_client", return_value=client),
            patch.object(worker, "token_urlsafe", return_value="owner-token"),
            patch.object(
                worker,
                "run_deadlock_automation_once",
                AsyncMock(return_value=expected),
            ) as run_once,
        ):
            result = await worker._run_locked_deadlock_automation_once()

        self.assertEqual(result, expected)
        run_once.assert_awaited_once_with()
        client.set.assert_awaited_once_with(
            worker.AUTOMATION_LOCK_KEY,
            "owner-token",
            nx=True,
            ex=worker.AUTOMATION_LOCK_TTL_SECONDS,
        )
        client.eval.assert_awaited_once_with(
            worker.AUTOMATION_LOCK_RELEASE_SCRIPT,
            1,
            worker.AUTOMATION_LOCK_KEY,
            "owner-token",
        )
        client.aclose.assert_awaited_once()

    async def test_tick_skips_when_lock_is_already_held(self) -> None:
        client = Mock()
        client.set = AsyncMock(return_value=False)
        client.eval = AsyncMock()
        client.aclose = AsyncMock()

        with (
            patch.object(worker, "redis_client", return_value=client),
            patch.object(worker, "token_urlsafe", return_value="blocked-token"),
            patch.object(worker.logger, "warning") as warning,
            patch.object(
                worker,
                "run_deadlock_automation_once",
                AsyncMock(),
            ) as run_once,
        ):
            result = await worker._run_locked_deadlock_automation_once()

        self.assertEqual(result, worker.DeadlockAutomationResult().as_dict())
        run_once.assert_not_awaited()
        client.set.assert_awaited_once_with(
            worker.AUTOMATION_LOCK_KEY,
            "blocked-token",
            nx=True,
            ex=worker.AUTOMATION_LOCK_TTL_SECONDS,
        )
        client.eval.assert_not_awaited()
        client.aclose.assert_awaited_once()
        warning.assert_called_once_with(
            "Skipping Deadlock automation tick because another tick holds the lock."
        )

    async def test_commitment_reconciliation_skips_when_lock_is_held(self) -> None:
        client = Mock()
        client.set = AsyncMock(return_value=False)
        client.eval = AsyncMock()
        client.aclose = AsyncMock()

        with (
            patch.object(worker, "redis_client", return_value=client),
            patch.object(worker, "token_urlsafe", return_value="blocked-token"),
            patch.object(worker, "reconcile_player_commitments", AsyncMock()) as reconcile,
        ):
            result = await worker._run_locked_player_commitment_reconciliation()

        self.assertEqual(result, {"ok": True, "skipped": True, "released_total": 0})
        reconcile.assert_not_awaited()
        client.eval.assert_not_awaited()
        client.aclose.assert_awaited_once()
