from __future__ import annotations

import hashlib
import json
from pathlib import Path
import fcntl
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

from tools import platform_validate_wheelhouse


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_install.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_rollback.sh"
TRANSACTION_TOOL = REPO_ROOT / "platform" / "tools" / "platform_release_transaction.py"
RUNTIME_RESTORE_SCRIPT = REPO_ROOT / "platform" / "tools" / "platform_release_restore_runtime.sh"
TRANSACTION_STATE_NAME = ".release-operation.json"
BUILT_AT = "20260811T120000Z"


class PlatformReleaseVenvRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app_dir = self.root / "platform-app"
        self.releases_dir = self.app_dir / "releases"
        self.shared_dir = self.app_dir / "shared"
        self.releases_dir.mkdir(parents=True)
        self.shared_dir.mkdir()
        (self.shared_dir / ".env.platform").write_text("PLATFORM_TESTING=1\n")
        (self.shared_dir / ".env.platform").chmod(0o600)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fresh_offline_venv_is_retained_for_release_rollback(self) -> None:
        previous_release = self.add_installed_release("previous-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(previous_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("new-release", pip_result="new")
        hostile_cwd = self.root / "hostile-cwd"
        hostile_cwd.mkdir()
        hostile_marker = hostile_cwd / "imported-hostile-module"
        (hostile_cwd / "venv.py").write_text(
            f"from pathlib import Path\nPath({str(hostile_marker)!r}).write_text('venv')\n"
            "raise SystemExit(97)\n"
        )
        hostile_pip = hostile_cwd / "pip"
        hostile_pip.mkdir()
        (hostile_pip / "__init__.py").write_text("")
        (hostile_pip / "__main__.py").write_text(
            f"from pathlib import Path\nPath({str(hostile_marker)!r}).write_text('pip')\n"
            "raise SystemExit(98)\n"
        )

        self.run_script(
            INSTALL_SCRIPT,
            str(artifact),
            str(self.app_dir),
            cwd=hostile_cwd,
        )

        current_release = (self.app_dir / "current").resolve()
        snapshot_dir = current_release / ".rollback" / "shared-venv-before-install"
        self.assertEqual(current_release.name, f"new-release-{BUILT_AT}")
        self.assertEqual((self.app_dir / "previous").resolve(), previous_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        console = subprocess.run(
            [str(self.shared_dir / "venv" / "bin" / "fake-pip-cli")],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(console.stdout, "relocated console script\n")
        self.assertFalse(hostile_marker.exists())
        self.assertEqual((snapshot_dir / "deps-version").read_text(), "old\n")
        self.assertEqual(
            (current_release / ".rollback" / "previous-release").read_text(),
            f"{previous_release}\n",
        )

        self.run_script(ROLLBACK_SCRIPT, "--app-dir", str(self.app_dir), "--no-restart")

        self.assertEqual((self.app_dir / "current").resolve(), previous_release)
        self.assertEqual((self.app_dir / "previous").resolve(), current_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )

    def test_failed_offline_dependency_install_keeps_active_runtime_and_pointers(
        self,
    ) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("broken-release", pip_result="fail")

        result = self.run_script(
            INSTALL_SCRIPT, str(artifact), str(self.app_dir), check=False
        )

        self.assertEqual(result.returncode, 42)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertFalse((self.releases_dir / f"broken-release-{BUILT_AT}").exists())
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertFalse(any(self.shared_dir.glob(".venv-install-*")))

    def test_migration_uncertain_state_refuses_automatic_recovery(self) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("migration-uncertain", pip_result="new")

        self.run_script(
            INSTALL_SCRIPT,
            "--stage-only",
            str(artifact),
            str(self.app_dir),
        )
        state = self.shared_dir / TRANSACTION_STATE_NAME
        self.assertEqual(json.loads(state.read_text())["phase"], "staged")
        self.run_script(
            TRANSACTION_TOOL,
            "phase",
            "--state",
            str(state),
            "--expected",
            "staged",
            "--phase",
            "migration-pending",
        )

        result = self.run_script(
            TRANSACTION_TOOL,
            "recover",
            "--state",
            str(state),
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("migration outcome is not safely reversible", result.stderr)
        self.assertTrue(state.exists())

        result = self.run_script(
            TRANSACTION_TOOL,
            "authorize-recovery",
            "--state",
            str(state),
            "--confirm",
            "WRONG_CONFIRMATION",
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(state.exists())
        self.run_script(
            TRANSACTION_TOOL,
            "authorize-recovery",
            "--state",
            str(state),
            "--confirm",
            "MIGRATION_NOT_REVERSED",
        )
        self.assertEqual(json.loads(state.read_text())["phase"], "recovery-authorized")
        self.run_script(TRANSACTION_TOOL, "recover", "--state", str(state))
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertEqual((self.shared_dir / "venv" / "deps-version").read_text(), "old\n")
        self.assertFalse(state.exists())

    def test_skip_python_deps_refuses_an_existing_venv_that_does_not_match_freeze(
        self,
    ) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("skip-mismatch", pip_result="new")

        result = self.run_script(
            INSTALL_SCRIPT,
            "--skip-python-deps",
            str(artifact),
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertFalse((self.releases_dir / f"skip-mismatch-{BUILT_AT}").exists())

    def test_skip_python_deps_publishes_receipt_and_default_rollback_preserves_venv(
        self,
    ) -> None:
        original_current = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(original_current)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="unchanged", matching_freeze=True)
        shared_identity = (self.shared_dir / "venv").stat()
        artifact = self.build_artifact("skip-match", pip_result="unused")

        self.run_script(
            INSTALL_SCRIPT,
            "--skip-python-deps",
            str(artifact),
            str(self.app_dir),
        )

        candidate = (self.app_dir / "current").resolve()
        rollback_dir = candidate / ".rollback"
        freeze = candidate / "requirements-platform.freeze.txt"
        self.assertEqual((self.app_dir / "previous").resolve(), original_current)
        self.assertEqual(
            (rollback_dir / "previous-release").read_text(),
            f"{original_current}\n",
        )
        self.assertEqual((rollback_dir / "venv-transition").read_text(), "unchanged\n")
        self.assertEqual(
            (rollback_dir / "shared-freeze.sha256").read_text(),
            f"{hashlib.sha256(freeze.read_bytes()).hexdigest()}\n",
        )
        for receipt in (
            rollback_dir / "previous-release",
            rollback_dir / "venv-transition",
            rollback_dir / "shared-freeze.sha256",
        ):
            self.assertEqual(receipt.stat().st_uid, 0)
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        self.assertFalse((rollback_dir / "shared-venv-before-install").exists())

        self.run_script(ROLLBACK_SCRIPT, "--app-dir", str(self.app_dir), "--no-restart")

        restored_identity = (self.shared_dir / "venv").stat()
        self.assertEqual((self.app_dir / "current").resolve(), original_current)
        self.assertEqual((self.app_dir / "previous").resolve(), candidate)
        self.assertEqual(
            (restored_identity.st_dev, restored_identity.st_ino),
            (shared_identity.st_dev, shared_identity.st_ino),
        )
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(),
            "unchanged\n",
        )

    def test_unchanged_venv_rollback_refuses_missing_or_tampered_receipt(self) -> None:
        cases = ("missing-transition", "invalid-transition", "invalid-digest", "drift")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                if index:
                    self.reset_app_tree()
                original_current = self.add_installed_release(f"current-{index}")
                protected_release = self.add_installed_release(f"protected-{index}")
                (self.app_dir / "current").symlink_to(original_current)
                (self.app_dir / "previous").symlink_to(protected_release)
                self.add_fake_shared_venv(marker="unchanged", matching_freeze=True)
                artifact = self.build_artifact(
                    f"skip-receipt-{index}", pip_result="unused"
                )
                self.run_script(
                    INSTALL_SCRIPT,
                    "--skip-python-deps",
                    str(artifact),
                    str(self.app_dir),
                )
                candidate = (self.app_dir / "current").resolve()
                rollback_dir = candidate / ".rollback"
                if case == "missing-transition":
                    (rollback_dir / "venv-transition").unlink()
                elif case == "invalid-transition":
                    (rollback_dir / "venv-transition").write_text("invalid\n")
                elif case == "invalid-digest":
                    (rollback_dir / "shared-freeze.sha256").write_text(f"{'0' * 64}\n")
                else:
                    self.write_executable(
                        self.shared_dir / "venv" / "bin" / "python",
                        'if [ "$*" = "-I -m pip freeze --all" ]; then\n'
                        "  printf '%s\\n' 'pip==0.0.0'\n"
                        "fi\n",
                    )

                result = self.run_script(
                    ROLLBACK_SCRIPT,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.app_dir / "current").resolve(), candidate)
                self.assertEqual(
                    (self.app_dir / "previous").resolve(), original_current
                )
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_transaction_refuses_tampered_skip_receipt_before_state_publish(
        self,
    ) -> None:
        for index, field in enumerate(("transition", "freeze")):
            with self.subTest(field=field):
                if index:
                    self.reset_app_tree()
                current = self.add_installed_release(f"receipt-current-{index}")
                protected = self.add_installed_release(f"receipt-protected-{index}")
                candidate = self.add_installed_release(f"receipt-candidate-{index}")
                self.add_fake_shared_venv(marker="unchanged", matching_freeze=True)
                freeze = candidate / "requirements-platform.freeze.txt"
                freeze.write_text("pip==26.1.2\n")
                freeze.chmod(0o444)
                rollback_dir = candidate / ".rollback"
                rollback_dir.mkdir(mode=0o700)
                records = {
                    "previous-release": f"{current}\n",
                    "venv-transition": "unchanged\n",
                    "shared-freeze.sha256": (
                        f"{hashlib.sha256(freeze.read_bytes()).hexdigest()}\n"
                    ),
                }
                if field == "transition":
                    records["venv-transition"] = "snapshot\n"
                else:
                    records["shared-freeze.sha256"] = f"{'0' * 64}\n"
                for name, value in records.items():
                    record = rollback_dir / name
                    record.write_text(value)
                    record.chmod(0o600)

                result = self.run_script(
                    TRANSACTION_TOOL,
                    "create",
                    "--state",
                    str(self.shared_dir / TRANSACTION_STATE_NAME),
                    "--operation",
                    "install",
                    "--app-dir",
                    str(self.app_dir),
                    "--current-before",
                    str(current),
                    "--previous-before",
                    str(protected),
                    "--candidate-release",
                    str(candidate),
                    "--shared-venv",
                    str(self.shared_dir / "venv"),
                    "--peer",
                    str(self.shared_dir / f".venv-install-{candidate.name}.none"),
                    "--snapshot",
                    str(rollback_dir / "shared-venv-before-install"),
                    "--transition",
                    "none",
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_failure_between_pointer_updates_restores_exact_preinstall_state(
        self,
    ) -> None:
        current_release = self.add_installed_release("current-release")
        protected_release = self.add_installed_release("protected-release")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(protected_release)
        self.add_fake_shared_venv(marker="old")
        artifact = self.build_artifact("pointer-failure", pip_result="new")
        injected_installer = self.write_injected_script(
            INSTALL_SCRIPT,
            "platform_release_install_fail_current.sh",
            "    --phase previous-switched\n",
            "false # test-injected current-pointer failure\n",
        )

        result = self.run_script(
            injected_installer,
            str(artifact),
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), protected_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertFalse((self.releases_dir / f"pointer-failure-{BUILT_AT}").exists())
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_release_lock_contention_causes_no_install_or_rollback_mutation(
        self,
    ) -> None:
        artifact = self.root / "lock-test.tar.gz"
        artifact.write_bytes(b"unused while the lock is held")
        Path(f"{artifact}.sha256").write_text(f"{'0' * 64}  {artifact.name}\n")
        lock_fd = os.open(self.shared_dir, os.O_RDONLY)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            install = self.run_script(
                INSTALL_SCRIPT,
                str(artifact),
                str(self.app_dir),
                check=False,
            )
            rollback = self.run_script(
                ROLLBACK_SCRIPT,
                "--app-dir",
                str(self.app_dir),
                "--no-restart",
                check=False,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

        self.assertEqual(install.returncode, 3)
        self.assertEqual(rollback.returncode, 3)
        self.assertFalse((self.releases_dir / "lock-test").exists())
        self.assertFalse((self.shared_dir / "venv").exists())
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_interrupted_installs_are_exactly_recoverable(self) -> None:
        cases = (
            (
                "after-exchange",
                '    /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange '
                '--state "$TRANSACTION_STATE"\n',
                True,
            ),
            (
                "after-snapshot-move",
                "      --mode place-snapshot\n",
                True,
            ),
            (
                "after-previous-pointer",
                "    --phase previous-switched\n",
                False,
            ),
        )
        for index, (label, needle, previous_present) in enumerate(cases):
            with self.subTest(label=label):
                if index:
                    self.reset_app_tree()
                current_release = self.add_installed_release(f"current-{index}")
                protected_release = self.add_installed_release(f"protected-{index}")
                (self.app_dir / "current").symlink_to(current_release)
                if previous_present:
                    (self.app_dir / "previous").symlink_to(protected_release)
                self.add_fake_shared_venv(marker=f"old-{index}")
                release_ref = f"kill-{index}"
                artifact = self.build_artifact(release_ref, pip_result=f"new-{index}")
                injected = self.write_injected_script(
                    INSTALL_SCRIPT,
                    f"platform_release_install_{label}.sh",
                    needle,
                    '/bin/kill -KILL "$$" # test abrupt interruption\n',
                )

                result = self.run_script(
                    injected,
                    str(artifact),
                    str(self.app_dir),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((self.shared_dir / "venv").is_dir())
                self.assertTrue((self.shared_dir / TRANSACTION_STATE_NAME).is_file())
                self.run_script(
                    ROLLBACK_SCRIPT,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                )
                self.assertEqual((self.app_dir / "current").resolve(), current_release)
                if previous_present:
                    self.assertEqual(
                        (self.app_dir / "previous").resolve(), protected_release
                    )
                else:
                    self.assertFalse(os.path.lexists(self.app_dir / "previous"))
                self.assertEqual(
                    (self.shared_dir / "venv" / "deps-version").read_text(),
                    f"old-{index}\n",
                )
                self.assertFalse(
                    (self.releases_dir / f"{release_ref}-{BUILT_AT}").exists()
                )
                self.assertFalse(any(self.shared_dir.glob(".venv-install-*")))
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_rollback_interruptions_restore_exact_original_state(self) -> None:
        current_release, previous_release, snapshot = self.prepare_rollback_fixture()
        cases = (
            (
                "after-exchange",
                '  /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange '
                '--state "$TRANSACTION_STATE"\n',
            ),
            ("between-pointers", "  --phase current-switched\n"),
        )
        for label, needle in cases:
            with self.subTest(label=label):
                injected = self.write_injected_script(
                    ROLLBACK_SCRIPT,
                    f"platform_release_rollback_{label}.sh",
                    needle,
                    '/bin/kill -KILL "$$" # test abrupt interruption\n',
                )
                result = self.run_script(
                    injected,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((self.shared_dir / "venv").is_dir())
                self.assertTrue((self.shared_dir / TRANSACTION_STATE_NAME).is_file())
                self.run_script(
                    ROLLBACK_SCRIPT,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                )
                self.assertEqual((self.app_dir / "current").resolve(), current_release)
                self.assertEqual(
                    (self.app_dir / "previous").resolve(), previous_release
                )
                self.assertEqual(
                    (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
                )
                self.assertEqual((snapshot / "deps-version").read_text(), "old\n")
                self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_catchable_rollback_failure_after_exchange_recovers_automatically(
        self,
    ) -> None:
        current_release, previous_release, snapshot = self.prepare_rollback_fixture()
        injected = self.write_injected_script(
            ROLLBACK_SCRIPT,
            "platform_release_rollback_false_after_exchange.sh",
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" exchange '
            '--state "$TRANSACTION_STATE"\n',
            "false # test catchable failure\n",
        )

        result = self.run_script(
            injected,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current_release)
        self.assertEqual((self.app_dir / "previous").resolve(), previous_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        self.assertEqual((snapshot / "deps-version").read_text(), "old\n")
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_restart_pending_recovery_finishes_without_rolling_forward(self) -> None:
        current_release, previous_release, snapshot = self.prepare_rollback_fixture()
        systemctl = (
            "/usr/bin/systemctl restart deadlock-api deadlock-worker deadlock-web"
        )
        interrupted_runtime = self.root / "platform_release_restore_runtime_restart_kill.sh"
        interrupted_runtime.write_text(
            RUNTIME_RESTORE_SCRIPT.read_text().replace(
                systemctl, '/bin/kill -KILL "$PPID" # test interrupted restart', 1
            )
        )
        interrupted_runtime.chmod(0o755)
        interrupted_text = self._script_with_physical_tools(ROLLBACK_SCRIPT).replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{interrupted_runtime}"',
            1,
        )
        interrupted = self.root / "platform_release_rollback_restart_kill.sh"
        interrupted.write_text(interrupted_text)
        interrupted.chmod(0o755)

        result = self.run_script(
            interrupted,
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        state = self.shared_dir / TRANSACTION_STATE_NAME
        self.assertTrue(state.is_file())
        self.assertEqual(json.loads(state.read_text())["phase"], "restart-pending")
        self.assertEqual((self.app_dir / "current").resolve(), previous_release)
        self.assertEqual((self.app_dir / "previous").resolve(), current_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertEqual((snapshot / "deps-version").read_text(), "new\n")

        recovery_runtime = self.root / "platform_release_restore_runtime_recover.sh"
        recovery_runtime.write_text("#!/usr/bin/env sh\nexit 0\n")
        recovery_runtime.chmod(0o755)
        recovery_text = self._script_with_physical_tools(ROLLBACK_SCRIPT).replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{recovery_runtime}"',
            1,
        )
        recovery = self.root / "platform_release_rollback_restart_recover.sh"
        recovery.write_text(recovery_text)
        recovery.chmod(0o755)
        self.run_script(
            recovery,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
        )

        self.assertEqual((self.app_dir / "current").resolve(), previous_release)
        self.assertEqual((self.app_dir / "previous").resolve(), current_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )
        self.assertEqual((snapshot / "deps-version").read_text(), "new\n")
        self.assertFalse(state.exists())

    def test_rollback_refuses_unsafe_pointer_snapshot_and_expected_record(self) -> None:
        outside = self.root / "outside-release"
        outside.mkdir()
        previous = self.add_installed_release("previous")
        (self.app_dir / "current").symlink_to(outside)
        (self.app_dir / "previous").symlink_to(previous)
        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

        self.reset_app_tree()
        current, previous, snapshot = self.prepare_rollback_fixture()
        moved_snapshot = current / ".rollback" / "unsafe-snapshot-target"
        snapshot.rename(moved_snapshot)
        snapshot.symlink_to(moved_snapshot, target_is_directory=True)
        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

        snapshot.unlink()
        moved_snapshot.rename(snapshot)
        expected = current / ".rollback" / "previous-release"
        os.link(expected, current / ".rollback" / "unexpected-hardlink")
        result = self.run_script(
            ROLLBACK_SCRIPT,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "new\n"
        )
        self.assertFalse((self.shared_dir / TRANSACTION_STATE_NAME).exists())

    def test_rollback_helper_path_survives_current_symlink_switch(self) -> None:
        current_release, previous_release, _snapshot = self.prepare_rollback_fixture()
        release_tools = current_release / "tools"
        release_tools.mkdir(exist_ok=True)
        shutil.copy2(ROLLBACK_SCRIPT, release_tools / ROLLBACK_SCRIPT.name)
        shutil.copy2(TRANSACTION_TOOL, release_tools / TRANSACTION_TOOL.name)
        shutil.copy2(RUNTIME_RESTORE_SCRIPT, release_tools / RUNTIME_RESTORE_SCRIPT.name)
        invoked_through_current = (
            self.app_dir / "current" / "tools" / ROLLBACK_SCRIPT.name
        )

        self.run_script(
            invoked_through_current,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
        )

        self.assertEqual((self.app_dir / "current").resolve(), previous_release)
        self.assertEqual((self.app_dir / "previous").resolve(), current_release)
        self.assertEqual(
            (self.shared_dir / "venv" / "deps-version").read_text(), "old\n"
        )

    def reset_app_tree(self) -> None:
        shutil.rmtree(self.app_dir)
        self.releases_dir.mkdir(parents=True)
        self.shared_dir.mkdir()
        (self.shared_dir / ".env.platform").write_text("PLATFORM_TESTING=1\n")
        (self.shared_dir / ".env.platform").chmod(0o600)

    def prepare_rollback_fixture(self) -> tuple[Path, Path, Path]:
        current_release = self.add_installed_release("rollback-current")
        previous_release = self.add_installed_release("rollback-previous")
        (self.app_dir / "current").symlink_to(current_release)
        (self.app_dir / "previous").symlink_to(previous_release)
        self.add_fake_shared_venv(marker="new")
        rollback_dir = current_release / ".rollback"
        rollback_dir.mkdir(mode=0o700)
        snapshot = rollback_dir / "shared-venv-before-install"
        self.add_fake_venv(snapshot, marker="old")
        expected = rollback_dir / "previous-release"
        expected.write_text(f"{previous_release}\n")
        expected.chmod(0o600)
        return current_release, previous_release, snapshot

    def _script_with_physical_tools(self, source: Path) -> str:
        lines = source.read_text().splitlines(keepends=True)
        tools_lines = [
            index for index, line in enumerate(lines) if line.startswith('TOOLS_DIR="')
        ]
        self.assertEqual(len(tools_lines), 1)
        lines[tools_lines[0]] = f'TOOLS_DIR="{source.parent}"\n'
        return "".join(lines)

    def write_injected_script(
        self,
        source: Path,
        name: str,
        needle: str,
        insertion: str,
    ) -> Path:
        script = self._script_with_physical_tools(source)
        self.assertIn(needle, script)
        script = script.replace(needle, needle + insertion, 1)
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_replaced_script(
        self,
        source: Path,
        name: str,
        needle: str,
        replacement: str,
    ) -> Path:
        script = self._script_with_physical_tools(source)
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement))
        target.chmod(0o755)
        return target

    def add_installed_release(self, name: str) -> Path:
        release = self.releases_dir / name
        release.mkdir()
        tools = release / "tools"
        tools.mkdir()
        for tool_name in (
            "platform_install_systemd_units.sh",
            "platform_install_nginx.py",
            "platform_deploy_smoke.py",
        ):
            tool = tools / tool_name
            tool.write_text("#!/usr/bin/env sh\nexit 0\n")
            tool.chmod(0o755)
        return release

    def build_artifact(self, release_ref: str, *, pip_result: str) -> Path:
        slug = f"{release_ref}-{BUILT_AT}"
        release = self.root / "artifact-src" / slug
        static_dir = (
            release
            / "apps"
            / "platform_web"
            / ".next"
            / "standalone"
            / ".next"
            / "static"
        )
        static_dir.mkdir(parents=True)
        (
            release / "apps" / "platform_web" / ".next" / "standalone" / "server.js"
        ).write_text("console.log('ok');\n")
        (release / "apps" / "platform_web" / "package-lock.json").write_text("{}\n")
        (release / ".env.platform.example").write_text("PLATFORM_TESTING=1\n")
        (release / "requirements-platform.txt").write_text("pip==26.1.2\n")
        lock = release / "requirements-platform.lock.txt"
        lock.write_text("pip==26.1.2\n")
        lock.chmod(0o644)
        freeze = release / "requirements-platform.freeze.txt"
        freeze.write_text("pip==26.1.2\n")
        freeze.chmod(0o444)
        wheelhouse = release / "wheelhouse"
        wheelhouse.mkdir()
        self.add_fake_pip_wheel(wheelhouse, result=pip_result)
        platform_validate_wheelhouse.create_manifest(
            wheelhouse,
            release / "requirements-platform.txt",
            lock,
            freeze,
        )
        payload = {
            "artifact_format_version": 1,
            "release_slug": slug,
            "built_at_utc": BUILT_AT,
            "release_ref": release_ref,
            "source_git_commit": "a" * 40,
            "python_requirements_file": "requirements-platform.txt",
            "python_lock_file": "requirements-platform.lock.txt",
            "python_freeze_file": "requirements-platform.freeze.txt",
            "python_wheelhouse_dir": "wheelhouse",
            "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
            "web_package_lock_file": "apps/platform_web/package-lock.json",
            "web_build_id": "test-build-id",
            "node_version": "26.3.1",
            "npm_version": "11.16.0",
            "runtime_layout": {
                "app_dir": "/opt/oldsparky/platform",
                "current_symlink": "/opt/oldsparky/platform/current",
                "previous_symlink": "/opt/oldsparky/platform/previous",
                "shared_dir": "/opt/oldsparky/platform/shared",
                "shared_env_file": "/opt/oldsparky/platform/shared/.env.platform",
                "shared_venv_dir": "/opt/oldsparky/platform/shared/venv",
            },
        }
        release_json = release / "RELEASE.json"
        release_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        release_json.chmod(0o444)

        artifact = self.root / f"{slug}.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            archive.add(release, arcname=slug)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        Path(f"{artifact}.sha256").write_text(f"{digest}  {artifact.name}\n")
        return artifact

    def add_fake_pip_wheel(self, wheelhouse: Path, *, result: str) -> Path:
        wheel = wheelhouse / "pip-26.1.2-py3-none-any.whl"
        module = f"""\
from pathlib import Path
import sys

arguments = sys.argv[1:]
if arguments and arguments[0] == "install":
    Path(sys.prefix, "deps-version").write_text({result!r} + "\\n")
    raise SystemExit({42 if result == "fail" else 0})
if arguments and arguments[0] == "check":
    print("No broken requirements found.")
    raise SystemExit(0)
if arguments[:2] == ["freeze", "--all"]:
    print("pip==26.1.2")
    raise SystemExit(0)
raise SystemExit("unsupported fake pip invocation: " + repr(arguments))
"""
        dist_info = "pip-26.1.2.dist-info"
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("pip/__init__.py", '__version__ = "26.1.2"\n')
            archive.writestr("pip/__main__.py", module)
            archive.writestr(
                "pip/cli.py",
                'def main():\n    print("relocated console script")\n',
            )
            archive.writestr(
                f"{dist_info}/METADATA",
                "Metadata-Version: 2.1\nName: pip\nVersion: 26.1.2\n\n",
            )
            archive.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                "Tag: py3-none-any\n",
            )
            archive.writestr(
                f"{dist_info}/entry_points.txt",
                "[console_scripts]\nfake-pip-cli = pip.cli:main\n",
            )
            archive.writestr(f"{dist_info}/RECORD", "")
        return wheel

    def add_fake_shared_venv(
        self, *, marker: str, matching_freeze: bool = False
    ) -> None:
        self.add_fake_venv(self.shared_dir / "venv", marker=marker)
        if matching_freeze:
            self.write_executable(
                self.shared_dir / "venv" / "bin" / "python",
                'if [ "$*" = "-I -m pip freeze --all" ]; then\n'
                "  printf '%s\\n' 'pip==26.1.2'\n"
                "fi\n",
            )

    def add_fake_venv(self, venv: Path, *, marker: str) -> None:
        bin_dir = venv / "bin"
        bin_dir.mkdir(parents=True)
        (venv / "deps-version").write_text(f"{marker}\n")
        self.write_executable(bin_dir / "python", "exit 0\n")

    def write_executable(self, path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env sh\nset -eu\n{body}")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_script(
        self,
        script: Path,
        *args: str,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *args],
            cwd=cwd or REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
            check=check,
        )


if __name__ == "__main__":
    unittest.main()
