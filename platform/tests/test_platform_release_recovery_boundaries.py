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
from unittest import mock

from tests import platform_test_lock_support as lock_support
from tools import platform_recovery_bootstrap as recovery
from tools import platform_release_transaction as transaction


REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SCRIPT = REPO_ROOT / "platform/tools/platform_release_deploy.sh"
ROLLBACK_SCRIPT = REPO_ROOT / "platform/tools/platform_release_rollback.sh"
ALEMBIC_SCRIPT = REPO_ROOT / "platform/tools/platform_run_alembic.sh"
RUNTIME_RESTORE_SCRIPT = REPO_ROOT / "platform/tools/platform_release_restore_runtime.sh"
RECOVERY_WRAPPER = REPO_ROOT / "platform/tools/platform_recover_pending.sh"
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

    def test_immutable_recovery_wrapper_first_install_avoids_systemd_and_current_helpers(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(None, None, "first-install")
        systemctl, systemctl_log = self.write_failing_systemctl("first-install")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertFalse((self.app_dir / "current").exists())
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertFalse(candidate.exists())

    def test_immutable_recovery_wrapper_first_install_validates_but_does_not_use_snapshot(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(
            None,
            None,
            "first-install-complete-snapshot",
            complete_snapshot=True,
        )
        systemctl, systemctl_log = self.write_failing_systemctl(
            "first-install-complete-snapshot"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_operationless_first_install_quiesce_v1_v2_is_systemd_free(self) -> None:
        """Pre-promotion first-install receipts are validated as no-op cleanup."""

        for version in (2, 1):
            with self.subTest(version=version):
                if version == 1:
                    self.tearDown()
                    self.setUp()
                generation = self.install_recovery_generation()
                candidate = self.releases / f"quiesce-first-install-v{version}"
                self.run_transaction(
                    "prepare-quiesce",
                    "--app-dir",
                    str(self.app_dir),
                    "--candidate-release",
                    str(candidate),
                    "--service-state",
                    "deadlock-api=inactive",
                    "--service-state",
                    "deadlock-worker=inactive",
                    "--service-state",
                    "deadlock-web=inactive",
                    "--timer-active-before",
                    "inactive",
                    "--service-enabled",
                    "deadlock-api=disabled",
                    "--service-enabled",
                    "deadlock-worker=disabled",
                    "--service-enabled",
                    "deadlock-web=disabled",
                    "--timer-enabled-before",
                    "disabled",
                )
                receipt = self.shared / STATE_NAME
                if version == 1:
                    record = json.loads(receipt.read_text(encoding="ascii"))
                    record["version"] = 1
                    record.pop("service_enabled_before")
                    record.pop("timer_enabled_before")
                    receipt.write_text(
                        json.dumps(record, sort_keys=True) + "\n", encoding="ascii"
                    )
                    receipt.chmod(0o600)
                systemctl, systemctl_log = self.write_failing_systemctl(
                    f"quiesce-first-install-v{version}"
                )
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(systemctl_log.exists())
                self.assertFalse(receipt.exists())
                self.assertFalse(candidate.exists())

    def test_operationless_first_install_quiesce_noninactive_fails_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.releases / "quiesce-first-install-active"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=inactive",
            "--timer-active-before",
            "inactive",
            "--service-enabled",
            "deadlock-api=disabled",
            "--service-enabled",
            "deadlock-worker=disabled",
            "--service-enabled",
            "deadlock-web=disabled",
            "--timer-enabled-before",
            "disabled",
        )
        systemctl, systemctl_log = self.write_failing_systemctl(
            "quiesce-first-install-active"
        )
        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_immutable_recovery_wrapper_current_only_avoids_systemd_and_tampered_helpers(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("current-only-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_current_control_helper_bombs(current)
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "current-only",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertFalse(candidate.exists())
        self.assertFalse((self.root / "current-helper-used").exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text()),
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
            },
        )

    def test_immutable_recovery_wrapper_current_only_accepts_second_pointer_window(
        self,
    ) -> None:
        """A kill after current moves but before its phase marker is recoverable."""

        generation = self.install_recovery_generation()
        current = self.add_release("current-only-pointer-window-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "current-only-pointer-window",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        # Reproduce the exact durable window: previous has moved, current has
        # moved, but the transaction still carries previous-switched.
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-applied"
        )
        self.run_transaction(
            "phase", "--expected", "migration-applied", "--phase", "previous-switched"
        )
        (self.app_dir / "current").unlink()
        (self.app_dir / "current").symlink_to(candidate)
        (self.app_dir / "previous").symlink_to(current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "inactive",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertEqual(
            json.loads((self.root / "systemd-state.json").read_text()),
            {
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
                "deadlock-cloudflare-ips.timer": "active",
            },
        )

    def test_immutable_recovery_wrapper_first_install_after_current_switch_is_systemd_free(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(
            None,
            None,
            "first-install-pointer-window",
            complete_snapshot=True,
        )
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")
        self.run_transaction(
            "phase", "--expected", "migration-pending", "--phase", "migration-applied"
        )
        self.run_transaction(
            "phase", "--expected", "migration-applied", "--phase", "current-switched"
        )
        (self.app_dir / "current").symlink_to(candidate)
        systemctl, systemctl_log = self.write_failing_systemctl(
            "first-install-pointer-window"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertFalse((self.app_dir / "current").exists())
        self.assertFalse((self.app_dir / "previous").exists())

    def test_immutable_recovery_wrapper_current_only_rejects_missing_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("current-only-missing-snapshot-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current, None, "current-only-missing-snapshot"
        )
        systemctl, systemctl_log = self.write_failing_systemctl(
            "current-only-missing-snapshot"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(candidate.exists())
        self.assertFalse(systemctl_log.exists())

    def test_immutable_recovery_wrapper_current_only_rejects_partial_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("current-only-partial-snapshot-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "current-only-partial-snapshot",
            complete_snapshot=True,
            service_states={
                "deadlock-api": "active",
                "deadlock-worker": "inactive",
                "deadlock-web": "active",
            },
            timer_active=True,
        )
        state = self.shared / STATE_NAME
        record = json.loads(state.read_text(encoding="ascii"))
        del record["service_state_before"]["deadlock-web"]
        state.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
        state.chmod(0o600)
        systemctl, systemctl_log = self.write_failing_systemctl(
            "current-only-partial-snapshot"
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state.exists())
        self.assertTrue(candidate.exists())
        self.assertFalse(systemctl_log.exists())

    def test_immutable_recovery_wrapper_current_only_replays_after_each_partial_service_restore(
        self,
    ) -> None:
        cases = (
            ("restart deadlock-api", "deadlock-api", "active"),
            ("stop deadlock-worker", "deadlock-worker", "inactive"),
            ("restart deadlock-web", "deadlock-web", "active"),
            (
                "start deadlock-cloudflare-ips.timer",
                "deadlock-cloudflare-ips.timer",
                "active",
            ),
        )
        for index, (kill_after, unit, expected_after_kill) in enumerate(cases):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(kill_after=kill_after):
                generation = self.install_recovery_generation()
                current = self.add_release(f"current-only-retry-{index}-current")
                (self.app_dir / "current").symlink_to(current)
                candidate = self.create_wrapper_transaction(
                    current,
                    None,
                    f"current-only-retry-{index}",
                    complete_snapshot=True,
                    service_states={
                        "deadlock-api": "active",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "active",
                    },
                    timer_active=True,
                )
                initial_state = {
                    "deadlock-api": "inactive",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "inactive",
                    "deadlock-cloudflare-ips.timer": "inactive",
                }
                systemctl = self.write_stateful_systemctl(initial_state)
                first = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    env={"PLATFORM_TEST_SYSTEMCTL_KILL_AFTER": kill_after},
                    check=False,
                )
                self.assertNotEqual(first.returncode, 0)
                self.assertTrue((self.shared / STATE_NAME).exists())
                self.assertFalse((self.shared / ".release-systemd-state.json").exists())
                self.assertTrue(candidate.exists())
                self.assertEqual(
                    self.state_phase(), "filesystem-restored-services-pending"
                )
                partial_state = json.loads(
                    (self.root / "systemd-state.json").read_text()
                )
                self.assertEqual(partial_state[unit], expected_after_kill)
                self.assertTrue(
                    (self.shared / STATE_NAME).exists(),
                    "receipt must survive a crash before service restoration completes",
                )

                retry = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse((self.shared / ".release-systemd-state.json").exists())
                self.assertFalse(candidate.exists())
                self.assertEqual(
                    json.loads((self.root / "systemd-state.json").read_text()),
                    {
                        "deadlock-api": "active",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "active",
                        "deadlock-cloudflare-ips.timer": "active",
                    },
                )

    def test_immutable_recovery_wrapper_recovers_operationless_quiesce_receipts(
        self,
    ) -> None:
        """A SIGKILL before promotion restores the exact immutable snapshot."""

        for index, topology in enumerate(("current-only", "upgrade")):
            if index:
                self.tearDown()
                self.setUp()
            generation = self.install_recovery_generation()
            current = self.add_release(f"quiesce-{topology}-current")
            (self.app_dir / "current").symlink_to(current)
            previous = None
            if topology == "upgrade":
                previous = self.add_release("quiesce-upgrade-previous")
                (self.app_dir / "previous").symlink_to(previous)
            self.add_current_control_helper_bombs(current)
            systemctl = self.write_stateful_systemctl(
                {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                    "deadlock-cloudflare-ips.timer": "active",
                    "deadlock-cloudflare-ips.service": "inactive",
                }
            )
            artifact = self.root / f"quiesce-{topology}.tar.gz"
            artifact.write_bytes(b"not reached after the quiesce receipt")
            interrupted = self.copy_initial_deploy_with_fault(
                f"quiesce-{topology}-kill.sh",
                systemctl,
                "  quiesce_runtime_writers\n",
                '  quiesce_runtime_writers\n  /bin/kill -KILL "$$"\n',
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
            receipt = json.loads((self.shared / STATE_NAME).read_text())
            self.assertEqual(receipt["phase"], "quiesce-pending")
            self.assertNotIn("operation_id", receipt)
            self.assertEqual(
                receipt["service_state_before"],
                {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                },
            )
            self.assertEqual(receipt["timer_active_before"], True)
            self.assertFalse((self.shared / ".release-systemd-state.json").exists())
            self.assertEqual(
                json.loads((self.root / "systemd-state.json").read_text())[
                    "deadlock-api"
                ],
                "inactive",
            )
            (self.root / "systemctl.log").write_text("")

            result = self.run_script(
                generation / RECOVERY_WRAPPER.name,
                "--app-dir",
                str(self.app_dir),
                "--systemctl",
                str(systemctl),
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.shared / STATE_NAME).exists())
            self.assertFalse((self.shared / ".release-quiesce.json").exists())
            self.assertEqual(
                json.loads((self.root / "systemd-state.json").read_text()),
                {
                    "deadlock-api": "active",
                    "deadlock-worker": "inactive",
                    "deadlock-web": "active",
                    "deadlock-cloudflare-ips.timer": "active",
                    "deadlock-cloudflare-ips.service": "inactive",
                },
            )
            self.assertFalse((self.root / "current-helper-used").exists())

    def test_immutable_recovery_wrapper_rejects_partial_quiesce_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("quiesce-partial-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "quiesce-partial-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        receipt_path = self.shared / STATE_NAME
        receipt = json.loads(receipt_path.read_text())
        receipt["service_state_before"].pop("deadlock-web")
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
        systemctl, systemctl_log = self.write_failing_systemctl("quiesce-partial")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(receipt_path.exists())
        self.assertFalse(systemctl_log.exists())

    def test_quiesce_v1_active_only_receipt_restores_without_enablement(self) -> None:
        current = self.add_release("quiesce-v1-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "quiesce-v1-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "active",
        )
        receipt = self.shared / STATE_NAME
        record = json.loads(receipt.read_text(encoding="ascii"))
        record["version"] = 1
        record.pop("service_enabled_before")
        record.pop("timer_enabled_before")
        receipt.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
        receipt.chmod(0o600)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api": "inactive",
                "deadlock-worker": "active",
                "deadlock-web": "inactive",
                "deadlock-cloudflare-ips.timer": "inactive",
            }
        )
        self.run_transaction("verify-quiesce")
        self.run_transaction("restore-quiesce", "--systemctl", str(systemctl))
        self.run_transaction("abort-quiesce")
        self.assertFalse(receipt.exists())
        log = (self.root / "systemctl.log").read_text(encoding="ascii")
        self.assertNotIn("enable ", log)
        self.assertNotIn("disable ", log)

    def test_quiesce_v1_v2_hybrids_are_rejected(self) -> None:
        for version, remove_enabled in ((1, False), (2, True)):
            with self.subTest(version=version):
                current = self.add_release(f"quiesce-hybrid-{version}-current")
                (self.app_dir / "current").unlink(missing_ok=True)
                (self.app_dir / "current").symlink_to(current)
                candidate = self.releases / f"quiesce-hybrid-{version}-candidate"
                self.run_transaction(
                    "prepare-quiesce",
                    "--app-dir",
                    str(self.app_dir),
                    "--candidate-release",
                    str(candidate),
                    "--service-state",
                    "deadlock-api=inactive",
                    "--service-state",
                    "deadlock-worker=inactive",
                    "--service-state",
                    "deadlock-web=inactive",
                    "--timer-active-before",
                    "inactive",
                )
                receipt = self.shared / STATE_NAME
                malformed = json.loads(receipt.read_text(encoding="ascii"))
                malformed["version"] = version
                if remove_enabled:
                    malformed.pop("service_enabled_before")
                    malformed.pop("timer_enabled_before")
                receipt.write_text(
                    json.dumps(malformed, sort_keys=True) + "\n", encoding="ascii"
                )
                receipt.chmod(0o600)
                invalid = self.run_script(
                    TRANSACTION_TOOL,
                    "verify-quiesce",
                    "--state",
                    str(receipt),
                    check=False,
                )
                self.assertNotEqual(
                    invalid.returncode,
                    0,
                )
                receipt.unlink(missing_ok=True)

    def test_immutable_recovery_wrapper_rejects_unbound_quiesce_receipts_before_systemd(
        self,
    ) -> None:
        for index, mutation in enumerate(("phase", "extra", "candidate")):
            if index:
                self.tearDown()
                self.setUp()
            generation = self.install_recovery_generation()
            current = self.add_release(f"quiesce-unbound-{mutation}-current")
            (self.app_dir / "current").symlink_to(current)
            candidate = self.releases / f"quiesce-unbound-{mutation}-candidate"
            self.run_transaction(
                "prepare-quiesce",
                "--app-dir",
                str(self.app_dir),
                "--candidate-release",
                str(candidate),
                "--service-state",
                "deadlock-api=active",
                "--service-state",
                "deadlock-worker=inactive",
                "--service-state",
                "deadlock-web=active",
                "--timer-active-before",
                "active",
            )
            receipt_path = self.shared / STATE_NAME
            receipt = json.loads(receipt_path.read_text())
            if mutation == "phase":
                receipt["phase"] = "prepared"
            elif mutation == "extra":
                receipt["unexpected"] = True
            else:
                candidate.mkdir()
                (candidate / "unbound.txt").write_text("not receipt-bound")
            receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
            systemctl, systemctl_log = self.write_failing_systemctl(
                f"quiesce-unbound-{mutation}"
            )

            result = self.run_script(
                generation / RECOVERY_WRAPPER.name,
                "--app-dir",
                str(self.app_dir),
                "--systemctl",
                str(systemctl),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, mutation)
            self.assertTrue(receipt_path.exists(), mutation)
            self.assertFalse(systemctl_log.exists(), mutation)

    def test_abort_quiesce_low_level_candidate_guard_is_fail_closed(self) -> None:
        for index, mutation in enumerate(("occupied", "symlink", "hardlink", "special")):
            if index:
                self.tearDown()
                self.setUp()
            current = self.add_release(f"abort-candidate-{mutation}-current")
            (self.app_dir / "current").symlink_to(current)
            candidate = self.releases / f"abort-candidate-{mutation}"
            self.run_transaction(
                "prepare-quiesce",
                "--app-dir",
                str(self.app_dir),
                "--candidate-release",
                str(candidate),
                "--service-state",
                "deadlock-api=active",
                "--service-state",
                "deadlock-worker=inactive",
                "--service-state",
                "deadlock-web=active",
                "--timer-active-before",
                "inactive",
            )
            if mutation == "occupied":
                candidate.mkdir()
                (candidate / "unbound.txt").write_text("not receipt-bound")
            elif mutation == "symlink":
                replacement = self.releases / f"{mutation}-replacement"
                replacement.mkdir()
                candidate.symlink_to(replacement, target_is_directory=True)
            elif mutation == "hardlink":
                source = self.root / "candidate-hardlink-source"
                source.write_text("not a directory")
                candidate.hardlink_to(source)
            else:
                os.mkfifo(candidate)

            result = self.run_script(
                TRANSACTION_TOOL,
                "abort-quiesce",
                "--state",
                str(self.shared / STATE_NAME),
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, mutation)
            self.assertTrue((self.shared / STATE_NAME).exists(), mutation)
            self.assertTrue(os.path.lexists(candidate), mutation)
            if mutation == "occupied":
                self.assertTrue((candidate / "unbound.txt").exists())

    def test_recursive_release_cleanup_validates_complete_tree_before_delete(self) -> None:
        for mutation in ("hardlink", "symlink", "special"):
            with self.subTest(mutation=mutation):
                tree = self.root / f"unsafe-cleanup-{mutation}"
                tree.mkdir(mode=0o700)
                safe_file = tree / "safe.txt"
                safe_file.write_text("retained", encoding="ascii")
                safe_file.chmod(0o600)
                if mutation == "hardlink":
                    outside = self.root / "outside-hardlink"
                    outside.write_text("outside", encoding="ascii")
                    outside.chmod(0o600)
                    (tree / "linked.txt").hardlink_to(outside)
                elif mutation == "symlink":
                    (tree / "linked.txt").symlink_to(safe_file)
                else:
                    os.mkfifo(tree / "special")

                with self.assertRaises(transaction.TransactionError):
                    transaction._remove_tree(tree)
                self.assertTrue(tree.exists())
                self.assertTrue(safe_file.exists())
                if mutation == "hardlink":
                    self.assertTrue((self.root / "outside-hardlink").exists())
                elif mutation == "symlink":
                    self.assertTrue((tree / "linked.txt").is_symlink())
                else:
                    self.assertTrue((tree / "special").exists())

    def test_recursive_release_cleanup_quarantines_only_rechecked_inode(self) -> None:
        tree = self.root / "cleanup-race"
        tree.mkdir(mode=0o700)
        (tree / "original.txt").write_text("original", encoding="ascii")
        (tree / "original.txt").chmod(0o600)
        replacement = self.root / "cleanup-race-original"
        real_rename = os.rename

        def replace_before_quarantine(source: str | bytes | os.PathLike[str], destination: str | bytes | os.PathLike[str]) -> None:
            if Path(source) == tree:
                real_rename(source, replacement)
                tree.mkdir(mode=0o700)
                marker = tree / "replacement.txt"
                marker.write_text("replacement", encoding="ascii")
                marker.chmod(0o600)
            real_rename(source, destination)

        with mock.patch.object(transaction.os, "rename", side_effect=replace_before_quarantine):
            with self.assertRaises(transaction.TransactionError):
                transaction._remove_tree(tree)
        self.assertTrue(replacement.exists())
        self.assertTrue(tree.exists())
        self.assertTrue((tree / "replacement.txt").exists())

    def test_recursive_release_cleanup_partial_delete_is_retained_and_retryable(self) -> None:
        tree = self.root / "cleanup-partial"
        tree.mkdir(mode=0o700)
        first = tree / "first.txt"
        second = tree / "second.txt"
        first.write_text("first", encoding="ascii")
        second.write_text("second", encoding="ascii")
        for path in (first, second):
            path.chmod(0o600)
        outside = self.root / "cleanup-outside"
        outside.write_text("must remain", encoding="ascii")
        outside.chmod(0o600)
        real_rmtree = transaction.shutil.rmtree

        def partial_delete(path: str | bytes | os.PathLike[str], *args, **kwargs) -> None:
            quarantined_first = Path(path) / first.name
            quarantined_first.unlink()
            raise OSError("injected partial cleanup failure")

        with mock.patch.object(transaction.shutil, "rmtree", side_effect=partial_delete):
            with self.assertRaises(transaction.TransactionError):
                transaction._remove_tree(tree)
        # The failed quarantine is put back under its original receipt-bound
        # name, while unrelated paths remain untouched and cleanup can retry.
        self.assertTrue(tree.exists())
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(outside.read_text(encoding="ascii"), "must remain")
        with mock.patch.object(transaction.shutil, "rmtree", wraps=real_rmtree):
            transaction._remove_tree(tree)
        self.assertFalse(tree.exists())
        self.assertTrue(outside.exists())

    def test_abort_quiesce_retains_unbound_temporary_directory(self) -> None:
        current = self.add_release("abort-temp-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "abort-temp-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        temporary = self.shared / f".venv-install-{candidate.name}.0000"
        temporary.mkdir(mode=0o700)
        marker = temporary / "unbound.txt"
        marker.write_text("must remain", encoding="ascii")
        marker.chmod(0o600)

        result = self.run_script(
            TRANSACTION_TOOL,
            "abort-quiesce",
            "--state",
            str(self.shared / STATE_NAME),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(marker.exists())

    def test_abort_quiesce_empty_candidate_and_mutable_wrapper_cleanup(self) -> None:
        current = self.add_release("abort-empty-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.releases / "abort-empty-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        candidate.mkdir()
        result = self.run_script(
            TRANSACTION_TOOL,
            "abort-quiesce",
            "--state",
            str(self.shared / STATE_NAME),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

        self.tearDown()
        self.setUp()
        current = self.add_release("abort-mutable-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_runtime_stubs(current)
        self.add_fake_venv(self.shared / "venv", marker="mutable-abort")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        candidate = self.releases / "abort-mutable-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        candidate.mkdir()
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
            env=self.runtime_env(label="mutable-abort"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

        self.tearDown()
        self.setUp()
        current = self.add_release("abort-mutable-occupied-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_runtime_stubs(current)
        self.add_fake_venv(self.shared / "venv", marker="mutable-occupied")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        candidate = self.releases / "abort-mutable-occupied-candidate"
        self.run_transaction(
            "prepare-quiesce",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--service-state",
            "deadlock-api=active",
            "--service-state",
            "deadlock-worker=inactive",
            "--service-state",
            "deadlock-web=active",
            "--timer-active-before",
            "inactive",
        )
        candidate.mkdir()
        marker = candidate / "unbound.txt"
        marker.write_text("retain me")
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
            env=self.runtime_env(label="mutable-occupied"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(marker.exists())
        self.assertIn("restart deadlock-api", (self.root / "systemctl.log").read_text())

    def test_immutable_recovery_wrapper_rejects_incomplete_two_pointer_snapshot_before_systemd(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("two-pointer-current")
        previous = self.add_release("two-pointer-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_current_control_helper_bombs(current)
        candidate = self.create_wrapper_transaction(current, previous, "two-pointer")
        systemctl, systemctl_log = self.write_failing_systemctl("two-pointer")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).is_file())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertTrue(candidate.is_dir())
        self.assertFalse((self.root / "current-helper-used").exists())

    def test_immutable_recovery_wrapper_two_pointer_uses_bound_data_helpers(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("upgrade-current")
        previous = self.add_release("upgrade-previous")
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_bound_release_tools(current)
        venv = self.shared / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
            encoding="utf-8",
        )
        (venv / "bin" / "python").chmod(0o755)
        (venv / "deps-version").write_text("unchanged\n", encoding="ascii")
        candidate = self.create_wrapper_transaction(
            current, previous, "upgrade", complete_snapshot=True
        )
        # The immutable helper must use the supplied control-plane systemctl
        # boundary; a release-specific control helper is only digest-read.
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "inactive",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "inactive",
            }
        )

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "current-helper-used").exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertFalse(candidate.exists())

    def test_immutable_recovery_wrapper_retains_receipt_on_systemctl_rc4_or_timeout(
        self,
    ) -> None:
        for index, (exit_code, label) in enumerate(((4, "rc4"), (124, "timeout"))):
            if index:
                self.tearDown()
                self.setUp()
            with self.subTest(exit_code=exit_code):
                generation = self.install_recovery_generation()
                current = self.add_release(f"{label}-current")
                previous = self.add_release(f"{label}-previous")
                (self.app_dir / "current").symlink_to(current)
                (self.app_dir / "previous").symlink_to(previous)
                self.add_bound_release_tools(current)
                venv = self.shared / "venv"
                (venv / "bin").mkdir(parents=True)
                (venv / "bin" / "python").write_text(
                    "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
                    encoding="utf-8",
                )
                (venv / "bin" / "python").chmod(0o755)
                (venv / "deps-version").write_text("unchanged\n", encoding="ascii")
                candidate = self.create_wrapper_transaction(
                    current, previous, label, complete_snapshot=True
                )
                good_systemctl = self.write_stateful_systemctl(
                    {
                        "deadlock-api.service": "inactive",
                        "deadlock-worker.service": "inactive",
                        "deadlock-web.service": "inactive",
                    }
                )
                receipt = self.shared / ".release-systemd-state.json"
                self.run_script(
                    SYSTEMD_STATE_TOOL,
                    "capture-transaction",
                    "--state",
                    str(receipt),
                    "--transaction",
                    str(self.shared / STATE_NAME),
                    "--app-dir",
                    str(self.app_dir),
                    "--helper-release",
                    str(current),
                    "--require-helper-manifest",
                    "--systemctl",
                    str(good_systemctl),
                )
                bad_systemctl, systemctl_log = self.write_failing_systemctl(
                    label, exit_code=exit_code
                )
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(bad_systemctl),
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(systemctl_log.exists())
                self.assertTrue(
                    receipt.exists(),
                    f"rc={exit_code} must not clear the systemd receipt",
                )
                self.assertTrue(
                    (self.shared / STATE_NAME).exists(),
                    f"rc={exit_code} must retain the transaction for an immutable retry",
                )
                self.assertTrue(candidate.exists())

    def test_immutable_recovery_wrapper_rejects_tampered_generation_before_transaction(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        candidate = self.create_wrapper_transaction(None, None, "tampered-generation")
        systemctl, systemctl_log = self.write_failing_systemctl("tampered-generation")
        transaction_helper = generation / "platform_release_transaction.py"
        transaction_helper.chmod(0o644)
        transaction_helper.write_bytes(transaction_helper.read_bytes() + b"\n# tampered\n")
        transaction_helper.chmod(0o444)

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertTrue((self.shared / STATE_NAME).is_file())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertFalse((self.app_dir / "current").exists())
        self.assertFalse((self.app_dir / "previous").exists())
        self.assertTrue(candidate.is_dir())

    def test_immutable_recovery_wrapper_rollback_pre_runtime_uses_bound_receipt(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-wrapper-current")
        previous = self.add_release("rollback-wrapper-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.create_wrapper_rollback_transaction(current, previous, phase="prepared")
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "active",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_immutable_recovery_wrapper_rollback_rejects_receipt_identity_before_systemd(self) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-receipt-current")
        previous = self.add_release("rollback-receipt-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.create_wrapper_rollback_transaction(current, previous, phase="prepared")
        systemctl = self.write_stateful_systemctl({})
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        payload = json.loads(receipt.read_text())
        payload["operation_id"] = "0" * 32
        receipt.write_text(json.dumps(payload, sort_keys=True) + "\n")
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertTrue(receipt.exists())
        self.assertEqual((self.root / "systemctl.log").read_text(), "")

    def test_immutable_recovery_wrapper_rollback_restart_pending_resumes_immutable_runtime(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-restart-current")
        previous = self.add_release("rollback-restart-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_fake_venv(self.shared / "venv", marker="rollback")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_wrapper_rollback_transaction(current, previous, phase="restart-pending")
        self.switch_pointer("current", previous)
        self.switch_pointer("previous", current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "active",
                "deadlock-web.service": "inactive",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env={
                **self.runtime_env(label="previous"),
                "PLATFORM_TEST_NGINX_LABEL": "previous",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

    def test_immutable_recovery_wrapper_rollback_runtime_pending_restores_original_state(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-runtime-current")
        previous = self.add_release("rollback-runtime-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(current)
        (self.app_dir / "previous").symlink_to(previous)
        self.add_fake_venv(self.shared / "venv", marker="rollback")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_wrapper_rollback_transaction(
            current, previous, phase="rollback-runtime-pending"
        )
        self.switch_pointer("current", previous)
        self.switch_pointer("previous", current)
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "active",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env={
                **self.runtime_env(label="current"),
                "PLATFORM_TEST_NGINX_LABEL": "current",
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_immutable_recovery_wrapper_replays_runtime_after_filesystem_recovery_crash(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        current = self.add_release("rollback-window-current")
        previous = self.add_release("rollback-window-previous")
        self.add_runtime_stubs(current)
        self.add_runtime_stubs(previous)
        (self.app_dir / "current").symlink_to(previous)
        (self.app_dir / "previous").symlink_to(current)
        self.add_fake_venv(self.shared / "venv", marker="rollback")
        self.write_fake_python(self.shared / "venv" / "bin" / "python")
        self.create_wrapper_rollback_transaction(
            current, previous, phase="rollback-runtime-pending"
        )
        systemctl = self.write_stateful_systemctl(
            {
                "deadlock-api.service": "active",
                "deadlock-worker.service": "inactive",
                "deadlock-web.service": "active",
            }
        )
        receipt = self.shared / ".release-systemd-state.json"
        self.run_script(
            SYSTEMD_STATE_TOOL,
            "capture-transaction",
            "--state",
            str(receipt),
            "--transaction",
            str(self.shared / STATE_NAME),
            "--app-dir",
            str(self.app_dir),
            "--helper-release",
            str(previous),
            "--require-helper-manifest",
            "--systemctl",
            str(systemctl),
        )
        (self.root / "systemctl.log").write_text("")
        interrupted = self.root / "filesystem-recovery-kill.sh"
        interrupted.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"/usr/bin/python3 -I {shlex.quote(str(TRANSACTION_TOOL))} recover --retain --runtime-pending --state {shlex.quote(str(self.shared / STATE_NAME))}\n"
            "/bin/kill -KILL \"$$\"\n",
            encoding="utf-8",
        )
        interrupted.chmod(0o755)
        result = self.run_script(interrupted, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "filesystem-restored-runtime-pending")
        self.assertTrue(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

        result = self.run_script(
            generation / RECOVERY_WRAPPER.name,
            "--app-dir",
            str(self.app_dir),
            "--systemctl",
            str(systemctl),
            env={
                **self.runtime_env(label="current"),
                "PLATFORM_TEST_NGINX_LABEL": "current",
            },
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

    def test_immutable_recovery_wrapper_early_rollback_without_receipt_is_filesystem_only(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        for phase in ("prepared", "pointers-switched"):
            with self.subTest(phase=phase):
                for pointer in (self.app_dir / "current", self.app_dir / "previous"):
                    pointer.unlink(missing_ok=True)
                current = self.add_release(f"rollback-no-receipt-{phase}-current")
                previous = self.add_release(f"rollback-no-receipt-{phase}-previous")
                self.add_runtime_stubs(current)
                self.add_runtime_stubs(previous)
                (self.app_dir / "current").symlink_to(current)
                (self.app_dir / "previous").symlink_to(previous)
                self.create_wrapper_rollback_transaction(current, previous, phase=phase)
                systemctl, systemctl_log = self.write_failing_systemctl(f"rollback-no-receipt-{phase}")

                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(systemctl_log.exists())
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse((self.shared / ".release-systemd-state.json").exists())

    def test_immutable_recovery_wrapper_late_runtime_phases_use_immutable_restore(self) -> None:
        generation = self.install_recovery_generation()
        for phase in ("services-restarted", "smoke-passed"):
            with self.subTest(phase=phase):
                for pointer in (self.app_dir / "current", self.app_dir / "previous"):
                    pointer.unlink(missing_ok=True)
                shutil.rmtree(self.shared / "venv", ignore_errors=True)
                current = self.add_release(f"rollback-{phase}-current")
                previous = self.add_release(f"rollback-{phase}-previous")
                self.add_runtime_stubs(current)
                self.add_runtime_stubs(previous)
                (self.app_dir / "current").symlink_to(current)
                (self.app_dir / "previous").symlink_to(previous)
                self.add_fake_venv(self.shared / "venv", marker="rollback")
                self.write_fake_python(self.shared / "venv" / "bin" / "python")
                self.create_wrapper_rollback_transaction(current, previous, phase=phase)
                self.switch_pointer("current", previous)
                self.switch_pointer("previous", current)
                systemctl = self.write_stateful_systemctl(
                    {
                        "deadlock-api.service": "active",
                        "deadlock-worker.service": "inactive",
                        "deadlock-web.service": "active",
                    }
                )
                receipt = self.shared / ".release-systemd-state.json"
                self.run_script(
                    SYSTEMD_STATE_TOOL,
                    "capture-transaction",
                    "--state",
                    str(receipt),
                    "--transaction",
                    str(self.shared / STATE_NAME),
                    "--app-dir",
                    str(self.app_dir),
                    "--helper-release",
                    str(previous),
                    "--require-helper-manifest",
                    "--systemctl",
                    str(systemctl),
                )
                (self.root / "systemctl.log").write_text("")
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    env={
                        **self.runtime_env(label="current"),
                        "PLATFORM_TEST_NGINX_LABEL": "current",
                    },
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.shared / STATE_NAME).exists())
                self.assertFalse(receipt.exists())

    def test_immutable_recovery_wrapper_post_clear_runtime_and_restored_retries_are_idempotent(
        self,
    ) -> None:
        generation = self.install_recovery_generation()
        for phase in ("rollback-runtime-applied", "recovery-restored"):
            with self.subTest(phase=phase):
                for pointer in (self.app_dir / "current", self.app_dir / "previous"):
                    pointer.unlink(missing_ok=True)
                current = self.add_release(f"rollback-post-clear-{phase}-current")
                previous = self.add_release(f"rollback-post-clear-{phase}-previous")
                self.add_runtime_stubs(current)
                self.add_runtime_stubs(previous)
                (self.app_dir / "current").symlink_to(previous)
                (self.app_dir / "previous").symlink_to(current)
                transaction_phase = (
                    "rollback-runtime-applied" if phase == "recovery-restored" else phase
                )
                self.create_wrapper_rollback_transaction(
                    current, previous, phase=transaction_phase
                )
                systemctl, systemctl_log = self.write_failing_systemctl(f"rollback-post-clear-{phase}")
                receipt = self.shared / ".release-systemd-state.json"
                if phase == "recovery-restored":
                    self.run_transaction("recover", "--retain")
                    self.assertEqual(self.state_phase(), "recovery-restored")
                receipt.unlink(missing_ok=True)
                result = self.run_script(
                    generation / RECOVERY_WRAPPER.name,
                    "--app-dir",
                    str(self.app_dir),
                    "--systemctl",
                    str(systemctl),
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(systemctl_log.exists())
                self.assertFalse((self.shared / STATE_NAME).exists())

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
            "--require-helper-manifest",
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

    def test_rollback_crash_after_systemd_clear_retries_transaction_completion(self) -> None:
        current, previous = self.prepare_rollback_state()
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
        clear_block = (
            "  clear_rollback_systemd_state\n"
            "  /usr/bin/python3 -I \"$TRANSACTION_TOOL\" complete --state \"$TRANSACTION_STATE\"\n"
        )
        self.assertIn(clear_block, rollback.read_text(encoding="utf-8"))
        interrupted = self.root / "rollback-after-systemd-clear.sh"
        interrupted.write_text(
            rollback.read_text(encoding="utf-8").replace(
                clear_block,
                "  clear_rollback_systemd_state\n"
                "  /bin/kill -KILL \"$$\"\n"
                "  /usr/bin/python3 -I \"$TRANSACTION_TOOL\" complete --state \"$TRANSACTION_STATE\"\n",
                1,
            ),
            encoding="utf-8",
        )
        interrupted.chmod(0o755)
        result = self.run_script(
            interrupted,
            "--app-dir",
            str(self.app_dir),
            "--no-restart",
            env=self.runtime_env(label="previous"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.shared / STATE_NAME).exists())
        self.assertEqual(self.state_phase(), "rollback-runtime-applied")
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())
        self.assertEqual((self.app_dir / "current").resolve(), previous)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

        retry = self.copy_rollback_with_systemctl(systemctl)
        result = self.run_script(
            retry,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="previous"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse((self.shared / ".release-systemd-state.json").exists())

    def test_rollback_recovery_receipt_retry_after_retained_cleanup_crash(self) -> None:
        current, previous = self.prepare_rollback_state()
        runtime = self.copy_runtime_with_fault(
            "runtime-before-recovery-retry.sh",
            '  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"\n',
            '  PLATFORM_APP_DIR="$APP_DIR" "$UNITS_TOOL"\n'
            '  /bin/kill -KILL "$PPID"\n',
        )
        interrupted = self.copy_rollback_with_runtime(
            "rollback-before-recovery-retry.sh", runtime
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
        systemd_receipt = self.shared / ".release-systemd-state.json"
        self.assertTrue(systemd_receipt.is_file())

        recovery_runtime = self.write_test_runtime_restore()
        recovery = self.copy_rollback_with_runtime(
            "rollback-retained-cleanup-kill.sh", recovery_runtime
        )
        retained_cleanup = (
            "  /usr/bin/python3 -I \"$TRANSACTION_TOOL\" complete-recovery \\\n"
            "    --retain-receipt \\\n"
            "    --state \"$TRANSACTION_STATE\"\n"
        )
        recovery_text = recovery.read_text(encoding="utf-8")
        self.assertIn(retained_cleanup, recovery_text)
        recovery.write_text(
            recovery_text.replace(
                retained_cleanup,
                retained_cleanup + '  /bin/kill -KILL "$$"\n',
                1,
            ),
            encoding="utf-8",
        )
        recovery.chmod(0o755)
        result = self.run_script(
            recovery,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="current"),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "recovery-restored")
        self.assertTrue(systemd_receipt.is_file())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

        retry = self.copy_rollback_with_runtime(
            "rollback-retained-cleanup-retry.sh", recovery_runtime
        )
        result = self.run_script(
            retry,
            "--recover-pending",
            "--app-dir",
            str(self.app_dir),
            env=self.runtime_env(label="current"),
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(systemd_receipt.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)

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

    def test_install_pointer_gap_durably_recovers_before_current_switch(self) -> None:
        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="migration-applied")
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-between-pointer-switches-kill.sh",
            "      phase=previous-switched\n",
            "      phase=previous-switched\n      /bin/kill -KILL \"$$\"\n",
            self.write_fake_systemctl(),
        )

        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "previous-switched")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

        self.run_transaction("recover", "--retain")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.run_transaction("complete-recovery")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_install_pointer_gap_durably_recovers_after_current_switch(self) -> None:
        """A SIGKILL after the second symlink still retains a recoverable receipt."""

        current, previous, candidate = self.prepare_install_state()
        self.advance_install_state(candidate, current, phase="migration-applied")
        interrupted = self.copy_deploy_script_with_fault(
            "deploy-after-current-pointer-kill.sh",
            "    set_phase previous-switched current-switched || exit 1\n",
            '    /bin/kill -KILL "$$"\n'
            "    set_phase previous-switched current-switched || exit 1\n",
            self.write_fake_systemctl(),
        )

        result = self.run_script(
            interrupted,
            "--resume",
            "--app-dir",
            str(self.app_dir),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state_phase(), "previous-switched")
        self.assertEqual((self.app_dir / "current").resolve(), candidate)
        self.assertEqual((self.app_dir / "previous").resolve(), current)

        self.run_transaction("recover", "--retain")
        self.assertEqual((self.app_dir / "current").resolve(), current)
        self.assertEqual((self.app_dir / "previous").resolve(), previous)
        self.run_transaction("complete-recovery")
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

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

    def test_first_install_migration_does_not_touch_old_service_systemd_topology(self) -> None:
        """An install with no old pointers must not stop or inspect old units."""

        candidate = self.add_release("first-install-candidate")
        shared_venv = self.shared / "venv"
        self.add_fake_venv(shared_venv, marker="first-install")
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(shared_venv),
            "--peer",
            str(self.shared / ".venv-install-first-install-candidate.0000"),
            "--snapshot",
            str(candidate / ".rollback" / "shared-venv-before-install"),
            "--transition",
            "none",
        )
        self.run_transaction("phase", "--expected", "prepared", "--phase", "venv-transitioned")
        self.run_transaction("phase", "--expected", "venv-transitioned", "--phase", "staged")
        self.run_transaction("record-services", "--service-state", "deadlock-api=inactive", "--service-state", "deadlock-worker=inactive", "--service-state", "deadlock-web=inactive", "--timer-active-before", "inactive")
        self.run_transaction("phase", "--expected", "staged", "--phase", "migration-pending")

        systemctl_log = self.root / "first-install-systemctl.log"
        systemctl = self.root / "systemctl-first-install"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(systemctl_log)!r}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        systemctl.chmod(0o755)
        fake_python_log = self.root / "first-install-python.log"
        fake_python = self.root / "first-install-python"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(fake_python_log)!r}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        script = ALEMBIC_SCRIPT.read_text(encoding="utf-8")
        tools_needle = 'TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'
        self.assertIn(tools_needle, script)
        script = script.replace(tools_needle, f'TOOLS_DIR="{self.tools_dir}"', 1)
        preflight_tool = '"$TOOLS_DIR/platform_release_preflight.sh"'
        self.assertIn(preflight_tool, script)
        script = script.replace(preflight_tool, "/usr/bin/true", 1)
        script = script.replace("/usr/bin/systemctl", str(systemctl))
        migration = self.root / "first-install-run-alembic.sh"
        migration.write_text(script, encoding="utf-8")
        migration.chmod(0o755)

        result = self.run_script(
            migration,
            "upgrade",
            "head",
            env={
                "PLATFORM_ENVIRONMENT": "production",
                "PLATFORM_APP_DIR": str(self.app_dir),
                "PLATFORM_ENV_FILE": str(self.shared / ".env.platform"),
                "PLATFORM_PYTHON_BIN": str(fake_python),
            },
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(systemctl_log.exists())
        self.assertIn("-m alembic upgrade head", fake_python_log.read_text(encoding="utf-8"))

    def test_mutable_first_install_staged_failure_uses_optional_snapshot_policy(self) -> None:
        """A staged first-install failure cleans only a safe nullable receipt."""

        candidate = self.create_wrapper_transaction(None, None, "mutable-first-install")
        # create_wrapper_transaction deliberately leaves the initial receipt at
        # prepared unless it records a snapshot. Advance it to the exact
        # pre-migration staged boundary with no service snapshot at all.
        self.run_transaction("phase", "--expected", "prepared", "--phase", "venv-transitioned")
        self.run_transaction("phase", "--expected", "venv-transitioned", "--phase", "staged")
        systemctl, systemctl_log = self.write_failing_systemctl("mutable-first-install")
        artifact = self.root / "mutable-first-install.tar.gz"
        artifact.write_bytes(b"not reached")
        failed = self.copy_initial_deploy_with_fault(
            "deploy-mutable-first-install-failure.sh",
            systemctl,
            '  "$INSTALL_TOOL" --stage-only "$ARTIFACT" "$APP_DIR"\n',
            '  /bin/false\n',
        )

        result = self.run_script(
            failed,
            "--artifact",
            str(artifact),
            "--app-dir",
            str(self.app_dir),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(systemctl_log.exists())
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())

    def test_mutable_first_install_abort_accepts_only_strict_inactive_snapshot(self) -> None:
        """Nullable abort cleanup accepts absent or complete inactive state only."""

        for label, mutation in (
            ("active", lambda record: record["service_state_before"].__setitem__("deadlock-api", "active")),
            ("enabled", lambda record: record["service_enabled_before"].__setitem__("deadlock-api", "enabled")),
            ("partial", lambda record: record.pop("timer_enabled_before")),
        ):
            with self.subTest(label=label):
                if label != "active":
                    self.tearDown()
                    self.setUp()
                candidate = self.create_wrapper_transaction(
                    None,
                    None,
                    f"mutable-first-install-{label}",
                    complete_snapshot=True,
                )
                state = self.shared / STATE_NAME
                record = json.loads(state.read_text(encoding="ascii"))
                mutation(record)
                state.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")
                state.chmod(0o600)
                systemctl, systemctl_log = self.write_failing_systemctl(
                    f"mutable-first-install-abort-{label}"
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
                self.assertTrue(state.exists())
                self.assertTrue(candidate.exists())
                self.assertFalse(systemctl_log.exists())

    def test_mutable_abort_retained_staged_nullable_topologies(self) -> None:
        """The staged map covers current-only cleanup and rejects drift."""

        current = self.add_release("mutable-current-only-staged-current")
        (self.app_dir / "current").symlink_to(current)
        self.add_runtime_stubs(current)
        self.add_fake_venv(self.shared / "venv", marker="mutable-current-only")
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "mutable-current-only-staged",
            complete_snapshot=True,
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
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.shared / STATE_NAME).exists())
        self.assertFalse(candidate.exists())
        self.assertEqual((self.app_dir / "current").resolve(), current)

        self.tearDown()
        self.setUp()
        current = self.add_release("mutable-current-only-staged-invalid-current")
        (self.app_dir / "current").symlink_to(current)
        candidate = self.create_wrapper_transaction(
            current,
            None,
            "mutable-current-only-staged-invalid",
            complete_snapshot=True,
        )
        candidate_pointer = self.app_dir / "current"
        candidate_pointer.unlink()
        candidate_pointer.symlink_to(candidate)
        systemctl, systemctl_log = self.write_failing_systemctl(
            "mutable-current-only-staged-invalid"
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
        self.assertTrue(candidate.exists())
        self.assertFalse(systemctl_log.exists())

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
                timeout=120,
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
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "case \"${1:-}\" in\n"
            "  is-active) printf '%s\\n' active ;;\n"
            "  is-enabled) printf '%s\\n' enabled ;;\n"
            "esac\n"
            "exit 0\n"
        )
        path.chmod(0o755)
        return path

    def recovery_provenance(self) -> dict[str, object]:
        return {
            "repository": "StrayForest/old_sparky",
            "workflow": "Platform security and build",
            "job": "Verification contract",
            "run_id": "12345",
            "run_attempt": "2",
            "recovery_workflow_sha": "c" * 40,
            "source_sha": "a" * 40,
            "artifact_name": "platform-ci-route-12345-2",
            "artifact_sha256": "b" * 64,
            "deployable": False,
        }

    def install_recovery_generation(self) -> Path:
        source_tools = self.root / "recovery-source" / "platform" / "tools"
        source_tools.mkdir(parents=True)
        for name in recovery.RECOVERY_FILES:
            source = self.tools_dir / name
            destination = source_tools / name
            shutil.copy2(source, destination, follow_symlinks=False)
            destination.chmod(0o755 if name.endswith(".sh") else 0o644)
        self.install_test_lock_helper(source_tools / "platform_release_lock.sh")
        bundle = self.root / "recovery-bundle.zip"
        summary = recovery.build_bundle(
            source_tools.parent.parent,
            source_sha="a" * 40,
            provenance=self.recovery_provenance(),
            output=bundle,
        )
        return recovery.install_bundle(
            bundle,
            app_dir=self.app_dir,
            expected_bundle_sha=str(summary["bundle_sha256"]),
            expected_source_sha="a" * 40,
            expected_provenance=self.recovery_provenance(),
        )

    def create_wrapper_transaction(
        self,
        current: Path | None,
        previous: Path | None,
        label: str,
        *,
        complete_snapshot: bool = False,
        service_states: dict[str, str] | None = None,
        timer_active: bool = False,
    ) -> Path:
        candidate = self.add_release(f"{label}-candidate")
        rollback = candidate / ".rollback"
        rollback.mkdir()
        current_value = "" if current is None else str(current)
        previous_value = "" if previous is None else str(previous)
        if current is not None:
            freeze = candidate / "requirements-platform.freeze.txt"
            freeze.write_text("pip==test\n", encoding="ascii")
            freeze.chmod(0o444)
            previous_record = rollback / "previous-release"
            previous_record.write_text(f"{current}\n", encoding="ascii")
            previous_record.chmod(0o600)
            transition = rollback / "venv-transition"
            transition.write_text("unchanged\n", encoding="ascii")
            transition.chmod(0o600)
            freeze_record = rollback / "shared-freeze.sha256"
            freeze_record.write_text(
                f"{hashlib.sha256(freeze.read_bytes()).hexdigest()}\n",
                encoding="ascii",
            )
            freeze_record.chmod(0o600)
        self.run_transaction(
            "create",
            "--operation",
            "install",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            current_value,
            "--previous-before",
            previous_value,
            "--candidate-release",
            str(candidate),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(self.shared / f".venv-install-{candidate.name}.0000"),
            "--snapshot",
            str(rollback / "shared-venv-before-install"),
            "--transition",
            "none",
        )
        if complete_snapshot:
            service_args = [
                argument
                for unit, state in (
                    service_states
                    or {
                        "deadlock-api": "inactive",
                        "deadlock-worker": "inactive",
                        "deadlock-web": "inactive",
                    }
                ).items()
                for argument in ("--service-state", f"{unit}={state}")
            ]
            self.run_transaction(
                "phase", "--expected", "prepared", "--phase", "venv-transitioned"
            )
            self.run_transaction(
                "phase", "--expected", "venv-transitioned", "--phase", "staged"
            )
            self.run_transaction(
                "record-services",
                *service_args,
                "--timer-active-before",
                "active" if timer_active else "inactive",
                *(
                    option
                    for unit in ("deadlock-api", "deadlock-worker", "deadlock-web")
                    for option in ("--service-enabled", f"{unit}=disabled")
                )
                if current is None
                else (),
                "--timer-enabled-before",
                "disabled",
            )
        return candidate

    def create_wrapper_rollback_transaction(
        self, current: Path, previous: Path, *, phase: str
    ) -> None:
        rollback = current / ".rollback"
        rollback.mkdir()
        snapshot = rollback / "shared-venv-before-install"
        self.run_transaction(
            "create",
            "--operation",
            "rollback",
            "--app-dir",
            str(self.app_dir),
            "--current-before",
            str(current),
            "--previous-before",
            str(previous),
            "--candidate-release",
            str(current),
            "--shared-venv",
            str(self.shared / "venv"),
            "--peer",
            str(snapshot),
            "--snapshot",
            str(snapshot),
            "--transition",
            "none",
        )
        if phase != "prepared":
            for expected, next_phase in (
                ("prepared", "venv-transitioned"),
                ("venv-transitioned", "current-switched"),
                ("current-switched", "pointers-switched"),
                ("pointers-switched", "rollback-runtime-pending"),
                ("rollback-runtime-pending", "restart-pending"),
                ("restart-pending", "services-restarted"),
                ("services-restarted", "smoke-passed"),
                ("smoke-passed", "rollback-runtime-applied"),
            ):
                self.run_transaction("phase", "--expected", expected, "--phase", next_phase)
                if next_phase == phase:
                    return
            raise AssertionError(f"unsupported rollback test phase: {phase}")

    def write_failing_systemctl(
        self, label: str, *, exit_code: int = 99
    ) -> tuple[Path, Path]:
        log = self.root / f"{label}-systemctl.log"
        path = self.root / f"systemctl-{label}"
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {str(log)!r}\n"
            f"exit {exit_code}\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path, log

    def add_current_control_helper_bombs(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir()
        for name in (
            "platform_release_transaction.py",
            "platform_release_systemd_state.py",
            "platform_release_restore_runtime.sh",
            "platform_release_lock.sh",
        ):
            helper = tools / name
            helper.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"printf '%s\\n' used >> {str(self.root / 'current-helper-used')!r}\n"
                "exit 99\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)

    def add_bound_release_tools(self, release: Path) -> None:
        tools = release / "tools"
        tools.mkdir()
        helper_names = (
            "platform_install_systemd_units.sh",
            "platform_install_nginx.py",
            "platform_deploy_smoke.py",
            "platform_live_qa_runtime_install.py",
            "platform_install_logging.sh",
            "platform_prepare_service_user.sh",
            "platform_render_service_envs.py",
            "platform_deploy_smoke_impl.py",
            "platform_safe_env_exec.py",
            "platform_release_restore_runtime.sh",
            "platform_release_systemd_state.py",
            "platform_release_transaction.py",
            "platform_release_lock.sh",
        )
        control_names = {
            "platform_release_restore_runtime.sh",
            "platform_release_systemd_state.py",
            "platform_release_transaction.py",
            "platform_release_lock.sh",
        }
        for name in helper_names:
            helper = tools / name
            if name in control_names:
                helper.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    f"printf '%s\\n' used >> {str(self.root / 'current-helper-used')!r}\n"
                    "exit 99\n",
                    encoding="utf-8",
                )
            else:
                helper.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": "a" * 40}) + "\n",
            encoding="ascii",
        )

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
        for name in ("platform_install_nginx.py", "platform_deploy_smoke.py"):
            helper = tools / name
            helper.write_text("# test stub\n")
            helper.chmod(0o755)
        runtime_installer = tools / "platform_live_qa_runtime_install.py"
        runtime_installer.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
        runtime_installer.chmod(0o755)
        for name in (
            "platform_install_logging.sh",
            "platform_prepare_service_user.sh",
            "platform_render_service_envs.py",
            "platform_deploy_smoke_impl.py",
            "platform_safe_env_exec.py",
            "platform_release_restore_runtime.sh",
            "platform_release_systemd_state.py",
            "platform_release_transaction.py",
            "platform_release_lock.sh",
        ):
            helper = tools / name
            helper.write_text("#!/usr/bin/env bash\nexit 0\n" if name.endswith(".sh") else "# test stub\n")
            helper.chmod(0o755)
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
        normalized = list(args)
        if normalized and normalized[0] in {"record-services", "prepare-quiesce"}:
            if "--service-enabled" not in normalized:
                for unit in ("deadlock-api", "deadlock-worker", "deadlock-web"):
                    normalized.extend(("--service-enabled", f"{unit}=enabled"))
                normalized.extend(("--timer-enabled-before", "disabled"))
        return self.run_script(
            TRANSACTION_TOOL,
            *normalized,
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
        preflight_tool = '"$TOOLS_DIR/platform_release_preflight.sh"'
        self.assertIn(preflight_tool, script)
        # Keep the deploy wrapper's topology selection and dynamic
        # --require-previous/--allow-no-previous/--allow-initial-install
        # flags under test; only bypass the host-dependent preflight body.
        script = script.replace(preflight_tool, "/usr/bin/true", 1)
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
        preflight_tool = '"$TOOLS_DIR/platform_release_preflight.sh"'
        self.assertIn(preflight_tool, script)
        # Preserve the topology-derived flag array while replacing only the
        # host-dependent preflight executable with a no-op.
        script = script.replace(preflight_tool, "/usr/bin/true", 1)
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
            "def state_unit(unit):\n"
            "    if unit in state:\n"
            "        return unit\n"
            "    service_unit = unit + '.service'\n"
            "    return service_unit if service_unit in state else unit\n"
            "def enabled_unit(unit):\n"
            "    if unit in enabled:\n"
            "        return unit\n"
            "    service_unit = unit + '.service'\n"
            "    return service_unit if service_unit in enabled else unit\n"
            "argv = sys.argv[1:]\n"
            "action = argv[0] if argv else \"\"\n"
            "units = [value for value in argv[1:] if not value.startswith(\"-\")]\n"
            "if action == \"is-active\":\n"
            "    unit = units[0]\n"
            "    value = state.get(state_unit(unit), \"inactive\")\n"
            "    if \"--quiet\" not in argv:\n"
            "        print(value)\n"
            "    raise SystemExit(0 if value == \"active\" else 3)\n"
            "if action == \"is-enabled\":\n"
            "    unit = units[0]\n"
            "    value = enabled.get(enabled_unit(unit), \"static\")\n"
            "    if \"--quiet\" not in argv:\n"
            "        print(value)\n"
            "    raise SystemExit(0 if value in {\"enabled\", \"static\"} else 1)\n"
            "if action in {\"enable\", \"disable\"}:\n"
            "    value = \"enabled\" if action == \"enable\" else \"disabled\"\n"
            "    for unit in units:\n"
            "        key = enabled_unit(unit)\n"
            "        if enabled.get(key) != \"static\":\n"
            "            enabled[key] = value\n"
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
            "        key = state_unit(unit)\n"
            "        if key in state:\n"
            "            state[key] = \"inactive\" if action == \"stop\" else \"active\"\n"
            "    state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "    if os.getenv(\"PLATFORM_TEST_SYSTEMCTL_KILL_AFTER\") == f\"{action} {units[0]}\":\n"
            "        os.kill(os.getpid(), 9)\n"
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
            timeout=120,
        )
        if check and result.returncode != 0:
            self.fail(f"{script} failed: {result.returncode}\n{result.stderr}")
        return result


if __name__ == "__main__":
    unittest.main()
