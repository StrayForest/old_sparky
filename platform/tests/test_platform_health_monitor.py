from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_health_monitor.py"
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


if __name__ == "__main__":
    unittest.main()
