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
DEFAULT_USERS_PER_TOURNAMENT = 500
INVITE_MAX_USES = 500
INVITE_EXTRA_CAPACITY = 64
INVITE_MAX_USERS = INVITE_MAX_USES - INVITE_EXTRA_CAPACITY


MATRIX_STATES = {
    8: (
        ("public", "registered"),
        ("invite_only", "ready"),
        ("public", None),
        ("invite_only", "registered"),
        ("public", None),
    ),
    16: (
        ("public", "ready"),
        ("invite_only", None),
        ("public", "registered"),
        ("invite_only", None),
        ("public", None),
    ),
    32: (
        ("invite_only", "ready"),
        ("public", None),
        ("invite_only", "registered"),
        ("public", None),
        ("invite_only", None),
    ),
    64: (
        ("invite_only", "assigned"),
        ("public", "ready"),
        ("invite_only", "registered"),
        ("public", None),
        ("public", None),
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a retained 20-tournament, 10,000-user QA matrix for "
            "manual owner inspection."
        )
    )
    parser.add_argument(
        "--control-email",
        "--owner-email",
        dest="control_email",
        required=True,
        help="Existing account used for manual control states; its profile is never changed.",
    )
    parser.add_argument("--origin", default="http://127.0.0.1")
    parser.add_argument("--concurrency", type=int, default=80)
    parser.add_argument(
        "--users-per-tournament",
        type=int,
        default=DEFAULT_USERS_PER_TOURNAMENT,
    )
    parser.add_argument(
        "--skip-performance",
        action="store_true",
        help="Skip request/system performance collection (not recommended for retained runs).",
    )
    parser.add_argument(
        "--skip-profile-journey",
        action="store_true",
        help="Skip real API profile/captain writes and persistence reads.",
    )
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
        states = MATRIX_STATES[teams]
        if len(states) != TOURNAMENTS_PER_SIZE:
            raise RuntimeError(f"Invalid state matrix for {teams} teams")
        for index, (visibility, control_state) in enumerate(states, start=1):
            plan.append(
                {
                    "teams": teams,
                    "index": index,
                    "visibility": visibility,
                    "control_state": control_state,
                }
            )
    return plan


def validate_matrix_plan(plan: list[dict[str, Any]]) -> None:
    size_counts = Counter(int(item["teams"]) for item in plan)
    state_counts = Counter(
        str(item["control_state"])
        for item in plan
        if item["control_state"] is not None
    )
    if len(plan) != 20 or size_counts != Counter({8: 5, 16: 5, 32: 5, 64: 5}):
        raise RuntimeError(f"Invalid tournament matrix sizes: {dict(size_counts)}")
    if state_counts != Counter({"assigned": 1, "registered": 5, "ready": 4}):
        raise RuntimeError(f"Invalid control-state matrix: {dict(state_counts)}")
    assigned_items = [item for item in plan if item["control_state"] == "assigned"]
    if len(assigned_items) != 1 or int(assigned_items[0]["teams"]) != 64:
        raise RuntimeError("The assigned control state must be on the 64-team run")
    if Counter(str(item["visibility"]) for item in plan) != Counter(
        {"public": 11, "invite_only": 9}
    ):
        raise RuntimeError("Invalid tournament visibility matrix")


