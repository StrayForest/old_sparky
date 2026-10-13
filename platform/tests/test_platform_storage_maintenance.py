from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
from types import SimpleNamespace
import textwrap
import unittest
import uuid
from unittest import mock
import zipfile

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


def _read_linux_proc_state(proc_stat: Path) -> str | None:
    try:
        stat_text = proc_stat.read_text(encoding="ascii")
    except FileNotFoundError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise
    except ProcessLookupError as exc:
        if exc.errno == errno.ESRCH:
            return None
        raise

    _prefix, separator, remainder = stat_text.rpartition(")")
    if not separator:
        raise ValueError("malformed /proc stat record")
    fields = remainder.split()
    if not fields or len(fields[0]) != 1 or fields[0] not in "RSDZTtXxKWPI":
        raise ValueError("malformed /proc stat process state")
    return fields[0]


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
            verify_existing_backup_only=False,
            preserve_existing_backups=False,
            purge_profile_access_cache_after_restore=False,
            evict_pinned_build_node_cache=False,
            eviction_run_id=None,
            eviction_run_attempt=None,
            eviction_source_sha=None,
            eviction_bundle_sha256=None,
            compact_legacy_fallback_runtime_cache=False,
            compaction_run_id=None,
            compaction_run_attempt=None,
            compaction_source_sha=None,
            compaction_bundle_sha256=None,
            resume_legacy_fallback_runtime_cache_compaction=False,
            resume_compaction_run_id=None,
            resume_compaction_run_attempt=None,
            resume_compaction_source_sha=None,
            resume_compaction_bundle_sha256=None,
            private_backup_diagnostics=False,
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
        self.assertFalse(args.preserve_existing_backups)
        self.assertFalse(args.evict_pinned_build_node_cache)
        self.assertIsNone(args.eviction_run_id)

    def test_authorization_probe_mode_is_exclusive_and_requires_private_json(self) -> None:
        valid = [
            "platform_storage_maintenance.py",
            "--authorization-probe-only",
            "--private-backup-diagnostics",
            "--apply",
            "--json",
        ]
        with mock.patch.object(maintenance.sys, "argv", valid):
            args = maintenance.parse_args()
        self.assertTrue(args.authorization_probe_only)
        self.assertTrue(args.private_backup_diagnostics)
        self.assertTrue(args.apply)
        self.assertTrue(args.as_json)

        invalid = (
            valid[:-2],
            [*valid, "--backup-only"],
            [*valid, "--purge-profile-access-cache-after-restore"],
            [*valid, "--preserve-existing-backups"],
        )
        for argv in invalid:
            with self.subTest(argv=argv), mock.patch.object(
                maintenance.sys, "argv", argv
            ), self.assertRaises(SystemExit):
                maintenance.parse_args()

    def test_authorization_probe_cli_uses_disjoint_closed_envelopes(self) -> None:
        success_report = {
            "schema": 1,
            "kind": "authorization_probe",
            "status": "completed",
            "authorization_probe": {"schema": 1, "status": "completed"},
        }
        with (
            mock.patch.object(
                maintenance.sys,
                "argv",
                [
                    "platform_storage_maintenance.py",
                    "--authorization-probe-only",
                    "--private-backup-diagnostics",
                    "--apply",
                    "--json",
                ],
            ),
            mock.patch.object(maintenance, "run_maintenance", return_value=success_report),
            mock.patch.object(maintenance, "write_report") as write_report,
            mock.patch.object(maintenance.sys, "stdout", io.StringIO()) as stdout,
        ):
            self.assertEqual(maintenance.main(), 0)
        write_report.assert_not_called()
        self.assertEqual(json.loads(stdout.getvalue()), success_report)
        self.assertNotIn("ok", success_report)

        failed = RuntimeError("private failure text must not be emitted")
        failed.scratch_cleanup_status = "failed"
        failed.scratch_first_fault = {
            "schema": 1,
            "origin": "worker",
            "site": "worker_mount_snapshot",
            "exception_kind": "permission",
            "errno": 1,
        }
        self.assertTrue(maintenance._valid_scratch_first_fault(failed.scratch_first_fault))
        self.assertTrue(
            maintenance.backup_restore_drill.valid_scratch_first_fault(
                failed.scratch_first_fault
            )
        )
        self.assertFalse(
            maintenance._valid_scratch_first_fault(
                {**failed.scratch_first_fault, "origin": "controller"}
            )
        )
        self.assertFalse(
            maintenance.backup_restore_drill.valid_scratch_first_fault(
                {**failed.scratch_first_fault, "origin": "controller"}
            )
        )
        controller_fault = {
            **failed.scratch_first_fault,
            "origin": "controller",
            "site": "controller_cleanup",
        }
        self.assertTrue(maintenance._valid_scratch_first_fault(controller_fault))
        self.assertFalse(
            maintenance._valid_scratch_first_fault(
                {**failed.scratch_first_fault, "raw_message": "not allowed"}
            )
        )
        for malformed in (
            {**failed.scratch_first_fault, "schema": 1.0},
            {**failed.scratch_first_fault, "errno": True},
            {**failed.scratch_first_fault, "errno": 13.0},
            {**failed.scratch_first_fault, "exception_kind": 7},
        ):
            with self.subTest(first_fault=malformed):
                self.assertFalse(maintenance._valid_scratch_first_fault(malformed))
        stderr = io.StringIO()
        with (
            mock.patch.object(
                maintenance.sys,
                "argv",
                [
                    "platform_storage_maintenance.py",
                    "--authorization-probe-only",
                    "--private-backup-diagnostics",
                    "--apply",
                    "--json",
                ],
            ),
            mock.patch.object(maintenance, "run_maintenance", side_effect=failed),
            mock.patch.object(maintenance.sys, "stdout", io.StringIO()) as stdout,
            mock.patch.object(maintenance.sys, "stderr", stderr),
        ):
            self.assertEqual(maintenance.main(), 1)
        failure_report = json.loads(stdout.getvalue())
        self.assertEqual(
            set(failure_report),
            {"schema", "kind", "status", "cleanup_status", "first_fault"},
        )
        self.assertEqual(failure_report["kind"], "authorization_probe")
        self.assertEqual(failure_report["status"], "failed")
        self.assertEqual(failure_report["cleanup_status"], "failed")
        self.assertEqual(failure_report["first_fault"], failed.scratch_first_fault)
        self.assertNotIn("private failure text", stdout.getvalue())
        self.assertNotIn("private failure text", stderr.getvalue())
        self.assertNotIn("ok", failure_report)

    def test_preserve_existing_backups_is_full_apply_only(self) -> None:
        with mock.patch.object(
            maintenance.sys,
            "argv",
            ["platform_storage_maintenance.py", "--preserve-existing-backups"],
        ), self.assertRaises(SystemExit):
            maintenance.parse_args()

        invalid_modes = (
            ["--apply", "--preserve-existing-backups", "--skip-backup"],
            ["--apply", "--preserve-existing-backups", "--backup-only"],
            [
                "--apply",
                "--preserve-existing-backups",
                "--verify-existing-backup-only",
            ],
        )
        for options in invalid_modes:
            with self.subTest(mode=options[-1]), mock.patch.object(
                maintenance.sys,
                "argv",
                ["platform_storage_maintenance.py", *options],
            ), self.assertRaises(SystemExit):
                maintenance.parse_args()

        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--apply",
                "--preserve-existing-backups",
            ],
        ):
            self.assertTrue(maintenance.parse_args().preserve_existing_backups)

    def test_full_maintenance_preserves_backups_and_keeps_other_retention(self) -> None:
        app_dir = self.root / "runtime" / "platform"
        self.add_runtime_release(app_dir, "release-current")
        self.add_runtime_release(app_dir, "release-previous")
        (app_dir / "current").symlink_to(app_dir / "releases" / "release-current")
        (app_dir / "previous").symlink_to(app_dir / "releases" / "release-previous")
        args = self.maintenance_args(app_dir)
        args.skip_backup = False
        args.preserve_existing_backups = True

        with (
            mock.patch.object(maintenance, "run_backup", return_value={"restore_verified": True}) as backup,
            mock.patch.object(maintenance, "apply_release_plan") as release_retention,
            mock.patch.object(maintenance, "apply_artifact_retention_plan") as artifact_retention,
        ):
            result = maintenance._plan_and_maybe_apply(
                args,
                app_dir=app_dir,
                source_release_dir=self.release_dir,
            )

        backup.assert_called_once_with(
            app_dir,
            keep=14,
            max_age_hours=24.0,
            rotate_existing=False,
        )
        release_retention.assert_called_once()
        artifact_retention.assert_called_once()
        self.assertTrue(result[5]["restore_verified"])

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
                "--evict-pinned-build-node-cache",
                "--backup-only",
                "--apply",
            ],
        ):
            with self.assertRaises(SystemExit):
                maintenance.parse_args()
        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--verify-existing-backup-only",
                "--evict-pinned-build-node-cache",
                "--eviction-run-id",
                "12345",
                "--eviction-run-attempt",
                "2",
                "--eviction-source-sha",
                "c" * 40,
                "--eviction-bundle-sha256",
                "d" * 64,
                "--apply",
            ],
        ):
            args = maintenance.parse_args()
            self.assertTrue(args.evict_pinned_build_node_cache)
            self.assertEqual(args.eviction_run_id, "12345")
        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--verify-existing-backup-only",
                "--compact-legacy-fallback-runtime-cache",
                "--compaction-run-id",
                "12345",
                "--compaction-run-attempt",
                "1",
                "--compaction-source-sha",
                "c" * 40,
                "--compaction-bundle-sha256",
                "d" * 64,
                "--resume-legacy-fallback-runtime-cache-compaction",
                "--resume-compaction-run-id",
                "12344",
                "--resume-compaction-run-attempt",
                "2",
                "--resume-compaction-source-sha",
                "c" * 40,
                "--resume-compaction-bundle-sha256",
                "e" * 64,
                "--apply",
            ],
        ):
            args = maintenance.parse_args()
            self.assertTrue(args.compact_legacy_fallback_runtime_cache)
            self.assertTrue(args.resume_legacy_fallback_runtime_cache_compaction)
            self.assertEqual(args.compaction_source_sha, "c" * 40)
            self.assertEqual(args.resume_compaction_run_id, "12344")
        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--verify-existing-backup-only",
                "--compact-legacy-fallback-runtime-cache",
                "--compaction-run-id",
                "12345",
                "--compaction-run-attempt",
                "1",
                "--compaction-source-sha",
                "c" * 40,
                "--compaction-bundle-sha256",
                "d" * 64,
                "--resume-legacy-fallback-runtime-cache-compaction",
                "--resume-compaction-run-id",
                "12344",
                "--resume-compaction-run-attempt",
                "2",
                "--resume-compaction-source-sha",
                "f" * 40,
                "--resume-compaction-bundle-sha256",
                "e" * 64,
                "--apply",
            ],
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

        for argv in (
            [
                "platform_storage_maintenance.py",
                "--purge-profile-access-cache-after-restore",
            ],
            [
                "platform_storage_maintenance.py",
                "--purge-profile-access-cache-after-restore",
                "--apply",
            ],
        ):
            with self.subTest(argv=argv), mock.patch.object(
                maintenance.sys, "argv", argv
            ), self.assertRaises(SystemExit):
                maintenance.parse_args()

        with mock.patch.object(
            maintenance.sys,
            "argv",
            [
                "platform_storage_maintenance.py",
                "--purge-profile-access-cache-after-restore",
                "--apply",
                "--json",
            ],
        ):
            args = maintenance.parse_args()
            self.assertTrue(args.purge_profile_access_cache_after_restore)

    def test_restore_profile_access_cache_purge_requires_stopped_services_and_skips_retention(
        self,
    ) -> None:
        app_dir = self.root / "runtime" / "platform"
        (app_dir / "shared").mkdir(parents=True)
        current = self.add_runtime_release(app_dir, "release-current")
        (app_dir / "current").symlink_to(current)
        args = self.maintenance_args(app_dir)
        args.purge_profile_access_cache_after_restore = True
        args.as_json = True
        args.source_release_dir = self.release_dir
        events: list[str] = []

        venv = app_dir / "shared" / "venv"
        python = venv / "bin" / "python"
        (venv / "bin").mkdir(parents=True)
        python.symlink_to(maintenance.RESTORE_PURGE_SYSTEM_PYTHON)

        shared_env = app_dir / "shared" / ".env.platform"
        shared_env.write_text(
            "PLATFORM_REDIS_URL=redis://127.0.0.1:6379/15\n", encoding="utf-8"
        )
        shared_env.chmod(0o600)
        purge_helper = (
            current
            / "apps"
            / "platform_api"
            / "app"
            / "services"
            / "tournament_profile_access.py"
        )
        purge_helper.parent.mkdir(parents=True)
        purge_helper.write_text("# test-only fixed purge source\n", encoding="utf-8")

        python.unlink()
        python.symlink_to("/usr/bin/true")
        with (
            mock.patch.object(maintenance, "DEFAULT_APP_DIR", app_dir),
            mock.patch.object(maintenance.subprocess, "run") as rejected_child,
            self.assertRaisesRegex(RuntimeError, "Python runtime is unsafe"),
        ):
            maintenance._purge_restored_profile_access_cache(app_dir, current)
        rejected_child.assert_not_called()
        python.unlink()
        python.symlink_to(maintenance.RESTORE_PURGE_SYSTEM_PYTHON)

        inactive = subprocess.CompletedProcess(
            ["systemctl", "is-active", "deadlock-api.service"],
            3,
            "inactive\n",
            "",
        )
        with mock.patch.object(
            maintenance.subprocess,
            "run",
            side_effect=[inactive, inactive],
        ) as systemctl:
            maintenance._require_restore_services_stopped()
        self.assertEqual(systemctl.call_count, 2)

        active = subprocess.CompletedProcess(
            ["systemctl", "is-active", "deadlock-api.service"], 0, "active\n", ""
        )
        with mock.patch.object(maintenance.subprocess, "run", return_value=active):
            with self.assertRaisesRegex(RuntimeError, "stopped API and worker"):
                maintenance._require_restore_services_stopped()

        with mock.patch.object(
            maintenance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "17\n", "private detail"),
        ) as purge_child:
            with mock.patch.object(maintenance, "DEFAULT_APP_DIR", app_dir):
                self.assertEqual(
                    maintenance._purge_restored_profile_access_cache(app_dir, current), 17
                )
        child_command = purge_child.call_args.args[0]
        child_environment = purge_child.call_args.kwargs["env"]
        self.assertEqual(
            child_command[:3], [str(python), "-I", "-B"]
        )
        self.assertEqual(child_command[-1], str(current))
        self.assertIn(
            "purge_all_tournament_profile_access_cache",
            child_command[child_command.index("-c") + 1],
        )
        self.assertIn("sys.path[:0]", child_command[child_command.index("-c") + 1])
        self.assertEqual(purge_child.call_args.kwargs["cwd"], "/")
        self.assertEqual(
            child_environment["PLATFORM_REDIS_URL"], "redis://127.0.0.1:6379/15"
        )
        self.assertEqual(
            set(child_environment),
            {
                "PATH",
                "PYTHONDONTWRITEBYTECODE",
                "PLATFORM_ENVIRONMENT",
                "PLATFORM_SHARED_DIR",
                "PLATFORM_REDIS_URL",
            },
        )

        @maintenance.contextmanager
        def tracked_scope(*_args: object, **_kwargs: object):
            events.append("maintenance-locks-enter")
            yield self.release_dir
            events.append("maintenance-locks-exit")

        @maintenance.contextmanager
        def tracked_live_qa_lock():
            events.append("live-qa-lock-enter")
            yield
            events.append("live-qa-lock-exit")

        def stopped_services() -> None:
            events.append("services-stopped-check")

        def purge_cache(_app_dir: Path, _current: Path) -> int:
            events.append("purge")
            return 17

        with (
            mock.patch.object(maintenance, "DEFAULT_APP_DIR", app_dir),
            mock.patch.object(maintenance, "DEFAULT_SOURCE_RELEASE_DIR", self.release_dir),
            mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
            mock.patch.object(maintenance, "live_qa_machine_lock", tracked_live_qa_lock),
            mock.patch.object(
                maintenance,
                "_require_restore_services_stopped",
                side_effect=stopped_services,
            ),
            mock.patch.object(
                maintenance,
                "_purge_restored_profile_access_cache",
                side_effect=purge_cache,
            ) as purge,
            mock.patch.object(maintenance, "run_backup") as run_backup,
            mock.patch.object(maintenance, "verify_existing_backup") as verify_backup,
            mock.patch.object(maintenance, "_plan_and_maybe_apply") as retention,
        ):
            report = run_maintenance(args)

        self.assertEqual(
            report,
            {
                "ok": True,
                "status": "completed",
                "mode": "restore-profile-access-cache-purge",
                "purged_key_count": 17,
                "services_stopped": True,
            },
        )

        # Exercise the actual isolated restore child against the guarded local
        # Redis DB15 fixture.  The production entrypoint is fixed to the
        # canonical app root, so this test binds that root only inside its
        # disposable fixture and preserves the same argv/runtime checks.
        from redis.asyncio import from_url

        from tools.platform_test_runner import validate_test_resource_configuration

        resources = validate_test_resource_configuration()
        self.assertEqual(resources.database_name, "platformdb_test")
        self.assertEqual(resources.database_schema, "platform")
        self.assertEqual(resources.redis_database, "15")
        self.assertEqual(resources.redis_host, "127.0.0.1")

        test_prefixes = (
            "platform:tournament:profile-access:v1",
            "platform:tournament:profile-viewers:v1",
            "platform:tournament:profile-roster:v1",
            "platform:tournament:profile-access:v2",
            "platform:tournament:profile-viewers:v2",
            "platform:tournament:profile-roster:v2",
        )
        nonce = uuid.uuid4().hex
        cache_keys = tuple(f"{prefix}:restore-proof-{nonce}" for prefix in test_prefixes)
        unrelated_key = f"platform:restore-proof-unrelated:{nonce}"
        runtime_venv = app_dir / "shared" / "venv"
        python.unlink()
        python.symlink_to(maintenance.RESTORE_PURGE_SYSTEM_PYTHON)

        runtime_site = Path(sysconfig.get_path("purelib")).resolve(strict=True)
        try:
            runtime_site.relative_to(Path(sys.prefix).resolve(strict=True))
        except ValueError as exc:
            raise AssertionError("test child dependencies are outside the pinned venv") from exc
        self.assertEqual(sys.version_info[:2], (3, 12))
        self.assertTrue((runtime_site / "redis").is_dir())
        self.assertTrue((runtime_site / "sqlalchemy").is_dir())
        self.assertTrue((runtime_site / "pydantic_settings").is_dir())
        pyvenv_cfg = runtime_venv / "pyvenv.cfg"
        pyvenv_cfg.write_text(
            "home = /usr/bin\n"
            "include-system-site-packages = false\n"
            f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
            encoding="ascii",
        )
        pyvenv_cfg.chmod(0o600)
        venv_site = (
            runtime_venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        venv_site.parent.mkdir(parents=True)
        venv_site.symlink_to(runtime_site, target_is_directory=True)

        shutil.rmtree(current / "apps")
        api_root = current / "apps" / "platform_api"
        api_root.mkdir(parents=True)
        platform_root = REPO_ROOT / "platform"
        shutil.copytree(
            platform_root / "apps" / "platform_api" / "app", api_root / "app"
        )
        shutil.copytree(platform_root / "python_packages", current / "python_packages")
        # The source tree is copied into the disposable release so its settings
        # loader cannot read the developer checkout's ignored environment file.
        self.assertFalse((current / ".env.platform").exists())

        redis_client = from_url(resources.redis_url, decode_responses=False)
        real_run = maintenance.subprocess.run

        def systemctl_or_child(
            command: list[str], *run_args: object, **run_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["systemctl", "is-active"]:
                self.assertIn(command[2], maintenance.RESTORE_PURGE_SERVICES)
                events.append("services-stopped-check")
                return subprocess.CompletedProcess(command, 3, "inactive\n", "")
            events.append("purge-child")
            return real_run(command, *run_args, **run_kwargs)

        async def exercise_real_child() -> None:
            try:
                self.assertTrue(await redis_client.ping())
                self.assertEqual(await redis_client.exists(*cache_keys, unrelated_key), 0)
                await redis_client.mset({key: b"test" for key in cache_keys})
                await redis_client.set(unrelated_key, b"preserve")

                event_offset = len(events)
                with (
                    mock.patch.object(maintenance, "DEFAULT_APP_DIR", app_dir),
                    mock.patch.object(
                        maintenance, "DEFAULT_SOURCE_RELEASE_DIR", self.release_dir
                    ),
                    mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
                    mock.patch.object(maintenance, "live_qa_machine_lock", tracked_live_qa_lock),
                    mock.patch.object(
                        maintenance.subprocess, "run", side_effect=systemctl_or_child
                    ),
                ):
                    real_report = run_maintenance(args)

                self.assertEqual(real_report["mode"], "restore-profile-access-cache-purge")
                self.assertGreaterEqual(real_report["purged_key_count"], len(cache_keys))
                self.assertEqual(await redis_client.exists(*cache_keys), 0)
                self.assertEqual(await redis_client.get(unrelated_key), b"preserve")
                child_events = events[event_offset:]
                self.assertEqual(
                    child_events,
                    [
                        "maintenance-locks-enter",
                        "live-qa-lock-enter",
                        "services-stopped-check",
                        "services-stopped-check",
                        "purge-child",
                        "live-qa-lock-exit",
                        "maintenance-locks-exit",
                    ],
                )
            finally:
                await redis_client.delete(*cache_keys, unrelated_key)
                await redis_client.aclose()

        asyncio.run(exercise_real_child())
        purge.assert_called_once_with(app_dir, current)
        self.assertEqual(
            events[:6],
            [
                "maintenance-locks-enter",
                "live-qa-lock-enter",
                "services-stopped-check",
                "purge",
                "live-qa-lock-exit",
                "maintenance-locks-exit",
            ],
        )
        run_backup.assert_not_called()
        verify_backup.assert_not_called()
        retention.assert_not_called()

    def test_backup_command_verifies_freshness_before_returning(self) -> None:
        create_result = {
            "ok": True,
            "dump_file": "platformdb-20261012T120000Z.dump",
            "format_version": 2,
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
            "postexisting_archive_count": 10,
            "preexisting_archive_inventory_sha256": "a" * 64,
            "postexisting_archive_inventory_sha256": "a" * 64,
            "preexisting_archives_preserved": True,
        }
        check_result = {
            "ok": True,
            "restore_verified": True,
            "age_hours": 0.25,
        }
        with (
            mock.patch.object(
                maintenance.backup_restore_drill,
                "create_backup",
                side_effect=(create_result, preserve_result),
            ) as create_backup,
            mock.patch.object(
                maintenance.backup_restore_drill,
                "check_latest_backup",
                side_effect=(check_result, check_result),
            ) as check_latest,
            mock.patch.object(
                maintenance.subprocess,
                "run",
                side_effect=AssertionError("run_backup must not start a helper subprocess"),
            ),
        ):
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

        self.assertEqual(create_backup.call_count, 2)
        self.assertEqual(check_latest.call_count, 2)
        create_args = create_backup.call_args_list[0].args[0]
        self.assertEqual(
            create_args.env_file,
            self.root / "runtime" / "platform" / "shared" / ".env.platform",
        )
        self.assertEqual(
            create_args.output_dir,
            self.root / "runtime" / "platform" / "shared" / "backups",
        )
        self.assertEqual(create_args.keep, 14)
        self.assertIs(create_args.dump_only, False)
        self.assertIs(create_args.rotate_existing, True)
        self.assertIs(create_args.private_scratch_diagnostics, False)
        self.assertEqual(check_latest.call_args_list[0].args[0], create_args.output_dir)
        self.assertEqual(check_latest.call_args_list[0].kwargs, {"max_age_hours": 24.0})
        preserve_args = create_backup.call_args_list[1].args[0]
        self.assertIs(preserve_args.rotate_existing, False)
        self.assertTrue(result["restore_verified"])
        self.assertTrue(result["alembic_revision_verified"])
        self.assertTrue(result["checksum_present"])
        self.assertEqual(result["age_hours"], 0.25)
        self.assertEqual(result["rotation_mode"], "rotate-existing")
        self.assertTrue(preserved_result["preexisting_archives_preserved"])
        self.assertEqual(preserved_result["preexisting_archive_count"], 9)
        self.assertEqual(preserved_result["postexisting_archive_count"], 10)

        with (
            mock.patch.object(
                maintenance.backup_restore_drill,
                "create_backup",
                return_value=create_result,
            ) as private_create,
            mock.patch.object(
                maintenance.backup_restore_drill,
                "check_latest_backup",
                return_value=check_result,
            ),
            mock.patch.object(
                maintenance.subprocess,
                "run",
                side_effect=AssertionError("run_backup must not start a helper subprocess"),
            ),
        ):
            maintenance.run_backup(
                self.root / "runtime" / "platform",
                keep=14,
                private_failure_diagnostics=True,
            )
        private_args = private_create.call_args.args[0]
        self.assertIs(private_args.private_scratch_diagnostics, True)
        self.assertIs(private_args.rotate_existing, True)

        admission_diagnostic = {
            "schema": 1,
            "phase": "pre_create_admission",
            "guard_reason": "memory_reserve",
            "available_bytes": 988_966_912,
            "required_bytes": 1_073_741_824,
            "helper_rss_bytes": 27_893_760,
            "direct_parent_rss_bytes": 41_349_120,
            "observed_pair_rss_bytes": 69_242_880,
            "observed_process_count": 2,
        }
        host_guard_diagnostic = {
            "schema": 2,
            "guard_site": "worker_pg_restore_resource_guard",
            "guard_reason": "memory_reserve",
            "available_bytes": 1_060_000_000,
            "required_bytes": 1_073_741_824,
        }
        create_failure = RuntimeError("scratch admission failed")
        create_failure.scratch_admission_diagnostic = admission_diagnostic
        create_failure.scratch_host_guard_diagnostic = host_guard_diagnostic
        create_failure.scratch_unit_private_output = (b"worker stdout", b"worker stderr")
        not_checked_after_failure = mock.Mock()
        public_stderr = io.StringIO()
        signal_scope = mock.MagicMock()
        with (
            mock.patch.object(
                maintenance.backup_restore_drill,
                "create_backup",
                side_effect=create_failure,
            ),
            mock.patch.object(
                maintenance.backup_restore_drill,
                "check_latest_backup",
                not_checked_after_failure,
            ),
            mock.patch.object(
                maintenance.backup_restore_drill,
                "_termination_signal_scope",
                return_value=signal_scope,
            ) as termination_scope,
            mock.patch.object(maintenance.sys, "stderr", public_stderr),
        ):
            with self.assertRaises(maintenance.BackupCommandFailure) as public_error:
                maintenance.run_backup(self.root / "runtime" / "platform", keep=14)
        termination_scope.assert_called_once_with()
        signal_scope.__enter__.assert_called_once_with()
        signal_scope.__exit__.assert_called_once()
        not_checked_after_failure.assert_not_called()
        self.assertIsNone(public_error.exception.scratch_admission_diagnostic)
        self.assertIsNone(public_error.exception.scratch_host_guard_diagnostic)
        self.assertEqual(public_stderr.getvalue(), "")

        private_stderr = io.StringIO()
        with (
            mock.patch.object(
                maintenance.backup_restore_drill,
                "create_backup",
                side_effect=create_failure,
            ),
            mock.patch.object(
                maintenance.backup_restore_drill,
                "check_latest_backup",
                not_checked_after_failure,
            ),
            mock.patch.object(maintenance.sys, "stderr", private_stderr),
        ):
            with self.assertRaises(maintenance.BackupCommandFailure) as private_error:
                maintenance.run_backup(
                    self.root / "runtime" / "platform",
                    keep=14,
                    private_failure_diagnostics=True,
                )
        not_checked_after_failure.assert_not_called()
        self.assertEqual(
            private_error.exception.scratch_admission_diagnostic, admission_diagnostic
        )
        self.assertEqual(
            private_error.exception.scratch_host_guard_diagnostic,
            host_guard_diagnostic,
        )
        self.assertIn("SCRATCH_UNIT_PRIVATE_OUTPUT_V1", private_stderr.getvalue())
        self.assertIn(
            maintenance.backup_restore_drill.SCRATCH_HOST_GUARD_FRAME_MARKER.decode("ascii"),
            private_stderr.getvalue(),
        )
        self.assertIn("scratch admission failed", private_stderr.getvalue())
        self.assertNotIn("worker stderr", private_stderr.getvalue())

        backup_script = str(REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py")
        with mock.patch.object(
            maintenance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), ""),
        ) as verify_run:
            maintenance.verify_existing_backup(
                self.root / "runtime" / "platform", max_age_hours=24.0
            )
        verify_command = verify_run.call_args.args[0]
        self.assertEqual(verify_command[:3], [sys.executable, "-B", backup_script])
        self.assertIn("--verify-latest-existing", verify_command)

        with mock.patch.object(
            maintenance.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, json.dumps({"ok": True}), ""),
        ) as private_verify_run:
            maintenance.verify_existing_backup(
                self.root / "runtime" / "platform",
                max_age_hours=24.0,
                private_failure_diagnostics=True,
            )
        self.assertIn(
            "--private-scratch-diagnostics", private_verify_run.call_args.args[0]
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            probe = Path(temporary_dir)
            (probe / "probe_helper.py").write_text("VALUE = 1\n", encoding="ascii")
            (probe / "probe.py").write_text(
                "import probe_helper\nassert probe_helper.VALUE == 1\n",
                encoding="ascii",
            )
            no_bytecode = subprocess.run(
                [sys.executable, "-B", str(probe / "probe.py")],
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(no_bytecode.returncode, 0, no_bytecode.stderr.decode("utf-8", "replace"))
            self.assertFalse((probe / "__pycache__").exists())

        diagnostic = {
            "schema": 1,
            "restore_stage": "restore_platform",
            "guard_reason": "disk_floor",
            "free_bytes": 10,
            "required_free_bytes": 20,
            "temporary_database_created": True,
            "drop_outcome": "confirmed_absent",
            "temporary_database_absent": True,
            "archive_sha256": "a" * 64,
            "archive_size_bytes": 120,
        }
        scratch_diagnostic = {
            "schema": 1,
            "phase": "unit_exited_before_confinement",
            "active_state": "failed",
            "sub_state": "failed",
            "result": "exit-code",
            "exec_main_status": 203,
            "launcher_exit_status": 1,
            "stdout_bytes": 0,
            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
            "stdout_truncated": False,
            "stderr_bytes": 0,
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            "stderr_truncated": False,
            "verification_error": "none",
            "verification_errno": None,
        }
        failure = subprocess.CompletedProcess(
            [],
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": "private failure detail",
                    "restore_diagnostic": diagnostic,
                    "backup_disk_diagnostic": {
                        "schema": 1,
                        "phase": "pg_dump_monitor",
                        "guard_reason": "disk_floor",
                        "free_bytes": 100,
                        "required_free_bytes": 200,
                    },
                    "scratch_unit_diagnostic": scratch_diagnostic,
                    "scratch_admission_diagnostic": {
                        "schema": 1,
                        "phase": "pre_create_admission",
                        "guard_reason": "memory_reserve",
                        "available_bytes": 1_060_000_000,
                        "required_bytes": 1_073_741_824,
                        "helper_rss_bytes": 18_000_000,
                        "direct_parent_rss_bytes": 9_000_000,
                        "observed_pair_rss_bytes": 27_000_000,
                        "observed_process_count": 2,
                    },
                    "scratch_host_guard_diagnostic": {
                        "schema": 2,
                        "guard_site": "worker_pg_restore_resource_guard",
                        "guard_reason": "memory_reserve",
                        "available_bytes": 1_060_000_000,
                        "required_bytes": 1_073_741_824,
                    },
                }
            ),
            "SCRATCH_UNIT_PRIVATE_OUTPUT_V1\nprivate framed bytes\n",
        )
        private_stderr = io.StringIO()
        with (
            mock.patch.object(maintenance.subprocess, "run", return_value=failure),
            mock.patch.object(maintenance.sys, "stderr", private_stderr),
        ):
            with self.assertRaisesRegex(RuntimeError, "Platform backup failed") as failure_error:
                maintenance._run_backup_command(
                    ["fixed-backup-child"], forward_failure_diagnostics=True
                )
        self.assertEqual(failure_error.exception.restore_diagnostic, diagnostic)
        self.assertEqual(
            failure_error.exception.backup_disk_diagnostic,
            {
                "schema": 1,
                "phase": "pg_dump_monitor",
                "guard_reason": "disk_floor",
                "free_bytes": 100,
                "required_free_bytes": 200,
            },
        )
        self.assertEqual(failure_error.exception.scratch_unit_diagnostic, scratch_diagnostic)
        admission_diagnostic = {
            "schema": 1,
            "phase": "pre_create_admission",
            "guard_reason": "memory_reserve",
            "available_bytes": 1_060_000_000,
            "required_bytes": 1_073_741_824,
            "helper_rss_bytes": 18_000_000,
            "direct_parent_rss_bytes": 9_000_000,
            "observed_pair_rss_bytes": 27_000_000,
            "observed_process_count": 2,
        }
        self.assertEqual(
            failure_error.exception.scratch_admission_diagnostic,
            admission_diagnostic,
        )
        host_guard_diagnostic = {
            "schema": 2,
            "guard_site": "worker_pg_restore_resource_guard",
            "guard_reason": "memory_reserve",
            "available_bytes": 1_060_000_000,
            "required_bytes": 1_073_741_824,
        }
        self.assertEqual(
            failure_error.exception.scratch_host_guard_diagnostic,
            host_guard_diagnostic,
        )
        after_confinement_diagnostic = {
            **scratch_diagnostic,
            "phase": "unit_exited_after_confinement",
            "active_state": "inactive",
            "sub_state": "dead",
            "exec_main_status": 1,
        }
        after_confinement_failure = subprocess.CompletedProcess(
            [],
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": "private failure detail",
                    "scratch_unit_diagnostic": after_confinement_diagnostic,
                }
            ),
            "SCRATCH_UNIT_PRIVATE_OUTPUT_V1\nprivate framed bytes\n",
        )
        with (
            mock.patch.object(
                maintenance.subprocess, "run", return_value=after_confinement_failure
            ),
            mock.patch.object(maintenance.sys, "stderr", io.StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "Platform backup failed") as after_error:
                maintenance._run_backup_command(
                    ["fixed-backup-child"], forward_failure_diagnostics=True
                )
        self.assertEqual(
            after_error.exception.scratch_unit_diagnostic,
            after_confinement_diagnostic,
        )
        malformed_failure = subprocess.CompletedProcess(
            [],
            1,
            json.dumps(
                {
                    "ok": False,
                    "backup_disk_diagnostic": {
                        "schema": 1,
                        "phase": "pg_dump_monitor",
                        "guard_reason": "disk_floor",
                        "free_bytes": 100,
                        "required_free_bytes": 200,
                        "private_path": "/must-not-cross-the-boundary",
                    },
                    "scratch_unit_diagnostic": {
                        **scratch_diagnostic,
                        "private_path": "/must-not-cross-the-boundary",
                    },
                    "scratch_host_guard_diagnostic": {
                        "schema": 2,
                        "guard_site": "worker_pg_restore_resource_guard",
                        "guard_reason": "memory_reserve",
                        "available_bytes": 100,
                        "required_bytes": 1_073_741_824,
                        "private_path": "/must-not-cross-the-boundary",
                    },
                }
            ),
            "",
        )
        with (
            mock.patch.object(
                maintenance.subprocess, "run", return_value=malformed_failure
            ),
            self.assertRaisesRegex(RuntimeError, "Platform backup failed") as malformed_error,
        ):
            maintenance._run_backup_command(["fixed-backup-child"])
        self.assertIsNone(malformed_error.exception.backup_disk_diagnostic)
        self.assertIsNone(malformed_error.exception.scratch_unit_diagnostic)
        self.assertIsNone(malformed_error.exception.scratch_admission_diagnostic)
        self.assertIsNone(malformed_error.exception.scratch_host_guard_diagnostic)
        self.assertIn("private failure detail", private_stderr.getvalue())
        self.assertIn("SCRATCH_UNIT_PRIVATE_OUTPUT_V1", private_stderr.getvalue())
        public_stderr = io.StringIO()
        with (
            mock.patch.object(maintenance.subprocess, "run", return_value=failure),
            mock.patch.object(maintenance.sys, "stderr", public_stderr),
        ):
            with self.assertRaisesRegex(RuntimeError, "Platform backup failed") as public_error:
                maintenance._run_backup_command(["fixed-backup-child"])
        self.assertIsNone(public_error.exception.scratch_unit_diagnostic)
        self.assertIsNone(public_error.exception.scratch_admission_diagnostic)
        self.assertIsNone(public_error.exception.scratch_host_guard_diagnostic)
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
                    "postexisting_archive_count": 10,
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

        args.backup_only = False
        args.verify_existing_backup_only = True
        result = {
            "ok": True,
            "status": "verified-existing",
            "verified_existing": True,
            "created": False,
            "rotation_mode": "preserve-existing",
            "removed_count": 0,
            "metadata_file": "platformdb-latest.json",
            "dump_file": "platformdb-latest.dump",
            "age_hours": 20.8,
            "restore_verified": True,
            "alembic_revision_verified": True,
            "restored_table_count": 321,
            "sha256": "a" * 64,
            "restore_verified_at_utc": "2026-10-10T12:00:00Z",
        }
        events.clear()
        with (
            mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
            mock.patch.object(maintenance, "_plan_and_maybe_apply") as retention_plan,
            mock.patch.object(
                maintenance, "verify_existing_backup", return_value=result
            ) as verify_backup,
            mock.patch.object(
                maintenance.live_qa_guard,
                "prune_runtime_cache_release_lock_held",
            ) as live_qa_prune,
        ):
            report = run_maintenance(args)

        self.assertEqual(report["mode"], "verify-existing-backup-only")
        self.assertTrue(report["ok"])
        self.assertTrue(report["backup"]["verified_existing"])
        self.assertFalse(report["backup"]["created"])
        self.assertEqual(report["backup"]["rotation_mode"], "preserve-existing")
        self.assertEqual(report["backup"]["removed_count"], 0)
        self.assertEqual(events, ["locks-enter", "locks-exit"])
        verify_backup.assert_called_once_with(
            app_dir,
            max_age_hours=24.0,
            private_failure_diagnostics=False,
        )
        live_qa_prune.assert_not_called()
        retention_plan.assert_not_called()
        self.assertTrue(candidate.exists())
        self.assertEqual(report["production_releases"]["deleted_count"], 0)
        self.assertEqual(report["source_release_artifacts"]["deleted_count"], 0)
        self.assertEqual(report["live_qa_runtime_caches"]["deleted_count"], 0)

        args.evict_pinned_build_node_cache = True
        args.eviction_run_id = "12345"
        args.eviction_run_attempt = "2"
        args.eviction_source_sha = "c" * 40
        args.eviction_bundle_sha256 = "d" * 64
        events.clear()

        @maintenance.contextmanager
        def tracked_live_qa_lock():
            events.append("live-qa-enter")
            try:
                yield
            finally:
                events.append("live-qa-exit")

        cache_identity = {
            "schema": 1,
            "event": "build_node_cache_eviction",
            "node_version": "26.3.1",
            "cache_dev": 27,
            "cache_ino": 123456,
            "manifest_tree_sha256": "b" * 64,
            "total_bytes": 234_000_000,
        }

        def evict_cache(*, write_intent, write_completion) -> dict[str, object]:
            events.append("cache-evict")
            write_intent({**cache_identity, "status": "intent"})
            write_completion(
                {
                    **cache_identity,
                    "status": "removed",
                    "reclaimed_bytes": 234_000_000,
                    "regeneration": "pinned_archive_required",
                }
            )
            return {
                "status": "removed",
                "node_version": "26.3.1",
                "manifest_tree_sha256": "b" * 64,
                "reclaimed_bytes": 234_000_000,
                "regeneration": "pinned_archive_required",
            }

        receipt_writer = maintenance.write_build_node_cache_receipt

        def write_receipt(app_path: Path, *, phase: str, **kwargs: object) -> str:
            events.append(f"receipt-{phase}")
            return receipt_writer(app_path, phase=phase, **kwargs)

        def verify_latest(*_args: object, **_kwargs: object) -> dict[str, object]:
            events.append("verify-latest")
            return result

        with (
            mock.patch.object(maintenance, "DEFAULT_APP_DIR", app_dir),
            mock.patch.object(
                maintenance, "DEFAULT_SOURCE_RELEASE_DIR", self.release_dir
            ),
            mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
            mock.patch.object(maintenance, "live_qa_machine_lock", tracked_live_qa_lock),
            mock.patch.object(
                maintenance.platform_build_node_cache,
                "evict_pinned_build_node_cache",
                side_effect=evict_cache,
            ) as evict,
            mock.patch.object(
                maintenance,
                "write_build_node_cache_receipt",
                side_effect=write_receipt,
            ) as write_receipt_mock,
            mock.patch.object(
                maintenance, "verify_existing_backup", side_effect=verify_latest
            ) as verify_backup,
            mock.patch.object(maintenance, "_plan_and_maybe_apply") as retention_plan,
            mock.patch.object(
                maintenance.live_qa_guard,
                "prune_runtime_cache_release_lock_held",
            ) as live_qa_prune,
        ):
            report = run_maintenance(args)

        self.assertEqual(
            events,
            [
                "locks-enter",
                "live-qa-enter",
                "cache-evict",
                "receipt-intent",
                "receipt-completion",
                "verify-latest",
                "live-qa-exit",
                "locks-exit",
            ],
        )
        evict.assert_called_once()
        self.assertEqual(write_receipt_mock.call_count, 2)
        verify_backup.assert_called_once_with(
            app_dir,
            max_age_hours=24.0,
            private_failure_diagnostics=False,
        )
        self.assertEqual(report["backup"]["build_node_cache"]["status"], "removed")
        self.assertEqual(
            report["backup"]["build_node_cache"]["reclaimed_bytes"], 234_000_000
        )
        for phase in ("intent", "completion"):
            receipt = app_dir / "shared" / (
                f"build-node-cache-eviction-12345-2.{phase}.json"
            )
            receipt_stat = receipt.lstat()
            self.assertEqual(stat.S_IMODE(receipt_stat.st_mode), 0o600)
            self.assertEqual((receipt_stat.st_uid, receipt_stat.st_gid), (0, 0))
            self.assertEqual(receipt_stat.st_nlink, 1)
            payload = json.loads(receipt.read_text(encoding="ascii"))
            self.assertEqual(payload["source_sha"], "c" * 40)
            self.assertEqual(payload["bundle_sha256"], "d" * 64)
            self.assertEqual(payload["phase"], phase)
        live_qa_prune.assert_not_called()
        retention_plan.assert_not_called()

        args.evict_pinned_build_node_cache = False
        args.compact_legacy_fallback_runtime_cache = True
        args.compaction_run_id = "123456"
        args.compaction_run_attempt = "1"
        args.compaction_source_sha = "f" * 40
        args.compaction_bundle_sha256 = "a" * 64
        events.clear()
        compacted = {
            "status": "completed",
            "result": "compacted",
            "reclaimed_bytes": 397_402_112,
            "source_commit": "4a04b2dffaf0d02c2d3910e7ba28dca9b89de209",
            "old_tree_sha256": "b" * 64,
            "new_tree_sha256": "c" * 64,
            "intent_receipt": "liveqa-fallback-cache-compaction-123456-1.intent.json",
            "validated_receipt": "liveqa-fallback-cache-compaction-123456-1.validated.json",
            "completion_receipt": "liveqa-fallback-cache-compaction-123456-1.completion.json",
        }

        def compact_cache(app_path: Path, **kwargs: object) -> dict[str, object]:
            events.append("cache-compact")
            self.assertEqual(app_path, app_dir)
            self.assertEqual(kwargs["run_id"], "123456")
            self.assertEqual(kwargs["run_attempt"], "1")
            self.assertEqual(kwargs["source_sha"], "f" * 40)
            self.assertEqual(kwargs["bundle_sha256"], "a" * 64)
            self.assertIsNone(kwargs["resume_run_id"])
            return compacted

        with (
            mock.patch.object(maintenance, "DEFAULT_APP_DIR", app_dir),
            mock.patch.object(
                maintenance, "DEFAULT_SOURCE_RELEASE_DIR", self.release_dir
            ),
            mock.patch.object(maintenance, "maintenance_lock_scope", tracked_scope),
            mock.patch.object(maintenance, "live_qa_machine_lock", tracked_live_qa_lock),
            mock.patch.object(
                maintenance,
                "compact_legacy_fallback_runtime_cache",
                side_effect=compact_cache,
            ) as compact,
            mock.patch.object(
                maintenance, "verify_existing_backup", side_effect=verify_latest
            ) as verify_backup,
            mock.patch.object(maintenance, "_plan_and_maybe_apply") as retention_plan,
            mock.patch.object(
                maintenance.live_qa_guard,
                "prune_runtime_cache_release_lock_held",
            ) as live_qa_prune,
        ):
            report = run_maintenance(args)

        self.assertEqual(
            events,
            [
                "locks-enter",
                "live-qa-enter",
                "cache-compact",
                "verify-latest",
                "live-qa-exit",
                "locks-exit",
            ],
        )
        compact.assert_called_once()
        verify_backup.assert_called_once_with(
            app_dir,
            max_age_hours=24.0,
            private_failure_diagnostics=False,
        )
        self.assertEqual(
            report["backup"]["fallback_runtime_cache_compaction"], compacted
        )
        live_qa_prune.assert_not_called()
        retention_plan.assert_not_called()

        if os.geteuid() == 0:
            self.assertEqual(
                maintenance.LEGACY_FALLBACK_RUNTIME_COMMIT,
                maintenance.live_qa_guard.LEGACY_FALLBACK_RUNTIME_COMMIT,
            )
            receipt_app = self.root / "compaction-receipt-runtime"
            receipt_shared = receipt_app / "shared"
            receipt_shared.mkdir(parents=True)
            original_manifest = {
                "version": 1,
                "source_commit": "4a04b2dffaf0d02c2d3910e7ba28dca9b89de209",
                "tree_sha256": "1" * 64,
                "node_archive_sha256": "2" * 64,
                "package_lock_sha256": "3" * 64,
                "playwright_browsers_sha256": "4" * 64,
            }
            original_manifest_raw = maintenance.live_qa_guard._encode_cache_manifest(
                original_manifest
            )
            intent_record = {
                "schema": 1,
                "event": maintenance.FALLBACK_CACHE_COMPACTION_EVENT,
                "run_id": 123456,
                "attempt": 1,
                "operation_source_sha": "f" * 40,
                "source_commit": "4a04b2dffaf0d02c2d3910e7ba28dca9b89de209",
                "cache_dev": 27,
                "cache_ino": 1234,
                "old_tree_sha256": "1" * 64,
                "old_non_chromium_tree_sha256": "2" * 64,
                "old_manifest_sha256": hashlib.sha256(original_manifest_raw).hexdigest(),
                "old_manifest": original_manifest,
                "old_manifest_raw_b64": maintenance.base64.b64encode(
                    original_manifest_raw
                ).decode("ascii"),
                "old_chromium_tree_sha256": "4" * 64,
                "old_chromium_entry_count": 1,
                "old_chromium_inventory_sha256": "5" * 64,
                "old_chromium_inventory": [{"path": "root"}],
                "old_chromium_dev": 27,
                "old_chromium_ino": 5678,
                "old_chromium_allocated_bytes": 397_402_112,
                "old_sandbox_sha256": maintenance.live_qa_guard.CHROMIUM_SANDBOX_SHA256,
                "rollback_name": ".runtime-4a04b2dffaf0d02c2d3910e7ba28dca9b89de209.chromium-compaction-0123456789abcdef0123456789abcdef",
                "phase": "intent",
            }
            receipt_name = maintenance.write_runtime_cache_compaction_receipt(
                receipt_app,
                phase="intent",
                record=intent_record,
                run_id="123456",
                run_attempt="1",
                source_sha="f" * 40,
                bundle_sha256="a" * 64,
            )
            receipt_stat = (receipt_shared / receipt_name).lstat()
            self.assertEqual(stat.S_IMODE(receipt_stat.st_mode), 0o600)
            self.assertEqual((receipt_stat.st_uid, receipt_stat.st_gid), (0, 0))
            self.assertEqual(receipt_stat.st_nlink, 1)
            self.assertEqual(
                maintenance.read_runtime_cache_compaction_receipt(
                    receipt_app,
                    phase="intent",
                    run_id="123456",
                    run_attempt="1",
                    source_sha="f" * 40,
                    bundle_sha256="a" * 64,
                ),
                intent_record,
            )
            round_tripped = maintenance.read_runtime_cache_compaction_receipt(
                receipt_app,
                phase="intent",
                run_id="123456",
                run_attempt="1",
                source_sha="f" * 40,
                bundle_sha256="a" * 64,
            )
            self.assertEqual(
                maintenance.base64.b64decode(round_tripped["old_manifest_raw_b64"]),
                original_manifest_raw,
            )
            with self.assertRaises(RuntimeError):
                maintenance.read_runtime_cache_compaction_receipt(
                    receipt_app,
                    phase="intent",
                    run_id="123456",
                    run_attempt="1",
                    source_sha="f" * 40,
                    bundle_sha256="b" * 64,
                )

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
                "platform_storage_maintenance.py --apply "
                "--preserve-existing-backups --backup-keep 14 "
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
        self.assertIn("MemoryMax=512M", service)
        self.assertIn("TimeoutStartSec=30min", service)
        self.assertIn("--minimum-free-gib 5 --maximum-used-percent 85", service)
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
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("platform_storage_maintenance.py", workflow)
        workflow_document = yaml.safe_load(workflow)
        inputs = workflow_document.get(True, workflow_document).get("workflow_dispatch", {}).get("inputs", {})
        self.assertEqual(
            set(inputs["operation"]["options"]),
            {"create", "verify-existing", "authorization-probe"},
        )
        self.assertTrue(inputs["security_run_id"]["required"])
        self.assertTrue(inputs["security_run_attempt"]["required"])
        self.assertTrue(inputs["source_sha"]["required"])
        self.assertIn("for poll in $(seq 1 360)", workflow)
        self.assertIn("time.monotonic_ns() + 1_800_000_000_000", workflow)
        self.assertIn('--max-time "$request_timeout"', workflow)
        self.assertIn('sleep "$sleep_seconds"', workflow)
        self.assertIn("time.monotonic_ns() + 6_900_000_000_000", workflow)
        self.assertIn("remaining_ns < 3_600_000_000_000", workflow)
        self.assertIn("timeout --signal=TERM --kill-after=15s 600s gh attestation verify", workflow)
        timeout_seconds = workflow_document["jobs"]["backup"]["timeout-minutes"] * 60
        total_job_budget_seconds = 120 * 60
        poll_seconds = 30 * 60
        backup_operation_budget_seconds = 45 * 60
        preflight_and_cleanup_budget_seconds = 15 * 60
        operation_reserve_seconds = 60 * 60
        self.assertEqual(timeout_seconds, total_job_budget_seconds)
        self.assertGreaterEqual(
            total_job_budget_seconds,
            poll_seconds + preflight_and_cleanup_budget_seconds + operation_reserve_seconds,
        )
        self.assertLessEqual(backup_operation_budget_seconds, operation_reserve_seconds)
        self.assertIn('run.get("head_sha") != expected_sha', workflow)
        self.assertIn('ref.get("ref") != "refs/heads/dev"', workflow)
        self.assertIn('ref["object"].get("sha") != sys.argv[2]', workflow)
        operation_run = self._backup_operation_run(workflow_document)
        remote_runner = operation_run
        source_binding = remote_runner.index('[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]')
        operation_branch = remote_runner.index(
            'if [[ "$BACKUP_OPERATION" = verify-existing ]]; then'
        )
        shared_bundle_stage = remote_runner.index(
            'bundle_stage="/tmp/oldsparky-backup-verifier-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"'
        )
        remote_arguments = remote_runner.index(
            'remote_arguments="$GITHUB_RUN_ID $GITHUB_RUN_ATTEMPT $REMOTE_OPERATION $SOURCE_SHA $BUNDLE_SHA256'
        )
        self.assertLess(source_binding, operation_branch)
        self.assertLess(operation_branch, shared_bundle_stage)
        self.assertLess(operation_branch, remote_arguments)
        self.assertNotIn('bundle_stage=none', remote_runner)
        self.assertNotIn('create none none false false false', remote_runner)
        remote_script = remote_runner.rsplit("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        remote_operation_allowlists = [
            line.strip()
            for line in remote_script.splitlines()
            if line.strip().startswith('[[ "$operation" = ')
            and '|| "$operation" =' in line
            and line.strip().endswith(" ]]")
        ]
        parsed_remote_operation_sets = []
        for allowlist in remote_operation_allowlists:
            choices = re.findall(r'"\$operation" = ([a-z-]+)', allowlist)
            if choices:
                self.assertEqual(
                    allowlist,
                    "[[ " + " || ".join(f'"$operation" = {choice}' for choice in choices) + " ]]",
                )
                parsed_remote_operation_sets.append(choices)
        self.assertEqual(
            parsed_remote_operation_sets,
            [["create", "verify-existing", "authorization-probe"]],
        )
        helper_validation_marker = "failure_stage=helper_bundle_validation\n"
        self.assertEqual(remote_script.count(helper_validation_marker), 1)
        helper_validation_index = remote_script.index(helper_validation_marker)
        helper_validation_gate_start = remote_script.rfind("if [[ ", 0, helper_validation_index)
        helper_validation_gate = remote_script[helper_validation_gate_start:helper_validation_index]
        self.assertEqual(
            helper_validation_gate,
            'if [[ "$operation" = verify-existing || "$operation" = create '
            '|| "$operation" = authorization-probe ]]; then\n  ',
        )
        failure_cleanup_gate = (
            'if [[ ( "$operation" = verify-existing || "$operation" = create '
            '|| "$operation" = authorization-probe ) && -n "$helper_stage" '
            '&& -n "$helper_stage_identity" ]]; then'
        )
        self.assertEqual(remote_script.count(failure_cleanup_gate), 1)
        self.assertIn(
            'maintenance_tool="$helper_dir/platform_storage_maintenance.py"',
            workflow,
        )
        self._assert_backup_source_runner_branches(workflow_document)
        self._assert_backup_remote_executes_staged_helper(workflow_document)
        self._assert_backup_stage_finalizer_is_bounded_and_identity_pinned(workflow_document)
        self._assert_backup_stage_and_empty_identity_shell_paths(workflow_document)
        staged_helper_conditions = {
            step.get("id"): step.get("if")
            for step in workflow_document["jobs"]["backup"]["steps"]
            if step.get("id") in {"backup-helper", "stage-backup-helper"}
        }
        self.assertEqual(set(staged_helper_conditions), {"backup-helper", "stage-backup-helper"})
        for step_id, condition in staged_helper_conditions.items():
            self.assertIsInstance(condition, str, step_id)
            expression = condition.removeprefix("${{").removesuffix("}}").strip()
            condition_operations = []
            for clause in expression.split("||"):
                match = re.fullmatch(
                    r"inputs\.operation\s*==\s*'([a-z-]+)'", clause.strip()
                )
                self.assertIsNotNone(
                    match,
                    f"unsupported operation condition for {step_id}: {condition!r}",
                )
                condition_operations.append(match.group(1))
            self.assertEqual(
                set(condition_operations),
                {"create", "verify-existing", "authorization-probe"},
            )
            self.assertEqual(len(condition_operations), 3)
        self.assertIn("BACKUP_SUCCESS schema=1 run_id=", workflow)
        self.assertIn(
            "if: ${{ success() && inputs.operation == 'create' && steps.cleanup_ssh.outcome == 'success' }}",
            workflow,
        )
        self.assertIn("storage_helper_sha256=", workflow)
        self.assertIn("backup_drill_sha256=", workflow)
        self.assertIn("remote success marker source/run binding is invalid", workflow)
        self.assertIn("--backup-only", workflow)
        self.assertIn('"--backup-max-age-hours", "24"', workflow)
        self.assertNotIn("--backup-keep 14", workflow)
        self.assertIn("--apply", workflow)
        self.assertIn('"platform_backup_restore_drill.py"', workflow)
        self.assertNotIn('"--verify-latest-existing"', workflow)
        self.assertIn("--verify-existing-backup-only", workflow)
        self.assertIn("gh attestation verify", workflow)
        self.assertIn("/tmp/oldsparky-backup-verifier-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}", workflow)
        self.assertNotIn("--check-latest", workflow)
        self.assertIn("evict_build_node_cache", workflow)
        self.assertIn('"--evict-pinned-build-node-cache"', workflow)
        self.assertIn('"platform_build_node_cache.py"', workflow)
        self.assertIn('"--eviction-bundle-sha256"', workflow)
        self.assertIn('report.get("mode") != expected_mode', workflow)
        self.assertIn('backup.get("rotation_mode") != "preserve-existing"', workflow)
        self.assertIn('backup.get("preexisting_archives_preserved") is not True', workflow)
        self.assertIn('backup.get("removed_count") != 0', workflow)
        self.assertIn('isinstance(backup.get("preexisting_archive_count"), int)', workflow)
        self.assertIn('isinstance(backup.get("preexisting_sidecar_count"), int)', workflow)
        self.assertIn('backup.get("preexisting_sidecar_count") != backup.get("preexisting_archive_count")', workflow)
        self.assertIn('backup.get("postexisting_archive_count") != backup.get("preexisting_archive_count", -1) + 1', workflow)
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

        def workflow_python(marker: str, source: str = workflow) -> str:
            self.assertEqual(
                source.count(marker),
                1,
                f"expected one workflow Python heredoc marker, got {source.count(marker)}: {marker!r}",
            )
            marker_start = source.index(marker)
            body_start = source.index("\n", marker_start) + 1
            body_end = source.index("\n          PY", body_start)
            return textwrap.dedent(source[body_start:body_end])

        def run_inline(
            script: str, *arguments: str
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-I", "-B", "-", *arguments],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )

        poll_script = workflow_python(
            'poll_state="$(/usr/bin/python3 -I -B - "$poll_path"'
        )
        success_marker_script = workflow_python(
            'success_marker="$(/usr/bin/python3 -I - "$remote_report" "$GITHUB_RUN_ID" '
            '"$GITHUB_RUN_ATTEMPT" "$REMOTE_OPERATION" "$SOURCE_SHA"'
        )
        probe_success_marker_script = workflow_python(
            'success_marker="$(/usr/bin/python3 -I - "$remote_report" "$GITHUB_RUN_ID" '
            '"$GITHUB_RUN_ATTEMPT" "$SECURITY_RUN_ID" "$SECURITY_RUN_ATTEMPT" '
            '"$SOURCE_SHA" "$BUNDLE_SHA256"'
        )
        with tempfile.TemporaryDirectory() as binding_dir_name:
            binding_path = Path(binding_dir_name) / "receipt.json"
            run_record = {
                "id": 123456,
                "run_attempt": 2,
                "path": ".github/workflows/platform-security.yml",
                "event": "push",
                "head_branch": "dev",
                "head_sha": "a" * 40,
                "status": "in_progress",
            }
            binding_path.write_text(json.dumps(run_record), encoding="ascii")
            pending = run_inline(
                poll_script, str(binding_path), "123456", "2", "a" * 40
            )
            self.assertEqual(pending.returncode, 0, pending.stderr)
            self.assertEqual(pending.stdout, "pending\n")
            run_record.update(status="completed", conclusion="success")
            binding_path.write_text(json.dumps(run_record), encoding="ascii")
            completed = run_inline(
                poll_script, str(binding_path), "123456", "2", "a" * 40
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "success\n")
            run_record["head_sha"] = "c" * 40
            binding_path.write_text(json.dumps(run_record), encoding="ascii")
            moved_head = run_inline(
                poll_script, str(binding_path), "123456", "2", "a" * 40
            )
            self.assertNotEqual(moved_head.returncode, 0)

            marker = (
                "BACKUP_SUCCESS schema=1 run_id=123456 attempt=2 operation=create "
                f"source_sha={'a' * 40} bundle_sha256={'b' * 64} "
                f"storage_helper_sha256={'c' * 64} backup_drill_sha256={'d' * 64}"
            )
            binding_path.write_text(
                marker + "\n" + json.dumps({"mode": "backup-only"}) + "\n",
                encoding="ascii",
            )
            bound_success = run_inline(
                success_marker_script,
                str(binding_path),
                "123456",
                "2",
                "create",
                "a" * 40,
                "b" * 64,
                "c" * 64,
                "d" * 64,
            )
            self.assertEqual(bound_success.returncode, 0, bound_success.stderr)
            self.assertEqual(bound_success.stdout, marker + "\n")
            probe_rejects_backup_success = run_inline(
                probe_success_marker_script,
                str(binding_path),
                "123456",
                "2",
                "123456",
                "2",
                "a" * 40,
                "b" * 64,
            )
            self.assertNotEqual(probe_rejects_backup_success.returncode, 0)
            wrong_bundle = run_inline(
                success_marker_script,
                str(binding_path),
                "123456",
                "2",
                "create",
                "a" * 40,
                "e" * 64,
                "c" * 64,
                "d" * 64,
            )
            self.assertNotEqual(wrong_bundle.returncode, 0)

        summary_validation_script = workflow_python(
            '/usr/bin/python3 -I - "$backup_report_file" "$operation" '
            '"$evict_build_node_cache" "$compact_fallback_runtime_cache"'
        )
        normalizer_script = workflow_python(
            '/usr/bin/python3 -I - "$public_report" "$operation" '
            '"$backup_report_file" "$EVICT_BUILD_NODE_CACHE"'
        )
        with tempfile.TemporaryDirectory() as normalizer_dir_name:
            normalizer_report_path = Path(normalizer_dir_name) / "report.json"
            normalizer_compaction = {
                "status": "completed",
                "result": "compacted",
                "reclaimed_bytes": 397_406_208,
                "source_commit": "4a04b2dffaf0d02c2d3910e7ba28dca9b89de209",
                "old_tree_sha256": "1" * 64,
                "new_tree_sha256": "2" * 64,
                "intent_receipt": "liveqa-fallback-cache-compaction-123456-1.intent.json",
                "validated_receipt": "liveqa-fallback-cache-compaction-123456-1.validated.json",
                "completion_receipt": "liveqa-fallback-cache-compaction-123456-1.completion.json",
            }
            normalizer_report = {
                "backup": {
                    "fallback_runtime_cache_compaction": normalizer_compaction
                }
            }

            def normalize_public_summary(
                public_json: str = '{"mode":"unknown"}',
            ) -> subprocess.CompletedProcess[str]:
                normalizer_report_path.write_text(
                    json.dumps(normalizer_report, sort_keys=True), encoding="utf-8"
                )
                return run_inline(
                    normalizer_script,
                    public_json,
                    "verify-existing",
                    str(normalizer_report_path),
                    "false",
                    "true",
                )

            normalized = normalize_public_summary()
            self.assertEqual(normalized.returncode, 0, normalized.stderr)
            normalized_report = json.loads(normalized.stdout)
            self.assertEqual(normalized_report["mode"], "verify-existing-backup-only")
            self.assertEqual(
                normalized_report["fallback_runtime_cache_compaction"]["reclaimed_bytes"],
                397_406_208,
            )

            invalid_summary = normalize_public_summary('{"mode":"unexpected"}')
            self.assertNotEqual(invalid_summary.returncode, 0)
            self.assertEqual(
                invalid_summary.stdout,
                "BACKUP_PUBLIC_SUMMARY_FAILURE reason=normalizer_mode_mismatch\n",
            )
            invalid_json = normalize_public_summary("not-json")
            self.assertNotEqual(invalid_json.returncode, 0)
            self.assertEqual(
                invalid_json.stdout,
                "BACKUP_PUBLIC_SUMMARY_FAILURE reason=normalizer_summary_invalid\n",
            )

            del normalizer_compaction["new_tree_sha256"]
            missing_projection = normalize_public_summary()
            self.assertNotEqual(missing_projection.returncode, 0)
            self.assertEqual(
                missing_projection.stdout,
                "BACKUP_PUBLIC_SUMMARY_FAILURE reason=normalizer_projection_invalid\n",
            )
            self.assertNotIn("4a04b2dffaf0d02c2d3910e7ba28dca9b89de209", missing_projection.stdout)

        with tempfile.TemporaryDirectory() as summary_dir_name:
            summary_dir = Path(summary_dir_name)
            summary_report_path = summary_dir / "report.json"
            compact_summary = {
                "status": "completed",
                "result": "compacted",
                "reclaimed_bytes": 397_402_112,
                "source_commit": "4a04b2dffaf0d02c2d3910e7ba28dca9b89de209",
                "old_tree_sha256": "1" * 64,
                "new_tree_sha256": "2" * 64,
                "intent_receipt": "liveqa-fallback-cache-compaction-123456-1.intent.json",
                "validated_receipt": "liveqa-fallback-cache-compaction-123456-1.validated.json",
                "completion_receipt": "liveqa-fallback-cache-compaction-123456-1.completion.json",
                "resumed_run_id": "123456",
                "resumed_run_attempt": "1",
            }
            summary_payload = {
                "mode": "verify-existing-backup-only",
                "ok": True,
                "backup": {
                    "status": "completed",
                    "restore_verified": True,
                    "alembic_revision_verified": True,
                    "rotation_mode": "preserve-existing",
                    "removed_count": 0,
                    "restored_table_count": 1,
                    "verified_existing": True,
                    "created": False,
                    "sha256": "a" * 64,
                    "age_hours": 1.0,
                    "restore_verified_at_utc": "2026-10-10T00:00:00Z",
                    "build_node_cache": {"status": "not-requested"},
                    "fallback_runtime_cache_compaction": compact_summary,
                },
                "production_releases": {"deleted_count": 0, "reclaimable_bytes": 0},
                "source_release_artifacts": {"deleted_count": 0, "reclaimable_bytes": 0},
                "live_qa_runtime_caches": {"deleted_count": 0, "reclaimable_bytes": 0},
                "transient": {
                    category: {"count": 0, "reclaimable_bytes": 0}
                    for category in (
                        "failed_builds",
                        "browser_test_artifacts",
                        "preprod_screenshots",
                    )
                },
            }

            def validate_compaction_summary() -> subprocess.CompletedProcess[str]:
                summary_report_path.write_text(
                    json.dumps(summary_payload, sort_keys=True), encoding="utf-8"
                )
                return run_inline(
                    summary_validation_script,
                    str(summary_report_path),
                    "verify-existing",
                    "false",
                    "true",
                    "true",
                    "123457",
                    "2",
                    "d" * 40,
                    "e" * 64,
                    "123456",
                    "1",
                    "f" * 64,
                )

            resumed_prior = validate_compaction_summary()
            self.assertEqual(resumed_prior.returncode, 0, resumed_prior.stderr)
            compact_summary["intent_receipt"] = (
                "liveqa-fallback-cache-compaction-123457-2.intent.json"
            )
            compact_summary["validated_receipt"] = (
                "liveqa-fallback-cache-compaction-123457-2.validated.json"
            )
            compact_summary["completion_receipt"] = (
                "liveqa-fallback-cache-compaction-123457-2.completion.json"
            )
            resumed_recompaction = validate_compaction_summary()
            self.assertEqual(
                resumed_recompaction.returncode, 0, resumed_recompaction.stderr
            )
            compact_summary["resumed_run_id"] = "123455"
            mismatched_resume = validate_compaction_summary()
            self.assertNotEqual(mismatched_resume.returncode, 0)

        producer_workflow = (
            REPO_ROOT / ".github/workflows/platform-security.yml"
        ).read_text(encoding="utf-8")
        producer_script = workflow_python(
            '/usr/bin/python3 -I - "$GITHUB_WORKSPACE" '
            '"$RUNNER_TEMP/platform-backup-verifier.zip"',
            producer_workflow,
        )
        attestation_script = workflow_python(
            '/usr/bin/python3 -I -B - "$artifact_dir/attestation.json"'
        )
        bundle_validation_script = workflow_python(
            '/usr/bin/python3 -I -B - "$bundle_path" "$SOURCE_SHA"'
        )
        extraction_script = workflow_python(
            '/usr/bin/python3 -I -B - "$helper_stage/bundle.zip" "$bundle_sha"'
        )
        self.assertLess(
            workflow.index('test "$bundle_sha" = "$(awk -F='),
            workflow.index("gh attestation verify \"$bundle_path\""),
        )
        self.assertLess(
            workflow.index("gh attestation verify \"$bundle_path\""),
            workflow.index(
                "- name: Stage the verified helper bundle on the production host"
            ),
        )
        self.assertLess(
            workflow.index("backup verifier attestation policy failed"),
            workflow.index(
                "- name: Stage the verified helper bundle on the production host"
            ),
        )

        with tempfile.TemporaryDirectory(dir="/dev/shm") as fixture_root_name:
            fixture_root = Path(fixture_root_name)
            bundle_path = fixture_root / "bundle.zip"
            source_sha = "a" * 40
            names = (
                "platform_backup_restore_drill.py",
                "platform_disk_policy.py",
                "platform_live_qa_guard.py",
                "platform_release_retention.py",
                "platform_build_node_cache.py",
                "platform_storage_maintenance.py",
                "platform_storage_evidence_summary.py",
            )
            source_modes = {
                "platform_backup_restore_drill.py": 0o755,
                "platform_disk_policy.py": 0o644,
                "platform_live_qa_guard.py": 0o755,
                "platform_release_retention.py": 0o755,
                "platform_build_node_cache.py": 0o644,
                "platform_storage_maintenance.py": 0o755,
                "platform_storage_evidence_summary.py": 0o644,
            }

            producer_root = fixture_root / "checkout"
            producer_tools = producer_root / "platform" / "tools"
            producer_tools.mkdir(parents=True)
            for name in names:
                source = REPO_ROOT / "platform" / "tools" / name
                self.assertEqual(
                    stat.S_IMODE(source.lstat().st_mode), source_modes[name], name
                )
                target = producer_tools / name
                target.write_bytes(source.read_bytes())
                target.chmod(source_modes[name])
            producer_archive = fixture_root / "producer-bundle.zip"
            producer_result = run_inline(
                producer_script,
                str(producer_root),
                str(producer_archive),
                source_sha,
                "StrayForest/old_sparky",
            )
            self.assertEqual(producer_result.returncode, 0, producer_result.stderr)
            with zipfile.ZipFile(producer_archive) as produced:
                produced_manifest = json.loads(
                    produced.read("platform-backup-verifier/manifest.json")
                )
                produced_rows = {
                    row["path"].removeprefix("platform/tools/"): row
                    for row in produced_manifest["files"]
                }
                self.assertEqual(
                    {name: row["source_mode"] for name, row in produced_rows.items()},
                    source_modes,
                )
                for name in names:
                    info = produced.getinfo(f"platform-backup-verifier/{name}")
                    self.assertEqual(
                        stat.S_IMODE(info.external_attr >> 16), 0o444, name
                    )
                    self.assertEqual(
                        hashlib.sha256(produced.read(info)).hexdigest(),
                        produced_rows[name]["sha256"],
                        name,
                    )
            producer_bundle_valid = run_inline(
                bundle_validation_script,
                str(producer_archive),
                source_sha,
                str(fixture_root / "producer-manifest-output.env"),
            )
            self.assertEqual(
                producer_bundle_valid.returncode, 0, producer_bundle_valid.stderr
            )
            wrong_mode_name = "platform_disk_policy.py"
            (producer_tools / wrong_mode_name).chmod(0o755)
            wrong_mode_result = run_inline(
                producer_script,
                str(producer_root),
                str(fixture_root / "wrong-mode.zip"),
                source_sha,
                "StrayForest/old_sparky",
            )
            self.assertNotEqual(wrong_mode_result.returncode, 0)

            payloads = {
                name: f"bounded fixture {name}\n".encode("ascii")
                for name in names
            }
            manifest_rows = [
                {
                    "path": f"platform/tools/{name}",
                    "source_mode": source_modes[name],
                    "size": len(payloads[name]),
                    "sha256": hashlib.sha256(payloads[name]).hexdigest(),
                }
                for name in names
            ]
            manifest_rows_by_name = {
                row["path"].removeprefix("platform/tools/"): row
                for row in manifest_rows
            }
            valid_manifest = {
                "schema": 1,
                "repository": "StrayForest/old_sparky",
                "source_sha": source_sha,
                "files": manifest_rows,
            }

            def write_bundle(
                *,
                manifest: dict[str, object] | None = None,
                member_mode: int = stat.S_IFREG | 0o444,
                duplicate_member: bool = False,
            ) -> None:
                selected_manifest = manifest or valid_manifest
                with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name in names:
                        info = zipfile.ZipInfo(f"platform-backup-verifier/{name}")
                        mode = member_mode if name == names[0] else stat.S_IFREG | 0o444
                        info.external_attr = mode << 16
                        archive.writestr(info, payloads[name])
                    manifest_info = zipfile.ZipInfo(
                        "platform-backup-verifier/manifest.json"
                    )
                    manifest_info.external_attr = (stat.S_IFREG | 0o444) << 16
                    archive.writestr(
                        manifest_info,
                        json.dumps(selected_manifest, separators=(",", ":")),
                    )
                    if duplicate_member:
                        duplicate = zipfile.ZipInfo(
                            f"platform-backup-verifier/{names[0]}"
                        )
                        duplicate.external_attr = (stat.S_IFREG | 0o444) << 16
                        archive.writestr(duplicate, payloads[names[0]])

            write_bundle()
            bundle_sha = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
            attestation_path = fixture_root / "attestation.json"
            expected_workflow = (
                "https://github.com/StrayForest/old_sparky/.github/workflows/"
                "platform-security.yml@refs/heads/dev"
            )
            valid_attestation = [
                {
                    "verificationResult": {
                        "signature": {
                            "certificate": {
                                "issuer": "https://token.actions.githubusercontent.com",
                                "sourceRepositoryURI": (
                                    "https://github.com/StrayForest/old_sparky"
                                ),
                                "sourceRepositoryRef": "refs/heads/dev",
                                "sourceRepositoryDigest": source_sha,
                                "buildConfigURI": expected_workflow,
                                "buildSignerURI": expected_workflow,
                                "runInvocationURI": (
                                    "https://github.com/StrayForest/old_sparky/"
                                    "actions/runs/123456/attempts/2"
                                ),
                            }
                        },
                        "statement": {
                            "subject": [{"digest": {"sha256": bundle_sha}}]
                        },
                    }
                }
            ]

            def attestations_pass(values: object) -> bool:
                attestation_path.write_text(json.dumps(values), encoding="utf-8")
                result = run_inline(
                    attestation_script,
                    str(attestation_path),
                    bundle_sha,
                    "123456",
                    "2",
                    source_sha,
                )
                return result.returncode == 0

            self.assertTrue(attestations_pass(valid_attestation))
            for field, wrong in (
                ("sourceRepositoryDigest", "b" * 40),
                (
                    "runInvocationURI",
                    "https://github.com/StrayForest/old_sparky/"
                    "actions/runs/123457/attempts/2",
                ),
            ):
                broken = json.loads(json.dumps(valid_attestation))
                broken[0]["verificationResult"]["signature"]["certificate"][
                    field
                ] = wrong
                self.assertFalse(attestations_pass(broken), field)
            broken_subject = json.loads(json.dumps(valid_attestation))
            broken_subject[0]["verificationResult"]["statement"]["subject"][0][
                "digest"
            ]["sha256"] = "c" * 64
            self.assertFalse(attestations_pass(broken_subject), "subject digest")
            broken_subject[0]["verificationResult"]["statement"]["subject"][0][
                "digest"
            ]["sha256"] = bundle_sha
            broken_subject[0]["verificationResult"]["statement"]["subject"].append(
                {"digest": {"sha256": bundle_sha}}
            )
            self.assertFalse(attestations_pass(broken_subject), "multiple subjects")

            valid_zip = run_inline(
                bundle_validation_script,
                str(bundle_path),
                source_sha,
                str(fixture_root / "manifest-output.env"),
            )
            self.assertEqual(valid_zip.returncode, 0, valid_zip.stderr)
            manifest_output = (fixture_root / "manifest-output.env").read_text()
            self.assertIn(
                "storage_helper_sha256="
                f"{manifest_rows_by_name['platform_storage_maintenance.py']['sha256']}\n",
                manifest_output,
            )
            self.assertIn(
                "backup_drill_sha256="
                f"{manifest_rows_by_name['platform_backup_restore_drill.py']['sha256']}\n",
                manifest_output,
            )
            for kwargs, reason in (
                (
                    {"manifest": {**valid_manifest, "source_sha": "d" * 40}},
                    "source binding",
                ),
                (
                    {
                        "manifest": {
                            **valid_manifest,
                            "files": [
                                {**manifest_rows[0], "sha256": "e" * 64},
                                *manifest_rows[1:],
                            ],
                        }
                    },
                    "inner member digest",
                ),
                (
                    {
                        "manifest": {
                            **valid_manifest,
                            "files": [
                                {**manifest_rows[1], "source_mode": 0o755},
                                *manifest_rows[:1],
                                *manifest_rows[2:],
                            ],
                        }
                    },
                    "wrong source mode",
                ),
                ({"duplicate_member": True}, "duplicate ZIP member"),
                ({"member_mode": stat.S_IFLNK | 0o777}, "symlink ZIP member"),
            ):
                write_bundle(**kwargs)
                rejected = run_inline(
                    bundle_validation_script,
                    str(bundle_path),
                    source_sha,
                    str(fixture_root / "rejected-manifest-output.env"),
                )
                self.assertNotEqual(rejected.returncode, 0, reason)

            write_bundle()
            archive_sha = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
            output_dir = fixture_root / "extracted"
            wrong_outer_sha = run_inline(
                extraction_script,
                str(bundle_path),
                "e" * 64,
                source_sha,
                str(output_dir),
            )
            self.assertNotEqual(wrong_outer_sha.returncode, 0)
            self.assertFalse(output_dir.exists())
            extracted = run_inline(
                extraction_script,
                str(bundle_path),
                archive_sha,
                source_sha,
                str(output_dir),
            )
            self.assertEqual(extracted.returncode, 0, extracted.stderr)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()}, set(names)
            )
            self.assertTrue(
                all(
                    stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) == 0o444
                    and path.stat(follow_symlinks=False).st_nlink == 1
                    for path in output_dir.iterdir()
                )
            )
            isolated_tools = fixture_root / "attested-tools"
            staged_archive_sha = hashlib.sha256(producer_archive.read_bytes()).hexdigest()
            staged_extraction = run_inline(
                extraction_script,
                str(producer_archive),
                staged_archive_sha,
                source_sha,
                str(isolated_tools),
            )
            self.assertEqual(staged_extraction.returncode, 0, staged_extraction.stderr)
            isolated_help = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(isolated_tools / "platform_storage_maintenance.py"),
                    "--help",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
            self.assertEqual(isolated_help.returncode, 0, isolated_help.stderr)
            self.assertIn("--verify-existing-backup-only", isolated_help.stdout)
        self.assertIn('backup.get("restore_verified") is not True', workflow)
        self.assertIn('backup.get("checksum_present") is not True', workflow)
        self.assertIn('public["mode"] = "backup-only" if sys.argv[2] == "create" else "verify-existing-backup-only"', workflow)
        self.assertIn(
            '"production_releases",\n              "source_release_artifacts",\n              "live_qa_runtime_caches"',
            workflow,
        )
        self._assert_backup_failure_trap_executes(workflow_path)
        self._assert_backup_child_group_is_terminated(workflow_path)
        self._assert_backup_wrapper_bounds_private_stderr(workflow_path)

    def _assert_backup_stage_finalizer_is_bounded_and_identity_pinned(
        self, workflow_document: dict
    ) -> None:
        steps = workflow_document["jobs"]["backup"]["steps"]
        cleanup = next(
            step
            for step in steps
            if step.get("name") == "Remove staged helper bundle from production host"
        )
        self.assertIn("always()", cleanup["if"])
        self.assertIn("steps.stage-backup-helper.outcome != 'skipped'", cleanup["if"])
        operation_step = self._backup_operation_step(workflow_document)
        operation_index = next(
            index
            for index, step in enumerate(steps)
            if step is operation_step
        )
        self.assertLess(operation_index, steps.index(cleanup))
        self.assertLess(
            steps.index(cleanup),
            next(
                index
                for index, step in enumerate(steps)
                if step.get("name") == "Remove production SSH material"
            ),
        )
        run = cleanup["run"]
        self.assertIn("timeout --foreground --signal=TERM --kill-after=5s 60s ssh", run)
        self.assertIn("ServerAliveInterval=10", run)
        self.assertIn("ServerAliveCountMax=3", run)
        stage_lock = "oldsparky-backup-verifier-stage"
        self.assertIn(stage_lock, run)
        self.assertLess(run.index(stage_lock), run.index("oldsparky-platform-release.lock"))
        self.assertIn("flock -n 9 && flock -n 8", run)
        self.assertIn("except FileNotFoundError:", run)

        stage_step = next(
            step for step in steps if step.get("name") == "Stage the verified helper bundle on the production host"
        )["run"]
        self.assertIn(stage_lock, stage_step)
        self.assertIn("cat >\\\"\\$stage/bundle.zip\\\"", stage_step)
        self.assertNotIn("\n          scp \\", stage_step)
        self.assertLess(stage_step.index(stage_lock), stage_step.index('mkdir -m 700 --'))

        remote = run.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE", 1)[0]
        self.assertIn(stage_lock, remote)
        self.assertLess(remote.index(stage_lock), remote.index('stage="/tmp/oldsparky-backup-verifier-'))
        operation_run = self._backup_operation_run(workflow_document)
        operation_remote = operation_run.rsplit("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        lock_start = operation_remote.index("prepare_stage_lock() {")
        lock_end = operation_remote.index("\n}\nprepare_stage_lock", lock_start)
        lock_function = operation_remote[lock_start : lock_end + len("\n}")]
        self.assertIn('test ! -L "$lock_dir"', lock_function)
        self.assertIn("test ! -L \"$lock_parent\"", lock_function)
        self.assertIn("1777", lock_function)
        self.assertIn('test ! -L "$lock_file"', lock_function)
        self.assertIn("/proc/$$/fd/7", lock_function)
        self.assertIn('test -f "$lock_file"', lock_function)
        lock_function = lock_function.replace(
            "/run/lock",
            "__TEST_LOCK_PARENT__",
        )
        lock_function = lock_function.replace(
            '= "0:0:1777"', f'= "{os.geteuid()}:{os.getegid()}:1777"'
        ).replace('= "0:0:755:2"', f'= "{os.geteuid()}:{os.getegid()}:755:2"')
        lock_function = lock_function.replace('= "0:0:644:1"', f'= "{os.geteuid()}:{os.getegid()}:644:1"')

        def run_lock_fixture(lock_parent: Path) -> subprocess.CompletedProcess[str]:
            script = lock_function.replace("__TEST_LOCK_PARENT__", str(lock_parent))
            return subprocess.run(
                ["bash", "-c", f"set -euo pipefail\n{script}\nprepare_stage_lock\n"],
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )

        with tempfile.TemporaryDirectory() as temporary_dir:
            attack_root = Path(temporary_dir)
            safe_parent = attack_root / "safe-parent"
            safe_parent.mkdir(mode=0o755)
            safe_parent.chmod(0o1777)
            accepted = run_lock_fixture(safe_parent)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            lock_file = safe_parent / "oldsparky-backup-verifier-stage" / "operation.lock"
            lock_stat = lock_file.lstat()
            self.assertEqual((lock_stat.st_uid, lock_stat.st_gid), (os.geteuid(), os.getegid()))
            self.assertEqual((stat.S_IMODE(lock_stat.st_mode), lock_stat.st_nlink), (0o644, 1))

            target = attack_root / "untouched-target"
            target.write_bytes(b"keep this target unchanged")
            symlink_parent = attack_root / "symlink-parent"
            symlink_parent.symlink_to(attack_root, target_is_directory=True)
            rejected_parent = run_lock_fixture(symlink_parent)
            self.assertNotEqual(rejected_parent.returncode, 0)
            wrong_parent = attack_root / "wrong-parent-mode"
            wrong_parent.mkdir(mode=0o755)
            wrong_parent.chmod(0o755)
            rejected_parent_mode = run_lock_fixture(wrong_parent)
            self.assertNotEqual(rejected_parent_mode.returncode, 0)
            symlink_lock_parent = attack_root / "symlink-lock-parent"
            symlink_lock_parent.mkdir(mode=0o755)
            symlink_lock_parent.chmod(0o1777)
            (symlink_lock_parent / "oldsparky-backup-verifier-stage").symlink_to(
                attack_root, target_is_directory=True
            )
            rejected_dir = run_lock_fixture(symlink_lock_parent)
            self.assertNotEqual(rejected_dir.returncode, 0)
            symlink_file_parent = attack_root / "symlink-lock-file-parent"
            symlink_file_parent.mkdir(mode=0o755)
            symlink_file_parent.chmod(0o1777)
            symlink_file_dir = symlink_file_parent / "oldsparky-backup-verifier-stage"
            symlink_file_dir.mkdir(mode=0o755)
            (symlink_file_dir / "operation.lock").symlink_to(target)
            rejected_file = run_lock_fixture(symlink_file_parent)
            self.assertNotEqual(rejected_file.returncode, 0)
            wrong_mode_parent = attack_root / "wrong-mode-parent"
            wrong_mode_parent.mkdir(mode=0o755)
            wrong_mode_parent.chmod(0o1777)
            wrong_mode_dir = wrong_mode_parent / "oldsparky-backup-verifier-stage"
            wrong_mode_dir.mkdir(mode=0o777)
            wrong_mode_dir.chmod(0o777)
            rejected_mode = run_lock_fixture(wrong_mode_parent)
            self.assertNotEqual(rejected_mode.returncode, 0)
            hardlink_parent = attack_root / "hardlink-lock-parent"
            hardlink_parent.mkdir(mode=0o755)
            hardlink_parent.chmod(0o1777)
            hardlink_dir = hardlink_parent / "oldsparky-backup-verifier-stage"
            hardlink_dir.mkdir(mode=0o755)
            (hardlink_dir / "operation.lock").hardlink_to(target)
            rejected_link = run_lock_fixture(hardlink_parent)
            self.assertNotEqual(rejected_link.returncode, 0)
            self.assertEqual(target.read_bytes(), b"keep this target unchanged")
        python_source = remote.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as temporary_dir:
            stage = Path(temporary_dir) / "stage"
            stage.mkdir(mode=0o700)
            bundle = stage / "bundle.zip"
            data = b"closed source-bound helper bundle fixture"
            bundle.write_bytes(data)
            bundle.chmod(0o600)
            identity = f"{stage.stat().st_dev}:{stage.stat().st_ino}"
            digest = hashlib.sha256(data).hexdigest()

            def run_cleanup(
                stage_path: Path,
                wanted_identity: str,
                wanted_sha: str,
                wanted_source: str = "a" * 40,
            ):
                return subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-",
                        str(stage_path),
                        wanted_identity,
                        wanted_sha,
                        wanted_source,
                    ],
                    input=python_source,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            cleaned = run_cleanup(stage, identity, digest)
            self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
            self.assertFalse(stage.exists())
            absent = run_cleanup(stage, identity, digest)
            self.assertEqual(absent.returncode, 0, absent.stderr)

            stage.mkdir(mode=0o700)
            discovered_identity = run_cleanup(stage, "", digest)
            self.assertNotEqual(discovered_identity.returncode, 0)
            self.assertTrue(stage.is_dir())
            stage.rmdir()

            stage.mkdir(mode=0o700)
            tools = stage / "tools"
            tools.mkdir(mode=0o700)
            helper_names = {
                "platform_backup_restore_drill.py",
                "platform_disk_policy.py",
                "platform_live_qa_guard.py",
                "platform_release_retention.py",
                "platform_build_node_cache.py",
                "platform_storage_maintenance.py",
                "platform_storage_evidence_summary.py",
            }
            source_sha = "a" * 40
            members = {name: f"# helper {name}\n".encode() for name in helper_names}
            rows = [
                {
                    "path": f"platform/tools/{name}",
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "size": len(contents),
                }
                for name, contents in members.items()
            ]
            manifest = {
                "schema": 1,
                "repository": "StrayForest/old_sparky",
                "source_sha": source_sha,
                "files": rows,
            }
            bundle = stage / "bundle.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr(
                    "platform-backup-verifier/manifest.json",
                    json.dumps(manifest, sort_keys=True),
                )
            bundle.chmod(0o600)
            for name, contents in members.items():
                helper = tools / name
                helper.write_bytes(contents)
                helper.chmod(0o444)
            tools.chmod(0o555)
            stage_identity = f"{stage.stat().st_dev}:{stage.stat().st_ino}"
            staged_digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            extracted = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(stage), stage_identity,
                 staged_digest, source_sha],
                input=python_source,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(extracted.returncode, 0, extracted.stderr)
            self.assertFalse(stage.exists())

            stage.mkdir(mode=0o700)
            bundle = stage / "bundle.zip"
            bundle.write_bytes(data)
            bundle.chmod(0o600)
            mismatch = run_cleanup(stage, identity, "0" * 64)
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertEqual(bundle.read_bytes(), data)

            mismatch_identity = run_cleanup(stage, "0:0", digest)
            self.assertNotEqual(mismatch_identity.returncode, 0)
            self.assertEqual(bundle.read_bytes(), data)

    def _assert_backup_stage_and_empty_identity_shell_paths(
        self, workflow_document: dict
    ) -> None:
        """Execute the local stage/cleanup shell with fake SSH, never a host connection."""
        steps = workflow_document["jobs"]["backup"]["steps"]
        stage_script = next(
            step["run"]
            for step in steps
            if step.get("name") == "Stage the verified helper bundle on the production host"
        )
        cleanup_script = next(
            step["run"]
            for step in steps
            if step.get("name") == "Remove staged helper bundle from production host"
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            bin_dir = root / "bin"
            bin_dir.mkdir(mode=0o700)
            runner_temp = root / "runner"
            bundle_dir = runner_temp / "platform-backup-verifier-input"
            bundle_dir.mkdir(parents=True, mode=0o700)
            bundle = bundle_dir / "platform-backup-verifier.zip"
            bundle_bytes = b"workflow stage fixture bytes"
            bundle.write_bytes(bundle_bytes)
            bundle.chmod(0o600)
            ssh_dir = runner_temp / "platform-production-backup-ssh"
            ssh_dir.mkdir(mode=0o700)
            (ssh_dir / "known_hosts").write_text("fixture\n", encoding="ascii")
            (ssh_dir / "id_ed25519").write_text("fixture-only\n", encoding="ascii")
            (ssh_dir / "known_hosts").chmod(0o600)
            (ssh_dir / "id_ed25519").chmod(0o600)
            ssh_record = root / "ssh-command"
            ssh_stdin = root / "ssh-stdin"
            timeout_stub = bin_dir / "timeout"
            timeout_stub.write_text(
                "#!/bin/sh\n"
                "while [ $# -gt 0 ] && [ \"$1\" != ssh ]; do shift; done\n"
                "test \"${1:-}\" = ssh || exit 91\n"
                "exec \"$@\"\n",
                encoding="ascii",
            )
            timeout_stub.chmod(0o700)
            ssh_stub = bin_dir / "ssh"
            ssh_stub.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                "command=\"${!#}\"\n"
                "printf '%s' \"$command\" >\"$FAKE_SSH_RECORD\"\n"
                "bash -n -c \"$command\" || { cat \"$FAKE_SSH_RECORD\" >&2; exit 92; }\n"
                "cat >\"$FAKE_SSH_STDIN\"\n"
                "printf '2049:987654\\n'\n",
                encoding="ascii",
            )
            ssh_stub.chmod(0o700)
            github_output = root / "github-output"
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_RUN_ID": "38199374511",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": "a" * 40,
                "GITHUB_API_URL": "https://api.github.test",
                "GITHUB_OUTPUT": str(github_output),
                "GH_TOKEN": "test-only-placeholder",
                "REPOSITORY": "StrayForest/old_sparky",
                "SOURCE_SHA": "a" * 40,
                "PROD_SSH_HOST": "production.example.invalid",
                "PROD_SSH_USER": "root",
                "BUNDLE_PATH": str(bundle),
                "BUNDLE_SHA256": hashlib.sha256(bundle_bytes).hexdigest(),
                "SSH_DIR": str(ssh_dir),
                "FAKE_SSH_RECORD": str(ssh_record),
                "FAKE_SSH_STDIN": str(ssh_stdin),
            }
            env.pop("BASH_ENV", None)
            env.pop("ENV", None)
            (bundle_dir / "current-dev-ref.json").write_text(
                json.dumps({"ref": "refs/heads/dev", "object": {"sha": "a" * 40}}),
                encoding="ascii",
            )
            curl = bin_dir / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "out=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then shift; out=$1; fi\n"
                "  shift\n"
                "done\n"
                "test -n \"$out\" || exit 2\n"
                "printf '{\"ref\":\"refs/heads/dev\",\"object\":{\"sha\":\"%s\"}}' \"$SOURCE_SHA\" >\"$out\"\n",
                encoding="ascii",
            )
            curl.chmod(0o700)
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", textwrap.dedent(stage_script)],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(ssh_stdin.read_bytes(), bundle_bytes)
            self.assertIn("local lock_dir=$lock_parent/oldsparky-backup-verifier-stage", ssh_record.read_text())
            self.assertIn('lock_identity=$(stat -c %d:%i:%u:%g:%a:%h "$lock_file")', ssh_record.read_text())
            self.assertIn('stage="/tmp/oldsparky-backup-verifier-$run_id-$run_attempt"', ssh_record.read_text())
            self.assertEqual(github_output.read_text(encoding="ascii"), "stage_identity=2049:987654\n")

            # A bad local source binding fails before SSH is invoked. The trap
            # has no stage identity and therefore performs no remote cleanup.
            ssh_record.unlink()
            failed_env = {**env, "SOURCE_SHA": "b" * 40}
            failed = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", textwrap.dedent(stage_script)],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=failed_env,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(ssh_record.exists())

            # An empty identity must stay a positional argument through the
            # cleanup SSH command; otherwise SHA/source fields shift left.
            ssh_stub.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                "printf '%s\\n' \"$@\" >\"$FAKE_SSH_RECORD\"\n"
                "cat >\"$FAKE_SSH_STDIN\"\n",
                encoding="ascii",
            )
            cleanup_env = {
                **env,
                "STAGED_HELPER_IDENTITY": "",
                "SOURCE_SHA": "a" * 40,
            }
            cleanup = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", textwrap.dedent(cleanup_script)],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=cleanup_env,
            )
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            cleanup_argv = ssh_record.read_text(encoding="ascii").splitlines()
            remote_command = cleanup_argv[-1]
            self.assertIn(
                "bash -s -- 38199374511 1 none "
                f"{hashlib.sha256(bundle_bytes).hexdigest()} {'a' * 40}",
                remote_command,
            )
            cleanup_remote = ssh_stdin.read_text(encoding="ascii")
            cleanup_python = cleanup_remote.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
            absent_path = root / "remote-stage-absent"
            absent_result = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(absent_path), "", "b" * 64, "a" * 40],
                input=cleanup_python,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(absent_result.returncode, 0, absent_result.stderr)
            existing_path = root / "remote-stage-existing"
            existing_path.mkdir(mode=0o700)
            rejected_missing_identity = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(existing_path), "", "b" * 64, "a" * 40],
                input=cleanup_python,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
            self.assertNotEqual(rejected_missing_identity.returncode, 0)
            self.assertTrue(existing_path.is_dir())

    def _assert_backup_source_runner_branches(self, workflow_document: dict) -> None:
        run_script = self._backup_operation_run(workflow_document)
        operation_gate = (
            '[[ "$BACKUP_OPERATION" = create || "$BACKUP_OPERATION" = verify-existing '
            '|| "$BACKUP_OPERATION" = authorization-probe ]]'
        )
        self.assertEqual(run_script.count(operation_gate), 1)
        branch_start = run_script.index(
            operation_gate
        )
        branch_end = run_script.index('remote_report=', branch_start)
        branch_script = "set -euo pipefail\n" + run_script[branch_start:branch_end]
        with tempfile.TemporaryDirectory() as temporary_dir:
            temp = Path(temporary_dir)
            bin_dir = temp / "bin"
            bin_dir.mkdir(mode=0o700)
            (temp / "platform-backup-verifier-input").mkdir(mode=0o700)
            fake_curl = bin_dir / "curl"
            fake_curl.write_text(
                "#!/bin/sh\n"
                "out=\n"
                "while [ $# -gt 0 ]; do\n"
                "  if [ \"$1\" = --output ]; then shift; out=$1; fi\n"
                "  shift\n"
                "done\n"
                "test -n \"$out\" || exit 2\n"
                "printf '{\"ref\":\"%s\",\"object\":{\"sha\":\"%s\"}}' "
                "\"${CURRENT_DEV_REF:-refs/heads/dev}\" "
                "\"${CURRENT_DEV_SHA:-$SOURCE_SHA}\" > \"$out\"\n",
                encoding="ascii",
            )
            fake_curl.chmod(0o700)
            runner_env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "AUTHORIZATION_PROBE_BEFORE_CREATE": "false",
                "EVICT_BUILD_NODE_CACHE": "false",
                "COMPACT_FALLBACK_RUNTIME_CACHE": "false",
                "RESUME_FALLBACK_CACHE_COMPACTION": "false",
                "RESUME_COMPACTION_RUN_ID": "",
                "RESUME_COMPACTION_RUN_ATTEMPT": "",
                "RESUME_COMPACTION_BUNDLE_SHA256": "",
                "SOURCE_SHA": "a" * 40,
                "CURRENT_DEV_REF": "refs/heads/dev",
                "CURRENT_DEV_SHA": "a" * 40,
                "BUNDLE_SHA256": "b" * 64,
                "EXPECTED_STORAGE_HELPER_SHA256": "c" * 64,
                "EXPECTED_BACKUP_DRILL_SHA256": "d" * 64,
                "REPOSITORY": "StrayForest/old_sparky",
                "GITHUB_SHA": "a" * 40,
                "GITHUB_RUN_ID": "123456",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_API_URL": "https://api.github.com",
                "RUNNER_TEMP": str(temp),
                "GH_TOKEN": "fixture-token",
                "STAGED_HELPER_IDENTITY": "",
            }
            for operation in ("create", "verify-existing", "authorization-probe"):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        branch_script
                        + '\nverify_current_dev_source\nprintf "%s\\n" "$bundle_stage"\n',
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    env={**runner_env, "BACKUP_OPERATION": operation},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                self.assertEqual(lines, ["/tmp/oldsparky-backup-verifier-123456-1"])
            for operation in ("create", "verify-existing", "authorization-probe"):
                invalid_env = {
                    **runner_env,
                    "BACKUP_OPERATION": operation,
                    "BUNDLE_SHA256": "",
                }
                invalid = subprocess.run(
                    ["bash", "-c", branch_script],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    env=invalid_env,
                )
                self.assertNotEqual(invalid.returncode, 0, operation)
            invalid_probe_precreate = {
                **runner_env,
                "BACKUP_OPERATION": "verify-existing",
                "AUTHORIZATION_PROBE_BEFORE_CREATE": "true",
            }
            rejected_probe_precreate = subprocess.run(
                ["bash", "-c", branch_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env=invalid_probe_precreate,
            )
            self.assertNotEqual(rejected_probe_precreate.returncode, 0)
            rejected_unknown_operation = subprocess.run(
                ["bash", "-c", branch_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env={**runner_env, "BACKUP_OPERATION": "delete"},
            )
            self.assertNotEqual(rejected_unknown_operation.returncode, 0)
            rejected_source_sha = subprocess.run(
                ["bash", "-c", branch_script],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env={**runner_env, "BACKUP_OPERATION": "create", "GITHUB_SHA": "e" * 40},
            )
            self.assertNotEqual(rejected_source_sha.returncode, 0)
            for invalid_ref, invalid_sha in (
                ("refs/heads/release", "a" * 40),
                ("refs/heads/dev", "e" * 40),
            ):
                rejected_dev_ref = subprocess.run(
                    [
                        "bash",
                        "-c",
                        branch_script + "\nverify_current_dev_source\n",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    env={
                        **runner_env,
                        "BACKUP_OPERATION": "create",
                        "CURRENT_DEV_REF": invalid_ref,
                        "CURRENT_DEV_SHA": invalid_sha,
                    },
                )
                self.assertNotEqual(rejected_dev_ref.returncode, 0)

    def _assert_backup_remote_executes_staged_helper(self, workflow_document: dict) -> None:
        run_script = self._backup_operation_run(workflow_document)
        remote_script = run_script.rsplit("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        stop_at = remote_script.index("failure_stage=summary_validation\n")
        remote_script = remote_script[:stop_at]
        remote_script = remote_script.replace(
            'runtime=/opt/oldsparky/platform', 'runtime=__TEST_RUNTIME__'
        )
        remote_script = remote_script.replace(
            'helper_stage="/tmp/oldsparky-backup-verifier-${run_id}-${run_attempt}"',
            'helper_stage=__TEST_STAGE__',
        )
        remote_script += (
            'printf "SELECTED_HELPER=%s\\nSUMMARY_HELPER=%s\\nHELPER_SHA=%s\\n" '
            '"$maintenance_tool" "$summary_tool" "$storage_helper_sha256"\nexit 0\n'
        )
        helper_names = (
            "platform_backup_restore_drill.py",
            "platform_disk_policy.py",
            "platform_live_qa_guard.py",
            "platform_release_retention.py",
            "platform_build_node_cache.py",
            "platform_storage_maintenance.py",
            "platform_storage_evidence_summary.py",
        )
        helper_modes = {
            "platform_backup_restore_drill.py": 0o755,
            "platform_disk_policy.py": 0o644,
            "platform_live_qa_guard.py": 0o755,
            "platform_release_retention.py": 0o755,
            "platform_build_node_cache.py": 0o644,
            "platform_storage_maintenance.py": 0o755,
            "platform_storage_evidence_summary.py": 0o644,
        }
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            stage_lock = root / "stage-lock"
            runtime = root / "platform"
            release = runtime / "release"
            shared = runtime / "shared"
            tools = release / "tools"
            tools.mkdir(parents=True, mode=0o755)
            shared.mkdir(mode=0o755)
            (shared / ".env.platform").write_text("fixture=1\n", encoding="ascii")
            (runtime / "current").symlink_to(release, target_is_directory=True)
            (tools / "platform_storage_maintenance.py").write_text(
                "raise SystemExit('installed helper must not execute')\n", encoding="ascii"
            )
            (tools / "platform_storage_evidence_summary.py").write_text(
                "raise SystemExit('installed summary must not execute')\n", encoding="ascii"
            )
            python_bin = shared / "venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True, mode=0o755)
            python_bin.symlink_to(sys.executable)
            stage = root / "stage"
            stage.mkdir(mode=0o700)
            archive_path = stage / "bundle.zip"
            archive_path.touch(mode=0o600)
            members = {}
            for name in helper_names:
                if name == "platform_storage_maintenance.py":
                    content = (
                        "import json, os, sys\n"
                        "from pathlib import Path\n"
                        "sys.path.insert(0, str(Path(sys.argv[0]).parent))\n"
                        "import platform_backup_restore_drill as drill\n"
                        "Path(os.environ['BACKUP_HELPER_INVOCATION']).write_text("
                        "json.dumps({'script': sys.argv[0], 'argv': sys.argv[1:], "
                        "'drill': drill.__file__}), encoding='ascii')\n"
                        "print('{}')\n"
                    ).encode("ascii")
                elif name == "platform_backup_restore_drill.py":
                    content = b"SOURCE_BUNDLE_FIXTURE = True\n"
                else:
                    content = f"# signed fixture member {name}\\n".encode("ascii")
                members[name] = content
            manifest = {
                "schema": 1,
                "repository": "StrayForest/old_sparky",
                "source_sha": source_sha,
                "files": [
                    {
                        "path": f"platform/tools/{name}",
                        "source_mode": helper_modes[name],
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    for name, content in members.items()
                ],
            }
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, content in members.items():
                    info = zipfile.ZipInfo(f"platform-backup-verifier/{name}")
                    info.external_attr = (stat.S_IFREG | 0o444) << 16
                    archive.writestr(info, content)
                info = zipfile.ZipInfo("platform-backup-verifier/manifest.json")
                info.external_attr = (stat.S_IFREG | 0o444) << 16
                archive.writestr(info, json.dumps(manifest, separators=(",", ":")))
            archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            invocation_log = root / "helper-invocation.json"
            for operation in ("create", "verify-existing"):
                run_id = str(100_000_000 + uuid.uuid4().int % 800_000_000)
                operation_stage = root / f"stage-{operation}"
                operation_stage.mkdir(mode=0o700)
                staged_zip = operation_stage / "bundle.zip"
                shutil.copyfile(archive_path, staged_zip)
                staged_zip.chmod(0o600)
                script = remote_script.replace(
                    "runtime=__TEST_RUNTIME__", f"runtime={runtime}"
                ).replace("helper_stage=__TEST_STAGE__", f"helper_stage={operation_stage}")
                script = script.replace(
                    "/run/lock/oldsparky-backup-verifier-stage", str(stage_lock)
                )
                script = script.replace(" >/dev/null 2>&1 || :", " >&2 || :")
                script = "set -x\n" + script
                completed = subprocess.run(
                    [
                        "bash", "-s", "--", run_id, "1", operation, source_sha,
                        archive_sha, "false", "false", "false", "none", "none", "none",
                        "123456", "1", "false",
                    ],
                    input=script,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                    env={
                        **os.environ,
                        "BACKUP_HELPER_INVOCATION": str(invocation_log),
                    },
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                expected_helper = operation_stage / "tools/platform_storage_maintenance.py"
                expected_summary = operation_stage / "tools/platform_storage_evidence_summary.py"
                expected_hash = hashlib.sha256(members["platform_storage_maintenance.py"]).hexdigest()
                self.assertIn(f"SELECTED_HELPER={expected_helper}", completed.stdout)
                self.assertIn(f"SUMMARY_HELPER={expected_summary}", completed.stdout)
                self.assertIn(f"HELPER_SHA={expected_hash}", completed.stdout)
                invocation = json.loads(invocation_log.read_text(encoding="ascii"))
                self.assertEqual(invocation["script"], str(expected_helper))
                self.assertEqual(
                    invocation["drill"],
                    str(operation_stage / "tools/platform_backup_restore_drill.py"),
                )
                self.assertIn(
                    "--backup-only" if operation == "create" else "--verify-existing-backup-only",
                    invocation["argv"],
                )
                self.assertFalse(operation_stage.exists())
                invocation_log.unlink()

            missing_stage = root / "missing-stage"
            invalid = subprocess.run(
                [
                    "bash", "-s", "--", str(900_000_000 + uuid.uuid4().int % 90_000_000), "1", "create", "a" * 40, archive_sha,
                    "false", "false", "false", "none", "none", "none",
                    "123456", "1", "false",
                ],
                input=remote_script.replace(
                    "runtime=__TEST_RUNTIME__", f"runtime={runtime}"
                ).replace("helper_stage=__TEST_STAGE__", f"helper_stage={missing_stage}"),
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
                env={**os.environ, "BACKUP_HELPER_INVOCATION": str(invocation_log)},
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertFalse(invocation_log.exists())

    def _assert_backup_failure_trap_executes(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(
            workflow_path.read_text()
        )
        run_script = self._backup_operation_run(workflow)
        remote_script = run_script.rsplit("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        remote_script = remote_script.replace(
            "runtime=/opt/oldsparky/platform", "runtime=__TEST_RUNTIME__"
        )
        remote_script = remote_script.replace(
            'test "$(id -u)" -eq 0 || { echo "Production backup must run as root" >&2; exit 1; }',
            ":",
        )
        helper_validation_marker = "failure_stage=helper_bundle_validation\n"
        self.assertEqual(remote_script.count(helper_validation_marker), 1)
        helper_validation_index = remote_script.index(helper_validation_marker)
        helper_validation_gate_start = remote_script.rfind("if [[ ", 0, helper_validation_index)
        helper_validation_gate_end = remote_script.index("then\n", helper_validation_gate_start) + len("then\n")
        helper_validation_gate = remote_script[helper_validation_gate_start:helper_validation_gate_end]
        self.assertEqual(
            helper_validation_gate,
            'if [[ "$operation" = verify-existing || "$operation" = create '
            '|| "$operation" = authorization-probe ]]; then\n',
        )
        self.assertLess(helper_validation_gate_start, helper_validation_index)
        failure_cleanup_gate = (
            'if [[ ( "$operation" = verify-existing || "$operation" = create '
            '|| "$operation" = authorization-probe ) && -n "$helper_stage" '
            '&& -n "$helper_stage_identity" ]]; then'
        )
        self.assertEqual(remote_script.count(failure_cleanup_gate), 1)
        remote_script = (
            remote_script[:helper_validation_gate_start]
            + "if false; then\n"
            + remote_script[helper_validation_gate_end:]
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            test_root = Path(temporary_dir)
            runtime = test_root / "platform"
            shared = runtime / "shared"
            shared.mkdir(parents=True, mode=0o755)
            shared.chmod(0o755)
            remote_script = remote_script.replace(
                "runtime=__TEST_RUNTIME__", f"runtime={runtime}"
            )
            report_path = test_root / "remote-report"
            remote_script = remote_script.replace(
                'backup_report_file="$(mktemp /tmp/oldsparky-production-backup-report.XXXXXX)"',
                f'backup_report_file="$(mktemp {report_path}.XXXXXX)"',
            )
            completed = subprocess.run(
                [
                    "bash",
                    "-s",
                    "--",
                    "123456",
                    "1",
                    "create",
                    "a" * 40,
                    "b" * 64,
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
                    "123456",
                    "1",
                    "false",
                ],
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
            receipt_path = shared / "backup-failure-123456-1.json"
            receipt_stat = receipt_path.lstat()
            self.assertTrue(stat.S_ISREG(receipt_stat.st_mode))
            self.assertEqual(receipt_stat.st_uid, os.geteuid())
            self.assertEqual(stat.S_IMODE(receipt_stat.st_mode), 0o600)
            self.assertEqual(receipt_stat.st_nlink, 1)
            receipt = json.loads(receipt_path.read_text(encoding="ascii"))
            self.assertEqual(
                set(receipt),
                {
                    "schema", "event", "run_id", "attempt", "operation", "source_sha",
                    "bundle_sha256", "stage", "public_summary_error", "exit_code", "report_state",
                    "report_error_class", "restore_diagnostic", "scratch_unit_diagnostic",
                    "scratch_admission_diagnostic", "scratch_first_fault", "report_bytes",
                    "backup_disk_diagnostic",
                    "report_sha256", "stderr_state",
                    "stderr_exception_class", "stderr_bytes", "stderr_sha256",
                },
            )
            self.assertEqual(receipt["event"], "platform_backup_failure")
            self.assertEqual(receipt["stage"], "preflight")
            self.assertEqual(receipt["public_summary_error"], "none")
            self.assertEqual(receipt["exit_code"], completed.returncode)
            self.assertEqual(receipt["report_error_class"], "none")
            self.assertIsNone(receipt["scratch_unit_diagnostic"])
            self.assertIsNone(receipt["scratch_admission_diagnostic"])
            self.assertIsNone(receipt["scratch_first_fault"])
            self.assertEqual(receipt["stderr_exception_class"], "none")
            self.assertNotIn("Current release symlink is missing", receipt_path.read_text())
            self.assertFalse(report_path.exists())

            typed_runtime = test_root / "typed-platform"
            typed_shared = typed_runtime / "shared"
            typed_shared.mkdir(parents=True, mode=0o755)
            typed_shared.chmod(0o755)
            typed_report = test_root / "typed-report.json"
            typed_report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_class": "backup",
                        "error": "fixture-private detail must not be retained",
                        "restore_diagnostic": {
                            "schema": 1,
                            "restore_stage": "restore_platform",
                            "guard_reason": "disk_floor",
                            "free_bytes": 10,
                            "required_free_bytes": 20,
                            "temporary_database_created": True,
                            "drop_outcome": "confirmed_absent",
                            "temporary_database_absent": True,
                            "archive_sha256": "a" * 64,
                            "archive_size_bytes": 120,
                        },
                        "backup_disk_diagnostic": {
                            "schema": 1,
                            "phase": "backup_admission",
                            "guard_reason": "disk_floor",
                            "free_bytes": 10,
                            "required_free_bytes": 20,
                        },
                        "scratch_unit_diagnostic": {
                            "schema": 2,
                            "phase": "confinement_verification_failed",
                            "active_state": "active",
                            "sub_state": "running",
                            "result": "success",
                            "exec_main_status": 0,
                            "launcher_exit_status": 1,
                            "stdout_bytes": 2048,
                            "stdout_sha256": "a" * 64,
                            "stdout_truncated": False,
                            "stderr_bytes": 32768,
                            "stderr_sha256": "b" * 64,
                            "stderr_truncated": False,
                            "verification_error": "os_error",
                            "verification_errno": 3,
                            "terminal_snapshot": {
                                "load_state": "loaded",
                                "active_state": "active",
                                "sub_state": "running",
                                "result": "success",
                                "exec_main_status": 0,
                                "start_token_sent": False,
                                "completion_token_sent": False,
                            },
                        },
                        "scratch_admission_diagnostic": {
                            "schema": 1,
                            "phase": "pre_create_admission",
                            "guard_reason": "memory_reserve",
                            "available_bytes": 1_060_000_000,
                            "required_bytes": 1_073_741_824,
                            "helper_rss_bytes": 18_000_000,
                            "direct_parent_rss_bytes": 9_000_000,
                            "observed_pair_rss_bytes": 27_000_000,
                            "observed_process_count": 2,
                        },
                    }
                ),
                encoding="ascii",
            )
            typed_report.chmod(0o600)
            typed_script = remote_script.replace(
                f"runtime={runtime}", f"runtime={typed_runtime}"
            ).replace(
                f'backup_report_file="$(mktemp {report_path}.XXXXXX)"',
                f'backup_report_file="{typed_report}"',
            )
            typed_completed = subprocess.run(
                [
                    "bash",
                    "-s",
                    "--",
                    "123457",
                    "1",
                    "create",
                    "a" * 40,
                    "b" * 64,
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
                    "123456",
                    "1",
                    "false",
                ],
                input=typed_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(typed_completed.returncode, 0)
            typed_receipt_path = typed_shared / "backup-failure-123457-1.json"
            typed_receipt = json.loads(typed_receipt_path.read_text(encoding="ascii"))
            self.assertEqual(typed_receipt["report_state"], "typed_failure")
            self.assertEqual(typed_receipt["report_error_class"], "backup")
            self.assertIsNone(typed_receipt["scratch_first_fault"])
            self.assertNotIn("1060000000", typed_completed.stdout)
            self.assertNotIn("27000000", typed_completed.stdout)
            self.assertEqual(
                typed_receipt["restore_diagnostic"]["guard_reason"], "disk_floor"
            )
            self.assertEqual(
                typed_receipt["restore_diagnostic"]["required_free_bytes"], 20
            )
            self.assertEqual(
                typed_receipt["backup_disk_diagnostic"],
                {
                    "schema": 1,
                    "phase": "backup_admission",
                    "guard_reason": "disk_floor",
                    "free_bytes": 10,
                    "required_free_bytes": 20,
                },
            )
            self.assertEqual(
                typed_receipt["scratch_unit_diagnostic"],
                {
                    "schema": 2,
                    "phase": "confinement_verification_failed",
                    "active_state": "active",
                    "sub_state": "running",
                    "result": "success",
                    "exec_main_status": 0,
                    "launcher_exit_status": 1,
                    "stdout_bytes": 2048,
                    "stdout_sha256": "a" * 64,
                    "stdout_truncated": False,
                    "stderr_bytes": 32768,
                    "stderr_sha256": "b" * 64,
                    "stderr_truncated": False,
                    "verification_error": "os_error",
                    "verification_errno": 3,
                    "terminal_snapshot": {
                        "load_state": "loaded",
                        "active_state": "active",
                        "sub_state": "running",
                        "result": "success",
                        "exec_main_status": 0,
                        "start_token_sent": False,
                        "completion_token_sent": False,
                    },
                },
            )
            self.assertEqual(
                typed_receipt["scratch_admission_diagnostic"],
                {
                    "schema": 1,
                    "phase": "pre_create_admission",
                    "guard_reason": "memory_reserve",
                    "available_bytes": 1_060_000_000,
                    "required_bytes": 1_073_741_824,
                    "helper_rss_bytes": 18_000_000,
                    "direct_parent_rss_bytes": 9_000_000,
                    "observed_pair_rss_bytes": 27_000_000,
                    "observed_process_count": 2,
                },
            )
            self.assertNotIn(
                "fixture-private detail",
                typed_receipt_path.read_text(encoding="ascii"),
            )
            self.assertNotIn("SCRATCH_UNIT_PRIVATE_OUTPUT_V1", typed_receipt_path.read_text(encoding="ascii"))
            self.assertFalse(typed_report.exists())

            valid_admission = {
                "schema": 1,
                "phase": "pre_create_admission",
                "guard_reason": "memory_reserve",
                "available_bytes": 1_060_000_000,
                "required_bytes": 1_073_741_824,
                "helper_rss_bytes": 18_000_000,
                "direct_parent_rss_bytes": 9_000_000,
                "observed_pair_rss_bytes": 27_000_000,
                "observed_process_count": 2,
            }
            invalid_admissions = []
            for field, bad_value in (
                ("available_bytes", True),
                ("available_bytes", -1),
                ("required_bytes", None),
                ("helper_rss_bytes", True),
                ("helper_rss_bytes", (1 << 40) + 1),
                ("observed_pair_rss_bytes", 27_000_001),
            ):
                invalid_admissions.append(
                    {**valid_admission, field: bad_value}
                )
            invalid_admissions.append(
                {key: value for key, value in valid_admission.items() if key != "available_bytes"}
            )
            invalid_admissions.extend(
                [
                    {**valid_admission, "observed_process_count": True},
                    {**valid_admission, "direct_parent_rss_bytes": (1 << 40) + 1},
                    {**valid_admission, "observed_process_count": 1},
                    {**valid_admission, "observed_pair_rss_bytes": None},
                ]
            )
            self.assertTrue(maintenance.valid_scratch_admission_diagnostic(valid_admission))
            for invalid_admission in invalid_admissions:
                self.assertFalse(
                    maintenance.valid_scratch_admission_diagnostic(invalid_admission)
                )

            typed_report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_class": "backup",
                        "scratch_unit_diagnostic": {
                            "schema": 1,
                            "phase": "unit_exited_after_confinement",
                            "active_state": "inactive",
                            "sub_state": "dead",
                            "result": "exit-code",
                            "exec_main_status": 1,
                            "launcher_exit_status": 1,
                            "stdout_bytes": 0,
                            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                            "stdout_truncated": False,
                            "stderr_bytes": 0,
                            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                            "stderr_truncated": False,
                            "verification_error": "none",
                            "verification_errno": None,
                        },
                    }
                ),
                encoding="ascii",
            )
            typed_report.chmod(0o600)
            after_confinement_completed = subprocess.run(
                [
                    "bash", "-s", "--", "123460", "1", "create", "a" * 40,
                    "b" * 64, "false", "false", "false", "none", "none", "none",
                    "123456", "1", "false",
                ],
                input=typed_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(after_confinement_completed.returncode, 0)
            after_confinement_receipt_path = typed_shared / "backup-failure-123460-1.json"
            self.assertTrue(
                after_confinement_receipt_path.exists(),
                f"after-confinement receipt missing: files={sorted(path.name for path in typed_shared.iterdir())!r} "
                f"stdout={after_confinement_completed.stdout!r} stderr={after_confinement_completed.stderr!r}",
            )
            after_confinement_receipt = json.loads(
                after_confinement_receipt_path.read_text(encoding="ascii")
            )
            self.assertEqual(after_confinement_receipt["report_state"], "typed_failure")
            self.assertEqual(
                after_confinement_receipt["scratch_unit_diagnostic"]["phase"],
                "unit_exited_after_confinement",
            )
            self.assertEqual(
                after_confinement_receipt["scratch_unit_diagnostic"]["active_state"],
                "inactive",
            )
            self.assertFalse(typed_report.exists())

            typed_report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_class": "backup",
                        "scratch_unit_diagnostic": {
                            "schema": 1,
                            "phase": "unit_exited_after_confinement",
                            "active_state": "active",
                            "sub_state": "running",
                            "result": "exit-code",
                            "exec_main_status": 1,
                            "launcher_exit_status": 1,
                            "stdout_bytes": 0,
                            "stdout_sha256": hashlib.sha256(b"").hexdigest(),
                            "stdout_truncated": False,
                            "stderr_bytes": 0,
                            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                            "stderr_truncated": False,
                            "verification_error": "none",
                            "verification_errno": None,
                        },
                    }
                ),
                encoding="ascii",
            )
            typed_report.chmod(0o600)
            invalid_after_confinement_completed = subprocess.run(
                [
                    "bash", "-s", "--", "123461", "1", "create", "a" * 40,
                    "b" * 64, "false", "false", "false", "none", "none", "none",
                    "123456", "1", "false",
                ],
                input=typed_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(invalid_after_confinement_completed.returncode, 0)
            invalid_after_confinement_receipt = json.loads(
                (typed_shared / "backup-failure-123461-1.json").read_text(encoding="ascii")
            )
            self.assertEqual(invalid_after_confinement_receipt["report_state"], "unclassified_json")
            self.assertIsNone(invalid_after_confinement_receipt["scratch_unit_diagnostic"])
            self.assertFalse(typed_report.exists())

            typed_report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_class": "backup",
                        "restore_diagnostic": {
                            "schema": 1,
                            "restore_stage": "create_database",
                            "guard_reason": "none",
                            "free_bytes": None,
                            "required_free_bytes": None,
                            "temporary_database_created": None,
                            "drop_outcome": "not_required",
                            "temporary_database_absent": None,
                            "archive_sha256": "b" * 64,
                            "archive_size_bytes": 120,
                        },
                        "backup_disk_diagnostic": {
                            "schema": 1,
                            "phase": "backup_admission",
                            "guard_reason": "disk_floor",
                            "free_bytes": 10,
                            "required_free_bytes": 20,
                            "unexpected": "must not be retained",
                        },
                        "scratch_unit_diagnostic": {
                            "schema": 1,
                            "phase": "confinement_verification_failed",
                            "active_state": "active",
                            "sub_state": "running",
                            "result": "success",
                            "exec_main_status": 1,
                            "launcher_exit_status": 1,
                            "stdout_bytes": 32,
                            "stdout_sha256": "a" * 64,
                            "stdout_truncated": False,
                            "stderr_bytes": 128,
                            "stderr_sha256": "b" * 64,
                            "stderr_truncated": False,
                            "verification_error": "policy_error",
                            "verification_errno": 2,
                        },
                    }
                ),
                encoding="ascii",
            )
            typed_report.chmod(0o600)
            uncertain_completed = subprocess.run(
                [
                    "bash",
                    "-s",
                    "--",
                    "123458",
                    "1",
                    "create",
                    "a" * 40,
                    "b" * 64,
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
                    "123456",
                    "1",
                    "false",
                ],
                input=typed_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(uncertain_completed.returncode, 0)
            uncertain_receipt_path = typed_shared / "backup-failure-123458-1.json"
            uncertain_receipt = json.loads(
                uncertain_receipt_path.read_text(encoding="ascii")
            )
            self.assertEqual(uncertain_receipt["report_state"], "unclassified_json")
            self.assertIsNone(
                uncertain_receipt["restore_diagnostic"]["temporary_database_created"]
            )
            self.assertEqual(
                uncertain_receipt["restore_diagnostic"]["restore_stage"],
                "create_database",
            )
            self.assertIsNone(uncertain_receipt["backup_disk_diagnostic"])
            self.assertIsNone(uncertain_receipt["scratch_unit_diagnostic"])
            self.assertEqual(uncertain_receipt["report_state"], "unclassified_json")
            self.assertFalse(typed_report.exists())

            overflow_report = test_root / "overflow-report.json"
            overflow_report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "status": "failed",
                        "error_class": "backup",
                        "scratch_unit_diagnostic": {
                            "schema": 1,
                            "phase": "unit_exited_before_confinement",
                            "active_state": "failed",
                            "sub_state": "failed",
                            "result": "exit-code",
                            "exec_main_status": 1,
                            "launcher_exit_status": 1,
                            "stdout_bytes": 2049,
                            "stdout_sha256": "a" * 64,
                            "stdout_truncated": True,
                            "stderr_bytes": 32768,
                            "stderr_sha256": "b" * 64,
                            "stderr_truncated": False,
                            "verification_error": "none",
                            "verification_errno": None,
                        },
                    }
                ),
                encoding="ascii",
            )
            overflow_report.chmod(0o600)
            overflow_script = remote_script.replace(
                f"runtime={runtime}", f"runtime={typed_runtime}"
            ).replace(
                f'backup_report_file="$(mktemp {report_path}.XXXXXX)"',
                f'backup_report_file="{overflow_report}"',
            )
            overflow_completed = subprocess.run(
                [
                    "bash", "-s", "--", "123459", "1", "create", "a" * 40,
                    "b" * 64, "false", "false", "false", "none", "none", "none",
                    "123456", "1", "false",
                ],
                input=overflow_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(overflow_completed.returncode, 0)
            overflow_receipt = json.loads(
                (typed_shared / "backup-failure-123459-1.json").read_text(encoding="ascii")
            )
            self.assertEqual(overflow_receipt["report_state"], "unclassified_json")
            self.assertIsNone(overflow_receipt["scratch_unit_diagnostic"])
            self.assertFalse(overflow_report.exists())

            trap_start = remote_script.index(
                "import hashlib\n", remote_script.index("backup_failure_marker() {")
            )
            trap_end = remote_script.index("\nPY", trap_start)
            failure_receipt_script = textwrap.dedent(
                remote_script[trap_start:trap_end]
            )
            classifier_start = remote_script.index(
                "classify_public_summary_error() {"
            )
            classifier_end = remote_script.index("\n}", classifier_start) + len(
                "\n}"
            )
            classifier_source = textwrap.dedent(
                remote_script[classifier_start:classifier_end]
            )
            classifier_script = (
                "public_summary_error=none\n"
                + classifier_source
                + '\nclassify_public_summary_error "$1"\n'
                + "printf '%s\\n' \"$public_summary_error\"\n"
            )
            accepted_classification = subprocess.run(
                ["bash", "-s", "--", "BACKUP_PUBLIC_SUMMARY_FAILURE reason=normalizer_projection_invalid"],
                input=classifier_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(accepted_classification.returncode, 0)
            self.assertEqual(accepted_classification.stdout, "normalizer_projection_invalid\n")
            untrusted_classification = subprocess.run(
                ["bash", "-s", "--", "private detail operator@example.test"],
                input=classifier_script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(untrusted_classification.returncode, 0)
            self.assertEqual(untrusted_classification.stdout, "unclassified\n")
            self.assertNotIn("operator@example.test", untrusted_classification.stdout)

            for run_id, stage, summary_error in (
                ("123459", "public_summary", "normalizer_projection_invalid"),
                ("123460", "summary_validation", "summary_tool_failed"),
            ):
                failure_receipt = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-",
                        str(shared),
                        run_id,
                        "1",
                        "verify-existing",
                        "a" * 40,
                        "b" * 64,
                        stage,
                        "1",
                        "",
                        "",
                        summary_error,
                    ],
                    input=failure_receipt_script,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(failure_receipt.returncode, 0, failure_receipt.stderr)
                saved_path = shared / f"backup-failure-{run_id}-1.json"
                saved = json.loads(saved_path.read_text(encoding="ascii"))
                self.assertEqual(saved["stage"], stage)
                self.assertEqual(saved["public_summary_error"], summary_error)
                self.assertEqual(saved["source_sha"], "a" * 40)
                self.assertEqual(saved["bundle_sha256"], "b" * 64)
                self.assertEqual(stat.S_IMODE(saved_path.lstat().st_mode), 0o600)
                self.assertEqual(saved_path.lstat().st_nlink, 1)
                self.assertNotIn("private detail", saved_path.read_text(encoding="ascii"))

            first_fault_shared = test_root / "first-fault-shared"
            first_fault_shared.mkdir(mode=0o755)
            first_fault_shared.chmod(0o755)
            first_fault = {
                "schema": 1,
                "origin": "worker",
                "site": "worker_host_reserve",
                "exception_kind": "os_error",
                "errno": 12,
            }
            first_fault_report = {
                "ok": False,
                "status": "failed",
                "error_class": "backup",
                "scratch_first_fault": first_fault,
            }
            first_fault_frame = (
                b"SCRATCH_FIRST_FAULT_V1\n"
                + json.dumps(
                    first_fault, sort_keys=True, separators=(",", ":")
                ).encode("ascii")
                + b"\n"
            )

            def run_first_fault_trap(run_id: str, frame: bytes) -> subprocess.CompletedProcess[str]:
                report_file = test_root / f"first-fault-report-{run_id}.json"
                capture_file = test_root / f"first-fault-stderr-{run_id}.log"
                report_file.write_text(json.dumps(first_fault_report), encoding="ascii")
                report_file.chmod(0o600)
                capture_file.write_bytes(frame)
                capture_file.chmod(0o600)
                return subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-",
                        str(first_fault_shared),
                        run_id,
                        "1",
                        "create",
                        "a" * 40,
                        "b" * 64,
                        "backup_create",
                        "1",
                        str(report_file),
                        str(capture_file),
                        "none",
                    ],
                    input=failure_receipt_script,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=10,
                )

            bound_fault = run_first_fault_trap("123461", first_fault_frame)
            self.assertEqual(bound_fault.returncode, 0, bound_fault.stderr)
            self.assertEqual(bound_fault.stdout, "")
            self.assertNotIn("SCRATCH_FIRST_FAULT_V1", bound_fault.stdout)
            self.assertNotIn("worker_host_reserve", bound_fault.stdout)
            bound_receipt = json.loads(
                (first_fault_shared / "backup-failure-123461-1.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(bound_receipt["report_state"], "typed_failure")
            self.assertEqual(bound_receipt["scratch_first_fault"], first_fault)
            self.assertEqual(
                set(bound_receipt["scratch_first_fault"]),
                {"schema", "origin", "site", "exception_kind", "errno"},
            )
            self.assertNotIn(
                "SCRATCH_FIRST_FAULT_V1",
                (first_fault_shared / "backup-failure-123461-1.json").read_text(
                    encoding="ascii"
                ),
            )

            mismatched_fault = dict(first_fault, site="worker_archive_open")
            mismatched_frame = (
                b"SCRATCH_FIRST_FAULT_V1\n"
                + json.dumps(
                    mismatched_fault, sort_keys=True, separators=(",", ":")
                ).encode("ascii")
                + b"\n"
            )
            mismatched = run_first_fault_trap("123462", mismatched_frame)
            self.assertEqual(mismatched.returncode, 0, mismatched.stderr)
            mismatched_receipt = json.loads(
                (first_fault_shared / "backup-failure-123462-1.json").read_text(
                    encoding="ascii"
                )
            )
            self.assertEqual(mismatched_receipt["report_state"], "unclassified_json")
            self.assertIsNone(mismatched_receipt["scratch_first_fault"])

    def _backup_operation_step(self, workflow_document: dict) -> dict:
        """Return the YAML step whose run scalar defines the remote operation."""
        steps = workflow_document["jobs"]["backup"]["steps"]
        matches = [
            step
            for step in steps
            if isinstance(step.get("run"), str)
            and "run_remote_operation() {" in step["run"]
            and 'run_remote_operation "$BACKUP_OPERATION" false' in step["run"]
        ]
        self.assertEqual(len(matches), 1, "expected one backup operation run scalar")
        return matches[0]

    def _backup_operation_run(self, workflow_document: dict) -> str:
        run_script = self._backup_operation_step(workflow_document)["run"]
        self.assertIn("run_remote_operation() {", run_script)
        self.assertIn("remote_arguments=", run_script)
        self.assertIn(
            "$SECURITY_RUN_ID $SECURITY_RUN_ATTEMPT $retain_helper_stage",
            run_script,
        )
        return run_script

    def _assert_backup_child_group_is_terminated(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(workflow_path.read_text())
        run_script = self._backup_operation_run(workflow)
        remote_script = run_script.rsplit("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        wrapper_script = next(
            block
            for block in re.findall(r"<<'PY'\n(.*?)\n[ \t]*PY(?=\n)", remote_script, re.S)
            if "def stop_child_group(process):" in block
        )
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
            proc_stat = Path(f"/proc/{os.getpid()}/stat")
            for missing_error in (
                FileNotFoundError(errno.ENOENT, "process exited"),
                ProcessLookupError(errno.ESRCH, "process exited"),
            ):
                with mock.patch.object(
                    Path,
                    "read_text",
                    side_effect=missing_error,
                ) as read_stat:
                    self.assertIsNone(_read_linux_proc_state(proc_stat))
                    read_stat.assert_called_once_with(encoding="ascii")
            with mock.patch.object(
                Path,
                "read_text",
                side_effect=PermissionError(errno.EACCES, "denied"),
            ):
                with self.assertRaises(PermissionError):
                    _read_linux_proc_state(proc_stat)
            with mock.patch.object(Path, "read_text", return_value="malformed"):
                with self.assertRaises(ValueError):
                    _read_linux_proc_state(proc_stat)
            with mock.patch.object(Path, "read_text", return_value="123 (python) RR 1"):
                with self.assertRaises(ValueError):
                    _read_linux_proc_state(proc_stat)

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
                process_state = _read_linux_proc_state(proc_stat)
                if process_state is None:
                    break
                if process_state == "Z":
                    break
                time.sleep(0.1)
            else:
                self.fail("backup cancellation left a child process running")

    def _assert_backup_wrapper_bounds_private_stderr(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(workflow_path.read_text())
        run_script = self._backup_operation_run(workflow)
        remote_script = run_script.rsplit("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
        wrapper_script = next(
            block
            for block in re.findall(r"<<'PY'\n(.*?)\n[ \t]*PY(?=\n)", remote_script, re.S)
            if "def stop_child_group(process):" in block
        )
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

        with tempfile.TemporaryDirectory() as temporary_dir:
            fixture_root = Path(temporary_dir)
            fake_maintenance = fixture_root / "fake_maintenance.py"
            fake_maintenance.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            capture_path = fixture_root / "private.stderr"
            bound_wrapper = wrapper_script.replace(
                "capture_path = Path(sys.argv[7])",
                f"capture_path = Path({str(capture_path)!r})",
            )

            def invoke_wrapper(
                *,
                operation: str,
                evict: str,
                source_sha: str,
                bundle_sha: str,
                compact: str = "false",
                resume: str = "false",
                resume_run_id: str = "none",
                resume_attempt: str = "none",
                resume_bundle_sha: str = "none",
            ) -> list[str]:
                report_path = fixture_root / f"report-{operation}-{evict}.json"
                report_path.write_text("", encoding="utf-8")
                os.chmod(report_path, 0o600)
                capture_path.unlink(missing_ok=True)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-",
                        sys.executable,
                        str(fake_maintenance),
                        "/opt/oldsparky/platform",
                        "123456",
                        "1",
                        str(report_path),
                        operation,
                        evict,
                        compact,
                        resume,
                        resume_run_id,
                        resume_attempt,
                        resume_bundle_sha,
                        source_sha,
                        bundle_sha,
                    ],
                    input=bound_wrapper,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return json.loads(report_path.read_text(encoding="utf-8"))

            source_sha = "a" * 40
            bundle_sha = "b" * 64
            verify_argv = invoke_wrapper(
                operation="verify-existing",
                evict="true",
                source_sha=source_sha,
                bundle_sha=bundle_sha,
            )
            self.assertIn("--verify-existing-backup-only", verify_argv)
            for pair in (
                ("--evict-pinned-build-node-cache", None),
                ("--eviction-run-id", "123456"),
                ("--eviction-run-attempt", "1"),
                ("--eviction-source-sha", source_sha),
                ("--eviction-bundle-sha256", bundle_sha),
            ):
                option, value = pair
                self.assertIn(option, verify_argv)
                if value is not None:
                    self.assertEqual(verify_argv[verify_argv.index(option) + 1], value)

            verify_without_eviction = invoke_wrapper(
                operation="verify-existing",
                evict="false",
                source_sha=source_sha,
                bundle_sha=bundle_sha,
            )
            self.assertIn("--verify-existing-backup-only", verify_without_eviction)
            self.assertNotIn("--evict-pinned-build-node-cache", verify_without_eviction)
            self.assertNotIn("--eviction-source-sha", verify_without_eviction)

            compact_argv = invoke_wrapper(
                operation="verify-existing",
                evict="false",
                source_sha=source_sha,
                bundle_sha=bundle_sha,
                compact="true",
            )
            for option, value in (
                ("--compact-legacy-fallback-runtime-cache", None),
                ("--compaction-run-id", "123456"),
                ("--compaction-run-attempt", "1"),
                ("--compaction-source-sha", source_sha),
                ("--compaction-bundle-sha256", bundle_sha),
            ):
                self.assertIn(option, compact_argv)
                if value is not None:
                    self.assertEqual(compact_argv[compact_argv.index(option) + 1], value)
            self.assertNotIn("--evict-pinned-build-node-cache", compact_argv)

            resume_bundle = "e" * 64
            resume_argv = invoke_wrapper(
                operation="verify-existing",
                evict="false",
                source_sha=source_sha,
                bundle_sha=bundle_sha,
                compact="true",
                resume="true",
                resume_run_id="123455",
                resume_attempt="2",
                resume_bundle_sha=resume_bundle,
            )
            for option, value in (
                ("--resume-legacy-fallback-runtime-cache-compaction", None),
                ("--resume-compaction-run-id", "123455"),
                ("--resume-compaction-run-attempt", "2"),
                ("--resume-compaction-source-sha", source_sha),
                ("--resume-compaction-bundle-sha256", resume_bundle),
            ):
                self.assertIn(option, resume_argv)
                if value is not None:
                    self.assertEqual(resume_argv[resume_argv.index(option) + 1], value)

            create_argv = invoke_wrapper(
                operation="create", evict="false", source_sha=source_sha, bundle_sha=bundle_sha
            )
            self.assertIn("--backup-only", create_argv)
            self.assertNotIn("--verify-existing-backup-only", create_argv)
            self.assertNotIn("--evict-pinned-build-node-cache", create_argv)

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
                    "create",
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
                    "none",
                    "none",
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
                        "backup": {
                            "status": "completed",
                            "created": True,
                            "restore_verified": True,
                            "alembic_revision_verified": True,
                            "rotation_mode": "preserve-existing",
                            "preexisting_archives_preserved": True,
                            "preexisting_archive_count": 0,
                            "preexisting_sidecar_count": 0,
                            "postexisting_archive_count": 1,
                            "removed": [],
                            "removed_count": 0,
                            "preexisting_archive_inventory_sha256": "a" * 64,
                            "postexisting_archive_inventory_sha256": "a" * 64,
                        },
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

            altered = json.loads(report.read_text(encoding="utf-8"))
            altered["backup"]["rotation_mode"] = "rotate-existing"
            report.write_text(json.dumps(altered), encoding="utf-8")
            report.chmod(0o600)
            refused_policy = subprocess.run(
                [sys.executable, "-I", "-", *arguments],
                input=binder,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(refused_policy.returncode, 0)
            self.assertNotIn("private-maintenance-report", refused_policy.stderr)

            altered["backup"]["rotation_mode"] = "preserve-existing"
            altered["backup"]["postexisting_archive_count"] = 0
            report.write_text(json.dumps(altered), encoding="utf-8")
            report.chmod(0o600)
            refused_count = subprocess.run(
                [sys.executable, "-I", "-", *arguments],
                input=binder,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(refused_count.returncode, 0)
            self.assertNotIn("private-maintenance-report", refused_count.stderr)

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

    def test_timer_activation_is_opt_in_and_requires_source_bound_qa_proofs(self) -> None:
        workflow = (
            REPO_ROOT
            / ".github/workflows/platform-production-storage-maintenance.yml"
        ).read_text(encoding="utf-8")
        workflow_payload = yaml.safe_load(workflow)
        jobs = workflow_payload["jobs"]
        self.assertNotRegex(
            repr(workflow_payload.get("env", {})), r"secrets\.PROD_SSH_"
        )
        for job in jobs.values():
            self.assertNotRegex(repr(job.get("env", {})), r"secrets\.PROD_SSH_")
        verifier_job = jobs["verify-live-qa"]
        maintenance_job = jobs["maintenance"]
        verifier = (
            REPO_ROOT / "platform/tools/platform_maintenance_qa_evidence.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(verifier_job["if"], "${{ inputs.enable_maintenance_timer }}")
        self.assertEqual(
            verifier_job["permissions"],
            {"actions": "read", "contents": "read"},
        )
        self.assertNotIn("environment", verifier_job)
        verifier_text = repr(verifier_job)
        self.assertNotRegex(verifier_text, r"secrets\.PROD_SSH_|PROD_SSH_(?:HOST|USER|KEY)")
        self.assertIn("GH_TOKEN", verifier_text)
        checkout = next(
            step
            for step in verifier_job["steps"]
            if step.get("uses", "").startswith("actions/checkout@")
        )
        self.assertEqual(
            checkout["uses"],
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        )
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)
        proof_step = next(
            step
            for step in verifier_job["steps"]
            if step.get("name") == "Verify exact live QA artifacts before timer activation"
        )
        self.assertEqual(proof_step["env"], {"GH_TOKEN": "${{ github.token }}"})
        self.assertIn('test "$GITHUB_REF" = "refs/heads/dev"', proof_step["run"])
        self.assertIn('test "$(git rev-parse HEAD)" = "$GITHUB_SHA"', proof_step["run"])
        self.assertIn('test "$EXPECTED_SHA" = "$GITHUB_SHA"', proof_step["run"])
        self.assertIn("platform/tools/platform_maintenance_qa_evidence.py", proof_step["run"])
        self.assertEqual(maintenance_job["needs"], "verify-live-qa")
        self.assertEqual(
            maintenance_job["if"],
            "${{ always() && !cancelled() && (needs.verify-live-qa.result == 'success' || (inputs.enable_maintenance_timer == false && needs.verify-live-qa.result == 'skipped')) }}",
        )
        self.assertEqual(
            maintenance_job["permissions"],
            {"actions": "none", "contents": "none"},
        )
        maintenance_text = repr(maintenance_job)
        self.assertNotIn("actions/checkout@", maintenance_text)
        self.assertNotIn("platform/tools/", maintenance_text)
        self.assertNotIn("GH_TOKEN", maintenance_text)
        ssh_secret_steps = {
            "Validate production SSH inputs": {
                "PROD_SSH_HOST": "${{ secrets.PROD_SSH_HOST }}",
                "PROD_SSH_USER": "${{ secrets.PROD_SSH_USER }}",
                "PROD_SSH_KEY": "${{ secrets.PROD_SSH_KEY }}",
            },
            "Configure pinned production SSH": {
                "PROD_SSH_HOST": "${{ secrets.PROD_SSH_HOST }}",
                "PROD_SSH_KEY": "${{ secrets.PROD_SSH_KEY }}",
            },
            "Run installed bounded maintenance service": {
                "PROD_SSH_HOST": "${{ secrets.PROD_SSH_HOST }}",
                "PROD_SSH_USER": "${{ secrets.PROD_SSH_USER }}",
            },
        }
        observed_ssh_secret_steps = {}
        for step in maintenance_job["steps"]:
            name = step.get("name")
            self.assertNotRegex(
                repr({key: value for key, value in step.items() if key != "env"}),
                r"secrets\.PROD_SSH_",
            )
            if name in ssh_secret_steps:
                observed_ssh_secret_steps[name] = step.get("env")
            else:
                self.assertNotRegex(
                    repr(step.get("env", {})), r"secrets\.PROD_SSH_"
                )
        self.assertEqual(observed_ssh_secret_steps, ssh_secret_steps)
        self.assertIn("enable_maintenance_timer:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("if: ${{ inputs.enable_maintenance_timer }}", workflow)
        self.assertIn("platform_maintenance_qa_evidence.py", workflow)
        self.assertIn("validate-maintenance-unit", workflow)
        self.assertIn("CPUQuotaPerSecUSec,MemoryMax,TimeoutStartUSec", workflow)
        self.assertIn('"$LIVE_USER_QA_RUN_ATTEMPT" == 1', workflow)
        self.assertIn("validate_workflow_run(", verifier)
        self.assertIn("run_id_value = _input_id(run_id)", verifier)
        self.assertIn("attempt_value = _input_id(attempt)", verifier)
        self.assertIn('run.get("run_attempt") != attempt_value', verifier)
        self.assertIn('artifact_run.get("head_sha") != expected_sha', verifier)
        self.assertIn('artifact_run.get("head_branch") != "dev"', verifier)
        self.assertIn("platform-live-launch-sanitized", verifier)
        self.assertIn("platform-live-user-qa", verifier)
        self.assertIn("total != 54", verifier)
        self.assertIn('counts["attempt_total"] != 1', verifier)
        proof_step = workflow.index("Verify exact live QA artifacts before timer activation")
        ssh_step = workflow.index("Validate production SSH inputs")
        self.assertLess(proof_step, ssh_step)
        report_health = workflow.index("printf 'health=passed\\n'")
        timer_enable = workflow.index("systemctl enable --now deadlock-maintenance.timer")
        self.assertLess(report_health, timer_enable)
        self.assertIn("timer_rollback_unverified", workflow)
        self.assertIn('timer_active_after" == inactive && "$timer_enabled_after" == disabled', workflow)
        self.assertIn('"rotation_mode") != "preserve-existing"', workflow)
        unit_preflight = workflow.index("validate-maintenance-unit")
        service_start = workflow.index("if systemctl start deadlock-maintenance.service; then")
        self.assertLess(unit_preflight, service_start)
        preflight_failure = workflow.index(
            "fail_with_status maintenance_unit_contract_unavailable 2", unit_preflight
        )
        self.assertLess(preflight_failure, service_start)

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
            def report(ok: bool) -> dict[str, object]:
                return {
                    "mode": "apply",
                    "ok": ok,
                    "backup": {
                        "status": "completed",
                        "created": True,
                        "restore_verified": True,
                        "alembic_revision_verified": True,
                        "rotation_mode": "preserve-existing",
                        "preexisting_archives_preserved": True,
                        "preexisting_archive_count": 0,
                        "preexisting_sidecar_count": 0,
                        "postexisting_archive_count": 1,
                        "removed": [],
                        "removed_count": 0,
                        "preexisting_archive_inventory_sha256": "a" * 64,
                        "postexisting_archive_inventory_sha256": "a" * 64,
                    },
                    "disk_after": {
                        "free_bytes": 5_200_000_000,
                        "used_percent": 86.96,
                    },
                    "limits": {
                        "minimum_free_bytes": 5 * BYTES_PER_GIB,
                        "maximum_used_percent": 85,
                    },
                    "private": "PRIVATE_REPORT_CONTENT",
                }

            summary_path.write_text(json.dumps(report(False)), encoding="utf-8")
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

            summary_path.write_text(json.dumps(report(True)), encoding="utf-8")
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

            exact_boundary_report = {
                "mode": "apply",
                "ok": True,
                "backup": {
                    "status": "completed",
                    "created": True,
                    "restore_verified": True,
                    "alembic_revision_verified": True,
                    "rotation_mode": "preserve-existing",
                    "preexisting_archives_preserved": True,
                    "preexisting_archive_count": 0,
                    "preexisting_sidecar_count": 0,
                    "postexisting_archive_count": 1,
                    "removed": [],
                    "removed_count": 0,
                    "preexisting_archive_inventory_sha256": "a" * 64,
                    "postexisting_archive_inventory_sha256": "a" * 64,
                },
                "disk_after": {
                    "free_bytes": 5 * BYTES_PER_GIB,
                    "used_percent": 85,
                },
                "limits": {
                    "minimum_free_bytes": 5 * BYTES_PER_GIB,
                    "maximum_used_percent": 85,
                },
            }
            summary_path.write_text(
                json.dumps(exact_boundary_report), encoding="utf-8"
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
                "ENABLE_MAINTENANCE_TIMER": "false",
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
        def tracked_source_lock(
            path: Path, *, initialize_if_missing: bool = False
        ):
            events.append("source-enter")
            with original_source_lock(
                path, initialize_if_missing=initialize_if_missing
            ) as resolved:
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

        # The cache-only opt-in may initialize only the canonical build-lock
        # directory. Exercise its no-follow creation and inode-race guards
        # without changing the normal missing-lock behavior above.
        with tempfile.TemporaryDirectory(dir="/root") as source_root_name:
            source_root = Path(source_root_name) / "platform"
            source_root.mkdir(mode=0o755)
            source_lock_path = source_root / "dist" / "releases"

            # Reject a noncanonical app before the opt-in lock scope can
            # initialize the otherwise-canonical source lock directory.
            canonical_app = Path(source_root_name) / "canonical-app"
            canonical_app.mkdir()
            noncanonical_app = Path(source_root_name) / "other-app"
            noncanonical_app.mkdir()
            noncanonical_args = self.maintenance_args(noncanonical_app)
            noncanonical_args.verify_existing_backup_only = True
            noncanonical_args.evict_pinned_build_node_cache = True
            noncanonical_args.source_release_dir = source_lock_path
            with (
                mock.patch.object(maintenance, "DEFAULT_APP_DIR", canonical_app),
                mock.patch.object(
                    maintenance, "DEFAULT_SOURCE_RELEASE_DIR", source_lock_path
                ),
                mock.patch.object(
                    maintenance,
                    "maintenance_lock_scope",
                    side_effect=AssertionError("lock scope ran before path rejection"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "canonical app/build lock roots"):
                    maintenance.run_maintenance(noncanonical_args)
            self.assertFalse(source_root.joinpath("dist").exists())

            with (
                mock.patch.object(
                    maintenance, "DEFAULT_PLATFORM_SOURCE_ROOT", source_root
                ),
                mock.patch.object(
                    maintenance, "DEFAULT_SOURCE_RELEASE_DIR", source_lock_path
                ),
            ):
                previous_umask = os.umask(0o077)
                try:
                    with maintenance.source_release_lock(
                        source_lock_path, initialize_if_missing=True
                    ) as locked_path:
                        self.assertEqual(locked_path, source_lock_path)
                        lock_stat = source_lock_path.stat(follow_symlinks=False)
                        dist_stat = source_lock_path.parent.stat(
                            follow_symlinks=False
                        )
                        self.assertEqual(stat.S_IMODE(lock_stat.st_mode), 0o755)
                        self.assertEqual(stat.S_IMODE(dist_stat.st_mode), 0o755)
                        self.assertEqual((lock_stat.st_uid, lock_stat.st_gid), (0, 0))
                finally:
                    os.umask(previous_umask)

                unsafe_root = Path(source_root_name) / "unsafe-platform"
                unsafe_root.mkdir(mode=0o755)
                outside = Path(source_root_name) / "outside"
                outside.mkdir(mode=0o755)
                (unsafe_root / "dist").symlink_to(outside, target_is_directory=True)
                unsafe_lock_path = unsafe_root / "dist" / "releases"
                with mock.patch.object(
                    maintenance, "DEFAULT_PLATFORM_SOURCE_ROOT", unsafe_root
                ), mock.patch.object(
                    maintenance, "DEFAULT_SOURCE_RELEASE_DIR", unsafe_lock_path
                ):
                    with self.assertRaises(OSError):
                        maintenance._ensure_canonical_source_release_lock_directory(
                            unsafe_lock_path
                        )
                self.assertFalse((outside / "releases").exists())

                race_root = Path(source_root_name) / "race-platform"
                race_root.mkdir(mode=0o755)
                race_lock_path = race_root / "dist" / "releases"
                with (
                    mock.patch.object(
                        maintenance, "DEFAULT_PLATFORM_SOURCE_ROOT", race_root
                    ),
                    mock.patch.object(
                        maintenance, "DEFAULT_SOURCE_RELEASE_DIR", race_lock_path
                    ),
                ):
                    @maintenance.contextmanager
                    def replace_lock_directory(path: Path, *, label: str):
                        moved = path.with_name("releases.original")
                        path.rename(moved)
                        path.mkdir(mode=0o755)
                        yield path

                    with mock.patch.object(
                        maintenance,
                        "exclusive_directory_lock",
                        replace_lock_directory,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "identity changed"):
                            with maintenance.source_release_lock(
                                race_lock_path, initialize_if_missing=True
                            ):
                                self.fail("replaced source lock directory was accepted")
                    self.assertTrue(race_lock_path.is_dir())
                    self.assertTrue(race_lock_path.with_name("releases.original").is_dir())

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
