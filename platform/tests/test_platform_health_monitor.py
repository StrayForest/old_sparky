from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_health_monitor.py"
PLATFORM_ROOT = SCRIPT_PATH.parents[1]
TOOLS_ROOT = SCRIPT_PATH.parent
for import_root in (str(PLATFORM_ROOT), str(TOOLS_ROOT)):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

from tools import platform_storage_maintenance as STORAGE  # noqa: E402
from tools.platform_disk_policy import (  # noqa: E402
    BYTES_PER_GIB,
    snapshot_from_usage,
)

SPEC = importlib.util.spec_from_file_location("platform_health_monitor", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlatformHealthMonitorTests(unittest.TestCase):
    def test_backup_requires_fresh_restore_verified_archive(self) -> None:
        now = datetime(2026, 8, 1, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            dump_path = directory / "platformdb-20260801T110000Z.dump"
            dump_path.write_bytes(b"not-empty")
            metadata = {
                "completed_at_utc": (now - timedelta(hours=1)).isoformat(),
                "dump_file": dump_path.name,
                "restore_verified": True,
            }
            (directory / "platformdb-20260801T110000Z.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )

            result = MODULE.check_backup(directory, max_age_hours=36, now=now)

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["restore_verified"], True)

    def test_backup_rejects_stale_archive(self) -> None:
        now = datetime(2026, 8, 1, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temporary_dir:
            directory = Path(temporary_dir)
            dump_path = directory / "platformdb-20260730T000000Z.dump"
            dump_path.write_bytes(b"not-empty")
            (directory / "platformdb-20260730T000000Z.json").write_text(
                json.dumps(
                    {
                        "completed_at_utc": (now - timedelta(hours=60)).isoformat(),
                        "dump_file": dump_path.name,
                        "restore_verified": True,
                    }
                ),
                encoding="utf-8",
            )

            stale = MODULE.check_backup(directory, max_age_hours=36, now=now)

        self.assertFalse(stale.ok)
        self.assertGreater(stale.detail["age_hours"], 36)

    def test_memory_check_uses_available_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            meminfo = Path(temporary_dir) / "meminfo"
            meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 120 kB\n", encoding="utf-8")

            result = MODULE.check_memory(min_available_percent=10, meminfo_path=meminfo)

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["available_percent"], 12.0)

    @patch.object(MODULE.subprocess, "run")
    def test_certificate_check_never_returns_command_output(self, run_mock) -> None:
        run_mock.return_value.returncode = 1
        run_mock.return_value.stdout = "sensitive output"
        run_mock.return_value.stderr = "sensitive error"

        result = MODULE.check_certificate(Path("/safe/cert.pem"), min_days=30)

        self.assertFalse(result.ok)
        self.assertNotIn("sensitive", json.dumps(result.detail))

    def test_api_check_rejects_non_loopback_url_without_request(self) -> None:
        with patch.object(MODULE, "urlopen") as open_mock:
            result = MODULE.check_api_ready("https://example.com/health", timeout=1)

        self.assertFalse(result.ok)
        self.assertEqual(result.detail["error"], "non_loopback_url")
        open_mock.assert_not_called()

    def test_disk_check_accepts_exact_hard_boundaries(self) -> None:
        cases = (
            (100 * BYTES_PER_GIB, 15 * BYTES_PER_GIB, "percent"),
            (20 * BYTES_PER_GIB, 5 * BYTES_PER_GIB, "free"),
        )
        for total, free, boundary in cases:
            with self.subTest(boundary=boundary), patch.object(
                MODULE,
                "snapshot_for_path",
                return_value=snapshot_from_usage(
                    SimpleNamespace(total=total, free=free)
                ),
            ):
                result = MODULE.check_disk(Path("/safe"))

            self.assertTrue(result.ok)
            self.assertEqual(result.detail["minimum_free_gib"], 5.0)
            if boundary == "percent":
                self.assertEqual(result.detail["used_percent"], 85.0)
            else:
                self.assertEqual(result.detail["free_gib"], 5.0)

    def test_disk_check_rejects_each_hard_limit(self) -> None:
        cases = (
            (100 * BYTES_PER_GIB, 14 * BYTES_PER_GIB, "percent"),
            (20 * BYTES_PER_GIB, 4 * BYTES_PER_GIB, "free"),
        )
        for total, free, boundary in cases:
            with self.subTest(boundary=boundary), patch.object(
                MODULE,
                "snapshot_for_path",
                return_value=snapshot_from_usage(
                    SimpleNamespace(total=total, free=free)
                ),
            ):
                result = MODULE.check_disk(Path("/safe"))

            self.assertFalse(result.ok)

    def test_disk_check_fails_closed_for_invalid_total(self) -> None:
        with patch.object(
            MODULE,
            "snapshot_for_path",
            return_value=snapshot_from_usage(SimpleNamespace(total=0, free=0)),
        ):
            result = MODULE.check_disk(Path("/safe"))

        self.assertFalse(result.ok)
        self.assertEqual(result.detail["error"], "invalid_usage")

    def test_parser_defaults_match_canonical_disk_policy(self) -> None:
        with patch.object(MODULE.sys, "argv", [str(SCRIPT_PATH)]):
            args = MODULE.parse_args()

        self.assertEqual(args.disk_min_free_gib, 5.0)
        self.assertEqual(args.disk_max_used_percent, 85.0)

    def test_storage_and_health_share_conservative_disk_policy(self) -> None:
        snapshot = snapshot_from_usage(
            SimpleNamespace(total=100 * BYTES_PER_GIB, free=15 * BYTES_PER_GIB)
        )
        with patch.object(MODULE, "snapshot_for_path", return_value=snapshot), patch.object(
            STORAGE, "disk_snapshot_for_path", return_value=snapshot
        ):
            health_result = MODULE.check_disk(Path("/safe"))
            storage_result = STORAGE.disk_snapshot(Path("/safe"))

        self.assertTrue(health_result.ok)
        self.assertEqual(storage_result["used_bytes"], 85 * BYTES_PER_GIB)
        self.assertEqual(storage_result["free_bytes"], 15 * BYTES_PER_GIB)
        self.assertEqual(storage_result["used_percent"], 85.0)
        self.assertTrue(
            STORAGE.disk_is_healthy(
                snapshot,
                min_free_bytes=5 * BYTES_PER_GIB,
                max_used_percent=85.0,
            )
        )

    def test_staged_health_monitor_imports_sibling_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            staged_tools = root / "candidate" / "tools"
            staged_tools.mkdir(parents=True)
            shutil.copyfile(SCRIPT_PATH, staged_tools / SCRIPT_PATH.name)
            shutil.copyfile(
                TOOLS_ROOT / "platform_disk_policy.py",
                staged_tools / "platform_disk_policy.py",
            )
            decoy = root / "decoy"
            decoy.mkdir()
            (decoy / "platform_disk_policy.py").write_text(
                "raise RuntimeError('ambient policy loaded')\n", encoding="utf-8"
            )
            environment = {
                "HOME": str(root / "home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(decoy),
            }
            completed = subprocess.run(
                ["/usr/bin/python3", "-I", str(staged_tools / SCRIPT_PATH.name), "--help"],
                cwd=decoy,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("ambient policy loaded", completed.stderr)


if __name__ == "__main__":
    unittest.main()
