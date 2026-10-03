from __future__ import annotations

import asyncio
from unittest.mock import patch

from python_packages.platform_infra.authenticated_read_admission import (
    AuthenticatedReadAdmission,
    _has_session_cookie,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


_ADMISSION_OBSERVER_TIMEOUT_SECONDS = 0.5


async def _cancel_and_reap(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait_for(
        asyncio.gather(task, return_exceptions=True),
        timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
    )


class AuthenticatedReadAdmissionTests(PlatformIsolatedAsyncioTestCase):
    async def test_saturated_read_is_shed_before_database_work(self) -> None:
        controller = AuthenticatedReadAdmission(
            limit=1,
            max_waiters=0,
            wait_timeout_ms=0,
        )
        admitted, _wait, snapshot = await controller.acquire()
        self.assertTrue(admitted)
        self.assertEqual(snapshot.inflight, 1)

        shed, wait, snapshot = await controller.acquire()

        self.assertFalse(shed)
        self.assertLess(wait, 0.01)
        self.assertEqual(snapshot.waiters, 0)
        self.assertEqual(snapshot.shed_total, 1)
        await controller.release()
        self.assertEqual(controller.snapshot().inflight, 0)

    async def test_waiter_budget_is_bounded(self) -> None:
        controller = AuthenticatedReadAdmission(
            limit=1,
            max_waiters=1,
            wait_timeout_ms=100,
        )
        admitted, _wait, _snapshot = await controller.acquire()
        self.assertTrue(admitted)

        wait_started = asyncio.Event()
        original_wait = controller._condition.wait

        async def observed_wait() -> None:
            wait_started.set()
            await original_wait()

        waiter: asyncio.Task | None = None
        try:
            with patch.object(
                controller._condition,
                "wait",
                side_effect=observed_wait,
            ):
                waiter = asyncio.create_task(controller.acquire())
                await asyncio.wait_for(
                    wait_started.wait(),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                self.assertEqual(controller.snapshot().waiters, 1)
                shed, _wait, snapshot = await controller.acquire()
                self.assertFalse(shed)
                self.assertEqual(snapshot.shed_total, 1)

                await controller.release()
                waiter_admitted, _wait, _snapshot = await asyncio.wait_for(
                    asyncio.shield(waiter),
                    timeout=_ADMISSION_OBSERVER_TIMEOUT_SECONDS,
                )
                self.assertTrue(waiter_admitted)
                await controller.release()
        finally:
            if waiter is not None:
                await _cancel_and_reap(waiter)
            for _ in range(2):
                if controller.snapshot().inflight <= 0:
                    break
                await controller.release()
            self.assertEqual(controller.snapshot().inflight, 0)
            self.assertEqual(controller.snapshot().waiters, 0)

    def test_cookie_match_requires_the_configured_session_name(self) -> None:
        scope = {
            "headers": [
                (b"cookie", b"theme=dark; deadlock_platform_session=token"),
            ]
        }
        self.assertTrue(_has_session_cookie(scope, "deadlock_platform_session"))
        self.assertFalse(_has_session_cookie(scope, "other_session"))
