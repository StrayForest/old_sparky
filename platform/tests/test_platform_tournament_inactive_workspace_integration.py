from __future__ import annotations

import unittest
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentInvite,
    TournamentParticipant,
    User,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformTournamentInactiveWorkspaceIntegrationTests(PlatformIsolatedAsyncioTestCase):
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

    async def test_inactive_members_are_denied_on_all_private_tournament_child_reads(self) -> None:
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
                    json={"entry_type": "solo", "invite_code": invite["code"]},
                ),
                201,
            )
            await self._set_participant_status(
                slug,
                str(member["user_id"]),
                participant_status,
            )
            members.append((participant_status, member))

        async with session_factory()() as db_session:
            stored_invite = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.code == invite["code"])
            )
            self.assertIsNotNone(stored_invite)
            invite_use_count_before = stored_invite.use_count
            participant_state_before = {
                row.user_id: row.status
                for row in (
                    await db_session.scalars(
                        select(TournamentParticipant)
                        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
                        .where(Tournament.slug == slug)
                    )
                ).all()
            }

        # A valid, unrevoked and unexpired code is a read-only bearer grant for
        # exactly these current surfaces, including retained inactive users.
        bearer_read_suffixes = ("workspace", "participants", "matches", "bracket")
        for participant_status, member in members:
            for suffix in bearer_read_suffixes:
                with self.subTest(status=participant_status, suffix=f"bearer:{suffix}"):
                    response = await member["client"].get(
                        f"/api/v1/tournaments/{slug}/{suffix}",
                        params={"invite_code": invite["code"]},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
            with self.subTest(status=participant_status, suffix="bearer:summary"):
                response = await member["client"].get(
                    f"/api/v1/tournaments/{slug}",
                    params={"invite_code": invite["code"]},
                )
                self.assertEqual(response.status_code, 200, response.text)

        anonymous = await self._new_client()
        anonymous_summary = await anonymous.get(
            f"/api/v1/tournaments/{slug}",
            params={"invite_code": invite["code"]},
        )
        self.assertEqual(anonymous_summary.status_code, 200, anonymous_summary.text)
        for suffix in ("workspace", "participants", "matches", "bracket"):
            with self.subTest(status="anonymous", suffix=f"bearer:{suffix}"):
                response = await anonymous.get(
                    f"/api/v1/tournaments/{slug}/{suffix}",
                    params={"invite_code": invite["code"]},
                )
                self.assertEqual(response.status_code, 200, response.text)

        base_protected_suffixes = (
            "invites",
            "deadlock/ready-check",
            "deadlock/captain-preview",
            "deadlock/captain-round",
            "deadlock/auto-assignment",
        )
        for participant_status, member in members:
            protected_suffixes = base_protected_suffixes + (
                f"profiles/{member['user_id']}",
            )
            for suffix in protected_suffixes:
                with self.subTest(status=participant_status, suffix=suffix):
                    response = await member["client"].get(
                        f"/api/v1/tournaments/{slug}/{suffix}",
                        params={"invite_code": invite["code"]},
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertIn(
                        "Inactive tournament participants cannot access private tournament workspace data.",
                        response.json()["detail"],
                    )
            with self.subTest(status=participant_status, suffix="join:POST"):
                response = await member["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo", "invite_code": invite["code"]},
                    headers={"Idempotency-Key": f"inactive-{participant_status}-join"},
                )
                self.assertEqual(response.status_code, 403, response.text)
            with self.subTest(status=participant_status, suffix="join:DELETE"):
                response = await member["client"].delete(
                    f"/api/v1/tournaments/{slug}/join",
                )
                self.assertEqual(response.status_code, 403, response.text)
            with self.subTest(status=participant_status, suffix="invites/claim:POST"):
                response = await member["client"].post(
                    "/api/v1/tournaments/invites/claim",
                    json={"code": invite["code"], "entry_type": "solo", "team_name": None},
                )
                self.assertEqual(response.status_code, 403, response.text)
            with self.subTest(status=participant_status, suffix="participants/manage"):
                response = await member["client"].get(
                    f"/api/v1/tournaments/{slug}/participants/manage",
                    params={"invite_code": invite["code"]},
                )
                self.assertEqual(response.status_code, 403, response.text)

        async with session_factory()() as db_session:
            stored_invite = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.code == invite["code"])
            )
            self.assertIsNotNone(stored_invite)
            self.assertEqual(stored_invite.use_count, invite_use_count_before)
            participant_state_after = {
                row.user_id: row.status
                for row in (
                    await db_session.scalars(
                        select(TournamentParticipant)
                        .join(Tournament, Tournament.id == TournamentParticipant.tournament_id)
                        .where(Tournament.slug == slug)
                    )
                ).all()
            }
            self.assertEqual(participant_state_after, participant_state_before)

        organizer_workspace = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/workspace"
        )
        self.assertEqual(organizer_workspace.status_code, 200, organizer_workspace.text)
        organizer_roster = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/participants"
        )
        self.assertEqual(organizer_roster.status_code, 200, organizer_roster.text)

    async def test_bearer_validity_and_terminal_status_matrix(self) -> None:
        organizer = await self._register_user("matrix-organizer")
        tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-matrix",
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
        expired_invite = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/invites",
                json={"note": "expired", "max_uses": 1, "expires_at": None},
            ),
            201,
        )
        async with session_factory()() as db_session:
            expired_row = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.code == expired_invite["code"])
            )
            self.assertIsNotNone(expired_row)
            expired_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db_session.commit()

        revoked_invite = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/invites",
                json={"note": "revoked", "max_uses": 1, "expires_at": None},
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].delete(
                f"/api/v1/tournaments/{slug}/invites/{revoked_invite['id']}"
            ),
            204,
        )
        valid_invite = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/invites",
                json={"note": "terminal states", "max_uses": 1, "expires_at": None},
            ),
            201,
        )
        anonymous = await self._new_client()

        async def read_matrix(code: str) -> dict[str, int]:
            responses: dict[str, int] = {}
            for suffix in ("", "workspace", "participants", "matches", "bracket"):
                path = f"/api/v1/tournaments/{slug}{f'/{suffix}' if suffix else ''}"
                response = await anonymous.get(path, params={"invite_code": code})
                responses[suffix or "summary"] = response.status_code
            return responses

        for label, code in (
            ("wrong tournament", "ZZZZZZZZZZ"),
            ("expired", expired_invite["code"]),
            ("revoked", revoked_invite["code"]),
        ):
            with self.subTest(state=label):
                self.assertEqual(
                    await read_matrix(code),
                    {
                        "summary": 401,
                        "workspace": 401,
                        "participants": 401,
                        "matches": 401,
                        "bracket": 401,
                    },
                )

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        self.assertEqual(
            await read_matrix(valid_invite["code"]),
            {
                "summary": 200,
                "workspace": 200,
                "participants": 200,
                "matches": 200,
                "bracket": 200,
            },
        )
        async with session_factory()() as db_session:
            current_tournament = await db_session.scalar(
                select(Tournament).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(current_tournament)
            current_tournament.status = "completed"
            await db_session.commit()
        self.assertEqual(
            await read_matrix(valid_invite["code"]),
            {
                "summary": 200,
                "workspace": 200,
                "participants": 200,
                "matches": 200,
                "bracket": 200,
            },
        )


if __name__ == "__main__":
    unittest.main()
