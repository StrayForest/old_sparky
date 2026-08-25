from __future__ import annotations

import asyncio
from pathlib import Path
import unittest

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client
from python_packages.platform_infra import sse_connection_limit as sse


class PlatformSseConnectionLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = get_settings()
        if self.settings.platform_environment.strip().lower() != "test":
            self.skipTest("SSE limiter integration tests require PLATFORM_ENVIRONMENT=test.")
        await self._clear_limit_keys()

    async def asyncTearDown(self) -> None:
        await self._clear_limit_keys()

    async def _clear_limit_keys(self) -> None:
        cache = redis_client()
        try:
            keys = [
                key
                async for key in cache.scan_iter(match=f"{sse.SSE_KEY_PREFIX}:*")
            ]
            if keys:
                await cache.delete(*keys)
        finally:
            await cache.aclose()

    async def test_concurrent_source_reservations_are_atomically_bounded(self) -> None:
        async def reserve():
            try:
                return await sse.reserve_sse_connection(
                    "203.0.113.10",
                    settings=self.settings,
                    global_limit=100,
                    source_limit=6,
                )
            except sse.SseConnectionLimitExceeded as exc:
                return exc

        results = await asyncio.gather(*(reserve() for _ in range(12)))
        leases = [
            result
            for result in results
            if isinstance(result, sse.SseConnectionLease)
        ]
        rejections = [
            result
            for result in results
            if isinstance(result, sse.SseConnectionLimitExceeded)
        ]
        try:
            self.assertEqual(len(leases), 6)
            self.assertEqual(len(rejections), 6)
            self.assertTrue(all(rejection.scope == "source" for rejection in rejections))
        finally:
            await asyncio.gather(*(lease.release() for lease in leases))

    async def test_global_limit_is_shared_across_sources(self) -> None:
        leases: list[sse.SseConnectionLease] = []
        try:
            for address in ("203.0.113.11", "203.0.113.12"):
                leases.append(
                    await sse.reserve_sse_connection(
                        address,
                        settings=self.settings,
                        global_limit=2,
                        source_limit=10,
                    )
                )
            with self.assertRaises(sse.SseConnectionLimitExceeded) as context:
                await sse.reserve_sse_connection(
                    "203.0.113.13",
                    settings=self.settings,
                    global_limit=2,
                    source_limit=10,
                )
            self.assertEqual(context.exception.scope, "global")
        finally:
            await asyncio.gather(*(lease.release() for lease in leases))

    async def test_allowlisted_source_bypasses_source_cap_but_not_global_cap(self) -> None:
        settings = self.settings.model_copy(
            update={"platform_load_test_source_ips": "203.0.113.50"}
        )
        leases = [
            await sse.reserve_sse_connection(
                "203.0.113.50",
                settings=settings,
                global_limit=2,
                source_limit=1,
            )
            for _ in range(2)
        ]
        try:
            with self.assertRaises(sse.SseConnectionLimitExceeded) as context:
                await sse.reserve_sse_connection(
                    "203.0.113.50",
                    settings=settings,
                    global_limit=2,
                    source_limit=1,
                )
            self.assertEqual(context.exception.scope, "global")
        finally:
            await asyncio.gather(*(lease.release() for lease in leases))

    async def test_signed_qa_bypass_skips_source_cap_but_not_global_cap(self) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/test/bracket/events",
            "headers": [
                (
                    sse.SSE_LOAD_TEST_BYPASS_HEADER.encode("ascii"),
                    sse.sse_load_test_bypass_token(self.settings).encode("ascii"),
                )
            ],
        }
        self.assertTrue(sse._has_sse_load_test_bypass(scope, self.settings))
        leases = [
            await sse.reserve_sse_connection(
                "127.0.0.1",
                settings=self.settings,
                global_limit=2,
                source_limit=1,
                bypass_source_limit=True,
            )
            for _ in range(2)
        ]
        try:
            with self.assertRaises(sse.SseConnectionLimitExceeded) as context:
                await sse.reserve_sse_connection(
                    "127.0.0.1",
                    settings=self.settings,
                    global_limit=2,
                    source_limit=1,
                    bypass_source_limit=True,
                )
            self.assertEqual(context.exception.scope, "global")
        finally:
            await asyncio.gather(*(lease.release() for lease in leases))

    async def test_user_limit_spans_distinct_source_addresses(self) -> None:
        leases = [
            await sse.reserve_sse_connection(
                f"203.0.113.{20 + index}",
                settings=self.settings,
                global_limit=10,
                source_limit=10,
            )
            for index in range(3)
        ]
        try:
            await leases[0].add_user_scope("user-1", user_limit=2)
            await leases[1].add_user_scope("user-1", user_limit=2)
            with self.assertRaises(sse.SseConnectionLimitExceeded) as context:
                await leases[2].add_user_scope("user-1", user_limit=2)
            self.assertEqual(context.exception.scope, "user")
        finally:
            await asyncio.gather(*(lease.release() for lease in leases))

    async def test_release_immediately_returns_source_capacity(self) -> None:
        first = await sse.reserve_sse_connection(
            "203.0.113.30",
            settings=self.settings,
            global_limit=10,
            source_limit=1,
        )
        with self.assertRaises(sse.SseConnectionLimitExceeded):
            await sse.reserve_sse_connection(
                "203.0.113.30",
                settings=self.settings,
                global_limit=10,
                source_limit=1,
            )

        await first.release()
        replacement = await sse.reserve_sse_connection(
            "203.0.113.30",
            settings=self.settings,
            global_limit=10,
            source_limit=1,
        )
        await replacement.release()

    async def test_expired_crash_lease_is_pruned_before_capacity_check(self) -> None:
        stale_now = int(__import__("time").time()) - 120
        stale = await sse.reserve_sse_connection(
            "203.0.113.40",
            settings=self.settings,
            global_limit=1,
            source_limit=1,
            lease_seconds=1,
            now_epoch=stale_now,
        )
        replacement = await sse.reserve_sse_connection(
            "203.0.113.41",
            settings=self.settings,
            global_limit=1,
            source_limit=1,
        )
        await stale.release()
        await replacement.release()


class PlatformSseNginxGuardTests(unittest.TestCase):
    def test_nginx_has_coarse_per_ip_and_global_sse_caps(self) -> None:
        platform_root = Path(__file__).resolve().parents[1]
        nginx = (
            platform_root / "deploy/nginx/deadlock-platform.conf"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "limit_conn_zone $binary_remote_addr zone=platform_sse_ip:1m;",
            nginx,
        )
        self.assertIn(
            "limit_conn_zone $server_name zone=platform_sse_global:1m;",
            nginx,
        )
        self.assertIn("limit_conn platform_sse_ip 10240;", nginx)
        self.assertIn("limit_conn platform_sse_global 10240;", nginx)
        self.assertIn("limit_conn_status 429;", nginx)
        self.assertIn('"limit_conn_status":"$limit_conn_status"', nginx)
        self.assertIn("proxy_read_timeout 660s;", nginx)


if __name__ == "__main__":
    unittest.main()
