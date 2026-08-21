from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, update

from apps.platform_api.app.main import create_app
from apps.platform_api.app.api.routes import profiles as profile_routes
from apps.platform_api.app.services import media as media_service_helpers
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.service import MediaService
from python_packages.platform_infra.media.source_store import MediaSourceStore
from python_packages.platform_infra.models import (
    AuditLog,
    MediaAsset,
    MediaVariant,
    PlayerProfile,
    User,
    UserSession,
    new_uuid,
)


class PlatformProfilesApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-profile-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
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
                await db_session.execute(
                    update(PlayerProfile)
                    .where(PlayerProfile.user_id.in_(user_ids))
                    .values(avatar_asset_id=None, banner_asset_id=None)
                )
                await db_session.execute(
                    delete(MediaAsset).where(MediaAsset.owner_user_id.in_(user_ids))
                )
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
                await db_session.commit()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )

    async def _register(self, label: str) -> httpx.AsyncClient:
        client = await self._new_client()
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-{label}@example.com",
                "password": self.password,
                "display_name": f"test-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return client

    async def test_registration_rejects_nickname_longer_than_fifteen_characters(self) -> None:
        client = await self._new_client()
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-long-name@example.com",
                "password": self.password,
                "display_name": "x" * 16,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    async def test_public_profile_hides_email_and_exposes_non_auth_contacts(self) -> None:
        owner = await self._register("owner")
        handle = f"{self.prefix}-player"
        account_response = await owner.put(
            "/api/v1/profiles/me",
            json={
                "handle": handle,
                "contact_email": "private@example.com",
                "discord_account": "private-discord",
                "region": "Private region",
            },
        )
        self.assertEqual(account_response.status_code, 200, account_response.text)
        deadlock_response = await owner.put(
            "/api/v1/profiles/me/deadlock",
            json={
                "rank": "Oracle",
                "subrank": 4,
                "playtime": "1001-1500",
                "roles": ["Carry"],
                "pool": ["Abrams"],
                "captain_priority": "neutral",
            },
        )
        self.assertEqual(deadlock_response.status_code, 200, deadlock_response.text)

        anonymous = await self._new_client()
        response = await anonymous.get(f"/api/v1/profiles/public/{handle}")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["handle"], handle)
        self.assertEqual(payload["deadlock_profile"]["rank"], "Oracle")
        self.assertNotIn("contact_email", payload)
        self.assertNotIn("steam_id", payload)
        self.assertNotIn("steam_linked", payload)
        self.assertEqual(payload["discord_account"], "private-discord")
        self.assertEqual(payload["region"], "Private region")

    async def test_profile_rejects_manual_steam_identity(self) -> None:
        owner = await self._register("manual-steam")
        response = await owner.put(
            "/api/v1/profiles/me",
            json={"steam_id": "76561198000000000"},
        )
        self.assertEqual(response.status_code, 422, response.text)

    async def test_unknown_profile_is_not_resolvable(self) -> None:
        owner = await self._register("known")
        handle = f"{self.prefix}-known"
        response = await owner.put(
            "/api/v1/profiles/me",
            json={"handle": handle},
        )
        self.assertEqual(response.status_code, 200, response.text)

        anonymous = await self._new_client()
        known_response = await anonymous.get(f"/api/v1/profiles/public/{handle}")
        missing_response = await anonymous.get(f"/api/v1/profiles/public/{self.prefix}-missing")
        self.assertEqual(known_response.status_code, 200, known_response.text)
        self.assertEqual(missing_response.status_code, 404, missing_response.text)

    async def test_captain_team_name_is_saved_on_profile(self) -> None:
        owner = await self._register("captain")
        response = await owner.put(
            "/api/v1/profiles/me",
            json={"captain_team_name": "Alpha Team"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["captain_team_name"], "Alpha Team")

        loaded = await owner.get("/api/v1/profiles/me")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["captain_team_name"], "Alpha Team")

        cleared = await owner.put(
            "/api/v1/profiles/me",
            json={"captain_team_name": ""},
        )
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertIsNone(cleared.json()["captain_team_name"])

    async def test_profile_update_rejects_arbitrary_media_urls(self) -> None:
        owner = await self._register("media-url")
        response = await owner.put(
            "/api/v1/profiles/me",
            json={
                "avatar_url": "https://attacker.invalid/avatar.png",
                "banner_url": "https://attacker.invalid/banner.png",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

        profile = await owner.get("/api/v1/profiles/me")
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertIsNone(profile.json()["avatar_url"])
        self.assertIsNone(profile.json()["banner_url"])

    async def test_ready_profile_media_is_serialized_from_database_metadata(self) -> None:
        owner = await self._register("ready-media")
        me = await owner.get("/api/v1/users/me")
        self.assertEqual(me.status_code, 200, me.text)
        user_id = me.json()["id"]
        asset_id = new_uuid()
        handle = f"{self.prefix}-ready"
        object_key = f"public/avatars/{user_id}/{asset_id}/avatar-256.webp"

        async with session_factory()() as db_session:
            db_session.add(
                MediaAsset(
                    id=asset_id,
                    owner_user_id=user_id,
                    purpose="profile_avatar",
                    status="ready",
                    source_mime="image/png",
                    source_bytes=68,
                    source_sha256="a" * 64,
                    version_id=new_uuid(),
                    attempt_count=1,
                )
            )
            await db_session.flush()
            db_session.add(
                MediaVariant(
                    id=new_uuid(),
                    asset_id=asset_id,
                    variant_name="avatar-256",
                    object_key=object_key,
                    mime_type="image/webp",
                    width=256,
                    height=256,
                    byte_size=1234,
                    sha256="b" * 64,
                )
            )
            profile = await db_session.scalar(
                select(PlayerProfile).where(PlayerProfile.user_id == user_id)
            )
            profile.handle = handle
            profile.avatar_asset_id = asset_id
            profile.avatar_url = None
            await db_session.commit()

        settings = SimpleNamespace(
            platform_media_public_base_url="https://media.example.test"
        )
        with patch.object(media_service_helpers, "get_settings", return_value=settings):
            response = await owner.get("/api/v1/profiles/me")
            self.assertEqual(response.status_code, 200, response.text)
            expected_url = f"https://media.example.test/{object_key}"
            self.assertEqual(response.json()["avatar_url"], expected_url)
            self.assertEqual(
                response.json()["avatar_media"]["variants"][0]["url"],
                expected_url,
            )

            anonymous = await self._new_client()
            public = await anonymous.get(f"/api/v1/profiles/public/{handle}")
            self.assertEqual(public.status_code, 200, public.text)
            self.assertEqual(public.json()["avatar_url"], expected_url)

            owned_status = await owner.get(f"/api/v1/media/{asset_id}/status")
            self.assertEqual(owned_status.status_code, 200, owned_status.text)
            self.assertEqual(owned_status.json()["variants"][0]["url"], expected_url)

    async def test_account_password_change_requires_current_password_and_revokes_other_sessions(self) -> None:
        owner = await self._register("security")
        email = f"{self.prefix}-security@example.com"
        second_session = await self._new_client()
        login = await second_session.post(
            "/api/v1/auth/login",
            json={"email": email, "password": self.password},
        )
        self.assertEqual(login.status_code, 200, login.text)

        new_password = "new-integration-pass-456"
        rejected = await owner.patch(
            "/api/v1/auth/account",
            json={
                "current_password": "wrong-password",
                "new_password": new_password,
            },
        )
        self.assertEqual(rejected.status_code, 401, rejected.text)

        updated = await owner.patch(
            "/api/v1/auth/account",
            json={
                "current_password": self.password,
                "new_password": new_password,
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["email"], email)
        synced_profile = await owner.get("/api/v1/profiles/me")
        self.assertEqual(synced_profile.status_code, 200, synced_profile.text)
        self.assertEqual(synced_profile.json()["contact_email"], email)

        current_session = await owner.get("/api/v1/users/me")
        self.assertEqual(current_session.status_code, 200, current_session.text)
        self.assertEqual(current_session.json()["email"], email)
        revoked_session = await second_session.get("/api/v1/users/me")
        self.assertEqual(revoked_session.status_code, 401, revoked_session.text)

        fresh = await self._new_client()
        old_login = await fresh.post(
            "/api/v1/auth/login",
            json={"email": email, "password": self.password},
        )
        self.assertEqual(old_login.status_code, 401, old_login.text)
        new_login = await fresh.post(
            "/api/v1/auth/login",
            json={"email": email, "password": new_password},
        )
        self.assertEqual(new_login.status_code, 200, new_login.text)

        async with session_factory()() as db_session:
            active_sessions = list(
                (
                    await db_session.scalars(
                        select(UserSession).where(
                            UserSession.user_id == updated.json()["id"],
                            UserSession.invalidated_at.is_(None),
                        )
                    )
                ).all()
            )
            self.assertEqual(len(active_sessions), 2)

    async def test_avatar_upload_is_staged_owner_safe_and_replaces_inflight_media(self) -> None:
        owner = await self._register("avatar")
        outsider = await self._register("avatar-outsider")
        profile = await owner.get("/api/v1/profiles/me")
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["account_email"], f"{self.prefix}-avatar@example.com")

        tiny_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
            b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
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
                patch.object(profile_routes, "api_media_service", side_effect=service_factory),
                patch.object(
                    profile_routes,
                    "enqueue_media_asset",
                    side_effect=lambda asset_id: queued.append(asset_id),
                ),
            ):
                gif_rejected = await owner.post(
                    "/api/v1/profiles/me/avatar",
                    files={"file": ("avatar.gif", b"GIF89a", "image/gif")},
                )
                self.assertEqual(gif_rejected.status_code, 415, gif_rejected.text)
                self.assertEqual(gif_rejected.json()["detail"]["code"], "unsupported_media_type")

                oversized = b"\x89PNG\r\n\x1a\n" + b"0" * settings.platform_media_max_input_bytes
                too_large = await owner.post(
                    "/api/v1/profiles/me/avatar",
                    files={"file": ("avatar.png", oversized, "image/png")},
                )
                self.assertEqual(too_large.status_code, 413, too_large.text)
                self.assertEqual(too_large.json()["detail"]["code"], "media_too_large")

                first = await owner.post(
                    "/api/v1/profiles/me/avatar",
                    files={"file": ("avatar.png", tiny_png, "image/png")},
                )
                self.assertEqual(first.status_code, 202, first.text)
                first_asset_id = first.json()["asset_id"]
                self.assertEqual(first.json()["status"], "pending")
                self.assertEqual(queued, [first_asset_id])

                owned_status = await owner.get(first.json()["status_url"])
                self.assertEqual(owned_status.status_code, 200, owned_status.text)
                self.assertEqual(owned_status.json()["status"], "pending")
                hidden_status = await outsider.get(first.json()["status_url"])
                self.assertEqual(hidden_status.status_code, 404, hidden_status.text)

                second = await owner.post(
                    "/api/v1/profiles/me/avatar",
                    files={"file": ("avatar.png", tiny_png, "image/png")},
                )
                self.assertEqual(second.status_code, 202, second.text)
                replaced_status = await owner.get(first.json()["status_url"])
                self.assertEqual(replaced_status.json()["status"], "replaced")

                deleted = await owner.delete("/api/v1/profiles/me/avatar")
                self.assertEqual(deleted.status_code, 202, deleted.text)
                self.assertEqual(deleted.json()["status"], "cleanup_pending")
                second_status = await owner.get(second.json()["status_url"])
                self.assertEqual(second_status.json()["status"], "replaced")

        owner_record = await owner.get("/api/v1/users/me")
        self.assertEqual(owner_record.status_code, 200, owner_record.text)
        async with session_factory()() as db_session:
            audit_actions = set(
                await db_session.scalars(
                    select(AuditLog.action).where(
                        AuditLog.actor_user_id == owner_record.json()["id"]
                    )
                )
            )
        self.assertIn("profile.avatar.upload.accepted", audit_actions)
        self.assertIn("profile.avatar.delete.accepted", audit_actions)