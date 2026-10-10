#!/usr/bin/env python3
"""Canonical load-profile registry and external load dispatcher.

The JSON profiles own scenario shape, retry behavior and acceptance budgets.
This module resolves a reviewed profile and delegates HTTP execution to the
existing external client.  It never runs the measured generator on the origin.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Iterator, Mapping
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Sequence

try:
    from platform_evidence_sanitizer import SAFE_ERROR_CLASSES, safe_error_class
except ModuleNotFoundError:  # Imported as ``tools.platform_load`` by tests.
    from tools.platform_evidence_sanitizer import SAFE_ERROR_CLASSES, safe_error_class


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
WORKFLOW_PENDING_ORIGIN_EXIT = 3
PENDING_ORIGIN_DECISIONS = frozenset(
    {
        "STRESS PENDING ORIGIN EVIDENCE",
        "SPIKE PENDING ORIGIN EVIDENCE",
        "CAPACITY PENDING ORIGIN EVIDENCE",
    }
)


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
    max_runner_minutes = _require_number(
        cost_budget.get("max_runner_minutes"),
        field="portfolio.cost_budget.max_runner_minutes",
        minimum=0.1,
        maximum=100_000,
    )
    if request_budget["max_duration_seconds"] >= max_runner_minutes * 60:
        raise LoadProfileError(
            "portfolio.max_duration_seconds must be strictly below the whole-runner budget"
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
        timed_phases: list[tuple[datetime, datetime]] = []
        timestamps_valid = True
        if phase_plan and isinstance(authored, Mapping):
            for name in expected_names:
                phase = authored.get(name)
                if not isinstance(phase, Mapping):
                    timestamps_valid = False
                    break
                started_at = phase.get("started_at")
                finished_at = phase.get("finished_at")
                if not isinstance(started_at, str) or not isinstance(finished_at, str):
                    timestamps_valid = False
                    break
                try:
                    started = datetime.fromisoformat(started_at)
                    finished = datetime.fromisoformat(finished_at)
                except ValueError:
                    timestamps_valid = False
                    break
                if (
                    started.tzinfo is None
                    or started.utcoffset() is None
                    or finished.tzinfo is None
                    or finished.utcoffset() is None
                    or started >= finished
                ):
                    timestamps_valid = False
                    break
                timed_phases.append((started, finished))
            if timestamps_valid:
                timestamps_valid = all(
                    previous_finished <= current_started
                    for (_, previous_finished), (current_started, _) in zip(
                        timed_phases, timed_phases[1:]
                    )
                )
        phase_plan_closed = (
            (not phase_plan and "ramp" not in phases)
            or (
                bool(phase_plan)
                and isinstance(phases.get("ramp"), Mapping)
                and isinstance(authored, Mapping)
                and set(authored_names) == set(expected_names)
                and len(authored_names) == len(expected_names)
                and timestamps_valid
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
            ramp = phases.get("capacity_ramp")
            ramp = ramp if isinstance(ramp, Mapping) else {}
            ramp_stages = ramp.get("stages")
            authored_stages = traffic.get("concurrency_stages") or []
            expected_stages = {str(stage) for stage in authored_stages}
            phase_values_mapping = phase_values_mapping and isinstance(
                ramp_stages, Mapping
            ) and set(str(stage) for stage in ramp_stages) == expected_stages and all(
                isinstance(value, Mapping) for value in ramp_stages.values()
            )
            actual_stages = ramp.get("concurrency_stages")
            phase_plan_closed = (
                isinstance(authored_stages, list)
                and isinstance(actual_stages, list)
                and all(
                    isinstance(stage, int) and not isinstance(stage, bool)
                    for stage in actual_stages
                )
                and actual_stages == authored_stages
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
    try:
        expected_app_target, expected_source_binding, expected_binding_digest = (
            _source_binding_identity()
        )
        app_identity_valid = True
    except LoadProfileError:
        expected_app_target = ""
        expected_source_binding = None
        expected_binding_digest = None
        app_identity_valid = False
    actual_app_target = report.get("app_target_sha")
    if actual_app_target is None and expected_source_binding is None:
        # Preserve the historical same-source report path: with no receipt,
        # the checked-out runner SHA itself is the only permitted app target.
        actual_app_target = actual_source
    app_source_ok = (
        app_identity_valid
        and isinstance(actual_app_target, str)
        and SOURCE_SHA_RE.fullmatch(actual_app_target) is not None
        and actual_app_target == expected_app_target
    )
    source_binding_ok = (
        report.get("source_binding") == expected_source_binding
        and (
            report.get("source_binding") is None
            or isinstance(report.get("source_binding"), Mapping)
        )
    )
    binding_digest_ok = report.get("source_binding_sha256") == expected_binding_digest

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
        "app_target_sha": app_source_ok,
        "source_binding": source_binding_ok,
        "source_binding_sha256": binding_digest_ok,
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
            "app_target_sha": expected_app_target or None,
            "source_binding_sha256": expected_binding_digest,
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
            "app_target_sha": actual_app_target,
            "source_binding_sha256": report.get("source_binding_sha256"),
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
    runtime_budget: Mapping[str, Any] | None = None,
    partial_work: bool = False,
    inflight_unknown: bool = False,
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
        "partial_work": bool(partial_work),
        "inflight_unknown": bool(inflight_unknown),
    }
    if runtime_budget is not None:
        payload["runtime_budget"] = dict(runtime_budget)
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


def _source_binding_identity() -> tuple[str, dict[str, Any] | None, str | None]:
    """Validate the workflow-provided runner/app source handoff.

    The resolver validates the API artifact and receipt before creating the
    private input. This runner-side check binds the same canonical bytes to
    the fixture and report; it never accepts an app target from a user input.
    """

    runner_sha = _source_git_sha()
    app_target_sha = os.environ.get("APP_TARGET_SHA", "").strip() or runner_sha
    if SOURCE_SHA_RE.fullmatch(app_target_sha) is None:
        raise LoadProfileError("APP_TARGET_SHA must be a lowercase 40-character SHA")
    encoded = os.environ.get("SOURCE_BINDING_BASE64", "")
    expected_digest = os.environ.get("SOURCE_BINDING_SHA256", "")
    if not encoded:
        if app_target_sha != runner_sha or expected_digest:
            raise LoadProfileError("mismatched app source lacks a verified source binding")
        return app_target_sha, None, None
    if not expected_digest or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise LoadProfileError("SOURCE_BINDING_SHA256 is malformed")
    try:
        try:
            from tools.platform_noop_source_binding import (
                parse_source_binding_argument,
                validate_active_runtime_tuple,
            )
        except ModuleNotFoundError:  # Direct execution from platform/tools.
            from platform_noop_source_binding import (
                parse_source_binding_argument,
                validate_active_runtime_tuple,
            )
        binding = parse_source_binding_argument(encoded)
        canonical = json.dumps(
            binding,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if (
            base64.b64encode(canonical).decode("ascii") != encoded
            or hashlib.sha256(canonical).hexdigest() != expected_digest
            or binding.get("runner_sha") != runner_sha
            or binding.get("app_target_sha") != app_target_sha
        ):
            raise ValueError("source binding does not match the workflow identities")
        validate_active_runtime_tuple(
            binding,
            binding.get("baseline_identity"),
            expected_runner_sha=runner_sha,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise LoadProfileError("SOURCE_BINDING_BASE64 failed closed validation") from exc
    return app_target_sha, binding, expected_digest


def _external_run_id() -> str:
    """Return the explicit GitHub workflow run identity."""

    candidate = os.environ.get("GITHUB_RUN_ID", "").strip()
    if RUN_ID_RE.fullmatch(candidate) is None:
        raise LoadProfileError(
            "GITHUB_RUN_ID is required and must be a numeric workflow run ID"
        )
    return candidate


def run_profile_worker(
    profile: Mapping[str, Any],
    manifest_path: Path,
    report_path: Path,
    *,
    timeout_diagnostics_run_id: str | None = None,
    runtime_config: Mapping[str, Any] | None = None,
) -> int:
    """Execute one load scenario inside the killable worker process.

    This function deliberately does not create a process or publish a final
    report.  ``run_profile`` is the parent-side entry point; the worker CLI
    invokes this function with a private child-report path.
    """
    ensure_dispatchable(profile)
    # Imported lazily so profile listing and contract validation remain free of
    # application/runtime imports.  The module is the external runner client;
    # this process is expected to run on the GitHub-hosted load runner.
    try:
        from tools.platform_external_load import ExternalLoadError, load_manifest, run_load
        from tools.platform_load_runtime import (
            LoadRuntimeBudget,
            LoadRuntimeBudgetExceeded,
            ProgressCheckpoint,
        )
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_external_load import ExternalLoadError, load_manifest, run_load
        from platform_load_runtime import (  # type: ignore[no-redef]
            LoadRuntimeBudget,
            LoadRuntimeBudgetExceeded,
            ProgressCheckpoint,
        )

    contract = profile_contract(profile)
    try:
        source_git_sha = _source_git_sha()
        external_run_id = _external_run_id()
        app_target_sha, source_binding, source_binding_sha256 = (
            _source_binding_identity()
        )
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
    progress_checkpoint = None
    if runtime_config is not None:
        progress_path_raw = runtime_config.get("progress_checkpoint_path")
        progress_context_raw = runtime_config.get("progress_context")
        try:
            planned = contract["planned_work"]
            request_limits = profile["portfolio"]["request_budget"]
            expected_progress_context = {
                "source_git_sha": source_git_sha,
                "app_target_sha": app_target_sha,
                "source_binding_sha256": source_binding_sha256,
                "profile_id": str(contract["profile_id"]),
                "profile_digest": str(contract["profile_digest"]),
                "external_run_id": external_run_id,
                "max_logical_actions": int(planned["logical_actions"] or 0),
                "max_http_attempts": int(request_limits["max_http_attempts"]),
            }
            if (
                isinstance(progress_path_raw, str)
                and isinstance(progress_context_raw, Mapping)
                and dict(progress_context_raw) == expected_progress_context
            ):
                progress_checkpoint = ProgressCheckpoint(
                    Path(progress_path_raw),
                    progress_context_raw,
                )
                progress_checkpoint.emit("fixture_validation", inflight_unknown=False)
        except (OSError, TypeError, ValueError):
            progress_checkpoint = None

    def record_progress(phase: str, result: Any | None) -> None:
        nonlocal progress_checkpoint
        checkpoint = progress_checkpoint
        if checkpoint is None:
            return
        try:
            if result is None:
                checkpoint.emit(phase, inflight_unknown=(phase == "http"))
                return
            attempts = getattr(result, "attempts", None)
            if isinstance(attempts, list):
                statuses = [getattr(attempt, "status", None) for attempt in attempts]
            else:
                statuses = [getattr(result, "status", None)]
            if any(type(status) is not int for status in statuses):
                return
            checkpoint.record_http_result(statuses, logical_delta=1)
        except (OSError, TypeError, ValueError):
            # The progress channel is never allowed to change load execution.
            progress_checkpoint = None

    try:
        manifest, users = load_manifest(manifest_path)
        manifest_app_target = manifest.get("app_target_sha", source_git_sha)
        manifest_binding = manifest.get("source_binding")
        manifest_binding_digest = manifest.get("source_binding_sha256")
        if (
            manifest_app_target != app_target_sha
            or manifest_binding != source_binding
            or manifest_binding_digest != source_binding_sha256
        ):
            raise ExternalLoadError("fixture manifest source binding differs from workflow")
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
    runtime_budget = None
    if runtime_config is not None:
        runtime_budget = LoadRuntimeBudget(
            float(runtime_config["scenario_deadline_monotonic"])
            - float(runtime_config["started_at_monotonic"]),
            max_runner_minutes=(
                float(runtime_config["runner_deadline_monotonic"])
                - float(runtime_config["started_at_monotonic"])
            )
            / 60.0,
            started_at_monotonic=float(runtime_config["started_at_monotonic"]),
            runner_started_at_monotonic=float(runtime_config["started_at_monotonic"]),
        )
    try:
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
                "app_target_sha": app_target_sha,
                "source_binding": source_binding,
                "source_binding_sha256": source_binding_sha256,
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
            runtime_budget=runtime_budget,
            progress_callback=(record_progress if progress_checkpoint is not None else None),
        )
        if progress_checkpoint is not None:
            try:
                # Returning from run_load means all measured futures drained.
                # A timeout or worker kill skips this line and stays unknown.
                progress_checkpoint.emit("teardown", inflight_unknown=False)
            except (OSError, TypeError, ValueError):
                progress_checkpoint = None
    except LoadRuntimeBudgetExceeded as exc:
        status = runtime_budget.runner_budget_status(
            phase=exc.phase,
            reason=exc.reason,
        ) if runtime_budget is not None else None
        _write_failed_report(
            profile,
            contract,
            report_path,
            exc,
            decision="LOAD RUNTIME BUDGET EXCEEDED",
            runtime_budget=status,
            partial_work=True,
            inflight_unknown=True,
        )
        return 1
    if report is None:
        return 1
    report["source_git_sha"] = source_git_sha
    report["app_target_sha"] = app_target_sha
    report["source_binding"] = source_binding
    report["source_binding_sha256"] = source_binding_sha256
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
    if runtime_budget is not None:
        report["runtime_budget"] = runtime_budget.runner_budget_status()
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


def run_profile(
    profile: Mapping[str, Any],
    manifest_path: Path,
    report_path: Path,
    *,
    timeout_diagnostics_run_id: str | None = None,
    defer_pending_origin: bool = False,
) -> int:
    """Run the external generator behind the mandatory PID-namespace guard.

    The parent owns both absolute budgets and never executes measured HTTP
    work.  A valid worker report is copied atomically only after the child has
    exited and the namespace is closed; a killed, malformed or missing child
    report becomes a closed failed report so the independent fixture finalizer
    can still run. A rejected capability preflight returns a pure exit/error
    channel before it touches the caller's report path.
    """

    # This is intentionally the first side-effect boundary in the run path.
    # A root/local runner must receive a pure stderr/exit failure; it must not
    # create report directories, remove stale reports, or create the worker
    # TemporaryDirectory before the mandatory non-root namespace probe passes.
    try:
        from tools.platform_load_runtime import (
            NamespaceCapabilityError,
            ProgressCheckpoint,
            require_pid_namespace_capability,
            write_closed_progress_artifact,
        )
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_runtime import (  # type: ignore[no-redef]
            NamespaceCapabilityError,
            ProgressCheckpoint,
            require_pid_namespace_capability,
            write_closed_progress_artifact,
        )
    try:
        require_pid_namespace_capability()
    except NamespaceCapabilityError as exc:
        print(
            json.dumps(
                {
                    "decision": "LOAD ISOLATION UNAVAILABLE",
                    "passed": False,
                    "error_class": safe_error_class(type(exc).__name__),
                    "reason": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    ensure_dispatchable(profile)
    contract = profile_contract(profile)
    try:
        source_git_sha = _source_git_sha()
        external_run_id = _external_run_id()
        app_target_sha, source_binding, source_binding_sha256 = (
            _source_binding_identity()
        )
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

    request_budget = profile["portfolio"]["request_budget"]
    cost_budget = profile["portfolio"]["cost_budget"]
    planned_work = contract["planned_work"]
    progress_context = {
        "source_git_sha": source_git_sha,
        "app_target_sha": app_target_sha,
        "source_binding_sha256": source_binding_sha256,
        "profile_id": str(contract["profile_id"]),
        "profile_digest": str(contract["profile_digest"]),
        "external_run_id": external_run_id,
        "max_logical_actions": int(planned_work["logical_actions"] or 0),
        "max_http_attempts": int(request_budget["max_http_attempts"]),
    }
    max_duration_seconds = float(request_budget["max_duration_seconds"])
    max_runner_minutes = float(cost_budget["max_runner_minutes"])
    try:
        from tools.platform_load_runtime import run_supervised
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_runtime import run_supervised

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{report_path.name}.worker-",
        dir=report_path.parent,
    ) as worker_directory:
        worker_report_path = Path(worker_directory) / "child-report.json"
        progress_path = Path(worker_directory) / "progress.json"
        worker_config = {
            "profile_id": str(profile["profile_id"]),
            "manifest_path": str(manifest_path),
            "timeout_diagnostics_run_id": timeout_diagnostics_run_id,
            "binding": {
                "source_git_sha": source_git_sha,
                "app_target_sha": app_target_sha,
                "source_binding": source_binding,
                "source_binding_sha256": source_binding_sha256,
                "external_run_id": external_run_id,
            },
            "progress_checkpoint_path": str(progress_path),
            "progress_context": progress_context,
        }
        result = run_supervised(
            worker_command=(
                sys.executable,
                str(Path(__file__).with_name("platform_load_worker.py")),
            ),
            report_path=report_path,
            worker_report_path=worker_report_path,
            max_duration_seconds=max_duration_seconds,
            max_runner_minutes=max_runner_minutes,
            worker_config=worker_config,
        )
        progress_snapshot = None
        if result.namespace_closed is True and result.descendants_reaped is True:
            try:
                progress_snapshot = ProgressCheckpoint(
                    progress_path,
                    progress_context,
                ).closed_snapshot(
                    namespace_closed=True,
                    descendants_reaped=True,
                    partial_work=bool(result.partial_work),
                )
            except (OSError, TypeError, ValueError):
                # Progress is diagnostic-only and cannot change the primary
                # load result or its acceptance decision.
                progress_snapshot = None

    if progress_snapshot is not None:
        progress_artifact_path = report_path.with_name(
            f"{report_path.stem}.progress.json"
        )
        try:
            write_closed_progress_artifact(progress_artifact_path, progress_snapshot)
        except (FileExistsError, OSError, TypeError, ValueError):
            # Preserve the primary report and never overwrite a prior or
            # replaced artifact. Missing progress remains explicitly unknown.
            pass

    payload = result.report
    decision = (
        payload.get("acceptance", {}).get("decision")
        if isinstance(payload.get("acceptance"), Mapping)
        else None
    )
    print(
        json.dumps(
            {
                "profile_id": profile.get("profile_id"),
                "decision": decision or result.reason,
                "passed": payload.get("acceptance", {}).get("passed") is True
                if isinstance(payload.get("acceptance"), Mapping)
                else False,
                "partial_work": result.partial_work,
                "inflight_unknown": result.inflight_unknown,
                "signal": result.signal,
            },
            ensure_ascii=False,
        )
    )
    if defer_pending_origin and _is_closed_pending_origin_candidate(profile, result):
        # This reserved code is an orchestration receipt, not acceptance.
        # Direct callers retain the normal nonzero result for pending origin.
        return WORKFLOW_PENDING_ORIGIN_EXIT
    return (
        0
        if isinstance(payload.get("acceptance"), Mapping)
        and payload["acceptance"].get("passed") is True
        and result.returncode == 0
        else 1
    )


def _is_closed_pending_origin_candidate(
    profile: Mapping[str, Any], result: Any
) -> bool:
    """Recognize only the fully reaped worker's exact deferred-origin result.

    The external workflow may treat this as a completed candidate producer so
    its independently bound origin observer can run.  It must never turn an
    arbitrary child failure, partial report, or standalone pending result into
    acceptance.
    """

    if (
        getattr(result, "returncode", None) != 1
        or getattr(result, "reason", None) != "none"
        or getattr(result, "worker_started", None) is not True
        or getattr(result, "worker_exited", None) is not True
        or getattr(result, "killed", None) is not False
        or getattr(result, "partial_work", None) is not False
        or getattr(result, "inflight_unknown", None) is not False
        or getattr(result, "descendants_reaped", None) is not True
        or getattr(result, "namespace_closed", None) is not True
    ):
        return False
    report = getattr(result, "report", None)
    if not isinstance(report, Mapping):
        return False
    try:
        from tools.platform_load_runtime import (
            PID_NAMESPACE_ISOLATION,
            WORKER_REPORT_SCHEMA,
        )
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_runtime import (  # type: ignore[no-redef]
            PID_NAMESPACE_ISOLATION,
            WORKER_REPORT_SCHEMA,
        )
    acceptance = report.get("acceptance")
    supervisor = report.get("runtime_supervisor")
    if not isinstance(acceptance, Mapping) or not isinstance(supervisor, Mapping):
        return False
    if (
        report.get("worker_report_schema") != WORKER_REPORT_SCHEMA
        or report.get("report_complete") is not True
        or type(report.get("worker_exit_code")) is not int
        or report.get("worker_exit_code") != 1
        or report.get("namespace_closed") is not True
        or report.get("authoritative") is not True
        or report.get("dispatchable") is not True
        or report.get("isolation") != PID_NAMESPACE_ISOLATION
        or report.get("partial_work") is not False
        or report.get("inflight_unknown") is not False
        or getattr(result, "isolation", None) != PID_NAMESPACE_ISOLATION
        or supervisor.get("reason") != "none"
        or type(supervisor.get("returncode")) is not int
        or supervisor.get("returncode") != 1
        or supervisor.get("isolation") != PID_NAMESPACE_ISOLATION
        or supervisor.get("namespace_closed") is not True
        or supervisor.get("descendants_reaped") is not True
        or supervisor.get("partial_work") is not False
        or supervisor.get("inflight_unknown") is not False
        or supervisor.get("report_error") is not None
        or acceptance.get("passed") is not False
        or acceptance.get("pending_origin_evidence") is not True
    ):
        return False
    terminal_status_failure = _closed_terminal_status_failure(profile, report)
    if acceptance.get("contract_ok") is not True and not terminal_status_failure:
        return False
    profile_acceptance = profile.get("acceptance")
    profile_kind = (
        profile_acceptance.get("kind")
        if isinstance(profile_acceptance, Mapping)
        else None
    )
    expected_decision = {
        "slo": "SLO FAIL",
        "stress": "STRESS PENDING ORIGIN EVIDENCE",
        "spike": "SPIKE PENDING ORIGIN EVIDENCE",
        "capacity": "CAPACITY PENDING ORIGIN EVIDENCE",
    }.get(str(profile_kind))
    if expected_decision is None or acceptance.get("decision") != expected_decision:
        return False
    if _report_binding(profile, report).get("complete") is not True:
        return False
    observer_binding = acceptance.get("observer_binding")
    if (
        "origin_observability" in report
        or acceptance.get("origin_safety") is not None
        or not _closed_missing_observer_binding(observer_binding)
    ):
        return False
    if profile_kind == "capacity":
        closed_terminal_ramp_stages = (
            _closed_terminal_ramp_stage_names(profile, report)
            if terminal_status_failure
            else frozenset()
        )
        return _closed_capacity_pending_origin_acceptance(
            profile,
            acceptance,
            allow_closed_terminal_status_failure=terminal_status_failure,
            closed_terminal_ramp_stages=closed_terminal_ramp_stages,
        )
    closed_terminal_ramp_stages = (
        _closed_terminal_ramp_stage_names(profile, report)
        if terminal_status_failure
        else frozenset()
    )
    allowed_ramp_budget_checks = _authored_ramp_budget_checks(
        profile,
        acceptance,
        allow_closed_terminal_status_failure=terminal_status_failure,
        closed_terminal_ramp_stages=closed_terminal_ramp_stages,
    )
    if allowed_ramp_budget_checks is None:
        return False
    return _acceptance_failure_is_budget_only(
        acceptance,
        str(profile_kind),
        allowed_ramp_budget_checks=allowed_ramp_budget_checks,
        allow_no_budget_failure=True,
        allow_pending_observer_binding=True,
        allowed_phase_names=_profile_budget_phase_names(profile),
        allow_closed_terminal_status_failure=terminal_status_failure,
        closed_terminal_ramp_stages=closed_terminal_ramp_stages,
    )


_CLOSED_TERMINAL_STATUSES = frozenset({0, 500})
_STATUS_ZERO_ERROR_CLASSES = frozenset(
    {"http_error", "other", "timeout", "transport"}
)


def _iter_status_summaries(
    value: Any,
) -> Iterator[tuple[tuple[str, ...], str, Mapping[str, Any]]]:
    """Yield each raw or logical summary in an untrusted report tree."""

    seen: set[int] = set()

    def visit(
        node: Any, path: tuple[str, ...] = ()
    ) -> Iterator[tuple[tuple[str, ...], str, Mapping[str, Any]]]:
        if not isinstance(node, Mapping) or id(node) in seen:
            return
        seen.add(id(node))
        if "status_counts" in node:
            yield path, "raw", node
        if "final_status_counts" in node:
            yield path, "logical", node
        for key, child in node.items():
            if isinstance(child, (Mapping, list, tuple)):
                yield from visit(child, path + (str(key),))

    yield from visit(value)


def _iter_metric_summaries(
    value: Any,
) -> Iterator[tuple[tuple[str, ...], Mapping[str, Any]]]:
    """Yield report summary nodes whose timing and population are explicit."""

    seen: set[int] = set()

    def visit(
        node: Any, path: tuple[str, ...] = ()
    ) -> Iterator[tuple[tuple[str, ...], Mapping[str, Any]]]:
        if not isinstance(node, Mapping) or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node.get("timing"), Mapping) and (
            "requests" in node or "actions" in node
        ):
            yield path, node
        for key, child in node.items():
            if isinstance(child, (Mapping, list, tuple)):
                yield from visit(child, path + (str(key),))

    yield from visit(value)


def _closed_terminal_ramp_stage_names(
    profile: Mapping[str, Any], report: Mapping[str, Any]
) -> frozenset[str]:
    """Return ramp stages with validated status-0/500 evidence in a closed report."""

    acceptance = profile.get("acceptance")
    expected = acceptance.get("expected_statuses") if isinstance(acceptance, Mapping) else None
    if (
        not isinstance(expected, (list, tuple))
        or not expected
        or any(type(status) is not int for status in expected)
    ):
        return frozenset()
    allowed_statuses = frozenset(expected) | _CLOSED_TERMINAL_STATUSES
    stages: set[str] = set()
    for path, kind, summary in _iter_status_summaries(report):
        if "capacity_ramp" not in path or "stages" not in path:
            continue
        stage_index = path.index("stages") + 1
        if stage_index >= len(path):
            continue
        field = "status_counts" if kind == "raw" else "final_status_counts"
        counts = _closed_status_distribution(
            summary,
            field=field,
            allowed_statuses=allowed_statuses,
        )
        if counts is not None and any(
            count and status not in expected for status, count in counts.items()
        ):
            stages.add(path[stage_index])
    return frozenset(stages)


def _closed_timing_evidence_tree(value: Any) -> bool:
    """Reject partial, malformed or inconsistent timing evidence anywhere."""

    try:
        from tools.platform_load_acceptance import timing_summary_is_complete
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_acceptance import timing_summary_is_complete  # type: ignore[no-redef]

    seen: set[int] = set()

    def visit(node: Any) -> bool:
        if isinstance(node, (list, tuple)):
            return all(visit(child) for child in node)
        if not isinstance(node, Mapping) or id(node) in seen:
            return True
        seen.add(id(node))
        if "partial" in node and node.get("partial") is not False:
            return False
        if "inflight_unknown" in node and node.get("inflight_unknown") is not False:
            return False
        if "timing_schema" in node and "partial" in node:
            if not timing_summary_is_complete(node, require_metrics=True):
                return False
        return all(
            visit(child)
            for child in node.values()
            if isinstance(child, (Mapping, list, tuple))
        )

    return visit(value)


def _closed_status_distribution(
    summary: Mapping[str, Any],
    *,
    field: str,
    allowed_statuses: frozenset[int],
) -> dict[int, int] | None:
    distribution = summary.get(field)
    if not isinstance(distribution, Mapping):
        return None
    result: dict[int, int] = {}
    for raw_status, raw_count in distribution.items():
        if not isinstance(raw_status, str) or not raw_status.isdigit():
            return None
        status = int(raw_status)
        if str(status) != raw_status or status not in allowed_statuses:
            return None
        count = _strict_nonnegative_int(raw_count)
        if count is None:
            return None
        result[status] = count
    return result


def _closed_raw_error_classes(
    summary: Mapping[str, Any],
    status_counts: Mapping[int, int],
) -> bool:
    error_count = _strict_nonnegative_int(summary.get("errors"))
    error_kinds = summary.get("error_kinds")
    if error_count is None or not isinstance(error_kinds, Mapping):
        return False
    typed_kinds: dict[str, int] = {}
    for name, raw_count in error_kinds.items():
        if not isinstance(name, str) or name not in SAFE_ERROR_CLASSES:
            return False
        count = _strict_nonnegative_int(raw_count)
        if count is None:
            return False
        typed_kinds[name] = count
    if sum(typed_kinds.values()) != error_count:
        return False

    # Every nonzero HTTP error has a deterministic sanitizer class. Status 0
    # has no HTTP response, so only the client's finite transport/error
    # classes may account for the residual count.
    expected_by_status: dict[str, int] = {}
    zero_status_count = status_counts.get(0, 0)
    for status, count in status_counts.items():
        if status == 0 or 200 <= status < 400:
            continue
        error_class = safe_error_class("", status=status)
        expected_by_status[error_class] = expected_by_status.get(error_class, 0) + count
    residual: dict[str, int] = {}
    for error_class in SAFE_ERROR_CLASSES:
        actual = typed_kinds.get(error_class, 0)
        expected = expected_by_status.get(error_class, 0)
        if actual < expected:
            return False
        if actual > expected:
            residual[error_class] = actual - expected
    return (
        sum(residual.values()) == zero_status_count
        and set(residual).issubset(_STATUS_ZERO_ERROR_CLASSES)
    )


def _closed_terminal_status_failure(
    profile: Mapping[str, Any], report: Mapping[str, Any]
) -> bool:
    """Validate a closed report whose only non-profile statuses are 0/500.

    This evidence never changes acceptance. It only permits the workflow's
    reserved completed-candidate handoff when the ordinary evaluator has
    already failed. Every raw and logical population must still reconcile.
    """

    acceptance_contract = profile.get("acceptance")
    report_acceptance = report.get("acceptance")
    if (
        not isinstance(acceptance_contract, Mapping)
        or not isinstance(report_acceptance, Mapping)
        or report_acceptance.get("contract_ok") is not False
        or report_acceptance.get("passed") is not False
    ):
        return False
    raw_expected = acceptance_contract.get("expected_statuses")
    if (
        not isinstance(raw_expected, (list, tuple))
        or not raw_expected
        or any(type(status) is not int or status < 100 or status > 599 for status in raw_expected)
        or len(set(raw_expected)) != len(raw_expected)
    ):
        return False
    expected_statuses = frozenset(raw_expected)
    # Current profile contracts model successful HTTP outcomes plus explicitly
    # recognized 503 overloads. A novel expected error status would need its
    # own typed contract before it can enter this reserved path.
    if any(not (200 <= status < 400 or status == 503) for status in expected_statuses):
        return False
    allowed_statuses = expected_statuses | _CLOSED_TERMINAL_STATUSES
    if not _closed_timing_evidence_tree(report):
        return False
    raw_http_summary = report.get("raw_http")
    overall_summary = report.get("overall")
    logical_summary = report.get("logical")
    if not all(
        isinstance(summary, Mapping)
        for summary in (raw_http_summary, overall_summary, logical_summary)
    ):
        return False
    if (
        "status_counts" not in raw_http_summary
        or "status_counts" not in overall_summary
        or (
            "final_status_counts" not in logical_summary
            if profile.get("mode") == "ready-vote"
            else "status_counts" not in logical_summary
        )
    ):
        return False
    try:
        from tools.platform_load_acceptance import (
            _outcome_consistency,
            normalize_evidence,
            timing_summary_is_complete,
        )
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_acceptance import (  # type: ignore[no-redef]
            _outcome_consistency,
            normalize_evidence,
            timing_summary_is_complete,
        )

    metric_summaries = list(_iter_metric_summaries(report))
    if not metric_summaries:
        return False
    for _path, summary in metric_summaries:
        timing = summary.get("timing")
        if (
            not isinstance(timing, Mapping)
            or "timing_schema" not in timing
            or "partial" not in timing
            or not timing_summary_is_complete(timing, require_metrics=True)
        ):
            return False
        if "requests" in summary and "status_counts" not in summary:
            return False
        if "actions" in summary and "final_status_counts" not in summary:
            return False
    raw_summaries = 0
    logical_summaries = 0
    terminal_status_seen = False
    top_status_incomplete: set[str] = set()
    phase_status_incomplete: set[tuple[str, str]] = set()
    ramp_status_incomplete: set[tuple[str, str]] = set()
    phase_shared_scope_names: set[str] = set()
    ramp_shared_scope_names: set[str] = set()
    ramp_stage_normalized: dict[str, Mapping[str, Any]] = {}
    ramp_stage_populations: dict[str, tuple[int, Mapping[str, Any]]] = {}
    top_raw_terminal_status_seen = False
    for path, kind, summary in _iter_status_summaries(report):
        status_field = "status_counts" if kind == "raw" else "final_status_counts"
        counts = _closed_status_distribution(
            summary,
            field=status_field,
            allowed_statuses=allowed_statuses,
        )
        if counts is None:
            return False
        unexpected_status_counts = {
            status: count
            for status, count in counts.items()
            if count and status not in expected_statuses
        }
        if unexpected_status_counts:
            if any(
                status not in _CLOSED_TERMINAL_STATUSES
                for status in unexpected_status_counts
            ):
                return False
            terminal_status_seen = True
            if path == ("raw_http",) and kind == "raw":
                top_raw_terminal_status_seen = True
        population_field = "requests" if kind == "raw" else "actions"
        success_field = "successful_responses" if kind == "raw" else "final_successes"
        failure_field = "errors" if kind == "raw" else "final_failures"
        population = _strict_nonnegative_int(summary.get(population_field))
        successes = _strict_nonnegative_int(summary.get(success_field))
        failures = _strict_nonnegative_int(summary.get(failure_field))
        status_successes = sum(
            count for status, count in counts.items() if 200 <= status < 400
        )
        status_failures = sum(
            count for status, count in counts.items() if not 200 <= status < 400
        )
        if (
            population is None
            or successes is None
            or failures is None
            or sum(counts.values()) != population
            or successes != status_successes
            or failures != status_failures
        ):
            return False
        if path and "capacity_ramp" in path and "stages" in path:
            stage_index = path.index("stages") + 1
            if stage_index >= len(path):
                return False
            ramp_stage_populations[path[stage_index]] = (population, summary)
        # The evaluator combines outcome consistency with the expected-status
        # check for canonical evidence. Recompute outcomes and rounded failure
        # rates independently so a false combined leaf is status-derived only.
        if _outcome_consistency(summary, required=True).get("complete") is not True:
            return False
        if kind == "raw":
            raw_summaries += 1
            temporary_overloads = _strict_nonnegative_int(
                summary.get("temporary_overload_responses")
            )
            unexpected = _strict_nonnegative_int(summary.get("unexpected_statuses"))
            recognized_overloads = counts.get(503, 0)
            if (
                temporary_overloads != recognized_overloads
                or unexpected != failures - recognized_overloads
                or unexpected
                != sum(
                    count
                    for status, count in counts.items()
                    if status not in expected_statuses
                )
                or not _closed_raw_error_classes(summary, counts)
            ):
                return False
        else:
            logical_summaries += 1
        expected_scope = (
            "logical_user_actions" if kind == "logical" else "full_population"
        )
        normalized = normalize_evidence(
            summary,
            expected_scope=expected_scope,
            required=True,
            canonical=True,
            # State reads are diagnostic, not an action-goodput population.
            # This matches the canonical ready-vote state evidence check.
            require_goodput=path != ("phases", "state"),
            label="terminal_status",
            expected_statuses=expected_statuses,
        )
        normalized_checks = normalized.get("checks")
        if (
            not isinstance(normalized_checks, Mapping)
            or any(type(value) is not bool for value in normalized_checks.values())
        ):
            return False
        failed_normalized_checks = {
            name for name, value in normalized_checks.items() if value is False
        }
        status_check = (
            "final_status_counts_matches_expected_statuses"
            if kind == "logical"
            else "status_counts_matches_expected_statuses"
        )
        allowed_normalization_failures = {status_check}
        if kind == "raw":
            allowed_normalization_failures.add("raw_unexpected_statuses_zero")
        if not failed_normalized_checks.issubset(allowed_normalization_failures):
            return False
        if bool(failed_normalized_checks) != any(
            count and status not in expected_statuses
            for status, count in counts.items()
        ):
            return False
        normalized_scope = (
            "raw"
            if path and path[-1] == "raw_http"
            else "logical"
            if path and path[-1] == "logical"
            else "logical"
            if kind == "logical"
            else "raw"
        )
        if failed_normalized_checks:
            if path and path[0] == "phases":
                if "capacity_ramp" in path and "stages" in path:
                    stage_index = path.index("stages") + 1
                    if stage_index >= len(path):
                        return False
                    stage_name = path[stage_index]
                    ramp_status_incomplete.add((stage_name, normalized_scope))
                    ramp_stage_normalized[stage_name] = normalized
                    if (
                        profile.get("mode") != "ready-vote"
                        and kind == "raw"
                        and len(path) == 4
                        and path[1:3] == ("capacity_ramp", "stages")
                        and summary.get("scope") == "full_population"
                    ):
                        report_phases = report.get("phases")
                        ramp_phase = (
                            report_phases.get("capacity_ramp")
                            if isinstance(report_phases, Mapping)
                            else None
                        )
                        ramp_stages = (
                            ramp_phase.get("stages")
                            if isinstance(ramp_phase, Mapping)
                            else None
                        )
                        if (
                            isinstance(ramp_stages, Mapping)
                            and ramp_stages.get(stage_name) is summary
                            and "logical" not in summary
                            and "raw_http" not in summary
                        ):
                            ramp_shared_scope_names.add(stage_name)
                else:
                    phase_name = next(
                        (part for part in path if part in _profile_budget_phase_names(profile)),
                        None,
                    )
                    if phase_name is None:
                        return False
                    phase_status_incomplete.add((phase_name, normalized_scope))
                    if (
                        profile.get("mode") != "ready-vote"
                        and kind == "raw"
                        and len(path) == 2
                        and summary.get("scope") == "full_population"
                    ):
                        report_phases = report.get("phases")
                        if (
                            isinstance(report_phases, Mapping)
                            and report_phases.get(phase_name) is summary
                            and "logical" not in summary
                            and "raw_http" not in summary
                        ):
                            phase_shared_scope_names.add(phase_name)
            elif path and path[0] in {"logical", "raw_http", "overall"}:
                top_status_incomplete.add(path[0])

    if ramp_status_incomplete:
        ramp_acceptance = report_acceptance.get("capacity_ramp_evidence")
        ramp_stages = (
            ramp_acceptance.get("stages")
            if isinstance(ramp_acceptance, Mapping)
            else None
        )
        expected_stage_counts = profile_contract(profile).get("planned_work", {}).get(
            "stage_logical_actions"
        )
        if not isinstance(ramp_stages, Mapping) or not isinstance(
            expected_stage_counts, Mapping
        ):
            return False
        for stage_name, _scope in ramp_status_incomplete:
            normalized = ramp_stage_normalized.get(stage_name)
            stage_evidence = ramp_stages.get(stage_name)
            normalized_checks = (
                normalized.get("checks") if isinstance(normalized, Mapping) else None
            )
            stage_checks = (
                stage_evidence.get("checks")
                if isinstance(stage_evidence, Mapping)
                else None
            )
            stage_check_aliases = {
                "terminal_status_scope": f"ramp_{stage_name}_scope",
                "terminal_status_scope_shape": f"ramp_{stage_name}_scope_shape",
                "terminal_status_outcome": f"ramp_{stage_name}_outcome",
            }
            expected_count = _strict_nonnegative_int(
                expected_stage_counts.get(stage_name)
            )
            observed_stage = ramp_stage_populations.get(stage_name)
            if (
                not isinstance(normalized, Mapping)
                or not isinstance(stage_evidence, Mapping)
                or not isinstance(normalized_checks, Mapping)
                or not isinstance(stage_checks, Mapping)
                or any(
                    type(value) is not bool
                    or stage_checks.get(stage_check_aliases.get(name, name)) is not value
                    for name, value in normalized_checks.items()
                )
                or expected_count is None
                or observed_stage is None
                or observed_stage[0] != expected_count
                or any(
                    _strict_nonnegative_int(observed_stage[1]["timing"].get(field))
                    != expected_count
                    for field in ("expected_count", "submitted_count", "completed_count")
                )
            ):
                return False

    acceptance = report_acceptance
    acceptance_checks = acceptance.get("checks")
    capacity_acceptance = acceptance_contract.get("kind") == "capacity"
    if not isinstance(acceptance_checks, Mapping):
        if capacity_acceptance and acceptance_checks is None:
            acceptance_checks = {}
        else:
            return False
    if (
        acceptance_checks.get("logical_outcome_consistency") is False
        and "logical" not in top_status_incomplete
    ):
        return False
    if (
        acceptance_checks.get("raw_outcome_consistency") is False
        and "raw_http" not in top_status_incomplete
    ):
        return False
    if (
        acceptance_checks.get("unexpected_statuses") is False
        and not top_raw_terminal_status_seen
    ):
        return False

    if capacity_acceptance:
        phase_slo = acceptance.get("phase_slo")
        phase_budgets = acceptance.get("phase_budget_evidence")
        if not isinstance(phase_slo, Mapping) or not isinstance(phase_budgets, Mapping):
            return False
        for phase_name, phase_result in phase_slo.items():
            checks = (
                phase_result.get("checks")
                if isinstance(phase_result, Mapping)
                else None
            )
            if not isinstance(checks, Mapping):
                return False
            if checks.get("logical_outcome_consistency") is False and (
                not _phase_status_is_bound(
                    str(phase_name),
                    "logical",
                    phase_status_incomplete=phase_status_incomplete,
                    phase_shared_scope_names=phase_shared_scope_names,
                )
            ):
                return False
            if checks.get("raw_outcome_consistency") is False and (
                not _phase_status_is_bound(
                    str(phase_name),
                    "raw",
                    phase_status_incomplete=phase_status_incomplete,
                    phase_shared_scope_names=phase_shared_scope_names,
                )
            ):
                return False
            if checks.get("timing_complete") is False:
                return False
        for phase_name, budget_result in phase_budgets.items():
            checks = (
                budget_result.get("checks")
                if isinstance(budget_result, Mapping)
                else None
            )
            if not isinstance(checks, Mapping):
                return False
            for check_name, semantic_scope in (
                ("logical_timing_complete", "logical"),
                ("raw_http_timing_complete", "raw"),
                ("logical_outcome_consistent", "logical"),
                ("raw_outcome_consistent", "raw"),
            ):
                if checks.get(check_name) is not False:
                    continue
                if phase_name == "primary":
                    status_bound = (
                        "logical" in top_status_incomplete
                        if semantic_scope == "logical"
                        else "raw_http" in top_status_incomplete
                    )
                elif phase_name == "duplicate":
                    status_bound = False
                else:
                    status_bound = _phase_status_is_bound(
                        str(phase_name),
                        semantic_scope,
                        phase_status_incomplete=phase_status_incomplete,
                        phase_shared_scope_names=phase_shared_scope_names,
                    )
                if not status_bound:
                    return False

    def check_status_timing_leaf(path: tuple[str, ...], name: str) -> bool:
        if name not in {
            "logical_outcome_consistent",
            "logical_timing_complete",
            "raw_outcome_consistent",
            "raw_http_timing_complete",
        }:
            return False
        if len(path) == 3 and path[0] == "phase_budget_evidence":
            phase_name = path[1]
        elif (
            len(path) == 4
            and path[:2] == ("phase_plan_evidence", "phase_budgets")
        ):
            phase_name = path[2]
        else:
            return False
        scope = "logical" if name.startswith("logical_") else "raw"
        return _phase_status_is_bound(
            phase_name,
            scope,
            phase_status_incomplete=phase_status_incomplete,
            phase_shared_scope_names=phase_shared_scope_names,
        )

    def check_ramp_status_timing_leaf(path: tuple[str, ...], name: str) -> bool:
        if (
            len(path) != 5
            or path[:2] != ("capacity_ramp_evidence", "stages")
            or path[3:] != ("budget_evidence", "checks")
        ):
            return False
        if name not in {
            "logical_outcome_consistent",
            "logical_timing_complete",
            "raw_outcome_consistent",
            "raw_http_timing_complete",
        }:
            return False
        scope = "logical" if name.startswith("logical_") else "raw"
        stage_name = path[2]
        return _ramp_status_is_bound(
            stage_name,
            scope,
            ramp_status_incomplete=ramp_status_incomplete,
            ramp_shared_scope_names=ramp_shared_scope_names,
        )

    def validate_status_check_paths(node: Any, path: tuple[str, ...] = ()) -> bool:
        if isinstance(node, Mapping):
            nested_checks = node.get("checks")
            if isinstance(nested_checks, Mapping):
                check_path = path + ("checks",)
                for name, value in nested_checks.items():
                    if value is False and name in {
                        "logical_outcome_consistent",
                        "logical_timing_complete",
                        "raw_outcome_consistent",
                        "raw_http_timing_complete",
                    }:
                        if not check_status_timing_leaf(check_path, str(name)):
                            if not check_ramp_status_timing_leaf(
                                check_path, str(name)
                            ):
                                return False
            return all(
                validate_status_check_paths(child, path + (str(key),))
                for key, child in node.items()
                if key != "checks" and isinstance(child, (Mapping, list, tuple))
            )
        if isinstance(node, (list, tuple)):
            return all(
                validate_status_check_paths(child, path + (str(index),))
                for index, child in enumerate(node)
            )
        return True

    if not validate_status_check_paths(acceptance):
        return False
    return (
        raw_summaries > 0
        and (logical_summaries > 0 or profile.get("mode") != "ready-vote")
        and terminal_status_seen
        and _report_binding(profile, report).get("complete") is True
    )


def _all_true_boolean_mapping(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value)
        and all(type(item) is bool and item is True for item in value.values())
    )


def _authored_ramp_budget_checks(
    profile: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    *,
    allow_closed_terminal_status_failure: bool = False,
    closed_terminal_ramp_stages: frozenset[str] = frozenset(),
) -> frozenset[str] | None:
    """Validate read-mix ramp evidence against its authored closed stage set."""

    traffic = profile.get("traffic")
    authored_stages = traffic.get("concurrency_stages") if isinstance(traffic, Mapping) else None
    if authored_stages is None or authored_stages == []:
        evidence = acceptance.get("capacity_ramp_evidence")
        if evidence is None:
            return frozenset()
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("complete") is not True
            or evidence.get("checks") != {}
            or evidence.get("stages") != {}
        ):
            return None
        return frozenset()
    if (
        not isinstance(authored_stages, list)
        or not authored_stages
        or any(type(stage) is not int or stage <= 0 for stage in authored_stages)
        or len(set(authored_stages)) != len(authored_stages)
    ):
        return None
    evidence = acceptance.get("capacity_ramp_evidence")
    checks = evidence.get("checks") if isinstance(evidence, Mapping) else None
    if not isinstance(evidence, Mapping) or not isinstance(checks, Mapping):
        return None
    expected = {
        "ramp_present",
        "ramp_authored_stage_plan",
        "ramp_stages_present",
        "ramp_stage_names_closed",
        "ramp_stage_order",
    } | {
        check
        for stage in authored_stages
        for check in (
            f"ramp_{stage}_expected_count_typed",
            f"ramp_{stage}_population",
            f"ramp_{stage}_budgets",
        )
    }
    if set(checks) != expected or any(type(value) is not bool for value in checks.values()):
        return None
    stages = evidence.get("stages")
    if not isinstance(stages, Mapping) or set(stages) != {
        str(stage) for stage in authored_stages
    }:
        return None
    budget_keys = {f"ramp_{stage}_budgets" for stage in authored_stages}
    closed_population_keys = {
        f"ramp_{stage}_population" for stage in closed_terminal_ramp_stages
    }
    failed_nonbudget_checks = {
        name
        for name, value in checks.items()
        if value is False and name not in budget_keys
    }
    if (
        any(
            value is not True
            for name, value in checks.items()
            if name not in budget_keys
            and not (
                allow_closed_terminal_status_failure
                and name in closed_population_keys
            )
        )
        or (
            failed_nonbudget_checks
            and (
                not allow_closed_terminal_status_failure
                or not failed_nonbudget_checks.issubset(closed_population_keys)
            )
        )
        or evidence.get("complete") is not all(value is True for value in checks.values())
    ):
        return None
    return frozenset(budget_keys)


def _phase_status_is_bound(
    phase_name: str, scope: str, *,
    phase_status_incomplete: set[tuple[str, str]],
    phase_shared_scope_names: set[str],
) -> bool:
    """Match a status failure to its exact phase scope or verified shared summary."""

    return (phase_name, scope) in phase_status_incomplete or (
        scope in {"logical", "raw"}
        and phase_name in phase_shared_scope_names
        and (phase_name, "raw") in phase_status_incomplete
    )


def _ramp_status_is_bound(
    stage_name: str, scope: str, *,
    ramp_status_incomplete: set[tuple[str, str]],
    ramp_shared_scope_names: set[str],
) -> bool:
    """Match a status failure to its exact ramp scope or verified shared summary."""

    return (stage_name, scope) in ramp_status_incomplete or (
        scope in {"logical", "raw"}
        and stage_name in ramp_shared_scope_names
        and (stage_name, "raw") in ramp_status_incomplete
    )


def _capacity_phase_budget_check_names(profile: Mapping[str, Any]) -> frozenset[str]:
    """Return the exact check keys emitted for an authored capacity phase."""

    acceptance = profile.get("acceptance")
    traffic = profile.get("traffic")
    contract = acceptance.get("slo") if isinstance(acceptance, Mapping) else None
    if not isinstance(contract, Mapping):
        return frozenset()
    if isinstance(acceptance, Mapping):
        contract = {
            **contract,
            **{
                key: acceptance[key]
                for key in (
                    "minimum_useful_goodput_actions_per_second",
                    "expected_statuses",
                )
                if key in acceptance
            },
        }
    names = {
        "logical_timing_complete",
        "raw_http_timing_complete",
        "logical_outcome_consistent",
        "raw_outcome_consistent",
        "retry_population_reconciled",
        "logical_actions_match_timing",
        "raw_requests_match_timing",
        "raw_requests_cover_logical_actions",
    }
    retry = traffic.get("retry") if isinstance(traffic, Mapping) else None
    if isinstance(retry, Mapping) and "max_retries" in retry:
        names.add("raw_requests_within_retry_bound")
    if "logical_final_failure_percent" in contract:
        names.add("logical_final_failure_budget")
    for percentile in ("p95", "p99"):
        if f"{percentile}_ms" in contract.get("accepted_request_latency", {}):
            names.add(f"accepted_{percentile}")
        if f"{percentile}_ms" in contract.get("logical_latency", {}):
            names.add(f"logical_{percentile}")
    if "max_shed_percent" in contract:
        names.add("shed_percent")
    if "max_retry_amplification_percent" in contract:
        names.add("retry_amplification_percent")
    if "minimum_useful_goodput_actions_per_second" in contract:
        names.update({"successful_goodput_measured", "minimum_useful_goodput"})
    return frozenset(names)


def _capacity_phase_plan_check_names(profile: Mapping[str, Any]) -> frozenset[str]:
    """Return the phase-plan check keys derived from the selected profile."""

    traffic = profile.get("traffic")
    phases = traffic.get("phases") if isinstance(traffic, Mapping) else None
    planned = profile_contract(profile).get("planned_work")
    phase_counts = planned.get("phase_logical_actions") if isinstance(planned, Mapping) else None
    if not isinstance(phases, list) or not isinstance(phase_counts, Mapping):
        return frozenset()
    names = [phase.get("name") for phase in phases if isinstance(phase, Mapping)]
    if len(names) != len(phases) or any(not isinstance(name, str) for name in names):
        return frozenset()
    expected = {
        "phase_names_closed",
        "phase_population_names_closed",
        "auxiliary_phase_names_closed",
        "expected_duplicate_count_typed",
        "expected_state_read_count_typed",
        "max_retries_typed",
        "phase_names_and_order",
        "duplicate_population",
        "state_read_population",
    }
    expected.update(f"phase_{name}" for name in names)
    expected.update(f"phase_population_{name}" for name in phase_counts)
    return frozenset(expected)


def _acceptance_budget_check_names(profile: Mapping[str, Any]) -> frozenset[str]:
    """Derive the required contract-validation leaves from the trusted profile."""

    contract = profile.get("acceptance")
    if not isinstance(contract, Mapping):
        return frozenset()
    try:
        from tools.platform_load_acceptance import _acceptance_budget_evidence
    except ModuleNotFoundError:  # Direct execution from platform/tools.
        from platform_load_acceptance import _acceptance_budget_evidence
    evidence = _acceptance_budget_evidence(contract, require_statuses=True)
    checks = evidence.get("checks") if isinstance(evidence, Mapping) else None
    return frozenset(checks) if isinstance(checks, Mapping) else frozenset()


def _profile_budget_phase_names(profile: Mapping[str, Any]) -> frozenset[str]:
    """Return only the phase keys authorized to carry budget evidence."""

    planned = profile_contract(profile).get("planned_work")
    phases = planned.get("phase_logical_actions") if isinstance(planned, Mapping) else None
    if not isinstance(phases, Mapping):
        return frozenset()
    return frozenset(str(name) for name, count in phases.items() if type(count) is int and count > 0)


def _closed_missing_observer_binding(value: Any) -> bool:
    """Accept only the evaluator's exact no-origin observer state."""

    expected_checks = {
        "fixture_marker",
        "external_run_id",
        "binding_complete",
        "observer_stop_file_seen",
        "observer_not_timed_out",
    }
    return (
        isinstance(value, Mapping)
        and set(value)
        == {
            "complete",
            "fixture_marker",
            "external_run_id",
            "expected_fixture_marker",
            "expected_external_run_id",
            "checks",
        }
        and value.get("complete") is False
        and value.get("fixture_marker") is None
        and value.get("external_run_id") is None
        and value.get("expected_fixture_marker") is None
        and value.get("expected_external_run_id") is None
        and isinstance(value.get("checks"), Mapping)
        and set(value["checks"]) == expected_checks
        and all(type(item) is bool and item is False for item in value["checks"].values())
    )


