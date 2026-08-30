#!/usr/bin/env python3
"""Canonical load-profile registry and external load dispatcher.

The JSON profiles own scenario shape, retry behavior and acceptance budgets.
This module resolves a reviewed profile and delegates HTTP execution to the
existing external client.  It never runs the measured generator on the origin.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Sequence


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = PLATFORM_ROOT / "performance" / "profiles"
PROFILE_SCHEMA = 2
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]+$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_MODES = {"ready-vote", "read-mix"}
ALLOWED_CATEGORIES = {"load", "stress", "spike", "soak", "capacity"}


class LoadProfileError(ValueError):
    """Raised for an invalid, duplicated or unsafe load profile."""


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LoadProfileError(f"profile {field} must be an object")
    return value


def _require_int(
    value: Any,
    *,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LoadProfileError(f"profile {field} must be an integer")
    if minimum is not None and value < minimum:
        raise LoadProfileError(f"profile {field} is below {minimum}")
    if maximum is not None and value > maximum:
        raise LoadProfileError(f"profile {field} is above {maximum}")
    return value


def _require_number(
    value: Any,
    *,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoadProfileError(f"profile {field} must be numeric")
    number = float(value)
    if minimum is not None and number < minimum:
        raise LoadProfileError(f"profile {field} is below {minimum}")
    if maximum is not None and number > maximum:
        raise LoadProfileError(f"profile {field} is above {maximum}")
    return number


def _validate_latency_budget(value: Any, *, field: str, percentiles: tuple[str, ...]) -> dict[str, Any]:
    budget = _require_mapping(value, field=field)
    previous = -1.0
    for percentile in percentiles:
        current = _require_number(
            budget.get(f"{percentile}_ms"),
            field=f"{field}.{percentile}_ms",
            minimum=0,
        )
        if current < previous:
            raise LoadProfileError(f"{field} percentiles must be monotonic")
        previous = current
    return dict(budget)


def _validate_phase_plan(value: Any, *, total_users: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LoadProfileError("traffic.phases must be a non-empty array")
    phases: list[dict[str, Any]] = []
    total_actions = 0
    names: set[str] = set()
    for index, raw_phase in enumerate(value, start=1):
        phase = _require_mapping(raw_phase, field=f"traffic.phases[{index}]")
        name = phase.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise LoadProfileError(f"traffic.phases[{index}].name is invalid or duplicated")
        duration = _require_number(
            phase.get("duration_seconds"),
            field=f"traffic.phases[{index}].duration_seconds",
            minimum=1,
            maximum=3_600,
        )
        rate = _require_number(
            phase.get("target_logical_actions_per_second"),
            field=f"traffic.phases[{index}].target_logical_actions_per_second",
            minimum=0.1,
            maximum=512,
        )
        planned_actions = phase.get("logical_actions")
        if planned_actions is None:
            planned_actions = int(math.ceil(rate * duration))
        planned_actions = _require_int(
            planned_actions,
            field=f"traffic.phases[{index}].logical_actions",
            minimum=1,
            maximum=20_000,
        )
        if planned_actions > total_users:
            raise LoadProfileError("a phase requires more users than the fixture provides")
        names.add(name)
        total_actions += planned_actions
        phases.append({
            "name": name,
            "duration_seconds": duration,
            "target_logical_actions_per_second": rate,
            "logical_actions": planned_actions,
        })
    if total_actions > total_users:
        raise LoadProfileError("traffic.phases require more unique users than the fixture provides")
    return phases


def validate_profile(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a plain profile mapping."""

    if payload.get("schema") != PROFILE_SCHEMA:
        raise LoadProfileError("profile schema is unsupported")
    profile_id = payload.get("profile_id")
    if not isinstance(profile_id, str) or PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise LoadProfileError("profile_id must be a stable lowercase ID ending in -vN")
    _require_int(payload.get("profile_version"), field="profile_version", minimum=1)
    category = payload.get("category")
    if category not in ALLOWED_CATEGORIES:
        raise LoadProfileError(f"profile category is unsupported: {category!r}")
    mode = payload.get("mode")
    if mode not in ALLOWED_MODES:
        raise LoadProfileError(f"profile mode is unsupported: {mode!r}")
    for field in ("purpose", "description"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise LoadProfileError(f"profile {field} is required")

    fixture = _require_mapping(payload.get("fixture"), field="fixture")
    tournament_count = _require_int(
        fixture.get("tournament_count"),
        field="fixture.tournament_count",
        minimum=1,
        maximum=40,
    )
    users_per_tournament = _require_int(
        fixture.get("users_per_tournament"),
        field="fixture.users_per_tournament",
        minimum=14,
        maximum=500,
    )
    _require_int(
        fixture.get("setup_concurrency"),
        field="fixture.setup_concurrency",
        minimum=1,
        maximum=256,
    )
    max_total_users = _require_int(
        fixture.get("max_total_users"),
        field="fixture.max_total_users",
        minimum=1,
        maximum=20_000,
    )
    total_users = tournament_count * users_per_tournament
    if total_users > max_total_users or total_users > 20_000:
        raise LoadProfileError("profile fixture exceeds its declared user limit")

    traffic = _require_mapping(payload.get("traffic"), field="traffic")
    _require_int(traffic.get("concurrency"), field="traffic.concurrency", minimum=1, maximum=512)
    _require_number(
        traffic.get("spread_seconds"),
        field="traffic.spread_seconds",
        minimum=0,
        maximum=3_600,
    )
    _require_number(
        traffic.get("timeout_seconds"),
        field="traffic.timeout_seconds",
        minimum=0.1,
        maximum=300,
    )
    duplicate_count = _require_int(
        traffic.get("duplicate_count"),
        field="traffic.duplicate_count",
        minimum=0,
        maximum=total_users,
    )
    manual_refresh_count = _require_int(
        traffic.get("manual_refresh_count"),
        field="traffic.manual_refresh_count",
        minimum=0,
        maximum=total_users,
    )
    if mode == "ready-vote" and manual_refresh_count:
        raise LoadProfileError("ready-vote profiles cannot define manual refresh actions")
    if mode == "read-mix" and duplicate_count:
        raise LoadProfileError("read-mix profiles cannot define duplicate vote actions")
    workspace_users = sum(index % 10 < 5 for index in range(total_users))
    if manual_refresh_count > workspace_users:
        raise LoadProfileError("profile manual refresh count exceeds the workspace cohort")
    phase_plan = []
    if traffic.get("phases") is not None:
        phase_plan = _validate_phase_plan(traffic.get("phases"), total_users=total_users)
    if category in {"capacity", "spike"} and not phase_plan:
        raise LoadProfileError(f"{category} profiles must define traffic.phases")
    planned_actions = sum(int(phase["logical_actions"]) for phase in phase_plan)
    if duplicate_count > planned_actions and phase_plan:
        raise LoadProfileError("traffic.duplicate_count exceeds planned primary actions")

    retry = _require_mapping(traffic.get("retry"), field="traffic.retry")
    max_retries = _require_int(retry.get("max_retries"), field="traffic.retry.max_retries", minimum=0, maximum=2)
    if mode == "ready-vote":
        if retry.get("overload_status") != 503 or retry.get("overload_code") != "READY_VOTE_OVERLOADED":
            raise LoadProfileError("ready-vote retry policy must target READY_VOTE_OVERLOADED/503")
        windows = retry.get("jitter_windows_ms")
        if not isinstance(windows, list) or len(windows) != max_retries:
            raise LoadProfileError("ready-vote retry jitter windows must match max_retries")
        for index, window in enumerate(windows):
            if (
                not isinstance(window, list)
                or len(window) != 2
                or any(isinstance(item, bool) or not isinstance(item, int) for item in window)
                or not 0 <= window[0] <= window[1] <= 2_000
            ):
                raise LoadProfileError(f"profile traffic.retry.jitter_windows_ms[{index}] is invalid")
    elif max_retries != 0 or retry.get("jitter_windows_ms") != []:
        raise LoadProfileError("read-mix profiles cannot define retries")

    acceptance = _require_mapping(payload.get("acceptance"), field="acceptance")
    expected_kind = {
        "stress": "stress",
        "capacity": "capacity",
        "spike": "spike",
    }.get(category, "slo")
    if acceptance.get("kind") != expected_kind:
        raise LoadProfileError(
            f"profile acceptance.kind must be {expected_kind!r} for category {category!r}"
        )
    acceptance_budget = acceptance
    if expected_kind == "capacity":
        acceptance_budget = _require_mapping(acceptance.get("slo"), field="acceptance.slo")
        capacity = _require_mapping(acceptance.get("capacity"), field="acceptance.capacity")
        target_rates = capacity.get("target_logical_actions_per_second")
        if (
            not isinstance(target_rates, list)
            or len(target_rates) != len(phase_plan)
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in target_rates)
        ):
            raise LoadProfileError("acceptance.capacity target rates must match traffic.phases")
        _require_number(
            capacity.get("steady_duration_seconds"),
            field="acceptance.capacity.steady_duration_seconds",
            minimum=1,
            maximum=3_600,
        )
    _validate_latency_budget(
        acceptance_budget.get("accepted_request_latency"),
        field="acceptance.accepted_request_latency",
        percentiles=("p50", "p90", "p95", "p99"),
    )
    _validate_latency_budget(
        acceptance_budget.get("logical_latency"),
        field="acceptance.logical_latency",
        percentiles=("p95", "p99"),
    )
    if expected_kind not in {"stress", "spike"}:
        _require_number(
            acceptance_budget.get("logical_final_failure_percent"),
            field="acceptance.logical_final_failure_percent",
            minimum=0,
            maximum=100,
        )
    for field in ("max_shed_percent", "max_retry_amplification_percent"):
        _require_number(acceptance_budget.get(field), field=f"acceptance.{field}", minimum=0, maximum=1000)
    if expected_kind == "slo" and float(acceptance_budget["logical_final_failure_percent"]) > 0.5:
        raise LoadProfileError("canonical SLO final-failure budget cannot exceed 0.5 percent")
    if expected_kind in {"stress", "spike"}:
        _validate_latency_budget(
            acceptance.get("accepted_request_latency"),
            field="acceptance.accepted_request_latency",
            percentiles=("p50", "p90", "p95", "p99"),
        )
        for field in (
            "max_postgres_connections",
            "max_waiting_backends",
            "max_lock_waiters",
            "max_cpu_per_core_percent",
        ):
            _require_number(acceptance.get(field), field=f"acceptance.{field}", minimum=0)
        pool_budget = _validate_latency_budget(
            acceptance.get("pool_checkout_wait_ms"),
            field="acceptance.pool_checkout_wait_ms",
            percentiles=("p95", "p99"),
        )
        if float(pool_budget["p99_ms"]) < float(pool_budget["p95_ms"]):
            raise LoadProfileError("pool checkout p99 budget cannot be below p95")
    if "require_origin_evidence" in acceptance and not isinstance(
        acceptance.get("require_origin_evidence"), bool
    ):
        raise LoadProfileError("acceptance.require_origin_evidence must be boolean")
    resource_safety = acceptance.get("resource_safety")
    if resource_safety is not None:
        resource = _require_mapping(resource_safety, field="acceptance.resource_safety")
        for field in (
            "max_postgres_connections",
            "max_waiting_backends",
            "max_lock_waiters",
            "max_cpu_per_core_percent",
        ):
            _require_number(resource.get(field), field=f"acceptance.resource_safety.{field}", minimum=0)
        _validate_latency_budget(
            resource.get("pool_checkout_wait_ms"),
            field="acceptance.resource_safety.pool_checkout_wait_ms",
            percentiles=("p95", "p99"),
        )
    if expected_kind == "spike":
        recovery = _require_mapping(acceptance.get("recovery"), field="acceptance.recovery")
        for field in ("burst_phase", "recovery_phase"):
            if not isinstance(recovery.get(field), str) or not recovery[field].strip():
                raise LoadProfileError(f"acceptance.recovery.{field} is required")
    statuses = acceptance.get("expected_statuses")
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(isinstance(item, bool) or not isinstance(item, int) for item in statuses)
    ):
        raise LoadProfileError("profile acceptance.expected_statuses is invalid")
    _require_int(acceptance.get("unexpected_statuses"), field="acceptance.unexpected_statuses", minimum=0)

    correctness = _require_mapping(payload.get("correctness"), field="correctness")
    if correctness.get("cleanup_required") is not True:
        raise LoadProfileError("every canonical load profile must require cleanup")
    execution = _require_mapping(payload.get("execution"), field="execution")
    if execution.get("generator") != "GitHub-hosted external runner":
        raise LoadProfileError("canonical load generator must run on an external GitHub runner")
    if execution.get("measured_origin") != "https://old-sparky.com":
        raise LoadProfileError("canonical load profile must target the canonical public origin")
    if not isinstance(execution.get("cleanup_workflow"), str) or not isinstance(execution.get("abort_workflow"), str):
        raise LoadProfileError("canonical load profile must name cleanup and abort workflows")

    return dict(payload)


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LoadProfileError(f"profile {path.name} is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise LoadProfileError(f"profile {path.name} must be an object")
    try:
        return validate_profile(payload)
    except LoadProfileError as exc:
        raise LoadProfileError(f"{path.name}: {exc}") from exc


def load_profiles() -> dict[str, dict[str, Any]]:
    """Load the only authored canonical profile registry."""

    if not PROFILE_ROOT.is_dir():
        raise LoadProfileError(f"load profile directory is missing: {PROFILE_ROOT}")
    profiles: dict[str, dict[str, Any]] = {}
    versions: set[tuple[str, int]] = set()
    for path in sorted(PROFILE_ROOT.glob("*.json")):
        profile = _read_profile(path)
        profile_id = str(profile["profile_id"])
        version_key = (profile_id, int(profile["profile_version"]))
        if profile_id in profiles or version_key in versions:
            raise LoadProfileError(f"duplicate load profile ID/version: {profile_id}")
        profiles[profile_id] = profile
        versions.add(version_key)
    if not profiles:
        raise LoadProfileError("no canonical load profiles were found")
    return profiles


def profile_digest(profile: Mapping[str, Any]) -> str:
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_profile(profile_id: str) -> dict[str, Any]:
    profiles = load_profiles()
    try:
        return profiles[profile_id]
    except KeyError as exc:
        available = ", ".join(sorted(profiles))
        raise LoadProfileError(f"unknown load profile {profile_id!r}; available: {available}") from exc


def profile_contract(profile: Mapping[str, Any]) -> dict[str, Any]:
    fixture = profile["fixture"]
    traffic = profile["traffic"]
    acceptance = profile["acceptance"]
    total_users = int(fixture["tournament_count"]) * int(fixture["users_per_tournament"])
    return {
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "profile_digest": profile_digest(profile),
        "category": profile["category"],
        "mode": profile["mode"],
        "purpose": profile["purpose"],
        "description": profile["description"],
        "fixture": {
            "tournament_count": fixture["tournament_count"],
            "users_per_tournament": fixture["users_per_tournament"],
            "total_logical_users": total_users,
            "setup_concurrency": fixture["setup_concurrency"],
        },
        "traffic": {
            "concurrency": traffic["concurrency"],
            "spread_seconds": traffic["spread_seconds"],
            "timeout_seconds": traffic["timeout_seconds"],
            "duplicate_count": traffic["duplicate_count"],
            "manual_refresh_count": traffic["manual_refresh_count"],
            "retry": traffic["retry"],
            "phases": traffic.get("phases", []),
            "planned_logical_actions": sum(
                int(phase["logical_actions"])
                for phase in traffic.get("phases", [])
            ),
        },
        "acceptance": acceptance,
        "correctness": profile["correctness"],
        "execution": profile["execution"],
    }


def _env_values(profile: Mapping[str, Any]) -> dict[str, str]:
    fixture = profile["fixture"]
    traffic = profile["traffic"]
    return {
        "LOAD_MODE": str(profile["mode"]),
        "TOURNAMENT_COUNT": str(fixture["tournament_count"]),
        "USERS_PER_TOURNAMENT": str(fixture["users_per_tournament"]),
        "SETUP_CONCURRENCY": str(fixture["setup_concurrency"]),
        "LOAD_CONCURRENCY": str(traffic["concurrency"]),
        "SPREAD_SECONDS": str(traffic["spread_seconds"]),
        "DUPLICATE_COUNT": str(traffic["duplicate_count"]),
        "MANUAL_REFRESH_COUNT": str(traffic["manual_refresh_count"]),
        "LOAD_TIMEOUT_SECONDS": str(traffic["timeout_seconds"]),
        "LOAD_PROFILE_VERSION": str(profile["profile_version"]),
        "LOAD_PROFILE_DIGEST": profile_digest(profile),
    }


def _source_git_sha() -> str:
    candidate = os.environ.get("SOURCE_GIT_SHA", "").strip()
    if candidate:
        if SOURCE_SHA_RE.fullmatch(candidate) is None:
            raise LoadProfileError("SOURCE_GIT_SHA must be a lowercase 40-character SHA")
        return candidate
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PLATFORM_ROOT.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise LoadProfileError("unable to resolve source_git_sha") from exc
    candidate = result.stdout.strip()
    if result.returncode or SOURCE_SHA_RE.fullmatch(candidate) is None:
        raise LoadProfileError("unable to resolve a 40-character source_git_sha")
    return candidate


def run_profile(profile: Mapping[str, Any], manifest_path: Path, report_path: Path) -> int:
    # Imported lazily so profile listing and contract validation remain free of
    # application/runtime imports.  The module is the external runner client;
    # this process is expected to run on the GitHub-hosted load runner.
    try:
        from tools.platform_external_load import load_manifest, run_load
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_external_load import load_manifest, run_load

    contract = profile_contract(profile)
    manifest, users = load_manifest(manifest_path)
    traffic = profile["traffic"]
    acceptance = profile["acceptance"]
    report = run_load(
        manifest,
        users,
        mode=str(profile["mode"]),
        spread_seconds=float(traffic["spread_seconds"]),
        concurrency=int(traffic["concurrency"]),
        timeout=float(traffic["timeout_seconds"]),
        duplicate_count=int(traffic["duplicate_count"]),
        manual_refresh_count=int(traffic["manual_refresh_count"]),
        p95_budget_ms=float(
            (acceptance.get("logical_latency") or acceptance.get("slo", {}).get("logical_latency"))["p95_ms"]
        ),
        p99_budget_ms=float(
            (acceptance.get("logical_latency") or acceptance.get("slo", {}).get("logical_latency"))["p99_ms"]
        ),
        failure_budget_percent=(
            float(acceptance["logical_final_failure_percent"])
            if "logical_final_failure_percent" in acceptance
            else None
        ),
        retry_policy=traffic["retry"],
        phase_plan=traffic.get("phases") or None,
        scenario_kind=str(acceptance.get("kind") or "slo"),
        acceptance_contract=acceptance,
    )
    report["source_git_sha"] = _source_git_sha()
    report["load_contract"] = contract
    report["runner"] = {
        "name": "platform_load.py",
        "version": 1,
        "location": "GitHub-hosted external runner",
    }
    raw_http = report.get("raw_http") or report.get("overall") or {}
    logical = report.get("logical") or {}
    if logical:
        offered_actions = sum(
            int((phase.get("logical") or {}).get("actions") or 0)
            for phase in (report.get("phases") or {}).values()
            if isinstance(phase, dict) and phase.get("logical")
        )
    else:
        offered_actions = int(raw_http.get("requests") or 0)
    report["load_contract"]["offered_logical_actions"] = offered_actions
    report["load_contract"]["http_attempts"] = int(raw_http.get("requests") or 0)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "profile_id": contract["profile_id"],
                "profile_version": contract["profile_version"],
                "profile_digest": contract["profile_digest"],
                "source_git_sha": report["source_git_sha"],
                "mode": report.get("mode"),
                "passed": report.get("acceptance", {}).get("passed", False),
                "users": report.get("users"),
                "http_attempts": report["load_contract"]["http_attempts"],
                "logical_actions": report["load_contract"]["offered_logical_actions"],
            },
            ensure_ascii=False,
        )
    )
    acceptance_result = report.get("acceptance", {})
    return 0 if (
        acceptance_result.get("passed") is True
        or acceptance_result.get("pending_origin_evidence") is True
    ) else 1


