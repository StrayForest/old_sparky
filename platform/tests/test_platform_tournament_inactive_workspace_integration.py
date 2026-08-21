from __future__ import annotations

from contextlib import AsyncExitStack
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentParticipant,
    User,
)


class PlatformTournamentInactiveWorkspaceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-as04-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
        self.base_url = "http://testserver"
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
                        select(User.id).where(User.email.like(f"{self.prefix}-%@example.com"))
                    )
                ).all()
            )
            if user_ids:
                await db_session.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids)))
            await db_session.execute(delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%")))
            if user_ids:
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
            await db_session.commit()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self.base_url,
            )
        )

    def _assert_status(self, response: httpx.Response, expected_status: int) -> dict:
        self.assertEqual(response.status_code, expected_status, response.text)
        if not response.content:
            return {}
        return response.json()

    async def _register_user(self, label: str) -> dict[str, object]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        payload = self._assert_status(
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": self.password,
                    "display_name": f"as04-{label}"[:15],
                },
            ),
            201,
        )
        return {
            "client": client,
            "user_id": payload["user"]["id"],
            "email": email,
        }

    async def _set_participant_status(self, slug: str, user_id: str, participant_status: str) -> None:
        async with session_factory()() as db_session:
            participant = await db_session.scalar(
                select(TournamentParticipant)
                .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
                .where(
                    Tournament.slug == slug,
                    TournamentParticipant.user_id == user_id,
                )
            )
            self.assertIsNotNone(participant)
            participant.status = participant_status
            await db_session.commit()

    async def test_inactive_members_are_denied_on_every_private_workspace_endpoint(self) -> None:
        organizer = await self._register_user("organizer")
        tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-private",
                    "description": "AS-04 private workspace regression",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        invite = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/invites",
                json={"note": "AS-04 regression", "max_uses": 2, "expires_at": None},
            ),
            201,
        )

        members = []
        for participant_status in ("withdrawn", "disqualified"):
            member = await self._register_user(participant_status)
            self._assert_status(
                await member["client"].post(
                    "/api/v1/tournaments/invites/claim",
                    json={"code": invite["code"], "entry_type": "solo", "team_name": None},
                ),
                201,
            )
            self._assert_status(
                await member["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )
            await self._set_participant_status(
                slug,
                str(member["user_id"]),
                participant_status,
            )
            members.append((participant_status, member))

        protected_suffixes = (
            "workspace",
            "participants",
            "matches",
            "bracket",
            "bracket/events",
        )
        for participant_status, member in members:
            for suffix in protected_suffixes:
                with self.subTest(status=participant_status, suffix=suffix):
                    response = await member["client"].get(
                        f"/api/v1/tournaments/{slug}/{suffix}"
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertIn(
                        "Inactive tournament participants cannot access private tournament workspace data.",
                        response.json()["detail"],
                    )

        organizer_workspace = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/workspace"
        )
        self.assertEqual(organizer_workspace.status_code, 200, organizer_workspace.text)
        organizer_roster = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/participants"
        )
        self.assertEqual(organizer_roster.status_code, 200, organizer_roster.text)


if __name__ == "__main__":
    unittest.main()
