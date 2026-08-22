from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, update

from apps.platform_api.app.api.routes import auth_identities
from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import ExternalIdentity, User
from python_packages.platform_infra.models import AuditLog, PasswordCredential


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


class AuthIdentityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-steam-unlink-{uuid4().hex[:8]}"
        self.app = create_app()
        self.clients = AsyncExitStack()
        self.user_ids: list[str] = []

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        async with session_factory()() as db_session:
            if self.user_ids:
                await db_session.execute(
                    delete(AuditLog).where(AuditLog.actor_user_id.in_(self.user_ids))
                )
                await db_session.execute(delete(User).where(User.id.in_(self.user_ids)))
                await db_session.commit()
        await dispose_engine()

    async def _register(self, label: str) -> tuple[httpx.AsyncClient, str]:
        client = await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-{label}@example.com",
                "password": "integration-pass-123",
                "display_name": f"steam-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        user_id = response.json()["user"]["id"]
        self.user_ids.append(user_id)
        return client, user_id

    async def _seed_identity(self, user_id: str, subject: str) -> None:
        async with session_factory()() as db_session:
            await db_session.execute(
                update(User)
                .where(User.id == user_id)
                .values(status="active", email_verified_at=datetime.now(UTC))
            )
            db_session.add(
                ExternalIdentity(
                    id=str(uuid4()),
                    user_id=user_id,
                    provider="steam",
                    subject=subject,
                )
            )
            await db_session.commit()

    async def test_steam_unlink_enforces_login_boundary_and_is_idempotent(self) -> None:
        verified_client, verified_user_id = await self._register("verified")
        await self._seed_identity(verified_user_id, "76561198000010001")

        verified_me = await verified_client.get("/api/v1/users/me")
        self.assertEqual(verified_me.status_code, 200, verified_me.text)
        self.assertTrue(verified_me.json()["can_unlink_steam"])

        unlinked = await verified_client.delete("/api/v1/auth/identities/steam")
        self.assertEqual(unlinked.status_code, 200, unlinked.text)
        self.assertFalse(unlinked.json()["steam_linked"])
        async with session_factory()() as db_session:
            self.assertIsNone(
                await db_session.scalar(
                    select(ExternalIdentity).where(ExternalIdentity.user_id == verified_user_id)
                )
            )
            self.assertIsNotNone(
                await db_session.scalar(
                    select(AuditLog).where(
                        AuditLog.actor_user_id == verified_user_id,
                        AuditLog.action == "auth.steam.unlink",
                    )
                )
            )

        already_unlinked = await verified_client.delete("/api/v1/auth/identities/steam")
        self.assertEqual(already_unlinked.status_code, 200, already_unlinked.text)
        self.assertFalse(already_unlinked.json()["steam_linked"])

        steam_only_client, steam_only_user_id = await self._register("steam-only")
        await self._seed_identity(steam_only_user_id, "76561198000010002")
        async with session_factory()() as db_session:
            user = await db_session.get(User, steam_only_user_id)
            self.assertIsNotNone(user)
            user.email = None
            user.email_verified_at = None
            await db_session.execute(
                delete(PasswordCredential).where(PasswordCredential.user_id == steam_only_user_id)
            )
            await db_session.commit()

        steam_only_me = await steam_only_client.get("/api/v1/users/me")
        self.assertEqual(steam_only_me.status_code, 200, steam_only_me.text)
        self.assertFalse(steam_only_me.json()["can_unlink_steam"])

        blocked = await steam_only_client.delete("/api/v1/auth/identities/steam")
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("подтвержденную почту", blocked.json()["detail"])

        async with session_factory()() as db_session:
            await db_session.execute(
                update(User).where(User.id == steam_only_user_id).values(status="disabled")
            )
            await db_session.commit()
        inactive = await steam_only_client.delete("/api/v1/auth/identities/steam")
        self.assertEqual(inactive.status_code, 401, inactive.text)


if __name__ == "__main__":
    unittest.main()
