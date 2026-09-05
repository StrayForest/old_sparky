from __future__ import annotations

import unittest
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.api.routes import auth as auth_routes
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.google_oauth import (
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GOOGLE_TOKEN_ENDPOINT,
    GOOGLE_USERINFO_ENDPOINT,
    GoogleIdentity,
    GoogleOAuthError,
    verify_google_authorization_code,
)
from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    ExternalIdentity,
    GoogleAuthFlow,
    User,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class GoogleOAuthUnitTests(PlatformIsolatedAsyncioTestCase):
    def _settings(self) -> PlatformSettings:
        return PlatformSettings(
            _env_file=None,
            platform_google_client_id="google-client-id.apps.googleusercontent.com",
            platform_google_client_secret="google-client-secret",
            platform_google_oauth_timeout_seconds=3,
        )

    async def test_code_exchange_fetches_verified_userinfo(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
                self.assertEqual(request.method, "POST")
                body = (await request.aread()).decode()
                self.assertIn("grant_type=authorization_code", body)
                return httpx.Response(
                    200,
                    json={"access_token": "access-token", "token_type": "Bearer"},
                )
            self.assertEqual(str(request.url), GOOGLE_USERINFO_ENDPOINT)
            self.assertEqual(request.headers["authorization"], "Bearer access-token")
            return httpx.Response(
                200,
                json={
                    "sub": "google-subject-1",
                    "email": "Google.User@example.com",
                    "email_verified": True,
                    "name": "Google User",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        ) as client:
            identity = await verify_google_authorization_code(
                "one-time-code",
                callback_url="https://old-sparky.com/api/v1/auth/google/callback",
                settings=self._settings(),
                client=client,
            )

        self.assertEqual(
            identity,
            GoogleIdentity(
                subject="google-subject-1",
                email="google.user@example.com",
                display_name="Google User",
            ),
        )
        self.assertEqual([str(request.url) for request in requests], [
            GOOGLE_TOKEN_ENDPOINT,
            GOOGLE_USERINFO_ENDPOINT,
        ])

    async def test_unverified_email_is_rejected(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GOOGLE_TOKEN_ENDPOINT:
                return httpx.Response(
                    200,
                    json={"access_token": "access-token", "token_type": "Bearer"},
                )
            return httpx.Response(
                200,
                json={
                    "sub": "google-subject-2",
                    "email": "unverified@example.com",
                    "email_verified": False,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(GoogleOAuthError):
                await verify_google_authorization_code(
                    "one-time-code",
                    callback_url="https://old-sparky.com/api/v1/auth/google/callback",
                    settings=self._settings(),
                    client=client,
                )


class GoogleAuthIntegrationTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # The full backend suite uses one process with a fresh event loop for
        # each IsolatedAsyncioTestCase. Detach any pool left by the preceding
        # test before this integration test opens its first session.
        await dispose_engine()
        self.prefix = f"it-google-{uuid4().hex[:8]}"
        self.settings = PlatformSettings(
            _env_file=None,
            platform_secret_key="google-integration-secret",
            platform_google_login_enabled=True,
            platform_google_client_id="google-client-id.apps.googleusercontent.com",
            platform_google_client_secret="google-client-secret",
            platform_google_callback_url="http://testserver/api/v1/auth/google/callback",
            platform_auth_generic_response_min_seconds=0,
        )
        self.app = create_app()
        self.clients = AsyncExitStack()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        async with session_factory()() as db_session:
            user_ids = set(
                (
                    await db_session.scalars(
                        select(User.id).where(User.email.like(f"{self.prefix}-%@example.com"))
                    )
                ).all()
            )
            await db_session.execute(
                delete(GoogleAuthFlow).where(GoogleAuthFlow.return_path.like(f"/{self.prefix}%"))
            )
            if user_ids:
                await db_session.execute(
                    delete(AuditLog).where(
                        (AuditLog.actor_user_id.in_(user_ids))
                        | (
                            (AuditLog.subject_type == "user")
                            & AuditLog.subject_id.in_(user_ids)
                        )
                    )
                )
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
            await db_session.commit()
        await dispose_engine()

    async def _client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
                follow_redirects=False,
            )
        )

    async def test_google_callback_creates_verified_account_and_reuses_identity(self) -> None:
        client = await self._client()
        with (
            patch.object(auth_routes, "get_settings", return_value=self.settings),
            patch.object(
                auth_routes,
                "verify_google_authorization_code",
                AsyncMock(
                    return_value=GoogleIdentity(
                        subject="google-integration-subject",
                        email=f"{self.prefix}-user@example.com",
                        display_name="Google Integration",
                    )
                ),
            ) as verifier,
        ):
            started = await client.post(
                "/api/v1/auth/google/login/start",
                json={"return_to": f"/{self.prefix}/profile"},
            )
            self.assertEqual(started.status_code, 200, started.text)
            authorization = started.json()["authorization_url"]
            parsed = urlparse(authorization)
            self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", GOOGLE_AUTHORIZATION_ENDPOINT)
            query = parse_qs(parsed.query)
            state = query["state"][0]
            callback = await client.get(
                "/api/v1/auth/google/callback",
                params={"state": state, "code": "one-time-code"},
            )

            started_again = await client.post(
                "/api/v1/auth/google/login/start",
                json={"return_to": f"/{self.prefix}/profile"},
            )
            self.assertEqual(started_again.status_code, 200, started_again.text)
            state_again = parse_qs(urlparse(started_again.json()["authorization_url"]).query)["state"][0]
            callback_again = await client.get(
                "/api/v1/auth/google/callback",
                params={"state": state_again, "code": "one-time-code"},
            )

        self.assertEqual(callback.status_code, 303, callback.text)
        self.assertIn("google_auth=success", callback.headers["location"])
        self.assertEqual(callback_again.status_code, 303, callback_again.text)
        self.assertIn("google_auth=success", callback_again.headers["location"])
        self.assertEqual(verifier.await_count, 2)
        current = await client.get("/api/v1/users/me")
        self.assertEqual(current.status_code, 200, current.text)
        user = current.json()
        self.assertEqual(user["email"], f"{self.prefix}-user@example.com")
        self.assertTrue(user["has_password"] is False)
        async with session_factory()() as db_session:
            identity_count = len(
                (
                    await db_session.scalars(
                        select(ExternalIdentity.id).where(
                            ExternalIdentity.provider == "google",
                            ExternalIdentity.subject == "google-integration-subject",
                        )
                    )
                ).all()
            )
        self.assertEqual(identity_count, 1)


if __name__ == "__main__":
    unittest.main()
