from __future__ import annotations

from contextlib import AsyncExitStack
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, func, or_, select

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import deadlock_automation
from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AdminTournamentTestFixture,
    AdminTournamentTestPlayer,
    AuditLog,
    ExternalIdentity,
    PasswordCredential,
    PlayerTournamentCommitment,
    Role,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockReadyRound,
    TournamentDeadlockReadyVote,
    TournamentParticipant,
    TournamentTeam,
    TournamentTeamMember,
    UserSession,
    User,
    UserRole,
)
from tests.platform_async_case import (
    PlatformIsolatedAsyncioTestCase,
    next_test_asgi_peer,
    same_origin_request_headers,
)
from tests.platform_integration_password import (
    INTEGRATION_PASSWORD,
    patch_integration_registration_hash,
)


class AdminTournamentTestWorkflowTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-admin-fixture-{uuid4().hex[:8]}"
        self.base_url = "https://testserver"
        self.app = create_app()
        self.clients = AsyncExitStack()
        await self._cleanup_test_data()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        await self._cleanup_test_data()
        await dispose_engine()

    async def _cleanup_test_data(self) -> None:
        async with session_factory()() as db_session:
            fixture_rows = (await db_session.execute(
                select(AdminTournamentTestFixture.id, AdminTournamentTestFixture.tournament_id)
                .join(Tournament, Tournament.id == AdminTournamentTestFixture.tournament_id)
                .where(Tournament.name.like(f"%{self.prefix}%"))
            )).all()
            fixture_ids = [row.id for row in fixture_rows]
            fixture_tournament_ids = [row.tournament_id for row in fixture_rows]
            fixture_user_ids = list((await db_session.scalars(
                select(AdminTournamentTestPlayer.user_id).where(
                    AdminTournamentTestPlayer.fixture_id.in_(fixture_ids or ["-"])
                )
            )).all())
            tournament_ids = list((await db_session.scalars(
                select(Tournament.id).where(or_(
                    Tournament.name.like(f"%{self.prefix}%"),
                    Tournament.slug.like(f"{self.prefix}%"),
                ))
            )).all())
            user_ids = list((await db_session.scalars(
                select(User.id).where(User.email.like(f"{self.prefix}-%@example.com"))
            )).all())
            if tournament_ids or fixture_tournament_ids:
                await db_session.execute(delete(Tournament).where(
                    Tournament.id.in_(set(tournament_ids) | set(fixture_tournament_ids))
                ))
            all_user_ids = set(user_ids) | set(fixture_user_ids)
            if all_user_ids:
                await db_session.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(all_user_ids)))
                await db_session.execute(delete(User).where(User.id.in_(all_user_ids)))
            await db_session.commit()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=next_test_asgi_peer()),
            base_url=self.base_url,
        ))

    def _assert_status(self, response: httpx.Response, expected_status: int) -> dict:
        self.assertEqual(response.status_code, expected_status, response.text)
        return response.json() if response.content else {}

    async def _register_user(self, label: str, *, with_deadlock_profile: bool = False) -> dict[str, object]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        display_name = f"test-{label}"[:15]
        with patch_integration_registration_hash():
            registered = self._assert_status(await client.post(
                "/api/v1/auth/register",
                json={"email": email, "password": INTEGRATION_PASSWORD, "display_name": display_name},
                headers=same_origin_request_headers(client),
            ), 201)
        user_id = str(registered["user"]["id"])
        if with_deadlock_profile:
            self._assert_status(await client.put(
                "/api/v1/profiles/me/deadlock",
                json={
                    "rank": "Ascendant", "subrank": 6, "playtime": "1501-2000",
                    "roles": ["Carry", "Semi-Carry", "Support", "Semi-Support"],
                    "pool": ["Abrams", "Kelvin", "Seven"], "captain_priority": None,
                },
                headers=same_origin_request_headers(client),
            ), 200)
        return {"client": client, "user_id": user_id, "email": email, "display_name": registered["user"]["display_name"]}

    async def _grant_admin(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            role = await db_session.scalar(select(Role).where(Role.slug == "admin"))
            self.assertIsNotNone(role, "Admin role is missing.")
            existing = await db_session.scalar(select(UserRole).where(
                UserRole.user_id == user_id, UserRole.role_id == role.id,
            ))
            if existing is None:
                db_session.add(UserRole(user_id=user_id, role_id=role.id))
                await db_session.commit()

    async def _create_fixture(self, admin: dict[str, object], *, label: str, visibility: str, key: str) -> dict:
        payload = {"name": f"{self.prefix}-{label}", "visibility": visibility, "format_slug": "solo"}
        headers = same_origin_request_headers(admin["client"])
        headers["Idempotency-Key"] = key
        return self._assert_status(await admin["client"].post(
            "/api/v1/admin/tournament-tests", json=payload, headers=headers,
        ), 201)

    async def _admin_post(
        self, admin: dict[str, object], fixture_id: str, operation: str,
        *, key: str, payload: dict | None = None,
    ) -> httpx.Response:
        headers = same_origin_request_headers(admin["client"])
        headers["Idempotency-Key"] = key
        return await admin["client"].post(
            f"/api/v1/admin/tournament-tests/{fixture_id}/{operation}",
            json=payload or {}, headers=headers,
        )

    async def _join(self, user: dict[str, object], fixture: dict, *, invite_code: str | None = None) -> dict:
        payload = {"entry_type": "solo"}
        if invite_code:
            payload["invite_code"] = invite_code
        return self._assert_status(await user["client"].post(
            f"/api/v1/tournaments/{fixture['slug']}/join",
            json=payload,
            headers=same_origin_request_headers(user["client"]),
        ), 201)

    async def _advance_fixture_automation(self, slug: str) -> dict[str, int]:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament)
            return (await advance_deadlock_tournament_automation(
                db_session, tournament=tournament,
            )).as_dict()

    async def test_admins_can_resume_and_generate_teams_with_every_non_ready_human(self) -> None:
        creator = await self._register_user("creator")
        operator = await self._register_user("operator")
        humans = [
            await self._register_user("real-yes", with_deadlock_profile=True),
            await self._register_user("real-no", with_deadlock_profile=True),
            await self._register_user("real-no-vote", with_deadlock_profile=True),
        ]
        await self._grant_admin(str(creator["user_id"]))
        await self._grant_admin(str(operator["user_id"]))

        human = humans[0]
        denied_list = await human["client"].get("/api/v1/admin/tournament-tests")
        self.assertEqual(denied_list.status_code, 403, denied_list.text)
        denied_create = await human["client"].post(
            "/api/v1/admin/tournament-tests",
            json={"name": f"{self.prefix}-denied", "visibility": "public", "format_slug": "solo"},
            headers=same_origin_request_headers(human["client"], extra={"Idempotency-Key": f"{self.prefix}-denied-create"}),
        )
        self.assertEqual(denied_create.status_code, 403, denied_create.text)

        create_key = f"{self.prefix}-create"
        fixture = await self._create_fixture(creator, label="public", visibility="public", key=create_key)
        replayed_create = await self._create_fixture(creator, label="public", visibility="public", key=create_key)
        self.assertEqual(replayed_create["id"], fixture["id"])
        self.assertEqual(fixture["stage"], "registration_open")
        self.assertEqual(fixture["status"], "registration_open")
        self.assertTrue(fixture["is_test_fixture"])
        self.assertTrue(fixture["name"].startswith("[TEST] "))
        self.assertTrue(fixture["join_code"])

        resumed = self._assert_status(await operator["client"].get("/api/v1/admin/tournament-tests"), 200)
        resumed_fixture = next(item for item in resumed if item["id"] == fixture["id"])
        self.assertEqual(resumed_fixture["join_code"], fixture["join_code"])
        await self._join(human, fixture)
        await self._join(humans[1], fixture)
        await self._join(humans[2], fixture)
        scheduler_result = await self._advance_fixture_automation(fixture["slug"])
        self.assertEqual(scheduler_result["registration_opened"], 0)
        self.assertEqual(scheduler_result["registration_closed"], 0)
        self.assertEqual(scheduler_result["ready_started"], 0)
        self.assertEqual(scheduler_result["assignment_generated"], 0)
        self.assertEqual(scheduler_result["errors"], 0)
        denied_mutation = await self._admin_post(
            human, fixture["id"], "ready-check/start", key=f"{self.prefix}-denied-start",
        )
        self.assertEqual(denied_mutation.status_code, 403, denied_mutation.text)

        started = self._assert_status(await self._admin_post(
            operator, fixture["id"], "ready-check/start", key=f"{self.prefix}-start",
        ), 200)
        started_replay = self._assert_status(await self._admin_post(
            operator, fixture["id"], "ready-check/start", key=f"{self.prefix}-start",
        ), 200)
        self.assertEqual(started["stage"], "ready_check_active")
        self.assertEqual(started_replay["stage"], "ready_check_active")
        vote_yes = self._assert_status(await human["client"].post(
            f"/api/v1/tournaments/{fixture['slug']}/deadlock/ready-check/vote",
            json={"choice": "yes"}, headers=same_origin_request_headers(human["client"]),
        ), 200)
        vote_no = self._assert_status(await humans[1]["client"].post(
            f"/api/v1/tournaments/{fixture['slug']}/deadlock/ready-check/vote",
            json={"choice": "no"}, headers=same_origin_request_headers(humans[1]["client"]),
        ), 200)

        closed = self._assert_status(await self._admin_post(
            operator, fixture["id"], "ready-check/close", key=f"{self.prefix}-close",
        ), 200)
        closed_replay = self._assert_status(await self._admin_post(
            operator, fixture["id"], "ready-check/close", key=f"{self.prefix}-close",
        ), 200)
        self.assertEqual(closed["stage"], "ready_check_closed")
        self.assertEqual(closed_replay["stage"], "ready_check_closed")
        self.assertEqual(closed["counts"]["human_active"], 3)
        self.assertEqual(closed["counts"]["human_ready_yes"], 1)

        add_key = f"{self.prefix}-fake-add"
        fakes = self._assert_status(await self._admin_post(
            operator, fixture["id"], "players", key=add_key, payload={"count": 11},
        ), 200)
        fakes_replay = self._assert_status(await self._admin_post(
            operator, fixture["id"], "players", key=add_key, payload={"count": 11},
        ), 200)
        self.assertEqual(fakes["counts"]["fake_total"], 11)
        self.assertEqual(fakes["counts"]["fake_ready_yes"], 11)
        self.assertEqual(fakes_replay["counts"]["fake_total"], 11)
        self.assertEqual(fakes["counts"]["filler_needed"], 0)

        generate_key = f"{self.prefix}-generate"
        generated = self._assert_status(await self._admin_post(
            operator, fixture["id"], "teams/generate", key=generate_key,
        ), 200)
        generated_replay = self._assert_status(await self._admin_post(
            operator, fixture["id"], "teams/generate", key=generate_key,
        ), 200)
        self.assertEqual(generated["stage"], "teams_locked")
        self.assertEqual(generated["assignment_status"], "locked")
        self.assertEqual(generated_replay["assignment_run_id"], generated["assignment_run_id"])
        self.assertGreater(generated["bracket_match_count"], 0)

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == fixture["slug"]))
            self.assertIsNotNone(tournament)
            round_rows = (await db_session.scalars(select(TournamentDeadlockReadyRound).where(
                TournamentDeadlockReadyRound.tournament_id == tournament.id,
            ))).all()
            self.assertEqual(len(round_rows), 1)
            human_votes = await db_session.scalar(select(func.count()).select_from(TournamentDeadlockReadyVote).where(
                TournamentDeadlockReadyVote.round_id == round_rows[0].id,
                TournamentDeadlockReadyVote.user_id == human["user_id"],
            ))
            self.assertEqual(human_votes, 1, "The workflow must preserve the one human yes vote.")
            vote_choices = dict((await db_session.execute(select(
                TournamentDeadlockReadyVote.user_id, TournamentDeadlockReadyVote.choice,
            ).where(
                TournamentDeadlockReadyVote.round_id == round_rows[0].id,
                TournamentDeadlockReadyVote.user_id.in_([item["user_id"] for item in humans]),
            ))).all())
            self.assertEqual(vote_choices, {
                str(humans[0]["user_id"]): "yes",
                str(humans[1]["user_id"]): "no",
            })
            human_members = list((await db_session.scalars(select(TournamentTeamMember).where(
                TournamentTeamMember.tournament_id == tournament.id,
                TournamentTeamMember.user_id.in_([item["user_id"] for item in humans]),
            ))).all())
            self.assertEqual({member.user_id for member in human_members}, {str(item["user_id"]) for item in humans})
            human_commitments = list((await db_session.scalars(select(PlayerTournamentCommitment).where(
                PlayerTournamentCommitment.tournament_id == tournament.id,
                PlayerTournamentCommitment.user_id.in_([item["user_id"] for item in humans]),
                PlayerTournamentCommitment.assignment_run_id == generated["assignment_run_id"],
                PlayerTournamentCommitment.released_at.is_(None),
            ))).all())
            self.assertEqual({item.user_id for item in human_commitments}, {str(item["user_id"]) for item in humans})
            fake_id = await db_session.scalar(select(AdminTournamentTestPlayer.user_id).where(
                AdminTournamentTestPlayer.fixture_id == fixture["id"],
            ).limit(1))
            self.assertIsNotNone(fake_id)
            self.assertNotEqual(fake_id, human["user_id"])
            fake_ids = list((await db_session.scalars(select(AdminTournamentTestPlayer.user_id).where(
                AdminTournamentTestPlayer.fixture_id == fixture["id"],
            ))).all())
            self.assertEqual(len(fake_ids), 11)
            for model in (PasswordCredential, UserSession, ExternalIdentity):
                count = await db_session.scalar(select(func.count()).select_from(model).where(model.user_id.in_(fake_ids)))
                self.assertEqual(count, 0, f"Fake users must not have {model.__tablename__} rows.")
            locked_run_count = await db_session.scalar(select(func.count()).select_from(TournamentDeadlockAssignmentRun).where(
                TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
                TournamentDeadlockAssignmentRun.status == "locked",
            ))
            self.assertEqual(locked_run_count, 1)
            team = await db_session.scalar(select(TournamentTeam).where(
                TournamentTeam.id == human_members[0].team_id,
                TournamentTeam.source_assignment_run_id == generated["assignment_run_id"],
            ))
            self.assertIsNotNone(team)

            foreign_tournament = Tournament(
                slug=f"{self.prefix}-foreign-ref",
                name=f"{self.prefix}-foreign-ref",
                visibility="public", status="registration_open", format_slug="solo",
                allowed_ranks=[], organizer_user_id=creator["user_id"],
                match_format="bo1", final_format="bo3",
            )
            db_session.add(foreign_tournament)
            await db_session.flush()
            db_session.add(TournamentParticipant(
                tournament_id=foreign_tournament.id,
                user_id=fake_id,
                entry_type="solo",
                status="registered",
            ))
            await db_session.commit()
            tournament_id = tournament.id
            foreign_tournament_id = foreign_tournament.id
            fake_user_id = str(fake_id)

        failed_delete_headers = same_origin_request_headers(operator["client"])
        failed_delete_headers["Idempotency-Key"] = f"{self.prefix}-blocked-delete"
        blocked_delete = await operator["client"].request(
            "DELETE", f"/api/v1/admin/tournament-tests/{fixture['id']}",
            json={"confirmation_name": fixture["name"]}, headers=failed_delete_headers,
        )
        self.assertEqual(blocked_delete.status_code, 409, blocked_delete.text)
        self.assertEqual(blocked_delete.json()["detail"]["code"], "fake_users_have_external_references")
        async with session_factory()() as db_session:
            self.assertIsNotNone(await db_session.get(Tournament, tournament_id))
            self.assertIsNotNone(await db_session.get(User, fake_user_id))
            for real_human in humans:
                self.assertIsNotNone(await db_session.get(User, real_human["user_id"]))
            await db_session.execute(delete(Tournament).where(Tournament.id == foreign_tournament_id))
            await db_session.commit()

        delete_headers = same_origin_request_headers(operator["client"])
        delete_headers["Idempotency-Key"] = f"{self.prefix}-final-delete"
        deleted = await operator["client"].request(
            "DELETE", f"/api/v1/admin/tournament-tests/{fixture['id']}",
            json={"confirmation_name": fixture["name"]}, headers=delete_headers,
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        async with session_factory()() as db_session:
            self.assertIsNone(await db_session.get(Tournament, tournament_id))
            self.assertIsNone(await db_session.get(User, fake_user_id))
            for real_human in humans:
                self.assertIsNotNone(await db_session.get(User, real_human["user_id"]))

    async def test_private_fixture_code_and_fifty_fake_player_cap_are_admin_scoped(self) -> None:
        creator = await self._register_user("private-creator")
        operator = await self._register_user("private-operator")
        human = await self._register_user("private-human", with_deadlock_profile=True)
        await self._grant_admin(str(creator["user_id"]))
        await self._grant_admin(str(operator["user_id"]))

        fixture = await self._create_fixture(
            creator, label="private", visibility="invite_only", key=f"{self.prefix}-private-create",
        )
        self.assertEqual(fixture["visibility"], "invite_only")
        self.assertTrue(fixture["join_code"])
        await self._join(human, fixture, invite_code=fixture["join_code"])

        self._assert_status(await self._admin_post(
            operator, fixture["id"], "ready-check/start", key=f"{self.prefix}-private-start",
        ), 200)
        closed = self._assert_status(await self._admin_post(
            operator, fixture["id"], "ready-check/close", key=f"{self.prefix}-private-close",
        ), 200)
        self.assertEqual(closed["stage"], "ready_check_closed")

        add_key = f"{self.prefix}-private-add-fifty"
        full = self._assert_status(await self._admin_post(
            operator, fixture["id"], "players", key=add_key, payload={"count": 50},
        ), 200)
        replay = self._assert_status(await self._admin_post(
            operator, fixture["id"], "players", key=add_key, payload={"count": 50},
        ), 200)
        self.assertEqual(full["counts"]["fake_total"], 50)
        self.assertEqual(full["counts"]["fake_ready_yes"], 50)
        self.assertEqual(replay["counts"]["fake_total"], 50)
        self.assertFalse(full["capabilities"]["can_add_fake_players"])

        overflow = await self._admin_post(
            operator, fixture["id"], "players", key=f"{self.prefix}-private-overflow", payload={"count": 1},
        )
        self.assertEqual(overflow.status_code, 422, overflow.text)
        self.assertEqual(overflow.json()["detail"]["code"], "fake_player_limit")
        summary = self._assert_status(await operator["client"].get(
            f"/api/v1/admin/tournament-tests",
        ), 200)
        private_summary = next(item for item in summary if item["id"] == fixture["id"])
        self.assertEqual(private_summary["counts"]["fake_total"], 50)
        delete_headers = same_origin_request_headers(operator["client"])
        delete_headers["Idempotency-Key"] = f"{self.prefix}-private-delete"
        deleted = await operator["client"].request(
            "DELETE", f"/api/v1/admin/tournament-tests/{fixture['id']}",
            json={"confirmation_name": fixture["name"]}, headers=delete_headers,
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        async with session_factory()() as db_session:
            self.assertIsNotNone(await db_session.get(User, human["user_id"]))
            fake_count = await db_session.scalar(select(func.count()).select_from(AdminTournamentTestPlayer).where(
                AdminTournamentTestPlayer.fixture_id == fixture["id"],
            ))
            self.assertEqual(fake_count, 0)

    async def test_generation_rolls_back_handoff_when_a_human_commitment_disappears(self) -> None:
        admin = await self._register_user("rollback-admin")
        human = await self._register_user("rollback-human", with_deadlock_profile=True)
        await self._grant_admin(str(admin["user_id"]))
        fixture = await self._create_fixture(
            admin, label="rollback", visibility="public", key=f"{self.prefix}-rollback-create",
        )
        await self._join(human, fixture)
        self._assert_status(await self._admin_post(
            admin, fixture["id"], "ready-check/start", key=f"{self.prefix}-rollback-start",
        ), 200)
        self._assert_status(await self._admin_post(
            admin, fixture["id"], "ready-check/close", key=f"{self.prefix}-rollback-close",
        ), 200)
        self._assert_status(await self._admin_post(
            admin, fixture["id"], "players", key=f"{self.prefix}-rollback-fakes", payload={"count": 13},
        ), 200)

        original_finalize = deadlock_automation.finalize_deadlock_assignment_with_commitments

        async def drop_human_commitment(*args: object, **kwargs: object):
            result = await original_finalize(*args, **kwargs)
            db_session = args[0]
            tournament = kwargs["tournament"]
            await db_session.execute(delete(PlayerTournamentCommitment).where(
                PlayerTournamentCommitment.tournament_id == tournament.id,
                PlayerTournamentCommitment.user_id == human["user_id"],
            ))
            await db_session.flush()
            return result

        with patch.object(
            deadlock_automation,
            "finalize_deadlock_assignment_with_commitments",
            side_effect=drop_human_commitment,
        ):
            response = await self._admin_post(
                admin, fixture["id"], "teams/generate", key=f"{self.prefix}-rollback-generate",
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "human_assignment_failed")
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == fixture["slug"]))
            self.assertIsNotNone(tournament)
            for model in (TournamentDeadlockAssignmentRun, TournamentTeam, TournamentTeamMember, PlayerTournamentCommitment):
                count = await db_session.scalar(select(func.count()).select_from(model).where(
                    model.tournament_id == tournament.id,
                ))
                self.assertEqual(count, 0, f"Failed handoff must roll back {model.__tablename__} rows.")
            from python_packages.platform_infra.models import TournamentMatch
            match_count = await db_session.scalar(select(func.count()).select_from(TournamentMatch).where(
                TournamentMatch.tournament_id == tournament.id,
            ))
            self.assertEqual(match_count, 0)

    async def test_generation_fails_closed_when_a_real_human_profile_is_missing(self) -> None:
        admin = await self._register_user("profile-block-admin")
        human = await self._register_user("profile-block-human")
        await self._grant_admin(str(admin["user_id"]))
        fixture = await self._create_fixture(
            admin, label="profile-block", visibility="public", key=f"{self.prefix}-profile-create",
        )
        await self._join(human, fixture)
        self._assert_status(await self._admin_post(
            admin, fixture["id"], "ready-check/start", key=f"{self.prefix}-profile-start",
        ), 200)
        self._assert_status(await self._admin_post(
            admin, fixture["id"], "ready-check/close", key=f"{self.prefix}-profile-close",
        ), 200)
        self._assert_status(await self._admin_post(
            admin, fixture["id"], "players", key=f"{self.prefix}-profile-fakes", payload={"count": 13},
        ), 200)
        summary = self._assert_status(await admin["client"].get(
            "/api/v1/admin/tournament-tests", headers=same_origin_request_headers(admin["client"]),
        ), 200)
        current = next(item for item in summary if item["id"] == fixture["id"])
        self.assertIn("human_profiles_missing", {item["code"] for item in current["blockers"]})
        self.assertFalse(current["capabilities"]["can_generate_teams"])
        generated = await self._admin_post(
            admin, fixture["id"], "teams/generate", key=f"{self.prefix}-profile-generate",
        )
        self.assertEqual(generated.status_code, 409, generated.text)
        self.assertEqual(generated.json()["detail"]["code"], "human_profiles_missing")
        async with session_factory()() as db_session:
            tournament_id = await db_session.scalar(select(Tournament.id).where(Tournament.slug == fixture["slug"]))
            run_count = await db_session.scalar(select(func.count()).select_from(TournamentDeadlockAssignmentRun).where(
                TournamentDeadlockAssignmentRun.tournament_id == tournament_id,
            ))
            member_count = await db_session.scalar(select(func.count()).select_from(TournamentTeamMember).where(
                TournamentTeamMember.tournament_id == tournament_id,
            ))
            self.assertEqual(run_count, 0)
            self.assertEqual(member_count, 0)
