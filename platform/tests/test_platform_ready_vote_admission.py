from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Request

from apps.platform_api.app.api.routes import tournaments
from apps.platform_api.app.api.schemas import TournamentDeadlockReadyVoteRequest
from python_packages.platform_infra import ready_vote_admission
from python_packages.platform_infra.ready_vote_admission import (
    READY_VOTE_ADMISSION_SEVERE,
    ReadyVoteAdmissionConfig,
    ReadyVoteAdmissionController,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


_ADMISSION_OBSERVER_TIMEOUT_SECONDS = 0.5


async def _cancel_and_reap(task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait_for(
        asyncio.gather(task, return_exceptions=True),
        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
    )


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
        waiter: asyncio.Task | None = None
        first_held = None
        admitted_held = None
        clock = [100.0]

        async def controlled_wait_for(awaitable, timeout: float) -> None:
            self.assertAlmostEqual(timeout, 0.05)
            await awaitable

        try:
            # Control the controller clock and its private wait seam only;
            # orchestration barriers below retain real bounded asyncio waits.
            with (
                patch.object(
                    ready_vote_admission,
                    "perf_counter",
                    side_effect=lambda: clock[0],
                ),
                patch.object(
                    ready_vote_admission,
                    "_wait_for",
                    new=controlled_wait_for,
                ),
            ):
                first_held = await controller.acquire()
                self.assertIsNotNone(first_held)
                wait_started = asyncio.Event()
                original_wait = controller._capacity_event.wait

                async def observed_wait() -> None:
                    wait_started.set()
                    await original_wait()

                with patch.object(controller._capacity_event, "wait", new=observed_wait):
                    waiter = asyncio.create_task(controller.acquire())
                    await asyncio.wait_for(
                        wait_started.wait(),
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                    self.assertEqual(controller.snapshot().waiters, 1)

                    shed = await controller.acquire()
                    self.assertIsNone(shed)
                    self.assertEqual(controller.snapshot().shed_total, 1)

                    clock[0] += 0.025
                    await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
                    first_held = None
                    admitted = await asyncio.wait_for(
                        asyncio.shield(waiter),
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                    waiter = None
                    self.assertIsNotNone(admitted)
                    assert admitted is not None
                    admitted_held = admitted
                    self.assertLessEqual(admitted.wait_ms, 50.0)
                    await admitted_held.release(service_ms=20.0, pool_wait_ms=0.0)
                    admitted_held = None
                snapshot = controller.snapshot()
                self.assertEqual(snapshot.inflight, 0)
                self.assertEqual(snapshot.waiters, 0)
                self.assertEqual(snapshot.shed_total, 1)
        finally:
            if waiter is not None:
                await _cancel_and_reap(waiter)
            if admitted_held is not None and not admitted_held.released:
                await admitted_held.release(service_ms=20.0, pool_wait_ms=0.0)
            if first_held is not None and not first_held.released:
                await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
            self.assertEqual(controller.snapshot().inflight, 0)
            self.assertEqual(controller.snapshot().waiters, 0)

    async def test_capacity_wake_after_wait_deadline_is_shed(self) -> None:
        controller = self.controller(
            min_concurrency=1,
            initial_concurrency=1,
            max_concurrency=1,
            max_waiters=1,
            wait_timeout_ms=50.0,
        )
        waiter: asyncio.Task | None = None
        first_held = None
        clock = [100.0]

        async def controlled_wait_for(awaitable, timeout: float) -> None:
            self.assertAlmostEqual(timeout, 0.05)
            await awaitable

        async def observed_wait() -> None:
            wait_started.set()
            await original_wait()
            wait_returned.set()

        try:
            with (
                patch.object(
                    ready_vote_admission,
                    "perf_counter",
                    side_effect=lambda: clock[0],
                ),
                patch.object(
                    ready_vote_admission,
                    "_wait_for",
                    new=controlled_wait_for,
                ),
            ):
                first_held = await controller.acquire()
                self.assertIsNotNone(first_held)
                wait_started = asyncio.Event()
                wait_returned = asyncio.Event()
                original_wait = controller._capacity_event.wait
                with patch.object(
                    controller._capacity_event,
                    "wait",
                    new=observed_wait,
                ):
                    waiter = asyncio.create_task(controller.acquire())
                    await asyncio.wait_for(
                        wait_started.wait(),
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                    self.assertEqual(controller.snapshot().waiters, 1)

                    # The capacity wake wins, but the controller clock has
                    # crossed its deadline before the waiter can take the slot.
                    clock[0] += 0.051
                    await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
                    first_held = None
                    expired = await asyncio.wait_for(
                        asyncio.shield(waiter),
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                    waiter = None

                self.assertIsNone(expired)
                snapshot = controller.snapshot()
                self.assertEqual(snapshot.inflight, 0)
                self.assertEqual(snapshot.waiters, 0)
                self.assertEqual(snapshot.shed_total, 1)
        finally:
            if waiter is not None:
                await _cancel_and_reap(waiter)
            if first_held is not None and not first_held.released:
                await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
            self.assertEqual(controller.snapshot().inflight, 0)
            self.assertEqual(controller.snapshot().waiters, 0)

    async def test_cancel_after_capacity_wake_unregisters_before_lock_reacquire(self) -> None:
        controller = self.controller(
            min_concurrency=1,
            initial_concurrency=1,
            max_concurrency=1,
            max_waiters=1,
            wait_timeout_ms=500.0,
        )
        waiter: asyncio.Task | None = None
        first_held = None
        lock_held = False
        wait_started = asyncio.Event()
        wait_returned = asyncio.Event()
        reacquire_started = asyncio.Event()

        class ReacquireObservedLock(asyncio.Lock):
            observe_reacquire = False

            async def acquire(self) -> bool:
                if self.observe_reacquire:
                    reacquire_started.set()
                return await super().acquire()

        controller._ensure_event_loop()
        controlled_lock = ReacquireObservedLock()
        controller._state_lock = controlled_lock
        try:
            first_held = await controller.acquire()
            self.assertIsNotNone(first_held)
            original_wait = controller._capacity_event.wait

            async def observed_wait() -> None:
                wait_started.set()
                await original_wait()
                wait_returned.set()

            with (
                patch.object(controller._capacity_event, "wait", new=observed_wait),
                patch.object(
                    ready_vote_admission,
                    "record_ready_vote_admission_start",
                ) as started,
            ):
                waiter = asyncio.create_task(controller.acquire())
                await asyncio.wait_for(
                    wait_started.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                self.assertEqual(controller.snapshot().waiters, 1)

                await controlled_lock.acquire()
                lock_held = True
                controlled_lock.observe_reacquire = True
                controller._capacity_event.set()
                await asyncio.wait_for(
                    wait_returned.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                await asyncio.wait_for(
                    reacquire_started.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                waiter.cancel()
                controlled_lock.release()
                lock_held = False
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(
                        waiter,
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                waiter = None

                snapshot = controller.snapshot()
                self.assertEqual(snapshot.waiters, 0)
                self.assertEqual(snapshot.inflight, 1)
                self.assertEqual(snapshot.shed_total, 0)
                self.assertFalse(first_held.released)
                self.assertFalse(controller._capacity_event.is_set())
                started.assert_not_called()
        finally:
            if lock_held:
                controlled_lock.release()
            if waiter is not None:
                await _cancel_and_reap(waiter)
            if first_held is not None and not first_held.released:
                await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
            self.assertEqual(controller.snapshot().inflight, 0)
            self.assertEqual(controller.snapshot().waiters, 0)

    async def test_cancel_during_timeout_cleanup_unregisters_without_shedding(self) -> None:
        controller = self.controller(
            min_concurrency=1,
            initial_concurrency=1,
            max_concurrency=1,
            max_waiters=1,
            wait_timeout_ms=50.0,
        )
        waiter: asyncio.Task | None = None
        first_held = None
        lock_held = False
        wait_started = asyncio.Event()
        fixture_ready = asyncio.Event()
        cleanup_reacquire_started = asyncio.Event()

        class CleanupObservedLock(asyncio.Lock):
            observe_reacquire = False

            async def acquire(self) -> bool:
                if self.observe_reacquire:
                    cleanup_reacquire_started.set()
                return await super().acquire()

        controller._ensure_event_loop()
        controlled_lock = CleanupObservedLock()
        controller._state_lock = controlled_lock
        try:
            first_held = await controller.acquire()
            self.assertIsNotNone(first_held)
            original_wait = controller._capacity_event.wait

            async def observed_wait() -> None:
                wait_started.set()
                await original_wait()

            async def timeout_after_fixture_ready(awaitable, timeout: float) -> None:
                self.assertGreater(timeout, 0.0)
                self.assertLessEqual(timeout, 0.05)
                event_waiter = asyncio.ensure_future(awaitable)
                try:
                    await asyncio.wait_for(
                        fixture_ready.wait(),
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                finally:
                    if not event_waiter.done():
                        event_waiter.cancel()
                    await asyncio.wait_for(
                        asyncio.gather(event_waiter, return_exceptions=True),
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                raise asyncio.TimeoutError

            with (
                patch.object(controller._capacity_event, "wait", new=observed_wait),
                patch.object(
                    ready_vote_admission,
                    "_wait_for",
                    new=timeout_after_fixture_ready,
                ),
                patch.object(
                    ready_vote_admission,
                    "record_ready_vote_admission_start",
                ) as started,
            ):
                waiter = asyncio.create_task(controller.acquire())
                await asyncio.wait_for(
                    wait_started.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                self.assertEqual(controller.snapshot().waiters, 1)

                await controlled_lock.acquire()
                lock_held = True
                controlled_lock.observe_reacquire = True
                fixture_ready.set()
                await asyncio.wait_for(
                    cleanup_reacquire_started.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                waiter.cancel()
                controlled_lock.release()
                lock_held = False
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(
                        waiter,
                        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                    )
                waiter = None

                snapshot = controller.snapshot()
                self.assertEqual(snapshot.waiters, 0)
                self.assertEqual(snapshot.inflight, 1)
                self.assertEqual(snapshot.shed_total, 0)
                self.assertFalse(first_held.released)
                self.assertFalse(controller._capacity_event.is_set())
                started.assert_not_called()
        finally:
            if lock_held:
                controlled_lock.release()
            if waiter is not None:
                await _cancel_and_reap(waiter)
            if first_held is not None and not first_held.released:
                await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
            self.assertEqual(controller.snapshot().inflight, 0)
            self.assertEqual(controller.snapshot().waiters, 0)

    async def test_real_wait_timeout_sheds_once_without_capacity_wake(self) -> None:
        controller = self.controller(
            min_concurrency=1,
            initial_concurrency=1,
            max_concurrency=1,
            max_waiters=1,
            wait_timeout_ms=50.0,
        )
        waiter: asyncio.Task | None = None
        first_held = None
        wait_started = asyncio.Event()

        try:
            first_held = await controller.acquire()
            self.assertIsNotNone(first_held)
            original_wait = controller._capacity_event.wait

            async def observed_wait() -> None:
                wait_started.set()
                await original_wait()

            with (
                patch.object(controller._capacity_event, "wait", new=observed_wait),
                patch.object(
                    ready_vote_admission,
                    "record_ready_vote_admission_start",
                ) as started,
            ):
                waiter = asyncio.create_task(controller.acquire())
                await asyncio.wait_for(
                    wait_started.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                self.assertEqual(controller.snapshot().waiters, 1)
                shed = await asyncio.wait_for(
                    asyncio.shield(waiter),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                waiter = None

                self.assertIsNone(shed)
                snapshot = controller.snapshot()
                self.assertEqual(snapshot.waiters, 0)
                self.assertEqual(snapshot.inflight, 1)
                self.assertEqual(snapshot.shed_total, 1)
                self.assertFalse(controller._capacity_event.is_set())
                started.assert_called_once()
                self.assertIs(started.call_args.kwargs["admitted"], False)
        finally:
            if waiter is not None:
                await _cancel_and_reap(waiter)
            if first_held is not None and not first_held.released:
                await first_held.release(service_ms=20.0, pool_wait_ms=0.0)
            self.assertEqual(controller.snapshot().inflight, 0)
            self.assertEqual(controller.snapshot().waiters, 0)

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
