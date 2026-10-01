from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
import hashlib
import os
import pickle
from pathlib import Path
import tempfile
from types import SimpleNamespace
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
            supervisor.EvidenceSession.start(
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
        restore.expected_alembic_head.return_value = "20260913_0053"
        restore.create_backup.side_effect = RuntimeError("pg_dump sentinel")
        app_dir = Path("/tmp/oldsparky-supervisor-head-regression")
        lock = mock.Mock()
        evidence = mock.Mock()
        args = SimpleNamespace(dump_only=False)
        observed_head: dict[str, object] = {}

        def invoke(
            capability: object,
            trusted_head: object,
            _restore: object,
        ) -> object:
            assert trusted_head is not None
            observed_head["value"] = trusted_head.value
            observed_head["source"] = trusted_head.source_root
            with self.assertRaises(TypeError):
                trusted_head.value = "not-current-head"
            with self.assertRaises(TypeError):
                trusted_head.source_root = Path("/tmp/forged-source")
            with self.assertRaises(TypeError):
                copy.copy(trusted_head)
            with self.assertRaises(TypeError):
                pickle.dumps(trusted_head)
            with self.assertRaises(AttributeError):
                object.__setattr__(trusted_head, "_value", "stolen-head")
            return supervisor.run_local_backup(
                app_dir,
                trusted_alembic_head=trusted_head,
                capability=capability,
                lock=lock,
                evidence=evidence,
            )

        with mock.patch.object(
            supervisor.importlib, "import_module", return_value=restore
        ):
            with self.assertRaisesRegex(RuntimeError, "pg_dump sentinel"):
                supervisor._run_local_backup_scope(
                    args,
                    app_dir=app_dir,
                    lock=lock,
                    callback=invoke,
                )

        self.assertGreaterEqual(lock.validate.call_count, 2)
        restore.expected_alembic_head.assert_called_once_with(app_dir / "current")
        restore.create_backup.assert_called_once()
        self.assertEqual(observed_head["value"], "20260913_0053")
        self.assertEqual(observed_head["source"], (app_dir / "current").resolve(strict=False))
        with self.assertRaises(TypeError):
            supervisor._TrustedAlembicHead(
                object(),
                "20260913_0053",
                (app_dir / "current").resolve(strict=False),
            )
        uninitialized = object.__new__(supervisor._TrustedAlembicHead)
        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.require_trusted_alembic_head(uninitialized)

    def test_local_backup_rejects_untrusted_explicit_expected_head(self) -> None:
        restore = mock.Mock()
        restore.expected_alembic_head.return_value = "20260913_0053"
        app_dir = Path("/tmp/oldsparky-supervisor-head-mismatch")
        lock = mock.Mock()

        with mock.patch.object(
            supervisor.importlib, "import_module", return_value=restore
        ):
            with self.assertRaisesRegex(
                supervisor.BackupSupervisorError, "trusted Alembic head"
            ):
                supervisor._run_local_backup_scope(
                    SimpleNamespace(dump_only=False),
                    app_dir=app_dir,
                    lock=lock,
                    callback=lambda capability, _trusted_head, _restore: supervisor.run_local_backup(
                        app_dir,
                        trusted_alembic_head=object(),
                        capability=capability,
                        lock=lock,
                        evidence=mock.Mock(),
                    ),
                )
            with self.assertRaisesRegex(
                supervisor.BackupSupervisorError, "mutation capability"
            ):
                supervisor._run_local_backup_scope(
                    SimpleNamespace(dump_only=False),
                    app_dir=app_dir,
                    lock=lock,
                    callback=lambda capability, _trusted_head, _restore: supervisor.require_mutation_capability(
                        capability, "offsite"
                    ),
                )

        restore.create_backup.assert_not_called()

    def test_production_backup_entrypoint_propagates_trusted_expected_head(self) -> None:
        app_dir = Path("/tmp/oldsparky-supervisor-entrypoint")
        expected_head = "20260913_0053"
        restore = mock.Mock()
        restore.expected_alembic_head.return_value = expected_head
        evidence = mock.Mock()
        evidence_context = mock.MagicMock()
        evidence_context.__enter__.return_value = evidence
        lock = mock.Mock()
        scope_context = mock.MagicMock()
        scope_context.__enter__.return_value = (lock, None)
        observed_head: dict[str, object] = {}

        def run_local_with_observation(
            *_args: object, **kwargs: object
        ) -> dict[str, bool]:
            trusted_head = kwargs["trusted_alembic_head"]
            observed_head["value"] = trusted_head.value
            observed_head["source"] = trusted_head.source_root
            return {"ok": True}

        args = SimpleNamespace(
            keep=14,
            max_age_hours=24.0,
            env_file=None,
            output_dir=None,
            admin_database_url=None,
        )

        with (
            mock.patch.object(
                supervisor, "evidence_session", return_value=evidence_context
            ),
            mock.patch.object(
                supervisor, "ordered_backup_lock_scope", return_value=scope_context
            ),
            mock.patch.object(supervisor, "_source_sha_from_release", return_value="unknown"),
            mock.patch.object(
                supervisor, "run_local_backup", side_effect=run_local_with_observation
            ),
            mock.patch.object(
                supervisor.importlib, "import_module", return_value=restore
            ),
        ):
            result = supervisor.run_backup_entrypoint(args, app_dir=app_dir)

        self.assertEqual(result, {"ok": True})
        restore.expected_alembic_head.assert_called_once_with(app_dir / "current")
        self.assertEqual(observed_head["value"], expected_head)
        self.assertEqual(observed_head["source"], (app_dir / "current").resolve(strict=False))

    def test_capability_and_nested_evidence_schema_fail_closed(self) -> None:
        captured: list[object] = []

        def inspect_capability(capability: object) -> str:
            captured.append(capability)
            self.assertEqual(capability.operation, "maintenance")
            with self.assertRaises(TypeError):
                capability.operation = "offsite"
            with self.assertRaises(supervisor.BackupSupervisorError):
                supervisor.require_mutation_capability(capability, "offsite")
            with self.assertRaises(TypeError):
                copy.copy(capability)
            with self.assertRaises(TypeError):
                pickle.dumps(capability)
            with self.assertRaises(AttributeError):
                object.__setattr__(capability, "_operation_token", "offsite")
            with self.assertRaises(TypeError):
                supervisor._MutationCapability(object(), object())
            with self.assertRaises(TypeError):

                class Forged(supervisor._MutationCapability):
                    def prove(self, _operation: str) -> None:
                        return None

            uninitialized = object.__new__(supervisor._MutationCapability)
            with self.assertRaises(supervisor.BackupSupervisorError):
                supervisor.require_mutation_capability(uninitialized, "maintenance")
            return "completed"

        result = supervisor._run_maintenance_scope(
            lock=mock.Mock(),
            callback=inspect_capability,
        )
        self.assertEqual(result, "completed")
        capability = captured[0]
        with self.assertRaisesRegex(
            supervisor.BackupSupervisorError, "cannot escape"
        ):
            supervisor._run_maintenance_scope(
                lock=mock.Mock(), callback=lambda scoped_capability: scoped_capability
            )
        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.require_mutation_capability(capability, "maintenance")

        class NoOpProve:
            operation = "maintenance"

            def prove(self, _operation: str) -> None:
                return None

        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.require_mutation_capability(NoOpProve(), "maintenance")
        with self.assertRaises(supervisor.BackupSupervisorError):
            supervisor.require_mutation_capability(object(), "maintenance")
        self.assertFalse(hasattr(supervisor, "_capability"))
        self.assertFalse(hasattr(supervisor, "_authority_issue_capability"))
        self.assertFalse(hasattr(supervisor, "_authority_issue_head"))
        for name, value in vars(supervisor).items():
            if any(fragment in name.lower() for fragment in ("issue", "register", "mutate")):
                self.assertFalse(callable(value), name)

        blocked_lock = mock.Mock()
        blocked_lock.validate.side_effect = supervisor.BackupSupervisorError("no lock")
        callback = mock.Mock()
        with mock.patch.object(supervisor.importlib, "import_module") as import_module:
            with self.assertRaises(supervisor.BackupSupervisorError):
                supervisor._run_local_backup_scope(
                    SimpleNamespace(dump_only=False),
                    app_dir=Path("/tmp/oldsparky-no-lock"),
                    lock=blocked_lock,
                    callback=callback,
                )
        callback.assert_not_called()
        import_module.assert_not_called()

        restore = mock.Mock()
        restore.expected_alembic_head.side_effect = RuntimeError("missing graph")
        producer = mock.Mock()
        with mock.patch.object(supervisor.importlib, "import_module", return_value=restore):
            with self.assertRaises(supervisor.BackupSupervisorError):
                supervisor._run_local_backup_scope(
                    SimpleNamespace(dump_only=False),
                    app_dir=Path("/tmp/oldsparky-missing-source"),
                    lock=mock.Mock(),
                    callback=producer,
                )
        producer.assert_not_called()
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
