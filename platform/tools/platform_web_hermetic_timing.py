#!/usr/bin/env python3
"""Write a bounded, secret-free summary for the web hermetic gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


MAX_PHASES = 32
MAX_RUNS = 8
MAX_INPUT_BYTES = 64 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STATUS_KEYS = ("passed", "failed", "timedOut", "skipped", "interrupted", "other")


def _safe_name(value: object, fallback: str) -> str:
    candidate = value if isinstance(value, str) else ""
    return candidate if NAME_RE.fullmatch(candidate) else fallback


def _bounded_status_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {key: 0 for key in STATUS_KEYS}
    return {
        key: value.get(key, 0)
        if isinstance(value.get(key, 0), int) and value.get(key, 0) >= 0
        else 0
        for key in STATUS_KEYS
    }


def _bounded_playwright_run(label: str, path: Path) -> dict[str, Any]:
    run: dict[str, Any] = {
        "name": _safe_name(label, "unknown"),
        "status": "missing",
        "duration_ms": 0,
        "tests": 0,
        "status_counts": {key: 0 for key in STATUS_KEYS},
        "projects": [],
    }
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            run["status"] = "oversize"
            return run
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return run
    if not isinstance(payload, dict):
        return run

    run["status"] = _safe_name(payload.get("status"), "unknown")
    for field in ("duration_ms", "tests"):
        value = payload.get(field)
        if isinstance(value, int) and value >= 0:
            run[field] = value
    run["status_counts"] = _bounded_status_counts(payload.get("status_counts"))
    projects = payload.get("projects")
    if isinstance(projects, list):
        for item in projects[:MAX_RUNS]:
            if not isinstance(item, dict):
                continue
            project = {
                "name": _safe_name(item.get("name"), "unknown"),
                "tests": 0,
                "duration_ms": 0,
                "status_counts": _bounded_status_counts(item.get("status_counts")),
            }
            for field in ("duration_ms", "tests"):
                value = item.get(field)
                if isinstance(value, int) and value >= 0:
                    project[field] = value
            run["projects"].append(project)
    return run


def _phases(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return entries
    for line in lines[:MAX_PHASES]:
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, start, end, status = parts
        if not NAME_RE.fullmatch(name):
            continue
        try:
            start_ms = int(start)
            end_ms = int(end)
            status_code = int(status)
        except ValueError:
            continue
        if start_ms < 0 or end_ms < start_ms:
            continue
        entries.append(
            {
                "name": name,
                "duration_ms": end_ms - start_ms,
                "status": "passed" if status_code == 0 else "failed",
            }
        )
    return entries


def write_summary(
    *,
    output: Path,
    phase_log: Path,
    exit_status: int,
    playwright_runs: list[tuple[str, Path]],
) -> None:
    summary = {
        "schema": 1,
        "gate": "web-hermetic",
        "status": "passed" if exit_status == 0 else "failed",
        "phase_count": len(_phases(phase_log)),
        "phases": _phases(phase_log),
        "playwright": [
            _bounded_playwright_run(label, path)
            for label, path in playwright_runs[:MAX_RUNS]
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{json.dumps(summary, separators=(',', ':'))}\n", encoding="utf-8")
    output.chmod(0o600)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase-log", type=Path, required=True)
    parser.add_argument("--exit-status", type=int, required=True)
    parser.add_argument(
        "--playwright-run",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="bounded reporter output, repeated once per Playwright contour",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    runs: list[tuple[str, Path]] = []
    for raw in args.playwright_run[:MAX_RUNS]:
        label, separator, raw_path = raw.partition("=")
        if not separator or not NAME_RE.fullmatch(label) or not raw_path:
            continue
        runs.append((label, Path(raw_path)))
    write_summary(
        output=args.output,
        phase_log=args.phase_log,
        exit_status=args.exit_status,
        playwright_runs=runs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
