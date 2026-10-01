from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from tools import platform_backup_supervisor as supervisor


class PlatformBackupSupervisorTests(unittest.TestCase):
    def test_stale_filename_is_reused_and_conflict_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / supervisor.BACKUP_LOCK_PATH.name
            path.write_bytes(b"stale")
            path.chmod(0o600)
            with supervisor.exclusive_backup_lock(path):
                with self.assertRaises(supervisor.BackupLockConflict) as context:
                    with supervisor.exclusive_backup_lock(path):
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
                with supervisor.exclusive_backup_lock(path):
                    pass
            path.unlink()
            path.write_bytes(b"")
            path.chmod(0o600)
            hardlink = root / "hardlink"
            hardlink.hardlink_to(path)
            with self.assertRaises(supervisor.BackupLockError):
                with supervisor.exclusive_backup_lock(path):
                    pass
            hardlink.unlink()
            with self.assertRaises(supervisor.BackupLockError):
                with supervisor.exclusive_backup_lock(path):
                    replacement = root / "replacement"
                    replacement.write_bytes(b"")
                    replacement.chmod(0o600)
                    os.replace(replacement, path)

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

    def test_destructive_production_restore_is_fail_closed(self) -> None:
        with self.assertRaises(supervisor.ProductionRestoreDisabled):
            supervisor.run_production_restore()

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
