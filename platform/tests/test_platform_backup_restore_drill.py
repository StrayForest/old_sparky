from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


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
                        restore_error="restore not run",
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
            self.assertIn("not restore-verified", result.stderr)

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
                            restore_error=None if restore_verified else "restore failed",
                        )
                    ),
                    encoding="utf-8",
                )
                dump_path.with_suffix(".json").chmod(0o600)
            verified_metadata = verified_dump.with_suffix(".json")
            failed_metadata = failed_dump.with_suffix(".json")

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
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    dump_path = pathlib.Path(command[command.index("--file") + 1])
                    dump_path.write_bytes(b"new-backup")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise RuntimeError("restore failed")

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
            ):
                with self.assertRaisesRegex(RuntimeError, "restore verification failed"):
                    backup_drill.create_backup(args)

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            self.assertGreaterEqual(len(tuple(output_dir.glob("platformdb-*.dump"))), 2)


if __name__ == "__main__":
    unittest.main()
