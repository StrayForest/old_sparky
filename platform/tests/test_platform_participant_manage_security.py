from __future__ import annotations

from contextlib import AsyncExitStack
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import AuditLog, Tournament, User


class PlatformParticipantManageSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-participant-security-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
        self.app = create_app()
        self.clients = AsyncExitStack()
        await self._cleanup_test_data()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        await self._cleanup_test_data()
        await dispose_engine()

    async def _cleanup_test_data(self) -> None:
        async with session_factory()() as db_session:
            user_ids = list(
                (
                    await db_session.scalars(
                        select(User.id).where(
                            User.email.like(f"{self.prefix}-%@example.com")
                        )
                    )
                ).all()
            )
            if user_ids:
                await db_session.execute(
                    delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids))
                )
            await db_session.execute(
                delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%"))
            )
            if user_ids:
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
            await db_session.commit()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )

    async def _register_user(self, label: str) -> dict[str, object]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": self.password,
                "display_name": f"sec-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        return {
            "client": client,
            "email": email,
            "user_id": payload["user"]["id"],
        }

    async def test_organizer_add_resolves_only_scoped_invite_access(self) -> None:
        organizer = await self._register_user("organizer")
        target = await self._register_user("target")

        create_response = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-cup",
                "visibility": "invite_only",
                "format_slug": "solo",
                "max_participants": 8,
            },
        )
        self.assertEqual(create_response.status_code, 201, create_response.text)
        tournament = create_response.json()
        slug = tournament["slug"]

        open_response = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "registration_open"},
        )
        self.assertEqual(open_response.status_code, 200, open_response.text)

        existing_without_access = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/participants/manage",
            json={
                "user_email": target["email"],
                "entry_type": "solo",
                "team_name": None,
            },
        )
        missing_account = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/participants/manage",
            json={
                "user_email": f"{self.prefix}-missing@example.com",
                "entry_type": "solo",
                "team_name": None,
            },
        )

        self.assertEqual(existing_without_access.status_code, 404)
        self.assertEqual(missing_account.status_code, 404)
        self.assertEqual(existing_without_access.json(), missing_account.json())
        self.assertEqual(
            existing_without_access.json()["detail"],
            "Participant could not be added.",
        )

        invites_response = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/invites"
        )
        self.assertEqual(invites_response.status_code, 200, invites_response.text)
        invites = invites_response.json()
        self.assertEqual(len(invites), 1)

        claim_response = await target["client"].post(
            "/api/v1/tournaments/invites/claim",
            json={
                "code": invites[0]["code"],
                "entry_type": "solo",
                "team_name": None,
            },
        )
        self.assertEqual(claim_response.status_code, 201, claim_response.text)

        add_response = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/participants/manage",
            json={
                "user_email": target["email"],
                "entry_type": "solo",
                "team_name": None,
            },
        )
        self.assertEqual(add_response.status_code, 201, add_response.text)
        self.assertEqual(add_response.json()["user_id"], target["user_id"])


if __name__ == "__main__":
    unittest.main()
