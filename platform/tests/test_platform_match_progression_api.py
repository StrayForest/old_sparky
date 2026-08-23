from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    User,
)


class PlatformMatchProgressionApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-bracket-{uuid4().hex[:8]}"
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
        display_name = f"test-{label}"[:15]
        payload = self._assert_status(
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": self.password,
                    "display_name": display_name,
                },
            ),
            201,
        )
        return {
            "client": client,
            "user_id": payload["user"]["id"],
        }

    async def _lock_deadlock_roster(self, slug: str, organizer_user_id: str) -> None:
        now = datetime.now(UTC)
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
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
                    summary_text="Test locked Deadlock roster.",
                    result_snapshot={"teams": [{"team_id": "1"}, {"team_id": "2"}]},
                    candidate_pool_user_ids=[],
                    leftover_user_ids=[],
                )
            )
            await db_session.commit()

    async def test_match_progression_generates_next_round_from_completed_results(self) -> None:
        organizer = await self._register_user("organizer")
        spectator = await self._register_user("spectator")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Bracket progression helper",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))

        semifinal_one = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 1",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 4",
                    "scheduled_at": None,
                },
            ),
            201,
        )
        semifinal_two = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 2",
                    "round_number": 1,
                    "sequence_number": 2,
                    "home_label": "Team 2",
                    "away_label": "Team 3",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        schedule_base = datetime.now(UTC) + timedelta(days=1)
        semifinal_one_start = schedule_base.replace(minute=0, second=0, microsecond=0)
        semifinal_two_start = semifinal_one_start + timedelta(hours=1)
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/schedule",
                json={"scheduled_at": semifinal_one_start.isoformat()},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_two['id']}/schedule",
                json={"scheduled_at": semifinal_two_start.isoformat()},
            ),
            200,
        )
        spectator_schedule_attempt = await spectator["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/schedule",
            json={"scheduled_at": semifinal_one_start.isoformat()},
        )
        self.assertEqual(spectator_schedule_attempt.status_code, 403, spectator_schedule_attempt.text)

        blocked_next_round = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-next-round"
        )
        self.assertEqual(blocked_next_round.status_code, 409, blocked_next_round.text)
        self.assertIn("Complete every match", blocked_next_round.json()["detail"])

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/report",
                json={"home_score": 2, "away_score": 0, "note": "Semifinal one complete."},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_two['id']}/report",
                json={"home_score": 2, "away_score": 1, "note": "Semifinal two complete."},
            ),
            200,
        )

        premature_completion = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "completed"},
        )
        self.assertEqual(premature_completion.status_code, 422, premature_completion.text)
        self.assertIn(
            "Cannot move tournament from registration_closed to completed.",
            premature_completion.json()["detail"],
        )

        spectator_progression_attempt = await spectator["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-next-round"
        )
        self.assertEqual(spectator_progression_attempt.status_code, 403, spectator_progression_attempt.text)
        self.assertIn(
            "Only the organizer or a platform admin can manage this tournament.",
            spectator_progression_attempt.json()["detail"],
        )

        next_round_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-next-round"
            ),
            201,
        )
        self.assertEqual(len(next_round_payload), 1)
        self.assertEqual(next_round_payload[0]["round_number"], 2)
        self.assertEqual(next_round_payload[0]["sequence_number"], 1)
        self.assertEqual(next_round_payload[0]["title"], "Grand Final")
        self.assertEqual(next_round_payload[0]["home_label"], "Team 1")
        self.assertEqual(next_round_payload[0]["away_label"], "Team 2")

        final_match_id = next_round_payload[0]["id"]
        invalid_final_schedule = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{final_match_id}/schedule",
            json={"scheduled_at": (semifinal_two_start - timedelta(minutes=10)).isoformat()},
        )
        self.assertEqual(invalid_final_schedule.status_code, 422, invalid_final_schedule.text)
        self.assertIn("cannot start before", invalid_final_schedule.json()["detail"])

        final_start = semifinal_two_start + timedelta(hours=1)
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{final_match_id}/schedule",
                json={"scheduled_at": final_start.isoformat()},
            ),
            200,
        )
        invalid_source_reschedule = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{semifinal_two['id']}/schedule",
            json={"scheduled_at": (final_start + timedelta(minutes=10)).isoformat()},
        )
        self.assertEqual(invalid_source_reschedule.status_code, 422, invalid_source_reschedule.text)
        self.assertIn("cannot start after", invalid_source_reschedule.json()["detail"])

        idempotent_progression = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-next-round"
            ),
            201,
        )
        self.assertEqual(idempotent_progression[0]["id"], next_round_payload[0]["id"])

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{next_round_payload[0]['id']}/report",
                json={"home_score": 3, "away_score": 1, "note": "Grand final complete."},
            ),
            200,
        )

        completed_tournament = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(completed_tournament["status"], "completed")

        completed_round_read = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-next-round"
        )
        self.assertEqual(completed_round_read.status_code, 409, completed_round_read.text)
        self.assertIn(
            "Bracket progression is unavailable after the tournament is completed or cancelled.",
            completed_round_read.json()["detail"],
        )

        frozen_status_update = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{next_round_payload[0]['id']}/status",
            json={"status": "scheduled"},
        )
        self.assertEqual(frozen_status_update.status_code, 409, frozen_status_update.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            frozen_status_update.json()["detail"],
        )

        frozen_report_update = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/{next_round_payload[0]['id']}/report",
            json={"home_score": 4, "away_score": 0, "note": "Post-completion edit."},
        )
        self.assertEqual(frozen_report_update.status_code, 409, frozen_report_update.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            frozen_report_update.json()["detail"],
        )

    async def test_two_team_final_auto_starts_before_completion(self) -> None:
        organizer = await self._register_user("two-team-organizer")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-2f",
                    "description": "Final lifecycle regression fixture",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))

        final_match = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Grand Final",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        reported = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{final_match['id']}/report",
                json={"home_score": 3, "away_score": 1, "note": "Final complete."},
            ),
            200,
        )
        self.assertEqual(reported["status"], "completed")
        completed_tournament = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(completed_tournament["status"], "completed")

        async with session_factory()() as db_session:
            transitions = list(
                (
                    await db_session.scalars(
                        select(AuditLog)
                        .where(
                            AuditLog.subject_type == "tournament",
                            AuditLog.subject_id == tournament_payload["id"],
                            AuditLog.action.in_(
                                (
                                    "tournament.status.auto_start",
                                    "tournament.status.auto_complete",
                                )
                            ),
                        )
                        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
                    )
                ).all()
            )
        self.assertEqual(
            [entry.action for entry in transitions],
            [
                "tournament.status.auto_start",
                "tournament.status.auto_complete",
            ],
        )
        self.assertEqual(transitions[0].payload["from_status"], "registration_closed")
        self.assertEqual(transitions[0].payload["to_status"], "in_progress")
        self.assertEqual(transitions[1].payload["from_status"], "in_progress")
        self.assertEqual(transitions[1].payload["to_status"], "completed")

    async def test_manual_match_creation_respects_round_progression_guardrails(self) -> None:
        organizer = await self._register_user("organizer")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Manual bracket staging guardrails",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))

        semifinal_one = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 1",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 4",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        premature_final = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Grand Final",
                "round_number": 2,
                "sequence_number": 1,
                "home_label": "Winner SF1",
                "away_label": "Winner SF2",
                "scheduled_at": None,
            },
        )
        self.assertEqual(premature_final.status_code, 409, premature_final.text)
        self.assertIn(
            "Complete every match in round 1 before staging round 2.",
            premature_final.json()["detail"],
        )

        semifinal_two = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 2",
                    "round_number": 1,
                    "sequence_number": 2,
                    "home_label": "Team 2",
                    "away_label": "Team 3",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "in_progress"},
            ),
            200,
        )

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/report",
                json={"home_score": 2, "away_score": 0, "note": "Semifinal one complete."},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_two['id']}/report",
                json={"home_score": 2, "away_score": 1, "note": "Semifinal two complete."},
            ),
            200,
        )

        final_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Grand Final",
                    "round_number": 2,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )
        self.assertEqual(final_payload["round_number"], 2)

        blocked_backfill = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Late semifinal",
                "round_number": 1,
                "sequence_number": 3,
                "home_label": "Team 5",
                "away_label": "Team 6",
                "scheduled_at": None,
            },
        )
        self.assertEqual(blocked_backfill.status_code, 409, blocked_backfill.text)
        self.assertIn(
            "Round 1 can no longer be staged after round 2 matches already exist.",
            blocked_backfill.json()["detail"],
        )

        skipped_round = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Skipped round",
                "round_number": 4,
                "sequence_number": 1,
                "home_label": "Winner Final",
                "away_label": "TBD",
                "scheduled_at": None,
            },
        )
        self.assertEqual(skipped_round.status_code, 409, skipped_round.text)
        self.assertIn(
            "Create round 3 before staging round 4.",
            skipped_round.json()["detail"],
        )

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{final_payload['id']}/report",
                json={"home_score": 3, "away_score": 1, "note": "Final complete."},
            ),
            200,
        )

        blocked_after_final = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Post-final round",
                "round_number": 3,
                "sequence_number": 1,
                "home_label": "Winner",
                "away_label": "TBD",
                "scheduled_at": None,
            },
        )
        self.assertEqual(blocked_after_final.status_code, 409, blocked_after_final.text)
        self.assertIn(
            "Matches cannot be added after the tournament is completed or cancelled.",
            blocked_after_final.json()["detail"],
        )

    async def test_latest_round_recovery_can_unwind_and_reseed_bracket(self) -> None:
        organizer = await self._register_user("organizer")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Latest-round recovery flow",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))

        semifinal_one = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 1",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 4",
                    "scheduled_at": None,
                },
            ),
            201,
        )
        semifinal_two = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 2",
                    "round_number": 1,
                    "sequence_number": 2,
                    "home_label": "Team 2",
                    "away_label": "Team 3",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "in_progress"},
            ),
            200,
        )

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/report",
                json={"home_score": 2, "away_score": 0, "note": "Original semifinal one."},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_two['id']}/report",
                json={"home_score": 2, "away_score": 1, "note": "Original semifinal two."},
            ),
            200,
        )

        final_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-next-round"
            ),
            201,
        )
        final_match_id = final_payload[0]["id"]

        reopened_semifinal = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/status",
                json={"status": "scheduled"},
            ),
            200,
        )
        self.assertEqual(reopened_semifinal["status"], "scheduled")
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/report",
                json={"home_score": 2, "away_score": 0, "note": "Restored result."},
            ),
            200,
        )

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{final_match_id}/report",
                json={"home_score": 3, "away_score": 0, "note": "Original final result."},
            ),
            200,
        )

        match_listing = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/matches"),
            200,
        )
        listed_final = next(match for match in match_listing if match["id"] == final_match_id)
        listed_semifinal_one = next(match for match in match_listing if match["id"] == semifinal_one["id"])
        self.assertEqual(listed_final["available_next_statuses"], [])
        self.assertEqual(listed_semifinal_one["available_next_statuses"], [])

        reset_final = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{final_match_id}/status",
            json={"status": "scheduled"},
        )
        self.assertEqual(reset_final.status_code, 409, reset_final.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            reset_final.json()["detail"],
        )

    async def test_cancelled_match_requires_reset_before_progression_and_completion(self) -> None:
        organizer = await self._register_user("organizer")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Cancelled match recovery guardrails",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))

        semifinal_one = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 1",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 4",
                    "scheduled_at": None,
                },
            ),
            201,
        )
        semifinal_two = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Semifinal 2",
                    "round_number": 1,
                    "sequence_number": 2,
                    "home_label": "Team 2",
                    "away_label": "Team 3",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "in_progress"},
            ),
            200,
        )

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/status",
                json={"status": "cancelled"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_two['id']}/report",
                json={"home_score": 2, "away_score": 1, "note": "Other semifinal complete."},
            ),
            200,
        )

        blocked_next_round = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-next-round"
        )
        self.assertEqual(blocked_next_round.status_code, 409, blocked_next_round.text)
        self.assertIn("Complete every match", blocked_next_round.json()["detail"])

        blocked_manual_final = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Grand Final",
                "round_number": 2,
                "sequence_number": 1,
                "home_label": "Winner SF1",
                "away_label": "Winner SF2",
                "scheduled_at": None,
            },
        )
        self.assertEqual(blocked_manual_final.status_code, 409, blocked_manual_final.text)
        self.assertIn(
            "Round 1 has cancelled matches. Reset them to scheduled before staging round 2.",
            blocked_manual_final.json()["detail"],
        )

        blocked_completion = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "completed"},
        )
        self.assertEqual(blocked_completion.status_code, 409, blocked_completion.text)
        self.assertIn(
            "Round 1 has cancelled matches. Reset them to scheduled before marking the tournament completed.",
            blocked_completion.json()["detail"],
        )

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/status",
                json={"status": "scheduled"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{semifinal_one['id']}/report",
                json={"home_score": 2, "away_score": 0, "note": "Replay complete."},
            ),
            200,
        )

        next_round_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-next-round"
            ),
            201,
        )
        self.assertEqual(len(next_round_payload), 1)

    async def test_cancelled_tournament_freezes_match_admin_actions(self) -> None:
        organizer = await self._register_user("organizer")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Terminal tournament match freeze",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "match_format": "bo3",
                    "final_format": "bo5",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))

        created_match = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Opening match",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        cancelled_tournament = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "cancelled"},
            ),
            200,
        )
        self.assertEqual(cancelled_tournament["status"], "cancelled")

        frozen_status_update = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{created_match['id']}/status",
            json={"status": "cancelled"},
        )
        self.assertEqual(frozen_status_update.status_code, 409, frozen_status_update.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            frozen_status_update.json()["detail"],
        )

        frozen_report_update = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/{created_match['id']}/report",
            json={"home_score": 2, "away_score": 0, "note": "Should stay frozen."},
        )
        self.assertEqual(frozen_report_update.status_code, 409, frozen_report_update.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            frozen_report_update.json()["detail"],
        )

        frozen_delete = await organizer["client"].delete(
            f"/api/v1/tournaments/{slug}/matches/{created_match['id']}"
        )
        self.assertEqual(frozen_delete.status_code, 409, frozen_delete.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            frozen_delete.json()["detail"],
        )
