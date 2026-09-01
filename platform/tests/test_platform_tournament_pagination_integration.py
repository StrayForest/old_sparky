from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest
from uuid import uuid4

import httpx
from sqlalchemy import delete

from apps.platform_api.app.main import create_app
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import Tournament, User


class PlatformTournamentPaginationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-pagination-{uuid4().hex[:8]}"
        self.app = create_app()
        self.organizer_id = str(uuid4())
        async with session_factory()() as db_session:
            db_session.add(
                User(
                    id=self.organizer_id,
                    email=f"{self.prefix}@example.com",
                    display_name="Pagination Organizer",
                )
            )
            await db_session.flush()
            created_at = datetime(2026, 6, 13, 8, 0, tzinfo=UTC)
            for index, allowed_ranks in enumerate(
                ([], ["Oracle"], ["Phantom"], ["Oracle", "Phantom"])
            ):
                db_session.add(
                    Tournament(
                        id=str(uuid4()),
                        slug=f"{self.prefix}-{index}",
                        name=f"{self.prefix} Pagination Cup {index}",
                        visibility="public",
                        status="registration_open",
                        format_slug="solo",
                        allowed_ranks=allowed_ranks,
                        organizer_user_id=self.organizer_id,
                        created_at=created_at + timedelta(minutes=index),
                    )
                )
            await db_session.commit()

    async def asyncTearDown(self) -> None:
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(Tournament).where(Tournament.slug.like(f"{self.prefix}%"))
            )
            await db_session.execute(delete(User).where(User.id == self.organizer_id))
            await db_session.commit()
        await dispose_engine()

    async def test_public_rank_filter_pages_without_overlap(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://testserver",
        ) as client:
            first = await client.get(
                "/api/v1/tournaments",
                params=[
                    ("search", self.prefix),
                    ("rank", "Oracle"),
                    ("limit", "2"),
                ],
            )
            second = await client.get(
                "/api/v1/tournaments",
                params=[
                    ("search", self.prefix),
                    ("rank", "Oracle"),
                    ("limit", "2"),
                    ("cursor", first.headers.get("X-Next-Cursor", "")),
                ],
            )
            invalid = await client.get("/api/v1/tournaments", params={"limit": "101"})

        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(first.headers["X-Has-More"], "true")
        self.assertEqual(second.headers["X-Has-More"], "false")
        self.assertNotIn("X-Total-Count", first.headers)
        self.assertNotIn("X-Offset", second.headers)

        first_slugs = [item["slug"] for item in first.json()]
        second_slugs = [item["slug"] for item in second.json()]
        self.assertEqual(len(first_slugs), 2)
        self.assertEqual(len(second_slugs), 1)
        self.assertFalse(set(first_slugs).intersection(second_slugs))
        self.assertEqual(
            first_slugs + second_slugs,
            [
                f"{self.prefix}-3",
                f"{self.prefix}-1",
                f"{self.prefix}-0",
            ],
        )


if __name__ == "__main__":
    unittest.main()
