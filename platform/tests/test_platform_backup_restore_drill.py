from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import datetime as dt
import hashlib
import importlib.util
from io import StringIO
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

from tests import platform_test_lock_support as lock_support
from tools import platform_backup_supervisor


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_restore_drill", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
backup_drill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup_drill
SPEC.loader.exec_module(backup_drill)

MANIFEST_SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_manifest.py"
MANIFEST_SPEC = importlib.util.spec_from_file_location(
    "platform_backup_manifest_for_restore_tests", MANIFEST_SCRIPT_PATH
)
assert MANIFEST_SPEC is not None and MANIFEST_SPEC.loader is not None
manifest_contract = importlib.util.module_from_spec(MANIFEST_SPEC)
sys.modules[MANIFEST_SPEC.name] = manifest_contract
MANIFEST_SPEC.loader.exec_module(manifest_contract)

TRUSTED_RUNUSER = "/usr/sbin/runuser"
TRUSTED_CREATEDB = "/usr/bin/createdb"
TRUSTED_DROPDB = "/usr/bin/dropdb"
TEST_HELPERS = platform_backup_supervisor.TrustedPostgresHelpers(
    runuser=TRUSTED_RUNUSER,
    createdb=TRUSTED_CREATEDB,
    dropdb=TRUSTED_DROPDB,
    psql="/usr/bin/psql",
    pg_dump="/usr/bin/pg_dump",
    pg_restore="/usr/bin/pg_restore",
)


@contextmanager
def _held_test_lock():
    with tempfile.TemporaryDirectory() as temporary_dir:
        path = pathlib.Path(temporary_dir) / platform_backup_supervisor.BACKUP_LOCK_PATH.name
        with lock_support.root_owned_backup_lock(platform_backup_supervisor, path) as lock:
            yield lock


def _trusted_source(source_root: pathlib.Path) -> pathlib.Path:
    versions = source_root / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "001.py").write_text(
        "revision = '20260913_0053'\ndown_revision = None\n",
        encoding="utf-8",
    )
    return source_root


class PlatformBackupRestoreDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._ensure_monitor_patch = mock.patch.object(
            platform_backup_supervisor, "ensure_process_monitor"
        )
        self._ensure_monitor_patch.start()
        self.addCleanup(self._ensure_monitor_patch.stop)
        self._restore_lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore_lock_dir.cleanup)
        restore_lock_path = pathlib.Path(self._restore_lock_dir.name) / "restore-lifecycle.lock"
        self._restore_lock_path_patch = mock.patch.object(
            backup_drill, "RESTORE_LIFECYCLE_LOCK_PATH", restore_lock_path
        )
        self._restore_lock_path_patch.start()
        self.addCleanup(self._restore_lock_path_patch.stop)
        for name, value in (
            ("RESTORE_LIFECYCLE_LOCK_OWNER", os.geteuid()),
            ("RESTORE_LIFECYCLE_LOCK_GROUP", os.getegid()),
        ):
            patcher = mock.patch.object(backup_drill, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _creator_args(self, output_dir: pathlib.Path, *, dump_only: bool = True) -> argparse.Namespace:
        return argparse.Namespace(
            env_file=str(output_dir / ".env.platform"),
            output_dir=str(output_dir),
            keep=2,
            admin_database_url=None,
            dump_only=dump_only,
        )

    def _create_backup(
        self,
        args: argparse.Namespace,
        *,
        source_root: pathlib.Path | None = None,
    ) -> dict[str, object]:
        with _held_test_lock() as lock:
            return platform_backup_supervisor._run_local_backup_scope(
                args,
                app_dir=pathlib.Path(args.output_dir),
                source_root=source_root,
                _restore_module=backup_drill,
                lock=lock,
                callback=lambda capability, trusted_head, _restore: backup_drill.create_backup(
                    args,
                    capability=capability,
                    trusted_alembic_head=trusted_head,
                )
            )

    def _creator_environment(self) -> dict[str, str]:
        return {
            "PLATFORM_DATABASE_URL": "postgresql://platform_user@127.0.0.1:5432/platformdb"
        }

    def test_parse_database_url_accepts_platformdb_and_decodes_credentials(self) -> None:
        target = backup_drill.parse_database_url(
            "postgresql+asyncpg://platform%5Fuser:p%40ss@127.0.0.1:5433/platformdb"
        )

        self.assertEqual(target.username, "platform_user")
        self.assertEqual(target.password, "p@ss")
        self.assertEqual(target.host, "127.0.0.1")
        self.assertEqual(target.port, 5433)
        self.assertEqual(target.database, "platformdb")

    def test_parse_database_url_refuses_legacy_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected the isolated platformdb"):
            backup_drill.parse_database_url(
                "postgresql+asyncpg://platform_user:secret@127.0.0.1:5432/sparkydb"
            )

    def test_expected_alembic_head_is_derived_from_trusted_source_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            versions = root / "alembic" / "versions"
            versions.mkdir(parents=True)
            (versions / "001.py").write_text(
                "revision = '001'\ndown_revision = None\n", encoding="utf-8"
            )
            (versions / "002.py").write_text(
                "revision = '002'\ndown_revision = '001'\n", encoding="utf-8"
            )
            self.assertEqual(backup_drill.expected_alembic_head(root), "002")
            (versions / "003.py").write_text(
                "revision = '003'\ndown_revision = '001'\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one head"):
                backup_drill.expected_alembic_head(root)

    def test_local_admin_commands_use_postgres_os_user(self) -> None:
        target = backup_drill.DatabaseTarget("127.0.0.1", 5432, "platform_user", None, "platformdb")
        database = "platform_restore_drill_test"

        self.assertEqual(
            backup_drill.local_postgres_admin_command(
                "create", target, database, TEST_HELPERS
            ),
            [
                TRUSTED_RUNUSER,
                "-u",
                "postgres",
                "--",
                TRUSTED_CREATEDB,
                "--owner",
                "platform_user",
                database,
            ],
        )
        self.assertEqual(
            backup_drill.local_postgres_admin_command(
                "drop", target, database, TEST_HELPERS
            ),
            [TRUSTED_RUNUSER, "-u", "postgres", "--", TRUSTED_DROPDB, "--if-exists", database],
        )

    def test_remote_admin_commands_use_explicit_absolute_postgres_helpers(self) -> None:
        admin_target = backup_drill.DatabaseTarget(
            "db.internal", 5433, "platform_admin", "secret", "postgres"
        )
        app_target = backup_drill.DatabaseTarget(
            "db.internal", 5433, "platform_user", None, "platformdb"
        )
        database = "platform_restore_drill_test"
        base = ["--host", "db.internal", "--port", "5433", "--username", "platform_admin"]

        self.assertEqual(
            backup_drill.remote_admin_command(
                "create", admin_target, app_target, database, TEST_HELPERS
            ),
            [TRUSTED_CREATEDB, *base, "--owner", "platform_user", database],
        )
        self.assertEqual(
            backup_drill.remote_admin_command(
                "drop", admin_target, app_target, database, TEST_HELPERS
            ),
            [TRUSTED_DROPDB, *base, "--if-exists", database],
        )

    def test_public_postgres_resolver_rejects_unsafe_inputs(self) -> None:
        for unsafe in ("/tmp/createdb", "../createdb", "createdb\x00"):
            with self.assertRaises(platform_backup_supervisor.BackupCommandError):
                platform_backup_supervisor.trusted_executable(unsafe)

    def test_restore_drill_captures_extension_output_for_json_callers(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "secret", "platformdb"
        )
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess(
                [],
                0,
                '{"name":"platform_restore_drill_' + "a" * 32 + '","oid":42,"owner":"platform_user"}\n',
                "",
            ),
            subprocess.CompletedProcess([], 0, "CREATE EXTENSION\n", ""),
            subprocess.CompletedProcess([], 0, "CREATE SCHEMA\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "22\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "20260801_0036\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess(
                [],
                0,
                '{"name":"platform_restore_drill_' + "a" * 32 + '","oid":42,"owner":"platform_user"}\n',
                "",
            ),
            subprocess.CompletedProcess([], 0, "", ""),
        ]

        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch.object(
            backup_drill, "run_command", side_effect=responses
        ) as run_command, mock.patch.object(
            backup_drill, "_trusted_alembic_head", return_value="20260801_0036"
        ), mock.patch.object(backup_drill.secrets, "token_hex", return_value="a" * 32):
            table_count = backup_drill.perform_restore_drill(
                pathlib.Path(temporary_dir) / "backup.dump",
                app_target=target,
                admin_target=None,
                helpers=TEST_HELPERS,
                timestamp_slug="20260720T120000Z",
                expected_alembic_head="20260801_0036",
                deadline=platform_backup_supervisor.operation_deadline(30.0),
            )

        self.assertEqual(table_count, 22)
        self.assertTrue(run_command.call_args_list[3].kwargs["capture_output"])
        self.assertTrue(run_command.call_args_list[4].kwargs["capture_output"])
        self.assertIn("CREATE SCHEMA platform", run_command.call_args_list[4].args[0][-1])
        self.assertIn("--schema=platform", run_command.call_args_list[5].args[0])
        self.assertIn("--schema=public", run_command.call_args_list[6].args[0])

    @staticmethod
    def _identity_json(name: str, *, oid: int = 42, owner: str = "platform_user") -> str:
        return json.dumps({"name": name, "oid": oid, "owner": owner}) + "\n"

    def test_restore_drill_drops_only_matching_database_identity(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        database = "platform_restore_drill_" + "a" * 32
        identity = self._identity_json(database)
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, identity, ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "head\n", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
            subprocess.CompletedProcess([], 0, identity, ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with (
            mock.patch.object(backup_drill, "run_command", side_effect=responses) as run_command,
            mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
            mock.patch.object(backup_drill.secrets, "token_hex", return_value="a" * 32),
            mock.patch.object(backup_drill, "REQUIRED_PLATFORM_EXTENSIONS", ()),
        ):
            self.assertEqual(
                backup_drill.perform_restore_drill(
                    pathlib.Path("/tmp/backup.dump"),
                    app_target=target,
                    admin_target=None,
                    helpers=TEST_HELPERS,
                    timestamp_slug="ignored",
                    expected_alembic_head="head",
                    deadline=platform_backup_supervisor.operation_deadline(30.0),
                ),
                1,
            )

        command_names = [
            pathlib.Path(
                call.args[0][4] if pathlib.Path(call.args[0][0]).name == "runuser" else call.args[0][0]
            ).name
            for call in run_command.call_args_list
        ]
        self.assertEqual(command_names[-1], "dropdb")
        self.assertGreater(
            run_command.call_args_list[2].kwargs["cleanup_reserve_seconds"], 0
        )
        self.assertEqual(
            run_command.call_args_list[-2].kwargs["cleanup_reserve_seconds"], 0.0
        )

    def test_restore_drill_never_drops_replaced_or_owner_mismatched_database(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        for changed in (
            {"oid": 99, "owner": "platform_user"},
            {"oid": 42, "owner": "other_owner"},
        ):
            with self.subTest(changed=changed):
                database = "platform_restore_drill_" + "b" * 32
                responses = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess(
                        [], 0, self._identity_json(database), ""
                    ),
                    RuntimeError("restore failed"),
                    subprocess.CompletedProcess(
                        [], 0, self._identity_json(database, **changed), ""
                    ),
                ]
                with (
                    mock.patch.object(backup_drill, "run_command", side_effect=responses) as run_command,
                    mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value="b" * 32),
                    mock.patch.object(backup_drill, "REQUIRED_PLATFORM_EXTENSIONS", ()),
                ):
                    with self.assertRaisesRegex(RuntimeError, "restore failed") as raised:
                        backup_drill.perform_restore_drill(
                            pathlib.Path("/tmp/backup.dump"),
                            app_target=target,
                            admin_target=None,
                            helpers=TEST_HELPERS,
                            timestamp_slug="ignored",
                            expected_alembic_head="head",
                            deadline=platform_backup_supervisor.operation_deadline(30.0),
                        )
                self.assertEqual(
                    getattr(raised.exception, "backup_cleanup_unproven", None),
                    database,
                )
                command_names = [
                    pathlib.Path(
                        call.args[0][4]
                        if pathlib.Path(call.args[0][0]).name == "runuser"
                        else call.args[0][0]
                    ).name
                    for call in run_command.call_args_list
                ]
                self.assertNotIn("dropdb", command_names)

    def test_restore_lifecycle_lock_blocks_an_in_scope_second_creator(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            lock_path = pathlib.Path(temporary_dir) / "restore-lifecycle.lock"
            with mock.patch.object(backup_drill, "RESTORE_LIFECYCLE_LOCK_PATH", lock_path):
                with backup_drill._restore_lifecycle_lock(time.monotonic() + 5):
                    with mock.patch.object(backup_drill, "run_command") as run_command:
                        with self.assertRaises(platform_backup_supervisor.BackupCommandTimeout):
                            backup_drill.perform_restore_drill(
                                pathlib.Path("/tmp/backup.dump"),
                                app_target=target,
                                admin_target=None,
                                helpers=TEST_HELPERS,
                                timestamp_slug="ignored",
                                deadline=time.monotonic() + 2.2,
                            )
                        run_command.assert_not_called()

    def test_restore_drill_rejects_initial_owner_mismatch_without_adoption(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        database = "platform_restore_drill_" + "e" * 32
        with (
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess(
                        [], 0, self._identity_json(database, owner="other_owner"), ""
                    ),
                ],
            ) as run_command,
            mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
            mock.patch.object(backup_drill.secrets, "token_hex", return_value="e" * 32),
        ):
            with self.assertRaises(
                platform_backup_supervisor.BackupCleanupUnproven
            ) as raised:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/tmp/backup.dump"),
                    app_target=target,
                    admin_target=None,
                    helpers=TEST_HELPERS,
                    timestamp_slug="ignored",
                    expected_alembic_head="head",
                    deadline=platform_backup_supervisor.operation_deadline(30.0),
                )
        self.assertEqual(raised.exception.database_id, database)
        self.assertEqual(run_command.call_count, 3)

    def test_cleanup_cancellation_keeps_sanitized_identity_for_primary_and_cleanup(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        database = "platform_restore_drill_" + "f" * 32
        for cancellation in (KeyboardInterrupt, SystemExit):
            with self.subTest(cancellation=cancellation):
                responses = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, self._identity_json(database), ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    RuntimeError("restore failed"),
                    cancellation(),
                ]
                with (
                    mock.patch.object(backup_drill, "run_command", side_effect=responses),
                    mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value="f" * 32),
                    mock.patch.object(backup_drill, "REQUIRED_PLATFORM_EXTENSIONS", ()),
                ):
                    with self.assertRaisesRegex(RuntimeError, "restore failed") as raised:
                        backup_drill.perform_restore_drill(
                            pathlib.Path("/tmp/backup.dump"),
                            app_target=target,
                            admin_target=None,
                            helpers=TEST_HELPERS,
                            timestamp_slug="ignored",
                            expected_alembic_head="head",
                            deadline=platform_backup_supervisor.operation_deadline(30.0),
                        )
                self.assertEqual(
                    getattr(raised.exception, "backup_cleanup_unproven", None), database
                )

    def test_cleanup_only_cancellation_keeps_sanitized_identity(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        database = "platform_restore_drill_" + "1" * 32
        for cancellation in (KeyboardInterrupt, SystemExit):
            with self.subTest(cancellation=cancellation):
                responses = [
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, self._identity_json(database), ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "1\n", ""),
                    subprocess.CompletedProcess([], 0, "1\n", ""),
                    subprocess.CompletedProcess([], 0, "head\n", ""),
                    subprocess.CompletedProcess([], 0, "0\n", ""),
                    cancellation(),
                ]
                with (
                    mock.patch.object(backup_drill, "run_command", side_effect=responses),
                    mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value="1" * 32),
                    mock.patch.object(backup_drill, "REQUIRED_PLATFORM_EXTENSIONS", ()),
                ):
                    with self.assertRaises(cancellation) as raised:
                        backup_drill.perform_restore_drill(
                            pathlib.Path("/tmp/backup.dump"),
                            app_target=target,
                            admin_target=None,
                            helpers=TEST_HELPERS,
                            timestamp_slug="ignored",
                            expected_alembic_head="head",
                            deadline=platform_backup_supervisor.operation_deadline(30.0),
                        )
                self.assertEqual(
                    getattr(raised.exception, "backup_cleanup_unproven", None), database
                )
                payload = platform_backup_supervisor.safe_error_payload(raised.exception)
                self.assertEqual(payload["cleanup_status"], "unproven")
                self.assertEqual(payload["database_id"], database)

    def test_restore_drill_identity_capture_rejects_absent_ambiguous_and_timeout(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", None, "platformdb"
        )
        database = "platform_restore_drill_" + "c" * 32
        identity_outputs = (
            "",
            "{}\n{}\n",
            (
                '{"name":"'
                + database
                + '","name":"'
                + database
                + '","oid":42,"owner":"platform_user"}\n'
            ),
        )
        for identity_output in identity_outputs:
            with self.subTest(identity_output=identity_output):
                with (
                    mock.patch.object(
                        backup_drill,
                        "run_command",
                        side_effect=[
                            subprocess.CompletedProcess([], 0, "", ""),
                            subprocess.CompletedProcess([], 0, "", ""),
                            subprocess.CompletedProcess([], 0, identity_output, ""),
                        ],
                    ) as run_command,
                    mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value="c" * 32),
                ):
                    with self.assertRaises(platform_backup_supervisor.BackupCleanupUnproven):
                        backup_drill.perform_restore_drill(
                            pathlib.Path("/tmp/backup.dump"),
                            app_target=target,
                            admin_target=None,
                            helpers=TEST_HELPERS,
                            timestamp_slug="ignored",
                            expected_alembic_head="head",
                            deadline=platform_backup_supervisor.operation_deadline(30.0),
                        )
                self.assertEqual(run_command.call_count, 3)

        with (
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, "", ""),
                    platform_backup_supervisor.BackupCommandTimeout(),
                ],
            ) as run_command,
            mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"),
            mock.patch.object(backup_drill.secrets, "token_hex", return_value="c" * 32),
        ):
            with self.assertRaises(platform_backup_supervisor.BackupCleanupUnproven):
                backup_drill.perform_restore_drill(
                    pathlib.Path("/tmp/backup.dump"),
                    app_target=target,
                    admin_target=None,
                    helpers=TEST_HELPERS,
                    timestamp_slug="ignored",
                    expected_alembic_head="head",
                    deadline=platform_backup_supervisor.operation_deadline(30.0),
                )
        self.assertEqual(run_command.call_count, 3)

    def test_create_backup_persists_real_cleanup_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir) / "backups"
            output_dir.mkdir()
            trusted_source = _trusted_source(pathlib.Path(temporary_dir) / "trusted-source")
            args = self._creator_args(output_dir, dump_only=False)
            database = "platform_restore_drill_" + "d" * 32
            calls: list[list[str]] = []

            def fake_run_command(
                command: list[str], *, stdout: int | None = None, **_: object
            ) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                executable = pathlib.Path(command[0]).name
                nested = pathlib.Path(command[4]).name if executable == "runuser" else executable
                if nested == "pg_dump":
                    assert stdout is not None
                    os.write(stdout, b"PGDMP durable cleanup evidence")
                if nested == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                if nested == "psql":
                    query = command[-1]
                    if "json_build_object" in query:
                        identity = {
                            "name": database,
                            "oid": 99 if sum("json_build_object" in item[-1] for item in calls) > 1 else 42,
                            "owner": "platform_user",
                        }
                        return subprocess.CompletedProcess(
                            command, 0, json.dumps(identity) + "\n", ""
                        )
                    if "pg_database" in query:
                        return subprocess.CompletedProcess(command, 0, "", "")
                    if "information_schema.tables" in query:
                        return subprocess.CompletedProcess(command, 0, "1\n", "")
                    if query == "SELECT 1;":
                        return subprocess.CompletedProcess(command, 0, "1\n", "")
                    if "version_num" in query:
                        return subprocess.CompletedProcess(command, 0, "20260913_0053\n", "")
                    if "pg_extension" in query:
                        return subprocess.CompletedProcess(command, 0, "1\n", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    self._creator_environment(),
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill, "load_env", return_value=self._creator_environment()
                ),
                mock.patch.object(
                    backup_drill, "require_commands", return_value=TEST_HELPERS
                ),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value="d" * 32),
            ):
                with self.assertRaisesRegex(RuntimeError, "restore verification failed"):
                    self._create_backup(args, source_root=trusted_source)

            manifest_path = next(output_dir.glob("*.json"))
            parsed = manifest_contract.read_manifest_file(
                manifest_path,
                expected_owner=os.geteuid(),
                expected_group=os.getegid(),
            ).manifest
            self.assertEqual(parsed.restore_error, "cleanup_unproven")
            self.assertEqual(parsed.cleanup_status, "unproven")
            self.assertEqual(parsed.database_id, database)
            self.assertEqual(
                parsed.operator_action,
                platform_backup_supervisor.CLEANUP_OPERATOR_ACTION,
            )
            command_names = [
                pathlib.Path(
                    command[4] if pathlib.Path(command[0]).name == "runuser" else command[0]
                ).name
                for command in calls
            ]
            self.assertNotIn("dropdb", command_names)

    def test_restore_drill_rejects_explicit_head_not_in_trusted_graph(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "secret", "platformdb"
        )
        source_root = pathlib.Path("/tmp/oldsparky-trusted-source")
        with mock.patch.object(
            backup_drill, "_trusted_alembic_head", return_value="20260913_0053"
        ) as trusted_head, mock.patch.object(backup_drill, "run_command") as run_command:
            with self.assertRaisesRegex(RuntimeError, "trusted deployed source graph"):
                backup_drill.perform_restore_drill(
                    pathlib.Path("/tmp/backup.dump"),
                    app_target=target,
                    admin_target=None,
                    helpers=TEST_HELPERS,
                    timestamp_slug="20261001T120000Z",
                    expected_alembic_head="not-current-head",
                    source_root=source_root,
                    deadline=platform_backup_supervisor.operation_deadline(30.0),
                )

        trusted_head.assert_called_once_with(source_root)
        run_command.assert_not_called()

    def test_verify_existing_dump_forwards_one_absolute_deadline(self) -> None:
        sentinel = 12345.678
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            dump_path = root / "existing.dump"
            dump_path.write_bytes(b"custom-format-backup")
            args = argparse.Namespace(
                verify_dump=str(dump_path),
                env_file=str(root / ".env.platform"),
                admin_database_url=None,
                timeout_seconds=37.5,
            )
            monitor = platform_backup_supervisor.ensure_process_monitor
            monitor.reset_mock()
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=True,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill, "require_commands", return_value=TEST_HELPERS
                ),
                mock.patch.object(
                    platform_backup_supervisor,
                    "operation_deadline",
                    return_value=sentinel,
                ) as operation_deadline,
                mock.patch.object(
                    backup_drill,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as run_command,
                mock.patch.object(
                    backup_drill, "perform_restore_drill", return_value=7
                ) as restore_drill,
            ):
                result = backup_drill.verify_existing_dump(args)

        operation_deadline.assert_called_once_with(37.5)
        monitor.assert_called_once_with(deadline=sentinel)
        self.assertEqual(run_command.call_args.kwargs["deadline"], sentinel)
        self.assertEqual(restore_drill.call_args.kwargs["deadline"], sentinel)
        self.assertEqual(result["restored_table_count"], 7)

    def test_main_forwards_legacy_timeout_to_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            args = argparse.Namespace(
                check_latest=False,
                dump_only=False,
                verify_dump=None,
                output_dir=str(pathlib.Path(temporary_dir) / "backups"),
                keep=2,
                max_age_hours=24.0,
                env_file=str(pathlib.Path(temporary_dir) / ".env.platform"),
                admin_database_url=None,
                timeout_seconds=37.5,
                as_json=False,
            )
            with (
                mock.patch.object(backup_drill, "parse_args", return_value=args),
                mock.patch.object(
                    platform_backup_supervisor,
                    "run_backup_entrypoint",
                    return_value={"ok": True},
                ) as run_backup_entrypoint,
                mock.patch.object(backup_drill, "print_result"),
            ):
                self.assertEqual(backup_drill.main(), 0)

        forwarded_args = run_backup_entrypoint.call_args.args[0]
        self.assertEqual(forwarded_args.backup_timeout_seconds, 37.5)
        self.assertFalse(hasattr(forwarded_args, "timeout_seconds"))

    def test_ambiguous_create_never_attempts_drop(self) -> None:
        target = backup_drill.DatabaseTarget("127.0.0.1", 5432, "platform_user", None, "platformdb")
        with mock.patch.object(backup_drill, "_trusted_alembic_head", return_value="head"), mock.patch.object(
            backup_drill, "run_command", side_effect=[subprocess.CompletedProcess([], 0, "", ""), platform_backup_supervisor.BackupCommandTimeout()]
        ) as run_command:
            with self.assertRaises(platform_backup_supervisor.BackupCleanupUnproven) as error:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/tmp/backup.dump"),
                    app_target=target,
                    admin_target=None,
                    helpers=TEST_HELPERS,
                    timestamp_slug="ignored",
                    deadline=platform_backup_supervisor.operation_deadline(30.0),
                )
        self.assertRegex(error.exception.database_id or "", platform_backup_supervisor.RESTORE_DRILL_DATABASE_ID_RE)
        self.assertEqual(run_command.call_count, 2)

    def test_json_cli_errors_use_bounded_shared_payload(self) -> None:
        args = argparse.Namespace(
            check_latest=True, dump_only=False, verify_dump=None, output_dir="/tmp",
            max_age_hours=24.0, as_json=True,
        )
        human_args = argparse.Namespace(**{**vars(args), "as_json": False})
        valid_id = "platform_restore_drill_" + "a" * 32
        primary_valid = RuntimeError("primary-secret")
        primary_valid.backup_cleanup_unproven = valid_id
        primary_invalid = RuntimeError("primary-secret")
        primary_invalid.backup_cleanup_unproven = "arbitrary-secret"
        failures = (
            (platform_backup_supervisor.BackupCleanupUnproven(database_id=valid_id), "backup_cleanup_unproven", valid_id, "unproven"),
            (platform_backup_supervisor.BackupCleanupUnproven(database_id="secret-id"), "backup_cleanup_unproven", None, "unproven"),
            (primary_valid, "operation_failed", valid_id, "unproven"),
            (primary_invalid, "operation_failed", None, "unproven"),
            (RuntimeError("stderr=password https://user:secret@example.invalid/db"), "operation_failed", None, None),
        )
        for failure, expected_class, expected_id, expected_cleanup in failures:
            expected_action = platform_backup_supervisor.CLEANUP_OPERATOR_ACTION if expected_id else None
            expected = platform_backup_supervisor.safe_error_payload(failure)
            self.assertEqual(expected["error_class"], expected_class)
            self.assertEqual(expected.get("cleanup_status"), expected_cleanup)
            with mock.patch.object(backup_drill, "parse_args", return_value=args), mock.patch.object(
                backup_drill, "check_latest_backup", side_effect=failure
            ), mock.patch("builtins.print") as printed:
                self.assertEqual(backup_drill.main(), 1)
            raw = printed.call_args.args[0]
            payload = json.loads(raw)
            self.assertEqual(payload, expected)
            self.assertNotRegex(raw, r"password|secret|postgres://|arbitrary-secret")
            self.assertEqual(payload.get("database_id"), expected_id)
            self.assertEqual(
                payload.get("operator_action"),
                expected_action,
            )
            with mock.patch.object(backup_drill, "parse_args", return_value=human_args), mock.patch.object(
                backup_drill, "check_latest_backup", side_effect=failure
            ), mock.patch("sys.stderr", new_callable=StringIO) as stderr:
                self.assertEqual(backup_drill.main(), 1)
            self.assertIn(expected_class, stderr.getvalue())
            self.assertNotRegex(stderr.getvalue(), r"password|secret|postgres://|arbitrary-secret")
            with mock.patch.object(platform_backup_supervisor, "run_backup_entrypoint", side_effect=failure), mock.patch(
                "builtins.print"
            ) as printed:
                self.assertEqual(platform_backup_supervisor.main(["backup", "--json"]), 1)
            supervisor_payload = json.loads(printed.call_args.args[0])
            self.assertEqual(supervisor_payload, expected)
            with mock.patch.object(platform_backup_supervisor, "run_backup_entrypoint", side_effect=failure), mock.patch(
                "sys.stderr", new_callable=StringIO
            ) as stderr:
                self.assertEqual(platform_backup_supervisor.main(["backup"]), 1)
            self.assertIn(expected_class, stderr.getvalue())
            self.assertNotRegex(stderr.getvalue(), r"password|secret|postgres://|arbitrary-secret")

    def test_check_latest_validates_restore_age_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            now = dt.datetime.now(dt.UTC)
            run_id = "b" * 32
            dump_path = output_dir / f"platformdb-{now:%Y%m%dT%H%M%SZ}-{run_id}.dump"
            dump_path.write_bytes(b"custom-format-backup")
            dump_path.chmod(0o600)
            metadata_path = dump_path.with_suffix(".json")
            metadata_path.write_text(
                json.dumps(
                    manifest_contract.build_manifest(
                        run_id=run_id,
                        dump_file=dump_path.name,
                        size_bytes=dump_path.stat().st_size,
                        sha256=hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        started_at_utc=now,
                        completed_at_utc=now,
                        duration_seconds=0,
                        restore_verified=True,
                        alembic_revision_verified=True,
                        restored_table_count=31,
                        restore_error=None,
                    )
                ),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)

            result = backup_drill.check_latest_backup(output_dir, max_age_hours=24)

            self.assertTrue(result["ok"])
            self.assertEqual(result["restored_table_count"], 31)

    def test_check_latest_rejects_read_only_legacy_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            now = dt.datetime.now(dt.UTC)
            run_id = "a" * 32
            dump_path = output_dir / f"platformdb-{now:%Y%m%dT%H%M%SZ}-{run_id}.dump"
            dump_path.write_bytes(b"custom-format-backup")
            dump_path.chmod(0o600)
            current = manifest_contract.build_manifest(
                run_id=run_id,
                dump_file=dump_path.name,
                size_bytes=dump_path.stat().st_size,
                sha256=hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                started_at_utc=now,
                completed_at_utc=now,
                duration_seconds=0,
                restore_verified=True,
                alembic_revision_verified=True,
                restored_table_count=31,
                restore_error=None,
            )
            legacy = {
                key: value
                for key, value in current.items()
                if key in manifest_contract.LEGACY_MANIFEST_KEY_SET
            }
            legacy["format_version"] = manifest_contract.LEGACY_MANIFEST_FORMAT_VERSION
            metadata_path = dump_path.with_suffix(".json")
            metadata_path.write_text(json.dumps(legacy), encoding="utf-8")
            metadata_path.chmod(0o600)

            with self.assertRaisesRegex(RuntimeError, "legacy manifest"):
                backup_drill.check_latest_backup(output_dir, max_age_hours=24)

    def test_check_latest_cli_rejects_unverified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            now = dt.datetime.now(dt.UTC)
            run_id = "c" * 32
            dump_path = output_dir / f"platformdb-{now:%Y%m%dT%H%M%SZ}-{run_id}.dump"
            dump_path.write_bytes(b"custom-format-backup")
            dump_path.chmod(0o600)
            dump_path.with_suffix(".json").write_text(
                json.dumps(
                    manifest_contract.build_manifest(
                        run_id=run_id,
                        dump_file=dump_path.name,
                        size_bytes=dump_path.stat().st_size,
                        sha256=hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        started_at_utc=now,
                        completed_at_utc=now,
                        duration_seconds=0,
                        restore_verified=False,
                        alembic_revision_verified=False,
                        restored_table_count=None,
                        restore_error="restore_verification_failed",
                    )
                ),
                encoding="utf-8",
            )
            dump_path.with_suffix(".json").chmod(0o600)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--output-dir",
                    str(output_dir),
                    "--check-latest",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("[FAIL] Platform backup restore (operation_failed)", result.stderr)

    def test_prune_unverified_backups_keeps_verified_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            now = dt.datetime.now(dt.UTC)
            verified_dump = output_dir / f"platformdb-{now:%Y%m%dT%H%M%SZ}-{'e' * 32}.dump"
            failed_dump = output_dir / f"platformdb-{now:%Y%m%dT%H%M%SZ}-{'f' * 32}.dump"
            verified_dump.write_bytes(b"verified")
            failed_dump.write_bytes(b"failed")
            for dump_path, run_id, restore_verified in (
                (verified_dump, "e" * 32, True),
                (failed_dump, "f" * 32, False),
            ):
                dump_path.chmod(0o600)
                dump_path.with_suffix(".json").write_text(
                    json.dumps(
                        manifest_contract.build_manifest(
                            run_id=run_id,
                            dump_file=dump_path.name,
                            size_bytes=dump_path.stat().st_size,
                            sha256=hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                            started_at_utc=now,
                            completed_at_utc=now,
                            duration_seconds=0,
                            restore_verified=restore_verified,
                            alembic_revision_verified=restore_verified,
                            restored_table_count=1 if restore_verified else None,
                            restore_error=None
                            if restore_verified
                            else "restore_verification_failed",
                        )
                    ),
                    encoding="utf-8",
                )
                dump_path.with_suffix(".json").chmod(0o600)
            verified_metadata = verified_dump.with_suffix(".json")
            failed_metadata = failed_dump.with_suffix(".json")

            with _held_test_lock() as lock:
                removed = platform_backup_supervisor._run_maintenance_scope(
                    lock=lock,
                    callback=lambda capability: backup_drill.prune_unverified_backups(
                        output_dir,
                        preserve_metadata=verified_metadata,
                        capability=capability,
                    ),
                )

            self.assertTrue(verified_dump.exists())
            self.assertTrue(verified_metadata.exists())
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(len(removed), 2)

    def test_concurrent_same_second_creators_reserve_distinct_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            output_dir.mkdir(exist_ok=True)
            fixed_now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)

            def fake_run_command(
                command: list[str], *, stdout: int | None = None, **_: object
            ) -> subprocess.CompletedProcess[str]:
                if pathlib.Path(command[0]).name == "pg_dump":
                    assert stdout is not None
                    self.assertNotIn("--file", command)
                    os.write(stdout, b"PGDMP concurrent creator output")
                return subprocess.CompletedProcess(command, 0, "", "")

            with ThreadPoolExecutor(max_workers=2) as executor:
                with (
                    mock.patch.dict(
                        backup_drill.os.environ, self._creator_environment(), clear=False
                    ),
                    mock.patch.object(
                        backup_drill, "load_env", return_value=self._creator_environment()
                    ),
                    mock.patch.object(
                        backup_drill, "require_commands", return_value=TEST_HELPERS
                    ),
                    mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                    mock.patch.object(backup_drill, "utc_now", return_value=fixed_now),
                ):
                    results = list(
                        executor.map(
                            lambda _index: self._create_backup(
                                self._creator_args(output_dir)
                            ),
                            range(2),
                        )
                    )

            self.assertEqual(len({result["run_id"] for result in results}), 2)
            self.assertEqual(len(tuple(output_dir.glob("*.dump"))), 2)
            self.assertEqual(len(tuple(output_dir.glob("*.json"))), 2)
            for result in results:
                manifest_path = output_dir / result["metadata_file"]
                self.assertTrue(manifest_path.exists())
                parsed = manifest_contract.read_manifest_file(
                    manifest_path,
                    expected_owner=os.geteuid(),
                    expected_group=os.getegid(),
                    expected_dump_file=result["dump_file"],
                ).manifest
                self.assertEqual(parsed.run_id, result["run_id"])

    def test_secure_temp_rejects_symlink_and_hardlink_preplants(self) -> None:
        fixed_uuid = uuid.UUID("11111111111111111111111111111111")
        for preplant_kind in ("symlink", "hardlink"):
            with self.subTest(preplant_kind=preplant_kind), tempfile.TemporaryDirectory() as temporary_dir:
                root = pathlib.Path(temporary_dir)
                output_dir = root / "backups"
                output_dir.mkdir()
                dump_name = (
                    f"platformdb-20261001T120000Z-{fixed_uuid.hex}.dump"
                )
                temporary_path = output_dir / f".{dump_name}.{os.getpid()}.tmp"
                outside = root / "outside.bin"
                outside.write_bytes(b"protected outside data")
                if preplant_kind == "symlink":
                    temporary_path.symlink_to(outside)
                else:
                    os.link(outside, temporary_path)

                with (
                    mock.patch.dict(
                        backup_drill.os.environ, self._creator_environment(), clear=False
                    ),
                    mock.patch.object(
                        backup_drill, "load_env", return_value=self._creator_environment()
                    ),
                    mock.patch.object(
                        backup_drill, "require_commands", return_value=TEST_HELPERS
                    ),
                    mock.patch.object(backup_drill.uuid, "uuid4", return_value=fixed_uuid),
                    mock.patch.object(
                        backup_drill,
                        "utc_now",
                        return_value=dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC),
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "already occupied"):
                        self._create_backup(self._creator_args(output_dir))

                self.assertEqual(outside.read_bytes(), b"protected outside data")
                self.assertTrue(temporary_path.is_symlink() or temporary_path.is_file())
                self.assertEqual(tuple(output_dir.glob("platformdb-*.dump")), ())
                self.assertEqual(tuple(output_dir.glob("platformdb-*.json")), ())

    def test_secure_temp_rejects_symlink_and_hardlink_replacement_races(self) -> None:
        fixed_uuid = uuid.UUID("22222222222222222222222222222222")
        for replacement_kind in ("symlink", "hardlink"):
            with self.subTest(replacement_kind=replacement_kind), tempfile.TemporaryDirectory() as temporary_dir:
                root = pathlib.Path(temporary_dir)
                output_dir = root / "backups"
                output_dir.mkdir()
                dump_name = (
                    f"platformdb-20261001T120000Z-{fixed_uuid.hex}.dump"
                )
                temporary_path = output_dir / f".{dump_name}.{os.getpid()}.tmp"
                outside = root / "outside.bin"
                outside.write_bytes(b"protected outside data")

                def racing_run_command(
                    command: list[str], *, stdout: int | None = None, **_: object
                ) -> subprocess.CompletedProcess[str]:
                    if pathlib.Path(command[0]).name == "pg_dump":
                        assert stdout is not None
                        os.write(stdout, b"PGDMP race output")
                        temporary_path.unlink()
                        if replacement_kind == "symlink":
                            temporary_path.symlink_to(outside)
                        else:
                            os.link(outside, temporary_path)
                    return subprocess.CompletedProcess(command, 0, "", "")

                with (
                    mock.patch.dict(
                        backup_drill.os.environ, self._creator_environment(), clear=False
                    ),
                    mock.patch.object(
                        backup_drill, "load_env", return_value=self._creator_environment()
                    ),
                    mock.patch.object(
                        backup_drill, "require_commands", return_value=TEST_HELPERS
                    ),
                    mock.patch.object(backup_drill.uuid, "uuid4", return_value=fixed_uuid),
                    mock.patch.object(
                        backup_drill,
                        "utc_now",
                        return_value=dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC),
                    ),
                    mock.patch.object(
                        backup_drill, "run_command", side_effect=racing_run_command
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "replaced|hardlink"):
                        self._create_backup(self._creator_args(output_dir))

                self.assertEqual(outside.read_bytes(), b"protected outside data")
                self.assertEqual(tuple(output_dir.glob("platformdb-*.dump")), ())
                self.assertEqual(tuple(output_dir.glob("platformdb-*.json")), ())

    def test_publication_failures_remove_all_partial_backup_artifacts(self) -> None:
        manifest_module = sys.modules[backup_drill.write_manifest.__module__]
        for failure_stage in ("dump_directory", "manifest_file", "manifest_directory"):
            with self.subTest(failure_stage=failure_stage), tempfile.TemporaryDirectory() as temporary_dir:
                output_dir = pathlib.Path(temporary_dir)

                def fake_run_command(
                    command: list[str], *, stdout: int | None = None, **_: object
                ) -> subprocess.CompletedProcess[str]:
                    if pathlib.Path(command[0]).name == "pg_dump":
                        assert stdout is not None
                        os.write(stdout, b"PGDMP failure-injection output")
                    return subprocess.CompletedProcess(command, 0, "", "")

                patchers = [
                    mock.patch.dict(
                        backup_drill.os.environ, self._creator_environment(), clear=False
                    ),
                    mock.patch.object(
                        backup_drill, "load_env", return_value=self._creator_environment()
                    ),
                    mock.patch.object(
                        backup_drill, "require_commands", return_value=TEST_HELPERS
                    ),
                    mock.patch.object(
                        backup_drill, "run_command", side_effect=fake_run_command
                    ),
                ]
                if failure_stage == "dump_directory":
                    patchers.append(
                        mock.patch.object(
                            backup_drill,
                            "_fsync_directory",
                            side_effect=OSError("injected dump directory fsync failure"),
                        )
                    )
                elif failure_stage == "manifest_file":
                    patchers.append(
                        mock.patch.object(
                            manifest_module,
                            "_fsync_file",
                            side_effect=OSError("injected manifest file fsync failure"),
                        )
                    )
                else:
                    patchers.append(
                        mock.patch.object(
                            manifest_module,
                            "_fsync_directory",
                            side_effect=OSError("injected manifest directory fsync failure"),
                        )
                    )
                with ExitStack() as stack:
                    for patcher in patchers:
                        stack.enter_context(patcher)
                    with self.assertRaises(Exception):
                        self._create_backup(self._creator_args(output_dir))

                self.assertEqual(tuple(output_dir.iterdir()), ())

    def test_restore_failure_does_not_prune_existing_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump = output_dir / "platformdb-old.dump"
            old_metadata = old_dump.with_suffix(".json")
            old_dump.write_bytes(b"old-backup")
            old_metadata.write_text(
                json.dumps(
                    {
                        "dump_file": old_dump.name,
                        "restore_verified": True,
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
            )
            trusted_source = _trusted_source(output_dir / "trusted-source")

            with self.assertRaisesRegex(
                platform_backup_supervisor.BackupSupervisorError,
                "trusted deployed Alembic source graph",
            ):
                self._create_backup(args)

            def fake_run_command(
                command: list[str], *, stdout: int | None = None, **_: object
            ) -> subprocess.CompletedProcess[str]:
                if pathlib.Path(command[0]).name == "pg_dump":
                    assert stdout is not None
                    backup_drill.os.write(stdout, b"PGDMP new-backup")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if pathlib.Path(command[0]).name == "pg_restore":
                    if "--list" in command:
                        return subprocess.CompletedProcess(command, 0, "", "")
                    raise RuntimeError("restore failed")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill, "require_commands", return_value=TEST_HELPERS
                ),
                mock.patch.object(
                    backup_drill,
                    "run_command",
                    side_effect=fake_run_command,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "restore verification failed"):
                    self._create_backup(
                        args,
                        source_root=trusted_source,
                    )

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            self.assertGreaterEqual(len(tuple(output_dir.glob("platformdb-*.dump"))), 2)

            valid_id = "platform_restore_drill_" + "b" * 32
            for index, (failure, expected_error, expected_id) in enumerate(
                (
                    (RuntimeError("primary-secret"), "restore_verification_failed", valid_id),
                    (platform_backup_supervisor.BackupCommandTimeout(), "backup_command_timeout", valid_id),
                    (RuntimeError("primary-secret"), "restore_verification_failed", None),
                    (platform_backup_supervisor.BackupCommandTimeout(), "backup_command_timeout", None),
                )
            ):
                if expected_id is not None:
                    failure.backup_cleanup_unproven = expected_id
                else:
                    failure.backup_cleanup_unproven = "arbitrary-secret"
                case_dir = output_dir / f"round-trip-{index}"
                case_dir.mkdir()
                def round_trip_command(
                    command: list[str], *, stdout: int | None = None, **_: object
                ) -> subprocess.CompletedProcess[str]:
                    if pathlib.Path(command[0]).name == "pg_dump":
                        assert stdout is not None
                        backup_drill.os.write(stdout, b"PGDMP decorated restore failure")
                    return subprocess.CompletedProcess(command, 0, "", "")

                with (mock.patch.dict(backup_drill.os.environ, self._creator_environment(), clear=False),
                      mock.patch.object(backup_drill, "load_env", return_value=self._creator_environment()),
                      mock.patch.object(
                          backup_drill, "require_commands", return_value=TEST_HELPERS
                      ),
                      mock.patch.object(backup_drill, "run_command", side_effect=round_trip_command),
                      mock.patch.object(backup_drill, "perform_restore_drill", side_effect=failure)):
                    with self.assertRaisesRegex(RuntimeError, "restore verification failed") as raised:
                        self._create_backup(
                            self._creator_args(case_dir, dump_only=False),
                            source_root=_trusted_source(case_dir / "trusted-source"),
                        )
                wrapped = platform_backup_supervisor.safe_error_payload(raised.exception)
                self.assertEqual((wrapped.get("cleanup_status"), wrapped.get("database_id")),
                                 ("unproven", expected_id))

                manifest_path = next(case_dir.glob("*.json"))
                parsed = manifest_contract.read_manifest_file(
                    manifest_path,
                    expected_owner=os.geteuid(),
                    expected_group=os.getegid(),
                ).manifest
                self.assertEqual(parsed.restore_error, expected_error)
                self.assertEqual(parsed.cleanup_status, "unproven")
                self.assertEqual(parsed.database_id, expected_id)
                self.assertEqual(
                    parsed.operator_action,
                    platform_backup_supervisor.CLEANUP_OPERATOR_ACTION if expected_id else None,
                )


if __name__ == "__main__":
    unittest.main()