def allocate_user_counts(
    plan: list[dict[str, Any]],
    *,
    users_per_tournament: int,
) -> list[int]:
    target_total = users_per_tournament * len(plan)
    assigned_index = next(
        index
        for index, item in enumerate(plan)
        if item["control_state"] == "assigned"
    )
    assigned_users = int(plan[assigned_index]["teams"]) * 6 - 1
    remaining_indexes = [index for index in range(len(plan)) if index != assigned_index]
    remaining_total = target_total - assigned_users
    if remaining_total < len(remaining_indexes) * 14:
        raise RuntimeError(
            "--users-per-tournament is too small for the exact-control assignment run"
        )
    base, remainder = divmod(remaining_total, len(remaining_indexes))
    counts = [0] * len(plan)
    counts[assigned_index] = assigned_users
    for position, index in enumerate(remaining_indexes):
        counts[index] = base + (1 if position < remainder else 0)

    # The scale QA flow reserves one invite code for the whole invite-only
    # tournament and keeps 64 uses available for retained/control members.
    # Keep the synthetic population within the API's max_uses=500 contract;
    # public tournaments absorb the exact-total remainder.
    invite_overflow = 0
    for index in remaining_indexes:
        if plan[index]["visibility"] != "invite_only":
            continue
        if counts[index] > INVITE_MAX_USERS:
            invite_overflow += counts[index] - INVITE_MAX_USERS
            counts[index] = INVITE_MAX_USERS
    public_indexes = [
        index
        for index in remaining_indexes
        if plan[index]["visibility"] == "public"
    ]
    if invite_overflow and not public_indexes:
        raise RuntimeError("Invite-only cap requires at least one public tournament")
    if public_indexes:
        public_base, public_remainder = divmod(invite_overflow, len(public_indexes))
        for position, index in enumerate(public_indexes):
            counts[index] += public_base + (1 if position < public_remainder else 0)
    return counts


def compact_performance(report_path: Path) -> dict[str, Any]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    performance = report.get("performance")
    if not isinstance(performance, dict) or not performance:
        return {}
    http_client = performance.get("http_client")
    if not isinstance(http_client, dict):
        http_client = {}
    bottleneck = performance.get("bottleneck_summary")
    if not isinstance(bottleneck, dict):
        bottleneck = {}
    return {
        "http_overall": http_client.get("overall"),
        "likely_bottleneck_classes": bottleneck.get("likely_bottleneck_classes", []),
        "resource_flags": bottleneck.get("resource_flags", {}),
        "top_client_phases_by_p95": [
            {
                key: row.get(key)
                for key in ("name", "p95_ms", "p99_ms")
                if key in row
            }
            for row in (bottleneck.get("top_client_phases_by_p95") or [])[:5]
            if isinstance(row, dict)
        ],
        "top_server_routes_by_p95": [
            {
                key: row.get(key)
                for key in ("route", "p95_ms", "avg_db_time_ms", "max_sql_time_ms")
                if key in row
            }
            for row in (bottleneck.get("top_server_routes_by_p95") or [])[:5]
            if isinstance(row, dict)
        ],
    }


def summarize_matrix_performance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    class_counts: Counter[str] = Counter()
    flag_counts: Counter[str] = Counter()
    phase_max_p95: dict[str, float] = {}
    route_max_p95: dict[str, float] = {}
    p95_values: list[float] = []
    p99_values: list[float] = []
    runs_with_performance = 0
    for row in rows:
        performance = row.get("performance")
        if not isinstance(performance, dict) or not performance:
            continue
        runs_with_performance += 1
        http_overall = performance.get("http_overall")
        if isinstance(http_overall, dict):
            if isinstance(http_overall.get("p95_ms"), (int, float)):
                p95_values.append(float(http_overall["p95_ms"]))
            if isinstance(http_overall.get("p99_ms"), (int, float)):
                p99_values.append(float(http_overall["p99_ms"]))
        for bottleneck_class in performance.get("likely_bottleneck_classes") or []:
            class_counts[str(bottleneck_class)] += 1
        for flag, enabled in (performance.get("resource_flags") or {}).items():
            if enabled:
                flag_counts[str(flag)] += 1
        for phase in performance.get("top_client_phases_by_p95") or []:
            if isinstance(phase, dict) and isinstance(phase.get("name"), str):
                value = phase.get("p95_ms")
                if isinstance(value, (int, float)):
                    phase_max_p95[phase["name"]] = max(
                        phase_max_p95.get(phase["name"], 0.0),
                        float(value),
                    )
        for route in performance.get("top_server_routes_by_p95") or []:
            if isinstance(route, dict) and isinstance(route.get("route"), str):
                value = route.get("p95_ms")
                if isinstance(value, (int, float)):
                    route_max_p95[route["route"]] = max(
                        route_max_p95.get(route["route"], 0.0),
                        float(value),
                    )

    def ranked(values: dict[str, float]) -> list[dict[str, Any]]:
        return [
            {"name": name, "max_p95_ms": round(value, 3)}
            for name, value in sorted(
                values.items(), key=lambda item: item[1], reverse=True
            )[:10]
        ]

    return {
        "runs_with_performance": runs_with_performance,
        "worst_http_p95_ms": round(max(p95_values), 3) if p95_values else None,
        "worst_http_p99_ms": round(max(p99_values), 3) if p99_values else None,
        "bottleneck_classes": [
            {"class": name, "runs": count}
            for name, count in class_counts.most_common()
        ],
        "resource_flags": [
            {"flag": name, "runs": count}
            for name, count in flag_counts.most_common()
        ],
        "top_client_phases_by_worst_p95": ranked(phase_max_p95),
        "top_server_routes_by_worst_p95": ranked(route_max_p95),
    }


