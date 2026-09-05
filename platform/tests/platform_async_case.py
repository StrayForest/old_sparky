from __future__ import annotations

import unittest

from python_packages.platform_infra.db import dispose_engine
from python_packages.platform_infra.redis import dispose_redis_clients


class PlatformIsolatedAsyncioTestCase(unittest.IsolatedAsyncioTestCase):
    """Close process-level async infrastructure before each test loop exits."""

    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult:
        self.addAsyncCleanup(self._dispose_platform_resources)
        return super().run(result)

    @staticmethod
    async def _dispose_platform_resources() -> None:
        await dispose_redis_clients()
        await dispose_engine()
