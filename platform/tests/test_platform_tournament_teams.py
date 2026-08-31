from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import unittest
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from apps.platform_api.app.api.routes.tournaments import (
    build_tournament_bracket_response,
    build_tournament_workspace_detail_bracket_response,
)
from apps.platform_api.app.services.tournament_teams import (
    TournamentTeamMaterializationError,
    load_tournament_team_state,
    materialize_assignment_run_teams,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentTeam,
    TournamentTeamMember,
    User,
    new_uuid,
)


class PlatformTournamentTeamMaterializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-team-state-{uuid4().hex[:8]}"
        await self._cleanup()

    async def asyncTearDown(self) -> None:
        await self._cleanup()
        await dispose_engine()

    async def _cleanup(self) -> None:
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%"))
            )
            await db_session.execute(
                delete(User).where(User.email.like(f"{self.prefix}-%@example.com"))
            )
            await db_session.commit()

    async def _seed_materialized_run(self) -> tuple[str, str, list[str]]:
        now = datetime.now(UTC)
        user_ids = [new_uuid() for _ in range(7)]
        async with session_factory()() as db_session:
            db_session.add_all(
                User(
                    id=user_id,
                    email=f"{self.prefix}-{index}@example.com",
                    display_name=f"Team player {index}",
                )
                for index, user_id in enumerate(user_ids)
            )
            await db_session.flush()
            tournament = Tournament(
                slug=f"{self.prefix}-cup",
                name=f"{self.prefix} Cup",
                visibility="public",
                status="registration_closed",
                format_slug="solo",
                teams_count=1,
                organizer_user_id=user_ids[0],
            )
            db_session.add(tournament)
            await db_session.flush()
            ready_round = TournamentDeadlockReadyRound(
                tournament_id=tournament.id,
                status="closed",
                eligible_user_ids=user_ids,
                initiated_by_user_id=user_ids[0],
                closed_at=now,
            )
            db_session.add(ready_round)
            await db_session.flush()
            captain_round = TournamentDeadlockCaptainRound(
                tournament_id=tournament.id,
                source_ready_round_id=ready_round.id,
                teams_count=1,
                status="finalized",
                initiated_by_user_id=user_ids[0],
                closed_at=now,
                finalized_at=now,
            )
            db_session.add(captain_round)
            await db_session.flush()
            run_row = TournamentDeadlockAssignmentRun(
                tournament_id=tournament.id,
                source_captain_round_id=captain_round.id,
                source_ready_round_id=ready_round.id,
                created_by_user_id=user_ids[0],
                status="published",
                published_at=now,
                published_by_user_id=user_ids[0],
                summary_text="Normalized team state fixture.",
                result_snapshot={
                    "teams": [
                        {
                            "team_id": "1",
                            "team_name": "The Normalized Five",
                            "starter_strength": 5900.0,
                            "starter_average_strength": 1180.0,
                            "captain": {
                                "user_id": user_ids[0],
                                "rank": "Oracle",
                                "subrank": 3,
                                "strength": 1200.0,
                                "assigned_role": "Carry",
                            },
                            "starter_slots": [
                                {
                                    "slot_number": index,
                                    "assigned_role": "Support",
                                    "assigned_player": {
                                        "user_id": user_id,
                                        "rank": "Ascendant",
                                        "subrank": 2,
                                        "strength": 1100.0 + index,
                                    },
                                }
                                for index, user_id in enumerate(user_ids[1:6], start=1)
                            ],
                            "reserve_slot": {
                                "assigned_player": {
                                    "user_id": user_ids[6],
                                    "rank": "Phantom",
                                    "subrank": 1,
                                    "strength": 900.0,
                                }
                            },
                        }
                    ],
                    "optimization_summary": {"source": "fixture"},
                    "preference_metrics": {"starter_slots_total": 5},
                },
                candidate_pool_user_ids=user_ids,
                leftover_user_ids=[],
            )
            db_session.add(run_row)
            await db_session.flush()
            await materialize_assignment_run_teams(
                db_session,
                tournament=tournament,
                run_row=run_row,
                now=now,
            )
            await db_session.commit()
            return tournament.id, run_row.id, user_ids

    async def test_materialization_preserves_snapshot_and_live_api_uses_rows(self) -> None:
        tournament_id, run_id, user_ids = await self._seed_materialized_run()

        async with session_factory()() as db_session:
            run_row = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            tournament = await db_session.get(Tournament, tournament_id)
            self.assertIsNotNone(run_row)
            self.assertIsNotNone(tournament)
            team_rows, member_rows = await load_tournament_team_state(
                db_session,
                tournament_id=tournament_id,
                include_members=True,
            )
            self.assertEqual(len(team_rows), 1)
            self.assertEqual(len(member_rows), 7)
            self.assertEqual(team_rows[0].team_key, "1")
            self.assertEqual(team_rows[0].name, "The Normalized Five")
            self.assertEqual(team_rows[0].captain_user_id, user_ids[0])
            self.assertEqual(team_rows[0].starter_strength, 5900.0)
            self.assertEqual(team_rows[0].starter_average_strength, 1180.0)
            self.assertEqual(
                [(member.roster_role, member.slot_number) for member in member_rows],
                [
                    ("captain", 0),
                    ("starter", 1),
                    ("starter", 2),
                    ("starter", 3),
                    ("starter", 4),
                    ("starter", 5),
                    ("substitute", 6),
                ],
            )
            self.assertEqual(member_rows[0].assigned_role, "Carry")
            self.assertEqual(member_rows[0].strength, 1200.0)
            self.assertEqual(member_rows[0].rank, "Oracle")
            self.assertEqual(member_rows[0].subrank, 3)
            self.assertEqual(member_rows[1].assigned_role, "Support")
            self.assertEqual(member_rows[1].strength, 1101.0)
            self.assertEqual(member_rows[-1].strength, 900.0)
            self.assertEqual(member_rows[-1].rank, "Phantom")
            self.assertEqual(
                {member.user_id for member in member_rows},
                set(user_ids),
            )
            self.assertEqual(run_row.result_snapshot["teams"][0]["team_name"], "The Normalized Five")

            changed_snapshot = dict(run_row.result_snapshot)
            changed_snapshot["teams"] = [
                {**changed_snapshot["teams"][0], "team_name": "Historical Rewrite"}
            ]
            run_row.result_snapshot = changed_snapshot
            await db_session.commit()

            tournament = await db_session.get(Tournament, tournament_id)
            bracket = await build_tournament_bracket_response(
                db_session,
                tournament=tournament,
                auth_session=None,
                include_team_members=True,
            )
            self.assertEqual(bracket.teams[0].id, "1")
            self.assertEqual(bracket.teams[0].name, "The Normalized Five")
            self.assertEqual(bracket.teams[0].captain_id, user_ids[0])
            self.assertEqual(len(bracket.teams[0].members), 7)
            self.assertTrue(bracket.teams[0].members[-1].is_substitute)

            workspace_bracket = await build_tournament_workspace_detail_bracket_response(
                db_session,
                tournament=tournament,
                can_manage=False,
            )
            self.assertEqual(workspace_bracket.teams[0].name, "The Normalized Five")

    async def test_materialization_is_idempotent_without_duplicate_rows(self) -> None:
        tournament_id, run_id, _ = await self._seed_materialized_run()

        async with session_factory()() as db_session:
            tournament = await db_session.get(Tournament, tournament_id)
            run_row = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            self.assertIsNotNone(tournament)
            self.assertIsNotNone(run_row)
            await materialize_assignment_run_teams(
                db_session,
                tournament=tournament,
                run_row=run_row,
                now=datetime.now(UTC),
            )
            await db_session.commit()
            self.assertEqual(
                await db_session.scalar(
                    select(func.count()).select_from(TournamentTeam).where(
                        TournamentTeam.tournament_id == tournament_id
                    )
                ),
                1,
            )
            self.assertEqual(
                await db_session.scalar(
                    select(func.count()).select_from(TournamentTeamMember).where(
                        TournamentTeamMember.tournament_id == tournament_id
                    )
                ),
                7,
            )

    async def test_invalid_materialization_rolls_back_as_one_transaction(self) -> None:
        tournament_id, run_id, _ = await self._seed_materialized_run()

        async with session_factory()() as db_session:
            current_run = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            tournament = await db_session.get(Tournament, tournament_id)
            self.assertIsNotNone(current_run)
            self.assertIsNotNone(tournament)
            current_run.status = "superseded"
            await db_session.commit()

            bad_run = TournamentDeadlockAssignmentRun(
                tournament_id=tournament_id,
                source_captain_round_id=current_run.source_captain_round_id,
                source_ready_round_id=current_run.source_ready_round_id,
                created_by_user_id=current_run.created_by_user_id,
                status="published",
                published_at=datetime.now(UTC),
                published_by_user_id=current_run.created_by_user_id,
                summary_text="Invalid normalized team fixture.",
                result_snapshot={
                    "teams": [
                        {
                            "team_id": "1",
                            "team_name": "Should Not Commit",
                            "captain": {"user_id": new_uuid()},
                            "starter_slots": [],
                            "reserve_slot": None,
                        }
                    ]
                },
                candidate_pool_user_ids=[],
                leftover_user_ids=[],
            )
            db_session.add(bad_run)
            await db_session.flush()
            with self.assertRaises(TournamentTeamMaterializationError):
                await materialize_assignment_run_teams(
                    db_session,
                    tournament=tournament,
                    run_row=bad_run,
                    now=datetime.now(UTC),
                )
            await db_session.rollback()

            teams, members = await load_tournament_team_state(
                db_session,
                tournament_id=tournament_id,
                include_members=True,
            )
            self.assertEqual([(team.team_key, team.name) for team in teams], [("1", "The Normalized Five")])
            self.assertEqual(len(members), 7)

    async def test_database_rejects_duplicate_slot_and_cross_team_user(self) -> None:
        tournament_id, run_id, user_ids = await self._seed_materialized_run()

        async with session_factory()() as db_session:
            team = await db_session.scalar(
                select(TournamentTeam).where(
                    TournamentTeam.tournament_id == tournament_id,
                    TournamentTeam.team_key == "1",
                )
            )
            run_row = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            self.assertIsNotNone(team)
            self.assertIsNotNone(run_row)
            spare_user = User(
                email=f"{self.prefix}-spare@example.com",
                display_name="Spare player",
            )
            db_session.add(spare_user)
            await db_session.flush()
            db_session.add(
                TournamentTeamMember(
                    tournament_id=tournament_id,
                    team_id=team.id,
                    user_id=spare_user.id,
                    slot_number=1,
                    roster_role="starter",
                    strength=1.0,
                )
            )
            with self.assertRaises(IntegrityError):
                await db_session.flush()
            await db_session.rollback()

            second_team = TournamentTeam(
                tournament_id=tournament_id,
                source_assignment_run_id=run_id,
                team_key="2",
                name="Second team",
                starter_strength=0.0,
                starter_average_strength=0.0,
            )
            db_session.add(second_team)
            await db_session.commit()
            db_session.add(
                TournamentTeamMember(
                    tournament_id=tournament_id,
                    team_id=second_team.id,
                    user_id=user_ids[0],
                    slot_number=0,
                    roster_role="captain",
                    strength=1.0,
                )
            )
            with self.assertRaises(IntegrityError):
                await db_session.flush()
            await db_session.rollback()

            self.assertEqual(
                await db_session.scalar(
                    select(func.count()).select_from(TournamentTeamMember).where(
                        TournamentTeamMember.tournament_id == tournament_id
                    )
                ),
                7,
            )

    def test_schema_defines_roster_invariants_and_migration_backfill(self) -> None:
        team_constraint_names = {
            constraint.name for constraint in TournamentTeam.__table__.constraints
        }
        member_constraint_names = {
            constraint.name for constraint in TournamentTeamMember.__table__.constraints
        }
        self.assertIn("uq_tournament_teams_tournament_team_key", team_constraint_names)
        self.assertIn("uq_tournament_team_members_team_slot", member_constraint_names)
        self.assertIn("uq_tournament_team_members_tournament_user", member_constraint_names)
        self.assertTrue(
            any(name.endswith("roster_role_slot_consistent") for name in member_constraint_names)
        )
        captain_index = next(
            index
            for index in TournamentTeamMember.__table__.indexes
            if index.name == "uq_tournament_team_members_team_captain"
        )
        self.assertTrue(captain_index.unique)
        self.assertIn("roster_role = 'captain'", str(captain_index.dialect_options["postgresql"]["where"]))

        migration = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260831_0049_tournament_teams.py"
        ).read_text(encoding="utf-8")
        self.assertIn("WHERE run.status IN ('published', 'locked')", migration)
        self.assertIn("jsonb_array_elements", migration)
        self.assertIn("INSERT INTO platform.tournament_teams", migration)
        self.assertIn("INSERT INTO platform.tournament_team_members", migration)


if __name__ == "__main__":
    unittest.main()
