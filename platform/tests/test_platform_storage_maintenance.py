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
        self.assertFalse(args.evict_pinned_build_node_cache)
        self.assertIsNone(args.eviction_run_id)

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
        backup_script = str(REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py")
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], [sys.executable, "-B", backup_script])
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
        failure = subprocess.CompletedProcess(
            [],
            1,
            json.dumps(
                {
                    "ok": False,
                    "error": "private failure detail",
                    "restore_diagnostic": diagnostic,
                }
            ),
            "child diagnostic",
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
        workflow = workflow_path.read_text(encoding="utf-8")
        self.assertIn("platform_storage_maintenance.py", workflow)
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
            )
            source_modes = {
                "platform_backup_restore_drill.py": 0o755,
                "platform_disk_policy.py": 0o644,
                "platform_live_qa_guard.py": 0o755,
                "platform_release_retention.py": 0o755,
                "platform_build_node_cache.py": 0o644,
                "platform_storage_maintenance.py": 0o755,
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
                bundle_validation_script, str(producer_archive), source_sha
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
                bundle_validation_script, str(bundle_path), source_sha
            )
            self.assertEqual(valid_zip.returncode, 0, valid_zip.stderr)
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
                    bundle_validation_script, str(bundle_path), source_sha
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
            "runtime=/opt/oldsparky/platform", "runtime=__TEST_RUNTIME__"
        )
        remote_script = remote_script.replace(
            'test "$(id -u)" -eq 0 || { echo "Production backup must run as root" >&2; exit 1; }',
            ":",
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
                    "none",
                    "none",
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
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
                    "report_error_class", "restore_diagnostic", "report_bytes",
                    "report_sha256", "stderr_state",
                    "stderr_exception_class", "stderr_bytes", "stderr_sha256",
                },
            )
            self.assertEqual(receipt["event"], "platform_backup_failure")
            self.assertEqual(receipt["stage"], "preflight")
            self.assertEqual(receipt["public_summary_error"], "none")
            self.assertEqual(receipt["exit_code"], completed.returncode)
            self.assertEqual(receipt["report_error_class"], "none")
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
                    "none",
                    "none",
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
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
            self.assertEqual(
                typed_receipt["restore_diagnostic"]["guard_reason"], "disk_floor"
            )
            self.assertEqual(
                typed_receipt["restore_diagnostic"]["required_free_bytes"], 20
            )
            self.assertNotIn(
                "fixture-private detail",
                typed_receipt_path.read_text(encoding="ascii"),
            )
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
                    "none",
                    "none",
                    "false",
                    "false",
                    "false",
                    "none",
                    "none",
                    "none",
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
            self.assertEqual(uncertain_receipt["report_state"], "typed_failure")
            self.assertIsNone(
                uncertain_receipt["restore_diagnostic"]["temporary_database_created"]
            )
            self.assertEqual(
                uncertain_receipt["restore_diagnostic"]["restore_stage"],
                "create_database",
            )
            self.assertFalse(typed_report.exists())

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

    def _assert_backup_child_group_is_terminated(self, workflow_path: Path) -> None:
        workflow = yaml.safe_load(workflow_path.read_text())
        run_script = next(
            step["run"]
            for step in workflow["jobs"]["backup"]["steps"]
            if step.get("name") == "Create and verify the production backup"
        )
        remote_script = run_script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
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
        run_script = next(
            step["run"]
            for step in workflow["jobs"]["backup"]["steps"]
            if step.get("name") == "Create and verify the production backup"
        )
        remote_script = run_script.split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
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
                operation="create", evict="false", source_sha="none", bundle_sha="none"
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
