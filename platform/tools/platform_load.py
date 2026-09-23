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
import sys
from typing import Any, Sequence

try:
    from platform_evidence_sanitizer import safe_error_class
except ModuleNotFoundError:  # Imported as ``tools.platform_load`` by tests.
    from tools.platform_evidence_sanitizer import safe_error_class


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = PLATFORM_ROOT / "performance" / "profiles"
PROFILE_SCHEMA = 2
REPORT_SCHEMA = 1
MEASUREMENT_SCHEMA = 2
TIMING_SCHEMA = 1
PROFILE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-v[0-9]+$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
FIXTURE_MARKER_RE = re.compile(r"^preprod[0-9]{12}[0-9a-f]{4}$")
ALLOWED_MODES = {"ready-vote", "read-mix", "page-load", "tournament-lifecycle"}
ALLOWED_CATEGORIES = {"load", "stress", "spike", "soak", "capacity"}
ALLOWED_PORTFOLIO_CLASSES = {"default", "diagnostic", "historical"}
ALLOWED_PORTFOLIO_STATUSES = {"active", "deprecated", "replacement-needed"}
ALLOWED_PORTFOLIO_CADENCES = {"release", "operator", "on-demand", "never"}
ALLOWED_PORTFOLIO_ENVIRONMENTS = {"production", "qa-preprod"}
MAX_CONCURRENCY = 512
MAX_PROFILE_BUDGET = 1_000_000_000_000.0
DEFAULT_CLIENT_TRANSPORT = "urllib-http1-close"
ALLOWED_PAGE_TRANSPORTS = {DEFAULT_CLIENT_TRANSPORT, "http1-keepalive"}


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
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise LoadProfileError(f"profile {field} must be a finite number") from exc
    if not math.isfinite(number):
        raise LoadProfileError(f"profile {field} must be finite")
    if minimum is not None and number < minimum:
        raise LoadProfileError(f"profile {field} is below {minimum}")
    if maximum is not None and number > maximum:
        raise LoadProfileError(f"profile {field} is above {maximum}")
    return number


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object members instead of silently overwriting."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LoadProfileError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


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


def _validate_optional_budget_number(
    mapping: Mapping[str, Any],
    key: str,
    *,
    field: str,
    minimum: float = 0,
    maximum: float | None = None,
) -> None:
    """Validate an optional numeric budget whenever it is authored.

    Unknown optional budget fields must not create a NaN/negative escape hatch:
    current profiles may omit a field, but an authored value is always
    validated with the same finite/range rules as required budgets.
    """

    if key in mapping:
        _require_number(
            mapping.get(key),
            field=field,
            minimum=minimum,
            maximum=maximum,
        )


def _validate_nonnegative_budget_tree(value: Any, *, field: str) -> None:
    """Reject non-finite/negative numeric leaves in an acceptance budget.

    Known budget fields receive their tighter field-specific bounds below.
    This closed-world walk covers newly authored budget members as well, so an
    unknown numeric escape hatch cannot carry ``NaN``, infinity or a negative
    value past profile validation.  Booleans are schema values rather than
    numeric budgets and are validated at their owning field when applicable.
    """

    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_nonnegative_budget_tree(child, field=f"{field}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_nonnegative_budget_tree(child, field=f"{field}[{index}]")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    _require_number(
        value,
        field=field,
        minimum=0,
        maximum=MAX_PROFILE_BUDGET,
    )


def _validate_phase_plan(value: Any, *, total_users: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LoadProfileError("traffic.phases must be a non-empty array")
    phases: list[dict[str, Any]] = []
    total_actions = 0
    names: set[str] = set()
    reserved_names = {"primary", "duplicate", "state", "ramp", "capacity_ramp"}
    for index, raw_phase in enumerate(value, start=1):
        phase = _require_mapping(raw_phase, field=f"traffic.phases[{index}]")
        name = phase.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in names
            or name in reserved_names
        ):
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


def _validate_concurrency_stages(
    value: Any,
    *,
    mode: str,
) -> list[int]:
    if value is None:
        return []
    if mode != "read-mix":
        raise LoadProfileError("traffic.concurrency_stages is only valid for read-mix profiles")
    if not isinstance(value, list) or not value:
        raise LoadProfileError("traffic.concurrency_stages must be a non-empty array")
    stages: list[int] = []
    previous = 0
    for index, raw_stage in enumerate(value):
        stage = _require_int(
            raw_stage,
            field=f"traffic.concurrency_stages[{index}]",
            minimum=1,
            maximum=MAX_CONCURRENCY,
        )
        if stage <= previous:
            raise LoadProfileError("traffic.concurrency_stages must be strictly ascending")
        stages.append(stage)
        previous = stage
    return stages


def _planned_work(payload: Mapping[str, Any]) -> dict[str, int | None]:
    """Return the bounded logical/request work implied by one profile.

    Ready Vote has duplicate actions, bounded retries, and one authoritative
    state GET per tournament in addition to the primary actions. Keep these
    costs in one planner so validation and runtime enforcement agree.
    Lifecycle profiles are executed by the separate QA harness and therefore
    deliberately have no external HTTP-attempt plan here.
    """

    fixture = _require_mapping(payload.get("fixture"), field="fixture")
    traffic = _require_mapping(payload.get("traffic"), field="traffic")
    mode = str(payload.get("mode") or "")
    total_users = int(fixture["tournament_count"]) * int(fixture["users_per_tournament"])
    phases = traffic.get("phases") or []
    primary_actions = (
        sum(int(phase["logical_actions"]) for phase in phases)
        if phases
        else total_users
    )
    duplicate_count = int(traffic.get("duplicate_count") or 0)
    manual_refresh_count = int(traffic.get("manual_refresh_count") or 0)
    retry = _require_mapping(traffic.get("retry"), field="traffic.retry")
    max_retries = int(retry.get("max_retries") or 0)

    if mode == "ready-vote":
        logical_actions = primary_actions + duplicate_count
        http_attempts = (
            (primary_actions + duplicate_count) * (max_retries + 1)
            + int(fixture["tournament_count"])
        )
        phase_logical_actions = {
            str(phase["name"]): int(phase["logical_actions"])
            for phase in phases
        }
        phase_logical_actions.update(
            {
                "primary": primary_actions,
                "duplicate": duplicate_count,
            }
        )
        return {
            "primary_logical_actions": primary_actions,
            "duplicate_logical_actions": duplicate_count,
            "state_read_requests": int(fixture["tournament_count"]),
            "logical_actions": logical_actions,
            "http_attempts": http_attempts,
            "phase_logical_actions": phase_logical_actions,
        }
    if mode == "read-mix":
        stages = traffic.get("concurrency_stages") or [traffic["concurrency"]]
        primary_requests = total_users * len(stages)
        logical_actions = primary_requests + manual_refresh_count
        return {
            "primary_logical_actions": primary_requests,
            "stage_logical_actions": {
                str(stage): total_users for stage in stages
            },
            "duplicate_logical_actions": 0,
            "state_read_requests": 0,
            "logical_actions": logical_actions,
            "http_attempts": logical_actions,
            "phase_logical_actions": {
                "read_mix": primary_requests,
                **(
                    {"manual_refresh": manual_refresh_count}
                    if manual_refresh_count > 0
                    else {}
                ),
            },
        }
    if mode == "page-load":
        return {
            "primary_logical_actions": total_users,
            "stage_logical_actions": {},
            "duplicate_logical_actions": 0,
            "state_read_requests": 0,
            "logical_actions": total_users,
            "http_attempts": total_users,
            "phase_logical_actions": {"authenticated_page_load": total_users},
        }
    return {
        "primary_logical_actions": primary_actions,
        "stage_logical_actions": {},
        "duplicate_logical_actions": 0,
        "state_read_requests": 0,
        "logical_actions": primary_actions,
        "http_attempts": None,
        "phase_logical_actions": {},
    }


