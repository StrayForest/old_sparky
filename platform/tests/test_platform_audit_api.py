from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete, select

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import AuditLog, User


class PlatformAuditApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-audit-{uuid4().hex[:8]}"
        self.app = create_app()
        self.clients = AsyncExitStack()
        await self._cleanup()

    async def asyncTearDown(self) -> None:
        await self.clients.aclose()
        await self._cleanup()
        await dispose_engine()

    async def _cleanup(self) -> None:
        async with session_factory()() as db_session:
            user_ids = list(
                await db_session.scalars(
                    select(User.id).where(User.email.like(f"{self.prefix}-%@example.com"))
                )
            )
            if user_ids:
                await db_session.execute(delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids)))
                await db_session.execute(delete(User).where(User.id.in_(user_ids)))
                await db_session.commit()

    async def _client(self) -> httpx.AsyncClient:
        return await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://testserver",
            )
        )

    async def _register(self, label: str) -> tuple[httpx.AsyncClient, str]:
        client = await self._client()
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"{self.prefix}-{label}@example.com",
                "password": "integration-pass-123",
                "display_name": f"audit-{label}",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return client, response.json()["user"]["id"]

    async def test_my_audit_events_are_private_newest_first_and_limited(self) -> None:
        owner, owner_id = await self._register("owner")
        other, other_id = await self._register("other")

        anonymous = await self._client()
        anonymous_response = await anonymous.get("/api/v1/audit/me")
        self.assertEqual(anonymous_response.status_code, 401, anonymous_response.text)

        base_time = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
        async with session_factory()() as db_session:
            db_session.add(
                AuditLog(
                    actor_user_id=other_id,
                    action="audit.other.hidden",
                    subject_type="test",
                    subject_id="other",
                    payload={"owner": "other"},
                    created_at=base_time + timedelta(seconds=10_000),
                )
            )
            for index in range(52):
                db_session.add(
                    AuditLog(
                        actor_user_id=owner_id,
                        action=f"audit.owner.{index}",
                        subject_type="test",
                        subject_id=str(index),
                        payload={"index": index},
                        created_at=base_time + timedelta(seconds=index),
                    )
                )
            await db_session.commit()

        response = await owner.get("/api/v1/audit/me")
        self.assertEqual(response.status_code, 200, response.text)
        rows = response.json()
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["action"], "audit.owner.51")
        self.assertEqual(rows[-1]["action"], "audit.owner.2")
        self.assertTrue(all(row["action"].startswith("audit.owner.") for row in rows))
        self.assertNotIn("audit.other.hidden", {row["action"] for row in rows})


if __name__ == "__main__":
    unittest.main()
