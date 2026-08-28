from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, patch
import unittest
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
import httpx
from sqlalchemy import delete, or_, select, update

from apps.platform_api.app.main import create_app
from apps.platform_api.app.api.routes import auth as auth_routes
from apps.platform_api.app.services import auth_mail
from python_packages.platform_infra import auth_rate_limit
from python_packages.platform_infra.auth_lifecycle import (
    issue_password_reset_token,
    one_time_code_digest,
)
from python_packages.platform_infra.auth_rate_limit import (
    check_registration_rate_limit,
    check_password_reset_rate_limit,
    progressive_delay_seconds,
    record_login_failure,
)
from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.csrf import (
    CsrfProtectionMiddleware,
    PUBLIC_AUTH_PATHS,
    csrf_cookie_name,
    generate_csrf_token,
    issue_csrf_token,
)
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    EmailVerificationToken,
    PasswordResetToken,
    PlayerProfile,
    Role,
    SteamEmailLinkIntent,
    User,
    UserRole,
    UserSession,
)
from python_packages.platform_infra.security import (
    get_optional_authenticated_session,
    invalidate_user_session_cache,
    public_registration_enabled,
    set_session_cookie,
    validate_auth_security_settings,
)
from python_packages.platform_infra.turnstile import verify_turnstile_token
from tools.platform_create_operator import bootstrap_operator, normalize_confirmed_email


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def eval(self, script: str, key_count: int, key: str, expires_at: int) -> int:
        del script, key_count, expires_at
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def get(self, key: str) -> int | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def aclose(self) -> None:
        return None


class AuthSecurityUnitTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, **overrides: object) -> PlatformSettings:
        values: dict[str, object] = {
            "_env_file": None,
            "platform_secret_key": "unit-test-secret-key",
            "platform_web_origin": "https://old-sparky.com",
            "platform_session_cookie_name": "__Host-old_sparky_session",
            "platform_cookie_secure": True,
            "platform_public_registration_enabled": None,
        }
        values.update(overrides)
        return PlatformSettings(**values)

    async def test_one_time_code_digest_is_bound_to_the_server_secret(self) -> None:
        first = one_time_code_digest("user-1", "123456", secret_key="first-secret")
        second = one_time_code_digest("user-1", "123456", secret_key="second-secret")

        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)
        self.assertNotIn("123456", first)

    async def test_auth_email_contains_only_short_lived_code(self) -> None:
        settings = self._settings(
            platform_email_sender_email="support@old-sparky.com",
            platform_password_reset_ttl_minutes=10,
        )
        with patch.object(auth_mail, "send_email_message", AsyncMock()) as send:
            await auth_mail.send_password_reset_email(
                settings,
                recipient_email="player@example.com",
                code="123456",
            )

        message = send.await_args.args[1]
        self.assertIn("123456", message.get_content())
        self.assertIn("10 минут", message.get_content())
        self.assertNotIn("http", message.get_content())

    async def _csrf_client(self) -> tuple[httpx.AsyncClient, PlatformSettings]:
        settings = self._settings(platform_csrf_enabled=True)
        app = FastAPI()
        app.add_middleware(
            CsrfProtectionMiddleware,
            settings_factory=lambda: settings,
        )

        @app.post("/unsafe")
        async def unsafe() -> dict[str, bool]:
            return {"accepted": True}

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://old-sparky.com",
        )
        return client, settings

    def test_issue_csrf_token_sets_matching_cookie_and_response_header(self) -> None:
        settings = self._settings()
        response = Response()

        token = issue_csrf_token(response, "session-token", settings)

        cookies = SimpleCookie()
        cookies.load(response.headers["set-cookie"])
        self.assertEqual(response.headers["X-CSRF-Token"], token)
        self.assertEqual(cookies[csrf_cookie_name(settings)].value, token)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_non_production_cors_exposes_browser_response_headers(self) -> None:
        settings = self._settings(
            platform_environment="test",
            platform_database_url=(
                "postgresql+asyncpg://platform_test:platform_test@127.0.0.1:5432/"
                "platformdb_test"
            ),
            platform_redis_url="redis://127.0.0.1:6379/15",
            platform_object_storage_backend="local",
            platform_csrf_enabled=True,
        )
        with patch(
            "apps.platform_api.app.main.get_settings",
            return_value=settings,
        ):
            app = create_app()

            @app.get("/cors-pagination-probe")
            async def cors_pagination_probe() -> Response:
                return Response(
                    headers={
                        "Access-Control-Expose-Headers": (
                            "X-Total-Count, X-Limit, X-Offset, X-Has-More"
                        ),
                        "X-Total-Count": "1",
                        "X-Limit": "25",
                        "X-Offset": "0",
                        "X-Has-More": "false",
                    }
                )

            @app.post("/cors-unsafe-probe")
            async def cors_unsafe_probe() -> dict[str, bool]:
                return {"accepted": True}

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://old-sparky.com",
            ) as client:
                response = await client.get(
                    "/cors-pagination-probe",
                    headers={"Origin": settings.platform_web_origin},
                )
                client.cookies.set(
                    settings.platform_session_cookie_name,
                    "session-token",
                )
                rejected = await client.post(
                    "/cors-unsafe-probe",
                    headers={"Origin": settings.platform_web_origin},
                )

        self.assertEqual(response.status_code, 200, response.text)
        exposed_headers = {
            value.strip().lower()
            for value in response.headers["Access-Control-Expose-Headers"].split(",")
        }
        self.assertEqual(
            exposed_headers,
            {
                "x-csrf-token",
                "x-total-count",
                "x-limit",
                "x-offset",
                "x-has-more",
                "retry-after",
            },
        )
        self.assertEqual(response.headers["X-Total-Count"], "1")
        self.assertEqual(response.headers["X-Limit"], "25")
        self.assertEqual(response.headers["X-Offset"], "0")
        self.assertEqual(response.headers["X-Has-More"], "false")
        self.assertEqual(rejected.status_code, 403, rejected.text)
        self.assertEqual(
            rejected.headers["Access-Control-Allow-Origin"],
            settings.platform_web_origin,
        )
        self.assertEqual(
            {
                value.strip().lower()
                for value in rejected.headers["Access-Control-Expose-Headers"].split(",")
            },
            exposed_headers,
        )

    async def test_csrf_accepts_exact_origin_or_referer_with_double_submit_token(self) -> None:
        client, settings = await self._csrf_client()
        async with client:
            session_token = "session-token"
            client.cookies.set(settings.platform_session_cookie_name, session_token)
            token = issue_csrf_token(Response(), session_token, settings)
            client.cookies.set(csrf_cookie_name(settings), token)
            origin_response = await client.post(
                "/unsafe",
                headers={
                    "Origin": "https://old-sparky.com",
                    "Sec-Fetch-Site": "same-origin",
                    "X-CSRF-Token": token,
                },
            )
            referer_response = await client.post(
                "/unsafe",
                headers={
                    "Referer": "https://old-sparky.com/account/security",
                    "Sec-Fetch-Site": "same-origin",
                    "X-CSRF-Token": token,
                },
            )
        self.assertEqual(origin_response.status_code, 200, origin_response.text)
        self.assertEqual(referer_response.status_code, 200, referer_response.text)

    async def test_pre_generated_csrf_token_uses_the_same_session_binding(self) -> None:
        client, settings = await self._csrf_client()
        async with client:
            session_token = "session-token"
            token = generate_csrf_token(session_token, settings)
            client.cookies.set(settings.platform_session_cookie_name, session_token)
            client.cookies.set(csrf_cookie_name(settings), token)
            response = await client.post(
                "/unsafe",
                headers={
                    "Origin": "https://old-sparky.com",
                    "Sec-Fetch-Site": "same-origin",
                    "X-CSRF-Token": token,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)

    async def test_csrf_rejects_cross_site_origin_and_fetch_metadata(self) -> None:
        client, settings = await self._csrf_client()
        async with client:
            session_token = "session-token"
            client.cookies.set(settings.platform_session_cookie_name, session_token)
            token = issue_csrf_token(Response(), session_token, settings)
            client.cookies.set(csrf_cookie_name(settings), token)
            wrong_origin = await client.post(
                "/unsafe",
                headers={"Origin": "https://evil.example", "X-CSRF-Token": token},
            )
            cross_site = await client.post(
                "/unsafe",
                headers={
                    "Origin": "https://old-sparky.com",
                    "Sec-Fetch-Site": "cross-site",
                    "X-CSRF-Token": token,
                },
            )
            missing_token = await client.post(
                "/unsafe",
                headers={"Origin": "https://old-sparky.com"},
            )
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(missing_token.status_code, 403)

    async def test_csrf_token_is_bound_to_the_authenticated_session(self) -> None:
        client, settings = await self._csrf_client()
        async with client:
            token = issue_csrf_token(Response(), "first-session", settings)
            client.cookies.set(settings.platform_session_cookie_name, "rotated-session")
            client.cookies.set(csrf_cookie_name(settings), token)
            response = await client.post(
                "/unsafe",
                headers={
                    "Origin": "https://old-sparky.com",
                    "Sec-Fetch-Site": "same-origin",
                    "X-CSRF-Token": token,
                },
            )
        self.assertEqual(response.status_code, 403)

    async def test_csrf_does_not_apply_without_cookie_authentication(self) -> None:
        client, _ = await self._csrf_client()
        async with client:
            response = await client.post("/unsafe")
        self.assertEqual(response.status_code, 200, response.text)

    async def test_stale_session_cookie_cannot_trap_public_auth_paths(self) -> None:
        settings = self._settings(platform_csrf_enabled=True)
        app = FastAPI()
        app.add_middleware(
            CsrfProtectionMiddleware,
            settings_factory=lambda: settings,
        )

        async def public_auth() -> dict[str, bool]:
            return {"accepted": True}

        for path in PUBLIC_AUTH_PATHS:
            app.add_api_route(path, public_auth, methods=["POST"])

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://old-sparky.com",
        ) as client:
            client.cookies.set(settings.platform_session_cookie_name, "revoked-session-token")
            for path in sorted(PUBLIC_AUTH_PATHS):
                with self.subTest(path=path):
                    accepted = await client.post(
                        path,
                        headers={
                            "Origin": "https://old-sparky.com",
                            "Sec-Fetch-Site": "same-origin",
                        },
                    )
                    cross_site = await client.post(
                        path,
                        headers={
                            "Origin": "https://evil.example",
                            "Sec-Fetch-Site": "cross-site",
                        },
                    )
                    self.assertEqual(accepted.status_code, 200, accepted.text)
                    self.assertEqual(cross_site.status_code, 403, cross_site.text)

    def test_production_cookie_requires_secure_host_prefix_and_path_root(self) -> None:
        settings = self._settings(
            platform_environment="production",
            platform_turnstile_mode="always",
            platform_turnstile_site_key="test-site-key",
            platform_turnstile_secret_key="test-secret-key",
        )
        validate_auth_security_settings(settings)
        response = Response()
        with patch("python_packages.platform_infra.security.get_settings", return_value=settings):
            set_session_cookie(response, "session-token")
        cookie = response.headers["set-cookie"]
        self.assertIn("Path=/", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertNotIn("Domain=", cookie)

        with self.assertRaises(RuntimeError):
            validate_auth_security_settings(
                self._settings(
                    platform_environment="production",
                    platform_session_cookie_name="session",
                    platform_turnstile_mode="always",
                    platform_turnstile_site_key="test-site-key",
                    platform_turnstile_secret_key="test-secret-key",
                )
            )
        with self.assertRaises(RuntimeError):
            validate_auth_security_settings(
                self._settings(
                    platform_environment="production",
                    platform_cookie_secure=False,
                    platform_turnstile_mode="always",
                    platform_turnstile_site_key="test-site-key",
                    platform_turnstile_secret_key="test-secret-key",
                )
            )
        with self.assertRaisesRegex(RuntimeError, "Turnstile"):
            validate_auth_security_settings(
                self._settings(platform_environment="production")
            )

    def test_production_public_registration_requires_real_email_delivery(self) -> None:
        production_values = {
            "platform_environment": "production",
            "platform_public_registration_enabled": True,
            "platform_turnstile_mode": "always",
            "platform_turnstile_site_key": "test-site-key",
            "platform_turnstile_secret_key": "test-secret-key",
        }
        with self.assertRaisesRegex(RuntimeError, "email delivery"):
            validate_auth_security_settings(self._settings(**production_values))
        with self.assertRaisesRegex(RuntimeError, "email delivery"):
            validate_auth_security_settings(
                self._settings(
                    **production_values,
                    platform_support_smtp_host="smtp.example.com",
                    platform_support_smtp_sender_email="noreply@old-sparky.com",
                    platform_support_smtp_starttls=False,
                    platform_support_smtp_ssl=False,
                )
            )

        validate_auth_security_settings(
            self._settings(
                **production_values,
                platform_support_smtp_host="smtp.example.com",
                platform_support_smtp_sender_email="noreply@old-sparky.com",
            )
        )
        validate_auth_security_settings(
            self._settings(
                **production_values,
                platform_email_sender_email="support@old-sparky.com",
                platform_resend_api_key="replace-me-resend-key",
            )
        )

    def test_public_registration_defaults_closed_only_in_production(self) -> None:
        self.assertTrue(public_registration_enabled(self._settings(platform_environment="development")))
        self.assertFalse(public_registration_enabled(self._settings(platform_environment="production")))
        self.assertTrue(
            public_registration_enabled(
                self._settings(
                    platform_environment="production",
                    platform_public_registration_enabled=True,
                )
            )
        )

    async def test_redis_registration_limit_expires_at_fixed_window_boundary(self) -> None:
        settings = self._settings(
            platform_auth_rate_limit_enabled=True,
            platform_auth_register_ip_limit=2,
            platform_auth_register_window_seconds=60,
        )
        request = Request({"type": "http", "headers": [], "client": ("192.0.2.10", 1234)})
        cache = _FakeRedis()
        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            await check_registration_rate_limit(request, settings=settings, now_epoch=120)
            await check_registration_rate_limit(request, settings=settings, now_epoch=120)
            with self.assertRaises(HTTPException) as raised:
                await check_registration_rate_limit(request, settings=settings, now_epoch=120)
            await check_registration_rate_limit(request, settings=settings, now_epoch=180)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertGreaterEqual(int(raised.exception.headers["Retry-After"]), 1)

    async def test_password_reset_has_separate_ip_and_account_fixed_windows(self) -> None:
        settings = self._settings(
            platform_auth_rate_limit_enabled=True,
            platform_auth_reset_ip_limit=10,
            platform_auth_reset_account_limit=2,
            platform_auth_reset_window_seconds=60,
        )
        request = Request({"type": "http", "headers": [], "client": ("192.0.2.11", 1234)})
        cache = _FakeRedis()
        with patch.object(auth_rate_limit, "redis_client", return_value=cache):
            await check_password_reset_rate_limit(
                request,
                "player@example.com",
                operation="request",
                settings=settings,
                now_epoch=120,
            )
            await check_password_reset_rate_limit(
                request,
                "player@example.com",
                operation="request",
                settings=settings,
                now_epoch=120,
            )
            with self.assertRaises(HTTPException) as raised:
                await check_password_reset_rate_limit(
                    request,
                    "player@example.com",
                    operation="request",
                    settings=settings,
                    now_epoch=120,
                )
            await check_password_reset_rate_limit(
                request,
                "player@example.com",
                operation="request",
                settings=settings,
                now_epoch=180,
            )
        self.assertEqual(raised.exception.status_code, 429)

    async def test_generic_auth_response_padding_has_a_bounded_minimum(self) -> None:
        settings = self._settings(platform_auth_generic_response_min_seconds=0.35)
        sleep = AsyncMock()
        with (
            patch.object(auth_routes.time, "monotonic", return_value=10.2),
            patch.object(auth_routes.asyncio, "sleep", sleep),
        ):
            await auth_routes._wait_for_generic_auth_response(10.0, settings)
        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 0.15)

        sleep.reset_mock()
        with (
            patch.object(auth_routes.time, "monotonic", return_value=10.5),
            patch.object(auth_routes.asyncio, "sleep", sleep),
        ):
            await auth_routes._wait_for_generic_auth_response(10.0, settings)
        sleep.assert_not_awaited()

    async def test_progressive_login_delay_is_bounded_and_window_scoped(self) -> None:
        settings = self._settings(
            platform_auth_rate_limit_enabled=True,
            platform_auth_login_account_limit=2,
            platform_auth_login_window_seconds=60,
            platform_auth_progressive_delay_base_seconds=0.25,
            platform_auth_progressive_delay_max_seconds=0.5,
        )
        self.assertEqual(progressive_delay_seconds(100, settings), 0.5)
        request = Request({"type": "http", "headers": [], "client": ("192.0.2.20", 1234)})
        cache = _FakeRedis()
        sleep = AsyncMock()
        with (
            patch.object(auth_rate_limit, "redis_client", return_value=cache),
            patch.object(auth_rate_limit.asyncio, "sleep", sleep),
        ):
            await record_login_failure(request, "player@example.com", settings=settings, now_epoch=120)
            await record_login_failure(request, "player@example.com", settings=settings, now_epoch=120)
            with self.assertRaises(HTTPException) as raised:
                await record_login_failure(
                    request,
                    "player@example.com",
                    settings=settings,
                    now_epoch=120,
                )
            await record_login_failure(request, "player@example.com", settings=settings, now_epoch=180)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertTrue(all(call.args[0] <= 0.5 for call in sleep.await_args_list))

    async def test_turnstile_validates_hostname_action_and_hides_token_on_failure(self) -> None:
        settings = self._settings(
            platform_turnstile_mode="always",
            platform_turnstile_site_key="test-site-key",
            platform_turnstile_secret_key="turnstile-secret",
            platform_turnstile_expected_hostname="old-sparky.com",
        )

        async def success_handler(request: httpx.Request) -> httpx.Response:
            self.assertNotIn("turnstile-secret", str(request.url))
            return httpx.Response(
                200,
                json={"success": True, "hostname": "old-sparky.com", "action": "login"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(success_handler)) as client:
            await verify_turnstile_token(
                "valid-turnstile-token",
                expected_action="login",
                remote_ip="192.0.2.30",
                settings=settings,
                client=client,
            )

        async def wrong_action_handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"success": True, "hostname": "old-sparky.com", "action": "register"},
            )

        secret_token = "must-never-appear-in-errors"
        async with httpx.AsyncClient(transport=httpx.MockTransport(wrong_action_handler)) as client:
            with self.assertRaises(HTTPException) as raised:
                await verify_turnstile_token(
                    secret_token,
                    expected_action="login",
                    remote_ip=None,
                    settings=settings,
                    client=client,
                )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertNotIn(secret_token, str(raised.exception))

    def test_operator_email_requires_matching_confirmation(self) -> None:
        self.assertEqual(
            normalize_confirmed_email("Player@Example.com", " player@example.com "),
            "player@example.com",
        )
        with self.assertRaises(ValueError):
            normalize_confirmed_email("first@example.com", "second@example.com")


class AuthSecurityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-auth-security-{uuid4().hex[:8]}"
        self.password = "integration-pass-123"
        self.app = create_app()
        self.clients = AsyncExitStack()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        async with session_factory()() as db_session:
            user_ids = list(
                (
                    await db_session.scalars(
                        select(User.id).where(User.email.like(f"{self.prefix}-%@example.com"))
                    )
                ).all()
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
                await db_session.commit()
                for user_id in user_ids:
                    invalidate_user_session_cache(user_id)
        await dispose_engine()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )

    async def _register(
        self,
        client: httpx.AsyncClient,
        *,
        email: str,
        user_agent: str | None = None,
    ) -> httpx.Response:
        headers = {"User-Agent": user_agent} if user_agent is not None else None
        return await client.post(
            "/api/v1/auth/register",
            headers=headers,
            json={
                "email": email,
                "password": self.password,
                "display_name": "safe-player",
            },
        )

    async def test_registration_never_grants_admin_and_inactive_sessions_are_rejected(self) -> None:
        email = f"{self.prefix}-player@example.com"
        client = await self._new_client()
        registered = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": self.password,
                "display_name": "safe-player",
            },
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.assertEqual(registered.headers["Cache-Control"], "no-store")
        self.assertEqual(set(registered.json()["user"]["roles"]), {"authenticated_user", "player"})
        self.assertNotIn("admin", registered.json()["user"]["roles"])
        self.assertNotIn("superadmin", registered.json()["user"]["roles"])

        user_id = registered.json()["user"]["id"]
        session_cookie_name = PlatformSettings(_env_file=None).platform_session_cookie_name
        session_token = client.cookies.get(session_cookie_name)
        self.assertIsNotNone(session_token)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "headers": [(b"cookie", f"{session_cookie_name}={session_token}".encode("ascii"))],
                "client": ("127.0.0.1", 1),
            }
        )
        async with session_factory()() as db_session:
            optional_session = await get_optional_authenticated_session(request, db_session)
        self.assertIsNotNone(optional_session)

        async with session_factory()() as db_session:
            await db_session.execute(update(User).where(User.id == user_id).values(status="suspended"))
            await db_session.commit()

        login_client = await self._new_client()
        login = await login_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": self.password},
        )
        current_session = await client.get("/api/v1/auth/session")
        async with session_factory()() as db_session:
            optional_after_deactivation = await get_optional_authenticated_session(request, db_session)
        self.assertEqual(login.status_code, 401, login.text)
        self.assertEqual(login.json()["detail"], "Invalid credentials.")
        self.assertEqual(current_session.status_code, 401, current_session.text)
        self.assertIsNone(optional_after_deactivation)

    async def test_csrf_endpoint_clears_a_stale_http_only_session_cookie(self) -> None:
        client = await self._new_client()
        settings = PlatformSettings(_env_file=None)
        client.cookies.set(
            settings.platform_session_cookie_name,
            "stale-session-token",
            domain="testserver.local",
            path="/",
        )

        response = await client.get("/api/v1/auth/csrf")

        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIsNone(client.cookies.get(settings.platform_session_cookie_name))

    async def test_operator_bootstrap_is_idempotent_and_audited(self) -> None:
        email = f"{self.prefix}-operator@example.com"
        async with session_factory()() as db_session:
            first = await bootstrap_operator(
                db_session,
                email=email,
                display_name="operator-test",
                initial_password=self.password,
            )
            await db_session.commit()
        async with session_factory()() as db_session:
            second = await bootstrap_operator(
                db_session,
                email=email,
                display_name="operator-test",
                initial_password=None,
            )
            await db_session.commit()

            role_slugs = set(
                (
                    await db_session.scalars(
                        select(Role.slug)
                        .join(UserRole, UserRole.role_id == Role.id)
                        .where(UserRole.user_id == first.user_id)
                    )
                ).all()
            )
            audit_count = len(
                (
                    await db_session.scalars(
                        select(AuditLog.id).where(
                            AuditLog.actor_user_id == first.user_id,
                            AuditLog.action == "platform.operator.bootstrap",
                        )
                    )
                ).all()
            )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.added_roles, ())
        self.assertEqual(role_slugs, {"authenticated_user", "player", "admin", "superadmin"})
        self.assertEqual(audit_count, 2)

    async def test_operator_activation_requires_explicit_flag(self) -> None:
        email = f"{self.prefix}-pending-operator@example.com"
        client = await self._new_client()
        registered = await self._register(client, email=email)
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user"]["id"]
        async with session_factory()() as db_session:
            await db_session.execute(
                update(User)
                .where(User.id == user_id)
                .values(status="pending_verification", email_verified_at=None)
            )
            await db_session.commit()

        async with session_factory()() as db_session:
            with self.assertRaisesRegex(ValueError, "--activate-existing"):
                await bootstrap_operator(
                    db_session,
                    email=email,
                    display_name=None,
                    initial_password=None,
                )
            await db_session.rollback()

        async with session_factory()() as db_session:
            result = await bootstrap_operator(
                db_session,
                email=email,
                display_name=None,
                initial_password=None,
                activate_existing=True,
            )
            await db_session.commit()
            user = await db_session.get(User, user_id)

        self.assertFalse(result.created)
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.status, "active")
        self.assertIsNotNone(user.email_verified_at)

    async def test_session_cap_list_individual_revoke_and_logout_all(self) -> None:
        email = f"{self.prefix}-sessions@example.com"
        settings = PlatformSettings(_env_file=None, platform_session_max_active=3)
        clients_by_user_agent: dict[str, httpx.AsyncClient] = {}

        with patch(
            "python_packages.platform_infra.security.get_settings",
            return_value=settings,
        ):
            registration_client = await self._new_client()
            clients_by_user_agent["session-register"] = registration_client
            registered = await self._register(
                registration_client,
                email=email,
                user_agent="session-register",
            )
            self.assertEqual(registered.status_code, 201, registered.text)

            for index in range(3):
                user_agent = f"session-login-{index}"
                client = await self._new_client()
                clients_by_user_agent[user_agent] = client
                logged_in = await client.post(
                    "/api/v1/auth/login",
                    headers={"User-Agent": user_agent},
                    json={"email": email, "password": self.password},
                )
                self.assertEqual(logged_in.status_code, 200, logged_in.text)

            current_client = clients_by_user_agent["session-login-2"]
            current_session = await current_client.get("/api/v1/auth/session")
            self.assertEqual(current_session.status_code, 200, current_session.text)
            self.assertEqual(current_session.headers["Cache-Control"], "no-store")
            listed = await current_client.get("/api/v1/auth/sessions")
            self.assertEqual(listed.status_code, 200, listed.text)
            sessions = listed.json()
            self.assertEqual(len(sessions), 3)
            self.assertEqual(sum(bool(item["is_current"]) for item in sessions), 1)
            self.assertNotIn(
                "session-register",
                {item["user_agent"] for item in sessions},
            )
            self.assertEqual(
                (await registration_client.get("/api/v1/auth/session")).status_code,
                401,
            )

            revoked = next(item for item in sessions if not item["is_current"])
            revoked_client = clients_by_user_agent[revoked["user_agent"]]
            revoke_response = await current_client.delete(
                f"/api/v1/auth/sessions/{revoked['id']}"
            )
            self.assertEqual(revoke_response.status_code, 204, revoke_response.text)
            self.assertEqual(
                (await revoked_client.get("/api/v1/auth/session")).status_code,
                401,
            )

            logout_all = await current_client.post("/api/v1/auth/logout-all")
            self.assertEqual(logout_all.status_code, 204, logout_all.text)
            self.assertEqual(
                (await current_client.get("/api/v1/auth/session")).status_code,
                401,
            )

        async with session_factory()() as db_session:
            active_session_count = len(
                (
                    await db_session.scalars(
                        select(UserSession.id)
                        .join(User, User.id == UserSession.user_id)
                        .where(
                            User.email == email,
                            UserSession.invalidated_at.is_(None),
                        )
                    )
                ).all()
            )
        self.assertEqual(active_session_count, 0)

    async def test_password_reset_is_generic_digest_only_one_time_and_revokes_sessions(self) -> None:
        email = f"{self.prefix}-reset@example.com"
        first_client = await self._new_client()
        registered = await self._register(first_client, email=email)
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user"]["id"]

        second_client = await self._new_client()
        second_login = await second_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": self.password},
        )
        self.assertEqual(second_login.status_code, 200, second_login.text)

        delivered_codes: list[str] = []

        async def capture_committed_reset_email(
            _settings: PlatformSettings,
            *,
            recipient_email: str,
            code: str,
        ) -> None:
            self.assertEqual(recipient_email, email)
            async with session_factory()() as db_session:
                committed_token_id = await db_session.scalar(
                    select(PasswordResetToken.id).where(
                        PasswordResetToken.token_digest
                        == one_time_code_digest(
                            user_id,
                            code,
                            secret_key=settings.platform_secret_key,
                        )
                    )
                )
            self.assertIsNotNone(committed_token_id)
            delivered_codes.append(code)

        mail = AsyncMock(side_effect=capture_committed_reset_email)
        settings = PlatformSettings(
            _env_file=None,
            platform_support_smtp_host="smtp.example.com",
            platform_support_smtp_sender_email="noreply@old-sparky.com",
            platform_auth_generic_response_min_seconds=0,
        )
        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_password_reset_email", mail),
            patch.object(
                auth_routes,
                "verify_turnstile_token",
                new_callable=AsyncMock,
            ) as turnstile,
        ):
            requested = await first_client.post(
                "/api/v1/auth/password-reset/request",
                json={"email": email},
            )
            missing = await first_client.post(
                "/api/v1/auth/password-reset/request",
                json={"email": f"{self.prefix}-missing@example.com"},
            )

            self.assertEqual(requested.status_code, 202, requested.text)
            self.assertEqual(missing.status_code, 202, missing.text)
            self.assertEqual(requested.json(), missing.json())
            self.assertEqual(requested.headers["Cache-Control"], "no-store")
            mail.assert_awaited_once()
            reset_code = delivered_codes[0]

            async with session_factory()() as db_session:
                stored_token = await db_session.scalar(
                    select(PasswordResetToken).where(
                        PasswordResetToken.user_id == user_id
                    )
                )
                request_audit = await db_session.scalar(
                    select(AuditLog).where(
                        AuditLog.subject_id == user_id,
                        AuditLog.action == "auth.password_reset.request",
                    )
                )
            self.assertIsNotNone(stored_token)
            assert stored_token is not None
            self.assertIsNotNone(request_audit)
            assert request_audit is not None
            self.assertIsNone(request_audit.actor_user_id)
            self.assertEqual(
                stored_token.token_digest,
                one_time_code_digest(
                    user_id,
                    reset_code,
                    secret_key=settings.platform_secret_key,
                ),
            )
            self.assertNotEqual(stored_token.token_digest, reset_code)

            async with session_factory()() as db_session:
                db_session.add(
                    PasswordResetToken(
                        user_id=user_id,
                        token_digest=one_time_code_digest(
                            user_id,
                            "654321",
                            secret_key=settings.platform_secret_key,
                        ),
                        expires_at=stored_token.expires_at,
                    )
                )
                await db_session.commit()

            new_password = "new-integration-pass-456"
            verified = await first_client.post(
                "/api/v1/auth/password-reset/verify-code",
                json={"email": email, "code": reset_code},
            )
            confirmed = await first_client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"email": email, "code": reset_code, "new_password": new_password},
            )
            reused = await first_client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"email": email, "code": reset_code, "new_password": "another-pass-789"},
            )
            self.assertEqual(turnstile.await_count, 2)
            self.assertTrue(
                all(
                    call.kwargs["expected_action"] == "reset_request"
                    for call in turnstile.await_args_list
                )
            )

        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(reused.status_code, 400, reused.text)
        async with session_factory()() as db_session:
            outstanding_token_count = len(
                (
                    await db_session.scalars(
                        select(PasswordResetToken.id).where(
                            PasswordResetToken.user_id == user_id,
                            PasswordResetToken.consumed_at.is_(None),
                        )
                    )
                ).all()
            )
        self.assertEqual(outstanding_token_count, 0)
        self.assertEqual((await first_client.get("/api/v1/auth/session")).status_code, 200)
        self.assertEqual((await second_client.get("/api/v1/auth/session")).status_code, 401)

        login_client = await self._new_client()
        old_login = await login_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": self.password},
        )
        new_login = await login_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": new_password},
        )
        self.assertEqual(old_login.status_code, 401, old_login.text)
        self.assertEqual(new_login.status_code, 200, new_login.text)

    async def test_pending_email_registration_can_be_restarted_with_new_password(self) -> None:
        email = f"{self.prefix}-restart-pending@example.com"
        first_client = await self._new_client()
        second_client = await self._new_client()
        mail = AsyncMock()
        settings = PlatformSettings(
            _env_file=None,
            platform_email_verification_required=True,
            platform_support_smtp_host="smtp.example.com",
            platform_support_smtp_sender_email="noreply@old-sparky.com",
            platform_auth_generic_response_min_seconds=0,
            platform_auth_delivery_cooldown_seconds=30,
        )

        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_email_verification_email", mail),
            patch.object(
                auth_routes,
                "reserve_auth_delivery_cooldown",
                AsyncMock(),
            ),
        ):
            first = await self._register(first_client, email=email)
            self.assertEqual(first.status_code, 201, first.text)
            self.assertTrue(first.json()["verification_required"])
            self.assertIsNone(first.json()["user"])
            first_code = mail.await_args.kwargs["code"]

            mail.reset_mock()
            new_password = "replacement-password-456"

            restarted = await second_client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": new_password,
                    "display_name": "Restarted User",
                },
            )

            self.assertEqual(restarted.status_code, 201, restarted.text)
            self.assertTrue(restarted.json()["verification_required"])
            self.assertIsNone(restarted.json()["user"])
            mail.assert_awaited_once()
            second_code = mail.await_args.kwargs["code"]
            self.assertNotEqual(first_code, second_code)

            old_code = await second_client.post(
                "/api/v1/auth/email-verification/confirm",
                json={"email": email, "code": first_code},
            )
            self.assertEqual(old_code.status_code, 400, old_code.text)

            confirmed = await second_client.post(
                "/api/v1/auth/email-verification/confirm",
                json={"email": email, "code": second_code},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)

            old_password_login = await first_client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": self.password},
            )
            self.assertEqual(old_password_login.status_code, 401, old_password_login.text)

            new_password_login = await first_client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": new_password},
            )
            self.assertEqual(new_password_login.status_code, 200, new_password_login.text)

    async def test_verified_registration_hides_existing_active_accounts(self) -> None:
        email = f"{self.prefix}-active@example.com"
        existing_client = await self._new_client()
        existing = await self._register(existing_client, email=email)
        self.assertEqual(existing.status_code, 201, existing.text)

        attacker = await self._new_client()
        fresh_client = await self._new_client()
        mail = AsyncMock()
        settings = PlatformSettings(
            _env_file=None,
            platform_email_verification_required=True,
            platform_support_smtp_host="smtp.example.com",
            platform_support_smtp_sender_email="noreply@old-sparky.com",
            platform_auth_generic_response_min_seconds=0,
            platform_auth_delivery_cooldown_seconds=30,
        )

        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_email_verification_email", mail),
            patch.object(
                auth_routes,
                "reserve_auth_delivery_cooldown",
                AsyncMock(),
            ),
        ):
            duplicate = await attacker.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "password": "attacker-replacement-password",
                    "display_name": "attacker",
                },
            )
            fresh = await self._register(
                fresh_client,
                email=f"{self.prefix}-fresh@example.com",
            )

        self.assertEqual(duplicate.status_code, 201, duplicate.text)
        self.assertEqual(fresh.status_code, 201, fresh.text)
        self.assertEqual(duplicate.json(), fresh.json())
        self.assertEqual(
            duplicate.json(),
            {
                "user": None,
                "expires_at": None,
                "verification_required": True,
                "retry_after_seconds": 30,
            },
        )
        self.assertEqual(mail.await_count, 1)

        original_login = await existing_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": self.password},
        )
        attacker_login = await attacker.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "attacker-replacement-password"},
        )
        self.assertEqual(original_login.status_code, 200, original_login.text)
        self.assertEqual(attacker_login.status_code, 401, attacker_login.text)

    async def test_email_verification_is_digest_only_one_time_and_gates_login(self) -> None:
        email = f"{self.prefix}-verify@example.com"
        client = await self._new_client()
        mail = AsyncMock()
        settings = PlatformSettings(
            _env_file=None,
            platform_email_verification_required=True,
            platform_support_smtp_host="smtp.example.com",
            platform_support_smtp_sender_email="noreply@old-sparky.com",
            platform_auth_generic_response_min_seconds=0,
        )
        with (
            patch.object(auth_routes, "get_settings", return_value=settings),
            patch.object(auth_routes, "send_email_verification_email", mail),
        ):
            registered = await self._register(client, email=email)
            self.assertEqual(registered.status_code, 201, registered.text)
            self.assertTrue(registered.json()["verification_required"])
            self.assertIsNone(registered.json()["expires_at"])
            self.assertIsNone(registered.json()["user"])
            async with session_factory()() as db_session:
                user_id = await db_session.scalar(
                    select(User.id).where(User.email == email)
                )
            self.assertIsNotNone(user_id)
            mail.assert_awaited_once()
            first_verification_code = mail.await_args.kwargs["code"]

            blocked_login = await client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": self.password},
            )
            self.assertEqual(blocked_login.status_code, 401, blocked_login.text)

            mail.reset_mock()
            resent = await client.post(
                "/api/v1/auth/email-verification/resend",
                json={"email": email},
            )
            missing = await client.post(
                "/api/v1/auth/email-verification/resend",
                json={"email": f"{self.prefix}-missing@example.com"},
            )
            self.assertEqual(resent.status_code, 202, resent.text)
            self.assertEqual(missing.status_code, 202, missing.text)
            self.assertEqual(resent.json(), missing.json())
            mail.assert_awaited_once()
            verification_code = mail.await_args.kwargs["code"]
            self.assertNotEqual(first_verification_code, verification_code)

            async with session_factory()() as db_session:
                user = await db_session.get(User, user_id)
                stored_tokens = list(
                    (
                        await db_session.scalars(
                    select(EmailVerificationToken).where(
                        EmailVerificationToken.user_id == user_id
                    )
                        )
                    ).all()
                )
                resend_audit = await db_session.scalar(
                    select(AuditLog).where(
                        AuditLog.subject_id == user_id,
                        AuditLog.action == "auth.email_verification.resend",
                    )
                )
            self.assertIsNotNone(user)
            assert user is not None
            self.assertEqual(user.status, "pending_verification")
            self.assertIsNone(user.email_verified_at)
            self.assertEqual(len(stored_tokens), 1)
            active_tokens = [
                token for token in stored_tokens if token.consumed_at is None
            ]
            self.assertEqual(len(active_tokens), 1)
            stored_token = active_tokens[0]
            self.assertEqual(
                stored_token.token_digest,
                one_time_code_digest(
                    user_id,
                    verification_code,
                    secret_key=settings.platform_secret_key,
                ),
            )
            self.assertNotEqual(stored_token.token_digest, verification_code)
            self.assertIsNotNone(resend_audit)
            assert resend_audit is not None
            self.assertIsNone(resend_audit.actor_user_id)

            confirmed = await client.post(
                "/api/v1/auth/email-verification/confirm",
                json={"email": email, "code": verification_code},
            )
            reused = await client.post(
                "/api/v1/auth/email-verification/confirm",
                json={"email": email, "code": verification_code},
            )

        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.headers["Cache-Control"], "no-store")
        self.assertEqual(reused.status_code, 400, reused.text)
        self.assertEqual((await client.get("/api/v1/auth/session")).status_code, 200)

    async def test_email_change_requires_reauth_and_writes_only_after_confirm(self) -> None:
        old_email = f"{self.prefix}-email-change-old@example.com"
        new_email = f"{self.prefix}-email-change-new@example.com"
        other_candidate = f"{self.prefix}-email-change-other@example.com"

        registration_client = await self._new_client()
        registered = await self._register(registration_client, email=old_email)
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user"]["id"]

        change_client = await self._new_client()
        other_browser = await self._new_client()

        for client in (change_client, other_browser):
            logged_in = await client.post(
                "/api/v1/auth/login",
                json={"email": old_email, "password": self.password},
            )
            self.assertEqual(logged_in.status_code, 200, logged_in.text)

        settings = auth_routes.get_settings()
        previous_session_token = change_client.cookies.get(
            settings.platform_session_cookie_name
        )
        self.assertIsNotNone(previous_session_token)

        # Keep one live reset token so successful email change must revoke it.
        async with session_factory()() as db_session:
            await issue_password_reset_token(
                db_session,
                user_id=user_id,
                secret_key=settings.platform_secret_key,
                ttl_minutes=30,
            )
            await db_session.commit()

        # Legacy PATCH /auth/account must never change login email anymore.
        legacy_change = await change_client.patch(
            "/api/v1/auth/account",
            json={
                "current_password": self.password,
                "email": new_email,
                "new_password": None,
            },
        )
        self.assertEqual(legacy_change.status_code, 409, legacy_change.text)

        delivery = AsyncMock()
        rate_limit = AsyncMock()

        with (
            patch.object(auth_routes, "_deliver_email_verification", delivery),
            patch.object(auth_routes, "check_email_link_rate_limit", rate_limit),
            patch.object(
                auth_routes,
                "email_delivery_configured",
                return_value=True,
            ),
        ):
            # Password is mandatory for a password-bearing account.
            missing_password = await change_client.post(
                "/api/v1/auth/email-change/request",
                json={"email": new_email},
            )
            self.assertEqual(missing_password.status_code, 422, missing_password.text)

            wrong_password = await change_client.post(
                "/api/v1/auth/email-change/request",
                json={
                    "email": new_email,
                    "current_password": "definitely-wrong-password",
                },
            )
            self.assertEqual(wrong_password.status_code, 401, wrong_password.text)
            delivery.assert_not_awaited()

            requested = await change_client.post(
                "/api/v1/auth/email-change/request",
                json={
                    "email": new_email,
                    "current_password": self.password,
                },
            )
            self.assertEqual(requested.status_code, 202, requested.text)
            delivery.assert_awaited_once()

            delivered_kwargs = delivery.await_args.kwargs
            self.assertEqual(delivered_kwargs["recipient_email"], new_email)
            code = delivered_kwargs["code"]
            self.assertRegex(code, r"^\d{6}$")

            # Request must not mutate the login identity.
            async with session_factory()() as db_session:
                user = await db_session.scalar(
                    select(User).where(User.id == user_id)
                )
                self.assertIsNotNone(user)
                assert user is not None
                self.assertEqual(user.email, old_email)
                self.assertEqual(user.status, "active")

                intent = await db_session.scalar(
                    select(SteamEmailLinkIntent).where(
                        SteamEmailLinkIntent.user_id == user_id,
                        SteamEmailLinkIntent.candidate_email == new_email,
                        SteamEmailLinkIntent.consumed_at.is_(None),
                    )
                )
                self.assertIsNotNone(intent)

            # Same authenticated account in another browser has no matching
            # browser grant and therefore cannot use the code.
            wrong_browser = await other_browser.post(
                "/api/v1/auth/email-change/confirm",
                json={"email": new_email, "code": code},
            )
            self.assertEqual(wrong_browser.status_code, 400, wrong_browser.text)

            # Code is bound to the exact candidate email.
            wrong_candidate = await change_client.post(
                "/api/v1/auth/email-change/confirm",
                json={"email": other_candidate, "code": code},
            )
            self.assertEqual(wrong_candidate.status_code, 400, wrong_candidate.text)

            bad_code = "000000" if code != "000000" else "111111"
            wrong_code = await change_client.post(
                "/api/v1/auth/email-change/confirm",
                json={"email": new_email, "code": bad_code},
            )
            self.assertEqual(wrong_code.status_code, 400, wrong_code.text)

            # Failed confirmations still must not alter the account.
            async with session_factory()() as db_session:
                user = await db_session.scalar(
                    select(User).where(User.id == user_id)
                )
                self.assertIsNotNone(user)
                assert user is not None
                self.assertEqual(user.email, old_email)
                self.assertEqual(user.status, "active")

            confirmed = await change_client.post(
                "/api/v1/auth/email-change/confirm",
                json={"email": new_email, "code": code},
            )
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["email"], new_email)
            self.assertEqual(confirmed.json()["status"], "active")
            rotated_csrf_header = confirmed.headers.get("X-CSRF-Token")
            self.assertIsNotNone(rotated_csrf_header)

        # Current browser receives a rotated session.
        rotated_session_token = change_client.cookies.get(
            settings.platform_session_cookie_name
        )
        self.assertIsNotNone(rotated_session_token)
        self.assertNotEqual(previous_session_token, rotated_session_token)
        self.assertEqual(
            rotated_csrf_header,
            change_client.cookies.get(csrf_cookie_name(settings)),
        )

        current_session = await change_client.get("/api/v1/auth/session")
        self.assertEqual(current_session.status_code, 200, current_session.text)
        self.assertEqual(current_session.json()["email"], new_email)

        # Other sessions are invalidated by the sensitive identity change.
        other_session = await other_browser.get("/api/v1/auth/session")
        self.assertEqual(other_session.status_code, 401, other_session.text)

        async with session_factory()() as db_session:
            user = await db_session.scalar(
                select(User).where(User.id == user_id)
            )
            self.assertIsNotNone(user)
            assert user is not None
            self.assertEqual(user.email, new_email)
            self.assertEqual(user.status, "active")
            self.assertIsNotNone(user.email_verified_at)

            profile = await db_session.scalar(
                select(PlayerProfile).where(PlayerProfile.user_id == user_id)
            )
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(profile.contact_email, new_email)

            live_reset_token = await db_session.scalar(
                select(PasswordResetToken.id).where(
                    PasswordResetToken.user_id == user_id,
                    PasswordResetToken.consumed_at.is_(None),
                )
            )
            self.assertIsNone(live_reset_token)

            intent = await db_session.scalar(
                select(SteamEmailLinkIntent).where(
                    SteamEmailLinkIntent.user_id == user_id,
                    SteamEmailLinkIntent.candidate_email == new_email,
                )
            )
            self.assertIsNotNone(intent)
            assert intent is not None
            self.assertIsNotNone(intent.consumed_at)

            audit = await db_session.scalar(
                select(AuditLog).where(
                    AuditLog.subject_id == user_id,
                    AuditLog.action == "auth.email_change.confirm",
                )
            )
            self.assertIsNotNone(audit)


    async def test_email_change_rejects_email_owned_by_another_user(self) -> None:
        old_email = f"{self.prefix}-email-owner-source@example.com"
        occupied_email = f"{self.prefix}-email-owner-target@example.com"

        source_registration = await self._new_client()
        source_registered = await self._register(
            source_registration,
            email=old_email,
        )
        self.assertEqual(
            source_registered.status_code,
            201,
            source_registered.text,
        )
        user_id = source_registered.json()["user"]["id"]

        target_registration = await self._new_client()
        target_registered = await self._register(
            target_registration,
            email=occupied_email,
        )
        self.assertEqual(
            target_registered.status_code,
            201,
            target_registered.text,
        )

        client = await self._new_client()
        logged_in = await client.post(
            "/api/v1/auth/login",
            json={"email": old_email, "password": self.password},
        )
        self.assertEqual(logged_in.status_code, 200, logged_in.text)

        delivery = AsyncMock()
        with (
            patch.object(auth_routes, "_deliver_email_verification", delivery),
            patch.object(
                auth_routes,
                "email_delivery_configured",
                return_value=True,
            ),
        ):
            response = await client.post(
                "/api/v1/auth/email-change/request",
                json={
                    "email": occupied_email,
                    "current_password": self.password,
                },
            )

        self.assertEqual(response.status_code, 409, response.text)
        delivery.assert_not_awaited()

        async with session_factory()() as db_session:
            user = await db_session.scalar(
                select(User).where(User.id == user_id)
            )
            self.assertIsNotNone(user)
            assert user is not None
            self.assertEqual(user.email, old_email)
            self.assertEqual(user.status, "active")

            intent = await db_session.scalar(
                select(SteamEmailLinkIntent).where(
                    SteamEmailLinkIntent.user_id == user_id,
                    SteamEmailLinkIntent.candidate_email == occupied_email,
                    SteamEmailLinkIntent.consumed_at.is_(None),
                )
            )
            self.assertIsNone(intent)


    async def test_login_and_password_reset_serialize_before_session_creation(self) -> None:
        email = f"{self.prefix}-login-reset-race@example.com"
        registration_client = await self._new_client()
        registered = await self._register(registration_client, email=email)
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user"]["id"]
        secret_key = auth_routes.get_settings().platform_secret_key

        async with session_factory()() as db_session:
            issued = await issue_password_reset_token(
                db_session,
                user_id=user_id,
                secret_key=secret_key,
                ttl_minutes=30,
            )
            await db_session.commit()

        login_client = await self._new_client()
        reset_client = await self._new_client()
        login_reached_session_creation = asyncio.Event()
        allow_login_session_creation = asyncio.Event()
        original_create_session = auth_routes.create_user_session

        async def paused_create_session(**kwargs):
            login_reached_session_creation.set()
            await allow_login_session_creation.wait()
            return await original_create_session(**kwargs)

        with patch.object(auth_routes, "create_user_session", paused_create_session):
            login_task = asyncio.create_task(
                login_client.post(
                    "/api/v1/auth/login",
                    json={"email": email, "password": self.password},
                )
            )
            await asyncio.wait_for(login_reached_session_creation.wait(), timeout=3)
            reset_task = asyncio.create_task(
                reset_client.post(
                    "/api/v1/auth/password-reset/confirm",
                    json={
                        "email": email,
                        "code": issued.code,
                        "new_password": "race-safe-new-password-456",
                    },
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(reset_task.done())
            allow_login_session_creation.set()
            login_response, reset_response = await asyncio.gather(
                login_task,
                reset_task,
            )

        self.assertEqual(login_response.status_code, 200, login_response.text)
        self.assertEqual(reset_response.status_code, 200, reset_response.text)
        self.assertEqual((await login_client.get("/api/v1/auth/session")).status_code, 401)

    async def test_concurrent_reset_issuance_keeps_only_one_live_token(self) -> None:
        email = f"{self.prefix}-token-race@example.com"
        client = await self._new_client()
        registered = await self._register(client, email=email)
        self.assertEqual(registered.status_code, 201, registered.text)
        user_id = registered.json()["user"]["id"]
        secret_key = PlatformSettings(_env_file=None).platform_secret_key

        async def issue_once() -> str:
            async with session_factory()() as db_session:
                issued = await issue_password_reset_token(
                    db_session,
                    user_id=user_id,
                    secret_key=secret_key,
                    ttl_minutes=30,
                )
                await db_session.commit()
                return issued.code

        issued_tokens = await asyncio.gather(issue_once(), issue_once())

        async with session_factory()() as db_session:
            stored_tokens = list(
                (
                    await db_session.scalars(
                        select(PasswordResetToken).where(
                            PasswordResetToken.user_id == user_id
                        )
                    )
                ).all()
            )
        self.assertEqual(len(stored_tokens), 1)
        self.assertEqual(
            sum(token.consumed_at is None for token in stored_tokens),
            1,
        )
        self.assertIn(
            stored_tokens[0].token_digest,
            {
                one_time_code_digest(user_id, code, secret_key=secret_key)
                for code in issued_tokens
            },
        )


if __name__ == "__main__":
    unittest.main()
