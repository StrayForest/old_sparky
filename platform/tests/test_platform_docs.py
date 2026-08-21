from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_docs_check.py"
SPEC = importlib.util.spec_from_file_location("platform_docs_check", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PlatformDocsTests(unittest.TestCase):
    def test_repository_documentation_is_indexed_and_linked(self) -> None:
        self.assertEqual(MODULE.collect_issues(MODULE.DOCS_ROOT), [])

    def test_missing_local_target_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            (root / "README.md").write_text(
                "# Docs\n\n[Guide](guide.md)\n",
                encoding="utf-8",
            )
            (root / "guide.md").write_text(
                "# Guide\n\n[Missing](missing.md)\n",
                encoding="utf-8",
            )

            issues = MODULE.collect_issues(root)

        self.assertIn("guide.md: missing local link target: missing.md", issues)


if __name__ == "__main__":
    unittest.main()
