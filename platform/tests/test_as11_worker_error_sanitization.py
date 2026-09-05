from __future__ import annotations

import unittest
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import deadlock_automation
from python_packages.platform_infra.db import (
    PUBLIC_AUTOMATION_FAILURE_MESSAGE,
    dispose_engine,
    session_factory,
)
from python_packages.platform_infra.models import AuditLog, Tournament, User
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase


class AS11WorkerErrorSanitizationTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-as11-{uuid4().hex[:8]}"
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
                        select(User.id).where(
                            User.email.like(f"{self.prefix}-%@example.com")
                        )
                    )
                ).all()
            )
            if user_ids:
                await db_session.execute(
                    delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids))
                )
            await db_session.execute(
                delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%"))
            )
            if user_ids:
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
            await db_session.commit()

    async def _new_client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )

    async def _register_organizer(self) -> tuple[httpx.AsyncClient, str]:
        client = await self._new_client()
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-organizer@example.com",
                "password": self.password,
                "display_name": "as11-organizer",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        user_id = str(response.json()["user"]["id"])
        async with session_factory()() as db_session:
            user = await db_session.scalar(select(User).where(User.id == user_id))
            self.assertIsNotNone(user)
            user.public_tournament_credits = 1
            await db_session.commit()
        return client, user_id

    async def test_worker_exception_text_never_reaches_public_tournament_response(self) -> None:
        organizer, _ = await self._register_organizer()
        created = await organizer.post(
            "/api/v1/tournaments",
            json={
                "name": f"{self.prefix}-public",
                "visibility": "public",
                "format_slug": "solo",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        slug = str(created.json()["slug"])
        marker = "AS11-SENSITIVE-WORKER-DETAIL api-key=user@example.com"

        async with session_factory()() as db_session:
            tournament = await db_session.scalar(
                select(Tournament).where(Tournament.slug == slug)
            )
            self.assertIsNotNone(tournament)
            with self.assertLogs(
                "python_packages.platform_infra.db",
                level="WARNING",
            ) as captured:
                deadlock_automation._record_automation_failure(
                    tournament,
                    error=RuntimeError(marker),
                    now=datetime.now(UTC),
                )
                await db_session.commit()
            await db_session.refresh(tournament)
            self.assertEqual(
                tournament.automation_last_error,
                PUBLIC_AUTOMATION_FAILURE_MESSAGE,
            )

        log_output = "\n".join(captured.output)
        self.assertNotIn(marker, log_output)
        self.assertIn("error_fingerprint=", log_output)

        anonymous = await self._new_client()
        public_response = await anonymous.get(f"/api/v1/tournaments/{slug}")
        self.assertEqual(public_response.status_code, 200, public_response.text)
        payload = public_response.json()
        self.assertEqual(
            payload["automation_last_error"],
            PUBLIC_AUTOMATION_FAILURE_MESSAGE,
        )
        self.assertNotIn(marker, public_response.text)


if __name__ == "__main__":
    unittest.main()
