from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Request

from apps.platform_api.app.api.routes import tournaments
from apps.platform_api.app.api.schemas import TournamentDeadlockReadyVoteRequest
from python_packages.platform_infra.ready_vote_admission import (
    READY_VOTE_ADMISSION_SEVERE,
    ReadyVoteAdmissionConfig,
    ReadyVoteAdmissionController,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class ReadyVoteAdmissionControllerTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.controllers: list[ReadyVoteAdmissionController] = []

    async def asyncTearDown(self) -> None:
        for controller in self.controllers:
            await controller.stop()

    def controller(self, **overrides: object) -> ReadyVoteAdmissionController:
        values = {
            "cpu_sample_interval_seconds": 5.0,
            "control_interval_seconds": 0.0,
            "recovery_samples": 2,
        }
        values.update(overrides)
        controller = ReadyVoteAdmissionController(ReadyVoteAdmissionConfig(**values))
        self.controllers.append(controller)
        return controller

    async def test_healthy_request_is_admitted_without_wait_or_shed(self) -> None:
        controller = self.controller()

        lease = await controller.acquire()

        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertLess(lease.wait_ms, 5.0)
        self.assertEqual(lease.snapshot.inflight, 1)
        self.assertEqual(lease.snapshot.limit, 8)
        self.assertEqual(controller.snapshot().shed_total, 0)
        await lease.release(service_ms=25.0, pool_wait_ms=0.0)
        self.assertEqual(controller.snapshot().inflight, 0)

    async def test_excess_is_shed_before_any_waiter_or_database_work(self) -> None:
        controller = self.controller(min_concurrency=1, initial_concurrency=1, max_concurrency=1)
        first = await controller.acquire()
        self.assertIsNotNone(first)

        second = await controller.acquire()

        self.assertIsNone(second)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.shed_total, 1)
        self.assertEqual(snapshot.waiters, 0)
        assert first is not None
        await first.release(service_ms=20.0, pool_wait_ms=0.0)

    async def test_saturated_inflight_signal_reduces_adaptive_limit(self) -> None:
        controller = self.controller(
            min_concurrency=4,
            initial_concurrency=8,
            max_concurrency=12,
        )
        leases = [await controller.acquire() for _ in range(8)]

        self.assertIsNone(await controller.acquire())
        self.assertEqual(controller.snapshot().limit, 7)

        for lease in leases:
            assert lease is not None
            await lease.release(service_ms=20.0, pool_wait_ms=0.0)

    async def test_waiter_budget_is_bounded_and_deadline_is_short(self) -> None:
        controller = self.controller(
            min_concurrency=1,
            initial_concurrency=1,
            max_concurrency=1,
            max_waiters=1,
            wait_timeout_ms=50.0,
        )
        first = await controller.acquire()
        self.assertIsNotNone(first)
        waiter = asyncio.create_task(controller.acquire())
        await asyncio.sleep(0)
        self.assertEqual(controller.snapshot().waiters, 1)

        shed = await controller.acquire()
        self.assertIsNone(shed)
        self.assertEqual(controller.snapshot().shed_total, 1)

        assert first is not None
        await first.release(service_ms=20.0, pool_wait_ms=0.0)
        admitted = await asyncio.wait_for(waiter, timeout=0.2)
        self.assertIsNotNone(admitted)
        assert admitted is not None
        self.assertLessEqual(admitted.wait_ms, 50.0)
        await admitted.release(service_ms=20.0, pool_wait_ms=0.0)

    async def test_sustained_cpu_pressure_reduces_and_health_recovers_limit_slowly(self) -> None:
        controller = self.controller(
            min_concurrency=4,
            initial_concurrency=8,
            max_concurrency=16,
            cpu_ewma_alpha=1.0,
        )

        await controller.observe_cpu_percent(95.0)
        pressured = controller.snapshot()
        self.assertEqual(pressured.state, READY_VOTE_ADMISSION_SEVERE)
        self.assertEqual(pressured.limit, 7)
        await controller.observe_cpu_percent(95.0)
        self.assertEqual(controller.snapshot().limit, 5)

        for _ in range(5):
            await controller.observe_cpu_percent(20.0)
        recovered = controller.snapshot()
        self.assertEqual(recovered.state, "normal")
        self.assertGreater(recovered.limit, 5)
        self.assertLess(recovered.limit, 16)


class ReadyVoteAdmissionRouteTests(PlatformIsolatedAsyncioTestCase):
    async def test_overload_response_does_not_enter_ready_vote_db_scope(self) -> None:
        snapshot = SimpleNamespace(
            state="pressure",
            inflight=4,
            limit=4,
            waiters=0,
            cpu_pressure=82.0,
            admitted_total=4,
            shed_total=1,
            limit_changes=1,
        )
        controller = SimpleNamespace(
            acquire=AsyncMock(return_value=None),
            snapshot=lambda: snapshot,
        )
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/v1/tournaments/demo/deadlock/ready-check/vote",
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "scheme": "http",
            }
        )

        with (
            patch.object(tournaments, "get_ready_vote_admission_controller", return_value=controller),
            patch.object(tournaments, "ready_vote_db_session") as db_scope,
        ):
            response = await tournaments.vote_deadlock_ready_check(
                "demo",
                TournamentDeadlockReadyVoteRequest(choice="yes"),
                request,
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["retry-after"], "1")
        self.assertEqual(
            json.loads(response.body),
            {
                "code": "READY_VOTE_OVERLOADED",
                "retryable": True,
                "retry_after_ms": 250,
            },
        )
        db_scope.assert_not_called()


if __name__ == "__main__":
    unittest.main()
