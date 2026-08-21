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


class PlatformTournamentParticipantExclusionIntegrationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-exclude-{uuid4().hex[:8]}"
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
                    "display_name": f"exclude-{label}"[:15],
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
    ) -> tuple[dict, dict]:
        tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-{label}",
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
        invites = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/invites"
            ),
            200,
        )
        self.assertEqual(len(invites), 1)
        return tournament, invites[0]

    async def test_organizer_remove_retains_disqualification_and_blocks_rejoin(
        self,
    ) -> None:
        organizer = await self._register_user("organizer-a")
        other_organizer = await self._register_user("organizer-b")
        player = await self._register_user("player")

        tournament_a, invite_a = await self._create_open_private_tournament(
            organizer,
            "a",
        )
        slug_a = tournament_a["slug"]

        self._assert_status(
            await player["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={
                    "code": invite_a["code"],
                    "entry_type": "solo",
                    "team_name": None,
                },
            ),
            201,
        )
        joined = self._assert_status(
            await player["client"].post(
                f"/api/v1/tournaments/{slug_a}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        participant_id = joined["id"]

        removed = await organizer["client"].delete(
            f"/api/v1/tournaments/{slug_a}/participants/{participant_id}"
        )
        self.assertEqual(removed.status_code, 204, removed.text)
        self.assertEqual(removed.content, b"")

        async with session_factory()() as db_session:
            retained = await db_session.scalar(
                select(TournamentParticipant).where(
                    TournamentParticipant.id == participant_id
                )
            )
            self.assertIsNotNone(retained)
            self.assertEqual(retained.status, "disqualified")
            self.assertEqual(
                retained.moderated_by_user_id,
                organizer["user_id"],
            )
            self.assertIsNotNone(retained.moderated_at)

        active_roster = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug_a}/participants"
            ),
            200,
        )
        self.assertEqual(active_roster, [])

        management_roster = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug_a}/participants/manage"
            ),
            200,
        )
        self.assertEqual(len(management_roster), 1)
        self.assertEqual(management_roster[0]["id"], participant_id)
        self.assertEqual(management_roster[0]["status"], "disqualified")

        invites_before_retry = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug_a}/invites"
            ),
            200,
        )
        self.assertEqual(invites_before_retry[0]["use_count"], 1)

        rejected_claim = await player["client"].post(
            "/api/v1/tournaments/invites/claim",
            json={
                "code": invite_a["code"],
                "entry_type": "solo",
                "team_name": None,
            },
        )
        self.assertEqual(rejected_claim.status_code, 403, rejected_claim.text)
        self.assertIn("Disqualified", rejected_claim.json()["detail"])

        invites_after_retry = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug_a}/invites"
            ),
            200,
        )
        self.assertEqual(invites_after_retry[0]["use_count"], 1)

        rejected_rejoin = await player["client"].post(
            f"/api/v1/tournaments/{slug_a}/join",
            json={"entry_type": "solo"},
        )
        self.assertEqual(rejected_rejoin.status_code, 409, rejected_rejoin.text)
        self.assertIn("already registered", rejected_rejoin.json()["detail"])

        denied_workspace = await player["client"].get(
            f"/api/v1/tournaments/{slug_a}/workspace"
        )
        self.assertEqual(denied_workspace.status_code, 403, denied_workspace.text)

        tournament_b, invite_b = await self._create_open_private_tournament(
            other_organizer,
            "b",
        )
        slug_b = tournament_b["slug"]
        self._assert_status(
            await player["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={
                    "code": invite_b["code"],
                    "entry_type": "solo",
                    "team_name": None,
                },
            ),
            201,
        )
        joined_b = self._assert_status(
            await player["client"].post(
                f"/api/v1/tournaments/{slug_b}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        self.assertEqual(joined_b["user_id"], player["user_id"])

        restored = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug_a}/participants/{participant_id}/moderation",
                json={
                    "status": "registered",
                    "moderation_note": "Restored by organizer.",
                },
            ),
            200,
        )
        self.assertEqual(restored["status"], "registered")

        restored_roster = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug_a}/participants"
            ),
            200,
        )
        self.assertEqual(len(restored_roster), 1)
        self.assertEqual(restored_roster[0]["user_id"], player["user_id"])

        restored_workspace = await player["client"].get(
            f"/api/v1/tournaments/{slug_a}/workspace"
        )
        self.assertEqual(restored_workspace.status_code, 200, restored_workspace.text)


if __name__ == "__main__":
    unittest.main()