def evaluate_report(
    profile: Mapping[str, Any],
    report_path: Path,
    server_observability_path: Path | None,
) -> int:
    """Attach origin evidence and make the final profile-owned decision."""

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LoadProfileError("external load report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise LoadProfileError("external load report must be an object")
    origin_observability: dict[str, Any] | None = None
    if server_observability_path is not None:
        try:
            candidate = json.loads(server_observability_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LoadProfileError("origin observability report is not valid JSON") from exc
        if not isinstance(candidate, dict):
            raise LoadProfileError("origin observability report must be an object")
        origin_observability = candidate
        report["origin_observability"] = {
            "schema": candidate.get("schema"),
            "started_at": candidate.get("started_at"),
            "finished_at": candidate.get("finished_at"),
            "stop_file_seen": candidate.get("stop_file_seen"),
            "timed_out": candidate.get("timed_out"),
            "system": candidate.get("system"),
            "server_request_perf_logs": candidate.get("server_request_perf_logs"),
        }
    try:
        from tools.platform_load_acceptance import evaluate_acceptance
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_acceptance import evaluate_acceptance
    acceptance = profile["acceptance"]
    report["acceptance"] = evaluate_acceptance(
        contract_ok=bool(
            (report.get("acceptance") or {}).get("contract_ok", False)
            or (report.get("contract") or {}).get("ok", False)
        ),
        logical_summary=report.get("logical") or report.get("overall") or {},
        raw_http_summary=report.get("raw_http") or report.get("overall") or {},
        acceptance_contract=acceptance,
        origin_observability=origin_observability,
        phase_summaries=(
            ((report.get("phases") or {}).get("ramp") or {}).get("phases")
            if isinstance(report.get("phases"), dict)
            else None
        ),
    )
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = report["acceptance"]
    print(json.dumps({
        "profile_id": profile["profile_id"],
        "decision": result.get("decision"),
        "passed": result.get("passed", False),
    }, ensure_ascii=False))
    return 0 if result.get("passed") is True else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage canonical OldSparky load profiles.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--profile", dest="profile_id")

    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("--profile", dest="profile_id", required=True)
    profile_parser.add_argument("--json", action="store_true")

    env_parser = subparsers.add_parser("export-env")
    env_parser.add_argument("--profile", dest="profile_id", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", dest="profile_id", required=True)
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--report-path", type=Path, required=True)

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--profile", dest="profile_id", required=True)
    evaluate_parser.add_argument("--report", type=Path, required=True)
    evaluate_parser.add_argument("--server-observability", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    profiles = load_profiles()
    if args.command == "list":
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": PROFILE_SCHEMA,
                        "profiles": [profile_contract(profiles[key]) for key in sorted(profiles)],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for profile_id in sorted(profiles):
                profile = profiles[profile_id]
                print(f"{profile_id}: {profile['category']} — {profile['description']}")
        return 0
    if args.command == "validate":
        if args.profile_id:
            get_profile(args.profile_id)
        print("platform load profiles: ok")
        return 0
    profile = get_profile(args.profile_id)
    if args.command == "profile":
        payload = profile_contract(profile)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"{payload['profile_id']} digest={payload['profile_digest']}")
        return 0
    if args.command == "export-env":
        for key, value in _env_values(profile).items():
            print(f"{key}={value}")
        return 0
    if args.command == "evaluate":
        return evaluate_report(profile, args.report, args.server_observability)
    return run_profile(profile, args.manifest, args.report_path)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LoadProfileError as exc:
        print(f"LOAD PROFILE BLOCKED: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
