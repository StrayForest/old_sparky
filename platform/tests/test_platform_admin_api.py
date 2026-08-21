from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, or_, select, update

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    MediaAsset,
    PreprodTestRun,
    Role,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentInvite,
    TournamentMatch,
    TournamentParticipant,
    User,
    UserRole,
)


class PlatformAdminApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-admin-{uuid4().hex[:8]}"
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
            await db_session.execute(delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%")))
            await db_session.execute(delete(PreprodTestRun).where(PreprodTestRun.marker.like(f"{self.prefix}%")))
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

    async def test_admin_override_can_freeze_then_restore_organizer_match_control(self) -> None:
        organizer = await self._register_user("organizer")
        admin_user = await self._register_user("admin")
        await self._grant_role(admin_user["user_id"], "admin")

        forbidden_list = await organizer["client"].get("/api/v1/admin/tournaments")
        self.assertEqual(forbidden_list.status_code, 403, forbidden_list.text)
        self.assertIn("Admin role is required.", forbidden_list.json()["detail"])

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Manual admin intervention recovery checks",
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
                    "title": "Admin recovery opener",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        forced_completion = self._assert_status(
            await admin_user["client"].patch(
                f"/api/v1/admin/tournaments/{slug}",
                json={
                    "status": "completed",
                    "note": "Freeze organizer workflow while a disputed result is reviewed.",
                },
            ),
            200,
        )
        self.assertEqual(forced_completion["status"], "completed")
        self.assertEqual(forced_completion["match_count"], 1)
        self.assertEqual(forced_completion["latest_round_number"], 1)
        self.assertEqual(forced_completion["unfinished_match_count"], 1)
        self.assertEqual(forced_completion["completed_match_count"], 0)
        self.assertEqual(forced_completion["cancelled_match_count"], 0)
        self.assertIn("1 match(es) are still unresolved", forced_completion["admin_override_warning"])
        self.assertIn("Reopen the tournament to in_progress", forced_completion["admin_recovery_hint"])

        frozen_match_update = await organizer["client"].patch(
            f"/api/v1/tournaments/{slug}/matches/{created_match['id']}/status",
            json={"status": "live"},
        )
        self.assertEqual(frozen_match_update.status_code, 409, frozen_match_update.text)
        self.assertIn(
            "Match administration is unavailable after the tournament is completed or cancelled.",
            frozen_match_update.json()["detail"],
        )

        admin_listing = self._assert_status(
            await admin_user["client"].get("/api/v1/admin/tournaments"),
            200,
        )
        listed_tournament = next(
            tournament for tournament in admin_listing if tournament["slug"] == slug
        )
        self.assertEqual(listed_tournament["match_count"], 1)
        self.assertEqual(listed_tournament["latest_round_number"], 1)
        self.assertEqual(listed_tournament["unfinished_match_count"], 1)
        self.assertIn("Reopen the tournament to in_progress", listed_tournament["admin_recovery_hint"])

        filtered_response = await admin_user["client"].get(
            "/api/v1/admin/tournaments",
            params={
                "search": slug,
                "status": "completed",
                "visibility": "invite_only",
                "attention": "true",
                "limit": 1,
                "offset": 0,
            },
        )
        filtered_listing = self._assert_status(filtered_response, 200)
        self.assertEqual([row["slug"] for row in filtered_listing], [slug])
        self.assertEqual(filtered_response.headers["X-Total-Count"], "1")
        self.assertEqual(filtered_response.headers["X-Limit"], "1")
        self.assertEqual(filtered_response.headers["X-Offset"], "0")
        self.assertEqual(filtered_response.headers["X-Has-More"], "false")

        overview = self._assert_status(
            await admin_user["client"].get("/api/v1/admin/overview"),
            200,
        )
        self.assertGreaterEqual(overview["tournaments_attention_total"], 1)

        reopened = self._assert_status(
            await admin_user["client"].patch(
                f"/api/v1/admin/tournaments/{slug}",
                json={
                    "status": "in_progress",
                    "note": "Return organizer control after dispute review completes.",
                },
            ),
            200,
        )
        self.assertEqual(reopened["status"], "in_progress")
        self.assertEqual(reopened["unfinished_match_count"], 1)
        self.assertIn("Organizer-side match reporting", reopened["admin_recovery_hint"])

        live_match = self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/matches/{created_match['id']}/status",
                json={"status": "live"},
            ),
            200,
        )
        self.assertEqual(live_match["status"], "live")

        completed_match = self._assert_status(
            await organizer["client"].post(
                f"/api/v1/tournaments/{slug}/matches/{created_match['id']}/report",
                json={
                    "home_score": 2,
                    "away_score": 0,
                    "note": "Organizer control restored after admin reopen.",
                },
            ),
            200,
        )
        self.assertEqual(completed_match["status"], "completed")
        self.assertEqual(completed_match["winner_side"], "home")

    async def test_admin_tournament_filters_apply_before_pagination(self) -> None:
        organizer = await self._register_user("filter-organizer")
        admin_user = await self._register_user("filter-admin")
        await self._grant_role(admin_user["user_id"], "admin")

        async with session_factory()() as db_session:
            db_session.add_all([
                Tournament(
                    slug=f"{self.prefix}-public",
                    name="Admin Public Cup",
                    visibility="public",
                    status="registration_closed",
                    format_slug="solo",
                    organizer_user_id=str(organizer["user_id"]),
                ),
                Tournament(
                    slug=f"{self.prefix}-target",
                    name="Admin Target Invitational",
                    visibility="invite_only",
                    status="registration_closed",
                    format_slug="solo",
                    organizer_user_id=str(organizer["user_id"]),
                ),
                Tournament(
                    slug=f"{self.prefix}-completed",
                    name="Admin Completed Cup",
                    visibility="public",
                    status="completed",
                    format_slug="solo",
                    organizer_user_id=str(organizer["user_id"]),
                ),
            ])
            await db_session.commit()

        search_response = await admin_user["client"].get(
            "/api/v1/admin/tournaments",
            params={"search": "Target Invitational", "limit": 1, "offset": 0},
        )
        search_rows = self._assert_status(search_response, 200)
        self.assertEqual(search_response.headers["X-Total-Count"], "1")
        self.assertEqual([row["slug"] for row in search_rows], [f"{self.prefix}-target"])

        filtered_response = await admin_user["client"].get(
            "/api/v1/admin/tournaments",
            params={
                "search": self.prefix,
                "status": "registration_closed",
                "visibility": "invite_only",
                "attention": "true",
                "limit": 1,
                "offset": 0,
            },
        )
        filtered_rows = self._assert_status(filtered_response, 200)
        self.assertEqual(filtered_response.headers["X-Total-Count"], "1")
        self.assertEqual([row["slug"] for row in filtered_rows], [f"{self.prefix}-target"])

        overview = self._assert_status(
            await admin_user["client"].get("/api/v1/admin/overview"),
            200,
        )
        self.assertGreaterEqual(overview["tournaments_total"], 3)
        self.assertGreaterEqual(overview["tournaments_attention_total"], 1)

        invalid_status = await admin_user["client"].get(
            "/api/v1/admin/tournaments",
            params={"status": "draft"},
        )
        self.assertEqual(invalid_status.status_code, 422, invalid_status.text)

    async def test_admin_visibility_override_requires_note_and_reapplies_workspace_access(self) -> None:
        organizer = await self._register_user("organizer")
        outsider = await self._register_user("outsider")
        admin_user = await self._register_user("admin")
        await self._grant_role(admin_user["user_id"], "admin")
        anonymous = await self._new_client()

        tournament_payload = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Admin visibility override regression coverage",
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
                    "title": "Visibility override opener",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                    "scheduled_at": None,
                },
            ),
            201,
        )

        initial_listing = self._assert_status(await anonymous.get("/api/v1/tournaments"), 200)
        self.assertNotIn(slug, {tournament["slug"] for tournament in initial_listing})

        blocked_matches = await outsider["client"].get(f"/api/v1/tournaments/{slug}/matches")
        self.assertEqual(blocked_matches.status_code, 403, blocked_matches.text)
        self.assertIn(
            "Tournament roster and bracket data are visible only to joined participants, the organizer, or platform admins.",
            blocked_matches.json()["detail"],
        )

        missing_note = await admin_user["client"].patch(
            f"/api/v1/admin/tournaments/{slug}",
            json={"visibility": "public"},
        )
        self.assertEqual(missing_note.status_code, 422, missing_note.text)
        self.assertIn(
            "Provide an audit note before applying an admin override.",
            missing_note.json()["detail"],
        )

        public_override = self._assert_status(
            await admin_user["client"].patch(
                f"/api/v1/admin/tournaments/{slug}",
                json={
                    "visibility": "public",
                    "note": "Expose bracket reads during temporary public review.",
                },
            ),
            200,
        )
        self.assertEqual(public_override["visibility"], "public")
        self.assertIsNone(public_override["admin_override_warning"])

        public_listing = self._assert_status(await anonymous.get("/api/v1/tournaments"), 200)
        self.assertIn(slug, {tournament["slug"] for tournament in public_listing})

        visible_matches = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/matches"),
            200,
        )
        self.assertEqual(len(visible_matches), 1)
        self.assertEqual(visible_matches[0]["title"], "Visibility override opener")

        visible_participants = self._assert_status(
            await outsider["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(visible_participants, [])

        restored_private = self._assert_status(
            await admin_user["client"].patch(
                f"/api/v1/admin/tournaments/{slug}",
                json={
                    "visibility": "invite_only",
                    "note": "Restore scoped reads after public review completes.",
                },
            ),
            200,
        )
        self.assertEqual(restored_private["visibility"], "invite_only")
        self.assertIn("Invite-only visibility removes the tournament", restored_private["admin_override_warning"])

        private_listing = self._assert_status(await anonymous.get("/api/v1/tournaments"), 200)
        self.assertNotIn(slug, {tournament["slug"] for tournament in private_listing})

        reblocked_matches = await outsider["client"].get(f"/api/v1/tournaments/{slug}/matches")
        self.assertEqual(reblocked_matches.status_code, 403, reblocked_matches.text)
        self.assertIn(
            "Tournament roster and bracket data are visible only to joined participants, the organizer, or platform admins.",
            reblocked_matches.json()["detail"],
        )

    async def test_admin_credits_roles_and_audit_log_are_managed_with_notes(self) -> None:
        regular_admin = await self._register_user("regular-admin")
        superadmin = await self._register_user("superadmin")
        managed_user = await self._register_user("managed")
        await self._grant_role(regular_admin["user_id"], "admin")
        await self._grant_role(superadmin["user_id"], "superadmin")

        quota_update = self._assert_status(
            await regular_admin["client"].patch(
                f"/api/v1/admin/users/{managed_user['user_id']}/tournament-credits",
                json={
                    "public_tournament_credits": 3,
                    "private_tournament_credits": 5,
                    "note": "Approved expanded organizer capacity.",
                },
            ),
            200,
        )
        self.assertEqual(quota_update["public_tournament_credits"], 3)
        self.assertEqual(quota_update["private_tournament_credits"], 5)

        blocked_role_update = await regular_admin["client"].patch(
            f"/api/v1/admin/users/{managed_user['user_id']}/admin-role",
            json={
                "is_admin": True,
                "note": "Regular admins cannot grant admin access.",
            },
        )
        self.assertEqual(blocked_role_update.status_code, 403, blocked_role_update.text)

        role_update = self._assert_status(
            await superadmin["client"].patch(
                f"/api/v1/admin/users/{managed_user['user_id']}/admin-role",
                json={
                    "is_admin": True,
                    "note": "Add a second platform administrator.",
                },
            ),
            200,
        )
        self.assertIn("admin", role_update["roles"])

        audit_rows = self._assert_status(
            await superadmin["client"].get("/api/v1/admin/audit-logs?limit=200"),
            200,
        )
        quota_audit = next(
            row for row in audit_rows
            if row["action"] == "admin.user.tournament_credits"
            and row["subject_id"] == managed_user["user_id"]
        )
        self.assertEqual(quota_audit["actor_email"], regular_admin["email"])
        self.assertEqual(
            quota_audit["payload"]["note"],
            "Approved expanded organizer capacity.",
        )
        self.assertEqual(quota_audit["payload"]["public_tournament_credits"], 3)
        self.assertEqual(quota_audit["payload"]["private_tournament_credits"], 5)
        role_audit = next(
            row for row in audit_rows
            if row["action"] == "admin.user.admin_role"
            and row["subject_id"] == managed_user["user_id"]
        )
        self.assertEqual(role_audit["actor_email"], superadmin["email"])

    async def test_admin_user_list_includes_retained_qa_email_domains(self) -> None:
        admin_user = await self._register_user("qa-list-admin")
        await self._grant_role(admin_user["user_id"], "admin")
        qa_email = f"{self.prefix}-retained@oldsparky.invalid"
        async with session_factory()() as db_session:
            db_session.add(User(email=qa_email, display_name="Retained QA"))
            await db_session.commit()

        users = self._assert_status(
            await admin_user["client"].get("/api/v1/admin/users"),
            200,
        )
        self.assertIn(qa_email, {user["email"] for user in users})

    async def test_admin_user_list_serializes_steam_only_user_without_email(self) -> None:
        admin_user = await self._register_user("steam-list-admin")
        await self._grant_role(admin_user["user_id"], "admin")
        display_name = f"{self.prefix}-steam-only"
        async with session_factory()() as db_session:
            steam_user = User(
                email=None,
                display_name=display_name,
                status="active",
                email_verified_at=None,
            )
            db_session.add(steam_user)
            await db_session.commit()

        users = self._assert_status(
            await admin_user["client"].get("/api/v1/admin/users"),
            200,
        )
        serialized = next(user for user in users if user["display_name"] == display_name)
        self.assertIsNone(serialized["email"])

    async def test_superadmin_can_cleanup_tracked_preprod_test_data(self) -> None:
        regular_admin = await self._register_user("cleanup-admin")
        superadmin = await self._register_user("cleanup-superadmin")
        synthetic_owner = await self._register_user("cleanup-owner")
        await self._grant_role(regular_admin["user_id"], "admin")
        await self._grant_role(superadmin["user_id"], "superadmin")

        marker = f"{self.prefix}-preprod"
        async with session_factory()() as db_session:
            tournament = Tournament(
                slug=f"{self.prefix}-preprod",
                name=f"{self.prefix}-preprod",
                description="Tracked synthetic tournament.",
                visibility="public",
                status="registration_closed",
                format_slug="solo",
                organizer_user_id=synthetic_owner["user_id"],
            )
            db_session.add(tournament)
            await db_session.flush()
            db_session.add(
                PreprodTestRun(
                    marker=marker,
                    status="passed",
                    origin="http://testserver",
                    requested_users=1,
                    created_users=1,
                    tournaments_created=1,
                    report={
                        "user_ids": [synthetic_owner["user_id"]],
                        "tournament_ids": [tournament.id],
                    },
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
            )
            await db_session.commit()
            tournament_id = tournament.id

        blocked = await regular_admin["client"].post(
            f"/api/v1/admin/preprod-test-runs/{marker}/cleanup",
            json={
                "confirm": "DELETE_TEST_DATA",
                "note": "Only superadmins may clean test data.",
            },
        )
        self.assertEqual(blocked.status_code, 403, blocked.text)

        cleaned = self._assert_status(
            await superadmin["client"].post(
                f"/api/v1/admin/preprod-test-runs/{marker}/cleanup",
                json={
                    "confirm": "DELETE_TEST_DATA",
                    "note": "Remove inspected synthetic data.",
                },
            ),
            200,
        )
        self.assertTrue(cleaned["ok"])
        self.assertEqual(cleaned["users_deleted"], 1)
        self.assertEqual(cleaned["tournaments_deleted"], 1)
        self.assertEqual(cleaned["remaining_users"], 0)
        self.assertEqual(cleaned["remaining_tournaments"], 0)

        async with session_factory()() as db_session:
            self.assertIsNone(await db_session.get(User, synthetic_owner["user_id"]))
            self.assertIsNone(await db_session.get(Tournament, tournament_id))
            run = await db_session.scalar(select(PreprodTestRun).where(PreprodTestRun.marker == marker))
            self.assertIsNotNone(run)
            self.assertEqual(run.status, "cleaned")
            self.assertTrue(run.cleanup_state["ok"])

    async def test_opening_registration_requires_a_new_future_workflow_schedule(self) -> None:
        organizer = await self._register_user("schedule-organizer")
        admin_user = await self._register_user("schedule-admin")
        await self._grant_role(admin_user["user_id"], "admin")

        tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-s",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament["slug"]

        missing_schedule = await admin_user["client"].patch(
            f"/api/v1/admin/tournaments/{slug}",
            json={
                "status": "registration_open",
                "note": "Reopen registration after schedule review.",
            },
        )
        self.assertEqual(missing_schedule.status_code, 422, missing_schedule.text)
        self.assertIn("Opening registration requires new", missing_schedule.text)

        now = datetime.now(UTC)
        registration_close = now + timedelta(hours=2)
        ready_start = registration_close + timedelta(hours=1)
        ready_end = ready_start + timedelta(minutes=30)
        captain_start = ready_end + timedelta(minutes=10)
        tournament_start = captain_start + timedelta(hours=1)
        reopened = self._assert_status(
            await admin_user["client"].patch(
                f"/api/v1/admin/tournaments/{slug}",
                json={
                    "status": "registration_open",
                    "registration_closes_at": registration_close.isoformat(),
                    "ready_check_starts_at": ready_start.isoformat(),
                    "ready_check_ends_at": ready_end.isoformat(),
                    "captain_selection_starts_at": captain_start.isoformat(),
                    "starts_at": tournament_start.isoformat(),
                    "note": "Reopen registration with a completely new workflow schedule.",
                },
            ),
            200,
        )
        self.assertEqual(reopened["status"], "registration_open")
        self.assertEqual(reopened["registration_closes_at"], registration_close.isoformat().replace("+00:00", "Z"))
        self.assertIsNone(reopened["automation_ready_check_started_at"])

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_closed"},
            ),
            200,
        )
        await self._lock_deadlock_roster(slug, str(organizer["user_id"]))
        locked_reopen = await admin_user["client"].patch(
            f"/api/v1/admin/tournaments/{slug}",
            json={
                "status": "registration_open",
                "registration_closes_at": registration_close.isoformat(),
                "ready_check_starts_at": ready_start.isoformat(),
                "ready_check_ends_at": ready_end.isoformat(),
                "captain_selection_starts_at": captain_start.isoformat(),
                "starts_at": tournament_start.isoformat(),
                "note": "Attempt reopening a locked roster.",
            },
        )
        self.assertEqual(locked_reopen.status_code, 409, locked_reopen.text)

    async def test_admin_can_delete_any_tournament_with_dependencies_and_retained_audit(self) -> None:
        organizer = await self._register_user("delete-organizer")
        admin_user = await self._register_user("delete-admin")
        await self._grant_role(admin_user["user_id"], "admin")

        tournament = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-delete",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = tournament["slug"]
        tournament_id = tournament["id"]
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
                    "title": "Delete dependency match",
                    "round_number": 1,
                    "sequence_number": 1,
                    "home_label": "Team 1",
                    "away_label": "Team 2",
                },
            ),
            201,
        )

        wrong_confirmation = await admin_user["client"].request(
            "DELETE",
            f"/api/v1/admin/tournaments/{slug}",
            json={
                "confirmation_name": "wrong",
                "note": "Verify exact-name deletion guard.",
            },
        )
        self.assertEqual(wrong_confirmation.status_code, 422, wrong_confirmation.text)

        media_asset_id = str(uuid4())
        async with session_factory()() as db_session:
            db_session.add(
                MediaAsset(
                    id=media_asset_id,
                    tournament_id=tournament_id,
                    purpose="tournament_banner",
                    status="ready",
                    source_mime="image/png",
                    source_bytes=128,
                    source_sha256="0" * 64,
                    version_id=str(uuid4()),
                )
            )
            await db_session.commit()

        media_blocked = await admin_user["client"].request(
            "DELETE",
            f"/api/v1/admin/tournaments/{slug}",
            json={
                "confirmation_name": tournament["name"],
                "note": "Do not orphan a prepared R2 banner.",
            },
        )
        self.assertEqual(media_blocked.status_code, 409, media_blocked.text)
        self.assertEqual(
            media_blocked.json()["detail"]["code"],
            "tournament_media_cleanup_required",
        )

        async with session_factory()() as db_session:
            await db_session.execute(
                update(MediaAsset)
                .where(MediaAsset.id == media_asset_id)
                .values(status="deleted")
            )
            await db_session.commit()

        deleted = await admin_user["client"].request(
            "DELETE",
            f"/api/v1/admin/tournaments/{slug}",
            json={
                "confirmation_name": tournament["name"],
                "note": "Remove obsolete tournament and all scoped data.",
            },
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)

        async with session_factory()() as db_session:
            self.assertIsNone(await db_session.get(Tournament, tournament_id))
            self.assertIsNone(await db_session.get(MediaAsset, media_asset_id))
            for model in (
                TournamentInvite,
                TournamentParticipant,
                TournamentMatch,
                TournamentDeadlockAssignmentRun,
                TournamentDeadlockCaptainRound,
                TournamentDeadlockReadyRound,
            ):
                remaining = await db_session.scalar(
                    select(model).where(model.tournament_id == tournament_id).limit(1)
                )
                self.assertIsNone(remaining, model.__name__)
            audit = await db_session.scalar(
                select(AuditLog).where(
                    AuditLog.action == "admin.tournament.delete",
                    AuditLog.subject_id == tournament_id,
                )
            )
            self.assertIsNotNone(audit)
            self.assertEqual(audit.payload["slug"], slug)
