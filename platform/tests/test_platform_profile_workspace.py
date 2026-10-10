from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from unittest.mock import patch
from uuid import uuid4

import httpx
from sqlalchemy import delete, select
from sqlalchemy.sql import operators
from sqlalchemy.sql.visitors import iterate

from apps.platform_api.app.api.routes import profiles
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services import profile_workspace as profile_workspace_service
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    DeadlockDreamSlot,
    DeadlockProfile,
    PlayerProfile,
    User,
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


class PlatformProfileWorkspaceTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.prefix = f"it-profile-workspace-{uuid4().hex[:8]}"
        self.password = INTEGRATION_PASSWORD
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
                (
                    await db_session.scalars(
                        select(User.id).where(
                            User.email.like(f"{self.prefix}-%@example.com")
                        )
                    )
                ).all()
            )
            if not user_ids:
                return
            await db_session.execute(
                delete(AuditLog).where(AuditLog.actor_user_id.in_(user_ids))
            )
            await db_session.execute(
                delete(DeadlockDreamSlot).where(
                    DeadlockDreamSlot.user_id.in_(user_ids)
                )
            )
            await db_session.execute(
                delete(DeadlockProfile).where(DeadlockProfile.user_id.in_(user_ids))
            )
            await db_session.execute(
                delete(PlayerProfile).where(PlayerProfile.user_id.in_(user_ids))
            )
            await db_session.execute(delete(User).where(User.id.in_(user_ids)))
            await db_session.commit()

    async def _register(self, label: str) -> httpx.AsyncClient:
        client = await self.clients.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app, client=next_test_asgi_peer()),
                base_url="https://testserver",
            )
        )
        with patch_integration_registration_hash():
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"{self.prefix}-{label}@example.com",
                    "password": self.password,
                    "display_name": f"test-{label}"[:15],
                },
                headers=same_origin_request_headers(client),
            )
        self.assertEqual(response.status_code, 201, response.text)
        return client

    @staticmethod
    def _is_user_owner_lock_statement(statement, user_id: str) -> bool:
        if getattr(statement, "_for_update_arg", None) is None:
            return False
        if not any(
            description.get("entity") is User and description.get("expr") is User
            for description in getattr(statement, "column_descriptions", ())
        ):
            return False
        whereclause = getattr(statement, "whereclause", None)
        if whereclause is None:
            return False
        for criterion in iterate(whereclause):
            if getattr(criterion, "operator", None) is not operators.eq:
                continue
            for column, value in (
                (getattr(criterion, "left", None), getattr(criterion, "right", None)),
                (getattr(criterion, "right", None), getattr(criterion, "left", None)),
            ):
                if not hasattr(column, "compare") or not column.compare(User.__table__.c.id):
                    continue
                if getattr(value, "value", object()) == user_id:
                    return True
        return False

    async def _assert_user_lock_serializes_slot_write(
        self,
        owner: httpx.AsyncClient,
        request,
        *,
        observe_scalar_lock=False,
    ) -> httpx.Response:
        me = await owner.get("/api/v1/users/me")
        self.assertEqual(me.status_code, 200, me.text)
        user_id = me.json()["id"]

        lock_attempted = asyncio.Event()
        lock_acquired = asyncio.Event()
        if observe_scalar_lock:
            self.assertTrue(
                self._is_user_owner_lock_statement(
                    select(User).where(User.id == user_id).with_for_update(),
                    user_id,
                )
            )
            self.assertFalse(
                self._is_user_owner_lock_statement(
                    select(User).where(User.id == user_id),
                    user_id,
                )
            )
            self.assertFalse(
                self._is_user_owner_lock_statement(
                    select(User).where(User.email == user_id).with_for_update(),
                    user_id,
                )
            )
            self.assertFalse(
                self._is_user_owner_lock_statement(
                    select(PlayerProfile)
                    .where(PlayerProfile.user_id == user_id)
                    .with_for_update(),
                    user_id,
                )
            )
            self.assertFalse(
                self._is_user_owner_lock_statement(
                    select(User)
                    .where(DeadlockDreamSlot.user_id == user_id)
                    .with_for_update(),
                    user_id,
                )
            )
            original_scalar = profile_workspace_service.AsyncSession.scalar

            async def observe_scalar(db_session, statement, *args, **kwargs):
                if self._is_user_owner_lock_statement(statement, user_id):
                    lock_attempted.set()
                    result = await original_scalar(
                        db_session, statement, *args, **kwargs
                    )
                    lock_acquired.set()
                    return result
                return await original_scalar(db_session, statement, *args, **kwargs)

            lock_patch = patch.object(
                profile_workspace_service.AsyncSession,
                "scalar",
                new=observe_scalar,
            )
        else:
            original_lock = profiles.lock_profile_owner

            async def observe_lock(*args, **kwargs):
                lock_attempted.set()
                result = await original_lock(*args, **kwargs)
                lock_acquired.set()
                return result

            lock_patch = patch.object(
                profiles,
                "lock_profile_owner",
                side_effect=observe_lock,
            )

        task: asyncio.Task[httpx.Response] | None = None
        try:
            async with session_factory()() as blocker:
                await blocker.execute(
                    select(User.id).where(User.id == user_id).with_for_update()
                )
                try:
                    with lock_patch:
                        task = asyncio.create_task(request())
                        await asyncio.wait_for(lock_attempted.wait(), timeout=5)
                        self.assertFalse(lock_acquired.is_set())
                        self.assertFalse(task.done())
                        await asyncio.wait_for(blocker.rollback(), timeout=5)
                        response = await asyncio.wait_for(task, timeout=5)
                        self.assertTrue(lock_acquired.is_set())
                finally:
                    if blocker.in_transaction():
                        await asyncio.wait_for(blocker.rollback(), timeout=5)
        finally:
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        return response

    async def test_workspace_returns_deadlock_priority_and_complete_dream_slots(self) -> None:
        owner = await self._register("snapshot")

        deadlock = await owner.put(
            "/api/v1/profiles/me/deadlock",
            json={
                "rank": "Oracle",
                "subrank": 4,
                "playtime": "1001-1500",
                "roles": ["Carry"],
                "pool": ["Abrams", "Haze", "Ivy"],
                "captain_priority": "yes",
            },
            headers=same_origin_request_headers(owner),
        )
        self.assertEqual(deadlock.status_code, 200, deadlock.text)

        captain = await owner.put(
            "/api/v1/profiles/me/captain",
            json={
                "captain_team_name": "Alpha Team",
                "slots": [
                    {
                        "slot_number": 2,
                        "allowed_roles": ["Support"],
                        "desired_heroes": ["Ivy"],
                    }
                ],
            },
            headers=same_origin_request_headers(owner),
        )
        self.assertEqual(captain.status_code, 200, captain.text)

        workspace = await owner.get("/api/v1/profiles/me/workspace")
        self.assertEqual(workspace.status_code, 200, workspace.text)
        payload = workspace.json()

        self.assertEqual(payload["profile"]["captain_team_name"], "Alpha Team")
        self.assertEqual(payload["deadlock_profile"]["captain_priority"], "yes")
        self.assertEqual(len(payload["dream_slots"]), 6)
        self.assertEqual(
            [slot["slot_number"] for slot in payload["dream_slots"]],
            [1, 2, 3, 4, 5, 6],
        )
        self.assertEqual(payload["dream_slots"][1]["allowed_roles"], ["Support"])
        self.assertEqual(payload["dream_slots"][1]["desired_heroes"], ["Ivy"])

        bootstrap = await owner.get("/api/v1/profiles/me/bootstrap")
        self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
        bootstrap_payload = bootstrap.json()
        self.assertEqual(bootstrap_payload["account"]["id"], payload["profile"]["user_id"])
        self.assertEqual(bootstrap_payload["account"]["email"], payload["profile"]["account_email"])
        self.assertEqual(bootstrap_payload["account"]["roles"], ["authenticated_user", "player"])
        self.assertEqual(bootstrap_payload["profile"], payload["profile"])
        self.assertEqual(bootstrap_payload["dream_slots"], payload["dream_slots"])

        me = await owner.get("/api/v1/users/me")
        self.assertEqual(me.status_code, 200, me.text)
        async with session_factory()() as db_session:
            actions = set(
                await db_session.scalars(
                    select(AuditLog.action).where(
                        AuditLog.actor_user_id == me.json()["id"]
                    )
                )
            )
        self.assertIn("profile.captain.update", actions)

    async def test_invalid_captain_update_preserves_existing_team_and_slots(self) -> None:
        owner = await self._register("atomic")

        initial = await owner.put(
            "/api/v1/profiles/me/captain",
            json={
                "captain_team_name": "Alpha Team",
                "slots": [
                    {
                        "slot_number": 1,
                        "allowed_roles": ["Carry"],
                        "desired_heroes": ["Abrams"],
                    }
                ],
            },
            headers=same_origin_request_headers(owner),
        )
        self.assertEqual(initial.status_code, 200, initial.text)

        rejected = await owner.put(
            "/api/v1/profiles/me/captain",
            json={
                "captain_team_name": "Beta Team",
                "slots": [
                    {
                        "slot_number": 1,
                        "allowed_roles": ["Carry"],
                        "desired_heroes": ["Definitely Not A Hero"],
                    }
                ],
            },
            headers=same_origin_request_headers(owner),
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)

        workspace = await owner.get("/api/v1/profiles/me/workspace")
        self.assertEqual(workspace.status_code, 200, workspace.text)
        payload = workspace.json()
        self.assertEqual(payload["profile"]["captain_team_name"], "Alpha Team")
        self.assertEqual(payload["dream_slots"][0]["allowed_roles"], ["Carry"])
        self.assertEqual(payload["dream_slots"][0]["desired_heroes"], ["Abrams"])

    async def test_dream_slot_update_waits_for_shared_user_lock(self) -> None:
        owner = await self._register("slot-lock")

        response = await self._assert_user_lock_serializes_slot_write(
            owner,
            lambda: owner.put(
                "/api/v1/profiles/me/deadlock/dream-slots",
                json={
                    "slots": [
                        {
                            "slot_number": 1,
                            "allowed_roles": ["Carry"],
                            "desired_heroes": ["Abrams"],
                        }
                    ]
                },
                headers=same_origin_request_headers(owner),
            ),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["desired_heroes"], ["Abrams"])

    async def test_captain_update_waits_for_shared_user_lock(self) -> None:
        owner = await self._register("captain-lock")

        response = await self._assert_user_lock_serializes_slot_write(
            owner,
            lambda: owner.put(
                "/api/v1/profiles/me/captain",
                json={
                    "captain_team_name": "Alpha Team",
                    "slots": [
                        {
                            "slot_number": 2,
                            "allowed_roles": ["Support"],
                            "desired_heroes": ["Ivy"],
                        }
                    ],
                },
                headers=same_origin_request_headers(owner),
            ),
            observe_scalar_lock=True,
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["dream_slots"][1]["desired_heroes"], ["Ivy"])

    async def test_concurrent_captain_replace_all_keeps_exactly_one_payload(self) -> None:
        owner = await self._register("concurrent-replace-all")
        payloads = [
            {
                "captain_team_name": "Alpha Team",
                "slots": [
                    {
                        "slot_number": 1,
                        "allowed_roles": ["Carry"],
                        "desired_heroes": ["Abrams"],
                    }
                ],
            },
            {
                "captain_team_name": "Beta Team",
                "slots": [
                    {
                        "slot_number": 6,
                        "allowed_roles": ["Support"],
                        "desired_heroes": ["Ivy"],
                    }
                ],
            },
        ]

        responses = await asyncio.gather(
            *(
                owner.put(
                    "/api/v1/profiles/me/captain",
                    json=payload,
                    headers=same_origin_request_headers(owner),
                )
                for payload in payloads
            )
        )
        self.assertEqual([response.status_code for response in responses], [200, 200])

        workspace = await owner.get("/api/v1/profiles/me/workspace")
        self.assertEqual(workspace.status_code, 200, workspace.text)
        payload = workspace.json()
        observed = (
            payload["profile"]["captain_team_name"],
            tuple(
                (slot["slot_number"], tuple(slot["desired_heroes"]))
                for slot in payload["dream_slots"]
                if slot["desired_heroes"]
            ),
        )
        self.assertIn(
            observed,
            {
                ("Alpha Team", ((1, ("Abrams",)),)),
                ("Beta Team", ((6, ("Ivy",)),)),
            },
            "a replace-all write must not merge the two concurrent payloads",
        )
