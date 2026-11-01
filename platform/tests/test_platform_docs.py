from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_docs_check.py"
SPEC = importlib.util.spec_from_file_location("platform_docs_check", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _bash_fenced_blocks(markdown: str) -> list[str]:
    blocks: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    current: list[str] = []
    for line in markdown.splitlines():
        if fence_char is None:
            match = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
            if match is None:
                continue
            info = match.group(2).strip().split(maxsplit=1)
            if not info or info[0].lower() not in {"bash", "sh", "shell"}:
                continue
            fence_char = match.group(1)[0]
            fence_length = len(match.group(1))
            current = []
            continue
        closing = re.match(r"^\s*(`{3,}|~{3,})\s*$", line)
        if (closing is not None and closing.group(1)[0] == fence_char
                and len(closing.group(1)) >= fence_length):
            blocks.append("\n".join(current))
            fence_char = None
            fence_length = 0
            current = []
        else:
            current.append(line)
    return blocks


def _unsupported_gh_api_options(block: str) -> set[str]:
    unsupported: set[str] = set()
    separators = {";", "&&", "||", "|"}
    lines = block.splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = re.search(r"\bgh\s+api\b", line)
        if match is None:
            continue
        command = line[match.start():]
        continuation = index
        while command.rstrip().endswith("\\") and continuation + 1 < len(lines):
            command = command.rstrip()[:-1] + " " + lines[continuation + 1].lstrip()
            continuation += 1
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        for token in tokens[2:]:
            if token in separators:
                break
            if (token in {"--repo", "-R", "--output", "-o"}
                    or token.startswith(("--repo=", "--output="))):
                unsupported.add(token)
    return unsupported


class PlatformDocsTests(unittest.TestCase):
    def test_repository_documentation_is_indexed_and_linked(self) -> None:
        self.assertEqual(MODULE.collect_issues(MODULE.DOCS_ROOT), [])
        sample_markdown = (
            "Prose mentions gh api --repo, but is not an executable block.\n"
            "~~~bash\n"
            "# A comment mentioning gh api --repo is ignored.\n"
            "gh api --repo StrayForest/old_sparky \\\n  repos/StrayForest/old_sparky/actions/runs/1\n"
            "gh api \\\n  --output result.zip repos/StrayForest/old_sparky/actions/artifacts/2/zip\n"
            "gh run watch 3 --repo StrayForest/old_sparky --exit-status\n"
            "~~~\n"
        )
        sample_blocks = _bash_fenced_blocks(sample_markdown)
        self.assertEqual(len(sample_blocks), 1)
        self.assertEqual(
            _unsupported_gh_api_options(sample_blocks[0]),
            {"--repo", "--output"},
        )
        offenders = []
        for document in sorted(MODULE.DOCS_ROOT.rglob("*.md")):
            for number, block in enumerate(
                _bash_fenced_blocks(document.read_text(encoding="utf-8")), 1
            ):
                options = _unsupported_gh_api_options(block)
                if options:
                    offenders.append(
                        f"{document.relative_to(MODULE.DOCS_ROOT)} bash block {number}: "
                        f"{', '.join(sorted(options))}"
                    )
        self.assertEqual(offenders, [])

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
