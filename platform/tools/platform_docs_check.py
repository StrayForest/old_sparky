#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = PLATFORM_ROOT / "docs"
REPO_ROOT = PLATFORM_ROOT.parent
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_DOCUMENT_LINES = 600
MAX_SKILL_LINES = 500


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


def _frontmatter(skill: Path) -> tuple[dict[str, str], list[str]]:
    lines = skill.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}, ["missing frontmatter"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, ["unterminated frontmatter"]
    values: dict[str, str] = {}
    issues: list[str] = []
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            issues.append(f"invalid frontmatter line: {line}")
            continue
        key = key.strip()
        value = value.strip()
        if key not in {"name", "description"}:
            issues.append(f"unsupported frontmatter key: {key}")
        if not value:
            issues.append(f"empty frontmatter value: {key}")
        values[key] = value
    for required in ("name", "description"):
        if required not in values:
            issues.append(f"missing frontmatter field: {required}")
    return values, issues


def _check_skill(skill: Path, skills_root: Path) -> list[str]:
    relative = skill.parent.relative_to(skills_root)
    label = f"skills/{relative}"
    issues: list[str] = []
    values, frontmatter_issues = _frontmatter(skill)
    issues.extend(f"{label}: {issue}" for issue in frontmatter_issues)
    name = values.get("name", "")
    if name and (name != skill.parent.name or not SKILL_NAME.fullmatch(name)):
        issues.append(f"{label}: name must match its directory and use lowercase hyphens")
    lines = skill.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_SKILL_LINES:
        issues.append(f"{label}: {len(lines)} lines exceeds the {MAX_SKILL_LINES}-line limit")
    if len(lines) <= 3 or not any(line.startswith("# ") for line in lines[3:]):
        issues.append(f"{label}: body must contain a heading after frontmatter")
    for raw_target in MARKDOWN_LINK.findall(skill.read_text(encoding="utf-8")):
        target = _local_target(skill, raw_target)
        if target is None:
            continue
        try:
            target.relative_to(skill.parent.resolve())
        except ValueError:
            issues.append(f"{label}: local link escapes skill directory: {raw_target}")
            continue
        if not target.exists():
            issues.append(f"{label}: missing local link target: {raw_target}")

    interface = skill.parent / "agents" / "openai.yaml"
    if interface.is_file() and name:
        interface_text = interface.read_text(encoding="utf-8")
        if "default_prompt:" not in interface_text:
            issues.append(f"{label}: agents/openai.yaml is missing default_prompt")
        elif f"${name}" not in interface_text:
            issues.append(f"{label}: default_prompt must mention ${name}")
    return issues


def _check_skills(skills_root: Path = SKILLS_ROOT) -> list[str]:
    if not skills_root.is_dir():
        return [".agents/skills is missing"]
    issues: list[str] = []
    for directory in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill = directory / "SKILL.md"
        if skill.is_file():
            issues.extend(_check_skill(skill, skills_root))
    return issues


def collect_issues(docs_root: Path) -> list[str]:
    root = docs_root.resolve()
    allowed_link_root = REPO_ROOT if root == DOCS_ROOT.resolve() else root
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
                target.relative_to(allowed_link_root)
            except ValueError:
                issues.append(f"{relative}: local link escapes repository docs scope: {raw_target}")
                continue
            if not target.exists():
                issues.append(f"{relative}: missing local link target: {raw_target}")

    for document in documents:
        if document.parent == root and document != index and document.name not in indexed_top_level:
            issues.append(f"README.md: top-level document is not indexed: {document.name}")
    return issues + _check_skills()


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