def run() -> int:
    args = parse_args()
    if args.users_per_tournament < 14:
        raise RuntimeError("--users-per-tournament must be at least 14")
    plan = matrix_plan()
    validate_matrix_plan(plan)
    user_counts = allocate_user_counts(
        plan,
        users_per_tournament=args.users_per_tournament,
    )
    if args.dry_run:
        print(
            json.dumps(
                {"passed": True, "plan": plan, "user_counts": user_counts},
                ensure_ascii=False,
            )
        )
        return 0
    started_at = datetime.now(UTC)
    batch_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    title_suffix = started_at.strftime("%H%M%S")
    batch_dir = args.output_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []

    for position, (item, allocated_users) in enumerate(
        zip(plan, user_counts),
        start=1,
    ):
        teams = int(item["teams"])
        visibility = str(item["visibility"])
        control_state = item["control_state"]
        synthetic_users = allocated_users
        state_label = str(control_state or "none")
        tournament_name = f"QA {teams:02d} {visibility[:3]} T{item['index']} {title_suffix}"
        report_path = batch_dir / (
            f"teams-{teams:02d}-{item['index']}-{visibility}-{state_label}.json"
        )
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
            "--tournament-visibility",
            visibility,
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
        if not args.skip_profile_journey:
            command.append("--profile-journey")
        if not args.skip_performance:
            command.append("--collect-performance")
        if control_state is not None:
            command.extend(
                [
                    "--control-participant-email",
                    args.control_email,
                    "--control-participant-state",
                    str(control_state),
                ]
            )

        print(
            f"[{position:02d}/20] {tournament_name}: {teams} teams, "
            f"visibility={visibility}, control={state_label}",
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
        try:
            compact = json.loads(stdout_lines[-1]) if stdout_lines else {}
        except (TypeError, ValueError):
            compact = {"passed": False, "error": "qa_child_summary_missing"}
        row = {
            **item,
            "name": tournament_name,
            "synthetic_users": synthetic_users,
            "report_path": str(report_path),
            "returncode": completed.returncode,
            "result": compact,
            "performance": compact_performance(report_path),
            "stderr": completed.stderr[-2000:] if completed.stderr else "",
        }
        rows.append(row)
        if completed.returncode != 0 or not compact.get("passed"):
            break

    summary = {
        "batch_id": batch_id,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "control_email": args.control_email.strip().lower(),
        "planned_tournaments": 20,
        "completed_tournaments": len(rows),
        "planned_users": sum(user_counts),
        "completed_users": sum(int(row["synthetic_users"]) for row in rows),
        "planned_teams": sum(int(item["teams"]) for item in plan),
        "passed": len(rows) == 20
        and all(
            row["returncode"] == 0
            and bool((row["result"] or {}).get("passed"))
            for row in rows
        ),
        "performance_summary": summarize_matrix_performance(rows),
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
