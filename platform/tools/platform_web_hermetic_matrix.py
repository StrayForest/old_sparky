#!/usr/bin/env python3
"""Accept the executable Playwright ownership matrix for web hermetic tests."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Iterable


LISTING_RE = re.compile(
    r"^\s+\[(?P<project>[^\]]+)\] › (?P<file>[^:]+):\d+:\d+ › (?P<title>.+)$"
)
TOTAL_RE = re.compile(r"^Total: (?P<count>\d+) tests? in ")


@dataclass(frozen=True)
class ListedTest:
    project: str
    file: str
    title: str


def parse_listing(output: str) -> tuple[list[ListedTest], int | None]:
    entries: list[ListedTest] = []
    total: int | None = None
    for line in output.splitlines():
        match = LISTING_RE.match(line)
        if match:
            entries.append(
                ListedTest(
                    project=match.group("project"),
                    file=Path(match.group("file")).name,
                    title=match.group("title"),
                )
            )
            continue
        total_match = TOTAL_RE.match(line)
        if total_match:
            total = int(total_match.group("count"))
    return entries, total


def _run_list(web_root: Path, config_name: str) -> list[ListedTest]:
    tools_root = Path(__file__).resolve().parent
    node_wrapper = tools_root / "platform_node.sh"
    playwright_cli = web_root / "node_modules" / ".bin" / "playwright"
    if not node_wrapper.is_file() or not playwright_cli.is_file():
        raise RuntimeError("Node/Playwright dependencies are required for the web matrix acceptance")
    completed = subprocess.run(
        [
            str(node_wrapper),
            str(playwright_cli),
            "test",
            f"--config={config_name}",
            "--list",
        ],
        cwd=web_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
        raise RuntimeError(
            f"Playwright list failed for {config_name}: {detail[0] if detail else 'unknown error'}"
        )
    entries, total = parse_listing(completed.stdout)
    if total is None or total != len(entries):
        raise RuntimeError(
            f"Playwright list count mismatch for {config_name}: declared={total!r} parsed={len(entries)}"
        )
    return entries


def _by_key(entries: Iterable[ListedTest]) -> dict[tuple[str, str], set[str]]:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entry in entries:
        grouped[(entry.file, entry.title)].add(entry.project)
    return grouped


def _assert_exact_projects(
    grouped: dict[tuple[str, str], set[str]],
    *,
    file_name: str,
    title: str,
    expected: set[str],
) -> None:
    actual = grouped.get((file_name, title))
    if actual != expected:
        raise RuntimeError(
            f"ownership mismatch for {file_name} / {title!r}: expected={sorted(expected)} actual={sorted(actual or set())}"
        )


def assert_matrix(web_root: Path) -> dict[str, int]:
    smoke = _run_list(web_root, "playwright.config.ts")
    source = _run_list(web_root, "playwright.source-contract.config.ts")
    participant = _run_list(web_root, "playwright.participant.config.ts")

    smoke_grouped = _by_key(smoke)
    source_grouped = _by_key(source)
    responsive_projects = {"desktop", "wide-1300", "tablet-820", "mobile-layout"}

    route_keys = [key for key in smoke_grouped if key[0] == "platform-routes.spec.ts"]
    if not route_keys:
        raise RuntimeError("responsive platform-routes tests are missing from the smoke list")
    for file_name, title in route_keys:
        _assert_exact_projects(
            smoke_grouped,
            file_name=file_name,
            title=title,
            expected=responsive_projects,
        )

    race_keys = [
        key
        for key in smoke_grouped
        if key[0] == "tournament-registration-race.spec.ts"
    ]
    if len(race_keys) != 2:
        raise RuntimeError(f"expected two registration-race tests, found {len(race_keys)}")
    for file_name, title in race_keys:
        _assert_exact_projects(
            smoke_grouped,
            file_name=file_name,
            title=title,
            expected={"desktop"},
        )

    discovery_keys = [
        key
        for key in smoke_grouped
        if key[0] == "public-discovery-documents.spec.ts"
    ]
    if len(discovery_keys) != 1:
        raise RuntimeError(f"expected one discovery-document test, found {len(discovery_keys)}")
    _assert_exact_projects(
        smoke_grouped,
        file_name=discovery_keys[0][0],
        title=discovery_keys[0][1],
        expected={"request-contract"},
    )

    if any(entry.file == "origin-validator-contract.spec.ts" for entry in smoke):
        raise RuntimeError("origin validator leaked into the browser smoke contour")
    origin_keys = [
        key
        for key in source_grouped
        if key[0] == "origin-validator-contract.spec.ts"
    ]
    if len(origin_keys) != 1:
        raise RuntimeError(f"expected one source origin-validator test, found {len(origin_keys)}")
    _assert_exact_projects(
        source_grouped,
        file_name=origin_keys[0][0],
        title=origin_keys[0][1],
        expected={"source-contract"},
    )

    if len(participant) != 18:
        raise RuntimeError(f"participant contour changed: expected 18 tests, found {len(participant)}")
    participant_projects = Counter(entry.project for entry in participant)
    if participant_projects != Counter({"desktop": 9, "mobile": 9}):
        raise RuntimeError(
            f"participant project matrix changed: expected desktop/mobile 9+9, found {dict(participant_projects)}"
        )
    if any(entry.file != "tournament-participant-progressive.spec.ts" for entry in participant):
        raise RuntimeError("participant contour owns an unexpected spec")

    return {
        "smoke": len(smoke),
        "source_contract": len(source),
        "participant": len(participant),
        "responsive_route_tests": len(route_keys),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-root", type=Path, required=True)
    args = parser.parse_args()
    summary = assert_matrix(args.web_root.resolve())
    print(
        "WEB_HERMETIC_MATRIX "
        + " ".join(f"{key}={value}" for key, value in summary.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
