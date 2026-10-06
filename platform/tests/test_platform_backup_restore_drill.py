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
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "CREATE EXTENSION\n", ""),
            subprocess.CompletedProcess([], 0, "CREATE SCHEMA\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "22\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "20260801_0036\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]

        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch.object(
            backup_drill, "run_command", side_effect=responses
        ) as run_command:
            table_count = backup_drill.perform_restore_drill(
                pathlib.Path(temporary_dir) / "backup.dump",
                app_target=target,
                admin_target=None,
                timestamp_slug="20260720T120000Z",
            )

        self.assertEqual(table_count, 22)
        self.assertTrue(run_command.call_args_list[1].kwargs["capture_output"])
        self.assertTrue(run_command.call_args_list[2].kwargs["capture_output"])
        self.assertIn("CREATE SCHEMA platform", run_command.call_args_list[2].args[0][-1])
        self.assertIn("--schema=platform", run_command.call_args_list[3].args[0])
        self.assertIn("--schema=public", run_command.call_args_list[4].args[0])

    def test_check_latest_validates_restore_age_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"custom-format-backup")
            metadata_path = dump_path.with_suffix(".json")
            metadata_path.write_text(
                json.dumps(
                    {
                        "dump_file": dump_path.name,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                        "restore_verified": True,
                        "restored_table_count": 31,
                    }
                ),
                encoding="utf-8",
            )

            result = backup_drill.check_latest_backup(output_dir, max_age_hours=24)

            self.assertTrue(result["ok"])
            self.assertEqual(result["restored_table_count"], 31)

    def test_check_latest_cli_rejects_unverified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"custom-format-backup")
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
                rotate_existing=False,
            )
            old_entries = []
            for index in range(15):
                dump = output_dir / f"platformdb-old-{index:02}.dump"
                metadata = dump.with_suffix(".json")
                dump.write_bytes(f"old-{index}".encode())
                metadata.write_text(
                    json.dumps({"dump_file": dump.name, "restore_verified": True}),
                    encoding="utf-8",
                )
                old_entries.extend((dump, metadata))
            unverified_dump = output_dir / "platformdb-unverified.dump"
            unverified_metadata = unverified_dump.with_suffix(".json")
            unverified_dump.write_bytes(b"unverified-old")
            unverified_metadata.write_text(
                json.dumps({"dump_file": unverified_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )
            old_entries.extend((unverified_dump, unverified_metadata))
            old_entries.extend((old_dump, old_metadata))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    dump_path = pathlib.Path(command[command.index("--file") + 1])
                    dump_path.write_bytes(b"new-backup")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise RuntimeError("restore failed")

            initial_inventory = backup_drill.backup_inventory(output_dir)
            changed_inventory = tuple(
                (*entry[:-1], entry[-1] + 1) for entry in initial_inventory
            )
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
                    "run_command",
                    side_effect=fake_run_command,
                ),
                mock.patch.object(
                    backup_drill,
                    "backup_inventory",
                    side_effect=(initial_inventory, initial_inventory, changed_inventory),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "restore verification failed") as raised:
                    backup_drill.create_backup(args)
            self.assertEqual(
                getattr(raised.exception, "_backup_preservation_integrity", None),
                "preexisting_inventory_changed",
            )
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
            self.assertIn(
                "[backup_integrity=preexisting_inventory_changed]",
                json.loads(output.getvalue())["error"],
            )

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

            self._assert_preserve_existing_keeps_all_archives_after_success()
            self._assert_destination_collision_fails_before_dump_and_preserves_sidecar()
            self._assert_explicit_rotation_retains_keep_policy()

    def _assert_preserve_existing_keeps_all_archives_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_entries: list[pathlib.Path] = []
            for index in range(16):
                dump = output_dir / f"platformdb-old-{index:02}.dump"
                sidecar = dump.with_suffix(".json")
                dump.write_bytes(f"verified-{index}".encode())
                sidecar.write_text(
                    json.dumps({"dump_file": dump.name, "restore_verified": True}),
                    encoding="utf-8",
                )
                old_entries.extend((dump, sidecar))
            unverified_dump = output_dir / "platformdb-old-unverified.dump"
            unverified_sidecar = unverified_dump.with_suffix(".json")
            unverified_dump.write_bytes(b"old-unverified")
            unverified_sidecar.write_text(
                json.dumps({"dump_file": unverified_dump.name, "restore_verified": False}),
                encoding="utf-8",
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
                mock.patch.object(backup_drill, "perform_restore_drill", return_value=16),
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

    def _assert_destination_collision_fails_before_dump_and_preserves_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            sidecar_path = dump_path.with_suffix(".json")
            sidecar_path.write_bytes(b"pre-existing-sidecar")
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
            self.assertFalse(dump_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path.write_bytes(b"pre-existing-target")
            os.chmod(dump_path, 0o640)
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
                mock.patch.object(backup_drill, "perform_restore_drill", return_value=20),
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


if __name__ == "__main__":
    unittest.main()
