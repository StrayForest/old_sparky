from __future__ import annotations

import unittest
from pathlib import Path


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
