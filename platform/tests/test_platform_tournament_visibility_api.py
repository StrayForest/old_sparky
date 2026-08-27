from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.api.routes import tournaments as tournament_routes
from apps.platform_api.app.main import create_app
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


class PlatformTournamentVisibilityApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-visibility-{uuid4().hex[:8]}"
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
            "email": email,
        }

    async def _grant_role(self, user_id: str, role_slug: str) -> None:
        async with session_factory()() as db_session:
            role = await db_session.scalar(select(Role).where(Role.slug == role_slug))
            self.assertIsNotNone(role, f"Role {role_slug} is missing.")
            existing = await db_session.scalar(
                select(UserRole).where(
                    UserRole.user_id == user_id,
                    UserRole.role_id == role.id,
                )
            )
            if existing is None:
                db_session.add(UserRole(user_id=user_id, role_id=role.id))
                await db_session.commit()

    async def _grant_public_creation(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user, f"User {user_id} is missing.")
            user.public_tournament_credits = 100
            await db_session.commit()

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
            captain_snapshot = {
                "user_id": organizer_user_id,
                "username": "Visibility Captain",
                "rank": "Oracle",
                "subrank": 3,
                "playtime": "1501-2000",
                "strength": 1000.0,
                "pool": ["Abrams"],
                "roles": ["Carry"],
                "assigned_role": "Carry",
            }
            result_snapshot = {
                "teams": [
                    {
                        "team_id": "1",
                        "starter_strength": 1000.0,
                        "starter_average_strength": 1000.0,
                        "captain": captain_snapshot,
                        "starter_slots": [],
                        "reserve_slot": None,
                    },
                    {
                        "team_id": "2",
                        "starter_strength": 1000.0,
                        "starter_average_strength": 1000.0,
                        "captain": captain_snapshot,
                        "starter_slots": [],
                        "reserve_slot": None,
                    },
                ],
                "optimization_summary": {
                    "threshold": 0.0,
                    "spread_percent": 0.0,
                    "mad_percent": 0.0,
                    "std_percent": 0.0,
                    "candidate_pool_size": 0,
                    "selected_player_count": 0,
                    "source": "fixture",
                    "pool_step": None,
                    "role_rescue_used": False,
                    "accepted_swap_moves": 0,
                    "accepted_replacement_moves": 0,
                    "accepted_hierarchy_moves": 0,
                    "stage": None,
                },
                "preference_metrics": {
                    "starter_slots_total": 0,
                    "starter_preference_slots_total": 0,
                    "starter_role_restricted_slots_total": 0,
                    "starter_role_match_count": 0,
                    "starter_role_match_rate_percent": 0.0,
                    "starter_desired_slots_total": 0,
                    "starter_desired_slots_with_any_match": 0,
                    "starter_desired_slot_hit_rate_percent": 0.0,
                    "starter_desired_heroes_requested_total": 0,
                    "starter_desired_heroes_hit_total": 0,
                    "starter_desired_hero_hit_rate_percent": 0.0,
                    "starter_preference_slots_fully_honored": 0,
                    "starter_preference_slots_fully_honored_rate_percent": 0.0,
                    "reserve_slots_total": 0,
                    "reserve_desired_slots_total": 0,
                    "reserve_desired_slots_with_any_match": 0,
                },
            }
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
                    result_snapshot=result_snapshot,
                    candidate_pool_user_ids=[],
                    leftover_user_ids=[],
                )
            )
            await db_session.commit()

    async def test_public_hub_hides_invite_only_tournaments(self) -> None:
        organizer = await self._register_user("organizer")
        await self._grant_public_creation(str(organizer["user_id"]))
        anonymous = await self._new_client()

        public_tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Visible on the public hub",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        private_tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-b",
                    "description": "Should stay off the public hub",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )

        listing = self._assert_status(await anonymous.get("/api/v1/tournaments"), 200)
        slugs = {tournament["slug"] for tournament in listing}
        self.assertIn(public_tournament["slug"], slugs)
        self.assertNotIn(private_tournament["slug"], slugs)

    async def test_workspace_can_skip_current_user_without_changing_visibility(self) -> None:
        organizer = await self._register_user("organizer")
        outsider = await self._register_user("outsider")
        await self._grant_public_creation(str(organizer["user_id"]))

        public_tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-p",
                    "description": "Public workspace current-user toggle",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        private_tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-i",
                    "description": "Private workspace current-user toggle",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        workspace_params = {
            "participants_limit": 0,
            "workspace_view": "bracket_summary",
        }
        permission_workspace_params = {
            "participants_limit": 0,
            "workspace_view": "bracket",
        }

        default_public_workspace = self._assert_status(
            await outsider["client"].get(
                f"/api/v1/tournaments/{public_tournament['slug']}/workspace",
                params=workspace_params,
            ),
            200,
        )
        default_private_workspace = self._assert_status(
            await outsider["client"].get(
                f"/api/v1/tournaments/{private_tournament['slug']}/workspace",
                params=workspace_params,
            ),
            200,
        )
        default_organizer_workspace = self._assert_status(
            await organizer["client"].get(
                f"/api/v1/tournaments/{public_tournament['slug']}/workspace",
                params=permission_workspace_params,
            ),
            200,
        )
        expected_current_user = self._assert_status(
            await outsider["client"].get("/api/v1/users/me"),
            200,
        )
        expected_organizer_current_user = self._assert_status(
            await organizer["client"].get("/api/v1/users/me"),
            200,
        )
        self.assertEqual(default_public_workspace["current_user"], expected_current_user)
        self.assertEqual(default_private_workspace["current_user"], expected_current_user)
        self.assertEqual(
            default_organizer_workspace["current_user"],
            expected_organizer_current_user,
        )

        with (
            patch.object(
                tournament_routes,
                "private_tournament_monthly_remaining",
                new_callable=AsyncMock,
            ) as monthly_remaining,
            patch.object(
                tournament_routes,
                "serialize_current_user",
                new_callable=AsyncMock,
            ) as serialize_current_user,
        ):
            lean_public_workspace = self._assert_status(
                await outsider["client"].get(
                    f"/api/v1/tournaments/{public_tournament['slug']}/workspace",
                    params={**workspace_params, "include_current_user": False},
                ),
                200,
            )
            lean_private_workspace = self._assert_status(
                await outsider["client"].get(
                    f"/api/v1/tournaments/{private_tournament['slug']}/workspace",
                    params={**workspace_params, "include_current_user": False},
                ),
                200,
            )
            lean_organizer_workspace = self._assert_status(
                await organizer["client"].get(
                    f"/api/v1/tournaments/{public_tournament['slug']}/workspace",
                    params={
                        **permission_workspace_params,
                        "include_current_user": False,
                    },
                ),
                200,
            )

        monthly_remaining.assert_not_awaited()
        serialize_current_user.assert_not_awaited()
        workspace_pairs = (
            (default_public_workspace, lean_public_workspace),
            (default_private_workspace, lean_private_workspace),
            (default_organizer_workspace, lean_organizer_workspace),
        )
        for default_workspace, lean_workspace in workspace_pairs:
            default_workspace_without_ticket = {
                **default_workspace,
                "bracket": (
                    {
                        **default_workspace["bracket"],
                        "sse_admission_ticket": None,
                    }
                    if default_workspace["bracket"] is not None
                    else None
                ),
            }
            lean_workspace_without_ticket = {
                **lean_workspace,
                "bracket": (
                    {
                        **lean_workspace["bracket"],
                        "sse_admission_ticket": None,
                    }
                    if lean_workspace["bracket"] is not None
                    else None
                ),
            }
            self.assertEqual(
                lean_workspace_without_ticket,
                {**default_workspace_without_ticket, "current_user": None},
            )
            self.assertEqual(
                lean_workspace["tournament"],
                default_workspace["tournament"],
            )
            self.assertEqual(
                lean_workspace["tournament"]["current_user_participant_status"],
                default_workspace["tournament"]["current_user_participant_status"],
            )
            self.assertEqual(
                lean_workspace_without_ticket["bracket"],
                default_workspace_without_ticket["bracket"],
            )
            if lean_workspace["bracket"] is not None:
                self.assertEqual(
                    lean_workspace["bracket"]["can_manage"],
                    default_workspace["bracket"]["can_manage"],
                )
            self.assertEqual(
                lean_workspace["ready_check"],
                default_workspace["ready_check"],
            )
            self.assertEqual(
                lean_workspace["auto_assignment"],
                default_workspace["auto_assignment"],
            )
            self.assertEqual(
                lean_workspace["current_user_active_commitment"],
                default_workspace["current_user_active_commitment"],
            )
            self.assertEqual(
                lean_workspace["participants_available"],
                default_workspace["participants_available"],
            )

        self.assertTrue(default_public_workspace["participants_available"])
        self.assertFalse(default_private_workspace["participants_available"])
        self.assertTrue(default_organizer_workspace["bracket"]["can_manage"])
        self.assertIsNotNone(default_organizer_workspace["ready_check"])
        self.assertIsNotNone(default_organizer_workspace["auto_assignment"])

    async def test_invite_only_summary_requires_auth_and_scopes_roster_reads(self) -> None:
        organizer = await self._register_user("organizer")
        outsider = await self._register_user("outsider")
        anonymous = await self._new_client()

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Invite-only visibility checks",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament_payload["slug"]

        anonymous_detail = await anonymous.get(f"/api/v1/tournaments/{slug}")
        self.assertEqual(anonymous_detail.status_code, 401, anonymous_detail.text)
        self.assertIn(
            "Authentication required to view invite-only tournaments.",
            anonymous_detail.json()["detail"],
        )

        summary_payload = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(summary_payload["slug"], slug)
        self.assertEqual(summary_payload["visibility"], "invite_only")

        blocked_participants = await outsider["client"].get(f"/api/v1/tournaments/{slug}/participants")
        self.assertEqual(blocked_participants.status_code, 403, blocked_participants.text)
        self.assertIn(
            "Tournament roster and bracket data are visible only to joined participants, the organizer, or platform admins.",
            blocked_participants.json()["detail"],
        )

        blocked_matches = await outsider["client"].get(f"/api/v1/tournaments/{slug}/matches")
        self.assertEqual(blocked_matches.status_code, 403, blocked_matches.text)
        self.assertIn(
            "Tournament roster and bracket data are visible only to joined participants, the organizer, or platform admins.",
            blocked_matches.json()["detail"],
        )
        blocked_bracket = await outsider["client"].get(f"/api/v1/tournaments/{slug}/bracket")
        self.assertEqual(blocked_bracket.status_code, 403, blocked_bracket.text)
        self.assertIn(
            "Tournament roster and bracket data are visible only to joined participants, the organizer, or platform admins.",
            blocked_bracket.json()["detail"],
        )
        scoped_workspace = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/workspace"),
            200,
        )
        self.assertEqual(scoped_workspace["tournament"]["slug"], slug)
        self.assertFalse(scoped_workspace["participants_available"])
        self.assertEqual(scoped_workspace["participants_total"], 0)
        self.assertIsNone(scoped_workspace["bracket"])

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        invite_payload = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/invites",
                json={"note": "Visibility test", "max_uses": 1, "expires_at": None},
            ),
            201,
        )

        self._assert_status(
            await outsider["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={"code": invite_payload["code"], "entry_type": "solo", "team_name": None},
            ),
            201,
        )
        self._assert_status(
            await outsider["client"].post(
                f"/api/v1/tournaments/{slug}/join",
                json={"entry_type": "solo"},
            ),
            201,
        )

        visible_participants = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(visible_participants), 1)
        self.assertEqual(visible_participants[0]["user_id"], outsider["user_id"])

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
                    "title": "Invite opener",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )
        self.assertEqual(created_match["title"], "Invite opener")

        visible_matches = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/matches"),
            200,
        )
        self.assertEqual(len(visible_matches), 1)
        self.assertEqual(visible_matches[0]["title"], "Invite opener")
        visible_bracket = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/bracket"),
            200,
        )
        self.assertEqual(visible_bracket["status"], "ready")
        self.assertEqual(len(visible_bracket["matches"]), 1)
        visible_workspace = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/workspace"),
            200,
        )
        self.assertTrue(visible_workspace["participants_available"])
        self.assertEqual(visible_workspace["participants_total"], 1)
        self.assertEqual(len(visible_workspace["participants"]), 1)
        self.assertEqual(visible_workspace["bracket"]["status"], "ready")
        self.assertEqual(len(visible_workspace["bracket"]["matches"]), 1)
        self.assertIsNone(visible_workspace["auto_assignment"])
        current_user = self._assert_status(
            await outsider["client"].get("/api/v1/users/me"),
            200,
        )
        self.assertEqual(visible_workspace["current_user"], current_user)

        detail_workspace = self._assert_status(
            await outsider["client"].get(
                f"/api/v1/tournaments/{slug}/workspace?participants_limit=0&workspace_view=detail"
            ),
            200,
        )
        self.assertTrue(detail_workspace["participants_available"])
        self.assertEqual(detail_workspace["participants_total"], 1)
        self.assertEqual(len(detail_workspace["participants"]), 0)
        self.assertEqual(detail_workspace["bracket"]["status"], "ready")
        self.assertEqual(detail_workspace["bracket"]["revision"], visible_workspace["bracket"]["revision"])
        self.assertEqual(detail_workspace["bracket"]["matches"], [])

        shell_workspace = self._assert_status(
            await outsider["client"].get(
                f"/api/v1/tournaments/{slug}/workspace?participants_limit=0&workspace_view=bracket_summary"
            ),
            200,
        )
        self.assertTrue(shell_workspace["participants_available"])
        self.assertEqual(shell_workspace["participants_total"], 1)
        self.assertEqual(shell_workspace["participants"], [])
        self.assertEqual(shell_workspace["bracket"]["status"], "ready")
        self.assertEqual(shell_workspace["bracket"]["revision"], visible_workspace["bracket"]["revision"])
        self.assertEqual(shell_workspace["bracket"]["teams"], [])
        self.assertEqual(shell_workspace["bracket"]["matches"], [])
        self.assertIsNone(shell_workspace["ready_check"])
        self.assertIsNone(shell_workspace["auto_assignment"])

        organizer_workspace = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/workspace"),
            200,
        )
        self.assertEqual(organizer_workspace["auto_assignment"]["published_run"]["status"], "locked")

    async def test_admin_can_read_invite_only_roster_and_matches_without_joining(self) -> None:
        organizer = await self._register_user("organizer")
        managed_player = await self._register_user("player")
        admin_user = await self._register_user("admin")
        await self._grant_role(admin_user["user_id"], "admin")

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Admin visibility checks",
                    "visibility": "invite_only",
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
        invites = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/invites"),
            200,
        )
        self.assertEqual(len(invites), 1)
        self._assert_status(
            await managed_player["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={
                    "code": invites[0]["code"],
                    "entry_type": "solo",
                    "team_name": None,
                },
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/participants/manage",
                json={
                    "user_email": managed_player["email"],
                    "entry_type": "solo",
                    "team_name": None,
                },
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
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))
        self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches",
                json={
                    "title": "Admin visible match",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        admin_summary = self._assert_status(
            await admin_user["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(admin_summary["slug"], slug)

        admin_participants = self._assert_status(
            await admin_user["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(admin_participants), 1)
        self.assertEqual(admin_participants[0]["user_id"], managed_player["user_id"])

        admin_matches = self._assert_status(
            await admin_user["client"].get(f"/api/v1/tournaments/{slug}/matches"),
            200,
        )
        self.assertEqual(len(admin_matches), 1)
        self.assertEqual(admin_matches[0]["title"], "Admin visible match")
