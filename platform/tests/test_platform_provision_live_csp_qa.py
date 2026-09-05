from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import delete, func, select

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    DeadlockProfile,
    MediaAsset,
    PasswordCredential,
    PlayerProfile,
    Role,
    Tournament,
    TournamentParticipant,
    User,
    UserRole,
    UserSession,
)
from tests.platform_async_case import PlatformIsolatedAsyncioTestCase
from tools import platform_cleanup_live_user_qa as cleanup_tool
from tools import platform_provision_live_csp_qa as provisioner


class LiveCspQaProvisionerUnitTests(unittest.TestCase):
    def test_runtime_target_requires_production_without_internal_test_override(self) -> None:
        settings = SimpleNamespace(platform_environment="test")
        with patch.object(provisioner, "validate_platform_settings", return_value=None):
            with self.assertRaisesRegex(
                provisioner.ProvisioningError,
                "outside production",
            ):
                provisioner.validate_runtime_target(settings)
            provisioner.validate_runtime_target(
                settings,
                allow_test_environment=True,
            )

        production = SimpleNamespace(
            platform_environment="production",
            platform_web_origin="https://attacker.example",
        )
        with (
            patch.object(provisioner, "validate_platform_settings", return_value=None),
            self.assertRaisesRegex(provisioner.ProvisioningError, "old-sparky.com"),
        ):
            provisioner.validate_runtime_target(production)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned bundle contract requires root")
    def test_bundle_replacement_refuses_manual_and_automated_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_dir = Path(temporary)
            secret_dir.chmod(0o700)
            bundle_path = secret_dir / "csp-live-qa.json"
            manual_state = secret_dir / "csp-live-qa.manual-auth-state.json"
            manual_state.write_text("{}", encoding="utf-8")
            manual_state.chmod(0o600)
            with self.assertRaisesRegex(
                provisioner.ProvisioningError,
                "manual QA recovery",
            ):
                provisioner.validate_recovery_state_absent(bundle_path)
            manual_state.unlink()

            automated_state = secret_dir / "live-user-qa.recovery"
            automated_state.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                provisioner.ProvisioningError,
                "automated QA recovery",
            ):
                provisioner.validate_recovery_state_absent(bundle_path)

    def test_marker_reuse_refuses_a_remaining_qa_tournament(self) -> None:
        with (
            patch.object(
                provisioner,
                "_marker_user_ids",
                AsyncMock(return_value=set()),
            ),
            patch.object(
                provisioner,
                "_marker_tournament_ids",
                AsyncMock(return_value={str(uuid4())}),
            ),
            self.assertRaisesRegex(provisioner.ProvisioningError, "tournaments"),
        ):
            asyncio.run(
                provisioner.validate_replacement_scope(
                    AsyncMock(),
                    marker="liveqa-csp-tournament-reuse",
                    existing=None,
                )
            )

    def test_marker_emails_and_unique_passwords_are_bounded(self) -> None:
        marker = "liveqa-csp-unit-20260809"

        primary = provisioner.marker_email("qa@auth.old-sparky.com", marker)
        roster = provisioner.roster_emails("qa@auth.old-sparky.com", marker)
        passwords = provisioner.generate_unique_passwords(
            14,
            factory=(
                value
                for value in (f"unique-password-{index:02d}" for index in range(14))
            ).__next__,
        )

        self.assertEqual(primary, f"qa+{marker}@auth.old-sparky.com")
        self.assertEqual(len(roster), 13)
        self.assertEqual(len(set(roster)), 13)
        self.assertTrue(all(email.count(marker) == 1 for email in roster))
        self.assertEqual(len(passwords), 14)
        self.assertEqual(len(set(passwords)), 14)
        self.assertTrue(all(10 <= len(password) <= 128 for password in passwords))

    @unittest.skipUnless(os.geteuid() == 0, "root-owned helper contract requires root")
    def test_mailbox_helper_requires_exact_owner_only_executable_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary) / "helper"
            helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            helper.chmod(0o700)
            with self.assertRaisesRegex(provisioner.ProvisioningError, "0500"):
                provisioner.validate_mailbox_helper(helper)

            helper.chmod(0o500)
            self.assertEqual(provisioner.validate_mailbox_helper(helper), str(helper))

    @unittest.skipUnless(os.geteuid() == 0, "root-controlled helper chain requires root")
    def test_mailbox_helper_rejects_non_root_controlled_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            untrusted = Path(temporary) / "untrusted"
            untrusted.mkdir(mode=0o700)
            helper = untrusted / "helper"
            helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            helper.chmod(0o500)
            os.chown(untrusted, 65534, 65534)
            with self.assertRaisesRegex(
                provisioner.ProvisioningError,
                "root-controlled",
            ):
                provisioner.validate_mailbox_helper(helper)
            os.chown(untrusted, 0, 0)
            second_link = untrusted / "helper-link"
            os.link(helper, second_link)
            with self.assertRaisesRegex(
                provisioner.ProvisioningError,
                "single-link",
            ):
                provisioner.validate_mailbox_helper(helper)

    def test_bundle_schema_rejects_bool_version_timestamp_and_nested_extra(
        self,
    ) -> None:
        marker = "liveqa-csp-schema-20260809"
        accounts = [
            {
                "id": str(uuid4()),
                "email": email,
                "password": f"schema-password-{index:02d}",
            }
            for index, email in enumerate(
                provisioner.roster_emails("qa@auth.old-sparky.com", marker),
                start=1,
            )
        ]
        payload: dict[str, object] = {
            "version": 1,
            "marker": marker,
            "created_at": "2026-08-09T12:00:00Z",
            "email": "qa@auth.old-sparky.com",
            "password": "primary-password-01",
            "mailbox_helper": "/root/helper",
            "roster_accounts": accounts,
        }
        with patch.object(
            provisioner, "validate_mailbox_helper", return_value="/root/helper"
        ):
            provisioner.validate_bundle_payload(payload)

            payload["version"] = True
            with self.assertRaisesRegex(provisioner.ProvisioningError, "version"):
                provisioner.validate_bundle_payload(payload)
            payload["version"] = 1

            payload["created_at"] = "2026-08-09T12:00:00+00:00"
            with self.assertRaisesRegex(provisioner.ProvisioningError, "created_at"):
                provisioner.validate_bundle_payload(payload)
            payload["created_at"] = "2026-08-09T12:00:00Z"

            accounts[0]["role"] = "admin"
            with self.assertRaisesRegex(
                provisioner.ProvisioningError, "unexpected schema"
            ):
                provisioner.validate_bundle_payload(payload)

    def test_bundle_parent_must_be_root_owned_0700(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            target = parent / "bundle.json"
            parent.chmod(0o755)
            with self.assertRaisesRegex(provisioner.ProvisioningError, "0700"):
                provisioner.validate_secret_parent(target)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned bundle contract requires root")
    def test_unknown_commit_outcome_retains_new_exact_id_bundle(self) -> None:
        marker = "liveqa-csp-unknown-commit"

        class FailingSession:
            rollback_calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, _kind, _value, _traceback):
                return False

            async def commit(self) -> None:
                raise RuntimeError("outcome unknown")

            async def rollback(self) -> None:
                self.rollback_calls += 1
                raise RuntimeError("connection is unavailable")

        class Factory:
            def __init__(self, session: FailingSession) -> None:
                self.session = session

            def __call__(self) -> FailingSession:
                return self.session

        with tempfile.TemporaryDirectory() as temporary:
            secret_dir = Path(temporary)
            secret_dir.chmod(0o700)
            helper = secret_dir / "helper"
            helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            helper.chmod(0o500)
            bundle_path = secret_dir / "bundle.json"
            emails = provisioner.roster_emails("qa@auth.old-sparky.com", marker)
            accounts = [
                {
                    "id": str(uuid4()),
                    "email": email,
                    "password": f"unknown-commit-{index:02d}",
                }
                for index, email in enumerate(emails, start=1)
            ]
            session = FailingSession()
            with (
                patch.object(
                    provisioner, "session_factory", return_value=Factory(session)
                ),
                patch.object(provisioner, "validate_runtime_target", return_value=None),
                patch.object(
                    provisioner,
                    "validate_replacement_scope",
                    AsyncMock(return_value=None),
                ),
                patch.object(
                    provisioner,
                    "create_roster",
                    AsyncMock(return_value=accounts),
                ),
            ):
                with self.assertRaisesRegex(
                    provisioner.ProvisioningError, "outcome is unknown"
                ):
                    asyncio.run(
                        provisioner.provision_bundle(
                            marker=marker,
                            bundle_path=bundle_path,
                            primary_email="qa@auth.old-sparky.com",
                            mailbox_helper=helper,
                            now=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
                        )
                    )

            self.assertTrue(bundle_path.is_file())
            retained = provisioner.load_bundle(bundle_path)
            self.assertEqual(retained.marker, marker)
            self.assertEqual(
                set(retained.user_ids), {account["id"] for account in accounts}
            )
            self.assertEqual(session.rollback_calls, 1)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned bundle contract requires root")
    def test_parent_fsync_failure_reports_unknown_published_state(self) -> None:
        marker = "liveqa-csp-unknown-fsync"

        class FailingSession:
            commit_calls = 0
            rollback_calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, _kind, _value, _traceback):
                return False

            async def commit(self) -> None:
                self.commit_calls += 1

            async def rollback(self) -> None:
                self.rollback_calls += 1

        class Factory:
            def __init__(self, session: FailingSession) -> None:
                self.session = session

            def __call__(self) -> FailingSession:
                return self.session

        with tempfile.TemporaryDirectory() as temporary:
            secret_dir = Path(temporary)
            secret_dir.chmod(0o700)
            helper = secret_dir / "helper"
            helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            helper.chmod(0o500)
            bundle_path = secret_dir / "bundle.json"
            accounts = [
                {
                    "id": str(uuid4()),
                    "email": email,
                    "password": f"unknown-fsync-{index:02d}",
                }
                for index, email in enumerate(
                    provisioner.roster_emails(
                        "qa@auth.old-sparky.com",
                        marker,
                    ),
                    start=1,
                )
            ]
            session = FailingSession()
            with (
                patch.object(
                    provisioner,
                    "session_factory",
                    return_value=Factory(session),
                ),
                patch.object(
                    provisioner,
                    "validate_runtime_target",
                    return_value=None,
                ),
                patch.object(
                    provisioner,
                    "validate_replacement_scope",
                    AsyncMock(return_value=None),
                ),
                patch.object(
                    provisioner,
                    "create_roster",
                    AsyncMock(return_value=accounts),
                ),
                patch.object(
                    provisioner,
                    "_fsync_parent",
                    side_effect=OSError("fsync failed"),
                ),
            ):
                with self.assertRaisesRegex(
                    provisioner.ProvisioningError,
                    "outcome is unknown",
                ):
                    asyncio.run(
                        provisioner.provision_bundle(
                            marker=marker,
                            bundle_path=bundle_path,
                            primary_email="qa@auth.old-sparky.com",
                            mailbox_helper=helper,
                            now=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
                        )
                    )

            self.assertTrue(bundle_path.is_file())
            self.assertEqual(provisioner.load_bundle(bundle_path).marker, marker)
            self.assertEqual(session.commit_calls, 0)
            self.assertEqual(session.rollback_calls, 1)


