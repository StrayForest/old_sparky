from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "platform/tools/platform_release_systemd_state.py"
UNITS = (
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
STATIC_UNITS = {
    "deadlock-maintenance.service",
    "deadlock-logrotate.service",
    "deadlock-offsite-backup.service",
    "deadlock-cloudflare-ips.service",
    "deadlock-health-monitor.service",
}


class PlatformReleaseSystemdStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app = self.root / "platform-app"
        self.releases = self.app / "releases"
        self.shared = self.app / "shared"
        self.releases.mkdir(parents=True)
        self.shared.mkdir()
        self.current = self.releases / "current"
        self.previous = self.releases / "previous"
        self.candidate = self.releases / "candidate"
        self.current.mkdir()
        self.previous.mkdir()
        self.candidate.mkdir()
        self.state_path = self.root / "systemd-state.json"
        self.enabled_path = self.root / "systemd-enabled.json"
        self.log_path = self.root / "systemctl.log"
        self.fail_marker = self.root / "fail-once"
        self.fake_systemctl = self.root / "systemctl"
        self._write_fake_systemctl()
        self.helper = self.root / "platform_release_systemd_state.py"
        helper_source = HELPER.read_text(encoding="utf-8")
        helper_source = helper_source.replace(
            'SYSTEMCTL: Final = "/usr/bin/systemctl"',
            f"SYSTEMCTL: Final = {str(self.fake_systemctl)!r}",
            1,
        )
        self.helper.write_text(helper_source, encoding="utf-8")
        self.helper.chmod(0o755)
        self.receipt = self.shared / ".release-systemd-state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_fake_systemctl(self) -> None:
        active = {unit: "inactive" for unit in UNITS}
        active["deadlock-api.service"] = "active"
        active["deadlock-web.service"] = "active"
        active["deadlock-maintenance.timer"] = "active"
        enabled = {
            unit: (
                "static"
                if unit in STATIC_UNITS
                else "disabled"
                if unit
                in {
                    "deadlock-worker.service",
                    "deadlock-logrotate.timer",
                    "deadlock-offsite-backup.timer",
                    "deadlock-health-monitor.timer",
                }
                else "enabled"
            )
            for unit in UNITS
        }
        self.state_path.write_text(json.dumps(active, sort_keys=True), encoding="utf-8")
        self.enabled_path.write_text(
            json.dumps(enabled, sort_keys=True), encoding="utf-8"
        )
        self.log_path.write_text("", encoding="utf-8")
        self._initial_active = active
        self._initial_enabled = enabled
        self.fake_systemctl.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"state_path = Path({str(self.state_path)!r})\n"
            f"enabled_path = Path({str(self.enabled_path)!r})\n"
            f"log_path = Path({str(self.log_path)!r})\n"
            f"fail_marker = Path({str(self.fail_marker)!r})\n"
            "state = json.loads(state_path.read_text())\n"
            "enabled = json.loads(enabled_path.read_text())\n"
            "argv = sys.argv[1:]\n"
            "action = argv[0]\n"
            "unit = next((value for value in argv[1:] if not value.startswith('-')), '')\n"
            "if action == 'is-active':\n"
            "    value = state.get(unit, 'inactive')\n"
            "    print(value)\n"
            "    raise SystemExit(0 if value == 'active' else 3)\n"
            "if action == 'is-enabled':\n"
            "    value = enabled.get(unit, 'static')\n"
            "    print(value)\n"
            "    raise SystemExit(0 if value == 'enabled' else 1)\n"
            "if action in {'enable', 'disable'}:\n"
            "    if os.getenv('PLATFORM_TEST_FAIL_ACTION') == action and not fail_marker.exists():\n"
            "        fail_marker.write_text('failed')\n"
            "        raise SystemExit(1)\n"
            "    if enabled.get(unit) != 'static':\n"
            "        enabled[unit] = 'enabled' if action == 'enable' else 'disabled'\n"
            "    log_path.open('a').write(f'{action} {unit}\\n')\n"
            "    enabled_path.write_text(json.dumps(enabled, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if action in {'start', 'stop', 'restart'}:\n"
            "    state[unit] = 'active' if action != 'stop' else 'inactive'\n"
            "    log_path.open('a').write(f'{action} {unit}\\n')\n"
            "    state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "    raise SystemExit(0)\n"
            "if action == 'daemon-reload':\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        self.fake_systemctl.chmod(0o755)

    def run_helper(
        self, *args: str, env: dict[str, str] | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env.update(env or {})
        result = subprocess.run(
            [str(self.helper), *args],
            cwd=REPO_ROOT,
            env=command_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"helper failed: {result.returncode}\n{result.stderr}")
        return result

    def capture(self) -> None:
        self.run_helper(
            "capture",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
            "--current-before",
            str(self.current),
            "--previous-before",
            str(self.previous),
        )

    def write_install_transaction(self) -> Path:
        def identity(path: Path) -> dict[str, int]:
            metadata = path.stat()
            return {"dev": metadata.st_dev, "ino": metadata.st_ino}

        transaction = self.shared / ".release-operation.json"
        record = {
            "version": 2,
            "operation": "install",
            "phase": "activation-pending",
            "app_dir": str(self.app),
            "current_before": str(self.current),
            "previous_before": str(self.previous),
            "candidate_release": str(self.candidate),
            "shared_venv": str(self.shared / "venv"),
            "peer": str(self.shared / ".venv-install-candidate"),
            "snapshot": str(self.candidate / ".rollback"),
            "transition": "exchange",
            "shared_before": None,
            "peer_before": identity(self.candidate),
            "current_before_identity": identity(self.current),
            "previous_before_identity": identity(self.previous),
            "candidate_identity": identity(self.candidate),
            "remove_env_on_recovery": False,
            "service_state_before": {
                "deadlock-api": "inactive",
                "deadlock-worker": "active",
                "deadlock-web": "inactive",
            },
            "quiesced_services": [
                "deadlock-api",
                "deadlock-worker",
                "deadlock-web",
            ],
            "timer_active_before": True,
        }
        transaction.write_text(json.dumps(record, sort_keys=True) + "\n")
        transaction.chmod(0o600)
        return transaction

    def test_transaction_capture_binds_recorded_active_and_live_enabled_state(self) -> None:
        transaction = self.write_install_transaction()
        self.run_helper(
            "capture-transaction",
            "--state",
            str(self.receipt),
            "--transaction",
            str(transaction),
            "--app-dir",
            str(self.app),
        )

    def test_transaction_receipt_retry_restores_mixed_runtime_without_enabling_units(self) -> None:
        """The recovery receipt is the sole authority for both state dimensions."""

        transaction = self.write_install_transaction()
        self.run_helper(
            "capture-transaction",
            "--state",
            str(self.receipt),
            "--transaction",
            str(transaction),
            "--app-dir",
            str(self.app),
        )
        # Simulate an interrupted install that left every unit active and
        # enabled. Recovery must restore the recorded mixed state and never
        # issue an unconditional enable/start operation.
        self.state_path.write_text(
            json.dumps({unit: "active" for unit in UNITS}, sort_keys=True),
            encoding="utf-8",
        )
        self.enabled_path.write_text(
            json.dumps(
                {
                    unit: ("static" if unit in STATIC_UNITS else "enabled")
                    for unit in UNITS
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self.log_path.write_text("", encoding="utf-8")

        self.run_helper(
            "restore",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
        )
        self.run_helper(
            "verify",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
        )

        expected_active = {
            item["name"]: item["active"]
            for item in json.loads(self.receipt.read_text())["units"]
        }
        self.assertEqual(json.loads(self.state_path.read_text()), expected_active)
        self.assertEqual(
            json.loads(self.enabled_path.read_text()), self._initial_enabled
        )
        operations = self.log_path.read_text().splitlines()
        self.assertNotIn("start deadlock-api.service", operations)
        self.assertNotIn("enable --now", operations)
        self.assertTrue(self.receipt.exists())

        record = json.loads(self.receipt.read_text())
        units = {item["name"]: item for item in record["units"]}
        self.assertEqual(units["deadlock-api.service"]["active"], "inactive")
        self.assertEqual(units["deadlock-worker.service"]["active"], "active")
        self.assertEqual(units["deadlock-web.service"]["active"], "inactive")
        self.assertEqual(
            units["deadlock-cloudflare-ips.timer"]["active"], "active"
        )
        self.assertEqual(
            {unit: item["enabled"] for unit, item in units.items()},
            self._initial_enabled,
        )
        self.run_helper(
            "validate",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
        )

    def test_mixed_active_enabled_and_static_states_restore_without_legacy_touch(self) -> None:
        self.capture()
        changed_active = {unit: "active" for unit in UNITS}
        changed_enabled = {unit: "enabled" for unit in UNITS}
        for unit in STATIC_UNITS:
            changed_enabled[unit] = "static"
        self.state_path.write_text(json.dumps(changed_active, sort_keys=True))
        self.enabled_path.write_text(json.dumps(changed_enabled, sort_keys=True))
        legacy_before = {"legacy.service": "active"}
        legacy_path = self.root / "legacy.json"
        legacy_path.write_text(json.dumps(legacy_before, sort_keys=True))

        self.run_helper(
            "restore",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
        )

        self.assertEqual(json.loads(self.state_path.read_text()), self._initial_active)
        self.assertEqual(
            json.loads(self.enabled_path.read_text()), self._initial_enabled
        )
        self.assertEqual(json.loads(legacy_path.read_text()), legacy_before)
        self.assertNotIn("legacy.service", self.log_path.read_text())

    def test_malformed_receipt_fails_before_systemd_mutation(self) -> None:
        self.capture()
        record = json.loads(self.receipt.read_text())
        record["units"][0]["name"] = "legacy.service"
        self.receipt.write_text(json.dumps(record) + "\n")
        self.receipt.chmod(0o600)
        self.log_path.write_text("")

        result = self.run_helper(
            "restore",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.log_path.read_text(), "")
        self.assertTrue(self.receipt.exists())

    def test_unsupported_masked_state_fails_closed(self) -> None:
        enabled = json.loads(self.enabled_path.read_text())
        enabled["deadlock-api.service"] = "masked"
        self.enabled_path.write_text(json.dumps(enabled, sort_keys=True))

        result = self.run_helper(
            "capture",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
            "--current-before",
            str(self.current),
            "--previous-before",
            str(self.previous),
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.log_path.read_text(), "")

    def test_installer_or_enable_failure_retains_receipt_and_retry_is_idempotent(self) -> None:
        self.capture()
        changed_enabled = {unit: "disabled" for unit in UNITS}
        for unit in STATIC_UNITS:
            changed_enabled[unit] = "static"
        self.enabled_path.write_text(json.dumps(changed_enabled, sort_keys=True))
        result = self.run_helper(
            "restore-enabled",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
            env={"PLATFORM_TEST_FAIL_ACTION": "enable"},
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.receipt.exists())

        self.run_helper(
            "restore-enabled",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
        )
        self.assertEqual(
            json.loads(self.enabled_path.read_text()), self._initial_enabled
        )

    def test_clear_requires_valid_receipt_and_is_durable(self) -> None:
        self.capture()
        self.run_helper(
            "clear",
            "--state",
            str(self.receipt),
            "--app-dir",
            str(self.app),
        )
        self.assertFalse(self.receipt.exists())


if __name__ == "__main__":
    unittest.main()
