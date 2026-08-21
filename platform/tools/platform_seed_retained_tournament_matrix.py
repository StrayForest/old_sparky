#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
QA_TOOL = PLATFORM_ROOT / "tools" / "platform_production_qa.py"
TEAM_SIZES = (8, 16, 32, 64)
TOURNAMENTS_PER_SIZE = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a retained 20-tournament QA matrix for manual owner inspection."
    )
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--origin", default="http://127.0.0.1")
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("/opt/oldsparky/platform/shared/preprod-retained-matrix"),
    )
    return parser.parse_args()


def matrix_plan() -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for teams in TEAM_SIZES:
        for index in range(1, TOURNAMENTS_PER_SIZE + 1):
            state = "assigned"
            if index == TOURNAMENTS_PER_SIZE and teams in (8, 16):
                state = "registered"
            elif index == TOURNAMENTS_PER_SIZE:
                state = "ready-unassigned"
            plan.append({"teams": teams, "index": index, "owner_state": state})
    return plan


def validate_matrix_plan(plan: list[dict[str, Any]]) -> None:
    size_counts = Counter(int(item["teams"]) for item in plan)
    state_counts = Counter(str(item["owner_state"]) for item in plan)
    if len(plan) != 20 or size_counts != Counter({8: 5, 16: 5, 32: 5, 64: 5}):
        raise RuntimeError(f"Invalid tournament matrix sizes: {dict(size_counts)}")
    if state_counts != Counter({"assigned": 16, "registered": 2, "ready-unassigned": 2}):
        raise RuntimeError(f"Invalid owner-state matrix: {dict(state_counts)}")


def run() -> int:
    args = parse_args()
    plan = matrix_plan()
    validate_matrix_plan(plan)
    if args.dry_run:
        print(json.dumps({"passed": True, "plan": plan}, ensure_ascii=False))
        return 0
    started_at = datetime.now(UTC)
    batch_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    title_suffix = started_at.strftime("%H%M%S")
    batch_dir = args.output_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []

    for position, item in enumerate(plan, start=1):
        teams = int(item["teams"])
        owner_state = str(item["owner_state"])
        synthetic_users = teams * 7 - (1 if owner_state == "assigned" else 0)
        tournament_name = f"QA {teams:02d} T{item['index']} {title_suffix}"
        report_path = batch_dir / f"teams-{teams:02d}-{item['index']}-{owner_state}.json"
        command = [
            sys.executable,
            str(QA_TOOL),
            "--mode",
            "scale",
            "--keep-data",
            "--origin",
            args.origin,
            "--scale-users",
            str(synthetic_users),
            "--scale-teams",
            str(teams),
            "--scale-site-mix-users",
            "0",
            "--scale-bracket-view-users",
            "0",
            "--concurrency",
            str(max(1, args.concurrency)),
            "--tournament-name",
            tournament_name,
            "--report-path",
            str(report_path),
        ]
        if owner_state == "assigned":
            command.extend(["--rostered-participant-email", args.owner_email])
        else:
            command.extend(
                [
                    "--retained-participant-email",
                    args.owner_email,
                    "--retained-participant-state",
                    owner_state,
                ]
            )

        print(
            f"[{position:02d}/20] {tournament_name}: {teams} teams, owner={owner_state}",
            flush=True,
        )
        completed = subprocess.run(
            command,
            cwd=PLATFORM_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        compact = json.loads(stdout_lines[-1]) if stdout_lines else {}
        row = {
            **item,
            "name": tournament_name,
            "synthetic_users": synthetic_users,
            "report_path": str(report_path),
            "returncode": completed.returncode,
            "result": compact,
            "stderr": completed.stderr[-2000:] if completed.stderr else "",
        }
        rows.append(row)
        if completed.returncode != 0 or not compact.get("passed"):
            break

    summary = {
        "batch_id": batch_id,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "owner_email": args.owner_email.strip().lower(),
        "planned_tournaments": 20,
        "completed_tournaments": len(rows),
        "passed": len(rows) == 20 and all(row["returncode"] == 0 for row in rows),
        "rows": rows,
    }
    summary_path = batch_dir / "matrix-summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**summary, "rows": None, "summary_path": str(summary_path)}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(run())