@unittest.skipUnless(os.geteuid() == 0, "root-owned bundle contract requires root")
class LiveCspQaProvisionerIntegrationTests(PlatformIsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = get_settings()
        if (
            settings.platform_environment != "test"
            or "platformdb_test" not in settings.platform_database_url
        ):
            self.skipTest("isolated platform test database is required")
        self.unique = uuid4().hex[:8]
        self.candidate_marker = f"liveqa-csp-candidate-{self.unique}"
        self.final_marker = f"liveqa-csp-final-{self.unique}"
        self.temporary = tempfile.TemporaryDirectory()
        self.secret_dir = Path(self.temporary.name)
        self.secret_dir.chmod(0o700)
        self.bundle_path = self.secret_dir / "csp-live-qa.json"
        self.inventory_path = self.secret_dir / "inventory.json"
        self.sessions_path = self.secret_dir / "browser-sessions.json"
        self.helper = self.secret_dir / "mailbox-helper"
        self.helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.helper.chmod(0o500)

    async def asyncTearDown(self) -> None:
        markers = (self.candidate_marker, self.final_marker)
        async with session_factory()() as db_session:
            user_ids = list(
                (
                    await db_session.scalars(
                        select(User.id).where(
                            func.lower(User.email).contains(markers[0])
                            | func.lower(User.email).contains(markers[1])
                        )
                    )
                ).all()
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
        self.temporary.cleanup()

    async def _provision(self, marker: str) -> dict[str, object]:
        with patch.object(
            provisioner, "hash_password", side_effect=lambda value: f"test:{value}"
        ):
            return await provisioner.provision_bundle(
                marker=marker,
                bundle_path=self.bundle_path,
                primary_email="qa@auth.old-sparky.com",
                mailbox_helper=self.helper,
                now=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
                _allow_test_environment=True,
            )

    async def test_exact_roster_cleanup_and_safe_final_replacement(self) -> None:
        candidate = await self._provision(self.candidate_marker)
        candidate_accounts = candidate["roster_accounts"]
        assert isinstance(candidate_accounts, list)
        candidate_ids = [str(account["id"]) for account in candidate_accounts]

        async with session_factory()() as db_session:
            rows = (
                await db_session.execute(
                    select(User, PlayerProfile, DeadlockProfile, PasswordCredential)
                    .join(PlayerProfile, PlayerProfile.user_id == User.id)
                    .join(DeadlockProfile, DeadlockProfile.user_id == User.id)
                    .join(PasswordCredential, PasswordCredential.user_id == User.id)
                    .where(User.id.in_(candidate_ids))
                )
            ).all()
            role_rows = (
                await db_session.execute(
                    select(UserRole.user_id, Role.slug)
                    .join(Role, Role.id == UserRole.role_id)
                    .where(UserRole.user_id.in_(candidate_ids))
                )
            ).all()
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(row.User.status == "active" for row in rows))
        self.assertTrue(all(row.User.email_verified_at is not None for row in rows))
        self.assertTrue(all(row.DeadlockProfile.rank == "Phantom" for row in rows))
        self.assertTrue(
            all(
                set(row.DeadlockProfile.roles) == set(provisioner.ROLE_OPTIONS)
                for row in rows
            )
        )
        roles_by_user = {
            user_id: {
                slug
                for candidate_user_id, slug in role_rows
                if candidate_user_id == user_id
            }
            for user_id in candidate_ids
        }
        self.assertTrue(
            all(
                value == {"authenticated_user", "player"}
                for value in roles_by_user.values()
            )
        )
        self.assertFalse(
            any(
                "admin" in value or "superadmin" in value
                for value in roles_by_user.values()
            )
        )

        with self.assertRaisesRegex(provisioner.ProvisioningError, "prior exact QA scope"):
            await self._provision(self.final_marker)

        marker = provisioner.prepare_inventory(
            bundle_path=self.bundle_path,
            inventory_path=self.inventory_path,
        )
        self.assertEqual(marker, self.candidate_marker)
        inventory_payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        self.assertEqual(set(inventory_payload["user_ids"]), set(candidate_ids))

        async with session_factory()() as db_session:
            tampered = await db_session.get(User, candidate_ids[0])
            assert tampered is not None
            original_email = tampered.email
            tampered.email = f"tampered-{self.unique}@example.net"
            await db_session.commit()
        with self.assertRaisesRegex(
            provisioner.ProvisioningError,
            "exactly match",
        ):
            await provisioner.prepare_browser_sessions(
                bundle_path=self.bundle_path,
                inventory_path=self.inventory_path,
                sessions_path=self.sessions_path,
                now=datetime.now(UTC),
                _allow_test_environment=True,
            )
        self.assertFalse(self.sessions_path.exists())
        self.assertEqual(
            len(json.loads(self.inventory_path.read_text(encoding="utf-8"))["user_ids"]),
            13,
        )
        async with session_factory()() as db_session:
            tampered = await db_session.get(User, candidate_ids[0])
            assert tampered is not None
            tampered.email = original_email
            await db_session.commit()

        with patch.object(
            provisioner, "hash_password", side_effect=lambda value: f"test:{value}"
        ):
            session_marker = await provisioner.prepare_browser_sessions(
                bundle_path=self.bundle_path,
                inventory_path=self.inventory_path,
                sessions_path=self.sessions_path,
                now=datetime.now(UTC),
                _allow_test_environment=True,
            )
        self.assertEqual(session_marker, self.candidate_marker)
        self.assertEqual(self.sessions_path.stat().st_mode & 0o777, 0o600)
        session_payload = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(session_payload),
            {
                "version",
                "marker",
                "cookie_name",
                "created_at",
                "expires_at",
                "roster_sessions",
                "workflow_player",
            },
        )
        created_at = datetime.fromisoformat(
            session_payload["created_at"].replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(
            session_payload["expires_at"].replace("Z", "+00:00")
        )
        self.assertEqual(expires_at - created_at, timedelta(hours=1))
        self.assertEqual(len(session_payload["roster_sessions"]), 13)
        all_session_entries = [
            *session_payload["roster_sessions"],
            session_payload["workflow_player"],
        ]
        self.assertEqual(len({row["user_id"] for row in all_session_entries}), 14)
        self.assertEqual(len({row["session_id"] for row in all_session_entries}), 14)
        self.assertEqual(len({row["token"] for row in all_session_entries}), 14)
        inventory_payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(inventory_payload["user_ids"]),
            {row["user_id"] for row in all_session_entries},
        )

        async with session_factory()() as db_session:
            stored_sessions = list(
                (
                    await db_session.scalars(
                        select(UserSession).where(
                            UserSession.user_id.in_(inventory_payload["user_ids"])
                        )
                    )
                ).all()
            )
        self.assertEqual(len(stored_sessions), 14)
        self.assertEqual(
            {row.token_digest for row in stored_sessions},
            {
                provisioner.session_token_digest(row["token"])
                for row in all_session_entries
            },
        )
        inventory = cleanup_tool.load_inventory(
            self.inventory_path,
            expected_marker=self.candidate_marker,
        )
        cleanup_result = await cleanup_tool.cleanup(
            inventory,
            _allow_test_environment=True,
        )
        self.assertEqual(cleanup_result["users"], 14)
        self.assertEqual(cleanup_result["sessions"], 14)
        async with session_factory()() as db_session:
            remaining_sessions = await db_session.scalar(
                select(func.count(UserSession.id)).where(
                    UserSession.user_id.in_(inventory_payload["user_ids"])
                )
            )
        self.assertEqual(int(remaining_sessions or 0), 0)

        final = await self._provision(self.final_marker)
        self.assertEqual(final["marker"], self.final_marker)
        self.assertNotEqual(candidate["created_at"], "")
        stored = provisioner.load_bundle(self.bundle_path)
        self.assertEqual(stored.marker, self.final_marker)
        self.assertEqual(len(stored.user_ids), 13)

        self.inventory_path.unlink()
        provisioner.prepare_inventory(
            bundle_path=self.bundle_path,
            inventory_path=self.inventory_path,
        )
        final_inventory = cleanup_tool.load_inventory(
            self.inventory_path,
            expected_marker=self.final_marker,
        )
        final_cleanup = await cleanup_tool.cleanup(
            final_inventory,
            _allow_test_environment=True,
        )
        self.assertEqual(final_cleanup["users"], 13)

    async def test_cleanup_recovers_when_recorded_workflow_user_is_already_absent(
        self,
    ) -> None:
        await self._provision(self.candidate_marker)
        provisioner.prepare_inventory(
            bundle_path=self.bundle_path,
            inventory_path=self.inventory_path,
        )
        with patch.object(
            provisioner, "hash_password", side_effect=lambda value: f"test:{value}"
        ):
            await provisioner.prepare_browser_sessions(
                bundle_path=self.bundle_path,
                inventory_path=self.inventory_path,
                sessions_path=self.sessions_path,
                now=datetime.now(UTC),
                _allow_test_environment=True,
            )
        session_payload = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        workflow_user_id = str(session_payload["workflow_player"]["user_id"])
        inventory_payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        self.assertIn(workflow_user_id, inventory_payload["user_ids"])

        async with session_factory()() as db_session:
            await db_session.execute(delete(User).where(User.id == workflow_user_id))
            await db_session.commit()

        inventory = cleanup_tool.load_inventory(
            self.inventory_path,
            expected_marker=self.candidate_marker,
        )
        result = await cleanup_tool.cleanup(
            inventory,
            _allow_test_environment=True,
        )

        self.assertEqual(result["users"], 13)
        self.assertEqual(result["sessions"], 13)

    async def test_cleanup_refuses_user_linked_to_external_tournament(self) -> None:
        candidate = await self._provision(self.candidate_marker)
        provisioner.prepare_inventory(
            bundle_path=self.bundle_path,
            inventory_path=self.inventory_path,
        )
        candidate_accounts = candidate["roster_accounts"]
        assert isinstance(candidate_accounts, list)
        candidate_user_id = str(candidate_accounts[0]["id"])
        owner_id = str(uuid4())
        tournament_id = str(uuid4())
        participant_id = str(uuid4())
        async with session_factory()() as db_session:
            db_session.add(
                User(
                    id=owner_id,
                    email=f"external-owner-{self.unique}@example.net",
                    display_name="External owner",
                )
            )
            await db_session.flush()
            db_session.add(
                Tournament(
                    id=tournament_id,
                    slug=f"external-{self.unique}",
                    name=f"External {self.unique}",
                    description="Not live QA scope.",
                    visibility="invite_only",
                    format_slug="deadlock",
                    organizer_user_id=owner_id,
                )
            )
            db_session.add(
                TournamentParticipant(
                    id=participant_id,
                    tournament_id=tournament_id,
                    user_id=candidate_user_id,
                )
            )
            await db_session.commit()

        inventory = cleanup_tool.load_inventory(
            self.inventory_path,
            expected_marker=self.candidate_marker,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "outside the inventory"):
                await cleanup_tool.cleanup(
                    inventory,
                    _allow_test_environment=True,
                )
            async with session_factory()() as db_session:
                self.assertIsNotNone(await db_session.get(User, candidate_user_id))
                self.assertIsNotNone(
                    await db_session.get(TournamentParticipant, participant_id)
                )
        finally:
            async with session_factory()() as db_session:
                await db_session.execute(
                    delete(TournamentParticipant).where(
                        TournamentParticipant.id == participant_id
                    )
                )
                await db_session.execute(
                    delete(Tournament).where(Tournament.id == tournament_id)
                )
                await db_session.execute(delete(User).where(User.id == owner_id))
                await db_session.commit()
        result = await cleanup_tool.cleanup(
            inventory,
            _allow_test_environment=True,
        )
        self.assertEqual(result["users"], 13)

    async def test_cleanup_accepts_one_exact_marker_tournament_graph(self) -> None:
        candidate = await self._provision(self.candidate_marker)
        provisioner.prepare_inventory(
            bundle_path=self.bundle_path,
            inventory_path=self.inventory_path,
        )
        accounts = candidate["roster_accounts"]
        assert isinstance(accounts, list)
        tournament_id = str(uuid4())
        async with session_factory()() as db_session:
            db_session.add(
                Tournament(
                    id=tournament_id,
                    slug=f"exact-{self.unique}",
                    name=f"Exact {self.unique}",
                    description=(
                        "Accelerated live browser acceptance "
                        f"{self.candidate_marker}."
                    ),
                    visibility="invite_only",
                    format_slug="deadlock",
                    organizer_user_id=str(accounts[0]["id"]),
                )
            )
            await db_session.commit()
        payload = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        payload["tournament_ids"] = [tournament_id]
        provisioner.publish_secret_json(
            self.inventory_path,
            payload,
            expected_existing=provisioner._load_preseeded_inventory(
                self.inventory_path,
                bundle=provisioner.load_bundle(self.bundle_path),
            ),
        )

        inventory = cleanup_tool.load_inventory(
            self.inventory_path,
            expected_marker=self.candidate_marker,
        )
        result = await cleanup_tool.cleanup(
            inventory,
            _allow_test_environment=True,
        )

        self.assertEqual(result["tournaments"], 1)
        self.assertEqual(result["users"], 13)

    async def test_cleanup_refuses_real_actor_audit_for_inventory_user(self) -> None:
        candidate = await self._provision(self.candidate_marker)
        provisioner.prepare_inventory(
            bundle_path=self.bundle_path,
            inventory_path=self.inventory_path,
        )
        accounts = candidate["roster_accounts"]
        assert isinstance(accounts, list)
        candidate_user_id = str(accounts[0]["id"])
        actor_id = str(uuid4())
        audit_id: int | None = None
        async with session_factory()() as db_session:
            db_session.add(
                User(
                    id=actor_id,
                    email=f"external-auditor-{self.unique}@example.net",
                    display_name="External auditor",
                )
            )
            await db_session.flush()
            row = AuditLog(
                actor_user_id=actor_id,
                action="admin.user.status.update",
                subject_type="user",
                subject_id=candidate_user_id,
                payload={"status": "active"},
            )
            db_session.add(row)
            await db_session.commit()
            audit_id = row.id

        inventory = cleanup_tool.load_inventory(
            self.inventory_path,
            expected_marker=self.candidate_marker,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "real actor"):
                await cleanup_tool.cleanup(
                    inventory,
                    _allow_test_environment=True,
                )
            async with session_factory()() as db_session:
                self.assertIsNotNone(await db_session.get(AuditLog, audit_id))
        finally:
            async with session_factory()() as db_session:
                await db_session.execute(delete(AuditLog).where(AuditLog.id == audit_id))
                await db_session.execute(delete(User).where(User.id == actor_id))
                await db_session.commit()
        result = await cleanup_tool.cleanup(
            inventory,
            _allow_test_environment=True,
        )
        self.assertEqual(result["users"], 13)

    async def test_cleanup_refuses_unrelated_recorded_deleted_media(self) -> None:
        owner_id = str(uuid4())
        media_id = str(uuid4())
        marker = f"liveqa-csp-media-{self.unique}"
        async with session_factory()() as db_session:
            db_session.add(
                User(
                    id=owner_id,
                    email=f"media-owner-{self.unique}@example.net",
                    display_name="Media owner",
                )
            )
            await db_session.flush()
            db_session.add(
                MediaAsset(
                    id=media_id,
                    owner_user_id=owner_id,
                    purpose="profile_avatar",
                    status="deleted",
                    source_mime="image/png",
                    source_bytes=1,
                    source_sha256="0" * 64,
                    version_id=str(uuid4()),
                )
            )
            await db_session.commit()

        inventory = cleanup_tool.CleanupInventory(
            marker=marker,
            user_ids=(),
            tournament_ids=(),
            media_ids=(media_id,),
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "outside liveqa ownership"):
                await cleanup_tool.cleanup(
                    inventory,
                    _allow_test_environment=True,
                )
            async with session_factory()() as db_session:
                self.assertIsNotNone(await db_session.get(MediaAsset, media_id))
        finally:
            async with session_factory()() as db_session:
                await db_session.execute(delete(MediaAsset).where(MediaAsset.id == media_id))
                await db_session.execute(delete(User).where(User.id == owner_id))
                await db_session.commit()


if __name__ == "__main__":
    unittest.main()
