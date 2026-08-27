from __future__ import annotations

import asyncio
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.requests import Request

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.redis import redis_client
from python_packages.platform_infra import sse_connection_limit as sse


class PlatformSseConnectionLimitTests(unittest.IsolatedAsyncioTestCase):
    def test_global_cap_leaves_headroom_below_observed_edge_queue(self) -> None:
        self.assertEqual(sse.SSE_GLOBAL_LIMIT, 3_000)

    async def asyncSetUp(self) -> None:
        self.settings = get_settings()
        if self.settings.platform_environment.strip().lower() != "test":
            self.skipTest("SSE limiter integration tests require PLATFORM_ENVIRONMENT=test.")
        await self._clear_limit_keys()

    async def asyncTearDown(self) -> None:
        await self._clear_limit_keys()
        await sse.dispose_sse_connection_limiter()

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

    def test_signed_qa_capacity_proof_is_bounded_and_tamper_evident(self) -> None:
        now = 1_700_000_000
        token = sse.sse_load_test_capacity_token(
            self.settings,
            15_000,
            now_epoch=now,
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/test/bracket/events",
            "headers": [
                (
                    sse.SSE_LOAD_TEST_CAPACITY_HEADER.encode("ascii"),
                    token.encode("ascii"),
                )
            ],
        }

        with patch.object(sse.time, "time", return_value=now):
            self.assertEqual(sse._qa_global_limit(scope, self.settings), 15_000)

        with patch.object(
            sse.time,
            "time",
            return_value=now + sse.SSE_LOAD_TEST_CAPACITY_TTL_SECONDS,
        ):
            self.assertIsNone(sse._qa_global_limit(scope, self.settings))

        tampered = token.replace("15000:", "30000:", 1)
        scope["headers"] = [
            (
                sse.SSE_LOAD_TEST_CAPACITY_HEADER.encode("ascii"),
                tampered.encode("ascii"),
            )
        ]
        with patch.object(sse.time, "time", return_value=now):
            self.assertIsNone(sse._qa_global_limit(scope, self.settings))

        with self.assertRaises(ValueError):
            sse.sse_load_test_capacity_token(
                self.settings,
                sse.SSE_QA_GLOBAL_LIMIT_MAX + 1,
                now_epoch=now,
            )

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

    async def test_lease_renewal_extends_all_scopes_after_interval(self) -> None:
        lease = sse.SseConnectionLease(
            member="member",
            settings=self.settings,
            keys=["global", "source", "user"],
            lease_seconds=120,
            last_renewed_epoch=100,
        )
        with patch.object(sse, "_renew_keys", new=AsyncMock()) as renew_keys:
            await lease.renew(now_epoch=120)
            renew_keys.assert_not_awaited()
            await lease.renew(now_epoch=130)

        renew_keys.assert_awaited_once_with(
            lease.keys,
            member="member",
            lease_seconds=120,
            now_epoch=130,
        )
        self.assertEqual(lease.last_renewed_epoch, 130)

    async def test_authenticated_user_scope_uses_route_auth_result(self) -> None:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/test/bracket/events",
            "headers": [],
            "client": ("127.0.0.1", 443),
        }
        lease = sse.SseConnectionLease(member="member", settings=self.settings)
        scope[sse.SSE_CONNECTION_LEASE_SCOPE] = lease
        auth_session = SimpleNamespace(user=SimpleNamespace(id="user-1"))

        with patch.object(
            sse.SseConnectionLease,
            "add_user_scope",
            new_callable=AsyncMock,
        ) as add_user_scope:
            await sse.admit_sse_authenticated_user(
                request=Request(scope),
                auth_session=auth_session,
            )

        add_user_scope.assert_awaited_once_with("user-1")


class PlatformSseConnectionReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_commands_are_bounded_during_mass_teardown(self) -> None:
        active = 0
        maximum_active = 0

        async def eval_script(*_args):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            active -= 1
            return 0

        cache = MagicMock()
        cache.eval = eval_script
        with patch.object(
            sse,
            "_sse_release_semaphore",
            asyncio.Semaphore(2),
        ), patch.object(sse, "_limiter_client", return_value=cache):
            await asyncio.gather(
                *(
                    sse._release_keys(["global"], member=f"member-{index}")
                    for index in range(8)
                )
            )

        self.assertLessEqual(maximum_active, 2)


class PlatformSseConnectionMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_limit_failure_is_returned_as_controlled_429(self) -> None:
        settings = get_settings()
        lease = MagicMock(spec=sse.SseConnectionLease)
        lease.release = AsyncMock()
        sent_messages = []

        async def app(_scope, _receive, _send):
            raise sse.SseConnectionLimitExceeded("user")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent_messages.append(message)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/test/bracket/events",
            "headers": [],
            "client": ("127.0.0.1", 443),
        }
        middleware = sse.SseConnectionLimitMiddleware(
            app,
            settings_factory=lambda: settings,
        )

        with patch.object(
            sse,
            "reserve_sse_connection",
            new=AsyncMock(return_value=lease),
        ):
            await middleware(scope, receive, send)

        self.assertEqual(sent_messages[0]["type"], "http.response.start")
        self.assertEqual(sent_messages[0]["status"], 429)
        lease.release.assert_awaited_once_with()

    async def test_signed_qa_capacity_limit_is_passed_without_bypassing_global_admission(
        self,
    ) -> None:
        settings = get_settings()
        lease = MagicMock(spec=sse.SseConnectionLease)
        lease.release = AsyncMock()

        async def app(_scope, _receive, _send):
            return None

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        now = 1_700_000_000
        token = sse.sse_load_test_capacity_token(settings, 15_000, now_epoch=now)
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/tournaments/test/bracket/events",
            "headers": [
                (
                    sse.SSE_LOAD_TEST_CAPACITY_HEADER.encode("ascii"),
                    token.encode("ascii"),
                )
            ],
            "client": ("127.0.0.1", 443),
        }
        middleware = sse.SseConnectionLimitMiddleware(
            app,
            settings_factory=lambda: settings,
        )
        with (
            patch.object(sse.time, "time", return_value=now),
            patch.object(
                sse,
                "reserve_sse_connection",
                new=AsyncMock(return_value=lease),
            ) as reserve,
        ):
            await middleware(scope, receive, send)

        reserve.assert_awaited_once_with(
            "127.0.0.1",
            settings=settings,
            global_limit=15_000,
            bypass_source_limit=False,
        )
        lease.release.assert_awaited_once_with()


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
        self.assertIn('"upstream_status":"$upstream_status"', nginx)
        self.assertIn('"request_completion":"$request_completion"', nginx)
        self.assertIn('"connection_requests":$connection_requests', nginx)
        self.assertIn("proxy_read_timeout 60s;", nginx)


if __name__ == "__main__":
    unittest.main()
