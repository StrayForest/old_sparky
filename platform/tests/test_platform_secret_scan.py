from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_secret_scan.py"
SPEC = importlib.util.spec_from_file_location("platform_secret_scan", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlatformSecretScanTests(unittest.TestCase):
    def test_detects_private_key_without_returning_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            path = root / "unsafe.txt"
            unsafe_marker = "-----BEGIN PRIVATE KEY-----"  # secret-scan: allow-test-fixture
            path.write_text(f"{unsafe_marker}\nsecret\n", encoding="utf-8")

            findings = MODULE.scan_file(path, root)

        self.assertEqual([finding.rule for finding in findings], ["private_key_material"])
        self.assertFalse(hasattr(findings[0], "value"))

    def test_example_placeholders_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            path = root / ".env.platform.example"
            path.write_text(
                "PLATFORM_SECRET_KEY=CHANGE_ME_RANDOM_48_OR_MORE_CHARACTERS\n",
                encoding="utf-8",
            )

            self.assertEqual(MODULE.scan_file(path, root), [])

    def test_explicit_test_fixture_marker_suppresses_only_its_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            path = root / "fixture.py"
            path.write_text(
                'PLATFORM_SECRET_KEY="fixture"  # secret-scan: allow-test-fixture\n'
                'PLATFORM_SECRET_KEY="still-detected"\n',
                encoding="utf-8",
            )

            findings = MODULE.scan_file(path, root)

        self.assertEqual([finding.line for finding in findings], [2])


if __name__ == "__main__":
    unittest.main()
