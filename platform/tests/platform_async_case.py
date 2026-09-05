from __future__ import annotations

import unittest

from python_packages.platform_infra.db import dispose_engine
from python_packages.platform_infra.redis import dispose_redis_clients


class PlatformIsolatedAsyncioTestCase(unittest.IsolatedAsyncioTestCase):
    """Close process-level async infrastructure before each test loop exits."""

    def setUp(self) -> None:
        # IsolatedAsyncioTestCase enables asyncio debug mode and its generic
        # 100 ms callback threshold. Real PostgreSQL integration steps can
        # legitimately exceed that threshold; keep a five-second stall signal
        # while avoiding routine scheduler noise in the backend gate.
        assert self._asyncioRunner is not None
        self._asyncioRunner.get_loop().slow_callback_duration = 5.0

    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult:
        self.addAsyncCleanup(self._dispose_platform_resources)
        return super().run(result)

    @staticmethod
    async def _dispose_platform_resources() -> None:
        await dispose_redis_clients()
        await dispose_engine()
