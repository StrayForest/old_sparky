from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
import os
from pathlib import Path
import tempfile
import unittest

from tools.platform_release_retention import (
    apply_plan,
    build_retention_plan,
    release_operation_lock,
)


class PlatformReleaseRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app_dir = Path(self.temp_dir.name) / "platform"
        self.releases_dir = self.app_dir / "releases"
        self.releases_dir.mkdir(parents=True)
        (self.app_dir / "shared").mkdir()
        self.now = datetime(2026, 6, 11, tzinfo=UTC)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

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
        descriptor = os.open(self.app_dir / "shared", os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(
                RuntimeError, "holds the platform release lock"
            ):
                with release_operation_lock(self.app_dir):
                    self.fail("contended retention lock was acquired")
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
