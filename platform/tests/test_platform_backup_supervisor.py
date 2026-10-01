from __future__ import annotations

import json
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from tools import platform_backup_supervisor as supervisor


class PlatformBackupSupervisorTests(unittest.TestCase):
    def test_stale_filename_is_reused_and_conflict_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / supervisor.BACKUP_LOCK_PATH.name
            path.write_bytes(b"stale")
            path.chmod(0o600)
            with supervisor._exclusive_backup_lock_for_test(path):
                with self.assertRaises(supervisor.BackupLockConflict) as context:
                    with supervisor._exclusive_backup_lock_for_test(path):
                        pass
                self.assertEqual(context.exception.status, "blocked")
                self.assertEqual(context.exception.exit_code, 75)
            self.assertTrue(path.exists())

    def test_lock_rejects_symlink_hardlink_and_same_owner_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            path = root / supervisor.BACKUP_LOCK_PATH.name
            target = root / "target"
            target.write_bytes(b"")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises(supervisor.BackupLockError):
                with supervisor._exclusive_backup_lock_for_test(path):
                    pass
            path.unlink()
            path.write_bytes(b"")
            path.chmod(0o600)
            hardlink = root / "hardlink"
            hardlink.hardlink_to(path)
            with self.assertRaises(supervisor.BackupLockError):
                with supervisor._exclusive_backup_lock_for_test(path):
                    pass
            hardlink.unlink()
            with self.assertRaises(supervisor.BackupLockError):
                with supervisor._exclusive_backup_lock_for_test(path):
                    replacement = root / "replacement"
                    replacement.write_bytes(b"")
                    replacement.chmod(0o600)
                    os.replace(replacement, path)

    def test_kernel_singleton_blocks_after_lock_inode_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            path = root / supervisor.BACKUP_LOCK_PATH.name
            path.write_bytes(b"")
            path.chmod(0o600)
            singleton = b"\0oldsparky-platform-backup-test-replaced"
            with self.assertRaises(supervisor.BackupLockError):
                with supervisor._exclusive_backup_lock_for_test(
                    path, singleton_name=singleton
                ):
                    replacement = root / "replacement"
                    replacement.write_bytes(b"")
                    replacement.chmod(0o600)
                    os.replace(replacement, path)
                    with self.assertRaises(supervisor.BackupLockConflict):
                        with supervisor._exclusive_backup_lock_for_test(
                            path, singleton_name=singleton
                        ):
                            pass

    def test_lock_matrix_rejects_reverse_edges_and_duplicates(self) -> None:
        self.assertEqual(
            supervisor.operation_lock_requirements("maintenance"),
            ("release", "retained-load", "source/build", "live-QA", "backup"),
        )
        supervisor.assert_operation_lock_order(
            "offsite", ["backup"]
        )
        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.validate_lock_sequence(["backup", "release"])
        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.validate_lock_sequence(["backup", "backup"])

    def test_inprogress_evidence_is_unknown_and_final_publication_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            app_dir = Path(temporary_dir)
            (app_dir / "shared").mkdir()
            session = supervisor.EvidenceSession.start(
                app_dir, "offsite", locks=("backup",)
            )
            payload = supervisor.read_latest_evidence(app_dir)
            self.assertEqual(payload["status"], "unknown")
            final = session.finish("passed")
            self.assertEqual(final.stat().st_mode & 0o777, 0o600)
            published = json.loads(final.read_text(encoding="utf-8"))
            self.assertEqual(published["status"], "passed")
            self.assertEqual(
                set(published),
                supervisor.EVIDENCE_KEYS,
            )
            self.assertNotIn("stderr", json.dumps(published).lower())
            self.assertNotIn("pid", json.dumps(published).lower())
            self.assertNotIn("password", json.dumps(published).lower())

    def test_stale_inprogress_is_recovered_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            app_dir = Path(temporary_dir)
            (app_dir / "shared").mkdir()
            session = supervisor.EvidenceSession.start(
                app_dir, "local-backup", locks=supervisor.LOCK_ORDER
            )
            recovered = supervisor.recover_inprogress_evidence(app_dir)
            self.assertEqual(len(recovered), 1)
            payload = json.loads(recovered[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "unknown")
            self.assertEqual(payload["error_class"], "interrupted_evidence")

    def test_evidence_fsync_failure_removes_final_and_keeps_unknown_inprogress(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            app_dir = Path(temporary_dir)
            (app_dir / "shared").mkdir()
            session = supervisor.EvidenceSession.start(
                app_dir, "offsite", locks=("backup",)
            )
            final_path = session.path.with_name(session.path.name.removesuffix(".inprogress"))
            with mock.patch.object(
                supervisor, "_sync_directory", side_effect=OSError("injected fsync")
            ):
                with self.assertRaises(supervisor.BackupEvidenceError):
                    session.finish("passed")
            self.assertFalse(final_path.exists())
            self.assertTrue(session.path.exists())
            self.assertEqual(supervisor.read_latest_evidence(app_dir)["status"], "unknown")
            with self.assertRaises(supervisor.BackupEvidenceError):
                supervisor._load_evidence(final_path)

    def test_held_pair_rejects_path_replacement_after_fd_pin(self) -> None:
        from tools import platform_backup_manifest as manifest

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dump = root / f"platformdb-20261001T120000Z-{'a' * 32}.dump"
            dump.write_bytes(b"PGDMP held-source")
            dump.chmod(0o600)
            now = datetime.now(UTC)
            metadata = manifest.build_manifest(
                run_id="a" * 32,
                dump_file=dump.name,
                size_bytes=dump.stat().st_size,
                sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
                started_at_utc=now,
                completed_at_utc=now,
                duration_seconds=0,
                restore_verified=True,
                alembic_revision_verified=True,
                restored_table_count=1,
                restore_error=None,
            )
            manifest_path = dump.with_suffix(".json")
            manifest_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
            manifest_path.chmod(0o600)
            with self.assertRaises(supervisor.BackupSupervisorError):
                with supervisor.held_backup_pair(dump, manifest_path) as pair:
                    self.assertEqual(os.pread(pair.dump_fd, 5, 0), b"PGDMP")
                    replacement = root / "replacement.dump"
                    replacement.write_bytes(b"PGDMP replacement")
                    replacement.chmod(0o600)
                    os.replace(replacement, dump)
                    pair.validate()

    def test_destructive_production_restore_is_fail_closed(self) -> None:
        with self.assertRaises(supervisor.ProductionRestoreDisabled):
            supervisor.run_production_restore()

    def test_local_backup_passes_explicit_expected_head_before_pg_dump(self) -> None:
        """The supervisor must build restore args without hidden CLI state."""

        restore = mock.Mock()
        restore.create_backup.side_effect = RuntimeError("pg_dump sentinel")
        app_dir = Path("/tmp/oldsparky-supervisor-head-regression")
        lock = mock.Mock()
        evidence = mock.Mock()

        with mock.patch.object(
            supervisor.importlib, "import_module", return_value=restore
        ):
            with self.assertRaisesRegex(RuntimeError, "pg_dump sentinel"):
                supervisor.run_local_backup(
                    app_dir,
                    expected_alembic_head="20260913_0053",
                    capability=supervisor._capability("maintenance"),
                    lock=lock,
                    evidence=evidence,
                )

        lock.validate.assert_called_once_with()
        restore.expected_alembic_head.assert_not_called()
        restore.create_backup.assert_called_once()
        restore_args = restore.create_backup.call_args.args[0]
        self.assertEqual(restore_args.expected_alembic_head, "20260913_0053")

    def test_capability_and_nested_evidence_schema_fail_closed(self) -> None:
        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.require_mutation_capability(object(), "maintenance")
        payload = supervisor._empty_evidence(
            operation="offsite",
            operation_id="0" * 32,
            status="started",
            started_at="unknown",
        )
        supervisor.validate_evidence(payload)
        payload["remote_transport"]["stderr"] = "private output"
        with self.assertRaises(supervisor.BackupEvidenceError):
            supervisor.validate_evidence(payload)

    def test_mutating_entrypoints_name_the_supervisor(self) -> None:
        tools_root = Path(__file__).resolve().parents[1] / "tools"
        restore_source = (tools_root / "platform_backup_restore_drill.py").read_text()
        offsite_source = (tools_root / "platform_backup_offsite.py").read_text()
        maintenance_source = (
            tools_root / "platform_storage_maintenance.py"
        ).read_text()
        self.assertIn("supervisor.run_backup_entrypoint", restore_source)
        self.assertIn("supervisor.run_offsite_entrypoint", offsite_source)
        self.assertIn("supervisor.run_maintenance_entrypoint", maintenance_source)


if __name__ == "__main__":
    unittest.main()
