from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
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
                ),
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
        ) as run_command, mock.patch.object(
            backup_drill, "_trusted_alembic_head", return_value="20260801_0036"
        ):
            table_count = backup_drill.perform_restore_drill(
                pathlib.Path(temporary_dir) / "backup.dump",
                app_target=target,
                admin_target=None,
                timestamp_slug="20260720T120000Z",
                expected_alembic_head="20260801_0036",
            )

        self.assertEqual(table_count, 22)
        self.assertTrue(run_command.call_args_list[1].kwargs["capture_output"])
        self.assertTrue(run_command.call_args_list[2].kwargs["capture_output"])
        self.assertIn("CREATE SCHEMA platform", run_command.call_args_list[2].args[0][-1])
        self.assertIn("--schema=platform", run_command.call_args_list[3].args[0])
        self.assertIn("--schema=public", run_command.call_args_list[4].args[0])

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
                    timestamp_slug="20261001T120000Z",
                    expected_alembic_head="not-current-head",
                    source_root=source_root,
                )

        trusted_head.assert_called_once_with(source_root)
        run_command.assert_not_called()

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
                if command[0] == "pg_dump":
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
                    mock.patch.object(backup_drill, "require_commands"),
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
                    mock.patch.object(backup_drill, "require_commands"),
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
                    if command[0] == "pg_dump":
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
                    mock.patch.object(backup_drill, "require_commands"),
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
                    if command[0] == "pg_dump":
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
                    mock.patch.object(backup_drill, "require_commands"),
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
                if command[0] == "pg_dump":
                    assert stdout is not None
                    backup_drill.os.write(stdout, b"PGDMP new-backup")
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
                    self._create_backup(
                        args,
                        source_root=trusted_source,
                    )

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            self.assertGreaterEqual(len(tuple(output_dir.glob("platformdb-*.dump"))), 2)


if __name__ == "__main__":
    unittest.main()
