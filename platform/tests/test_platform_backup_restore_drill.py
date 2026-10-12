from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import os
import signal
import unittest
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_restore_drill", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
backup_drill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup_drill
SPEC.loader.exec_module(backup_drill)


class PlatformBackupRestoreDrillTests(unittest.TestCase):
    @staticmethod
    def _write_backup_pair(
        output_dir: pathlib.Path,
        dump_name: str,
        payload: bytes,
        *,
        restore_verified: bool = True,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        dump_path = output_dir / dump_name
        metadata_path = dump_path.with_suffix(".json")
        dump_path.write_bytes(payload)
        metadata_path.write_text(
            json.dumps(
                {
                    "format_version": 2,
                    "dump_file": dump_path.name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "restore_verified": restore_verified,
                }
            ),
            encoding="utf-8",
        )
        return dump_path, metadata_path

    def _assert_latest_verification_preflight_rejects(
        self,
        output_dir: pathlib.Path,
        env_file: pathlib.Path,
        message: str,
    ) -> None:
        with mock.patch.object(backup_drill, "require_commands") as require_commands:
            with self.assertRaisesRegex((RuntimeError, OSError), message):
                backup_drill.verify_latest_existing_backup(
                    output_dir, env_file, max_age_hours=24
                )
        require_commands.assert_not_called()

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

    def test_local_admin_commands_use_postgres_os_user(self) -> None:
        target = backup_drill.DatabaseTarget("127.0.0.1", 5432, "platform_user", None, "platformdb")

        self.assertEqual(
            backup_drill.local_postgres_admin_command("create", target, "platform_restore_drill_test"),
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "createdb",
                "--owner",
                "platform_user",
                "platform_restore_drill_test",
            ],
        )

    def test_restore_drill_captures_extension_output_for_json_callers(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "secret", "platformdb"
        )
        admin_target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "postgres", None, "postgres"
        )
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "CREATE EXTENSION\n", ""),
            subprocess.CompletedProcess([], 0, "CREATE SCHEMA\n", ""),
            subprocess.CompletedProcess([], 0, "22\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "20260801_0036\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
        ]

        archive = io.BytesIO(b"PGDMPsynthetic archive")
        resource_guard = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch.object(
            backup_drill, "run_command", side_effect=responses
        ) as run_command, mock.patch.object(
            backup_drill, "run_restore_command", return_value=None
        ) as run_restore_command, mock.patch.object(
            backup_drill, "require_restore_headroom"
        ):
            table_count = backup_drill.perform_restore_drill(
                pathlib.Path(temporary_dir) / "backup.dump",
                app_target=target,
                admin_target=admin_target,
                timestamp_slug="20260720T120000Z",
                archive_stdin=archive,
                restore_as_postgres=True,
                restore_no_tablespaces=True,
                resource_guard=resource_guard,
            )

        self.assertEqual(table_count, 22)
        self.assertEqual(run_command.call_args_list[0].kwargs["target"], admin_target)
        self.assertIn("--owner", run_command.call_args_list[0].args[0])
        self.assertIn("platform_user", run_command.call_args_list[0].args[0])
        for index in (1, 2, 3, 4, 5, 6):
            self.assertEqual(run_command.call_args_list[index].kwargs["target"].username, "platform_user")
        self.assertEqual(run_command.call_args_list[7].kwargs["target"], admin_target)
        self.assertIn("postgres", run_command.call_args_list[7].args[0])
        self.assertTrue(run_command.call_args_list[1].kwargs["capture_output"])
        self.assertTrue(run_command.call_args_list[2].kwargs["capture_output"])
        self.assertIn("CREATE SCHEMA platform", run_command.call_args_list[2].args[0][-1])
        self.assertEqual(run_restore_command.call_count, 2)
        for index, schema in enumerate(("--schema=platform", "--schema=public")):
            command = run_restore_command.call_args_list[index].args[0]
            self.assertEqual(command[:4], ["runuser", "-u", "postgres", "--"])
            self.assertEqual(run_restore_command.call_args_list[index].kwargs["target"].username, "platform_user")
            self.assertIn(["--username", "platform_user"], [command[i : i + 2] for i in range(len(command) - 1)])
            self.assertIn(schema, command)
            self.assertIn("--no-tablespaces", command)
            self.assertNotIn(str(pathlib.Path(temporary_dir) / "backup.dump"), command)
            self.assertIs(run_restore_command.call_args_list[index].kwargs["stdin_handle"], archive)
            self.assertIs(run_restore_command.call_args_list[index].kwargs["resource_guard"], resource_guard)
        self.assertEqual(archive.tell(), 0)

        with mock.patch.object(backup_drill, "run_command") as role_command:
            backup_drill._scratch_create_application_role(admin_target)
        self.assertEqual(role_command.call_args.kwargs["target"], admin_target)
        self.assertEqual(
            role_command.call_args.args[0][-1],
            "CREATE ROLE platform_user LOGIN;",
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            scratch_root = pathlib.Path(temporary_dir) / "scratch"
            scratch_root.mkdir(mode=0o700)
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            command = backup_drill._scratch_unit_command(
                mountpoint=scratch_root,
                archive_path=archive_path,
                archive_stat=archive_path.stat(),
                archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                timestamp_slug="20261012T000000Z",
                unit_name="oldsparky-backup-restore-0123456789abcdef01234567.service",
            )
        self.assertIn("--property=MemoryMax=671088640", command)
        self.assertIn("--property=MemorySwapMax=0", command)
        self.assertIn("--property=RestrictAddressFamilies=AF_UNIX", command)
        self.assertIn("--property=PrivateNetwork=yes", command)
        self.assertIn("--property=OOMPolicy=kill", command)
        self.assertIn("--property=TemporaryFileSystem=" + str(scratch_root) + ":rw,size=536870912,mode=0700", command)
        self.assertTrue(any(item.startswith("--property=BindReadOnlyPaths=") and str(archive_path) in item for item in command))
        self.assertFalse(any("ReadWritePaths=" in item for item in command))
        self.assertFalse(any("PGPASSWORD" in item or "PLATFORM_DATABASE_URL" in item for item in command))

        self._assert_low_space_abort_kills_owned_restore_and_drops_only_drill_db()

    def _assert_low_space_abort_kills_owned_restore_and_drops_only_drill_db(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        total_bytes = 100 * 1024**3
        floor = backup_drill._required_disk_floor(total_bytes)
        self.assertEqual(floor, 15 * 1024**3)
        self.assertEqual(
            backup_drill._required_disk_floor(20 * 1024**3), 5 * 1024**3
        )
        self.assertEqual(
            backup_drill._required_disk_floor(40 * 1024**3 + 1),
            6 * 1024**3 + 1,
        )
        high = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )()
        low = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES - 1,
            },
        )()

        class RestoreChild:
            def __init__(self) -> None:
                self.stopped = False
                self.wait_calls = 0
                self.wait_timeouts: list[float | None] = []
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return -signal.SIGKILL if self.stopped else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True
                self.stopped = True

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                self.wait_timeouts.append(timeout)
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("pg_restore", timeout)
                self.stopped = True
                return -signal.SIGKILL

        child = RestoreChild()
        command_results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
        ]
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            mock.patch.object(backup_drill, "snapshot_for_path", side_effect=(high, high, low)),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
            mock.patch.object(
                backup_drill, "run_command", side_effect=command_results
            ) as run_command,
        ):
            with self.assertRaisesRegex(backup_drill.RestoreGuardStop, "low-disk safety guard") as failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path(temporary_dir) / "backup.dump",
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)
        self.assertEqual(child.wait_calls, 2)
        self.assertEqual(child.wait_timeouts, [1.0, None])
        drop_command = run_command.call_args_list[-2].args[0]
        self.assertIn("dropdb", drop_command)
        self.assertEqual(
            drop_command[-1],
            f"platform_restore_drill_20260720t120000z_{os.getpid()}",
        )
        diagnostic = failure.exception.restore_diagnostic
        self.assertEqual(diagnostic["restore_stage"], "restore_platform")
        self.assertEqual(diagnostic["guard_reason"], "disk_floor")
        self.assertEqual(diagnostic["free_bytes"], low.free_bytes)
        self.assertEqual(
            diagnostic["required_free_bytes"],
            floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
        )
        self.assertTrue(diagnostic["temporary_database_created"])
        self.assertEqual(diagnostic["drop_outcome"], "confirmed_absent")
        self.assertIs(diagnostic["temporary_database_absent"], True)

        before_create = backup_drill.RestoreGuardStop(
            "disk_floor", free_bytes=10, required_free_bytes=20
        )
        with (
            mock.patch.object(
                backup_drill, "require_restore_headroom", side_effect=before_create
            ),
            mock.patch.object(backup_drill, "run_command") as preflight_commands,
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as preflight_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        preflight_commands.assert_not_called()
        preflight_diagnostic = preflight_failure.exception.restore_diagnostic
        self.assertEqual(preflight_diagnostic["restore_stage"], "pre_create_admission")
        self.assertEqual(preflight_diagnostic["guard_reason"], "disk_floor")
        self.assertIs(preflight_diagnostic["temporary_database_created"], False)
        self.assertEqual(preflight_diagnostic["drop_outcome"], "not_required")
        self.assertIsNone(preflight_diagnostic["temporary_database_absent"])

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=subprocess.CalledProcessError(1, ["createdb"]),
            ) as create_command,
        ):
            with self.assertRaises(subprocess.CalledProcessError) as create_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        create_command.assert_called_once()
        create_diagnostic = create_failure.exception.restore_diagnostic
        self.assertEqual(create_diagnostic["restore_stage"], "create_database")
        self.assertIsNone(create_diagnostic["temporary_database_created"])
        self.assertEqual(create_diagnostic["drop_outcome"], "not_required")
        self.assertIsNone(create_diagnostic["temporary_database_absent"])

        drop_failure_commands = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CalledProcessError(1, ["dropdb"]),
        ]
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=drop_failure_commands,
            ),
            mock.patch.object(
                backup_drill,
                "run_restore_command",
                side_effect=backup_drill.RestoreGuardStop(
                    "disk_floor", free_bytes=10, required_free_bytes=20
                ),
            ),
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as drop_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        drop_diagnostic = drop_failure.exception.restore_diagnostic
        self.assertEqual(drop_diagnostic["restore_stage"], "restore_platform")
        self.assertEqual(drop_diagnostic["guard_reason"], "disk_floor")
        self.assertEqual(drop_diagnostic["drop_outcome"], "drop_failed")
        self.assertIsNone(drop_diagnostic["temporary_database_absent"])

        serialized_error = backup_drill.RestoreGuardStop("disk_floor")
        serialized_diagnostic = {
            **diagnostic,
            "archive_sha256": "a" * 64,
            "archive_size_bytes": 120,
        }
        serialized_error.restore_diagnostic = serialized_diagnostic
        cli_args = argparse.Namespace(
            check_latest=False,
            verify_latest_existing=True,
            dump_only=False,
            verify_dump=None,
            output_dir="/private/backups",
            env_file="/private/env",
            max_age_hours=24.0,
            admin_database_url=None,
            as_json=True,
        )
        output = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(
                backup_drill,
                "verify_latest_existing_backup",
                side_effect=serialized_error,
            ),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(backup_drill.main(), 1)
        serialized = json.loads(output.getvalue())
        self.assertEqual(serialized["restore_diagnostic"], serialized_diagnostic)
        self._assert_unavailable_disk_fails_before_child_start(target)
        self._assert_normal_guarded_child_completes(target, high)
        self._assert_low_space_abort_stops_only_owned_dump()
        self._assert_backup_admission_reports_private_disk_phase()

    def _assert_unavailable_disk_fails_before_child_start(
        self, target: backup_drill.DatabaseTarget
    ) -> None:
        invalid = type(
            "Disk", (), {"valid": False, "total_bytes": 0, "free_bytes": 0}
        )()
        with (
            mock.patch.object(backup_drill, "snapshot_for_path", return_value=invalid),
            mock.patch.object(backup_drill.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(
                backup_drill.RestoreGuardStop, "disk space could not be verified"
            ):
                backup_drill.run_disk_guarded_command(
                    ["pg_dump", "--file", "/private/temp.dump"],
                    target=target,
                    stage="pg_dump",
                )
        popen.assert_not_called()

    def _assert_normal_guarded_child_completes(
        self, target: backup_drill.DatabaseTarget, disk_snapshot: object
    ) -> None:
        class CompletedChild:
            def __init__(self) -> None:
                self.poll_calls = 0

            def poll(self) -> int | None:
                self.poll_calls += 1
                return None if self.poll_calls == 1 else 0

            def wait(self, *, timeout: float | None = None) -> int:
                return 0

        child = CompletedChild()
        with (
            mock.patch.object(
                backup_drill,
                "snapshot_for_path",
                return_value=disk_snapshot,
            ),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
        ):
            backup_drill.run_disk_guarded_command(
                ["pg_dump", "--file", "/private/temp.dump"],
                target=target,
                stage="pg_dump",
            )
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)

    def _assert_low_space_abort_stops_only_owned_dump(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        total_bytes = 100 * 1024**3
        floor = backup_drill._required_disk_floor(total_bytes)
        high = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )()
        low = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES - 1,
            },
        )()

        class DumpChild:
            def __init__(self) -> None:
                self.stopped = False
                self.terminated = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return 0 if self.stopped else None

            def terminate(self) -> None:
                self.terminated = True
                self.stopped = True

            def kill(self) -> None:
                raise AssertionError("graceful TERM should stop the fake pg_dump child")

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                return 0

        child = DumpChild()
        with (
            mock.patch.object(backup_drill, "snapshot_for_path", side_effect=(high, high, low)),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                backup_drill.RestoreGuardStop, "low-disk safety guard"
            ) as raised:
                backup_drill.run_disk_guarded_command(
                    ["pg_dump", "--file", "/private/temp.dump"],
                    target=target,
                    stage="pg_dump",
                )
        failure = raised.exception

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)
        self.assertTrue(child.terminated)
        self.assertEqual(child.wait_calls, 1)
        self.assertEqual(
            failure.private_backup_disk_diagnostic(),
            {
                "schema": 1,
                "phase": "pg_dump_monitor",
                "guard_reason": "disk_floor",
                "free_bytes": low.free_bytes,
                "required_free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )

    def _assert_backup_admission_reports_private_disk_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_sidecar = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"retained backup"
            )
            before = {
                path: (path.read_bytes(), path.lstat().st_ino, path.lstat().st_mode)
                for path in (old_dump, old_sidecar)
            }
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            guard = backup_drill.RestoreGuardStop(
                "disk_floor",
                free_bytes=100,
                required_free_bytes=200,
                phase="backup_admission",
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_restore_headroom", side_effect=guard),
                mock.patch.object(backup_drill, "require_commands") as require_commands,
                mock.patch.object(backup_drill.tempfile, "mkstemp") as mkstemp,
            ):
                with self.assertRaises(backup_drill.RestoreGuardStop) as raised:
                    backup_drill.create_backup(args)

            require_commands.assert_not_called()
            mkstemp.assert_not_called()
            self.assertEqual(
                raised.exception.private_backup_disk_diagnostic(),
                {
                    "schema": 1,
                    "phase": "backup_admission",
                    "guard_reason": "disk_floor",
                    "free_bytes": 100,
                    "required_free_bytes": 200,
                },
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual(
                    (path.read_bytes(), metadata.st_ino, metadata.st_mode), expected
                )

    def test_check_latest_validates_restore_age_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            env_file = output_dir / ".env.platform"
            env_file.write_text(
                "PLATFORM_DATABASE_URL=postgresql://platform_user:synthetic@127.0.0.1/platformdb\n",
                encoding="utf-8",
            )
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"PGDMPcustom-format-backup")
            dump_path.chmod(0o600)
            metadata_path = dump_path.with_suffix(".json")
            original_created_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(
                json.dumps(
                    {
                        "format_version": 2,
                        "database": "platformdb",
                        "schemas": ["platform", "public"],
                        "required_extensions": ["pg_trgm"],
                        "dump_file": dump_path.name,
                        "size_bytes": dump_path.stat().st_size,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": original_created_at,
                        "restore_verified": False,
                        "alembic_revision_verified": False,
                        "restored_table_count": 31,
                        "restore_error": "historical closed diagnostic",
                    }
                ),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)

            original_dump_stat = dump_path.stat()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run_command, mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ) as perform_restore:
                result = backup_drill.verify_latest_existing_backup(
                    output_dir, env_file, max_age_hours=24
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["restored_table_count"], 31)
            run_command.assert_not_called()
            perform_restore.assert_called_once()
            updated = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["completed_at_utc"], original_created_at)
            self.assertEqual(updated["restore_error"], "historical closed diagnostic")
            self.assertIs(updated["restore_verified"], True)
            self.assertIs(updated["alembic_revision_verified"], True)
            self.assertIsInstance(updated["restore_verified_at_utc"], str)
            self.assertEqual(
                backup_drill.check_latest_backup(output_dir, max_age_hours=24)["sha256"],
                hashlib.sha256(b"PGDMPcustom-format-backup").hexdigest(),
            )
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")
            self.assertEqual(
                (dump_path.stat().st_ino, dump_path.stat().st_mtime_ns),
                (original_dump_stat.st_ino, original_dump_stat.st_mtime_ns),
            )

            updated_sidecar_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill,
                "_run_isolated_restore_drill",
                side_effect=backup_drill.RestoreGuardStop("command_failed"),
            ):
                with self.assertRaises(backup_drill.RestoreGuardStop):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), updated_sidecar_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "_run_isolated_restore_drill",
                side_effect=subprocess.CalledProcessError(1, ["systemd-run"]),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), updated_sidecar_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            crossing = dict(updated)
            start_now = dt.datetime.now(dt.UTC)
            crossing["completed_at_utc"] = (
                start_now - dt.timedelta(hours=23, minutes=59)
            ).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(json.dumps(crossing), encoding="utf-8")
            metadata_path.chmod(0o600)
            crossing_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ), mock.patch.object(
                backup_drill,
                "utc_now",
                side_effect=[start_now, start_now, start_now + dt.timedelta(hours=2)],
            ):
                with self.assertRaisesRegex(RuntimeError, "creation-age window"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), crossing_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            # A directory-fsync failure after rename must restore the exact old sidecar.
            stable_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ), mock.patch.object(
                backup_drill.os,
                "fsync",
                side_effect=[None, OSError("synthetic directory fsync failure"), None, None],
            ):
                with self.assertRaisesRegex(OSError, "synthetic directory fsync failure"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), stable_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            def write_metadata(value: dict[str, object]) -> None:
                metadata_path.write_text(json.dumps(value), encoding="utf-8")
                metadata_path.chmod(0o600)

            def restore_hook(callback):
                with mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user:synthetic@127.0.0.1/platformdb"
                        )
                    },
                ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                    backup_drill,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", side_effect=callback
                ):
                    return backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )

            size_omitted_v1 = dict(updated)
            size_omitted_v1["format_version"] = 1
            size_omitted_v1.pop("size_bytes", None)
            write_metadata(size_omitted_v1)
            original_v1_bytes = metadata_path.read_bytes()
            failure = subprocess.CalledProcessError(1, ["createdb"])
            failure.restore_diagnostic = {
                "schema": 1,
                "restore_stage": "create_database",
                "guard_reason": "none",
                "free_bytes": None,
                "required_free_bytes": None,
                "temporary_database_created": None,
                "drop_outcome": "not_required",
                "temporary_database_absent": None,
            }
            with self.assertRaises(subprocess.CalledProcessError) as failed_v1:
                restore_hook(lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
            self.assertEqual(
                failed_v1.exception.restore_diagnostic["archive_size_bytes"],
                dump_path.stat().st_size,
            )
            self.assertEqual(
                failed_v1.exception.restore_diagnostic["archive_sha256"],
                hashlib.sha256(dump_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(metadata_path.read_bytes(), original_v1_bytes)

            baseline = dict(updated)
            baseline["completed_at_utc"] = original_created_at
            write_metadata(baseline)
            malformed = b'{"dump_file":"one","dump_file":"two"}'
            metadata_path.write_bytes(malformed)
            metadata_path.chmod(0o600)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "malformed"
            )

            metadata_path.write_bytes(b" " * (1_048_577))
            metadata_path.chmod(0o600)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )

            wrong_schema = dict(baseline)
            wrong_schema["schemas"] = ["platform", "sparkydb"]
            write_metadata(wrong_schema)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsupported schema set"
            )

            naive_timestamp = dict(baseline)
            naive_timestamp["completed_at_utc"] = dt.datetime.now(dt.UTC).replace(
                tzinfo=None
            ).isoformat()
            write_metadata(naive_timestamp)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "creation timestamp is invalid"
            )

            write_metadata(baseline)
            saved_metadata = output_dir / ".platformdb-metadata.saved"
            metadata_path.rename(saved_metadata)
            metadata_path.symlink_to(saved_metadata.name)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "Too many levels|unsafe file metadata"
            )
            metadata_path.unlink()
            saved_metadata.rename(metadata_path)

            hardlink_path = output_dir / ".platformdb-metadata.hardlink"
            os.link(metadata_path, hardlink_path)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            hardlink_path.unlink()

            dump_hardlink = output_dir / ".platformdb-dump.hardlink"
            os.link(dump_path, dump_hardlink)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            dump_hardlink.unlink()

            metadata_path.chmod(0o640)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            metadata_path.chmod(0o600)

            if os.geteuid() == 0:
                os.chown(metadata_path, 65534, metadata_path.stat().st_gid)
                self._assert_latest_verification_preflight_rejects(
                    output_dir, env_file, "unsafe file metadata"
                )
                os.chown(metadata_path, 0, 0)
                metadata_path.chmod(0o600)
                os.chown(metadata_path, 0, 65534)
                self._assert_latest_verification_preflight_rejects(
                    output_dir, env_file, "unsafe file metadata"
                )
                os.chown(metadata_path, 0, 0)

            original_archive_bytes = dump_path.read_bytes()
            original_archive_stat = dump_path.stat()

            def mutate_archive_during_restore(*_args, **_kwargs):
                dump_path.write_bytes(b"mutated same inode")
                os.utime(
                    dump_path,
                    ns=(original_archive_stat.st_atime_ns, original_archive_stat.st_mtime_ns),
                )
                return 31

            original_sidecar_bytes = metadata_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "checksum does not match"):
                restore_hook(mutate_archive_during_restore)
            self.assertEqual(metadata_path.read_bytes(), original_sidecar_bytes)
            self.assertEqual(dump_path.stat().st_ino, original_archive_stat.st_ino)
            dump_path.write_bytes(original_archive_bytes)
            dump_path.chmod(0o600)

            def replace_metadata_during_restore(*_args, **_kwargs):
                replacement = dict(baseline)
                replacement["restore_error"] = "external metadata replacement"
                replacement.pop("restore_verified_at_utc", None)
                replacement_path = output_dir / ".replacement.json"
                replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
                replacement_path.chmod(0o600)
                os.replace(replacement_path, metadata_path)
                return 31

            with self.assertRaisesRegex(RuntimeError, "metadata changed during restore"):
                restore_hook(replace_metadata_during_restore)
            replacement_after = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(replacement_after["restore_error"], "external metadata replacement")
            self.assertNotIn("restore_verified_at_utc", replacement_after)

            write_metadata(baseline)

            def add_newer_pair_during_restore(*_args, **_kwargs):
                newer_dump = output_dir / "platformdb-newer.dump"
                newer_dump.write_bytes(b"newer archive")
                newer_dump.chmod(0o600)
                newer_metadata = newer_dump.with_suffix(".json")
                newer_metadata.write_text(
                    json.dumps(
                        {
                            "dump_file": newer_dump.name,
                            "sha256": hashlib.sha256(newer_dump.read_bytes()).hexdigest(),
                            "size_bytes": newer_dump.stat().st_size,
                            "format_version": 2,
                            "database": "platformdb",
                            "schemas": ["platform", "public"],
                            "required_extensions": ["pg_trgm"],
                            "completed_at_utc": original_created_at,
                            "restore_verified": False,
                            "alembic_revision_verified": False,
                        }
                    ),
                    encoding="utf-8",
                )
                newer_metadata.chmod(0o600)
                future_ns = metadata_path.stat().st_mtime_ns + 10_000_000
                os.utime(newer_metadata, ns=(future_ns, future_ns))
                return 31

            original_sidecar_bytes = metadata_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "metadata changed during restore"):
                restore_hook(add_newer_pair_during_restore)
            self.assertEqual(metadata_path.read_bytes(), original_sidecar_bytes)

            too_old = dict(updated)
            too_old["completed_at_utc"] = (
                dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
            ).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(json.dumps(too_old), encoding="utf-8")
            metadata_path.chmod(0o600)
            rewritten_stat = metadata_path.stat()
            os.utime(
                metadata_path,
                ns=(rewritten_stat.st_atime_ns, rewritten_stat.st_mtime_ns + 1_000_000_000),
            )
            expired_bytes = metadata_path.read_bytes()
            with mock.patch.object(backup_drill, "require_commands") as require_commands:
                with self.assertRaisesRegex(RuntimeError, "creation-age window"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            require_commands.assert_not_called()
            self.assertEqual(metadata_path.read_bytes(), expired_bytes)

    def test_check_latest_cli_rejects_unverified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"PGDMPcustom-format-backup")
            dump_path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "dump_file": dump_path.name,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                        "restore_verified": False,
                    }
                ),
                encoding="utf-8",
            )

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
            self.assertIn("not restore-verified", result.stderr)

    def test_prune_unverified_backups_keeps_verified_archive(self) -> None:
        with mock.patch.object(backup_drill.sys, "argv", ["platform_backup_restore_drill.py"]):
            self.assertTrue(backup_drill.parse_args().rotate_existing)
        with mock.patch.object(
            backup_drill.sys,
            "argv",
            ["platform_backup_restore_drill.py", "--preserve-existing"],
        ):
            self.assertFalse(backup_drill.parse_args().rotate_existing)

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            verified_dump = output_dir / "platformdb-verified.dump"
            failed_dump = output_dir / "platformdb-failed.dump"
            verified_dump.write_bytes(b"verified")
            failed_dump.write_bytes(b"failed")
            verified_metadata = verified_dump.with_suffix(".json")
            failed_metadata = failed_dump.with_suffix(".json")
            verified_metadata.write_text(
                json.dumps({"dump_file": verified_dump.name, "restore_verified": True}),
                encoding="utf-8",
            )
            failed_metadata.write_text(
                json.dumps({"dump_file": failed_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )

            removed = backup_drill.prune_unverified_backups(
                output_dir,
                preserve_metadata=verified_metadata,
            )

            self.assertTrue(verified_dump.exists())
            self.assertTrue(verified_metadata.exists())
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(len(removed), 2)

    def test_restore_failure_does_not_prune_existing_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-old.dump", b"old-backup"
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )
            old_entries = []
            for index in range(15):
                dump, metadata = self._write_backup_pair(
                    output_dir,
                    f"platformdb-old-{index:02}.dump",
                    f"old-{index}".encode(),
                )
                old_entries.extend((dump, metadata))
            unverified_dump, unverified_metadata = self._write_backup_pair(
                output_dir,
                "platformdb-unverified.dump",
                b"unverified-old",
                restore_verified=False,
            )
            old_entries.extend((unverified_dump, unverified_metadata))
            old_entries.extend((old_dump, old_metadata))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }

            initial_inventory = backup_drill.backup_inventory(output_dir)
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
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill,
                    "_run_isolated_restore_drill",
                    side_effect=RuntimeError("restore failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "restore failed") as raised:
                    backup_drill.create_backup(args)
            output = io.StringIO()
            main_args = argparse.Namespace(
                check_latest=False,
                dump_only=False,
                verify_dump=None,
                output_dir=str(output_dir),
                max_age_hours=24,
                as_json=True,
            )
            with (
                mock.patch.object(backup_drill, "parse_args", return_value=main_args),
                mock.patch.object(backup_drill, "create_backup", side_effect=raised.exception),
                mock.patch.object(backup_drill.sys, "stdout", output),
            ):
                self.assertEqual(backup_drill.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["error"], "restore failed")

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

            self._assert_postpublication_inventory_failure_rolls_back_only_new_pair()

            self._assert_preserve_existing_keeps_all_archives_after_success()
            self._assert_destination_collision_fails_before_dump_and_preserves_sidecar()
            self._assert_explicit_rotation_retains_keep_policy()

    def _assert_postpublication_inventory_failure_rolls_back_only_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-old.dump", b"old-backup"
            )
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in (old_dump, old_metadata)
            }
            initial_inventory = backup_drill.backup_inventory(output_dir)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )
            real_inventory = backup_drill.backup_inventory
            inventory_calls = 0

            def fail_after_publication(path: pathlib.Path) -> tuple[object, ...]:
                nonlocal inventory_calls
                inventory_calls += 1
                if inventory_calls == 3:
                    raise RuntimeError("synthetic post-publication inventory failure")
                return real_inventory(path)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=1),
                mock.patch.object(backup_drill, "backup_inventory", side_effect=fail_after_publication),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "synthetic post-publication inventory failure"
                ):
                    backup_drill.create_backup(args)

            self.assertEqual(inventory_calls, 4)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {old_dump.name, old_metadata.name},
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

    def _assert_preserve_existing_keeps_all_archives_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_entries: list[pathlib.Path] = []
            for index in range(16):
                dump, sidecar = self._write_backup_pair(
                    output_dir,
                    f"platformdb-old-{index:02}.dump",
                    f"verified-{index}".encode(),
                )
                old_entries.extend((dump, sidecar))
            unverified_dump, unverified_sidecar = self._write_backup_pair(
                output_dir,
                "platformdb-old-unverified.dump",
                b"old-unverified",
                restore_verified=False,
            )
            old_entries.extend((unverified_dump, unverified_sidecar))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command[0]}")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=16),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)] * 3,
                ),
            ):
                result = backup_drill.create_backup(args)

            self.assertTrue(result["restore_verified"])
            self.assertEqual(result["rotation_mode"], "preserve-existing")
            self.assertEqual(result["preexisting_archive_count"], 17)
            self.assertEqual(result["preexisting_sidecar_count"], 17)
            self.assertEqual(result["preexisting_archives_preserved"], True)
            self.assertEqual(result["removed"], [])
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

    def test_preserve_existing_rejects_incomplete_or_mismatched_pairs(self) -> None:
        for malformed in ("orphan_dump", "orphan_sidecar", "wrong_digest", "wrong_dump_name"):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as temporary_dir:
                output_dir = pathlib.Path(temporary_dir)
                dump_path, sidecar_path = self._write_backup_pair(
                    output_dir, "platformdb-existing.dump", b"existing-archive"
                )
                if malformed == "orphan_dump":
                    sidecar_path.unlink()
                elif malformed == "orphan_sidecar":
                    dump_path.unlink()
                else:
                    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    metadata["sha256" if malformed == "wrong_digest" else "dump_file"] = (
                        "0" * 64 if malformed == "wrong_digest" else "platformdb-other.dump"
                    )
                    sidecar_path.write_text(json.dumps(metadata), encoding="utf-8")
                args = argparse.Namespace(
                    env_file=str(output_dir / ".env.platform"),
                    output_dir=str(output_dir),
                    keep=1,
                    admin_database_url=None,
                    dump_only=True,
                    rotate_existing=False,
                )
                with (
                    mock.patch.dict(
                        backup_drill.os.environ,
                        {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                    ),
                    mock.patch.object(backup_drill, "load_env", return_value={}),
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "run_disk_guarded_command") as dump_command,
                    self.assertRaisesRegex(RuntimeError, "incomplete|mismatched|invalid"),
                ):
                    backup_drill.create_backup(args)
                dump_command.assert_not_called()

    def test_preserve_existing_detects_same_inode_content_tamper_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, _ = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"original-payload"
            )
            original_stat = old_dump.stat()
            original_inode = old_dump.lstat().st_ino
            original_mode = old_dump.lstat().st_mode
            original_entry = next(
                entry
                for entry in backup_drill.backup_inventory(output_dir)
                if entry[0] == old_dump.name
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )

            def mutate_existing_archive(*_: object, **__: object) -> int:
                with old_dump.open("r+b") as handle:
                    handle.write(b"tampered-payload")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(old_dump, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                mutated_entry = next(
                    entry
                    for entry in backup_drill.backup_inventory(output_dir)
                    if entry[0] == old_dump.name
                )
                self.assertEqual(original_entry[:7], mutated_entry[:7])
                self.assertNotEqual(original_entry[7], mutated_entry[7])
                return 1

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command[0]}")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_drill, "run_disk_guarded_command", side_effect=self._fake_guarded_dump),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", side_effect=mutate_existing_archive),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)] * 3,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "mismatched existing backup pair"):
                    backup_drill.create_backup(args)

            self.assertEqual(old_dump.lstat().st_ino, original_inode)
            self.assertEqual(old_dump.lstat().st_mode, original_mode)
            self.assertEqual(old_dump.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertFalse((output_dir / "platformdb-20261006T120000Z.dump").exists())

    def _assert_destination_collision_fails_before_dump_and_preserves_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path, sidecar_path = self._write_backup_pair(
                output_dir, dump_path.name, b"pre-existing-target"
            )
            os.chmod(dump_path, 0o640)
            os.chmod(sidecar_path, 0o640)
            before = (sidecar_path.read_bytes(), sidecar_path.lstat().st_mode, sidecar_path.lstat().st_ino)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    backup_drill.create_backup(args)

            run_command.assert_not_called()
            metadata = sidecar_path.lstat()
            self.assertEqual((sidecar_path.read_bytes(), metadata.st_mode, metadata.st_ino), before)
            self.assertTrue(dump_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path, sidecar_path = self._write_backup_pair(
                output_dir, dump_path.name, b"pre-existing-target"
            )
            os.chmod(dump_path, 0o640)
            os.chmod(sidecar_path, 0o640)
            before = (dump_path.read_bytes(), dump_path.lstat().st_mode, dump_path.lstat().st_ino)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    backup_drill.create_backup(args)
            run_command.assert_not_called()
            metadata = dump_path.lstat()
            self.assertEqual((dump_path.read_bytes(), metadata.st_mode, metadata.st_ino), before)
            self.assertTrue(sidecar_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            sidecar_path = dump_path.with_suffix(".json")
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            real_link = backup_drill.os.link
            link_count = 0

            def race_sidecar_collision(source: object, destination: object, **kwargs: object) -> None:
                nonlocal link_count
                link_count += 1
                if link_count == 1:
                    real_link(source, destination, **kwargs)
                    return
                sidecar_path.write_bytes(b"racing-sidecar")
                os.chmod(sidecar_path, 0o640)
                raise FileExistsError

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill.os, "link", side_effect=race_sidecar_collision),
            ):
                with self.assertRaisesRegex(RuntimeError, "destination already exists") as raised:
                    backup_drill.create_backup(args)

            self.assertEqual(
                getattr(raised.exception, "_backup_preservation_integrity", None),
                "preexisting_inventory_changed",
            )
            self.assertEqual(link_count, 2)
            self.assertEqual(sidecar_path.read_bytes(), b"racing-sidecar")
            self.assertEqual(sidecar_path.stat().st_mode & 0o777, 0o640)
            self.assertFalse(dump_path.exists())

    def _assert_explicit_rotation_retains_keep_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_verified: list[pathlib.Path] = []
            base_time = dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp()
            for index in range(16):
                dump = output_dir / f"platformdb-verified-{index:02}.dump"
                sidecar = dump.with_suffix(".json")
                dump.write_bytes(f"old-{index}".encode())
                sidecar.write_text(
                    json.dumps({"dump_file": dump.name, "restore_verified": True}),
                    encoding="utf-8",
                )
                old_time = base_time + index
                os.utime(dump, (old_time, old_time))
                os.utime(sidecar, (old_time, old_time))
                old_verified.append(dump)
            failed_dump = output_dir / "platformdb-unverified.dump"
            failed_metadata = failed_dump.with_suffix(".json")
            failed_dump.write_bytes(b"old-unverified")
            failed_metadata.write_text(
                json.dumps({"dump_file": failed_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=True,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=20),
            ):
                result = backup_drill.create_backup(args)

            self.assertEqual(result["rotation_mode"], "rotate-existing")
            self.assertEqual(len(result["removed"]), 8)
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(
                len(tuple(output_dir.glob("platformdb-verified-*.dump"))),
                13,
            )
            self.assertEqual(
                len(tuple(output_dir.glob("platformdb-*.dump"))),
                14,
            )

    @staticmethod
    def _fake_guarded_dump(
        command: list[str], *, target: object, stage: str, disk_paths: tuple[pathlib.Path, ...]
    ) -> None:
        del target, disk_paths
        if stage != "pg_dump":
            raise AssertionError("unexpected guarded command in backup unit test")
        dump_path = pathlib.Path(command[command.index("--file") + 1])
        dump_path.write_bytes(b"PGDMPnew-archive")


if __name__ == "__main__":
    unittest.main()
