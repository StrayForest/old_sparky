from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.api.schemas import UserResponse
from apps.platform_api.app.services.current_user import (
    serialize_current_user_from_account_read_model,
)
from apps.platform_api.app.services.user_account_read_models import (
    _encode,
    get_or_build_user_account_read_model,
    user_account_read_model_key,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class UserAccountReadModelTests(PlatformIsolatedAsyncioTestCase):
    def test_serializer_keeps_auth_fields_outside_cached_account_payload(self) -> None:
        user = SimpleNamespace(
            id="user-1",
            email="user@example.com",
            display_name="Player",
            status="active",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            public_tournament_credits=2,
            private_tournament_credits=1,
        )

        response = serialize_current_user_from_account_read_model(
            user,
            role_slugs=frozenset({"player"}),
            account={
                "avatar_url": None,
                "avatar_media": None,
                "steam_id": "76561198000000000",
                "steam_linked": True,
                "has_password": True,
                "can_unlink_steam": True,
                "private_tournament_monthly_remaining": 0,
                "private_tournament_monthly_limit": 1,
            },
        )

        self.assertIsInstance(response, UserResponse)
        self.assertEqual(response.email, "user@example.com")
        self.assertEqual(response.roles, ["player"])
        self.assertEqual(response.steam_id, "76561198000000000")
        self.assertEqual(response.private_tournament_monthly_remaining, 0)

    async def test_cache_hit_refreshes_quota_without_rebuilding_account_joins(self) -> None:
        user = SimpleNamespace(id="user-1")
        account = {
            "avatar_url": None,
            "avatar_media": None,
            "steam_id": None,
            "steam_linked": False,
            "has_password": False,
            "can_unlink_steam": False,
            "private_tournament_monthly_remaining": 1,
            "private_tournament_monthly_limit": 1,
        }
        client = SimpleNamespace(
            get=AsyncMock(
                return_value=_encode(revision=10, payload=account),
            ),
            aclose=AsyncMock(),
        )
        db_session = SimpleNamespace(execute=AsyncMock())

        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.private_tournament_monthly_remaining",
            new=AsyncMock(return_value=0),
        ):
            result = await get_or_build_user_account_read_model(
                db_session,
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )

        self.assertEqual(result["private_tournament_monthly_remaining"], 0)
        self.assertEqual(result["private_tournament_monthly_limit"], 1)
        client.get.assert_awaited_once_with(user_account_read_model_key("user-1"))
        db_session.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
