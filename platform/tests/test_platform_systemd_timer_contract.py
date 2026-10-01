from __future__ import annotations

import json
from itertools import permutations
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import platform_release_systemd_state as release_state
from tools import platform_systemd_timer_contract as contract


REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_ROOT = REPO_ROOT / "platform" / "deploy" / "systemd"
SYSTEMD_INSTALLER = REPO_ROOT / "platform" / "tools" / "platform_install_systemd_units.sh"
MAINTENANCE_INSTALLER = REPO_ROOT / "platform" / "tools" / "platform_install_maintenance.sh"

# Keep command expectations independent from the production contract constants.
# If both are accidentally changed together, these assertions must still catch
# an installer widening its enable set or enabling off-site backup.
EXPECTED_SYSTEMD_SERVICE_ENABLE = (
    "deadlock-api.service",
    "deadlock-worker.service",
    "deadlock-web.service",
)
EXPECTED_SYSTEMD_TIMER_ENABLE = (
    "deadlock-maintenance.timer",
    "deadlock-logrotate.timer",
    "deadlock-cloudflare-ips.timer",
    "deadlock-health-monitor.timer",
)
EXPECTED_MAINTENANCE_TIMER_ENABLE = (
    "deadlock-maintenance.timer",
    "deadlock-logrotate.timer",
)


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
            "if action == 'enable' and '--now' in args and os.environ.get('FAKE_SYSTEMCTL_FAIL_ENABLE_NOW') == '1':\n"
            "    raise SystemExit(int(os.environ.get('FAKE_SYSTEMCTL_FAIL_RC', '4')))\n"
            "if action == 'start' and os.environ.get('FAKE_SYSTEMCTL_FAIL_START') == '1':\n"
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
            "    count_path = os.environ.get('FAKE_SYSTEMCTL_RELOAD_COUNT')\n"
            "    count = 0\n"
            "    if count_path:\n"
            "        marker = Path(count_path)\n"
            "        count = int(marker.read_text()) if marker.exists() else 0\n"
            "        count += 1\n"
            "        marker.write_text(str(count))\n"
            "        if os.environ.get('FAKE_SYSTEMCTL_FAIL_RELOAD_AT') == str(count):\n"
            "            raise SystemExit(int(os.environ.get('FAKE_SYSTEMCTL_FAIL_RC', '4')))\n"
            "    raise SystemExit(0)\n"
            "active = state.setdefault('active', {})\n"
            "enabled = state.setdefault('enabled', {})\n"
            "partial_after = int(os.environ.get('FAKE_SYSTEMCTL_PARTIAL_ENABLE_NOW_AFTER', '0'))\n"
            "if action == 'enable' and '--now' in args and partial_after > 0:\n"
            "    for index, unit in enumerate(units, 1):\n"
            "        enabled[unit] = 'enabled'\n"
            "        active[unit] = 'active'\n"
            "        state_path.write_text(json.dumps(state, sort_keys=True))\n"
            "        if index >= partial_after:\n"
            "            raise SystemExit(int(os.environ.get('FAKE_SYSTEMCTL_FAIL_RC', '4')))\n"
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
            "PLATFORM_JOURNALCTL_BIN": str(self.journalctl),
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
        self.assertEqual(
            contract.EXPECTED_SERVICES,
            contract.LONG_RUNNING_SERVICES + contract.ONESHOT_SERVICES,
        )
        self.assertEqual(
            set(contract.LONG_RUNNING_SERVICES),
            {
                "deadlock-api.service",
                "deadlock-worker.service",
                "deadlock-web.service",
            },
        )
        self.assertEqual(
            set(contract.ONESHOT_SERVICES),
            {
                "deadlock-maintenance.service",
                "deadlock-logrotate.service",
                "deadlock-offsite-backup.service",
                "deadlock-cloudflare-ips.service",
                "deadlock-health-monitor.service",
            },
        )
        self.assertNotIn(contract.OFFSITE_TIMER, contract.EXPECTED_SYSTEMD_INSTALL_ENABLE[1])
        self.assertNotRegex(
            "\n".join(unit.text for unit in units.values()),
            contract._FORBIDDEN_MARKERS,
        )

    def test_service_contract_is_explicit_and_worker_has_no_startup_refresh_hook(self) -> None:
        units = contract.validate_all()
        self.assertEqual(
            set(contract._EXPECTED_SERVICE_SECTIONS),
            set(contract.EXPECTED_SERVICES),
        )
        self.assertEqual(
            units["deadlock-worker.service"].values("Service", "ExecStartPost"),
            (),
        )
        for service_name in contract.LONG_RUNNING_SERVICES:
            service = units[service_name]
            self.assertEqual(service.one("Service", "Type"), "simple")
            self.assertEqual(len(service.values("Service", "ExecStart")), 1)
            self.assertEqual(
                len(service.values("Service", "Environment")),
                5,
            )
            self.assertEqual(service.one("Service", "RestartSec"), "5")
        for service_name in contract.ONESHOT_SERVICES:
            self.assertEqual(
                units[service_name].one("Service", "Type"),
                "oneshot",
            )

    def test_inventory_matches_release_state_and_both_installers(self) -> None:
        self.assertEqual(tuple(release_state.OWNED_UNITS), contract.EXPECTED_UNITS)
        systemd_installer = SYSTEMD_INSTALLER.read_text(encoding="utf-8")
        maintenance_installer = MAINTENANCE_INSTALLER.read_text(encoding="utf-8")
        for unit_name in contract.EXPECTED_UNITS:
            self.assertIn(unit_name, systemd_installer)
        for unit_name in (
            "deadlock-maintenance.service",
            "deadlock-maintenance.timer",
            "deadlock-offsite-backup.service",
            "deadlock-offsite-backup.timer",
            "deadlock-logrotate.service",
            "deadlock-logrotate.timer",
        ):
            self.assertIn(unit_name, maintenance_installer)

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

    def test_parser_rejects_symlink_escape_replacement_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "units"
            root.mkdir()
            for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                shutil.copy2(source, root / source.name)
            for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                shutil.copy2(source, root / source.name)

            target = root / "deadlock-maintenance.timer"
            outside = root.parent / "outside.timer"
            outside.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            target.unlink()
            target.symlink_to(outside)
            with self.assertRaisesRegex(contract.SystemdContractError, "symlink"):
                contract.load_units(root)

            # A replacement between directory enumeration and open must not
            # turn the trusted regular-file check into a symlink read.
            target.unlink()
            target.write_text(outside.read_text(encoding="utf-8"), encoding="utf-8")
            replacement_done = False
            original_lstat = contract.os.lstat

            def replace_after_lstat(
                path: object, *args: object, **kwargs: object
            ) -> os.stat_result:
                nonlocal replacement_done
                metadata = original_lstat(path, *args, **kwargs)
                if Path(path).name == target.name and not replacement_done:
                    replacement_done = True
                    target.unlink()
                    target.symlink_to(outside)
                return metadata

            with mock.patch.object(contract.os, "lstat", side_effect=replace_after_lstat):
                with self.assertRaisesRegex(contract.SystemdContractError, "open|symlink"):
                    contract.load_units(root)

            target.unlink()
            hardlink_source = root.parent / "hardlink-source.timer"
            shutil.copy2(SYSTEMD_ROOT / target.name, hardlink_source)
            target.hardlink_to(hardlink_source)
            with self.assertRaisesRegex(contract.SystemdContractError, "hard-link"):
                contract.load_units(root)

    def test_parser_rejects_owner_group_and_mode_drift(self) -> None:
        mutations = (
            ("mode", lambda path: path.chmod(0o600), "mode"),
            ("owner", lambda path: os.chown(path, 1, -1), "ownership"),
            ("group", lambda path: os.chown(path, -1, 1), "ownership"),
        )
        for label, mutate, message in mutations:
            with self.subTest(metadata=label):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    mutate(root / "deadlock-maintenance.timer")
                    with self.assertRaisesRegex(contract.SystemdContractError, message):
                        contract.load_units(root)

    def test_parser_rejects_replacement_after_open_before_final_lstat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                shutil.copy2(source, root / source.name)
            for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                shutil.copy2(source, root / source.name)

            target = root / "deadlock-maintenance.timer"
            outside = root.parent / "outside.timer"
            outside.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
            original_open = contract.os.open
            replacement_done = False

            def replace_after_open(*args: object, **kwargs: object) -> int:
                nonlocal replacement_done
                descriptor = original_open(*args, **kwargs)
                if (
                    Path(args[0]).name == target.name
                    and kwargs.get("dir_fd") is not None
                    and not replacement_done
                ):
                    replacement_done = True
                    target.unlink()
                    target.symlink_to(outside)
                return descriptor

            with mock.patch.object(contract.os, "open", side_effect=replace_after_open):
                with self.assertRaisesRegex(
                    contract.SystemdContractError, "changed|symlink|hard-linked"
                ):
                    contract.load_units(root)

    def test_parser_rejects_root_directory_replacement_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "units"
            root.mkdir()
            for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                shutil.copy2(source, root / source.name)
            for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                shutil.copy2(source, root / source.name)
            replacement = parent / "replacement"
            replacement.mkdir()
            for source in root.iterdir():
                shutil.copy2(source, replacement / source.name)

            original_open = contract.os.open
            replacement_done = False

            def replace_root_after_open(*args: object, **kwargs: object) -> int:
                nonlocal replacement_done
                descriptor = original_open(*args, **kwargs)
                if (
                    not replacement_done
                    and kwargs.get("dir_fd") is None
                    and Path(args[0]) == root
                ):
                    replacement_done = True
                    root.rename(parent / "original-units")
                    replacement.rename(root)
                return descriptor

            with mock.patch.object(contract.os, "open", side_effect=replace_root_after_open):
                with self.assertRaisesRegex(
                    contract.SystemdContractError, "root.*replaced|root.*changed"
                ):
                    contract.load_units(root)

    def test_parser_rejects_root_directory_enumeration_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                shutil.copy2(source, root / source.name)
            for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                shutil.copy2(source, root / source.name)

            original_listdir = contract.os.listdir
            mutated = False

            def mutate_after_first_enumeration(path: object) -> list[str]:
                nonlocal mutated
                entries = original_listdir(path)
                if not mutated and isinstance(path, int):
                    mutated = True
                    (root / "transient-not-a-unit.txt").write_text("unexpected", encoding="ascii")
                return entries

            with mock.patch.object(
                contract.os, "listdir", side_effect=mutate_after_first_enumeration
            ):
                with self.assertRaisesRegex(
                    contract.SystemdContractError, "root.*(enumeration|changed)"
                ):
                    contract.load_units(root)

    def test_parser_rejects_same_size_in_place_mutation_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                shutil.copy2(source, root / source.name)
            for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                shutil.copy2(source, root / source.name)

            target = root / "deadlock-maintenance.timer"
            before = os.stat(target, follow_symlinks=False)
            original_open = contract.os.open
            mutated = False

            def mutate_after_open(*args: object, **kwargs: object) -> int:
                nonlocal mutated
                descriptor = original_open(*args, **kwargs)
                if (
                    not mutated
                    and kwargs.get("dir_fd") is not None
                    and Path(args[0]).name == target.name
                ):
                    mutated = True
                    payload = target.read_bytes()
                    replacement = payload.replace(b"04:15:00", b"04:16:00", 1)
                    self.assertEqual(len(replacement), len(payload))
                    target.write_bytes(replacement)
                    os.utime(
                        target,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                        follow_symlinks=False,
                    )
                    after = os.stat(target, follow_symlinks=False)
                    self.assertEqual(after.st_size, before.st_size)
                    self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(after.st_ino, before.st_ino)
                    self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
                return descriptor

            with mock.patch.object(contract.os, "open", side_effect=mutate_after_open):
                with self.assertRaisesRegex(
                    contract.SystemdContractError, "changed while being read"
                ):
                    contract.load_units(root)
            self.assertTrue(mutated)

    def test_failure_contract_rejects_oneshot_failure_hiding_mutations(self) -> None:
        mutations = (
            (
                "SuccessExitStatus=143\n",
                "SuccessExitStatus",
            ),
            (
                "ExecStartPost=-/bin/true\n",
                "fail closed",
            ),
            (
                "ExecStartPre=-/bin/true\n",
                "fail closed",
            ),
            (
                "ExecCondition=-/bin/true\n",
                "fail closed",
            ),
            (
                "ConditionPathIsSymbolicLink=-/tmp/not-current\n",
                "must not hide a failure",
            ),
            (
                "Restart=on-failure\nRestartSec=5\n",
                "restart directives",
            ),
        )
        for addition, message in mutations:
            with self.subTest(addition=addition):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    service = root / "deadlock-maintenance.service"
                    service.write_text(
                        service.read_text(encoding="utf-8") + addition,
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(contract.SystemdContractError, message):
                        contract.validate_all(root)

    def test_failure_contract_rejects_combined_ignored_exec_prefixes(self) -> None:
        # systemd accepts these command-prefix combinations in any order;
        # every one still makes a non-zero command status non-fatal.
        for prefix in ("+-", "@-", ":-", "!-", "!!-"):
            with self.subTest(prefix=prefix):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    service = root / "deadlock-maintenance.service"
                    text = service.read_text(encoding="utf-8")
                    text = text.replace(
                        "ExecStart=/opt/oldsparky/platform/shared/venv/bin/python",
                        f"ExecStart={prefix}/bin/true",
                        1,
                    )
                    service.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(contract.SystemdContractError, "fail closed"):
                        contract.validate_all(root)

    def test_ignored_exec_prefix_detector_covers_all_systemd_combinations(self) -> None:
        markers = ("@", ":", "+", "!", "!!", "-")
        prefixes = {
            "".join(parts)
            for length in range(1, len(markers) + 1)
            for parts in permutations(markers, length)
            if "-" in parts
        }
        self.assertGreater(len(prefixes), 100)
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                self.assertTrue(
                    contract._has_ignored_exec_prefix(prefix + "/bin/true")
                )
        for prefix in ("", "@", ":", "+", "!", "!!", "@:+!!"):
            with self.subTest(prefix=prefix, no_ignore=True):
                self.assertFalse(
                    contract._has_ignored_exec_prefix(prefix + "/bin/true")
                )

    def test_failure_contract_scans_every_exec_lifecycle_directive(self) -> None:
        directives = (
            "ExecStartPre",
            "ExecStartPost",
            "ExecStartReload",
            "ExecStop",
            "ExecStopPost",
            "ExecCondition",
        )
        for directive in directives:
            with self.subTest(directive=directive):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    service = root / "deadlock-maintenance.service"
                    with service.open("a", encoding="utf-8") as stream:
                        stream.write(f"{directive}=+-/bin/true\n")
                    with self.assertRaisesRegex(
                        contract.SystemdContractError,
                        "must fail closed",
                    ):
                        contract.validate_all(root)

    def test_failure_contract_scans_condition_prefix_combinations(self) -> None:
        for prefix in ("-", "+-", "@-", ":-", "!-", "!!-"):
            with self.subTest(prefix=prefix):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    service = root / "deadlock-maintenance.service"
                    with service.open("a", encoding="utf-8") as stream:
                        stream.write(
                            f"ConditionPathIsSymbolicLink={prefix}/tmp/not-current\n"
                        )
                    with self.assertRaisesRegex(
                        contract.SystemdContractError,
                        "must not hide a failure",
                    ):
                        contract.validate_all(root)

    def test_failure_contract_rejects_encoded_or_ambiguous_execstart_values(self) -> None:
        # systemd decodes these forms before applying command prefixes.  The
        # contract intentionally rejects them instead of maintaining a partial
        # quote/C-escape parser that could drift from the manager.
        mutations = (
            'ExecStart="!!-/bin/false"\n',
            'ExecStart="+-/bin/false"\n',
            "ExecStart=@/bin/false\n",
            "ExecStart=:/bin/false\n",
            "ExecStart=+/bin/false\n",
            "ExecStart=!/bin/false\n",
            "ExecStart=!!/bin/false\n",
            "ExecStart=!!-/bin/false\n",
            "ExecStart=+-/bin/false\n",
            "ExecStart=\\x2d/bin/false\n",
            "ExecStart=\\055/bin/false\n",
            "ExecStart='/bin/true'\n",
            "ExecStart=\"/bin/true\"\n",
            "ExecStart=/bin/true  --flag\n",
            "ExecStart=/bin/true\t--flag\n",
        )
        for addition in mutations:
            with self.subTest(addition=addition):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    service = root / "deadlock-maintenance.service"
                    service.write_text(
                        service.read_text(encoding="utf-8") + addition,
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        contract.SystemdContractError, "fail closed"
                    ):
                        contract.validate_all(root)

    def test_parser_models_execstart_reset_and_order_before_exact_policy(self) -> None:
        parsed = contract.parse_unit(
            Path("deadlock-maintenance.service"),
            text=(
                "[Service]\n"
                "ExecStart=/bin/true\n"
                "ExecStart=-/bin/false\n"
                "ExecStart=\n"
                "ExecStart=/bin/echo ok\n"
            ),
        )
        self.assertEqual(
            parsed.effective_resettable_values("Service", "ExecStart"),
            ("/bin/echo ok",),
        )

    def test_failure_contract_rejects_execstart_drift(self) -> None:
        mutations = (
            "ExecStart=\n",
            "ExecStart=/bin/true\nExecStart=/bin/false\n",
            "ExecStart=/bin/true\nExecStart=\n",
            "ExecStart=-/bin/false\nExecStart=\nExecStart=/bin/true\n",
            "ExecStart=\nExecStart=-/bin/false\n",
        )
        for addition in mutations:
            with self.subTest(addition=addition):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.service"):
                        shutil.copy2(source, root / source.name)
                    for source in SYSTEMD_ROOT.glob("deadlock-*.timer"):
                        shutil.copy2(source, root / source.name)
                    service = root / "deadlock-maintenance.service"
                    service.write_text(
                        service.read_text(encoding="utf-8") + addition,
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        contract.SystemdContractError, "ExecStart"
                    ):
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
            "run_systemctl enable --now deadlock-maintenance.timer deadlock-logrotate.timer",
            maintenance_installer,
        )
        self.assertIn("run_journalctl --rotate", maintenance_installer)
        self.assertIn('SYSTEMD_TIMEOUT_SECONDS=30', maintenance_installer)
        self.assertIn('--kill-after=5s', maintenance_installer)
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
                ["enable", *EXPECTED_SYSTEMD_SERVICE_ENABLE],
                ["enable", "--now", *EXPECTED_SYSTEMD_TIMER_ENABLE],
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

    def test_systemd_installer_stops_after_service_enable_failure(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_systemd(FAKE_SYSTEMCTL_FAIL_ACTION="enable")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                _read_lines(harness.systemctl_log),
                [
                    ["daemon-reload"],
                    ["enable", *EXPECTED_SYSTEMD_SERVICE_ENABLE],
                ],
            )
            state = (
                json.loads(harness.state.read_text(encoding="utf-8"))
                if harness.state.exists()
                else {"active": {}, "enabled": {}}
            )
            self.assertEqual(state, {"active": {}, "enabled": {}})
        finally:
            harness.close()

    def test_systemd_installer_stops_after_enable_now_failure(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_systemd(FAKE_SYSTEMCTL_FAIL_ENABLE_NOW="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                _read_lines(harness.systemctl_log),
                [
                    ["daemon-reload"],
                    ["enable", *EXPECTED_SYSTEMD_SERVICE_ENABLE],
                    ["enable", "--now", *EXPECTED_SYSTEMD_TIMER_ENABLE],
                ],
            )
            state = json.loads(harness.state.read_text(encoding="utf-8"))
            self.assertEqual(
                state["enabled"],
                {unit: "enabled" for unit in EXPECTED_SYSTEMD_SERVICE_ENABLE},
            )
            self.assertEqual(state["active"], {})
        finally:
            harness.close()

    def test_systemd_installer_partial_enable_now_is_fail_visible_and_rolls_back_retired(
        self,
    ) -> None:
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
                FAKE_SYSTEMCTL_PARTIAL_ENABLE_NOW_AFTER="1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Installed platform", result.stdout)
            log = _read_lines(harness.systemctl_log)
            self.assertIn(
                ["enable", "--now", *EXPECTED_SYSTEMD_TIMER_ENABLE],
                log,
            )
            self.assertIn(["start", retired.name], log)
            self.assertIn(["enable", retired.name], log)
            self.assertTrue(retired.exists())
            state = json.loads(harness.state.read_text(encoding="utf-8"))
            self.assertEqual(state["active"][retired.name], "active")
            self.assertEqual(state["enabled"][retired.name], "enabled")
            self.assertEqual(
                state["active"][EXPECTED_SYSTEMD_TIMER_ENABLE[0]], "active"
            )
            self.assertEqual(
                state["enabled"][EXPECTED_SYSTEMD_TIMER_ENABLE[0]], "enabled"
            )
            self.assertNotIn(EXPECTED_SYSTEMD_TIMER_ENABLE[1], state["active"])
            self.assertNotIn(EXPECTED_SYSTEMD_TIMER_ENABLE[1], state["enabled"])
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

    def test_systemd_installer_post_install_reload_failure_restores_retired_unit(self) -> None:
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
                encoding="utf-8",
            )
            result = harness.run_systemd(
                allow_retired=True,
                FAKE_SYSTEMCTL_RELOAD_COUNT=str(harness.root / "reload-count"),
                FAKE_SYSTEMCTL_FAIL_RELOAD_AT="2",
            )
            self.assertNotEqual(result.returncode, 0)
            log = _read_lines(harness.systemctl_log)
            self.assertIn(["daemon-reload"], log)
            self.assertGreaterEqual(log.count(["daemon-reload"]), 3)
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

    def test_systemd_installer_retains_rollback_receipt_when_rollback_fails(self) -> None:
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
                encoding="utf-8",
            )
            result = harness.run_systemd(
                allow_retired=True,
                FAKE_SYSTEMCTL_FAIL_ACTION="disable",
                FAKE_SYSTEMCTL_FAIL_START="1",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(retired.exists())
            self.assertTrue(
                (harness.destination / ".oldsparky-retired-rollback-status").exists()
            )
            self.assertTrue(list(harness.destination.glob(".oldsparky-retired.*")))
            self.assertIn(["start", retired.name], _read_lines(harness.systemctl_log))
        finally:
            harness.close()

    def test_maintenance_installer_enables_only_its_two_timers_and_is_idempotent(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            first = harness.run_maintenance()
            self.assertEqual(first.returncode, 0, first.stderr)
            expected_systemctl = [
                ["daemon-reload"],
                ["enable", "--now", *EXPECTED_MAINTENANCE_TIMER_ENABLE],
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

    def test_maintenance_installer_stops_after_enable_now_failure(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_maintenance(FAKE_SYSTEMCTL_FAIL_ENABLE_NOW="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                _read_lines(harness.systemctl_log),
                [
                    ["daemon-reload"],
                    ["enable", "--now", *EXPECTED_MAINTENANCE_TIMER_ENABLE],
                ],
            )
            self.assertEqual(_read_lines(harness.journalctl_log), [])
        finally:
            harness.close()

    def test_maintenance_installer_fails_closed_on_systemctl_timeout(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_maintenance(
                FAKE_SYSTEMCTL_FAIL_ACTION="daemon-reload",
                FAKE_SYSTEMCTL_FAIL_RC="124",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(_read_lines(harness.systemctl_log), [["daemon-reload"]])
            self.assertEqual(_read_lines(harness.journalctl_log), [])
        finally:
            harness.close()

    def test_maintenance_installer_fails_closed_on_journalctl_timeout(self) -> None:
        harness = _SystemdInstallerHarness()
        try:
            result = harness.run_maintenance(FAKE_JOURNALCTL_RC="124")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                _read_lines(harness.systemctl_log),
                [
                    ["daemon-reload"],
                    ["enable", "--now", *EXPECTED_MAINTENANCE_TIMER_ENABLE],
                ],
            )
            self.assertEqual(_read_lines(harness.journalctl_log), [["--rotate"]])
        finally:
            harness.close()


if __name__ == "__main__":
    unittest.main()