def _validate_portfolio(value: Any) -> dict[str, Any]:
    portfolio = _require_mapping(value, field="portfolio")
    for field in ("owner", "hypothesis"):
        if not isinstance(portfolio.get(field), str) or not portfolio[field].strip():
            raise LoadProfileError(f"portfolio.{field} is required")
    for field, allowed in (
        ("class", ALLOWED_PORTFOLIO_CLASSES),
        ("status", ALLOWED_PORTFOLIO_STATUSES),
        ("cadence", ALLOWED_PORTFOLIO_CADENCES),
        ("environment", ALLOWED_PORTFOLIO_ENVIRONMENTS),
    ):
        if portfolio.get(field) not in allowed:
            raise LoadProfileError(f"portfolio.{field} is unsupported")
    request_budget = _require_mapping(
        portfolio.get("request_budget"),
        field="portfolio.request_budget",
    )
    for field in ("max_logical_actions", "max_http_attempts", "max_duration_seconds"):
        _require_int(
            request_budget.get(field),
            field=f"portfolio.request_budget.{field}",
            minimum=1,
            maximum=10_000_000,
        )
    cost_budget = _require_mapping(
        portfolio.get("cost_budget"),
        field="portfolio.cost_budget",
    )
    _require_number(
        cost_budget.get("max_runner_minutes"),
        field="portfolio.cost_budget.max_runner_minutes",
        minimum=0.1,
        maximum=100_000,
    )
    if not isinstance(cost_budget.get("basis"), str) or not cost_budget["basis"].strip():
        raise LoadProfileError("portfolio.cost_budget.basis is required")
    evidence = portfolio.get("last_accepted_evidence")
    if evidence is not None:
        evidence = _require_mapping(evidence, field="portfolio.last_accepted_evidence")
        run_id = evidence.get("run_id")
        if not isinstance(run_id, str) or re.fullmatch(r"[1-9][0-9]{0,31}", run_id) is None:
            raise LoadProfileError("portfolio.last_accepted_evidence.run_id is invalid")
        source_sha = evidence.get("source_sha")
        if not isinstance(source_sha, str) or SOURCE_SHA_RE.fullmatch(source_sha) is None:
            raise LoadProfileError("portfolio.last_accepted_evidence.source_sha is invalid")
    return dict(portfolio)


def _validate_portfolio_semantics(
    portfolio: Mapping[str, Any],
    *,
    mode: str,
    execution: Mapping[str, Any],
) -> None:
    """Reject class/status/cadence/environment/runner combinations with no owner."""

    portfolio_class = portfolio["class"]
    status = portfolio["status"]
    cadence = portfolio["cadence"]
    environment = portfolio["environment"]

    if portfolio_class == "default":
        if status != "active" or cadence != "release":
            raise LoadProfileError("default profiles must be active release profiles")
    elif portfolio_class == "diagnostic":
        allowed_cadences = {
            "active": {"release", "operator", "on-demand"},
            "deprecated": {"never"},
            "replacement-needed": {"on-demand", "never"},
        }
        if cadence not in allowed_cadences.get(status, set()):
            raise LoadProfileError(
                "diagnostic profile status/cadence combination is unsupported"
            )
    else:
        raise LoadProfileError("historical profiles are retained-only")

    if mode == "tournament-lifecycle":
        if (
            portfolio_class != "diagnostic"
            or status != "replacement-needed"
            or cadence != "on-demand"
            or environment != "qa-preprod"
        ):
            raise LoadProfileError(
                "lifecycle profiles must be diagnostic replacement-needed QA/preprod profiles"
            )
        if execution.get("non_dispatchable") is not True:
            raise LoadProfileError("lifecycle profiles must be explicitly non-dispatchable")
        if execution.get("profile_binding") != "not-integrated-with-production-qa":
            raise LoadProfileError(
                "lifecycle profiles must declare their missing profile/digest binding"
            )
        return

    if environment != "production":
        raise LoadProfileError("external profiles must declare the production environment")
    if execution.get("non_dispatchable") is True:
        raise LoadProfileError("external profiles cannot be marked non-dispatchable")


