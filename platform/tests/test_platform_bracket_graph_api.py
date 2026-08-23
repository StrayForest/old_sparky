from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.bracket_events import (
    publish_bracket_event,
    stream_bracket_events,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Role,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    User,
    UserRole,
)


class PlatformBracketGraphApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-bg-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
        self.app = create_app()
        self.clients = AsyncExitStack()
        await self._cleanup()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        await self._cleanup()
        await dispose_engine()

    async def _cleanup(self) -> None:
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

    async def _client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )

    def _payload(self, response: httpx.Response, status_code: int):
        self.assertEqual(response.status_code, status_code, response.text)
        return response.json() if response.content else None

    async def _register(self, label: str) -> dict[str, object]:
        client = await self._client()
        payload = self._payload(
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"{self.prefix}-{label}@example.com",
                    "password": self.password,
                    "display_name": f"test-{label}"[:15],
                },
            ),
            201,
        )
        return {"client": client, "user_id": payload["user"]["id"]}

    async def _grant_role(self, user_id: str, role_slug: str) -> None:
        async with session_factory()() as db_session:
            role = await db_session.scalar(
                select(Role).where(Role.slug == role_slug)
            )
            self.assertIsNotNone(role)
            db_session.add(UserRole(user_id=user_id, role_id=role.id))
            await db_session.commit()

    async def _grant_public_creation(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user)
            user.public_tournament_credits = 100
            await db_session.commit()

    async def _lock_eight_team_roster(
        self,
        *,
        organizer_user_id: str,
        slug: str,
    ) -> None:
        now = datetime.now(UTC)
        strengths = (800, 700, 600, 500, 400, 300, 200, 100)
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
                teams_count=8,
                status="finalized",
                initiated_by_user_id=organizer_user_id,
                closed_at=now,
                finalized_at=now,
            )
            db_session.add(captain_round)
            await db_session.flush()
            db_session.add(
                TournamentDeadlockAssignmentRun(
                    tournament_id=tournament.id,
                    source_captain_round_id=captain_round.id,
                    source_ready_round_id=ready_round.id,
                    created_by_user_id=organizer_user_id,
                    status="locked",
                    published_at=now,
                    published_by_user_id=organizer_user_id,
                    locked_at=now,
                    locked_by_user_id=organizer_user_id,
                    summary_text="Eight-team strength bracket fixture.",
                    result_snapshot={
                        "teams": [
                            {
                                "team_id": str(index),
                                "starter_strength": strength,
                                "starter_average_strength": strength / 6,
                            }
                            for index, strength in enumerate(strengths, start=1)
                        ]
                    },
                    candidate_pool_user_ids=[],
                    leftover_user_ids=[],
                )
            )
            await db_session.commit()

    async def test_full_graph_progression_recovery_and_revision(self) -> None:
        organizer = await self._register("organizer")
        admin = await self._register("admin")
        spectator = await self._register("spectator")
        await self._grant_role(str(admin["user_id"]), "admin")
        await self._grant_public_creation(str(organizer["user_id"]))

        tournament = self._payload(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-cup",
                    "description": "Full bracket graph integration fixture.",
                    "visibility": "public",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament["slug"]
        self._payload(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._payload(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_eight_team_roster(
            organizer_user_id=str(organizer["user_id"]),
            slug=slug,
        )

        opening = self._payload(
            await admin["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
            ),
            201,
        )
        self.assertEqual(len(opening), 4)

        bracket = self._payload(
            await admin["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertTrue(bracket["can_manage"])
        self.assertEqual(bracket["revision"], 1)
        self.assertEqual(len(bracket["matches"]), 7)
        self.assertEqual(
            [
                (match["team_a_id"], match["team_b_id"])
                for match in bracket["matches"]
                if match["round_number"] == 1
            ],
            [("1", "8"), ("4", "5"), ("3", "6"), ("2", "7")],
        )
        self.assertEqual(
            [team["starter_strength"] for team in bracket["teams"]],
            [800, 700, 600, 500, 400, 300, 200, 100],
        )

        spectator_bracket = self._payload(
            await spectator["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertFalse(spectator_bracket["can_manage"])
        reorder_removed = await admin["client"].patch(
            f"/api/v1/tournaments/{slug}/bracket/rounds/1",
            json={
                "ordered_team_ids": ["1", "4", "8", "5", "3", "6", "2", "7"],
                "expected_revision": 1,
            },
        )
        self.assertEqual(reorder_removed.status_code, 404, reorder_removed.text)
        reset_removed = await admin["client"].post(
            f"/api/v1/tournaments/{slug}/bracket/rounds/1/reset",
            json={"expected_revision": 1},
        )
        self.assertEqual(reset_removed.status_code, 404, reset_removed.text)

        quarterfinals = [
            match for match in bracket["matches"] if match["round_number"] == 1
        ]
        invalid_score = await admin["client"].post(
            f"/api/v1/tournaments/{slug}/matches/{quarterfinals[0]['id']}/report",
            json={
                "home_score": 1,
                "away_score": 0,
                "expected_revision": 1,
            },
        )
        self.assertEqual(invalid_score.status_code, 422, invalid_score.text)

        self._payload(
            await admin["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{quarterfinals[0]['id']}/report",
                json={
                    "home_score": 2,
                    "away_score": 0,
                    "expected_revision": 1,
                },
            ),
            200,
        )

        reopened = self._payload(
            await admin["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{quarterfinals[0]['id']}/status",
                json={"status": "scheduled", "expected_revision": 2},
            ),
            200,
        )
        self.assertEqual(reopened["status"], "scheduled")
        bracket = self._payload(
            await admin["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        semifinal = next(
            match
            for match in bracket["matches"]
            if match["round_number"] == 2 and match["match_order"] == 1
        )
        self.assertIsNone(semifinal["team_a_id"])

        self._payload(
            await admin["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{quarterfinals[0]['id']}/report",
                json={
                    "home_score": 0,
                    "away_score": 2,
                    "expected_revision": 3,
                },
            ),
            200,
        )
        self._payload(
            await admin["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{quarterfinals[1]['id']}/report",
                json={
                    "home_score": 2,
                    "away_score": 1,
                    "expected_revision": 4,
                },
            ),
            200,
        )
        bracket = self._payload(
            await admin["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        semifinal = next(
            match
            for match in bracket["matches"]
            if match["round_number"] == 2 and match["match_order"] == 1
        )
        self.assertEqual(
            (semifinal["team_a_id"], semifinal["team_b_id"]),
            ("8", "4"),
        )

        self._payload(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "in_progress"},
            ),
            200,
        )
        self._payload(
            await admin["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{semifinal['id']}/status",
                json={"status": "live", "expected_revision": 5},
            ),
            200,
        )
        blocked_recovery = await admin["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{quarterfinals[0]['id']}/status",
            json={"status": "scheduled", "expected_revision": 6},
        )
        self.assertEqual(blocked_recovery.status_code, 409, blocked_recovery.text)

    async def test_score_report_advances_bracket_without_starting_match_or_tournament(self) -> None:
        organizer = await self._register("simple-organizer")
        await self._grant_public_creation(str(organizer["user_id"]))

        tournament = self._payload(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-simple-cup",
                    "description": "Simplified bracket report fixture.",
                    "visibility": "public",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament["slug"]
        self._payload(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._payload(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_eight_team_roster(
            organizer_user_id=str(organizer["user_id"]),
            slug=slug,
        )

        opening = self._payload(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
            ),
            201,
        )
        reported = self._payload(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{opening[0]['id']}/report",
                json={
                    "home_score": 2,
                    "away_score": 0,
                    "expected_revision": 1,
                },
            ),
            200,
        )
        self.assertEqual(reported["status"], "completed")
        self.assertEqual(reported["winner_side"], "home")

        bracket = self._payload(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket["status"], "ready")
        self.assertEqual(bracket["revision"], 2)
        semifinal = next(
            match
            for match in bracket["matches"]
            if match["round_number"] == 2 and match["match_order"] == 1
        )
        self.assertEqual(semifinal["team_a_id"], opening[0]["home_team_id"])

    async def test_bracket_contract_exposes_terminal_lifecycle_and_capabilities(self) -> None:
        organizer = await self._register("capability-organizer")
        await self._grant_public_creation(str(organizer["user_id"]))

        tournament = self._payload(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-cap-cup",
                    "description": "Bracket lifecycle capability fixture.",
                    "visibility": "public",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament["slug"]
        for next_status in ("registration_open", "registration_closed"):
            self._payload(
                await organizer["client"].patch(
                    f"/api/v1/tournaments/{slug}/status",
                    json={"status": next_status},
                ),
                200,
            )
        await self._lock_eight_team_roster(
            organizer_user_id=str(organizer["user_id"]),
            slug=slug,
        )
        opening = self._payload(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
            ),
            201,
        )
        bracket = self._payload(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket["tournament_status"], "registration_closed")
        self.assertEqual(bracket["capabilities"], {
            "can_manage": True,
            "can_schedule_matches": True,
            "can_report_matches": True,
        })

        self._payload(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "cancelled"},
            ),
            200,
        )
        terminal_bracket = self._payload(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(terminal_bracket["status"], "ready")
        self.assertEqual(terminal_bracket["tournament_status"], "cancelled")
        self.assertEqual(terminal_bracket["capabilities"], {
            "can_manage": False,
            "can_schedule_matches": False,
            "can_report_matches": False,
        })

        for path, payload in (
            (
                f"/api/v1/tournaments/{slug}/matches/{opening[0]['id']}/schedule",
                {"scheduled_at": None, "expected_revision": terminal_bracket["revision"]},
            ),
            (
                f"/api/v1/tournaments/{slug}/matches/{opening[0]['id']}/report",
                {"home_score": 2, "away_score": 0, "expected_revision": terminal_bracket["revision"]},
            ),
        ):
            method = "PATCH" if path.endswith("/schedule") else "POST"
            response = await organizer["client"].request(method, path, json=payload)
            self.assertEqual(response.status_code, 409, response.text)

    async def test_bracket_sse_delivers_redis_event_within_two_seconds(self) -> None:
        tournament_id = f"{self.prefix}-realtime"
        stream = stream_bracket_events(tournament_id)
        connected = await asyncio.wait_for(anext(stream), timeout=2)
        self.assertIn("event: connected", connected)

        await publish_bracket_event(
            tournament_id,
            {
                "type": "test",
                "tournament_id": tournament_id,
                "revision": 11,
                "match_id": None,
            },
        )
        event = ""
        async with asyncio.timeout(2):
            while "event: bracket" not in event:
                event = await anext(stream)
        self.assertIn("event: bracket", event)
        self.assertIn('"revision":11', event)
        await stream.aclose()
