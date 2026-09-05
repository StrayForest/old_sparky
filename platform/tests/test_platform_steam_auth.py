from __future__ import annotations

import unittest
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from starlette.datastructures import QueryParams

from apps.platform_api.app.api.routes import auth as auth_routes
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.steam_openid import (
    OPENID_NS,
    STEAM_OPENID_ENDPOINT,
    SteamOpenIDError,
    SteamOpenIDVerificationError,
    normalize_return_path,
    verify_openid_assertion,
)
from python_packages.platform_infra import auth_rate_limit
from python_packages.platform_infra.auth_rate_limit import (
    reserve_auth_delivery_cooldown,
)
from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    EmailVerificationToken,
    ExternalIdentity,
    SteamAuthFlow,
    SteamEmailLinkIntent,
    User,
)
from python_packages.platform_infra.security import invalidate_user_session_cache
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class _CooldownRedis:
    def __init__(self, ttl: int = 60) -> None:
        self.ttl = ttl
        self.reserved: set[str] = set()

    async def eval(
        self,
        _script: str,
        _key_count: int,
        key: str,
        _seconds: int,
    ) -> int:
        if key in self.reserved:
            return self.ttl
        self.reserved.add(key)
        return 0

    async def aclose(self) -> None:
        return None


class SteamOpenIDUnitTests(PlatformIsolatedAsyncioTestCase):
    def _settings(self, **overrides: object) -> PlatformSettings:
        values: dict[str, object] = {
            "_env_file": None,
            "platform_secret_key": "steam-unit-test-secret",
            "platform_auth_flow_ttl_minutes": 15,
        }
        values.update(overrides)
        return PlatformSettings(**values)

    def _assertion(self, return_to: str, steam_id: str = "76561198000000001") -> dict[str, str]:
        # Steam documents the canonical Claimed ID with the http scheme even
        # though provider assertion verification itself is pinned to HTTPS.
        claimed_id = f"http://steamcommunity.com/openid/id/{steam_id}"
        return {
            "openid.ns": OPENID_NS,
            "openid.mode": "id_res",
            "openid.op_endpoint": STEAM_OPENID_ENDPOINT,
            "openid.claimed_id": claimed_id,
            "openid.identity": claimed_id,
            "openid.return_to": return_to,
            "openid.response_nonce": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "nonce",
            "openid.assoc_handle": "test-association",
            "openid.signed": (
                "op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle"
            ),
            "openid.sig": "provider-signature",
        }

    async def test_openid_verification_is_provider_pinned_and_strict(self) -> None:
        return_to = "https://old-sparky.com/api/v1/auth/steam/callback?state=opaque"
        assertion = self._assertion(return_to)

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), STEAM_OPENID_ENDPOINT)
            self.assertEqual(request.method, "POST")
            self.assertIn(b"openid.mode=check_authentication", await request.aread())
            return httpx.Response(200, text="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            steam_id = await verify_openid_assertion(
                assertion,
                return_to,
                self._settings(),
                client,
            )
        self.assertEqual(steam_id, "76561198000000001")

        for key, value in (
            ("openid.op_endpoint", "https://evil.example/openid"),
            ("openid.return_to", "https://evil.example/callback"),
            ("openid.identity", "http://steamcommunity.com/openid/id/76561198000000002"),
            ("openid.claimed_id", "http://steamcommunity.com/openid/id/not-a-steam-id"),
            ("openid.claimed_id", "https://steamcommunity.com/openid/id/76561198000000001"),
        ):
            invalid = {**assertion, key: value}
            with self.subTest(key=key), self.assertRaises(SteamOpenIDError):
                await verify_openid_assertion(invalid, return_to, self._settings(), client)

    async def test_openid_rejects_duplicate_parameters_and_unsafe_return_paths(self) -> None:
        return_to = "https://old-sparky.com/api/v1/auth/steam/callback?state=opaque"
        duplicate = QueryParams(
            [*self._assertion(return_to).items(), ("openid.sig", "second-signature")]
        )
        with self.assertRaises(SteamOpenIDError):
            await verify_openid_assertion(duplicate, return_to, self._settings())

        self.assertEqual(normalize_return_path("/profile?tab=account"), "/profile?tab=account")
        for unsafe in ("https://evil.example", "//evil.example", "/../admin", "/%2e%2e/admin", "/a\\b"):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(normalize_return_path(unsafe), "/")

    async def test_delivery_cooldown_is_atomic_and_returns_retry_after(self) -> None:
        settings = self._settings(
            platform_auth_rate_limit_enabled=True,
            platform_auth_delivery_cooldown_seconds=60,
        )
        cache = _CooldownRedis(ttl=43)
        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            reserved = await reserve_auth_delivery_cooldown(
                "player@example.com",
                scope="email-verification",
                settings=settings,
            )
            with self.assertRaises(HTTPException) as raised:
                await reserve_auth_delivery_cooldown(
                    "player@example.com",
                    scope="email-verification",
                    settings=settings,
                )
        self.assertEqual(reserved, 60)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "43")


class SteamAuthIntegrationTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-steam-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
        self.app = create_app()
        self.clients = AsyncExitStack()
        self.created_user_ids: set[str] = set()
        self.settings = PlatformSettings(
            _env_file=None,
            platform_steam_callback_url=(
                "http://testserver/api/v1/auth/steam/callback"
            ),
            platform_steam_login_enabled=True,
            platform_auth_generic_response_min_seconds=0,
        )

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        async with session_factory()() as db_session:
            identity_user_ids = set(
                (
                    await db_session.scalars(
                        select(ExternalIdentity.user_id).where(
                            ExternalIdentity.subject.like("7656119800001%")
                        )
                    )
                ).all()
            )
            email_user_ids = set(
                (
                    await db_session.scalars(
                        select(User.id).where(
                            User.email.like(f"{self.prefix}-%@example.com")
                        )
                    )
                ).all()
            )
            user_ids = self.created_user_ids | identity_user_ids | email_user_ids
            await db_session.execute(
                delete(SteamAuthFlow).where(
                    SteamAuthFlow.return_path.like(f"/{self.prefix}%")
                )
            )
            if user_ids:
                await db_session.execute(
                    delete(AuditLog).where(
                        or_(
                            AuditLog.actor_user_id.in_(user_ids),
                            (
                                (AuditLog.subject_type == "user")
                                & AuditLog.subject_id.in_(user_ids)
                            ),
                        )
                    )
                )
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
                for user_id in user_ids:
                    invalidate_user_session_cache(user_id)
            await db_session.commit()
        await dispose_engine()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
                follow_redirects=False,
            )
        )

    async def _register_email_user(
        self,
        client: httpx.AsyncClient,
        label: str,
    ) -> dict[str, object]:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-{label}@example.com",
                "password": self.password,
                "display_name": f"safe-{label}"[:15],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.created_user_ids.add(payload["user"]["id"])
        return payload

    async def _steam_callback(
        self,
        client: httpx.AsyncClient,
        *,
        steam_id: str,
        purpose: str = "login",
        return_path: str | None = None,
    ) -> httpx.Response:
        target_path = return_path or f"/{self.prefix}/profile"
        start_path = f"/api/v1/auth/steam/{purpose}/start"
        with patch.object(auth_routes, "get_settings", return_value=self.settings):
            started = await client.post(
                start_path,
                json={"return_to": target_path},
            )
        self.assertEqual(started.status_code, 200, started.text)
        authorization_query = parse_qs(urlparse(started.json()["authorization_url"]).query)
        return_to = authorization_query["openid.return_to"][0]
        state = parse_qs(urlparse(return_to).query)["state"][0]
        with (
            patch.object(auth_routes, "get_settings", return_value=self.settings),
            patch.object(
                auth_routes,
                "verify_openid_assertion",
                AsyncMock(return_value=steam_id),
            ) as verifier,
        ):
            callback = await client.get(
                "/api/v1/auth/steam/callback",
                params={"state": state, "openid.mode": "id_res"},
            )
        if callback.status_code == 303 and "steam_auth=success" in callback.headers.get("location", ""):
            verifier.assert_awaited_once()
        return callback

    async def test_unknown_steam_creates_full_user_and_subsequent_login_reuses_it(self) -> None:
        steam_id = "76561198000010001"
        first_client = await self._new_client()
        callback = await self._steam_callback(first_client, steam_id=steam_id)
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertIn("steam_auth=success", callback.headers["location"])

        current = await first_client.get("/api/v1/users/me")
        self.assertEqual(current.status_code, 200, current.text)
        user = current.json()
        self.created_user_ids.add(user["id"])
        self.assertIsNone(user["email"])
        self.assertFalse(user["has_password"])
        self.assertTrue(user["steam_linked"])
        self.assertEqual(user["steam_id"], steam_id)
        self.assertEqual(set(user["roles"]), {"authenticated_user", "player"})
        profile = await first_client.get("/api/v1/profiles/me")
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["user_id"], user["id"])

        second_client = await self._new_client()
        second_callback = await self._steam_callback(second_client, steam_id=steam_id)
        self.assertEqual(second_callback.status_code, 303, second_callback.text)
        second_user = (await second_client.get("/api/v1/users/me")).json()
        self.assertEqual(second_user["id"], user["id"])
        async with session_factory()() as db_session:
            identity_count = len(
                (
                    await db_session.scalars(
                        select(ExternalIdentity.id).where(
                            ExternalIdentity.provider == "steam",
                            ExternalIdentity.subject == steam_id,
                        )
                    )
                ).all()
            )
        self.assertEqual(identity_count, 1)

    async def test_kill_switch_blocks_new_flows_but_security_config_reports_it(self) -> None:
        client = await self._new_client()
        disabled = self.settings.model_copy(
            update={"platform_steam_login_enabled": False}
        )
        with patch.object(auth_routes, "get_settings", return_value=disabled):
            security_config = await client.get("/api/v1/auth/security-config")
            start = await client.post(
                "/api/v1/auth/steam/login/start",
                json={"return_to": "/"},
            )
        self.assertEqual(security_config.status_code, 200, security_config.text)
        self.assertFalse(security_config.json()["steam_login_enabled"])
        self.assertEqual(start.status_code, 503, start.text)

    async def test_link_is_session_bound_rotates_sessions_and_rejects_collision(self) -> None:
        steam_id = "76561198000010002"
        owner = await self._new_client()
        owner_payload = await self._register_email_user(owner, "owner")
        second_owner_session = await self._new_client()
        logged_in = await second_owner_session.post(
            "/api/v1/auth/login",
            json={
                "email": owner_payload["user"]["email"],
                "password": self.password,
            },
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        linked = await self._steam_callback(owner, steam_id=steam_id, purpose="link")
        self.assertEqual(linked.status_code, 303, linked.text)
        self.assertEqual((await second_owner_session.get("/api/v1/users/me")).status_code, 401)
        linked_user = (await owner.get("/api/v1/users/me")).json()
        self.assertEqual(linked_user["steam_id"], steam_id)

        collision_client = await self._new_client()
        collision_payload = await self._register_email_user(collision_client, "collision")
        rejected = await self._steam_callback(
            collision_client,
            steam_id=steam_id,
            purpose="link",
        )
        self.assertEqual(rejected.status_code, 303, rejected.text)
        self.assertIn("steam_auth=error", rejected.headers["location"])
        still_authenticated = await collision_client.get("/api/v1/users/me")
        self.assertEqual(still_authenticated.status_code, 200, still_authenticated.text)
        self.assertEqual(still_authenticated.json()["id"], collision_payload["user"]["id"])
        self.assertFalse(still_authenticated.json()["steam_linked"])

    async def test_callback_rejects_missing_browser_grant_without_provider_call(self) -> None:
        client = await self._new_client()
        with patch.object(auth_routes, "get_settings", return_value=self.settings):
            started = await client.post(
                "/api/v1/auth/steam/login/start",
                json={"return_to": f"/{self.prefix}/login"},
            )
        return_to = parse_qs(urlparse(started.json()["authorization_url"]).query)[
            "openid.return_to"
        ][0]
        state = parse_qs(urlparse(return_to).query)["state"][0]
        client.cookies.clear()
        with (
            patch.object(auth_routes, "get_settings", return_value=self.settings),
            patch.object(
                auth_routes,
                "verify_openid_assertion",
                new_callable=AsyncMock,
            ) as verifier,
        ):
            callback = await client.get(
                "/api/v1/auth/steam/callback",
                params={"state": state},
            )
        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertIn("steam_auth=error", callback.headers["location"])
        verifier.assert_not_awaited()

    async def test_callback_errors_redirect_to_safe_web_routes(self) -> None:
        client = await self._new_client()
        with patch.object(auth_routes, "get_settings", return_value=self.settings):
            missing_state = await client.get("/api/v1/auth/steam/callback")
            unknown_state = await client.get(
                "/api/v1/auth/steam/callback",
                params={"state": "unknown-state-secret"},
            )
        for response in (missing_state, unknown_state):
            self.assertEqual(response.status_code, 303, response.text)
            self.assertEqual(
                response.headers["location"],
                "http://127.0.0.1:3000/auth/login?steam_auth=error",
            )

        with patch.object(auth_routes, "get_settings", return_value=self.settings):
            started = await client.post(
                "/api/v1/auth/steam/login/start",
                json={"return_to": f"/{self.prefix}/provider-failure"},
            )
        return_to = parse_qs(urlparse(started.json()["authorization_url"]).query)[
            "openid.return_to"
        ][0]
        state = parse_qs(urlparse(return_to).query)["state"][0]
        with (
            patch.object(auth_routes, "get_settings", return_value=self.settings),
            patch.object(
                auth_routes,
                "verify_openid_assertion",
                AsyncMock(
                    side_effect=SteamOpenIDVerificationError("provider timeout")
                ),
            ),
        ):
            provider_timeout = await client.get(
                "/api/v1/auth/steam/callback",
                params={"state": state, "openid.mode": "id_res"},
            )
        self.assertEqual(provider_timeout.status_code, 303, provider_timeout.text)
        self.assertEqual(
            provider_timeout.headers["location"],
            f"http://127.0.0.1:3000/{self.prefix}/provider-failure?steam_auth=error",
        )

        with patch.object(auth_routes, "get_settings", return_value=self.settings):
            started = await client.post(
                "/api/v1/auth/steam/login/start",
                json={"return_to": f"/{self.prefix}/rate-limited"},
            )
        return_to = parse_qs(urlparse(started.json()["authorization_url"]).query)[
            "openid.return_to"
        ][0]
        state = parse_qs(urlparse(return_to).query)["state"][0]
        with (
            patch.object(auth_routes, "get_settings", return_value=self.settings),
            patch.object(
                auth_routes,
                "check_steam_auth_rate_limit",
                AsyncMock(
                    side_effect=HTTPException(
                        status_code=429,
                        detail="Too many requests.",
                        headers={"Retry-After": "60"},
                    )
                ),
            ),
            patch.object(
                auth_routes,
                "verify_openid_assertion",
                new_callable=AsyncMock,
            ) as verifier,
        ):
            rate_limited = await client.get(
                "/api/v1/auth/steam/callback",
                params={"state": state, "openid.mode": "id_res"},
            )
        self.assertEqual(rate_limited.status_code, 303, rate_limited.text)
        self.assertEqual(
            rate_limited.headers["location"],
            f"http://127.0.0.1:3000/{self.prefix}/rate-limited?steam_auth=error",
        )
        verifier.assert_not_awaited()

    async def test_pending_account_recovery_replaces_password_and_activates(self) -> None:
        email = f"{self.prefix}-pending@example.com"
        registering_client = await self._new_client()
        recovery_client = await self._new_client()
        settings = self.settings.model_copy(
            update={
                "platform_email_verification_required": True,
                "platform_support_smtp_host": "smtp.example.com",
                "platform_support_smtp_sender_email": "noreply@old-sparky.com",
            }
        )
        verification_mail = AsyncMock()
        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_email_verification_email", verification_mail),
        ):
            registered = await registering_client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": self.password,
                    "display_name": "pending-user",
                },
            )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertIsNone(registered.json()["user"])
        async with session_factory()() as db_session:
            user_id = await db_session.scalar(select(User.id).where(User.email == email))
        self.assertIsNotNone(user_id)
        assert user_id is not None
        self.created_user_ids.add(user_id)

        reset_mail = AsyncMock()
        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_password_reset_email", reset_mail),
            patch.object(auth_routes, "verify_turnstile_token", new_callable=AsyncMock),
        ):
            requested = await recovery_client.post(
                "/api/v1/auth/password-reset/request",
                json={"email": email},
            )
            self.assertEqual(requested.status_code, 202, requested.text)
            reset_code = reset_mail.await_args.kwargs["code"]
            new_password = "recovered-password-456"
            recovered = await recovery_client.post(
                "/api/v1/auth/password-reset/confirm",
                json={
                    "email": email,
                    "code": reset_code,
                    "new_password": new_password,
                },
            )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["user"]["status"], "active")
        async with session_factory()() as db_session:
            user = await db_session.get(User, user_id)
            audit = await db_session.scalar(
                select(AuditLog).where(
                    AuditLog.subject_id == user_id,
                    AuditLog.action == "auth.pending_account.recover",
                )
            )
            live_verification_tokens = len(
                (
                    await db_session.scalars(
                        select(EmailVerificationToken.id).where(
                            EmailVerificationToken.user_id == user_id,
                            EmailVerificationToken.consumed_at.is_(None),
                        )
                    )
                ).all()
            )
        self.assertIsNotNone(user)
        assert user is not None
        self.assertIsNotNone(user.email_verified_at)
        self.assertIsNotNone(audit)
        self.assertEqual(live_verification_tokens, 0)

        old_login_client = await self._new_client()
        with patch.object(auth_routes, "get_settings", return_value=settings):
            old_login = await old_login_client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": self.password},
            )
            new_login = await old_login_client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": new_password},
            )
        self.assertEqual(old_login.status_code, 401, old_login.text)
        self.assertEqual(new_login.status_code, 200, new_login.text)

    async def test_verification_requires_originating_registration_browser(self) -> None:
        email = f"{self.prefix}-grant@example.com"
        origin_client = await self._new_client()
        other_client = await self._new_client()
        settings = self.settings.model_copy(
            update={
                "platform_email_verification_required": True,
                "platform_support_smtp_host": "smtp.example.com",
                "platform_support_smtp_sender_email": "noreply@old-sparky.com",
            }
        )
        mail = AsyncMock()
        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_email_verification_email", mail),
            patch.object(
                auth_routes,
                "verify_turnstile_token",
                new_callable=AsyncMock,
            ),
        ):
            registered = await origin_client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": self.password,
                    "display_name": "grant-user",
                },
            )
            code = mail.await_args.kwargs["code"]
            mail.reset_mock()
            standalone_resend = await other_client.post(
                "/api/v1/auth/email-verification/resend",
                json={"email": email},
            )
            rejected = await other_client.post(
                "/api/v1/auth/email-verification/confirm",
                json={"email": email, "code": code},
            )
            confirmed = await origin_client.post(
                "/api/v1/auth/email-verification/confirm",
                json={"email": email, "code": code},
            )
        self.assertIsNone(registered.json()["user"])
        async with session_factory()() as db_session:
            user_id = await db_session.scalar(select(User.id).where(User.email == email))
        self.assertIsNotNone(user_id)
        assert user_id is not None
        self.created_user_ids.add(user_id)
        self.assertEqual(standalone_resend.status_code, 202, standalone_resend.text)
        mail.assert_not_awaited()
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

    async def test_steam_email_link_is_confirmed_before_write_and_reset_creates_password(self) -> None:
        steam_id = "76561198000010003"
        client = await self._new_client()
        callback = await self._steam_callback(client, steam_id=steam_id)
        self.assertEqual(callback.status_code, 303, callback.text)
        current = (await client.get("/api/v1/users/me")).json()
        user_id = current["id"]
        self.created_user_ids.add(user_id)
        email = f"{self.prefix}-steam-email@example.com"
        mail_settings = self.settings.model_copy(
            update={
                "platform_support_smtp_host": "smtp.example.com",
                "platform_support_smtp_sender_email": "noreply@old-sparky.com",
            }
        )
        email_mail = AsyncMock()
        with (
            patch.object(auth_routes, "get_settings", return_value=mail_settings),
            patch.object(auth_routes, "send_email_verification_email", email_mail),
        ):
            requested = await client.post(
                "/api/v1/auth/email-link/request",
                json={"email": email},
            )
            self.assertEqual(requested.status_code, 202, requested.text)
            async with session_factory()() as db_session:
                before_confirm = await db_session.get(User, user_id)
                intent = await db_session.scalar(
                    select(SteamEmailLinkIntent).where(
                        SteamEmailLinkIntent.user_id == user_id,
                        SteamEmailLinkIntent.consumed_at.is_(None),
                    )
                )
            self.assertIsNone(before_confirm.email if before_confirm else "missing")
            self.assertIsNotNone(intent)
            confirmed = await client.post(
                "/api/v1/auth/email-link/confirm",
                json={"email": email, "code": email_mail.await_args.kwargs["code"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["email"], email)
        self.assertFalse(confirmed.json()["has_password"])

        reset_mail = AsyncMock()
        with (
            patch.object(auth_routes, "get_settings", return_value=mail_settings),
            patch.object(auth_routes, "send_password_reset_email", reset_mail),
            patch.object(auth_routes, "verify_turnstile_token", new_callable=AsyncMock),
        ):
            reset_requested = await client.post(
                "/api/v1/auth/password-reset/request",
                json={"email": email},
            )
            self.assertEqual(reset_requested.status_code, 202, reset_requested.text)
            password_created = await client.post(
                "/api/v1/auth/password-reset/confirm",
                json={
                    "email": email,
                    "code": reset_mail.await_args.kwargs["code"],
                    "new_password": "first-email-password-789",
                },
            )
        self.assertEqual(password_created.status_code, 200, password_created.text)
        self.assertTrue(password_created.json()["user"]["has_password"])


if __name__ == "__main__":
    unittest.main()