_CAPACITY_PHASE_SLO_REQUIRED_CHECKS = frozenset(
    {
        "contract",
        "logical_final_failure",
        "timing_complete",
        "capacity_ramp_evidence",
        "acceptance_budget_evidence",
        "logical_outcome_consistency",
        "raw_outcome_consistency",
        "retry_population_reconciled",
        "accepted_p50",
        "accepted_p90",
        "accepted_p95",
        "accepted_p99",
        "logical_p95",
        "logical_p99",
        "normal_overload_shedding",
        "retry_amplification_percent",
        "observer_binding",
        "successful_goodput_measured",
        "minimum_useful_goodput",
    }
)

_CAPACITY_EMPTY_PHASE_BUDGET_CHECKS = frozenset(
    {
        "empty_phase",
        "logical_actions_match_timing",
        "logical_outcome_consistent",
        "logical_timing_complete",
        "raw_http_timing_complete",
        "raw_outcome_consistent",
        "raw_requests_cover_logical_actions",
        "raw_requests_match_timing",
        "retry_population_reconciled",
    }
)

_CAPACITY_ACCEPTANCE_KEYS = frozenset(
    {
        "acceptance_budget_evidence",
        "capacity_ramp_evidence",
        "contract_ok",
        "decision",
        "experiment_complete",
        "max_stable_goodput_actions_per_second",
        "note",
        "observer_binding",
        "origin_safety",
        "outcome_evidence",
        "passed",
        "pending_origin_evidence",
        "phase_budget_evidence",
        "phase_completion",
        "phase_plan_evidence",
        "phase_population_evidence",
        "phase_retry_evidence",
        "phase_slo",
        "population_checks",
        "raw_logical_population_evidence",
        "slo_capacity_logical_actions_per_second",
        "target_passed",
        "timing_evidence",
        "top_population_timing_evidence",
    }
)

