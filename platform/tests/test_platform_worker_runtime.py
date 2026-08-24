from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from apps.platform_api.app.api.routes import tournaments as tournament_routes
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

    def test_background_work_is_routed_to_bounded_priority_queues(self) -> None:
        routes = worker.celery_app.conf.task_routes
        self.assertEqual(
            routes["platform.deadlock_automation_tick"]["queue"],
            worker.HIGH_PRIORITY_QUEUE,
        )
        self.assertEqual(
            routes["platform.player_commitment_reconciliation"]["queue"],
            worker.LOW_PRIORITY_QUEUE,
        )
        self.assertEqual(worker.celery_app.conf.worker_prefetch_multiplier, 1)
        self.assertTrue(worker.celery_app.conf.task_acks_late)

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

    def test_auto_assignment_task_has_the_expected_celery_contract(self) -> None:
        self.assertEqual(worker.deadlock_auto_assignment_run.name, "platform.deadlock_auto_assignment_run")
        self.assertFalse(worker.deadlock_auto_assignment_run.ignore_result)


class PlatformAutoAssignmentTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_persists_generated_run_and_releases_lock(self) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.released = False

            async def set(self, *args, **kwargs):
                return True

            async def eval(self, *args, **kwargs):
                self.released = True
                return 1

            async def aclose(self) -> None:
                return None

        fake_redis = FakeRedis()
        fake_session = SimpleNamespace(
            scalar=AsyncMock(return_value=SimpleNamespace(id="tournament-1")),
        )
        session_context = AsyncMock()
        session_context.__aenter__.return_value = fake_session
        session_context.__aexit__.return_value = False
        generated = SimpleNamespace(id="run-1")

        with (
            patch.object(worker, "redis_client", return_value=fake_redis),
            patch.object(worker, "session_factory", return_value=lambda: session_context),
            patch.object(
                worker,
                "generate_deadlock_auto_assignment_run_for_tournament",
                new=AsyncMock(return_value=generated),
            ) as generate_run,
        ):
            result = await worker._run_locked_deadlock_auto_assignment("tournament-1", "user-1")

        self.assertEqual(result, {
            "ok": True,
            "status": "generated",
            "tournament_id": "tournament-1",
            "run_id": "run-1",
        })
        generate_run.assert_awaited_once()
        self.assertTrue(fake_redis.released)

    async def test_worker_failure_is_reported_and_lock_is_still_released(self) -> None:
        class FakeRedis:
            async def set(self, *args, **kwargs):
                return True

            async def eval(self, *args, **kwargs):
                return 1

            async def aclose(self) -> None:
                return None

        fake_session = SimpleNamespace(
            scalar=AsyncMock(return_value=SimpleNamespace(id="tournament-1")),
        )
        session_context = AsyncMock()
        session_context.__aenter__.return_value = fake_session
        session_context.__aexit__.return_value = False

        with (
            patch.object(worker, "redis_client", return_value=FakeRedis()),
            patch.object(worker, "session_factory", return_value=lambda: session_context),
            patch.object(
                worker,
                "generate_deadlock_auto_assignment_run_for_tournament",
                new=AsyncMock(side_effect=HTTPException(status_code=409, detail="staging closed")),
            ),
        ):
            result = await worker._run_locked_deadlock_auto_assignment("tournament-1", "user-1")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["http_status"], 409)
        self.assertEqual(result["error"], "staging closed")


class PlatformAutoAssignmentRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_route_enqueues_the_registered_worker_with_expiry(self) -> None:
        tournament = SimpleNamespace(
            id="tournament-1",
            format_slug="solo",
            status="registration_closed",
            organizer_user_id="user-1",
            slug="test-tournament",
        )
        auth_session = SimpleNamespace(user=SimpleNamespace(id="user-1"))
        task = Mock(id="celery-task-1")

        with (
            patch.object(tournament_routes, "get_tournament_or_404", new=AsyncMock(return_value=tournament)),
            patch.object(tournament_routes, "ensure_deadlock_tournament_format"),
            patch.object(tournament_routes, "ensure_tournament_organizer"),
            patch.object(tournament_routes, "tournament_has_locked_deadlock_roster", new=AsyncMock(return_value=False)),
            patch.object(tournament_routes, "ensure_deadlock_roster_staging_allowed"),
            patch("apps.platform_worker.worker.deadlock_auto_assignment_run") as celery_task,
        ):
            celery_task.apply_async.return_value = task
            response = await tournament_routes.queue_deadlock_auto_assignment(
                "test-tournament",
                auth_session=auth_session,
                db_session=SimpleNamespace(),
            )

        self.assertEqual(response.task_id, "celery-task-1")
        celery_task.apply_async.assert_called_once_with(
            args=["tournament-1", "user-1"],
            expires=900,
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
