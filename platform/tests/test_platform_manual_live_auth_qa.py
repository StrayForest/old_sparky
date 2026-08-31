from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, or_, select

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    PasswordCredential,
    User,
    UserSession,
)
from python_packages.platform_infra.security import hash_password
from tools import platform_manual_live_auth_qa as manual_tool


NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


@unittest.skipUnless(os.geteuid() == 0, "root-only manual QA contract requires root")
class ManualLiveAuthQaUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.secret_dir = Path(self.temporary.name)
        self.secret_dir.chmod(0o700)
        self.marker = f"liveqa-manual-{uuid4().hex[:10]}"
        self.helper = self.secret_dir / "mailbox-helper"
        self.helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.helper.chmod(0o500)
        self.bundle_path = self.secret_dir / "csp-live-qa.json"
        self.registration_password = "RegistrationPassword-01"
        payload = {
            "version": 1,
            "marker": self.marker,
            "created_at": manual_tool._timestamp(NOW - timedelta(seconds=30)),
            "email": "qa@auth.old-sparky.com",
            "password": self.registration_password,
            "mailbox_helper": str(self.helper),
            "roster_accounts": [
                {
                    "id": str(uuid4()),
                    "email": f"roster{index:02d}+{self.marker}@auth.old-sparky.com",
                    "password": f"RosterPassword-{index:02d}",
                }
                for index in range(1, 14)
            ],
        }
        self.bundle_path.write_text(json.dumps(payload), encoding="utf-8")
        self.bundle_path.chmod(0o600)
        self.bundle = manual_tool._load_bundle(self.bundle_path, now=NOW)
        self.reset_password = "ResetPassword-For-Manual-QA"
        manual_tool._publish_private_json(
            manual_tool._state_path(self.bundle_path),
            {
                "version": 1,
                "marker": self.marker,
                "prepared_at": manual_tool._timestamp(NOW - timedelta(seconds=20)),
                "expected_origin": manual_tool.EXPECTED_PRODUCTION_ORIGIN,
                "bundle_fingerprint": list(self.bundle.fingerprint),
                "reset_password": self.reset_password,
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepared_state_exposes_only_requested_secret_in_memory(self) -> None:
        self.assertEqual(
            manual_tool.secret_for_display(
                bundle_path=self.bundle_path,
                kind="email",
                now=NOW,
            ),
            f"qa+{self.marker}@auth.old-sparky.com",
        )
        self.assertEqual(
            manual_tool.secret_for_display(
                bundle_path=self.bundle_path,
                kind="display-name",
                now=NOW,
            ),
            manual_tool.manual_display_name(self.marker),
        )
        self.assertRegex(manual_tool.manual_display_name(self.marker), r"^liveqa-[0-9a-f]{8}$")
        self.assertLessEqual(len(manual_tool.manual_display_name(self.marker)), 15)
        self.assertEqual(
            manual_tool.secret_for_display(
                bundle_path=self.bundle_path,
                kind="registration-password",
                now=NOW,
            ),
            self.registration_password,
        )
        self.assertEqual(
            manual_tool.secret_for_display(
                bundle_path=self.bundle_path,
                kind="reset-password",
                now=NOW,
            ),
            self.reset_password,
        )
        state_path = manual_tool._state_path(self.bundle_path)
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_state_rejects_mode_symlink_expiry_and_bundle_replacement(self) -> None:
        state_path = manual_tool._state_path(self.bundle_path)
        state_path.chmod(0o640)
        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "root-owned 0600",
        ):
            manual_tool._load_prepared(bundle_path=self.bundle_path, now=NOW)
        state_path.chmod(0o600)

        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "expired",
        ):
            manual_tool._load_prepared(
                bundle_path=self.bundle_path,
                now=NOW + timedelta(hours=3),
            )
        stale_bundle, stale_state = manual_tool._load_prepared(
            bundle_path=self.bundle_path,
            now=NOW + timedelta(hours=6),
            require_fresh=False,
        )
        self.assertEqual(stale_bundle.marker, self.marker)
        self.assertEqual(stale_state.marker, self.marker)

        link = state_path.with_name("state-link.json")
        link.symlink_to(state_path)
        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "0600 regular file",
        ):
            manual_tool._load_state(
                link,
                bundle_path=self.bundle_path,
                bundle=self.bundle,
                now=NOW,
            )

        payload = json.loads(self.bundle_path.read_text(encoding="utf-8"))
        self.bundle_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.bundle_path.chmod(0o600)
        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "bundle changed",
        ):
            manual_tool._load_prepared(bundle_path=self.bundle_path, now=NOW)

    def test_private_json_write_loop_handles_partial_writes_without_skipping(self) -> None:
        target = self.secret_dir / "partial.json"
        payload = {"version": 1, "value": "x" * 200}
        real_write = os.write

        def partial_write(descriptor: int, value) -> int:
            return real_write(descriptor, bytes(value[:7]))

        with patch.object(manual_tool.os, "write", side_effect=partial_write):
            manual_tool._publish_private_json(target, payload)

        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_private_json_link_failure_never_publishes_a_partial_target(self) -> None:
        target = self.secret_dir / "atomic.json"

        with (
            patch.object(manual_tool.os, "link", side_effect=OSError("link failed")),
            self.assertRaisesRegex(
                manual_tool.ManualLiveAuthQaError,
                "publication failed",
            ),
        ):
            manual_tool._publish_private_json(target, {"version": 1})

        self.assertFalse(target.exists())
        self.assertEqual(tuple(self.secret_dir.glob(f".{target.name}.*.tmp")), ())

    def test_private_json_parent_fsync_ambiguity_retains_only_complete_target(self) -> None:
        target = self.secret_dir / "durable.json"
        payload = {"version": 1, "value": "complete"}

        with (
            patch.object(
                manual_tool,
                "_fsync_parent",
                side_effect=OSError("fsync failed"),
            ),
            self.assertRaisesRegex(
                manual_tool.ManualLiveAuthQaError,
                "complete target may be retained",
            ),
        ):
            manual_tool._publish_private_json(target, payload)

        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
        self.assertEqual(tuple(self.secret_dir.glob(f".{target.name}.*.tmp")), ())

    def test_mailbox_exec_has_no_email_password_code_or_inherited_environment(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(argv: list[str], **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, stdout=b"123456\n", stderr=b"")

        code = manual_tool.mailbox_code(
            bundle_path=self.bundle_path,
            purpose="email-verification",
            now=NOW,
            runner=runner,
        )

        self.assertEqual(code, "123456")
        self.assertEqual(calls[0][0], [str(self.helper), "code", "email-verification"])
        self.assertEqual(
            calls[0][1]["env"],
            {manual_tool.MAILBOX_ENV_NAME: str(self.bundle_path)},
        )
        self.assertIs(calls[0][1]["stdin"], subprocess.DEVNULL)
        self.assertFalse(calls[0][1]["shell"])
        invocation = repr(calls)
        self.assertNotIn(f"qa+{self.marker}@auth.old-sparky.com", invocation)
        self.assertNotIn(self.registration_password, invocation)
        self.assertNotIn(self.reset_password, invocation)
        self.assertNotIn("123456", repr(calls[0][0]) + repr(calls[0][1]["env"]))

    def test_mailbox_output_is_exactly_one_six_digit_code(self) -> None:
        for stdout, stderr, returncode in (
            (b"12345\n", b"", 0),
            (b"123456 extra\n", b"", 0),
            (b"123456\n", b"warning", 0),
            (b"", b"failed", 1),
        ):
            with self.subTest(stdout=stdout, stderr=stderr, returncode=returncode):
                def runner(*_args, **_kwargs):
                    return subprocess.CompletedProcess(
                        [], returncode, stdout=stdout, stderr=stderr
                    )

                with self.assertRaises(manual_tool.ManualLiveAuthQaError):
                    manual_tool.mailbox_code(
                        bundle_path=self.bundle_path,
                        purpose="password-reset",
                        now=NOW,
                        runner=runner,
                    )

    def test_tty_writer_fails_closed_without_root_or_real_tty(self) -> None:
        with (
            patch.object(manual_tool.os, "geteuid", return_value=1000),
            self.assertRaisesRegex(manual_tool.ManualLiveAuthQaError, "requires root"),
        ):
            manual_tool.write_secret_to_interactive_tty("sensitive")

        with (
            patch.object(manual_tool.os, "open", side_effect=OSError("no tty")),
            self.assertRaisesRegex(manual_tool.ManualLiveAuthQaError, "interactive TTY"),
        ):
            manual_tool.write_secret_to_interactive_tty("sensitive")

    def test_tty_writer_handles_partial_writes(self) -> None:
        written = bytearray()

        def partial_write(_descriptor: int, value) -> int:
            chunk = bytes(value[:2])
            written.extend(chunk)
            return len(chunk)

        with (
            patch.object(manual_tool.os, "open", return_value=100),
            patch.object(
                manual_tool.os,
                "fstat",
                return_value=SimpleNamespace(st_mode=stat.S_IFCHR | 0o600),
            ),
            patch.object(manual_tool.os, "isatty", return_value=True),
            patch.object(manual_tool.os, "write", side_effect=partial_write),
            patch.object(manual_tool.os, "close"),
        ):
            manual_tool.write_secret_to_interactive_tty("sensitive")

        self.assertEqual(bytes(written), b"sensitive\n")

    def test_runtime_gate_rejects_wrong_origin_and_non_always_turnstile(self) -> None:
        settings = get_settings().model_copy(
            update={
                "platform_environment": "production",
                "platform_web_origin": "https://wrong.example",
                "platform_turnstile_mode": "always",
            }
        )
        with (
            patch.object(manual_tool, "validate_platform_settings", return_value=None),
            patch.object(manual_tool, "validate_auth_security_settings", return_value=None),
        ):
            with self.assertRaisesRegex(
                manual_tool.ManualLiveAuthQaError,
                "canonical production origin",
            ):
                manual_tool._validate_runtime_target(settings)

            settings = settings.model_copy(
                update={
                    "platform_web_origin": manual_tool.EXPECTED_PRODUCTION_ORIGIN,
                    "platform_turnstile_mode": "adaptive",
                }
            )
            with self.assertRaisesRegex(
                manual_tool.ManualLiveAuthQaError,
                "always mode",
            ):
                manual_tool._validate_runtime_target(settings)

    def test_root_qa_wrappers_disable_xtrace_and_do_not_source_runtime_env(self) -> None:
        for name in (
            "platform_provision_live_csp_qa.sh",
            "platform_live_user_qa.sh",
            "platform_manual_live_auth_qa.sh",
        ):
            with self.subTest(wrapper=name):
                source = (TOOLS_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(source.splitlines()[1], "set +x")
                self.assertNotIn("source ", source)
                self.assertNotIn("platform_runtime_common.sh", source)
                self.assertNotIn("platform_load_env_file", source)
                self.assertNotIn("${PYTHONPATH:+:$PYTHONPATH}", source)
                self.assertIn('SYSTEM_PYTHON="/usr/bin/python3.12"', source)
                self.assertIn("platform_safe_env_exec.py", source)


@unittest.skipUnless(os.geteuid() == 0, "root-only manual QA contract requires root")
class ManualLiveAuthQaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = get_settings()
        if (
            settings.platform_environment != "test"
            or "platformdb_test" not in settings.platform_database_url
        ):
            self.skipTest("isolated platform test database is required")
        # IsolatedAsyncioTestCase gives every test its own event loop, while
        # the infrastructure module keeps one process-global AsyncEngine.
        # Dispose any engine left by an earlier test before checking out the
        # first connection on this loop.
        await dispose_engine()
        self.temporary = tempfile.TemporaryDirectory()
        self.secret_dir = Path(self.temporary.name)
        self.secret_dir.chmod(0o700)
        self.marker = f"liveqa-manual-{uuid4().hex[:10]}"
        self.helper = self.secret_dir / "mailbox-helper"
        self.helper.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.helper.chmod(0o500)
        self.bundle_path = self.secret_dir / "csp-live-qa.json"
        self.registration_password = "RegistrationPassword-02"
        self.reset_password = "ResetPassword-For-Integration"
        self.now = datetime.now(UTC).replace(microsecond=0)
        payload = {
            "version": 1,
            "marker": self.marker,
            "created_at": manual_tool._timestamp(self.now - timedelta(minutes=2)),
            "email": "qa@auth.old-sparky.com",
            "password": self.registration_password,
            "mailbox_helper": str(self.helper),
            "roster_accounts": [
                {
                    "id": str(uuid4()),
                    "email": f"roster{index:02d}+{self.marker}@auth.old-sparky.com",
                    "password": f"RosterPassword-{index:02d}",
                }
                for index in range(1, 14)
            ],
        }
        self.bundle_path.write_text(json.dumps(payload), encoding="utf-8")
        self.bundle_path.chmod(0o600)
        bundle = manual_tool._load_bundle(self.bundle_path, now=self.now)
        manual_tool._publish_private_json(
            manual_tool._state_path(self.bundle_path),
            {
                "version": 1,
                "marker": self.marker,
                "prepared_at": manual_tool._timestamp(self.now - timedelta(minutes=1)),
                "expected_origin": manual_tool.EXPECTED_PRODUCTION_ORIGIN,
                "bundle_fingerprint": list(bundle.fingerprint),
                "reset_password": self.reset_password,
            },
        )
        self.user_id = str(uuid4())
        self.email = f"qa+{self.marker}@auth.old-sparky.com"
        async with session_factory()() as db_session:
            user = User(
                id=self.user_id,
                email=self.email,
                display_name=manual_tool.manual_display_name(self.marker),
                status="active",
                email_verified_at=self.now,
                created_at=self.now,
                updated_at=self.now,
                public_tournament_credits=0,
                private_tournament_credits=0,
            )
            db_session.add(user)
            await db_session.flush()
            db_session.add(
                PasswordCredential(
                    user_id=self.user_id,
                    password_hash=hash_password(self.registration_password),
                    password_version="argon2id",
                )
            )
            sessions = [
                UserSession(
                    id=str(uuid4()),
                    user_id=self.user_id,
                    token_digest=f"{index:064x}",
                    created_at=self.now,
                    last_seen_at=self.now,
                    expires_at=self.now + timedelta(hours=1),
                    invalidated_at=self.now,
                )
                for index in range(1, 5)
            ]
            db_session.add_all(sessions)
            await db_session.flush()
            subjects = {
                "auth.logout:0": ("session", sessions[0].id),
                "auth.logout:1": ("session", sessions[1].id),
                "auth.logout:2": ("session", sessions[3].id),
            }
            logout_index = 0
            for action in manual_tool.REQUIRED_AUDIT_SEQUENCE:
                actor = self.user_id
                subject_type = "user"
                subject_id = self.user_id
                payload_for_action: dict[str, object] = {}
                if action in {
                    "auth.register",
                    "auth.email_verification.confirm",
                    "auth.password_reset.request",
                    "auth.password_reset.confirm",
                }:
                    actor = None
                if action == "auth.register":
                    payload_for_action = {
                        "email": self.email,
                        "verification_required": True,
                    }
                elif action == "auth.password_reset.confirm":
                    payload_for_action = {"session_rotated": True}
                elif action == "auth.account.update":
                    payload_for_action = {
                        "password_changed": True,
                        "email_changed": False,
                        "session_rotated": True,
                    }
                elif action == "auth.logout":
                    subject_type, subject_id = subjects[f"auth.logout:{logout_index}"]
                    logout_index += 1
                db_session.add(
                    AuditLog(
                        actor_user_id=actor,
                        action=action,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        payload=payload_for_action,
                        created_at=self.now,
                    )
                )
            await db_session.commit()

    async def asyncTearDown(self) -> None:
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(AuditLog).where(
                    or_(
                        AuditLog.actor_user_id == self.user_id,
                        (AuditLog.subject_type == "user")
                        & (AuditLog.subject_id == self.user_id),
                    )
                )
            )
            await db_session.execute(delete(User).where(User.id == self.user_id))
            await db_session.commit()
        await dispose_engine()
        self.temporary.cleanup()

    async def test_attestation_publishes_exact_inventory_and_cleanup_removes_everything(self) -> None:
        result = await manual_tool.attest_and_cleanup(
            bundle_path=self.bundle_path,
            now=self.now + timedelta(minutes=1),
            allow_test_environment=True,
        )

        self.assertEqual(result["users"], 1)
        self.assertEqual(result["sessions"], 4)
        self.assertFalse(manual_tool._state_path(self.bundle_path).exists())
        self.assertFalse(manual_tool._inventory_path(self.bundle_path).exists())
        async with session_factory()() as db_session:
            remaining = await db_session.scalar(
                select(func.count()).select_from(User).where(
                    func.lower(User.email).contains(self.marker)
                )
            )
            remaining_sessions = await db_session.scalar(
                select(func.count()).select_from(UserSession).where(
                    UserSession.user_id == self.user_id
                )
            )
        self.assertEqual(int(remaining or 0), 0)
        self.assertEqual(int(remaining_sessions or 0), 0)

    async def test_incomplete_flow_requires_abort_and_still_cleans_only_exact_user(self) -> None:
        async with session_factory()() as db_session:
            await db_session.execute(
                delete(AuditLog).where(
                    AuditLog.actor_user_id == self.user_id,
                    AuditLog.action == "auth.account.update",
                )
            )
            await db_session.commit()

        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "audit sequence",
        ):
            await manual_tool.attest_and_cleanup(
                bundle_path=self.bundle_path,
                now=self.now + timedelta(minutes=1),
                allow_test_environment=True,
            )
        self.assertFalse(manual_tool._inventory_path(self.bundle_path).exists())

        result = await manual_tool.abort_and_cleanup(
            bundle_path=self.bundle_path,
            now=self.now + timedelta(hours=6),
            allow_test_environment=True,
        )

        self.assertEqual(result["users"], 1)
        self.assertEqual(result["sessions"], 4)
        self.assertFalse(manual_tool._state_path(self.bundle_path).exists())
        self.assertFalse(manual_tool._inventory_path(self.bundle_path).exists())

    async def test_attestation_rejects_an_unexpected_user_audit_action(self) -> None:
        async with session_factory()() as db_session:
            db_session.add(
                AuditLog(
                    actor_user_id=self.user_id,
                    action="auth.logout_all",
                    subject_type="user",
                    subject_id=self.user_id,
                    payload={"sessions_invalidated": 0},
                    created_at=self.now,
                )
            )
            await db_session.commit()

        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "audit sequence",
        ):
            await manual_tool.attest_and_cleanup(
                bundle_path=self.bundle_path,
                now=self.now + timedelta(minutes=1),
                allow_test_environment=True,
            )

    async def test_attestation_requires_exactly_four_lifecycle_sessions(self) -> None:
        async with session_factory()() as db_session:
            db_session.add(
                UserSession(
                    id=str(uuid4()),
                    user_id=self.user_id,
                    token_digest=uuid4().hex + uuid4().hex,
                    created_at=self.now,
                    last_seen_at=self.now,
                    expires_at=self.now + timedelta(hours=1),
                    invalidated_at=self.now,
                )
            )
            await db_session.commit()

        with self.assertRaisesRegex(
            manual_tool.ManualLiveAuthQaError,
            "exactly four",
        ):
            await manual_tool.attest_and_cleanup(
                bundle_path=self.bundle_path,
                now=self.now + timedelta(minutes=1),
                allow_test_environment=True,
            )


if __name__ == "__main__":
    unittest.main()
