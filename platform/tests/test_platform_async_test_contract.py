from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformAsyncTestContractTests(unittest.TestCase):
    def test_async_tests_use_platform_resource_cleanup_owner(self) -> None:
        tests_root = Path(__file__).resolve().parent
        raw_base = "unittest." + "IsolatedAsyncioTestCase"
        offenders = [
            path.name
            for path in sorted(tests_root.glob("test_*.py"))
            if raw_base in path.read_text(encoding="utf-8")
        ]

        self.assertEqual(offenders, [])


class PlatformAsyncRuntimeContractTests(PlatformIsolatedAsyncioTestCase):
    async def test_integration_callback_threshold_keeps_five_second_stall_signal(
        self,
    ) -> None:
        self.assertEqual(asyncio.get_running_loop().slow_callback_duration, 5.0)
