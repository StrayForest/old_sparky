from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, update

from apps.platform_api.app.api.routes import tournaments as tournament_routes
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.deadlock_automation import advance_deadlock_tournament_automation
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.service import MediaService
from python_packages.platform_infra.media.source_store import MediaSourceStore
from python_packages.platform_infra.models import (
    AuditLog,
    MediaAsset,
    Role,
    Tournament,
    TournamentInvite,
    User,
    UserRole,
)
from python_packages.platform_infra.security import invalidate_user_session_cache


class PlatformTournamentPolicyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-policy-{uuid4().hex[:8]}"
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
            tournament_ids = tuple(
                await db_session.scalars(
                    select(Tournament.id).where(Tournament.slug.like(f"{self.prefix}%"))
                )
            )
            if tournament_ids:
                await db_session.execute(
                    update(Tournament)
                    .where(Tournament.id.in_(tournament_ids))
                    .values(banner_asset_id=None)
                )
                await db_session.execute(
                    delete(MediaAsset).where(MediaAsset.tournament_id.in_(tournament_ids))
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

    def _assert_status(self, response: httpx.Response, expected_status: int) -> dict:
        self.assertEqual(response.status_code, expected_status, response.text)
        if not response.content:
            return {}
        return response.json()

    async def _register_user(self, label: str) -> dict[str, object]:
        client = await self._new_client()
        email = f"{self.prefix}-{label}@example.com"
        payload = self._assert_status(
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": self.password,
                    "display_name": f"test-{label}"[:15],
                },
            ),
            201,
        )
        return {
            "client": client,
            "user_id": payload["user"]["id"],
            "email": email,
        }

    async def _grant_public_creation(self, user_id: str) -> None:
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user)
            user.public_tournament_credits = 100
            await db_session.commit()

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

    async def _put_deadlock_profile(self, user: dict[str, object], rank: str) -> None:
        self._assert_status(
            await user["client"].put(
                "/api/v1/profiles/me/deadlock",
                json={
                    "rank": rank,
                    "subrank": 3,
                    "playtime": "1001-1500",
                    "roles": ["Carry"],
                    "pool": ["Abrams"],
                    "captain_priority": "neutral" if rank in {"Eternus", "Ascendant"} else None,
                },
            ),
            200,
        )

    async def _advance_tournament_automation(self, slug: str, now: datetime) -> dict[str, int]:
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == slug))
            self.assertIsNotNone(tournament)
            result = await advance_deadlock_tournament_automation(
                db_session,
                tournament=tournament,
                now=now,
            )
            return result.as_dict()

    async def test_tournament_reference_fields_validate_and_future_registration_opens(self) -> None:
        organizer = await self._register_user("reference-organizer")
        await self._grant_public_creation(str(organizer["user_id"]))

        remote_cover = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-remote",
                "visibility": "public",
                "format_slug": "solo",
                "cover_url": "https://attacker.invalid/cover.png",
            },
        )
        self.assertEqual(remote_cover.status_code, 422, remote_cover.text)

        now = datetime.now(UTC).replace(microsecond=0)
        registration_start = now + timedelta(minutes=20)
        registration_close = registration_start + timedelta(minutes=10)
        ready_start = registration_close + timedelta(minutes=10)
        ready_end = ready_start + timedelta(minutes=15)
        captain_start = ready_end + timedelta(minutes=5)
        tournament_start = captain_start + timedelta(minutes=30)

        invalid_response = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-a",
                "description": "Date order validation",
                "visibility": "public",
                "format_slug": "solo",
                "registration_starts_at": registration_start.isoformat(),
                "registration_closes_at": registration_close.isoformat(),
                "ready_check_starts_at": ready_start.isoformat(),
                "ready_check_ends_at": ready_end.isoformat(),
                "captain_selection_starts_at": captain_start.isoformat(),
                "starts_at": ready_start.isoformat(),
            },
        )
        self.assertEqual(invalid_response.status_code, 422, invalid_response.text)

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-b",
                    "description": "Future registration smoke",
                    "visibility": "public",
                    "format_slug": "solo",
                    "registration_starts_at": registration_start.isoformat(),
                    "registration_closes_at": registration_close.isoformat(),
                    "ready_check_starts_at": ready_start.isoformat(),
                    "ready_check_ends_at": ready_end.isoformat(),
                    "captain_selection_starts_at": captain_start.isoformat(),
                    "starts_at": tournament_start.isoformat(),
                    "match_format": "bo1",
                    "final_format": "bo5",
                    "teams_count": 129,
                },
            ),
            201,
        )
        self.assertEqual(created["status"], "registration_closed")
        self.assertEqual(created["match_format"], "bo1")
        self.assertEqual(created["final_format"], "bo5")
        self.assertEqual(created["teams_count"], 256)
        self.assertEqual(
            datetime.fromisoformat(created["registration_starts_at"].replace("Z", "+00:00")),
            registration_start,
        )
        self.assertEqual(
            datetime.fromisoformat(created["registration_closes_at"].replace("Z", "+00:00")),
            registration_close,
        )
        slug = created["slug"]
        self.assertEqual(
            created["cover_url"],
            "/assets/tournament-covers/tournament-cover-template-1-v1.webp",
        )

        automation_result = await self._advance_tournament_automation(slug, registration_start)
        self.assertGreaterEqual(automation_result["registration_opened"], 1)
        updated = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(updated["status"], "registration_open")

        close_result = await self._advance_tournament_automation(slug, registration_close)
        self.assertGreaterEqual(close_result["registration_closed"], 1)
        updated = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}"),
            200,
        )
        self.assertEqual(updated["status"], "registration_closed")

    async def test_tournament_banner_upload_is_manager_only_staged_and_deletable(self) -> None:
        organizer = await self._register_user("cover-organizer")
        outsider = await self._register_user("cover-outsider")
        await self._grant_public_creation(str(organizer["user_id"]))

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "description": "Cover upload smoke",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = created["slug"]
        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        forbidden = await outsider["client"].post(
            f"/api/v1/tournaments/{slug}/cover",
            files={"file": ("cover.png", tiny_png, "image/png")},
        )
        self.assertEqual(forbidden.status_code, 403, forbidden.text)
        settings = get_settings()
        queued: list[str] = []
        with TemporaryDirectory() as temporary:
            source_store = MediaSourceStore(
                Path(temporary) / "private",
                max_input_bytes=settings.platform_media_max_input_bytes,
            )

            def service_factory(db_session):
                return MediaService(
                    db_session=db_session,
                    source_store=source_store,
                    processor=object(),
                    storage=object(),
                )

            with (
                patch.object(tournament_routes, "api_media_service", side_effect=service_factory),
                patch.object(
                    tournament_routes,
                    "enqueue_media_asset",
                    side_effect=lambda asset_id: queued.append(asset_id),
                ),
            ):
                invalid_type = await organizer["client"].post(
                    f"/api/v1/tournaments/{slug}/banner",
                    files={"file": ("cover.txt", b"not an image", "text/plain")},
                )
                self.assertEqual(invalid_type.status_code, 415, invalid_type.text)

                invalid_content = await organizer["client"].post(
                    f"/api/v1/tournaments/{slug}/banner",
                    files={"file": ("cover.png", b"not a png", "image/png")},
                )
                self.assertEqual(invalid_content.status_code, 415, invalid_content.text)

                oversized = (
                    b"\x89PNG\r\n\x1a\n"
                    + b"0" * settings.platform_media_max_input_bytes
                )
                too_large = await organizer["client"].post(
                    f"/api/v1/tournaments/{slug}/banner",
                    files={"file": ("cover.png", oversized, "image/png")},
                )
                self.assertEqual(too_large.status_code, 413, too_large.text)

                await self._grant_role(str(outsider["user_id"]), "admin")
                invalidate_user_session_cache(str(outsider["user_id"]))
                uploaded = self._assert_status(
                    await outsider["client"].post(
                        f"/api/v1/tournaments/{slug}/banner",
                        files={"file": ("cover.png", tiny_png, "image/png")},
                    ),
                    202,
                )
                self.assertEqual(uploaded["status"], "pending")
                self.assertEqual(queued, [uploaded["asset_id"]])

                organizer_status = await organizer["client"].get(uploaded["status_url"])
                self.assertEqual(organizer_status.status_code, 200, organizer_status.text)
                deleted = self._assert_status(
                    await organizer["client"].delete(
                        f"/api/v1/tournaments/{slug}/banner"
                    ),
                    202,
                )
                self.assertEqual(deleted["asset_id"], uploaded["asset_id"])
                self.assertEqual(deleted["status"], "cleanup_pending")

        async with session_factory()() as db_session:
            audit_actions = set(
                await db_session.scalars(
                    select(AuditLog.action).where(
                        AuditLog.subject_id == created["id"]
                    )
                )
            )
        self.assertIn("tournament.banner.upload.accepted", audit_actions)
        self.assertIn("tournament.banner.delete.accepted", audit_actions)

    async def test_regular_player_private_create_quota_auto_invite_and_format_blocks(self) -> None:
        organizer = await self._register_user("organizer")

        current_user = self._assert_status(await organizer["client"].get("/api/v1/users/me"), 200)
        self.assertEqual(current_user["private_tournament_monthly_remaining"], 1)
        self.assertEqual(current_user["private_tournament_monthly_limit"], 1)

        public_attempt = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-a",
                "visibility": "public",
                "format_slug": "solo",
            },
        )
        self.assertEqual(public_attempt.status_code, 403, public_attempt.text)

        standard_attempt = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-b",
                "visibility": "invite_only",
                "format_slug": "standard_bracket",
            },
        )
        self.assertEqual(standard_attempt.status_code, 422, standard_attempt.text)

        legacy_solo_attempt = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-legacy",
                "visibility": "invite_only",
                "format_slug": "solo_balanced_deadlock",
            },
        )
        self.assertEqual(legacy_solo_attempt.status_code, 422, legacy_solo_attempt.text)

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-c",
                    "description": "Default private policy",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        self.assertEqual(created["visibility"], "invite_only")
        self.assertEqual(created["format_slug"], "solo")

        current_user = self._assert_status(await organizer["client"].get("/api/v1/users/me"), 200)
        self.assertEqual(current_user["private_tournament_monthly_remaining"], 0)

        invites = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{created['slug']}/invites"),
            200,
        )
        self.assertEqual(len(invites), 1)
        self.assertTrue(invites[0]["is_active"])

        second_private = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-d",
                "visibility": "invite_only",
                "format_slug": "solo",
            },
        )
        self.assertEqual(second_private.status_code, 409, second_private.text)
        self.assertIn("monthly private tournament", second_private.json()["detail"])
        self.assertIn("no additional private tournament credits", second_private.json()["detail"])

        month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with session_factory()() as db_session:
            tournament = await db_session.scalar(select(Tournament).where(Tournament.id == created["id"]))
            self.assertIsNotNone(tournament)
            tournament.created_at = month_start - timedelta(seconds=1)
            await db_session.commit()

        current_user = self._assert_status(await organizer["client"].get("/api/v1/users/me"), 200)
        self.assertEqual(current_user["private_tournament_monthly_remaining"], 1)
        next_month_private = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-nm",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        self.assertEqual(next_month_private["visibility"], "invite_only")

    async def test_tournament_schedule_must_be_in_the_future_without_consuming_allowance(self) -> None:
        organizer = await self._register_user("future-schedule")
        now = datetime.now(UTC)

        past_schedule = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-past",
                "visibility": "invite_only",
                "format_slug": "solo",
                "starts_at": (now - timedelta(minutes=1)).isoformat(),
            },
        )
        self.assertEqual(past_schedule.status_code, 422, past_schedule.text)
        self.assertIn("Tournament start must be in the future", past_schedule.json()["detail"])

        future_schedule = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-future",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                    "registration_closes_at": (now + timedelta(hours=1)).isoformat(),
                    "ready_check_starts_at": (now + timedelta(hours=1)).isoformat(),
                    "ready_check_ends_at": (now + timedelta(hours=1, minutes=30)).isoformat(),
                    "captain_selection_starts_at": (now + timedelta(hours=1, minutes=30)).isoformat(),
                    "starts_at": (now + timedelta(hours=2)).isoformat(),
                },
            ),
            201,
        )
        self.assertEqual(future_schedule["status"], "registration_open")

    async def test_public_credit_is_consumed_once_across_concurrent_creates(self) -> None:
        organizer = await self._register_user("concurrent-credit")
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == organizer["user_id"]))
            self.assertIsNotNone(user)
            user.public_tournament_credits = 1
            await db_session.commit()

        responses = await asyncio.gather(
            organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-ca",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-cb",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
        )
        self.assertEqual(sorted(response.status_code for response in responses), [201, 403])
        current_user = self._assert_status(await organizer["client"].get("/api/v1/users/me"), 200)
        self.assertEqual(current_user["public_tournament_credits"], 0)

    async def test_public_name_policy_handles_private_overlap_and_concurrent_public_creates(self) -> None:
        organizer = await self._register_user("name-policy")
        await self._grant_role(str(organizer["user_id"]), "admin")
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == organizer["user_id"]))
            self.assertIsNotNone(user)
            user.private_tournament_credits = 1
            await db_session.commit()

        shared_name = f"{self.prefix}-same"
        first_private = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={"name": shared_name, "visibility": "invite_only", "format_slug": "solo"},
            ),
            201,
        )
        second_private = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={"name": shared_name, "visibility": "invite_only", "format_slug": "solo"},
            ),
            201,
        )
        self.assertNotEqual(first_private["slug"], second_private["slug"])

        public_with_private_name = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={"name": shared_name.upper(), "visibility": "public", "format_slug": "solo"},
            ),
            201,
        )
        self.assertEqual(public_with_private_name["visibility"], "public")

        duplicate_public = await organizer["client"].post(
            "/api/v1/tournaments",
            json={"name": shared_name, "visibility": "public", "format_slug": "solo"},
        )
        self.assertEqual(duplicate_public.status_code, 409, duplicate_public.text)

        private_after_public = await organizer["client"].post(
            "/api/v1/tournaments",
            json={"name": shared_name, "visibility": "invite_only", "format_slug": "solo"},
        )
        self.assertEqual(private_after_public.status_code, 409, private_after_public.text)

        concurrent_name = f"{self.prefix}-race"
        concurrent_responses = await asyncio.gather(
            organizer["client"].post(
                "/api/v1/tournaments",
                json={"name": concurrent_name, "visibility": "public", "format_slug": "solo"},
            ),
            organizer["client"].post(
                "/api/v1/tournaments",
                json={"name": concurrent_name.upper(), "visibility": "public", "format_slug": "solo"},
            ),
        )
        self.assertEqual(sorted(response.status_code for response in concurrent_responses), [201, 409])

    async def test_public_permission_allows_public_create_and_team_join_is_blocked(self) -> None:
        organizer = await self._register_user("organizer")
        player = await self._register_user("player")
        admin = await self._register_user("admin")
        await self._grant_role(admin["user_id"], "admin")

        permission = self._assert_status(
            await admin["client"].patch(
                f"/api/v1/admin/users/{organizer['user_id']}/tournament-credits",
                json={
                    "public_tournament_credits": 2,
                    "private_tournament_credits": 1,
                    "note": "Allow public event smoke coverage.",
                },
            ),
            200,
        )
        self.assertTrue(permission["can_create_public_tournaments"])
        self.assertEqual(permission["public_tournament_credits"], 2)
        self.assertEqual(permission["private_tournament_credits"], 1)

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = created["slug"]
        self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-b",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        over_quota = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-c",
                "visibility": "public",
                "format_slug": "solo",
            },
        )
        self.assertEqual(over_quota.status_code, 403, over_quota.text)
        self.assertIn("no public tournament credits", over_quota.json()["detail"])

        first_private = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-pb",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        self.assertEqual(first_private["visibility"], "invite_only")
        extra_private = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-pe",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        self.assertEqual(extra_private["visibility"], "invite_only")
        exhausted_private = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-px",
                "visibility": "invite_only",
                "format_slug": "solo",
            },
        )
        self.assertEqual(exhausted_private.status_code, 409, exhausted_private.text)
        current_user = self._assert_status(await organizer["client"].get("/api/v1/users/me"), 200)
        self.assertEqual(current_user["public_tournament_credits"], 0)
        self.assertEqual(current_user["private_tournament_credits"], 0)

        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )

        team_attempt = await player["client"].post(
            f"/api/v1/tournaments/{slug}/join",
            json={"entry_type": "team", "team_name": "No Teams"},
        )
        self.assertEqual(team_attempt.status_code, 422, team_attempt.text)

    async def test_create_tournament_rechecks_requested_invite_code(self) -> None:
        organizer = await self._register_user("invite-code-organizer")
        await self._grant_public_creation(organizer["user_id"])

        suggestion = self._assert_status(
            await organizer["client"].get("/api/v1/tournaments/invites/suggest-code"),
            200,
        )
        invite_code = suggestion["code"]
        self.assertTrue(suggestion["available"])

        availability = self._assert_status(
            await organizer["client"].get(
                "/api/v1/tournaments/invites/code-status",
                params={"code": invite_code.lower()},
            ),
            200,
        )
        self.assertEqual(availability["code"], invite_code)
        self.assertTrue(availability["available"])

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "visibility": "public",
                    "format_slug": "solo",
                    "invite_code": invite_code,
                },
            ),
            201,
        )
        self.assertEqual(created["visibility"], "public")

        async with session_factory()() as db_session:
            invite = await db_session.scalar(
                select(TournamentInvite).where(TournamentInvite.code == invite_code)
            )
            self.assertIsNotNone(invite)

        unavailable = self._assert_status(
            await organizer["client"].get(
                "/api/v1/tournaments/invites/code-status",
                params={"code": invite_code},
            ),
            200,
        )
        self.assertFalse(unavailable["available"])

        duplicate = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-b",
                "visibility": "public",
                "format_slug": "solo",
                "invite_code": invite_code,
            },
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

    async def test_legacy_standard_bracket_tournaments_are_hidden_from_player_lists(self) -> None:
        organizer = await self._register_user("organizer")
        legacy_slug = f"{self.prefix}-legacy-standard"
        supported_slug = f"{self.prefix}-supported-solo"

        async with session_factory()() as db_session:
            db_session.add_all(
                [
                    Tournament(
                        slug=legacy_slug,
                        name=f"{self.prefix} legacy standard",
                        visibility="public",
                        status="registration_closed",
                        format_slug="standard_bracket",
                        organizer_user_id=organizer["user_id"],
                    ),
                    Tournament(
                        slug=supported_slug,
                        name=f"{self.prefix} supported solo",
                        visibility="public",
                        status="registration_closed",
                        format_slug="solo",
                        organizer_user_id=organizer["user_id"],
                    ),
                ]
            )
            await db_session.commit()

        public_slugs = {
            tournament["slug"]
            for tournament in self._assert_status(await organizer["client"].get("/api/v1/tournaments"), 200)
        }
        self.assertIn(supported_slug, public_slugs)
        self.assertNotIn(legacy_slug, public_slugs)

        mine_slugs = {
            tournament["slug"]
            for tournament in self._assert_status(await organizer["client"].get("/api/v1/tournaments/mine"), 200)
        }
        self.assertIn(supported_slug, mine_slugs)
        self.assertNotIn(legacy_slug, mine_slugs)

    async def test_redeem_invite_adds_private_tournament_to_mine(self) -> None:
        organizer = await self._register_user("organizer")
        player = await self._register_user("player")

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "visibility": "invite_only",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = created["slug"]
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

        redeemed = self._assert_status(
            await player["client"].post(
                "/api/v1/tournaments/invites/claim",
                json={"code": invites[0]["code"], "entry_type": "solo", "team_name": None},
            ),
            201,
        )
        self.assertEqual(redeemed["tournament"]["slug"], slug)
        self.assertIsNone(redeemed["participant"])

        mine = self._assert_status(await player["client"].get("/api/v1/tournaments/mine"), 200)
        self.assertIn(slug, {tournament["slug"] for tournament in mine})

        participant = self._assert_status(
            await player["client"].post(f"/api/v1/tournaments/{slug}/join", json={"entry_type": "solo"}),
            201,
        )
        self.assertEqual(participant["user_id"], player["user_id"])

    async def test_ready_confirmed_participant_cannot_leave(self) -> None:
        organizer = await self._register_user("leave-organizer")
        player = await self._register_user("leave-player")
        await self._grant_public_creation(str(organizer["user_id"]))
        await self._put_deadlock_profile(player, "Phantom")

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-leave",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = created["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        self._assert_status(
            await player["client"].post(f"/api/v1/tournaments/{slug}/join", json={"entry_type": "solo"}),
            201,
        )
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
        self._assert_status(
            await player["client"].post(
                f"/api/v1/tournaments/{slug}/deadlock/ready-check/vote",
                json={"choice": "yes"},
            ),
            200,
        )

        leave_response = await player["client"].delete(f"/api/v1/tournaments/{slug}/join")
        self.assertEqual(leave_response.status_code, 409, leave_response.text)
        self.assertIn("Confirmed participants cannot leave", leave_response.json()["detail"])
        participants = self._assert_status(
            await organizer["client"].get(f"/api/v1/tournaments/{slug}/participants"),
            200,
        )
        self.assertEqual(len(participants), 1)

    async def test_participant_search_matches_nickname_and_hides_withdrawn_rows(self) -> None:
        organizer = await self._register_user("search-organizer")
        first_player = await self._register_user("search-one")
        second_player = await self._register_user("search-two")
        await self._grant_public_creation(str(organizer["user_id"]))
        await self._put_deadlock_profile(first_player, "Oracle")
        await self._put_deadlock_profile(second_player, "Phantom")

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-search",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        slug = created["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        for player in (first_player, second_player):
            self._assert_status(
                await player["client"].post(
                    f"/api/v1/tournaments/{slug}/join",
                    json={"entry_type": "solo"},
                ),
                201,
            )

        search_response = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/participants?search=search-one"
        )
        search_rows = self._assert_status(search_response, 200)
        self.assertEqual(len(search_rows), 1)
        self.assertIn("search-one", search_rows[0]["display_name"])
        self.assertEqual(search_response.headers["x-total-count"], "1")

        self._assert_status(
            await second_player["client"].delete(f"/api/v1/tournaments/{slug}/join"),
            204,
        )
        participant_response = await organizer["client"].get(
            f"/api/v1/tournaments/{slug}/participants"
        )
        participant_rows = self._assert_status(participant_response, 200)
        self.assertEqual(len(participant_rows), 1)
        self.assertEqual(participant_rows[0]["user_id"], first_player["user_id"])
        self.assertEqual(participant_response.headers["x-total-count"], "1")

    async def test_rank_limits_and_max_participants_are_enforced(self) -> None:
        organizer = await self._register_user("organizer")
        oracle_player = await self._register_user("oracle")
        phantom_player = await self._register_user("phantom")
        await self._grant_public_creation(organizer["user_id"])
        await self._put_deadlock_profile(oracle_player, "Oracle")
        await self._put_deadlock_profile(phantom_player, "Phantom")

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "visibility": "public",
                    "format_slug": "solo",
                    "allowed_ranks": ["Oracle"],
                    "max_participants": 1,
                },
            ),
            201,
        )
        self.assertEqual(created["allowed_ranks"], ["Oracle"])
        self.assertEqual(created["max_participants"], 1)
        slug = created["slug"]
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{slug}/status",
                json={"status": "registration_open"},
            ),
            200,
        )

        self._assert_status(
            await oracle_player["client"].post(f"/api/v1/tournaments/{slug}/join", json={"entry_type": "solo"}),
            201,
        )
        capacity_blocked = await phantom_player["client"].post(
            f"/api/v1/tournaments/{slug}/join",
            json={"entry_type": "solo"},
        )
        self.assertEqual(capacity_blocked.status_code, 409, capacity_blocked.text)
        self.assertIn("participant limit", capacity_blocked.json()["detail"])

        rank_only = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-b",
                    "visibility": "public",
                    "format_slug": "solo",
                    "allowed_ranks": ["Oracle"],
                    "max_participants": 2,
                },
            ),
            201,
        )
        self._assert_status(
            await organizer["client"].patch(
                f"/api/v1/tournaments/{rank_only['slug']}/status",
                json={"status": "registration_open"},
            ),
            200,
        )
        rank_blocked = await phantom_player["client"].post(
            f"/api/v1/tournaments/{rank_only['slug']}/join",
            json={"entry_type": "solo"},
        )
        self.assertEqual(rank_blocked.status_code, 409, rank_blocked.text)
        self.assertIn("outside this tournament", rank_blocked.json()["detail"])

    async def test_max_participants_accepts_nine_digit_cap_and_rejects_larger_values(self) -> None:
        organizer = await self._register_user("organizer")
        await self._grant_public_creation(organizer["user_id"])

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-a",
                    "visibility": "public",
                    "format_slug": "solo",
                    "max_participants": 999_999_999,
                },
            ),
            201,
        )
        self.assertEqual(created["max_participants"], 999_999_999)

        rejected = await organizer["client"].post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-b",
                "visibility": "public",
                "format_slug": "solo",
                "max_participants": 1_000_000_000,
            },
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)

    async def test_tournament_name_requires_english_and_limits_length(self) -> None:
        organizer = await self._register_user("organizer")
        await self._grant_public_creation(organizer["user_id"])

        created = self._assert_status(
            await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": f"{self.prefix}-ok",
                    "visibility": "public",
                    "format_slug": "solo",
                },
            ),
            201,
        )
        self.assertEqual(created["name"], f"{self.prefix}-ok")

        for invalid_name in (f"{self.prefix}-К", f"{self.prefix}-abcdefghi"):
            rejected = await organizer["client"].post(
                "/api/v1/tournaments",
                json={
                    "name": invalid_name,
                    "visibility": "public",
                    "format_slug": "solo",
                },
            )
            self.assertEqual(rejected.status_code, 422, rejected.text)
