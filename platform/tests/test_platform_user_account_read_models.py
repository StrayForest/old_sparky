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
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            result = await get_or_build_user_account_read_model(
                db_session,
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )

        self.assertEqual(result["private_tournament_monthly_remaining"], 0)
        self.assertEqual(result["private_tournament_monthly_limit"], 1)
        client.get.assert_awaited_once_with(user_account_read_model_key("user-1"))
        db_session.execute.assert_not_awaited()
        event = record_event.call_args.kwargs
        self.assertEqual(event["model"], "user_account")
        self.assertEqual(event["outcome"], "hit")
        self.assertGreaterEqual(event["get_ms"], 0)
        self.assertEqual(event["build_ms"], 0)
        self.assertEqual(event["set_ms"], 0)
        self.assertEqual(
            set(event), {
                "model", "outcome", "get_ms", "build_ms", "set_ms",
                "include_payload_metadata",
            }
        )

    async def test_cache_miss_records_build_and_swallowed_write_failure(self) -> None:
        user = SimpleNamespace(id="private-user-id")
        payload = {
            "avatar_url": None,
            "avatar_media": None,
            "steam_id": None,
            "steam_linked": False,
            "has_password": False,
            "can_unlink_steam": False,
            "private_tournament_monthly_remaining": 1,
            "private_tournament_monthly_limit": 1,
        }
        client = SimpleNamespace(get=AsyncMock(return_value=None), aclose=AsyncMock())
        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.build_user_account_read_model",
            new=AsyncMock(return_value=(10, payload)),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=AsyncMock(return_value=False),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            result = await get_or_build_user_account_read_model(
                SimpleNamespace(),
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )

        self.assertEqual(result, payload)
        client.get.assert_awaited_once_with(user_account_read_model_key("private-user-id"))
        client.aclose.assert_awaited_once()
        event = record_event.call_args.kwargs
        self.assertEqual(event["outcome"], "write_error")
        self.assertGreaterEqual(event["get_ms"], 0)
        self.assertGreaterEqual(event["build_ms"], 0)
        self.assertGreaterEqual(event["set_ms"], 0)
        self.assertEqual(
            set(event), {
                "model", "outcome", "get_ms", "build_ms", "set_ms",
                "include_payload_metadata",
            }
        )
        self.assertNotIn("private-user-id", repr(event))
        self.assertNotIn("private_tournament_monthly_remaining", repr(event))

    async def test_cache_read_and_write_errors_have_distinct_outcome(self) -> None:
        user = SimpleNamespace(id="user-1")
        payload = {
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
            get=AsyncMock(side_effect=OSError("private cache detail")),
            aclose=AsyncMock(),
        )
        with patch(
            "apps.platform_api.app.services.user_account_read_models.redis_client",
            return_value=client,
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.build_user_account_read_model",
            new=AsyncMock(return_value=(10, payload)),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models._write",
            new=AsyncMock(return_value=False),
        ), patch(
            "apps.platform_api.app.services.user_account_read_models.record_redis_read_model_event",
        ) as record_event:
            await get_or_build_user_account_read_model(
                SimpleNamespace(),
                user=user,
                now=datetime(2026, 1, 2, tzinfo=UTC),
            )

        self.assertEqual(record_event.call_args.kwargs["outcome"], "read_write_error")
        self.assertEqual(set(record_event.call_args.kwargs), {
            "model", "outcome", "get_ms", "build_ms", "set_ms",
            "include_payload_metadata",
        })


if __name__ == "__main__":
    unittest.main()
