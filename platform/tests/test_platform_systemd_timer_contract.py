from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools import platform_systemd_timer_contract as contract


REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_ROOT = REPO_ROOT / "platform" / "deploy" / "systemd"
SYSTEMD_INSTALLER = REPO_ROOT / "platform" / "tools" / "platform_install_systemd_units.sh"
MAINTENANCE_INSTALLER = REPO_ROOT / "platform" / "tools" / "platform_install_maintenance.sh"


def _read_lines(path: Path) -> list[list[str]]:
    """Read JSON-lines from a fake command log."""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _SystemdInstallerHarness:
    """Run copied installers against a temporary unit tree and fake daemons."""

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.platform = self.root / "platform"
        self.tools = self.platform / "tools"
        self.units = self.platform / "deploy" / "systemd"
        self.destination = self.root / "etc" / "systemd" / "system"
        self.app = self.root / "app"
        self.systemctl = self.root / "fake-systemctl"
        self.journalctl = self.root / "fake-journalctl"
        self.systemctl_log = self.root / "systemctl.jsonl"
        self.journalctl_log = self.root / "journalctl.jsonl"
        self.prepare_log = self.root / "prepare-service-user.log"
        self.state = self.root / "systemd-state.json"
        self._stage()

    def close(self) -> None:
        self.temporary.cleanup()

    def _stage(self) -> None:
        self.tools.mkdir(parents=True)
        self.units.mkdir(parents=True)
        self.destination.mkdir(parents=True)
        (self.app / "shared").mkdir(parents=True)
        for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
            shutil.copy2(source, self.units / source.name)
        for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
            shutil.copy2(source, self.units / source.name)

        deploy_root = REPO_ROOT / "platform" / "deploy"
        for directory in ("journald", "rsyslog", "logrotate"):
            shutil.copytree(deploy_root / directory, self.platform / "deploy" / directory)

        installer = self.tools / SYSTEMD_INSTALLER.name
        shutil.copy2(SYSTEMD_INSTALLER, installer)
        installer.chmod(0o755)
        maintenance = self.tools / MAINTENANCE_INSTALLER.name
        shutil.copy2(MAINTENANCE_INSTALLER, maintenance)
        maintenance.chmod(0o755)
        logging_installer = self.tools / "platform_install_logging.sh"
        shutil.copy2(
            REPO_ROOT / "platform" / "tools" / "platform_install_logging.sh",
            logging_installer,
        )
        logging_installer.chmod(0o755)
        prepare = self.tools / "platform_prepare_service_user.sh"
        prepare.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "printf '%s\\n' \"$*\" >> \"${FAKE_PREPARE_LOG}\"\n",
            encoding="ascii",
        )
        prepare.chmod(0o755)
        self.systemctl.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            "state_path = Path(os.environ['FAKE_SYSTEMD_STATE'])\n"
            "log_path = Path(os.environ['FAKE_SYSTEMCTL_LOG'])\n"
            "args = sys.argv[1:]\n"
            "log_path.open('a', encoding='utf-8').write(json.dumps(args) + '\\n')\n"
            "state = json.loads(state_path.read_text()) if state_path.exists() else {'active': {}, 'enabled': {}}\n"
            "action = args[0] if args else ''\n"
            "units = [value for value in args[1:] if not value.startswith('-')]\n"
            "fail = os.environ.get('FAKE_SYSTEMCTL_FAIL_ACTION')\n"
            "if action == fail:\n"
            "    raise SystemExit(int(os.environ.get('FAKE_SYSTEMCTL_FAIL_RC', '4')))\n"
            "if action == 'is-active':\n"
            "    unit = units[0]\n"
            "    value = state.get('active', {}).get(unit, 'inactive')\n"
            "    print(value)\n"
            "    raise SystemExit(0 if value == 'active' else 3)\n"
            "if action == 'is-enabled':\n"
            "    unit = units[0]\n"
            "    value = state.get('enabled', {}).get(unit, 'disabled')\n"
            "    print(value)\n"
            "    raise SystemExit(0 if value in {'enabled', 'static'} else 1)\n"
            "if action == 'daemon-reload':\n"
            "    raise SystemExit(0)\n"
            "active = state.setdefault('active', {})\n"
            "enabled = state.setdefault('enabled', {})\n"
            "if action == 'stop':\n"
            "    active[units[0]] = 'inactive'\n"
            "elif action == 'start':\n"
            "    active[units[0]] = 'active'\n"
            "elif action == 'disable':\n"
            "    for unit in units:\n"
            "        enabled[unit] = 'disabled'\n"
            "elif action == 'enable':\n"
            "    for unit in units:\n"
            "        enabled[unit] = 'enabled'\n"
            "        if '--now' in args:\n"
            "            active[unit] = 'active'\n"
            "state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "raise SystemExit(0)\n",
            encoding="ascii",
        )
        self.systemctl.chmod(0o755)
        self.journalctl.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['FAKE_JOURNALCTL_LOG']).open('a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "raise SystemExit(int(os.environ.get('FAKE_JOURNALCTL_RC', '0')))\n",
            encoding="ascii",
        )
        self.journalctl.chmod(0o755)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        (fake_bin / "systemctl").symlink_to(self.systemctl)
        (fake_bin / "journalctl").symlink_to(self.journalctl)

    def environment(self, **overrides: str) -> dict[str, str]:
        environment = {
            **os.environ,
            "PLATFORM_SYSTEMD_DIR": str(self.destination),
            "PLATFORM_APP_DIR": str(self.app),
            "PLATFORM_SYSTEMCTL_BIN": str(self.systemctl),
            "PLATFORM_JOURNALD_DIR": str(self.root / "etc" / "systemd" / "journald.conf.d"),
            "PLATFORM_RSYSLOG_DIR": str(self.root / "etc" / "rsyslog.d"),
            "PLATFORM_LOGROTATE_DIR": str(self.root / "etc" / "logrotate.d"),
            "FAKE_SYSTEMD_STATE": str(self.state),
            "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
            "FAKE_JOURNALCTL_LOG": str(self.journalctl_log),
            "FAKE_PREPARE_LOG": str(self.prepare_log),
            "PATH": str(self.root / "bin") + os.pathsep + os.environ.get("PATH", ""),
        }
        environment.update(overrides)
        return environment

    def run_systemd(self, *, enable: bool = True, allow_retired: bool = False, **overrides: str) -> subprocess.CompletedProcess[str]:
        installer = self.tools / SYSTEMD_INSTALLER.name
        source = SYSTEMD_INSTALLER.read_text(encoding="utf-8")
        if allow_retired:
            source = source.replace(
                'if [[ "$SYSTEMD_DEST_DIR" == "/etc/systemd/system" && "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then',
                'if [[ "$ENABLE_SYSTEMD_UNITS" == "1" ]]; then',
                1,
            )
            installer.write_text(source, encoding="utf-8")
            installer.chmod(0o755)
        environment = self.environment(
            PLATFORM_ENABLE_SYSTEMD_UNITS="1" if enable else "0",
            **overrides,
        )
        return subprocess.run(
            [str(installer)],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )

    def run_maintenance(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = self.environment(**overrides)
        return subprocess.run(
            [str(self.tools / MAINTENANCE_INSTALLER.name)],
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=20,
        )


class PlatformSystemdTimerContractTests(unittest.TestCase):
    def test_unit_graph_schedule_and_failure_policy(self) -> None:
        units = contract.validate_all()
        self.assertEqual(set(units), set(contract.EXPECTED_UNITS))
        self.assertNotIn(contract.OFFSITE_TIMER, contract.EXPECTED_SYSTEMD_INSTALL_ENABLE[1])
        self.assertNotRegex(
            "\n".join(unit.text for unit in units.values()),
            contract._FORBIDDEN_MARKERS,
        )

    def test_parser_rejects_missing_target_and_schedule_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                shutil.copy2(source, root / source.name)
            for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                shutil.copy2(source, root / source.name)

            (root / "deadlock-maintenance.service").unlink()
            with self.assertRaisesRegex(contract.SystemdContractError, "missing"):
                contract.validate_all(root)

            shutil.copy2(
                SYSTEMD_ROOT / "deadlock-maintenance.service",
                root / "deadlock-maintenance.service",
            )
            timer = root / "deadlock-maintenance.timer"
            timer.write_text(
                timer.read_text(encoding="utf-8").replace("04:15:00", "04:16:00"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(contract.SystemdContractError, "OnCalendar"):
                contract.validate_all(root)

    def test_installers_have_closed_enable_sets_and_offsite_is_not_enabled(self) -> None:
        systemd_installer = SYSTEMD_INSTALLER.read_text(encoding="utf-8")
        maintenance_installer = MAINTENANCE_INSTALLER.read_text(encoding="utf-8")
        self.assertIn(
            "run_systemctl enable deadlock-api.service deadlock-worker.service deadlock-web.service",
            systemd_installer,
        )
        self.assertIn(
            "run_systemctl enable --now \\\n    deadlock-maintenance.timer \\\n    deadlock-logrotate.timer \\\n    deadlock-cloudflare-ips.timer \\\n    deadlock-health-monitor.timer",
            systemd_installer,
        )
        self.assertIn(
            "systemctl enable --now deadlock-maintenance.timer deadlock-logrotate.timer",
            maintenance_installer,
        )
        self.assertNotIn(
            f"run_systemctl enable {contract.OFFSITE_TIMER}",
            systemd_installer,
        )
        self.assertNotIn(
            f"systemctl enable --now {contract.OFFSITE_TIMER}",
            maintenance_installer,
        )
        self.assertIn("PLATFORM_ENABLE_SYSTEMD_UNITS", systemd_installer)

    def test_systemd_installer_enables_exact_units_and_is_idempotent(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            first = harness.run_systemd()
            self.assertEqual(first.returncode, 0, first.stderr)
            expected = [
                ["daemon-reload"],
                ["enable", *contract.EXPECTED_SYSTEMD_INSTALL_ENABLE[0]],
                ["enable", "--now", *contract.EXPECTED_SYSTEMD_INSTALL_ENABLE[1]],
            ]
            self.assertEqual(_read_lines(harness.systemctl_log), expected)
            self.assertFalse(
                any(contract.OFFSITE_TIMER in call for call in expected),
                "offsite timer must remain disabled by default",
            )

            second = harness.run_systemd()
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(_read_lines(harness.systemctl_log), expected * 2)
            self.assertEqual(
                sorted(path.name for path in harness.destination.glob("deadlock-*")),
                sorted(contract.EXPECTED_UNITS),
            )

            disabled = harness.run_systemd(enable=False)
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assertEqual(
                _read_lines(harness.systemctl_log)[-1:], [["daemon-reload"]]
            )
        finally:
            harness.close()

    def test_systemd_installer_fails_before_enable_after_reload_failure(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_systemd(FAKE_SYSTEMCTL_FAIL_ACTION="daemon-reload")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(_read_lines(harness.systemctl_log), [["daemon-reload"]])
        finally:
            harness.close()

    def test_systemd_installer_retired_failure_restores_start_and_enable(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            retired = harness.destination / "deadlock-retired.service"
            retired.write_text("[Unit]\nDescription=retired\n", encoding="ascii")
            retired.chmod(0o644)
            harness.state.write_text(
                json.dumps(
                    {
                        "active": {retired.name: "active"},
                        "enabled": {retired.name: "enabled"},
                    }
                ),
                encoding="ascii",
            )
            result = harness.run_systemd(
                allow_retired=True,
                FAKE_SYSTEMCTL_FAIL_ACTION="disable",
            )
            self.assertNotEqual(result.returncode, 0)
            log = _read_lines(harness.systemctl_log)
            self.assertIn(["stop", retired.name], log)
            self.assertIn(["disable", retired.name], log)
            self.assertIn(["start", retired.name], log)
            self.assertIn(["enable", retired.name], log)
            self.assertTrue(retired.exists())
            state = json.loads(harness.state.read_text(encoding="utf-8"))
            self.assertEqual(state["active"][retired.name], "active")
            self.assertEqual(state["enabled"][retired.name], "enabled")
        finally:
            harness.close()

    def test_maintenance_installer_enables_only_its_two_timers_and_is_idempotent(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            first = harness.run_maintenance()
            self.assertEqual(first.returncode, 0, first.stderr)
            expected_systemctl = [
                ["daemon-reload"],
                ["enable", "--now", *contract.EXPECTED_MAINTENANCE_INSTALL_ENABLE[0]],
            ]
            self.assertEqual(_read_lines(harness.systemctl_log), expected_systemctl)
            self.assertEqual(
                _read_lines(harness.journalctl_log),
                [["--rotate"], ["--vacuum-time=30d"], ["--vacuum-size=256M"]],
            )
            self.assertFalse(any(contract.OFFSITE_TIMER in call for call in expected_systemctl))

            second = harness.run_maintenance()
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(_read_lines(harness.systemctl_log), expected_systemctl * 2)
            self.assertEqual(
                _read_lines(harness.journalctl_log),
                [["--rotate"], ["--vacuum-time=30d"], ["--vacuum-size=256M"]] * 2,
            )
        finally:
            harness.close()

    def test_maintenance_installer_fails_before_enable_on_systemctl_failure(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_maintenance(FAKE_SYSTEMCTL_FAIL_ACTION="daemon-reload")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(_read_lines(harness.systemctl_log), [["daemon-reload"]])
            self.assertEqual(_read_lines(harness.journalctl_log), [])
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
