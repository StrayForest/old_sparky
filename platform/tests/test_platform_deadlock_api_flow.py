from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import unittest
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from apps.platform_api.app.services.tournament_workflow import (
    generate_deadlock_auto_assignment_run_for_tournament,
)
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    PlayerProfile,
    PlayerTournamentCommitment,
    Tournament,
    User,
)


class PlatformDeadlockApiFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-deadlock-{uuid4().hex[:8]}"
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

    async def _register_user(
        self,
        *,
        label: str,
        rank: str,
        subrank: int,
        captain_priority: str | None = None,
    ) -> dict[str, Any]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        display_name = f"test-{label}"[:15]
        register_payload = self._assert_status(
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
        self._assert_status(
            await client.put(
                "/api/v1/profiles/me/deadlock",
                json={
                    "rank": rank,
                    "subrank": subrank,
                    "playtime": "1501-2000",
                    "roles": ["Carry", "Semi-Carry", "Support", "Semi-Support"],
                    "pool": ["Abrams", "Kelvin", "Seven"],
                    "captain_priority": captain_priority,
                },
            ),
            200,
        )
        return {
            "label": label,
            "email": email,
            "client": client,
            "user_id": register_payload["user"]["id"],
            "display_name": register_payload["user"]["display_name"],
        }

    async def _grant_public_creation(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user, f"User {user_id} is missing.")
            user.public_tournament_credits = 100
            await db_session.commit()

    async def _advance_deadlock_automation_for_slug(self, slug: str, *, now: datetime) -> dict[str, int]:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            result = await advance_deadlock_tournament_automation(
                db_session,
                tournament=tournament,
                now=now,
            )
            return result.as_dict()

    async def _generate_deadlock_auto_assignment_for_slug(
        self,
        slug: str,
        *,
        actor_user_id: str,
    ) -> str:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            run_row = await generate_deadlock_auto_assignment_run_for_tournament(
                db_session,
                tournament=tournament,
                actor_user_id=actor_user_id,
            )
            run_id = str(run_row.id)
            await db_session.commit()
            return run_id

    async def test_deadlock_api_flow_covers_ready_check_assignment_lock_and_handoff(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        players = [
            await self._register_user(label="player01", rank="Ascendant", subrank=6, captain_priority="yes"),
            await self._register_user(label="player02", rank="Phantom", subrank=6),
            await self._register_user(label="player03", rank="Phantom", subrank=5),
            await self._register_user(label="player04", rank="Phantom", subrank=4),
            await self._register_user(label="player05", rank="Oracle", subrank=6),
            await self._register_user(label="player06", rank="Oracle", subrank=5),
            await self._register_user(label="player07", rank="Emissary", subrank=6),
            await self._register_user(label="player08", rank="Emissary", subrank=5),
            await self._register_user(label="player09", rank="Ritualist", subrank=6),
            await self._register_user(label="player10", rank="Ritualist", subrank=5),
            await self._register_user(label="player11", rank="Mystic", subrank=6),
            await self._register_user(label="player12", rank="Mystic", subrank=5),
            await self._register_user(label="player13", rank="Acolyte", subrank=6),
        ]
        outsider = await self._register_user(label="profile-outsider", rank="Oracle", subrank=3)
        all_players = [organizer, *players]
        avatar_url = "/assets/main_logo/old-sparky-arena-logo-v3.webp"
        async with session_factory()() as db_session:
            fallback_profile = await db_session.get(PlayerProfile, players[0]["user_id"])
            self.assertIsNotNone(fallback_profile)
            fallback_profile.avatar_url = avatar_url
            fallback_profile.handle = None
            await db_session.commit()

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock API integration flow",
                    "visibility": "public",
                    "format_slug": "solo",
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

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(participants_payload), 14)

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        blocked_match_creation = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches",
            json={
                "title": "Premature semifinal",
                "round_number": 1,
                "sequence_number": 1,
                "home_label": "Team 1",
                "away_label": "Team 2",
                "scheduled_at": None,
            },
        )
        self.assertEqual(blocked_match_creation.status_code, 409, blocked_match_creation.text)
        self.assertIn("Lock a Deadlock roster before creating matches.", blocked_match_creation.json()["detail"])

        blocked_seed_creation = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
        )
        self.assertEqual(blocked_seed_creation.status_code, 409, blocked_seed_creation.text)
        self.assertIn("Lock a Deadlock roster before creating matches.", blocked_seed_creation.json()["detail"])

        blocked_in_progress = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "in_progress"},
        )
        self.assertEqual(blocked_in_progress.status_code, 422, blocked_in_progress.text)
        self.assertIn("Lock a Deadlock roster", blocked_in_progress.json()["detail"])

        ready_start_payload = self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )
        self.assertEqual(ready_start_payload["eligible_participant_count"], 14)

        for user in all_players:
            vote_payload = self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )
            self.assertEqual(vote_payload["status"], "active")

        ready_state_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_payload["active_round"]["ready_count"], 14)

        preview_payload = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/captain-preview",
                params={"teams_count": 2},
            ),
            200,
        )
        self.assertEqual(preview_payload["ready_player_count"], 14)
        self.assertEqual(
            [candidate["user_id"] for candidate in preview_payload["candidates"][:2]],
            [organizer["user_id"], players[0]["user_id"]],
        )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close"),
            200,
        )

        captain_start_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/captain-round/start",
                json={"teams_count": 2},
            ),
            201,
        )
        self.assertEqual(captain_start_payload["status"], "finalized")
        self.assertEqual(captain_start_payload["offered_count"], 0)
        self.assertEqual(captain_start_payload["assigned_count"], 2)
        self.assertEqual(captain_start_payload["declined_count"], 0)

        captain_state_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertIsNone(captain_state_payload["active_round"])
        latest_captain_payload = captain_state_payload["latest_round"]
        self.assertEqual(latest_captain_payload["status"], "finalized")
        self.assertEqual(latest_captain_payload["assigned_count"], 2)
        self.assertEqual(
            {
                entry["user_id"]
                for entry in latest_captain_payload["entries"]
                if entry["state"] == "assigned"
            },
            {organizer["user_id"], players[0]["user_id"]},
        )

        dream_slots_payload = self._assert_status(
            await organizer["client"].put(
                "/api/v1/profiles/me/deadlock/dream-slots",
                json={
                    "slots": [
                        {
                            "slot_number": 1,
                            "allowed_roles": ["Carry"],
                            "desired_heroes": ["Abrams"],
                        }
                    ]
                },
            ),
            200,
        )
        self.assertEqual(dream_slots_payload[0]["allowed_roles"], ["Carry"])
        self.assertEqual(dream_slots_payload[0]["desired_heroes"], ["Abrams"])

        generated_run_id = await self._generate_deadlock_auto_assignment_for_slug(
            slug,
            actor_user_id=str(organizer["user_id"]),
        )
        generated_state_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        generated_run_payload = generated_state_payload["latest_run"]
        self.assertEqual(generated_run_payload["id"], generated_run_id)
        self.assertEqual(generated_run_payload["status"], "generated")
        self.assertEqual(len(generated_run_payload["teams"]), 2)
        run_id = generated_run_payload["id"]
        target_profile_user_id = generated_run_payload["teams"][0]["captain"]["user_id"]

        organizer_profile_before_publish = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
            ),
            200,
        )
        self.assertEqual(organizer_profile_before_publish["profile"]["user_id"], target_profile_user_id)

        participant_profile_before_publish = await players[1]["client"].get(
            f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
        )
        self.assertEqual(participant_profile_before_publish.status_code, 409, participant_profile_before_publish.text)

        legacy_run_response = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/auto-assignment/run"
        )
        self.assertEqual(legacy_run_response.status_code, 404, legacy_run_response.text)
        with self.assertRaises(HTTPException) as duplicate_run_error:
            await self._generate_deadlock_auto_assignment_for_slug(
                slug,
                actor_user_id=str(organizer["user_id"]),
            )
        self.assertEqual(duplicate_run_error.exception.status_code, 409)
        self.assertIn("already matches the current captain", str(duplicate_run_error.exception.detail))

        published_run_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/auto-assignment/{run_id}/publish"
            ),
            200,
        )
        self.assertEqual(published_run_payload["status"], "published")

        participant_state_payload = self._assert_status(
            await players[1]["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        self.assertIsNone(participant_state_payload["latest_run"])
        self.assertEqual(participant_state_payload["published_run"]["id"], run_id)
        self.assertEqual(participant_state_payload["published_run"]["status"], "published")

        outsider_profile_response = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
        )
        self.assertEqual(outsider_profile_response.status_code, 403, outsider_profile_response.text)

        participant_profile_payload = self._assert_status(
            await players[1]["client"].get(
                f"/api/v1/tournaments/{slug}/profiles/{target_profile_user_id}"
            ),
            200,
        )
        self.assertEqual(participant_profile_payload["profile"]["user_id"], target_profile_user_id)
        self.assertIn("deadlock_profile", participant_profile_payload)
        self.assertEqual(len(participant_profile_payload["dream_slots"]), 6)

        locked_run_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/auto-assignment/{run_id}/lock"
            ),
            200,
        )
        self.assertEqual(locked_run_payload["status"], "locked")
        self.assertIsNotNone(locked_run_payload["locked_at"])

        locked_tournament_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertTrue(locked_tournament_payload["has_locked_deadlock_roster"])

        async with session_factory()() as db_session:
            active_commitments = (
                await db_session.scalars(
                    select(PlayerTournamentCommitment).where(
                        PlayerTournamentCommitment.tournament_id == locked_tournament_payload["id"],
                        PlayerTournamentCommitment.released_at.is_(None)
                    )
                )
            ).all()
            self.assertEqual(len(active_commitments), 14)
            self.assertEqual(
                len({commitment.user_id for commitment in active_commitments}),
                14,
            )

        waiting_tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-w",
                    "description": "Registration remains available while committed elsewhere.",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        waiting_slug = waiting_tournament["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{waiting_slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await players[1]["client"].post(
                f"/api/v1/tournaments/{waiting_slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        waiting_workspace = self._assert_status(
            await players[1]["client"].get(
                f"/api/v1/tournaments/{waiting_slug}/workspace",
                params={"participants_limit": 0, "workspace_view": "detail"},
            ),
            200,
        )
        self.assertEqual(
            waiting_workspace["current_user_active_commitment"]["tournament_id"],
            locked_tournament_payload["id"],
        )

        seeded_matches_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
            ),
            201,
        )
        self.assertEqual(len(seeded_matches_payload), 1)
        self.assertEqual(seeded_matches_payload[0]["title"], "Grand Final")
        self.assertEqual(seeded_matches_payload[0]["home_label"], "Team 1")
        self.assertEqual(seeded_matches_payload[0]["away_label"], "Team 2")
        match_id = seeded_matches_payload[0]["id"]
        bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket_payload["status"], "ready")
        self.assertEqual(len(bracket_payload["teams"]), 2)
        self.assertGreater(len(bracket_payload["teams"][0]["members"]), 0)
        roster_members = [
            member
            for team in bracket_payload["teams"]
            for member in team["members"]
        ]
        avatar_member = next(
            member
            for member in roster_members
            if member["user_id"] == players[0]["user_id"]
        )
        self.assertIsNone(avatar_member["avatar_url"])
        self.assertEqual(avatar_member["handle"], players[0]["display_name"])
        self.assertEqual(avatar_member["rank"], "Ascendant")
        self.assertEqual(avatar_member["subrank"], 6)
        self.assertEqual(len(bracket_payload["matches"]), 1)
        self.assertEqual(bracket_payload["matches"][0]["team_a_id"], "1")
        self.assertEqual(bracket_payload["matches"][0]["team_b_id"], "2")
        summary_bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket?teams_view=summary"),
            200,
        )
        self.assertEqual(summary_bracket_payload["status"], "ready")
        self.assertEqual(len(summary_bracket_payload["teams"]), 2)
        self.assertEqual(summary_bracket_payload["teams"][0]["members"], [])
        self.assertEqual(
            summary_bracket_payload["teams"][0]["starter_strength"],
            bracket_payload["teams"][0]["starter_strength"],
        )
        self.assertEqual(len(summary_bracket_payload["matches"]), 1)

        duplicate_seed_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/matches/seed-opening-round"
        )
        self.assertEqual(duplicate_seed_attempt.status_code, 409, duplicate_seed_attempt.text)
        self.assertIn("Matches already exist for this tournament.", duplicate_seed_attempt.json()["detail"])

        blocked_live_before_start = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{match_id}/status",
            json={"status": "live"},
        )
        self.assertEqual(blocked_live_before_start.status_code, 409, blocked_live_before_start.text)
        self.assertIn("Tournament must be in progress", blocked_live_before_start.json()["detail"])

        blocked_reopen = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/status",
            json={"status": "registration_open"},
        )
        self.assertEqual(blocked_reopen.status_code, 422, blocked_reopen.text)
        self.assertIn("Registration cannot be reopened", blocked_reopen.json()["detail"])

        in_progress_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "in_progress"},
            ),
            200,
        )
        self.assertEqual(in_progress_payload["status"], "in_progress")

        live_match_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{match_id}/status",
                json={"status": "live"},
            ),
            200,
        )
        self.assertEqual(live_match_payload["status"], "live")

        completed_match_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{match_id}/report",
                json={
                    "home_score": 2,
                    "away_score": 1,
                    "note": "Locked-roster handoff match completed.",
                },
            ),
            200,
        )
        self.assertEqual(completed_match_payload["status"], "completed")
        self.assertEqual(completed_match_payload["winner_side"], "home")
        completed_tournament_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(completed_tournament_payload["status"], "completed")
        async with session_factory()() as db_session:
            active_commitment_count = await db_session.scalar(
                select(func.count())
                .select_from(PlayerTournamentCommitment)
                .where(
                    PlayerTournamentCommitment.tournament_id == completed_tournament_payload["id"],
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            )
            self.assertEqual(int(active_commitment_count or 0), 0)
        released_workspace = self._assert_status(
            await players[1]["client"].get(
                f"/api/v1/tournaments/{waiting_slug}/workspace",
                params={"participants_limit": 0, "workspace_view": "detail"},
            ),
            200,
        )
        self.assertIsNone(released_workspace["current_user_active_commitment"])

    async def test_deadlock_permissions_require_joined_participant_or_organizer(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        participant = await self._register_user(
            label="participant",
            rank="Ascendant",
            subrank=6,
            captain_priority="yes",
        )
        outsider = await self._register_user(
            label="outsider",
            rank="Phantom",
            subrank=6,
        )

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock permission coverage",
                    "visibility": "public",
                    "format_slug": "solo",
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
            await participant["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        participant_start_attempt = await participant["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"
        )
        self.assertEqual(participant_start_attempt.status_code, 403, participant_start_attempt.text)
        self.assertIn("Only the organizer can manage this tournament.", participant_start_attempt.json()["detail"])

        outsider_ready_state = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check"
        )
        self.assertEqual(outsider_ready_state.status_code, 403, outsider_ready_state.text)
        self.assertIn("Join the tournament before viewing ready-check state.", outsider_ready_state.json()["detail"])

        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"
            ),
            201,
        )

        participant_ready_state = self._assert_status(
            await participant["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check"
            ),
            200,
        )
        self.assertEqual(participant_ready_state["active_round"]["status"], "active")

        outsider_captain_state = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round"
        )
        self.assertEqual(outsider_captain_state.status_code, 403, outsider_captain_state.text)
        self.assertIn("Join the tournament before viewing captain-round state.", outsider_captain_state.json()["detail"])

        outsider_auto_assignment_state = await outsider["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"
        )
        self.assertEqual(
            outsider_auto_assignment_state.status_code,
            403,
            outsider_auto_assignment_state.text,
        )
        self.assertIn(
            "Join the tournament before viewing Deadlock auto-assignment state.",
            outsider_auto_assignment_state.json()["detail"],
        )

    async def test_deadlock_ready_check_state_get_does_not_start_scheduled_round(self) -> None:
        organizer = await self._register_user(
            label="readonly",
            rank="Phantom",
            subrank=5,
        )
        await self._grant_public_creation(str(organizer["user_id"]))

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-r",
                    "description": "Ready-check GET must not advance workflow state.",
                    "visibility": "public",
                    "format_slug": "solo",
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
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            tournament.ready_check_starts_at = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
            await db_session.commit()

        ready_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertIsNone(ready_state["active_round"])
        self.assertIsNone(ready_state["latest_round"])

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament, f"Tournament {slug} is missing.")
            self.assertEqual(tournament.status, "registration_open")
            self.assertIsNone(tournament.automation_ready_check_started_at)

    async def test_deadlock_moderation_prunes_active_ready_check_state(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        active_one = await self._register_user(
            label="active-one",
            rank="Ascendant",
            subrank=6,
            captain_priority="yes",
        )
        active_two = await self._register_user(
            label="active-two",
            rank="Phantom",
            subrank=6,
        )
        removed_before_start = await self._register_user(
            label="removed-pre",
            rank="Oracle",
            subrank=6,
        )

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock moderation hardening",
                    "visibility": "public",
                    "format_slug": "solo",
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

        for user in (organizer, active_one, active_two, removed_before_start):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        participants_by_user_id = {
            participant["user_id"]: participant
            for participant in participants_payload
        }

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        withdrawn_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/participants/{participants_by_user_id[removed_before_start['user_id']]['id']}/moderation",
                json={
                    "status": "withdrawn",
                    "moderation_note": "Removed before ready-check.",
                },
            ),
            200,
        )
        self.assertEqual(withdrawn_payload["status"], "withdrawn")

        ready_start_payload = self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )
        self.assertEqual(ready_start_payload["eligible_participant_count"], 3)

        for user in (organizer, active_one, active_two):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )

        withdrawn_vote_attempt = await removed_before_start["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
            json={"choice": "yes"},
        )
        self.assertEqual(withdrawn_vote_attempt.status_code, 403, withdrawn_vote_attempt.text)
        self.assertIn(
            "Only joined participants can vote in deadlock ready-check.",
            withdrawn_vote_attempt.json()["detail"],
        )

        disqualified_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/participants/{participants_by_user_id[active_two['user_id']]['id']}/moderation",
                json={
                    "status": "disqualified",
                    "moderation_note": "Removed after ready-check vote.",
                },
            ),
            200,
        )
        self.assertEqual(disqualified_payload["status"], "disqualified")

        organizer_ready_state = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check"
            ),
            200,
        )
        self.assertEqual(organizer_ready_state["active_round"]["eligible_participant_count"], 2)
        self.assertEqual(organizer_ready_state["active_round"]["ready_count"], 2)
        self.assertEqual(organizer_ready_state["active_round"]["declined_count"], 0)

        disqualified_ready_state = await active_two["client"].get(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check"
        )
        self.assertEqual(disqualified_ready_state.status_code, 403, disqualified_ready_state.text)
        self.assertIn(
            "Join the tournament before viewing ready-check state.",
            disqualified_ready_state.json()["detail"],
        )

        disqualified_vote_attempt = await active_two["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
            json={"choice": "yes"},
        )
        self.assertEqual(disqualified_vote_attempt.status_code, 403, disqualified_vote_attempt.text)
        self.assertIn(
            "Only joined participants can vote in deadlock ready-check.",
            disqualified_vote_attempt.json()["detail"],
        )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close"),
            200,
        )

        preview_payload = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/captain-preview",
                params={"teams_count": 2},
            ),
            200,
        )
        self.assertEqual(preview_payload["ready_player_count"], 2)
        candidate_user_ids = [candidate["user_id"] for candidate in preview_payload["candidates"]]
        self.assertEqual(candidate_user_ids, [])
        self.assertNotIn(active_two["user_id"], candidate_user_ids)
        self.assertNotIn(removed_before_start["user_id"], candidate_user_ids)

    async def test_deadlock_moderation_reconciles_active_captain_round(self) -> None:
        organizer = await self._register_user(
            label="organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        active_one = await self._register_user(
            label="active-one",
            rank="Ascendant",
            subrank=6,
            captain_priority="yes",
        )
        active_two = await self._register_user(
            label="active-two",
            rank="Phantom",
            subrank=6,
        )

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Deadlock captain moderation hardening",
                    "visibility": "public",
                    "format_slug": "solo",
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

        for user in (organizer, active_one, active_two):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        participants_by_user_id = {
            participant["user_id"]: participant
            for participant in participants_payload
        }

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/start"),
            201,
        )
        for user in (organizer, active_one, active_two):
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )

        self._assert_status(
            await organizer["client"].post(f"/api/v1/tournaments/{slug}/deadlock/ready-check/close"),
            200,
        )

        captain_round_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/start",
            json={"teams_count": 2},
        )
        self.assertEqual(captain_round_attempt.status_code, 409, captain_round_attempt.text)
        self.assertIn("At least 14 ready players", captain_round_attempt.json()["detail"])

        disqualified_payload = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/participants/{participants_by_user_id[active_one['user_id']]['id']}/moderation",
                json={
                    "status": "disqualified",
                    "moderation_note": "Removed during captain round.",
                },
            ),
            200,
        )
        self.assertEqual(disqualified_payload["status"], "disqualified")

        disqualified_response_attempt = await active_one["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/respond",
            json={"decision": "accept"},
        )
        self.assertEqual(
            disqualified_response_attempt.status_code,
            403,
            disqualified_response_attempt.text,
        )
        self.assertIn(
            "Only joined participants can respond to captain offers.",
            disqualified_response_attempt.json()["detail"],
        )

        captain_state_payload = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{slug}/deadlock/captain-round"
            ),
            200,
        )
        self.assertIsNone(captain_state_payload["active_round"])
        self.assertIsNone(captain_state_payload["latest_round"])

    async def test_deadlock_captain_decline_is_disabled(self) -> None:
        organizer = await self._register_user(
            label="decline-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-d",
                    "description": "Captain decline disabled.",
                    "visibility": "public",
                    "format_slug": "solo",
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
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )

        decline_attempt = await organizer["client"].post(
            f"/api/v1/tournaments/{slug}/deadlock/captain-round/respond",
            json={"decision": "decline"},
        )
        self.assertEqual(decline_attempt.status_code, 409, decline_attempt.text)
        self.assertIn(
            "Captain decline is disabled",
            decline_attempt.json()["detail"],
        )

    async def test_tournament_deadlock_automation_schedule_drives_ready_captains_and_assignment(self) -> None:
        organizer = await self._register_user(
            label="auto-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        players = [
            await self._register_user(label=f"auto-player-{index:02}", rank="Phantom", subrank=6)
            for index in range(1, 14)
        ]
        all_players = [organizer, *players]
        base_time = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
        ready_start = base_time
        ready_end = ready_start + timedelta(minutes=10)
        captain_start = ready_end

        invalid_payload = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-a",
                "description": "Invalid automation schedule",
                "visibility": "public",
                "format_slug": "solo",
                "ready_check_starts_at": ready_start.isoformat(),
                "captain_selection_starts_at": (ready_start + timedelta(minutes=5)).isoformat(),
                "teams_count": 2,
            },
        )
        self.assertEqual(invalid_payload.status_code, 422, invalid_payload.text)

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-b",
                    "description": "Automated Deadlock flow",
                    "visibility": "public",
                    "format_slug": "solo",
                    "ready_check_starts_at": ready_start.isoformat(),
                    "captain_selection_starts_at": captain_start.isoformat(),
                    "teams_count": 2,
                },
            ),
            201,
        )
        self.assertEqual(tournament_payload["status"], "registration_open")
        slug = tournament_payload["slug"]

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        ready_start_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_start)
        self.assertEqual(ready_start_result["ready_started"], 1)
        ready_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state["active_round"]["eligible_participant_count"], 14)

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )

        ready_close_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_end)
        self.assertEqual(ready_close_result["ready_closed"], 1)
        captain_start_result = await self._advance_deadlock_automation_for_slug(slug, now=captain_start)
        self.assertEqual(
            ready_close_result["captain_started"] + captain_start_result["captain_started"],
            1,
            {"ready_close_result": ready_close_result, "captain_start_result": captain_start_result},
        )

        captain_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertIsNone(captain_state["active_round"])
        self.assertEqual(captain_state["latest_round"]["status"], "finalized")
        assignment_result = await self._advance_deadlock_automation_for_slug(
            slug,
            now=captain_start + timedelta(minutes=1),
        )
        self.assertEqual(
            ready_close_result["assignment_generated"]
            + captain_start_result["assignment_generated"]
            + assignment_result["assignment_generated"],
            1,
        )
        assignment_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        self.assertEqual(assignment_state["latest_run"]["status"], "locked")
        self.assertEqual(assignment_state["published_run"]["status"], "locked")
        bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket_payload["status"], "ready")
        self.assertEqual(len(bracket_payload["teams"]), 2)
        self.assertEqual(len(bracket_payload["matches"]), 1)
        refreshed_tournament = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertIsNotNone(refreshed_tournament["automation_assignment_generated_at"])

    async def test_deadlock_tournament_full_player_flow_auto_selects_captains_and_generates_teams(
        self,
    ) -> None:
        organizer = await self._register_user(
            label="full-organizer",
            rank="Eternus",
            subrank=6,
            captain_priority="yes",
        )
        await self._grant_public_creation(str(organizer["user_id"]))
        players = [
            await self._register_user(label="full-player-01", rank="Phantom", subrank=6, captain_priority="yes"),
            await self._register_user(label="full-player-02", rank="Ascendant", subrank=5, captain_priority="neutral"),
            await self._register_user(label="full-player-03", rank="Oracle", subrank=6, captain_priority="no"),
            await self._register_user(label="full-player-04", rank="Phantom", subrank=5),
            await self._register_user(label="full-player-05", rank="Oracle", subrank=5),
            await self._register_user(label="full-player-06", rank="Emissary", subrank=6),
            await self._register_user(label="full-player-07", rank="Emissary", subrank=5),
            await self._register_user(label="full-player-08", rank="Ritualist", subrank=6),
            await self._register_user(label="full-player-09", rank="Ritualist", subrank=5),
            await self._register_user(label="full-player-10", rank="Mystic", subrank=6),
            await self._register_user(label="full-player-11", rank="Mystic", subrank=5),
            await self._register_user(label="full-player-12", rank="Acolyte", subrank=6),
            await self._register_user(label="full-player-13", rank="Acolyte", subrank=5),
        ]
        all_players = [organizer, *players]

        base_time = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)
        ready_start = base_time
        ready_end = ready_start + timedelta(minutes=10)
        captain_start = ready_end

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Full Deadlock tournament workflow with automatic captains.",
                    "visibility": "public",
                    "format_slug": "solo",
                    "ready_check_starts_at": ready_start.isoformat(),
                    "captain_selection_starts_at": captain_start.isoformat(),
                    "teams_count": 2,
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]
        self.assertEqual(tournament_payload["status"], "registration_open")

        for user in all_players:
            self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        participants_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(participants_payload), 14)

        ready_start_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_start)
        self.assertEqual(ready_start_result["ready_started"], 1)
        ready_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state["active_round"]["eligible_participant_count"], 14)
        self.assertEqual(ready_state["active_round"]["ready_count"], 0)

        first_ready_user = all_players[0]
        first_yes_payload = self._assert_status(
            await first_ready_user["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                json={"choice": "yes"},
            ),
            200,
        )
        self.assertEqual(first_yes_payload["round_id"], ready_state["active_round"]["id"])
        self.assertEqual(first_yes_payload["current_user_choice"], "yes")
        self.assertTrue(first_yes_payload["changed"])
        self.assertNotIn("ready_count", first_yes_payload)
        self.assertNotIn("declined_count", first_yes_payload)
        ready_state_after_first_yes = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_first_yes["active_round"]["ready_count"], 1)
        self.assertEqual(ready_state_after_first_yes["active_round"]["declined_count"], 0)

        repeated_yes_payload = self._assert_status(
            await first_ready_user["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                json={"choice": "yes"},
            ),
            200,
        )
        self.assertEqual(repeated_yes_payload["current_user_choice"], "yes")
        self.assertFalse(repeated_yes_payload["changed"])
        ready_state_after_repeat = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_repeat["active_round"]["ready_count"], 1)
        self.assertEqual(ready_state_after_repeat["active_round"]["declined_count"], 0)

        declined_payload = self._assert_status(
            await first_ready_user["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                json={"choice": "no"},
            ),
            200,
        )
        self.assertEqual(declined_payload["current_user_choice"], "no")
        self.assertTrue(declined_payload["changed"])
        ready_state_after_decline = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_decline["active_round"]["ready_count"], 0)
        self.assertEqual(ready_state_after_decline["active_round"]["declined_count"], 1)

        restored_yes_payload = self._assert_status(
            await first_ready_user["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                json={"choice": "yes"},
            ),
            200,
        )
        self.assertEqual(restored_yes_payload["current_user_choice"], "yes")
        self.assertTrue(restored_yes_payload["changed"])
        ready_state_after_restore = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_restore["active_round"]["ready_count"], 1)
        self.assertEqual(ready_state_after_restore["active_round"]["declined_count"], 0)

        for user in all_players[1:]:
            vote_payload = self._assert_status(
                await user["client"].post(
                    f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                    json={"choice": "yes"},
                ),
                200,
            )
            self.assertEqual(vote_payload["current_user_choice"], "yes")
            self.assertTrue(vote_payload["changed"])
        ready_state_after_all_votes = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/ready-check"),
            200,
        )
        self.assertEqual(ready_state_after_all_votes["active_round"]["ready_count"], 14)
        self.assertEqual(ready_state_after_all_votes["active_round"]["declined_count"], 0)

        ready_close_result = await self._advance_deadlock_automation_for_slug(slug, now=ready_end)
        captain_start_result = await self._advance_deadlock_automation_for_slug(slug, now=captain_start)
        self.assertEqual(
            ready_close_result["captain_started"] + captain_start_result["captain_started"],
            1,
            {"ready_close_result": ready_close_result, "captain_start_result": captain_start_result},
        )

        captain_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertIsNone(captain_state["active_round"])
        self.assertEqual(captain_state["latest_round"]["status"], "finalized")
        captain_user_ids_to_assign = [
            entry["user_id"]
            for entry in captain_state["latest_round"]["entries"]
            if entry["state"] == "assigned"
        ]
        self.assertEqual(len(captain_user_ids_to_assign), 2)
        self.assertEqual(
            set(captain_user_ids_to_assign),
            {organizer["user_id"], players[0]["user_id"]},
        )

        assignment_result = await self._advance_deadlock_automation_for_slug(
            slug,
            now=captain_start + timedelta(minutes=1),
        )
        self.assertEqual(
            ready_close_result["assignment_generated"]
            + captain_start_result["assignment_generated"]
            + assignment_result["assignment_generated"],
            1,
        )

        captain_final_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/captain-round"),
            200,
        )
        self.assertEqual(captain_final_state["latest_round"]["status"], "finalized")
        assigned_captain_ids = {
            entry["user_id"]
            for entry in captain_final_state["latest_round"]["entries"]
            if entry["state"] == "assigned"
        }
        self.assertEqual(assigned_captain_ids, set(captain_user_ids_to_assign))

        assignment_state = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/deadlock/auto-assignment"),
            200,
        )
        latest_run = assignment_state["latest_run"]
        self.assertEqual(latest_run["status"], "locked")
        self.assertEqual(len(latest_run["teams"]), 2)
        self.assertEqual(
            {team["captain"]["user_id"] for team in latest_run["teams"]},
            set(captain_user_ids_to_assign),
        )
        assigned_user_ids = {
            team["captain"]["user_id"]
            for team in latest_run["teams"]
        }
        for team in latest_run["teams"]:
            assigned_user_ids.update(
                slot["assigned_player"]["user_id"]
                for slot in team["starter_slots"]
            )
            if team["reserve_slot"] is not None:
                assigned_user_ids.add(team["reserve_slot"]["assigned_player"]["user_id"])
        self.assertGreaterEqual(len(assigned_user_ids), 13)

        refreshed_tournament = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertIsNotNone(refreshed_tournament["automation_assignment_generated_at"])
        bracket_payload = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(bracket_payload["status"], "ready")
        self.assertEqual(len(bracket_payload["teams"]), 2)
        self.assertEqual(len(bracket_payload["matches"]), 1)