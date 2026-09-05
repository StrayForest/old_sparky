from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.services.auth_bootstrap import build_auth_bootstrap
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class AuthBootstrapServiceTests(PlatformIsolatedAsyncioTestCase):
    async def test_bootstrap_keeps_authority_in_auth_session_and_reads_cached_avatar(self) -> None:
        auth_session = SimpleNamespace(
            user=SimpleNamespace(
                id="user-1",
                email="player@example.com",
                display_name="Player",
                status="active",
                created_at=datetime(2026, 9, 2, tzinfo=UTC),
                public_tournament_credits=2,
                private_tournament_credits=4,
            ),
            role_slugs=frozenset({"player", "admin"}),
        )
        payload = (
            b'{"profile":{"avatar_url":"/media/avatar-256.webp",'
            b'"avatar_media":{"asset_id":"asset-1","purpose":"profile_avatar",'
            b'"status":"ready","variants":[]}}}'
        )

        with patch(
            "apps.platform_api.app.services.auth_bootstrap.get_or_build_profile_read_model",
            new=AsyncMock(return_value=payload),
        ) as read_model:
            result = await build_auth_bootstrap(auth_session)

        read_model.assert_awaited_once_with("user-1")
        self.assertEqual(result.id, "user-1")
        self.assertEqual(result.roles, ["admin", "player"])
        self.assertTrue(result.can_create_public_tournaments)
        self.assertEqual(result.avatar_url, "/media/avatar-256.webp")
        self.assertEqual(result.avatar_media.asset_id, "asset-1")
