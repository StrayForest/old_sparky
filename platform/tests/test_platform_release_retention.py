from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
import io
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
)


class PlatformReleaseRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self._lock_guard = None
        self._release_lock = None
        self._retained_load_lock = None
        self.original_release_lock_path = retention.RELEASE_LOCK_PATH
        self.original_retained_load_lock_path = retention.RETAINED_LOAD_LOCK_PATH
        try:
            self._lock_guard = lock_support.create_test_lock("retention-guard")
            self._lock_guard.acquire()
            self._release_lock = lock_support.create_test_lock("retention-release")
            self.release_lock_path = self._release_lock.path
            retention.RELEASE_LOCK_PATH = self.release_lock_path
            self._retained_load_lock = lock_support.create_test_lock("retention-load")
            self.retained_load_lock_path = self._retained_load_lock.path
            retention.RETAINED_LOAD_LOCK_PATH = self.retained_load_lock_path
            self.app_dir = Path(self.temp_dir.name) / "platform"
            self.releases_dir = self.app_dir / "releases"
            self.releases_dir.mkdir(parents=True)
            (self.app_dir / "shared").mkdir()
            self.now = datetime(2026, 6, 11, tzinfo=UTC)
        except BaseException:
            retention.RELEASE_LOCK_PATH = self.original_release_lock_path
            retention.RETAINED_LOAD_LOCK_PATH = self.original_retained_load_lock_path
            for lock in (self._retained_load_lock, self._release_lock, self._lock_guard):
                if lock is not None:
                    lock.cleanup()
            self.temp_dir.cleanup()
            raise

    def tearDown(self) -> None:
        cleanup_errors: list[BaseException] = []
        try:
            retention.RELEASE_LOCK_PATH = self.original_release_lock_path
            retention.RETAINED_LOAD_LOCK_PATH = self.original_retained_load_lock_path
        finally:
            self.temp_dir.cleanup()
            for lock in (self._retained_load_lock, self._release_lock, self._lock_guard):
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

    def test_protected_release_only_removes_matching_candidates_and_keeps_allowlist_exact(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        protected = self.add_release("release-preserve-failure", age_days=20)
        candidate = self.add_release("release-old", age_days=19)
        self.link("current", current)
        self.link("previous", previous)

        plan = build_retention_plan(
            self.app_dir,
            keep=0,
            min_age_days=0,
            protect_releases=(protected.name,),
            now=self.now,
        )

        self.assertEqual(
            {entry.path.name for entry in plan.candidates}, {candidate.name}
        )
        self.assertIn(protected.name, {entry.path.name for entry in plan.retained})
        with self.assertRaisesRegex(RuntimeError, "expected candidates"):
            apply_plan(
                plan,
                app_dir=self.app_dir,
                expected_candidates=(protected.name, candidate.name),
            )
        self.assertTrue(protected.is_dir())
        self.assertTrue(candidate.is_dir())

        apply_plan(plan, app_dir=self.app_dir, expected_candidates=(candidate.name,))
        self.assertTrue(protected.is_dir())
        self.assertFalse(candidate.exists())
        self.assertTrue(current.is_dir())
        self.assertTrue(previous.is_dir())

        receipt_candidate = self.add_release("release-cli-obsolete", age_days=18)
        argv = [
            "platform_release_retention.py",
            "--app-dir",
            str(self.app_dir),
            "--keep",
            "0",
            "--min-age-days",
            "0",
            "--apply",
            "--protected-release",
            protected.name,
            "--expected-candidate",
            receipt_candidate.name,
            "--json-receipt",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(retention.sys, "argv", argv),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = retention.main()
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(
            stdout.getvalue(),
            '{"schema":1,"status":"passed","candidate_count":1,'
            '"deleted_count":1,"reclaimed_bytes":16}\n',
        )
        self.assertNotIn(receipt_candidate.name, stdout.getvalue())
        self.assertFalse(receipt_candidate.exists())
        self.assertTrue(protected.exists())
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())

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
        apply_plan(plan, expected_candidates=(candidate.name,))

        self.assertFalse(candidate.exists())
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())
        self.assertTrue(newest.exists())
        self.assertTrue(young.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_apply_refuses_incomplete_candidate_allowlist_before_deleting(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate_one = self.add_release("release-old-one", age_days=20)
        candidate_two = self.add_release("release-old-two", age_days=19)
        self.link("current", current)
        self.link("previous", previous)
        plan = build_retention_plan(
            self.app_dir,
            keep=0,
            min_age_days=0,
            now=self.now,
        )

        with self.assertRaisesRegex(RuntimeError, "expected candidates"):
            apply_plan(
                plan,
                app_dir=self.app_dir,
                expected_candidates=(candidate_one.name,),
            )

        self.assertTrue(candidate_one.is_dir())
        self.assertTrue(candidate_two.is_dir())
        self.assertTrue(current.is_dir())
        self.assertTrue(previous.is_dir())

    def test_apply_refuses_stale_candidate_allowlist(self) -> None:
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

        with self.assertRaisesRegex(RuntimeError, "expected candidates"):
            apply_plan(
                plan,
                app_dir=self.app_dir,
                expected_candidates=("release-no-longer-planned",),
            )

        self.assertTrue(candidate.is_dir())

    def test_expected_candidate_cli_values_must_be_unique_safe_slugs(self) -> None:
        with mock.patch.object(
            retention.sys,
            "argv",
            [
                "platform_release_retention.py",
                "--apply",
                "--expected-candidate",
                "release-old",
            ],
        ):
            args = retention.parse_args()
        self.assertEqual(args.expected_candidate, ["release-old"])

        with mock.patch.object(
            retention.sys,
            "argv",
            [
                "platform_release_retention.py",
                "--protected-release",
                "release-old",
                "--protected-release",
                "release-current",
            ],
        ):
            protected_args = retention.parse_args()
        self.assertEqual(
            protected_args.protected_release, ["release-old", "release-current"]
        )

        with mock.patch.object(
            retention.sys,
            "argv",
            ["platform_release_retention.py", "--expected-candidate", "release-old"],
        ):
            with self.assertRaises(SystemExit):
                retention.parse_args()

        with mock.patch.object(
            retention.sys,
            "argv",
            ["platform_release_retention.py", "--json-receipt"],
        ):
            with self.assertRaises(SystemExit):
                retention.parse_args()

        for values in (
            ("release-old", "release-old"),
            ("../outside",),
        ):
            argv = ["platform_release_retention.py", "--apply"]
            for value in values:
                argv.extend(("--expected-candidate", value))
            with mock.patch.object(retention.sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    retention.parse_args()

        for values in (
            ("release-old", "release-old"),
            ("../outside",),
        ):
            argv = ["platform_release_retention.py"]
            for value in values:
                argv.extend(("--protected-release", value))
            with mock.patch.object(retention.sys, "argv", argv):
                with self.assertRaises(SystemExit):
                    retention.parse_args()

    def test_apply_cli_replans_under_release_lock_and_fails_before_deletion(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate_one = self.add_release("release-old-one", age_days=20)
        candidate_two = self.add_release("release-old-two", age_days=19)
        self.link("current", current)
        self.link("previous", previous)
        argv = [
            "platform_release_retention.py",
            "--app-dir",
            str(self.app_dir),
            "--keep",
            "0",
            "--min-age-days",
            "0",
            "--apply",
            "--json-receipt",
            "--expected-candidate",
            candidate_one.name,
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(retention.sys, "argv", argv),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = retention.main()

        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Release retention failed", stderr.getvalue())
        self.assertTrue(candidate_one.is_dir())
        self.assertTrue(candidate_two.is_dir())
        self.assertTrue(current.is_dir())
        self.assertTrue(previous.is_dir())

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
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate = self.add_release("release-old", age_days=20)
        self.link("current", current)
        self.link("previous", previous)

        sidecars = (
            ".release-operation.json",
            ".release-quiesce.json",
            ".release-systemd-state.json",
        )
        for sidecar in sidecars:
            with self.subTest(sidecar=sidecar):
                path = self.app_dir / "shared" / sidecar
                if sidecar == ".release-quiesce.json":
                    # lexists must fail closed for dangling sidecars too.
                    path.symlink_to(self.app_dir / "missing-recovery-record")
                else:
                    path.write_text("pending\n", encoding="utf-8")
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch("sys.argv", [
                    "platform_release_retention.py",
                    "--app-dir", str(self.app_dir),
                    "--keep", "0",
                    "--apply",
                    "--json-receipt",
                    "--expected-candidate", candidate.name,
                ]), redirect_stdout(stdout), redirect_stderr(stderr):
                    status = retention.main()
                self.assertEqual(status, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("status=passed", stdout.getvalue())
                self.assertTrue(candidate.is_dir())
                self.assertEqual(
                    current,
                    (self.app_dir / "current").resolve(strict=True),
                )
                path.unlink()

    def test_release_lock_contention_fails_closed(self) -> None:
        current = self.add_release("release-current", age_days=30)
        previous = self.add_release("release-previous", age_days=29)
        candidate = self.add_release("release-old", age_days=20)
        self.link("current", current)
        self.link("previous", previous)
        for lock, message in (
            (self._release_lock, "holds the platform release lock"),
            (self._retained_load_lock, "holds the retained-load lock"),
        ):
            with self.subTest(lock=message):
                assert lock is not None
                lock.acquire(nonblocking=True)
                stdout = io.StringIO()
                stderr = io.StringIO()
                try:
                    with mock.patch("sys.argv", [
                        "platform_release_retention.py",
                        "--app-dir", str(self.app_dir),
                        "--keep", "0",
                        "--apply",
                        "--json-receipt",
                        "--expected-candidate", candidate.name,
                    ]), redirect_stdout(stdout), redirect_stderr(stderr):
                        status = retention.main()
                    self.assertEqual(status, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("retention failed (lock)", stderr.getvalue())
                    self.assertTrue(candidate.is_dir())
                finally:
                    lock.release()

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
