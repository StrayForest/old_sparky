from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import cast
from unittest import mock
from zoneinfo import ZoneInfo

from tests import platform_test_lock_support as lock_support
from tools import platform_recovery_bootstrap as recovery
from tools import platform_release_transaction as transaction
from tools import platform_release_systemd_state as systemd_state


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "platform/tools/platform_release_deploy.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "platform/tools/platform_release_rollback.sh"
ALEMBIC_SCRIPT = REPO_ROOT / "platform/tools/platform_run_alembic.sh"
RUNTIME_RESTORE_SCRIPT = REPO_ROOT / "platform/tools/platform_release_restore_runtime.sh"
RECOVERY_WRAPPER = REPO_ROOT / "platform/tools/platform_recover_pending.sh"
TRANSACTION_TOOL = REPO_ROOT / "platform/tools/platform_release_transaction.py"
SYSTEMD_STATE_TOOL = REPO_ROOT / "platform/tools/platform_release_systemd_state.py"
STATE_NAME = ".release-operation.json"


class PlatformReleaseRecoveryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._release_lock = None
        try:
            self._release_lock = lock_support.create_test_lock("recovery")
            self.release_lock_path = self._release_lock.path
            self.tools_dir = self.root / "tools"
            shutil.copytree(REPO_ROOT / "platform/tools", self.tools_dir)
            self.install_test_lock_helper(self.tools_dir / "platform_release_lock.sh")
            self.app_dir = self.root / "platform-app"
            self.releases = self.app_dir / "releases"
            self.shared = self.app_dir / "shared"
            self.app_dir.mkdir(mode=0o755)
            self.app_dir.chmod(0o755)
            self.releases.mkdir(mode=0o755)
            self.releases.chmod(0o755)
            self.shared.mkdir(mode=0o755)
            self.shared.chmod(0o755)
            (self.shared / ".env.platform").write_text("PLATFORM_TESTING=1\n")
            (self.shared / ".env.platform").chmod(0o600)
        except BaseException:
            if self._release_lock is not None:
                self._release_lock.cleanup()
            self.temp_dir.cleanup()
            raise

    def tearDown(self) -> None:
        try:
            if self._release_lock is not None:
                self._release_lock.cleanup()
        finally:
            self.temp_dir.cleanup()

    def test_protected_activation_core_restore_is_idempotent_and_timer_free(self) -> None:
        state = self.shared / STATE_NAME
        candidate = self.releases / "candidate"
        current = self.releases / "current"
        previous = self.releases / "previous"
        shared = self.shared
        identities = {
            current: {
                "slug": current.name, "dev": 1, "ino": 2,
                "source_sha": transaction.PROTECTED_ACTIVATION_CURRENT_SHA,
            },
            previous: {
                "slug": previous.name, "dev": 1, "ino": 3,
                "source_sha": transaction.PROTECTED_ACTIVATION_PREVIOUS_SHA,
            },
            candidate: {
                "slug": candidate.name, "dev": 1, "ino": 4,
                "source_sha": transaction.PROTECTED_ACTIVATION_CANDIDATE_SHA,
            },
        }
        record: dict[str, object] = {
            "operation": "install",
            "phase": "filesystem-restored-services-pending",
            "candidate_release": candidate,
            "candidate_path": candidate,
            "current_before_path": current,
            "previous_before_path": previous,
            "shared": shared,
            "current_before_identity": {"dev": 1, "ino": 2},
            "previous_before_identity": {"dev": 1, "ino": 3},
            "candidate_identity": {"dev": 1, "ino": 4},
            "systemd_state_before": None,
            "timer_active_before": True,
            "timer_enabled_before": "enabled",
            "service_state_before": {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            "service_enabled_before": {
                "deadlock-api": "enabled",
                "deadlock-worker": "enabled",
                "deadlock-web": "enabled",
            },
        }
        with (
            mock.patch.object(transaction, "_load_record", return_value=record),
            mock.patch.object(
                transaction, "_quarantine_release_identity",
                side_effect=lambda path: identities[path],
            ),
            mock.patch.object(transaction, "_lexists", return_value=False),
            mock.patch.object(transaction, "validate_service_snapshot"),
            mock.patch.object(transaction, "_verify_original_pointers"),
            mock.patch.object(transaction, "_restore_venv"),
            mock.patch.object(transaction, "_systemctl_path", return_value="/usr/bin/systemctl"),
            mock.patch.object(
                transaction, "_read_systemctl_enabled", return_value="enabled"
            ),
            mock.patch.object(
                transaction, "_read_systemctl_state",
                side_effect=lambda _systemctl, unit: {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                }[unit],
            ),
            mock.patch.object(transaction, "_run_systemctl") as run_systemctl,
        ):
            transaction.restore_protected_activation_core_services(
                state, systemctl="/usr/bin/systemctl"
            )
            run_systemctl.assert_not_called()

            identities[candidate] = {**identities[candidate], "source_sha": "0" * 40}
            with self.assertRaises(transaction.TransactionError):
                transaction.restore_protected_activation_core_services(
                    state, systemctl="/usr/bin/systemctl"
                )

        self._assert_protected_activation_cli_reconstructs_serialized_paths()

    def _assert_protected_activation_cli_reconstructs_serialized_paths(self) -> None:
        current = self.add_release("protected-cli-current")
        previous = self.add_release("protected-cli-previous")
        def set_source_sha(release: Path, source_sha: str) -> None:
            metadata = release / "RELEASE.json"
            metadata.chmod(0o644)
            metadata.write_text(
                json.dumps({"source_git_commit": source_sha}) + "\n",
                encoding="ascii",
            )
            metadata.chmod(0o444)

        set_source_sha(current, transaction.PROTECTED_ACTIVATION_CURRENT_SHA)
        set_source_sha(previous, transaction.PROTECTED_ACTIVATION_PREVIOUS_SHA)
        candidate = self.create_wrapper_transaction(
            current,
            previous,
            "protected-cli",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        set_source_sha(candidate, transaction.PROTECTED_ACTIVATION_CANDIDATE_SHA)
        for expected, phase in (
            ("staged", "migration-pending"),
            ("migration-pending", "migration-applied"),
            ("migration-applied", "activation-pending"),
        ):
            self.run_transaction("phase", "--expected", expected, "--phase", phase)
        result = self.run_transaction(
            "validate-protected-activation-recovery",
            "--phase",
            "activation-pending",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        serialized = json.loads((self.shared / STATE_NAME).read_text(encoding="ascii"))
        self.assertIsInstance(serialized["candidate_release"], str)
        loaded = transaction._load_record(self.shared / STATE_NAME)
        self.assertIsInstance(loaded["candidate_path"], Path)
        original_identity_reader = transaction._quarantine_release_identity
        with mock.patch.object(
            transaction,
            "_quarantine_release_identity",
            wraps=original_identity_reader,
        ) as identity_reader:
            transaction._validate_protected_activation_recovery(
                self.shared / STATE_NAME,
                loaded,
                phase="activation-pending",
            )
        self.assertEqual(identity_reader.call_count, 3)
        self.assertTrue(
            all(isinstance(call.args[0], Path) for call in identity_reader.call_args_list)
        )

    def _assert_preauth_parser_shell_metadata_gate(self, wrapper_source: str) -> None:
        gate_start = wrapper_source.index(
            '  safe_env_parser="$original_current/tools/platform_safe_env_exec.py"'
        )
        gate_end = wrapper_source.index(
            '\n  if ! "$APP_DIR/shared/venv/bin/python"', gate_start
        )
        gate = wrapper_source[gate_start:gate_end]
        canonical_parser = REPO_ROOT / "platform/tools/platform_safe_env_exec.py"
        parser_bytes = canonical_parser.read_bytes()
        self.assertEqual(
            hashlib.sha256(parser_bytes).hexdigest(),
            "819ccc9e489c3fb8b14b3cb61eae500c111c5edb663161504b6ca6e02868399d",
        )

        def run_gate(name: str, *, mode: int, symlink: bool = False, mutate: bool = False):
            release = self.root / f"parser-gate-{name}"
            tools = release / "tools"
            tools.mkdir(parents=True)
            parser = tools / "platform_safe_env_exec.py"
            payload = parser_bytes + (b"\n# changed\n" if mutate else b"")
            if symlink:
                target = tools / "trusted-parser-target.py"
                target.write_bytes(payload)
                target.chmod(0o755)
                parser.symlink_to(target.name)
            else:
                parser.write_bytes(payload)
                parser.chmod(mode)
            script = (
                "set -Eeuo pipefail\n"
                "public_status() { return 0; }\n"
                f"original_current={shlex.quote(str(release))}\n"
                f"{gate}\n"
                "printf 'PARSER_GATE_PASSED\\n'\n"
            )
            return subprocess.run(
                ["/usr/bin/bash", "-Eeuo", "pipefail", "-c", script],
                text=True,
                capture_output=True,
                check=False,
            )

        canonical = run_gate("canonical", mode=0o755)
        self.assertEqual(canonical.returncode, 0, canonical.stderr)
        self.assertEqual(canonical.stdout, "PARSER_GATE_PASSED\n")
        self.assertNotIn("PLATFORM_DATABASE_URL", canonical.stdout + canonical.stderr)
        self.assertEqual(run_gate("group-writable", mode=0o775).returncode, 1)
        self.assertEqual(run_gate("symlink", mode=0o755, symlink=True).returncode, 1)
        self.assertEqual(run_gate("bad-digest", mode=0o755, mutate=True).returncode, 1)

    def test_protected_activation_recovery_requires_durable_migration_authorization(self) -> None:
        migration_fixture = self.root / "static-migration-fixture"
        versions = migration_fixture / "alembic" / "versions"
        versions.mkdir(parents=True)
        marker = self.root / "migration-module-executed"
        (versions / "0054_literal.py").write_text(
            "revision = '20261010_0054'\n"
            "down_revision = None\n"
            "depends_on = None\n"
            "branch_labels = None\n"
            "import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        self.assertEqual(
            transaction._protected_static_revision_graph(migration_fixture),
            {"20261010_0054": ()},
        )
        self.assertFalse(marker.exists())
        (versions / "0054_literal.py").write_text(
            "revision = get_revision()\n"
            "down_revision = None\n"
            "depends_on = None\n"
            "branch_labels = None\n",
            encoding="utf-8",
        )
        with self.assertRaises(transaction.TransactionError):
            transaction._protected_static_revision_graph(migration_fixture)

        import types

        preauth_roots = self.root / "preauth-release-roots"
        preauth_roots.mkdir()
        preauth_releases = []
        for name in ("candidate", "original"):
            release_root = preauth_roots / name
            versions = release_root / "alembic" / "versions"
            versions.mkdir(parents=True)
            (versions / "0054_literal.py").write_text(
                "revision = '20261010_0054'\n"
                "down_revision = None\n"
                "depends_on = None\n"
                "branch_labels = None\n",
                encoding="utf-8",
            )
            preauth_releases.append(release_root)
        wrapper_source = RECOVERY_WRAPPER.read_text(encoding="utf-8")
        self._assert_preauth_parser_shell_metadata_gate(wrapper_source)
        preauth_start = wrapper_source.index('"$safe_env_parser" "$api_env_file" <<\'PY\'')
        preauth_body_start = wrapper_source.index("\n", preauth_start) + 1
        preauth_body_end = wrapper_source.index("\nPY\n  then", preauth_body_start)
        preauth_body = wrapper_source[preauth_body_start:preauth_body_end]

        class FakeConnection:
            readonly = False

            class Transaction:
                def __init__(self, outer):
                    self.outer = outer

                async def __aenter__(self):
                    self.outer.readonly = True

                async def __aexit__(self, *_args):
                    return None

            def __init__(self):
                pass

            def transaction(self, *, readonly: bool):
                if not readonly:
                    raise AssertionError("preauth DB transaction must be readonly")
                return self.Transaction(self)

            async def fetchrow(self, _query):
                return {
                    "database_name": "platformdb",
                    "schema_name": "platform",
                    "resolved_oid": 123,
                }

            async def fetch(self, query):
                if "pg_catalog.pg_class" in query:
                    return [{"oid": 123, "schema_name": "platform", "relkind": "r"}]
                return [{"version_num": "20261010_0054"}]

            async def close(self):
                return None

        fake_connection = FakeConnection()
        fake_asyncpg = types.ModuleType("asyncpg")
        fake_sqlalchemy = types.ModuleType("sqlalchemy")
        fake_sqlalchemy_engine = types.ModuleType("sqlalchemy.engine")

        class FakeURL:
            def __init__(self, url: str):
                self.drivername = url.split("://", 1)[0]
                self.database = url.rsplit("/", 1)[-1]

            def set(self, *, drivername: str):
                self.drivername = drivername
                return self

            def render_as_string(self, *, hide_password: bool):
                return "postgresql://fixture:fixture@127.0.0.1/platformdb"

        fake_sqlalchemy_engine.make_url = FakeURL
        fake_sqlalchemy.engine = fake_sqlalchemy_engine

        async def fake_connect(*, dsn, timeout, server_settings):
            self.assertTrue(dsn.startswith("postgresql://"))
            self.assertEqual(timeout, 10)
            self.assertEqual(server_settings["default_transaction_read_only"], "on")
            return fake_connection

        fake_asyncpg.connect = fake_connect
        with tempfile.TemporaryDirectory(prefix="trusted-api-env-", dir="/root") as fixture_dir:
            env_path = Path(fixture_dir) / "api.env"
            env_path.write_text(
                "PLATFORM_DATABASE_URL=postgresql+asyncpg://fixture:fixture@127.0.0.1/platformdb\n",
                encoding="utf-8",
            )
            env_path.chmod(0o640)
            os.chown(env_path, 0, 985)
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "preauth",
                        str(TRANSACTION_TOOL),
                        str(preauth_releases[0]),
                        str(preauth_releases[1]),
                        str(REPO_ROOT / "platform/tools/platform_safe_env_exec.py"),
                        str(env_path),
                    ],
                ),
                mock.patch.dict(
                    sys.modules,
                    {
                        "asyncpg": fake_asyncpg,
                        "sqlalchemy": fake_sqlalchemy,
                        "sqlalchemy.engine": fake_sqlalchemy_engine,
                    },
                ),
                mock.patch.dict(os.environ),
            ):
                os.environ.pop("PLATFORM_DATABASE_URL", None)
                exec(compile(preauth_body, "recovery-preauth-heredoc", "exec"), {"__name__": "__main__"})
            self.assertTrue(fake_connection.readonly)

        current = self.add_release("protected-auth-current")
        previous = self.add_release("protected-auth-previous")

        def set_source_sha(release: Path, source_sha: str) -> None:
            metadata = release / "RELEASE.json"
            metadata.chmod(0o644)
            metadata.write_text(
                json.dumps({"source_git_commit": source_sha}) + "\n",
                encoding="ascii",
            )
            metadata.chmod(0o444)

        set_source_sha(current, transaction.PROTECTED_ACTIVATION_CURRENT_SHA)
        set_source_sha(previous, transaction.PROTECTED_ACTIVATION_PREVIOUS_SHA)
        candidate = self.create_wrapper_transaction(
            current,
            previous,
            "protected-auth",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        set_source_sha(candidate, transaction.PROTECTED_ACTIVATION_CANDIDATE_SHA)
        for expected, phase in (
            ("staged", "migration-pending"),
            ("migration-pending", "migration-applied"),
            ("migration-applied", "activation-pending"),
        ):
            self.run_transaction("phase", "--expected", expected, "--phase", phase)
        (self.app_dir / "current").symlink_to(candidate)
        (self.app_dir / "previous").symlink_to(current)
        state = self.shared / STATE_NAME
        before_state = state.read_bytes()
        before_pointers = (
            os.readlink(self.app_dir / "current"),
            os.readlink(self.app_dir / "previous"),
        )
        command = [
            "recover",
            "--retain",
            "--service-pending",
            "--state",
            str(state),
        ]

        # The real CLI/main path must refuse an uncertain migration before the
        # first pointer mutation, while preserving the serialized record.
        with mock.patch.object(sys, "argv", [str(TRANSACTION_TOOL), *command]):
            refused = transaction.main()
        self.assertEqual(refused, 2)
        self.assertEqual(state.read_bytes(), before_state)
        self.assertEqual(
            (os.readlink(self.app_dir / "current"), os.readlink(self.app_dir / "previous")),
            before_pointers,
        )

        self.run_transaction(
            "authorize-recovery", "--confirm", "MIGRATION_NOT_REVERSED"
        )
        self.assertEqual(self.state_phase(), "recovery-authorized")
        authorized_state = state.read_bytes()

        # A source identity mismatch remains a refusal even after the durable
        # operator decision; it cannot reach the pointer mutation barrier.
        set_source_sha(candidate, "0" * 40)
        with mock.patch.object(sys, "argv", [str(TRANSACTION_TOOL), *command]):
            wrong_tuple = transaction.main()
        self.assertEqual(wrong_tuple, 2)
        self.assertEqual(state.read_bytes(), authorized_state)
        self.assertEqual(
            (os.readlink(self.app_dir / "current"), os.readlink(self.app_dir / "previous")),
            before_pointers,
        )
        set_source_sha(candidate, transaction.PROTECTED_ACTIVATION_CANDIDATE_SHA)

        # Block the first actual pointer write and verify the genuine CLI
        # reaches it only for the exact authorized serialized transaction.
        with (
            mock.patch.object(sys, "argv", [str(TRANSACTION_TOOL), *command]),
            mock.patch.object(
                transaction,
                "_restore_pointers",
                side_effect=transaction.TransactionError("first-write barrier"),
            ) as first_write,
        ):
            barrier_result = transaction.main()
        self.assertEqual(barrier_result, 2)
        first_write.assert_called_once()
        self.assertEqual(self.state_phase(), "recovery-authorized")
        self.assertEqual(
            (os.readlink(self.app_dir / "current"), os.readlink(self.app_dir / "previous")),
            before_pointers,
        )

        # Retry from the same durable phase performs the protected rewind and
        # retains the transaction at the service-pending boundary.
        self.run_transaction(*command)
        self.assertEqual(self.state_phase(), "filesystem-restored-services-pending")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_health_monitor_wait_requires_fresh_success_after_restore(self) -> None:
        timer_properties = systemd_state._parse_show_fields(
            "TimersMonotonic={ OnUnitActiveUSec=5min ; next_elapse=2month 23min 5.751439s }\n"
            "TimersMonotonic={ OnBootUSec=5min ; next_elapse=5min }",
            ("TimersMonotonic",),
        )
        self.assertTrue(
            systemd_state._health_timer_schedule_is_fixed(
                timer_properties["TimersMonotonic"]
            )
        )
        self.assertFalse(
            systemd_state._health_timer_schedule_is_fixed(
                "{ OnCalendar=hourly ; next_elapse=5min }"
            )
        )
        self.assertEqual(
            systemd_state._systemd_timespan_usec("2month 18min 5.743733s"),
            5_259_600_000_000 + 1_080_000_000 + 5_743_733,
        )
        self.assertEqual(
            systemd_state._systemd_timespan_usec("1y 1s"),
            31_557_600_000_000 + 1_000_000,
        )
        self.assertEqual(
            systemd_state._systemd_timespan_usec(
                "1y 1month 1w 1d 1h 1min 1s 1ms 1us"
            ),
            31_557_600_000_000 + 2_629_800_000_000 + 604_800_000_000
            + 86_400_000_000 + 3_600_000_000 + 60_000_000 + 1_000_000
            + 1_000 + 1,
        )
        with self.assertRaises(systemd_state.StateError):
            systemd_state._systemd_timespan_usec("1year")
        with self.assertRaises(systemd_state.StateError):
            systemd_state._systemd_timespan_usec("1s 1min")
        with self.assertRaises(systemd_state.StateError):
            systemd_state._systemd_timespan_usec("1.0000001s")
        wrapper = RECOVERY_WRAPPER.read_text(encoding="utf-8")
        route_start = wrapper.index('if [[ "$pending_operation" == install')
        self.assertIn('namespace["_protected_static_revision_graph"]', wrapper[route_start:])
        self.assertNotIn("walk_revisions", wrapper[route_start:])
        self.assertIn('api_env_file="/opt/oldsparky/platform/shared/env/api.env"', wrapper[route_start:])
        self.assertIn(
            "819ccc9e489c3fb8b14b3cb61eae500c111c5edb663161504b6ca6e02868399d",
            wrapper[route_start:],
        )
        self.assertIn('env_before.st_gid != 985', wrapper[route_start:])
        self.assertNotIn('os.environ.get("PLATFORM_DATABASE_URL"', wrapper[route_start:])
        migration_head_at = wrapper.index("async def read_current()", route_start)
        archive_attestation_at = wrapper.index('proof["_validate_receipt"]', route_start)
        authorization_at = wrapper.index('authorize-recovery \\', route_start)
        rewind_at = wrapper.index('recover \\\n      --retain --service-pending', authorization_at)
        proof_at = wrapper.index("verify = namespace.get(\"verify_active_release_artifact\")", route_start)
        core_restore_at = wrapper.index(
            "restore-protected-activation-core-services", route_start
        )
        wait_at = wrapper.index("wait-health-monitor-success", route_start)
        capture_at = wrapper.index('capture-transaction \\', route_start)
        self.assertLess(migration_head_at, authorization_at)
        self.assertLess(archive_attestation_at, authorization_at)
        self.assertLess(authorization_at, rewind_at)
        self.assertLess(rewind_at, proof_at)
        self.assertLess(proof_at, core_restore_at)
        self.assertLess(core_restore_at, wait_at)
        self.assertLess(wait_at, capture_at)
        self.assertNotIn("restore-services \\", wrapper[route_start:capture_at])

        baseline = {
            "schema": 1,
            "invocation_id": "a" * 32,
            "last_trigger_usec_monotonic": 100,
        }
        service = {
            "InvocationID": "b" * 32,
            "ExecMainStartTimestampMonotonic": "10000001",
            "ExecMainExitTimestampMonotonic": "10000002",
            "Result": "success",
            "ExecMainCode": "1",
            "ExecMainStatus": "0",
            "MainPID": "0",
            "ControlPID": "0",
            "ActiveState": "inactive",
        }
        timer = {
            "LastTriggerUSecMonotonic": "10.000003s",
            "ActiveState": "active",
            "SubState": "waiting",
        }
        with (
            mock.patch.object(systemd_state, "_fixed_jobs", return_value=[]),
            mock.patch.object(
                systemd_state, "_health_monitor_identity", return_value=(service, timer)
            ),
            mock.patch.object(systemd_state.time, "clock_gettime", return_value=10.0),
            mock.patch.object(systemd_state.time, "monotonic", side_effect=[0.0, 0.0]),
        ):
            systemd_state._wait_health_monitor_success(
                json.dumps(baseline), timeout=450
            )

        stale = {**service, "InvocationID": baseline["invocation_id"]}
        with (
            mock.patch.object(systemd_state, "_fixed_jobs", return_value=[]),
            mock.patch.object(
                systemd_state, "_health_monitor_identity", return_value=(stale, timer)
            ),
            mock.patch.object(systemd_state.time, "clock_gettime", return_value=10.0),
            mock.patch.object(systemd_state.time, "monotonic", side_effect=[0.0, 451.0]),
            mock.patch.object(systemd_state.time, "sleep"),
            self.assertRaises(systemd_state.StateError),
        ):
            systemd_state._wait_health_monitor_success(
                json.dumps(baseline), timeout=450
            )

    def test_cache_purge_runtime_uses_transaction_bound_pre_activation_source(self) -> None:
        current = self.add_release("purge-source-current")
        older = self.add_release("purge-target-older")
        shared = self.shared / "venv"
        peer = self.shared / ".venv-peer"
        snapshot = self.shared / ".venv-snapshot"
        for path in (shared, peer, snapshot):
            path.mkdir(mode=0o755)
            path.chmod(0o755)
            (path / "bin").mkdir(mode=0o755)
            (path / "bin").chmod(0o755)
            (path / "bin/python").symlink_to("/usr/bin/python3.12")

        rollback_record: dict[str, object] = {
            "operation": "rollback",
            "transition": "exchange",
            "candidate_path": older,
            "current_before_path": current,
            "shared_before": transaction._identity(snapshot.lstat()),
            "peer_before": transaction._identity(shared.lstat()),
            "shared_venv_path": shared,
            "peer_path": peer,
            "snapshot_path": snapshot,
        }
        rollback_source, rollback_python = transaction._cache_purge_runtime(
            rollback_record
        )
        self.assertEqual(rollback_source, current)
        self.assertEqual(rollback_python, snapshot / "bin/python")
        self.assertNotEqual(rollback_source, older)

        install_record: dict[str, object] = {
            "operation": "install",
            "transition": "exchange",
            "candidate_path": older,
            "current_before_path": current,
            "shared_before": transaction._identity(shared.lstat()),
            "peer_before": transaction._identity(peer.lstat()),
            "shared_venv_path": shared,
            "peer_path": peer,
            "snapshot_path": snapshot,
        }
        install_source, install_python = transaction._cache_purge_runtime(
            install_record
        )
        self.assertEqual(install_source, older)
        self.assertEqual(install_python, peer / "bin/python")

        rollback_record["current_before_path"] = None
        with self.assertRaisesRegex(transaction.TransactionError, "source is unavailable"):
            transaction._cache_purge_runtime(rollback_record)

    def test_immutable_recovery_wrapper_first_install_avoids_systemd_and_current_helpers(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        retention_helper = (generation / "platform_release_retention.py").lstat()
        self.assertEqual(stat.S_IMODE(retention_helper.st_mode), recovery.DATA_MODE)
        candidate = self.create_wrapper_transaction(None, None, "first-install")
        systemctl, systemctl_log = self.write_failing_systemctl("first-install")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertFalse((self.app_dir / "current").exists())
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertFalse(candidate.exists())

    def test_immutable_recovery_wrapper_first_install_restores_transaction_systemd_snapshot(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(
            None,
            None,
            "first-install-complete-snapshot",
            complete_snapshot=True,
        )
        systemctl = self.write_initial_systemctl()
        self.run_transaction("capture-initial-systemd", "--systemctl", str(systemctl))

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        enabled = json.loads((self.root / "systemd-enabled.json").read_text())
        state = json.loads((self.root / "systemd-state.json").read_text())
        for unit in (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        ):
            self.assertEqual(enabled[unit], "disabled", unit)
        for unit in (
            "deadlock-api",
            "deadlock-worker",
            "deadlock-web",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        ):
            self.assertEqual(state[unit], "inactive", unit)

    def test_pending_recovery_first_install_restores_before_cleanup(self) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(
            None,
            None,
            "pending-first-install-systemd",
            complete_snapshot=True,
        )
        systemctl = self.write_initial_systemctl()
        self.run_transaction("capture-initial-systemd", "--systemctl", str(systemctl))
        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        log = (self.root / "systemctl.log").read_text(encoding="utf-8")
        self.assertIn("stop deadlock-api.service", log)
        self.assertIn("disable deadlock-health-monitor.timer", log)
        self.assertNotIn("offsite", log)

    def test_operationless_first_install_quiesce_v1_v2_is_systemd_free(self) -> None:
        """Pre-promotion first-install receipts are validated as no-op cleanup."""

        for version in (2, 1):
            with self.subTest(version=version):
                if version == 1:
                    self.tearDown()
                    self.setUp()
                generation = self.install_recovery_generation()
                candidate = self.releases / f"quiesce-first-install-v{version}"
                self.run_transaction(
                    "prepare-quiesce",
                    "--app-dir",
                    str(self.app_dir),
                    "--candidate-release",
                    str(candidate),
                    "--service-state",
                    "deadlock-api=inactive",
                    "--service-state",
                    "deadlock-worker=inactive",
                    "--service-state",
                    "deadlock-web=inactive",
                    "--timer-active-before",
                    "inactive",
                    "--service-enabled",
                    "deadlock-api=disabled",
                    "--service-enabled",
                    "deadlock-worker=disabled",
                    "--service-enabled",
                    "deadlock-web=disabled",
                    "--timer-enabled-before",
                    "disabled",
                )
                receipt = self.shared / STATE_NAME
                if version == 1:
                    record = json.loads(receipt.read_text(encoding="ascii"))
                    record["version"] = 1
                    record.pop("service_enabled_before")
                    record.pop("timer_enabled_before")
                    receipt.write_text(
                        json.dumps(record, sort_keys=True) + "\n", encoding="ascii"
                    )
                    receipt.chmod(0o600)
                systemctl, systemctl_log = self.write_failing_systemctl(
                    f"quiesce-first-install-v{version}"
                )
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(systemctl_log.exists())
                self.assertFalse(receipt.exists())
                self.assertFalse(candidate.exists())

    def test_operationless_first_install_quiesce_noninactive_fails_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.releases / "quiesce-first-install-active"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
            "--service-enabled",
            "deadlock-api=disabled",
            "--service-enabled",
            "deadlock-worker=disabled",
            "--service-enabled",
            "deadlock-web=disabled",
            "--timer-enabled-before",
            "disabled",
        )
        systemctl, systemctl_log = self.write_failing_systemctl(
            "quiesce-first-install-active"
        )
        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_immutable_recovery_wrapper_current_only_avoids_systemd_and_tampered_helpers(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("current-only-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_current_control_helper_bombs(current)
        self.prepare_wrapper_cache_purge_venv("current-only")
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "current-only",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        # Current-only recovery crosses the old-service restart boundary, so
        # the install candidate is the transaction-bound v1 purge source.
        self.prepare_wrapper_cache_purge(candidate)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertFalse(candidate.exists())
        self.assertFalse((self.root / "current-helper-used").exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text()),
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
            },
        )

    def test_immutable_recovery_wrapper_current_only_accepts_second_pointer_window(
        self,
    ) -> None:
        """A kill after current moves but before its phase marker is recoverable."""

        generation = self.install_recovery_generation()
        current = self.add_release("current-only-pointer-window-current")
        (self.app_dir / "current").symlink_to(current)
        self.prepare_wrapper_cache_purge_venv("current-only-pointer-window")
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "current-only-pointer-window",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        self.prepare_wrapper_cache_purge(candidate)
        # Reproduce the exact durable window: previous has moved, current has
        # moved, but the transaction still carries previous-switched.
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-applied"
        )
        self.run_transaction(
            "phase", "--expected", "migration-applied", "--phase", "previous-switched"
        )
        (self.app_dir / "current").unlink()
        (self.app_dir / "current").symlink_to(candidate)
        (self.app_dir / "previous").symlink_to(current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text()),
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
            },
        )

    def test_immutable_recovery_wrapper_first_install_after_current_switch_is_systemd_free(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(
            None,
            None,
            "first-install-pointer-window",
            complete_snapshot=True,
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-applied"
        )
        self.run_transaction(
            "phase", "--expected", "migration-applied", "--phase", "current-switched"
        )
        (self.app_dir / "current").symlink_to(candidate)
        systemctl, systemctl_log = self.write_failing_systemctl(
            "first-install-pointer-window"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertFalse((self.app_dir / "current").exists())
        self.assertFalse((self.app_dir / "previous").exists())

    def test_immutable_recovery_wrapper_current_only_rejects_missing_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("current-only-missing-snapshot-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current, None, "current-only-missing-snapshot"
        )
        systemctl, systemctl_log = self.write_failing_systemctl(
            "current-only-missing-snapshot"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())
        self.assertFalse(systemctl_log.exists())

    def test_immutable_recovery_wrapper_current_only_rejects_partial_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("current-only-partial-snapshot-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "current-only-partial-snapshot",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        state = self.shared / STATE_NAME
        record = json.loads(state.read_text(encoding="ascii"))
        del record["service_state_before"]["deadlock-web"]
        state.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
        state.chmod(0o600)
        systemctl, systemctl_log = self.write_failing_systemctl(
            "current-only-partial-snapshot"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state.exists())
        self.assertTrue(candidate.exists())
        self.assertFalse(systemctl_log.exists())

    def test_immutable_recovery_wrapper_current_only_replays_after_each_partial_service_restore(
        self,
    ) -> None:
        cases = (
            ("restart deadlock-api", "deadlock-api", "active"),
            ("stop deadlock-worker", "deadlock-worker", "inactive"),
            ("restart deadlock-web", "deadlock-web", "active"),
            (
                "start deadlock-cloudflare-ips.timer",
                "deadlock-cloudflare-ips.timer",
                "active",
            ),
        )
        for index, (kill_after, unit, expected_after_kill) in enumerate(cases):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(kill_after=kill_after):
                generation = self.install_recovery_generation()
                current = self.add_release(f"current-only-retry-{index}-current")
                (self.app_dir / "current").symlink_to(current)
                self.prepare_wrapper_cache_purge_venv(
                    f"current-only-retry-{index}"
                )
                candidate = self.create_wrapper_transaction(
                    current,
                    None,
                    f"current-only-retry-{index}",
                    complete_snapshot=True,
                    service_states={
                        "deadlock-api": "active",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "active",
                    },
                    timer_active=True,
                )
                self.prepare_wrapper_cache_purge(candidate)
                initial_state = {
                    "deadlock-api": "inactive",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "inactive",
                    "deadlock-cloudflare-ips.timer": "inactive",
                }
                systemctl = self.write_stateful_systemctl(initial_state)
                first = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    env={"PLATFORM_TEST_SYSTEMCTL_KILL_AFTER": kill_after},
                    check=False,
                )
                self.assertNotEqual(first.returncode, 0)
                self.assertTrue((self.shared / STATE_NAME).exists())
                self.assertFalse((self.shared / ".release-systemd-state.json").exists())
                self.assertTrue(candidate.exists())
                self.assertEqual(
                    self.state_phase(), "filesystem-restored-services-pending"
                )
                partial_state = json.loads(
                    (self.root / "systemd-state.json").read_text()
                )
                self.assertEqual(partial_state[unit], expected_after_kill)
                self.assertTrue(
                    (self.shared / STATE_NAME).exists(),
                    "receipt must survive a crash before service restoration completes",
                )

                retry = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse((self.shared / ".release-systemd-state.json").exists())
                self.assertFalse(candidate.exists())
                self.assertEqual(
                    json.loads((self.root / "systemd-state.json").read_text()),
                    {
                        "deadlock-api": "active",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "active",
                        "deadlock-cloudflare-ips.timer": "active",
                    },
                )

    def test_immutable_recovery_wrapper_recovers_operationless_quiesce_receipts(
        self,
    ) -> None:
        """A SIGKILL before promotion restores the exact immutable snapshot."""

        for index, topology in enumerate(("current-only", "upgrade")):
            if index:
                self.tearDown()
                self.setUp()
            generation = self.install_recovery_generation()
            current = self.add_release(f"quiesce-{topology}-current")
            (self.app_dir / "current").symlink_to(current)
            previous = None
            if topology == "upgrade":
                previous = self.add_release("quiesce-upgrade-previous")
                (self.app_dir / "previous").symlink_to(previous)
            self.add_current_control_helper_bombs(current)
            systemctl = self.write_stateful_systemctl(
                {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                    "deadlock-cloudflare-ips.timer": "active",
                    "deadlock-cloudflare-ips.service": "inactive",
                }
            )
            artifact = self.root / f"quiesce-{topology}.tar.gz"
            artifact.write_bytes(b"not reached after the quiesce receipt")
            interrupted = self.copy_initial_deploy_with_fault(
                f"quiesce-{topology}-kill.sh",
                systemctl,
                "  quiesce_runtime_writers\n",
                '  quiesce_runtime_writers\n  /bin/kill -KILL "$$"\n',
            )
            result = self.run_script(
                interrupted,
                "--artifact",
                str(artifact),
                "--app-dir",
                str(self.app_dir),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            receipt = json.loads((self.shared / STATE_NAME).read_text())
            self.assertEqual(receipt["phase"], "quiesce-pending")
            self.assertNotIn("operation_id", receipt)
            self.assertEqual(
                receipt["service_state_before"],
                {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                },
            )
            self.assertEqual(receipt["timer_active_before"], True)
            self.assertFalse((self.shared / ".release-systemd-state.json").exists())
            self.assertEqual(
                json.loads((self.root / "systemd-state.json").read_text())[
                    "deadlock-api"
                ],
                "inactive",
            )
            (self.root / "systemctl.log").write_text("")

            result = self.run_script(
                generation / RECOVERY_WRAPPER.name,
                "--app-dir",
                str(self.app_dir),
                "--systemctl",
                str(systemctl),
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.shared / STATE_NAME).exists())
            self.assertFalse((self.shared / ".release-quiesce.json").exists())
            self.assertEqual(
                json.loads((self.root / "systemd-state.json").read_text()),
                {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                    "deadlock-cloudflare-ips.timer": "active",
                    "deadlock-cloudflare-ips.service": "inactive",
                },
            )
            self.assertFalse((self.root / "current-helper-used").exists())

    def test_immutable_recovery_wrapper_rejects_partial_quiesce_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("quiesce-partial-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "quiesce-partial-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        receipt_path = self.shared / STATE_NAME
        receipt = json.loads(receipt_path.read_text())
        receipt["service_state_before"].pop("deadlock-web")
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        systemctl, systemctl_log = self.write_failing_systemctl("quiesce-partial")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(receipt_path.exists())
        self.assertFalse(systemctl_log.exists())

    def test_quiesce_v1_active_only_receipt_restores_without_enablement(self) -> None:
        current = self.add_release("quiesce-v1-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "quiesce-v1-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        receipt = self.shared / STATE_NAME
        record = json.loads(receipt.read_text(encoding="ascii"))
        record["version"] = 1
        record.pop("service_enabled_before")
        record.pop("timer_enabled_before")
        receipt.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
        receipt.chmod(0o600)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "active",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )
        self.run_transaction("verify-quiesce")
        self.run_transaction("restore-quiesce", "--systemctl", str(systemctl))
        self.run_transaction("abort-quiesce")
        self.assertFalse(receipt.exists())
        log = (self.root / "systemctl.log").read_text(encoding="ascii")
        self.assertNotIn("enable ", log)
        self.assertNotIn("disable ", log)

    def test_quiesce_v1_v2_hybrids_are_rejected(self) -> None:
        for version, remove_enabled in ((1, False), (2, True)):
            with self.subTest(version=version):
                current = self.add_release(f"quiesce-hybrid-{version}-current")
                (self.app_dir / "current").unlink(missing_ok=True)
                (self.app_dir / "current").symlink_to(current)
                candidate = self.releases / f"quiesce-hybrid-{version}-candidate"
                self.run_transaction(
                    "prepare-quiesce",
                    "--app-dir",
                    str(self.app_dir),
                    "--candidate-release",
                    str(candidate),
                    "--service-state",
                    "deadlock-api=inactive",
                    "--service-state",
                    "deadlock-worker=inactive",
                    "--service-state",
                    "deadlock-web=inactive",
                    "--timer-active-before",
                    "inactive",
                )
                receipt = self.shared / STATE_NAME
                malformed = json.loads(receipt.read_text(encoding="ascii"))
                malformed["version"] = version
                if remove_enabled:
                    malformed.pop("service_enabled_before")
                    malformed.pop("timer_enabled_before")
                receipt.write_text(
                    json.dumps(malformed, sort_keys=True) + "\n", encoding="ascii"
                )
                receipt.chmod(0o600)
                invalid = self.run_script(
                    TRANSACTION_TOOL,
                    "verify-quiesce",
                    "--state",
                    str(receipt),
                    check=False,
                )
                self.assertNotEqual(
                    invalid.returncode,
                    0,
                )
                receipt.unlink(missing_ok=True)

    def test_immutable_recovery_wrapper_rejects_unbound_quiesce_receipts_before_systemd(
        self,
    ) -> None:
        for index, mutation in enumerate(("phase", "extra", "candidate")):
            if index:
                self.tearDown()
                self.setUp()
            generation = self.install_recovery_generation()
            current = self.add_release(f"quiesce-unbound-{mutation}-current")
            (self.app_dir / "current").symlink_to(current)
            candidate = self.releases / f"quiesce-unbound-{mutation}-candidate"
            self.run_transaction(
                "prepare-quiesce",
                "--app-dir",
                str(self.app_dir),
                "--candidate-release",
                str(candidate),
                "--service-state",
                "deadlock-api=active",
                "--service-state",
                "deadlock-worker=inactive",
                "--service-state",
                "deadlock-web=active",
                "--timer-active-before",
                "active",
            )
            receipt_path = self.shared / STATE_NAME
            receipt = json.loads(receipt_path.read_text())
            if mutation == "phase":
                receipt["phase"] = "prepared"
            elif mutation == "extra":
                receipt["unexpected"] = True
            else:
                candidate.mkdir()
                (candidate / "unbound.txt").write_text("not receipt-bound")
            receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
            systemctl, systemctl_log = self.write_failing_systemctl(
                f"quiesce-unbound-{mutation}"
            )

            result = self.run_script(
                generation / RECOVERY_WRAPPER.name,
                "--app-dir",
                str(self.app_dir),
                "--systemctl",
                str(systemctl),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, mutation)
            self.assertTrue(receipt_path.exists(), mutation)
            self.assertFalse(systemctl_log.exists(), mutation)

    def test_abort_quiesce_low_level_candidate_guard_is_fail_closed(self) -> None:
        for index, mutation in enumerate(("occupied", "symlink", "hardlink", "special")):
            if index:
                self.tearDown()
                self.setUp()
            current = self.add_release(f"abort-candidate-{mutation}-current")
            (self.app_dir / "current").symlink_to(current)
            candidate = self.releases / f"abort-candidate-{mutation}"
            self.run_transaction(
                "prepare-quiesce",
                "--app-dir",
                str(self.app_dir),
                "--candidate-release",
                str(candidate),
                "--service-state",
                "deadlock-api=active",
                "--service-state",
                "deadlock-worker=inactive",
                "--service-state",
                "deadlock-web=active",
                "--timer-active-before",
                "inactive",
            )
            if mutation == "occupied":
                candidate.mkdir()
                (candidate / "unbound.txt").write_text("not receipt-bound")
            elif mutation == "symlink":
                replacement = self.releases / f"{mutation}-replacement"
                replacement.mkdir()
                candidate.symlink_to(replacement, target_is_directory=True)
            elif mutation == "hardlink":
                source = self.root / "candidate-hardlink-source"
                source.write_text("not a directory")
                candidate.hardlink_to(source)
            else:
                os.mkfifo(candidate)

            result = self.run_script(
                TRANSACTION_TOOL,
                "abort-quiesce",
                "--state",
                str(self.shared / STATE_NAME),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, mutation)
            self.assertTrue((self.shared / STATE_NAME).exists(), mutation)
            self.assertTrue(os.path.lexists(candidate), mutation)
            if mutation == "occupied":
                self.assertTrue((candidate / "unbound.txt").exists())

    def test_quarantine_populated_quiesce_preserves_exact_tree_until_snapshot_restore(self) -> None:
        source_sha = "a" * 40
        failed_run_id = "123456"
        failed_attempt = "2"
        recovery_run_id = "234567"
        recovery_attempt = "1"
        bundle_sha = "b" * 64
        candidate_slug = f"gha-{failed_run_id}-{failed_attempt}-{source_sha[:12]}"
        current = self.add_release("quarantine-current")
        previous = self.add_release("quarantine-previous")
        candidate = self.releases / candidate_slug
        candidate.mkdir(mode=0o755)
        (candidate / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha}) + "\n", encoding="ascii"
        )
        (candidate / "RELEASE.json").chmod(0o444)
        payload = candidate / "nested" / "evidence.txt"
        payload.parent.mkdir(mode=0o755)
        payload.write_bytes(b"preserve the populated untrusted candidate\n")
        payload.chmod(0o444)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--candidate-may-exist",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        original_pending = (self.shared / STATE_NAME).read_bytes()
        original_candidate_inode = candidate.stat().st_ino
        original_payload = payload.read_bytes()
        quarantine_root = self.shared / transaction.QUARANTINE_DIR_NAME
        receipt_path = quarantine_root / f"{candidate_slug}.json"
        quarantine_path = quarantine_root / candidate_slug
        command_args = (
            "--failed-deploy-run-id",
            failed_run_id,
            "--failed-deploy-run-attempt",
            failed_attempt,
            "--failed-deploy-source-sha",
            source_sha,
            "--recovery-run-id",
            recovery_run_id,
            "--recovery-run-attempt",
            recovery_attempt,
            "--bundle-sha",
            bundle_sha,
        )

        quarantined = self.run_transaction(
            "quarantine-populated-quiesce", *command_args
        )
        self.assertEqual(json.loads(quarantined.stdout)["phase"], "quarantined")
        receipt = json.loads(receipt_path.read_text(encoding="ascii"))
        self.assertEqual(set(receipt), transaction.QUARANTINE_RECEIPT_KEYS)
        self.assertEqual(receipt["phase"], "quarantined")
        self.assertEqual(
            receipt["service_state_before"],
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
        )
        self.assertEqual(
            receipt["service_enabled_before"],
            {
                "deadlock-api": "enabled",
                "deadlock-worker": "enabled",
                "deadlock-web": "enabled",
            },
        )
        self.assertIs(receipt["timer_active_before"], True)
        self.assertEqual(receipt["timer_enabled_before"], "disabled")
        self.assertEqual(
            receipt["pending_sha256"], hashlib.sha256(original_pending).hexdigest()
        )
        self.assertEqual(receipt["failed_deploy_run_id"], failed_run_id)
        self.assertEqual(receipt["failed_deploy_run_attempt"], failed_attempt)
        self.assertEqual(receipt["failed_deploy_source_sha"], source_sha)
        self.assertEqual(receipt["recovery_run_id"], recovery_run_id)
        self.assertEqual(receipt["recovery_run_attempt"], recovery_attempt)
        self.assertFalse(candidate.exists())
        self.assertEqual(quarantine_path.stat().st_ino, original_candidate_inode)
        self.assertEqual((quarantine_path / "nested/evidence.txt").read_bytes(), original_payload)

        # JSON booleans are integers in Python; the receipt validator must
        # reject that ambiguity before changing the retained evidence.
        malformed_receipt = dict(receipt)
        malformed_receipt["candidate_file_count"] = True
        receipt_path.write_text(
            json.dumps(malformed_receipt, sort_keys=True, separators=(",", ":")),
            encoding="ascii",
        )
        malformed = self.run_transaction(
            "inspect-quarantined-quiesce", *command_args, check=False
        )
        self.assertNotEqual(malformed.returncode, 0)
        self.assertEqual((self.shared / STATE_NAME).read_bytes(), original_pending)
        self.assertEqual(quarantine_path.stat().st_ino, original_candidate_inode)
        receipt_path.write_bytes(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("ascii")
        )
        receipt_path.chmod(0o600)

        # A repeated exact recovery binding resumes safely. A different
        # failed attempt cannot claim or move the preserved candidate.
        replay = self.run_transaction("quarantine-populated-quiesce", *command_args)
        self.assertEqual(json.loads(replay.stdout)["phase"], "quarantined")
        wrong_binding = list(command_args)
        wrong_binding[3] = "3"
        mismatch = self.run_transaction(
            "quarantine-populated-quiesce", *wrong_binding, check=False
        )
        self.assertNotEqual(mismatch.returncode, 0)
        self.assertEqual(quarantine_path.stat().st_ino, original_candidate_inode)
        advanced_binding = list(command_args)
        advanced_binding[9] = "2"
        advanced = self.run_transaction(
            "quarantine-populated-quiesce", *advanced_binding
        )
        self.assertEqual(json.loads(advanced.stdout)["phase"], "quarantined")
        self.assertEqual(
            json.loads(receipt_path.read_text(encoding="ascii"))["recovery_run_attempt"],
            "2",
        )
        wrong_recovery_run = list(advanced_binding)
        wrong_recovery_run[7] = "345678"
        rejected_recovery = self.run_transaction(
            "quarantine-populated-quiesce", *wrong_recovery_run, check=False
        )
        self.assertNotEqual(rejected_recovery.returncode, 0)
        stale_recovery_attempt = list(command_args)
        rejected_stale_attempt = self.run_transaction(
            "quarantine-populated-quiesce", *stale_recovery_attempt, check=False
        )
        self.assertNotEqual(rejected_stale_attempt.returncode, 0)
        command_args = tuple(advanced_binding)
        quarantined_payload = quarantine_path / "nested/evidence.txt"
        quarantined_payload.chmod(0o644)
        quarantined_payload.write_bytes(b"changed after quarantine\n")
        quarantined_payload.chmod(0o444)
        changed_tree = self.run_transaction(
            "quarantine-populated-quiesce", *command_args, check=False
        )
        self.assertNotEqual(changed_tree.returncode, 0)
        quarantined_payload.chmod(0o644)
        quarantined_payload.write_bytes(original_payload)
        quarantined_payload.chmod(0o444)

        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )
        self.run_transaction("restore-quiesce", "--systemctl", str(systemctl))
        restored = self.run_transaction(
            "mark-quarantine-services-restored", *command_args
        )
        self.assertEqual(json.loads(restored.stdout)["phase"], "services_restored")
        pending_state = self.shared / STATE_NAME
        pending_state.write_bytes(original_pending + b" ")
        changed_pending = self.run_transaction(
            "consume-quarantined-quiesce", *command_args, check=False
        )
        self.assertNotEqual(changed_pending.returncode, 0)
        self.assertTrue(pending_state.is_file())
        pending_state.write_bytes(original_pending)
        pending_state.chmod(0o600)
        consumed = self.run_transaction("consume-quarantined-quiesce", *command_args)
        self.assertEqual(json.loads(consumed.stdout)["phase"], "receipt_consumed")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text(encoding="ascii")),
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
            },
        )
        self.assertEqual((quarantine_path / "nested/evidence.txt").read_bytes(), original_payload)
        self.assertEqual(
            json.loads(receipt_path.read_text(encoding="ascii"))["phase"],
            "receipt_consumed",
        )
        inspected = self.run_transaction(
            "inspect-quarantined-quiesce", *command_args
        )
        inspection = json.loads(inspected.stdout)
        self.assertEqual(inspection["phase"], "receipt_consumed")
        self.assertEqual(inspection["api"], "active")
        self.assertEqual(inspection["web"], "active")

    def test_quarantine_populated_quiesce_rejects_collision_and_pointer_change(self) -> None:
        source_sha = "c" * 40
        candidate_slug = f"gha-345678-1-{source_sha[:12]}"
        current = self.add_release("quarantine-negative-current")
        candidate = self.releases / candidate_slug
        candidate.mkdir(mode=0o755)
        (candidate / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha}) + "\n", encoding="ascii"
        )
        (candidate / "RELEASE.json").chmod(0o444)
        evidence = candidate / "payload.bin"
        evidence.write_bytes(b"retain")
        evidence.chmod(0o444)
        (self.app_dir / "current").symlink_to(current)
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--candidate-may-exist",
            "--service-state",
            "deadlock-api=inactive",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
        )
        command_args = (
            "--failed-deploy-run-id",
            "345678",
            "--failed-deploy-run-attempt",
            "1",
            "--failed-deploy-source-sha",
            source_sha,
            "--recovery-run-id",
            "456789",
            "--recovery-run-attempt",
            "1",
            "--bundle-sha",
            "d" * 64,
        )
        quarantine_root = self.shared / transaction.QUARANTINE_DIR_NAME
        release_record = candidate / "RELEASE.json"
        release_record.chmod(0o644)
        release_record.write_text(
            json.dumps({"source_git_commit": "e" * 40}) + "\n",
            encoding="ascii",
        )
        release_record.chmod(0o444)
        source_mismatch = self.run_transaction(
            "quarantine-populated-quiesce", *command_args, check=False
        )
        self.assertNotEqual(source_mismatch.returncode, 0)
        self.assertFalse(quarantine_root.exists())
        self.assertTrue(candidate.is_dir())
        self.assertTrue((self.shared / STATE_NAME).is_file())
        release_record.chmod(0o644)
        release_record.write_text(
            json.dumps({"source_git_commit": source_sha}) + "\n",
            encoding="ascii",
        )
        release_record.chmod(0o444)
        quarantine_root.mkdir(mode=0o700)
        collision = quarantine_root / candidate_slug
        collision.mkdir(mode=0o700)
        before_pending = (self.shared / STATE_NAME).read_bytes()
        result = self.run_transaction(
            "quarantine-populated-quiesce", *command_args, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(evidence.read_bytes(), b"retain")
        self.assertEqual((self.shared / STATE_NAME).read_bytes(), before_pending)

        collision.rmdir()
        self.run_transaction("quarantine-populated-quiesce", *command_args)
        changed_current = self.add_release("quarantine-negative-replacement-current")
        (self.app_dir / "current").unlink()
        (self.app_dir / "current").symlink_to(changed_current)
        mark = self.run_transaction(
            "mark-quarantine-services-restored", *command_args, check=False
        )
        self.assertNotEqual(mark.returncode, 0)
        self.assertTrue((quarantine_root / candidate_slug / "payload.bin").exists())
        self.assertTrue((self.shared / STATE_NAME).is_file())

    def test_quarantine_tree_scan_rejects_unsafe_entries_and_enforces_bounds(self) -> None:
        candidate = self.releases / "quarantine-tree-boundary"
        candidate.mkdir(mode=0o755)
        release_file = candidate / "RELEASE.json"
        release_file.write_text(
            json.dumps({"source_git_commit": "e" * 40}) + "\n", encoding="ascii"
        )
        release_file.chmod(0o444)
        target = self.root / "outside-evidence.txt"
        target.write_text("do not follow or modify this target\n", encoding="ascii")
        target.chmod(0o444)
        link = candidate / "linked-evidence.txt"
        link.symlink_to(target)
        with self.assertRaisesRegex(transaction.TransactionError, "special file"):
            transaction._quarantine_tree_snapshot(candidate)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="ascii"), "do not follow or modify this target\n")
        link.unlink()

        hardlink = candidate / "hardlinked-evidence.txt"
        hardlink.hardlink_to(target)
        with self.assertRaisesRegex(transaction.TransactionError, "hard link"):
            transaction._quarantine_tree_snapshot(candidate)
        self.assertTrue(hardlink.exists())
        self.assertEqual(target.read_text(encoding="ascii"), "do not follow or modify this target\n")
        hardlink.unlink()

        special = candidate / "special-entry"
        os.mkfifo(special)
        with self.assertRaisesRegex(transaction.TransactionError, "special file"):
            transaction._quarantine_tree_snapshot(candidate)
        self.assertTrue(special.exists())
        special.unlink()

        with mock.patch.object(transaction, "QUARANTINE_MAX_ENTRIES", 0):
            with self.assertRaisesRegex(transaction.TransactionError, "evidence limits"):
                transaction._quarantine_tree_snapshot(candidate)
        with mock.patch.object(transaction, "QUARANTINE_MAX_BYTES", 1):
            with self.assertRaisesRegex(transaction.TransactionError, "evidence limits"):
                transaction._quarantine_tree_snapshot(candidate)
        self.assertEqual(
            release_file.read_text(encoding="ascii"),
            json.dumps({"source_git_commit": "e" * 40}) + "\n",
        )

        source_sha = "f" * 40
        candidate_slug = f"gha-678901-1-{source_sha[:12]}"
        transaction_candidate = self.releases / candidate_slug
        transaction_candidate.mkdir(mode=0o755)
        transaction_release = transaction_candidate / "RELEASE.json"
        transaction_release.write_text(
            json.dumps({"source_git_commit": source_sha}) + "\n", encoding="ascii"
        )
        transaction_release.chmod(0o444)
        unsafe_link = transaction_candidate / "linked-evidence.txt"
        unsafe_link.symlink_to(target)
        current = self.add_release("quarantine-tree-current")
        (self.app_dir / "current").symlink_to(current)
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(transaction_candidate),
            "--candidate-may-exist",
            "--service-state",
            "deadlock-api=inactive",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
        )
        pending_before = (self.shared / STATE_NAME).read_bytes()
        rejected = self.run_transaction(
            "quarantine-populated-quiesce",
            "--failed-deploy-run-id",
            "678901",
            "--failed-deploy-run-attempt",
            "1",
            "--failed-deploy-source-sha",
            source_sha,
            "--recovery-run-id",
            "789012",
            "--recovery-run-attempt",
            "1",
            "--bundle-sha",
            "d" * 64,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual((self.shared / STATE_NAME).read_bytes(), pending_before)
        self.assertTrue(transaction_candidate.is_dir())
        self.assertTrue(unsafe_link.is_symlink())
        self.assertEqual(target.read_text(encoding="ascii"), "do not follow or modify this target\n")
        self.assertFalse(
            (self.shared / transaction.QUARANTINE_DIR_NAME / candidate_slug).exists()
        )

    def test_recursive_release_cleanup_validates_complete_tree_before_delete(self) -> None:
        for mutation in ("hardlink", "symlink", "special"):
            with self.subTest(mutation=mutation):
                tree = self.root / f"unsafe-cleanup-{mutation}"
                tree.mkdir(mode=0o700)
                safe_file = tree / "safe.txt"
                safe_file.write_text("retained", encoding="ascii")
                safe_file.chmod(0o600)
                if mutation == "hardlink":
                    outside = self.root / "outside-hardlink"
                    outside.write_text("outside", encoding="ascii")
                    outside.chmod(0o600)
                    (tree / "linked.txt").hardlink_to(outside)
                elif mutation == "symlink":
                    (tree / "linked.txt").symlink_to(safe_file)
                else:
                    os.mkfifo(tree / "special")

                with self.assertRaises(transaction.TransactionError):
                    transaction._remove_tree(tree)
                self.assertTrue(tree.exists())
                self.assertTrue(safe_file.exists())
                if mutation == "hardlink":
                    self.assertTrue((self.root / "outside-hardlink").exists())
                elif mutation == "symlink":
                    self.assertTrue((tree / "linked.txt").is_symlink())
                else:
                    self.assertTrue((tree / "special").exists())

    def test_recursive_candidate_cleanup_allows_only_service_owned_web_cache(self) -> None:
        with mock.patch.object(
            transaction, "_run_systemctl", return_value="control-group"
        ) as read_kill_mode:
            transaction._require_web_unit_control_group("/usr/bin/systemctl")
        read_kill_mode.assert_called_once_with(
            "/usr/bin/systemctl",
            "show",
            "--property=KillMode",
            "--value",
            "deadlock-web.service",
        )
        with mock.patch.object(transaction, "_run_systemctl", return_value="process"):
            with self.assertRaisesRegex(
                transaction.TransactionError, "process containment is unsafe"
            ):
                transaction._require_web_unit_control_group("/usr/bin/systemctl")

        if os.geteuid() == 0:
            service_account = pwd.getpwnam("nobody")
            service_uid = service_account.pw_uid
            service_gid = service_account.pw_gid
        else:
            service_uid = os.geteuid()
            service_gid = os.getegid()
        self.assertGreater(service_uid, 0)
        self.assertGreater(service_gid, 0)

        def make_tree(label: str) -> tuple[Path, Path, Path]:
            tree = self.root / f"candidate-cache-{label}"
            tree.mkdir(mode=0o700)
            cache = tree / transaction.WEB_CACHE_RELATIVE_PATH
            cache.mkdir(parents=True, mode=0o750)
            nested = cache / "nested"
            nested.mkdir(mode=0o750)
            file = nested / "generated.txt"
            file.write_text("generated cache data\n", encoding="ascii")
            cache.chmod(0o750)
            nested.chmod(0o750)
            file.chmod(0o640)
            if os.geteuid() == 0:
                for path in (cache, nested, file):
                    os.chown(path, service_uid, service_gid)
            return tree, cache, file

        tree, cache, generated = make_tree("positive")
        with self.assertRaisesRegex(
            transaction.TransactionError,
            "requires verified legacy restoration",
        ):
            transaction._cleanup_recovered_install(
                self.root / "operation.json",
                {
                    "shared": self.root / "shared",
                    "peer_path": self.root / "peer",
                    "candidate_path": tree,
                    "peer_before": None,
                    "phase": "recovery-restored",
                    "operation": "install",
                },
            )
        self.assertTrue(tree.exists())
        real_rmtree = transaction.shutil.rmtree

        def remove_after_mode_check(path: str | bytes | os.PathLike[str], *args, **kwargs) -> None:
            quarantine_cache = Path(path) / transaction.WEB_CACHE_RELATIVE_PATH
            self.assertEqual(stat.S_IMODE(quarantine_cache.stat().st_mode), 0o750)
            self.assertEqual(
                stat.S_IMODE((quarantine_cache / "nested").stat().st_mode),
                0o750,
            )
            self.assertEqual(
                stat.S_IMODE((quarantine_cache / "nested/generated.txt").stat().st_mode),
                0o640,
            )
            real_rmtree(path, *args, **kwargs)

        with mock.patch.object(
            transaction.shutil, "rmtree", side_effect=remove_after_mode_check
        ):
            transaction._remove_tree(
                tree,
                service_owned_cache=(cache, service_uid, service_gid),
            )
        self.assertFalse(tree.exists())
        self.assertFalse(generated.exists())

        for mutation in (
            "wrong-owner",
            "nonroot-sibling",
            "symlink",
            "hardlink",
            "group-writable",
            "special-file",
        ):
            with self.subTest(mutation=mutation):
                tree, cache, generated = make_tree(mutation)
                expected_owner = (service_uid, service_gid)
                if mutation == "wrong-owner":
                    expected_owner = (service_uid + 1, service_gid + 1)
                elif mutation == "nonroot-sibling":
                    sibling = tree / "outside-cache" / "generated.txt"
                    sibling.parent.mkdir(mode=0o755)
                    sibling.write_text("outside cache\n", encoding="ascii")
                    if os.geteuid() == 0:
                        os.chown(sibling, service_uid, service_gid)
                elif mutation == "symlink":
                    target = self.root / "external-cache-target"
                    target.mkdir(exist_ok=True)
                    link = cache / "link"
                    link.symlink_to(target, target_is_directory=True)
                    if os.geteuid() == 0:
                        os.chown(link, service_uid, service_gid, follow_symlinks=False)
                elif mutation == "hardlink":
                    os.link(generated, cache / "duplicate.txt")
                elif mutation == "group-writable":
                    generated.chmod(0o660)
                else:
                    fifo = cache / "special"
                    os.mkfifo(fifo, 0o640)
                    if os.geteuid() == 0:
                        os.chown(fifo, service_uid, service_gid)

                with self.assertRaises(transaction.TransactionError):
                    transaction._remove_tree(
                        tree,
                        service_owned_cache=(cache, *expected_owner),
                    )
                self.assertTrue(tree.exists())
                self.assertTrue(generated.exists())

    def test_legacy_restore_marker_requires_loaded_control_group_for_cache(self) -> None:
        _current, _previous, candidate = self.prepare_install_state()
        cache = candidate / transaction.WEB_CACHE_RELATIVE_PATH
        cache.mkdir(parents=True, mode=0o750)
        record = transaction._load_record(self.shared / STATE_NAME)
        record["phase"] = "recovery-restored"
        transaction._write_record(
            self.shared / STATE_NAME,
            transaction._record_for_write(record),
            creating=False,
        )
        with (
            mock.patch.object(transaction, "_validate_legacy_liveqa_recovery"),
            mock.patch.object(transaction, "_run_systemctl") as run_systemctl,
        ):
            with self.assertRaisesRegex(
                transaction.TransactionError, "profile cache purge proof"
            ):
                transaction.restore_legacy_services(
                    self.shared / STATE_NAME,
                    systemctl="/usr/bin/systemctl",
                )
        run_systemctl.assert_not_called()

        purge_python = self.prepare_wrapper_cache_purge(candidate)
        purge = self.run_transaction(
            "purge-legacy-profile-access-cache", check=False
        )
        self.assertEqual(purge.returncode, 0, purge.stderr)
        if purge_python is not None:
            original = purge_python["original"]
            python_path = cast(Path, purge_python["path"])
            python_path.unlink(missing_ok=True)
            if original is not None:
                kind, value, mode = original
                if kind == "symlink":
                    python_path.symlink_to(cast(str, value))
                else:
                    python_path.write_bytes(cast(bytes, value))
                    python_path.chmod(cast(int, mode))
        enabled = cast(dict[str, str], record["service_enabled_before"])
        service_state = cast(dict[str, str], record["service_state_before"])
        with (
            mock.patch.object(transaction, "_validate_legacy_liveqa_recovery"),
            mock.patch.object(transaction, "_run_systemctl", return_value="process") as run_systemctl,
            mock.patch.object(
                transaction,
                "_read_systemctl_enabled",
                side_effect=lambda _systemctl, unit: (
                    cast(str, record["timer_enabled_before"])
                    if unit == "deadlock-cloudflare-ips.timer"
                    else enabled[unit]
                ),
            ),
            mock.patch.object(
                transaction,
                "_read_systemctl_state",
                side_effect=lambda _systemctl, unit: (
                    "active" if unit.endswith(".timer") else service_state[unit]
                ),
            ),
        ):
            transaction.restore_legacy_services(
                self.shared / STATE_NAME,
                systemctl="/usr/bin/systemctl",
            )
        self.assertGreater(run_systemctl.call_count, 0)
        receipt_before = (self.shared / STATE_NAME).read_bytes()

        with (
            mock.patch.object(transaction, "_validate_legacy_liveqa_recovery"),
            mock.patch.object(transaction, "_verify_legacy_readiness"),
            mock.patch.object(
                transaction,
                "_read_systemctl_enabled",
                side_effect=lambda _systemctl, unit: (
                    cast(str, record["timer_enabled_before"])
                    if unit == "deadlock-cloudflare-ips.timer"
                    else enabled[unit]
                ),
            ),
            mock.patch.object(
                transaction,
                "_read_systemctl_state",
                side_effect=lambda _systemctl, unit: (
                    "active" if unit.endswith(".timer") else service_state[unit]
                ),
            ),
            mock.patch.object(transaction, "_run_systemctl", return_value="process"),
        ):
            with self.assertRaisesRegex(
                transaction.TransactionError, "process containment is unsafe"
            ):
                transaction.mark_legacy_services_restored(
                    self.shared / STATE_NAME,
                    systemctl="/usr/bin/systemctl",
                )

        self.assertEqual((self.shared / STATE_NAME).read_bytes(), receipt_before)
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertTrue(cache.is_dir())

    def test_recursive_release_cleanup_unlinks_allowed_symlink_without_chmod_target(self) -> None:
        tree = self.root / "cleanup-venv-symlink"
        tree.mkdir(mode=0o700)
        outside = self.root / "external-venv-target"
        outside.mkdir(mode=0o555)
        (outside / "marker").write_text("must remain", encoding="ascii")
        link = tree / "lib64"
        link.symlink_to(outside, target_is_directory=True)
        before_mode = stat.S_IMODE(outside.stat().st_mode)

        transaction._remove_tree(tree, allowed_symlink_roots=(tree,))

        self.assertFalse(tree.exists())
        self.assertFalse(link.exists())
        self.assertTrue(outside.exists())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), before_mode)
        self.assertEqual((outside / "marker").read_text(encoding="ascii"), "must remain")

    def test_recursive_release_cleanup_quarantines_only_rechecked_inode(self) -> None:
        tree = self.root / "cleanup-race"
        tree.mkdir(mode=0o700)
        (tree / "original.txt").write_text("original", encoding="ascii")
        (tree / "original.txt").chmod(0o600)
        replacement = self.root / "cleanup-race-original"
        real_rename = os.rename

        def replace_before_quarantine(source: str | bytes | os.PathLike[str], destination: str | bytes | os.PathLike[str]) -> None:
            if Path(source) == tree:
                real_rename(source, replacement)
                tree.mkdir(mode=0o700)
                marker = tree / "replacement.txt"
                marker.write_text("replacement", encoding="ascii")
                marker.chmod(0o600)
            real_rename(source, destination)

        with mock.patch.object(transaction.os, "rename", side_effect=replace_before_quarantine):
            with self.assertRaises(transaction.TransactionError):
                transaction._remove_tree(tree)
        self.assertTrue(replacement.exists())
        self.assertTrue(tree.exists())
        self.assertTrue((tree / "replacement.txt").exists())

    def test_recursive_release_cleanup_partial_delete_is_retained_and_retryable(self) -> None:
        tree = self.root / "cleanup-partial"
        tree.mkdir(mode=0o700)
        first = tree / "first.txt"
        second = tree / "second.txt"
        first.write_text("first", encoding="ascii")
        second.write_text("second", encoding="ascii")
        for path in (first, second):
            path.chmod(0o600)
        outside = self.root / "cleanup-outside"
        outside.write_text("must remain", encoding="ascii")
        outside.chmod(0o600)
        real_rmtree = transaction.shutil.rmtree

        def partial_delete(path: str | bytes | os.PathLike[str], *args, **kwargs) -> None:
            quarantined_first = Path(path) / first.name
            quarantined_first.unlink()
            raise OSError("injected partial cleanup failure")

        with mock.patch.object(transaction.shutil, "rmtree", side_effect=partial_delete):
            with self.assertRaises(transaction.TransactionError):
                transaction._remove_tree(tree)
        # The failed quarantine is put back under its original receipt-bound
        # name, while unrelated paths remain untouched and cleanup can retry.
        self.assertTrue(tree.exists())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(outside.read_text(encoding="ascii"), "must remain")
        with mock.patch.object(transaction.shutil, "rmtree", wraps=real_rmtree):
            transaction._remove_tree(tree)
        self.assertFalse(tree.exists())
        self.assertTrue(outside.exists())

    def test_abort_quiesce_retains_unbound_temporary_directory(self) -> None:
        current = self.add_release("abort-temp-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "abort-temp-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        temporary = self.shared / f".venv-install-{candidate.name}.0000"
        temporary.mkdir(mode=0o700)
        marker = temporary / "unbound.txt"
        marker.write_text("must remain", encoding="ascii")
        marker.chmod(0o600)

        result = self.run_script(
            TRANSACTION_TOOL,
            "abort-quiesce",
            "--state",
            str(self.shared / STATE_NAME),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(marker.exists())

    def test_abort_quiesce_empty_candidate_and_mutable_wrapper_cleanup(self) -> None:
        current = self.add_release("abort-empty-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "abort-empty-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        candidate.mkdir()
        result = self.run_script(
            TRANSACTION_TOOL,
            "abort-quiesce",
            "--state",
            str(self.shared / STATE_NAME),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

        self.tearDown()
        self.setUp()
        current = self.add_release("abort-mutable-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_runtime_stubs(current)
        self.add_fake_venv(self.shared / "venv", marker="mutable-abort")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        candidate = self.releases / "abort-mutable-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        candidate.mkdir()
        self.prepare_wrapper_cache_purge(current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="mutable-abort"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

        self.tearDown()
        self.setUp()
        current = self.add_release("abort-mutable-occupied-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_runtime_stubs(current)
        self.add_fake_venv(self.shared / "venv", marker="mutable-occupied")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        candidate = self.releases / "abort-mutable-occupied-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        candidate.mkdir()
        marker = candidate / "unbound.txt"
        marker.write_text("retain me")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="mutable-occupied"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(marker.exists())
        self.assertIn("restart deadlock-api", (self.root / "systemctl.log").read_text())

    def test_immutable_recovery_wrapper_rejects_incomplete_two_pointer_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("two-pointer-current")
        previous = self.add_release("two-pointer-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_current_control_helper_bombs(current)
        candidate = self.create_wrapper_transaction(current, previous, "two-pointer")
        systemctl, systemctl_log = self.write_failing_systemctl("two-pointer")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).is_file())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertTrue(candidate.is_dir())
        self.assertFalse((self.root / "current-helper-used").exists())

    def test_immutable_recovery_wrapper_reports_real_capture_transaction_failure(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("capture-failure-current")
        previous = self.add_release("capture-failure-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_bound_release_tools(current)
        candidate = self.create_wrapper_transaction(
            current, previous, "capture-failure", complete_snapshot=True
        )
        systemctl, systemctl_log = self.write_failing_systemctl(
            "capture-failure", exit_code=4
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "RECOVERY_CHECK schema=1 status=failed check=capture_transaction",
            result.stderr,
        )
        self.assertIn(
            "RELEASE_RUNTIME schema=1 status=failed class=systemd_state",
            result.stderr,
        )
        self.assertTrue(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).is_file())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertTrue(candidate.exists())

    def test_immutable_recovery_wrapper_two_pointer_uses_bound_data_helpers(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("upgrade-current")
        previous = self.add_release("upgrade-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_bound_release_tools(current)
        venv = self.shared / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
            encoding="utf-8",
        )
        (venv / "bin" / "python").chmod(0o755)
        (venv / "deps-version").write_text("unchanged\n", encoding="ascii")
        candidate = self.create_wrapper_transaction(
            current, previous, "upgrade", complete_snapshot=True
        )
        # The immutable helper must use the supplied control-plane systemctl
        # boundary; a release-specific control helper is only digest-read.
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "inactive",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "inactive",
            }
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "current-helper-used").exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertFalse(candidate.exists())

    def test_immutable_recovery_wrapper_retains_receipt_on_systemctl_rc4_or_timeout(
        self,
    ) -> None:
        for index, (exit_code, label) in enumerate(((4, "rc4"), (124, "timeout"))):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(exit_code=exit_code):
                generation = self.install_recovery_generation()
                current = self.add_release(f"{label}-current")
                previous = self.add_release(f"{label}-previous")
                (self.app_dir / "current").symlink_to(current)
                (self.app_dir / "previous").symlink_to(previous)
                self.add_bound_release_tools(current)
                venv = self.shared / "venv"
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").write_text(
                    "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
                    encoding="utf-8",
                )
                (venv / "bin" / "python").chmod(0o755)
                (venv / "deps-version").write_text("unchanged\n", encoding="ascii")
                candidate = self.create_wrapper_transaction(
                    current, previous, label, complete_snapshot=True
                )
                good_systemctl = self.write_stateful_systemctl(
                    {
                        "deadlock-api.service": "inactive",
                        "deadlock-worker.service": "inactive",
                        "deadlock-web.service": "inactive",
                    }
                )
                receipt = self.shared / ".release-systemd-state.json"
                self.run_script(
                    SYSTEMD_STATE_TOOL,
                    "capture-transaction",
                    "--state",
                    str(receipt),
                    "--transaction",
                    str(self.shared / STATE_NAME),
                    "--app-dir",
                    str(self.app_dir),
                    "--helper-release",
                    str(current),
                    "--require-helper-manifest",
                    "--systemctl",
                    str(good_systemctl),
                )
                bad_systemctl, systemctl_log = self.write_failing_systemctl(
                    label, exit_code=exit_code
                )
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(bad_systemctl),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "RECOVERY_CHECK schema=1 status=failed check=verify_units",
                    result.stderr,
                )
                self.assertTrue(systemctl_log.exists())
                self.assertTrue(
                    receipt.exists(),
                    f"rc={exit_code} must not clear the systemd receipt",
                )
                self.assertTrue(
                    (self.shared / STATE_NAME).exists(),
                    f"rc={exit_code} must retain the transaction for an immutable retry",
                )
                self.assertTrue(candidate.exists())

        fixture = type(self)(self._testMethodName)
        fixture.setUp()
        try:
            fixture._assert_cloudflare_pending_guard_preflight_replay()
        finally:
            fixture.tearDown()

    def _run_cloudflare_recovery_exit_trap(
        self,
        *,
        systemctl: Path,
        evidence_path: Path,
        evidence_id: str,
        attempt_nonce: str,
        operation_id: str,
        transaction_sha: str,
        receipt_sha: str,
        invocation_id: str,
    ) -> subprocess.CompletedProcess[str]:
        wrapper_source = RECOVERY_WRAPPER.read_text(encoding="utf-8")
        start = wrapper_source.index("platform_release_recovery_exit() {")
        end = wrapper_source.index("\ntrap platform_release_recovery_exit EXIT", start)
        exit_function = wrapper_source[start:end]
        harness = "\n".join(
            (
                f"SYSTEMCTL_BIN={shlex.quote(str(systemctl))}",
                "CLOUDFLARE_PENDING_RESET_ATTEMPTED=1",
                f"CLOUDFLARE_PENDING_RESET_EVIDENCE_PATH={shlex.quote(str(evidence_path))}",
                f"CLOUDFLARE_PENDING_RESET_ATTEMPT_NONCE={shlex.quote(attempt_nonce)}",
                f"CLOUDFLARE_PENDING_RESET_EVIDENCE_ID={shlex.quote(evidence_id)}",
                f"CLOUDFLARE_PENDING_RESET_TX_SHA256={shlex.quote(transaction_sha)}",
                f"CLOUDFLARE_PENDING_RESET_RECEIPT_SHA256={shlex.quote(receipt_sha)}",
                f"CLOUDFLARE_PENDING_RESET_INVOCATION_ID={shlex.quote(invocation_id)}",
                f"operation_id={shlex.quote(operation_id)}",
                f"source_sha={shlex.quote(systemd_state.PENDING_GUARD_CURRENT_SOURCE_SHA)}",
                f"previous_source_sha={shlex.quote(systemd_state.PENDING_GUARD_PREVIOUS_SOURCE_SHA)}",
                'run_systemctl() { "$SYSTEMCTL_BIN" "$@"; }',
                "platform_release_lock_close() { :; }",
                exit_function,
                "trap platform_release_recovery_exit EXIT",
                "exit 0",
            )
        )
        return subprocess.run(
            ["/bin/bash", "-c", harness],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def _assert_cloudflare_pending_guard_preflight_replay(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("Cloudflare pending-guard preflight requires root-owned receipt fixtures")

        current = self.add_release("cloudflare-fixed-current")
        previous = self.add_release("cloudflare-fixed-previous")
        for release, source_sha in (
            (current, systemd_state.PENDING_GUARD_CURRENT_SOURCE_SHA),
            (previous, systemd_state.PENDING_GUARD_PREVIOUS_SOURCE_SHA),
        ):
            (release / "RELEASE.json").write_text(
                json.dumps({"source_git_commit": source_sha}) + "\n", encoding="ascii"
            )
            (release / "RELEASE.json").chmod(0o444)
        for relative in systemd_state.HELPER_RELATIVE_PATHS:
            source = REPO_ROOT / "platform" / relative
            for release in (current, previous):
                target = release / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
                target.chmod(0o755)
        unit_source = REPO_ROOT / "platform/deploy/systemd/deadlock-cloudflare-ips.service"
        for release in (current, previous):
            target = release / "deploy/systemd/deadlock-cloudflare-ips.service"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(unit_source, target, follow_symlinks=False)
            target.chmod(0o644)

        self.switch_pointer("current", current)
        self.switch_pointer("previous", previous)
        (self.shared / "venv").mkdir(mode=0o755)
        self.create_wrapper_transaction(
            current, previous, "cloudflare-pending-guard", complete_snapshot=True, timer_active=True
        )
        self.run_transaction("recover", "--retain")
        self.assertEqual(self.state_phase(), "recovery-restored")

        invocation = "179af136423f48e190994de6fba470af"
        start = datetime.now(ZoneInfo("Europe/Helsinki")).replace(microsecond=0) - timedelta(seconds=2)
        next_elapse = start + timedelta(hours=2)
        exec_start = (
            "{ path=/bin/bash ; argv[]=/bin/bash "
            "/opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh "
            "--app-dir /opt/oldsparky/platform -- "
            "/opt/oldsparky/platform/shared/venv/bin/python "
            "/opt/oldsparky/platform/current/tools/platform_update_cloudflare_ips.py "
            "--apply --reload ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
            "pid=0 ; code=(null) ; status=0/0 }"
        )
        service = {
            "Id": systemd_state.CLOUDFLARE_UNIT,
            "FragmentPath": systemd_state.CLOUDFLARE_FRAGMENT,
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "Type": "oneshot",
            "RemainAfterExit": "no",
            "KillMode": "control-group",
            "ActiveState": "failed",
            "SubState": "failed",
            "Result": "exit-code",
            "ExecMainCode": "1",
            "ExecMainStatus": "75",
            "MainPID": "0",
            "ControlPID": "0",
            "ControlGroup": "",
            "TasksCurrent": "[not set]",
            "UnitFileState": "static",
            "InvocationID": invocation,
            "ExecMainStartTimestamp": start.strftime("%a %Y-%m-%d %H:%M:%S %Z"),
            "ExecMainExitTimestamp": start.strftime("%a %Y-%m-%d %H:%M:%S %Z"),
            "ExecStart": exec_start,
        }
        timer = {
            "Id": systemd_state.CLOUDFLARE_TIMER,
            "FragmentPath": "/etc/systemd/system/deadlock-cloudflare-ips.timer",
            "DropInPaths": "",
            "NeedDaemonReload": "no",
            "ActiveState": "active",
            "SubState": "waiting",
            "Unit": systemd_state.CLOUDFLARE_UNIT,
            "UnitFileState": "enabled",
            "LastTriggerUSec": start.strftime("%a %Y-%m-%d %H:%M:%S %Z"),
            "NextElapseUSecRealtime": next_elapse.strftime("%a %Y-%m-%d %H:%M:%S %Z"),
        }
        states = {
            unit: ("active" if unit == systemd_state.CLOUDFLARE_TIMER else "inactive")
            for unit in systemd_state.OWNED_UNITS
        }
        states[systemd_state.CLOUDFLARE_UNIT] = "inactive"
        static_units = {
            unit for unit in systemd_state.OWNED_UNITS
            if unit.endswith(".service") and unit not in {
                "deadlock-api.service", "deadlock-worker.service", "deadlock-web.service"
            }
        }
        enabled = {
            unit: ("static" if unit in static_units else "disabled" if unit == "deadlock-offsite-backup.timer" else "enabled")
            for unit in systemd_state.OWNED_UNITS
        }
        config_path = self.root / "cloudflare-systemd-config.json"
        command_log = self.root / "cloudflare-systemd-commands.jsonl"
        config = {
            "states": states,
            "enabled": enabled,
            "properties": {systemd_state.CLOUDFLARE_UNIT: service, systemd_state.CLOUDFLARE_TIMER: timer},
            "jobs": [],
        }
        config_path.write_text(json.dumps(config), encoding="ascii")
        systemctl = self.root / "cloudflare-systemctl"
        systemctl.write_text(
            "#!/usr/bin/env python3\n"
            "import json,sys\n"
            f"config_path={str(config_path)!r}; log_path={str(command_log)!r}\n"
            "args=sys.argv[1:]\n"
            "with open(log_path,'a',encoding='ascii') as f: f.write(json.dumps(args)+'\\n')\n"
            "cfg=json.load(open(config_path,encoding='ascii')); cmd=args[0] if args else ''\n"
            "if cmd=='show':\n"
            " unit=args[1]; props=next(x.partition('=')[2] for x in args if x.startswith('--property=')); values=cfg['properties'][unit]\n"
            " if unit=='deadlock-cloudflare-ips.service' and cfg.get('fail_service_show_after') is not None:\n"
            "  count=cfg.get('service_show_count',0)+1; cfg['service_show_count']=count; json.dump(cfg,open(config_path,'w',encoding='ascii'))\n"
            "  if count>=cfg['fail_service_show_after']: raise SystemExit(1)\n"
            " for key in props.split(','): print(f'{key}={values[key]}')\n"
            "elif cmd=='list-jobs':\n"
            " if not cfg['jobs']: print('No jobs running.')\n"
            " else:\n  print('JOB UNIT TYPE STATE')\n  [print(' '.join(row)) for row in cfg['jobs']]\n"
            "elif cmd=='is-active':\n"
            " state=cfg['states'][args[1]]; print(state); raise SystemExit(0 if state=='active' else 3)\n"
            "elif cmd=='is-enabled':\n"
            " state=cfg['enabled'][args[1]]; print(state); raise SystemExit(1 if state=='disabled' else 0)\n"
            "elif cmd in ('stop','start','reset-failed'):\n"
            " unit=args[1]\n"
            " if cmd=='stop': cfg['states'][unit]='inactive'; cfg['properties'][unit]['ActiveState']='inactive'; cfg['properties'][unit]['SubState']='dead'\n"
            " if cmd=='start': cfg['states'][unit]='active'; cfg['properties'][unit]['ActiveState']='active'; cfg['properties'][unit]['SubState']='waiting'\n"
            " if cmd=='reset-failed' and cfg.get('fail_reset'): raise SystemExit(1)\n"
            " if cmd=='reset-failed':\n  cfg['states'][unit]='inactive'; p=cfg['properties'][unit]; p['ActiveState']='inactive'; p['SubState']='dead'; p['Result']='success'; p['ExecMainCode']='0'; p['ExecMainStatus']='0'\n"
            " json.dump(cfg,open(config_path,'w',encoding='ascii')); raise SystemExit(0)\n"
            "else: raise SystemExit(2)\n",
            encoding="ascii",
        )
        systemctl.chmod(0o755)
        timestamp_usec = str(int(start.astimezone(timezone.utc).timestamp() * 1_000_000))
        journal = self.root / "cloudflare-journalctl"
        journal.write_text(
            "#!/usr/bin/env python3\nimport json\nprint(json.dumps({"
            "'MESSAGE':'RELEASE_GUARD schema=1 status=failed class=pending_operation release_slug=unavailable source_sha=unavailable',"
            f"'_SYSTEMD_INVOCATION_ID':{invocation!r},'__REALTIME_TIMESTAMP':{timestamp_usec!r}"
            "},separators=(',',':')))\n",
            encoding="ascii",
        )
        journal.chmod(0o755)
        original_journal = journal.read_text(encoding="ascii")

        receipt = self.shared / ".release-systemd-state.json"
        capture = self.run_script(
            SYSTEMD_STATE_TOOL, "capture-transaction", "--state", str(receipt),
            "--transaction", str(self.shared / STATE_NAME), "--app-dir", str(self.app_dir),
            "--helper-release", str(current), "--require-helper-manifest", "--systemctl", str(systemctl),
            check=False,
        )
        self.assertEqual(capture.returncode, 0, capture.stderr)
        config = json.loads(config_path.read_text(encoding="ascii"))
        config["properties"][systemd_state.CLOUDFLARE_UNIT] = {
            **service,
            "ActiveState": "inactive",
            "SubState": "dead",
            "Result": "success",
            "ExecMainCode": "0",
            "ExecMainStatus": "0",
        }
        config["states"][systemd_state.CLOUDFLARE_UNIT] = "inactive"
        config_path.write_text(json.dumps(config), encoding="ascii")
        cli_skip = self.run_script(
            SYSTEMD_STATE_TOOL,
            "reset-cloudflare-pending-guard",
            "--state", str(receipt), "--transaction", str(self.shared / STATE_NAME),
            "--helper-release", str(current), "--app-dir", str(self.app_dir),
            "--systemctl", str(systemctl), "--attempt-nonce", "a" * 64,
            "--expected-event-id", "a" * 64,
            check=False,
        )
        self.assertEqual(cli_skip.returncode, 0, cli_skip.stderr)
        self.assertEqual(
            cli_skip.stdout.strip(),
            "RELEASE_CLOUDFLARE_PENDING_RESET schema=1 status=skip",
        )
        config["properties"][systemd_state.CLOUDFLARE_UNIT] = service
        config["states"][systemd_state.CLOUDFLARE_UNIT] = "failed"
        config_path.write_text(json.dumps(config), encoding="ascii")

        original_systemctl_path = systemd_state.SYSTEMCTL_PATH
        original_journal_path = systemd_state.JOURNALCTL_PATH
        attempt_nonce = "a" * 64

        def event_id_for(invocation_id: str = invocation) -> str:
            raw_transaction = (self.shared / STATE_NAME).read_bytes()
            raw_receipt = receipt.read_bytes()
            transaction_record = json.loads(raw_transaction)
            return hashlib.sha256(
                ":".join(
                    (
                        transaction_record["operation_id"],
                        hashlib.sha256(raw_transaction).hexdigest(),
                        hashlib.sha256(raw_receipt).hexdigest(),
                        invocation_id,
                    )
                ).encode("ascii")
            ).hexdigest()

        try:
            systemd_state.SYSTEMCTL_PATH = str(systemctl)
            systemd_state.JOURNALCTL_PATH = str(journal)
            evidence_dir = self.shared / ".release-cloudflare-pending-reset-evidence"

            def write_journal_records(entries: list[dict[str, str]]) -> None:
                journal.write_text(
                    "#!/usr/bin/env python3\nimport json\n"
                    f"records={entries!r}\n"
                    "for record in records: print(json.dumps(record,separators=(',',':')))\n",
                    encoding="ascii",
                )
                journal.chmod(0o755)

            initial_journal_record = systemd_state._invocation_guard_record(invocation)
            self.assertIsNotNone(initial_journal_record)
            canonical_record = {
                "MESSAGE": "RELEASE_GUARD schema=1 status=failed class=pending_operation release_slug=unavailable source_sha=unavailable",
                "_SYSTEMD_INVOCATION_ID": invocation,
                "__REALTIME_TIMESTAMP": timestamp_usec,
            }
            # Journald may serialize identical fields in a different order.
            write_journal_records([dict(reversed(list(canonical_record.items())))])
            reordered_journal_record = systemd_state._invocation_guard_record(invocation)
            self.assertEqual(reordered_journal_record, initial_journal_record)

            changed_record = {**canonical_record, "_HOSTNAME": "changed-host"}
            write_journal_records([changed_record])
            changed_journal_record = systemd_state._invocation_guard_record(invocation)
            self.assertIsNotNone(changed_journal_record)
            self.assertEqual(changed_journal_record[0], initial_journal_record[0])
            self.assertNotEqual(changed_journal_record[1], initial_journal_record[1])

            nonmarker_record = {
                **canonical_record,
                "MESSAGE": "unrelated same-invocation journal record",
            }
            write_journal_records([canonical_record, nonmarker_record])
            self.assertEqual(systemd_state._invocation_guard_record(invocation), initial_journal_record)

            write_journal_records([canonical_record, canonical_record])
            self.assertIsNone(systemd_state._invocation_guard_record(invocation))
            journal.write_text(original_journal, encoding="ascii")
            journal.chmod(0o755)

            # A receipt-matching ordinary inactive/static unit is not an
            # incident. It must skip before consulting journal/timer guards.
            config["properties"][systemd_state.CLOUDFLARE_UNIT] = {
                **service,
                "ActiveState": "inactive",
                "SubState": "dead",
                "Result": "success",
                "ExecMainCode": "0",
                "ExecMainStatus": "0",
            }
            config["states"][systemd_state.CLOUDFLARE_UNIT] = "inactive"
            config["properties"][systemd_state.CLOUDFLARE_TIMER]["NextElapseUSecRealtime"] = "n/a"
            config_path.write_text(json.dumps(config), encoding="ascii")
            journal.unlink()
            ordinary = systemd_state.reset_cloudflare_pending_guard(
                receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                helper_release=current, attempt_nonce=attempt_nonce,
                expected_event_id=event_id_for(),
            )
            self.assertEqual(ordinary, {"outcome": "skip"})
            self.assertFalse(evidence_dir.exists())
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertFalse(any(command and command[0] in {"stop", "start", "reset-failed"} for command in commands))

            config["properties"][systemd_state.CLOUDFLARE_UNIT] = service
            config["states"][systemd_state.CLOUDFLARE_UNIT] = "failed"
            config["properties"][systemd_state.CLOUDFLARE_TIMER]["NextElapseUSecRealtime"] = timer[
                "NextElapseUSecRealtime"
            ]
            config_path.write_text(json.dumps(config), encoding="ascii")
            journal.write_text(original_journal, encoding="ascii")
            journal.chmod(0o755)
            plan = systemd_state.reset_cloudflare_pending_guard(
                receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                helper_release=current, attempt_nonce=attempt_nonce,
                expected_event_id=event_id_for(), apply_reset=False,
            )
            journal_status, journal_output = systemd_state._bounded_command(
                [
                    str(journal), "--no-pager", "--quiet", "--output=json", "--lines=50",
                    "--unit=" + systemd_state.CLOUDFLARE_UNIT,
                    "_SYSTEMD_INVOCATION_ID=" + invocation,
                ],
                maximum=systemd_state.PRIVATE_JOURNAL_MAX_BYTES,
                timeout=15,
            )
            self.assertEqual((journal_status, journal_output.endswith("\n")), (0, True), repr(journal_output))
            self.assertIsNotNone(
                systemd_state._invocation_guard_record(invocation),
                f"synthetic invocation journal was not accepted: {journal.read_text(encoding='ascii')!r}",
            )
            self.assertIn(
                "service", plan,
                f"unexpected incident skip: {plan!r}; tail commands="
                f"{command_log.read_text(encoding='ascii').splitlines()[-20:]!r}",
            )
            self.assertEqual(plan["service"]["exec_main_status"], 75)
            self.assertEqual(plan["timer"]["unit"], systemd_state.CLOUDFLARE_UNIT)
            self.assertFalse(evidence_dir.exists())
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertFalse(any(command and command[0] in {"stop", "start", "reset-failed"} for command in commands))

            for change, expected_outcome in (
                ({"properties": {systemd_state.CLOUDFLARE_UNIT: {"FragmentPath": "/tmp/foreign.service"}}}, "skip"),
                ({"states": {systemd_state.CLOUDFLARE_UNIT: "active"}, "properties": {systemd_state.CLOUDFLARE_UNIT: {**service, "ActiveState": "active", "SubState": "running"}}}, "skip"),
                ({"properties": {systemd_state.CLOUDFLARE_UNIT: {**service, "ExecMainStatus": "1"}}}, "skip"),
                ({"properties": {systemd_state.CLOUDFLARE_UNIT: {**service, "ExecStart": exec_start.replace("--reload", "--changed")}}}, "skip"),
            ):
                config.update(change)
                config_path.write_text(json.dumps(config), encoding="ascii")
                outcome = systemd_state.reset_cloudflare_pending_guard(
                    receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                    helper_release=current, attempt_nonce=attempt_nonce,
                    expected_event_id=event_id_for(), apply_reset=False,
                )
                self.assertEqual(outcome["outcome"], expected_outcome)
                config = json.loads(config_path.read_text(encoding="ascii"))
                config["properties"][systemd_state.CLOUDFLARE_UNIT] = service
                config["states"][systemd_state.CLOUDFLARE_UNIT] = "failed"
                config["properties"][systemd_state.CLOUDFLARE_TIMER] = timer
                config["states"][systemd_state.CLOUDFLARE_TIMER] = "active"
                config["jobs"] = []
                config_path.write_text(json.dumps(config), encoding="ascii")
            journal.write_text(original_journal.replace("pending_operation", "other_failure"), encoding="ascii")
            outcome = systemd_state.reset_cloudflare_pending_guard(
                receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                helper_release=current, attempt_nonce=attempt_nonce,
                expected_event_id=event_id_for(), apply_reset=False,
            )
            self.assertEqual(outcome["outcome"], "skip")
            journal.write_text(original_journal, encoding="ascii")

            config["jobs"] = [["7", systemd_state.CLOUDFLARE_TIMER, "stop", "running"]]
            config_path.write_text(json.dumps(config), encoding="ascii")
            with self.assertRaises(systemd_state.StateError):
                systemd_state.reset_cloudflare_pending_guard(
                    receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                    helper_release=current, attempt_nonce=attempt_nonce,
                    expected_event_id=event_id_for(), apply_reset=False,
                )
            config["jobs"] = []
            config_path.write_text(json.dumps(config), encoding="ascii")
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertFalse(any(command and command[0] in {"stop", "start", "reset-failed"} for command in commands))

            config["service_show_count"] = 0
            config["fail_service_show_after"] = 2
            config_path.write_text(json.dumps(config), encoding="ascii")
            with self.assertRaises(systemd_state.StateError):
                systemd_state.reset_cloudflare_pending_guard(
                    receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                    helper_release=current, attempt_nonce=attempt_nonce,
                    expected_event_id=event_id_for(),
                )
            after_revalidation_failure = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertFalse(
                any(
                    command and command[0] in {"stop", "start", "reset-failed"}
                    for command in after_revalidation_failure
                )
            )
            self.assertFalse(evidence_dir.exists())
            config = json.loads(config_path.read_text(encoding="ascii"))
            config.pop("fail_service_show_after", None)
            config.pop("service_show_count", None)
            config_path.write_text(json.dumps(config), encoding="ascii")

            # A parsed journal field changing between preflight and recheck
            # must invalidate the binding even when the target marker remains.
            journal_sequence_state = self.root / "cloudflare-journal-sequence-count"
            journal_sequence_state.unlink(missing_ok=True)
            first_record = canonical_record
            second_record = {**canonical_record, "_HOSTNAME": "changed-during-recheck"}
            journal.write_text(
                "#!/usr/bin/env python3\nimport json\nfrom pathlib import Path\n"
                f"state=Path({str(journal_sequence_state)!r})\n"
                "count=int(state.read_text())+1 if state.exists() else 1\n"
                "state.write_text(str(count))\n"
                f"records={[first_record, second_record]!r}\n"
                "print(json.dumps(records[min(count-1,len(records)-1)],separators=(',',':')))\n",
                encoding="ascii",
            )
            journal.chmod(0o755)
            before_journal_drift = [json.loads(line) for line in command_log.read_text().splitlines()]
            with self.assertRaises(systemd_state.StateError):
                systemd_state.reset_cloudflare_pending_guard(
                    receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                    helper_release=current, attempt_nonce=attempt_nonce,
                    expected_event_id=event_id_for(),
                )
            self.assertFalse(evidence_dir.exists())
            after_journal_drift = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertEqual(
                [entry for entry in after_journal_drift if entry and entry[0] in {"stop", "start", "reset-failed"}],
                [entry for entry in before_journal_drift if entry and entry[0] in {"stop", "start", "reset-failed"}],
            )
            journal.write_text(original_journal, encoding="ascii")
            journal.chmod(0o755)
            journal_sequence_state.unlink(missing_ok=True)

            timer_status, timer_output = systemd_state._bounded_command(
                [
                    str(systemctl), "show", systemd_state.CLOUDFLARE_TIMER, "--no-pager",
                    "--property=" + ",".join(systemd_state.CLOUDFLARE_TIMER_PROPERTIES),
                ],
                maximum=32 * 1024,
            )
            self.assertEqual(
                timer_status, 0,
                f"fixed timer fixture failed: {timer_output!r}; "
                f"config={config_path.read_text(encoding='ascii')!r}",
            )

            # A fresh, otherwise valid invocation must not be acted on when
            # the wrapper supplied the prior event identity.
            stale_event_id = event_id_for()
            second_invocation = "279af136423f48e190994de6fba470af"
            config["properties"][systemd_state.CLOUDFLARE_UNIT] = {
                **service, "InvocationID": second_invocation,
            }
            config["states"][systemd_state.CLOUDFLARE_UNIT] = "failed"
            config_path.write_text(json.dumps(config), encoding="ascii")
            journal.write_text(original_journal.replace(invocation, second_invocation), encoding="ascii")
            journal.chmod(0o755)
            commands_before_drift = [json.loads(line) for line in command_log.read_text().splitlines()]
            drift = systemd_state.reset_cloudflare_pending_guard(
                receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                helper_release=current, attempt_nonce=attempt_nonce,
                expected_event_id=stale_event_id,
            )
            self.assertEqual(drift, {"outcome": "skip"})
            self.assertFalse(evidence_dir.exists())
            commands_after_drift = [json.loads(line) for line in command_log.read_text().splitlines()]
            mutation_commands = {"stop", "start", "reset-failed"}
            self.assertEqual(
                [entry for entry in commands_after_drift if entry and entry[0] in mutation_commands],
                [entry for entry in commands_before_drift if entry and entry[0] in mutation_commands],
            )

            config["properties"][systemd_state.CLOUDFLARE_UNIT] = service
            config_path.write_text(json.dumps(config), encoding="ascii")
            journal.write_text(original_journal, encoding="ascii")
            journal.chmod(0o755)

            result = systemd_state.reset_cloudflare_pending_guard(
                receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                helper_release=current, attempt_nonce=attempt_nonce,
                expected_event_id=event_id_for(),
            )
            self.assertEqual(result["outcome"], "performed")
            self.assertRegex(str(result["evidence_id"]), r"^[0-9a-f]{64}$")
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            actions = [tuple(command[:2]) for command in commands if command and command[0] in {"stop", "start", "reset-failed"}]
            self.assertEqual(actions, [("stop", systemd_state.CLOUDFLARE_TIMER), ("reset-failed", systemd_state.CLOUDFLARE_UNIT)])
            self.assertTrue(receipt.is_file())
            self.assertTrue((self.shared / STATE_NAME).is_file())
            records = list(evidence_dir.glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(stat.S_IMODE(records[0].stat().st_mode), 0o600)
            evidence = json.loads(records[0].read_text(encoding="ascii"))
            transaction_bytes = (self.shared / STATE_NAME).read_bytes()
            receipt_bytes = receipt.read_bytes()
            transaction_record = json.loads(transaction_bytes)
            expected_evidence_id = hashlib.sha256(
                ":".join(
                    (
                        transaction_record["operation_id"],
                        hashlib.sha256(transaction_bytes).hexdigest(),
                        hashlib.sha256(receipt_bytes).hexdigest(),
                        invocation,
                    )
                ).encode("ascii")
            ).hexdigest()
            self.assertEqual(result["evidence_id"], expected_evidence_id)
            self.assertEqual(records[0].name, expected_evidence_id + ".json")
            self.assertEqual(evidence["attempt_nonce"], attempt_nonce)
            self.assertEqual(evidence["transaction_sha256"], hashlib.sha256(transaction_bytes).hexdigest())
            self.assertEqual(evidence["systemd_receipt_sha256"], hashlib.sha256(receipt_bytes).hexdigest())
            self.assertEqual(evidence["service"]["invocation_id"], invocation)
            config = json.loads(config_path.read_text(encoding="ascii"))
            config["properties"][systemd_state.CLOUDFLARE_UNIT] = service
            config["states"][systemd_state.CLOUDFLARE_UNIT] = "failed"
            config["properties"][systemd_state.CLOUDFLARE_TIMER] = timer
            config["states"][systemd_state.CLOUDFLARE_TIMER] = "active"
            config["jobs"] = []
            config_path.write_text(json.dumps(config), encoding="ascii")
            before_collision = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            with self.assertRaises(systemd_state.StateError):
                systemd_state.reset_cloudflare_pending_guard(
                    receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                    helper_release=current, attempt_nonce="e" * 64,
                    expected_event_id=event_id_for(),
                )
            after_collision = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            self.assertEqual(after_collision, before_collision)

            active_timer_trap = self._run_cloudflare_recovery_exit_trap(
                systemctl=systemctl, evidence_path=records[0], evidence_id=expected_evidence_id,
                attempt_nonce=attempt_nonce, operation_id=transaction_record["operation_id"],
                transaction_sha=evidence["transaction_sha256"], receipt_sha=evidence["systemd_receipt_sha256"],
                invocation_id=invocation,
            )
            self.assertEqual(active_timer_trap.returncode, 0, active_timer_trap.stderr)
            after_active_timer_trap = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            self.assertEqual(after_active_timer_trap, before_collision)

            timer_property = config["properties"][systemd_state.CLOUDFLARE_TIMER]
            config["states"][systemd_state.CLOUDFLARE_TIMER] = "inactive"
            timer_property.update({"ActiveState": "inactive", "SubState": "dead"})
            config_path.write_text(json.dumps(config), encoding="ascii")
            before_trap_actions = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            missing_path = evidence_dir / ("f" * 64 + ".json")
            no_evidence = self._run_cloudflare_recovery_exit_trap(
                systemctl=systemctl, evidence_path=missing_path, evidence_id="f" * 64,
                attempt_nonce=attempt_nonce, operation_id=transaction_record["operation_id"],
                transaction_sha=evidence["transaction_sha256"], receipt_sha=evidence["systemd_receipt_sha256"],
                invocation_id=invocation,
            )
            self.assertEqual(no_evidence.returncode, 0, no_evidence.stderr)
            current_actions = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            self.assertEqual(current_actions, before_trap_actions)

            valid_evidence = self._run_cloudflare_recovery_exit_trap(
                systemctl=systemctl, evidence_path=records[0], evidence_id=expected_evidence_id,
                attempt_nonce=attempt_nonce, operation_id=transaction_record["operation_id"],
                transaction_sha=evidence["transaction_sha256"], receipt_sha=evidence["systemd_receipt_sha256"],
                invocation_id=invocation,
            )
            self.assertEqual(valid_evidence.returncode, 0, valid_evidence.stderr)
            current_actions = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            self.assertEqual(
                current_actions[len(before_trap_actions):],
                [("start", systemd_state.CLOUDFLARE_TIMER)],
            )
            timer_active_checks = [
                command for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command[:2] == ["is-active", systemd_state.CLOUDFLARE_TIMER]
            ]
            self.assertGreaterEqual(len(timer_active_checks), 2)

            config = json.loads(config_path.read_text(encoding="ascii"))
            config["states"][systemd_state.CLOUDFLARE_TIMER] = "inactive"
            config["properties"][systemd_state.CLOUDFLARE_TIMER].update(
                {"ActiveState": "inactive", "SubState": "dead"}
            )
            config_path.write_text(json.dumps(config), encoding="ascii")
            before_stale_trap = current_actions.copy()
            stale_nonce = self._run_cloudflare_recovery_exit_trap(
                systemctl=systemctl, evidence_path=records[0], evidence_id=expected_evidence_id,
                attempt_nonce="d" * 64, operation_id=transaction_record["operation_id"],
                transaction_sha=evidence["transaction_sha256"], receipt_sha=evidence["systemd_receipt_sha256"],
                invocation_id=invocation,
            )
            self.assertEqual(stale_nonce.returncode, 1)
            current_actions = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            self.assertEqual(current_actions, before_stale_trap)
            actions_before_retry = current_actions.copy()
            config = json.loads(config_path.read_text(encoding="ascii"))
            config["states"][systemd_state.CLOUDFLARE_TIMER] = "inactive"
            config["properties"][systemd_state.CLOUDFLARE_TIMER].update(
                {"ActiveState": "inactive", "SubState": "dead", "NextElapseUSecRealtime": "n/a"}
            )
            config["jobs"] = [["8", systemd_state.CLOUDFLARE_TIMER, "start", "running"]]
            config_path.write_text(json.dumps(config), encoding="ascii")
            journal.write_text(original_journal.replace("pending_operation", "unrelated"), encoding="ascii")
            retry = systemd_state.reset_cloudflare_pending_guard(
                receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                helper_release=current, attempt_nonce="b" * 64,
                expected_event_id=event_id_for(),
            )
            self.assertEqual(retry, {"outcome": "skip"})
            commands = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertEqual(
                [tuple(command[:2]) for command in commands if command and command[0] in {"stop", "start", "reset-failed"}],
                actions_before_retry,
            )

            # A failed reset restores only the fixed timer; it never starts
            # the Cloudflare service and retains both recovery records.
            second_invocation = "279af136423f48e190994de6fba470af"
            second_start = datetime.now(ZoneInfo("Europe/Helsinki")).replace(microsecond=0) - timedelta(seconds=2)
            second_next = second_start + timedelta(hours=2)
            second_start_text = second_start.strftime("%a %Y-%m-%d %H:%M:%S %Z")
            second_next_text = second_next.strftime("%a %Y-%m-%d %H:%M:%S %Z")
            config = json.loads(config_path.read_text(encoding="ascii"))
            config["states"][systemd_state.CLOUDFLARE_UNIT] = "failed"
            config["states"][systemd_state.CLOUDFLARE_TIMER] = "active"
            config["properties"][systemd_state.CLOUDFLARE_UNIT] = {
                **service,
                "InvocationID": second_invocation,
                "ExecMainStartTimestamp": second_start_text,
                "ExecMainExitTimestamp": second_start_text,
            }
            config["properties"][systemd_state.CLOUDFLARE_TIMER] = {
                **timer,
                "ActiveState": "active",
                "SubState": "waiting",
                "LastTriggerUSec": second_start_text,
                "NextElapseUSecRealtime": second_next_text,
            }
            config["jobs"] = []
            config["fail_reset"] = True
            config_path.write_text(json.dumps(config), encoding="ascii")
            second_usec = str(int(second_start.astimezone(timezone.utc).timestamp() * 1_000_000))
            journal.write_text(
                original_journal.replace(invocation, second_invocation).replace(timestamp_usec, second_usec),
                encoding="ascii",
            )
            before_failed_reset = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            with self.assertRaises(systemd_state.StateError):
                systemd_state.reset_cloudflare_pending_guard(
                    receipt, transaction=self.shared / STATE_NAME, app_dir=self.app_dir,
                    helper_release=current, attempt_nonce="c" * 64,
                    expected_event_id=event_id_for(second_invocation),
                )
            after_failed_reset = [
                tuple(command[:2]) for command in
                (json.loads(line) for line in command_log.read_text().splitlines())
                if command and command[0] in {"stop", "start", "reset-failed"}
            ]
            self.assertEqual(
                after_failed_reset[len(before_failed_reset):],
                [
                    ("stop", systemd_state.CLOUDFLARE_TIMER),
                    ("reset-failed", systemd_state.CLOUDFLARE_UNIT),
                    ("start", systemd_state.CLOUDFLARE_TIMER),
                ],
            )
            self.assertFalse(any(action == ("start", systemd_state.CLOUDFLARE_UNIT) for action in after_failed_reset))
            self.assertTrue(receipt.is_file())
            self.assertTrue((self.shared / STATE_NAME).is_file())
        finally:
            systemd_state.SYSTEMCTL_PATH = original_systemctl_path
            systemd_state.JOURNALCTL_PATH = original_journal_path

    def test_immutable_recovery_wrapper_rejects_tampered_generation_before_transaction(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(None, None, "tampered-generation")
        systemctl, systemctl_log = self.write_failing_systemctl("tampered-generation")
        transaction_helper = generation / "platform_release_transaction.py"
        transaction_helper.chmod(0o644)
        transaction_helper.write_bytes(transaction_helper.read_bytes() + b"\n# tampered\n")
        transaction_helper.chmod(0o444)

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).is_file())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertFalse((self.app_dir / "current").exists())
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertTrue(candidate.is_dir())

    def test_immutable_recovery_wrapper_rollback_pre_runtime_uses_bound_receipt(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-wrapper-current")
        previous = self.add_release("rollback-wrapper-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.create_wrapper_rollback_transaction(current, previous, phase="prepared")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "active",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_immutable_recovery_wrapper_rollback_rejects_receipt_identity_before_systemd(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-receipt-current")
        previous = self.add_release("rollback-receipt-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.create_wrapper_rollback_transaction(current, previous, phase="prepared")
        systemctl = self.write_stateful_systemctl({})
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        payload = json.loads(receipt.read_text())
        payload["operation_id"] = "0" * 32
        receipt.write_text(json.dumps(payload, sort_keys=True) + "\n")
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(receipt.exists())
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_immutable_recovery_wrapper_rollback_cache_purged_resumes_immutable_runtime(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-restart-current")
        previous = self.add_release("rollback-restart-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        runtime_args_log = self.root / "rollback-restart-runtime-args.txt"
        runtime_installer = previous / "tools/platform_live_qa_runtime_install.py"
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_fake_venv(self.shared / "venv", marker="rollback")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        fake_python = self.shared / "venv/bin/python"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"if [[ \"${{2:-}}\" == {str(runtime_installer)!r} ]]; then\n"
            f"  printf '%s\\n' \"$2\" > {str(runtime_args_log)!r}\n"
            "  shift 2\n"
            f"  printf '%s\\n' \"$*\" >> {str(runtime_args_log)!r}\n"
            "fi\n"
            "case \"${1:-}\" in\n"
            "  *platform_install_nginx.py)\n"
            "    if [[ \"${2:-}\" == \"--apply\" ]]; then\n"
            "      printf '%s\\n' \"$PLATFORM_TEST_NGINX_LABEL\" > \"$PLATFORM_TEST_NGINX_STATE\"\n"
            "    fi\n"
            "    ;;\n"
            "esac\n"
        )
        fake_python.chmod(0o755)
        self.create_wrapper_rollback_transaction(current, previous, phase="rollback-cache-purged")
        self.switch_pointer("current", previous)
        self.switch_pointer("previous", current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "active",
                "deadlock-web.service": "inactive",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env={
                **self.runtime_env(label="previous"),
                "PLATFORM_RUNTIME_PRIVATE_CAPTURE": "1",
                "PLATFORM_RUNTIME_PRIVATE_ROUTE": "recover_pending",
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("RUNTIME_PRIVATE_CONTEXT", result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        runtime_args = runtime_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(runtime_args[0], str(runtime_installer))
        self.assertIn("reconcile", runtime_args[1])
        self.assertNotIn("--rollback-reconcile-transaction", runtime_args[1])

    def test_immutable_recovery_rejects_legacy_restart_pending_without_purge_proof(
        self,
    ) -> None:
        # Exercise the only supported proof writer through the transaction CLI:
        # the fixed v1 purge child succeeds, then the receipt can advance and
        # complete.  Invalid bindings remain read-only failures.
        proof_current = self.add_release("purge-proof-current")
        proof_previous = self.add_release("purge-proof-previous")
        self.add_runtime_stubs(proof_current)
        self.add_runtime_stubs(proof_previous)
        (self.app_dir / "current").symlink_to(proof_current)
        (self.app_dir / "previous").symlink_to(proof_previous)
        venv = self.shared / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").symlink_to("/usr/bin/python3.12")
        safe_env = proof_current / "tools/platform_safe_env_exec.py"
        safe_env.write_text(
            "import base64\n"
            "value = base64.b64encode(b'redis://127.0.0.1:6379/15').decode('ascii')\n"
            "print('PLATFORM_REDIS_URL\\t' + value)\n",
            encoding="ascii",
        )
        safe_env.chmod(0o555)
        services = proof_current / "apps/platform_api/app/services"
        services.mkdir(parents=True)
        for package in (
            proof_current / "apps/platform_api/app/__init__.py",
            services.parent / "__init__.py",
            services / "__init__.py",
        ):
            package.write_text("", encoding="ascii")
            package.chmod(0o444)
        (services / "tournament_profile_access.py").write_text(
            "async def purge_legacy_profile_access_cache():\n"
            "    return 0\n",
            encoding="ascii",
        )
        (services / "tournament_profile_access.py").chmod(0o444)
        self.create_wrapper_rollback_transaction(
            proof_current, proof_previous, phase="rollback-runtime-pending"
        )
        transaction_state = self.shared / STATE_NAME
        purge = self.run_transaction("purge-legacy-profile-access-cache", check=False)
        self.assertEqual(purge.returncode, 0, purge.stderr)
        proof_bytes = transaction_state.read_bytes()
        proof_record = json.loads(proof_bytes)
        purge_proof = proof_record["profile_access_cache_purge_proof"]
        self.assertEqual(purge_proof["operation"], "rollback")
        self.assertEqual(purge_proof["operation_id"], proof_record["operation_id"])
        self.assertEqual(purge_proof["source_role"], "current-before")
        self.assertEqual(purge_proof["source_identity"], proof_record["current_before_identity"])
        self.assertEqual(purge_proof["scope"], "tournament-profile-access-v1")

        for mutate_proof in (
            lambda value: value.update(scope="tournament-profile-access-v2"),
            lambda value: value.update(operation="install"),
            lambda value: value.update(
                source_identity={
                    "dev": value["source_identity"]["dev"],
                    "ino": value["source_identity"]["ino"] + 1,
                }
            ),
        ):
            changed = json.loads(proof_bytes)
            mutate_proof(changed["profile_access_cache_purge_proof"])
            transaction_state.write_text(
                json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            transaction_state.chmod(0o600)
            invalid = self.run_transaction("status", "--json", check=False)
            self.assertNotEqual(invalid.returncode, 0)
            transaction_state.write_bytes(proof_bytes)
            transaction_state.chmod(0o600)

        self.run_transaction(
            "phase",
            "--expected",
            "rollback-runtime-pending",
            "--phase",
            "rollback-cache-purged",
        )
        self.run_transaction(
            "phase",
            "--expected",
            "rollback-cache-purged",
            "--phase",
            "services-restarted",
        )
        self.run_transaction(
            "phase", "--expected", "services-restarted", "--phase", "smoke-passed"
        )
        self.run_transaction(
            "phase",
            "--expected",
            "smoke-passed",
            "--phase",
            "rollback-runtime-applied",
        )
        self.switch_pointer("current", proof_previous)
        self.switch_pointer("previous", proof_current)
        self.run_transaction("complete", "--retain-receipt")
        self.assertEqual(
            json.loads(transaction_state.read_text())["profile_access_cache_purge_proof"],
            purge_proof,
        )
        self.run_transaction("complete")
        self.assertFalse(transaction_state.exists())
        (self.app_dir / "current").unlink()
        (self.app_dir / "previous").unlink()
        shutil.rmtree(venv)

        generation = self.install_recovery_generation()
        current = self.add_release("legacy-restart-current")
        previous = self.add_release("legacy-restart-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        runtime_args_log = self.root / "legacy-restart-runtime-args.txt"
        self.create_wrapper_rollback_transaction(
            current, previous, phase="rollback-runtime-pending"
        )
        transaction_state = self.shared / STATE_NAME
        runtime_pending_bytes = transaction_state.read_bytes()
        direct_phase = self.run_script(
            TRANSACTION_TOOL,
            "phase",
            "--state",
            str(transaction_state),
            "--expected",
            "rollback-runtime-pending",
            "--phase",
            "rollback-cache-purged",
            check=False,
        )
        self.assertNotEqual(direct_phase.returncode, 0)
        self.assertEqual(transaction_state.read_bytes(), runtime_pending_bytes)
        legacy_record = json.loads(transaction_state.read_text(encoding="utf-8"))
        legacy_record["phase"] = "restart-pending"
        transaction_state.write_text(
            json.dumps(legacy_record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        transaction_state.chmod(0o600)
        legacy_state_bytes = transaction_state.read_bytes()
        direct_completion = self.run_script(
            TRANSACTION_TOOL,
            "complete",
            "--state",
            str(transaction_state),
            check=False,
        )
        self.assertNotEqual(direct_completion.returncode, 0)
        self.assertEqual(transaction_state.read_bytes(), legacy_state_bytes)
        self.switch_pointer("current", previous)
        self.switch_pointer("previous", current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "active",
                "deadlock-web.service": "inactive",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(transaction_state),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(transaction_state.is_file())
        self.assertTrue(receipt.is_file())
        self.assertEqual(json.loads(transaction_state.read_text())["phase"], "restart-pending")
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertEqual((self.root / "systemctl.log").read_text(), "")
        self.assertFalse(runtime_args_log.exists())

        applied_record = json.loads(transaction_state.read_text(encoding="utf-8"))
        applied_record["phase"] = "rollback-runtime-applied"
        transaction_state.write_text(
            json.dumps(applied_record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        transaction_state.chmod(0o600)
        legacy_applied_bytes = transaction_state.read_bytes()
        direct_completion = self.run_script(
            TRANSACTION_TOOL,
            "complete",
            "--state",
            str(transaction_state),
            check=False,
        )
        self.assertNotEqual(direct_completion.returncode, 0)
        self.assertEqual(transaction_state.read_bytes(), legacy_applied_bytes)

    def test_immutable_recovery_wrapper_rollback_runtime_pending_restores_original_state(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-runtime-current")
        previous = self.add_release("rollback-runtime-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_fake_venv(self.shared / "venv", marker="rollback")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_wrapper_rollback_transaction(
            current, previous, phase="rollback-runtime-pending"
        )
        self.switch_pointer("current", previous)
        self.switch_pointer("previous", current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "active",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env={
                **self.runtime_env(label="current"),
                "PLATFORM_TEST_NGINX_LABEL": "current",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_immutable_recovery_wrapper_replays_runtime_after_filesystem_recovery_crash(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-window-current")
        previous = self.add_release("rollback-window-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(previous)
        (self.app_dir / "previous").symlink_to(current)
        self.add_fake_venv(self.shared / "venv", marker="rollback")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_wrapper_rollback_transaction(
            current, previous, phase="rollback-runtime-pending"
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "active",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")
        interrupted = self.root / "filesystem-recovery-kill.sh"
        interrupted.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"/usr/bin/python3 -I {shlex.quote(str(TRANSACTION_TOOL))} recover --retain --runtime-pending --state {shlex.quote(str(self.shared / STATE_NAME))}\n"
            "/bin/kill -KILL \"$$\"\n",
            encoding="utf-8",
        )
        interrupted.chmod(0o755)
        result = self.run_script(interrupted, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "filesystem-restored-runtime-pending")
        self.assertTrue(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env={
                **self.runtime_env(label="current"),
                "PLATFORM_TEST_NGINX_LABEL": "current",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_immutable_recovery_wrapper_early_rollback_without_receipt_is_filesystem_only(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        for phase in ("prepared", "pointers-switched"):
            with self.subTest(phase=phase):
                for pointer in (self.app_dir / "current", self.app_dir / "previous"):
                    pointer.unlink(missing_ok=True)
                current = self.add_release(f"rollback-no-receipt-{phase}-current")
                previous = self.add_release(f"rollback-no-receipt-{phase}-previous")
                self.add_runtime_stubs(current)
                self.add_runtime_stubs(previous)
                (self.app_dir / "current").symlink_to(current)
                (self.app_dir / "previous").symlink_to(previous)
                self.create_wrapper_rollback_transaction(current, previous, phase=phase)
                systemctl, systemctl_log = self.write_failing_systemctl(f"rollback-no-receipt-{phase}")

                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(systemctl_log.exists())
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse((self.shared / ".release-systemd-state.json").exists())

    def test_immutable_recovery_wrapper_late_runtime_phases_use_immutable_restore(self) -> None:
        generation = self.install_recovery_generation()
        for phase in ("services-restarted", "smoke-passed"):
            with self.subTest(phase=phase):
                for pointer in (self.app_dir / "current", self.app_dir / "previous"):
                    pointer.unlink(missing_ok=True)
                shutil.rmtree(self.shared / "venv", ignore_errors=True)
                current = self.add_release(f"rollback-{phase}-current")
                previous = self.add_release(f"rollback-{phase}-previous")
                self.add_runtime_stubs(current)
                self.add_runtime_stubs(previous)
                (self.app_dir / "current").symlink_to(current)
                (self.app_dir / "previous").symlink_to(previous)
                self.add_fake_venv(self.shared / "venv", marker="rollback")
                self.write_fake_python(self.shared / "venv" / "bin" / "python")
                self.create_wrapper_rollback_transaction(current, previous, phase=phase)
                self.switch_pointer("current", previous)
                self.switch_pointer("previous", current)
                systemctl = self.write_stateful_systemctl(
                    {
                        "deadlock-api.service": "active",
                        "deadlock-worker.service": "inactive",
                        "deadlock-web.service": "active",
                    }
                )
                receipt = self.shared / ".release-systemd-state.json"
                self.run_script(
                    SYSTEMD_STATE_TOOL,
                    "capture-transaction",
                    "--state",
                    str(receipt),
                    "--transaction",
                    str(self.shared / STATE_NAME),
                    "--app-dir",
                    str(self.app_dir),
                    "--helper-release",
                    str(previous),
                    "--require-helper-manifest",
                    "--systemctl",
                    str(systemctl),
                )
                (self.root / "systemctl.log").write_text("")
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    env={
                        **self.runtime_env(label="current"),
                        "PLATFORM_TEST_NGINX_LABEL": "current",
                    },
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse(receipt.exists())

    def test_immutable_recovery_wrapper_post_clear_runtime_and_restored_retries_are_idempotent(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        for phase in ("rollback-runtime-applied", "recovery-restored"):
            with self.subTest(phase=phase):
                for pointer in (self.app_dir / "current", self.app_dir / "previous"):
                    pointer.unlink(missing_ok=True)
                current = self.add_release(f"rollback-post-clear-{phase}-current")
                previous = self.add_release(f"rollback-post-clear-{phase}-previous")
                self.add_runtime_stubs(current)
                self.add_runtime_stubs(previous)
                (self.app_dir / "current").symlink_to(previous)
                (self.app_dir / "previous").symlink_to(current)
                transaction_phase = (
                    "rollback-runtime-applied" if phase == "recovery-restored" else phase
                )
                self.create_wrapper_rollback_transaction(
                    current, previous, phase=transaction_phase
                )
                systemctl, systemctl_log = self.write_failing_systemctl(f"rollback-post-clear-{phase}")
                receipt = self.shared / ".release-systemd-state.json"
                if phase == "recovery-restored":
                    self.run_transaction("recover", "--retain")
                    self.assertEqual(self.state_phase(), "recovery-restored")
                receipt.unlink(missing_ok=True)
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(systemctl_log.exists())
                self.assertFalse((self.shared / STATE_NAME).exists())

    def test_resume_activation_committed_cleans_receipt(self) -> None:
        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="activation-committed")
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)

        resume = self.copy_deploy_script_with_fault(
            "deploy-resume-activation-committed.sh",
            None,
            None,
            self.write_fake_systemctl(),
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertNotEqual(previous, candidate)

    def test_retained_recovery_retry_preserves_mixed_state_until_final_cleanup(self) -> None:
        """A post-pointer failure must leave both receipts retryable."""

        current, previous, candidate = self.prepare_install_state(
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.prepare_wrapper_cache_purge(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "inactive",
                "deadlock-worker.service": "active",
                "deadlock-web.service": "inactive",
                "deadlock-maintenance.timer": "active",
                "deadlock-logrotate.timer": "active",
                "deadlock-offsite-backup.timer": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-health-monitor.timer": "active",
            }
        )
        state = self.shared / STATE_NAME
        systemd_receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(systemd_receipt),
            "--transaction",
            str(state),
            "--require-helper-manifest",
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        enabled_path = self.root / "systemd-enabled.json"
        interrupted_enabled = json.loads(enabled_path.read_text())
        for unit, value in interrupted_enabled.items():
            if value != "static":
                interrupted_enabled[unit] = "enabled"
        enabled_path.write_text(json.dumps(interrupted_enabled, sort_keys=True))

        failed = self.root / "recover-after-pointer-failure.sh"
        failed.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"{shlex.quote(str(TRANSACTION_TOOL))} recover --retain --state {shlex.quote(str(state))}\n"
            "exit 42\n",
            encoding="utf-8",
        )
        failed.chmod(0o755)
        result = self.run_script(failed, check=False)
        self.assertEqual(result.returncode, 42)
        self.assertTrue(state.is_file())
        self.assertTrue(systemd_receipt.is_file())
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertTrue(candidate.exists())

        # A second attempt restores the recorded mixed active/enabled state,
        # verifies it, and only then performs final receipt/transaction cleanup.
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "restore",
            "--state",
            str(systemd_receipt),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "verify",
            "--state",
            str(systemd_receipt),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "clear",
            "--state",
            str(systemd_receipt),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        self.run_transaction("complete-recovery")

        self.assertFalse(systemd_receipt.exists())
        self.assertFalse(state.exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api.service"], "active")
        self.assertEqual(final_state["deadlock-worker.service"], "inactive")
        self.assertEqual(final_state["deadlock-web.service"], "active")
        final_enabled = json.loads(enabled_path.read_text())
        self.assertEqual(final_enabled["deadlock-api.service"], "enabled")
        self.assertEqual(final_enabled["deadlock-offsite-backup.timer"], "disabled")

    def test_abort_after_candidate_nginx_apply_restores_previous_nginx(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="services-restarted")
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)
        nginx_state = self.root / "nginx.state"

        interrupted = self.copy_script_with_replacement(
            DEPLOY_SCRIPT,
            "deploy-after-nginx-apply-kill.sh",
            '  set_phase nginx-pending nginx-applied\n',
            '  /bin/kill -KILL "$$"\n  set_phase nginx-pending nginx-applied\n',
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "candidate",
            },
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(nginx_state.exists(), result.stderr)
        self.assertEqual(nginx_state.read_text(), "candidate\n")
        self.assertEqual(self.state_phase(), "nginx-pending")

        self.prepare_wrapper_cache_purge(candidate)
        abort = self.copy_abort_script("abort-after-nginx-apply.sh")
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(nginx_state.read_text(), "previous\n")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

    def _assert_runtime_restore_public_readiness_retry_cases(self) -> None:
        app_dir = self.root / "readiness-app"
        release = app_dir / "releases" / "readiness-release"
        shared = app_dir / "shared"
        tools = release / "tools"
        tools.mkdir(parents=True)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "e" * 40}) + "\n", encoding="ascii"
        )
        release_manifest = (release / "RELEASE.json").read_bytes()
        (app_dir / "shared").mkdir(parents=True)
        (shared / "venv/bin").mkdir(parents=True)
        (shared / "venv/bin/python").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="ascii"
        )
        (shared / "venv/bin/python").chmod(0o755)
        units_tool = tools / "platform_install_systemd_units.sh"
        units_tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="ascii")
        units_tool.chmod(0o755)
        (tools / "platform_install_nginx.py").write_text("pass\n", encoding="ascii")
        (tools / "platform_live_qa_runtime_install.py").write_text(
            "pass\n", encoding="ascii"
        )
        (tools / "platform_deploy_smoke.py").write_text("pass\n", encoding="ascii")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
            }
        )
        curl = self.root / "curl-fixture"
        curl_calls = self.root / "curl-calls"
        api_count = self.root / "api-curl-count"
        public_count = self.root / "public-curl-count"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\t%s\\n' \"$*\" \"${{!#}}\" >> {str(curl_calls)!r}\n"
            "url=\"${!#}\"\n"
            "if [[ \"$url\" == 'http://127.0.0.1:8010/api/v1/health/ready' ]]; then\n"
            f"  target=api; count_file={str(api_count)!r}\n"
            "elif [[ \"$url\" == 'http://127.0.0.1:3000/' ]]; then\n"
            f"  target=web; count_file={str(public_count)!r}\n"
            "else exit 64; fi\n"
            "if [[ \"${PLATFORM_TEST_READINESS_TARGET:-}\" != \"$target\" ]]; then exit 0; fi\n"
            "count=$(cat \"$count_file\" 2>/dev/null || printf '0')\n"
            "count=$((count + 1))\n"
            "printf '%s\\n' \"$count\" > \"$count_file\"\n"
            "case \"${PLATFORM_TEST_READINESS_MODE:-transient}\" in\n"
            "  transient) if [[ \"$count\" -lt 3 ]]; then printf '%s\\n' 'curl: (7) connection refused' >&2; exit 7; fi ;;\n"
            "  refused) printf '%s\\n' 'curl: (7) connection refused' >&2; exit 7 ;;\n"
            "  invalid) printf '%s\\n' 'curl: (22) HTTP response code said error' >&2; exit 22 ;;\n"
            "  *) exit 64 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="ascii",
        )
        curl.chmod(0o755)
        sleep = self.root / "sleep-fixture"
        sleep_calls = self.root / "sleep-calls"
        sleep.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(sleep_calls)!r}\n",
            encoding="ascii",
        )
        sleep.chmod(0o755)

        for label, target, mode, expected_rc, expected_attempts in (
            ("api-transient", "api", "transient", 0, 3),
            ("api-permanent-refusal", "api", "refused", 7, 30),
            ("api-invalid-health", "api", "invalid", 22, 30),
            ("web-transient", "web", "transient", 0, 3),
            ("web-permanent-refusal", "web", "refused", 7, 30),
            ("web-invalid-health", "web", "invalid", 22, 30),
        ):
            with self.subTest(readiness=label):
                curl_calls.unlink(missing_ok=True)
                api_count.unlink(missing_ok=True)
                public_count.unlink(missing_ok=True)
                sleep_calls.unlink(missing_ok=True)
                (self.root / "systemctl.log").write_text("")
                runtime = self.root / f"runtime-{label}" / "platform_release_restore_runtime.sh"
                runtime.parent.mkdir()
                script = RUNTIME_RESTORE_SCRIPT.read_text(encoding="utf-8")
                self.assertIn("/usr/bin/curl", script)
                self.assertIn("sleep 1", script)
                script = script.replace("/usr/bin/curl", str(curl))
                script = script.replace("sleep 1", f"{sleep} 1")
                runtime.write_text(script, encoding="utf-8")
                runtime.chmod(0o755)
                self.install_test_lock_helper(runtime.parent / "platform_release_lock.sh")
                shutil.copy2(
                    SYSTEMD_STATE_TOOL,
                    runtime.parent / "platform_release_systemd_state.py",
                )
                (runtime.parent / "platform_release_systemd_state.py").chmod(0o755)

                result = self.run_script(
                    runtime,
                    "--restart-only",
                    "--skip-smoke",
                    "--app-dir",
                    str(app_dir),
                    "--release",
                    str(release),
                    "--systemctl",
                    str(systemctl),
                    env={
                        **self.runtime_env(label="readiness"),
                        "PLATFORM_RUNTIME_PRIVATE_CAPTURE": "1",
                        "PLATFORM_RUNTIME_PRIVATE_ROUTE": "recover_pending",
                        "PLATFORM_TEST_READINESS_MODE": mode,
                        "PLATFORM_TEST_READINESS_TARGET": target,
                    },
                    check=False,
                )

                self.assertEqual(result.returncode, expected_rc, result.stderr)
                curl_rows = [
                    line.split("\t", 1) for line in curl_calls.read_text().splitlines()
                ]
                api_rows = [
                    row
                    for row in curl_rows
                    if row[-1] == "http://127.0.0.1:8010/api/v1/health/ready"
                ]
                public_rows = [
                    row for row in curl_rows if row[-1] == "http://127.0.0.1:3000/"
                ]
                expected_api_attempts = expected_attempts if target == "api" else 1
                expected_web_attempts = (
                    expected_attempts
                    if target == "web"
                    else (0 if target == "api" and expected_rc != 0 else 1)
                )
                self.assertEqual(len(api_rows), expected_api_attempts)
                self.assertEqual(len(public_rows), expected_web_attempts)
                for row in api_rows:
                    self.assertEqual(
                        row[0],
                        "--fail --silent --show-error --max-time 5 "
                        "http://127.0.0.1:8010/api/v1/health/ready",
                    )
                for row in public_rows:
                    self.assertEqual(
                        row[0],
                        "--fail --silent --show-error --max-time 5 http://127.0.0.1:3000/",
                    )
                sleep_rows = (
                    sleep_calls.read_text().splitlines() if sleep_calls.exists() else []
                )
                expected_sleeps = (
                    expected_attempts - 1 if expected_rc == 0 else expected_attempts
                )
                self.assertEqual(len(sleep_rows), expected_sleeps)
                self.assertEqual(set(sleep_rows), {"1"})
                markers = [
                    line.removeprefix("RUNTIME_PRIVATE_CONTEXT ")
                    for line in result.stderr.splitlines()
                    if line.startswith("RUNTIME_PRIVATE_CONTEXT ")
                ]
                if expected_rc == 0:
                    self.assertEqual(markers, [])
                    self.assertIn("status=restored", result.stdout)
                    self.assertNotIn("connection refused", result.stderr)
                else:
                    self.assertEqual(len(markers), 1)
                    marker = json.loads(markers[0])
                    expected_component = (
                        "readiness_internal" if target == "api" else "readiness_public"
                    )
                    self.assertEqual(marker["component"], expected_component)
                    self.assertEqual(marker["returncode"], expected_rc)
                    self.assertIn("status=failed", result.stderr)
                    self.assertNotIn("connection refused", result.stdout)
                self.assertEqual((release / "RELEASE.json").read_bytes(), release_manifest)
                systemctl_calls = (self.root / "systemctl.log").read_text().splitlines()
                self.assertEqual(
                    [line for line in systemctl_calls if line.startswith("restart ")],
                    [
                        "restart deadlock-api",
                        "restart deadlock-worker",
                        "restart deadlock-web",
                    ],
                )
                self.assertFalse(
                    any(line.startswith(("start ", "stop ")) for line in systemctl_calls)
                )

    def test_rollback_restores_previous_units_and_nginx_before_completion(self) -> None:
        current = self.add_release("current")
        previous = self.add_release("previous")
        (current / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "d" * 40}) + "\n", encoding="ascii"
        )
        (previous / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "6343099bb7686671bdef49d0c4ecd10f21ef19d2"})
            + "\n",
            encoding="ascii",
        )
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        self.prepare_rollback_venv(current)
        units_state = self.root / "units.state"
        nginx_state = self.root / "nginx.state"
        units_state.write_text("current\n")
        nginx_state.write_text("current\n")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        runtime_restore = self.write_test_runtime_restore()
        rollback = self.copy_rollback_with_systemctl(systemctl, runtime_restore)

        result = self.run_script(
            rollback,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env={
                "PLATFORM_TEST_UNITS_STATE": str(units_state),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(units_state.read_text(), "previous\n")
        self.assertEqual(nginx_state.read_text(), "previous\n")
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertFalse((self.shared / STATE_NAME).exists())
        recorded_restore_calls = [
            shlex.split(line)
            for line in runtime_restore.with_suffix(".args").read_text(encoding="ascii").splitlines()
        ]
        expected_transaction = str(self.shared / STATE_NAME)
        self.assertTrue(
            any(
                "--release" in args
                and args[args.index("--release") + 1] == str(previous)
                and "--transaction" in args
                and args[args.index("--transaction") + 1] == expected_transaction
                and "--rollback-reconcile-transaction" in args
                and args[args.index("--rollback-reconcile-transaction") + 1]
                == expected_transaction
                and "--live-qa-runtime-installer" in args
                and args[args.index("--live-qa-runtime-installer") + 1]
                == str(current / "tools/platform_live_qa_runtime_install.py")
                for args in recorded_restore_calls
            ),
            recorded_restore_calls,
        )
        self._assert_runtime_restore_public_readiness_retry_cases()

    def test_rollback_crash_after_systemd_clear_retries_transaction_completion(self) -> None:
        current, previous = self.prepare_rollback_state()
        units_state = self.root / "units.state"
        nginx_state = self.root / "nginx.state"
        units_state.write_text("current\n")
        nginx_state.write_text("current\n")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        rollback = self.copy_rollback_with_systemctl(systemctl)
        clear_block = (
            "  clear_rollback_systemd_state\n"
            "  /usr/bin/python3 -I \"$TRANSACTION_TOOL\" complete --state \"$TRANSACTION_STATE\"\n"
        )
        self.assertIn(clear_block, rollback.read_text(encoding="utf-8"))
        interrupted = self.root / "rollback-after-systemd-clear.sh"
        interrupted.write_text(
            rollback.read_text(encoding="utf-8").replace(
                clear_block,
                "  clear_rollback_systemd_state\n"
                "  /bin/kill -KILL \"$$\"\n"
                "  /usr/bin/python3 -I \"$TRANSACTION_TOOL\" complete --state \"$TRANSACTION_STATE\"\n",
                1,
            ),
            encoding="utf-8",
        )
        interrupted.chmod(0o755)
        result = self.run_script(
            interrupted,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env=self.runtime_env(label="previous"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertEqual(self.state_phase(), "rollback-runtime-applied")
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

        retry = self.copy_rollback_with_systemctl(systemctl)
        result = self.run_script(
            retry,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())

    def test_rollback_recovery_receipt_retry_after_retained_cleanup_crash(self) -> None:
        current, previous = self.prepare_rollback_state()
        runtime = self.copy_runtime_with_fault(
            "runtime-before-recovery-retry.sh",
            '  PLATFORM_APP_DIR="$APP_DIR" run_private_command systemd_units "$UNITS_TOOL"\n',
            '  PLATFORM_APP_DIR="$APP_DIR" run_private_command systemd_units "$UNITS_TOOL"\n'
            '  /bin/kill -KILL "$PPID"\n',
        )
        interrupted = self.copy_rollback_with_runtime(
            "rollback-before-recovery-retry.sh", runtime
        )
        result = self.run_script(
            interrupted,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env=self.runtime_env(label="previous"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "rollback-runtime-pending")
        systemd_receipt = self.shared / ".release-systemd-state.json"
        self.assertTrue(systemd_receipt.is_file())

        recovery_runtime = self.write_test_runtime_restore()
        recovery = self.copy_rollback_with_runtime(
            "rollback-retained-cleanup-kill.sh", recovery_runtime
        )
        retained_cleanup = (
            "  /usr/bin/python3 -I \"$TRANSACTION_TOOL\" complete-recovery \\\n"
            "    --retain-receipt \\\n"
            "    --state \"$TRANSACTION_STATE\"\n"
        )
        recovery_text = recovery.read_text(encoding="utf-8")
        self.assertIn(retained_cleanup, recovery_text)
        recovery.write_text(
            recovery_text.replace(
                retained_cleanup,
                retained_cleanup + '  /bin/kill -KILL "$$"\n',
                1,
            ),
            encoding="utf-8",
        )
        recovery.chmod(0o755)
        result = self.run_script(
            recovery,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="current"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertTrue(systemd_receipt.is_file())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

        retry = self.copy_rollback_with_runtime(
            "rollback-retained-cleanup-retry.sh", recovery_runtime
        )
        result = self.run_script(
            retry,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="current"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(systemd_receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_deploy_faults_after_units_and_restart_resume_to_commit(self) -> None:
        for label, needle in (
            (
                "units",
                "  PLATFORM_ENABLE_SYSTEMD_UNITS=0 run_candidate tools/platform_install_systemd_units.sh\n",
            ),
            (
                "restart",
                '  set_phase activation-pending services-restarted\n',
            ),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                current, _previous, candidate = self.prepare_deploy_state(
                    "activation-pending"
                )
                systemctl = self.write_fake_systemctl()
                interrupted = self.copy_deploy_script_with_fault(
                    f"deploy-{label}-kill.sh",
                    needle,
                    f'  /bin/kill -KILL "$$"\n{needle}',
                    systemctl,
                )
                result = self.run_script(
                    interrupted,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="candidate"),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), "activation-pending")

                resume = self.copy_deploy_script_with_fault(
                    f"deploy-{label}-resume.sh", None, None, systemctl
                )
                result = self.run_script(
                    resume,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="candidate"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)
                self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_deploy_fault_after_smoke_resumes_and_commits(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("nginx-applied")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-smoke-kill.sh",
            '  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n',
            '  /bin/kill -KILL "$$"\n  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "nginx-applied")
        resume = self.copy_deploy_script_with_fault(
            "deploy-smoke-resume.sh", None, None, systemctl
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_deploy_fault_after_activation_commit_resumes_cleanup(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("smoke-passed")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-activation-commit-kill.sh",
            '  set_phase smoke-passed activation-committed\n',
            '  set_phase smoke-passed activation-committed\n  /bin/kill -KILL "$$"\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "activation-committed")
        resume = self.copy_deploy_script_with_fault(
            "deploy-resume-after-activation-commit.sh",
            None,
            None,
            systemctl,
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_fault_after_final_receipt_cleanup_leaves_committed_state(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("activation-committed")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-receipt-cleanup-kill.sh",
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"\n',
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"\n'
            '  /bin/kill -KILL "$$"\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_rollback_faults_in_runtime_restore_recover_original_state(self) -> None:
        for label, needle in (
            (
                "units",
                '  PLATFORM_APP_DIR="$APP_DIR" run_private_command systemd_units "$UNITS_TOOL"\n',
            ),
            (
                "nginx",
                '    "$SHARED_VENV/bin/python" "$NGINX_TOOL" --apply --reload --json\n',
            ),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                current, previous = self.prepare_rollback_state()
                runtime = self.copy_runtime_with_fault(
                    f"runtime-{label}-kill.sh",
                    needle,
                    f'{needle}  /bin/kill -KILL "$PPID"\n',
                )
                interrupted = self.copy_rollback_with_runtime(
                    f"rollback-{label}-kill.sh", runtime
                )
                result = self.run_script(
                    interrupted,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    env=self.runtime_env(label="previous"),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), "rollback-runtime-pending")
                recovery_runtime = self.write_test_runtime_restore()
                recovery = self.copy_rollback_with_runtime(
                    f"rollback-{label}-recover.sh", recovery_runtime
                )
                result = self.run_script(
                    recovery,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="current"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), current)
                self.assertEqual((self.app_dir / "previous").resolve(), previous)
                self.assertEqual(
                    (self.shared / "venv" / "deps-version").read_text(), "new\n"
                )

    def test_install_pointer_gap_durably_recovers_before_current_switch(self) -> None:
        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="migration-applied")
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-between-pointer-switches-kill.sh",
            "      phase=previous-switched\n",
            "      phase=previous-switched\n      /bin/kill -KILL \"$$\"\n",
            self.write_fake_systemctl(),
        )

        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "previous-switched")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

        self.run_transaction("recover", "--retain")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.run_transaction("complete-recovery")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_install_pointer_gap_durably_recovers_after_current_switch(self) -> None:
        """A SIGKILL after the second symlink still retains a recoverable receipt."""

        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="migration-applied")
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-after-current-pointer-kill.sh",
            "    set_phase previous-switched current-switched || exit 1\n",
            '    /bin/kill -KILL "$$"\n'
            "    set_phase previous-switched current-switched || exit 1\n",
            self.write_fake_systemctl(),
        )

        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "previous-switched")
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

        self.run_transaction("recover", "--retain")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.run_transaction("complete-recovery")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_pointer_switch_crash_recovers_through_old_current_helper(self) -> None:
        current, previous = self.prepare_rollback_state()
        legacy_helper = previous / "tools/platform_release_rollback.sh"
        legacy_helper.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' legacy-helper-used > \"$PLATFORM_LEGACY_HELPER_STATE\"\n"
            "exit 77\n"
        )
        legacy_helper.chmod(0o755)
        legacy_state = self.root / "legacy-helper.state"
        runtime = self.copy_runtime_with_fault(
            "runtime-after-pointer-switch-kill.sh",
            '  PLATFORM_APP_DIR="$APP_DIR" run_private_command systemd_units "$UNITS_TOOL"\n',
            '  PLATFORM_APP_DIR="$APP_DIR" run_private_command systemd_units "$UNITS_TOOL"\n'
            '  /bin/kill -KILL "$PPID"\n',
        )
        interrupted = self.copy_rollback_with_runtime(
            "rollback-after-pointer-switch-kill.sh", runtime
        )

        result = self.run_script(
            interrupted,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env={
                **self.runtime_env(label="previous"),
                "PLATFORM_LEGACY_HELPER_STATE": str(legacy_state),
            },
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "rollback-runtime-pending")
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertIn(
            "RECOVERY_DIR=\"$SHARED_DIR/.release-recovery\"",
            (previous / "tools/platform_release_rollback.sh").read_text(),
        )
        # The production recovery bundle uses the fixed systemctl path.  This
        # test substitutes the systemd boundary inside the private fixture so
        # the old-current recovery path exercises the same receipt checks
        # without touching the host service manager.
        recovery_bundle = self.shared / ".release-recovery/platform_release_rollback.sh"
        recovery_bundle.write_text(
            recovery_bundle.read_text().replace(
                "/usr/bin/systemctl", str(self.root / "systemctl")
            )
        )
        recovery_bundle.chmod(0o755)
        recovery_runtime = self.write_test_runtime_restore()
        shutil.copy2(
            recovery_runtime,
            self.shared / ".release-recovery/platform_release_restore_runtime.sh",
        )

        recovery = self.app_dir / "current/tools/platform_release_rollback.sh"
        result = self.run_script(
            recovery,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="current"),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertFalse(legacy_state.exists())
        self.assertEqual(
            (self.shared / "venv" / "deps-version").read_text(), "new\n"
        )

    def test_migration_failure_restores_only_pre_active_services_and_retains_receipt(
        self,
    ) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        (current / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "6343099bb7686671bdef49d0c4ecd10f21ef19d2"})
            + "\n",
            encoding="ascii",
        )
        (candidate / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "d" * 40}) + "\n",
            encoding="ascii",
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        # The child must run through the recorded venv launcher so Python
        # keeps the venv prefix under -I, while that launcher resolves to the
        # fixed system interpreter accepted by the transaction guard.
        shared_python = self.shared / "venv/bin/python"
        shared_python.unlink()
        shared_python.symlink_to("/usr/bin/python3.12")
        shared_venv = self.shared / "venv"
        (shared_venv / "pyvenv.cfg").write_text(
            "home = /usr/bin\ninclude-system-site-packages = true\n",
            encoding="ascii",
        )
        site_packages = (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        if not site_packages.is_dir():
            self.fail("test runtime site-packages are unavailable")
        candidate_site_packages = (
            shared_venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        candidate_site_packages.parent.mkdir(parents=True)
        candidate_site_packages.symlink_to(site_packages, target_is_directory=True)
        safe_env = candidate / "tools/platform_safe_env_exec.py"
        safe_env.write_text(
            "import base64\n"
            "value = base64.b64encode(b'redis://127.0.0.1:6379/15').decode('ascii')\n"
            "print('PLATFORM_REDIS_URL\\t' + value)\n",
            encoding="ascii",
        )
        safe_env.chmod(0o555)
        env_file = self.shared / ".env.platform"
        env_file.write_text(
            "PLATFORM_TESTING=1\nPLATFORM_REDIS_URL=redis://127.0.0.1:6379/15\n",
            encoding="ascii",
        )
        env_file.chmod(0o600)
        from redis.asyncio import from_url

        cache_token = uuid.uuid4().hex
        v1_keys = tuple(
            f"platform:tournament:{namespace}:v1:{cache_token}"
            for namespace in ("profile-access", "profile-viewers", "profile-roster")
        )
        v2_keys = tuple(
            f"platform:tournament:{namespace}:v2:{cache_token}"
            for namespace in ("profile-access", "profile-viewers", "profile-roster")
        )

        async def seed_profile_access_keys() -> None:
            client = from_url("redis://127.0.0.1:6379/15", decode_responses=False)
            try:
                for key in (*v1_keys, *v2_keys):
                    await client.set(key, b"test-only")
            finally:
                await client.aclose()

        async def remove_profile_access_keys() -> None:
            client = from_url("redis://127.0.0.1:6379/15", decode_responses=False)
            try:
                await client.unlink(*v1_keys, *v2_keys)
            finally:
                await client.aclose()

        async def profile_access_key_state() -> tuple[bool, ...]:
            client = from_url("redis://127.0.0.1:6379/15", decode_responses=False)
            try:
                states = []
                for key in (*v1_keys, *v2_keys):
                    states.append(bool(await client.exists(key)))
                return tuple(states)
            finally:
                await client.aclose()

        import asyncio

        self.addCleanup(lambda: asyncio.run(remove_profile_access_keys()))
        asyncio.run(seed_profile_access_keys())
        candidate_source = REPO_ROOT / "platform"
        shutil.copytree(candidate_source / "python_packages", candidate / "python_packages")
        candidate_services = candidate / "apps/platform_api/app/services"
        candidate_services.mkdir(parents=True)
        for package in (
            candidate / "apps/platform_api/app/__init__.py",
            candidate_services.parent / "__init__.py",
            candidate_services / "__init__.py",
        ):
            package.write_text("", encoding="ascii")
            package.chmod(0o444)
        shutil.copy2(
            candidate_source / "apps/platform_api/app/services/tournament_profile_access.py",
            candidate_services / "tournament_profile_access.py",
        )
        (candidate_services / "tournament_profile_access.py").chmod(0o444)
        shutil.copy2(
            candidate_source / "tools/platform_safe_env_exec.py",
            candidate / "tools/platform_safe_env_exec.py",
        )
        (candidate / "tools/platform_safe_env_exec.py").chmod(0o555)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        migration = candidate / "tools/platform_run_alembic.sh"
        migration.write_text("#!/usr/bin/env bash\nexit 42\n")
        migration.chmod(0o755)

        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        deploy = self.copy_deploy_migration_script(systemctl)
        result = self.run_script(
            deploy,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "migration-failed")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        transaction_record = json.loads((self.shared / STATE_NAME).read_text())
        self.assertIsNotNone(
            transaction_record["profile_access_cache_purge_proof"],
            "old services must not restart while a concurrent quiesce receipt hides the install transaction",
        )
        self.assertEqual(
            asyncio.run(profile_access_key_state()),
            (False, False, False, True, True, True),
            "the isolated purge child must remove only v1 keys using the real service module",
        )
        migration_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(migration_state["deadlock-api"], "active")
        self.assertEqual(migration_state["deadlock-worker"], "inactive")
        self.assertEqual(migration_state["deadlock-web"], "inactive")
        self.assertEqual(
            migration_state["deadlock-cloudflare-ips.timer"], "inactive"
        )
        self.assertFalse((self.shared / ".release-quiesce.json").exists())

        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        restore_args = self.root / "runtime-restore.args"
        recorded_restore_calls = [
            shlex.split(line)
            for line in restore_args.read_text(encoding="ascii").splitlines()
        ]
        expected_reconcile = str(self.shared / STATE_NAME)
        self.assertTrue(
            any(
                "--release" in args
                and args[args.index("--release") + 1] == str(current)
                and "--transaction" in args
                and args[args.index("--transaction") + 1] == expected_reconcile
                and "--rollback-reconcile-transaction" in args
                and args[args.index("--rollback-reconcile-transaction") + 1]
                == expected_reconcile
                and "--live-qa-runtime-installer" in args
                and args[args.index("--live-qa-runtime-installer") + 1]
                == str(candidate / "tools/platform_live_qa_runtime_install.py")
                for args in recorded_restore_calls
            ),
            recorded_restore_calls,
        )
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api"], "active")
        self.assertEqual(final_state["deadlock-worker"], "inactive")
        self.assertEqual(final_state["deadlock-web"], "inactive")
        log = (self.root / "systemctl.log").read_text()
        self.assertIn("restart deadlock-api", log)
        self.assertNotIn("restart deadlock-worker", log)
        self.assertNotIn("restart deadlock-web", log)

    def test_first_install_migration_does_not_touch_old_service_systemd_topology(self) -> None:
        """An install with no old pointers must not stop or inspect old units."""

        candidate = self.add_release("first-install-candidate")
        shared_venv = self.shared / "venv"
        self.add_fake_venv(shared_venv, marker="first-install")
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(shared_venv),
            "--peer",
            str(self.shared / ".venv-install-first-install-candidate.0000"),
            "--snapshot",
            str(candidate / ".rollback" / "shared-venv-before-install"),
            "--transition",
            "none",
        )
        self.run_transaction("phase", "--expected", "prepared", "--phase", "venv-transitioned")
        self.run_transaction("phase", "--expected", "venv-transitioned", "--phase", "staged")
        self.run_transaction("record-services", "--service-state", "deadlock-api=inactive", "--service-state", "deadlock-worker=inactive", "--service-state", "deadlock-web=inactive", "--timer-active-before", "inactive")
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")

        systemctl_log = self.root / "first-install-systemctl.log"
        systemctl = self.root / "systemctl-first-install"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        fake_python_log = self.root / "first-install-python.log"
        fake_python = self.root / "first-install-python"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(fake_python_log)!r}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        candidate_tools = candidate / "tools"
        shutil.copytree(self.tools_dir, candidate_tools)
        script = ALEMBIC_SCRIPT.read_text(encoding="utf-8")
        preflight_tool = '"$TOOLS_DIR/platform_release_preflight.sh"'
        self.assertIn(preflight_tool, script)
        script = script.replace(preflight_tool, "/usr/bin/true", 1)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        migration = candidate_tools / "platform_run_alembic.sh"
        migration.write_text(script, encoding="utf-8")
        migration.chmod(0o755)

        result = self.run_script(
            migration,
            "upgrade",
            "head",
            env={
                "PLATFORM_ENVIRONMENT": "production",
                "PLATFORM_APP_DIR": str(self.app_dir),
                "PLATFORM_ENV_FILE": str(self.shared / ".env.platform"),
                "PLATFORM_PYTHON_BIN": str(fake_python),
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertIn("-m alembic upgrade head", fake_python_log.read_text(encoding="utf-8"))

    def test_first_install_activation_uses_durable_transaction_boundary(self) -> None:
        """Production deploy activates seven units only after the durable boundary."""

        candidate, systemctl, enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-activation"
        )
        installer = candidate / "tools/platform_install_systemd_units.sh"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
            f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
            f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
            f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n",
            encoding="utf-8",
        )
        installer.chmod(0o755)
        receipt = json.loads((self.shared / STATE_NAME).read_text(encoding="ascii"))
        self.assertEqual(
            set(receipt["systemd_state_before"]),
            {
                "deadlock-api.service",
                "deadlock-worker.service",
                "deadlock-web.service",
                "deadlock-maintenance.timer",
                "deadlock-logrotate.timer",
                "deadlock-cloudflare-ips.timer",
                "deadlock-health-monitor.timer",
            },
        )
        self.assertNotIn("deadlock-offsite-backup.timer", receipt["systemd_state_before"])
        deploy = self.copy_deploy_script_with_fault(
            "deploy-first-install-activation.sh", None, None, systemctl
        )
        result = self.run_script(
            deploy,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="first-install"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        final_enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
        for unit in (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        ):
            self.assertEqual(final_enabled[unit], "enabled", unit)
        self.assertEqual(final_enabled["deadlock-offsite-backup.timer"], "disabled")
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        for unit in (
            "deadlock-api",
            "deadlock-worker",
            "deadlock-web",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        ):
            self.assertEqual(final_state[unit], "active", unit)
        log = (self.root / "systemctl.log").read_text(encoding="utf-8")
        self.assertNotIn("offsite", log)
        self.assertLess(log.index("disable deadlock-api.service"), log.index("enable deadlock-api.service"))

    def test_first_install_systemd_failures_restore_receipt_before_retry(self) -> None:
        """Enablement/readiness failures retain and restore the same receipt."""

        for failure in ("service-enable", "timer-enable", "readiness"):
            with self.subTest(failure=failure):
                self.tearDown()
                self.setUp()
                candidate, systemctl, enabled_path = self.prepare_initial_systemd_activation_state(
                    f"first-install-failure-{failure}"
                )
                mode_path = self.root / "initial-systemd-failure"
                mode_path.write_text(failure + "\n", encoding="ascii")
                installer = candidate / "tools/platform_install_systemd_units.sh"
                installer.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
                    f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
                    f"if [[ \"$(cat {mode_path})\" == service-enable ]]; then exit 41; fi\n"
                    f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
                    f"if [[ \"$(cat {mode_path})\" == timer-enable ]]; then exit 42; fi\n"
                    f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n",
                    encoding="utf-8",
                )
                installer.chmod(0o755)
                if failure == "readiness":
                    failed_deploy = self.copy_deploy_script_with_fault(
                        "deploy-first-install-readiness-failure.sh",
                        "  set_phase systemd-activation-pending systemd-activated\n",
                        "  /bin/false\n  set_phase systemd-activation-pending systemd-activated\n",
                        systemctl,
                    )
                else:
                    failed_deploy = self.copy_deploy_script_with_fault(
                        f"deploy-first-install-{failure}-failure.sh", None, None, systemctl
                    )
                result = self.run_script(
                    failed_deploy,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-{failure}"),
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.state_phase(), "systemd-activation-pending")
                self.assertTrue((self.shared / STATE_NAME).exists())
                record = json.loads((self.shared / STATE_NAME).read_text(encoding="ascii"))
                self.assertEqual(record["systemd_state_before"]["deadlock-api.service"]["active"], "inactive")
                live_state = json.loads((self.root / "systemd-state.json").read_text())
                self.assertTrue(all(live_state.get(unit) == "inactive" for unit in (
                    "deadlock-api", "deadlock-worker", "deadlock-web",
                    "deadlock-maintenance.timer", "deadlock-logrotate.timer",
                    "deadlock-cloudflare-ips.timer", "deadlock-health-monitor.timer",
                )))
                live_enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
                self.assertTrue(all(live_enabled[unit] == "disabled" for unit in (
                    "deadlock-api.service", "deadlock-worker.service", "deadlock-web.service",
                    "deadlock-maintenance.timer", "deadlock-logrotate.timer",
                    "deadlock-cloudflare-ips.timer", "deadlock-health-monitor.timer",
                )))
                self.assertNotIn("offsite", (self.root / "systemctl.log").read_text())
                mode_path.write_text("none\n", encoding="ascii")
                resume = self.copy_deploy_script_with_fault(
                    f"deploy-first-install-{failure}-retry.sh", None, None, systemctl
                )
                result = self.run_script(
                    resume,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-{failure}-retry"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)

    def test_first_install_post_activation_failure_rewinds_phase_before_retry(self) -> None:
        """A failure after either activation marker must leave a retryable phase."""

        for marker in (
            "systemd-activation-pending systemd-activated",
            "systemd-activated activation-committed",
        ):
            with self.subTest(marker=marker):
                self.tearDown()
                self.setUp()
                candidate, systemctl, enabled_path = self.prepare_initial_systemd_activation_state(
                    f"first-install-post-{marker.split()[-1]}"
                )
                installer = candidate / "tools/platform_install_systemd_units.sh"
                installer.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
                    f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
                    f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
                    f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n",
                    encoding="utf-8",
                )
                installer.chmod(0o755)
                needle = f"  set_phase {marker}\n"
                failed_deploy = self.copy_deploy_script_with_fault(
                    f"deploy-first-install-post-{marker.split()[-1]}-failure.sh",
                    needle,
                    needle + "  /bin/false\n",
                    systemctl,
                )
                result = self.run_script(
                    failed_deploy,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-post-{marker.split()[-1]}"),
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.state_phase(), "systemd-activation-pending")
                self.assertTrue((self.shared / STATE_NAME).exists())
                enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
                self.assertTrue(
                    all(
                        enabled[unit] == "disabled"
                        for unit in (
                            "deadlock-api.service",
                            "deadlock-worker.service",
                            "deadlock-web.service",
                            "deadlock-maintenance.timer",
                            "deadlock-logrotate.timer",
                            "deadlock-cloudflare-ips.timer",
                            "deadlock-health-monitor.timer",
                        )
                    )
                )
                retry = self.copy_deploy_script_with_fault(
                    f"deploy-first-install-post-{marker.split()[-1]}-retry.sh",
                    None,
                    None,
                    systemctl,
                )
                result = self.run_script(
                    retry,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-post-{marker.split()[-1]}-retry"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)

    def test_first_install_sigkill_after_activation_marker_resumes(self) -> None:
        """SIGKILL after either durable activation marker remains resumable."""

        for marker in (
            "systemd-activation-pending systemd-activated",
            "systemd-activated activation-committed",
        ):
            with self.subTest(marker=marker):
                self.tearDown()
                self.setUp()
                candidate, systemctl, enabled_path = self.prepare_initial_systemd_activation_state(
                    f"first-install-kill-{marker.split()[-1]}"
                )
                installer = candidate / "tools/platform_install_systemd_units.sh"
                installer.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
                    f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
                    f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
                    f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n",
                    encoding="utf-8",
                )
                installer.chmod(0o755)
                needle = f"  set_phase {marker}\n"
                interrupted = self.copy_deploy_script_with_fault(
                    f"deploy-first-install-kill-{marker.split()[-1]}.sh",
                    needle,
                    needle + '  /bin/kill -KILL "$$"\n',
                    systemctl,
                )
                result = self.run_script(
                    interrupted,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-kill-{marker.split()[-1]}"),
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), marker.split()[-1])
                enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
                self.assertTrue(
                    all(
                        enabled[unit] == "enabled"
                        for unit in (
                            "deadlock-api.service",
                            "deadlock-worker.service",
                            "deadlock-web.service",
                            "deadlock-maintenance.timer",
                            "deadlock-logrotate.timer",
                            "deadlock-cloudflare-ips.timer",
                            "deadlock-health-monitor.timer",
                        )
                    )
                )
                retry = self.copy_deploy_script_with_fault(
                    f"deploy-first-install-kill-{marker.split()[-1]}-retry.sh",
                    None,
                    None,
                    systemctl,
                )
                result = self.run_script(
                    retry,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-kill-{marker.split()[-1]}-retry"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)

    def test_first_install_abort_restores_systemd_before_removing_candidate(self) -> None:
        candidate, systemctl, enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-abort"
        )
        installer = candidate / "tools/platform_install_systemd_units.sh"
        installer.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
            "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
            f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
            f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
            f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
            "exit 42\n",
            encoding="utf-8",
        )
        installer.chmod(0o755)
        failed = self.copy_deploy_script_with_fault(
            "deploy-first-install-abort-failure.sh", None, None, systemctl
        )
        result = self.run_script(
            failed,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="first-install-abort"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertFalse(os.path.lexists(self.app_dir / "current"))
        enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
        self.assertEqual(enabled["deadlock-api.service"], "disabled")
        self.assertEqual(enabled["deadlock-maintenance.timer"], "disabled")
        self.assertNotIn("offsite", (self.root / "systemctl.log").read_text())

    def test_first_install_abort_verifies_baseline_at_staged_and_restored_phases(self) -> None:
        for phase in ("staged", "recovery-restored"):
            with self.subTest(phase=phase):
                self.tearDown()
                self.setUp()
                if phase == "staged":
                    candidate = self.create_wrapper_transaction(
                        None,
                        None,
                        "first-install-abort-staged-baseline",
                        complete_snapshot=True,
                    )
                    systemctl = self.write_initial_systemctl()
                    self.run_transaction(
                        "capture-initial-systemd", "--systemctl", str(systemctl)
                    )
                else:
                    candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
                        "first-install-abort-restored-baseline"
                    )
                    # ``recover`` refuses an uncertain activation phase until
                    # the operator has durably acknowledged that migration
                    # was not reversed.  The deploy abort path then owns the
                    # final baseline proof and cleanup for recovery-restored.
                    self.run_transaction(
                        "authorize-recovery", "--confirm", "MIGRATION_NOT_REVERSED"
                    )
                    self.run_transaction("recover", "--retain")
                    self.assertEqual(self.state_phase(), "recovery-restored")
                abort = self.copy_abort_script_with_systemctl(systemctl)
                result = self.run_script(
                    abort,
                    "--abort-retained",
                    "--confirm-migration-not-reversed",
                    "--app-dir",
                    str(self.app_dir),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse(candidate.exists())

    def test_first_install_abort_retries_partial_systemd_restore_before_cleanup(self) -> None:
        """A killed restore retains the receipt and retries before cleanup."""

        candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-abort-partial-restore"
        )
        for expected, next_phase in (
            ("activation-pending", "services-restarted"),
            ("services-restarted", "nginx-pending"),
            ("nginx-pending", "nginx-applied"),
            ("nginx-applied", "smoke-passed"),
            ("smoke-passed", "systemd-activation-pending"),
            ("systemd-activation-pending", "systemd-activated"),
        ):
            self.run_transaction("phase", "--expected", expected, "--phase", next_phase)
        units = (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        )
        subprocess.run([str(systemctl), "enable", *units], check=True)
        subprocess.run([str(systemctl), "start", *units], check=True)
        abort = self.copy_abort_script_with_systemctl(systemctl)
        interrupted = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={"PLATFORM_TEST_SYSTEMCTL_KILL_AFTER": "stop deadlock-api.service"},
            check=False,
        )
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertEqual(self.state_phase(), "systemd-activated")
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())

        retry = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertFalse(os.path.lexists(self.app_dir / "current"))

    def test_initial_systemd_restore_has_per_call_and_aggregate_deadlines(self) -> None:
        _candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-systemd-deadlines"
        )
        with mock.patch.object(
            transaction.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired([str(systemctl), "stop"], 30),
        ) as run:
            with self.assertRaises(transaction.TransactionError):
                transaction._run_systemctl(str(systemctl), "stop", "deadlock-api.service")
        self.assertEqual(run.call_args.kwargs["timeout"], transaction.SYSTEMD_CALL_TIMEOUT_SECONDS)

        deadlines: list[float | None] = []

        def record_systemctl(
            _systemctl: str, *_arguments: str, deadline: float | None = None
        ) -> str:
            deadlines.append(deadline)
            return ""

        start = time.monotonic()
        with mock.patch.object(transaction, "_run_systemctl", side_effect=record_systemctl):
            with mock.patch.object(
                transaction, "_verify_initial_systemd_snapshot", return_value=None
            ):
                transaction.restore_initial_systemd(
                    self.shared / STATE_NAME, systemctl=str(systemctl)
                )
        self.assertEqual(
            len(deadlines), 2 * len(transaction.INITIAL_SYSTEMD_UNITS)
        )
        self.assertEqual(len(set(deadlines)), 1)
        assert deadlines[0] is not None
        self.assertGreater(deadlines[0], start)
        self.assertLessEqual(
            deadlines[0] - start,
            transaction.SYSTEMD_OPERATION_TIMEOUT_SECONDS + 1.0,
        )

    def test_first_install_recovery_authorized_requires_verified_baseline(self) -> None:
        """An authorized receipt cannot clean up while units are still active."""

        candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-authorized-window"
        )
        for expected, next_phase in (
            ("activation-pending", "services-restarted"),
            ("services-restarted", "nginx-pending"),
            ("nginx-pending", "nginx-applied"),
            ("nginx-applied", "smoke-passed"),
            ("smoke-passed", "systemd-activation-pending"),
            ("systemd-activation-pending", "systemd-activated"),
        ):
            self.run_transaction("phase", "--expected", expected, "--phase", next_phase)
        units = (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        )
        subprocess.run([str(systemctl), "enable", *units], check=True)
        subprocess.run([str(systemctl), "start", *units], check=True)
        self.run_transaction(
            "authorize-recovery", "--confirm", "MIGRATION_NOT_REVERSED"
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        failed = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())
        self.assertEqual(self.state_phase(), "recovery-authorized")
        state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertTrue(
            all(
                state[unit] == "inactive"
                for unit in (
                    "deadlock-api",
                    "deadlock-worker",
                    "deadlock-web",
                    "deadlock-maintenance.timer",
                    "deadlock-logrotate.timer",
                    "deadlock-cloudflare-ips.timer",
                    "deadlock-health-monitor.timer",
                )
            )
        )
        retry = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_initial_systemd_shell_budget_bounds_hanging_installer(self) -> None:
        """The shell boundary caps an installer hang and retains recovery state."""

        candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-systemd-shell-budget"
        )
        installer = candidate / "tools/platform_install_systemd_units.sh"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
            f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
            f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
            f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n",
            encoding="utf-8",
        )
        installer.chmod(0o755)
        deploy = self.copy_deploy_script_with_fault(
            "deploy-first-install-systemd-shell-budget.sh", None, None, systemctl
        )
        deploy.write_text(
            deploy.read_text(encoding="utf-8").replace(
                "SYSTEMD_OPERATION_TIMEOUT_SECONDS=120",
                "SYSTEMD_OPERATION_TIMEOUT_SECONDS=2",
                1,
            ),
            encoding="utf-8",
        )
        deploy.chmod(0o755)
        started = time.monotonic()
        result = self.run_script(
            deploy,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env={
                **self.runtime_env(label="first-install-systemd-shell-budget"),
                "PLATFORM_TEST_SYSTEMCTL_SLEEP_SECONDS": "10",
                "PLATFORM_TEST_SYSTEMCTL_SLEEP_ACTION": "enable",
            },
            check=False,
        )
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(elapsed, 12.0, result.stderr)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())
        self.assertEqual(self.state_phase(), "systemd-activation-pending")

    def test_initial_live_qa_reconcile_budget_retains_receipt_and_retries(self) -> None:
        """A hanging live-QA reconcile is bounded before activation can continue."""

        candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
            "first-install-liveqa-reconcile"
        )
        self.install_initial_systemd_units_fixture(candidate, systemctl)
        mode_path = self.root / "liveqa-reconcile-mode"
        log_path = self.root / "liveqa-reconcile.log"
        self.install_live_qa_reconcile_fixture(candidate, mode_path, log_path)
        mode_path.write_text("hang\n", encoding="ascii")
        deploy = self.copy_deploy_script_with_fault(
            "deploy-first-install-liveqa-reconcile.sh", None, None, systemctl
        )
        started = time.monotonic()
        test_env = self.runtime_env(label="first-install-liveqa-reconcile")
        test_env["PLATFORM_CANDIDATE_DEADLINE_MONOTONIC_NS"] = str(
            time.monotonic_ns() + 630 * 1_000_000_000
        )
        result = self.run_script(
            deploy,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=test_env,
            check=False,
        )
        elapsed = time.monotonic() - started

        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 35.0, result.stderr)
        self.assertEqual(self.state_phase(), "activation-pending")
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())
        self.assertIn("reconcile\n", log_path.read_text(encoding="ascii"))
        self.assertRegex(
            result.stderr,
            r"outcome=timeout budget_seconds=(?:[1-9]|1[0-9]|2[0-5]) child_exit=124",
        )
        payload_path = log_path.with_suffix(".payload")
        self.assertEqual(payload_path.read_text(encoding="ascii"), "published\n")

        mode_path.write_text("slow\n", encoding="ascii")
        retry = self.copy_deploy_script_with_fault(
            "deploy-first-install-liveqa-reconcile-retry.sh", None, None, systemctl
        )
        retry.write_text(
            retry.read_text(encoding="utf-8").replace(
                "SYSTEMD_OPERATION_TIMEOUT_SECONDS=120",
                "SYSTEMD_OPERATION_TIMEOUT_SECONDS=10",
                1,
            ),
            encoding="utf-8",
        )
        retry.chmod(0o755)
        result = self.run_script(
            retry,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="first-install-liveqa-reconcile-retry"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual(payload_path.read_text(encoding="ascii"), "published\n")
        self.assertEqual(log_path.read_text(encoding="ascii").splitlines(), ["reconcile", "reconcile"])

    def test_initial_activation_readiness_budget_retains_receipt_and_retries(self) -> None:
        """Both activation readiness phases enforce the same aggregate budget."""

        for target_phase in ("activation-pending", "systemd-activation-pending"):
            with self.subTest(target_phase=target_phase):
                self.tearDown()
                self.setUp()
                candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
                    f"first-install-readiness-{target_phase}"
                )
                self.install_initial_systemd_units_fixture(candidate, systemctl)
                if target_phase == "systemd-activation-pending":
                    interrupted = self.copy_deploy_script_with_fault(
                        "deploy-first-install-readiness-phase-kill.sh",
                        "    set_phase smoke-passed systemd-activation-pending\n",
                        "    set_phase smoke-passed systemd-activation-pending\n"
                        '    /bin/kill -KILL "$$"\n',
                        systemctl,
                    )
                    result = self.run_script(
                        interrupted,
                        "--resume",
                        "--app-dir",
                        str(self.app_dir),
                        env=self.runtime_env(
                            label=f"first-install-readiness-{target_phase}-interrupted"
                        ),
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.state_phase(), target_phase)

                mode_path = self.root / f"readiness-{target_phase}-mode"
                log_path = self.root / f"readiness-{target_phase}.log"
                curl = self.write_hanging_curl(mode_path, log_path)
                mode_path.write_text("hang\n", encoding="ascii")
                timeout_seconds = 2 if target_phase == "activation-pending" else 10
                deploy = self.copy_deploy_script_with_curl(
                    f"deploy-first-install-readiness-{target_phase}.sh",
                    systemctl,
                    curl,
                    timeout_seconds=timeout_seconds,
                )
                started = time.monotonic()
                result = self.run_script(
                    deploy,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label=f"first-install-readiness-{target_phase}"),
                    check=False,
                )
                elapsed = time.monotonic() - started

                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertLess(elapsed, 20.0, result.stderr)
                self.assertEqual(self.state_phase(), target_phase)
                self.assertTrue((self.shared / STATE_NAME).exists())
                self.assertTrue(candidate.exists())
                self.assertTrue(log_path.read_text(encoding="ascii"))

                mode_path.write_text("ok\n", encoding="ascii")
                retry = self.copy_deploy_script_with_curl(
                    f"deploy-first-install-readiness-{target_phase}-retry.sh",
                    systemctl,
                    curl,
                )
                result = self.run_script(
                    retry,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(
                        label=f"first-install-readiness-{target_phase}-retry"
                    ),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)

    def test_first_install_missing_or_invalid_systemd_receipt_fails_before_mutation(self) -> None:
        for mutation in ("missing", "invalid"):
            with self.subTest(mutation=mutation):
                self.tearDown()
                self.setUp()
                candidate, systemctl, _enabled_path = self.prepare_initial_systemd_activation_state(
                    f"first-install-receipt-{mutation}"
                )
                state = self.shared / STATE_NAME
                record = json.loads(state.read_text(encoding="ascii"))
                if mutation == "missing":
                    record.pop("systemd_state_before")
                else:
                    record["systemd_state_before"]["deadlock-api.service"]["active"] = "active"
                state.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
                state.chmod(0o600)
                (self.root / "systemctl.log").write_text("", encoding="ascii")
                deploy = self.copy_deploy_script_with_fault(
                    f"deploy-first-install-{mutation}-receipt.sh", None, None, systemctl
                )
                result = self.run_script(
                    deploy,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_mutable_first_install_staged_failure_uses_optional_snapshot_policy(self) -> None:
        """A staged first-install failure cleans only a safe nullable receipt."""

        candidate = self.create_wrapper_transaction(None, None, "mutable-first-install")
        # create_wrapper_transaction deliberately leaves the initial receipt at
        # prepared unless it records a snapshot. Advance it to the exact
        # pre-migration staged boundary with no service snapshot at all.
        self.run_transaction("phase", "--expected", "prepared", "--phase", "venv-transitioned")
        self.run_transaction("phase", "--expected", "venv-transitioned", "--phase", "staged")
        systemctl, systemctl_log = self.write_failing_systemctl("mutable-first-install")
        artifact = self.root / "mutable-first-install.tar.gz"
        artifact.write_bytes(b"not reached")
        failed = self.copy_initial_deploy_with_fault(
            "deploy-mutable-first-install-failure.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            '  /bin/false\n',
        )

        result = self.run_script(
            failed,
            "--artifact",
            str(artifact),
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_mutable_first_install_abort_accepts_only_strict_inactive_snapshot(self) -> None:
        """Nullable abort cleanup accepts absent or complete inactive state only."""

        for label, mutation in (
            ("active", lambda record: record["service_state_before"].__setitem__("deadlock-api", "active")),
            ("enabled", lambda record: record["service_enabled_before"].__setitem__("deadlock-api", "enabled")),
            ("partial", lambda record: record.pop("timer_enabled_before")),
        ):
            with self.subTest(label=label):
                if label != "active":
                    self.tearDown()
                    self.setUp()
                candidate = self.create_wrapper_transaction(
                    None,
                    None,
                    f"mutable-first-install-{label}",
                    complete_snapshot=True,
                )
                state = self.shared / STATE_NAME
                record = json.loads(state.read_text(encoding="ascii"))
                mutation(record)
                state.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
                state.chmod(0o600)
                systemctl, systemctl_log = self.write_failing_systemctl(
                    f"mutable-first-install-abort-{label}"
                )
                abort = self.copy_abort_script_with_systemctl(systemctl)
                result = self.run_script(
                    abort,
                    "--abort-retained",
                    "--confirm-migration-not-reversed",
                    "--app-dir",
                    str(self.app_dir),
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                self.assertFalse(systemctl_log.exists())

    def test_mutable_abort_retained_staged_nullable_topologies(self) -> None:
        """The staged map covers current-only cleanup and rejects drift."""

        current = self.add_release("mutable-current-only-staged-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_runtime_stubs(current)
        self.add_fake_venv(self.shared / "venv", marker="mutable-current-only")
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "mutable-current-only-staged",
            complete_snapshot=True,
        )
        self.prepare_wrapper_cache_purge(candidate)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

        self.tearDown()
        self.setUp()
        current = self.add_release("mutable-current-only-staged-invalid-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "mutable-current-only-staged-invalid",
            complete_snapshot=True,
        )
        candidate_pointer = self.app_dir / "current"
        candidate_pointer.unlink()
        candidate_pointer.symlink_to(candidate)
        systemctl, systemctl_log = self.write_failing_systemctl(
            "mutable-current-only-staged-invalid"
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())
        self.assertFalse(systemctl_log.exists())

    def test_sigkill_after_snapshot_leaves_abortable_receipt_without_transaction(
        self,
    ) -> None:
        current, _previous, _candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        (self.shared / STATE_NAME).unlink()
        artifact = self.root / "release.tar.gz"
        artifact.write_bytes(b"not reached")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        interrupted = self.copy_initial_deploy_with_fault(
            "deploy-after-snapshot-kill.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            '  /bin/kill -KILL "$$"\n'
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
        )
        result = self.run_script(
            interrupted,
            "--artifact",
            str(artifact),
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertEqual(self.state_phase(), "quiesce-pending")
        self.assertFalse((self.shared / ".release-quiesce.json").exists())
        self.assertTrue(
            all(
                value == "inactive"
                for value in json.loads((self.root / "systemd-state.json").read_text()).values()
            )
        )

        self.prepare_wrapper_cache_purge(current)
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api"], "active")
        self.assertEqual(final_state["deadlock-worker"], "inactive")
        self.assertEqual(final_state["deadlock-web"], "active")
        self.assertEqual(final_state["deadlock-cloudflare-ips.timer"], "inactive")

    def test_stage_failure_recovers_pre_active_services_without_swallowing_failure(
        self,
    ) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.prepare_wrapper_cache_purge(candidate)
        (self.shared / STATE_NAME).unlink()
        artifact = self.root / "stage-failure.tar.gz"
        artifact.write_bytes(b"not reached")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        failed = self.copy_initial_deploy_with_fault(
            "deploy-stage-failure.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            '  install -d -o root -g root -m 0755 "$APP_DIR/releases/stage-failure"\n'
            "  /bin/false\n",
        )
        result = self.run_script(
            failed,
            "--artifact",
            str(artifact),
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.releases / "stage-failure").exists())
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api"], "active")
        self.assertEqual(final_state["deadlock-worker"], "inactive")
        self.assertEqual(final_state["deadlock-web"], "active")
        self.assertEqual(final_state["deadlock-cloudflare-ips.timer"], "active")

    def test_deploy_lock_contention_blocks_preflight_and_quiesce(self) -> None:
        current, _previous, _candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        (self.shared / STATE_NAME).unlink()
        artifact = self.root / "lock-contention.tar.gz"
        artifact.write_bytes(b"not reached")
        initial_states = {
            "deadlock-api": "active",
            "deadlock-worker": "inactive",
            "deadlock-web": "active",
            "deadlock-cloudflare-ips.timer": "active",
            "deadlock-cloudflare-ips.service": "inactive",
        }
        systemctl = self.write_stateful_systemctl(initial_states)
        failed = self.copy_initial_deploy_with_fault(
            "deploy-lock-contention.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            "  /bin/false\n",
        )
        assert self._release_lock is not None
        try:
            self._release_lock.acquire(nonblocking=True)
            result = self.run_script(
                failed,
                "--artifact",
                str(artifact),
                "--app-dir",
                str(self.app_dir),
                check=False,
            )
        finally:
            self._release_lock.release()

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text()),
            initial_states,
        )
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_killed_release_body_cannot_leave_a_background_child_holding_lock(
        self,
    ) -> None:
        """The lock supervisor must close the descriptor before body children run."""
        helper = self.root / "platform_release_lock.sh"
        self.install_test_lock_helper(helper)
        child_pid_file = self.root / "child.pid"
        script = self.root / "lock-body-kill.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            f"source {shlex.quote(str(helper))}\n"
            "ORIGINAL_ARGS=(\"$@\")\n"
            "platform_release_lock_supervise \"${ORIGINAL_ARGS[@]}\"\n"
            "[[ \"${PLATFORM_RELEASE_LOCK_SUPERVISED:-}\" == 1 ]] || exit 0\n"
            "platform_release_lock_open\n"
            f"/bin/sleep 30 >/dev/null 2>&1 </dev/null & child=$!; printf '%s\\n' \"$child\" > {shlex.quote(str(child_pid_file))}\n"
            "/bin/kill -KILL \"$$\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        env = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_TESTING": "1",
        }
        child_pid = None
        try:
            result = self.run_script(script, env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
            assert self._release_lock is not None
            self._release_lock.acquire(nonblocking=True)
            self._release_lock.release()
        finally:
            if child_pid is not None:
                try:
                    command_line = Path(f"/proc/{child_pid}/cmdline").read_bytes()
                except OSError:
                    command_line = b""
                if b"sleep" in command_line:
                    try:
                        os.kill(child_pid, 15)
                    except ProcessLookupError:
                        pass

    def test_inherited_release_fd_is_rejected_without_root_path_or_body(self) -> None:
        helper = self.root / "platform_release_lock.sh"
        self.install_test_lock_helper(helper)
        child_pid_file = self.root / "inherited-child.pid"
        script = self.root / "inherited-lock-body-kill.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            f"source {shlex.quote(str(helper))}\n"
            "ORIGINAL_ARGS=(\"$@\")\n"
            "platform_release_lock_supervise \"${ORIGINAL_ARGS[@]}\"\n"
            "[[ \"${PLATFORM_RELEASE_LOCK_SUPERVISED:-}\" == 1 ]] || exit 0\n"
            "platform_release_lock_open\n"
            f"/bin/sleep 30 >/dev/null 2>&1 </dev/null & child=$!; printf '%s\\n' \"$child\" > {shlex.quote(str(child_pid_file))}\n"
            "/bin/kill -KILL \"$$\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        env = {"PLATFORM_ENVIRONMENT": "test", "PLATFORM_TESTING": "1"}
        assert self._release_lock is not None
        lock_fd = self._release_lock.fd
        child_pid = None
        try:
            self._release_lock.acquire(nonblocking=True)
            env["PLATFORM_RELEASE_LOCK_FD"] = str(lock_fd)
            result = subprocess.run(
                [str(script)],
                cwd=REPO_ROOT,
                env={**os.environ, **env},
                pass_fds=(lock_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse(child_pid_file.exists(), result.stderr)
        finally:
            self._release_lock.release()
        try:
            self._release_lock.acquire(nonblocking=True)
            self._release_lock.release()
        finally:
            if child_pid is not None:
                try:
                    command_line = Path(f"/proc/{child_pid}/cmdline").read_bytes()
                except OSError:
                    command_line = b""
                if b"sleep" in command_line:
                    try:
                        os.kill(child_pid, 15)
                    except ProcessLookupError:
                        pass

    def test_abort_restart_failure_retains_receipt_and_repeated_abort_recovers(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-failed"
        )
        self.prepare_wrapper_cache_purge(candidate)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
                "PLATFORM_TEST_SYSTEMCTL_FAIL_RESTART": "1",
            },
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertTrue((self.shared / STATE_NAME).exists())

        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

    def test_abort_refuses_release_identity_drift_before_service_restart(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=active",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-failed"
        )
        moved = self.releases / "current-moved"
        current.rename(moved)
        replacement = self.releases / "current"
        replacement.mkdir()
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertNotIn("restart", (self.root / "systemctl.log").read_text())

    def test_abort_refuses_lock_contention_before_recovery_or_restart(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=active",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-failed"
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        assert self._release_lock is not None
        try:
            self._release_lock.acquire(nonblocking=True)
            result = self.run_script(
                abort,
                "--abort-retained",
                "--confirm-migration-not-reversed",
                "--app-dir",
                str(self.app_dir),
                check=False,
            )
        finally:
            self._release_lock.release()

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.state_phase(), "migration-failed")
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def prepare_deploy_state(self, phase: str) -> tuple[Path, Path, Path]:
        current, previous, candidate = self.prepare_install_state(
            with_fake_python=True
        )
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase=phase)
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)
        return current, previous, candidate

    def prepare_initial_systemd_activation_state(
        self, label: str
    ) -> tuple[Path, Path, Path]:
        candidate = self.add_release(f"{label}-candidate")
        (candidate / ".rollback").mkdir()
        peer = self.shared / f".venv-install-{label}-candidate.0000"
        self.add_fake_venv(peer, marker="first-install")
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(peer),
            "--snapshot",
            str(candidate / ".rollback" / "shared-venv-before-install"),
            "--transition",
            "create",
        )
        self.run_transaction("rename", "--mode", "activate-created")
        self.run_transaction(
            "phase", "--expected", "prepared", "--phase", "venv-transitioned"
        )
        self.run_transaction(
            "phase", "--expected", "venv-transitioned", "--phase", "staged"
        )
        self.add_runtime_stubs(candidate)
        systemctl = self.write_initial_systemctl()
        self.run_transaction("capture-initial-systemd", "--systemctl", str(systemctl))
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=inactive",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
            "--service-enabled",
            "deadlock-api=disabled",
            "--service-enabled",
            "deadlock-worker=disabled",
            "--service-enabled",
            "deadlock-web=disabled",
            "--timer-enabled-before",
            "disabled",
        )
        self.run_transaction(
            "phase", "--expected", "staged", "--phase", "migration-pending"
        )
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-applied"
        )
        self.switch_pointer("current", candidate)
        self.run_transaction(
            "phase", "--expected", "migration-applied", "--phase", "current-switched"
        )
        self.run_transaction(
            "phase", "--expected", "current-switched", "--phase", "pointers-switched"
        )
        self.run_transaction(
            "phase", "--expected", "pointers-switched", "--phase", "activation-pending"
        )
        return candidate, systemctl, self.root / "systemd-enabled.json"

    def prepare_rollback_state(self) -> tuple[Path, Path]:
        current = self.add_release("rollback-current")
        previous = self.add_release("rollback-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        self.add_fake_venv(self.shared / "venv", marker="new")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        shutil.copytree(self.shared / "venv", snapshot)
        self.prepare_wrapper_cache_purge(current)
        (rollback / "previous-release").write_text(f"{previous}\n")
        (rollback / "previous-release").chmod(0o600)
        (rollback / "venv-transition").write_text("snapshot\n")
        (rollback / "venv-transition").chmod(0o600)
        return current, previous

    def runtime_env(self, *, label: str) -> dict[str, str]:
        return {
            "PLATFORM_CANDIDATE_DEADLINE_MONOTONIC_NS": str(
                time.monotonic_ns() + 1800 * 1_000_000_000
            ),
            "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
            "PLATFORM_TEST_UNITS_LABEL": label,
            "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
            "PLATFORM_TEST_NGINX_LABEL": label,
        }

    def write_fake_systemctl(self) -> Path:
        path = self.root / "systemctl"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  is-active) printf '%s\\n' active ;;\n"
            "  is-enabled) printf '%s\\n' enabled ;;\n"
            "esac\n"
            "exit 0\n"
        )
        path.chmod(0o755)
        return path

    def recovery_provenance(self) -> dict[str, object]:
        return {
            "repository": "StrayForest/old_sparky",
            "workflow": "Platform security and build",
            "job": "Verification contract",
            "run_id": "12345",
            "run_attempt": "2",
            "recovery_workflow_sha": "c" * 40,
            "source_sha": "a" * 40,
            "artifact_name": "platform-ci-route-12345-2",
            "artifact_sha256": "b" * 64,
            "deployable": False,
        }

    def install_recovery_generation(self) -> Path:
        source_tools = self.root / "recovery-source" / "platform" / "tools"
        source_tools.mkdir(parents=True)
        for name in recovery.RECOVERY_FILES:
            source = self.tools_dir / name
            destination = source_tools / name
            shutil.copy2(source, destination, follow_symlinks=False)
            destination.chmod(0o755 if name.endswith(".sh") else 0o644)
        self.install_test_lock_helper(source_tools / "platform_release_lock.sh")
        bundle = self.root / "recovery-bundle.zip"
        summary = recovery.build_bundle(
            source_tools.parent.parent,
            source_sha="a" * 40,
            provenance=self.recovery_provenance(),
            output=bundle,
        )
        return recovery.install_bundle(
            bundle,
            app_dir=self.app_dir,
            expected_bundle_sha=str(summary["bundle_sha256"]),
            expected_source_sha="a" * 40,
            expected_provenance=self.recovery_provenance(),
        )

    def create_wrapper_transaction(
        self,
        current: Path | None,
        previous: Path | None,
        label: str,
        *,
        complete_snapshot: bool = False,
        service_states: dict[str, str] | None = None,
        timer_active: bool = False,
    ) -> Path:
        candidate = self.add_release(f"{label}-candidate")
        rollback = candidate / ".rollback"
        rollback.mkdir()
        current_value = "" if current is None else str(current)
        previous_value = "" if previous is None else str(previous)
        if current is not None:
            freeze = candidate / "requirements-platform.freeze.txt"
            freeze.write_text("pip==test\n", encoding="ascii")
            freeze.chmod(0o444)
            previous_record = rollback / "previous-release"
            previous_record.write_text(f"{current}\n", encoding="ascii")
            previous_record.chmod(0o600)
            transition = rollback / "venv-transition"
            transition.write_text("unchanged\n", encoding="ascii")
            transition.chmod(0o600)
            freeze_record = rollback / "shared-freeze.sha256"
            freeze_record.write_text(
                f"{hashlib.sha256(freeze.read_bytes()).hexdigest()}\n",
                encoding="ascii",
            )
            freeze_record.chmod(0o600)
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            current_value,
            "--previous-before",
            previous_value,
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(self.shared / f".venv-install-{candidate.name}.0000"),
            "--snapshot",
            str(rollback / "shared-venv-before-install"),
            "--transition",
            "none",
        )
        if complete_snapshot:
            service_args = [
                argument
                for unit, state in (
                    service_states
                    or {
                        "deadlock-api": "inactive",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "inactive",
                    }
                ).items()
                for argument in ("--service-state", f"{unit}={state}")
            ]
            self.run_transaction(
                "phase", "--expected", "prepared", "--phase", "venv-transitioned"
            )
            self.run_transaction(
                "phase", "--expected", "venv-transitioned", "--phase", "staged"
            )
            self.run_transaction(
                "record-services",
                *service_args,
                "--timer-active-before",
                "active" if timer_active else "inactive",
                *(
                    option
                    for unit in ("deadlock-api", "deadlock-worker", "deadlock-web")
                    for option in ("--service-enabled", f"{unit}=disabled")
                )
                if current is None
                else tuple(
                    option
                    for unit in ("deadlock-api", "deadlock-worker", "deadlock-web")
                    for option in ("--service-enabled", f"{unit}=enabled")
                ),
                "--timer-enabled-before",
                "disabled" if current is None else "enabled",
            )
        return candidate

    def create_wrapper_rollback_transaction(
        self, current: Path, previous: Path, *, phase: str
    ) -> None:
        needs_cache_purge = phase in transaction.ROLLBACK_CACHE_PURGE_PROOF_PHASES
        purge_python = (
            self.prepare_wrapper_cache_purge(current) if needs_cache_purge else None
        )
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        self.run_transaction(
            "create",
            "--operation",
            "rollback",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            str(current),
            "--previous-before",
            str(previous),
            "--candidate-release",
            str(current),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(snapshot),
            "--snapshot",
            str(snapshot),
            "--transition",
            "none",
        )
        if phase != "prepared":
            for expected, next_phase in (
                ("prepared", "venv-transitioned"),
                ("venv-transitioned", "current-switched"),
                ("current-switched", "pointers-switched"),
                ("pointers-switched", "rollback-runtime-pending"),
                ("rollback-runtime-pending", "rollback-cache-purged"),
                ("rollback-cache-purged", "services-restarted"),
                ("services-restarted", "smoke-passed"),
                ("smoke-passed", "rollback-runtime-applied"),
            ):
                if next_phase == "rollback-cache-purged":
                    purge = self.run_transaction(
                        "purge-legacy-profile-access-cache", check=False
                    )
                    self.assertEqual(purge.returncode, 0, purge.stderr)
                    if purge_python is not None:
                        original = purge_python["original"]
                        python_path = cast(Path, purge_python["path"])
                        python_path.unlink(missing_ok=True)
                        if original is not None:
                            kind, value, mode = original
                            if kind == "symlink":
                                python_path.symlink_to(cast(str, value))
                            else:
                                python_path.write_bytes(cast(bytes, value))
                                python_path.chmod(cast(int, mode))
                self.run_transaction("phase", "--expected", expected, "--phase", next_phase)
                if next_phase == phase:
                    return
            raise AssertionError(f"unsupported rollback test phase: {phase}")

    def prepare_wrapper_cache_purge(
        self, current: Path
    ) -> dict[str, object]:
        """Make the immutable purge CLI fixture use the pinned system Python."""

        venv = self.shared / "venv"
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        python_path = bin_dir / "python"
        original: tuple[str, object, int | None] | None = None
        if python_path.is_symlink():
            original = ("symlink", os.readlink(python_path), None)
        elif python_path.exists():
            metadata = python_path.lstat()
            original = ("file", python_path.read_bytes(), stat.S_IMODE(metadata.st_mode))
        python_path.unlink(missing_ok=True)
        python_path.symlink_to("/usr/bin/python3.12")

        self.add_cache_purge_source_fixture(current)
        return {"path": python_path, "original": original}

    def prepare_wrapper_cache_purge_venv(self, marker: str) -> None:
        """Create the no-op shared venv before the transaction snapshots it."""

        venv = self.shared / "venv"
        self.add_fake_venv(venv, marker=marker)
        self.write_fake_python(venv / "bin/python")

    def add_cache_purge_source_fixture(self, release: Path) -> None:
        """Give a synthetic purge source its fixed env and import closure."""

        tools = release / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        safe_env = tools / "platform_safe_env_exec.py"
        safe_env.write_text(
            "import base64\n"
            "value = base64.b64encode(b'redis://127.0.0.1:6379/15').decode('ascii')\n"
            "print('PLATFORM_REDIS_URL\\t' + value)\n",
            encoding="ascii",
        )
        safe_env.chmod(0o555)
        services = release / "apps/platform_api/app/services"
        services.mkdir(parents=True, exist_ok=True)
        for package in (
            release / "apps/platform_api/app/__init__.py",
            services.parent / "__init__.py",
            services / "__init__.py",
        ):
            package.write_text("", encoding="ascii")
            package.chmod(0o444)
        (services / "tournament_profile_access.py").write_text(
            "async def purge_legacy_profile_access_cache():\n"
            "    return 0\n",
            encoding="ascii",
        )
        (services / "tournament_profile_access.py").chmod(0o444)

    def write_failing_systemctl(
        self, label: str, *, exit_code: int = 99
    ) -> tuple[Path, Path]:
        log = self.root / f"{label}-systemctl.log"
        path = self.root / f"systemctl-{label}"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(log)!r}\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path, log

    def add_current_control_helper_bombs(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir()
        for name in (
            "platform_release_transaction.py",
            "platform_release_systemd_state.py",
            "platform_release_restore_runtime.sh",
            "platform_release_lock.sh",
        ):
            helper = tools / name
            helper.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"printf '%s\\n' used >> {str(self.root / 'current-helper-used')!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)

    def add_bound_release_tools(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir()
        helper_names = (
            "platform_install_systemd_units.sh",
            "platform_install_nginx.py",
            "platform_deploy_smoke.py",
            "platform_live_qa_runtime_install.py",
            "platform_install_logging.sh",
            "platform_prepare_service_user.sh",
            "platform_render_service_envs.py",
            "platform_deploy_smoke_impl.py",
            "platform_safe_env_exec.py",
            "platform_release_restore_runtime.sh",
            "platform_release_systemd_state.py",
            "platform_release_transaction.py",
            "platform_release_lock.sh",
        )
        control_names = {
            "platform_release_restore_runtime.sh",
            "platform_release_systemd_state.py",
            "platform_release_transaction.py",
            "platform_release_lock.sh",
        }
        for name in helper_names:
            helper = tools / name
            if name in control_names:
                helper.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    f"printf '%s\\n' used >> {str(self.root / 'current-helper-used')!r}\n"
                    "exit 99\n",
                    encoding="utf-8",
                )
            else:
                helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "a" * 40}) + "\n",
            encoding="ascii",
        )

    def copy_deploy_script_with_fault(
        self,
        name: str,
        needle: str | None,
        replacement: str | None,
        systemctl: Path,
    ) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        script = script.replace(
            "  release_preflight\n  set_phase nginx-applied smoke-passed\n",
            "  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n",
            1,
        )
        if needle is not None:
            self.assertIn(needle, script)
            assert replacement is not None
            script = script.replace(needle, replacement, 1)
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_deploy_script_with_curl(
        self,
        name: str,
        systemctl: Path,
        curl: Path,
        *,
        timeout_seconds: int | None = None,
    ) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace(
            "  release_preflight\n  set_phase nginx-applied smoke-passed\n",
            "  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n",
            1,
        )
        script = script.replace("/usr/bin/curl", str(curl))
        if timeout_seconds is not None:
            script = script.replace(
                "SYSTEMD_OPERATION_TIMEOUT_SECONDS=120",
                f"SYSTEMD_OPERATION_TIMEOUT_SECONDS={timeout_seconds}",
                1,
            )
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_runtime_with_fault(
        self, name: str, needle: str, replacement: str
    ) -> Path:
        script = RUNTIME_RESTORE_SCRIPT.read_text()
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement, 1))
        target.chmod(0o755)
        self.install_test_lock_helper(target.parent / "platform_release_lock.sh")
        shutil.copy2(
            REPO_ROOT / "platform/tools/platform_release_systemd_state.py",
            target.parent / "platform_release_systemd_state.py",
        )
        (target.parent / "platform_release_systemd_state.py").chmod(0o755)
        return target

    def copy_rollback_with_runtime(self, name: str, runtime: Path) -> Path:
        script = self.script_with_physical_tools(ROLLBACK_SCRIPT)
        needle = 'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"'
        self.assertIn(needle, script)
        script = script.replace(needle, f'RUNTIME_RESTORE_TOOL="{runtime}"', 1)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_rollback_with_systemctl(
        self, systemctl: Path, runtime_restore: Path | None = None
    ) -> Path:
        script = self.script_with_physical_tools(ROLLBACK_SCRIPT)
        if runtime_restore is not None:
            script = script.replace(
                'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
                f'RUNTIME_RESTORE_TOOL="{runtime_restore}"',
                1,
            )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        target = self.root / "rollback-with-stateful-systemctl.sh"
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_test_runtime_restore(self) -> Path:
        path = self.root / "runtime-restore.sh"
        args_log = path.with_suffix(".args")
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(args_log)!r}\n"
            "APP_DIR=\"\"; RELEASE=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    --app-dir) APP_DIR=\"$2\"; shift 2 ;;\n"
            "    --release) RELEASE=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "PLATFORM_APP_DIR=\"$APP_DIR\" \"$RELEASE/tools/platform_install_systemd_units.sh\"\n"
            "\"$APP_DIR/shared/venv/bin/python\" \"$RELEASE/tools/platform_install_nginx.py\" --apply\n"
        )
        path.chmod(0o755)
        return path

    def prepare_install_state(
        self,
        *,
        with_fake_python: bool = False,
        service_state_required: bool = False,
    ) -> tuple[Path, Path, Path]:
        current = self.add_release("current")
        previous = self.add_release("previous")
        candidate = self.add_release("candidate")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        (candidate / ".rollback").mkdir()
        freeze = candidate / "requirements-platform.freeze.txt"
        freeze.write_text("pip==test\n")
        freeze.chmod(0o444)
        (candidate / ".rollback" / "previous-release").write_text(f"{current}\n")
        (candidate / ".rollback" / "previous-release").chmod(0o600)
        (candidate / ".rollback" / "venv-transition").write_text("unchanged\n")
        (candidate / ".rollback" / "venv-transition").chmod(0o600)
        digest = hashlib.sha256(freeze.read_bytes()).hexdigest()
        (candidate / ".rollback" / "shared-freeze.sha256").write_text(f"{digest}\n")
        (candidate / ".rollback" / "shared-freeze.sha256").chmod(0o600)
        self.add_fake_venv(self.shared / "venv", marker="shared")
        if with_fake_python:
            self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_transaction(
            candidate,
            current,
            previous,
            service_state_required=service_state_required,
        )
        if not service_state_required:
            self.run_transaction(
                "phase", "--expected", "prepared", "--phase", "venv-transitioned"
            )
            self.run_transaction(
                "phase", "--expected", "venv-transitioned", "--phase", "staged"
            )
            self.run_transaction(
                "record-services",
                "--service-state",
                "deadlock-api=active",
                "--service-state",
                "deadlock-worker=active",
                "--service-state",
                "deadlock-web=active",
                "--timer-active-before",
                "active",
            )
        return current, previous, candidate

    def advance_install_state(self, candidate: Path, current: Path, *, phase: str) -> None:
        phases = (
            "venv-transitioned",
            "staged",
            "migration-pending",
            "migration-applied",
            "activation-pending",
            "services-restarted",
            "nginx-applied",
            "smoke-passed",
            "activation-committed",
        )
        current_phase = self.state_phase()
        if current_phase in phases:
            phases = phases[phases.index(current_phase) + 1 :]
        for next_phase in phases:
            self.run_transaction("phase", "--expected", self.state_phase(), "--phase", next_phase)
            if next_phase == phase:
                break

    def create_transaction(
        self,
        candidate: Path,
        current: Path,
        previous: Path,
        *,
        service_state_required: bool = False,
    ) -> None:
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            str(current),
            "--previous-before",
            str(previous),
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(self.shared / ".venv-install-candidate.0000"),
            "--snapshot",
            str(candidate / ".rollback" / "shared-venv-before-install"),
            "--transition",
            "none",
        )

    def prepare_rollback_venv(self, current: Path) -> None:
        self.add_fake_venv(self.shared / "venv", marker="new")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        shutil.copytree(self.shared / "venv", snapshot)
        self.prepare_wrapper_cache_purge(current)
        (rollback / "previous-release").write_text(f"{self.releases / 'previous'}\n")
        (rollback / "previous-release").chmod(0o600)
        (rollback / "venv-transition").write_text("snapshot\n")
        (rollback / "venv-transition").chmod(0o600)

    def add_runtime_stubs(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir(exist_ok=True)
        units = tools / "platform_install_systemd_units.sh"
        units.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$PLATFORM_TEST_UNITS_LABEL\" > \"$PLATFORM_TEST_UNITS_STATE\"\n"
        )
        units.chmod(0o755)
        nginx = tools / "platform_install_nginx.py"
        nginx.write_text(
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "if len(sys.argv) > 1 and sys.argv[1] == '--apply':\n"
            "    Path(os.environ['PLATFORM_TEST_NGINX_STATE']).write_text(\n"
            "        os.environ['PLATFORM_TEST_NGINX_LABEL'] + '\\n', encoding='ascii'\n"
            "    )\n",
            encoding="ascii",
        )
        nginx.chmod(0o755)
        smoke = tools / "platform_deploy_smoke.py"
        smoke.write_text("pass\n", encoding="ascii")
        smoke.chmod(0o755)
        runtime_installer = tools / "platform_live_qa_runtime_install.py"
        runtime_installer.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
        runtime_installer.chmod(0o755)
        for name in (
            "platform_install_logging.sh",
            "platform_prepare_service_user.sh",
            "platform_render_service_envs.py",
            "platform_deploy_smoke_impl.py",
            "platform_safe_env_exec.py",
            "platform_release_restore_runtime.sh",
            "platform_release_systemd_state.py",
            "platform_release_transaction.py",
            "platform_release_lock.sh",
        ):
            helper = tools / name
            helper.write_text("#!/usr/bin/env bash\nexit 0\n" if name.endswith(".sh") else "# test stub\n")
            helper.chmod(0o755)

    def install_live_qa_reconcile_fixture(
        self, release: Path, mode_path: Path, log_path: Path
    ) -> None:
        runtime_installer = release / "tools/platform_live_qa_runtime_install.py"
        runtime_installer.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            f"mode_path = Path({str(mode_path)!r})\n"
            f"log_path = Path({str(log_path)!r})\n"
            f"payload_path = Path({str(log_path.with_suffix('.payload'))!r})\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'reconcile':\n"
            "    with log_path.open('a', encoding='ascii') as stream:\n"
            "        stream.write('reconcile\\n')\n"
            "    payload_path.write_text('published\\n', encoding='ascii')\n"
            "    if mode_path.read_text(encoding='ascii').strip() == 'hang':\n"
            "        time.sleep(60)\n"
            "    if mode_path.read_text(encoding='ascii').strip() == 'slow':\n"
            "        time.sleep(8)\n",
            encoding="utf-8",
        )
        python = self.shared / "venv/bin/python"
        python.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${1:-}\" == \"-I\" && \"${2:-}\" == *platform_live_qa_runtime_install.py ]]; then\n"
            "  exec /usr/bin/python3 \"$@\"\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        python.chmod(0o755)

    def install_initial_systemd_units_fixture(
        self, release: Path, systemctl: Path
    ) -> None:
        installer = release / "tools/platform_install_systemd_units.sh"
        installer.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${PLATFORM_ENABLE_SYSTEMD_UNITS:-0}\" != 1 ]]; then exit 0; fi\n"
            f"{systemctl} enable deadlock-api.service deadlock-worker.service deadlock-web.service\n"
            f"{systemctl} enable deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n"
            f"{systemctl} start deadlock-maintenance.timer deadlock-logrotate.timer deadlock-cloudflare-ips.timer deadlock-health-monitor.timer\n",
            encoding="utf-8",
        )
        installer.chmod(0o755)

    def write_hanging_curl(self, mode_path: Path, log_path: Path) -> Path:
        curl = self.root / f"curl-{mode_path.stem}"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(log_path)!r}\n"
            f"if [[ \"$(cat {str(mode_path)!r})\" == hang ]]; then sleep 60; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        curl.chmod(0o755)
        return curl

    def add_release(self, name: str) -> Path:
        release = self.releases / name
        release.mkdir(mode=0o755)
        release.chmod(0o755)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": hashlib.sha1(name.encode("utf-8")).hexdigest()})
            + "\n",
            encoding="ascii",
        )
        (release / "RELEASE.json").chmod(0o444)
        return release

    def add_fake_venv(self, venv: Path, *, marker: str) -> None:
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {marker!r}\n")
        python.chmod(0o755)
        (venv / "deps-version").write_text(f"{marker}\n")

    def write_fake_python(self, path: Path) -> None:
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  *platform_install_nginx.py)\n"
            "    if [[ \"${2:-}\" == \"--apply\" ]]; then\n"
            "      printf '%s\\n' \"$PLATFORM_TEST_NGINX_LABEL\" > \"$PLATFORM_TEST_NGINX_STATE\"\n"
            "    fi\n"
            "    ;;\n"
            "esac\n"
        )
        path.chmod(0o755)

    def switch_pointer(self, name: str, target: Path) -> None:
        pointer = self.app_dir / name
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(target)

    def state_phase(self) -> str:
        import json

        return json.loads((self.shared / STATE_NAME).read_text())["phase"]

    def run_transaction(
        self, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        normalized = list(args)
        if normalized and normalized[0] in {"record-services", "prepare-quiesce"}:
            if "--service-enabled" not in normalized:
                for unit in ("deadlock-api", "deadlock-worker", "deadlock-web"):
                    normalized.extend(("--service-enabled", f"{unit}=enabled"))
                normalized.extend(("--timer-enabled-before", "disabled"))
        return self.run_script(
            TRANSACTION_TOOL,
            *normalized,
            "--state",
            str(self.shared / STATE_NAME),
            check=check,
        )

    def copy_script_with_replacement(
        self, source: Path, name: str, needle: str, replacement: str
    ) -> Path:
        script = self.script_with_physical_tools(source)
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement, 1))
        target.chmod(0o755)
        return target

    def copy_abort_script(self, name: str) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        runtime_restore = self.root / "test-runtime-restore.sh"
        runtime_restore.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "APP_DIR=\"\"\n"
            "RELEASE=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    --app-dir) APP_DIR=\"$2\"; shift 2 ;;\n"
            "    --release) RELEASE=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "PLATFORM_APP_DIR=\"$APP_DIR\" \"$RELEASE/tools/platform_install_systemd_units.sh\"\n"
            "\"$APP_DIR/shared/venv/bin/python\" \"$RELEASE/tools/platform_install_nginx.py\" --apply\n"
        )
        runtime_restore.chmod(0o755)
        script = script.replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{runtime_restore}"',
            1,
        )
        script = script.replace(
            'PLATFORM_APP_DIR="$APP_DIR" "$APP_DIR/current/tools/platform_install_systemd_units.sh"',
            "/usr/bin/true",
        )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_abort_script_with_systemctl(self, systemctl: Path) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        runtime_restore = self.write_test_runtime_restore()
        script = script.replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{runtime_restore}"',
            1,
        )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / "abort-with-stateful-systemctl.sh"
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_deploy_migration_script(self, systemctl: Path) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        preflight_tool = '"$TOOLS_DIR/platform_release_preflight.sh"'
        self.assertIn(preflight_tool, script)
        # Keep the deploy wrapper's topology selection and dynamic
        # --require-previous/--allow-no-previous/--allow-initial-install
        # flags under test; only bypass the host-dependent preflight body.
        script = script.replace(preflight_tool, "/usr/bin/true", 1)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / "deploy-migration-failure.sh"
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_initial_deploy_with_fault(
        self,
        name: str,
        systemctl: Path,
        needle: str,
        replacement: str,
    ) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        preflight_tool = '"$TOOLS_DIR/platform_release_preflight.sh"'
        self.assertIn(preflight_tool, script)
        # Preserve the topology-derived flag array while replacing only the
        # host-dependent preflight executable with a no-op.
        script = script.replace(preflight_tool, "/usr/bin/true", 1)
        self.assertIn(needle, script)
        script = script.replace(needle, replacement, 1)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_stateful_systemctl(self, states: dict[str, str]) -> Path:
        state_path = self.root / "systemd-state.json"
        state_path.write_text(json.dumps(states, sort_keys=True))
        enabled_path = self.root / "systemd-enabled.json"
        owned_units = (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.service",
            "deadlock-logrotate.timer",
            "deadlock-offsite-backup.service",
            "deadlock-offsite-backup.timer",
            "deadlock-cloudflare-ips.service",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.service",
            "deadlock-health-monitor.timer",
        )
        static_units = {
            "deadlock-maintenance.service",
            "deadlock-logrotate.service",
            "deadlock-offsite-backup.service",
            "deadlock-cloudflare-ips.service",
            "deadlock-health-monitor.service",
        }
        enabled_path.write_text(
            json.dumps(
                {
                    unit: (
                        "static"
                        if unit in static_units
                        else "disabled"
                        if unit == "deadlock-offsite-backup.timer"
                        else "enabled"
                    )
                    for unit in owned_units
                },
                sort_keys=True,
            )
        )
        log_path = self.root / "systemctl.log"
        log_path.write_text("")
        path = self.root / "systemctl"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"state_path = Path({str(state_path)!r})\n"
            f"enabled_path = Path({str(enabled_path)!r})\n"
            f"log_path = Path({str(log_path)!r})\n"
            "state = json.loads(state_path.read_text())\n"
            "enabled = json.loads(enabled_path.read_text())\n"
            "def state_unit(unit):\n"
            "    if unit in state:\n"
            "        return unit\n"
            "    if unit.endswith('.service') and unit[:-8] in state:\n"
            "        return unit[:-8]\n"
            "    service_unit = unit + '.service'\n"
            "    return service_unit if service_unit in state else unit\n"
            "def enabled_unit(unit):\n"
            "    if unit in enabled:\n"
            "        return unit\n"
            "    if unit.endswith('.service') and unit[:-8] in enabled:\n"
            "        return unit[:-8]\n"
            "    service_unit = unit + '.service'\n"
            "    return service_unit if service_unit in enabled else unit\n"
            "argv = sys.argv[1:]\n"
            "action = argv[0] if argv else \"\"\n"
            "units = [value for value in argv[1:] if not value.startswith(\"-\")]\n"
            "sleep_seconds = os.getenv(\"PLATFORM_TEST_SYSTEMCTL_SLEEP_SECONDS\")\n"
            "sleep_action = os.getenv(\"PLATFORM_TEST_SYSTEMCTL_SLEEP_ACTION\")\n"
            "if sleep_seconds and (not sleep_action or sleep_action == action):\n"
            "    import time\n"
            "    time.sleep(float(sleep_seconds))\n"
            "if action == \"is-active\":\n"
            "    unit = units[0]\n"
            "    value = state.get(state_unit(unit), \"inactive\")\n"
            "    if \"--quiet\" not in argv:\n"
            "        print(value)\n"
            "    raise SystemExit(0 if value == \"active\" else 3)\n"
            "if action == \"is-enabled\":\n"
            "    unit = units[0]\n"
            "    value = enabled.get(enabled_unit(unit), \"static\")\n"
            "    if \"--quiet\" not in argv:\n"
            "        print(value)\n"
            "    raise SystemExit(0 if value in {\"enabled\", \"static\"} else 1)\n"
            "if action in {\"enable\", \"disable\"}:\n"
            "    value = \"enabled\" if action == \"enable\" else \"disabled\"\n"
            "    for unit in units:\n"
            "        key = enabled_unit(unit)\n"
            "        if enabled.get(key) != \"static\":\n"
            "            enabled[key] = value\n"
            "        with log_path.open(\"a\", encoding=\"utf-8\") as stream:\n"
            "            stream.write(f\"{action} {unit}\\n\")\n"
            "    enabled_path.write_text(json.dumps(enabled, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if action in {\"stop\", \"start\", \"restart\"}:\n"
            "    for unit in units:\n"
                "        with log_path.open(\"a\", encoding=\"utf-8\") as stream:\n"
                "            stream.write(f\"{action} {unit}\\n\")\n"
            "        if action == \"restart\" and unit == \"deadlock-api\" and os.getenv(\"PLATFORM_TEST_SYSTEMCTL_FAIL_RESTART\") == \"1\":\n"
            "            raise SystemExit(1)\n"
            "        key = state_unit(unit)\n"
            "        if key in state:\n"
            "            state[key] = \"inactive\" if action == \"stop\" else \"active\"\n"
            "    state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "    if os.getenv(\"PLATFORM_TEST_SYSTEMCTL_KILL_AFTER\") == f\"{action} {units[0]}\":\n"
            "        os.kill(os.getpid(), 9)\n"
            "    raise SystemExit(0)\n"
            "if action in {\"daemon-reload\", \"reload\"}:\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n"
        )
        path.chmod(0o755)
        return path

    def write_initial_systemctl(self) -> Path:
        """Return a fake manager with the strict clean-install baseline."""

        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-maintenance.timer": "inactive",
                "deadlock-logrotate.timer": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-health-monitor.timer": "inactive",
            }
        )
        enabled_path = self.root / "systemd-enabled.json"
        enabled = json.loads(enabled_path.read_text(encoding="utf-8"))
        for unit in (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.timer",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.timer",
        ):
            enabled[unit] = "disabled"
        enabled_path.write_text(json.dumps(enabled, sort_keys=True))
        return systemctl

    def script_with_physical_tools(self, source: Path) -> str:
        script = source.read_text()
        needle = 'TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"'
        self.assertIn(needle, script)
        return script.replace(needle, f'TOOLS_DIR="{self.tools_dir}"', 1)

    def install_test_lock_helper(self, destination: Path) -> None:
        """Install a test-local helper while retaining production validation."""

        helper = (REPO_ROOT / "platform/tools/platform_release_lock.sh").read_text(
            encoding="utf-8"
        )
        helper = helper.replace(
            "/run/lock/oldsparky-platform-release.lock",
            str(self.release_lock_path),
        )
        destination.write_text(helper, encoding="utf-8")
        destination.chmod(0o755)

    def run_script(
        self,
        script: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env["PLATFORM_ENVIRONMENT"] = "test"
        command_env["PLATFORM_TESTING"] = "1"
        command_env.update(env or {})
        result = subprocess.run(
            [str(script), *args],
            cwd=REPO_ROOT,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
        if check and result.returncode != 0:
            self.fail(f"{script} failed: {result.returncode}\n{result.stderr}")
        return result


if __name__ == "__main__":
    unittest.main()
