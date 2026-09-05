from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from python_packages.platform_infra import redis as redis_infra
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformRedisLifecycleTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_clients = redis_infra._shared_clients
        redis_infra._shared_clients = {}

    async def asyncTearDown(self) -> None:
        redis_infra._shared_clients = self.previous_clients

    async def test_shared_client_is_reused_and_closed_on_owning_loop(self) -> None:
        client = Mock()
        client.aclose = AsyncMock()
        settings = Mock(
            platform_redis_url="redis://127.0.0.1:6379/15",
            platform_redis_max_connections=4,
        )

        with (
            patch.object(redis_infra, "get_settings", return_value=settings),
            patch.object(redis_infra, "from_url", return_value=client) as from_url,
        ):
            first = redis_infra.redis_client(decode_responses=False, shared=True)
            second = redis_infra.redis_client(decode_responses=False, shared=True)
            await redis_infra.dispose_redis_clients()

        self.assertIs(first, client)
        self.assertIs(second, client)
        from_url.assert_called_once_with(
            settings.platform_redis_url,
            decode_responses=False,
            max_connections=settings.platform_redis_max_connections,
        )
        client.aclose.assert_awaited_once_with()
        self.assertEqual(redis_infra._shared_clients, {})

    async def test_shared_client_rejects_cross_loop_replacement(self) -> None:
        old_loop = asyncio.new_event_loop()
        client = Mock()
        redis_infra._shared_clients[True] = (old_loop, client)

        try:
            with self.assertRaisesRegex(RuntimeError, "another event loop"):
                redis_infra.redis_client(shared=True)
        finally:
            redis_infra._shared_clients.clear()
            old_loop.close()

    async def test_dispose_rejects_client_owned_by_another_loop(self) -> None:
        old_loop = asyncio.new_event_loop()
        client = Mock()
        client.aclose = AsyncMock()
        redis_infra._shared_clients[True] = (old_loop, client)

        try:
            with self.assertRaisesRegex(RuntimeError, "owning event loop"):
                await redis_infra.dispose_redis_clients()
            self.assertEqual(redis_infra._shared_clients[True], (old_loop, client))
        finally:
            redis_infra._shared_clients.clear()
            old_loop.close()

        client.aclose.assert_not_awaited()
