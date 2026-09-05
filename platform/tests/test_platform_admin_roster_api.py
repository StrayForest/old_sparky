from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import delete, or_, select

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.tournament_teams import (
    materialize_assignment_run_teams,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    DeadlockProfile,
    MediaAsset,
    PlayerTournamentCommitment,
    Role,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentParticipant,
    User,
    UserRole,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class PlatformAdminRosterApiTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-admin-roster-{uuid4().hex[:8]}"
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
                            or_(
                                User.email.like(f"{self.prefix}-%"),
                                User.display_name.like(f"{self.prefix}-%"),
                            )
                        )
                    )
                ).all()
            )
            if user_ids:
                await db_session.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids)))
                await db_session.execute(delete(MediaAsset).where(MediaAsset.owner_user_id.in_(user_ids)))
            await db_session.execute(delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%")))
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
                "display_name": f"ops-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return {"client": client, "user_id": response.json()["user"]["id"]}

    async def _grant_role(self, user_id: str, role_slug: str) -> None:
        async with session_factory()() as db_session:
            role = await db_session.scalar(select(Role).where(Role.slug == role_slug))
            self.assertIsNotNone(role)
            db_session.add(UserRole(user_id=user_id, role_id=role.id))
            await db_session.commit()

    async def _seed_roster(self) -> tuple[dict[str, object], list[dict[str, object]], str, str]:
        admin = await self._register_user("admin")
        await self._grant_role(str(admin["user_id"]), "admin")
        players = [await self._register_user(f"player-{index}") for index in range(6)]
        player_ids = [str(player["user_id"]) for player in players]
        now = datetime.now(UTC)

        async with session_factory()() as db_session:
            tournament = Tournament(
                slug=f"{self.prefix}-cup",
                name=f"{self.prefix} Cup",
                visibility="invite_only",
                status="registration_closed",
                format_slug="solo",
                teams_count=2,
                organizer_user_id=str(admin["user_id"]),
            )
            db_session.add(tournament)
            await db_session.flush()
            db_session.add_all(
                TournamentParticipant(
                    tournament_id=tournament.id,
                    user_id=user_id,
                    status="registered",
                )
                for user_id in player_ids
            )
            db_session.add_all(
                DeadlockProfile(
                    user_id=user_id,
                    rank="Seeker",
                    subrank=1,
                    playtime="0-5",
                    roles=["Carry", "Support"],
                    pool=[],
                )
                for user_id in player_ids
            )
            ready_round = TournamentDeadlockReadyRound(
                tournament_id=tournament.id,
                status="closed",
                eligible_user_ids=player_ids,
                initiated_by_user_id=str(admin["user_id"]),
                closed_at=now,
            )
            db_session.add(ready_round)
            await db_session.flush()
            captain_round = TournamentDeadlockCaptainRound(
                tournament_id=tournament.id,
                source_ready_round_id=ready_round.id,
                teams_count=2,
                status="finalized",
                initiated_by_user_id=str(admin["user_id"]),
                closed_at=now,
                finalized_at=now,
            )
            db_session.add(captain_round)
            await db_session.flush()
            run = TournamentDeadlockAssignmentRun(
                tournament_id=tournament.id,
                source_captain_round_id=captain_round.id,
                source_ready_round_id=ready_round.id,
                created_by_user_id=str(admin["user_id"]),
                status="published",
                published_at=now,
                published_by_user_id=str(admin["user_id"]),
                summary_text="Admin roster fixture.",
                result_snapshot={
                    "teams": [
                        {
                            "team_id": "1",
                            "team_name": "Alpha",
                            "starter_strength": 2.0,
                            "starter_average_strength": 1.0,
                            "captain": {
                                "user_id": player_ids[0],
                                "rank": "Seeker",
                                "subrank": 1,
                                "strength": 1.0,
                                "assigned_role": "Carry",
                            },
                            "starter_slots": [
                                {
                                    "slot_number": 1,
                                    "assigned_player": {
                                        "user_id": player_ids[1],
                                        "rank": "Seeker",
                                        "subrank": 1,
                                        "strength": 1.0,
                                    },
                                    "assigned_role": "Support",
                                }
                            ],
                        },
                        {
                            "team_id": "2",
                            "team_name": "Beta",
                            "starter_strength": 2.0,
                            "starter_average_strength": 1.0,
                            "captain": {
                                "user_id": player_ids[2],
                                "rank": "Seeker",
                                "subrank": 1,
                                "strength": 1.0,
                                "assigned_role": "Carry",
                            },
                            "starter_slots": [
                                {
                                    "slot_number": 1,
                                    "assigned_player": {
                                        "user_id": player_ids[3],
                                        "rank": "Seeker",
                                        "subrank": 1,
                                        "strength": 1.0,
                                    },
                                    "assigned_role": "Support",
                                }
                            ],
                        },
                    ]
                },
                candidate_pool_user_ids=player_ids,
                leftover_user_ids=player_ids[4:],
            )
            db_session.add(run)
            await db_session.flush()
            await materialize_assignment_run_teams(
                db_session,
                tournament=tournament,
                run_row=run,
                now=now,
            )
            await db_session.commit()
            return admin, players, tournament.slug, run.id

    @staticmethod
    def _assert_json(response: httpx.Response, expected: int) -> dict:
        if response.status_code != expected:
            raise AssertionError(f"{response.status_code}: {response.text}")
        return response.json()

    async def test_roster_commands_use_live_rows_and_preserve_assignment_provenance(self) -> None:
        admin, players, slug, run_id = await self._seed_roster()
        client = admin["client"]
        player_ids = [str(player["user_id"]) for player in players]

        initial = self._assert_json(
            await client.get(f"/api/v1/admin/tournaments/{slug}/roster"),
            200,
        )
        self.assertEqual(initial["source_assignment_run_id"], run_id)
        self.assertEqual(len(initial["teams"]), 2)
        self.assertEqual(len(initial["unassigned_participants"]), 2)
        initial_version = initial["state_version"]
        add_payload = {
            "expected_state_version": initial_version,
            "reason": "Fill an open starter slot after a confirmed withdrawal replacement.",
            "team_key": "1",
            "user_id": player_ids[4],
            "slot_number": 2,
        }
        added = self._assert_json(
            await client.post(
                f"/api/v1/admin/tournaments/{slug}/roster/add-player",
                headers={"Idempotency-Key": "roster-add-once"},
                json=add_payload,
            ),
            200,
        )
        self.assertNotEqual(added["state_version"], initial_version)
        self.assertTrue(added["manually_modified"])
        self.assertEqual(added["source_assignment_run_id"], run_id)
        self.assertEqual(
            {member["user_id"] for team in added["teams"] for member in team["members"]},
            {player_ids[0], player_ids[1], player_ids[2], player_ids[3], player_ids[4]},
        )

        replayed = self._assert_json(
            await client.post(
                f"/api/v1/admin/tournaments/{slug}/roster/add-player",
                headers={"Idempotency-Key": "roster-add-once"},
                json=add_payload,
            ),
            200,
        )
        self.assertEqual(replayed["state_version"], added["state_version"])

        stale = await client.post(
            f"/api/v1/admin/tournaments/{slug}/roster/remove-player",
            json={
                "expected_state_version": initial_version,
                "reason": "Try a stale screen mutation.",
                "team_key": "1",
                "user_id": player_ids[1],
            },
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("state changed", stale.json()["detail"])

        moved = self._assert_json(
            await client.post(
                f"/api/v1/admin/tournaments/{slug}/roster/move-player",
                json={
                    "expected_state_version": added["state_version"],
                    "reason": "Balance the two active teams after the roster correction.",
                    "team_key": "1",
                    "user_id": player_ids[4],
                    "destination_team_key": "2",
                    "destination_slot": 2,
                },
            ),
            200,
        )
        self.assertTrue(any(
            member["user_id"] == player_ids[4]
            and member["slot_number"] == 2
            for member in moved["teams"][1]["members"]
        ))

        changed_captain = self._assert_json(
            await client.post(
                f"/api/v1/admin/tournaments/{slug}/roster/change-captain",
                json={
                    "expected_state_version": moved["state_version"],
                    "reason": "The current captain handed leadership to the active starter.",
                    "team_key": "1",
                    "user_id": player_ids[1],
                },
            ),
            200,
        )
        alpha = next(team for team in changed_captain["teams"] if team["team_key"] == "1")
        self.assertEqual(alpha["captain_user_id"], player_ids[1])
        self.assertEqual(next(member for member in alpha["members"] if member["slot_number"] == 0)["user_id"], player_ids[1])

        removed = self._assert_json(
            await client.post(
                f"/api/v1/admin/tournaments/{slug}/roster/remove-player",
                json={
                    "expected_state_version": changed_captain["state_version"],
                    "reason": "Release the former captain back to the participant pool.",
                    "team_key": "2",
                    "user_id": player_ids[4],
                },
            ),
            200,
        )
        self.assertIn(player_ids[4], {player["user_id"] for player in removed["unassigned_participants"]})

        replaced = self._assert_json(
            await client.post(
                f"/api/v1/admin/tournaments/{slug}/roster/replace-player",
                json={
                    "expected_state_version": removed["state_version"],
                    "reason": "Use the next eligible participant for the open starter slot.",
                    "team_key": "2",
                    "slot_number": 1,
                    "replacement_user_id": player_ids[5],
                },
            ),
            200,
        )
        beta = next(team for team in replaced["teams"] if team["team_key"] == "2")
        self.assertEqual(next(member for member in beta["members"] if member["slot_number"] == 1)["user_id"], player_ids[5])

        async with session_factory()() as db_session:
            run = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.result_snapshot["teams"][0]["team_name"], "Alpha")
            audit_count = await db_session.scalar(
                select(AuditLog.id).where(
                    AuditLog.subject_id == run.tournament_id,
                    AuditLog.action.like("admin.tournament.roster.%"),
                ).order_by(AuditLog.id.desc()).limit(1)
            )
            self.assertIsNotNone(audit_count)

    async def test_locked_roster_requires_superadmin_override_and_syncs_commitments(self) -> None:
        admin, players, slug, run_id = await self._seed_roster()
        superadmin = await self._register_user("superadmin")
        await self._grant_role(str(superadmin["user_id"]), "superadmin")
        player_id = str(players[4]["user_id"])

        async with session_factory()() as db_session:
            run = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(run)
            self.assertIsNotNone(tournament)
            now = datetime.now(UTC)
            run.status = "locked"
            run.locked_at = now
            run.locked_by_user_id = str(superadmin["user_id"])
            await db_session.commit()

        roster = self._assert_json(
            await admin["client"].get(f"/api/v1/admin/tournaments/{slug}/roster"),
            200,
        )
        self.assertTrue(roster["locked"])
        self.assertTrue(roster["capabilities"]["requires_override"])
        blocked = await admin["client"].post(
            f"/api/v1/admin/tournaments/{slug}/roster/add-player",
            json={
                "expected_state_version": roster["state_version"],
                "reason": "Regular admin must not edit a locked roster.",
                "team_key": "1",
                "user_id": player_id,
                "slot_number": 2,
            },
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

        changed = self._assert_json(
            await superadmin["client"].post(
                f"/api/v1/admin/tournaments/{slug}/roster/add-player",
                json={
                    "expected_state_version": roster["state_version"],
                    "reason": "Superadmin recovery replaces a documented last-minute withdrawal.",
                    "override": True,
                    "team_key": "1",
                    "user_id": player_id,
                    "slot_number": 2,
                },
            ),
            200,
        )
        self.assertEqual(changed["source_assignment_run_id"], run_id)
        async with session_factory()() as db_session:
            commitment = await db_session.scalar(
                select(PlayerTournamentCommitment).where(
                    PlayerTournamentCommitment.user_id == player_id,
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            )
            self.assertIsNotNone(commitment)
            self.assertEqual(commitment.tournament_id, changed["tournament_id"])
