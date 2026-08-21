#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PLATFORM_ROOT / "docs"
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
MAX_DOCUMENT_LINES = 600


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the platform documentation index and local links."
    )
    parser.add_argument("--docs-root", type=Path, default=DOCS_ROOT)
    return parser.parse_args()


def _local_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip().strip("<>")
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("#"):
        return None
    path = unquote(parsed.path)
    if not path:
        return None
    return (source.parent / path).resolve()


def collect_issues(docs_root: Path) -> list[str]:
    root = docs_root.resolve()
    documents = sorted(root.rglob("*.md"))
    index = root / "README.md"
    issues: list[str] = []
    if not index.is_file():
        return ["docs/README.md is missing"]

    index_text = index.read_text(encoding="utf-8")
    indexed_top_level = {
        path.name
        for path in documents
        if path.parent == root and path != index and path.name in index_text
    }
    for document in documents:
        relative = document.relative_to(root)
        text = document.read_text(encoding="utf-8")
        lines = text.splitlines()
        headings = [line for line in lines if line.startswith("# ")]
        if len(headings) != 1:
            issues.append(f"{relative}: expected exactly one H1, found {len(headings)}")
        if len(lines) > MAX_DOCUMENT_LINES:
            issues.append(
                f"{relative}: {len(lines)} lines exceeds the {MAX_DOCUMENT_LINES}-line limit"
            )
        for raw_target in MARKDOWN_LINK.findall(text):
            target = _local_target(document, raw_target)
            if target is None:
                continue
            try:
                target.relative_to(root)
            except ValueError:
                issues.append(f"{relative}: local link escapes docs root: {raw_target}")
                continue
            if not target.exists():
                issues.append(f"{relative}: missing local link target: {raw_target}")

    for document in documents:
        if document.parent == root and document != index and document.name not in indexed_top_level:
            issues.append(f"README.md: top-level document is not indexed: {document.name}")
    return issues


def main() -> int:
    args = parse_args()
    issues = collect_issues(args.docs_root)
    if issues:
        for issue in issues:
            print(issue)
        return 1
    print("platform docs: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
