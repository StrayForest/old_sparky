from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from apps.platform_api.app.api.routes import auth_identities
from python_packages.platform_infra.models import ExternalIdentity, User


class AuthIdentityRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_steam_unlink_route_is_defined_for_delete(self) -> None:
        route = next(
            (
                candidate
                for candidate in auth_identities.router.routes
                if getattr(candidate, "path", None) == "/identities/steam"
            ),
            None,
        )

        self.assertIsNotNone(route)
        self.assertIn("DELETE", getattr(route, "methods", set()))

    async def test_steam_unlink_allows_verified_password_account(self) -> None:
        user = User(
            id="unlink-user",
            email="player@example.com",
            display_name="Player",
            status="active",
            email_verified_at=datetime.now(UTC),
        )
        identity = ExternalIdentity(
            id="unlink-steam",
            user_id=user.id,
            provider="steam",
            subject="76561198000010001",
        )
        auth_session = SimpleNamespace(
            user=user,
            role_slugs=frozenset({"authenticated_user"}),
        )
        db_session = AsyncMock()
        db_session.scalar = AsyncMock(side_effect=[user, identity, user.id])
        db_session.delete = AsyncMock()
        db_session.commit = AsyncMock()
        serialized_user = object()

        with (
            patch.object(
                auth_identities,
                "write_audit_log",
                new_callable=AsyncMock,
            ) as write_audit_log,
            patch.object(
                auth_identities,
                "serialize_current_user",
                AsyncMock(return_value=serialized_user),
            ) as serialize_current_user,
        ):
            result = await auth_identities.unlink_steam_identity(
                auth_session=auth_session,
                db_session=db_session,
            )

        self.assertIs(result, serialized_user)
        db_session.delete.assert_awaited_once_with(identity)
        db_session.commit.assert_awaited_once_with()
        write_audit_log.assert_awaited_once()
        serialize_current_user.assert_awaited_once_with(
            db_session,
            user,
            role_slugs=auth_session.role_slugs,
        )


if __name__ == "__main__":
    unittest.main()
