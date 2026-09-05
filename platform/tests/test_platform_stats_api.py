from __future__ import annotations

from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    DeadlockProfile,
    Tournament,
    TournamentMatch,
    TournamentParticipant,
    User,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformStatsApiTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-stats-{uuid4().hex[:8]}"
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

    async def _seed_stats_fixture(self) -> None:
        async with session_factory()() as db_session:
            organizer = User(
                email=f"{self.prefix}-organizer@example.com",
                display_name=f"{self.prefix}-organizer",
            )
            player_one = User(
                email=f"{self.prefix}-player-one@example.com",
                display_name=f"{self.prefix}-player-one",
            )
            player_two = User(
                email=f"{self.prefix}-player-two@example.com",
                display_name=f"{self.prefix}-player-two",
            )
            db_session.add_all([organizer, player_one, player_two])
            await db_session.flush()

            completed = Tournament(
                slug=f"{self.prefix}-completed",
                name=f"{self.prefix} completed",
                description="Completed stats fixture",
                visibility="public",
                status="completed",
                format_slug="solo",
                organizer_user_id=organizer.id,
            )
            active = Tournament(
                slug=f"{self.prefix}-active",
                name=f"{self.prefix} active",
                description="Active stats fixture",
                visibility="public",
                status="registration_open",
                format_slug="solo",
                organizer_user_id=organizer.id,
            )
            db_session.add_all([completed, active])
            await db_session.flush()

            db_session.add_all(
                [
                    TournamentParticipant(
                        tournament_id=active.id,
                        user_id=player_one.id,
                        status="registered",
                        entry_type="solo",
                    ),
                    TournamentParticipant(
                        tournament_id=active.id,
                        user_id=player_two.id,
                        status="withdrawn",
                        entry_type="solo",
                    ),
                    TournamentMatch(
                        tournament_id=completed.id,
                        round_number=1,
                        sequence_number=1,
                        home_label="Team 1",
                        away_label="Team 2",
                        status="completed",
                        home_score=2,
                        away_score=0,
                        winner_side="home",
                    ),
                    DeadlockProfile(
                        user_id=player_one.id,
                        rank="Oracle",
                        subrank=4,
                        playtime="1001-1500",
                        roles=["Carry"],
                        pool=["Abrams"],
                        captain_priority="neutral",
                    ),
                    DeadlockProfile(
                        user_id=player_two.id,
                        rank="Phantom",
                        subrank=2,
                        playtime="501-1000",
                        roles=["Support"],
                        pool=["Ivy"],
                        captain_priority="neutral",
                    ),
                ]
            )
            await db_session.commit()

    async def test_stats_overview_is_public_and_counts_platform_data(self) -> None:
        await self._seed_stats_fixture()
        client = await self._new_client()

        response = await client.get("/api/v1/stats/overview")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()

        self.assertGreaterEqual(payload["total_tournaments"], 2)
        self.assertGreaterEqual(payload["completed_tournaments"], 1)
        self.assertGreaterEqual(payload["active_upcoming_tournaments"], 1)
        self.assertGreaterEqual(payload["registered_participants"], 1)
        self.assertGreaterEqual(payload["completed_matches"], 1)
        self.assertGreaterEqual(payload["deadlock_profiles_total"], 2)
        self.assertGreaterEqual(payload["registered_participants_with_deadlock_profile"], 1)
        self.assertGreaterEqual(payload["deadlock_profile_coverage_percent"], 0.0)
        self.assertLessEqual(payload["deadlock_profile_coverage_percent"], 100.0)

        rank_counts = {item["rank"]: item["count"] for item in payload["deadlock_rank_distribution"]}
        self.assertGreaterEqual(rank_counts.get("Oracle", 0), 1)
        self.assertGreaterEqual(rank_counts.get("Phantom", 0), 1)
