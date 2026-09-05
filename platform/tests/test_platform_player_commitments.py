from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from apps.platform_api.app.services.player_commitments import (
    historical_assignment_roster_members,
    reactivate_viable_tournament_commitments,
    reconcile_player_commitments,
    release_active_commitments,
)
from apps.platform_api.app.services.tournament_workflow import (
    TournamentWorkflowError,
    finalize_deadlock_assignment_with_commitments,
    generate_deadlock_auto_assignment_run_for_tournament,
    transition_tournament_status,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    DeadlockProfile,
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentParticipant,
    User,
    new_uuid,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class FastAssignmentEngine:
    def solve(self, captain_rows, ready_player_rows, dream_slot_rows):
        del dream_slot_rows
        captains = sorted(captain_rows, key=lambda row: str(row["team_id"]))
        ready_players = sorted(ready_player_rows, key=lambda row: str(row["user_id"]))
        selected = ready_players[: len(captains) * 6]
        teams = []
        for team_index, captain in enumerate(captains):
            assigned = selected[team_index * 6:(team_index + 1) * 6]
            teams.append(
                {
                    "team_id": str(captain["team_id"]),
                    "team_name": str(captain.get("team_name") or f"Team {captain['team_id']}"),
                    "captain": {"user_id": str(captain["user_id"])},
                    "starter_slots": [
                        {"assigned_player": {"user_id": str(player["user_id"])}}
                        for player in assigned[:5]
                    ],
                    "reserve_slot": {
                        "assigned_player": {"user_id": str(assigned[5]["user_id"])}
                    },
                }
            )
        return SimpleNamespace(
            result_snapshot={"teams": teams},
            summary_text="Deterministic commitment concurrency fixture.",
            candidate_pool=tuple(
                SimpleNamespace(user_id=str(player["user_id"]))
                for player in ready_players
            ),
            leftovers=tuple(
                SimpleNamespace(user_id=str(player["user_id"]))
                for player in ready_players[len(selected):]
            ),
            optimization_summary=SimpleNamespace(spread=0.0, mad_percent=0.0),
        )


class PlatformPlayerCommitmentTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-commitment-{uuid4().hex[:8]}"
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

    async def _seed_parallel_assignment_inputs(self) -> tuple[list[str], list[str]]:
        now = datetime.now(UTC)
        user_ids = [new_uuid() for _ in range(28)]
        tournament_ids: list[str] = []
        async with session_factory()() as db_session:
            for index, user_id in enumerate(user_ids):
                db_session.add(
                    User(
                        id=user_id,
                        email=f"{self.prefix}-player-{index:02}@example.com",
                        display_name=f"commit-{index:02}",
                    )
                )
            await db_session.flush()
            for index, user_id in enumerate(user_ids):
                db_session.add(
                    DeadlockProfile(
                        user_id=user_id,
                        rank="Eternus" if index < 4 else "Phantom",
                        subrank=max(1, 6 - (index % 6)),
                        playtime="1501-2000",
                        roles=["Carry", "Semi-Carry", "Support", "Semi-Support"],
                        pool=["Abrams", "Kelvin", "Seven"],
                        captain_priority="yes" if index < 4 else None,
                    )
                )

            for suffix in ("a", "b"):
                tournament = Tournament(
                    slug=f"{self.prefix}-{suffix}",
                    name=f"{self.prefix}-{suffix}",
                    visibility="invite_only",
                    status="registration_closed",
                    format_slug="solo",
                    teams_count=2,
                    organizer_user_id=user_ids[0],
                )
                db_session.add(tournament)
                await db_session.flush()
                tournament_ids.append(tournament.id)
                ready_round = TournamentDeadlockReadyRound(
                    tournament_id=tournament.id,
                    status="closed",
                    eligible_user_ids=user_ids,
                    initiated_by_user_id=user_ids[0],
                    closed_at=now,
                )
                db_session.add(ready_round)
                await db_session.flush()
                db_session.add(
                    TournamentDeadlockCaptainRound(
                        tournament_id=tournament.id,
                        source_ready_round_id=ready_round.id,
                        teams_count=2,
                        status="finalized",
                        initiated_by_user_id=user_ids[0],
                        closed_at=now,
                        finalized_at=now,
                    )
                )
                db_session.add_all(
                    TournamentParticipant(
                        tournament_id=tournament.id,
                        user_id=user_id,
                        status="checked_in",
                    )
                    for user_id in user_ids
                )
                db_session.add_all(
                    TournamentDeadlockReadyVote(
                        round_id=ready_round.id,
                        user_id=user_id,
                        choice="yes",
                        responded_at=now,
                    )
                    for user_id in user_ids
                )
            await db_session.commit()
        return user_ids, tournament_ids

    async def _generate_and_publish(self, tournament_id: str, actor_user_id: str) -> str:
        async with session_factory()() as db_session:
            tournament = await db_session.get(Tournament, tournament_id)
            self.assertIsNotNone(tournament)
            run_row = await generate_deadlock_auto_assignment_run_for_tournament(
                db_session,
                tournament=tournament,
                actor_user_id=actor_user_id,
            )
            run_row.status = "published"
            run_row.published_at = datetime.now(UTC)
            run_row.published_by_user_id = actor_user_id
            await db_session.commit()
            return run_row.id

    async def _finalize(self, tournament_id: str, run_id: str, actor_user_id: str):
        async with session_factory()() as db_session:
            tournament = await db_session.get(Tournament, tournament_id)
            run_row = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
            self.assertIsNotNone(tournament)
            self.assertIsNotNone(run_row)
            rebalanced, unavailable = await finalize_deadlock_assignment_with_commitments(
                db_session,
                tournament=tournament,
                run_row=run_row,
                actor_user_id=actor_user_id,
                now=datetime.now(UTC),
            )
            await db_session.commit()
            return {
                "rebalanced": rebalanced,
                "unavailable": unavailable,
                "roster_user_ids": {
                    member.user_id for member in historical_assignment_roster_members(run_row)
                },
            }

    async def _cancel_tournament(self, tournament_id: str, actor_user_id: str) -> None:
        async with session_factory()() as db_session:
            await transition_tournament_status(
                db_session,
                tournament_id=tournament_id,
                next_status="cancelled",
                now=datetime.now(UTC),
                actor_user_id=actor_user_id,
                audit_action="test.tournament.cancel",
            )
            await db_session.commit()

    async def test_generation_rechecks_status_after_stale_tournament_read(self) -> None:
        user_ids, tournament_ids = await self._seed_parallel_assignment_inputs()
        tournament_id = tournament_ids[0]

        async with session_factory()() as stale_session:
            stale_tournament = await stale_session.get(Tournament, tournament_id)
            self.assertIsNotNone(stale_tournament)
            await self._cancel_tournament(tournament_id, user_ids[0])

            with patch(
                "apps.platform_api.app.services.tournament_workflow.AutoAssignmentEngine",
                FastAssignmentEngine,
            ), self.assertRaises(HTTPException) as raised:
                await generate_deadlock_auto_assignment_run_for_tournament(
                    stale_session,
                    tournament=stale_tournament,
                    actor_user_id=user_ids[0],
                )
            self.assertEqual(raised.exception.status_code, 409)
            await stale_session.rollback()

        async with session_factory()() as db_session:
            run_count = await db_session.scalar(
                select(func.count(TournamentDeadlockAssignmentRun.id)).where(
                    TournamentDeadlockAssignmentRun.tournament_id == tournament_id
                )
            )
        self.assertEqual(run_count, 0)

    async def test_roster_lock_rechecks_status_after_stale_tournament_read(self) -> None:
        user_ids, tournament_ids = await self._seed_parallel_assignment_inputs()
        tournament_id = tournament_ids[0]
        with patch(
            "apps.platform_api.app.services.tournament_workflow.AutoAssignmentEngine",
            FastAssignmentEngine,
        ):
            run_id = await self._generate_and_publish(tournament_id, user_ids[0])

        async with session_factory()() as stale_session:
            stale_tournament = await stale_session.get(Tournament, tournament_id)
            run_row = await stale_session.get(TournamentDeadlockAssignmentRun, run_id)
            self.assertIsNotNone(stale_tournament)
            self.assertIsNotNone(run_row)
            await self._cancel_tournament(tournament_id, user_ids[0])

            with self.assertRaises(TournamentWorkflowError):
                await finalize_deadlock_assignment_with_commitments(
                    stale_session,
                    tournament=stale_tournament,
                    run_row=run_row,
                    actor_user_id=user_ids[0],
                    now=datetime.now(UTC),
                )
            await stale_session.rollback()

        async with session_factory()() as db_session:
            active_commitment_count = await db_session.scalar(
                select(func.count(PlayerTournamentCommitment.id)).where(
                    PlayerTournamentCommitment.tournament_id == tournament_id,
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            )
            final_run = await db_session.get(TournamentDeadlockAssignmentRun, run_id)
        self.assertEqual(active_commitment_count, 0)
        self.assertIsNotNone(final_run)
        self.assertEqual(final_run.status, "published")

    async def test_parallel_finalization_builds_disjoint_complete_rosters(self) -> None:
        user_ids, tournament_ids = await self._seed_parallel_assignment_inputs()
        with patch(
            "apps.platform_api.app.services.tournament_workflow.AutoAssignmentEngine",
            FastAssignmentEngine,
        ):
            run_ids = [
                await self._generate_and_publish(tournament_id, user_ids[0])
                for tournament_id in tournament_ids
            ]

            first, second = await asyncio.gather(
                self._finalize(tournament_ids[0], run_ids[0], user_ids[0]),
                self._finalize(tournament_ids[1], run_ids[1], user_ids[0]),
            )

        self.assertEqual(len(first["roster_user_ids"]), 14)
        self.assertEqual(len(second["roster_user_ids"]), 14)
        self.assertTrue(first["roster_user_ids"].isdisjoint(second["roster_user_ids"]))
        self.assertEqual(
            {first["rebalanced"], second["rebalanced"]},
            {False, True},
        )

        async with session_factory()() as db_session:
            active_commitments = (
                await db_session.scalars(
                    select(PlayerTournamentCommitment).where(
                        PlayerTournamentCommitment.tournament_id.in_(tournament_ids),
                        PlayerTournamentCommitment.released_at.is_(None),
                    )
                )
            ).all()
            self.assertEqual(len(active_commitments), 28)
            self.assertEqual(
                len({commitment.user_id for commitment in active_commitments}),
                28,
            )
            per_tournament = dict(
                (
                    await db_session.execute(
                        select(
                            PlayerTournamentCommitment.tournament_id,
                            func.count(),
                        )
                        .where(
                            PlayerTournamentCommitment.tournament_id.in_(tournament_ids),
                            PlayerTournamentCommitment.released_at.is_(None),
                        )
                        .group_by(PlayerTournamentCommitment.tournament_id)
                    )
                ).all()
            )
            self.assertEqual(per_tournament, {tournament_id: 14 for tournament_id in tournament_ids})

            cancelled = await db_session.get(Tournament, tournament_ids[0])
            self.assertIsNotNone(cancelled)
            cancelled.status = "cancelled"
            reconciliation = await reconcile_player_commitments(
                db_session,
                now=datetime.now(UTC),
            )
            self.assertEqual(reconciliation.terminal_released, 14)
            remaining_active = await db_session.scalar(
                select(func.count())
                .select_from(PlayerTournamentCommitment)
                .where(
                    PlayerTournamentCommitment.tournament_id.in_(tournament_ids),
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            )
            self.assertEqual(int(remaining_active or 0), 14)
            cancelled.status = "in_progress"
            reactivated = await reactivate_viable_tournament_commitments(
                db_session,
                tournament_id=tournament_ids[0],
                activated_at=datetime.now(UTC),
            )
            self.assertEqual(reactivated, 14)
            await release_active_commitments(
                db_session,
                tournament_id=tournament_ids[1],
                released_at=datetime.now(UTC),
                release_reason="test_cleanup",
            )
            await release_active_commitments(
                db_session,
                tournament_id=tournament_ids[0],
                released_at=datetime.now(UTC),
                release_reason="test_cleanup",
            )
            await db_session.commit()


if __name__ == "__main__":
    unittest.main()
