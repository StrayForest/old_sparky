from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import platform_operator_storage_retention as retention


class OperatorStorageRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="operator-storage-retention-")
        self.root = Path(self.temporary.name)
        self.owner_uid = os.geteuid()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure_roots(self) -> None:
        self.checkout = self.root / "old_sparky"
        self.checkout.mkdir()
        self.git_dir = self.checkout / ".git"
        self.git_dir.mkdir()
        self.npm_root = self.root / ".npm"
        self.npm_root.mkdir()
        self.cache_root = self.root / ".cache"
        self.cache_root.mkdir()
        self.vscode = self.root / ".vscode-server" / "cli" / "servers"
        self.vscode.mkdir(parents=True)
        self.tmp_root = self.root / "tmp"
        self.tmp_root.mkdir()
        for name, value in (
            ("MAIN_CHECKOUT", self.checkout),
            ("GIT_DIR", self.git_dir),
            ("NPM_ROOT", self.npm_root),
            ("CACHE_ROOT", self.cache_root),
            ("VSCODE_SERVERS", self.vscode),
            ("TMP_ROOT", self.tmp_root),
            ("EXPECTED_OWNER_UID", self.owner_uid),
            ("WORKTREE_SCAN_ROOTS", ()),
        ):
            self.enterContext(mock.patch.object(retention, name, value))

    @staticmethod
    def make_tree(path: Path) -> None:
        path.mkdir(parents=True)
        (path / "payload").write_bytes(b"recreatable")

    def test_plan_is_limited_to_exact_reproducible_roots_and_age(self) -> None:
        self.configure_roots()
        old = self.git_dir / "codex-performance-20261001"
        recent = self.git_dir / "codex-performance-20261121"
        wrong = self.git_dir / "codex-performance-backup"
        for path in (old, recent, wrong):
            self.make_tree(path)
        self.make_tree(self.npm_root / "_cacache")
        self.make_tree(self.npm_root / "_npx")
        self.make_tree(self.npm_root / "config")
        self.make_tree(self.cache_root / "ms-playwright")
        self.make_tree(self.cache_root / "future-cache")
        self.make_tree(self.cache_root / "oldsparky" / "actionlint-1.7.12")
        stable = self.vscode / ("Stable-" + "a" * 40)
        malformed = self.vscode / "Stable-latest"
        self.make_tree(stable)
        self.make_tree(malformed)
        temp_old = self.tmp_root / "oldsparky-web-hermetic.test-older"
        temp_recent = self.tmp_root / "oldsparky-web-hermetic.test-newer"
        self.make_tree(temp_old)
        self.make_tree(temp_recent)
        old_time = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
        os.utime(temp_old, (old_time, old_time))
        with mock.patch.object(retention, "_worktree_records", return_value=[]), mock.patch.object(
            retention, "process_references", return_value=False
        ):
            candidates, skipped, worktrees = retention.build_plan(
                datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
            )
        paths = {candidate.path for candidate in candidates}
        self.assertEqual(
            paths,
            {
                old,
                self.npm_root / "_cacache",
                self.npm_root / "_npx",
                self.cache_root / "ms-playwright",
                stable,
                temp_old,
            },
        )
        self.assertNotIn(self.cache_root / "oldsparky", paths)
        self.assertEqual(skipped["unsafe"], 0)
        self.assertEqual(worktrees["registered_extra"], 0)

    def test_busy_cache_is_skipped_and_unknown_cache_never_matches(self) -> None:
        self.configure_roots()
        npm_cache = self.npm_root / "_cacache"
        unknown = self.cache_root / "unexpected"
        self.make_tree(npm_cache)
        self.make_tree(unknown)
        with mock.patch.object(
            retention, "process_references", side_effect=lambda path, names=None: path == npm_cache
        ), mock.patch.object(retention, "_worktree_records", return_value=[]):
            candidates, skipped, _ = retention.build_plan()
        self.assertNotIn(npm_cache, {candidate.path for candidate in candidates})
        self.assertNotIn(unknown, {candidate.path for candidate in candidates})
        self.assertEqual(skipped["busy"], 1)

    def test_browser_and_python_tool_caches_check_root_consumers(self) -> None:
        self.configure_roots()
        browser_cache = self.cache_root / "ms-playwright"
        pip_cache = self.cache_root / "pip"
        for path in (browser_cache, pip_cache):
            self.make_tree(path)
        observed: dict[Path, set[str] | None] = {}

        def references(path: Path, names: set[str] | None = None) -> bool:
            observed[path] = names
            return False

        with mock.patch.object(retention, "process_references", side_effect=references), mock.patch.object(
            retention, "_worktree_records", return_value=[]
        ):
            retention.build_plan()
        self.assertIn("node", observed[browser_cache])
        self.assertIn("python3.12", observed[browser_cache])
        self.assertIn("pip", observed[pip_cache])

    def test_apply_removes_only_planned_cache_paths(self) -> None:
        self.configure_roots()
        npm_cache = self.npm_root / "_cacache"
        npm_config = self.npm_root / "config"
        future_cache = self.cache_root / "future-cache"
        for path in (npm_cache, npm_config, future_cache):
            self.make_tree(path)
        lock_path = self.root / "run" / "operator-retention.lock"
        lock_path.parent.mkdir()
        self.enterContext(mock.patch.object(retention, "LOCK_PATH", lock_path))
        self.enterContext(mock.patch.object(retention, "process_references", return_value=False))
        self.enterContext(mock.patch.object(retention, "_worktree_records", return_value=[]))
        self.enterContext(mock.patch.object(retention.os, "geteuid", return_value=0))
        result = retention.run(apply=True)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(npm_cache.exists())
        self.assertTrue(npm_config.exists())
        self.assertTrue(future_cache.exists())
        self.assertEqual(result["removed_counts"], {"npm_cache": 1})

    def test_apply_rechecks_browser_consumers_after_planning(self) -> None:
        self.configure_roots()
        browser_cache = self.cache_root / "ms-playwright"
        self.make_tree(browser_cache)
        lock_path = self.root / "run" / "operator-retention.lock"
        lock_path.parent.mkdir()
        self.enterContext(mock.patch.object(retention, "LOCK_PATH", lock_path))
        self.enterContext(mock.patch.object(retention.os, "geteuid", return_value=0))
        self.enterContext(mock.patch.object(retention, "_worktree_records", return_value=[]))
        observed: list[set[str] | None] = []

        def references(path: Path, names: set[str] | None = None) -> bool:
            if path != browser_cache:
                return False
            observed.append(names)
            return len(observed) > 1

        self.enterContext(mock.patch.object(retention, "process_references", side_effect=references))
        with self.assertRaises(retention.RetentionError):
            retention.run(apply=True)
        self.assertTrue(browser_cache.exists())
        self.assertEqual(len(observed), 2)
        self.assertIn("node", observed[-1])

    def test_worktree_requires_clean_attached_and_unlocked_state(self) -> None:
        self.configure_roots()
        worktree = self.root / "managed-worktree"
        self.make_tree(worktree)
        with (
            mock.patch.object(retention, "_managed_worktree", return_value=True),
            mock.patch.object(retention, "MAIN_CHECKOUT", self.checkout),
            mock.patch.object(retention, "_worktree_records", return_value=[(worktree, "task-branch", False)]),
            mock.patch.object(retention, "_same_common_directory", return_value=True),
            mock.patch.object(retention, "_worktree_status", return_value=(True, ())),
            mock.patch.object(retention, "process_references", return_value=False),
        ):
            candidates, _, summary = retention.build_plan()
        self.assertIn(worktree, {candidate.path for candidate in candidates})
        self.assertEqual(summary["registered_extra"], 1)
        self.assertEqual(summary["unregistered_directories"], 0)

    def test_dirty_and_locked_worktrees_are_counted_not_removed(self) -> None:
        self.configure_roots()
        dirty = self.root / "managed-dirty"
        locked = self.root / "managed-locked"
        self.make_tree(dirty)
        self.make_tree(locked)
        with (
            mock.patch.object(retention, "_managed_worktree", return_value=True),
            mock.patch.object(retention, "MAIN_CHECKOUT", self.checkout),
            mock.patch.object(
                retention,
                "_worktree_records",
                return_value=[(dirty, "dirty-branch", False), (locked, "locked-branch", True)],
            ),
            mock.patch.object(retention, "_same_common_directory", return_value=True),
            mock.patch.object(retention, "_worktree_status", return_value=(False, ())),
            mock.patch.object(retention, "process_references", return_value=False),
        ):
            candidates, skipped, summary = retention.build_plan()
        self.assertNotIn(dirty, {candidate.path for candidate in candidates})
        self.assertNotIn(locked, {candidate.path for candidate in candidates})
        self.assertEqual(summary["registered_extra"], 2)
        self.assertEqual(skipped["dirty_or_unknown_worktrees"], 1)
        self.assertEqual(skipped["locked_worktrees"], 1)

    def test_worktree_managed_prefixes_are_explicit(self) -> None:
        for value in (
            "/root/old_sparky-task",
            "/root/oldsparky-worktrees/anything",
            "/root/.codex/worktrees/backup-task",
            "/dev/shm/oldsparky-task",
        ):
            self.assertTrue(retention._managed_worktree(Path(value)), value)
        for value in (
            "/tmp/oldsparky-unregistered",
            "/root/old_sparky",
            "/root/random-project",
            "/dev/shm/other-project",
        ):
            self.assertFalse(retention._managed_worktree(Path(value)), value)

    def test_ignored_cache_parent_collapses_only_when_it_contains_owned_venv(self) -> None:
        self.configure_roots()
        worktree = self.root / "worktree"
        cache = worktree / "platform/.cache"
        (cache / "platform-ci-venv").mkdir(parents=True)
        with mock.patch.object(
            retention, "_run_git", return_value=b"!! platform/.cache/\0"
        ):
            clean, deps = retention._worktree_status(worktree)
        self.assertTrue(clean)
        self.assertEqual(deps, (Path("platform/.cache/platform-ci-venv"),))
        (cache / "unknown-cache").mkdir()
        with mock.patch.object(
            retention, "_run_git", return_value=b"!! platform/.cache/\0"
        ):
            clean, deps = retention._worktree_status(worktree)
        self.assertFalse(clean)
        self.assertEqual(deps, ())


if __name__ == "__main__":
    unittest.main()