_CAPACITY_LOGICAL_OUTCOME_CHECKS = frozenset(
    {
        "typed_actions",
        "typed_final_successes",
        "typed_final_failures",
        "outcomes_do_not_exceed_actions",
        "outcomes_sum_to_actions",
        "failure_rate_present",
        "failure_rate_recomputed",
    }
)
_CAPACITY_RAW_OUTCOME_CHECKS = frozenset(
    {
        "typed_requests",
        "typed_errors",
        "typed_successful_responses",
        "responses_do_not_exceed_requests",
        "responses_sum_to_requests",
        "failure_rate_present",
        "failure_rate_recomputed",
    }
)
_CAPACITY_PHASE_RETRY_CHECKS = frozenset(
    {
        "complete",
        "aggregate_retry_totals_present",
        "phase_retry_totals_reconcilable",
        "logical_retries_match_phase_totals",
        "raw_retries_match_phase_totals",
    }
)


def _capacity_outcome_evidence_is_closed(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"logical", "raw_http"}:
        return False
    logical = value.get("logical")
    raw_http = value.get("raw_http")
    if not isinstance(logical, Mapping) or not isinstance(raw_http, Mapping):
        return False
    logical_checks = logical.get("checks")
    raw_checks = raw_http.get("checks")
    return (
        set(logical) == {
            "present", "complete", "kind", "actions", "final_successes",
            "final_failures", "expected_failure_rate_percent",
            "actual_failure_rate_percent", "checks",
        }
        and logical.get("present") is True
        and logical.get("complete") is True
        and logical.get("kind") == "logical"
        and isinstance(logical_checks, Mapping)
        and set(logical_checks) == _CAPACITY_LOGICAL_OUTCOME_CHECKS
        and _all_true_boolean_mapping(logical_checks)
        and set(raw_http) == {
            "present", "complete", "kind", "requests", "errors",
            "successful_responses", "expected_failure_rate_percent",
            "actual_failure_rate_percent", "checks",
        }
        and raw_http.get("present") is True
        and raw_http.get("complete") is True
        and raw_http.get("kind") == "raw_http"
        and isinstance(raw_checks, Mapping)
        and set(raw_checks) == _CAPACITY_RAW_OUTCOME_CHECKS
        and _all_true_boolean_mapping(raw_checks)
    )


