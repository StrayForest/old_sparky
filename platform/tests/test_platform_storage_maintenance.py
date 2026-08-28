from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import platform_storage_maintenance as maintenance
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

    def test_operational_files_define_bounded_maintenance(self) -> None:
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
