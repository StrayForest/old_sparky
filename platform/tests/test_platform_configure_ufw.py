from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
SCRIPT_PATH = TOOLS_DIR / "platform_configure_ufw.py"
SPEC = importlib.util.spec_from_file_location("platform_configure_ufw", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PlatformConfigureUfwTests(unittest.TestCase):
    def test_managed_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "cloudflare-ufw.json"
            expected = {"192.0.2.0/24", "2001:db8::/32"}

            MODULE.write_state(state_path, expected)

            self.assertEqual(MODULE.load_state(state_path), expected)
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)

    def test_state_rejects_another_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            state_path = Path(temporary_dir) / "cloudflare-ufw.json"
            state_path.write_text(
                json.dumps({"format_version": 1, "comment": "other", "ranges": []}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "unsupported format or owner"):
                MODULE.load_state(state_path)


if __name__ == "__main__":
    unittest.main()