def _capacity_phase_retry_evidence_is_closed(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _CAPACITY_PHASE_RETRY_CHECKS
        and _all_true_boolean_mapping(value)
    )


def _closed_capacity_pending_origin_acceptance(
    profile: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    *,
    allow_closed_terminal_status_failure: bool = False,
    closed_terminal_ramp_stages: frozenset[str] = frozenset(),
) -> bool:
    """Validate capacity's phase-nested candidate while origin is pending.

    Capacity acceptance deliberately has no flat ``checks`` map. Completion,
    population and timing evidence are separate from phase/ramp target
    budgets; only known budget leaves and the expected pending observer
    binding may be false here. Origin safety is evaluated after attachment.
    """

    traffic = profile.get("traffic")
    if set(acceptance) != _CAPACITY_ACCEPTANCE_KEYS:
        return False
    phases = traffic.get("phases") if isinstance(traffic, Mapping) else None
    if not isinstance(phases, list) or not phases:
        return False
    phase_names = [
        str(phase.get("name"))
        for phase in phases
        if isinstance(phase, Mapping) and isinstance(phase.get("name"), str)
    ]
    if len(phase_names) != len(phases) or len(set(phase_names)) != len(phase_names):
        return False
    phase_slo = acceptance.get("phase_slo")
    phase_budget_evidence = acceptance.get("phase_budget_evidence")
    if (
        not isinstance(phase_slo, Mapping)
        or set(phase_slo) != set(phase_names)
        or not isinstance(phase_budget_evidence, Mapping)
        or set(phase_budget_evidence) != {*phase_names, "primary", "duplicate"}
    ):
        return False
    for name in phase_names:
        phase_result = phase_slo.get(name)
        budget_result = phase_budget_evidence.get(name)
        phase_checks = (
            phase_result.get("checks") if isinstance(phase_result, Mapping) else None
        )
        budget_checks = (
            budget_result.get("checks") if isinstance(budget_result, Mapping) else None
        )
        if (
            not isinstance(phase_result, Mapping)
            or phase_result.get("passed") is not False
            or phase_result.get("pending_origin_evidence") is not True
            or phase_result.get("contract_ok") is not True
            or phase_result.get("decision") != "SLO FAIL"
            or not isinstance(phase_checks, Mapping)
            or not _CAPACITY_PHASE_SLO_REQUIRED_CHECKS.issubset(phase_checks)
            or not _acceptance_failure_is_budget_only(
                phase_result,
                "slo",
                allow_no_budget_failure=True,
                allow_pending_observer_binding=True,
                allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
            )
            or not isinstance(budget_result, Mapping)
            or not isinstance(budget_checks, Mapping)
            or set(budget_checks) != _capacity_phase_budget_check_names(profile)
            or any(type(value) is not bool for value in budget_checks.values())
            or type(budget_result.get("passed")) is not bool
            or budget_result.get("passed") is not all(
                value is True for value in budget_checks.values()
            )
            or not _acceptance_failure_is_budget_only(
                budget_result,
                "capacity",
                allow_no_budget_failure=True,
                allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
            )
        ):
            return False

    # The producer emits one additional primary aggregate budget alongside
    # each authored traffic phase.  Bind that exact entry as well; it is a
    # target budget, not a substitute for any authored phase population.
    primary_budget = phase_budget_evidence.get("primary")
    primary_checks = (
        primary_budget.get("checks") if isinstance(primary_budget, Mapping) else None
    )
    if (
        not isinstance(primary_budget, Mapping)
        or not isinstance(primary_checks, Mapping)
        or set(primary_checks) != _capacity_phase_budget_check_names(profile)
        or any(type(value) is not bool for value in primary_checks.values())
        or type(primary_budget.get("passed")) is not bool
        or primary_budget.get("passed") is not all(
            value is True for value in primary_checks.values()
        )
        or not _acceptance_failure_is_budget_only(
            primary_budget,
            "capacity",
            allow_no_budget_failure=True,
            allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        )
    ):
        return False

    duplicate_budget = phase_budget_evidence.get("duplicate")
    duplicate_checks = (
        duplicate_budget.get("checks") if isinstance(duplicate_budget, Mapping) else None
    )
    if (
        not isinstance(duplicate_budget, Mapping)
        or duplicate_budget.get("passed") is not True
        or not isinstance(duplicate_checks, Mapping)
        or set(duplicate_checks) != _CAPACITY_EMPTY_PHASE_BUDGET_CHECKS
        or any(value is not True for value in duplicate_checks.values())
    ):
        return False

    stage_evidence = acceptance.get("capacity_ramp_evidence")
    if not isinstance(stage_evidence, Mapping):
        return False
    stage_checks = stage_evidence.get("checks")
    if not isinstance(stage_checks, Mapping):
        return False
    if any(type(value) is not bool for value in stage_checks.values()):
        return False
    allowed_ramp_budget_checks = _authored_ramp_budget_checks(
        profile,
        acceptance,
        allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        closed_terminal_ramp_stages=closed_terminal_ramp_stages,
    )
    if allowed_ramp_budget_checks is None:
        return False
    authored_stages = traffic.get("concurrency_stages")
    if allowed_ramp_budget_checks:
        expected_stage_checks = {
            "ramp_present",
            "ramp_authored_stage_plan",
            "ramp_stages_present",
            "ramp_stage_names_closed",
            "ramp_stage_order",
        } | {
            check
            for stage in authored_stages
            for check in (
                f"ramp_{stage}_expected_count_typed",
                f"ramp_{stage}_population",
                f"ramp_{stage}_budgets",
            )
        }
        if set(stage_checks) != expected_stage_checks:
            return False
        if not _acceptance_failure_is_budget_only(
            {"checks": stage_checks},
            "capacity",
            allow_no_budget_failure=True,
            allowed_ramp_budget_checks=allowed_ramp_budget_checks,
            allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
            closed_terminal_ramp_stages=closed_terminal_ramp_stages,
        ):
            return False
    elif stage_checks or stage_evidence.get("stages") != {}:
        return False
    elif stage_evidence.get("complete") is not True:
        return False
    if stage_evidence.get("complete") is not all(
        value is True for value in stage_checks.values()
    ):
        return False

    phase_plan = acceptance.get("phase_plan_evidence")
    budget_contract = acceptance.get("acceptance_budget_evidence")
    budget_contract_checks = (
        budget_contract.get("checks") if isinstance(budget_contract, Mapping) else None
    )
    acceptance_checks = acceptance.get("checks")
    if acceptance_checks is not None and (
        not isinstance(acceptance_checks, Mapping)
        or not acceptance_checks
        or any(type(value) is not bool or value is not True for value in acceptance_checks.values())
    ):
        return False
    if (
        acceptance.get("experiment_complete") is not False
        or acceptance.get("target_passed") is not False
        or acceptance.get("phase_completion") is not True
        or not _all_true_boolean_mapping(acceptance.get("population_checks"))
        or not _all_true_boolean_mapping(acceptance.get("phase_population_evidence"))
        or not _all_true_boolean_mapping(acceptance.get("raw_logical_population_evidence"))
        or not _all_true_boolean_mapping(acceptance.get("top_population_timing_evidence"))
        or not isinstance(acceptance.get("timing_evidence"), Mapping)
        or acceptance["timing_evidence"].get("complete") is not True
        or not isinstance(phase_plan, Mapping)
        or phase_plan.get("complete") is not True
        or phase_plan.get("phase_budgets") != phase_budget_evidence
        or set(phase_plan.get("checks", {}))
        != _capacity_phase_plan_check_names(profile)
        or not _all_true_boolean_mapping(phase_plan.get("checks"))
        or not isinstance(budget_contract, Mapping)
        or budget_contract.get("complete") is not True
        or not isinstance(budget_contract_checks, Mapping)
        or set(budget_contract_checks) != _acceptance_budget_check_names(profile)
        or not _all_true_boolean_mapping(budget_contract_checks)
        or not _capacity_outcome_evidence_is_closed(
            acceptance.get("outcome_evidence")
        )
        or not _capacity_phase_retry_evidence_is_closed(
            acceptance.get("phase_retry_evidence")
        )
    ):
        return False
    return True


