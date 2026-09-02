from __future__ import annotations

import asyncio
import unittest

from python_packages.platform_infra.authenticated_read_admission import (
    AuthenticatedReadAdmission,
    _has_session_cookie,
)


class AuthenticatedReadAdmissionTests(unittest.IsolatedAsyncioTestCase):
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

        waiter = asyncio.create_task(controller.acquire())
        await asyncio.sleep(0)
        self.assertEqual(controller.snapshot().waiters, 1)
        shed, _wait, snapshot = await controller.acquire()
        self.assertFalse(shed)
        self.assertEqual(snapshot.shed_total, 1)

        await controller.release()
        waiter_admitted, _wait, _snapshot = await asyncio.wait_for(waiter, 0.2)
        self.assertTrue(waiter_admitted)
        await controller.release()

    def test_cookie_match_requires_the_configured_session_name(self) -> None:
        scope = {
            "headers": [
                (b"cookie", b"theme=dark; deadlock_platform_session=token"),
            ]
        }
        self.assertTrue(_has_session_cookie(scope, "deadlock_platform_session"))
        self.assertFalse(_has_session_cookie(scope, "other_session"))
