from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from tests import platform_test_lock_support as lock_support


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "platform/tools/platform_release_deploy.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "platform/tools/platform_release_rollback.sh"
RUNTIME_RESTORE_SCRIPT = REPO_ROOT / "platform/tools/platform_release_restore_runtime.sh"
TRANSACTION_TOOL = REPO_ROOT / "platform/tools/platform_release_transaction.py"
SYSTEMD_STATE_TOOL = REPO_ROOT / "platform/tools/platform_release_systemd_state.py"
STATE_NAME = ".release-operation.json"


class PlatformReleaseRecoveryBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._release_lock = None
        try:
            self._release_lock = lock_support.create_test_lock("recovery")
            self.release_lock_path = self._release_lock.path
            self.tools_dir = self.root / "tools"
            shutil.copytree(REPO_ROOT / "platform/tools", self.tools_dir)
            self.install_test_lock_helper(self.tools_dir / "platform_release_lock.sh")
            self.app_dir = self.root / "platform-app"
            self.releases = self.app_dir / "releases"
            self.shared = self.app_dir / "shared"
            self.releases.mkdir(parents=True)
            self.shared.mkdir()
            (self.shared / ".env.platform").write_text("PLATFORM_TESTING=1\n")
            (self.shared / ".env.platform").chmod(0o600)
        except BaseException:
            if self._release_lock is not None:
                self._release_lock.cleanup()
            self.temp_dir.cleanup()
            raise

    def tearDown(self) -> None:
        try:
            if self._release_lock is not None:
                self._release_lock.cleanup()
        finally:
            self.temp_dir.cleanup()

    def test_resume_activation_committed_cleans_receipt(self) -> None:
        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="activation-committed")
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)

        resume = self.copy_deploy_script_with_fault(
            "deploy-resume-activation-committed.sh",
            None,
            None,
            self.write_fake_systemctl(),
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertNotEqual(previous, candidate)

    def test_retained_recovery_retry_preserves_mixed_state_until_final_cleanup(self) -> None:
        """A post-pointer failure must leave both receipts retryable."""

        current, previous, candidate = self.prepare_install_state(
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "inactive",
                "deadlock-worker.service": "active",
                "deadlock-web.service": "inactive",
                "deadlock-maintenance.timer": "active",
                "deadlock-logrotate.timer": "active",
                "deadlock-offsite-backup.timer": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-health-monitor.timer": "active",
            }
        )
        state = self.shared / STATE_NAME
        systemd_receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(systemd_receipt),
            "--transaction",
            str(state),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        enabled_path = self.root / "systemd-enabled.json"
        interrupted_enabled = json.loads(enabled_path.read_text())
        for unit, value in interrupted_enabled.items():
            if value != "static":
                interrupted_enabled[unit] = "enabled"
        enabled_path.write_text(json.dumps(interrupted_enabled, sort_keys=True))

        failed = self.root / "recover-after-pointer-failure.sh"
        failed.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"{shlex.quote(str(TRANSACTION_TOOL))} recover --retain --state {shlex.quote(str(state))}\n"
            "exit 42\n",
            encoding="utf-8",
        )
        failed.chmod(0o755)
        result = self.run_script(failed, check=False)
        self.assertEqual(result.returncode, 42)
        self.assertTrue(state.is_file())
        self.assertTrue(systemd_receipt.is_file())
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertTrue(candidate.exists())

        # A second attempt restores the recorded mixed active/enabled state,
        # verifies it, and only then performs final receipt/transaction cleanup.
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "restore",
            "--state",
            str(systemd_receipt),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "verify",
            "--state",
            str(systemd_receipt),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "clear",
            "--state",
            str(systemd_receipt),
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
        )
        self.run_transaction("complete-recovery")

        self.assertFalse(systemd_receipt.exists())
        self.assertFalse(state.exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api.service"], "active")
        self.assertEqual(final_state["deadlock-worker.service"], "inactive")
        self.assertEqual(final_state["deadlock-web.service"], "active")
        final_enabled = json.loads(enabled_path.read_text())
        self.assertEqual(final_enabled["deadlock-api.service"], "enabled")
        self.assertEqual(final_enabled["deadlock-offsite-backup.timer"], "disabled")

    def test_abort_after_candidate_nginx_apply_restores_previous_nginx(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="services-restarted")
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)
        nginx_state = self.root / "nginx.state"

        interrupted = self.copy_script_with_replacement(
            DEPLOY_SCRIPT,
            "deploy-after-nginx-apply-kill.sh",
            '  set_phase nginx-pending nginx-applied\n',
            '  /bin/kill -KILL "$$"\n  set_phase nginx-pending nginx-applied\n',
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "candidate",
            },
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(nginx_state.exists(), result.stderr)
        self.assertEqual(nginx_state.read_text(), "candidate\n")
        self.assertEqual(self.state_phase(), "nginx-pending")

        abort = self.copy_abort_script("abort-after-nginx-apply.sh")
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(nginx_state.read_text(), "previous\n")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

    def test_rollback_restores_previous_units_and_nginx_before_completion(self) -> None:
        current = self.add_release("current")
        previous = self.add_release("previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        self.prepare_rollback_venv(current)
        units_state = self.root / "units.state"
        nginx_state = self.root / "nginx.state"
        units_state.write_text("current\n")
        nginx_state.write_text("current\n")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        rollback = self.copy_rollback_with_systemctl(systemctl)

        result = self.run_script(
            rollback,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env={
                "PLATFORM_TEST_UNITS_STATE": str(units_state),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(nginx_state),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(units_state.read_text(), "previous\n")
        self.assertEqual(nginx_state.read_text(), "previous\n")
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)
        self.assertFalse((self.shared / STATE_NAME).exists())

    def test_deploy_faults_after_units_and_restart_resume_to_commit(self) -> None:
        for label, needle in (
            (
                "units",
                "  PLATFORM_ENABLE_SYSTEMD_UNITS=0 run_candidate tools/platform_install_systemd_units.sh\n",
            ),
            (
                "restart",
                '  set_phase activation-pending services-restarted\n',
            ),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                current, _previous, candidate = self.prepare_deploy_state(
                    "activation-pending"
                )
                systemctl = self.write_fake_systemctl()
                interrupted = self.copy_deploy_script_with_fault(
                    f"deploy-{label}-kill.sh",
                    needle,
                    f'  /bin/kill -KILL "$$"\n{needle}',
                    systemctl,
                )
                result = self.run_script(
                    interrupted,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="candidate"),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), "activation-pending")

                resume = self.copy_deploy_script_with_fault(
                    f"deploy-{label}-resume.sh", None, None, systemctl
                )
                result = self.run_script(
                    resume,
                    "--resume",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="candidate"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), candidate)
                self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_deploy_fault_after_smoke_resumes_and_commits(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("nginx-applied")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-smoke-kill.sh",
            '  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n',
            '  /bin/kill -KILL "$$"\n  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "nginx-applied")
        resume = self.copy_deploy_script_with_fault(
            "deploy-smoke-resume.sh", None, None, systemctl
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_deploy_fault_after_activation_commit_resumes_cleanup(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("smoke-passed")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-activation-commit-kill.sh",
            '  set_phase smoke-passed activation-committed\n',
            '  set_phase smoke-passed activation-committed\n  /bin/kill -KILL "$$"\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="candidate"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "activation-committed")
        resume = self.copy_deploy_script_with_fault(
            "deploy-resume-after-activation-commit.sh",
            None,
            None,
            systemctl,
        )
        result = self.run_script(
            resume,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_fault_after_final_receipt_cleanup_leaves_committed_state(self) -> None:
        current, _previous, candidate = self.prepare_deploy_state("activation-committed")
        systemctl = self.write_fake_systemctl()
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-receipt-cleanup-kill.sh",
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"\n',
            '  /usr/bin/python3 -I "$TRANSACTION_TOOL" complete --state "$TRANSACTION_STATE"\n'
            '  /bin/kill -KILL "$$"\n',
            systemctl,
        )
        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_rollback_faults_in_runtime_restore_recover_original_state(self) -> None:
        for label, needle in (
            (
                "units",
                '  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"\n',
            ),
            (
                "nginx",
                '    "$NGINX_TOOL" --apply --reload --json\n',
            ),
        ):
            with self.subTest(label=label):
                self.tearDown()
                self.setUp()
                current, previous = self.prepare_rollback_state()
                runtime = self.copy_runtime_with_fault(
                    f"runtime-{label}-kill.sh",
                    needle,
                    f'{needle}  /bin/kill -KILL "$PPID"\n',
                )
                interrupted = self.copy_rollback_with_runtime(
                    f"rollback-{label}-kill.sh", runtime
                )
                result = self.run_script(
                    interrupted,
                    "--app-dir",
                    str(self.app_dir),
                    "--no-restart",
                    env=self.runtime_env(label="previous"),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state_phase(), "rollback-runtime-pending")
                recovery_runtime = self.write_test_runtime_restore()
                recovery = self.copy_rollback_with_runtime(
                    f"rollback-{label}-recover.sh", recovery_runtime
                )
                result = self.run_script(
                    recovery,
                    "--recover-pending",
                    "--app-dir",
                    str(self.app_dir),
                    env=self.runtime_env(label="current"),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertEqual((self.app_dir / "current").resolve(), current)
                self.assertEqual((self.app_dir / "previous").resolve(), previous)
                self.assertEqual(
                    (self.shared / "venv" / "deps-version").read_text(), "new\n"
                )

    def test_pointer_switch_crash_recovers_through_old_current_helper(self) -> None:
        current, previous = self.prepare_rollback_state()
        legacy_helper = previous / "tools/platform_release_rollback.sh"
        legacy_helper.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' legacy-helper-used > \"$PLATFORM_LEGACY_HELPER_STATE\"\n"
            "exit 77\n"
        )
        legacy_helper.chmod(0o755)
        legacy_state = self.root / "legacy-helper.state"
        runtime = self.copy_runtime_with_fault(
            "runtime-after-pointer-switch-kill.sh",
            '  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"\n',
            '  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"\n'
            '  /bin/kill -KILL "$PPID"\n',
        )
        interrupted = self.copy_rollback_with_runtime(
            "rollback-after-pointer-switch-kill.sh", runtime
        )

        result = self.run_script(
            interrupted,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env={
                **self.runtime_env(label="previous"),
                "PLATFORM_LEGACY_HELPER_STATE": str(legacy_state),
            },
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "rollback-runtime-pending")
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertIn(
            "RECOVERY_DIR=\"$SHARED_DIR/.release-recovery\"",
            (previous / "tools/platform_release_rollback.sh").read_text(),
        )
        # The production recovery bundle uses the fixed systemctl path.  This
        # test substitutes the systemd boundary inside the private fixture so
        # the old-current recovery path exercises the same receipt checks
        # without touching the host service manager.
        recovery_bundle = self.shared / ".release-recovery/platform_release_rollback.sh"
        recovery_bundle.write_text(
            recovery_bundle.read_text().replace(
                "/usr/bin/systemctl", str(self.root / "systemctl")
            )
        )
        recovery_bundle.chmod(0o755)
        recovery_runtime = self.write_test_runtime_restore()
        shutil.copy2(
            recovery_runtime,
            self.shared / ".release-recovery/platform_release_restore_runtime.sh",
        )

        recovery = self.app_dir / "current/tools/platform_release_rollback.sh"
        result = self.run_script(
            recovery,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="current"),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertFalse(legacy_state.exists())
        self.assertEqual(
            (self.shared / "venv" / "deps-version").read_text(), "new\n"
        )

    def test_migration_failure_restores_only_pre_active_services_and_retains_receipt(
        self,
    ) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        migration = candidate / "tools/platform_run_alembic.sh"
        migration.write_text("#!/usr/bin/env bash\nexit 42\n")
        migration.chmod(0o755)

        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        deploy = self.copy_deploy_migration_script(systemctl)
        result = self.run_script(
            deploy,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "migration-failed")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        migration_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(migration_state["deadlock-api"], "active")
        self.assertEqual(migration_state["deadlock-worker"], "inactive")
        self.assertEqual(migration_state["deadlock-web"], "inactive")
        self.assertEqual(
            migration_state["deadlock-cloudflare-ips.timer"], "inactive"
        )
        self.assertFalse((self.shared / ".release-quiesce.json").exists())

        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api"], "active")
        self.assertEqual(final_state["deadlock-worker"], "inactive")
        self.assertEqual(final_state["deadlock-web"], "inactive")
        log = (self.root / "systemctl.log").read_text()
        self.assertIn("restart deadlock-api", log)
        self.assertNotIn("restart deadlock-worker", log)
        self.assertNotIn("restart deadlock-web", log)

    def test_sigkill_after_snapshot_leaves_abortable_receipt_without_transaction(
        self,
    ) -> None:
        current, _previous, _candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        (self.shared / STATE_NAME).unlink()
        artifact = self.root / "release.tar.gz"
        artifact.write_bytes(b"not reached")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        interrupted = self.copy_initial_deploy_with_fault(
            "deploy-after-snapshot-kill.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            '  /bin/kill -KILL "$$"\n'
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
        )
        result = self.run_script(
            interrupted,
            "--artifact",
            str(artifact),
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertEqual(self.state_phase(), "quiesce-pending")
        self.assertFalse((self.shared / ".release-quiesce.json").exists())
        self.assertTrue(
            all(
                value == "inactive"
                for value in json.loads((self.root / "systemd-state.json").read_text()).values()
            )
        )

        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api"], "active")
        self.assertEqual(final_state["deadlock-worker"], "inactive")
        self.assertEqual(final_state["deadlock-web"], "active")
        self.assertEqual(final_state["deadlock-cloudflare-ips.timer"], "inactive")

    def test_stage_failure_recovers_pre_active_services_without_swallowing_failure(
        self,
    ) -> None:
        current, _previous, _candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        (self.shared / STATE_NAME).unlink()
        artifact = self.root / "stage-failure.tar.gz"
        artifact.write_bytes(b"not reached")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        failed = self.copy_initial_deploy_with_fault(
            "deploy-stage-failure.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            '  install -d -o root -g root -m 0755 "$APP_DIR/releases/stage-failure"\n'
            "  /bin/false\n",
        )
        result = self.run_script(
            failed,
            "--artifact",
            str(artifact),
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.releases / "stage-failure").exists())
        final_state = json.loads((self.root / "systemd-state.json").read_text())
        self.assertEqual(final_state["deadlock-api"], "active")
        self.assertEqual(final_state["deadlock-worker"], "inactive")
        self.assertEqual(final_state["deadlock-web"], "active")
        self.assertEqual(final_state["deadlock-cloudflare-ips.timer"], "active")

    def test_deploy_lock_contention_blocks_preflight_and_quiesce(self) -> None:
        current, _previous, _candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        (self.shared / STATE_NAME).unlink()
        artifact = self.root / "lock-contention.tar.gz"
        artifact.write_bytes(b"not reached")
        initial_states = {
            "deadlock-api": "active",
            "deadlock-worker": "inactive",
            "deadlock-web": "active",
            "deadlock-cloudflare-ips.timer": "active",
            "deadlock-cloudflare-ips.service": "inactive",
        }
        systemctl = self.write_stateful_systemctl(initial_states)
        failed = self.copy_initial_deploy_with_fault(
            "deploy-lock-contention.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            "  /bin/false\n",
        )
        assert self._release_lock is not None
        try:
            self._release_lock.acquire(nonblocking=True)
            result = self.run_script(
                failed,
                "--artifact",
                str(artifact),
                "--app-dir",
                str(self.app_dir),
                check=False,
            )
        finally:
            self._release_lock.release()

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text()),
            initial_states,
        )
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_killed_release_body_cannot_leave_a_background_child_holding_lock(
        self,
    ) -> None:
        """The lock supervisor must close the descriptor before body children run."""
        helper = self.root / "platform_release_lock.sh"
        self.install_test_lock_helper(helper)
        child_pid_file = self.root / "child.pid"
        script = self.root / "lock-body-kill.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            f"source {shlex.quote(str(helper))}\n"
            "ORIGINAL_ARGS=(\"$@\")\n"
            "platform_release_lock_supervise \"${ORIGINAL_ARGS[@]}\"\n"
            "[[ \"${PLATFORM_RELEASE_LOCK_SUPERVISED:-}\" == 1 ]] || exit 0\n"
            "platform_release_lock_open\n"
            f"/bin/sleep 30 >/dev/null 2>&1 </dev/null & child=$!; printf '%s\\n' \"$child\" > {shlex.quote(str(child_pid_file))}\n"
            "/bin/kill -KILL \"$$\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        env = {
            "PLATFORM_ENVIRONMENT": "test",
            "PLATFORM_TESTING": "1",
        }
        child_pid = None
        try:
            result = self.run_script(script, env=env, check=False)
            self.assertNotEqual(result.returncode, 0)
            child_pid = int(child_pid_file.read_text(encoding="ascii").strip())
            assert self._release_lock is not None
            self._release_lock.acquire(nonblocking=True)
            self._release_lock.release()
        finally:
            if child_pid is not None:
                try:
                    command_line = Path(f"/proc/{child_pid}/cmdline").read_bytes()
                except OSError:
                    command_line = b""
                if b"sleep" in command_line:
                    try:
                        os.kill(child_pid, 15)
                    except ProcessLookupError:
                        pass

    def test_inherited_release_fd_is_rejected_without_root_path_or_body(self) -> None:
        helper = self.root / "platform_release_lock.sh"
        self.install_test_lock_helper(helper)
        child_pid_file = self.root / "inherited-child.pid"
        script = self.root / "inherited-lock-body-kill.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -Eeuo pipefail\n"
            f"source {shlex.quote(str(helper))}\n"
            "ORIGINAL_ARGS=(\"$@\")\n"
            "platform_release_lock_supervise \"${ORIGINAL_ARGS[@]}\"\n"
            "[[ \"${PLATFORM_RELEASE_LOCK_SUPERVISED:-}\" == 1 ]] || exit 0\n"
            "platform_release_lock_open\n"
            f"/bin/sleep 30 >/dev/null 2>&1 </dev/null & child=$!; printf '%s\\n' \"$child\" > {shlex.quote(str(child_pid_file))}\n"
            "/bin/kill -KILL \"$$\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        env = {"PLATFORM_ENVIRONMENT": "test", "PLATFORM_TESTING": "1"}
        assert self._release_lock is not None
        lock_fd = self._release_lock.fd
        child_pid = None
        try:
            self._release_lock.acquire(nonblocking=True)
            env["PLATFORM_RELEASE_LOCK_FD"] = str(lock_fd)
            result = subprocess.run(
                [str(script)],
                cwd=REPO_ROOT,
                env={**os.environ, **env},
                pass_fds=(lock_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse(child_pid_file.exists(), result.stderr)
        finally:
            self._release_lock.release()
        try:
            self._release_lock.acquire(nonblocking=True)
            self._release_lock.release()
        finally:
            if child_pid is not None:
                try:
                    command_line = Path(f"/proc/{child_pid}/cmdline").read_bytes()
                except OSError:
                    command_line = b""
                if b"sleep" in command_line:
                    try:
                        os.kill(child_pid, 15)
                    except ProcessLookupError:
                        pass

    def test_abort_restart_failure_retains_receipt_and_repeated_abort_recovers(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-failed"
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
                "PLATFORM_TEST_SYSTEMCTL_FAIL_RESTART": "1",
            },
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertTrue((self.shared / STATE_NAME).exists())

        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            env={
                "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
                "PLATFORM_TEST_UNITS_LABEL": "previous",
                "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

    def test_abort_refuses_release_identity_drift_before_service_restart(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=active",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-failed"
        )
        moved = self.releases / "current-moved"
        current.rename(moved)
        replacement = self.releases / "current"
        replacement.mkdir()
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        result = self.run_script(
            abort,
            "--abort-retained",
            "--confirm-migration-not-reversed",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertNotIn("restart", (self.root / "systemctl.log").read_text())

    def test_abort_refuses_lock_contention_before_recovery_or_restart(self) -> None:
        current, _previous, candidate = self.prepare_install_state(
            with_fake_python=True,
            service_state_required=True,
        )
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase="staged")
        self.run_transaction(
            "record-services",
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=active",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-failed"
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        abort = self.copy_abort_script_with_systemctl(systemctl)
        assert self._release_lock is not None
        try:
            self._release_lock.acquire(nonblocking=True)
            result = self.run_script(
                abort,
                "--abort-retained",
                "--confirm-migration-not-reversed",
                "--app-dir",
                str(self.app_dir),
                check=False,
            )
        finally:
            self._release_lock.release()

        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.state_phase(), "migration-failed")
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def prepare_deploy_state(self, phase: str) -> tuple[Path, Path, Path]:
        current, previous, candidate = self.prepare_install_state(
            with_fake_python=True
        )
        self.add_runtime_stubs(candidate)
        self.advance_install_state(candidate, current, phase=phase)
        self.switch_pointer("previous", current)
        self.switch_pointer("current", candidate)
        return current, previous, candidate

    def prepare_rollback_state(self) -> tuple[Path, Path]:
        current = self.add_release("rollback-current")
        previous = self.add_release("rollback-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        self.add_fake_venv(self.shared / "venv", marker="new")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        shutil.copytree(self.shared / "venv", snapshot)
        (rollback / "previous-release").write_text(f"{previous}\n")
        (rollback / "previous-release").chmod(0o600)
        (rollback / "venv-transition").write_text("snapshot\n")
        (rollback / "venv-transition").chmod(0o600)
        return current, previous

    def runtime_env(self, *, label: str) -> dict[str, str]:
        return {
            "PLATFORM_TEST_UNITS_STATE": str(self.root / "units.state"),
            "PLATFORM_TEST_UNITS_LABEL": label,
            "PLATFORM_TEST_NGINX_STATE": str(self.root / "nginx.state"),
            "PLATFORM_TEST_NGINX_LABEL": label,
        }

    def write_fake_systemctl(self) -> Path:
        path = self.root / "systemctl"
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
        path.chmod(0o755)
        return path

    def copy_deploy_script_with_fault(
        self,
        name: str,
        needle: str | None,
        replacement: str | None,
        systemctl: Path,
    ) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        script = script.replace(
            "  release_preflight\n  set_phase nginx-applied smoke-passed\n",
            "  /usr/bin/true\n  set_phase nginx-applied smoke-passed\n",
            1,
        )
        if needle is not None:
            self.assertIn(needle, script)
            assert replacement is not None
            script = script.replace(needle, replacement, 1)
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_runtime_with_fault(
        self, name: str, needle: str, replacement: str
    ) -> Path:
        script = RUNTIME_RESTORE_SCRIPT.read_text()
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement, 1))
        target.chmod(0o755)
        self.install_test_lock_helper(target.parent / "platform_release_lock.sh")
        shutil.copy2(
            REPO_ROOT / "platform/tools/platform_release_systemd_state.py",
            target.parent / "platform_release_systemd_state.py",
        )
        (target.parent / "platform_release_systemd_state.py").chmod(0o755)
        return target

    def copy_rollback_with_runtime(self, name: str, runtime: Path) -> Path:
        script = self.script_with_physical_tools(ROLLBACK_SCRIPT)
        needle = 'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"'
        self.assertIn(needle, script)
        script = script.replace(needle, f'RUNTIME_RESTORE_TOOL="{runtime}"', 1)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_rollback_with_systemctl(self, systemctl: Path) -> Path:
        script = self.script_with_physical_tools(ROLLBACK_SCRIPT)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        target = self.root / "rollback-with-stateful-systemctl.sh"
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_test_runtime_restore(self) -> Path:
        path = self.root / "runtime-restore.sh"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "APP_DIR=\"\"; RELEASE=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    --app-dir) APP_DIR=\"$2\"; shift 2 ;;\n"
            "    --release) RELEASE=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "PLATFORM_APP_DIR=\"$APP_DIR\" \"$RELEASE/tools/platform_install_systemd_units.sh\"\n"
            "\"$APP_DIR/shared/venv/bin/python\" \"$RELEASE/tools/platform_install_nginx.py\" --apply\n"
        )
        path.chmod(0o755)
        return path

    def prepare_install_state(
        self,
        *,
        with_fake_python: bool = False,
        service_state_required: bool = False,
    ) -> tuple[Path, Path, Path]:
        current = self.add_release("current")
        previous = self.add_release("previous")
        candidate = self.add_release("candidate")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        (candidate / ".rollback").mkdir()
        freeze = candidate / "requirements-platform.freeze.txt"
        freeze.write_text("pip==test\n")
        freeze.chmod(0o444)
        (candidate / ".rollback" / "previous-release").write_text(f"{current}\n")
        (candidate / ".rollback" / "previous-release").chmod(0o600)
        (candidate / ".rollback" / "venv-transition").write_text("unchanged\n")
        (candidate / ".rollback" / "venv-transition").chmod(0o600)
        digest = hashlib.sha256(freeze.read_bytes()).hexdigest()
        (candidate / ".rollback" / "shared-freeze.sha256").write_text(f"{digest}\n")
        (candidate / ".rollback" / "shared-freeze.sha256").chmod(0o600)
        self.add_fake_venv(self.shared / "venv", marker="shared")
        if with_fake_python:
            self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_transaction(
            candidate,
            current,
            previous,
            service_state_required=service_state_required,
        )
        if not service_state_required:
            self.run_transaction(
                "phase", "--expected", "prepared", "--phase", "venv-transitioned"
            )
            self.run_transaction(
                "phase", "--expected", "venv-transitioned", "--phase", "staged"
            )
            self.run_transaction(
                "record-services",
                "--service-state",
                "deadlock-api=active",
                "--service-state",
                "deadlock-worker=active",
                "--service-state",
                "deadlock-web=active",
                "--timer-active-before",
                "active",
            )
        return current, previous, candidate

    def advance_install_state(self, candidate: Path, current: Path, *, phase: str) -> None:
        phases = (
            "venv-transitioned",
            "staged",
            "migration-pending",
            "migration-applied",
            "activation-pending",
            "services-restarted",
            "nginx-applied",
            "smoke-passed",
            "activation-committed",
        )
        current_phase = self.state_phase()
        if current_phase in phases:
            phases = phases[phases.index(current_phase) + 1 :]
        for next_phase in phases:
            self.run_transaction("phase", "--expected", self.state_phase(), "--phase", next_phase)
            if next_phase == phase:
                break

    def create_transaction(
        self,
        candidate: Path,
        current: Path,
        previous: Path,
        *,
        service_state_required: bool = False,
    ) -> None:
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            str(current),
            "--previous-before",
            str(previous),
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(self.shared / ".venv-install-candidate.0000"),
            "--snapshot",
            str(candidate / ".rollback" / "shared-venv-before-install"),
            "--transition",
            "none",
        )

    def prepare_rollback_venv(self, current: Path) -> None:
        self.add_fake_venv(self.shared / "venv", marker="new")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        shutil.copytree(self.shared / "venv", snapshot)
        (rollback / "previous-release").write_text(f"{self.releases / 'previous'}\n")
        (rollback / "previous-release").chmod(0o600)
        (rollback / "venv-transition").write_text("snapshot\n")
        (rollback / "venv-transition").chmod(0o600)

    def add_runtime_stubs(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir(exist_ok=True)
        units = tools / "platform_install_systemd_units.sh"
        units.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$PLATFORM_TEST_UNITS_LABEL\" > \"$PLATFORM_TEST_UNITS_STATE\"\n"
        )
        units.chmod(0o755)
        (tools / "platform_install_nginx.py").write_text("# test stub\n")
        (tools / "platform_deploy_smoke.py").write_text("# test stub\n")
        runtime_installer = tools / "platform_live_qa_runtime_install.py"
        runtime_installer.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
        runtime_installer.chmod(0o755)
    def add_release(self, name: str) -> Path:
        release = self.releases / name
        release.mkdir()
        return release

    def add_fake_venv(self, venv: Path, *, marker: str) -> None:
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {marker!r}\n")
        python.chmod(0o755)
        (venv / "deps-version").write_text(f"{marker}\n")

    def write_fake_python(self, path: Path) -> None:
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  *platform_install_nginx.py)\n"
            "    if [[ \"${2:-}\" == \"--apply\" ]]; then\n"
            "      printf '%s\\n' \"$PLATFORM_TEST_NGINX_LABEL\" > \"$PLATFORM_TEST_NGINX_STATE\"\n"
            "    fi\n"
            "    ;;\n"
            "esac\n"
        )
        path.chmod(0o755)

    def switch_pointer(self, name: str, target: Path) -> None:
        pointer = self.app_dir / name
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(target)

    def state_phase(self) -> str:
        import json

        return json.loads((self.shared / STATE_NAME).read_text())["phase"]

    def run_transaction(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run_script(
            TRANSACTION_TOOL,
            *args,
            "--state",
            str(self.shared / STATE_NAME),
        )

    def copy_script_with_replacement(
        self, source: Path, name: str, needle: str, replacement: str
    ) -> Path:
        script = self.script_with_physical_tools(source)
        self.assertIn(needle, script)
        target = self.root / name
        target.write_text(script.replace(needle, replacement, 1))
        target.chmod(0o755)
        return target

    def copy_abort_script(self, name: str) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "active",
                "deadlock-worker": "active",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
                "deadlock-cloudflare-ips.service": "inactive",
            }
        )
        runtime_restore = self.root / "test-runtime-restore.sh"
        runtime_restore.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "APP_DIR=\"\"\n"
            "RELEASE=\"\"\n"
            "while [[ $# -gt 0 ]]; do\n"
            "  case \"$1\" in\n"
            "    --app-dir) APP_DIR=\"$2\"; shift 2 ;;\n"
            "    --release) RELEASE=\"$2\"; shift 2 ;;\n"
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            "PLATFORM_APP_DIR=\"$APP_DIR\" \"$RELEASE/tools/platform_install_systemd_units.sh\"\n"
            "\"$APP_DIR/shared/venv/bin/python\" \"$RELEASE/tools/platform_install_nginx.py\" --apply\n"
        )
        runtime_restore.chmod(0o755)
        script = script.replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{runtime_restore}"',
            1,
        )
        script = script.replace(
            'PLATFORM_APP_DIR="$APP_DIR" "$APP_DIR/current/tools/platform_install_systemd_units.sh"',
            "/usr/bin/true",
        )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_abort_script_with_systemctl(self, systemctl: Path) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        runtime_restore = self.write_test_runtime_restore()
        script = script.replace(
            'RUNTIME_RESTORE_TOOL="$TOOLS_DIR/platform_release_restore_runtime.sh"',
            f'RUNTIME_RESTORE_TOOL="{runtime_restore}"',
            1,
        )
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / "abort-with-stateful-systemctl.sh"
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_deploy_migration_script(self, systemctl: Path) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        preflight = '''release_preflight() {
  "$TOOLS_DIR/platform_release_preflight.sh" \\
    --app-dir "$APP_DIR" \\
    --require-previous \\
    --require-verified-backup \\
    --require-edge-parity \\
    --backup-max-age-hours 24
}
'''
        self.assertIn(preflight, script)
        script = script.replace(preflight, "release_preflight() { /usr/bin/true; }\n", 1)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / "deploy-migration-failure.sh"
        target.write_text(script)
        target.chmod(0o755)
        return target

    def copy_initial_deploy_with_fault(
        self,
        name: str,
        systemctl: Path,
        needle: str,
        replacement: str,
    ) -> Path:
        script = self.script_with_physical_tools(DEPLOY_SCRIPT)
        preflight = '''release_preflight() {
  "$TOOLS_DIR/platform_release_preflight.sh" \\
    --app-dir "$APP_DIR" \\
    --require-previous \\
    --require-verified-backup \\
    --require-edge-parity \\
    --backup-max-age-hours 24
}
'''
        self.assertIn(preflight, script)
        script = script.replace(preflight, "release_preflight() { /usr/bin/true; }\n", 1)
        self.assertIn(needle, script)
        script = script.replace(needle, replacement, 1)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        script = script.replace("/usr/bin/curl", "/usr/bin/true")
        target = self.root / name
        target.write_text(script)
        target.chmod(0o755)
        return target

    def write_stateful_systemctl(self, states: dict[str, str]) -> Path:
        state_path = self.root / "systemd-state.json"
        state_path.write_text(json.dumps(states, sort_keys=True))
        enabled_path = self.root / "systemd-enabled.json"
        owned_units = (
            "deadlock-api.service",
            "deadlock-worker.service",
            "deadlock-web.service",
            "deadlock-maintenance.service",
            "deadlock-maintenance.timer",
            "deadlock-logrotate.service",
            "deadlock-logrotate.timer",
            "deadlock-offsite-backup.service",
            "deadlock-offsite-backup.timer",
            "deadlock-cloudflare-ips.service",
            "deadlock-cloudflare-ips.timer",
            "deadlock-health-monitor.service",
            "deadlock-health-monitor.timer",
        )
        static_units = {
            "deadlock-maintenance.service",
            "deadlock-logrotate.service",
            "deadlock-offsite-backup.service",
            "deadlock-cloudflare-ips.service",
            "deadlock-health-monitor.service",
        }
        enabled_path.write_text(
            json.dumps(
                {
                    unit: (
                        "static"
                        if unit in static_units
                        else "disabled"
                        if unit == "deadlock-offsite-backup.timer"
                        else "enabled"
                    )
                    for unit in owned_units
                },
                sort_keys=True,
            )
        )
        log_path = self.root / "systemctl.log"
        log_path.write_text("")
        path = self.root / "systemctl"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"state_path = Path({str(state_path)!r})\n"
            f"enabled_path = Path({str(enabled_path)!r})\n"
            f"log_path = Path({str(log_path)!r})\n"
            "state = json.loads(state_path.read_text())\n"
            "enabled = json.loads(enabled_path.read_text())\n"
            "argv = sys.argv[1:]\n"
            "action = argv[0] if argv else \"\"\n"
            "units = [value for value in argv[1:] if not value.startswith(\"-\")]\n"
            "if action == \"is-active\":\n"
            "    unit = units[0]\n"
            "    value = state.get(unit, \"inactive\")\n"
            "    if \"--quiet\" not in argv:\n"
            "        print(value)\n"
            "    raise SystemExit(0 if value == \"active\" else 3)\n"
            "if action == \"is-enabled\":\n"
            "    unit = units[0]\n"
            "    value = enabled.get(unit, \"static\")\n"
            "    if \"--quiet\" not in argv:\n"
            "        print(value)\n"
            "    raise SystemExit(0 if value == \"enabled\" else 1)\n"
            "if action in {\"enable\", \"disable\"}:\n"
            "    value = \"enabled\" if action == \"enable\" else \"disabled\"\n"
            "    for unit in units:\n"
            "        if enabled.get(unit) != \"static\":\n"
            "            enabled[unit] = value\n"
            "        with log_path.open(\"a\", encoding=\"utf-8\") as stream:\n"
            "            stream.write(f\"{action} {unit}\\n\")\n"
            "    enabled_path.write_text(json.dumps(enabled, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if action in {\"stop\", \"start\", \"restart\"}:\n"
            "    for unit in units:\n"
                "        with log_path.open(\"a\", encoding=\"utf-8\") as stream:\n"
                "            stream.write(f\"{action} {unit}\\n\")\n"
                "        if action == \"restart\" and unit == \"deadlock-api\" and os.getenv(\"PLATFORM_TEST_SYSTEMCTL_FAIL_RESTART\") == \"1\":\n"
                "            raise SystemExit(1)\n"
            "        if unit in state:\n"
            "            state[unit] = \"inactive\" if action == \"stop\" else \"active\"\n"
            "    state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if action in {\"daemon-reload\", \"reload\"}:\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n"
        )
        path.chmod(0o755)
        return path

    def script_with_physical_tools(self, source: Path) -> str:
        script = source.read_text()
        needle = 'TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"'
        self.assertIn(needle, script)
        return script.replace(needle, f'TOOLS_DIR="{self.tools_dir}"', 1)

    def install_test_lock_helper(self, destination: Path) -> None:
        """Install a test-local helper while retaining production validation."""

        helper = (REPO_ROOT / "platform/tools/platform_release_lock.sh").read_text(
            encoding="utf-8"
        )
        helper = helper.replace(
            "/run/lock/oldsparky-platform-release.lock",
            str(self.release_lock_path),
        )
        destination.write_text(helper, encoding="utf-8")
        destination.chmod(0o755)

    def run_script(
        self,
        script: Path,
        *args: str,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env["PLATFORM_ENVIRONMENT"] = "test"
        command_env["PLATFORM_TESTING"] = "1"
        command_env.update(env or {})
        result = subprocess.run(
            [str(script), *args],
            cwd=REPO_ROOT,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"{script} failed: {result.returncode}\n{result.stderr}")
        return result


if __name__ == "__main__":
    unittest.main()
