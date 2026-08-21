from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select, text

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentInvite,
    TournamentInviteAccess,
    TournamentParticipant,
    User,
)


INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")


class PlatformTournamentConcurrencyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-as03-{uuid4().hex[:8]}"
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
                base_url=self.base_url,
            )
        )

    def _assert_status(
        self,
        response: httpx.Response,
        expected_status: int,
    ) -> dict:
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
                    "display_name": f"as03-{label}"[:15],
                },
            ),
            201,
        )
        return {
            "client": client,
            "user_id": payload["user"]["id"],
            "email": email,
        }

    async def _create_open_private_tournament(
        self,
        organizer: dict[str, object],
        label: str,
        *,
        max_participants: int | None = None,
    ) -> tuple[dict, dict]:
        tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-{label}",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "max_participants": max_participants,
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
        invites = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/invites"
            ),
            200,
        )
        self.assertEqual(len(invites), 1)
        return tournament, invites[0]

    async def _create_invite(
        self,
        organizer: dict[str, object],
        slug: str,
        *,
        max_uses: int = 1,
    ) -> dict:
        return self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/invites",
                json={"max_uses": max_uses},
            ),
            201,
        )

    async def _claim_invite(
        self,
        player: dict[str, object],
        code: str,
    ) -> httpx.Response:
        return await player["client"].post(
            "/api/v1/tournaments/invites/claim",
            json={
                "code": code,
                "entry_type": "solo",
                "team_name": None,
            },
        )

    async def _join(
        self,
        player: dict[str, object],
        slug: str,
    ) -> httpx.Response:
        return await player["client"].post(
            f"/api/v1/tournaments/{slug}/join",
            json={"entry_type": "solo"},
        )

    async def test_last_invite_use_is_serialized_before_claim_state_is_read(self) -> None:
        organizer = await self._register_user("invite-organizer")
        player_a = await self._register_user("invite-a")
        player_b = await self._register_user("invite-b")
        tournament, _automatic_invite = await self._create_open_private_tournament(
            organizer,
            "ir",
        )
        invite = await self._create_invite(
            organizer,
            tournament["slug"],
            max_uses=1,
        )

        async with session_factory()() as blocker:
            invite_id = await blocker.scalar(
                select(TournamentInvite.id).where(
                    TournamentInvite.code == invite["code"]
                )
            )
            self.assertIsNotNone(invite_id)
            await blocker.execute(
                select(TournamentInvite.id)
                .where(TournamentInvite.id == invite_id)
                .with_for_update()
            )

            tasks = [
                asyncio.create_task(self._claim_invite(player_a, invite["code"])),
                asyncio.create_task(self._claim_invite(player_b, invite["code"])),
            ]
            try:
                await asyncio.sleep(0.2)
            finally:
                await blocker.commit()
            responses = await asyncio.gather(*tasks)

        self.assertEqual(
            sorted(response.status_code for response in responses),
            [201, 409],
            [response.text for response in responses],
        )

        async with session_factory()() as db_session:
            stored_invite = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.id == invite_id)
            )
            self.assertIsNotNone(stored_invite)
            self.assertEqual(stored_invite.use_count, 1)
            access_count = int(
                await db_session.scalar(
                    select(func.count())
                    .select_from(TournamentInviteAccess)
                    .where(
                        TournamentInviteAccess.tournament_id == tournament["id"]
                    )
                )
                or 0
            )
            self.assertEqual(access_count, 1)

    async def test_last_participant_slot_serializes_self_join_and_organizer_add(
        self,
    ) -> None:
        organizer = await self._register_user("capacity-organizer")
        self_joiner = await self._register_user("capacity-self")
        managed_player = await self._register_user("capacity-managed")
        tournament, invite = await self._create_open_private_tournament(
            organizer,
            "cr",
            max_participants=1,
        )
        slug = tournament["slug"]
        self._assert_status(
            await self._claim_invite(self_joiner, invite["code"]),
            201,
        )

        async with session_factory()() as blocker:
            await blocker.execute(
                select(Tournament.id)
                .where(Tournament.id == tournament["id"])
                .with_for_update()
            )
            await blocker.execute(
                text("LOCK TABLE platform.tournament_participants IN SHARE MODE")
            )

            tasks = [
                asyncio.create_task(self._join(self_joiner, slug)),
                asyncio.create_task(
                    organizer["client"].post(
                        f"/api/v1/tournaments/{slug}/participants/manage",
                        json={
                            "user_email": managed_player["email"],
                            "entry_type": "solo",
                            "team_name": None,
                        },
                    )
                ),
            ]
            try:
                await asyncio.sleep(0.2)
            finally:
                await blocker.commit()
            responses = await asyncio.gather(*tasks)

        self.assertEqual(
            sorted(response.status_code for response in responses),
            [201, 409],
            [response.text for response in responses],
        )

        async with session_factory()() as db_session:
            active_count = int(
                await db_session.scalar(
                    select(func.count())
                    .select_from(TournamentParticipant)
                    .where(
                        TournamentParticipant.tournament_id == tournament["id"],
                        TournamentParticipant.status.not_in(
                            INACTIVE_PARTICIPANT_STATUSES
                        ),
                    )
                )
                or 0
            )
            self.assertEqual(active_count, 1)

    async def test_inactive_participant_cannot_be_restored_over_capacity(self) -> None:
        organizer = await self._register_user("restore-organizer")
        player_a = await self._register_user("restore-a")
        player_b = await self._register_user("restore-b")
        tournament, invite_a = await self._create_open_private_tournament(
            organizer,
            "rc",
            max_participants=1,
        )
        slug = tournament["slug"]

        self._assert_status(await self._claim_invite(player_a, invite_a["code"]), 201)
        joined_a = self._assert_status(await self._join(player_a, slug), 201)
        removed = await organizer["client"].delete(
            f"/api/v1/tournaments/{slug}/participants/{joined_a['id']}"
        )
        self.assertEqual(removed.status_code, 204, removed.text)

        invite_b = await self._create_invite(organizer, slug)
        self._assert_status(await self._claim_invite(player_b, invite_b["code"]), 201)
        self._assert_status(await self._join(player_b, slug), 201)

        restore = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/participants/{joined_a['id']}/moderation",
            json={
                "status": "registered",
                "moderation_note": "Capacity regression.",
            },
        )
        self.assertEqual(restore.status_code, 409, restore.text)
        self.assertIn("participant limit", restore.json()["detail"].lower())

        async with session_factory()() as db_session:
            restored_status = await db_session.scalar(
                select(TournamentParticipant.status).where(
                    TournamentParticipant.id == joined_a["id"]
                )
            )
            self.assertEqual(restored_status, "disqualified")
            active_count = int(
                await db_session.scalar(
                    select(func.count())
                    .select_from(TournamentParticipant)
                    .where(
                        TournamentParticipant.tournament_id == tournament["id"],
                        TournamentParticipant.status.not_in(
                            INACTIVE_PARTICIPANT_STATUSES
                        ),
                    )
                )
                or 0
            )
            self.assertEqual(active_count, 1)


if __name__ == "__main__":
    unittest.main()
