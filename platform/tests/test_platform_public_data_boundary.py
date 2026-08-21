from __future__ import annotations

from contextlib import AsyncExitStack
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import AuditLog, Tournament, User


class PlatformPublicDataBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-as05-{uuid4().hex[:8]}"
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
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-{label}@example.com",
                "password": self.password,
                "display_name": f"as05-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        return {
            "client": client,
            "user_id": payload["user"]["id"],
        }

    async def _grant_public_creation(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user)
            user.public_tournament_credits = 1
            await db_session.commit()

    async def test_anonymous_public_profile_has_no_account_or_auth_identity_fields(self) -> None:
        owner = await self._register_user("profile")
        handle = f"{self.prefix}-profile"
        response = await owner["client"].put(
            "/api/v1/profiles/me",
            json={
                "handle": handle,
                "contact_email": "private-profile@example.com",
                "discord_account": "public-discord",
                "region": "EU",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        anonymous = await self._new_client()
        public_response = await anonymous.get(f"/api/v1/profiles/public/{handle}")
        self.assertEqual(public_response.status_code, 200, public_response.text)
        payload = public_response.json()

        self.assertEqual(payload["handle"], handle)
        self.assertEqual(payload["discord_account"], "public-discord")
        self.assertEqual(payload["region"], "EU")
        for private_key in (
            "account_email",
            "contact_email",
            "steam_id",
            "steam_linked",
        ):
            self.assertNotIn(private_key, payload)

    async def test_anonymous_participant_roster_has_no_moderation_metadata(self) -> None:
        organizer = await self._register_user("organizer")
        player = await self._register_user("player")
        await self._grant_public_creation(str(organizer["user_id"]))

        created = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-public",
                "visibility": "public",
                "format_slug": "solo",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        slug = created.json()["slug"]

        opened = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "registration_open"},
        )
        self.assertEqual(opened.status_code, 200, opened.text)

        joined = await player["client"].post(
            f"/api/v1/tournaments/{slug}/join",
            json={"entry_type": "solo"},
        )
        self.assertEqual(joined.status_code, 201, joined.text)
        participant_id = joined.json()["id"]

        moderated = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/participants/{participant_id}/moderation",
            json={
                "status": "confirmed",
                "moderation_note": "organizer-only moderation context",
            },
        )
        self.assertEqual(moderated.status_code, 200, moderated.text)

        anonymous = await self._new_client()
        public_roster = await anonymous.get(
            f"/api/v1/tournaments/{slug}/participants"
        )
        self.assertEqual(public_roster.status_code, 200, public_roster.text)
        payload = public_roster.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["id"], participant_id)
        for private_key in (
            "moderation_note",
            "moderated_at",
            "moderated_by_user_id",
        ):
            self.assertNotIn(private_key, payload[0])

        management_roster = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/participants/manage"
        )
        self.assertEqual(management_roster.status_code, 200, management_roster.text)
        managed = management_roster.json()[0]
        self.assertEqual(
            managed["moderation_note"],
            "organizer-only moderation context",
        )
        self.assertIsNotNone(managed["moderated_at"])
        self.assertEqual(managed["moderated_by_user_id"], organizer["user_id"])

    async def test_openapi_public_contracts_exclude_private_fields(self) -> None:
        schemas = self.app.openapi()["components"]["schemas"]
        public_profile_fields = set(schemas["PublicProfileResponse"]["properties"])
        public_participant_fields = set(
            schemas["TournamentParticipantResponse"]["properties"]
        )
        management_participant_fields = set(
            schemas["TournamentParticipantManagementResponse"]["properties"]
        )

        self.assertTrue(
            {"account_email", "contact_email", "steam_id", "steam_linked"}.isdisjoint(
                public_profile_fields
            )
        )
        self.assertTrue(
            {"moderation_note", "moderated_at", "moderated_by_user_id"}.isdisjoint(
                public_participant_fields
            )
        )
        self.assertTrue(
            {"moderation_note", "moderated_at", "moderated_by_user_id"}.issubset(
                management_participant_fields
            )
        )


if __name__ == "__main__":
    unittest.main()
