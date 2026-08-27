from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from apps.platform_api.app.services import ready_check_events


class PlatformReadyCheckStateProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_probe_is_one_redis_get_and_no_database_dependency(self) -> None:
        client = MagicMock()
        client.get = AsyncMock(return_value='{"revision":184,"status":"waiting"}')
        client.aclose = AsyncMock()
        with (
            patch.object(ready_check_events, "redis_client", return_value=client),
            patch.object(ready_check_events, "_state_key", return_value="state-key"),
        ):
            state = await ready_check_events.read_ready_check_state(
                tournament_id="tournament-1",
                user_id="user-1",
                ready_check_starts_at=1_700_000_000,
            )

        self.assertEqual(state, {"revision": 184, "status": "waiting"})
        client.get.assert_awaited_once_with("state-key")
        client.aclose.assert_awaited_once_with()
