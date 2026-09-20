from __future__ import annotations

import asyncio
import unittest
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import patch

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import tournament_workflow, tournament_write_serialization
from apps.platform_api.app.services.tournament_workflow import transition_locked_tournament_status
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentInvite,
    TournamentParticipant,
    User,
    new_uuid,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase

INACTIVE_PARTICIPANT_STATUSES = ("withdrawn", "disqualified")


class PlatformTournamentConcurrencyIntegrationTests(PlatformIsolatedAsyncioTestCase):
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

    async def _seed_tournament(
        self,
        *,
        organizer_user_id: str,
        suffix: str,
        visibility: str = "public",
        status: str = "registration_open",
        max_participants: int | None = None,
    ) -> tuple[str, str]:
        async with session_factory()() as db_session:
            tournament = Tournament(
                slug=f"{self.prefix}-{suffix}",
                name=f"{self.prefix} {suffix}",
                visibility=visibility,
                status=status,
                format_slug="solo",
                allowed_ranks=[],
                max_participants=max_participants,
                organizer_user_id=organizer_user_id,
            )
            db_session.add(tournament)
            await db_session.commit()
            return tournament.slug, tournament.id

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
        invite_code: str | None = None,
    ) -> httpx.Response:
        return await player["client"].post(
            f"/api/v1/tournaments/{slug}/join",
            json={"entry_type": "solo", "invite_code": invite_code},
        )

    async def _seed_published_roster(
        self,
        *,
        organizer_user_id: str,
        suffix: str,
    ) -> tuple[str, str, str]:
        """Create the smallest persisted roster graph used by lock races."""

        now = datetime.now(UTC)
        slug, _ = await self._seed_tournament(
            organizer_user_id=organizer_user_id,
            suffix=suffix,
            status="registration_open",
        )
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(
                select(Tournament).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament)
            ready_round = TournamentDeadlockReadyRound(
                tournament_id=tournament.id,
                status="closed",
                eligible_user_ids=[],
                initiated_by_user_id=organizer_user_id,
                closed_at=now,
            )
            db_session.add(ready_round)
            await db_session.flush()
            captain_round = TournamentDeadlockCaptainRound(
                tournament_id=tournament.id,
                source_ready_round_id=ready_round.id,
                teams_count=2,
                status="finalized",
                initiated_by_user_id=organizer_user_id,
                closed_at=now,
                finalized_at=now,
            )
            db_session.add(captain_round)
            await db_session.flush()
            run = TournamentDeadlockAssignmentRun(
                id=new_uuid(),
                tournament_id=tournament.id,
                source_captain_round_id=captain_round.id,
                source_ready_round_id=ready_round.id,
                created_by_user_id=organizer_user_id,
                status="published",
                published_at=now,
                published_by_user_id=organizer_user_id,
                summary_text="Two-session roster lock race fixture.",
                result_snapshot={"teams": []},
                candidate_pool_user_ids=[],
                leftover_user_ids=[],
            )
            db_session.add(run)
            await db_session.commit()
            return slug, tournament.id, run.id

    async def _close_and_lock_roster(
        self,
        *,
        tournament_id: str,
        run_id: str,
        acquired: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        """Use the production parent-row lock order for a tiny roster handoff."""

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(
                select(Tournament)
                .where(Tournament.id == tournament_id)
                .with_for_update()
            )
            self.assertIsNotNone(tournament)
            run = await db_session.scalar(
                select(TournamentDeadlockAssignmentRun)
                .where(
                    TournamentDeadlockAssignmentRun.id == run_id,
                    TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
                )
                .with_for_update()
            )
            self.assertIsNotNone(run)
            transition = await transition_locked_tournament_status(
                db_session,
                tournament=tournament,
                next_status="registration_closed",
                now=datetime.now(UTC),
                actor_user_id=tournament.organizer_user_id,
                audit_action="test.tournament.close_before_roster_lock",
                expected_status="registration_open",
            )
            run.status = "locked"
            run.locked_at = datetime.now(UTC)
            run.locked_by_user_id = tournament.organizer_user_id
            await db_session.flush()
            if acquired is not None:
                acquired.set()
            if release is not None:
                await release.wait()
            await db_session.commit()
            self.assertEqual(transition.to_status, "registration_closed")

    async def test_join_waits_for_close_and_cannot_commit_after_registration_closes(
        self,
    ) -> None:
        organizer = await self._register_user("join-close-organizer")
        player = await self._register_user("join-close-player")
        slug, _ = await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="join-close",
            status="registration_open",
        )

        join_lock_called = asyncio.Event()
        close_lock_called = asyncio.Event()
        close_lock_acquired = asyncio.Event()
        close_release = asyncio.Event()
        original_dependency_lock = tournament_write_serialization._lock_tournament
        original_workflow_lock = tournament_workflow.lock_tournament_for_workflow

        async def gated_dependency_lock(*args: Any, **kwargs: Any):
            if kwargs.get("slug") == slug:
                join_lock_called.set()
            return await original_dependency_lock(*args, **kwargs)

        async def gated_workflow_lock(*args: Any, **kwargs: Any):
            close_lock_called.set()
            tournament = await original_workflow_lock(*args, **kwargs)
            close_lock_acquired.set()
            await close_release.wait()
            return tournament

        async with session_factory()() as blocker:
            tournament_id = await blocker.scalar(
                select(Tournament.id).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament_id)
            await blocker.execute(
                select(Tournament.id)
                .where(Tournament.id == tournament_id)
                .with_for_update()
            )
            close_task: asyncio.Task[httpx.Response] | None = None
            join_task: asyncio.Task[httpx.Response] | None = None
            try:
                with (
                    patch.object(
                        tournament_write_serialization,
                        "_lock_tournament",
                        side_effect=gated_dependency_lock,
                    ),
                    patch.object(
                        tournament_workflow,
                        "lock_tournament_for_workflow",
                        side_effect=gated_workflow_lock,
                    ),
                ):
                    close_task = asyncio.create_task(
                        organizer["client"].patch(
                            f"/api/v1/tournaments/{slug}/status",
                            json={"status": "registration_closed"},
                        )
                    )
                    await asyncio.wait_for(close_lock_called.wait(), timeout=5)
                    await blocker.commit()
                    await asyncio.wait_for(close_lock_acquired.wait(), timeout=5)
                    # The close transaction now owns Tournament.  Start the
                    # join only after that ownership is observed, making the
                    # winner deterministic rather than relying on lock waiter
                    # ordering.
                    join_task = asyncio.create_task(self._join(player, slug))
                    await asyncio.wait_for(join_lock_called.wait(), timeout=5)
                    close_release.set()
                    close_response, join_response = await asyncio.wait_for(
                        asyncio.gather(close_task, join_task),
                        timeout=5,
                    )
            finally:
                close_release.set()
                pending_tasks = [
                    task
                    for task in (close_task, join_task)
                    if task is not None and not task.done()
                ]
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

        self.assertEqual(close_response.status_code, 200, close_response.text)
        self.assertEqual(join_response.status_code, 409, join_response.text)
        self.assertEqual(await self._participant_count(slug), 0)

    async def test_leave_waits_for_close_and_serializes_after_registration_closes(
        self,
    ) -> None:
        organizer = await self._register_user("leave-close-organizer")
        player = await self._register_user("leave-close-player")
        slug, tournament_id = await self._seed_tournament(
            organizer_user_id=organizer["user_id"],
            suffix="leave-close",
            status="registration_open",
        )
        joined = await self._join(player, slug)
        self.assertEqual(joined.status_code, 201, joined.text)

        close_lock_called = asyncio.Event()
        close_lock_acquired = asyncio.Event()
        close_release = asyncio.Event()
        leave_lock_called = asyncio.Event()
        original_dependency_lock = tournament_write_serialization._lock_tournament
        original_workflow_lock = tournament_workflow.lock_tournament_for_workflow

        async def gated_dependency_lock(*args: Any, **kwargs: Any):
            if kwargs.get("slug") == slug:
                leave_lock_called.set()
            return await original_dependency_lock(*args, **kwargs)

        async def gated_workflow_lock(*args: Any, **kwargs: Any):
            close_lock_called.set()
            tournament = await original_workflow_lock(*args, **kwargs)
            close_lock_acquired.set()
            await close_release.wait()
            return tournament

        async with session_factory()() as blocker:
            await blocker.execute(
                select(Tournament.id)
                .where(Tournament.id == tournament_id)
                .with_for_update()
            )
            close_task: asyncio.Task[httpx.Response] | None = None
            leave_task: asyncio.Task[httpx.Response] | None = None
            try:
                with (
                    patch.object(
                        tournament_write_serialization,
                        "_lock_tournament",
                        side_effect=gated_dependency_lock,
                    ),
                    patch.object(
                        tournament_workflow,
                        "lock_tournament_for_workflow",
                        side_effect=gated_workflow_lock,
                    ),
                ):
                    close_task = asyncio.create_task(
                        organizer["client"].patch(
                            f"/api/v1/tournaments/{slug}/status",
                            json={"status": "registration_closed"},
                        )
                    )
                    await asyncio.wait_for(close_lock_called.wait(), timeout=5)
                    await blocker.commit()
                    await asyncio.wait_for(close_lock_acquired.wait(), timeout=5)
                    leave_task = asyncio.create_task(
                        player["client"].delete(f"/api/v1/tournaments/{slug}/join")
                    )
                    await asyncio.wait_for(leave_lock_called.wait(), timeout=5)
                    close_release.set()
                    close_response, leave_response = await asyncio.wait_for(
                        asyncio.gather(close_task, leave_task),
                        timeout=5,
                    )
            finally:
                close_release.set()
                pending_tasks = [
                    task
                    for task in (close_task, leave_task)
                    if task is not None and not task.done()
                ]
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)

        self.assertEqual(close_response.status_code, 200, close_response.text)
        # Leaving remains an allowed withdrawal after registration closes, but
        # it must linearize after the close rather than racing its stale read.
        self.assertEqual(leave_response.status_code, 204, leave_response.text)
        self.assertEqual(await self._participant_count(slug), 0)

    async def test_join_waits_for_roster_close_and_lock_without_late_membership(self) -> None:
        organizer = await self._register_user("join-roster-organizer")
        player = await self._register_user("join-roster-player")
        slug, tournament_id, run_id = await self._seed_published_roster(
            organizer_user_id=organizer["user_id"],
            suffix="join-roster",
        )
        roster_acquired = asyncio.Event()
        roster_release = asyncio.Event()
        join_lock_called = asyncio.Event()
        original_dependency_lock = tournament_write_serialization._lock_tournament

        async def gated_dependency_lock(*args: Any, **kwargs: Any):
            if kwargs.get("slug") == slug:
                join_lock_called.set()
            return await original_dependency_lock(*args, **kwargs)

        roster_task = asyncio.create_task(
            self._close_and_lock_roster(
                tournament_id=tournament_id,
                run_id=run_id,
                acquired=roster_acquired,
                release=roster_release,
            )
        )
        try:
            await asyncio.wait_for(roster_acquired.wait(), timeout=5)
            with patch.object(
                tournament_write_serialization,
                "_lock_tournament",
                side_effect=gated_dependency_lock,
            ):
                join_task = asyncio.create_task(self._join(player, slug))
                await asyncio.wait_for(join_lock_called.wait(), timeout=5)
                roster_release.set()
                join_response, _ = await asyncio.wait_for(
                    asyncio.gather(join_task, roster_task),
                    timeout=5,
                )
        finally:
            roster_release.set()
            await asyncio.gather(roster_task, return_exceptions=True)

        self.assertEqual(join_response.status_code, 409, join_response.text)
        self.assertEqual(await self._participant_count(slug), 0)
        async with session_factory()() as db_session:
            persisted_run = await db_session.get(
                TournamentDeadlockAssignmentRun,
                run_id,
            )
            persisted_tournament = await db_session.get(Tournament, tournament_id)
        self.assertIsNotNone(persisted_run)
        self.assertEqual(persisted_run.status, "locked")
        self.assertIsNotNone(persisted_tournament)
        self.assertEqual(persisted_tournament.status, "registration_closed")

    async def test_leave_waits_for_roster_close_and_lock_without_losing_membership(self) -> None:
        organizer = await self._register_user("leave-roster-organizer")
        player = await self._register_user("leave-roster-player")
        slug, tournament_id, run_id = await self._seed_published_roster(
            organizer_user_id=organizer["user_id"],
            suffix="leave-roster",
        )
        joined = await self._join(player, slug)
        self.assertEqual(joined.status_code, 201, joined.text)

        roster_acquired = asyncio.Event()
        roster_release = asyncio.Event()
        leave_lock_called = asyncio.Event()
        original_dependency_lock = tournament_write_serialization._lock_tournament

        async def gated_dependency_lock(*args: Any, **kwargs: Any):
            if kwargs.get("slug") == slug:
                leave_lock_called.set()
            return await original_dependency_lock(*args, **kwargs)

        roster_task = asyncio.create_task(
            self._close_and_lock_roster(
                tournament_id=tournament_id,
                run_id=run_id,
                acquired=roster_acquired,
                release=roster_release,
            )
        )
        try:
            await asyncio.wait_for(roster_acquired.wait(), timeout=5)
            with patch.object(
                tournament_write_serialization,
                "_lock_tournament",
                side_effect=gated_dependency_lock,
            ):
                leave_task = asyncio.create_task(
                    player["client"].delete(f"/api/v1/tournaments/{slug}/join")
                )
                await asyncio.wait_for(leave_lock_called.wait(), timeout=5)
                roster_release.set()
                leave_response, _ = await asyncio.wait_for(
                    asyncio.gather(leave_task, roster_task),
                    timeout=5,
                )
        finally:
            roster_release.set()
            await asyncio.gather(roster_task, return_exceptions=True)

        self.assertEqual(leave_response.status_code, 409, leave_response.text)
        self.assertEqual(await self._participant_count(slug), 1)
        async with session_factory()() as db_session:
            participant = await db_session.scalar(
                select(TournamentParticipant).where(
                    TournamentParticipant.tournament_id == tournament_id,
                    TournamentParticipant.user_id == player["user_id"],
                )
            )
        self.assertIsNotNone(participant)
        self.assertEqual(participant.status, "registered")

    async def test_concurrent_invite_claims_are_read_only(self) -> None:
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
            await asyncio.wait_for(
                blocker.execute(
                    text(
                        "LOCK TABLE platform.tournament_invites "
                        "IN ACCESS EXCLUSIVE MODE"
                    )
                ),
                timeout=5.0,
            )

            query_boundary = asyncio.Event()
            invite_query_count = 0
            original_scalar = AsyncSession.scalar

            async def observe_invite_query(
                session: AsyncSession,
                statement: object,
                *args: object,
                **kwargs: object,
            ) -> object:
                nonlocal invite_query_count
                if "tournament_invites" in str(statement):
                    invite_query_count += 1
                    if invite_query_count == 2:
                        query_boundary.set()
                return await original_scalar(session, statement, *args, **kwargs)

            tasks: list[asyncio.Task[httpx.Response]] = []
            responses: tuple[httpx.Response, httpx.Response]
            try:
                # The policy dependency's scalar SELECT is the first actual
                # database query for each claim.  The ACCESS EXCLUSIVE lock
                # makes both requests wait at that boundary, so coroutine
                # scheduling cannot produce a false concurrency proof.
                with patch.object(AsyncSession, "scalar", new=observe_invite_query):
                    tasks = [
                        asyncio.create_task(
                            self._claim_invite(player_a, invite["code"])
                        ),
                        asyncio.create_task(
                            self._claim_invite(player_b, invite["code"])
                        ),
                    ]
                    try:
                        await asyncio.wait_for(query_boundary.wait(), timeout=5.0)
                        await asyncio.sleep(0)
                        self.assertEqual(invite_query_count, 2)
                        self.assertTrue(
                            all(not task.done() for task in tasks),
                            "both invite requests must remain blocked before lock release",
                        )
                        await asyncio.wait_for(blocker.commit(), timeout=5.0)
                        responses = await asyncio.wait_for(
                            asyncio.gather(*tasks),
                            timeout=5.0,
                        )
                    finally:
                        if blocker.in_transaction():
                            await blocker.rollback()
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if blocker.in_transaction():
                    await blocker.rollback()

        self.assertEqual(
            sorted(response.status_code for response in responses),
            [201, 201],
            [response.text for response in responses],
        )

        async with session_factory()() as db_session:
            stored_invite = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.id == invite_id)
            )
            self.assertIsNotNone(stored_invite)
            self.assertEqual(stored_invite.use_count, 0)
            participant_count = await db_session.scalar(
                select(func.count(TournamentParticipant.id)).where(
                    TournamentParticipant.tournament_id == tournament["id"]
                )
            )
            self.assertEqual(participant_count, 0)

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
        managed_invite = await self._create_invite(organizer, slug)
        self._assert_status(
            await self._claim_invite(managed_player, managed_invite["code"]),
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
                    asyncio.create_task(self._join(self_joiner, slug, invite["code"])),
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
        joined_a = self._assert_status(await self._join(player_a, slug, invite_a["code"]), 201)
        removed = await organizer["client"].delete(
            f"/api/v1/tournaments/{slug}/participants/{joined_a['id']}"
        )
        self.assertEqual(removed.status_code, 204, removed.text)

        invite_b = await self._create_invite(organizer, slug)
        self._assert_status(await self._claim_invite(player_b, invite_b["code"]), 201)
        self._assert_status(await self._join(player_b, slug, invite_b["code"]), 201)

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
