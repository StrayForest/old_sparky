from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_r2_smoke.py"
SPEC = importlib.util.spec_from_file_location("platform_r2_smoke", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlatformR2SmokeTests(unittest.TestCase):
    def test_env_loader_accepts_private_file_without_printing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "runtime.env"
            path.write_text("PLATFORM_R2_BUCKET_NAME=test-bucket\n", encoding="utf-8")
            path.chmod(0o640)

            MODULE.load_env_file(path)

            self.assertEqual(os.environ["PLATFORM_R2_BUCKET_NAME"], "test-bucket")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_env_loader_rejects_world_readable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "runtime.env"
            path.write_text("PLATFORM_R2_BUCKET_NAME=test-bucket\n", encoding="utf-8")
            path.chmod(0o644)

            with self.assertRaises(PermissionError):
                MODULE.load_env_file(path)


if __name__ == "__main__":
    unittest.main()