def validate_profile(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a plain profile mapping."""

    if not isinstance(payload, Mapping):
        raise LoadProfileError("profile must be an object")
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
    portfolio = _validate_portfolio(payload.get("portfolio"))
    if portfolio.get("class") == "historical":
        raise LoadProfileError("historical profiles are retained-only and cannot be runnable")
    _validate_nonnegative_budget_tree(
        portfolio.get("request_budget"),
        field="portfolio.request_budget",
    )
    _validate_nonnegative_budget_tree(
        portfolio.get("cost_budget"),
        field="portfolio.cost_budget",
    )

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
    _require_int(
        traffic.get("concurrency"),
        field="traffic.concurrency",
        minimum=1,
        maximum=MAX_CONCURRENCY,
    )
    _validate_concurrency_stages(
        traffic.get("concurrency_stages"),
        mode=str(mode),
    )
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
    client_transport = payload.get("client_transport", DEFAULT_CLIENT_TRANSPORT)
    if not isinstance(client_transport, str) or client_transport not in ALLOWED_PAGE_TRANSPORTS:
        raise LoadProfileError("profile client_transport is unsupported")
    if mode != "page-load" and client_transport != DEFAULT_CLIENT_TRANSPORT:
        raise LoadProfileError(
            "non-default client_transport is only supported for page-load profiles"
        )
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
        raise LoadProfileError("read-mix and page-load profiles cannot define retries")

    planned_work = _planned_work(payload)
    request_budget = _require_mapping(
        portfolio.get("request_budget"),
        field="portfolio.request_budget",
    )
    if int(planned_work["logical_actions"] or 0) > int(request_budget["max_logical_actions"]):
        raise LoadProfileError(
            "portfolio.max_logical_actions is below the planned logical workload"
        )
    planned_http_attempts = planned_work["http_attempts"]
    if (
        planned_http_attempts is not None
        and int(planned_http_attempts) > int(request_budget["max_http_attempts"])
    ):
        raise LoadProfileError(
            "portfolio.max_http_attempts is below worst-case retries, duplicates and state reads"
        )

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
        ):
            raise LoadProfileError("acceptance.capacity target rates must match traffic.phases")
        for index, value in enumerate(target_rates):
            _require_number(
                value,
                field=f"acceptance.capacity.target_logical_actions_per_second[{index}]",
                minimum=0,
                maximum=512,
            )
        _require_number(
            capacity.get("steady_duration_seconds"),
            field="acceptance.capacity.steady_duration_seconds",
            minimum=1,
            maximum=3_600,
        )
    # Validate every authored acceptance budget, including optional fields on
    # a profile kind that does not consume that field.  Otherwise a negative
    # or non-finite value could be silently ignored by the evaluator branch.
    for field, maximum in (
        ("logical_final_failure_percent", 100),
        ("max_duplicate_failure_percent", 100),
        ("max_shed_percent", 100),
        ("max_retry_amplification_percent", 1000),
        ("minimum_useful_goodput_actions_per_second", 1_000_000_000),
        ("max_postgres_backend_connections", 1_000_000),
        ("max_waiting_backends", 1_000_000),
        ("max_lock_waiters", 1_000_000),
        ("max_cpu_per_core_percent", 100),
    ):
        _validate_optional_budget_number(
            acceptance,
            field,
            field=f"acceptance.{field}",
            minimum=0,
            maximum=maximum,
        )
        if acceptance_budget is not acceptance:
            _validate_optional_budget_number(
                acceptance_budget,
                field,
                field=f"acceptance.slo.{field}",
                minimum=0,
                maximum=maximum,
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
    _require_number(
        acceptance_budget.get("max_shed_percent"),
        field="acceptance.max_shed_percent",
        minimum=0,
        maximum=100,
    )
    _require_number(
        acceptance_budget.get("max_retry_amplification_percent"),
        field="acceptance.max_retry_amplification_percent",
        minimum=0,
        maximum=1000,
    )
    if expected_kind == "slo" and float(acceptance_budget["logical_final_failure_percent"]) > 0.5:
        raise LoadProfileError("canonical SLO final-failure budget cannot exceed 0.5 percent")
    if expected_kind in {"stress", "spike"} and mode != "tournament-lifecycle":
        _require_number(
            acceptance.get("minimum_useful_goodput_actions_per_second"),
            field="acceptance.minimum_useful_goodput_actions_per_second",
            minimum=0.0001,
            maximum=1_000_000_000,
        )
        _validate_latency_budget(
            acceptance.get("accepted_request_latency"),
            field="acceptance.accepted_request_latency",
            percentiles=("p50", "p90", "p95", "p99"),
        )
        for field in (
            "max_postgres_backend_connections",
            "max_waiting_backends",
            "max_lock_waiters",
            "max_cpu_per_core_percent",
        ):
            _require_number(
                acceptance.get(field),
                field=f"acceptance.{field}",
                minimum=0,
                maximum=100 if field == "max_cpu_per_core_percent" else 1_000_000,
            )
        pool_budget = _validate_latency_budget(
            acceptance.get("pool_checkout_wait_ms"),
            field="acceptance.pool_checkout_wait_ms",
            percentiles=("p95", "p99"),
        )
        if float(pool_budget["p99_ms"]) < float(pool_budget["p95_ms"]):
            raise LoadProfileError("pool checkout p99 budget cannot be below p95")
    elif expected_kind == "capacity" and mode != "tournament-lifecycle":
        _require_number(
            acceptance.get("minimum_useful_goodput_actions_per_second"),
            field="acceptance.minimum_useful_goodput_actions_per_second",
            minimum=0.0001,
            maximum=1_000_000_000,
        )
    if "require_origin_evidence" in acceptance and not isinstance(
        acceptance.get("require_origin_evidence"), bool
    ):
        raise LoadProfileError("acceptance.require_origin_evidence must be boolean")
    resource_safety = acceptance.get("resource_safety")
    if resource_safety is not None:
        resource = _require_mapping(resource_safety, field="acceptance.resource_safety")
        for field in (
            "max_postgres_backend_connections",
            "max_waiting_backends",
            "max_lock_waiters",
            "max_cpu_per_core_percent",
        ):
            _require_number(
                resource.get(field),
                field=f"acceptance.resource_safety.{field}",
                minimum=0,
                maximum=100 if field == "max_cpu_per_core_percent" else 1_000_000,
            )
        _validate_latency_budget(
            resource.get("pool_checkout_wait_ms"),
            field="acceptance.resource_safety.pool_checkout_wait_ms",
            percentiles=("p95", "p99"),
        )
    if expected_kind == "spike":
        recovery = _require_mapping(acceptance.get("recovery"), field="acceptance.recovery")
        phase_names = {str(phase["name"]) for phase in phase_plan}
        for field in ("burst_phase", "recovery_phase"):
            if not isinstance(recovery.get(field), str) or not recovery[field].strip():
                raise LoadProfileError(f"acceptance.recovery.{field} is required")
            if recovery[field] not in phase_names:
                raise LoadProfileError(
                    f"acceptance.recovery.{field} must name a configured traffic phase"
                )
    _validate_nonnegative_budget_tree(acceptance, field="acceptance")
    statuses = acceptance.get("expected_statuses")
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 100 <= item <= 599
            for item in statuses
        )
        or len(set(statuses)) != len(statuses)
    ):
        raise LoadProfileError("profile acceptance.expected_statuses is invalid")
    _require_int(
        acceptance.get("unexpected_statuses"),
        field="acceptance.unexpected_statuses",
        minimum=0,
        maximum=1_000_000_000,
    )

    correctness = _require_mapping(payload.get("correctness"), field="correctness")
    if correctness.get("cleanup_required") is not True:
        raise LoadProfileError("every canonical load profile must require cleanup")
    execution = _require_mapping(payload.get("execution"), field="execution")
    if not isinstance(execution.get("require_exact_observer_binding"), bool):
        raise LoadProfileError("execution.require_exact_observer_binding must be boolean")
    if mode == "tournament-lifecycle":
        if execution.get("generator") != "platform_production_qa.py":
            raise LoadProfileError(
                "tournament-lifecycle profiles must use platform_production_qa.py"
            )
        if execution.get("external_runner_forbidden") is not True:
            raise LoadProfileError(
                "tournament-lifecycle profiles must forbid the external load runner"
            )
        if execution.get("measured_origin") != "configured QA/preprod origin":
            raise LoadProfileError(
                "tournament-lifecycle profiles must target the configured QA/preprod origin"
            )
    else:
        if execution.get("generator") != "GitHub-hosted external runner":
            raise LoadProfileError("canonical load generator must run on an external GitHub runner")
        if execution.get("measured_origin") != "https://old-sparky.com":
            raise LoadProfileError("canonical load profile must target the canonical public origin")
    if not isinstance(execution.get("cleanup_workflow"), str) or not isinstance(execution.get("abort_workflow"), str):
        raise LoadProfileError("canonical load profile must name cleanup and abort workflows")
    _validate_portfolio_semantics(
        portfolio,
        mode=str(mode),
        execution=execution,
    )

    return dict(payload)


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
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
        "schema": PROFILE_SCHEMA,
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "profile_digest": profile_digest(profile),
        "category": profile["category"],
        "mode": profile["mode"],
        "environment": profile["portfolio"]["environment"],
        "client_transport": profile.get("client_transport", DEFAULT_CLIENT_TRANSPORT),
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
            "concurrency_stages": traffic.get("concurrency_stages") or [],
            "spread_seconds": traffic["spread_seconds"],
            "timeout_seconds": traffic["timeout_seconds"],
            "duplicate_count": traffic["duplicate_count"],
            "manual_refresh_count": traffic["manual_refresh_count"],
            "retry": traffic["retry"],
            "phases": traffic.get("phases") or [],
            "planned_logical_actions": sum(
                int(phase["logical_actions"])
                for phase in (traffic.get("phases") or [])
            ),
        },
        "acceptance": acceptance,
        "correctness": profile["correctness"],
        "execution": profile["execution"],
        "portfolio": profile["portfolio"],
        "planned_work": _planned_work(profile),
    }


def _canonical_value_matches(actual: Any, expected: Any) -> bool:
    """Compare JSON contract values without Python bool/int coercion."""

    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return False
        return (
            set(actual) == set(expected)
            and all(
                _canonical_value_matches(actual[key], value)
                for key, value in expected.items()
            )
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _canonical_value_matches(actual_value, expected_value)
                for actual_value, expected_value in zip(actual, expected, strict=True)
            )
        )
    return type(actual) is type(expected) and actual == expected


def _exact_value(actual: Any, expected: Any) -> bool:
    """Match a scalar while rejecting bool/int and other JSON coercions."""

    return type(actual) is type(expected) and actual == expected


def _strict_nonnegative_int(value: Any) -> int | None:
    """Read a JSON counter without accepting bools, floats or strings."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _runtime_budget_binding(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    report: Mapping[str, Any],
    actual_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate dispatcher-added accounting against the selected profile."""

    planned_work = contract.get("planned_work")
    planned_work = planned_work if isinstance(planned_work, Mapping) else {}
    traffic = profile.get("traffic")
    traffic = traffic if isinstance(traffic, Mapping) else {}
    retry = traffic.get("retry")
    retry = retry if isinstance(retry, Mapping) else {}
    mode = profile.get("mode")
    primary = _strict_nonnegative_int(planned_work.get("primary_logical_actions"))
    total_logical = _strict_nonnegative_int(planned_work.get("logical_actions"))
    state_reads = _strict_nonnegative_int(planned_work.get("state_read_requests"))
    worst_case = planned_work.get("http_attempts")
    worst_case = (
        _strict_nonnegative_int(worst_case)
        if worst_case is not None
        else None
    )
    max_http = _strict_nonnegative_int(
        (profile.get("portfolio") or {}).get("request_budget", {}).get(
            "max_http_attempts"
        )
    )
    max_retries = _strict_nonnegative_int(retry.get("max_retries"))
    max_retries = 0 if max_retries is None else max_retries
    overall = report.get("overall")
    overall = overall if isinstance(overall, Mapping) else {}
    actual_overall = _strict_nonnegative_int(overall.get("requests"))
    raw_http = report.get("raw_http")
    raw_http = raw_http if isinstance(raw_http, Mapping) else {}
    actual_raw_requests = _strict_nonnegative_int(raw_http.get("requests"))
    actual_state_reads = _strict_nonnegative_int(
        raw_http.get("state_read_requests")
    )
    actual_raw_total = _strict_nonnegative_int(
        raw_http.get("total_requests_including_state")
    )
    runtime = actual_contract.get("runtime_http_budget")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    runtime_actual = _strict_nonnegative_int(runtime.get("actual_http_attempts"))
    runtime_max = _strict_nonnegative_int(runtime.get("max_http_attempts"))
    runtime_planned = runtime.get("planned_worst_case")
    runtime_planned = (
        _strict_nonnegative_int(runtime_planned)
        if runtime_planned is not None
        else None
    )
    top_fields = {
        field: _strict_nonnegative_int(actual_contract.get(field))
        for field in (
            "offered_logical_actions",
            "primary_http_attempts",
            "http_attempts",
            "total_http_attempts",
        )
    }
    expected_total_requests = (
        total_logical + state_reads
        if mode == "ready-vote"
        and total_logical is not None
        and state_reads is not None
        else total_logical
    )
    minimum_total_requests = expected_total_requests
    maximum_total_requests = worst_case
    if mode == "ready-vote" and primary is not None:
        primary_min = primary
        primary_max = primary * (max_retries + 1)
    else:
        primary_min = expected_total_requests
        primary_max = expected_total_requests
    runtime_keys = {
        "planned_worst_case",
        "max_http_attempts",
        "actual_http_attempts",
        "within_budget",
    }
    runtime_checks = {
        "runtime_budget_present": set(runtime) == runtime_keys,
        "planned_worst_case_matches_profile": runtime_planned == worst_case,
        "max_http_attempts_matches_profile": runtime_max == max_http,
        "actual_http_attempts_matches_overall": (
            runtime_actual is not None
            and actual_overall is not None
            and runtime_actual == actual_overall
        ),
        "within_budget_is_typed": isinstance(runtime.get("within_budget"), bool),
        "within_budget_is_recomputed": (
            isinstance(runtime.get("within_budget"), bool)
            and actual_overall is not None
            and max_http is not None
            and runtime.get("within_budget") is (actual_overall <= max_http)
        ),
        "offered_logical_actions_matches_profile": (
            top_fields["offered_logical_actions"] == total_logical
        ),
        "primary_http_attempts_reconcilable": (
            top_fields["primary_http_attempts"] is not None
            and primary_min is not None
            and primary_max is not None
            and primary_min <= top_fields["primary_http_attempts"] <= primary_max
        ),
        "http_attempts_matches_overall": (
            top_fields["http_attempts"] is not None
            and actual_overall is not None
            and top_fields["http_attempts"] == actual_overall
        ),
        "total_http_attempts_matches_overall": (
            top_fields["total_http_attempts"] is not None
            and actual_overall is not None
            and top_fields["total_http_attempts"] == actual_overall
        ),
        "total_http_attempts_reconcilable": (
            actual_overall is not None
            and minimum_total_requests is not None
            and actual_overall >= minimum_total_requests
            and (
                maximum_total_requests is None
                or actual_overall <= maximum_total_requests
            )
        ),
        "raw_http_present": bool(raw_http),
        "raw_requests_typed": actual_raw_requests is not None,
        "raw_requests_match_logical_plan": (
            actual_raw_requests is not None
            and total_logical is not None
            and actual_raw_requests >= total_logical
            and (
                mode == "ready-vote"
                or actual_raw_requests == total_logical
            )
            and (
                mode != "ready-vote"
                or actual_raw_requests <= total_logical * (max_retries + 1)
            )
        ),
        "raw_requests_match_overall": (
            actual_overall is not None
            and actual_raw_requests is not None
            and (
                actual_raw_requests == actual_overall
                if mode != "ready-vote"
                else (
                    actual_state_reads is not None
                    and actual_raw_total is not None
                    and actual_state_reads == state_reads
                    and actual_raw_total == actual_overall
                    and actual_raw_requests + actual_state_reads == actual_overall
                )
            )
        ),
    }
    return {
        "complete": all(runtime_checks.values()),
        "checks": runtime_checks,
        "actual_overall_requests": actual_overall,
        "expected_total_requests": expected_total_requests,
        "expected_logical_actions": total_logical,
        "expected_state_reads": state_reads,
        "expected_worst_case_http_attempts": worst_case,
    }


def _report_phase_envelope(profile: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the report's closed-world population and phase envelopes."""

    mode = profile.get("mode")
    traffic = profile.get("traffic")
    traffic = traffic if isinstance(traffic, Mapping) else {}
    phase_plan = traffic.get("phases")
    phase_plan = phase_plan if isinstance(phase_plan, list) else []
    phases = report.get("phases")
    phases = phases if isinstance(phases, Mapping) else {}
    if mode == "ready-vote":
        expected_names = [
            phase.get("name")
            for phase in phase_plan
            if isinstance(phase, Mapping)
        ]
        expected_keys = {"primary", "duplicate", "state"}
        if phase_plan:
            expected_keys.add("ramp")
        phase_keys_closed = set(phases) == expected_keys
        ramp = phases.get("ramp")
        ramp = ramp if isinstance(ramp, Mapping) else {}
        authored = ramp.get("phases")
        authored = authored if isinstance(authored, Mapping) else {}
        authored_names = list(authored)
        phase_plan_closed = (
            (not phase_plan and "ramp" not in phases)
            or (
                bool(phase_plan)
                and isinstance(phases.get("ramp"), Mapping)
                and authored_names == expected_names
            )
        )
        phase_values_mapping = all(
            isinstance(phases.get(name), Mapping) for name in expected_keys
        )
        if phase_plan and isinstance(ramp.get("phases"), Mapping):
            phase_values_mapping = phase_values_mapping and all(
                isinstance(value, Mapping) for value in ramp["phases"].values()
            )
    elif mode == "read-mix":
        expected_keys = {"read_mix"}
        if int(traffic.get("manual_refresh_count") or 0) > 0:
            expected_keys.add("manual_refresh")
        if traffic.get("concurrency_stages") is not None:
            expected_keys.add("capacity_ramp")
        phase_keys_closed = set(phases) == expected_keys
        phase_plan_closed = True
        phase_values_mapping = all(
            isinstance(phases.get(name), Mapping) for name in expected_keys
        )
        if "capacity_ramp" in expected_keys:
            ramp_stages = phases.get("capacity_ramp", {}).get("stages")
            expected_stages = {
                str(stage) for stage in (traffic.get("concurrency_stages") or [])
            }
            phase_values_mapping = phase_values_mapping and isinstance(
                ramp_stages, Mapping
            ) and set(str(stage) for stage in ramp_stages) == expected_stages and all(
                isinstance(value, Mapping) for value in ramp_stages.values()
            )
            if isinstance(ramp_stages, Mapping):
                phase_values_mapping = phase_values_mapping and list(
                    str(stage) for stage in ramp_stages
                ) == list(
                    str(stage) for stage in (traffic.get("concurrency_stages") or [])
                )
    elif mode == "page-load":
        phase_keys_closed = set(phases) == {"authenticated_page_load"}
        phase_plan_closed = True
        phase_values_mapping = isinstance(phases.get("authenticated_page_load"), Mapping)
    else:
        expected_names = [
            phase.get("name")
            for phase in phase_plan
            if isinstance(phase, Mapping)
        ]
        phase_keys_closed = set(phases) == set(expected_names)
        phase_plan_closed = list(phases) == expected_names
        phase_values_mapping = all(
            isinstance(phases.get(name), Mapping) for name in expected_names
        )
    acceptance = report.get("acceptance")
    acceptance = acceptance if isinstance(acceptance, Mapping) else {}
    overall = report.get("overall")
    overall = overall if isinstance(overall, Mapping) else {}
    raw_http = report.get("raw_http")
    raw_http = raw_http if isinstance(raw_http, Mapping) else {}
    logical = report.get("logical")
    logical = logical if isinstance(logical, Mapping) else {}
    expected_logical_scope = (
        "logical_user_actions" if mode == "ready-vote" else "full_population"
    )
    # ``overall`` is the aggregate HTTP envelope (including Ready Vote state
    # reads), not a second logical-action summary.  Keep the boundary closed
    # here as well as in the acceptance normalizer so a report cannot carry a
    # plausible raw request count alongside logical-only counters and have
    # runtime-budget binding mistake the two populations for one another.
    overall_logical_only = {
        "actions",
        "final_successes",
        "final_failures",
        "final_status_counts",
    }
    checks = {
        "report_scope": report.get("scope") == "full_population",
        "overall_present": bool(overall),
        "overall_scope": overall.get("scope") == "full_population",
        "overall_scope_shape": not any(key in overall for key in overall_logical_only),
        "raw_http_present": bool(raw_http),
        "raw_http_scope": raw_http.get("scope") == "full_population",
        "logical_present": bool(logical),
        "logical_scope": logical.get("scope") == expected_logical_scope,
        "phases_present": isinstance(report.get("phases"), Mapping),
        "phase_keys_closed": phase_keys_closed,
        "phase_values_mapping": phase_values_mapping,
        "phase_plan_closed": phase_plan_closed,
        "acceptance_present": isinstance(report.get("acceptance"), Mapping),
        "acceptance_contract_ok_typed": isinstance(acceptance.get("contract_ok"), bool),
        "fixture_marker": (
            isinstance(report.get("fixture_marker"), str)
            and FIXTURE_MARKER_RE.fullmatch(report["fixture_marker"]) is not None
        ),
        "legacy_contract_override_absent": "contract" not in report,
    }
    return {"complete": all(checks.values()), "checks": checks}


def _report_binding(profile: Mapping[str, Any], report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate that a report belongs to this profile and this workflow run.

    The external report is untrusted artifact input.  Runtime measurements may
    add explicitly-owned fields to ``load_contract``; every authored contract
    field still has to match the selected profile exactly.  There is no legacy
    acceptance path: missing identity or schema evidence is a failed binding.
    """

    expected_contract = profile_contract(profile)
    actual_contract = report.get("load_contract")
    actual_contract_mapping = actual_contract if isinstance(actual_contract, Mapping) else {}
    allowed_runtime_fields = {
        "runtime_http_budget",
        "offered_logical_actions",
        "primary_http_attempts",
        "http_attempts",
        "total_http_attempts",
    }
    unexpected_contract_fields = sorted(
        key
        for key in actual_contract_mapping
        if key not in expected_contract and key not in allowed_runtime_fields
    )
    authored_contract = {
        key: value
        for key, value in actual_contract_mapping.items()
        if key not in allowed_runtime_fields
    }
    contract_ok = (
        isinstance(actual_contract, Mapping)
        and not unexpected_contract_fields
        and _canonical_value_matches(authored_contract, expected_contract)
    )
    runtime_budget = _runtime_budget_binding(
        profile,
        expected_contract,
        report,
        actual_contract_mapping,
    )
    envelope = _report_phase_envelope(profile, report)

    expected_source = os.environ.get("SOURCE_GIT_SHA", "").strip()
    actual_source = report.get("source_git_sha")
    source_ok = (
        isinstance(actual_source, str)
        and SOURCE_SHA_RE.fullmatch(actual_source) is not None
        and SOURCE_SHA_RE.fullmatch(expected_source) is not None
        and actual_source == expected_source
    )

    expected_run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    actual_run_id = report.get("external_run_id")
    run_identity_ok = (
        isinstance(actual_run_id, str)
        and RUN_ID_RE.fullmatch(actual_run_id) is not None
        and RUN_ID_RE.fullmatch(expected_run_id) is not None
        and actual_run_id == expected_run_id
    )

    expected_profile_id = profile.get("profile_id")
    expected_profile_version = profile.get("profile_version")
    expected_mode = profile.get("mode")
    expected_environment = (profile.get("portfolio") or {}).get("environment")
    contract_portfolio = actual_contract_mapping.get("portfolio")
    contract_environment = (
        contract_portfolio.get("environment")
        if isinstance(contract_portfolio, Mapping)
        else None
    )
    checks = {
        "report_schema": _exact_value(report.get("schema"), REPORT_SCHEMA),
        "measurement_schema": _exact_value(
            report.get("measurement_schema"), MEASUREMENT_SCHEMA
        ),
        "timing_schema": _exact_value(report.get("timing_schema"), TIMING_SCHEMA),
        "profile_id": _exact_value(report.get("profile_id"), expected_profile_id),
        "profile_version": _exact_value(
            report.get("profile_version"), expected_profile_version
        ),
        "profile_digest": _exact_value(
            report.get("profile_digest"), expected_contract["profile_digest"]
        ),
        "contract_schema": _exact_value(
            actual_contract_mapping.get("schema"), PROFILE_SCHEMA
        ),
        "contract": contract_ok,
        "mode": _exact_value(report.get("mode"), expected_mode)
        and _exact_value(actual_contract_mapping.get("mode"), expected_mode),
        "environment": _exact_value(report.get("environment"), expected_environment)
        and _exact_value(actual_contract_mapping.get("environment"), expected_environment)
        and _exact_value(contract_environment, expected_environment),
        "source_git_sha": source_ok,
        "run_identity": run_identity_ok,
        "authoritative": report.get("authoritative") is True,
        "dispatchable": report.get("dispatchable") is True,
        "runtime_budget": bool(runtime_budget["complete"]),
        "evidence_envelope": bool(envelope["complete"]),
    }
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "expected": {
            "profile_id": expected_profile_id,
            "profile_version": expected_profile_version,
            "profile_digest": expected_contract["profile_digest"],
            "schema": PROFILE_SCHEMA,
            "mode": expected_mode,
            "environment": expected_environment,
            "source_git_sha": expected_source or None,
            "external_run_id": expected_run_id or None,
        },
        "actual": {
            "profile_id": report.get("profile_id"),
            "profile_version": report.get("profile_version"),
            "profile_digest": report.get("profile_digest"),
            "schema": actual_contract_mapping.get("schema"),
            "mode": report.get("mode"),
            "environment": report.get("environment"),
            "contract_environment": contract_environment,
            "source_git_sha": actual_source,
            "external_run_id": actual_run_id,
        },
        "unexpected_contract_fields": unexpected_contract_fields,
        "runtime_budget": runtime_budget,
        "evidence_envelope": envelope,
    }


def ensure_dispatchable(profile: Mapping[str, Any]) -> None:
    """Fail before fixture setup when a profile is not a runnable contour."""

    portfolio = profile["portfolio"]
    if (
        portfolio.get("class") != "default" and portfolio.get("class") != "diagnostic"
    ) or portfolio.get("status") != "active":
        raise LoadProfileError(
            f"load profile {profile.get('profile_id')!r} is not dispatchable: "
            f"{portfolio.get('class')}/{portfolio.get('status')}"
        )
    if profile.get("mode") == "tournament-lifecycle":
        raise LoadProfileError(
            "lifecycle profiles are non-dispatchable until platform_production_qa.py "
            "records profile ID and digest"
        )
    if portfolio.get("environment") != "production":
        raise LoadProfileError("dispatchable external profiles must target production")


def _write_failed_report(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    report_path: Path,
    error: BaseException,
    *,
    decision: str = "LOAD RUN FAILED",
) -> None:
    """Persist a schema-compatible failed report for runner/setup errors."""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    error_class = safe_error_class(type(error).__name__)
    payload = {
        "schema": REPORT_SCHEMA,
        "measurement_schema": MEASUREMENT_SCHEMA,
        "timing_schema": TIMING_SCHEMA,
        "profile_id": profile.get("profile_id"),
        "profile_version": profile.get("profile_version"),
        "profile_digest": profile_digest(profile),
        "mode": profile.get("mode"),
        "environment": (profile.get("portfolio") or {}).get("environment"),
        "passed": False,
        "authoritative": False,
        "dispatchable": False,
        "error_class": error_class,
        "load_contract": dict(contract),
        "acceptance": {
            "passed": False,
            "decision": decision,
            "authoritative": False,
            "dispatchable": False,
            "contract_ok": False,
            "error_class": error_class,
        },
    }
    report_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _run_external_load_or_report(
    run_load: Any,
    *,
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    report_path: Path,
    error_type: type[BaseException],
    **kwargs: Any,
) -> dict[str, Any] | None:
    try:
        return run_load(**kwargs)
    except error_type as exc:
        _write_failed_report(profile, contract, report_path, exc)
        print(
            json.dumps(
                {
                    "profile_id": profile.get("profile_id"),
                    "decision": "LOAD RUN FAILED",
                    "passed": False,
                    "error_class": safe_error_class(type(exc).__name__),
                },
                ensure_ascii=False,
            )
        )
        return None


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
    """Return the explicit source identity supplied by the workflow.

    A local checkout's HEAD is not authoritative evidence for a retained
    external report: the checkout may be dirty, stale, or different from the
    source that prepared the fixture.  Canonical run/evaluate paths therefore
    require the workflow-provided SHA and never consult git as a fallback.
    """

    candidate = os.environ.get("SOURCE_GIT_SHA", "").strip()
    if SOURCE_SHA_RE.fullmatch(candidate) is None:
        raise LoadProfileError(
            "SOURCE_GIT_SHA is required and must be a lowercase 40-character SHA"
        )
    return candidate


def _external_run_id() -> str:
    """Return the explicit GitHub workflow run identity."""

    candidate = os.environ.get("GITHUB_RUN_ID", "").strip()
    if RUN_ID_RE.fullmatch(candidate) is None:
        raise LoadProfileError(
            "GITHUB_RUN_ID is required and must be a numeric workflow run ID"
        )
    return candidate


def run_profile(
    profile: Mapping[str, Any],
    manifest_path: Path,
    report_path: Path,
    *,
    timeout_diagnostics_run_id: str | None = None,
) -> int:
    ensure_dispatchable(profile)
    # Imported lazily so profile listing and contract validation remain free of
    # application/runtime imports.  The module is the external runner client;
    # this process is expected to run on the GitHub-hosted load runner.
    try:
        from tools.platform_external_load import ExternalLoadError, load_manifest, run_load
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_external_load import ExternalLoadError, load_manifest, run_load

    contract = profile_contract(profile)
    try:
        source_git_sha = _source_git_sha()
        external_run_id = _external_run_id()
    except LoadProfileError as exc:
        _write_failed_report(
            profile,
            contract,
            report_path,
            exc,
            decision="LOAD REPORT BINDING FAIL",
        )
        print(
            json.dumps(
                {
                    "profile_id": profile.get("profile_id"),
                    "decision": "LOAD REPORT BINDING FAIL",
                    "passed": False,
                    "error_class": safe_error_class(type(exc).__name__),
                },
                ensure_ascii=False,
            )
        )
        return 1
    try:
        manifest, users = load_manifest(manifest_path)
    except ExternalLoadError as exc:
        _write_failed_report(profile, contract, report_path, exc)
        print(
            json.dumps(
                {
                    "profile_id": profile.get("profile_id"),
                    "decision": "LOAD RUN FAILED",
                    "passed": False,
                    "error_class": safe_error_class(type(exc).__name__),
                },
                ensure_ascii=False,
            )
        )
        return 1
    traffic = profile["traffic"]
    acceptance = profile["acceptance"]
    client_transport = str(profile.get("client_transport", DEFAULT_CLIENT_TRANSPORT))
    report = _run_external_load_or_report(
        run_load,
        profile=profile,
        contract=contract,
        report_path=report_path,
        error_type=ExternalLoadError,
        manifest=manifest,
        users=users,
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
        concurrency_stages=traffic.get("concurrency_stages") or None,
        scenario_kind=str(acceptance.get("kind") or "slo"),
        acceptance_contract=acceptance,
        require_exact_observer_binding=(
            profile.get("execution", {}).get("require_exact_observer_binding") is True
        ),
        authoritative_binding={
            "profile_id": contract["profile_id"],
            "profile_version": contract["profile_version"],
            "profile_digest": contract["profile_digest"],
            "source_git_sha": source_git_sha,
            "external_run_id": external_run_id,
        },
        expected_profile_id=str(contract["profile_id"]),
        expected_profile_version=int(contract["profile_version"]),
        expected_profile_digest=str(contract["profile_digest"]),
        expected_primary_action_count=int(
            contract["planned_work"]["primary_logical_actions"]
        ),
        expected_total_logical_action_count=int(
            contract["planned_work"]["logical_actions"]
        ),
        expected_stage_action_counts=(
            contract["planned_work"].get("stage_logical_actions")
            if profile.get("mode") == "read-mix"
            and profile.get("traffic", {}).get("concurrency_stages") is not None
            else None
        ),
        expected_state_read_count=int(
            contract["planned_work"]["state_read_requests"]
        ),
        timeout_diagnostics_run_id=timeout_diagnostics_run_id,
        client_transport=client_transport,
        max_http_attempts=int(profile["portfolio"]["request_budget"]["max_http_attempts"]),
    )
    if report is None:
        return 1
    report["source_git_sha"] = source_git_sha
    report["external_run_id"] = external_run_id
    report["schema"] = REPORT_SCHEMA
    report["measurement_schema"] = MEASUREMENT_SCHEMA
    report["timing_schema"] = TIMING_SCHEMA
    report["profile_id"] = contract["profile_id"]
    report["profile_version"] = contract["profile_version"]
    report["profile_digest"] = contract["profile_digest"]
    report["environment"] = contract["environment"]
    report["load_contract"] = contract
    report["authoritative"] = True
    report["dispatchable"] = True
    report["runner"] = {
        "name": "platform_load.py",
        "version": 2,
        "location": "GitHub-hosted external runner",
    }
    raw_http = report.get("raw_http") or report.get("overall") or {}
    overall = report.get("overall") or raw_http
    actual_http_attempts = int(overall.get("requests") or 0)
    max_http_attempts = int(profile["portfolio"]["request_budget"]["max_http_attempts"])
    report["load_contract"]["runtime_http_budget"] = {
        "planned_worst_case": contract["planned_work"].get("http_attempts"),
        "max_http_attempts": max_http_attempts,
        "actual_http_attempts": actual_http_attempts,
        "within_budget": actual_http_attempts <= max_http_attempts,
    }
    if actual_http_attempts > max_http_attempts:
        report["acceptance"] = {
            "passed": False,
            "decision": "LOAD HTTP ATTEMPT BUDGET FAIL",
            "contract_ok": False,
            "error": (
                f"actual HTTP attempts {actual_http_attempts} exceed "
                f"max_http_attempts {max_http_attempts}"
            ),
        }
    logical = report.get("logical") or {}
    if logical:
        logical_timing = logical.get("timing") if isinstance(logical, Mapping) else None
        offered_actions = (
            int(logical_timing["expected_count"])
            if isinstance(logical_timing, Mapping)
            and type(logical_timing.get("expected_count")) is int
            else int(logical.get("actions") or 0)
        )
    else:
        offered_actions = int(raw_http.get("requests") or 0)
    report["load_contract"]["offered_logical_actions"] = offered_actions
    primary_phase = (report.get("phases") or {}).get("primary")
    primary_raw = (
        primary_phase.get("raw_http")
        if isinstance(primary_phase, Mapping)
        else None
    )
    report["load_contract"]["primary_http_attempts"] = int(
        (primary_raw or {}).get("requests") or raw_http.get("requests") or 0
    )
    report["load_contract"]["http_attempts"] = actual_http_attempts
    report["load_contract"]["total_http_attempts"] = actual_http_attempts
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
    # A runner report without origin evidence is deliberately non-green.  The
    # subsequent evaluate step is the only place that can attach the exact
    # observer binding and turn the report into an accepted result.
    return 0 if acceptance_result.get("passed") is True else 1


def evaluate_report(
    profile: Mapping[str, Any],
    report_path: Path,
    server_observability_path: Path | None,
) -> int:
    """Attach origin evidence and make the final profile-owned decision."""

    try:
        report = json.loads(
            report_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LoadProfileError("external load report is not valid JSON") from exc
    if not isinstance(report, dict):
        raise LoadProfileError("external load report must be an object")
    # Evaluation is an authoritative acceptance boundary, so it must apply
    # the same dispatchability policy as ``run`` before any historical report
    # can be upgraded to PASS.  Keep the evidence inspectable, but mark a
    # deprecated/lifecycle/wrong-environment report explicitly non-authoritative
    # and do not run its acceptance evaluator.
    try:
        ensure_dispatchable(profile)
    except LoadProfileError as exc:
        report["authoritative"] = False
        report["dispatchable"] = False
        report["acceptance"] = {
            "passed": False,
            "decision": "LOAD PROFILE NON-AUTHORITATIVE",
            "authoritative": False,
            "dispatchable": False,
            "contract_ok": False,
            "checks": {"dispatchable_profile": False},
            "error_class": safe_error_class(type(exc).__name__),
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "profile_id": profile.get("profile_id"),
                    "decision": "LOAD PROFILE NON-AUTHORITATIVE",
                    "passed": False,
                },
                ensure_ascii=False,
            )
        )
        return 1
    origin_observability: dict[str, Any] | None = None
    if server_observability_path is not None:
        try:
            candidate = json.loads(
                server_observability_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
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
            "binding": candidate.get("binding"),
        }
    try:
        from tools.platform_load_acceptance import (
            derive_expected_phase_plan,
            evaluate_acceptance,
        )
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_acceptance import derive_expected_phase_plan, evaluate_acceptance
    acceptance = profile["acceptance"]
    report_binding = _report_binding(profile, report)
    report["report_binding"] = report_binding
    if not report_binding["complete"]:
        report["acceptance"] = {
            "passed": False,
            "decision": "LOAD REPORT BINDING FAIL",
            "contract_ok": False,
            "checks": {"report_binding": False},
            "report_binding": report_binding,
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "profile_id": profile["profile_id"],
                    "decision": "LOAD REPORT BINDING FAIL",
                    "passed": False,
                },
                ensure_ascii=False,
            )
        )
        return 1
    report_phase_summaries: dict[str, Any] = {}
    report_phases = report.get("phases")
    if isinstance(report_phases, dict):
        ramp = report_phases.get("ramp")
        if isinstance(ramp, Mapping) and isinstance(ramp.get("phases"), dict):
            report_phase_summaries.update(ramp["phases"])
        if profile.get("mode") == "ready-vote":
            primary = report_phases.get("primary")
            if isinstance(primary, Mapping):
                report_phase_summaries["primary"] = primary
        duplicate = report_phases.get("duplicate")
        # Ready Vote reports always carry the duplicate phase, including its
        # configured zero-count form.  A missing phase must remain visible to
        # the exact-population evaluator instead of disappearing from the
        # acceptance input.
        if profile.get("mode") == "ready-vote":
            report_phase_summaries["duplicate"] = (
                duplicate if isinstance(duplicate, Mapping) else {}
            )
            state = report_phases.get("state")
            report_phase_summaries["state"] = (
                state if isinstance(state, Mapping) else {}
            )
        elif profile.get("mode") == "read-mix":
            capacity_ramp = report_phases.get("capacity_ramp")
            if isinstance(capacity_ramp, Mapping):
                report_phase_summaries["capacity_ramp"] = capacity_ramp
            for phase_name in ("read_mix", "manual_refresh"):
                phase = report_phases.get(phase_name)
                if isinstance(phase, Mapping):
                    # Read-mix summaries are one full HTTP population at each
                    # workload boundary; preserve both explicit envelopes so
                    # phase raw/logical checks cannot silently substitute a
                    # missing phase.
                    report_phase_summaries[phase_name] = {
                        "logical": phase,
                        "raw_http": phase,
                    }
        elif profile.get("mode") == "page-load":
            phase = report_phases.get("authenticated_page_load")
            if isinstance(phase, Mapping):
                report_phase_summaries["authenticated_page_load"] = {
                    "logical": phase,
                    "raw_http": phase,
                }
    elif profile.get("mode") == "ready-vote":
        report_phase_summaries["duplicate"] = {}
    report_acceptance = report.get("acceptance")
    report_acceptance = report_acceptance if isinstance(report_acceptance, Mapping) else {}
    # ``acceptance.contract_ok`` is the sole canonical producer assertion.
    # The retired top-level ``contract.ok`` field is rejected by the envelope
    # check and is never allowed to upgrade a missing/false flag.
    report_contract_ok = (
        report_acceptance.get("contract_ok")
        if isinstance(report_acceptance.get("contract_ok"), bool)
        else False
    )
    if report.get("partial_work") is True:
        report_contract_ok = False
    report["acceptance"] = evaluate_acceptance(
        contract_ok=report_contract_ok,
        logical_summary=(
            report.get("logical")
            if isinstance(report.get("logical"), Mapping)
            else {}
        ),
        raw_http_summary=(
            report.get("raw_http")
            if isinstance(report.get("raw_http"), Mapping)
            else {}
        ),
        acceptance_contract=acceptance,
        origin_observability=origin_observability,
        phase_summaries=report_phase_summaries or None,
        canonical_evidence=True,
        expected_logical_scope=(
            "logical_user_actions"
            if profile.get("mode") == "ready-vote"
            else "full_population"
        ),
        allowed_phase_names=set(report_phase_summaries),
        expected_stage_action_counts=(
            profile_contract(profile)["planned_work"].get("stage_logical_actions")
            if profile.get("mode") == "read-mix"
            and profile.get("traffic", {}).get("concurrency_stages") is not None
            else None
        ),
        expected_phase_action_counts=(
            profile_contract(profile)["planned_work"].get("phase_logical_actions")
            if profile.get("mode") in {"ready-vote", "read-mix", "page-load"}
            else None
        ),
        expected_fixture_marker=(
            report.get("fixture_marker")
            if isinstance(report.get("fixture_marker"), str)
            else None
        ),
        expected_external_run_id=(
            report.get("external_run_id")
            if isinstance(report.get("external_run_id"), str)
            else None
        ),
        require_exact_observer_binding=(
            profile.get("execution", {}).get("require_exact_observer_binding") is True
        ),
        expected_phase_plan=derive_expected_phase_plan(
            mode=profile.get("mode"),
            authored_phase_plan=profile.get("traffic", {}).get("phases") or None,
            expected_phase_action_counts=profile_contract(profile)["planned_work"].get(
                "phase_logical_actions"
            ),
        ),
        expected_duplicate_count=(
            int(profile.get("traffic", {}).get("duplicate_count") or 0)
            if profile.get("mode") == "ready-vote"
            else None
        ),
        expected_primary_action_count=(
            int(profile_contract(profile)["planned_work"]["primary_logical_actions"])
            if profile.get("mode") != "tournament-lifecycle"
            else None
        ),
        expected_total_logical_action_count=(
            int(profile_contract(profile)["planned_work"]["logical_actions"])
            if profile.get("mode") != "tournament-lifecycle"
            else None
        ),
        expected_state_read_count=(
            int(profile_contract(profile)["planned_work"]["state_read_requests"])
            if profile.get("mode") == "ready-vote"
            else None
        ),
        max_retries=(
            int(profile.get("traffic", {}).get("retry", {}).get("max_retries") or 0)
            if profile.get("mode") != "tournament-lifecycle"
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
    validate_parser.add_argument(
        "--dispatchable",
        action="store_true",
        help="also require an active profile that is safe to dispatch",
    )

    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("--profile", dest="profile_id", required=True)
    profile_parser.add_argument("--json", action="store_true")

    env_parser = subparsers.add_parser("export-env")
    env_parser.add_argument("--profile", dest="profile_id", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", dest="profile_id", required=True)
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--report-path", type=Path, required=True)
    run_parser.add_argument(
        "--timeout-diagnostics-run-id",
        help=(
            "Enable bounded per-request timeout-path evidence for an authenticated "
            "page-load run; value must be the numeric external workflow run id."
        ),
    )

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
            selected = get_profile(args.profile_id)
            if args.dispatchable:
                ensure_dispatchable(selected)
        elif args.dispatchable:
            raise LoadProfileError("--dispatchable requires --profile")
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
    return run_profile(
        profile,
        args.manifest,
        args.report_path,
        timeout_diagnostics_run_id=args.timeout_diagnostics_run_id,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except LoadProfileError as exc:
        print(
            f"LOAD PROFILE BLOCKED: {safe_error_class(type(exc).__name__)}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