def evaluate_report(
    profile: Mapping[str, Any],
    report_path: Path,
    server_observability_path: Path | None,
    *,
    defer_completed_slo_failure: bool = False,
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
        existing_acceptance = report.get("acceptance")
        existing_decision = (
            existing_acceptance.get("decision")
            if isinstance(existing_acceptance, Mapping)
            else None
        )
        runtime_supervisor = report.get("runtime_supervisor")
        runtime_reason = (
            runtime_supervisor.get("reason")
            if isinstance(runtime_supervisor, Mapping)
            else None
        )
        # Keep a closed supervisor outcome visible through the final evaluator.
        # A killed/malformed child cannot satisfy the normal binding contract,
        # but replacing a precise absolute-budget diagnosis with a generic
        # binding failure makes timeout triage needlessly ambiguous.
        runtime_budget_exceeded = (
            existing_decision == "LOAD RUNTIME BUDGET EXCEEDED"
            or runtime_reason in {"max_duration_seconds", "max_runner_minutes"}
        )
        binding_decision = (
            "LOAD RUNTIME BUDGET EXCEEDED"
            if runtime_budget_exceeded
            else "LOAD REPORT BINDING FAIL"
        )
        report["acceptance"] = {
            "passed": False,
            "decision": binding_decision,
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
                    "decision": binding_decision,
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
            authored_phases = profile.get("traffic", {}).get("phases") or []
            authored_names = [
                str(phase["name"])
                for phase in authored_phases
                if isinstance(phase, Mapping) and isinstance(phase.get("name"), str)
            ]
            for phase_name in authored_names:
                phase_summary = ramp["phases"].get(phase_name)
                if isinstance(phase_summary, Mapping):
                    report_phase_summaries[phase_name] = phase_summary
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
    if defer_completed_slo_failure and _is_complete_observer_bound_decision(
        profile, report
    ):
        # The workflow records this as a completed measured SLO failure and
        # publishes its sanitized evidence, then leaves the overall run red.
        return WORKFLOW_PENDING_ORIGIN_EXIT
    return 0 if result.get("passed") is True else 1


def _is_complete_observer_bound_decision(
    profile: Mapping[str, Any], report: Mapping[str, Any]
) -> bool:
    """Return true for a completed measurement decision, including an SLO fail.

    Binding, containment, origin safety, timing and population completeness
    remain hard requirements.  Only the final behavior/target outcome may be
    false for this workflow-only completion classification.
    """

    acceptance = report.get("acceptance")
    binding = report.get("report_binding")
    supervisor = report.get("runtime_supervisor")
    observability = report.get("origin_observability")
    if not all(
        isinstance(value, Mapping)
        for value in (acceptance, binding, supervisor, observability)
    ):
        return False
    assert isinstance(acceptance, Mapping)
    assert isinstance(binding, Mapping)
    assert isinstance(supervisor, Mapping)
    assert isinstance(observability, Mapping)
    origin_binding = observability.get("binding")
    acceptance_binding = acceptance.get("observer_binding")
    origin_safety = acceptance.get("origin_safety")
    timing = acceptance.get("timing_evidence")
    phase_plan = acceptance.get("phase_plan_evidence")
    if not all(
        isinstance(value, Mapping)
        for value in (origin_binding, acceptance_binding, timing)
    ):
        return False
    assert isinstance(origin_binding, Mapping)
    assert isinstance(acceptance_binding, Mapping)
    assert isinstance(timing, Mapping)
    profile_acceptance = profile.get("acceptance")
    profile_kind = (
        profile_acceptance.get("kind", "slo")
        if isinstance(profile_acceptance, Mapping)
        else "slo"
    )
    acceptance_checks = acceptance.get("checks")
    has_resource_safety = (
        isinstance(profile_acceptance, Mapping)
        and (
            isinstance(profile_acceptance.get("resource_safety"), Mapping)
            or (
                profile_kind in {"stress", "spike"}
                and all(
                    field in profile_acceptance
                    for field in (
                        "max_postgres_backend_connections",
                        "max_waiting_backends",
                        "max_lock_waiters",
                        "max_cpu_per_core_percent",
                        "pool_checkout_wait_ms",
                    )
                )
            )
        )
    )
    origin_budget_failure = False
    if has_resource_safety:
        safety_checks = (
            origin_safety.get("checks")
            if isinstance(origin_safety, Mapping)
            else None
        )
        if (
            not isinstance(origin_safety, Mapping)
            or type(origin_safety.get("passed")) is not bool
            or not isinstance(safety_checks, Mapping)
            or not safety_checks
            or any(type(value) is not bool for value in safety_checks.values())
            or not (_ORIGIN_RESOURCE_BUDGET_CHECKS | _ORIGIN_RESOURCE_HARD_CHECKS).issubset(
                safety_checks
            )
            or bool(
                set(safety_checks)
                - _ORIGIN_RESOURCE_BUDGET_CHECKS
                - _ORIGIN_RESOURCE_HARD_CHECKS
                - {"postgres_backend_ownership_consistent"}
            )
            or safety_checks.get("observer_completed") is not True
            or safety_checks.get("required_diagnostics_present") is not True
            or (
                "postgres_backend_ownership_consistent" in safety_checks
                and safety_checks["postgres_backend_ownership_consistent"] is not True
            )
            or origin_safety.get("passed") is not all(safety_checks.values())
            or (
                profile_kind == "capacity"
                and (
                    acceptance_checks is not None
                    or not _closed_capacity_completed_budget_failure(
                        profile,
                        acceptance,
                        origin_budget_failure=any(
                            check_name in _ORIGIN_RESOURCE_BUDGET_CHECKS
                            and check is False
                            for check_name, check in safety_checks.items()
                        ),
                    )
                )
            )
            or (
                profile_kind != "capacity"
                and (
                    not isinstance(acceptance_checks, Mapping)
                    or acceptance_checks.get("origin_safety")
                    is not origin_safety.get("passed")
                )
            )
        ):
            return False
        origin_budget_failure = any(
            check_name in _ORIGIN_RESOURCE_BUDGET_CHECKS and check is False
            for check_name, check in safety_checks.items()
        )
    elif origin_safety is not None or (
        isinstance(acceptance_checks, Mapping)
        and "origin_safety" in acceptance_checks
    ):
        return False
    if (
        report.get("authoritative") is not True
        or report.get("dispatchable") is not True
        or report.get("partial_work") is not False
        or report.get("inflight_unknown") is not False
        or binding.get("complete") is not True
        or acceptance.get("pending_origin_evidence") is not False
        or acceptance.get("passed") is not False
        or supervisor.get("reason") != "none"
        or supervisor.get("namespace_closed") is not True
        or supervisor.get("descendants_reaped") is not True
        or supervisor.get("partial_work") is not False
        or supervisor.get("inflight_unknown") is not False
        or supervisor.get("report_error") is not None
        or origin_binding.get("complete") is not True
        or acceptance_binding.get("complete") is not True
        or timing.get("complete") is not True
    ):
        return False
    for key in (
        "phase_population_evidence",
        "raw_logical_population_evidence",
        "top_population_timing_evidence",
    ):
        evidence = acceptance.get(key)
        if not isinstance(evidence, Mapping) or any(value is not True for value in evidence.values()):
            return False
    if not isinstance(phase_plan, Mapping) or phase_plan.get("complete") is not True:
        return False
    if acceptance.get("experiment_complete") is False:
        return False
    decision = acceptance.get("decision")
    profile_kind = (profile.get("acceptance") or {}).get("kind", "slo")
    expected_decision = {
        "slo": "SLO FAIL",
        "stress": "STRESS BEHAVIOR FAIL",
        "spike": "SPIKE BEHAVIOR FAIL",
        "capacity": "CAPACITY EXPERIMENT COMPLETE TARGET FAIL",
    }.get(profile_kind)
    if decision != expected_decision:
        return False
    terminal_status_failure = _closed_terminal_status_failure(profile, report)
    if acceptance.get("contract_ok") is not True and not terminal_status_failure:
        return False
    if profile_kind == "capacity" and acceptance.get("experiment_complete") is not True:
        return False
    if profile_kind == "capacity" and acceptance.get("phase_completion") is not True:
        return False
    traffic = profile.get("traffic")
    authored_stages = (
        traffic.get("concurrency_stages") if isinstance(traffic, Mapping) else None
    )
    allowed_ramp_budget_checks = (
        frozenset(f"ramp_{stage}_budgets" for stage in authored_stages)
        if isinstance(authored_stages, list)
        and authored_stages
        and all(type(stage) is int and stage > 0 for stage in authored_stages)
        else frozenset()
    )
    if profile_kind == "capacity":
        return _closed_capacity_completed_budget_failure(
            profile,
            acceptance,
            origin_budget_failure=origin_budget_failure,
            allow_closed_terminal_status_failure=terminal_status_failure,
            closed_terminal_ramp_stages=(
                _closed_terminal_ramp_stage_names(profile, report)
                if terminal_status_failure
                else frozenset()
            ),
        )
    return _acceptance_failure_is_budget_only(
        acceptance,
        str(profile_kind),
        allowed_ramp_budget_checks=allowed_ramp_budget_checks,
        allowed_phase_names=_profile_budget_phase_names(profile),
        allow_no_budget_failure=terminal_status_failure,
        allow_closed_terminal_status_failure=terminal_status_failure,
    )


def _closed_capacity_completed_budget_failure(
    profile: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    *,
    origin_budget_failure: bool,
    allow_closed_terminal_status_failure: bool = False,
    closed_terminal_ramp_stages: frozenset[str] = frozenset(),
) -> bool:
    """Require capacity completion and permit only authored target misses."""

    traffic = profile.get("traffic")
    if set(acceptance) != _CAPACITY_ACCEPTANCE_KEYS:
        return False
    phases = traffic.get("phases") if isinstance(traffic, Mapping) else None
    if not isinstance(phases, list) or not phases:
        return False
    phase_names = [
        str(phase.get("name"))
        for phase in phases
        if isinstance(phase, Mapping) and isinstance(phase.get("name"), str)
    ]
    if len(phase_names) != len(phases) or len(set(phase_names)) != len(phase_names):
        return False
    phase_slo = acceptance.get("phase_slo")
    phase_budget_evidence = acceptance.get("phase_budget_evidence")
    if (
        not isinstance(phase_slo, Mapping)
        or set(phase_slo) != set(phase_names)
        or not isinstance(phase_budget_evidence, Mapping)
        or set(phase_budget_evidence) != {*phase_names, "primary", "duplicate"}
    ):
        return False
    budget_failure_seen = origin_budget_failure or allow_closed_terminal_status_failure
    for name in phase_names:
        phase_result = phase_slo.get(name)
        budget_result = phase_budget_evidence.get(name)
        phase_checks = (
            phase_result.get("checks") if isinstance(phase_result, Mapping) else None
        )
        if (
            not isinstance(phase_result, Mapping)
            or type(phase_result.get("passed")) is not bool
            or phase_result.get("pending_origin_evidence") is not False
            or phase_result.get("contract_ok") is not True
            or phase_result.get("decision") != (
                "SLO PASS" if phase_result.get("passed") is True else "SLO FAIL"
            )
            or not isinstance(phase_checks, Mapping)
            or not _CAPACITY_PHASE_SLO_REQUIRED_CHECKS.issubset(phase_checks)
            or not isinstance(phase_result.get("observer_binding"), Mapping)
            or phase_result["observer_binding"].get("complete") is not True
            or not isinstance(budget_result, Mapping)
            or not isinstance(budget_result.get("checks"), Mapping)
            or type(budget_result.get("passed")) is not bool
            or budget_result.get("passed") is not all(
                value is True for value in budget_result["checks"].values()
            )
        ):
            return False
        if not _acceptance_failure_is_budget_only(
            phase_result,
            "slo",
            allow_no_budget_failure=True,
            allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        ) or not _acceptance_failure_is_budget_only(
            budget_result,
            "capacity",
            allow_no_budget_failure=True,
            allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        ):
            return False
        budget_failure_seen = (
            _acceptance_failure_is_budget_only(
                phase_result,
                "slo",
                allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
            )
            or _acceptance_failure_is_budget_only(
                budget_result,
                "capacity",
                allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
            )
            or budget_failure_seen
        )

    primary_budget = phase_budget_evidence.get("primary")
    primary_checks = (
        primary_budget.get("checks") if isinstance(primary_budget, Mapping) else None
    )
    if (
        not isinstance(primary_budget, Mapping)
        or not isinstance(primary_checks, Mapping)
        or set(primary_checks) != _capacity_phase_budget_check_names(profile)
        or any(type(value) is not bool for value in primary_checks.values())
        or type(primary_budget.get("passed")) is not bool
        or primary_budget.get("passed") is not all(
            value is True for value in primary_checks.values()
        )
        or not _acceptance_failure_is_budget_only(
            primary_budget,
            "capacity",
            allow_no_budget_failure=True,
            allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        )
    ):
        return False
    budget_failure_seen = (
        _acceptance_failure_is_budget_only(
            primary_budget,
            "capacity",
            allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        )
        or budget_failure_seen
    )

    duplicate_budget = phase_budget_evidence.get("duplicate")
    duplicate_checks = (
        duplicate_budget.get("checks") if isinstance(duplicate_budget, Mapping) else None
    )
    if (
        not isinstance(duplicate_budget, Mapping)
        or duplicate_budget.get("passed") is not True
        or not isinstance(duplicate_checks, Mapping)
        or set(duplicate_checks) != _CAPACITY_EMPTY_PHASE_BUDGET_CHECKS
        or any(value is not True for value in duplicate_checks.values())
    ):
        return False

    stage_evidence = acceptance.get("capacity_ramp_evidence")
    stage_checks = (
        stage_evidence.get("checks") if isinstance(stage_evidence, Mapping) else None
    )
    if not isinstance(stage_checks, Mapping) or any(
        type(value) is not bool for value in stage_checks.values()
    ):
        return False
    allowed_ramp_budget_checks = _authored_ramp_budget_checks(
        profile,
        acceptance,
        allow_closed_terminal_status_failure=allow_closed_terminal_status_failure,
        closed_terminal_ramp_stages=closed_terminal_ramp_stages,
    )
    if allowed_ramp_budget_checks is None:
        return False
    authored_stages = traffic.get("concurrency_stages")
    ramp_acceptance = {"checks": stage_checks}
    if allowed_ramp_budget_checks:
        expected_stage_checks = {
            "ramp_present",
            "ramp_authored_stage_plan",
            "ramp_stages_present",
            "ramp_stage_names_closed",
            "ramp_stage_order",
        } | {
            check
            for stage in authored_stages
            for check in (
                f"ramp_{stage}_expected_count_typed",
                f"ramp_{stage}_population",
                f"ramp_{stage}_budgets",
            )
        }
        if set(stage_checks) != expected_stage_checks:
            return False
        if not _acceptance_failure_is_budget_only(
            ramp_acceptance,
            "capacity",
            allow_no_budget_failure=True,
            allowed_ramp_budget_checks=allowed_ramp_budget_checks,
        ):
            return False
    elif stage_checks or stage_evidence.get("stages") != {}:
        return False
    elif stage_evidence.get("complete") is not True:
        return False
    if stage_evidence.get("complete") is not all(
        value is True for value in stage_checks.values()
    ):
        return False
    budget_failure_seen = any(
        name in allowed_ramp_budget_checks and value is False
        for name, value in stage_checks.items()
    ) or budget_failure_seen

    budget_contract = acceptance.get("acceptance_budget_evidence")
    budget_contract_checks = (
        budget_contract.get("checks") if isinstance(budget_contract, Mapping) else None
    )
    return (
        acceptance.get("experiment_complete") is True
        and acceptance.get("target_passed") is False
        and acceptance.get("phase_completion") is True
        and _all_true_boolean_mapping(acceptance.get("population_checks"))
        and _all_true_boolean_mapping(acceptance.get("phase_population_evidence"))
        and _all_true_boolean_mapping(acceptance.get("raw_logical_population_evidence"))
        and _all_true_boolean_mapping(acceptance.get("top_population_timing_evidence"))
        and isinstance(acceptance.get("timing_evidence"), Mapping)
        and acceptance["timing_evidence"].get("complete") is True
        and isinstance(acceptance.get("phase_plan_evidence"), Mapping)
        and acceptance["phase_plan_evidence"].get("complete") is True
        and acceptance["phase_plan_evidence"].get("phase_budgets")
        == phase_budget_evidence
        and _all_true_boolean_mapping(acceptance["phase_plan_evidence"].get("checks"))
        and isinstance(budget_contract, Mapping)
        and budget_contract.get("complete") is True
        and isinstance(budget_contract_checks, Mapping)
        and set(budget_contract_checks) == _acceptance_budget_check_names(profile)
        and _all_true_boolean_mapping(budget_contract_checks)
        and _capacity_outcome_evidence_is_closed(
            acceptance.get("outcome_evidence")
        )
        and _capacity_phase_retry_evidence_is_closed(
            acceptance.get("phase_retry_evidence")
        )
        and budget_failure_seen
    )


_PROFILE_BUDGET_CHECKS: dict[str, frozenset[str]] = {
    "slo": frozenset(
        {
            "logical_final_failure",
            "logical_final_failure_budget",
            "accepted_p50",
            "accepted_p90",
            "accepted_p95",
            "accepted_p99",
            "logical_p95",
            "logical_p99",
            "normal_overload_shedding",
            "retry_amplification_percent",
            "pool_checkout_p95_ms",
            "pool_checkout_p99_ms",
            "postgres_backend_connections",
            "waiting_backends",
            "lock_waiters",
            "cpu_per_core",
        }
    ),
    "stress": frozenset(
        {
            "logical_final_failure_budget",
            "accepted_p95_ms",
            "accepted_p99_ms",
            "logical_p95_ms",
            "logical_p99_ms",
            "retry_amplification_percent",
            "shed_percent",
            "minimum_useful_goodput",
            "recovery_goodput_positive",
            "pool_checkout_p95_ms",
            "pool_checkout_p99_ms",
            "postgres_backend_connections",
            "waiting_backends",
            "lock_waiters",
            "cpu_per_core",
        }
    ),
    "spike": frozenset(
        {
            "logical_final_failure_budget",
            "accepted_p95_ms",
            "accepted_p99_ms",
            "logical_p95_ms",
            "logical_p99_ms",
            "retry_amplification_percent",
            "shed_percent",
            "minimum_useful_goodput",
            "recovery_goodput_positive",
            "pool_checkout_p95_ms",
            "pool_checkout_p99_ms",
            "postgres_backend_connections",
            "waiting_backends",
            "lock_waiters",
            "cpu_per_core",
        }
    ),
    "capacity": frozenset(
        {
            "accepted_p50",
            "logical_final_failure_budget",
            "accepted_p90",
            "accepted_p95",
            "accepted_p99",
            "logical_p95",
            "logical_p99",
            "logical_final_failure",
            "normal_overload_shedding",
            "retry_amplification_percent",
            "shed_percent",
            "minimum_useful_goodput",
            "pool_checkout_p95_ms",
            "pool_checkout_p99_ms",
            "postgres_backend_connections",
            "waiting_backends",
            "lock_waiters",
            "cpu_per_core",
        }
    ),
}

_ORIGIN_RESOURCE_BUDGET_CHECKS = frozenset(
    {
        "pool_checkout_p95_ms",
        "pool_checkout_p99_ms",
        "postgres_backend_connections",
        "waiting_backends",
        "lock_waiters",
        "cpu_per_core",
    }
)
_ORIGIN_RESOURCE_HARD_CHECKS = frozenset(
    {"observer_completed", "required_diagnostics_present"}
)
_PHASE_TARGET_BUDGET_CHECKS = frozenset(
    {
        "logical_final_failure_budget",
        "accepted_p95",
        "logical_p95",
        "accepted_p99",
        "logical_p99",
        "shed_percent",
        "retry_amplification_percent",
        "minimum_useful_goodput",
    }
)
_RAMP_STAGE_BUDGET_CHECKS = frozenset(
    {
        "logical_final_failure_budget",
        "accepted_p95",
        "accepted_p99",
        "logical_p95",
        "logical_p99",
        "shed_percent",
        "retry_amplification_percent",
        "minimum_useful_goodput",
    }
)

_BUDGET_AGGREGATE_CHECKS = frozenset(
    {"phase_budgets", "capacity_ramp_evidence", "origin_safety"}
)


def _acceptance_failure_is_budget_only(
    acceptance: Mapping[str, Any],
    profile_kind: str,
    *,
    allowed_ramp_budget_checks: frozenset[str] = frozenset(),
    allowed_phase_names: frozenset[str] = frozenset(),
    allow_no_budget_failure: bool = False,
    allow_pending_observer_binding: bool = False,
    allow_closed_terminal_status_failure: bool = False,
    closed_terminal_ramp_stages: frozenset[str] = frozenset(),
) -> bool:
    """Reject an SLO-miss classification if any structural check failed.

    The acceptance evaluator exposes leaf checks plus aggregate evidence
    checks.  False budget leaves are allowed only when their matching nested
    check tree is present; every other false leaf is an invalid measurement,
    observer, population, or contract result and must remain a hard failure.
    """

    allowed = _PROFILE_BUDGET_CHECKS.get(profile_kind)
    checks = acceptance.get("checks")
    if allowed is None or not isinstance(checks, Mapping) or not checks:
        return False

    pending_observer_checks = {
        "fixture_marker",
        "external_run_id",
        "binding_complete",
        "observer_stop_file_seen",
        "observer_not_timed_out",
    }
    pending_observer_is_closed = _closed_missing_observer_binding(
        acceptance.get("observer_binding")
    )
    phase_slo = acceptance.get("phase_slo")

    def pending_phase_observer_is_closed(phase_name: str) -> bool:
        phase = phase_slo.get(phase_name) if isinstance(phase_slo, Mapping) else None
        return (
            isinstance(phase, Mapping)
            and phase.get("pending_origin_evidence") is True
            and _closed_missing_observer_binding(phase.get("observer_binding"))
        )

    phase_budget_evidence = acceptance.get("phase_budget_evidence")
    phase_plan_evidence = acceptance.get("phase_plan_evidence")
    phase_budget_evidence_is_bound = (
        isinstance(phase_budget_evidence, Mapping)
        and isinstance(phase_plan_evidence, Mapping)
        and phase_plan_evidence.get("phase_budgets") == phase_budget_evidence
    )

    authored_ramp_stages = {
        name.removeprefix("ramp_").removesuffix("_budgets")
        for name in allowed_ramp_budget_checks
    }
    ramp_evidence = acceptance.get("capacity_ramp_evidence")
    ramp_evidence_checks = (
        ramp_evidence.get("checks") if isinstance(ramp_evidence, Mapping) else None
    )
    failed_ramp_stages = {
        stage
        for stage in authored_ramp_stages
        if isinstance(ramp_evidence_checks, Mapping)
        and ramp_evidence_checks.get(f"ramp_{stage}_budgets") is False
    }
    terminal_status_failure_seen = False
    terminal_status_phase_names: set[str] = set()
    terminal_status_ramp_stages: set[str] = set(closed_terminal_ramp_stages)
    terminal_status_check_names = frozenset(
        {
            "contract",
            "logical_outcome_consistency",
            "raw_outcome_consistency",
            "unexpected_statuses",
            "logical_timing_complete",
            "raw_http_timing_complete",
        }
    )
    terminal_phase_status_check_names = frozenset(
        {
            "logical_outcome_consistent",
            "logical_timing_complete",
            "raw_outcome_consistent",
            "raw_http_timing_complete",
        }
    )
    if allow_closed_terminal_status_failure:
        ramp_evidence_stages = (
            ramp_evidence.get("stages")
            if isinstance(ramp_evidence, Mapping)
            else None
        )
        if isinstance(ramp_evidence_stages, Mapping):
            allowed_ramp_status_failures = frozenset(
                {
                    "status_counts_matches_expected_statuses",
                    "final_status_counts_matches_expected_statuses",
                    "raw_unexpected_statuses_zero",
                }
            )
            for stage_name, stage_evidence in ramp_evidence_stages.items():
                stage_checks = (
                    stage_evidence.get("checks")
                    if isinstance(stage_evidence, Mapping)
                    else None
                )
                failed_stage_checks = (
                    {
                        name
                        for name, value in stage_checks.items()
                        if value is False
                    }
                    if isinstance(stage_checks, Mapping)
                    else set()
                )
                if (
                    failed_stage_checks
                    and failed_stage_checks.issubset(allowed_ramp_status_failures)
                    and failed_stage_checks.intersection(
                        {
                            "status_counts_matches_expected_statuses",
                            "final_status_counts_matches_expected_statuses",
                        }
                    )
                ):
                    terminal_status_ramp_stages.add(str(stage_name))

    def check_tree(node: Any, path: tuple[str, ...] = ()) -> tuple[bool, bool]:
        nonlocal terminal_status_failure_seen
        budget_failure = False
        if isinstance(node, Mapping):
            nested_checks = node.get("checks")
            if nested_checks is not None:
                if not isinstance(nested_checks, Mapping):
                    return False, False
                check_path = path + ("checks",)
                for name, value in nested_checks.items():
                    if value is False:
                        ramp_stage_budget_check = (
                            len(check_path) == 5
                            and check_path[0] == "capacity_ramp_evidence"
                            and check_path[1] == "stages"
                            and check_path[2] in failed_ramp_stages
                            and check_path[3] == "budget_evidence"
                            and name in _RAMP_STAGE_BUDGET_CHECKS
                        )
                        pending_observer_leaf = (
                            allow_pending_observer_binding
                            and (
                                (
                                    pending_observer_is_closed
                                    and check_path == ("checks",)
                                    and name == "observer_binding"
                                )
                                or (
                                    pending_observer_is_closed
                                    and
                                    check_path == ("observer_binding", "checks")
                                    and name in pending_observer_checks
                                )
                                or (
                                    len(check_path) == 3
                                    and check_path[0] == "phase_slo"
                                    and check_path[1] in allowed_phase_names
                                    and check_path[2] == "checks"
                                    and name == "observer_binding"
                                    and pending_phase_observer_is_closed(check_path[1])
                                )
                                or (
                                    len(check_path) == 4
                                    and check_path[0] == "phase_slo"
                                    and check_path[1] in allowed_phase_names
                                    and check_path[2:] == ("observer_binding", "checks")
                                    and name in pending_observer_checks
                                    and pending_phase_observer_is_closed(check_path[1])
                                )
                            )
                        )
                        if pending_observer_leaf:
                            continue
                        terminal_status_leaf = (
                            allow_closed_terminal_status_failure
                            and (
                                check_path == ("checks",)
                                and name in terminal_status_check_names
                                or (
                                    len(check_path) == 3
                                    and check_path[0] == "phase_budget_evidence"
                                    and check_path[1] in allowed_phase_names
                                    and check_path[2] == "checks"
                                    and name in terminal_phase_status_check_names
                                )
                                or (
                                    len(check_path) == 4
                                    and check_path[:2]
                                    == ("phase_plan_evidence", "phase_budgets")
                                    and check_path[2] in allowed_phase_names
                                    and check_path[3] == "checks"
                                    and name in terminal_phase_status_check_names
                                )
                                or (
                                    len(check_path) == 5
                                    and check_path[:2]
                                    == ("capacity_ramp_evidence", "stages")
                                    and check_path[3:] == ("budget_evidence", "checks")
                                    and name in terminal_phase_status_check_names
                                )
                                or (
                                    len(check_path) == 4
                                    and check_path[:2]
                                    == ("capacity_ramp_evidence", "stages")
                                    and check_path[3] == "checks"
                                    and check_path[2] in terminal_status_ramp_stages
                                    and name in allowed_ramp_status_failures
                                )
                            )
                        )
                        if terminal_status_leaf:
                            terminal_status_failure_seen = True
                            if (
                                len(check_path) == 3
                                and check_path[0] == "phase_budget_evidence"
                            ):
                                terminal_status_phase_names.add(check_path[1])
                            elif (
                                len(check_path) == 4
                                and check_path[:2]
                                == ("phase_plan_evidence", "phase_budgets")
                            ):
                                terminal_status_phase_names.add(check_path[2])
                            elif (
                                len(check_path) == 5
                                and check_path[:2]
                                == ("capacity_ramp_evidence", "stages")
                            ):
                                terminal_status_ramp_stages.add(check_path[2])
                            elif (
                                len(check_path) == 4
                                and check_path[:2]
                                == ("capacity_ramp_evidence", "stages")
                            ):
                                terminal_status_ramp_stages.add(check_path[2])
                            continue
                        top_level_budget_leaf = (
                            check_path == ("checks",)
                            and (
                                (
                                    name in allowed
                                    and name not in _ORIGIN_RESOURCE_BUDGET_CHECKS
                                )
                                or name in allowed_ramp_budget_checks
                            )
                        )
                        phase_budget_leaf = (
                            (
                                len(check_path) == 3
                                and check_path[0] == "phase_budget_evidence"
                                and check_path[1] in allowed_phase_names
                                and check_path[2] == "checks"
                                or len(check_path) == 4
                                and check_path[0:2]
                                == ("phase_plan_evidence", "phase_budgets")
                                and check_path[2] in allowed_phase_names
                                and check_path[3] == "checks"
                            )
                            and name in _PHASE_TARGET_BUDGET_CHECKS
                            and phase_budget_evidence_is_bound
                        )
                        origin_budget_leaf = (
                            check_path == ("origin_safety", "checks")
                            and name in _ORIGIN_RESOURCE_BUDGET_CHECKS
                        )
                        ramp_budget_leaf = (
                            check_path == ("capacity_ramp_evidence", "checks")
                            and name in allowed_ramp_budget_checks
                        )
                        ramp_population_leaf = (
                            check_path == ("capacity_ramp_evidence", "checks")
                            and isinstance(name, str)
                            and name.startswith("ramp_")
                            and name.endswith("_population")
                            and name.removeprefix("ramp_").removesuffix("_population")
                            in terminal_status_ramp_stages
                        )
                        if (
                            top_level_budget_leaf
                            or phase_budget_leaf
                            or origin_budget_leaf
                            or ramp_budget_leaf
                            or ramp_population_leaf
                            or ramp_stage_budget_check
                        ):
                            budget_failure = True
                        elif check_path == ("checks",) and name in _BUDGET_AGGREGATE_CHECKS:
                            evidence_key = {
                                "phase_budgets": "phase_budget_evidence",
                                "capacity_ramp_evidence": "capacity_ramp_evidence",
                                "origin_safety": "origin_safety",
                            }[str(name)]
                            evidence = node.get(evidence_key)
                            if not isinstance(evidence, Mapping):
                                return False, False
                            valid, nested_budget_failure = check_tree(
                                evidence, path + (evidence_key,)
                            )
                            if not valid or not (
                                nested_budget_failure
                                or terminal_status_phase_names
                                or evidence_key == "capacity_ramp_evidence"
                                and terminal_status_ramp_stages
                            ):
                                return False, False
                            budget_failure = True
                        else:
                            return False, False
                    elif value is not True:
                        return False, False
            for key, value in node.items():
                if key in {"checks", "passed", "complete", "target_passed"}:
                    continue
                if isinstance(value, Mapping):
                    children = [(str(key), value)]
                elif isinstance(value, (list, tuple)):
                    children = [(str(key), child) for child in value]
                else:
                    children = []
                for child_key, child in children:
                    valid, child_budget_failure = check_tree(
                        child, path + (child_key,)
                    )
                    if not valid:
                        return False, False
                    budget_failure = budget_failure or child_budget_failure
            for status_key in (
                "passed",
                "complete",
                "target_passed",
                "experiment_complete",
                "phase_completion",
            ):
                if status_key not in node:
                    continue
                status = node[status_key]
                if type(status) is not bool:
                    return False, False
                if status is not False:
                    continue
                missing_observer_status = (
                    status_key == "complete"
                    and path == ("observer_binding",)
                    and allow_pending_observer_binding
                    and pending_observer_is_closed
                )
                ramp_budget_status = (
                    status_key == "complete"
                    and path == ("capacity_ramp_evidence",)
                    and (
                        bool(failed_ramp_stages) and budget_failure
                        or bool(terminal_status_ramp_stages)
                    )
                )
                terminal_ramp_status = (
                    status_key == "complete"
                    and len(path) == 3
                    and path[:2] == ("capacity_ramp_evidence", "stages")
                    and path[2] in terminal_status_ramp_stages
                )
                root_pending_status = (
                    status_key == "passed"
                    and path == ()
                    and (budget_failure or allow_no_budget_failure)
                )
                phase_pending_status = (
                    status_key == "passed"
                    and len(path) == 2
                    and path[0] == "phase_slo"
                    and path[1] in allowed_phase_names
                    and allow_pending_observer_binding
                    and pending_phase_observer_is_closed(path[1])
                    and (budget_failure or allow_no_budget_failure)
                )
                nested_budget_status = (
                    status_key == "passed"
                    and budget_failure
                    and (
                        (len(path) == 2 and path[0] == "phase_budget_evidence")
                        or (
                            len(path) == 3
                            and path[:2]
                            == ("phase_plan_evidence", "phase_budgets")
                        )
                        or (
                            len(path) == 4
                            and path[:2]
                            == ("capacity_ramp_evidence", "stages")
                            and path[3] == "budget_evidence"
                        )
                        or path == ("origin_safety",)
                    )
                )
                terminal_phase_status = (
                    status_key == "passed"
                    and allow_closed_terminal_status_failure
                    and (
                        len(path) == 2
                        and path[0] == "phase_budget_evidence"
                        and path[1] in terminal_status_phase_names
                        or len(path) == 3
                        and path[:2]
                        == ("phase_plan_evidence", "phase_budgets")
                        and path[2] in terminal_status_phase_names
                    )
                )
                terminal_ramp_budget_status = (
                    status_key == "passed"
                    and allow_closed_terminal_status_failure
                    and len(path) == 4
                    and path[:2] == ("capacity_ramp_evidence", "stages")
                    and path[3] == "budget_evidence"
                    and path[2] in terminal_status_ramp_stages
                )
                if not (
                    missing_observer_status
                    or ramp_budget_status
                    or root_pending_status
                    or phase_pending_status
                    or nested_budget_status
                    or terminal_phase_status
                    or terminal_ramp_status
                    or terminal_ramp_budget_status
                ):
                    return False, False
            return True, budget_failure
        return True, False

    valid, false_budget_seen = check_tree(acceptance)
    if not valid:
        return False
    if allow_closed_terminal_status_failure and not terminal_status_failure_seen:
        return False
    return false_budget_seen or allow_no_budget_failure


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
    run_parser.add_argument(
        "--defer-pending-origin",
        action="store_true",
        help=(
            "external-workflow orchestration only: return reserved exit 3 for a "
            "fully closed, exact-bound candidate awaiting origin evidence; this "
            "is not an acceptance pass"
        ),
    )

    evaluate_parser = subparsers.add_parser("evaluate")
    evaluate_parser.add_argument("--profile", dest="profile_id", required=True)
    evaluate_parser.add_argument("--report", type=Path, required=True)
    evaluate_parser.add_argument("--server-observability", type=Path)
    evaluate_parser.add_argument(
        "--defer-completed-slo-failure",
        action="store_true",
        help=(
            "external-workflow orchestration only: return reserved exit 3 for a "
            "fully complete observer-bound SLO failure so sanitized evidence can "
            "be published before the final workflow gate fails"
        ),
    )
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
        return evaluate_report(
            profile,
            args.report,
            args.server_observability,
            defer_completed_slo_failure=args.defer_completed_slo_failure,
        )
    return run_profile(
        profile,
        args.manifest,
        args.report_path,
        timeout_diagnostics_run_id=args.timeout_diagnostics_run_id,
        defer_pending_origin=args.defer_pending_origin,
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
