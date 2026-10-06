from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import textwrap
import unittest
from unittest import mock

import yaml

from tests import platform_test_lock_support as lock_support
from tools import platform_storage_maintenance as maintenance
from tools.platform_disk_policy import BYTES_PER_GIB, snapshot_from_usage
from tools.platform_storage_maintenance import (
    apply_artifact_retention_plan,
    build_artifact_retention_plan,
    collect_old_children,
    delete_known_children,
    run_maintenance,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class PlatformStorageMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.release_dir = self.root / "dist" / "releases"
        self.release_dir.mkdir(parents=True)
        self.now = datetime(2026, 7, 20, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_artifact_group(self, slug: str, *, age_days: int) -> None:
        release = self.release_dir / slug
        release.mkdir()
        (release / "RELEASE.json").write_text(
            json.dumps({"release_slug": slug}), encoding="utf-8"
        )
        archive = self.release_dir / f"{slug}.tar.gz"
        checksum = self.release_dir / f"{slug}.tar.gz.sha256"
        archive.write_bytes(slug.encode())
        checksum.write_text("checksum\n", encoding="utf-8")
        timestamp = (self.now - timedelta(days=age_days)).timestamp()
        for path in (release, archive, checksum):
            os.utime(path, (timestamp, timestamp))

    def add_runtime_release(self, app_dir: Path, slug: str) -> Path:
        release = app_dir / "releases" / slug
        release.mkdir(parents=True)
        (release / "RELEASE.json").write_text(
            json.dumps({"release_slug": slug}),
            encoding="utf-8",
        )
        return release

    def maintenance_args(self, app_dir: Path) -> SimpleNamespace:
        web_dir = self.root / "web"
        web_dir.mkdir(exist_ok=True)
        return SimpleNamespace(
            app_dir=app_dir,
            source_release_dir=self.release_dir,
            web_artifact_dir=web_dir,
            backup_keep=14,
            backup_max_age_hours=24.0,
            release_keep=0,
            test_artifact_max_age_days=7,
            screenshot_max_age_days=30,
            failed_build_max_age_days=1,
            live_qa_runtime_keep=1,
            live_qa_runtime_root=self.root / "liveqa-cache",
            minimum_free_gib=0.0,
            maximum_used_percent=100.0,
            skip_backup=True,
            apply=True,
            backup_only=False,
        )

    def test_artifact_plan_keeps_five_and_protects_rollback(self) -> None:
        for index in range(7):
            self.add_artifact_group(f"release-{index}", age_days=7 - index)

        plan = build_artifact_retention_plan(
            self.release_dir,
            protected_slugs={"release-0"},
            keep=5,
        )

        self.assertEqual([group.slug for group in plan.protected], ["release-0"])
        self.assertEqual(
            {group.slug for group in plan.retained},
            {"release-2", "release-3", "release-4", "release-5", "release-6"},
        )
        self.assertEqual([group.slug for group in plan.candidates], ["release-1"])

    def test_artifact_apply_removes_directory_archive_and_checksum(self) -> None:
        self.add_artifact_group("release-current", age_days=0)
        self.add_artifact_group("release-old", age_days=10)
        plan = build_artifact_retention_plan(
            self.release_dir,
            protected_slugs={"release-current"},
            keep=1,
        )

        apply_artifact_retention_plan(plan, self.release_dir)

        self.assertTrue((self.release_dir / "release-current").exists())
        self.assertFalse((self.release_dir / "release-old").exists())
        self.assertFalse((self.release_dir / "release-old.tar.gz").exists())
        self.assertFalse((self.release_dir / "release-old.tar.gz.sha256").exists())

    def test_artifact_apply_refuses_replaced_planned_path(self) -> None:
        self.add_artifact_group("release-current", age_days=0)
        self.add_artifact_group("release-old", age_days=10)
        plan = build_artifact_retention_plan(
            self.release_dir,
            protected_slugs={"release-current"},
            keep=1,
        )
        archive = self.release_dir / "release-old.tar.gz"
        archive.rename(self.release_dir / "release-old.original.tar.gz")
        archive.write_bytes(b"replacement")

        with self.assertRaisesRegex(RuntimeError, "unsafe artifact deletion"):
            apply_artifact_retention_plan(plan, self.release_dir)
        self.assertTrue(archive.exists())

    def test_apply_refuses_pending_release_transaction_before_deletion(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        shared_dir = app_dir / "shared"
        shared_dir.mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        previous = self.add_runtime_release(app_dir, "release-previous")
        candidate = self.add_runtime_release(app_dir, "release-old")
        (app_dir / "current").symlink_to(current)
        (app_dir / "previous").symlink_to(previous)
        (shared_dir / ".release-operation.json").write_text(
            "pending\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RuntimeError, "must be recovered"):
            run_maintenance(self.maintenance_args(app_dir))
        self.assertTrue(candidate.exists())

    def test_apply_refuses_build_output_lock_contention_before_deletion(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        previous = self.add_runtime_release(app_dir, "release-previous")
        candidate = self.add_runtime_release(app_dir, "release-old")
        (app_dir / "current").symlink_to(current)
        (app_dir / "previous").symlink_to(previous)
        descriptor = os.open(self.release_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                RuntimeError,
                "holds the platform release build output lock",
            ):
                run_maintenance(self.maintenance_args(app_dir))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertTrue(candidate.exists())

    def test_transient_cleanup_is_pattern_and_age_bounded(self) -> None:
        web_dir = self.root / "web"
        web_dir.mkdir()
        old_result = web_dir / "test-results-old"
        fresh_result = web_dir / "test-results-fresh"
        unrelated = web_dir / "uploads"
        for path in (old_result, fresh_result, unrelated):
            path.mkdir()
            (path / "payload.bin").write_bytes(b"x" * 8)
        old_timestamp = (self.now - timedelta(days=8)).timestamp()
        fresh_timestamp = (self.now - timedelta(days=2)).timestamp()
        os.utime(old_result, (old_timestamp, old_timestamp))
        os.utime(fresh_result, (fresh_timestamp, fresh_timestamp))
        os.utime(unrelated, (old_timestamp, old_timestamp))

        candidates = collect_old_children(
            web_dir,
            patterns=("test-results*", "playwright-report*"),
            max_age_days=7,
            now=self.now,
        )
        reclaimed = delete_known_children(web_dir, candidates)

        self.assertEqual(candidates, (old_result.resolve(),))
        self.assertEqual(reclaimed, 8)
        self.assertFalse(old_result.exists())
        self.assertTrue(fresh_result.exists())
        self.assertTrue(unrelated.exists())

    def test_parser_defaults_match_health_disk_policy(self) -> None:
        with mock.patch.object(maintenance.sys, "argv", ["platform_storage_maintenance.py"]):
            args = maintenance.parse_args()

        self.assertEqual(args.minimum_free_gib, 5.0)
        self.assertEqual(args.maximum_used_percent, 85.0)
        self.assertEqual(args.backup_max_age_hours, 24.0)
        self.assertFalse(args.backup_only)

    def test_backup_only_cli_is_apply_only_and_cannot_skip_backup(self) -> None:
        with mock.patch.object(
            maintenance.sys,
            "argv",
            ["platform_storage_maintenance.py", "--backup-only"],
        ):
            with self.assertRaises(SystemExit):
                maintenance.parse_args()
        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--backup-only",
                "--apply",
                "--private-backup-diagnostics",
            ],
        ):
            self.assertTrue(maintenance.parse_args().private_backup_diagnostics)
        with mock.patch.object(
            maintenance.sys,
            "argv",
            ["platform_storage_maintenance.py", "--private-backup-diagnostics"],
        ):
            with self.assertRaises(SystemExit):
                maintenance.parse_args()
        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--backup-only",
                "--apply",
                "--backup-keep",
                "13",
            ],
        ):
            self.assertEqual(maintenance.parse_args().backup_keep, 13)
        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--backup-only",
                "--apply",
                "--skip-backup",
            ],
        ):
            with self.assertRaises(SystemExit):
                maintenance.parse_args()

    def test_backup_command_verifies_freshness_before_returning(self) -> None:
        create_result = {
            "ok": True,
            "size_bytes": 123,
            "duration_seconds": 4.5,
            "restore_verified": True,
            "alembic_revision_verified": True,
            "sha256": "a" * 64,
            "restored_table_count": 12,
            "removed": [],
            "rotation_mode": "rotate-existing",
        }
        preserve_result = {
            **create_result,
            "rotation_mode": "preserve-existing",
            "preexisting_archive_count": 9,
            "preexisting_sidecar_count": 9,
            "preexisting_archive_inventory_sha256": "a" * 64,
            "postexisting_archive_inventory_sha256": "a" * 64,
            "preexisting_archives_preserved": True,
        }
        check_result = {
            "ok": True,
            "restore_verified": True,
            "age_hours": 0.25,
        }
        with mock.patch.object(
            maintenance.subprocess,
            "run",
            side_effect=(
                subprocess.CompletedProcess([], 0, json.dumps(create_result), ""),
                subprocess.CompletedProcess([], 0, json.dumps(check_result), ""),
                subprocess.CompletedProcess([], 0, json.dumps(preserve_result), ""),
                subprocess.CompletedProcess([], 0, json.dumps(check_result), ""),
            ),
        ) as run:
            result = maintenance.run_backup(
                self.root / "runtime" / "platform",
                keep=14,
                max_age_hours=24.0,
            )
            preserved_result = maintenance.run_backup(
                self.root / "runtime" / "platform",
                keep=14,
                max_age_hours=24.0,
                rotate_existing=False,
            )

        self.assertEqual(run.call_count, 4)
        self.assertIn("--keep", run.call_args_list[0].args[0])
        self.assertIn("14", run.call_args_list[0].args[0])
        self.assertIn("--rotate-existing", run.call_args_list[0].args[0])
        self.assertIn("--check-latest", run.call_args_list[1].args[0])
        self.assertIn("24.0", run.call_args_list[1].args[0])
        self.assertIn("--preserve-existing", run.call_args_list[2].args[0])
        self.assertNotIn("--rotate-existing", run.call_args_list[2].args[0])
        self.assertTrue(result["restore_verified"])
        self.assertTrue(result["alembic_revision_verified"])
        self.assertTrue(result["checksum_present"])
        self.assertEqual(result["age_hours"], 0.25)
        self.assertEqual(result["rotation_mode"], "rotate-existing")
        self.assertTrue(preserved_result["preexisting_archives_preserved"])
        self.assertEqual(preserved_result["preexisting_archive_count"], 9)

        failure = subprocess.CompletedProcess(
            [], 1, json.dumps({"ok": False, "error": "private failure detail"}), "child diagnostic"
        )
        private_stderr = io.StringIO()
        with (
            mock.patch.object(maintenance.subprocess, "run", return_value=failure),
            mock.patch.object(maintenance.sys, "stderr", private_stderr),
        ):
            with self.assertRaisesRegex(RuntimeError, "Platform backup failed"):
                maintenance._run_backup_command(
                    ["fixed-backup-child"], forward_failure_diagnostics=True
                )
        self.assertIn("private failure detail", private_stderr.getvalue())
        self.assertIn("child diagnostic", private_stderr.getvalue())
        public_stderr = io.StringIO()
        with (
            mock.patch.object(maintenance.subprocess, "run", return_value=failure),
            mock.patch.object(maintenance.sys, "stderr", public_stderr),
        ):
            with self.assertRaisesRegex(RuntimeError, "Platform backup failed"):
                maintenance._run_backup_command(["fixed-backup-child"])
        self.assertEqual(public_stderr.getvalue(), "")

    def test_backup_failure_prevents_retention_deletion(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        previous = self.add_runtime_release(app_dir, "release-previous")
        candidate = self.add_runtime_release(app_dir, "release-old")
        (app_dir / "current").symlink_to(current)
        (app_dir / "previous").symlink_to(previous)
        args = self.maintenance_args(app_dir)
        args.skip_backup = False

        with mock.patch.object(
            maintenance,
            "run_backup",
            side_effect=RuntimeError("backup failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                maintenance._plan_and_maybe_apply(
                    args,
                    app_dir=app_dir,
                    source_release_dir=self.release_dir,
                )

        self.assertTrue(candidate.exists())

    def _assert_backup_lock_contention_in_subprocess(
        self,
        *,
        release_lock_path: Path,
        retained_load_lock_path: Path,
        source_release_dir: Path,
        expected_message: str,
        marker: Path,
        app_dir: Path,
    ) -> None:
        child = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            from types import SimpleNamespace

            sys.path.insert(0, sys.argv[1])
            from tools import platform_release_retention as retention
            from tools import platform_storage_maintenance as maintenance

            retention.RELEASE_LOCK_PATH = Path(sys.argv[2])
            retention.RETAINED_LOAD_LOCK_PATH = Path(sys.argv[3])
            args = SimpleNamespace(source_release_dir=Path(sys.argv[4]))
            marker = Path(sys.argv[5])
            app_dir = Path(sys.argv[6])

            def fake_backup(*_args, **_kwargs):
                marker.write_text("called", encoding="utf-8")
                return {}

            maintenance.run_backup = fake_backup
            try:
                with maintenance.maintenance_lock_scope(args, app_dir=app_dir):
                    maintenance.run_backup(app_dir, keep=14)
            except RuntimeError as exc:
                if sys.argv[7] not in str(exc):
                    print(str(exc))
                    raise SystemExit(2)
            else:
                raise SystemExit(3)
            if marker.exists():
                raise SystemExit(4)
            """
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(REPO_ROOT / "platform"),
                str(release_lock_path),
                str(retained_load_lock_path),
                str(source_release_dir),
                str(marker),
                str(app_dir),
                expected_message,
            ],
            cwd=REPO_ROOT / "platform",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertFalse(marker.exists())

    def test_backup_lock_contention_fails_closed_in_subprocess(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        release_lock = lock_support.create_test_lock("backup-release")
        retained_load_lock = lock_support.create_test_lock("backup-retained")
        try:
            release_lock.acquire(nonblocking=True)
            self._assert_backup_lock_contention_in_subprocess(
                release_lock_path=release_lock.path,
                retained_load_lock_path=retained_load_lock.path,
                source_release_dir=self.root / "missing-source-releases",
                expected_message="holds the platform release lock",
                marker=self.root / "release-marker",
                app_dir=app_dir,
            )
            release_lock.release()

            retained_load_lock.acquire(nonblocking=True)
            self._assert_backup_lock_contention_in_subprocess(
                release_lock_path=release_lock.path,
                retained_load_lock_path=retained_load_lock.path,
                source_release_dir=self.root / "missing-source-releases",
                expected_message="holds the retained-load lock",
                marker=self.root / "retained-marker",
                app_dir=app_dir,
            )
            retained_load_lock.release()
        finally:
            release_lock.cleanup()
            retained_load_lock.cleanup()

    def test_backup_build_lock_contention_fails_closed_in_subprocess(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        source_release_dir = self.root / "source-releases"
        source_release_dir.mkdir(mode=0o700)
        source_release_dir.chmod(0o700)
        release_lock = lock_support.create_test_lock("backup-build-release")
        retained_load_lock = lock_support.create_test_lock("backup-build-retained")
        descriptor = os.open(source_release_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._assert_backup_lock_contention_in_subprocess(
                release_lock_path=release_lock.path,
                retained_load_lock_path=retained_load_lock.path,
                source_release_dir=source_release_dir,
                expected_message="holds the platform release build output lock",
                marker=self.root / "build-marker",
                app_dir=app_dir,
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            release_lock.cleanup()
            retained_load_lock.cleanup()

    def test_backup_only_success_never_applies_retention(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        previous = self.add_runtime_release(app_dir, "release-previous")
        candidate = self.add_runtime_release(app_dir, "release-old")
        (app_dir / "current").symlink_to(current)
        (app_dir / "previous").symlink_to(previous)
        web_candidate = self.root / "web" / "test-results-old"
        web_candidate.mkdir(parents=True)
        args = self.maintenance_args(app_dir)
        args.skip_backup = False
        args.backup_only = True
        events: list[str] = []

        @maintenance.contextmanager
        def tracked_scope(*_args: object, **_kwargs: object):
            events.append("locks-enter")
            yield self.release_dir
            events.append("locks-exit")

        with (
            mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
            mock.patch.object(maintenance, "_plan_and_maybe_apply") as retention_plan,
            mock.patch.object(
                maintenance,
                "run_backup",
                return_value={
                    "size_bytes": 123,
                    "duration_seconds": 1.0,
                    "restore_verified": True,
                    "alembic_revision_verified": True,
                    "checksum_present": True,
                    "restored_table_count": 3,
                    "age_hours": 0.1,
                    "removed_count": 0,
                    "rotation_mode": "preserve-existing",
                    "preexisting_archive_count": 9,
                    "preexisting_sidecar_count": 9,
                    "preexisting_archive_inventory_sha256": "a" * 64,
                    "postexisting_archive_inventory_sha256": "a" * 64,
                    "preexisting_archives_preserved": True,
                },
            ) as run_backup,
            mock.patch.object(
                maintenance.live_qa_guard,
                "prune_runtime_cache_release_lock_held",
            ) as live_qa_prune,
        ):
            report = run_maintenance(args)

        self.assertEqual(report["mode"], "backup-only")
        self.assertTrue(report["backup"]["restore_verified"])
        self.assertEqual(report["backup"]["rotation_mode"], "preserve-existing")
        self.assertTrue(report["backup"]["preexisting_archives_preserved"])
        self.assertEqual(events, ["locks-enter", "locks-exit"])
        run_backup.assert_called_once_with(
            app_dir,
            keep=14,
            max_age_hours=24.0,
            rotate_existing=False,
            private_failure_diagnostics=False,
        )
        live_qa_prune.assert_not_called()
        retention_plan.assert_not_called()
        self.assertTrue(candidate.exists())
        self.assertTrue(web_candidate.exists())
        self.assertEqual(report["production_releases"]["deleted_count"], 0)
        self.assertEqual(report["source_release_artifacts"]["deleted_count"], 0)
        self.assertEqual(report["live_qa_runtime_caches"]["deleted_count"], 0)
        self.assertEqual(report["limits"]["backup_keep"], 14)
        self.assertEqual(report["transient"]["failed_builds"]["count"], 0)
        self.assertEqual(report["transient"]["browser_test_artifacts"]["count"], 0)
        self.assertEqual(report["transient"]["preprod_screenshots"]["count"], 0)

    def test_backup_only_failure_does_not_prune_or_check_live_qa(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        previous = self.add_runtime_release(app_dir, "release-previous")
        candidate = self.add_runtime_release(app_dir, "release-old")
        (app_dir / "current").symlink_to(current)
        (app_dir / "previous").symlink_to(previous)
        args = self.maintenance_args(app_dir)
        args.skip_backup = False
        args.backup_only = True

        @maintenance.contextmanager
        def tracked_scope(*_args: object, **_kwargs: object):
            yield self.release_dir

        with (
            mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
            mock.patch.object(
                maintenance,
                "run_backup",
                side_effect=RuntimeError("restore verification failed"),
            ),
            mock.patch.object(
                maintenance.live_qa_guard,
                "prune_runtime_cache_release_lock_held",
            ) as live_qa_prune,
        ):
            with self.assertRaisesRegex(RuntimeError, "restore verification failed"):
                run_maintenance(args)

        live_qa_prune.assert_not_called()
        self.assertTrue(candidate.exists())

    def test_disk_snapshot_uses_available_free_for_conservative_percent(self) -> None:
        snapshot = snapshot_from_usage(
            SimpleNamespace(
                total=100 * BYTES_PER_GIB,
                free=15 * BYTES_PER_GIB,
            )
        )
        with mock.patch.object(
            maintenance, "disk_snapshot_for_path", return_value=snapshot
        ):
            result = maintenance.disk_snapshot(self.root)

        self.assertEqual(result["used_bytes"], 85 * BYTES_PER_GIB)
        self.assertEqual(result["free_bytes"], 15 * BYTES_PER_GIB)
        self.assertEqual(result["used_percent"], 85.0)

    def test_storage_gate_accepts_exact_boundaries_and_fails_closed(self) -> None:
        cases = (
            (100 * BYTES_PER_GIB, 15 * BYTES_PER_GIB, True),
            (20 * BYTES_PER_GIB, 5 * BYTES_PER_GIB, True),
            (100 * BYTES_PER_GIB, 14 * BYTES_PER_GIB, False),
            (20 * BYTES_PER_GIB, 4 * BYTES_PER_GIB, False),
            (0, 0, False),
        )
        for total, free, expected in cases:
            with self.subTest(total=total, free=free):
                snapshot = snapshot_from_usage(
                    SimpleNamespace(total=total, free=free)
                )
                self.assertEqual(
                    maintenance.disk_is_healthy(
                        snapshot,
                        min_free_bytes=5 * BYTES_PER_GIB,
                        max_used_percent=85.0,
                    ),
                    expected,
                )

    def test_operational_files_define_bounded_maintenance(self) -> None:
        health_service = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-health-monitor.service"
        ).read_text()
        health_monitor = (
            REPO_ROOT / "platform/tools/platform_health_monitor.py"
        ).read_text()
        self.assertIn("HEALTH_OPERATION_BUDGET_SECONDS", health_monitor)
        self.assertIn("HEALTH_SERVICE_TIMEOUT_SECONDS = HEALTH_OPERATION_BUDGET_SECONDS + 35.0", health_monitor)
        self.assertIn("TimeoutStartSec=90s", health_service)
        self.assertEqual(
            [line for line in health_service.splitlines() if line.startswith("ExecStart=")],
            [
                "ExecStart=/opt/oldsparky/platform/shared/venv/bin/python "
                "/opt/oldsparky/platform/current/tools/platform_health_monitor.py "
                "--disk-min-free-gib 5 --disk-max-used-percent 85"
            ],
        )
        service = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-maintenance.service"
        ).read_text()
        timer = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-maintenance.timer"
        ).read_text()
        journald = (
            REPO_ROOT / "platform/deploy/journald/60-deadlock-platform-retention.conf"
        ).read_text()

        self.assertIn("platform_storage_maintenance.py --apply", service)
        self.assertEqual(
            [line for line in service.splitlines() if line.startswith("ExecStart=")],
            [
                "ExecStart=/opt/oldsparky/platform/shared/venv/bin/python "
                "/opt/oldsparky/platform/current/tools/"
                "platform_storage_maintenance.py --apply --backup-keep 14 "
                "--release-keep 5 --test-artifact-max-age-days 7 "
                "--screenshot-max-age-days 30 --failed-build-max-age-days 1 "
                "--minimum-free-gib 5 --maximum-used-percent 85"
            ],
        )
        self.assertNotIn("prune-runtime-cache", service)
        retention = (
            REPO_ROOT / "platform/tools/platform_release_retention.py"
        ).read_text()
        maintenance_source = (
            REPO_ROOT / "platform/tools/platform_storage_maintenance.py"
        ).read_text()
        maintenance_workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-storage-maintenance.yml"
        ).read_text()
        self.assertIn("exclusive_retained_load_lock", retention)
        self.assertIn("with release_operation_lock(app_dir)", maintenance_source)
        self.assertIn("with exclusive_retained_load_lock()", maintenance_source)
        self.assertIn("reverse load -> release edge", maintenance_workflow)
        self.assertNotIn(
            'exec 9>"$retained_load_lock"', maintenance_workflow
        )
        self.assertIn("CPUQuota=50%", service)
        self.assertIn("IOSchedulingClass=idle", service)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=30m", timer)
        self.assertIn("SystemMaxUse=256M", journald)
        self.assertIn("SystemMaxFileSize=32M", journald)
        self.assertIn("ForwardToSyslog=no", journald)
        self.assertIn("SystemKeepFree=5G", journald)
        self.assertIn("MaxRetentionSec=30day", journald)

        nginx_logrotate = (REPO_ROOT / "platform/deploy/logrotate/nginx").read_text()
        rsyslog_logrotate = (REPO_ROOT / "platform/deploy/logrotate/rsyslog").read_text()
        ufw_logrotate = (REPO_ROOT / "platform/deploy/logrotate/ufw").read_text()
        btmp_logrotate = (REPO_ROOT / "platform/deploy/logrotate/btmp").read_text()
        logrotate_service = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-logrotate.service"
        ).read_text()
        logrotate_timer = (
            REPO_ROOT / "platform/deploy/systemd/deadlock-logrotate.timer"
        ).read_text()
        rsyslog_filter = (
            REPO_ROOT / "platform/deploy/rsyslog/05-deadlock-platform.conf"
        ).read_text()
        self.assertIn("size 50M", nginx_logrotate)
        self.assertIn("rotate 7", nginx_logrotate)
        self.assertIn("nginx -s reopen", nginx_logrotate)
        self.assertIn("size 50M", rsyslog_logrotate)
        self.assertIn("rotate 7", rsyslog_logrotate)
        self.assertNotIn("/var/log/ufw.log", rsyslog_logrotate)
        self.assertIn("size 50M", ufw_logrotate)
        self.assertIn("rotate 7", ufw_logrotate)
        self.assertIn("size 16M", btmp_logrotate)
        self.assertIn("/usr/sbin/logrotate /etc/logrotate.conf", logrotate_service)
        self.assertIn("OnUnitActiveSec=15m", logrotate_timer)
        self.assertIn(':msg,contains,"[UFW " /var/log/ufw.log', rsyslog_filter)
        self.assertIn("& stop", rsyslog_filter)

        release_install = (
            REPO_ROOT / "platform/tools/platform_release_install.sh"
        ).read_text()
        self.assertIn('chmod 0600 "$SHARED_ENV_FILE"', release_install)

    def test_manual_backup_uses_lock_aware_backup_only_workflow(self) -> None:
        workflow_path = REPO_ROOT / ".github/workflows/platform-production-backup.yml"
        workflow = workflow_path.read_text()
        self.assertIn("platform_storage_maintenance.py", workflow)
        self.assertIn("--backup-only", workflow)
        self.assertIn('"--backup-max-age-hours", "24"', workflow)
        self.assertNotIn("--backup-keep 14", workflow)
        self.assertIn("--apply", workflow)
        self.assertNotIn("platform_backup_restore_drill.py", workflow)
        self.assertNotIn("--check-latest", workflow)
        self.assertIn('report.get("mode") != "backup-only"', workflow)
        self.assertIn('backup.get("rotation_mode") != "preserve-existing"', workflow)
        self.assertIn('backup.get("preexisting_archives_preserved") is not True', workflow)
        self.assertIn('backup.get("removed_count") != 0', workflow)
        self.assertIn('isinstance(backup.get("preexisting_archive_count"), int)', workflow)
        self.assertIn('isinstance(backup.get("preexisting_sidecar_count"), int)', workflow)
        self.assertIn('r"[0-9a-f]{64}"', workflow)
        self.assertIn("BACKUP_FAILURE schema=1 stage=", workflow)
        self.assertIn('test "$(id -u)" -eq 0', workflow)
        self.assertIn("exit_code=", workflow)
        self.assertEqual(workflow.count("trap backup_failure_marker EXIT"), 1)
        self.assertNotIn("trap cleanup EXIT", workflow)
        self.assertIn('backup_report_file=""', workflow)
        self.assertIn('rm -f -- "$backup_report_file"', workflow)
        self.assertIn('trap - EXIT', workflow)
        self.assertIn('exit "$exit_code"', workflow)
        self.assertIn("start_new_session=True", workflow)
        self.assertIn("os.killpg(process.pid, signal.SIGTERM)", workflow)
        self.assertIn("process.wait(timeout=10)", workflow)
        self.assertIn("os.killpg(process.pid, signal.SIGKILL)", workflow)
        self.assertIn("if process is not None:\n                  stop_child_group(process)", workflow)
        self.assertIn("oldsparky-production-backup-{run_id}-{run_attempt}.stderr", workflow)
        self.assertIn("os.O_EXCL | os.O_NOFOLLOW", workflow)
        self.assertIn("captured < 65536", workflow)
        self.assertIn('"--private-backup-diagnostics"', workflow)
        self.assertIn("failure_stage=private_capture_cleanup", workflow)
        self.assertNotIn('cat "$remote_error"', workflow)
        self.assertLess(
            workflow.index("failure_stage=private_capture_cleanup"),
            workflow.index('printf \'%s\\n\' "$public_report"'),
        )
        self.assertIn('backup.get("restore_verified") is not True', workflow)
        self.assertIn('backup.get("checksum_present") is not True', workflow)
        self.assertIn('public["mode"] = "backup-only"', workflow)
        self.assertIn(
            '"production_releases",\n              "source_release_artifacts",\n              "live_qa_runtime_caches"',
            workflow,
        )
        self._assert_backup_failure_trap_executes(workflow_path)
        self._assert_backup_child_group_is_terminated(workflow_path)
        self._assert_backup_wrapper_bounds_private_stderr(workflow_path)

    def _assert_backup_failure_trap_executes(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(
            workflow_path.read_text()
        )
        run_script = next(
            step["run"]
            for step in workflow["jobs"]["backup"]["steps"]
            if step.get("name") == "Create and verify the production backup"
        )
        remote_script = run_script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        remote_script = remote_script.replace(
            "runtime=/opt/oldsparky/platform", "runtime=/not-a-production-tree"
        )
        remote_script = remote_script.replace(
            'test "$(id -u)" -eq 0 || { echo "Production backup must run as root" >&2; exit 1; }',
            ":",
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            report_path = Path(temporary_dir) / "remote-report"
            remote_script = remote_script.replace(
                'backup_report_file="$(mktemp /tmp/oldsparky-production-backup-report.XXXXXX)"',
                f'backup_report_file="$(mktemp {report_path}.XXXXXX)"',
            )
            completed = subprocess.run(
                ["bash", "-s", "--", "123456", "1"],
                input=remote_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertRegex(
                completed.stdout,
                r"^BACKUP_FAILURE schema=1 stage=preflight exit_code=[1-9][0-9]{0,2}\n$",
            )
            self.assertIn("Current release symlink is missing", completed.stderr)
            self.assertEqual(list(Path(temporary_dir).iterdir()), [])

    def _assert_backup_child_group_is_terminated(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(workflow_path.read_text())
        run_script = next(
            step["run"]
            for step in workflow["jobs"]["backup"]["steps"]
            if step.get("name") == "Create and verify the production backup"
        )
        remote_script = run_script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        wrapper_script = remote_script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        function_start = wrapper_script.index("def stop_child_group(process):")
        function_end = wrapper_script.index("signal.signal(signal.SIGTERM", function_start)
        function_source = wrapper_script[function_start:function_end].replace(
            "timeout=10", "timeout=0.2"
        )
        namespace: dict[str, object] = {
            "os": os,
            "signal": signal,
            "subprocess": subprocess,
        }
        exec(function_source, namespace)

        with tempfile.TemporaryDirectory() as temporary_dir:
            child_code = textwrap.dedent(
                """
                import signal
                import subprocess
                import sys
                import time
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                grandchild_code = (
                    "import signal,time; "
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    "time.sleep(120)"
                )
                grandchild = subprocess.Popen([sys.executable, "-c", grandchild_code])
                print(grandchild.pid, flush=True)
                while True:
                    time.sleep(1)
                """
            )
            process = subprocess.Popen(
                [sys.executable, "-c", child_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
                cwd=temporary_dir,
            )
            assert process.stdout is not None
            grandchild_pid = int(process.stdout.readline().strip())
            namespace["process"] = process
            with self.assertRaises(InterruptedError):
                namespace["interrupt_child"](signal.SIGTERM, None)  # type: ignore[operator]
            process.stdout.close()
            self.assertIsNotNone(process.returncode)
            proc_stat = Path(f"/proc/{grandchild_pid}/stat")
            for _ in range(30):
                if not proc_stat.exists() or proc_stat.read_text().split()[2] == "Z":
                    break
                time.sleep(0.1)
            else:
                self.fail("backup cancellation left a child process running")

    def _assert_backup_wrapper_bounds_private_stderr(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(workflow_path.read_text())
        run_script = next(
            step["run"]
            for step in workflow["jobs"]["backup"]["steps"]
            if step.get("name") == "Create and verify the production backup"
        )
        remote_script = run_script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        wrapper_script = remote_script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        wrapper_script = wrapper_script.replace(
            'capture_path = Path(f"/tmp/oldsparky-production-backup-{run_id}-{run_attempt}.stderr")',
            'capture_path = Path("/tmp/placeholder.stderr")',
        )
        wrapper_script = wrapper_script.replace(
            'capture_path = Path("/tmp/placeholder.stderr")',
            'capture_path = Path(sys.argv[7])',
        )
        wrapper_script = wrapper_script.replace(
            "capture_stat.st_uid != 0", "capture_stat.st_uid != os.geteuid()"
        ).replace("report_stat.st_uid != 0", "report_stat.st_uid != os.geteuid()")
        command_start = wrapper_script.index("command = [")
        command_end = wrapper_script.index("def stop_child_group", command_start)
        child_code = "import sys; sys.stderr.buffer.write(b'x' * 100000); raise SystemExit(7)"
        wrapper_script = (
            wrapper_script[:command_start]
            + f"command = [sys.executable, '-I', '-c', {child_code!r}]\n\n"
            + wrapper_script[command_end:]
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            report_path = Path(temporary_dir) / "report.json"
            report_path.write_text("", encoding="utf-8")
            os.chmod(report_path, 0o600)
            capture_path = Path(temporary_dir) / "private.stderr"
            wrapper_script = wrapper_script.replace(
                "capture_path = Path(sys.argv[7])",
                f"capture_path = Path({str(capture_path)!r})",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-",
                    "unused-python",
                    "unused-maintenance",
                    "unused-runtime",
                    "123456",
                    "1",
                    str(report_path),
                ],
                input=wrapper_script,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 7, completed.stderr)
            self.assertEqual(completed.stdout, "")
            captured = capture_path.stat()
            self.assertEqual(captured.st_mode & 0o777, 0o600)
            self.assertEqual(captured.st_size, 65536)
            self.assertEqual(capture_path.read_bytes(), b"x" * 65536)

        maintenance_source = (
            REPO_ROOT / "platform/tools/platform_storage_maintenance.py"
        ).read_text()
        lock_scope_start = maintenance_source.index("def maintenance_lock_scope")
        lock_scope = maintenance_source[lock_scope_start : maintenance_source.index(
            "def _plan_and_maybe_apply", lock_scope_start
        )]
        self.assertLess(
            lock_scope.index("release_operation_lock"),
            lock_scope.index("exclusive_retained_load_lock"),
        )
        self.assertLess(
            lock_scope.index("exclusive_retained_load_lock"),
            lock_scope.index("source_release_lock"),
        )
        backup_only_start = maintenance_source.index(
            "if getattr(args, \"backup_only\", False)"
        )
        backup_only = maintenance_source[backup_only_start : maintenance_source.index(
            "else:", backup_only_start
        )]
        self.assertIn("run_backup(", backup_only)
        self.assertNotIn("prune_runtime_cache_release_lock_held", backup_only)
        self.assertNotIn("_plan_and_maybe_apply(", backup_only)

    @unittest.skipUnless(os.geteuid() == 0, "report binding fixture requires root")
    def test_maintenance_workflow_binds_report_to_new_service_invocation(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-storage-maintenance.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('"owner":"deadlock-maintenance.service"', workflow)
        self.assertIn('"state":"service_managed"', workflow)
        self.assertNotIn("state=held", workflow)
        start_marker = 'report_name="$(/usr/bin/python3 -I - "$report_dir" '
        start = workflow.index(start_marker)
        heredoc = workflow.index("<<'PY'\n", start) + len("<<'PY'\n")
        end = workflow.index("\n          PY\n          )\"", heredoc)
        binder = textwrap.dedent(workflow[heredoc:end])

        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            completed_at = datetime.now(UTC) + timedelta(seconds=5)
            started_at = completed_at - timedelta(seconds=10)
            (report_dir / "platform-maintenance-20261005T120000Z.json").write_text(
                json.dumps(
                    {
                        "mode": "apply",
                        "ok": False,
                        "started_at_utc": (
                            started_at + timedelta(seconds=2)
                        ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                        "completed_at_utc": (
                            started_at + timedelta(seconds=3)
                        ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                        "production_releases": {
                            "protected": ["release-current", "release-previous"]
                        },
                        "backup": {"status": "completed", "restore_verified": True},
                        "private": "/root/private-maintenance-report.json",
                    }
                ),
                encoding="utf-8",
            )
            report = report_dir / "platform-maintenance-20261005T120000Z.json"
            report.chmod(0o600)
            arguments = [
                str(report_dir),
                started_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                completed_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "0" * 32,
                "1" * 32,
                "release-current",
                "release-previous",
            ]
            bound = subprocess.run(
                [sys.executable, "-I", "-", *arguments],
                input=binder,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            self.assertEqual(bound.stdout.strip(), report.name)
            self.assertNotIn("private-maintenance-report", bound.stdout)

            stale_start = completed_at + timedelta(seconds=10)
            stale_end = stale_start + timedelta(seconds=10)
            missing = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-",
                    str(report_dir),
                    stale_start.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    stale_end.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "1" * 32,
                    "2" * 32,
                    "release-current",
                    "release-previous",
                ],
                input=binder,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 3)
            self.assertEqual(missing.stdout, "")
            self.assertNotIn("private-maintenance-report", missing.stderr)

    def test_maintenance_workflow_preserves_unhealthy_report_and_remote_status_after_upload(self) -> None:
        workflow_path = (
            REPO_ROOT
            / ".github/workflows/platform-production-storage-maintenance.yml"
        )
        workflow = workflow_path.read_text(encoding="utf-8")
        classifier_start = workflow.index(
            'maintenance_result="$(/usr/bin/python3 -I - '
        )
        classifier_heredoc = workflow.index("<<'PY'\n", classifier_start) + len("<<'PY'\n")
        classifier_end = workflow.index("\n          PY\n          )\"", classifier_heredoc)
        classifier = textwrap.dedent(workflow[classifier_heredoc:classifier_end])

        with tempfile.TemporaryDirectory() as temporary:
            summary_path = Path(temporary) / "retention-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "mode": "apply",
                        "ok": False,
                        "backup": {"status": "completed", "restore_verified": True},
                        "disk_after": {"free_bytes": 5_200_000_000, "used_percent": 86.96},
                        "limits": {
                            "minimum_free_bytes": 5 * BYTES_PER_GIB,
                            "maximum_used_percent": 85,
                        },
                        "private": "PRIVATE_REPORT_CONTENT",
                    }
                ),
                encoding="utf-8",
            )
            unhealthy = subprocess.run(
                [sys.executable, "-I", "-", str(summary_path), "1"],
                input=classifier,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(unhealthy.returncode, 0, unhealthy.stderr)
            result = json.loads(unhealthy.stdout)
            self.assertEqual(result["status"], "unhealthy")
            self.assertEqual(result["error_class"], "disk_threshold")
            self.assertEqual(result["service_exit_code"], 1)
            self.assertNotIn("PRIVATE_REPORT_CONTENT", unhealthy.stdout)

            summary_path.write_text(
                json.dumps(
                    {
                        "mode": "apply",
                        "ok": True,
                        "backup": {"status": "completed", "restore_verified": True},
                        "disk_after": {"free_bytes": 5_200_000_000, "used_percent": 86.96},
                        "limits": {
                            "minimum_free_bytes": 5 * BYTES_PER_GIB,
                            "maximum_used_percent": 85,
                        },
                    }
                ),
                encoding="utf-8",
            )
            inconsistent = subprocess.run(
                [sys.executable, "-I", "-", str(summary_path), "0"],
                input=classifier,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(inconsistent.returncode, 0, inconsistent.stderr)
            inconsistent_result = json.loads(inconsistent.stdout)
            self.assertEqual(inconsistent_result["status"], "failed")
            self.assertEqual(inconsistent_result["error_class"], "inconsistent_result")

            inconsistent_service_exit = subprocess.run(
                [sys.executable, "-I", "-", str(summary_path), "23"],
                input=classifier,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(inconsistent_service_exit.returncode, 0)
            self.assertEqual(
                json.loads(inconsistent_service_exit.stdout)["service_exit_code"], 23
            )

            summary_path.write_text(
                json.dumps(
                    {
                        "mode": "apply",
                        "ok": True,
                        "backup": {"status": "completed", "restore_verified": True},
                        "disk_after": {
                            "free_bytes": 5 * BYTES_PER_GIB,
                            "used_percent": 85,
                        },
                        "limits": {
                            "minimum_free_bytes": 5 * BYTES_PER_GIB,
                            "maximum_used_percent": 85,
                        },
                    }
                ),
                encoding="utf-8",
            )
            exact_boundaries = subprocess.run(
                [sys.executable, "-I", "-", str(summary_path), "0"],
                input=classifier,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(exact_boundaries.returncode, 0)
            boundary_result = json.loads(exact_boundaries.stdout)
            self.assertEqual(boundary_result["status"], "ok")
            self.assertEqual(boundary_result["error_class"], "none")

            equal_boundary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
            equal_boundary_payload["ok"] = False
            summary_path.write_text(
                json.dumps(equal_boundary_payload), encoding="utf-8"
            )
            equal_boundary_mismatch = subprocess.run(
                [sys.executable, "-I", "-", str(summary_path), "1"],
                input=classifier,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(equal_boundary_mismatch.returncode, 0)
            equal_mismatch_result = json.loads(equal_boundary_mismatch.stdout)
            self.assertEqual(equal_mismatch_result["status"], "failed")
            self.assertEqual(
                equal_mismatch_result["error_class"], "inconsistent_result"
            )

            run_start = workflow.index("      - name: Run installed bounded maintenance service\n")
            run_marker = "        run: |\n"
            run_start = workflow.index(run_marker, run_start) + len(run_marker)
            run_end = workflow.index("\n      - name: Remove production SSH material", run_start)
            run_script = textwrap.dedent(workflow[run_start:run_end])
            enforce_start = workflow.index("      - name: Enforce maintenance result\n")
            enforce_start = workflow.index(run_marker, enforce_start) + len(run_marker)
            enforce_end = workflow.find("\n      - name: ", enforce_start)
            if enforce_end < 0:
                enforce_end = len(workflow)
            enforce_script = textwrap.dedent(workflow[enforce_start:enforce_end])

            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            fake_ssh = fake_bin / "ssh"
            fake_ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                "sys.stdout.write(os.environ['FIXTURE_REMOTE_STDOUT'])\n"
                "sys.stderr.write(os.environ['FIXTURE_REMOTE_STDERR'])\n"
                "raise SystemExit(int(os.environ['FIXTURE_SSH_STATUS']))\n",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o700)
            runner_temp = Path(temporary) / "runner"
            runner_temp.mkdir()
            github_output = Path(temporary) / "github-output"
            github_output.touch()
            github_env = Path(temporary) / "github-env"
            github_env.touch()
            private_stderr = "PRIVATE_SSH_STDERR_SENTINEL"
            fixture = (
                '{"schema":1,"kind":"collection_failure",'
                '"status":"failed","stage":"report_binding",'
                '"outcome":"report_unavailable","exit_code":23}\n'
            )
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.defpath}",
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(github_output),
                "GITHUB_ENV": str(github_env),
                "SSH_DIR": str(Path(temporary) / "absent-ssh-dir"),
                "PROD_SSH_HOST": "production.invalid",
                "PROD_SSH_USER": "operator",
                "EXPECTED_SHA": "a" * 40,
                "CONFIRMATION": "APPLY-PRODUCTION-STORAGE-MAINTENANCE",
                "FIXTURE_REMOTE_STDOUT": fixture,
                "FIXTURE_REMOTE_STDERR": private_stderr,
                "FIXTURE_SSH_STATUS": "23",
            }
            collected = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", run_script],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(collected.returncode, 0, collected.stderr)
            artifact = runner_temp / "platform-production-storage-maintenance-artifacts" / "maintenance.log"
            artifact_text = artifact.read_text(encoding="utf-8")
            self.assertIn(fixture.strip(), artifact_text)
            self.assertNotIn(private_stderr, artifact_text)
            self.assertIn("remote_status=23", github_output.read_text(encoding="utf-8"))

            cleanup_start = workflow.index("      - name: Remove production SSH material\n")
            cleanup_start = workflow.index(run_marker, cleanup_start) + len(run_marker)
            cleanup_end = workflow.index("\n      - name: Publish maintenance evidence", cleanup_start)
            cleanup_script = textwrap.dedent(workflow[cleanup_start:cleanup_end])
            cleaned = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", cleanup_script],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
            self.assertFalse(artifact.parent.joinpath("ssh-error.log").exists())

            enforcement = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", enforce_script],
                env={**environment, "REMOTE_STATUS": "23"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(enforcement.returncode, 23)
            self.assertIn("after evidence upload", enforcement.stderr)
            self.assertLess(
                workflow.index("- name: Publish maintenance evidence"),
                workflow.index("- name: Enforce maintenance result"),
            )

    def test_apply_lock_order_and_live_qa_report_are_rollback_safe(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        previous = self.add_runtime_release(app_dir, "release-previous")
        (app_dir / "current").symlink_to(current)
        (app_dir / "previous").symlink_to(previous)
        events: list[str] = []
        original_release_lock = maintenance.release_operation_lock
        original_source_lock = maintenance.source_release_lock

        @maintenance.contextmanager
        def tracked_release_lock(path: Path):
            events.append("release-enter")
            with original_release_lock(path) as resolved:
                yield resolved
            events.append("release-exit")

        @maintenance.contextmanager
        def tracked_source_lock(path: Path):
            events.append("source-enter")
            with original_source_lock(path) as resolved:
                yield resolved
            events.append("source-exit")

        plan = maintenance.live_qa_guard.RuntimeCacheRetentionPlan(
            protected=(),
            retained=(),
            candidates=(),
            tombstones=(),
        )

        def prune_with_release_lock(**kwargs: object):
            self.assertTrue(kwargs["apply"])
            events.append("liveqa")
            return plan

        with (
            mock.patch.object(
                maintenance,
                "release_operation_lock",
                side_effect=tracked_release_lock,
            ),
            mock.patch.object(
                maintenance,
                "source_release_lock",
                side_effect=tracked_source_lock,
            ),
            mock.patch.object(
                maintenance.live_qa_guard,
                "prune_runtime_cache_release_lock_held",
                side_effect=prune_with_release_lock,
            ),
        ):
            report = run_maintenance(self.maintenance_args(app_dir))

        self.assertEqual(
            events,
            [
                "release-enter",
                "source-enter",
                "liveqa",
                "source-exit",
                "release-exit",
            ],
        )
        self.assertEqual(
            report["live_qa_runtime_caches"],
            {
                "protected": [],
                "retained": [],
                "deleted": [],
                "reclaimed_tombstones": [],
                "protected_count": 0,
                "retained_count": 0,
                "deleted_count": 0,
                "reclaimed_tombstone_count": 0,
            },
        )

    def test_removed_legacy_backup_template_is_not_a_runtime_dependency(self) -> None:
        legacy_template = (
            REPO_ROOT
            / "deploy/ansible/roles/oldsparky/templates/oldsparky-backup.sh.j2"
        )
        self.assertFalse(legacy_template.exists())

        backup_entrypoint = (
            REPO_ROOT / "platform/tools/platform_backup_db.sh"
        ).read_text()
        self.assertIn("platform_runtime_common.sh", backup_entrypoint)
        self.assertIn("platform_backup_restore_drill.py", backup_entrypoint)


if __name__ == "__main__":
    unittest.main()
