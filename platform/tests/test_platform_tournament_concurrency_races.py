from __future__ import annotations
import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select

from apps.platform_api.app.api.routes import tournaments as tournament_routes
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import deadlock_automation, tournament_workflow
from apps.platform_api.app.services.deadlock_automation import (
    advance_deadlock_tournament_automation,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentInvite,
    TournamentInviteAccess,
    TournamentParticipant,
    User,
)


class PlatformTournamentConcurrencyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """PostgreSQL transaction-level races for tournament workflow writers."""

    async def asyncSetUp(self) -> None:
        self.prefix = f"it-concurrency-{uuid4().hex[:8]}"
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

    async def _register_user(self, label: str) -> dict[str, Any]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": self.password,
                "display_name": f"test-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        return {
            "client": client,
            "email": email,
            "user_id": payload["user"]["id"],
        }

    async def _seed_tournament(
        self,
        *,
        organizer_user_id: str,
        suffix: str,
        visibility: str = "public",
        status: str = "registration_open",
        max_participants: int | None = None,
        registration_starts_at: datetime | None = None,
        registration_closes_at: datetime | None = None,
        invite_code: str | None = None,
        invite_max_uses: int = 1,
    ) -> tuple[str, str | None]:
        async with session_factory()() as db_session:
            tournament = Tournament(
                slug=f"{self.prefix}-{suffix}",
                name=f"{self.prefix} {suffix}",
                visibility=visibility,
                status=status,
                format_slug="solo",
                allowed_ranks=[],
                max_participants=max_participants,
                registration_starts_at=registration_starts_at,
                registration_closes_at=registration_closes_at,
                organizer_user_id=organizer_user_id,
            )
            db_session.add(tournament)
            await db_session.flush()

            if invite_code is not None:
                db_session.add(
                    TournamentInvite(
                        tournament_id=tournament.id,
                        code=invite_code,
                        max_uses=invite_max_uses,
                        use_count=0,
                        created_by_user_id=organizer_user_id,
                    )
                )
            await db_session.commit()
            return tournament.slug, invite_code

    async def _participant_count(self, slug: str) -> int:
        async with session_factory()() as db_session:
            tournament_id = await db_session.scalar(
                select(Tournament.id).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament_id)
            return int(
                await db_session.scalar(
                    select(func.count(TournamentParticipant.id)).where(
                        TournamentParticipant.tournament_id == tournament_id
                    )
                )
                or 0
            )

    async def _invite_state(self, code: str) -> tuple[int, int]:
        async with session_factory()() as db_session:
            invite = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.code == code)
            )
            self.assertIsNotNone(invite)
            access_count = await db_session.scalar(
                select(func.count(TournamentInviteAccess.id)).where(
                    TournamentInviteAccess.invite_id == invite.id
                )
            )
            return int(invite.use_count), int(access_count or 0)

    async def _automation_once(self, slug: str, now: datetime):
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(
                select(Tournament).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament)
            return await advance_deadlock_tournament_automation(
                db_session,
                tournament=tournament,
                now=now,
            )

    async def test_concurrent_invite_claims_consume_one_use(self) -> None:
        organizer = await self._register_user("invite-organizer")
        first_player = await self._register_user("invite-first")
        second_player = await self._register_user("invite-second")
        code = f"{self.prefix.replace('-', '')[:16]}A1".upper()
        slug, _ = await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="invite",
            visibility="invite_only",
            invite_code=code,
            invite_max_uses=1,
        )

        async def claim(player: dict[str, Any]) -> httpx.Response:
            return await player["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={"code": code, "entry_type": "solo", "team_name": None},
            )

        with patch.object(tournament_routes, "check_invite_rate_limit", new=AsyncMock()):
            first_response, second_response = await asyncio.gather(
                claim(first_player),
                claim(second_player),
            )

        self.assertEqual(sorted((first_response.status_code, second_response.status_code)), [201, 409])
        self.assertEqual(await self._invite_state(code), (1, 1))
        self.assertEqual(await self._participant_count(slug), 0)

    async def test_closed_registration_invite_claim_does_not_grant_access(self) -> None:
        organizer = await self._register_user("closed-invite-organizer")
        player = await self._register_user("closed-invite-player")
        code = f"{self.prefix.replace('-', '')[:16]}C1".upper()
        await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="closed-invite",
            visibility="invite_only",
            status="registration_closed",
            invite_code=code,
        )

        with patch.object(tournament_routes, "check_invite_rate_limit", new=AsyncMock()):
            response = await player["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={"code": code, "entry_type": "solo", "team_name": None},
            )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(await self._invite_state(code), (0, 0))


    async def test_concurrent_self_joins_serialize_capacity_check(self) -> None:
        organizer = await self._register_user("join-organizer")
        first_player = await self._register_user("join-first")
        second_player = await self._register_user("join-second")
        slug, _ = await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="join",
            max_participants=1,
        )

        async def join(player: dict[str, Any]) -> httpx.Response:
            return await player["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            )

        first_response, second_response = await asyncio.gather(
            join(first_player),
            join(second_player),
        )

        self.assertEqual(sorted((first_response.status_code, second_response.status_code)), [201, 409])
        self.assertEqual(await self._participant_count(slug), 1)

    async def test_concurrent_organizer_adds_serialize_capacity_check(self) -> None:
        organizer = await self._register_user("manage-organizer")
        first_player = await self._register_user("manage-first")
        second_player = await self._register_user("manage-second")
        slug, _ = await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="manage",
            max_participants=1,
        )

        async def add_player(player: dict[str, Any]) -> httpx.Response:
            return await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/participants/manage",
                json={
                    "user_email": player["email"],
                    "entry_type": "solo",
                },
            )

        first_response, second_response = await asyncio.gather(
            add_player(first_player),
            add_player(second_player),
        )

        self.assertEqual(sorted((first_response.status_code, second_response.status_code)), [201, 409])
        self.assertEqual(await self._participant_count(slug), 1)

    async def test_status_update_and_automation_use_deterministic_row_lock(self) -> None:
        organizer = await self._register_user("status-organizer")
        now = datetime.now(UTC).replace(microsecond=0)
        slug, _ = await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="status",
            status="registration_closed",
            registration_starts_at=now - timedelta(minutes=1),
            registration_closes_at=now + timedelta(hours=1),
        )

        first_locked = asyncio.Event()
        second_waiting = asyncio.Event()
        release_first = asyncio.Event()
        original_lock = tournament_workflow.lock_tournament_for_workflow
        call_count = 0

        async def gated_lock(*args: Any, **kwargs: Any):
            nonlocal call_count
            call_count += 1
            lock_call = call_count
            if lock_call == 2:
                second_waiting.set()
            tournament = await original_lock(*args, **kwargs)
            if lock_call == 1:
                first_locked.set()
                await release_first.wait()
            return tournament

        automation_task = asyncio.create_task(self._automation_once(slug, now))
        status_task: asyncio.Task[httpx.Response] | None = None
        try:
            with (
                patch.object(
                    tournament_workflow,
                    "lock_tournament_for_workflow",
                    side_effect=gated_lock,
                ),
                patch.object(
                    deadlock_automation,
                    "lock_tournament_for_workflow",
                    side_effect=gated_lock,
                ),
            ):
                await asyncio.wait_for(first_locked.wait(), timeout=10)
                status_task = asyncio.create_task(
                    organizer["client"].patch(
                        f"/api/v1/tournaments/{slug}/status",
                        json={"status": "cancelled"},
                    )
                )
                await asyncio.wait_for(second_waiting.wait(), timeout=10)
                release_first.set()
                automation_result, status_response = await asyncio.wait_for(
                    asyncio.gather(automation_task, status_task),
                    timeout=10,
                )
        finally:
            release_first.set()
            tasks = [automation_task]
            if status_task is not None:
                tasks.append(status_task)
            await asyncio.gather(*tasks, return_exceptions=True)

        self.assertEqual(automation_result.registration_opened, 1)
        self.assertEqual(status_response.status_code, 200, status_response.text)

        async with session_factory()() as db_session:
            final_status = await db_session.scalar(
                select(Tournament.status).where(Tournament.slug == slug)
            )
        self.assertEqual(final_status, "cancelled")


if __name__ == "__main__":
    unittest.main()
