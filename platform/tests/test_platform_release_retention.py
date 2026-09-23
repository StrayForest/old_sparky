from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from tests import platform_test_lock_support as lock_support
from tools import platform_release_retention as retention
from tools.platform_release_retention import (
    apply_plan,
    build_retention_plan,
    release_operation_lock,
)


class PlatformReleaseRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self._lock_guard = None
        self._release_lock = None
        self.original_release_lock_path = retention.RELEASE_LOCK_PATH
        try:
            self._lock_guard = lock_support.create_test_lock("retention-guard")
            self._lock_guard.acquire()
            self._release_lock = lock_support.create_test_lock("retention-release")
            self.release_lock_path = self._release_lock.path
            retention.RELEASE_LOCK_PATH = self.release_lock_path
            self.app_dir = Path(self.temp_dir.name) / "platform"
            self.releases_dir = self.app_dir / "releases"
            self.releases_dir.mkdir(parents=True)
            (self.app_dir / "shared").mkdir()
            self.now = datetime(2026, 6, 11, tzinfo=UTC)
        except BaseException:
            retention.RELEASE_LOCK_PATH = self.original_release_lock_path
            for lock in (self._release_lock, self._lock_guard):
                if lock is not None:
                    lock.cleanup()
            self.temp_dir.cleanup()
            raise

    def tearDown(self) -> None:
        cleanup_errors: list[BaseException] = []
        try:
            retention.RELEASE_LOCK_PATH = self.original_release_lock_path
        finally:
            self.temp_dir.cleanup()
            for lock in (self._release_lock, self._lock_guard):
                if lock is not None:
                    try:
                        lock.cleanup()
                    except BaseException as exc:
                        cleanup_errors.append(exc)
        if cleanup_errors:
            raise cleanup_errors[0]

    def add_release(self, name: str, *, age_days: int, size: int = 16) -> Path:
        release = self.releases_dir / name
        release.mkdir()
        (release / "payload.bin").write_bytes(b"x" * size)
        timestamp = (self.now - timedelta(days=age_days)).timestamp()
        release.touch()
        Path(release / "payload.bin").touch()
        import os

        os.utime(release, (timestamp, timestamp))
        return release

    def link(self, name: str, target: Path) -> None:
        (self.app_dir / name).symlink_to(target)

    def test_plan_protects_current_previous_newest_and_young_releases(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        old_candidate = self.add_release("release-old", age_days=20)
        self.add_release("release-newest", age_days=1)
        self.add_release("release-young", age_days=3)
        self.link("current", current)
        self.link("previous", previous)

        plan = build_retention_plan(
            self.app_dir,
            keep=2,
            min_age_days=7,
            now=self.now,
        )

        self.assertEqual(
            {entry.path.name for entry in plan.protected},
            {"release-current", "release-previous"},
        )
        self.assertEqual(
            {entry.path.name for entry in plan.retained},
            {"release-newest", "release-young"},
        )
        self.assertEqual(
            {entry.path.name for entry in plan.candidates},
            {old_candidate.name},
        )

    def test_apply_deletes_only_candidates(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate = self.add_release("release-old", age_days=20)
        newest = self.add_release("release-newest", age_days=1)
        young = self.add_release("release-young", age_days=2)
        self.link("current", current)
        self.link("previous", previous)

        plan = build_retention_plan(
            self.app_dir,
            keep=2,
            min_age_days=7,
            now=self.now,
        )
        apply_plan(plan)

        self.assertFalse(candidate.exists())
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())
        self.assertTrue(newest.exists())
        self.assertTrue(young.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_zero_retention_keeps_only_current_and_previous(self) -> None:
        current = self.add_release("release-current", age_days=0)
        previous = self.add_release("release-previous", age_days=0)
        obsolete_one = self.add_release("release-obsolete-one", age_days=0)
        obsolete_two = self.add_release("release-obsolete-two", age_days=0)
        self.link("current", current)
        self.link("previous", previous)

        plan = build_retention_plan(
            self.app_dir,
            keep=0,
            min_age_days=0,
            now=self.now,
        )
        apply_plan(plan)

        self.assertEqual(
            {entry.path.name for entry in plan.candidates},
            {obsolete_one.name, obsolete_two.name},
        )
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())
        self.assertFalse(obsolete_one.exists())
        self.assertFalse(obsolete_two.exists())

    def test_plan_rejects_protected_target_outside_release_directory(self) -> None:
        current = self.add_release("release-current", age_days=1)
        external = Path(self.temp_dir.name) / "external"
        external.mkdir()
        self.link("current", current)
        self.link("previous", external)

        with self.assertRaisesRegex(RuntimeError, "outside"):
            build_retention_plan(
                self.app_dir,
                keep=2,
                min_age_days=7,
                now=self.now,
            )

    def test_apply_refuses_candidate_that_became_current_after_planning(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate = self.add_release("release-old", age_days=20)
        self.link("current", current)
        self.link("previous", previous)
        plan = build_retention_plan(
            self.app_dir,
            keep=0,
            min_age_days=0,
            now=self.now,
        )

        (self.app_dir / "current").unlink()
        self.link("current", candidate)

        with self.assertRaisesRegex(RuntimeError, "became protected"):
            apply_plan(plan, app_dir=self.app_dir)
        self.assertTrue(candidate.exists())

    def test_apply_refuses_candidate_inode_replacement(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate = self.add_release("release-old", age_days=20)
        self.link("current", current)
        self.link("previous", previous)
        plan = build_retention_plan(
            self.app_dir,
            keep=0,
            min_age_days=0,
            now=self.now,
        )

        replaced = self.releases_dir / "release-replaced"
        candidate.rename(replaced)
        candidate.mkdir()

        with self.assertRaisesRegex(RuntimeError, "changed after planning"):
            apply_plan(plan, app_dir=self.app_dir)
        self.assertTrue(candidate.exists())

    def test_release_lock_refuses_pending_transaction(self) -> None:
        state = self.app_dir / "shared" / ".release-operation.json"
        state.write_text("pending\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "must be recovered"):
            with release_operation_lock(self.app_dir):
                self.fail("pending transaction unexpectedly acquired retention lock")

    def test_release_lock_contention_fails_closed(self) -> None:
        try:
            assert self._release_lock is not None
            self._release_lock.acquire(nonblocking=True)
            with self.assertRaisesRegex(
                RuntimeError, "holds the platform release lock"
            ):
                with release_operation_lock(self.app_dir):
                    self.fail("contended retention lock was acquired")
        finally:
            assert self._release_lock is not None
            self._release_lock.release()

    def test_test_lock_creation_rejects_precreated_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            fixed_uuid = UUID("11111111-1111-1111-1111-111111111111")
            candidate = root / f"{lock_support.TEST_LOCK_PREFIX}symlink-{fixed_uuid.hex}.lock"
            victim = root / "victim"
            victim.write_bytes(b"must remain unchanged")
            candidate.symlink_to(victim)
            with mock.patch.object(lock_support, "uuid4", return_value=fixed_uuid):
                with self.assertRaises(FileExistsError):
                    lock_support.create_test_lock("symlink", root=root)
            self.assertEqual(victim.read_bytes(), b"must remain unchanged")

    def test_test_lock_cleanup_refuses_identity_swap(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            lock = lock_support.create_test_lock("identity", root=Path(temporary))
            replacement = lock.path
            replacement.unlink()
            replacement.write_bytes(b"replacement")
            replacement.chmod(0o600)
            with self.assertRaisesRegex(AssertionError, "identity changed"):
                lock.cleanup()
            self.assertEqual(replacement.read_bytes(), b"replacement")
            replacement.unlink()

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            fixed_uuid = UUID("22222222-2222-2222-2222-222222222222")
            candidate = root / f"{lock_support.TEST_LOCK_PREFIX}metadata-{fixed_uuid.hex}.lock"
            with (
                mock.patch.object(lock_support, "uuid4", return_value=fixed_uuid),
                mock.patch.object(
                    lock_support,
                    "_assert_lock_metadata",
                    side_effect=AssertionError("metadata validation failed"),
                ),
                self.assertRaisesRegex(AssertionError, "metadata validation failed"),
            ):
                lock_support.create_test_lock("metadata", root=root)
            self.assertFalse(candidate.exists() or candidate.is_symlink())
