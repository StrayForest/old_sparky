from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.services.auth_bootstrap import build_auth_bootstrap
from apps.platform_api.app.services.profile_read_models import ProfileAvatarReadModel
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class AuthBootstrapServiceTests(PlatformIsolatedAsyncioTestCase):
    async def test_bootstrap_without_request_session_omits_profile_media(self) -> None:
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
        result = await build_auth_bootstrap(auth_session)

        self.assertEqual(result.id, "user-1")
        self.assertEqual(result.roles, ["admin", "player"])
        self.assertTrue(result.can_create_public_tournaments)
        self.assertIsNone(result.avatar_url)
        self.assertIsNone(result.avatar_media)

    async def test_bootstrap_uses_lightweight_avatar_projection_with_request_session(self) -> None:
        auth_session = SimpleNamespace(
            user=SimpleNamespace(
                id="user-1",
                email="player@example.com",
                display_name="Player",
                status="active",
                created_at=datetime(2026, 9, 2, tzinfo=UTC),
                public_tournament_credits=0,
                private_tournament_credits=0,
            ),
            role_slugs=frozenset(),
        )
        projection = ProfileAvatarReadModel(
            avatar_url="/media/avatar-256.webp",
            avatar_media=None,
        )
        db_session = object()

        with patch(
            "apps.platform_api.app.services.auth_bootstrap.get_profile_avatar_read_model",
            new=AsyncMock(return_value=projection),
        ) as read_model:
            result = await build_auth_bootstrap(auth_session, db_session=db_session)

        read_model.assert_awaited_once_with(db_session, "user-1")
        self.assertEqual(result.avatar_url, "/media/avatar-256.webp")
        self.assertIsNone(result.avatar_media)
